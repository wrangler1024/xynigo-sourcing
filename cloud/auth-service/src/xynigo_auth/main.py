import logging
import uuid
from contextlib import asynccontextmanager
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from .config import Settings
from .database import Database
from .feishu import FeishuIdentity, FeishuOAuthClient, OAuthClient, OAuthProviderError
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
from .security import hash_token, pkce_challenge, random_url_token


logger = logging.getLogger(__name__)


SUPER_ADMIN_ROLE = "super_admin"
SUPER_ADMIN_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("system.member.manage", "成员管理"),
    ("system.role.manage", "角色管理"),
    ("system.lark_connection.manage", "飞书连接管理"),
    ("system.integration.manage", "外部服务管理"),
    ("system.audit.read", "审计日志查看"),
    ("resource.buyer.read", "买家号查看"),
    ("resource.buyer.credential.read", "买家号敏感凭证查看"),
    ("resource.buyer.import", "买家号入库"),
    ("resource.environment.create", "采购环境创建"),
    ("fulfillment.order.read", "履约订单查看"),
    ("fulfillment.order.export", "履约订单导出"),
)


class LocalLoginPollBody(BaseModel):
    pollToken: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def create_app(
    settings: Settings | None = None,
    oauth_client: OAuthClient | None = None,
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        database.dispose()

    app = FastAPI(
        title="Xynigo Auth Service",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.oauth_client = oauth_client
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
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
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

    @app.get("/v1/auth/local/complete", response_class=PlainTextResponse)
    def local_login_complete() -> str:
        return "Xynigo 登录成功，可以关闭此页面并返回本地系统。"

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
    role = session.scalar(
        select(Role).where(Role.tenant_id == tenant.id, Role.code == SUPER_ADMIN_ROLE)
    )
    if role is None:
        role = Role(tenant_id=tenant.id, code=SUPER_ADMIN_ROLE, name="超级管理员", is_system=True)
        session.add(role)
        session.flush()

    if session.get(UserRole, (user.id, role.id)) is None:
        session.add(UserRole(user_id=user.id, role_id=role.id))

    for code, name in SUPER_ADMIN_PERMISSIONS:
        permission = session.scalar(select(Permission).where(Permission.code == code))
        if permission is None:
            permission = Permission(code=code, name=name)
            session.add(permission)
            session.flush()
        if session.get(RolePermission, (role.id, permission.id)) is None:
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))


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
    details: dict[str, str] | None = None,
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
