import base64
import hashlib
import logging
import re
import uuid
from contextlib import asynccontextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from .config import Settings
from .database import Database
from .feishu import (
    DirectoryClient,
    DirectoryProviderError,
    FeishuDirectoryClient,
    FeishuDirectoryUser,
    FeishuIdentity,
    FeishuOAuthClient,
    OAuthClient,
    OAuthProviderError,
)
from .models import (
    AuditEvent,
    LocalLoginRequest,
    OAuthLoginAttempt,
    Permission,
    Role,
    RolePermission,
    SessionRecord,
    Tenant,
    User,
    UserRole,
)
from .purchase_contract import PurchaseDraft
from .purchase_service import PurchaseOrderService, PurchaseServiceError
from .security import hash_token, pkce_challenge, random_url_token


logger = logging.getLogger(__name__)


SUPER_ADMIN_ROLE = "super_admin"
ADMIN_ROLE = "admin"
MEMBER_ROLE = "member"
LOCAL_LOGIN_COMPLETE_SCRIPT = """(() => {
  const status = document.getElementById('status');
  window.setTimeout(() => {
    window.close();
    window.setTimeout(() => {
      if (!window.closed && status) {
        status.textContent = '登录已完成。浏览器未允许自动关闭，请手动关闭此页并返回 Xynigo。';
      }
    }, 300);
  }, 120);
})();"""
LOCAL_LOGIN_COMPLETE_SCRIPT_HASH = base64.b64encode(
    hashlib.sha256(LOCAL_LOGIN_COMPLETE_SCRIPT.encode("utf-8")).digest()
).decode("ascii")
PERMISSION_CATALOG: tuple[tuple[str, str], ...] = (
    ("workbench.access", "工作台访问"),
    ("procurement.access", "采购中心访问"),
    ("operations.access", "运营中心访问"),
    ("finance.access", "财务中心访问"),
    ("assistant.access", "小犀助手访问"),
    ("analytics.access", "数据分析访问"),
    ("system.member.manage", "成员管理"),
    ("system.role.manage", "角色管理"),
    ("system.lark_connection.manage", "飞书连接管理"),
    ("system.integration.manage", "外部服务管理"),
    ("system.audit.read", "审计日志查看"),
    ("resource.buyer.read", "买家号查看"),
    ("resource.buyer.credential.read", "买家号敏感凭证查看"),
    ("resource.buyer.import", "买家号入库"),
    ("resource.environment.create", "采购环境创建"),
    ("resource.store.read", "店铺资源查看"),
    ("resource.store.configure", "店铺环境配置"),
    ("resource.store.credential.update", "店铺账号凭证更新"),
    ("resource.store.clone", "店铺环境克隆"),
    ("resource.ip.read", "代理 IP 查看"),
    ("resource.ip.test", "代理 IP 检测"),
    ("resource.ip.allocate", "代理 IP 分配"),
    ("resource.ip.credential.manage", "代理 IP 凭证管理"),
    ("fulfillment.order.read", "履约订单查看"),
    ("fulfillment.order.export", "履约订单导出"),
    ("procurement.request.read", "运营采购单查看"),
    ("procurement.request.save", "运营采购单草稿保存"),
    ("procurement.request.submit", "运营采购单正式提交"),
    ("procurement.execution.manage", "采购任务认领与分单管理"),
)
SUPER_ADMIN_ONLY_PERMISSIONS = frozenset({
    "system.lark_connection.manage",
    "system.integration.manage",
    "resource.ip.credential.manage",
})
SYSTEM_MANAGED_PERMISSION_ROLES = frozenset({SUPER_ADMIN_ROLE, ADMIN_ROLE})


@dataclass(frozen=True)
class AdminActor:
    session_record: SessionRecord
    user: User
    tenant: Tenant


class RolePermissionsBody(BaseModel):
    permissionCodes: list[str] = Field(default_factory=list, max_length=100)


class RoleWriteBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class MemberRolesBody(BaseModel):
    roleIds: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class MemberInviteLookupBody(BaseModel):
    mobile: str = Field(min_length=6, max_length=32)

    @field_validator("mobile")
    @classmethod
    def normalize_mobile(cls, value: str) -> str:
        mobile = re.sub(r"[\s()\-]", "", value.strip())
        if not re.fullmatch(r"(?:1\d{10}|\+[1-9]\d{6,14})", mobile):
            raise ValueError("invalid mobile")
        return mobile


class MemberInviteBody(MemberInviteLookupBody):
    roleIds: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class LocalLoginPollBody(BaseModel):
    pollToken: str


class PurchaseOrderLookupBody(BaseModel):
    orderKey: str = Field(min_length=1, max_length=800)


class ProcurementClaimBody(BaseModel):
    purchaseOrderIds: list[uuid.UUID] = Field(default_factory=list, max_length=300)
    purchaseOrderLineIds: list[uuid.UUID] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def require_selection(self) -> "ProcurementClaimBody":
        if not self.purchaseOrderIds and not self.purchaseOrderLineIds:
            raise ValueError("at least one purchase order or line is required")
        return self


