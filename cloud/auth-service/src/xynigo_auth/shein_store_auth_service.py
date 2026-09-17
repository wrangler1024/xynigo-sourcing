"""SHEIN store authorization business logic.

安全红线（规划文档 §3.4）：
- secretKey 仅以密文落库；响应、事件、日志只出现掩码；
- tempToken 只在换钥调用期间存在于内存；
- state 一次性、绑定租户与发起人、短时效，回调消费后即失效。
"""

from __future__ import annotations

import base64
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import SheinAuthEvent, SheinAuthLink, SheinAuthorizedStore
from .shein_openapi_client import (
    SheinOpenApiClient,
    SheinOpenApiClientError,
    TEMP_TOKEN_EXPIRED_CODE,
)
from .shein_store_auth_crypto import (
    SheinStoreAuthCryptoError,
    SheinStoreSecretCipher,
)

LINK_TTL_MINUTES = 15
STATE_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
EVENT_LIMIT = 20


class SheinStoreAuthError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def mask_open_key_id(open_key_id: str) -> str:
    value = str(open_key_id or "")
    if len(value) <= 12:
        return value[:4] + "****"
    return f"{value[:8]}****{value[-4:]}"


def _as_utc(value: datetime) -> datetime:
    """SQLite 测试库读回 naive 时间；比较前统一规范成 UTC。"""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# query-store-info 只保留白名单字段入库：平台若以后返回敏感字段，
# 不会以明文 JSON 躺在库里（评审后补项）。
STORE_INFO_FIELDS = (
    "merchantId", "merchant_id", "storeName", "store_name",
    "storeCode", "store_code", "site", "region", "currency",
)


def _filtered_store_info(store_info: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (store_info or {}).items()
        if key in STORE_INFO_FIELDS
    }


def _mask_temp_token(token: str) -> str:
    value = str(token or "")
    if len(value) <= 8:
        return "****"
    return f"{value[:2]}****{value[-2:]}"




def _store_payload(store: SheinAuthorizedStore) -> dict[str, Any]:
    return {
        "id": str(store.id),
        "name": store.store_name,
        "mode": store.mode,
        "merchantId": store.merchant_id,
        "openKeyIdMasked": mask_open_key_id(store.open_key_id),
        "appId": store.app_id,
        "firstAuthorizedAt": store.first_authorized_at.isoformat(),
        "latestAuthorizedAt": store.latest_authorized_at.isoformat(),
        "lastVerifiedAt": (
            store.last_verified_at.isoformat() if store.last_verified_at else None
        ),
        "status": store.status,
    }


