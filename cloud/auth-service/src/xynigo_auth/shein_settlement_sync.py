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

import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable

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
    floor_to_second as _floor_second,
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
EXPENDITURE_EXPENSE = 2

# 待结算是「全部 checkStatus=1」，不是「最近一批」——所以取数每次都往回翻满
# 整个区间。「连续空窗就停」是错的：空窗不构成见底依据（见 _fetch_pending_batches）。
# 正确性由「最早那个窗口是否还有账单」来收口：还有 → 判不完整并显式失败。
DEFAULT_CHECK_ORDER_LOOKBACK_DAYS = 180   # 回溯上限（达到上限仍未见底 = 数据不完整）
MAX_CHECK_ORDER_PAGES_PER_WINDOW = 100    # 单窗口翻页上限
MAX_ORDER_LIST_PAGES_PER_WINDOW = 200     # 订单列表单窗口翻页上限
# 历史累计已结算要全量翻页；设上限是为了避免异常数据下无限翻，
# 真触发说明该店历史报账单量超预期，宁可显式失败也不要静默少算。
MAX_REPORT_ORDER_PAGES = 200


class SheinSettlementSyncError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class SheinSettlementSyncBusy(SheinSettlementSyncError):
    """同一租户已有同步在跑：手动刷新与定时任务共用一个闸门，避免同店并发取数。"""

    def __init__(self) -> None:
        super().__init__(
            "shein_settlement_sync_busy",
            "已有同步任务进行中，请稍后再试",
            status_code=409,
        )


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


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _as_decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


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
        check_order_lookback_days: int = DEFAULT_CHECK_ORDER_LOOKBACK_DAYS,
        fx_fetcher: Callable[[], FxRateQuote] | None = None,
    ) -> None:
        self.client = client
        self.cipher = cipher
        self.order_lookback_days = order_lookback_days
        self.check_order_lookback_days = check_order_lookback_days
        # None = 不自动取汇率（测试默认）；生产装配显式传 fetch_frankfurter_rates
        self.fx_fetcher = fx_fetcher

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
        # 水位优选用快照时间：安静期增量窗没有订单可写，max(last_synced_at) 不会
        # 前进，窗口会一轮轮变长直到重新打满 48h 分片（多打平台）。快照每轮都写。
        #
        # **但只认 status == "ok" 的快照**：失败快照写在 SAVEPOINT 之外、synced_at
        # 是本轮 now，而本轮拉到的台账更新已被 SAVEPOINT 回滚。拿它当水位会把
        # [上次成功, 本轮 − 重叠] 这段窗口整个跳过去——期间发生的签收/新发货再也
        # 不会出现在任何窗口里，在途长期偏离真值。
        snapshot_watermark = session.execute(
            select(func.max(SheinSettlementSnapshot.synced_at)).where(
                SheinSettlementSnapshot.tenant_id == store.tenant_id,
                SheinSettlementSnapshot.store_id == store.id,
                SheinSettlementSnapshot.status == "ok",
            )
        ).scalar()
        # 空店（台账 0 行）同样接受成功快照水位：否则一个从未出过单的店每轮都
        # 走一次 60 天全量回溯，白打几十次订单窗。
        if snapshot_watermark is not None and (
            last_synced is None
            or _as_platform_tz(snapshot_watermark) > _as_platform_tz(last_synced)
        ):
            last_synced = snapshot_watermark

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
                if len(items) < 30:
                    break
                page += 1
                if page > MAX_ORDER_LIST_PAGES_PER_WINDOW:
                    raise SheinSettlementSyncError(
                        "shein_settlement_order_pages_exceeded",
                        f"{store.store_name}：订单列表单窗口翻页超过 "
                        f"{MAX_ORDER_LIST_PAGES_PER_WINDOW} 页，可能被截断；"
                        "为避免少算在途已中止本店同步",
                    )

        # 本轮窗口没有订单**不代表台账里没有在途**：增量窗口只有 2 小时重叠，
        # 平台没返回更新单是常态。在途一律回读台账求和——早期版本在这里
        # `return 0, None`，导致每 6 小时的增量同步把看板的在途整段清空。
        shipped = [
            order_no for order_no, item in seen.items()
            if int(item.get("orderStatus") or 0) == ORDER_STATUS_SHIPPED
        ]
        amounts = self._fetch_order_amounts(
            store=store, secret=secret, order_nos=shipped)
        # 在途单拿不到预计收入时必须整店失败：SQL SUM 会跳过 NULL，静默把该店
        # 在途算少（极端情况显示 0），而少算不会报警。
        missing = [no for no in shipped
                   if amounts.get(no, {}).get("amount") is None]
        if missing:
            raise SheinSettlementSyncError(
                "shein_settlement_order_amount_missing",
                f"{store.store_name}：{len(missing)} 笔在途订单未返回预计收入"
                f"（如 {missing[0]}），无法计入在途；为避免少算已中止本店同步",
            )

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
        # 真机（20260919）：order-detail 的 info 直接是明细数组，无包裹键；
        # 币种字段为 orderCurrency（currencyCode/currency 作旧格式兼容）。
        if isinstance(payload, list):
            rows = [item for item in payload if isinstance(item, dict)]
        else:
            rows = _rows(payload, "orderList", "list")
        result: dict[str, dict[str, Any]] = {}
        for item in rows:
            order_no = str(item.get("orderNo") or "").strip()
            if not order_no:
                continue
            result[order_no] = {
                "amount": _as_decimal(
                    item.get("estimatedGrossIncome")
                    or item.get("estimateGrossIncome")),
                "currency": str(item.get("orderCurrency")
                                or item.get("currencyCode")
                                or item.get("currency") or ""),
            }
        return result

    def _sum_in_transit(
        self, session: Session, *, store: SheinAuthorizedStore,
    ) -> Decimal | None:
        # 先扫脏行：历史版本可能留下 order_status=4 但金额为 NULL 的行（SUM 会跳过
        # NULL → 该店在途静默算少）。只在求和前扫一次，增量空窗也能兜住。
        dirty = session.execute(
            select(func.count()).select_from(SheinSettlementOrder).where(
                SheinSettlementOrder.tenant_id == store.tenant_id,
                SheinSettlementOrder.store_id == store.id,
                SheinSettlementOrder.order_status == ORDER_STATUS_SHIPPED,
                SheinSettlementOrder.estimated_gross_income.is_(None),
            )
        ).scalar()
        if dirty:
            raise SheinSettlementSyncError(
                "shein_settlement_in_transit_amount_missing",
                f"{store.store_name}：台账中有 {dirty} 笔在途订单缺少预计收入，"
                "无法计入在途；为避免少算已中止本店同步",
            )
        total = session.execute(
            select(func.coalesce(func.sum(
                SheinSettlementOrder.estimated_gross_income), 0)).where(
                SheinSettlementOrder.tenant_id == store.tenant_id,
                SheinSettlementOrder.store_id == store.id,
                SheinSettlementOrder.order_status == ORDER_STATUS_SHIPPED,
            )
        ).scalar()
        return to_cent(Decimal(total)) if total is not None else None

    def _fetch_pending_batches(
        self, *, store: SheinAuthorizedStore, secret: str, now: datetime,
    ) -> tuple[dict[tuple[date, str], Decimal], bool]:
        """把**全部** checkStatus=1 的待结算按 (打款日, 币种) 聚合。

        返回 (buckets, complete)。`complete=False` 表示最早那个窗口里仍有待结算
        账单——即可能还有更早的没翻到，调用方必须显式失败：静默少算待结算比
        同步失败严重得多。

        **为什么每次都回溯完整区间、不用"连续空窗就停"**：空窗不构成见底依据。
        一张 40 天前生成、至今未结算的账单，前面隔着若干空窗（那段时间没生成新
        账单），连续空窗的启发式会在够到它之前就停下 → 永久少算。回溯上限设为
        足够长（默认 180 天）并用"最早窗口是否为空"这个精确判据收口。

        去重按对账单号：分片窗口是左闭右开的，但**平台的开闭语义未在真机确认**，
        若平台两端闭合，落在边界秒的账单会同时出现在相邻两个窗口里；按单号去重
        可让两种语义都不翻倍（重复拉到只是覆盖，不会重复计）。
        """
        buckets: dict[tuple[date, str], Decimal] = {}
        seen_orders: set[tuple[str, int]] = set()
        # 窗口从下界（now - lookback）向上切，**最早那个窗口的起点恰好等于下界**。
        # 这是判据成立的前提：先前按"从 now 往回切"的写法，最早窗口永远不贴着
        # 下界，于是"最早窗口是否还有账单"永远为假——判据成了死代码。
        limit = _floor_second(now) - timedelta(
            days=self.check_order_lookback_days)
        windows = slice_time_windows(limit, _floor_second(now),
                                     CHECK_ORDER_MAX_SPAN)
        for index, (window_start, window_end) in enumerate(windows):
            rows = self._fetch_check_order_window(
                store=store, secret=secret,
                start=window_start, end=window_end)
            if rows and index == 0:
                # 贴着下界的那一片还有未结账单 → 一定判不完整、整店会失败。
                # 立刻返回，不再往后打二十几个窗口白耗平台配额。
                return {}, False
            for item in rows:
                self._accumulate_pending_row(
                    item, buckets=buckets, seen_orders=seen_orders,
                    store=store)
        return buckets, True

    def _fetch_check_order_window(
        self, *, store: SheinAuthorizedStore, secret: str,
        start: datetime, end: datetime,
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self.client.query_check_orders(
                open_key_id=store.open_key_id, secret_key=secret,
                start_add_time=format_platform_time(start),
                end_add_time=format_platform_time(end),
                page=page, page_size=30,
                check_status=CHECK_STATUS_PENDING,
            )
            items = _rows(payload, "list")
            collected.extend(items)
            if len(items) < 30:
                break
            page += 1
            if page > MAX_CHECK_ORDER_PAGES_PER_WINDOW:
                # 与订单列表/报账单一致：**超限即失败**。原先是静默 break，
                # 截断结果仍会被当成"见底"，随后全删重建 → 正是第 2 项要防的
                # 「看着完整、实际少算」。
                raise SheinSettlementSyncError(
                    "shein_settlement_check_order_pages_exceeded",
                    f"{store.store_name}：对账单单窗口翻页超过 "
                    f"{MAX_CHECK_ORDER_PAGES_PER_WINDOW} 页，可能被截断；"
                    "为避免少算待结算已中止本店同步",
                )
        return collected

    def _accumulate_pending_row(
        self, item: dict[str, Any], *,
        buckets: dict[tuple[date, str], Decimal],
        seen_orders: set[tuple[str, int]],
        store: SheinAuthorizedStore,
    ) -> None:
        """把一条对账单并入待结算桶；关键字段缺失即整店失败。

        缺字段时**不能 continue**：那是一条真实的待结算金额，悄悄丢掉会让看板少算，
        而少算不会报警——财务只会看到一个偏小的数。
        """
        order_no = str(item.get("checkOrderNo") or "").strip()
        pay_date = _pay_date(item.get("estimatePayTime"))
        currency = str(item.get("currencyCode") or "").strip()
        amount = _as_decimal(item.get("estimateIncomeMoneyTotal"))
        # 单号是去重键，缺了就不能去重——分片边界秒被相邻两片各返回一次时金额会翻倍。
        # 所以它和其他关键字段同等对待：缺一即整店失败，不能 `if order_no:` 绕过去。
        if not order_no or pay_date is None or not currency or amount is None:
            raise SheinSettlementSyncError(
                "shein_settlement_check_order_incomplete",
                f"{store.store_name}：对账单缺少关键字段"
                f"（单号 {order_no or '缺失'}、打款日 {item.get('estimatePayTime')!r}、"
                f"币种 {currency!r}、金额 {item.get('estimateIncomeMoneyTotal')!r}），"
                "无法计入待结算；为避免少算或重复计已中止本店同步",
            )
        # 支出行取负：对账单按收支轧差，支出与收入成对出现。
        # 收支类型缺失或非法**不能默认成收入**：`int(x or 1)` 会把 0/None 都当收入，
        # 一笔真实的支出被算成正数，轧差即错。缺类型与缺单号同等对待。
        raw_kind = item.get("incomeExpenditureType")
        try:
            kind = int(raw_kind)
        except (TypeError, ValueError):
            kind = 0
        if kind not in (EXPENDITURE_INCOME, EXPENDITURE_EXPENSE):
            raise SheinSettlementSyncError(
                "shein_settlement_check_order_incomplete",
                f"{store.store_name}：对账单收支类型非法（单号 {order_no}、"
                f"取值为 {raw_kind!r}）；无法判断收入或支出，为避免轧差错算已中止本店同步",
            )
        # 去重键带收支类型：真机上同一单号是否会同时出现收入行与支出行尚未确认，
        # 只按单号去重会把成对出现的其中一行吃掉，轧差就错了。
        dedup_key = (order_no, kind)
        if dedup_key in seen_orders:
            return
        seen_orders.add(dedup_key)
        signed = abs(amount) if kind == EXPENDITURE_INCOME else -abs(amount)
        key = (pay_date, currency)
        buckets[key] = buckets.get(key, Decimal("0")) + signed

    def _collect_payout_batches(
        self, session: Session, *, store: SheinAuthorizedStore, secret: str,
        now: datetime,
    ) -> tuple[PayoutBatch, ...]:
        """取全部待结算并按预计打款日分批，然后**整体替换**该店批次。

        只有确认回溯见底（complete）才做替换：窗口不完整时删旧写新会把上一轮
        已经正确的待结算抹掉，而且看起来像"这批钱结算完了"。
        """
        buckets, complete = self._fetch_pending_batches(
            store=store, secret=secret, now=now)
        if not complete:
            raise SheinSettlementSyncError(
                "shein_settlement_check_orders_incomplete",
                f"{store.store_name}：待结算回溯超过 "
                f"{self.check_order_lookback_days} 天仍未见底，"
                "可能存在更早的未结算账单；为避免少算已中止本店同步",
            )

        session.execute(delete(SheinPayoutBatch).where(
            SheinPayoutBatch.tenant_id == store.tenant_id,
            SheinPayoutBatch.store_id == store.id,
        ))
        batches: list[PayoutBatch] = []
        for (pay_date, currency), amount in sorted(buckets.items()):
            net = to_cent(amount)
            session.add(SheinPayoutBatch(
                tenant_id=store.tenant_id, store_id=store.id,
                pay_date=pay_date, currency=currency, amount=net,
                synced_at=now,
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

    def active_run(
        self, session: Session, *, tenant_id: uuid.UUID,
        stale_after_seconds: int = 2 * 60 * 60,
        now: datetime | None = None,
    ) -> SheinSettlementSyncRun | None:
        """当前是否已有进行中的同步（超过 stale 阈值的视为异常中断、不再阻塞）。

        SQLite 里时间列读回来是 naive，比较前统一按平台时区解释——否则
        `TypeError: can't compare offset-naive and offset-aware datetimes`，
        或错误地把新记录判成"还没开始"。
        """
        moment = now or datetime.now(PLATFORM_TZ)
        rows = session.execute(
            select(SheinSettlementSyncRun).where(
                SheinSettlementSyncRun.tenant_id == tenant_id,
                SheinSettlementSyncRun.status == "running",
            ).order_by(SheinSettlementSyncRun.started_at.desc())
        ).scalars().all()
        for row in rows:
            age = (moment - _as_platform_tz(row.started_at)).total_seconds()
            if age < max(60, int(stale_after_seconds)):
                return row
        return None

    def recover_stale_runs(
        self, session: Session, *, tenant_id: uuid.UUID,
        stale_after_seconds: int = 2 * 60 * 60,
        now: datetime | None = None,
    ) -> int:
        """把异常中断（进程被杀等）留下的 running 记录标为 failed，避免永久占闸门。"""
        moment = now or datetime.now(PLATFORM_TZ)
        rows = session.execute(
            select(SheinSettlementSyncRun).where(
                SheinSettlementSyncRun.tenant_id == tenant_id,
                SheinSettlementSyncRun.status == "running",
            )
        ).scalars().all()
        recovered = 0
        for row in rows:
            age = (moment - _as_platform_tz(row.started_at)).total_seconds()
            if age >= max(60, int(stale_after_seconds)):
                row.status = "failed"
                row.finished_at = moment
                detail = dict(row.detail or {})
                detail["recovered"] = "同步超过预期时长仍未结束，已标记为失败"
                row.detail = detail
                recovered += 1
        if recovered:
            session.flush()
        return recovered

    def run(
        self, session: Session, *, tenant_id: uuid.UUID, trigger: str = "manual",
        store_ids: list[uuid.UUID] | None = None,
        user_id: uuid.UUID | None = None, now: datetime | None = None,
        stale_after_seconds: int = 2 * 60 * 60,
    ) -> SyncRunOutcome:
        """跑一轮同步。逐店串行、按店隔离失败（并发留到有限流实测数据后再开）。"""
        moment = now or datetime.now(PLATFORM_TZ)
        # 先把异常中断的 running 记录收掉，否则进程被杀后手动刷新会一直 409
        self.recover_stale_runs(
            session, tenant_id=tenant_id,
            stale_after_seconds=stale_after_seconds, now=moment)
        # 同店互斥：手动刷新与定时任务共用这一个闸门
        if self.active_run(
            session, tenant_id=tenant_id,
            stale_after_seconds=stale_after_seconds, now=moment,
        ) is not None:
            raise SheinSettlementSyncBusy()
        # 汇率先行（整轮一次）：取数失败不阻塞店铺同步——库内旧汇率继续顶上
        # （current_fx_rates 取 ≤ 今天的最新一条），失败原因记入 run.detail["fx"]。
        # 快照写入时会把当时生效的汇率固化进 fx_rate_snapshot，历史值不会漂。
        fx_note: dict[str, str] = {"status": "skipped"}
        if self.fx_fetcher is not None:
            try:
                quote = self.fx_fetcher()
            except Exception as exc:  # noqa: BLE001 - 汇率失败不算同步失败
                fx_note = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }
            else:
                for iso, rate in quote.rates.items():
                    upsert_fx_rate(
                        session, tenant_id=tenant_id, currency=iso, rate=rate,
                        effective_date=quote.rate_date, source=FX_RATE_SOURCE)
                fx_note = {
                    "status": "ok",
                    "rateDate": quote.rate_date.isoformat(),
                    "currencies": str(len(quote.rates)),
                }
                session.commit()
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
        # 两个时钟不能混用：started_at 取自 moment（可能是注入的测试时钟），
        # finished_at 若取真实当前时间，"到期才跑"的判断就会在测试里失控（也会在
        # 时钟回拨等场景下算错时长）。注入时钟时两者都取注入值。
        run.finished_at = datetime.now(PLATFORM_TZ) if now is None else moment
        run.status = (
            "succeeded" if failed == 0
            else ("failed" if ok == 0 else "partial")
        )
        run.detail = {
            "fx": fx_note,
            **{
                item.store_name: {
                    "status": item.status,
                    "error": item.error_summary,
                    "orders": item.orders_written,
                }
                for item in outcomes
            },
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
            store_ok = bool(snapshot and snapshot.status == "ok")
            # 失败店的历史批次必须整批丢掉：单店失败走 SAVEPOINT，上一轮成功写入的
            # 批次会留在库里。若照挂不误，卡片虽剔除该店、明细与导出却仍显示过期的
            # 待结算——两边数字对不上，而且看起来像"这家店还在正常出数"。
            effective_batches = batches if store_ok else []
            store_currency = (
                (snapshot.currency if snapshot else "")
                or (effective_batches[0].currency if effective_batches else "")
            )
            if currency and currency != store_currency:
                continue
            inputs.append(StoreSettlementInput(
                store_id=str(store.id), store_name=store.store_name,
                mode=store.mode, currency=store_currency,
                status="ok" if store_ok else "fail",
                error_summary=(
                    snapshot.error_summary if snapshot else "尚未同步"
                ) or "尚未同步",
                in_transit_amount=(
                    Decimal(snapshot.in_transit_amount) if store_ok
                    and snapshot.in_transit_amount is not None else None),
                settled_cumulative_amount=(
                    Decimal(snapshot.settled_cumulative_amount) if store_ok
                    and snapshot.settled_cumulative_amount is not None else None),
                payout_batches=tuple(
                    PayoutBatch(pay_date=row.pay_date, currency=row.currency,
                                amount=Decimal(row.amount))
                    for row in effective_batches),
                payment_method=snapshot.payment_method if snapshot else None,
                synced_at=snapshot.synced_at if snapshot else None,
            ))
        return build_settlement_summary(
            inputs, self.current_fx_rates(session, tenant_id=tenant_id))


def upsert_fx_rate(
    session: Session, *, tenant_id: uuid.UUID, currency: str, rate: Decimal,
    effective_date: date, source: str = "manual",
) -> SheinFxRate:
    """写入/更新某日汇率。frankfurter 自动取数在 run() 开头整轮执行一次。"""
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


class SheinSettlementSyncWorker:
    """结算看板定时同步：每 N 小时对到期租户跑一轮（默认 6 小时）。

    设计要点：
    - **到期才跑**：以该租户最近一次成功/部分成功的同步时间为准，未到间隔就跳过。
      这样容器重启不会重复同步，也不会因为多进程/重复调度而多打平台。
    - **与手动刷新共用闸门**：真正开跑前仍由 service.run() 做同店互斥，两路撞车
      时后来的那路收到 busy 并安静跳过（不记失败、不影响下一周期）。
    - **单租户失败不影响其他租户**：循环内逐租户兜异常。
    - 线程 + stop 事件，与仓内既有 worker（feishu_operation_sync / purchase_sync）同构。
    """

    def __init__(
        self, *, session_factory, service: SheinSettlementSyncService,
        interval_seconds: int = 6 * 60 * 60,
        stale_after_seconds: int = 2 * 60 * 60,
        initial_delay_seconds: int = 60,
        log=None,
    ) -> None:
        self.session_factory = session_factory
        self.service = service
        self.interval_seconds = max(60, int(interval_seconds))
        self.stale_after_seconds = max(60, int(stale_after_seconds))
        self.initial_delay_seconds = max(0, int(initial_delay_seconds))
        self._log = log or (lambda message: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        if self._stop.wait(self.initial_delay_seconds):
            return
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - 循环不能因单轮异常退出
                self._log(f"settlement sync worker run failed: {type(exc).__name__}")
            if self._stop.wait(self.interval_seconds):
                return

    def due_tenants(self, session: Session, *, now: datetime) -> list[uuid.UUID]:
        """到期（距上次成功同步已超过 interval）的租户列表。"""
        tenant_ids = [
            row for row in session.execute(
                select(SheinAuthorizedStore.tenant_id).where(
                    SheinAuthorizedStore.status != "expired",
                ).distinct()
            ).scalars().all()
        ]
        due: list[uuid.UUID] = []
        for tenant_id in tenant_ids:
            last = session.execute(
                select(func.max(SheinSettlementSyncRun.finished_at)).where(
                    SheinSettlementSyncRun.tenant_id == tenant_id,
                    SheinSettlementSyncRun.status.in_(("succeeded", "partial")),
                )
            ).scalar()
            if last is None:
                due.append(tenant_id)
                continue
            age = (now - _as_platform_tz(last)).total_seconds()
            if age >= self.interval_seconds:
                due.append(tenant_id)
        return due

    def run_once(self, *, now: datetime | None = None) -> int:
        """跑一轮到期租户；返回实际执行的租户数（便于测试与日志）。"""
        moment = now or datetime.now(PLATFORM_TZ)
        executed = 0
        with self.session_factory() as session:
            try:
                due = self.due_tenants(session, now=moment)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                self._log(f"settlement sync worker lookup failed: {type(exc).__name__}")
                return 0
        for tenant_id in due:
            with self.session_factory() as session:
                try:
                    self.service.recover_stale_runs(
                        session, tenant_id=tenant_id,
                        stale_after_seconds=self.stale_after_seconds, now=moment,
                    )
                    outcome = self.service.run(
                        session, tenant_id=tenant_id, trigger="schedule",
                        now=moment, stale_after_seconds=self.stale_after_seconds,
                    )
                    session.commit()
                except SheinSettlementSyncBusy:
                    # 手动刷新正在跑同一租户——跳过即可，不是错误
                    session.rollback()
                    continue
                except Exception as exc:  # noqa: BLE001 - 单租户失败不影响其他租户
                    session.rollback()
                    self._log(
                        f"settlement sync worker tenant failed: {type(exc).__name__}")
                    continue
                executed += 1
                self._log(
                    f"settlement sync done tenant={tenant_id} status={outcome.status} "
                    f"ok={outcome.store_ok} failed={outcome.store_failed}")
        return executed


# ---- 汇率自动取数（Jeff 20260919 拍板：frankfurter 做唯一来源）----
# frankfurter.app（开源项目 github.com/lineofflight/frankfurter）免费、无需 Key，
# 数据源为欧洲央行每日参考汇率。以 USD 为基准报价，兑人民币按交叉价计算：
#   1 MXN ≈ rates.CNY / rates.MXN
# 与中行折算价交叉验证差约 0.7%，作为参考看板足够。取不到的币种看板显示
# 「—」（不按 1:1 计入），这是刻意的失败保护。

FRANKFURTER_LATEST_URL = "https://api.frankfurter.dev/v1/latest"
FX_RATE_SOURCE = "frankfurter"


@dataclass(frozen=True)
class FxRateQuote:
    """一次汇率取数的结果。

    rates[ISO] = 人民币/1 外币（CNY 恒为 1）；rate_date 是行情所属日期
    （ECB 参考汇率日，周末停在周五），用作 shein_fx_rates.effective_date。
    """

    rates: dict[str, Decimal]
    rate_date: date


def parse_frankfurter_quote(payload: dict[str, Any]) -> FxRateQuote:
    """从 frankfurter latest 响应提取兑人民币汇率。

    纯函数，便于测试。frankfurter 以 USD 为基准（rates.CNY=6.6976 表示
    1 USD = 6.6976 CNY），其余币种按交叉价换算。响应缺 CNY 或金额非法时
    返回空 rates——聚合层对缺汇率的币种显示「—」，绝不按 1:1 静默计入。
    """
    rate_date = date.today()
    date_raw = str(payload.get("date") or "").strip()
    try:
        rate_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
    except ValueError:
        pass
    rates_raw = payload.get("rates")
    if not isinstance(rates_raw, dict):
        return FxRateQuote(rates={}, rate_date=rate_date)
    cny_per_usd = _positive_decimal(rates_raw.get("CNY"))
    if cny_per_usd is None:
        return FxRateQuote(rates={}, rate_date=rate_date)
    rates = {"CNY": Decimal("1"), "USD": cny_per_usd}
    for iso, per_usd_raw in rates_raw.items():
        if iso in ("CNY", "USD"):
            continue
        per_usd = _positive_decimal(per_usd_raw)
        if per_usd is None:
            continue
        rates[iso] = (cny_per_usd / per_usd).quantize(Decimal("0.00001"))
    return FxRateQuote(rates=rates, rate_date=rate_date)


def fetch_frankfurter_rates(
    *, timeout_seconds: float = 20.0,
) -> FxRateQuote:
    """从 frankfurter 拉取最新汇率（同步 httpx，与 SHEIN 客户端同风格）。

    独立函数便于测试注入。网络失败或非 200 抛 SheinSettlementSyncError——
    由调用方决定降级策略（沿用库内旧汇率），这里不做静默兜底。
    """
    import httpx as _httpx
    try:
        with _httpx.Client(timeout=timeout_seconds) as client:
            resp = client.get(
                FRANKFURTER_LATEST_URL,
                params={"base": "USD"},
                headers={"User-Agent": "xynigo-settlement/1.0"},
            )
    except _httpx.HTTPError as exc:
        raise SheinSettlementSyncError(
            "shein_fx_frankfurter_fetch_failed",
            f"frankfurter 请求失败：{type(exc).__name__}",
        ) from exc
    if resp.status_code != 200:
        raise SheinSettlementSyncError(
            "shein_fx_frankfurter_fetch_failed",
            f"frankfurter 返回 HTTP {resp.status_code}",
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise SheinSettlementSyncError(
            "shein_fx_frankfurter_fetch_failed",
            "frankfurter 响应不是合法 JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise SheinSettlementSyncError(
            "shein_fx_frankfurter_fetch_failed",
            "frankfurter 响应结构异常",
        )
    return parse_frankfurter_quote(payload)