class PurchaseSplitResourceBody(BaseModel):
    hubEnvironmentRef: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    hubEnvironmentName: str = Field(min_length=1, max_length=255)
    buyerAccountRef: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    buyerAccountLabel: str = Field(min_length=1, max_length=255)
    site: Literal["US", "MX"]

    @field_validator("hubEnvironmentName", "buyerAccountLabel")
    @classmethod
    def normalize_resource_label(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized or "@" in normalized:
            raise ValueError("resource labels must be display-safe and must not contain accounts")
        return normalized


class PurchaseSplitAllocationBody(BaseModel):
    purchaseOrderLineId: uuid.UUID
    quantity: int = Field(ge=1, le=100_000)


class PurchaseSplitGroupBody(BaseModel):
    clientKey: str = Field(min_length=1, max_length=64)
    resource: PurchaseSplitResourceBody | None = None
    note: str = Field(default="", max_length=1000)
    lines: list[PurchaseSplitAllocationBody] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_line_allocations(self) -> "PurchaseSplitGroupBody":
        line_ids = [item.purchaseOrderLineId for item in self.lines]
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("a purchase line can appear only once within a split")
        return self


class PurchaseSplitPlanBody(BaseModel):
    expectedRevision: int = Field(ge=0)
    groups: list[PurchaseSplitGroupBody] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_client_keys(self) -> "PurchaseSplitPlanBody":
        keys = [group.clientKey for group in self.groups]
        if len(keys) != len(set(keys)):
            raise ValueError("purchase split client keys must be unique")
        return self


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def create_app(
    settings: Settings | None = None,
    oauth_client: OAuthClient | None = None,
    directory_client: DirectoryClient | None = None,
    database: Database | None = None,
) -> FastAPI:
    settings = settings or Settings()  # type: ignore[call-arg]
    database = database or Database(settings.database_url.get_secret_value())
    oauth_client = oauth_client or FeishuOAuthClient(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret.get_secret_value(),
        redirect_uri=settings.feishu_redirect_uri,
        code_challenge_method=settings.feishu_pkce_method,
    )
    directory_client = directory_client or FeishuDirectoryClient(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret.get_secret_value(),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        database.dispose()

    app = FastAPI(
        title="Xynigo Auth Service",
        version="0.11.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.oauth_client = oauth_client
    app.state.directory_client = directory_client
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)

    def get_session() -> Iterator[Session]:
        yield from database.sessions()

    SessionDep = Annotated[Session, Depends(get_session)]

    def create_authorization_url(
        session: Session,
        *,
        local_login_request_id: uuid.UUID | None = None,
    ) -> str:
        now = utcnow()
        session.execute(delete(OAuthLoginAttempt).where(OAuthLoginAttempt.expires_at < now))
        session.execute(delete(LocalLoginRequest).where(LocalLoginRequest.expires_at < now))
        state_token = random_url_token()
        verifier = None if settings.feishu_pkce_method == "disabled" else random_url_token()
        session.add(
            OAuthLoginAttempt(
                state_hash=hash_token(state_token),
                code_verifier=verifier,
                local_login_request_id=local_login_request_id,
                expires_at=now + timedelta(seconds=settings.oauth_attempt_ttl_seconds),
            )
        )
        session.commit()
        if settings.feishu_pkce_method == "disabled":
            code_challenge = None
        elif settings.feishu_pkce_method == "plain":
            code_challenge = verifier
        else:
            assert verifier is not None
            code_challenge = pkce_challenge(verifier)
        return oauth_client.authorization_url(
            state=state_token,
            code_challenge=code_challenge,
        )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/v1/auth/local/complete":
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; "
                f"script-src 'sha256-{LOCAL_LOGIN_COMPLETE_SCRIPT_HASH}'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'"
            )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(session: SessionDep) -> dict[str, str]:
        session.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.get("/v1/auth/feishu/start", response_class=RedirectResponse)
    def start_feishu_login(session: SessionDep) -> RedirectResponse:
        url = create_authorization_url(session)
        return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    @app.post("/v1/auth/local/start", status_code=status.HTTP_201_CREATED)
    def start_local_login(session: SessionDep) -> dict[str, object]:
        now = utcnow()
        poll_token = random_url_token(48)
        local_login = LocalLoginRequest(
            poll_token_hash=hash_token(poll_token),
            status="pending",
            expires_at=now + timedelta(seconds=settings.local_login_ttl_seconds),
        )
        session.add(local_login)
        session.flush()
        login_url = create_authorization_url(
            session,
            local_login_request_id=local_login.id,
        )
        return {
            "status": "pending",
            "loginUrl": login_url,
            "pollToken": poll_token,
            "expiresIn": settings.local_login_ttl_seconds,
        }

    @app.get("/v1/auth/local/complete", response_class=HTMLResponse)
    def local_login_complete() -> str:
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Xynigo 登录完成</title>
</head>
<body>
  <p id="status">Xynigo 登录成功，正在自动关闭此页面…</p>
  <script>{LOCAL_LOGIN_COMPLETE_SCRIPT}</script>
</body>
</html>"""

    def resolve_local_login(
        session: Session,
        attempt: OAuthLoginAttempt,
        *,
        result: str,
        now: datetime,
        user_id: uuid.UUID | None = None,
        denial_code: str | None = None,
    ) -> LocalLoginRequest | None:
        if attempt.local_login_request_id is None:
            return None
        local_login = session.get(LocalLoginRequest, attempt.local_login_request_id)
        if (
            local_login is None
            or local_login.status != "pending"
            or as_utc(local_login.expires_at) <= now
        ):
            return None
        local_login.status = result
        local_login.user_id = user_id
        local_login.denial_code = denial_code
        local_login.resolved_at = now
        return local_login

    @app.get("/v1/auth/feishu/callback")
    def feishu_callback(
        request: Request,
        session: SessionDep,
        code: Annotated[str | None, Query()] = None,
        state_token: Annotated[str | None, Query(alias="state")] = None,
        oauth_error: Annotated[str | None, Query(alias="error")] = None,
    ) -> Response:
        if oauth_error:
            if state_token:
                denied_attempt = session.scalar(
                    select(OAuthLoginAttempt).where(
                        OAuthLoginAttempt.state_hash == hash_token(state_token)
                    )
                )
                if denied_attempt is not None and denied_attempt.used_at is None:
                    now = utcnow()
                    denied_attempt.used_at = now
                    denied_attempt.code_verifier = None
                    resolve_local_login(
                        session,
                        denied_attempt,
                        result="denied",
                        now=now,
                        denial_code="oauth_denied",
                    )
                    session.commit()
            raise HTTPException(status_code=401, detail={"code": "oauth_denied"})
        if not code or not state_token:
            raise HTTPException(status_code=400, detail={"code": "oauth_callback_invalid"})

        now = utcnow()
        attempt = session.scalar(
            select(OAuthLoginAttempt)
            .where(OAuthLoginAttempt.state_hash == hash_token(state_token))
            .with_for_update()
        )
        if (
            attempt is None
            or attempt.used_at is not None
            or as_utc(attempt.expires_at) <= now
            or (
                settings.feishu_pkce_method != "disabled"
                and not attempt.code_verifier
            )
        ):
            raise HTTPException(status_code=400, detail={"code": "oauth_state_invalid"})

        verifier = attempt.code_verifier
        attempt.used_at = now
        attempt.code_verifier = None
        session.commit()

        try:
            access_token = oauth_client.exchange_code(code=code, code_verifier=verifier)
            identity = oauth_client.get_identity(access_token)
        except OAuthProviderError as exc:
            logger.warning(
                "Feishu OAuth provider failure: stage=%s provider_code=%s request_id=%s",
                exc.stage,
                exc.provider_code,
                request.state.request_id,
            )
            resolve_local_login(
                session,
                attempt,
                result="denied",
                now=now,
                denial_code="oauth_provider_failed",
            )
            session.commit()
            raise HTTPException(
                status_code=502,
                detail={"code": "oauth_provider_failed", "stage": exc.stage},
            ) from exc
        finally:
            access_token = None

        if settings.allowed_tenant_key_set and identity.tenant_key not in settings.allowed_tenant_key_set:
            resolve_local_login(
                session,
                attempt,
                result="denied",
                now=now,
                denial_code="tenant_not_allowed",
            )
            _add_audit(
                session,
                request_id=request.state.request_id,
                action="auth.login",
                result="denied",
                details={"reason": "tenant_not_allowed"},
            )
            session.commit()
            raise HTTPException(status_code=403, detail={"code": "tenant_not_allowed"})

        tenant = _upsert_tenant(session, identity)
        is_bootstrap_admin = identity.open_id in settings.bootstrap_super_admin_open_id_set
        user = _upsert_user(
            session,
            tenant=tenant,
            identity=identity,
            activate_new=settings.auto_activate_users or is_bootstrap_admin,
            bootstrap_admin=is_bootstrap_admin,
        )
        if user.status != "active":
            resolve_local_login(
                session,
                attempt,
                result="denied",
                now=now,
                user_id=user.id,
                denial_code="user_pending_approval",
            )
            _add_audit(
                session,
                request_id=request.state.request_id,
                action="auth.login",
                result="denied",
                tenant_id=tenant.id,
                actor_user_id=user.id,
                details={"reason": "user_pending"},
            )
            session.commit()
            raise HTTPException(status_code=403, detail={"code": "user_pending_approval"})

        if is_bootstrap_admin:
            _ensure_super_admin(session, tenant=tenant, user=user)

        local_login = resolve_local_login(
            session,
            attempt,
            result="approved",
            now=now,
            user_id=user.id,
        )
        if attempt.local_login_request_id is not None:
            if local_login is None:
                raise HTTPException(status_code=400, detail={"code": "local_login_invalid"})
            user.last_login_at = now
            _add_audit(
                session,
                request_id=request.state.request_id,
                action="auth.local_login.approve",
                result="success",
                tenant_id=tenant.id,
                actor_user_id=user.id,
            )
            session.commit()
            return RedirectResponse(
                url="/v1/auth/local/complete",
                status_code=status.HTTP_303_SEE_OTHER,
            )

        raw_session_token = random_url_token(48)
        session.add(
            SessionRecord(
                user_id=user.id,
                token_hash=hash_token(raw_session_token),
                last_seen_at=now,
                expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
            )
        )
        user.last_login_at = now
        _add_audit(
            session,
            request_id=request.state.request_id,
            action="auth.login",
            result="success",
            tenant_id=tenant.id,
            actor_user_id=user.id,
        )
        session.commit()

        response = RedirectResponse(
            url=settings.login_success_path,
            status_code=status.HTTP_303_SEE_OTHER,
        )
        response.set_cookie(
            key=settings.cookie_name,
            value=raw_session_token,
            max_age=settings.session_ttl_seconds,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/v1/auth/local/poll")
    def poll_local_login(
        request: Request,
        body: LocalLoginPollBody,
        session: SessionDep,
    ) -> Response:
        poll_token = body.pollToken.strip()
        if len(poll_token) < 32:
            raise HTTPException(status_code=400, detail={"code": "local_login_invalid"})
        local_login = session.scalar(
            select(LocalLoginRequest)
            .where(LocalLoginRequest.poll_token_hash == hash_token(poll_token))
            .with_for_update()
        )
        now = utcnow()
        if local_login is None:
            raise HTTPException(status_code=400, detail={"code": "local_login_invalid"})
        if as_utc(local_login.expires_at) <= now:
            if local_login.status == "pending":
                local_login.status = "denied"
                local_login.denial_code = "local_login_expired"
                local_login.resolved_at = now
                session.commit()
            raise HTTPException(status_code=410, detail={"code": "local_login_expired"})
        if local_login.status == "pending":
            return JSONResponse({"status": "pending"}, status_code=status.HTTP_202_ACCEPTED)
        if local_login.status == "denied":
            raise HTTPException(
                status_code=403,
                detail={"code": local_login.denial_code or "local_login_denied"},
            )
        if local_login.status == "consumed" or local_login.consumed_at is not None:
            raise HTTPException(status_code=409, detail={"code": "local_login_consumed"})
        if local_login.status != "approved" or local_login.user_id is None:
            raise HTTPException(status_code=400, detail={"code": "local_login_invalid"})

        user = session.get(User, local_login.user_id)
        if user is None or user.status != "active":
            local_login.status = "denied"
            local_login.denial_code = "user_disabled"
            local_login.resolved_at = now
            session.commit()
            raise HTTPException(status_code=403, detail={"code": "user_disabled"})
        tenant = session.get(Tenant, user.tenant_id)
        if tenant is None or tenant.status != "active":
            local_login.status = "denied"
            local_login.denial_code = "tenant_disabled"
            local_login.resolved_at = now
            session.commit()
            raise HTTPException(status_code=403, detail={"code": "tenant_disabled"})

        raw_session_token = random_url_token(48)
        expires_at = now + timedelta(seconds=settings.session_ttl_seconds)
        session.add(
            SessionRecord(
                user_id=user.id,
                token_hash=hash_token(raw_session_token),
                last_seen_at=now,
                expires_at=expires_at,
            )
        )
        local_login.status = "consumed"
        local_login.consumed_at = now
        user.last_login_at = now
        _add_audit(
            session,
            request_id=request.state.request_id,
            action="auth.local_session.issue",
            result="success",
            tenant_id=tenant.id,
            actor_user_id=user.id,
        )
        identity_payload = _identity_payload(session, user, tenant)
        session.commit()
        return JSONResponse(
            {
                "status": "authenticated",
                "sessionToken": raw_session_token,
                "sessionExpiresAt": expires_at.isoformat(),
                "identity": identity_payload,
            }
        )

    @app.get("/v1/auth/me")
    def current_user(
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        raw_token = _request_session_token(session_token, authorization)
        record, user, tenant = _authenticated_identity(session, raw_token)
        record.last_seen_at = utcnow()
        payload = _identity_payload(session, user, tenant)
        session.commit()
        return payload

    @app.post("/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    def logout(
        response: Response,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        raw_token = _request_session_token(session_token, authorization)
        if raw_token:
            record = session.scalar(
                select(SessionRecord).where(SessionRecord.token_hash == hash_token(raw_token))
            )
            if record and record.revoked_at is None:
                record.revoked_at = utcnow()
                session.commit()
        response.delete_cookie(
            key=settings.cookie_name,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
            path="/",
        )

    def authorize_request(
        request: Request,
        session: Session,
        *,
        permission: str,
        session_token: str | None,
        authorization: str | None,
        audit_action: str,
    ) -> AdminActor:
        raw_token = _request_session_token(session_token, authorization)
        session_record, user, tenant = _authenticated_identity(session, raw_token)
        _ensure_system_catalog(session, tenant=tenant)
        if permission not in _permission_code_set(session, user):
            _add_audit(
                session,
                request_id=request.state.request_id,
                action=audit_action,
                result="denied",
                tenant_id=tenant.id,
                actor_user_id=user.id,
                details={"permission": permission},
            )
            session.commit()
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})
        session_record.last_seen_at = utcnow()
        return AdminActor(session_record=session_record, user=user, tenant=tenant)

    def authorize_admin(
        request: Request,
        session: Session,
        *,
        permission: str,
        session_token: str | None,
        authorization: str | None,
    ) -> AdminActor:
        return authorize_request(
            request,
            session,
            permission=permission,
            session_token=session_token,
            authorization=authorization,
            audit_action="admin.authorization",
        )

    def purchase_error(
        request: Request,
        session: Session,
        actor: AdminActor,
        action: str,
        exc: PurchaseServiceError,
    ) -> None:
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="denied" if exc.status in (403, 404, 409, 422) else "failure",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={"reason": exc.code},
        )
        session.commit()
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)})

    @app.post("/v1/purchase-orders/draft")
    def save_purchase_order_draft(
        request: Request,
        body: PurchaseDraft,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "purchase_order.draft.save"
        actor = authorize_request(
            request,
            session,
            permission="procurement.request.save",
            session_token=session_token,
            authorization=authorization,
            audit_action="purchase_order.authorization",
        )
        try:
            result = PurchaseOrderService(session).save_draft(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                draft=body,
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc)
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={
                "purchaseOrderId": result["purchaseOrderId"],
                "draftRevision": result["draftRevision"],
                "unchanged": result["unchanged"],
            },
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.post("/v1/purchase-orders/submit")
    def submit_purchase_order(
        request: Request,
        body: PurchaseDraft,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "purchase_order.submit"
        actor = authorize_request(
            request,
            session,
            permission="procurement.request.submit",
            session_token=session_token,
            authorization=authorization,
            audit_action="purchase_order.authorization",
        )
        try:
            result = PurchaseOrderService(session).submit(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                draft=body,
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc)
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={
                "purchaseOrderId": result["purchaseOrderId"],
                "draftRevision": result["draftRevision"],
                "unchanged": result["unchanged"],
            },
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.post("/v1/purchase-orders/get")
    def get_purchase_order(
        request: Request,
        body: PurchaseOrderLookupBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "purchase_order.read"
        actor = authorize_request(
            request,
            session,
            permission="procurement.request.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="purchase_order.authorization",
        )
        try:
            result = PurchaseOrderService(session).get(
                tenant_id=actor.tenant.id,
                order_key=body.orderKey,
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc)
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={
                "purchaseOrderId": result["purchaseOrderId"],
                "draftRevision": result["draftRevision"],
            },
        )
        actor.session_record.last_seen_at = utcnow()
        session.commit()
        return {"ok": True, "data": result}

    @app.get("/v1/procurement/overview")
    def get_procurement_overview(
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="procurement.request.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="purchase_order.authorization",
        )
        result = PurchaseOrderService(session).workspace_overview(
            tenant_id=actor.tenant.id,
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.get("/v1/procurement/orders")
    def list_procurement_orders(
        request: Request,
        session: SessionDep,
        submission_status: Annotated[
            Literal["draft", "submitted"] | None,
            Query(alias="submissionStatus"),
        ] = None,
        sync_status: Annotated[
            Literal["pending", "synced", "failed", "conflict"] | None,
            Query(alias="syncStatus"),
        ] = None,
        workflow_status: Annotated[
            Literal[
                "draft",
                "unclaimed",
                "claimed",
                "purchasing",
                "ordered",
                "logistics_filled",
                "completed",
                "returned",
                "exception",
            ]
            | None,
            Query(alias="workflowStatus"),
        ] = None,
        page: Annotated[int, Query(ge=1, le=100_000)] = 1,
        page_size: Annotated[int, Query(alias="pageSize", ge=1, le=300)] = 50,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="procurement.request.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="purchase_order.authorization",
        )
        result = PurchaseOrderService(session).workspace_list(
            tenant_id=actor.tenant.id,
            submission_status=submission_status,
            sync_status=sync_status,
            workflow_status=workflow_status,
            page=page,
            page_size=page_size,
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.get("/v1/procurement/orders/{purchase_order_id}")
    def get_procurement_order_detail(
        request: Request,
        purchase_order_id: uuid.UUID,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "purchase_order.workspace.detail.read"
        actor = authorize_request(
            request,
            session,
            permission="procurement.request.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="purchase_order.authorization",
        )
        try:
            result = PurchaseOrderService(session).workspace_detail(
                tenant_id=actor.tenant.id,
                purchase_order_id=purchase_order_id,
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc)
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={"purchaseOrderId": result["purchaseOrderId"]},
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.post("/v1/procurement/claims")
    def claim_procurement_lines(
        request: Request,
        body: ProcurementClaimBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "purchase_order.lines.claim"
        actor = authorize_request(
            request,
            session,
            permission="procurement.execution.manage",
            session_token=session_token,
            authorization=authorization,
            audit_action="purchase_order.authorization",
        )
        try:
            result = PurchaseOrderService(session).claim(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                purchase_order_ids=body.purchaseOrderIds,
                purchase_order_line_ids=body.purchaseOrderLineIds,
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc)
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={
                "orderCount": len(result["purchaseOrderIds"]),
                "lineCount": result["lineCount"],
                "claimedCount": result["claimedCount"],
            },
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.post("/v1/procurement/orders/{purchase_order_id}/splits")
    def save_procurement_split_plan(
        request: Request,
        purchase_order_id: uuid.UUID,
        body: PurchaseSplitPlanBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "purchase_order.split_plan.save"
        actor = authorize_request(
            request,
            session,
            permission="procurement.execution.manage",
            session_token=session_token,
            authorization=authorization,
            audit_action="purchase_order.authorization",
        )
        try:
            result = PurchaseOrderService(session).save_split_plan(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                purchase_order_id=purchase_order_id,
                expected_revision=body.expectedRevision,
                groups=[group.model_dump(mode="python") for group in body.groups],
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc)
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={
                "purchaseOrderId": result["purchaseOrderId"],
                "executionRevision": result["executionRevision"],
                "splitCount": result["splitCount"],
                "lineCount": result["lineCount"],
            },
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.get("/v1/procurement/execution/splits")
    def list_procurement_execution_splits(
        request: Request,
        session: SessionDep,
        split_status: Annotated[
            Literal[
                "waiting_binding",
                "waiting_order",
                "purchasing",
                "ordered",
                "exception",
            ]
            | None,
            Query(alias="status"),
        ] = None,
        site: Annotated[Literal["US", "MX"] | None, Query()] = None,
        binding: Annotated[Literal["bound", "unbound"] | None, Query()] = None,
        keyword: Annotated[str | None, Query(max_length=200)] = None,
        page: Annotated[int, Query(ge=1, le=100_000)] = 1,
        page_size: Annotated[int, Query(alias="pageSize", ge=1, le=300)] = 100,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="procurement.request.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="purchase_order.authorization",
        )
        result = PurchaseOrderService(session).execution_list(
            tenant_id=actor.tenant.id,
            status=split_status,
            site=site,
            binding=binding,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
        session.commit()
        return {"ok": True, "data": result}

    def tenant_member(
        request: Request,
        session: Session,
        actor: AdminActor,
        user_id: uuid.UUID,
        *,
        action: str,
        lock: bool = False,
    ) -> User:
        statement = select(User).where(
            User.id == user_id,
            User.tenant_id == actor.tenant.id,
        )
        if lock:
            statement = statement.with_for_update()
        user = session.scalar(statement)
        if user is None:
            _add_audit(
                session,
                request_id=request.state.request_id,
                action=action,
                result="denied",
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                details={"reason": "target_not_found_or_cross_tenant", "targetUserId": str(user_id)},
            )
            session.commit()
            raise HTTPException(status_code=404, detail={"code": "member_not_found"})
        return user

    def tenant_role(
        request: Request,
        session: Session,
        actor: AdminActor,
        role_id: uuid.UUID,
        *,
        action: str,
        lock: bool = False,
    ) -> Role:
        statement = select(Role).where(
            Role.id == role_id,
            Role.tenant_id == actor.tenant.id,
        )
        if lock:
            statement = statement.with_for_update()
        role = session.scalar(statement)
        if role is None:
            _add_audit(
                session,
                request_id=request.state.request_id,
                action=action,
                result="denied",
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                details={"reason": "target_not_found_or_cross_tenant", "targetRoleId": str(role_id)},
            )
            session.commit()
            raise HTTPException(status_code=404, detail={"code": "role_not_found"})
        return role

    def reject_state_change(
        request: Request,
        session: Session,
        actor: AdminActor,
        *,
        action: str,
        code: str,
        details: dict[str, Any],
        http_status: int = status.HTTP_409_CONFLICT,
    ) -> None:
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="denied",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details=details,
        )
        session.commit()
        raise HTTPException(status_code=http_status, detail={"code": code})

    def role_name_or_reject(
        request: Request,
        session: Session,
        actor: AdminActor,
        *,
        action: str,
        value: str,
        exclude_role_id: uuid.UUID | None = None,
    ) -> str:
        name = " ".join(value.split())
        if not name:
            raise HTTPException(status_code=422, detail={"code": "role_name_invalid"})
        existing_roles = session.scalars(
            select(Role).where(Role.tenant_id == actor.tenant.id)
        )
        if any(
            role.id != exclude_role_id and role.name.casefold() == name.casefold()
            for role in existing_roles
        ):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="role_name_conflict",
                details={"reason": "duplicate_role_name"},
            )
        return name

    def lock_role_catalog(session: Session, actor: AdminActor) -> None:
        # Serialize tenant role-definition writes so concurrent create/rename
        # requests cannot both pass the case-insensitive name check.
        session.scalar(
            select(Tenant.id)
            .where(Tenant.id == actor.tenant.id)
            .with_for_update()
        )

    def directory_user_or_reject(
        request: Request,
        session: Session,
        actor: AdminActor,
        *,
        action: str,
        mobile: str,
    ) -> FeishuDirectoryUser:
        try:
            directory_user = directory_client.find_user_by_mobile(mobile)
        except DirectoryProviderError as exc:
            error_code = (
                "feishu_directory_permission_missing"
                if str(exc.provider_code) in {"41050", "99991672"}
                else "feishu_directory_unavailable"
            )
            _add_audit(
                session,
                request_id=request.state.request_id,
                action=action,
                result="denied",
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                details={
                    "reason": error_code,
                    "providerStage": exc.stage,
                    "providerCode": str(exc.provider_code) if exc.provider_code is not None else None,
                },
            )
            session.commit()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": error_code},
            ) from exc
        if directory_user is None:
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="feishu_member_not_found",
                details={"reason": "mobile_not_found_or_out_of_scope"},
                http_status=status.HTTP_404_NOT_FOUND,
            )
        if (
            not directory_user.is_activated
            or directory_user.is_frozen
            or directory_user.is_resigned
            or directory_user.is_exited
            or directory_user.is_unjoin
        ):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="feishu_member_ineligible",
                details={
                    "reason": "directory_status_ineligible",
                    "isActivated": directory_user.is_activated,
                    "isFrozen": directory_user.is_frozen,
                    "isResigned": directory_user.is_resigned,
                    "isExited": directory_user.is_exited,
                    "isUnjoin": directory_user.is_unjoin,
                },
            )
        return directory_user

    def invitation_roles_or_reject(
        request: Request,
        session: Session,
        actor: AdminActor,
        *,
        action: str,
        role_ids: list[uuid.UUID],
    ) -> list[Role]:
        requested_ids = set(role_ids)
        if len(requested_ids) != len(role_ids):
            raise HTTPException(status_code=422, detail={"code": "role_ids_invalid"})
        roles = list(
            session.scalars(
                select(Role).where(
                    Role.tenant_id == actor.tenant.id,
                    Role.id.in_(requested_ids),
                )
            )
        ) if requested_ids else []
        if {role.id for role in roles} != requested_ids:
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="role_not_found",
                details={"reason": "role_not_found_or_cross_tenant"},
                http_status=status.HTTP_404_NOT_FOUND,
            )
        requested_codes = {role.code for role in roles}
        if SUPER_ADMIN_ROLE in requested_codes and not _user_has_role(
            session, actor.user, SUPER_ADMIN_ROLE
        ):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="super_admin_required",
                details={"reason": "super_admin_role_assignment"},
                http_status=status.HTTP_403_FORBIDDEN,
            )
        requested_permission_codes = {
            code for role in roles for code in _role_permission_codes(session, role)
        }
        if (
            not _user_has_role(session, actor.user, SUPER_ADMIN_ROLE)
            and not requested_permission_codes.issubset(
                _permission_code_set(session, actor.user)
            )
        ):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="permission_grant_exceeds_actor",
                details={"reason": "permission_ceiling"},
                http_status=status.HTTP_403_FORBIDDEN,
            )
        return roles

    @app.get("/v1/admin/members")
    def list_members(
        request: Request,
        session: SessionDep,
        member_status: Annotated[str | None, Query(alias="status")] = None,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_admin(
            request,
            session,
            permission="system.member.manage",
            session_token=session_token,
            authorization=authorization,
        )
        if member_status not in (None, "pending", "active", "disabled"):
            raise HTTPException(status_code=422, detail={"code": "member_status_invalid"})
        statement = select(User).where(User.tenant_id == actor.tenant.id)
        if member_status:
            statement = statement.where(User.status == member_status)
        users = list(session.scalars(statement.order_by(User.created_at, User.id)))
        payload = [_member_payload(session, user) for user in users]
        session.commit()
        return {"members": payload}

    @app.post("/v1/admin/members/invitations/resolve")
    def resolve_member_invitation(
        body: MemberInviteLookupBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "admin.member.invitation.resolve"
        actor = authorize_admin(
            request,
            session,
            permission="system.member.manage",
            session_token=session_token,
            authorization=authorization,
        )
        directory_user = directory_user_or_reject(
            request,
            session,
            actor,
            action=action,
            mobile=body.mobile,
        )
        existing = session.scalar(
            select(User).where(
                User.tenant_id == actor.tenant.id,
                User.feishu_open_id == directory_user.open_id,
            )
        )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={
                "matched": True,
                "existingMemberId": str(existing.id) if existing else None,
            },
        )
        session.commit()
        return {
            "candidate": _directory_candidate_payload(directory_user),
            "existingMember": _member_payload(session, existing) if existing else None,
        }

    @app.post("/v1/admin/members/invitations", status_code=status.HTTP_201_CREATED)
    def create_member_invitation(
        body: MemberInviteBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "admin.member.invitation.create"
        actor = authorize_admin(
            request,
            session,
            permission="system.member.manage",
            session_token=session_token,
            authorization=authorization,
        )
        if body.roleIds:
            actor = authorize_admin(
                request,
                session,
                permission="system.role.manage",
                session_token=session_token,
                authorization=authorization,
            )
        roles = invitation_roles_or_reject(
            request,
            session,
            actor,
            action=action,
            role_ids=body.roleIds,
        )
        directory_user = directory_user_or_reject(
            request,
            session,
            actor,
            action=action,
            mobile=body.mobile,
        )
        session.scalar(
            select(Tenant.id)
            .where(Tenant.id == actor.tenant.id)
            .with_for_update()
        )
        existing = session.scalar(
            select(User).where(
                User.tenant_id == actor.tenant.id,
                User.feishu_open_id == directory_user.open_id,
            )
        )
        if existing is not None:
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="member_already_exists",
                details={"targetUserId": str(existing.id)},
            )
        user = User(
            tenant_id=actor.tenant.id,
            feishu_open_id=directory_user.open_id,
            feishu_union_id=directory_user.union_id,
            display_name=directory_user.name,
            avatar_url=directory_user.avatar_url,
            status="pending",
        )
        session.add(user)
        session.flush()
        for role in roles:
            session.add(UserRole(user_id=user.id, role_id=role.id))
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={
                "targetUserId": str(user.id),
                "status": "pending",
                "roleCodes": sorted(role.code for role in roles),
                "source": "feishu_mobile_invitation",
            },
        )
        session.commit()
        return {"member": _member_payload(session, user)}

    @app.get("/v1/admin/members/{user_id}")
    def get_member(
        user_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_admin(
            request,
            session,
            permission="system.member.manage",
            session_token=session_token,
            authorization=authorization,
        )
        user = tenant_member(request, session, actor, user_id, action="admin.member.read")
        payload = _member_payload(session, user)
        session.commit()
        return {"member": payload}

    @app.post("/v1/admin/members/{user_id}/approve")
    def approve_member(
        user_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "admin.member.approve"
        actor = authorize_admin(
            request,
            session,
            permission="system.member.manage",
            session_token=session_token,
            authorization=authorization,
        )
        user = tenant_member(request, session, actor, user_id, action=action, lock=True)
        if user.status != "pending":
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="member_status_conflict",
                details={"targetUserId": str(user.id), "currentStatus": user.status},
            )
        user.status = "active"
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={"targetUserId": str(user.id), "fromStatus": "pending", "toStatus": "active"},
        )
        session.commit()
        return {"member": _member_payload(session, user)}

    @app.post("/v1/admin/members/{user_id}/disable")
    def disable_member(
        user_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "admin.member.disable"
        actor = authorize_admin(
            request,
            session,
            permission="system.member.manage",
            session_token=session_token,
            authorization=authorization,
        )
        user = tenant_member(request, session, actor, user_id, action=action, lock=True)
        if user.id == actor.user.id:
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="cannot_disable_self",
                details={"targetUserId": str(user.id)},
            )
        if _user_has_role(session, user, SUPER_ADMIN_ROLE) and not _user_has_role(
            session, actor.user, SUPER_ADMIN_ROLE
        ):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="super_admin_required",
                details={"targetUserId": str(user.id)},
                http_status=status.HTTP_403_FORBIDDEN,
            )
        if user.status == "disabled":
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="member_status_conflict",
                details={"targetUserId": str(user.id), "currentStatus": user.status},
            )
        previous_status = user.status
        user.status = "disabled"
        revoked_count = _revoke_active_sessions(session, user.id, utcnow())
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={
                "targetUserId": str(user.id),
                "fromStatus": previous_status,
                "toStatus": "disabled",
                "revokedSessionCount": revoked_count,
            },
        )
        session.commit()
        return {"member": _member_payload(session, user), "revokedSessionCount": revoked_count}

    @app.post("/v1/admin/members/{user_id}/restore")
    def restore_member(
        user_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "admin.member.restore"
        actor = authorize_admin(
            request,
            session,
            permission="system.member.manage",
            session_token=session_token,
            authorization=authorization,
        )
        user = tenant_member(request, session, actor, user_id, action=action, lock=True)
        if user.status != "disabled":
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="member_status_conflict",
                details={"targetUserId": str(user.id), "currentStatus": user.status},
            )
        if _user_has_role(session, user, SUPER_ADMIN_ROLE) and not _user_has_role(
            session, actor.user, SUPER_ADMIN_ROLE
        ):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="super_admin_required",
                details={"targetUserId": str(user.id)},
                http_status=status.HTTP_403_FORBIDDEN,
            )
        user.status = "active"
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={"targetUserId": str(user.id), "fromStatus": "disabled", "toStatus": "active"},
        )
        session.commit()
        return {"member": _member_payload(session, user)}

    @app.get("/v1/admin/permissions")
    def list_permissions(
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_admin(
            request,
            session,
            permission="system.role.manage",
            session_token=session_token,
            authorization=authorization,
        )
        _ensure_system_catalog(session, tenant=actor.tenant)
        permissions = list(session.scalars(select(Permission).order_by(Permission.code)))
        session.commit()
        return {"permissions": [_permission_payload(item) for item in permissions]}

    @app.get("/v1/admin/roles")
    def list_roles(
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_admin(
            request,
            session,
            permission="system.role.manage",
            session_token=session_token,
            authorization=authorization,
        )
        _ensure_system_catalog(session, tenant=actor.tenant)
        roles = list(
            session.scalars(
                select(Role).where(Role.tenant_id == actor.tenant.id).order_by(Role.code)
            )
        )
        session.commit()
        return {"roles": [_role_payload(session, role) for role in roles]}

    @app.post("/v1/admin/roles", status_code=status.HTTP_201_CREATED)
    def create_role(
        body: RoleWriteBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "admin.role.create"
        actor = authorize_admin(
            request,
            session,
            permission="system.role.manage",
            session_token=session_token,
            authorization=authorization,
        )
        lock_role_catalog(session, actor)
        _ensure_system_catalog(session, tenant=actor.tenant)
        name = role_name_or_reject(
            request,
            session,
            actor,
            action=action,
            value=body.name,
        )
        role = Role(
            tenant_id=actor.tenant.id,
            code=f"custom_{uuid.uuid4().hex}",
            name=name,
            is_system=False,
        )
        session.add(role)
        session.flush()
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={"targetRoleId": str(role.id), "roleCode": role.code, "name": role.name},
        )
        session.commit()
        return {"role": _role_payload(session, role)}

    @app.put("/v1/admin/roles/{role_id}")
    def rename_role(
        role_id: uuid.UUID,
        body: RoleWriteBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "admin.role.update"
        actor = authorize_admin(
            request,
            session,
            permission="system.role.manage",
            session_token=session_token,
            authorization=authorization,
        )
        lock_role_catalog(session, actor)
        role = tenant_role(request, session, actor, role_id, action=action, lock=True)
        if role.is_system:
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="system_role_immutable",
                details={"targetRoleId": str(role.id), "roleCode": role.code},
            )
        name = role_name_or_reject(
            request,
            session,
            actor,
            action=action,
            value=body.name,
            exclude_role_id=role.id,
        )
        previous_name = role.name
        role.name = name
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={
                "targetRoleId": str(role.id),
                "roleCode": role.code,
                "previousName": previous_name,
                "name": role.name,
            },
        )
        session.commit()
        return {"role": _role_payload(session, role)}

    @app.delete("/v1/admin/roles/{role_id}")
    def delete_role(
        role_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "admin.role.delete"
        actor = authorize_admin(
            request,
            session,
            permission="system.role.manage",
            session_token=session_token,
            authorization=authorization,
        )
        lock_role_catalog(session, actor)
        role = tenant_role(request, session, actor, role_id, action=action, lock=True)
        if role.is_system:
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="system_role_immutable",
                details={"targetRoleId": str(role.id), "roleCode": role.code},
            )
        assigned_member_count = session.scalar(
            select(func.count(UserRole.user_id)).where(UserRole.role_id == role.id)
        ) or 0
        if assigned_member_count:
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="role_in_use",
                details={
                    "targetRoleId": str(role.id),
                    "roleCode": role.code,
                    "assignedMemberCount": assigned_member_count,
                },
            )
        role_code = role.code
        session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        session.delete(role)
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={"targetRoleId": str(role_id), "roleCode": role_code},
        )
        session.commit()
        return {"deleted": True, "roleId": str(role_id)}

    @app.put("/v1/admin/roles/{role_id}/permissions")
    def update_role_permissions(
        role_id: uuid.UUID,
        body: RolePermissionsBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "admin.role.permissions.update"
        actor = authorize_admin(
            request,
            session,
            permission="system.role.manage",
            session_token=session_token,
            authorization=authorization,
        )
        role = tenant_role(request, session, actor, role_id, action=action, lock=True)
        if role.code in SYSTEM_MANAGED_PERMISSION_ROLES:
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="system_role_immutable",
                details={"targetRoleId": str(role.id), "roleCode": role.code},
            )
        permission_codes = sorted({item.strip() for item in body.permissionCodes if item.strip()})
        if len(permission_codes) != len(body.permissionCodes):
            raise HTTPException(status_code=422, detail={"code": "permission_codes_invalid"})
        permissions = list(
            session.scalars(select(Permission).where(Permission.code.in_(permission_codes)))
        ) if permission_codes else []
        found_codes = {item.code for item in permissions}
        if found_codes != set(permission_codes):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="permission_code_invalid",
                details={"targetRoleId": str(role.id), "reason": "unknown_permission_code"},
                http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        restricted_codes = set(permission_codes) & SUPER_ADMIN_ONLY_PERMISSIONS
        if restricted_codes:
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="super_admin_only_permission",
                details={
                    "targetRoleId": str(role.id),
                    "reason": "high_sensitive_permission_restricted",
                    "permissionCodes": sorted(restricted_codes),
                },
                http_status=status.HTTP_403_FORBIDDEN,
            )
        if (
            not _user_has_role(session, actor.user, SUPER_ADMIN_ROLE)
            and not set(permission_codes).issubset(_permission_code_set(session, actor.user))
        ):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="permission_grant_exceeds_actor",
                details={"targetRoleId": str(role.id), "reason": "permission_ceiling"},
                http_status=status.HTTP_403_FORBIDDEN,
            )
        previous_codes = _role_permission_codes(session, role)
        session.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        for permission in permissions:
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={
                "targetRoleId": str(role.id),
                "previousPermissionCodes": previous_codes,
                "permissionCodes": permission_codes,
            },
        )
        session.commit()
        return {"role": _role_payload(session, role)}

    @app.put("/v1/admin/members/{user_id}/roles")
    def update_member_roles(
        user_id: uuid.UUID,
        body: MemberRolesBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "admin.member.roles.update"
        actor = authorize_admin(
            request,
            session,
            permission="system.role.manage",
            session_token=session_token,
            authorization=authorization,
        )
        user = tenant_member(request, session, actor, user_id, action=action, lock=True)
        requested_ids = set(body.roleIds)
        if len(requested_ids) != len(body.roleIds):
            raise HTTPException(status_code=422, detail={"code": "role_ids_invalid"})
        roles = list(
            session.scalars(
                select(Role).where(
                    Role.tenant_id == actor.tenant.id,
                    Role.id.in_(requested_ids),
                )
            )
        ) if requested_ids else []
        if {role.id for role in roles} != requested_ids:
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="role_not_found",
                details={"targetUserId": str(user.id), "reason": "role_not_found_or_cross_tenant"},
                http_status=status.HTTP_404_NOT_FOUND,
            )
        current_codes = _user_role_codes(session, user)
        requested_codes = sorted(role.code for role in roles)
        super_admin_involved = (
            SUPER_ADMIN_ROLE in current_codes or SUPER_ADMIN_ROLE in requested_codes
        )
        if super_admin_involved and not _user_has_role(session, actor.user, SUPER_ADMIN_ROLE):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="super_admin_required",
                details={"targetUserId": str(user.id)},
                http_status=status.HTTP_403_FORBIDDEN,
            )
        requested_permission_codes = {
            code for role in roles for code in _role_permission_codes(session, role)
        }
        if (
            not _user_has_role(session, actor.user, SUPER_ADMIN_ROLE)
            and not requested_permission_codes.issubset(
                _permission_code_set(session, actor.user)
            )
        ):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="permission_grant_exceeds_actor",
                details={"targetUserId": str(user.id), "reason": "permission_ceiling"},
                http_status=status.HTTP_403_FORBIDDEN,
            )
        if (
            user.id == actor.user.id
            and SUPER_ADMIN_ROLE in current_codes
            and SUPER_ADMIN_ROLE not in requested_codes
        ):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="cannot_remove_own_super_admin",
                details={"targetUserId": str(user.id)},
            )
        session.execute(delete(UserRole).where(UserRole.user_id == user.id))
        for role in roles:
            session.add(UserRole(user_id=user.id, role_id=role.id))
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={
                "targetUserId": str(user.id),
                "previousRoleCodes": current_codes,
                "roleCodes": requested_codes,
            },
        )
        session.commit()
        return {"member": _member_payload(session, user)}

    @app.get("/v1/admin/sessions")
    def list_sessions(
        request: Request,
        session: SessionDep,
        user_id: Annotated[uuid.UUID | None, Query(alias="userId")] = None,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_admin(
            request,
            session,
            permission="system.member.manage",
            session_token=session_token,
            authorization=authorization,
        )
        if user_id is not None:
            tenant_member(request, session, actor, user_id, action="admin.session.list")
        now = utcnow()
        statement = (
            select(SessionRecord, User)
            .join(User, User.id == SessionRecord.user_id)
            .where(
                User.tenant_id == actor.tenant.id,
                SessionRecord.revoked_at.is_(None),
                SessionRecord.expires_at > now,
            )
        )
        if user_id is not None:
            statement = statement.where(User.id == user_id)
        rows = session.execute(statement.order_by(SessionRecord.last_seen_at.desc())).all()
        payload = [
            _session_payload(record, user, current_session_id=actor.session_record.id)
            for record, user in rows
        ]
        session.commit()
        return {"sessions": payload}

    @app.post("/v1/admin/sessions/{session_id}/revoke")
    def revoke_session(
        session_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "admin.session.revoke"
        actor = authorize_admin(
            request,
            session,
            permission="system.member.manage",
            session_token=session_token,
            authorization=authorization,
        )
        row = session.execute(
            select(SessionRecord, User)
            .join(User, User.id == SessionRecord.user_id)
            .where(
                SessionRecord.id == session_id,
                User.tenant_id == actor.tenant.id,
            )
            .with_for_update()
        ).one_or_none()
        if row is None:
            _add_audit(
                session,
                request_id=request.state.request_id,
                action=action,
                result="denied",
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                details={"reason": "target_not_found_or_cross_tenant", "targetSessionId": str(session_id)},
            )
            session.commit()
            raise HTTPException(status_code=404, detail={"code": "session_not_found"})
        record, target_user = row
        if _user_has_role(session, target_user, SUPER_ADMIN_ROLE) and not _user_has_role(
            session, actor.user, SUPER_ADMIN_ROLE
        ):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="super_admin_required",
                details={"targetSessionId": str(record.id)},
                http_status=status.HTTP_403_FORBIDDEN,
            )
        now = utcnow()
        revoked = record.revoked_at is None and as_utc(record.expires_at) > now
        if revoked:
            record.revoked_at = now
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={
                "targetSessionId": str(record.id),
                "targetUserId": str(target_user.id),
                "revoked": revoked,
            },
        )
        session.commit()
        return {"revoked": revoked, "sessionId": str(record.id)}

    @app.post("/v1/admin/members/{user_id}/sessions/revoke")
    def revoke_member_sessions(
        user_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "admin.member.sessions.revoke"
        actor = authorize_admin(
            request,
            session,
            permission="system.member.manage",
            session_token=session_token,
            authorization=authorization,
        )
        user = tenant_member(request, session, actor, user_id, action=action, lock=True)
        if _user_has_role(session, user, SUPER_ADMIN_ROLE) and not _user_has_role(
            session, actor.user, SUPER_ADMIN_ROLE
        ):
            reject_state_change(
                request,
                session,
                actor,
                action=action,
                code="super_admin_required",
                details={"targetUserId": str(user.id)},
                http_status=status.HTTP_403_FORBIDDEN,
            )
        revoked_count = _revoke_active_sessions(session, user.id, utcnow())
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            details={"targetUserId": str(user.id), "revokedSessionCount": revoked_count},
        )
        session.commit()
        return {"revokedSessionCount": revoked_count, "memberId": str(user.id)}

    return app


def _upsert_tenant(session: Session, identity: FeishuIdentity) -> Tenant:
    tenant = session.scalar(select(Tenant).where(Tenant.feishu_tenant_key == identity.tenant_key))
    if tenant is None:
        tenant = Tenant(feishu_tenant_key=identity.tenant_key, status="active")
        session.add(tenant)
        session.flush()
    if tenant.status != "active":
        raise HTTPException(status_code=403, detail={"code": "tenant_disabled"})
    return tenant


def _upsert_user(
    session: Session,
    *,
    tenant: Tenant,
    identity: FeishuIdentity,
    activate_new: bool,
    bootstrap_admin: bool,
) -> User:
    user = session.scalar(
        select(User).where(
            User.tenant_id == tenant.id,
            User.feishu_open_id == identity.open_id,
        )
    )
    if user is None:
        user = User(
            tenant_id=tenant.id,
            feishu_open_id=identity.open_id,
            feishu_union_id=identity.union_id,
            display_name=identity.name,
            avatar_url=identity.avatar_url,
            status="active" if activate_new else "pending",
        )
        session.add(user)
        session.flush()
    else:
        user.feishu_union_id = identity.union_id
        user.display_name = identity.name
        user.avatar_url = identity.avatar_url
        if bootstrap_admin and user.status == "pending":
            user.status = "active"
    return user


def _ensure_super_admin(session: Session, *, tenant: Tenant, user: User) -> None:
    role = _ensure_system_catalog(session, tenant=tenant)

    if session.get(UserRole, (user.id, role.id)) is None:
        session.add(UserRole(user_id=user.id, role_id=role.id))


def _ensure_system_catalog(session: Session, *, tenant: Tenant) -> Role:
    roles_by_code = {
        role.code: role
        for role in session.scalars(select(Role).where(Role.tenant_id == tenant.id))
    }
    role_definitions = (
        (SUPER_ADMIN_ROLE, "超级管理员"),
        (ADMIN_ROLE, "管理员"),
        (MEMBER_ROLE, "成员"),
    )
    for code, name in role_definitions:
        role = roles_by_code.get(code)
        if role is None:
            role = Role(tenant_id=tenant.id, code=code, name=name, is_system=True)
            session.add(role)
            session.flush()
            roles_by_code[code] = role
        else:
            role.name = name
            role.is_system = True

    super_admin_role = roles_by_code[SUPER_ADMIN_ROLE]
    admin_role = roles_by_code[ADMIN_ROLE]
    permissions_by_code: dict[str, Permission] = {}
    for code, name in PERMISSION_CATALOG:
        permission = session.scalar(select(Permission).where(Permission.code == code))
        if permission is None:
            permission = Permission(code=code, name=name)
            session.add(permission)
            session.flush()
        else:
            permission.name = name
        permissions_by_code[code] = permission
        if session.get(RolePermission, (super_admin_role.id, permission.id)) is None:
            session.add(RolePermission(role_id=super_admin_role.id, permission_id=permission.id))
        if (
            code not in SUPER_ADMIN_ONLY_PERMISSIONS
            and session.get(RolePermission, (admin_role.id, permission.id)) is None
        ):
            session.add(RolePermission(role_id=admin_role.id, permission_id=permission.id))
    restricted_permission_ids = [
        permissions_by_code[code].id
        for code in SUPER_ADMIN_ONLY_PERMISSIONS
        if code in permissions_by_code
    ]
    if restricted_permission_ids:
        session.execute(
            delete(RolePermission).where(
                RolePermission.role_id == admin_role.id,
                RolePermission.permission_id.in_(restricted_permission_ids),
            )
        )
    # Sessions intentionally disable autoflush. Persist newly discovered
    # catalog links now so a second authorization/catalog check in the same
    # request sees them instead of enqueueing duplicate composite keys.
    session.flush()
    return super_admin_role


def _permission_code_set(session: Session, user: User) -> set[str]:
    return set(
        session.scalars(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(UserRole, UserRole.role_id == RolePermission.role_id)
            .where(UserRole.user_id == user.id)
        )
    )


def _user_role_codes(session: Session, user: User) -> list[str]:
    return list(
        session.scalars(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id)
            .order_by(Role.code)
        )
    )


def _user_has_role(session: Session, user: User, role_code: str) -> bool:
    return session.scalar(
        select(Role.id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id, Role.code == role_code)
        .limit(1)
    ) is not None


def _role_permission_codes(session: Session, role: Role) -> list[str]:
    return list(
        session.scalars(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(RolePermission.role_id == role.id)
            .order_by(Permission.code)
        )
    )


def _permission_payload(permission: Permission) -> dict[str, object]:
    return {
        "id": str(permission.id),
        "code": permission.code,
        "name": permission.name,
        "description": permission.description,
    }


def _role_payload(session: Session, role: Role) -> dict[str, object]:
    assigned_member_count = session.scalar(
        select(func.count(UserRole.user_id)).where(UserRole.role_id == role.id)
    ) or 0
    return {
        "id": str(role.id),
        "code": role.code,
        "name": role.name,
        "isSystem": role.is_system,
        "nameEditable": not role.is_system,
        "deletable": not role.is_system and assigned_member_count == 0,
        "assignedMemberCount": assigned_member_count,
        "permissionCodes": _role_permission_codes(session, role),
        "permissionsEditable": role.code not in SYSTEM_MANAGED_PERMISSION_ROLES,
        "createdAt": role.created_at.isoformat(),
    }


def _member_payload(session: Session, user: User) -> dict[str, object]:
    roles = list(
        session.scalars(
            select(Role)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id)
            .order_by(Role.code)
        )
    )
    now = utcnow()
    active_session_count = session.scalar(
        select(func.count(SessionRecord.id)).where(
            SessionRecord.user_id == user.id,
            SessionRecord.revoked_at.is_(None),
            SessionRecord.expires_at > now,
        )
    ) or 0
    return {
        "id": str(user.id),
        "name": user.display_name,
        "avatarUrl": user.avatar_url,
        "status": user.status,
        "roles": [
            {"id": str(role.id), "code": role.code, "name": role.name}
            for role in roles
        ],
        "activeSessionCount": int(active_session_count),
        "lastLoginAt": user.last_login_at.isoformat() if user.last_login_at else None,
        "createdAt": user.created_at.isoformat(),
        "updatedAt": user.updated_at.isoformat(),
    }


def _directory_candidate_payload(user: FeishuDirectoryUser) -> dict[str, object]:
    return {
        "name": user.name,
        "avatarUrl": user.avatar_url,
        "departmentCount": len(user.department_ids),
        "isActivated": user.is_activated,
    }


def _session_payload(
    record: SessionRecord,
    user: User,
    *,
    current_session_id: uuid.UUID,
) -> dict[str, object]:
    return {
        "id": str(record.id),
        "member": {"id": str(user.id), "name": user.display_name},
        "createdAt": record.created_at.isoformat(),
        "lastSeenAt": record.last_seen_at.isoformat(),
        "expiresAt": record.expires_at.isoformat(),
        "isCurrent": record.id == current_session_id,
    }


def _revoke_active_sessions(session: Session, user_id: uuid.UUID, now: datetime) -> int:
    result = session.execute(
        update(SessionRecord)
        .where(
            SessionRecord.user_id == user_id,
            SessionRecord.revoked_at.is_(None),
            SessionRecord.expires_at > now,
        )
        .values(revoked_at=now)
    )
    return int(result.rowcount or 0)


def _request_session_token(cookie_token: str | None, authorization: str | None) -> str | None:
    if authorization is not None:
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.casefold() != "bearer" or not token.strip():
            raise HTTPException(status_code=401, detail={"code": "authentication_required"})
        return token.strip()
    return cookie_token


def _identity_payload(session: Session, user: User, tenant: Tenant) -> dict[str, object]:
    role_codes = list(
        session.scalars(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id)
            .order_by(Role.code)
        )
    )
    permission_codes = list(
        session.scalars(
            select(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id)
            .distinct()
            .order_by(Permission.code)
        )
    )
    return {
        "user": {
            "id": str(user.id),
            "name": user.display_name,
            "avatarUrl": user.avatar_url,
            "status": user.status,
        },
        "tenant": {"id": str(tenant.id), "name": tenant.name},
        "roles": role_codes,
        "permissions": permission_codes,
    }


def _authenticated_identity(
    session: Session,
    raw_token: str | None,
) -> tuple[SessionRecord, User, Tenant]:
    if not raw_token:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    record = session.scalar(
        select(SessionRecord).where(SessionRecord.token_hash == hash_token(raw_token))
    )
    now = utcnow()
    if record is None or record.revoked_at is not None or as_utc(record.expires_at) <= now:
        raise HTTPException(status_code=401, detail={"code": "session_invalid"})
    user = session.get(User, record.user_id)
    if user is None or user.status != "active":
        raise HTTPException(status_code=403, detail={"code": "user_disabled"})
    tenant = session.get(Tenant, user.tenant_id)
    if tenant is None or tenant.status != "active":
        raise HTTPException(status_code=403, detail={"code": "tenant_disabled"})
    return record, user, tenant


def _add_audit(
    session: Session,
    *,
    request_id: str,
    action: str,
    result: str,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            result=result,
            request_id=request_id,
            details=details or {},
        )
    )