class SheinStoreAuthService:
    def __init__(
        self,
        *,
        cipher: SheinStoreSecretCipher,
        client: SheinOpenApiClient,
        empower_host: str,
        redirect_base: str,
    ) -> None:
        self.cipher = cipher
        self.client = client
        self.empower_host = str(empower_host or "").strip("/")
        self.redirect_base = str(redirect_base or "").strip()

    # ---- 事件 ----

    def _event(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID | None,
        action: str,
        store_id: uuid.UUID | None,
        store_label: str,
        ok: bool,
        note: str,
    ) -> None:
        session.add(
            SheinAuthEvent(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action=action,
                store_id=store_id,
                store_label=store_label[:128],
                ok=ok,
                note=note[:300],
            )
        )

    # ---- 授权链接 ----

    def create_link(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        mode: str,
        target_store_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        if mode not in ("self", "semi"):
            raise SheinStoreAuthError("shein_auth_mode_invalid", "店铺类型无效", 422)
        if not self.client.configured or not self.redirect_base:
            raise SheinStoreAuthError(
                "shein_auth_not_configured",
                "SHEIN 开放平台应用凭证未配置，请联系管理员检查部署配置",
                503,
            )
        target_label = "（新店铺）"
        if target_store_id is not None:
            target = self._require_store(session, tenant_id, target_store_id)
            target_label = target.store_name
        state = "xy" + "".join(
            secrets.choice(STATE_ALPHABET) for _ in range(30)
        )
        now = datetime.now(UTC)
        link = SheinAuthLink(
            tenant_id=tenant_id,
            created_by_user_id=user_id,
            state=state,
            mode=mode,
            app_id=self.client.app_id,
            target_store_id=target_store_id,
            expires_at=now + timedelta(minutes=LINK_TTL_MINUTES),
        )
        session.add(link)
        self._event(
            session,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            action="link",
            store_id=target_store_id,
            store_label=target_label,
            ok=True,
            note=f"生成{'SHEIN 自营' if mode == 'self' else 'SHEIN 半托管'}"
                 f"授权链接（{LINK_TTL_MINUTES} 分钟内有效）",
        )
        session.flush()
        redirect_b64 = base64.b64encode(self.redirect_base.encode()).decode()
        url = (
            f"https://{self.empower_host}/#/empower"
            f"?appid={self.client.app_id}&redirectUrl={redirect_b64}&state={state}"
        )
        return {
            "url": url,
            "state": state,
            "mode": mode,
            "expiresAt": link.expires_at.isoformat(),
        }

    # ---- 回调换钥 ----

    def _claim_link(self, session: Session, state: str) -> SheinAuthLink:
        """原子消费 state（评审必须改 #2）。

        先短事务 UPDATE ... WHERE consumed_at IS NULL，rowcount 判定胜者；
        并发回调只有一个能拿到（串行 409 由既有分支覆盖，失败也计入一次性）。
        换钥的联网调用放在事务外，避免长时间持行锁。
        """
        link = session.scalar(
            select(SheinAuthLink).where(SheinAuthLink.state == str(state))
        )
        if link is None:
            session.rollback()
            raise SheinStoreAuthError(
                "shein_auth_state_unknown", "授权链接无效或已过期", 404
            )
        now = datetime.now(UTC)
        claimed = session.execute(
            update(SheinAuthLink)
            .where(
                SheinAuthLink.id == link.id,
                SheinAuthLink.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        ).rowcount
        session.commit()
        if not claimed:
            raise SheinStoreAuthError(
                "shein_auth_state_reused", "授权链接已使用，请重新生成", 409
            )
        if now > _as_utc(link.expires_at):
            raise SheinStoreAuthError(
                "shein_auth_state_expired", "授权链接已过期，请重新生成", 410
            )
        return link

    def _match_store_row(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        mode: str,
        target_store_id: uuid.UUID | None,
        open_key_id: str,
        merchant_id: str,
    ) -> SheinAuthorizedStore | None:
        """绑定匹配（评审必须改 #1，禁止用 open_key_id[:16] 冒充商家ID）：

        1) 重新授权链接带目标店 → 直接定位该行；
        2) 按 openKeyId（换钥必返且同店同应用稳定）匹配；
        3) 平台给出真实商家ID 时按其 + 模式匹配（openKeyId 轮换场景兜底）。
        """
        if target_store_id is not None:
            record = session.get(SheinAuthorizedStore, target_store_id)
            if record is not None and record.tenant_id == tenant_id:
                return record
        record = session.scalar(
            select(SheinAuthorizedStore).where(
                SheinAuthorizedStore.tenant_id == tenant_id,
                SheinAuthorizedStore.open_key_id == open_key_id,
            )
        )
        if record is not None:
            return record
        if merchant_id:
            return session.scalar(
                select(SheinAuthorizedStore).where(
                    SheinAuthorizedStore.tenant_id == tenant_id,
                    SheinAuthorizedStore.merchant_id == merchant_id,
                    SheinAuthorizedStore.mode == mode,
                )
            )
        return None

    def complete_callback(
        self,
        session: Session,
        *,
        temp_token: str,
        state: str,
    ) -> dict[str, Any]:
        link = self._claim_link(session, state)
        link_tenant_id = link.tenant_id
        link_actor_id = link.created_by_user_id
        link_target_id = link.target_store_id
        link_mode = link.mode
        link_app_id = link.app_id

        failure: SheinOpenApiClientError | None = None
        open_key_id = secret_key = ""
        try:
            open_key_id, secret_key = self.client.exchange_temp_token(temp_token)
        except SheinOpenApiClientError as exc:
            failure = exc
        if failure is not None:
            self._event(
                session,
                tenant_id=link_tenant_id,
                actor_user_id=link_actor_id,
                action="callback",
                store_id=link_target_id,
                store_label="（换钥失败）",
                ok=False,
                note=f"tempToken {_mask_temp_token(temp_token)} 换钥失败："
                     f"{failure.code}",
            )
            session.commit()
            if failure.code == TEMP_TOKEN_EXPIRED_CODE:
                raise SheinStoreAuthError(
                    "shein_temp_token_expired",
                    "平台临时令牌已过期（10 分钟内有效），请重新生成授权链接",
                    410,
                ) from failure
            # 平台原文留在服务端（事件/异常内），HTTP 只回稳定错误码与固定文案。
            raise SheinStoreAuthError(
                "shein_token_exchange_failed",
                "换取店铺密钥失败，请重新生成授权链接",
                502,
            ) from failure

        store_info: dict[str, Any] = {}
        try:
            store_info = self.client.query_store_info(
                open_key_id=open_key_id, secret_key=secret_key
            )
        except SheinOpenApiClientError:
            store_info = {}  # 换钥成功但店铺信息接口暂时失败：不阻断绑定

        merchant_id = str(
            store_info.get("merchantId")
            or store_info.get("merchant_id")
            or ""
        ).strip()
        platform_name = str(
            store_info.get("storeName")
            or store_info.get("store_name")
            or ""
        ).strip()

        now = datetime.now(UTC)
        record = self._match_store_row(
            session,
            tenant_id=link_tenant_id,
            mode=link_mode,
            target_store_id=link_target_id,
            open_key_id=open_key_id,
            merchant_id=merchant_id,
        )
        try:
            ciphertext = self.cipher.encrypt_secret(secret_key)
        except SheinStoreAuthCryptoError as exc:
            raise SheinStoreAuthError(
                "shein_store_encrypt_failed", "店铺密钥无法加密保存", 503
            ) from exc
        if record is None:
            record = SheinAuthorizedStore(
                tenant_id=link_tenant_id,
                merchant_id=merchant_id,  # 允许暂缺（空串），验证连接成功时回填
                store_name=(
                    platform_name
                    or (f"店铺 {merchant_id}" if merchant_id else "待命名店铺")
                ),
                open_key_id=open_key_id,
                secret_ciphertext=ciphertext,
                app_id=link_app_id,
                mode=link_mode,
                first_authorized_at=now,
                latest_authorized_at=now,
                status="pending",
                store_info=_filtered_store_info(store_info),
                created_by_user_id=link_actor_id,
            )
            session.add(record)
            event_note = "首次授权成功，密钥已加密保存"
        else:
            record.open_key_id = open_key_id
            record.secret_ciphertext = ciphertext
            record.app_id = link_app_id
            record.latest_authorized_at = now
            record.status = "pending"
            record.store_info = _filtered_store_info(store_info)
            if merchant_id:
                record.merchant_id = merchant_id
            if platform_name:
                record.store_name = platform_name
            event_note = "重新授权成功，密钥已轮换（旧密钥失效）"
        try:
            session.flush()
        except IntegrityError:
            # 并发插入同一 openKeyId：回滚后按唯一键改走更新路径（评审后补项）。
            session.rollback()
            record = session.scalar(
                select(SheinAuthorizedStore).where(
                    SheinAuthorizedStore.tenant_id == link_tenant_id,
                    SheinAuthorizedStore.open_key_id == open_key_id,
                )
            )
            if record is None:
                raise SheinStoreAuthError(
                    "shein_store_conflict", "店铺授权写入冲突，请重试", 409
                )
            record.secret_ciphertext = ciphertext
            record.latest_authorized_at = now
            record.status = "pending"
            if merchant_id:
                record.merchant_id = merchant_id
            if platform_name:
                record.store_name = platform_name
            session.flush()
            event_note = "重新授权成功，密钥已轮换（旧密钥失效）"
        self._event(
            session,
            tenant_id=link_tenant_id,
            actor_user_id=link_actor_id,
            action="callback",
            store_id=record.id,
            store_label=record.store_name,
            ok=True,
            note=event_note,
        )
        session.commit()
        return {"store": _store_payload(record), "ok": True}

    # ---- 店铺操作 ----

    def _require_store(
        self, session: Session, tenant_id: uuid.UUID, store_id: uuid.UUID
    ) -> SheinAuthorizedStore:
        record = session.get(SheinAuthorizedStore, store_id)
        if record is None or record.tenant_id != tenant_id:
            raise SheinStoreAuthError(
                "shein_store_not_found", "授权店铺不存在", 404
            )
        return record

    def list_stores(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        mode: str = "",
        keyword: str = "",
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, Any]:
        conditions = [SheinAuthorizedStore.tenant_id == tenant_id]
        if mode in ("self", "semi"):
            conditions.append(SheinAuthorizedStore.mode == mode)
        kw = str(keyword or "").strip().lower()
        if kw:
            like = f"%{kw}%"
            conditions.append(
                func.lower(SheinAuthorizedStore.store_name).like(like)
                | SheinAuthorizedStore.merchant_id.like(like)
            )
        total = session.scalar(
            select(func.count()).select_from(SheinAuthorizedStore).where(*conditions)
        ) or 0
        rows = session.scalars(
            select(SheinAuthorizedStore)
            .where(*conditions)
            .order_by(SheinAuthorizedStore.latest_authorized_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return {
            "items": [_store_payload(row) for row in rows],
            "total": int(total),
            "page": page,
            "pageSize": page_size,
        }

    def verify_store(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        store_id: uuid.UUID,
    ) -> dict[str, Any]:
        record = self._require_store(session, tenant_id, store_id)
        try:
            secret_key = self.cipher.decrypt_secret(record.secret_ciphertext)
        except SheinStoreAuthCryptoError as exc:
            raise SheinStoreAuthError(
                "shein_store_secret_unreadable", "店铺密钥暂时无法解密", 503
            ) from exc
        try:
            store_info = self.client.query_store_info(
                open_key_id=record.open_key_id, secret_key=secret_key
            )
            record.status = "ok"
            record.store_info = _filtered_store_info(store_info)
            note = "店铺信息接口验证通过"
            ok = True
            # 验证成功回填商家ID / 店铺名（首绑时店铺信息接口未必可用）；
            # 若同租户同模式已存在同一商家ID 的重复行（历史裂行），合并到本行。
            verified_merchant = str(
                store_info.get("merchantId")
                or store_info.get("merchant_id")
                or ""
            ).strip()
            verified_name = str(
                store_info.get("storeName")
                or store_info.get("store_name")
                or ""
            ).strip()
            if verified_merchant:
                duplicate = session.scalar(
                    select(SheinAuthorizedStore).where(
                        SheinAuthorizedStore.tenant_id == tenant_id,
                        SheinAuthorizedStore.merchant_id == verified_merchant,
                        SheinAuthorizedStore.mode == record.mode,
                        SheinAuthorizedStore.id != record.id,
                    )
                )
                record.merchant_id = verified_merchant
                if duplicate is not None:
                    duplicate_name = duplicate.store_name
                    session.delete(duplicate)
                    note = f"验证通过；合并重复授权行「{duplicate_name}」"
            if verified_name and (
                not record.store_name
                or record.store_name.startswith("待命名")
                or record.store_name.startswith("店铺 ")
            ):
                record.store_name = verified_name
        except SheinOpenApiClientError as exc:
            record.status = "expired"
            note = f"验证失败：{exc.code}"[:300]
            ok = False
        record.last_verified_at = datetime.now(UTC)
        self._event(
            session,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            action="verify",
            store_id=record.id,
            store_label=record.store_name,
            ok=ok,
            note=note,
        )
        session.commit()
        return _store_payload(record) | {"verifiedOk": ok}

    def rename_store(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        store_id: uuid.UUID,
        name: str,
    ) -> dict[str, Any]:
        record = self._require_store(session, tenant_id, store_id)
        value = str(name or "").strip()
        if not value or len(value) > 128:
            raise SheinStoreAuthError(
                "shein_store_name_invalid", "店铺名称无效", 422
            )
        old = record.store_name
        record.store_name = value
        self._event(
            session,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            action="rename",
            store_id=record.id,
            store_label=f"{old} → {value}",
            ok=True,
            note="修改店铺显示名称",
        )
        session.commit()
        return _store_payload(record)

    def delete_store(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        store_id: uuid.UUID,
    ) -> dict[str, Any]:
        record = self._require_store(session, tenant_id, store_id)
        label = record.store_name
        session.delete(record)
        self._event(
            session,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            action="delete",
            store_id=None,
            store_label=label,
            ok=True,
            note="删除店铺授权（密钥密文一并删除）",
        )
        session.commit()
        return {"deleted": True}

    def list_events(
        self, session: Session, *, tenant_id: uuid.UUID
    ) -> dict[str, Any]:
        rows = session.scalars(
            select(SheinAuthEvent)
            .where(SheinAuthEvent.tenant_id == tenant_id)
            .order_by(SheinAuthEvent.created_at.desc())
            .limit(EVENT_LIMIT)
        ).all()
        names = {
            "link": "生成链接",
            "callback": "授权回调",
            "verify": "验证连接",
            "rename": "修改名称",
            "delete": "删除授权",
        }
        return {
            "items": [
                {
                    "id": str(row.id),
                    "at": row.created_at.isoformat(),
                    "action": row.action,
                    "actionLabel": names.get(row.action, row.action),
                    "store": row.store_label,
                    "ok": row.ok,
                    "note": row.note,
                }
                for row in rows
            ]
        }
