# -*- coding: utf-8 -*-
"""结算看板同步编排：从 SHEIN 网关取数 → 落库 → 组装看板数据。

分层：
- 取数规则（哪些窗口、怎么分片）在 `shein_settlement_windows`（纯函数）
- 汇总口径（四指标、按打款日分批、折算）在 `shein_settlement_service`（纯函数）
- 本模块只做**编排与落库**：把上面两者接到 SHEIN 网关和数据库上

设计要点：
1. **按店铺隔离失败**：一家店报错不影响其他店；失败原因写进快照，前端显示为
   「同步失败」。这是看板可信度的前提——不能因为一家店挂了整批没数据。
2. **幂等**：所有写入按唯一键 upsert，重复跑不产生重复数据。
3. **订单台账只对"当前在途单"拉金额**：订单列表只回单号/状态/时间，金额要再调
   订单详情。逐单拉详情在 60 天回溯下是几百次调用，所以先按状态过滤，
   只对 orderStatus=4（已发货未签收）的单拉金额。
   —— 台账仍记录全部订单的状态（列表接口很便宜），只是不拉不需要的金额。
4. **金额一律 Decimal**：浮点累加会把长尾写进库与导出。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import (
    SheinAuthorizedStore,
    SheinFxRate,
    SheinPayoutBatch,
    SheinSettlementOrder,
    SheinSettlementSnapshot,
    SheinSettlementSyncRun,
)
from .shein_openapi_client import (
    ORDER_DETAIL_MAX_BATCH,
    SheinOpenApiClient,
    SheinOpenApiClientError,
)
from .shein_settlement_service import (
    PayoutBatch,
    SettlementSummary,
    StoreSettlementInput,
    build_settlement_summary,
    to_cent,
)
from .shein_settlement_windows import (
    DEFAULT_ORDER_BACKFILL_DAYS,
    PLATFORM_TZ,
    build_order_backfill_windows,
    build_order_incremental_windows,
    format_platform_time,
    slice_time_windows,
    CHECK_ORDER_MAX_SPAN,
)
from .shein_store_auth_crypto import SheinStoreSecretCipher

# 订单状态：4=已发货未签收（在途口径的来源）
ORDER_STATUS_SHIPPED = 4
# 对账单状态：1=待生成付款/待结算
CHECK_STATUS_PENDING = 1
# 报账单状态：2=已付款（历史累计已结算的来源）
REPORT_STATUS_PAID = 2
# 收支类型：1=收入 2=支出（对账单按轧差取净额）
EXPENDITURE_INCOME = 1

# 常规刷新只需覆盖最近的待结算批次；更早的批次早已进报账单。
DEFAULT_CHECK_ORDER_DAYS_BACK = 21
# 历史累计已结算要全量翻页；设上限是为了避免异常数据下无限翻，
# 真触发说明该店历史报账单量超预期，宁可显式失败也不要静默少算。
MAX_REPORT_ORDER_PAGES = 200


class SheinSettlementSyncError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class StoreSyncOutcome:
    """单店同步结果；status=fail 时金额与批次为空并带原因。"""

    store_id: uuid.UUID
    store_name: str
    status: str
    currency: str = ""
    error_summary: str = ""
    in_transit_amount: Decimal | None = None
    settled_cumulative_amount: Decimal | None = None
    batches: tuple[PayoutBatch, ...] = ()
    payment_method: int | None = None
    receiver_account: str = ""
    orders_written: int = 0


@dataclass
class SyncRunOutcome:
    run_id: uuid.UUID
    status: str
    store_total: int
    store_ok: int
    store_failed: int
    stores: list[StoreSyncOutcome] = field(default_factory=list)


def _as_platform_tz(value: datetime) -> datetime:
    """把从库里读回的时间统一成带时区。

    必须显式归一，且**naive 要按平台时区（UTC+8）解释，不能按 UTC**：
    - SQLite 没有原生时区类型，写入时偏移量被丢掉，读回来的是**上海墙上时间**
      （本模块写入的都是 PLATFORM_TZ 的 now）；
    - 若误按 UTC 解释，时间点会被推后 8 小时，导致
      `start(上次同步) >= end(现在)` → 增量窗口为空 → 整轮同步"没有数据可拉"，
      在途金额被写空。这个 bug 在真机上不一定复现（Postgres 返回带时区、
      不做转换），只在 SQLite 环境暴露。
    - Postgres 读回本就带时区，此处是 no-op。
    """
    return value if value.tzinfo is not None else value.replace(
        tzinfo=PLATFORM_TZ)


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _parse_platform_time(value: Any) -> datetime | None:
    """平台时间 `yyyy-MM-dd HH:mm:ss`（UTC+8）→ 带时区 datetime。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=PLATFORM_TZ)
        except ValueError:
            continue
    return None


def _pay_date(value: Any) -> date | None:
    parsed = _parse_platform_time(value)
    return parsed.date() if parsed else None


def _rows(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    """列表接口的条目数组名各接口不同（list / orderList），按序取第一个命中的。"""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


class SheinSettlementSyncService:
    """同步编排。client 与 cipher 可注入，便于合成测试。"""

    def __init__(
        self,
        *,
        client: SheinOpenApiClient,
        cipher: SheinStoreSecretCipher,
        order_lookback_days: int = DEFAULT_ORDER_BACKFILL_DAYS,
        check_order_days_back: int = DEFAULT_CHECK_ORDER_DAYS_BACK,
    ) -> None:
        self.client = client
        self.cipher = cipher
        self.order_lookback_days = order_lookback_days
        self.check_order_days_back = check_order_days_back

    # ------------------------------------------------------------------
    # 取数
    # ------------------------------------------------------------------

    def _secret_of(self, store: SheinAuthorizedStore) -> str:
        return self.cipher.decrypt_secret(store.secret_ciphertext)

    def _sync_order_ledger(
        self, session: Session, *, store: SheinAuthorizedStore, secret: str,
        now: datetime,
    ) -> tuple[int, Decimal | None]:
        """拉订单列表更新台账状态，再只对在途单拉金额。返回 (写入行数, 在途合计)。"""
        last_synced = session.execute(
            select(func.max(SheinSettlementOrder.last_synced_at)).where(
                SheinSettlementOrder.tenant_id == store.tenant_id,
                SheinSettlementOrder.store_id == store.id,
            )
        ).scalar()

        if last_synced is None:
            # 首次：向前回溯建账。48h 窗口拉不全"所有已发货未签收"——
            # 5 天前发货、这两天没更新过的单不会出现在窗口里。
            windows = build_order_backfill_windows(
                now, lookback_days=self.order_lookback_days)
        else:
            windows = build_order_incremental_windows(
                _as_platform_tz(last_synced), now)

        seen: dict[str, dict[str, Any]] = {}
        for start, end in windows:
            page = 1
            while True:
                payload = self.client.query_orders(
                    open_key_id=store.open_key_id, secret_key=secret,
                    query_type=2,
                    start_time=format_platform_time(start),
                    end_time=format_platform_time(end),
                    page=page, page_size=30,
                )
                items = _rows(payload, "orderList", "list")
                for item in items:
                    order_no = str(item.get("orderNo") or "").strip()
                    if not order_no:
                        continue
                    # 同一单可能跨窗口多次出现，后写覆盖先写（窗口已按时间升序）
                    seen[order_no] = item
                if len(items) < 30 or page >= MAX_REPORT_ORDER_PAGES:
                    break
                page += 1

        if not seen:
            return 0, None

        shipped = [
            order_no for order_no, item in seen.items()
            if int(item.get("orderStatus") or 0) == ORDER_STATUS_SHIPPED
        ]
        amounts = self._fetch_order_amounts(
            store=store, secret=secret, order_nos=shipped)

        synced_at = now
        written = 0
        currency = ""
        for order_no, item in seen.items():
            status = int(item.get("orderStatus") or 0)
            amount = amounts.get(order_no)
            row = session.execute(
                select(SheinSettlementOrder).where(
                    SheinSettlementOrder.tenant_id == store.tenant_id,
                    SheinSettlementOrder.store_id == store.id,
                    SheinSettlementOrder.order_no == order_no,
                )
            ).scalar_one_or_none()
            if row is None:
                row = SheinSettlementOrder(
                    tenant_id=store.tenant_id, store_id=store.id,
                    order_no=order_no, order_status=status,
                    currency="", last_synced_at=synced_at,
                )
                session.add(row)
            row.order_status = status
            row.order_time = _parse_platform_time(item.get("orderCreateTime"))
            row.update_time = _parse_platform_time(item.get("orderUpdateTime"))
            row.last_synced_at = synced_at
            if amount is not None:
                # amount 是 {"amount": Decimal|None, "currency": str}
                row.estimated_gross_income = amount["amount"]
                row.currency = str(amount.get("currency") or row.currency or "")
                currency = currency or row.currency
            written += 1

        session.flush()
        return written, self._sum_in_transit(session, store=store)

    def _fetch_order_amounts(
        self, *, store: SheinAuthorizedStore, secret: str,
        order_nos: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        """按 ≤30 单/次批量取订单详情里的预计收入。"""
        collected: dict[str, dict[str, Any]] = {}
        batch: list[str] = []
        for order_no in order_nos:
            batch.append(order_no)
            if len(batch) >= ORDER_DETAIL_MAX_BATCH:
                collected.update(self._fetch_one_detail_batch(
                    store=store, secret=secret, order_nos=batch))
                batch = []
        if batch:
            collected.update(self._fetch_one_detail_batch(
                store=store, secret=secret, order_nos=batch))
        return collected

    def _fetch_one_detail_batch(
        self, *, store: SheinAuthorizedStore, secret: str, order_nos: list[str],
    ) -> dict[str, dict[str, Any]]:
        payload = self.client.query_order_details(
            open_key_id=store.open_key_id, secret_key=secret,
            order_nos=order_nos,
        )
        result: dict[str, dict[str, Any]] = {}
        for item in _rows(payload, "orderList", "list"):
            order_no = str(item.get("orderNo") or "").strip()
            if not order_no:
                continue
            result[order_no] = {
                "amount": _as_decimal(
                    item.get("estimatedGrossIncome")
                    or item.get("estimateGrossIncome")),
                "currency": str(item.get("currencyCode")
                                or item.get("currency") or ""),
            }
        return result

    def _sum_in_transit(
        self, session: Session, *, store: SheinAuthorizedStore,
    ) -> Decimal | None:
        total = session.execute(
            select(func.coalesce(func.sum(
                SheinSettlementOrder.estimated_gross_income), 0)).where(
                SheinSettlementOrder.tenant_id == store.tenant_id,
                SheinSettlementOrder.store_id == store.id,
                SheinSettlementOrder.order_status == ORDER_STATUS_SHIPPED,
            )
        ).scalar()
        return to_cent(Decimal(total)) if total is not None else None

    def _collect_payout_batches(
        self, session: Session, *, store: SheinAuthorizedStore, secret: str,
        now: datetime,
    ) -> tuple[PayoutBatch, ...]:
        """对账单 checkStatus=1 按预计打款日分批（收支轧差净额）。"""
        start = now - timedelta(days=self.check_order_days_back)
        buckets: dict[tuple[date, str], Decimal] = {}
        for window_start, window_end in slice_time_windows(
                start, now, CHECK_ORDER_MAX_SPAN):
            page = 1
            while True:
                payload = self.client.query_check_orders(
                    open_key_id=store.open_key_id, secret_key=secret,
                    start_add_time=format_platform_time(window_start),
                    end_add_time=format_platform_time(window_end),
                    page=page, page_size=30,
                    check_status=CHECK_STATUS_PENDING,
                )
                items = _rows(payload, "list")
                for item in items:
                    pay_date = _pay_date(item.get("estimatePayTime"))
                    currency = str(item.get("currencyCode") or "").strip()
                    amount = _as_decimal(item.get("estimateIncomeMoneyTotal"))
                    if pay_date is None or not currency or amount is None:
                        continue
                    # 支出行取负：对账单按收支轧差，支出与收入成对出现
                    kind = int(item.get("incomeExpenditureType")
                               or EXPENDITURE_INCOME)
                    signed = abs(amount) if kind == EXPENDITURE_INCOME \
                        else -abs(amount)
                    key = (pay_date, currency)
                    buckets[key] = buckets.get(key, Decimal("0")) + signed
                if len(items) < 30:
                    break
                page += 1

        # 快照式覆盖：本次区间是最新事实，旧的批次行删掉重建，避免残留过期批次
        session.execute(delete(SheinPayoutBatch).where(
            SheinPayoutBatch.tenant_id == store.tenant_id,
            SheinPayoutBatch.store_id == store.id,
        ))
        now_ts = now
        batches: list[PayoutBatch] = []
        for (pay_date, currency), amount in sorted(buckets.items()):
            net = to_cent(amount)
            session.add(SheinPayoutBatch(
                tenant_id=store.tenant_id, store_id=store.id,
                pay_date=pay_date, currency=currency, amount=net,
                synced_at=now_ts,
            ))
            batches.append(PayoutBatch(
                pay_date=pay_date, currency=currency, amount=net))
        session.flush()
        return tuple(batches)

    def _collect_settled_cumulative(
        self, *, store: SheinAuthorizedStore, secret: str,
    ) -> tuple[Decimal, str, int | None, str]:
        """报账单 reportStatus=2 全量累计。返回 (累计金额, 币种, 收款方式, 收款账户)。"""
        total = Decimal("0")
        currency = ""
        payment_method: int | None = None
        receiver = ""
        page = 1
        while True:
            payload = self.client.query_report_orders(
                open_key_id=store.open_key_id, secret_key=secret,
                page=page, page_size=30, report_status=REPORT_STATUS_PAID,
            )
            items = _rows(payload, "list")
            for item in items:
                amount = _as_decimal(item.get("income"))
                if amount is not None:
                    total += amount
                currency = currency or str(item.get("currencyCode") or "")
                if payment_method is None and item.get("paymentMethod") is not None:
                    payment_method = int(item["paymentMethod"])
                receiver = receiver or str(item.get("rxAcct") or "")
            if len(items) < 30:
                break
            page += 1
            if page > MAX_REPORT_ORDER_PAGES:
                raise SheinSettlementSyncError(
                    "shein_settlement_report_orders_too_many",
                    f"历史报账单超过 {MAX_REPORT_ORDER_PAGES} 页仍未取完，"
                    "为避免少算已结算金额而中止本次同步",
                )
        return to_cent(total) or Decimal("0"), currency, payment_method, receiver

    def _resolve_currency(
        self, *, store: SheinAuthorizedStore, secret: str, fallback: str,
    ) -> str:
        """币种取站点信息（权威来源）；失败则回退到报账单里的币种。"""
        try:
            payload = self.client.query_site_list(
                open_key_id=store.open_key_id, secret_key=secret)
        except SheinOpenApiClientError:
            return fallback
        for entry in _rows(payload, "data"):
            for sub in entry.get("sub_site_list") or []:
                if isinstance(sub, dict) and sub.get("currency"):
                    return str(sub["currency"])
        return fallback

    # ------------------------------------------------------------------
    # 编排
    # ------------------------------------------------------------------

    def sync_store(
        self, session: Session, *, store: SheinAuthorizedStore, now: datetime,
        run_id: uuid.UUID | None = None,
    ) -> StoreSyncOutcome:
        """同步单店；任何异常都收敛成 fail 结果，不向上抛（隔离到店）。

        单店用 SAVEPOINT（begin_nested）隔离：这家店失败只回滚它自己的写入，
        否则会把同一轮里已成功的店铺、以及本轮的运行记录一起回滚掉。
        """
        outcome: StoreSyncOutcome | None = None
        try:
            with session.begin_nested():
                secret = self._secret_of(store)
                orders_written, in_transit = self._sync_order_ledger(
                    session, store=store, secret=secret, now=now)
                batches = self._collect_payout_batches(
                    session, store=store, secret=secret, now=now)
                settled, report_currency, payment_method, receiver = (
                    self._collect_settled_cumulative(store=store, secret=secret))
                fallback = report_currency or (
                    batches[0].currency if batches else "")
                currency = self._resolve_currency(
                    store=store, secret=secret, fallback=fallback)
                outcome = StoreSyncOutcome(
                    store_id=store.id, store_name=store.store_name,
                    status="ok", currency=currency,
                    in_transit_amount=in_transit,
                    settled_cumulative_amount=settled,
                    batches=batches, payment_method=payment_method,
                    receiver_account=receiver, orders_written=orders_written,
                )
        except SheinOpenApiClientError as exc:
            outcome = StoreSyncOutcome(
                store_id=store.id, store_name=store.store_name,
                status="fail", error_summary=f"接口错误：{exc.message}",
            )
        except SheinSettlementSyncError as exc:
            outcome = StoreSyncOutcome(
                store_id=store.id, store_name=store.store_name,
                status="fail", error_summary=exc.message,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：单店异常不得中断整轮
            outcome = StoreSyncOutcome(
                store_id=store.id, store_name=store.store_name,
                status="fail",
                error_summary=f"同步异常：{type(exc).__name__}",
            )

        self._write_snapshot(
            session, store=store, outcome=outcome, now=now, run_id=run_id)
        return outcome

    def _write_snapshot(
        self, session: Session, *, store: SheinAuthorizedStore,
        outcome: StoreSyncOutcome, now: datetime, run_id: uuid.UUID | None = None,
    ) -> None:
        nearest = None
        nearest_amount = None
        if outcome.batches:
            nearest = min(batch.pay_date for batch in outcome.batches)
            nearest_amount = to_cent(sum(
                batch.amount for batch in outcome.batches
                if batch.pay_date == nearest))
        session.add(SheinSettlementSnapshot(
            tenant_id=store.tenant_id, store_id=store.id,
            sync_run_id=run_id,
            snapshot_date=now.astimezone(PLATFORM_TZ).date(),
            synced_at=now,
            currency=outcome.currency,
            in_transit_amount=outcome.in_transit_amount,
            unsettled_amount=to_cent(sum(
                batch.amount for batch in outcome.batches))
            if outcome.batches else None,
            settled_cumulative_amount=outcome.settled_cumulative_amount,
            nearest_pay_date=nearest,
            nearest_pay_amount=nearest_amount,
            payment_method=outcome.payment_method,
            receiver_account=outcome.receiver_account or "",
            fx_rate_snapshot=self._fx_snapshot(session, tenant_id=store.tenant_id),
            status=outcome.status,
            error_summary=outcome.error_summary[:300],
        ))
        session.flush()

    def _fx_snapshot(
        self, session: Session, *, tenant_id: uuid.UUID,
    ) -> dict[str, str]:
        """把当日汇率固化进快照：否则回看历史时用今天的汇率重算，历史值会漂。"""
        rows = session.execute(
            select(SheinFxRate).where(SheinFxRate.tenant_id == tenant_id)
            .order_by(SheinFxRate.effective_date.desc())
        ).scalars().all()
        picked: dict[str, str] = {}
        for row in rows:
            picked.setdefault(row.currency, str(row.rate))
        return picked

    def run(
        self, session: Session, *, tenant_id: uuid.UUID, trigger: str = "manual",
        store_ids: list[uuid.UUID] | None = None,
        user_id: uuid.UUID | None = None, now: datetime | None = None,
    ) -> SyncRunOutcome:
        """跑一轮同步。逐店串行、按店隔离失败（并发留到有限流实测数据后再开）。"""
        moment = now or datetime.now(PLATFORM_TZ)
        query = select(SheinAuthorizedStore).where(
            SheinAuthorizedStore.tenant_id == tenant_id,
            SheinAuthorizedStore.status != "expired",
        )
        if store_ids:
            query = query.where(SheinAuthorizedStore.id.in_(store_ids))
        stores = session.execute(
            query.order_by(SheinAuthorizedStore.store_name)).scalars().all()

        run = SheinSettlementSyncRun(
            tenant_id=tenant_id, trigger=trigger, status="running",
            started_at=moment, store_total=len(stores),
            created_by_user_id=user_id,
        )
        session.add(run)
        session.flush()

        outcomes: list[StoreSyncOutcome] = []
        for store in stores:
            outcome = self.sync_store(
                session, store=store, now=moment, run_id=run.id)
            outcomes.append(outcome)
            # 每店提交一次：一是同步耗时较长（首次回溯单店几十次调用），
            # 整轮一个事务会长时间占写锁；二是中途中断时已取到的店不该白跑。
            session.commit()

        ok = sum(1 for item in outcomes if item.status == "ok")
        failed = len(outcomes) - ok
        run.store_ok = ok
        run.store_failed = failed
        run.finished_at = datetime.now(PLATFORM_TZ)
        run.status = (
            "succeeded" if failed == 0
            else ("failed" if ok == 0 else "partial")
        )
        run.detail = {
            item.store_name: {
                "status": item.status,
                "error": item.error_summary,
                "orders": item.orders_written,
            }
            for item in outcomes
        }
        session.flush()
        return SyncRunOutcome(
            run_id=run.id, status=run.status, store_total=len(stores),
            store_ok=ok, store_failed=failed, stores=outcomes,
        )

    # ------------------------------------------------------------------
    # 读：组装看板数据
    # ------------------------------------------------------------------

    def current_fx_rates(
        self, session: Session, *, tenant_id: uuid.UUID,
        on_date: date | None = None,
    ) -> dict[str, Decimal]:
        """取每币种在指定日期（默认今天）或之前的最新一条汇率。

        缺汇率的币种不在这里补默认值——聚合层会把它的人民币合计置 None
        （显示「—」），绝不按 1:1 静默计入。
        """
        target = on_date or datetime.now(PLATFORM_TZ).date()
        rows = session.execute(
            select(SheinFxRate).where(
                SheinFxRate.tenant_id == tenant_id,
                SheinFxRate.effective_date <= target,
            ).order_by(SheinFxRate.effective_date.desc())
        ).scalars().all()
        rates: dict[str, Decimal] = {}
        for row in rows:
            rates.setdefault(row.currency, Decimal(row.rate))
        return rates

    def build_summary(
        self, session: Session, *, tenant_id: uuid.UUID,
        currency: str = "",
    ) -> SettlementSummary:
        """从库里的最新事实组装看板数据（不触发任何网络调用）。

        `currency` 传空=全部币种。**筛选放在服务端**：合计口径（尤其"下次结算
        取最近一批"）依赖全局排期，前端按币种自行挑数会在跨币种打款日不同时
        算错最近批次。
        """
        stores = session.execute(
            select(SheinAuthorizedStore).where(
                SheinAuthorizedStore.tenant_id == tenant_id,
                SheinAuthorizedStore.status != "expired",
            ).order_by(SheinAuthorizedStore.store_name)
        ).scalars().all()

        inputs: list[StoreSettlementInput] = []
        for store in stores:
            snapshot = session.execute(
                select(SheinSettlementSnapshot).where(
                    SheinSettlementSnapshot.tenant_id == tenant_id,
                    SheinSettlementSnapshot.store_id == store.id,
                ).order_by(SheinSettlementSnapshot.synced_at.desc())
            ).scalars().first()
            batches = session.execute(
                select(SheinPayoutBatch).where(
                    SheinPayoutBatch.tenant_id == tenant_id,
                    SheinPayoutBatch.store_id == store.id,
                ).order_by(SheinPayoutBatch.pay_date)
            ).scalars().all()
            store_currency = (snapshot.currency if snapshot else "") or (
                batches[0].currency if batches else "")
            if currency and currency != store_currency:
                continue
            inputs.append(StoreSettlementInput(
                store_id=str(store.id), store_name=store.store_name,
                mode=store.mode, currency=store_currency,
                status="ok" if snapshot and snapshot.status == "ok" else "fail",
                error_summary=(
                    snapshot.error_summary if snapshot else "尚未同步"
                ) or "尚未同步",
                in_transit_amount=(
                    Decimal(snapshot.in_transit_amount) if snapshot
                    and snapshot.in_transit_amount is not None else None),
                settled_cumulative_amount=(
                    Decimal(snapshot.settled_cumulative_amount) if snapshot
                    and snapshot.settled_cumulative_amount is not None else None),
                payout_batches=tuple(
                    PayoutBatch(pay_date=row.pay_date, currency=row.currency,
                                amount=Decimal(row.amount))
                    for row in batches),
                payment_method=snapshot.payment_method if snapshot else None,
                synced_at=snapshot.synced_at if snapshot else None,
            ))
        return build_settlement_summary(
            inputs, self.current_fx_rates(session, tenant_id=tenant_id))


def upsert_fx_rate(
    session: Session, *, tenant_id: uuid.UUID, currency: str, rate: Decimal,
    effective_date: date, source: str = "manual",
) -> SheinFxRate:
    """写入/更新某日汇率。中行牌价的自动取数待接入（口径见需求文档 §6）。"""
    row = session.execute(
        select(SheinFxRate).where(
            SheinFxRate.tenant_id == tenant_id,
            SheinFxRate.currency == currency,
            SheinFxRate.effective_date == effective_date,
        )
    ).scalar_one_or_none()
    if row is None:
        row = SheinFxRate(
            tenant_id=tenant_id, currency=currency,
            effective_date=effective_date, rate=rate, source=source,
        )
        session.add(row)
    else:
        row.rate = rate
        row.source = source
    session.flush()
    return row
