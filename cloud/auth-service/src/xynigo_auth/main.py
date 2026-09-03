"""HTTP 接口层（FastAPI），相当于 Java 的 Controller + 一部分启动配置。

路由写在本文件；采购业务委托 PurchaseOrderService；登录走飞书 OAuth 与本地执行器桥。
"""

import base64
import hashlib
import logging
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .business_log import BusinessLogService
from .buyer_account_contract import (
    BuyerAccountPreflightBody,
    BuyerAccountSnapshotBody,
    safe_snapshot_items,
)
from .buyer_account_service import BuyerAccountService
from .buyer_credential_crypto import BuyerCredentialCipher
from .checkout_contract import (
    CheckoutAttemptAbandonBody,
    CheckoutAttemptBeginBody,
    CheckoutAttemptCreateBody,
    CheckoutAttemptReviseBody,
    CheckoutCleanupResultBody,
    CheckoutPaymentResultBody,
    ShipmentUpsertBody,
    plan_payload,
)
from .checkout_service import ProcurementCheckoutService
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
from .feishu_operation_sync import (
    FeishuOperationBaseClient,
    FeishuOperationSyncWorker,
)
from .feishu_purchase_sync import FeishuPurchaseBaseClient, FeishuPurchaseSyncWorker
from .executor_contract import (
    ExecutorConfigWriteBody,
    ExecutorPairBody,
    ExecutorPollBody,
    ExecutorTaskCancelBody,
    ExecutorTaskFinishBody,
    ExecutorTaskLeaseBody,
    ExecutorTaskProgressBody,
    ExecutorTaskStartBody,
    ExecutorWorkspaceRpcBody,
    ExecutorWorkspaceSnapshotRefreshBody,
    PairingCodeCreateBody,
)
from .executor_payload_crypto import ExecutorPayloadCipher
from .executor_service import ExecutorChannelService, ExecutorServiceError
from .environment_plan_crypto import EnvironmentPlanCipher
from .environment_plan_service import (
    CloudEnvironmentPlanError,
    CloudEnvironmentPlanService,
)
from .local_executor_release import (
    latest_local_executor_release,
    resolve_local_executor_release_asset,
)
from .integration_contract import FeishuIntegrationWriteBody, FeishuReadProxyBody
from .logistics_export import build_logistics_workbook_export
from .models import (
    EnvironmentWorkspacePreference,
    LocalExecutor,
    LocalLoginRequest,
    OAuthLoginAttempt,
    Permission,
    Role,
    RolePermission,
    SessionRecord,
    Tenant,
    TenantFeishuIntegration,
    User,
    UserRole,
    WorkspaceViewPreference,
)
from .operation_contract import (
    EnvironmentCreationRunBody,
    EnvironmentCreationRunCreateBody,
    EnvironmentPlanParseBody,
    EnvironmentRetryRunCreateBody,
    EnvironmentWorkspacePreferenceBody,
    LogisticsQueryRunBody,
    LogisticsQueryRunCreateBody,
    WorkspaceViewPreferenceBody,
)
from .operation_service import OperationResultService, OperationRunService
from .procurement_import_contract import (
    ProcurementImportParseBody,
    ProcurementImportSyncBody,
    ProcurementImportTargetInspectBody,
    ProcurementImportTargetValidateBody,
)
from .procurement_import_crypto import ProcurementImportCipher
from .procurement_import_service import (
    CloudProcurementImportError,
    CloudProcurementImportService,
    ProcurementImportWorker,
)
from .workspace_rpc import (
    WorkspaceRpcError, workspace_rpc_is_local_config,
    workspace_rpc_permission)
from .procurement_import_sheet import FeishuSheetsGateway
from .purchase_contract import PurchaseDraft
from .purchase_service import PurchaseOrderService, PurchaseServiceError
from .security import hash_token, pkce_challenge, random_url_token
from .system_log import (
    SYSTEM_ERROR_CATEGORY,
    SYSTEM_RUNTIME_CATEGORY,
    SystemLogService,
    normalize_route,
    should_capture_http,
)
from .tenant_feishu import TenantFeishuError, TenantFeishuService
from .tenant_integration_crypto import (
    TenantIntegrationCipher,
    TenantIntegrationCipherError,
)

logger = logging.getLogger(__name__)


WEB_ROOT = Path(__file__).with_name("web")
WEB_ROOT_ASSETS = {
    "/favicon.ico": ("image/x-icon", WEB_ROOT / "favicon.ico"),
    "/xynigo-logo.png": ("image/png", WEB_ROOT / "xynigo-logo.png"),
    "/xynigo-x.png": ("image/png", WEB_ROOT / "xynigo-x.png"),
    "/xynigo-x.ico": ("image/x-icon", WEB_ROOT / "xynigo-x.ico"),
    "/preview-product-a.svg": ("image/svg+xml", WEB_ROOT / "preview-product-a.svg"),
    "/preview-product-b.svg": ("image/svg+xml", WEB_ROOT / "preview-product-b.svg"),
    "/preview-product-c.svg": ("image/svg+xml", WEB_ROOT / "preview-product-c.svg"),
}


def _web_inline_script_csp() -> str:
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.IGNORECASE | re.DOTALL)
    hashes = [
        base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
        for script in scripts
    ]
    if not hashes:
        raise RuntimeError("cloud Web workspace must contain an inline application script")
    return " ".join(f"'sha256-{digest}'" for digest in hashes)


WEB_INLINE_SCRIPT_CSP = _web_inline_script_csp()


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
    ("system.runtime_log.read", "系统运行日志查看"),
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
    ("executor.device.read", "本地执行器设备查看"),
    ("executor.device.pair", "本地执行器设备配对"),
    ("executor.device.revoke", "本地执行器设备撤销"),
    ("executor.config.read", "本地执行器配置查看"),
    ("executor.config.write", "本地执行器配置修改"),
)
SUPER_ADMIN_ONLY_PERMISSIONS = frozenset({
    "system.lark_connection.manage",
    "system.integration.manage",
    "resource.ip.credential.manage",
})
SYSTEM_MANAGED_PERMISSION_ROLES = frozenset({SUPER_ADMIN_ROLE, ADMIN_ROLE})
REQUEST_CONTEXT_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")


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


class ProcurementReturnBody(BaseModel):
    reason: str = Field(min_length=2, max_length=300)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("return reason is required")
        return normalized


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
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def create_app(
    settings: Settings | None = None,
    oauth_client: OAuthClient | None = None,
    directory_client: DirectoryClient | None = None,
    database: Database | None = None,
    procurement_import_gateway: FeishuSheetsGateway | None = None,
    feishu_integration_transport: httpx.BaseTransport | None = None,
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
    buyer_credential_key = settings.buyer_credential_encryption_key.get_secret_value()
    buyer_credential_cipher = (
        BuyerCredentialCipher(buyer_credential_key) if buyer_credential_key else None
    )
    executor_payload_cipher = (
        ExecutorPayloadCipher(buyer_credential_key) if buyer_credential_key else None
    )
    tenant_feishu_service = TenantFeishuService(
        cipher=TenantIntegrationCipher(buyer_credential_key),
        fallback_app_id=settings.feishu_app_id,
        fallback_app_secret=settings.feishu_app_secret.get_secret_value(),
        transport=feishu_integration_transport,
    )
    environment_plan_service = (
        CloudEnvironmentPlanService(
            cipher=EnvironmentPlanCipher(buyer_credential_key),
            plan_ttl_seconds=settings.environment_plan_ttl_seconds,
            max_active_plans_per_tenant=(
                settings.environment_plan_max_active_plans_per_tenant
            ),
        )
        if buyer_credential_key
        else None
    )
    operation_sync_worker = None
    if settings.feishu_operation_sync_enabled:
        operation_sync_worker = FeishuOperationSyncWorker(
            session_factory=database.session_factory,
            client=FeishuOperationBaseClient(
                app_id=settings.feishu_app_id,
                app_secret=settings.feishu_app_secret.get_secret_value(),
                base_token=settings.feishu_operation_base_token,
            ),
            buyer_account_table_id=settings.feishu_buyer_account_table_id,
            environment_table_id=settings.feishu_environment_result_table_id,
            logistics_table_id=settings.feishu_logistics_result_table_id,
            buyer_credential_cipher=buyer_credential_cipher,
            interval_seconds=settings.feishu_operation_sync_interval_seconds,
        )
    purchase_sync_worker = None
    if settings.feishu_purchase_sync_enabled:
        purchase_sync_worker = FeishuPurchaseSyncWorker(
            session_factory=database.session_factory,
            client=FeishuPurchaseBaseClient(
                app_id=settings.feishu_app_id,
                app_secret=settings.feishu_app_secret.get_secret_value(),
                base_token=settings.feishu_purchase_base_token,
                master_table_id=settings.feishu_purchase_order_table_id,
                line_table_id=settings.feishu_purchase_line_table_id,
            ),
            interval_seconds=settings.feishu_purchase_sync_interval_seconds,
        )
    procurement_import_service = None
    procurement_import_worker = None
    if settings.procurement_import_enabled:
        procurement_import_gateway = procurement_import_gateway or FeishuSheetsGateway(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret.get_secret_value(),
        )
        procurement_import_service = CloudProcurementImportService(
            session_factory=database.session_factory,
            gateway=procurement_import_gateway,
            cipher=ProcurementImportCipher(buyer_credential_key),
            plan_ttl_seconds=settings.procurement_import_plan_ttl_seconds,
            max_active_plans_per_tenant=(
                settings.procurement_import_max_active_plans_per_tenant
            ),
        )
        procurement_import_worker = ProcurementImportWorker(
            service=procurement_import_service,
            interval_seconds=settings.procurement_import_worker_interval_seconds,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if operation_sync_worker is not None:
            operation_sync_worker.start()
        if purchase_sync_worker is not None:
            purchase_sync_worker.start()
        if procurement_import_worker is not None:
            procurement_import_worker.start()
        try:
            yield
        finally:
            if procurement_import_worker is not None:
                procurement_import_worker.stop()
            if purchase_sync_worker is not None:
                purchase_sync_worker.stop()
            if operation_sync_worker is not None:
                operation_sync_worker.stop()
            database.dispose()

    app = FastAPI(
        title="Xynigo Auth Service",
        version="0.13.20",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.oauth_client = oauth_client
    app.state.directory_client = directory_client
    app.state.buyer_credential_cipher = buyer_credential_cipher
    app.state.executor_payload_cipher = executor_payload_cipher
    app.state.environment_plan_service = environment_plan_service
    app.state.operation_sync_worker = operation_sync_worker
    app.state.purchase_sync_worker = purchase_sync_worker
    app.state.procurement_import_service = procurement_import_service
    app.state.procurement_import_worker = procurement_import_worker
    app.state.last_system_log_prune_at = 0.0
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)

    def get_session() -> Iterator[Session]:
        yield from database.sessions()

    SessionDep = Annotated[Session, Depends(get_session)]

    def executor_channel(session: Session) -> ExecutorChannelService:
        return ExecutorChannelService(
            session,
            pairing_ttl_seconds=settings.executor_pairing_ttl_seconds,
            lease_seconds=settings.executor_lease_seconds,
            online_window_seconds=settings.executor_online_window_seconds,
            payload_cipher=executor_payload_cipher,
        )

    def authenticated_executor(
        request: Request,
        session: Session,
        authorization: str | None,
    ) -> tuple[ExecutorChannelService, LocalExecutor]:
        # Device credentials are intentionally isolated from user sessions.
        # Rejecting cookies also prevents a browser session from accidentally
        # becoming a device session when both are present.
        if request.headers.get("Cookie"):
            raise ExecutorServiceError("executor_cookie_not_allowed", status_code=401)
        scheme, separator, raw_credential = str(authorization or "").partition(" ")
        if not separator or scheme.casefold() != "bearer" or not raw_credential.strip():
            raise ExecutorServiceError(
                "executor_authentication_required", status_code=401
            )
        service = executor_channel(session)
        executor = service.authenticate(raw_credential.strip())
        request.state.tenant_id = executor.tenant_id
        request.state.actor_name = executor.display_name
        request.state.log_source = "local_executor_device"
        return service, executor

    def bind_request_identity(request: Request, *, user: User, tenant: Tenant) -> None:
        request.state.tenant_id = tenant.id
        request.state.actor_user_id = user.id
        request.state.actor_name = user.display_name

    def persist_http_system_log(
        request: Request,
        *,
        status_code: int,
        duration_ms: int,
        exception: Exception | None = None,
    ) -> None:
        route_object = request.scope.get("route")
        route = normalize_route(getattr(route_object, "path", None) or "/unmatched")
        if not should_capture_http(
            method=request.method,
            route=route,
            status_code=status_code,
            trace_id=request.state.trace_id,
            runtime_sample_rate=settings.system_log_runtime_sample_rate,
        ):
            return
        is_error = status_code >= 500 or exception is not None
        level = "error" if is_error else "warning" if status_code >= 400 else "info"
        try:
            with database.session_factory() as system_log_session:
                service = SystemLogService(system_log_session)
                service.append(
                    tenant_id=getattr(request.state, "tenant_id", None),
                    actor_user_id=getattr(request.state, "actor_user_id", None),
                    actor_name=getattr(request.state, "actor_name", None),
                    category=SYSTEM_ERROR_CATEGORY if is_error else SYSTEM_RUNTIME_CATEGORY,
                    level=level,
                    service="auth_service",
                    component="http_api",
                    environment=settings.environment,
                    event_type="http.request.failed" if is_error else "http.request.completed",
                    message="Unhandled application exception" if exception else "HTTP request completed",
                    request_id=request.state.request_id,
                    trace_id=request.state.trace_id,
                    retention_days=settings.system_log_retention_days,
                    source=getattr(request.state, "log_source", "web_api"),
                    client_version=getattr(request.state, "client_version", None) or None,
                    http_method=request.method,
                    route=route,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    exception_type=type(exception).__name__ if exception else None,
                    error_code="unhandled_exception"
                    if exception
                    else f"http_{status_code}"
                    if status_code >= 400
                    else None,
                    details={"handled": exception is None},
                )
                system_log_session.flush()
                prune_now = time.monotonic()
                if prune_now - app.state.last_system_log_prune_at >= 3600:
                    service.enforce_retention(
                        retention_days=settings.system_log_retention_days,
                        max_rows_per_tenant=settings.system_log_max_rows_per_tenant,
                    )
                    app.state.last_system_log_prune_at = prune_now
                system_log_session.commit()
        except Exception:
            logger.exception(
                "Failed to append system log request_id=%s",
                request.state.request_id,
            )

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
        started_at = time.perf_counter()
        incoming_request_id = str(request.headers.get("X-Request-ID") or "").strip()
        request.state.request_id = (
            incoming_request_id
            if REQUEST_CONTEXT_PATTERN.fullmatch(incoming_request_id)
            else uuid.uuid4().hex
        )
        incoming_trace_id = str(request.headers.get("X-Trace-ID") or "").strip()
        request.state.trace_id = (
            incoming_trace_id
            if REQUEST_CONTEXT_PATTERN.fullmatch(incoming_trace_id)
            else request.state.request_id
        )
        incoming_source = str(request.headers.get("X-Xynigo-Source") or "").strip()
        request.state.log_source = (
            incoming_source
            if REQUEST_CONTEXT_PATTERN.fullmatch(incoming_source)
            else "web_api"
        )
        request.state.client_version = " ".join(
            str(request.headers.get("X-Xynigo-Client-Version") or "").split()
        )[:64]
        cookie_authenticated_mutation = (
            request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
            and bool(request.cookies.get(settings.cookie_name))
            and not request.headers.get("Authorization")
        )
        if (
            cookie_authenticated_mutation
            and request.headers.get("X-Xynigo-Web-CSRF") != "same-origin"
        ):
            response = JSONResponse(
                status_code=403,
                content={"detail": {"code": "web_csrf_required"}},
            )
        else:
            try:
                response = await call_next(request)
            except Exception as exc:
                persist_http_system_log(
                    request,
                    status_code=500,
                    duration_ms=round((time.perf_counter() - started_at) * 1000),
                    exception=exc,
                )
                raise
        persist_http_system_log(
            request,
            status_code=response.status_code,
            duration_ms=round((time.perf_counter() - started_at) * 1000),
        )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Trace-ID"] = request.state.trace_id
        response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; "
                f"script-src {WEB_INLINE_SCRIPT_CSP}; style-src 'unsafe-inline'; "
                "img-src 'self' data: blob: https:; connect-src 'self'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            )
        elif request.url.path == "/v1/auth/local/complete":
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

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        action, business_object_id = _validation_log_target(
            request.method, request.url.path
        )
        if action is not None:
            try:
                raw_token = _request_session_token(
                    request.cookies.get(settings.cookie_name),
                    request.headers.get("Authorization"),
                )
                with database.session_factory() as audit_session:
                    _record_validation_failure(
                        audit_session,
                        request=request,
                        raw_token=raw_token,
                        action=action,
                        business_object_id=business_object_id,
                    )
                    audit_session.commit()
            except HTTPException:
                # Invalid unauthenticated requests have no trusted operator
                # context and therefore are not business operation logs.
                pass
            except Exception:
                logger.exception(
                    "Failed to append validation business log request_id=%s",
                    request.state.request_id,
                )
        safe_errors = [
            {
                "type": str(error.get("type") or "validation_error")[:120],
                "loc": [str(item)[:120] for item in error.get("loc", ())],
                "msg": str(error.get("msg") or "请求参数无效")[:300],
            }
            for error in exc.errors()[:50]
        ]
        return JSONResponse(
            status_code=422,
            content={
                "detail": safe_errors,
                "code": "validation_failed",
                "requestId": request.state.request_id,
                "traceId": request.state.trace_id,
            },
        )

    @app.exception_handler(ExecutorServiceError)
    async def executor_service_error_handler(
        request: Request,
        exc: ExecutorServiceError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": {"code": exc.code},
                "requestId": request.state.request_id,
                "traceId": request.state.trace_id,
            },
        )

    @app.get("/", response_class=FileResponse)
    def web_workspace() -> FileResponse:
        """云端 Web 工作台入口；不依赖员工电脑上的本地执行器。"""

        return FileResponse(WEB_ROOT / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/favicon.ico", response_class=FileResponse)
    @app.get("/xynigo-logo.png", response_class=FileResponse)
    @app.get("/xynigo-x.png", response_class=FileResponse)
    @app.get("/xynigo-x.ico", response_class=FileResponse)
    @app.get("/preview-product-a.svg", response_class=FileResponse)
    @app.get("/preview-product-b.svg", response_class=FileResponse)
    @app.get("/preview-product-c.svg", response_class=FileResponse)
    def canonical_web_asset(request: Request) -> FileResponse:
        media_type, path = WEB_ROOT_ASSETS[request.url.path]
        return FileResponse(path, media_type=media_type)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(session: SessionDep) -> dict[str, str]:
        session.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.get("/v1/local-executor/releases/latest")
    def local_executor_latest_release(
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        """Return the reviewed installer catalog for authenticated members."""

        raw_token = _request_session_token(session_token, authorization)
        record, user, tenant = _authenticated_identity(session, raw_token)
        bind_request_identity(request, user=user, tenant=tenant)
        record.last_seen_at = utcnow()
        session.commit()
        return JSONResponse(
            latest_local_executor_release(),
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get(
        "/v1/local-executor/releases/{platform_key}/{variant}/download",
        response_class=FileResponse,
    )
    def download_local_executor_release(
        platform_key: str,
        variant: Literal["primary", "green"],
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> FileResponse:
        """Serve one verified immutable installer through the system origin."""

        raw_token = _request_session_token(session_token, authorization)
        record, user, tenant = _authenticated_identity(session, raw_token)
        bind_request_identity(request, user=user, tenant=tenant)
        asset = resolve_local_executor_release_asset(platform_key, variant)
        if asset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "local_executor_release_asset_not_found"},
            )
        record.last_seen_at = utcnow()
        session.commit()

        expected_size = int(asset["size"])
        asset_name = str(asset["assetName"])
        try:
            asset_root = Path(settings.local_executor_asset_dir).resolve(strict=True)
            asset_path = (asset_root / asset_name).resolve(strict=True)
            if asset_path.parent != asset_root or not asset_path.is_file():
                raise OSError("asset path is outside the reviewed directory")
            if asset_path.stat().st_size != expected_size:
                raise OSError("asset size does not match the release catalog")
            digest = hashlib.sha256()
            with asset_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != str(asset["sha256"]):
                raise OSError("asset digest does not match the release catalog")
        except OSError:
            logger.error(
                "local executor asset storage validation failed",
                extra={"platform": platform_key, "variant": variant},
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "local_executor_download_unavailable"},
            ) from None

        return FileResponse(
            asset_path,
            filename=asset_name,
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Xynigo-Asset-SHA256": str(asset["sha256"]),
            },
        )

    @app.get("/v1/auth/feishu/start", response_class=RedirectResponse)
    def start_feishu_login(session: SessionDep) -> RedirectResponse:
        url = create_authorization_url(session)
        return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    @app.get("/v1/auth/web/status")
    def web_login_status(
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
    ) -> JSONResponse:
        """Return a quiet 200 status so the public login shell does not emit a 401."""

        if not session_token:
            return JSONResponse({"authenticated": False})
        try:
            record, user, tenant = _authenticated_identity(session, session_token)
        except HTTPException:
            return JSONResponse({"authenticated": False})
        bind_request_identity(request, user=user, tenant=tenant)
        _ensure_system_catalog(session, tenant=tenant)
        now = utcnow()
        record.last_seen_at = now
        renewed_max_age = _slide_session_expiry(record, settings=settings, now=now)
        identity = _identity_payload(session, user, tenant)
        session.commit()
        response = JSONResponse({"authenticated": True, "identity": identity})
        if renewed_max_age is not None:
            response.set_cookie(
                key=settings.cookie_name,
                value=session_token,
                max_age=renewed_max_age,
                secure=settings.cookie_secure,
                httponly=True,
                samesite="lax",
                path="/",
            )
        return response

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

        _ensure_system_catalog(session, tenant=tenant)
        bind_request_identity(request, user=user, tenant=tenant)
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

        _ensure_system_catalog(session, tenant=tenant)
        bind_request_identity(request, user=user, tenant=tenant)
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
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        raw_token = _request_session_token(session_token, authorization)
        record, user, tenant = _authenticated_identity(session, raw_token)
        bind_request_identity(request, user=user, tenant=tenant)
        _ensure_system_catalog(session, tenant=tenant)
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
        bind_request_identity(request, user=user, tenant=tenant)
        _ensure_system_catalog(session, tenant=tenant)
        if permission not in _permission_code_set(session, user):
            _add_audit(
                session,
                request_id=request.state.request_id,
                action=audit_action,
                result="denied",
                outcome="permission_denied",
                tenant_id=tenant.id,
                actor_user_id=user.id,
                failure_reason="permission_denied",
                details={"permission": permission},
                **_request_log_context(request),
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

    def append_executor_audit(
        request: Request,
        session: Session,
        actor: AdminActor,
        *,
        action: str,
        object_id: uuid.UUID | str | None,
        summary: dict[str, Any],
    ) -> None:
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            module="local_executor",
            category="configuration",
            business_object_type="local_executor",
            business_object_id=object_id,
            change_summary=summary,
            details=summary,
            **_request_log_context(request),
        )
        session.commit()

    @app.get("/v1/executors")
    def list_local_executors(
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="executor.device.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="executor.device.list",
        )
        items = executor_channel(session).list_executors(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
        )
        session.commit()
        return {"items": items}

    @app.post(
        "/v1/executors/pairing-codes",
        status_code=status.HTTP_201_CREATED,
    )
    def create_executor_pairing_code(
        body: PairingCodeCreateBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="executor.device.pair",
            session_token=session_token,
            authorization=authorization,
            audit_action="executor.pairing_code.create",
        )
        record, raw_code = executor_channel(session).create_pairing_code(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            display_name_hint=body.displayNameHint,
        )
        append_executor_audit(
            request,
            session,
            actor,
            action="executor.pairing_code.create",
            object_id=record.id,
            summary={"expiresInSeconds": settings.executor_pairing_ttl_seconds},
        )
        return {
            "pairingRequestId": str(record.id),
            "pairingCode": f"{raw_code[:4]}-{raw_code[4:]}",
            "expiresAt": record.expires_at.isoformat(),
            "expiresIn": settings.executor_pairing_ttl_seconds,
        }

    @app.get("/v1/executors/pairing-codes/{pairing_id}")
    def get_executor_pairing_status(
        pairing_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="executor.device.pair",
            session_token=session_token,
            authorization=authorization,
            audit_action="executor.pairing_code.status.read",
        )
        return executor_channel(session).pairing_status(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            pairing_id=pairing_id,
        )

    @app.post("/v1/executor-channel/pair", status_code=status.HTTP_201_CREATED)
    def pair_local_executor(
        body: ExecutorPairBody,
        request: Request,
        session: SessionDep,
    ) -> dict[str, object]:
        if request.headers.get("Cookie"):
            raise ExecutorServiceError("executor_cookie_not_allowed", status_code=401)
        executor, credential = executor_channel(session).pair(body)
        request.state.tenant_id = executor.tenant_id
        request.state.actor_name = executor.display_name
        request.state.log_source = "local_executor_device"
        return {
            "executorId": str(executor.id),
            "deviceCredential": credential,
            "credentialType": "Bearer",
            "protocolVersion": executor.protocol_version,
            "pollPath": "/v1/executor-channel/poll",
        }

    @app.post("/v1/executor-channel/session")
    def issue_executor_owner_session(
        request: Request,
        session: SessionDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        _service, executor = authenticated_executor(
            request, session, authorization
        )
        user = session.get(User, executor.owner_user_id)
        tenant = session.get(Tenant, executor.tenant_id)
        if user is None or user.status != "active":
            raise ExecutorServiceError("user_disabled", status_code=403)
        if tenant is None or tenant.status != "active":
            raise ExecutorServiceError("tenant_disabled", status_code=403)
        _ensure_system_catalog(session, tenant=tenant)
        now = utcnow()
        expires_at = now + timedelta(seconds=settings.session_ttl_seconds)
        raw_session_token = random_url_token(48)
        session.add(
            SessionRecord(
                user_id=user.id,
                token_hash=hash_token(raw_session_token),
                last_seen_at=now,
                expires_at=expires_at,
            )
        )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action="executor.owner_session.issue",
            result="success",
            tenant_id=tenant.id,
            actor_user_id=user.id,
            details={"executorId": str(executor.id)},
        )
        session.commit()
        return {
            "sessionToken": raw_session_token,
            "sessionExpiresAt": expires_at.isoformat(),
        }

    @app.post("/v1/executors/{executor_id}/revoke")
    def revoke_local_executor(
        executor_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="executor.device.revoke",
            session_token=session_token,
            authorization=authorization,
            audit_action="executor.device.revoke",
        )
        service = executor_channel(session)
        executor = service.revoke(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            executor_id=executor_id,
        )
        append_executor_audit(
            request,
            session,
            actor,
            action="executor.device.revoke",
            object_id=executor.id,
            summary={"status": "revoked"},
        )
        return {"executor": service.executor_payload(executor)}

    @app.get("/v1/executors/{executor_id}/runtime-summary")
    def local_executor_runtime_summary(
        executor_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="executor.device.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="executor.runtime_summary.read",
        )
        payload = executor_channel(session).runtime_summary(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            executor_id=executor_id,
        )
        session.commit()
        return payload

    @app.post(
        "/v1/executors/{executor_id}/config/read",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def read_local_executor_config(
        executor_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="executor.config.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="executor.config.read.request",
        )
        service = executor_channel(session)
        try:
            task = service.create_config_task(
                tenant_id=actor.tenant.id,
                user_id=actor.user.id,
                executor_id=executor_id,
                task_type="config.read.v1",
                payload={},
            )
        except ExecutorServiceError as exc:
            if exc.code != "executor_task_busy":
                raise
            cached = service.cached_config_payload(
                tenant_id=actor.tenant.id,
                user_id=actor.user.id,
                executor_id=executor_id,
            )
            if cached is None:
                raise
            append_executor_audit(
                request,
                session,
                actor,
                action="executor.config.read.cached",
                object_id=executor_id,
                summary={"source": "workspace_snapshot"},
            )
            return {
                "task": None,
                "cached": True,
                "cachedResult": cached,
            }
        append_executor_audit(
            request,
            session,
            actor,
            action="executor.config.read.request",
            object_id=executor_id,
            summary={"taskId": str(task.id)},
        )
        return {"task": service.task_payload(task)}

    @app.put(
        "/v1/executors/{executor_id}/config",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def write_local_executor_config(
        executor_id: uuid.UUID,
        body: ExecutorConfigWriteBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="executor.config.write",
            session_token=session_token,
            authorization=authorization,
            audit_action="executor.config.write.request",
        )
        service = executor_channel(session)
        task = service.create_config_task(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            executor_id=executor_id,
            task_type="config.write.v1",
            payload={
                "expectedRevision": body.expectedRevision,
                "config": body.config,
            },
            idempotency_key=body.idempotencyKey,
        )
        append_executor_audit(
            request,
            session,
            actor,
            action="executor.config.write.request",
            object_id=executor_id,
            summary={"taskId": str(task.id), "expectedRevision": body.expectedRevision},
        )
        return {"task": service.task_payload(task)}

    @app.post(
        "/v1/executors/{executor_id}/workspace-rpc",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def execute_local_workspace_rpc(
        executor_id: uuid.UUID,
        body: ExecutorWorkspaceRpcBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        if workspace_rpc_is_local_config(body.path):
            permission = (
                "executor.config.read"
                if body.method == "GET" else "executor.config.write"
            )
        else:
            try:
                permission = workspace_rpc_permission(body.method, body.path)
            except WorkspaceRpcError as exc:
                raise HTTPException(
                    status_code=422, detail={"code": exc.code}
                ) from exc
        actor = authorize_request(
            request,
            session,
            permission=permission,
            session_token=session_token,
            authorization=authorization,
            audit_action="executor.workspace_rpc.request",
        )
        service = executor_channel(session)
        task = service.create_config_task(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            executor_id=executor_id,
            task_type="workspace.rpc.v1",
            payload={
                "method": body.method,
                "path": body.path,
                "body": body.body,
            },
            idempotency_key=body.idempotencyKey,
        )
        append_executor_audit(
            request,
            session,
            actor,
            action="executor.workspace_rpc.request",
            object_id=executor_id,
            summary={
                "taskId": str(task.id),
                "method": body.method,
                "path": body.path.split("?", 1)[0],
            },
        )
        return {"task": service.task_payload(task)}

    @app.get("/v1/executors/{executor_id}/workspace-snapshot")
    def get_executor_workspace_snapshot(
        executor_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="resource.environment.create",
            session_token=session_token,
            authorization=authorization,
            audit_action="executor.workspace_snapshot.read",
        )
        payload = executor_channel(session).workspace_snapshot_payload(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            executor_id=executor_id,
        )
        return merge_cloud_environment_preferences(
            session, actor.tenant.id, actor.user.id, payload
        )

    @app.post(
        "/v1/executors/{executor_id}/workspace-snapshot",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def refresh_executor_workspace_snapshot(
        executor_id: uuid.UUID,
        body: ExecutorWorkspaceSnapshotRefreshBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="resource.environment.create",
            session_token=session_token,
            authorization=authorization,
            audit_action="executor.workspace_snapshot.refresh",
        )
        service = executor_channel(session)
        task = service.create_config_task(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            executor_id=executor_id,
            task_type="workspace.snapshot.v1",
            payload={},
            idempotency_key=body.idempotencyKey,
        )
        payload = {
            **service.workspace_snapshot_payload(
                tenant_id=actor.tenant.id,
                user_id=actor.user.id,
                executor_id=executor_id,
            ),
            "task": service.task_payload(task),
        }
        return merge_cloud_environment_preferences(
            session, actor.tenant.id, actor.user.id, payload
        )

    def merge_cloud_environment_preferences(
        session: Session,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        record = session.get(EnvironmentWorkspacePreference, (tenant_id, user_id))
        if record is None or not isinstance(payload.get("snapshot"), dict):
            return payload
        result = dict(payload)
        snapshot = dict(payload["snapshot"])
        preferences = dict(snapshot.get("preferences") or {})
        tags = dict(preferences.get("purchaseTags") or {})
        tags.update({
            key: str(value or "")
            for key, value in dict(record.purchase_tags or {}).items()
            if key in {"US", "MX"}
        })
        preferences["purchaseSite"] = record.purchase_site
        preferences["purchaseTags"] = {
            "US": str(tags.get("US") or ""),
            "MX": str(tags.get("MX") or ""),
        }
        snapshot["preferences"] = preferences
        result["snapshot"] = snapshot
        return result

    @app.get("/v1/environment-preferences")
    def get_environment_preferences(
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="resource.environment.create",
            session_token=session_token,
            authorization=authorization,
            audit_action="resource.environment.preference.read",
        )
        record = session.get(
            EnvironmentWorkspacePreference, (actor.tenant.id, actor.user.id)
        )
        tags = dict(record.purchase_tags or {}) if record else {}
        return {
            "purchaseSite": record.purchase_site if record else "MX",
            "purchaseTags": {
                "US": str(tags.get("US") or ""),
                "MX": str(tags.get("MX") or ""),
            },
        }

    @app.put("/v1/environment-preferences")
    def put_environment_preferences(
        body: EnvironmentWorkspacePreferenceBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "resource.environment.preference.write"
        actor = authorize_request(
            request,
            session,
            permission="resource.environment.create",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        key = (actor.tenant.id, actor.user.id)
        record = session.get(EnvironmentWorkspacePreference, key)
        if record is None:
            record = EnvironmentWorkspacePreference(
                tenant_id=actor.tenant.id,
                user_id=actor.user.id,
                purchase_site=body.purchaseSite,
                purchase_tags={},
            )
            session.add(record)
        tags = dict(record.purchase_tags or {})
        tags.update(body.purchaseTags)
        record.purchase_site = body.purchaseSite
        record.purchase_tags = {
            key: str(value or "")
            for key, value in tags.items()
            if key in {"US", "MX"} and str(value or "")
        }
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="environment_workspace_preference",
            business_object_id=str(actor.user.id),
            change_summary={"purchaseSite": record.purchase_site},
            details={"purchaseSite": record.purchase_site},
            **_request_log_context(request),
        )
        session.commit()
        return {
            "purchaseSite": record.purchase_site,
            "purchaseTags": {
                "US": str(record.purchase_tags.get("US") or ""),
                "MX": str(record.purchase_tags.get("MX") or ""),
            },
        }

    @app.get("/v1/admin/integrations/feishu")
    def get_tenant_feishu_integration(
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="system.lark_connection.manage",
            session_token=session_token,
            authorization=authorization,
            audit_action="system.integration.feishu.read",
        )
        try:
            return tenant_feishu_service.public_status(
                session, actor.tenant.id, admin=True
            )
        except TenantFeishuError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code}
            ) from exc

    @app.put("/v1/admin/integrations/feishu")
    def put_tenant_feishu_integration(
        body: FeishuIntegrationWriteBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "system.integration.feishu.write"
        actor = authorize_request(
            request,
            session,
            permission="system.lark_connection.manage",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        record = session.get(TenantFeishuIntegration, actor.tenant.id)
        current_revision = record.revision if record else 0
        if body.expectedRevision != current_revision:
            raise HTTPException(
                status_code=409,
                detail={"code": "tenant_feishu_revision_conflict"},
            )
        app_secret = body.appSecret.get_secret_value()
        try:
            tenant_feishu_service.verify(actor.tenant.id, body.appId, app_secret)
            ciphertext = tenant_feishu_service.cipher.encrypt({
                "appId": body.appId,
                "appSecret": app_secret,
            })
        except (TenantFeishuError, TenantIntegrationCipherError) as exc:
            failure_code = (
                exc.code
                if isinstance(exc, TenantFeishuError)
                else "tenant_feishu_credential_encrypt_failed"
            )
            _add_audit(
                session,
                request_id=request.state.request_id,
                action=action,
                result="failure",
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                business_object_type="tenant_feishu_integration",
                business_object_id=str(actor.tenant.id),
                failure_reason=failure_code,
                details={"stage": "credential_verification"},
                **_request_log_context(request),
            )
            session.commit()
            raise HTTPException(
                status_code=exc.status_code if isinstance(exc, TenantFeishuError) else 503,
                detail={"code": failure_code},
            ) from exc
        finally:
            app_secret = ""
        now = utcnow()
        if record is None:
            record = TenantFeishuIntegration(
                tenant_id=actor.tenant.id,
                app_id=body.appId,
                credential_ciphertext=ciphertext,
                revision=1,
                configured_by_user_id=actor.user.id,
                verified_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
        else:
            record.app_id = body.appId
            record.credential_ciphertext = ciphertext
            record.revision += 1
            record.configured_by_user_id = actor.user.id
            record.verified_at = now
            record.updated_at = now
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="tenant_feishu_integration",
            business_object_id=str(actor.tenant.id),
            change_summary={"revision": record.revision, "configured": True},
            details={"source": "organization"},
            **_request_log_context(request),
        )
        session.commit()
        return tenant_feishu_service.public_status(
            session, actor.tenant.id, admin=True
        )

    @app.post("/v1/integrations/feishu/read")
    def proxy_tenant_feishu_read(
        body: FeishuReadProxyBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission=body.permission,
            session_token=session_token,
            authorization=authorization,
            audit_action="integration.feishu.read",
        )
        try:
            payload = tenant_feishu_service.proxy_get(
                session=session,
                tenant_id=actor.tenant.id,
                path=body.path,
                query=body.query,
            )
        except TenantFeishuError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code}
            ) from exc
        return {"ok": True, "data": payload}

    def normalized_view_key(view_key: str) -> str:
        normalized = str(view_key or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,63}", normalized):
            raise HTTPException(
                status_code=422, detail={"code": "workspace_view_key_invalid"}
            )
        return normalized

    @app.get("/v1/workspace/view-preferences/{view_key}")
    def get_workspace_view_preference(
        view_key: str,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="fulfillment.order.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="workspace.view_preference.read",
        )
        key = normalized_view_key(view_key)
        record = session.get(
            WorkspaceViewPreference, (actor.tenant.id, actor.user.id, key)
        )
        return {
            "viewKey": key,
            "schemaVersion": record.schema_version if record else 1,
            "settings": dict(record.settings or {}) if record else None,
        }

    @app.put("/v1/workspace/view-preferences/{view_key}")
    def put_workspace_view_preference(
        view_key: str,
        body: WorkspaceViewPreferenceBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "workspace.view_preference.write"
        actor = authorize_request(
            request,
            session,
            permission="fulfillment.order.read",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        key = normalized_view_key(view_key)
        identity = (actor.tenant.id, actor.user.id, key)
        record = session.get(WorkspaceViewPreference, identity)
        if record is None:
            record = WorkspaceViewPreference(
                tenant_id=actor.tenant.id,
                user_id=actor.user.id,
                view_key=key,
                schema_version=body.schemaVersion,
                settings={},
            )
            session.add(record)
        record.schema_version = body.schemaVersion
        record.settings = {
            "visibleFields": list(body.visibleFields),
            "fieldOrder": list(body.fieldOrder or body.visibleFields),
        }
        record.updated_at = utcnow()
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="workspace_view_preference",
            business_object_id=key,
            change_summary={"visibleFieldCount": len(body.visibleFields)},
            details={"viewKey": key},
            **_request_log_context(request),
        )
        session.commit()
        return {
            "viewKey": key,
            "schemaVersion": record.schema_version,
            "settings": dict(record.settings),
        }

    @app.get("/v1/environment-plans/latest")
    def latest_environment_plan(
        request: Request,
        session: SessionDep,
        site: Annotated[Literal["US", "MX"], Query()],
        environment_group: Annotated[
            str, Query(alias="environmentGroup", min_length=1, max_length=12)
        ],
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="resource.environment.create",
            session_token=session_token,
            authorization=authorization,
            audit_action="resource.environment.plan.latest",
        )
        if environment_plan_service is None:
            return {"plan": None}
        result = environment_plan_service.latest(
            session,
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            site=site,
            environment_group=environment_group,
        )
        if session.dirty:
            session.commit()
        return {"plan": result}

    @app.post(
        "/v1/environment-plans/parse",
        status_code=status.HTTP_201_CREATED,
    )
    def parse_environment_plan(
        body: EnvironmentPlanParseBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="resource.environment.create",
            session_token=session_token,
            authorization=authorization,
            audit_action="resource.environment.plan.parse",
        )
        if environment_plan_service is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "environment_plan_cloud_disabled",
                    "message": "云端买家号解析加密能力尚未启用",
                },
            )
        try:
            result = environment_plan_service.parse(
                session,
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                idempotency_key=body.idempotencyKey,
                filename=body.filename,
                content_base64=body.contentBase64,
                site=body.site,
                environment_group=body.environmentGroup,
            )
        except CloudEnvironmentPlanError as exc:
            session.rollback()
            _add_audit(
                session,
                request_id=request.state.request_id,
                action="resource.environment.plan.parse",
                result="denied" if exc.status in {409, 410, 422} else "failure",
                outcome="validation_failed" if exc.status == 422 else "business_conflict",
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                business_object_type="environment_account_plan",
                business_object_id=body.idempotencyKey,
                failure_reason=exc.code,
                details={"reason": exc.code, "site": body.site},
                **_request_log_context(request),
            )
            session.commit()
            raise HTTPException(
                status_code=exc.status,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        _add_audit(
            session,
            request_id=request.state.request_id,
            action="resource.environment.plan.parse",
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="environment_account_plan",
            business_object_id=result["cloudPlanId"],
            change_summary={
                "site": result["site"],
                "accountCount": result["count"],
                "runtime": "cloud",
                "reused": result["reused"],
            },
            details={
                "site": result["site"],
                "accountCount": result["count"],
                "reused": result["reused"],
                "uploadBytesApprox": len(body.contentBase64) * 3 // 4,
            },
            **_request_log_context(request),
        )
        session.commit()
        return result

    @app.get("/v1/executor-tasks/{task_id}")
    def get_executor_task(
        task_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="executor.device.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="executor.task.read",
        )
        service = executor_channel(session)
        task = service.get_task(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            task_id=task_id,
        )
        session.commit()
        return {"task": service.task_payload(task)}

    @app.post("/v1/executor-tasks/{task_id}/cancel")
    def cancel_executor_task(
        task_id: uuid.UUID,
        body: ExecutorTaskCancelBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="executor.config.write",
            session_token=session_token,
            authorization=authorization,
            audit_action="executor.task.cancel",
        )
        service = executor_channel(session)
        current = service.get_task(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            task_id=task_id,
        )
        if body.expectedStatus and current.status != body.expectedStatus:
            raise ExecutorServiceError("executor_task_state_conflict", status_code=409)
        task = service.cancel_task(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            task_id=task_id,
        )
        append_executor_audit(
            request,
            session,
            actor,
            action="executor.task.cancel",
            object_id=task.executor_id,
            summary={"taskId": str(task.id), "status": task.status},
        )
        return {"task": service.task_payload(task)}

    @app.post("/v1/executor-channel/poll")
    def poll_executor_channel(
        body: ExecutorPollBody,
        request: Request,
        session: SessionDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        service, executor = authenticated_executor(request, session, authorization)
        if body.waitSeconds > settings.executor_poll_timeout_seconds:
            body = body.model_copy(
                update={"waitSeconds": settings.executor_poll_timeout_seconds}
            )
        return service.poll(
            executor=executor,
            body=body,
            trace_id=request.state.trace_id,
        )

    @app.post("/v1/executor-channel/tasks/{task_id}/start")
    def start_executor_task(
        task_id: uuid.UUID,
        body: ExecutorTaskStartBody,
        request: Request,
        session: SessionDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        service, executor = authenticated_executor(request, session, authorization)
        task = service.start_task(
            executor=executor,
            task_id=task_id,
            lease_token=body.leaseToken,
            trace_id=request.state.trace_id,
        )
        return {"task": service.task_payload(task)}

    @app.put("/v1/executor-channel/tasks/{task_id}/lease")
    def renew_executor_task_lease(
        task_id: uuid.UUID,
        body: ExecutorTaskLeaseBody,
        request: Request,
        session: SessionDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        service, executor = authenticated_executor(request, session, authorization)
        task = service.renew_lease(
            executor=executor,
            task_id=task_id,
            lease_token=body.leaseToken,
        )
        return {"task": service.task_payload(task)}

    @app.post("/v1/executor-channel/tasks/{task_id}/progress")
    def report_executor_task_progress(
        task_id: uuid.UUID,
        body: ExecutorTaskProgressBody,
        request: Request,
        session: SessionDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        service, executor = authenticated_executor(request, session, authorization)
        task = service.progress(
            executor=executor,
            task_id=task_id,
            body=body,
            trace_id=request.state.trace_id,
        )
        return {"task": service.task_payload(task)}

    @app.post("/v1/executor-channel/tasks/{task_id}/finish")
    def finish_executor_task(
        task_id: uuid.UUID,
        body: ExecutorTaskFinishBody,
        request: Request,
        session: SessionDep,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        service, executor = authenticated_executor(request, session, authorization)
        task = service.finish(
            executor=executor,
            task_id=task_id,
            body=body,
            trace_id=request.state.trace_id,
        )
        return {"task": service.task_payload(task)}

    def purchase_error(
        request: Request,
        session: Session,
        actor: AdminActor,
        action: str,
        exc: PurchaseServiceError,
        *,
        business_object_id: str | uuid.UUID | None = None,
        business_object_no: str | None = None,
    ) -> None:
        # The failure log must survive, but a service failure must never cause
        # partial business mutations to be committed with it.
        session.rollback()
        if exc.status == 403:
            outcome = "permission_denied"
        elif exc.status == 404:
            outcome = "not_found"
        elif exc.status == 409:
            outcome = "business_conflict"
        elif exc.status == 422:
            outcome = "validation_failed"
        elif exc.status in (502, 503, 504):
            outcome = "external_service_failed"
        else:
            outcome = "failure"
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="denied" if exc.status in (403, 404, 409, 422) else "failure",
            outcome=outcome,
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_id=business_object_id,
            business_object_no=business_object_no,
            failure_reason=exc.code,
            details={"reason": exc.code},
            **_request_log_context(request),
        )
        session.commit()
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)})

    def checkout_success(
        request: Request,
        session: Session,
        actor: AdminActor,
        *,
        action: str,
        business_object_id: str | uuid.UUID,
        summary: dict[str, Any],
        result: dict[str, object],
    ) -> dict[str, object]:
        """只写入状态、版本和计数；资源引用、账号标签及物流号不进审计。"""

        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_id=business_object_id,
            change_summary=summary,
            details=summary,
            **_request_log_context(request),
        )
        session.commit()
        return {"ok": True, "data": result}

    def require_procurement_import_runtime() -> CloudProcurementImportService:
        if procurement_import_service is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "procurement_import_cloud_disabled",
                    "message": "云端采购协作导入尚未启用",
                },
            )
        return procurement_import_service

    def procurement_import_failure(
        request: Request,
        session: Session,
        actor: AdminActor,
        *,
        action: str,
        exc: CloudProcurementImportError,
        object_id: object | None = None,
    ) -> None:
        session.rollback()
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="denied" if exc.status in {403, 404, 409, 410, 422} else "failure",
            outcome=(
                "validation_failed"
                if exc.status == 422
                else "business_conflict"
                if exc.status in {409, 410}
                else "not_found"
                if exc.status == 404
                else "external_service_failed"
                if exc.status in {502, 503, 504}
                else "failure"
            ),
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="procurement_import",
            business_object_id=str(object_id or "")[:160] or None,
            failure_reason=exc.code,
            details={"reason": exc.code},
            **_request_log_context(request),
        )
        session.commit()
        raise HTTPException(
            status_code=exc.status,
            detail={"code": exc.code, "message": str(exc)},
        )

    @app.post(
        "/v1/assistant/procurement-import/parse",
        status_code=status.HTTP_201_CREATED,
    )
    def parse_procurement_import(
        request: Request,
        body: ProcurementImportParseBody,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "assistant.procurement_import.parse"
        actor = authorize_request(
            request,
            session,
            permission="assistant.access",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        runtime = require_procurement_import_runtime()
        try:
            result = runtime.parse(
                session,
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                filename=body.filename,
                content_base64=body.contentBase64,
            )
        except CloudProcurementImportError as exc:
            procurement_import_failure(
                request, session, actor, action=action, exc=exc
            )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="procurement_import_plan",
            business_object_id=result["planId"],
            change_summary={
                "sourceRows": result["sourceRows"],
                "orderCount": result["orderCount"],
                "detailCount": result["detailCount"],
                "imageCount": result["orderImageCount"],
            },
            details={
                "sourceRows": result["sourceRows"],
                "orderCount": result["orderCount"],
                "detailCount": result["detailCount"],
            },
            **_request_log_context(request),
        )
        session.commit()
        return result

    @app.get("/v1/assistant/procurement-import/image")
    def procurement_import_image(
        request: Request,
        session: SessionDep,
        plan_id: str = Query(alias="planId", min_length=1, max_length=64),
        row: int = Query(ge=0, le=100_000),
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        actor = authorize_request(
            request,
            session,
            permission="assistant.access",
            session_token=session_token,
            authorization=authorization,
            audit_action="assistant.procurement_import.image_read",
        )
        runtime = require_procurement_import_runtime()
        try:
            data, mime = runtime.preview_image(
                session,
                tenant_id=actor.tenant.id,
                plan_id=plan_id,
                row=row,
            )
        except CloudProcurementImportError as exc:
            procurement_import_failure(
                request,
                session,
                actor,
                action="assistant.procurement_import.image_read",
                exc=exc,
                object_id=plan_id,
            )
        session.commit()
        return Response(
            content=data,
            media_type=mime,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/v1/assistant/procurement-import/export")
    def procurement_import_export(
        request: Request,
        session: SessionDep,
        plan_id: str = Query(alias="planId", min_length=1, max_length=64),
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        action = "assistant.procurement_import.export"
        actor = authorize_request(
            request,
            session,
            permission="assistant.access",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        runtime = require_procurement_import_runtime()
        try:
            data, filename, mime = runtime.export(
                session, tenant_id=actor.tenant.id, plan_id=plan_id
            )
        except CloudProcurementImportError as exc:
            procurement_import_failure(
                request,
                session,
                actor,
                action=action,
                exc=exc,
                object_id=plan_id,
            )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="procurement_import_plan",
            business_object_id=plan_id,
            change_summary={"format": "xlsx"},
            details={"format": "xlsx"},
            **_request_log_context(request),
        )
        session.commit()
        return Response(
            content=data,
            media_type=mime,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/v1/assistant/procurement-import/target/inspect")
    def inspect_procurement_import_target(
        request: Request,
        body: ProcurementImportTargetInspectBody,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "assistant.procurement_import.target_inspect"
        actor = authorize_request(
            request,
            session,
            permission="assistant.access",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        runtime = require_procurement_import_runtime()
        try:
            result = runtime.inspect_target(
                session,
                tenant_id=actor.tenant.id,
                plan_id=body.planId,
                spreadsheet_url=body.spreadsheetUrl,
            )
        except CloudProcurementImportError as exc:
            procurement_import_failure(
                request,
                session,
                actor,
                action=action,
                exc=exc,
                object_id=body.planId,
            )
        session.commit()
        return result

    @app.post("/v1/assistant/procurement-import/target/validate")
    def validate_procurement_import_target(
        request: Request,
        body: ProcurementImportTargetValidateBody,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "assistant.procurement_import.target_validate"
        actor = authorize_request(
            request,
            session,
            permission="assistant.access",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        runtime = require_procurement_import_runtime()
        try:
            result = runtime.validate_target(
                session,
                tenant_id=actor.tenant.id,
                plan_id=body.planId,
                spreadsheet_url=body.spreadsheetUrl,
                sheet_id=body.sheetId,
            )
        except CloudProcurementImportError as exc:
            procurement_import_failure(
                request,
                session,
                actor,
                action=action,
                exc=exc,
                object_id=body.planId,
            )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="procurement_import_plan",
            business_object_id=body.planId,
            change_summary={
                "headerCount": result["headerCount"],
                "detailCount": result["detailCount"],
            },
            details={
                "headerCount": result["headerCount"],
                "detailCount": result["detailCount"],
            },
            **_request_log_context(request),
        )
        session.commit()
        return result

    @app.post(
        "/v1/assistant/procurement-import/sheet-sync",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_procurement_import_sync(
        request: Request,
        body: ProcurementImportSyncBody,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "assistant.procurement_import.sheet_sync_start"
        actor = authorize_request(
            request,
            session,
            permission="assistant.access",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        runtime = require_procurement_import_runtime()
        try:
            result = runtime.start_sync(
                session,
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                actor_name=actor.user.display_name,
                plan_id=body.planId,
                confirm_write=body.confirmWrite,
            )
        except CloudProcurementImportError as exc:
            procurement_import_failure(
                request,
                session,
                actor,
                action=action,
                exc=exc,
                object_id=body.planId,
            )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="procurement_import_job",
            business_object_id=result["jobId"],
            change_summary={"rowsTotal": result["rowsTotal"]},
            details={"rowsTotal": result["rowsTotal"]},
            **_request_log_context(request),
        )
        session.commit()
        return result

    @app.get("/v1/assistant/procurement-import/sheet-sync/status")
    def procurement_import_sync_status(
        request: Request,
        session: SessionDep,
        job_id: str = Query(alias="jobId", min_length=1, max_length=64),
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="assistant.access",
            session_token=session_token,
            authorization=authorization,
            audit_action="assistant.procurement_import.sheet_sync_status",
        )
        runtime = require_procurement_import_runtime()
        try:
            result = runtime.status(
                session, tenant_id=actor.tenant.id, job_id=job_id
            )
        except CloudProcurementImportError as exc:
            procurement_import_failure(
                request,
                session,
                actor,
                action="assistant.procurement_import.sheet_sync_status",
                exc=exc,
                object_id=job_id,
            )
        session.commit()
        return result

    @app.get("/v1/resources/buyer-accounts")
    def list_buyer_accounts(
        request: Request,
        session: SessionDep,
        site: str = Query(default="", max_length=20),
        account_status: str = Query(default="", alias="status", max_length=32),
        credential_status: str = Query(
            default="", alias="credentialStatus", max_length=32
        ),
        keyword: str = Query(default="", max_length=100),
        selectable_only: bool = Query(default=False, alias="selectableOnly"),
        include_credentials: bool = Query(default=False, alias="includeCredentials"),
        page: int = Query(default=1, ge=1, le=100_000),
        page_size: int = Query(default=100, alias="pageSize", ge=1, le=200),
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "resource.buyer_account.list"
        actor = authorize_request(
            request,
            session,
            permission="resource.buyer.read",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        if (
            include_credentials
            and "resource.buyer.credential.read"
            not in _permission_code_set(session, actor.user)
        ):
            _add_audit(
                session,
                request_id=request.state.request_id,
                action="resource.buyer_account.credential_list",
                result="denied",
                outcome="permission_denied",
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                failure_reason="permission_denied",
                details={"permission": "resource.buyer.credential.read"},
                **_request_log_context(request),
            )
            session.commit()
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})
        try:
            result = BuyerAccountService(
                session, credential_cipher=buyer_credential_cipher
            ).list_accounts(
                tenant_id=actor.tenant.id,
                site=site,
                status=account_status,
                credential_status=credential_status,
                keyword=keyword,
                selectable_only=selectable_only,
                page=page,
                page_size=page_size,
                include_credentials=include_credentials,
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc)
        if include_credentials:
            return checkout_success(
                request,
                session,
                actor,
                action="resource.buyer_account.credential_list",
                business_object_id=f"page:{page}",
                summary={
                    "page": page,
                    "returnedCount": len(result.get("rows") or []),
                    "total": result.get("total"),
                },
                result=result,
            )
        session.commit()
        return {"ok": True, "data": result}

    @app.put("/v1/resources/buyer-accounts/snapshot")
    def sync_buyer_account_snapshot(
        request: Request,
        body: BuyerAccountSnapshotBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "resource.buyer_account.snapshot_sync"
        actor = authorize_request(
            request,
            session,
            permission="resource.buyer.import",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        try:
            result = BuyerAccountService(
                session, credential_cipher=buyer_credential_cipher
            ).sync_snapshot(
                tenant_id=actor.tenant.id,
                source=body.source,
                snapshot_key=body.snapshotKey,
                accounts=safe_snapshot_items(body),
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc)
        return checkout_success(
            request,
            session,
            actor,
            action=action,
            business_object_id=body.snapshotKey,
            summary={
                "receivedCount": result["receivedCount"],
                "createdCount": result["createdCount"],
                "updatedCount": result["updatedCount"],
                "unchangedCount": result["unchangedCount"],
                "protectedCount": result["protectedCount"],
            },
            result=result,
        )

    @app.post("/v1/resources/buyer-accounts/preflight")
    def preflight_buyer_account_import(
        request: Request,
        body: BuyerAccountPreflightBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="resource.buyer.import",
            session_token=session_token,
            authorization=authorization,
            audit_action="resource.buyer_account.import_preflight",
        )
        result = BuyerAccountService(session).preflight_import(
            tenant_id=actor.tenant.id,
            items=[item.model_dump(mode="python") for item in body.items],
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.put("/v1/operations/environment-creation-runs")
    def ingest_environment_creation_run(
        request: Request,
        body: EnvironmentCreationRunBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "resource.environment.result_ingest"
        actor = authorize_request(
            request,
            session,
            permission="resource.environment.create",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        try:
            result = OperationResultService(session).ingest_environment_run(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                client_version=getattr(request.state, "client_version", None) or None,
                body=body,
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request,
                session,
                actor,
                action,
                exc,
                business_object_id=body.runKey,
            )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="environment_creation_run",
            business_object_id=result["runId"],
            change_summary={
                "totalCount": result["totalCount"],
                "successCount": result["successCount"],
                "failedCount": result["failedCount"],
                "syncStatus": result["syncStatus"],
                "unchanged": result["unchanged"],
            },
            details={
                "totalCount": result["totalCount"],
                "successCount": result["successCount"],
                "failedCount": result["failedCount"],
                "resourceConflictCount": result.get("resourceConflictCount", 0),
            },
            **_request_log_context(request),
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.post(
        "/v1/operation-runs/environment-creation",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_environment_operation_run(
        request: Request,
        body: EnvironmentCreationRunCreateBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "resource.environment.run.create"
        actor = authorize_request(
            request,
            session,
            permission="resource.environment.create",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        runs = OperationRunService(session)
        try:
            run, unchanged = runs.create_environment_run(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                body=body,
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request,
                session,
                actor,
                action,
                exc,
                business_object_id=body.idempotencyKey,
            )
        if not unchanged:
            plan_record = None
            plan_accounts = None
            cleanup_blocked_refs: list[str] = []
            channel = executor_channel(session)
            if body.mode == "bound":
                if environment_plan_service is None:
                    raise HTTPException(
                        status_code=503,
                        detail={
                            "code": "environment_plan_cloud_disabled",
                            "message": "云端买家号解析加密能力尚未启用",
                        },
                    )
                executor = channel.require_executor(
                    tenant_id=actor.tenant.id,
                    user_id=actor.user.id,
                    executor_id=body.executorId,
                )
                if "environment.cloud-plan.v1" not in set(executor.capabilities or []):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "executor_capability_missing",
                            "message": "本地执行器版本过旧，请先升级后再正式执行",
                        },
                    )
                try:
                    plan_record, plan_accounts = (
                        environment_plan_service.load_for_execution(
                            session,
                            tenant_id=actor.tenant.id,
                            actor_user_id=actor.user.id,
                            cloud_plan_id=body.cloudPlanId,
                            site=body.site,
                            environment_group=body.environmentGroup,
                            total_count=body.totalCount,
                        )
                    )
                except CloudEnvironmentPlanError as exc:
                    session.rollback()
                    raise HTTPException(
                        status_code=exc.status,
                        detail={"code": exc.code, "message": str(exc)},
                    ) from exc
                account_refs = {
                    hashlib.sha256(
                        str(item.get("email") or "")
                        .strip()
                        .casefold()
                        .encode("utf-8")
                    ).hexdigest()
                    for item in (plan_accounts or [])
                    if str(item.get("email") or "").strip()
                }
                try:
                    cleanup_blocked_refs = sorted(
                        runs.acquire_environment_account_guards(
                            run=run,
                            account_refs=account_refs,
                        )
                    )
                except PurchaseServiceError as exc:
                    session.rollback()
                    purchase_error(
                        request,
                        session,
                        actor,
                        action,
                        exc,
                        business_object_id=body.idempotencyKey,
                    )
            task_type = (
                "environment.create-bound.v1"
                if body.mode == "bound"
                else "environment.create-backup.v1"
            )
            task_payload = {
                "runId": str(run.id),
                "runKey": run.source_run_key,
                "mode": body.mode,
                "site": body.site,
                "purchaseDate": body.purchaseDate,
                "environmentGroup": body.environmentGroup,
                "cloudPlanId": body.cloudPlanId,
                "buyerLabel": body.buyerLabel,
                "totalCount": body.totalCount,
                "verifySampleCount": body.verifySampleCount,
                "assignments": [
                    item.model_dump(mode="json") for item in body.assignments
                ],
            }
            if plan_accounts is not None:
                task_payload["planAccounts"] = plan_accounts
                task_payload["cleanupBlockedAccountRefs"] = cleanup_blocked_refs
            task = channel.create_config_task(
                tenant_id=actor.tenant.id,
                user_id=actor.user.id,
                executor_id=body.executorId,
                task_type=task_type,
                payload=task_payload,
                idempotency_key=f"operation:{body.idempotencyKey}",
                commit=False,
            )
            if plan_record is not None:
                environment_plan_service.mark_submitted(plan_record)
            run.executor_task_id = task.id
            run.status = "queued"
            run.phase = "queued"
            run.updated_at = utcnow()
        result = runs.environment_snapshot(run, unchanged=unchanged)
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="environment_creation_run",
            business_object_id=str(run.id),
            change_summary={
                "status": run.status,
                "totalCount": run.total_count,
                "unchanged": unchanged,
            },
            details={"mode": run.run_mode, "site": run.site},
            **_request_log_context(request),
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.get("/v1/operation-runs/environment-creation/latest")
    def latest_environment_operation_run(
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="resource.environment.create",
            session_token=session_token,
            authorization=authorization,
            audit_action="resource.environment.run.latest",
        )
        service = OperationRunService(session)
        run = service.latest_environment_run(
            tenant_id=actor.tenant.id, actor_user_id=actor.user.id
        )
        return {"ok": True, "data": service.environment_snapshot(run) if run else None}

    @app.get("/v1/operation-runs/environment-creation/{run_id}")
    def get_environment_operation_run(
        run_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "resource.environment.run.read"
        actor = authorize_request(
            request,
            session,
            permission="resource.environment.create",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        service = OperationRunService(session)
        try:
            run = service.get_environment_run(
                tenant_id=actor.tenant.id, run_id=run_id
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request, session, actor, action, exc, business_object_id=str(run_id)
            )
        return {"ok": True, "data": service.environment_snapshot(run)}

    @app.post("/v1/operation-runs/environment-creation/{run_id}/cancel")
    def cancel_environment_operation_run(
        run_id: uuid.UUID,
        body: ExecutorTaskCancelBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="resource.environment.create",
            session_token=session_token,
            authorization=authorization,
            audit_action="resource.environment.run.cancel",
        )
        runs = OperationRunService(session)
        run = runs.get_environment_run(tenant_id=actor.tenant.id, run_id=run_id)
        if run.executor_task_id is None:
            raise HTTPException(
                status_code=409, detail={"code": "operation_run_not_cancellable"}
            )
        task = executor_channel(session).get_task(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            task_id=run.executor_task_id,
        )
        if body.expectedStatus and task.status != body.expectedStatus:
            raise ExecutorServiceError("executor_task_state_conflict", status_code=409)
        executor_channel(session).cancel_task(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            task_id=task.id,
        )
        session.refresh(run)
        return {"ok": True, "data": runs.environment_snapshot(run)}

    @app.post(
        "/v1/operation-runs/environment-creation/{run_id}/retry",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def retry_environment_operation_run(
        run_id: uuid.UUID,
        body: EnvironmentRetryRunCreateBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="resource.environment.create",
            session_token=session_token,
            authorization=authorization,
            audit_action="resource.environment.run.retry",
        )
        runs = OperationRunService(session)
        parent = runs.get_environment_run(
            tenant_id=actor.tenant.id, run_id=run_id
        )
        try:
            run, unchanged = runs.create_environment_retry_run(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                parent=parent,
                body=body,
            )
            if not unchanged:
                runs.acquire_environment_account_guards(
                    run=run,
                    account_refs=set(body.accountRefs),
                    allow_cleanup_failed=False,
                )
        except PurchaseServiceError as exc:
            session.rollback()
            purchase_error(
                request,
                session,
                actor,
                "resource.environment.run.retry",
                exc,
                business_object_id=str(run_id),
            )
        if not unchanged:
            task_type = (
                "environment.retry-row.v1"
                if body.retryMode == "single"
                else "environment.retry-failed.v1"
            )
            task = executor_channel(session).create_config_task(
                tenant_id=actor.tenant.id,
                user_id=actor.user.id,
                executor_id=run.executor_id,
                task_type=task_type,
                payload={
                    "runId": str(run.id),
                    "runKey": run.source_run_key,
                    "parentRunId": str(parent.id),
                    "retryMode": body.retryMode,
                    "accountRefs": list(body.accountRefs),
                    "totalCount": len(body.accountRefs),
                    "site": run.site,
                    "purchaseDate": run.purchase_date,
                    "environmentGroup": run.environment_group,
                },
                idempotency_key=f"operation:{body.idempotencyKey}",
                commit=False,
            )
            run.executor_task_id = task.id
            run.status = "queued"
            run.phase = "queued"
            run.updated_at = utcnow()
        result = runs.environment_snapshot(run, unchanged=unchanged)
        _add_audit(
            session,
            request_id=request.state.request_id,
            action="resource.environment.run.retry",
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="environment_creation_run",
            business_object_id=str(run.id),
            change_summary={
                "retryMode": body.retryMode,
                "totalCount": len(body.accountRefs),
                "unchanged": unchanged,
            },
            details={"parentRunId": str(parent.id)},
            **_request_log_context(request),
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.put("/v1/operations/logistics-query-runs")
    def ingest_logistics_query_run(
        request: Request,
        body: LogisticsQueryRunBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "fulfillment.logistics.result_ingest"
        actor = authorize_request(
            request,
            session,
            permission="fulfillment.order.read",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        try:
            result = OperationResultService(session).ingest_logistics_run(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                client_version=getattr(request.state, "client_version", None) or None,
                body=body,
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request,
                session,
                actor,
                action,
                exc,
                business_object_id=body.runKey,
            )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="logistics_query_run",
            business_object_id=result["runId"],
            change_summary={
                "totalCount": result["totalCount"],
                "successCount": result["successCount"],
                "failedCount": result["failedCount"],
                "syncStatus": result["syncStatus"],
                "unchanged": result["unchanged"],
            },
            details={
                "totalCount": result["totalCount"],
                "successCount": result["successCount"],
                "failedCount": result["failedCount"],
            },
            **_request_log_context(request),
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.post(
        "/v1/operation-runs/logistics-query",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_logistics_operation_run(
        request: Request,
        body: LogisticsQueryRunCreateBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "fulfillment.logistics.run.create"
        actor = authorize_request(
            request,
            session,
            permission="fulfillment.order.read",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        runs = OperationRunService(session)
        try:
            run, unchanged = runs.create_logistics_run(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                body=body,
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request,
                session,
                actor,
                action,
                exc,
                business_object_id=body.idempotencyKey,
            )
        if not unchanged:
            task = executor_channel(session).create_config_task(
                tenant_id=actor.tenant.id,
                user_id=actor.user.id,
                executor_id=body.executorId,
                task_type="logistics.query.v1",
                payload={
                    "runId": str(run.id),
                    "runKey": run.source_run_key,
                    "queryMode": body.queryMode,
                    "force": body.force,
                    "site": body.site,
                    "environmentSerials": list(body.environmentSerials),
                },
                idempotency_key=f"operation:{body.idempotencyKey}",
                commit=False,
            )
            run.executor_task_id = task.id
            run.status = "queued"
            run.phase = "queued"
            run.updated_at = utcnow()
        result = runs.logistics_snapshot(run, unchanged=unchanged)
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="logistics_query_run",
            business_object_id=str(run.id),
            change_summary={
                "status": run.status,
                "totalCount": run.total_count,
                "unchanged": unchanged,
            },
            details={"queryMode": run.query_mode, "site": run.site},
            **_request_log_context(request),
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.get("/v1/operation-runs/logistics-query/latest")
    def latest_logistics_operation_run(
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="fulfillment.order.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="fulfillment.logistics.run.latest",
        )
        service = OperationRunService(session)
        run = service.latest_logistics_run(
            tenant_id=actor.tenant.id, actor_user_id=actor.user.id
        )
        return {"ok": True, "data": service.logistics_snapshot(run) if run else None}

    @app.get("/v1/operation-runs/logistics-query/history")
    def list_logistics_operation_history(
        request: Request,
        session: SessionDep,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[uuid.UUID | None, Query()] = None,
        site: Annotated[Literal["US", "MX"] | None, Query()] = None,
        run_status: Annotated[
            Literal[
                "created",
                "queued",
                "leased",
                "running",
                "completed",
                "partial_failure",
                "failed",
                "cancelled",
                "uncertain",
            ]
            | None,
            Query(alias="status"),
        ] = None,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "fulfillment.logistics.history.list"
        actor = authorize_request(
            request,
            session,
            permission="fulfillment.order.read",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        service = OperationRunService(session)
        try:
            data = service.logistics_history(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                limit=limit,
                cursor=cursor,
                site=site,
                status=run_status,
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc)
        return {"ok": True, "data": data}

    @app.get("/v1/operation-runs/logistics-query/history/{root_run_id}")
    def get_logistics_operation_history(
        root_run_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "fulfillment.logistics.history.read"
        actor = authorize_request(
            request,
            session,
            permission="fulfillment.order.read",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        service = OperationRunService(session)
        try:
            data = service.logistics_history_snapshot(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                root_run_id=root_run_id,
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request,
                session,
                actor,
                action,
                exc,
                business_object_id=str(root_run_id),
            )
        return {"ok": True, "data": data}

    @app.get("/v1/operation-runs/logistics-query/{run_id}")
    def get_logistics_operation_run(
        run_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "fulfillment.logistics.run.read"
        actor = authorize_request(
            request,
            session,
            permission="fulfillment.order.read",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        service = OperationRunService(session)
        try:
            run = service.get_logistics_run(tenant_id=actor.tenant.id, run_id=run_id)
        except PurchaseServiceError as exc:
            purchase_error(
                request, session, actor, action, exc, business_object_id=str(run_id)
            )
        return {"ok": True, "data": service.logistics_snapshot(run)}

    @app.get(
        "/v1/operation-runs/logistics-query/{run_id}/screenshots/{environment_serial}"
    )
    def get_logistics_operation_screenshot(
        run_id: uuid.UUID,
        environment_serial: str,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        actor = authorize_request(
            request,
            session,
            permission="fulfillment.order.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="fulfillment.logistics.screenshot.read",
        )
        serial = str(environment_serial or "").strip()
        if not serial or len(serial) > 64:
            raise HTTPException(status_code=422, detail={"code": "environment_serial_invalid"})
        service = OperationRunService(session)
        try:
            _root_run, run = service.resolve_latest_logistics_history_run(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                root_run_id=run_id,
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request, session, actor,
                "fulfillment.logistics.screenshot.read", exc,
                business_object_id=str(run_id),
            )
        row = service.logistics_screenshot_row(
            run=run, environment_serial=serial
        )
        expires_at = row.screenshot_expires_at if row is not None else None
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if row is None or not row.screenshot_content \
                or (expires_at is not None and expires_at <= utcnow()):
            raise HTTPException(
                status_code=404, detail={"code": "logistics_screenshot_expired"}
            )
        content = bytes(row.screenshot_content)
        content_type = row.screenshot_content_type or "image/jpeg"
        session.commit()
        return Response(
            content=content,
            media_type=content_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/v1/operation-runs/logistics-query/{run_id}/export")
    def export_logistics_operation_run(
        run_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        include_screenshots: Annotated[
            bool, Query(alias="includeScreenshots")
        ] = True,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        actor = authorize_request(
            request,
            session,
            permission="fulfillment.order.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="fulfillment.logistics.export",
        )
        service = OperationRunService(session)
        try:
            root_run, run = service.resolve_latest_logistics_history_run(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                root_run_id=run_id,
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request, session, actor, "fulfillment.logistics.export", exc,
                business_object_id=str(run_id),
            )
        if run.status not in service.TERMINAL_STATUSES:
            raise HTTPException(
                status_code=409, detail={"code": "operation_run_active"}
            )
        snapshot = service.logistics_snapshot(run)

        def screenshot_reader(serial: str) -> bytes | None:
            row = service.logistics_screenshot_row(
                run=run, environment_serial=serial
            )
            if row is None or not row.screenshot_content:
                return None
            expires_at = row.screenshot_expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at is not None and expires_at <= utcnow():
                return None
            return bytes(row.screenshot_content)

        exported = build_logistics_workbook_export(
            snapshot["rows"],
            screenshot_reader if include_screenshots else None,
            include_screenshots=include_screenshots,
        )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action="fulfillment.logistics.export",
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_type="logistics_query_run",
            business_object_id=str(root_run.id),
            change_summary={
                "includeScreenshots": include_screenshots,
                "includedScreenshotCount": exported.included_screenshot_count,
                "missingScreenshotCount": exported.missing_screenshot_count,
            },
            details={"latestRunId": str(run.id)},
            **_request_log_context(request),
        )
        session.commit()
        filename = quote(
            "物流单号查询结果_%s.xlsx" % (
                "含截图" if include_screenshots else "无截图"
            )
        )
        return Response(
            content=exported.content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                "X-Content-Type-Options": "nosniff",
                "X-Xynigo-Screenshot-Included": str(
                    exported.included_screenshot_count
                ),
                "X-Xynigo-Screenshot-Missing": str(
                    exported.missing_screenshot_count
                ),
            },
        )

    @app.post("/v1/operation-runs/logistics-query/{run_id}/cancel")
    def cancel_logistics_operation_run(
        run_id: uuid.UUID,
        body: ExecutorTaskCancelBody,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="fulfillment.order.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="fulfillment.logistics.run.cancel",
        )
        runs = OperationRunService(session)
        run = runs.get_logistics_run(tenant_id=actor.tenant.id, run_id=run_id)
        if run.executor_task_id is None:
            raise HTTPException(
                status_code=409, detail={"code": "operation_run_not_cancellable"}
            )
        task = executor_channel(session).get_task(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            task_id=run.executor_task_id,
        )
        if body.expectedStatus and task.status != body.expectedStatus:
            raise ExecutorServiceError("executor_task_state_conflict", status_code=409)
        executor_channel(session).cancel_task(
            tenant_id=actor.tenant.id,
            user_id=actor.user.id,
            task_id=task.id,
        )
        session.refresh(run)
        return {"ok": True, "data": runs.logistics_snapshot(run)}

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
            audit_action=action,
        )
        try:
            result = PurchaseOrderService(session).save_draft(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                draft=body,
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request,
                session,
                actor,
                action,
                exc,
                business_object_no=body.platformOrderNo,
            )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_id=result["purchaseOrderId"],
            business_object_no=body.platformOrderNo,
            change_summary={
                "draftRevision": {"after": result["draftRevision"]},
                "unchanged": result["unchanged"],
            },
            details={
                "purchaseOrderId": result["purchaseOrderId"],
                "draftRevision": result["draftRevision"],
                "unchanged": result["unchanged"],
            },
            **_request_log_context(request),
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
            audit_action=action,
        )
        try:
            result = PurchaseOrderService(session).submit(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                draft=body,
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request,
                session,
                actor,
                action,
                exc,
                business_object_no=body.platformOrderNo,
            )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_id=result["purchaseOrderId"],
            business_object_no=body.platformOrderNo,
            change_summary={
                "submissionStatus": {"after": "submitted"},
                "draftRevision": {"after": result["draftRevision"]},
                "unchanged": result["unchanged"],
                "revised": result.get("revised", False),
            },
            details={
                "purchaseOrderId": result["purchaseOrderId"],
                "draftRevision": result["draftRevision"],
                "unchanged": result["unchanged"],
                "revised": result.get("revised", False),
            },
            **_request_log_context(request),
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
            audit_action=action,
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
            business_object_id=result["purchaseOrderId"],
            details={
                "purchaseOrderId": result["purchaseOrderId"],
                "draftRevision": result["draftRevision"],
            },
            **_request_log_context(request),
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
            audit_action="purchase_order.workspace.overview.read",
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
        claimed_by_me: Annotated[bool, Query(alias="claimedByMe")] = False,
        task_scope: Annotated[
            Literal["unclaimed", "processing", "ordered", "abnormal"] | None,
            Query(alias="taskScope"),
        ] = None,
        site: Annotated[str | None, Query(max_length=20)] = None,
        store: Annotated[str | None, Query(max_length=300)] = None,
        operator: Annotated[str | None, Query(max_length=100)] = None,
        keyword: Annotated[str | None, Query(max_length=200)] = None,
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
            audit_action="purchase_order.workspace.list",
        )
        result = PurchaseOrderService(session).workspace_list(
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            claimed_by_me=claimed_by_me,
            task_scope=task_scope,
            site=site,
            store=store,
            operator=operator,
            keyword=keyword,
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
            audit_action=action,
        )
        try:
            result = PurchaseOrderService(session).workspace_detail(
                tenant_id=actor.tenant.id,
                purchase_order_id=purchase_order_id,
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request,
                session,
                actor,
                action,
                exc,
                business_object_id=purchase_order_id,
            )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_id=result["purchaseOrderId"],
            business_object_no=str(result.get("platformOrderNo") or "") or None,
            change_summary={"sensitiveFields": ["recipientPhone", "address"]},
            details={"purchaseOrderId": result["purchaseOrderId"]},
            **_request_log_context(request),
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
            audit_action=action,
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
            business_object_id=(
                result["purchaseOrderIds"][0]
                if len(result["purchaseOrderIds"]) == 1
                else None
            ),
            change_summary={
                "workflowStatus": {"after": "claimed"},
                "orderCount": len(result["purchaseOrderIds"]),
                "lineCount": result["lineCount"],
                "claimedCount": result["claimedCount"],
            },
            details={
                "orderCount": len(result["purchaseOrderIds"]),
                "lineCount": result["lineCount"],
                "claimedCount": result["claimedCount"],
            },
            **_request_log_context(request),
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.post("/v1/procurement/orders/{purchase_order_id}/return")
    def return_procurement_order_to_task(
        request: Request,
        purchase_order_id: uuid.UUID,
        body: ProcurementReturnBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "purchase_order.lines.return"
        actor = authorize_request(
            request,
            session,
            permission="procurement.execution.manage",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        try:
            result = PurchaseOrderService(session).return_to_task(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                purchase_order_id=purchase_order_id,
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request,
                session,
                actor,
                action,
                exc,
                business_object_id=purchase_order_id,
            )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_id=result["purchaseOrderId"],
            change_summary={
                "workflowStatus": {"before": "claimed", "after": "unclaimed"},
                "lineCount": result["returnedCount"],
                "executionRevision": {"after": result["executionRevision"]},
            },
            details={
                "purchaseOrderId": result["purchaseOrderId"],
                "lineCount": result["returnedCount"],
                "reason": body.reason,
            },
            **_request_log_context(request),
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
            audit_action=action,
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
            purchase_error(
                request,
                session,
                actor,
                action,
                exc,
                business_object_id=purchase_order_id,
            )
        _add_audit(
            session,
            request_id=request.state.request_id,
            action=action,
            result="success",
            tenant_id=actor.tenant.id,
            actor_user_id=actor.user.id,
            business_object_id=result["purchaseOrderId"],
            change_summary={
                "executionRevision": {"after": result["executionRevision"]},
                "splitCount": result["splitCount"],
                "lineCount": result["lineCount"],
            },
            details={
                "purchaseOrderId": result["purchaseOrderId"],
                "executionRevision": result["executionRevision"],
                "splitCount": result["splitCount"],
                "lineCount": result["lineCount"],
            },
            **_request_log_context(request),
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.post("/v1/procurement/orders/{purchase_order_id}/checkout-attempts")
    def create_checkout_attempt(
        request: Request,
        purchase_order_id: uuid.UUID,
        body: CheckoutAttemptCreateBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "procurement.checkout_attempt.create"
        actor = authorize_request(
            request,
            session,
            permission="procurement.execution.manage",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        try:
            result = ProcurementCheckoutService(session).create_attempt(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                purchase_order_id=purchase_order_id,
                idempotency_key=body.idempotencyKey,
                expected_execution_revision=body.expectedExecutionRevision,
                plan=plan_payload(body),
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request,
                session,
                actor,
                action,
                exc,
                business_object_id=purchase_order_id,
            )
        return checkout_success(
            request,
            session,
            actor,
            action=action,
            business_object_id=result["checkoutAttemptId"],
            summary={
                "status": result["status"],
                "resourceStatus": result["resourceStatus"],
                "version": result["version"],
                "executionRevision": result["executionRevision"],
                "lineCount": len(result["lines"]),  # type: ignore[arg-type]
                "unchanged": result["unchanged"],
            },
            result=result,
        )

    @app.post("/v1/procurement/checkout-attempts/{attempt_id}/revise")
    def revise_checkout_attempt(
        request: Request,
        attempt_id: uuid.UUID,
        body: CheckoutAttemptReviseBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "procurement.checkout_attempt.revise"
        actor = authorize_request(
            request,
            session,
            permission="procurement.execution.manage",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        try:
            result = ProcurementCheckoutService(session).revise_attempt(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                attempt_id=attempt_id,
                expected_version=body.expectedVersion,
                expected_execution_revision=body.expectedExecutionRevision,
                plan=plan_payload(body),
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc, business_object_id=attempt_id)
        return checkout_success(
            request,
            session,
            actor,
            action=action,
            business_object_id=attempt_id,
            summary={
                "status": result["status"],
                "resourceStatus": result["resourceStatus"],
                "version": result["version"],
                "executionRevision": result["executionRevision"],
                "lineCount": len(result["lines"]),  # type: ignore[arg-type]
                "unchanged": result["unchanged"],
            },
            result=result,
        )

    @app.post("/v1/procurement/checkout-attempts/{attempt_id}/begin")
    def begin_checkout_attempt(
        request: Request,
        attempt_id: uuid.UUID,
        body: CheckoutAttemptBeginBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "procurement.checkout_attempt.begin"
        actor = authorize_request(
            request,
            session,
            permission="procurement.execution.manage",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        try:
            result = ProcurementCheckoutService(session).begin_attempt(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                attempt_id=attempt_id,
                expected_version=body.expectedVersion,
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc, business_object_id=attempt_id)
        return checkout_success(
            request,
            session,
            actor,
            action=action,
            business_object_id=attempt_id,
            summary={
                "status": result["status"],
                "resourceStatus": result["resourceStatus"],
                "version": result["version"],
                "executionRevision": result["executionRevision"],
                "unchanged": result["unchanged"],
            },
            result=result,
        )

    @app.post("/v1/procurement/checkout-attempts/{attempt_id}/abandon")
    def abandon_checkout_attempt(
        request: Request,
        attempt_id: uuid.UUID,
        body: CheckoutAttemptAbandonBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "procurement.checkout_attempt.abandon"
        actor = authorize_request(
            request,
            session,
            permission="procurement.execution.manage",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        try:
            result = ProcurementCheckoutService(session).abandon_attempt(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                attempt_id=attempt_id,
                expected_version=body.expectedVersion,
                reason=body.reason,
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc, business_object_id=attempt_id)
        return checkout_success(
            request,
            session,
            actor,
            action=action,
            business_object_id=attempt_id,
            summary={
                "status": result["status"],
                "resourceStatus": result["resourceStatus"],
                "pendingTerminalStatus": result["pendingTerminalStatus"],
                "version": result["version"],
                "executionRevision": result["executionRevision"],
                "unchanged": result["unchanged"],
            },
            result=result,
        )

    @app.post("/v1/procurement/checkout-attempts/{attempt_id}/payment-result")
    def record_checkout_payment_result(
        request: Request,
        attempt_id: uuid.UUID,
        body: CheckoutPaymentResultBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "procurement.checkout_attempt.payment_result"
        actor = authorize_request(
            request,
            session,
            permission="procurement.execution.manage",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        try:
            result = ProcurementCheckoutService(session).record_payment_result(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                attempt_id=attempt_id,
                expected_version=body.expectedVersion,
                result=body.model_dump(mode="python"),
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc, business_object_id=attempt_id)
        return checkout_success(
            request,
            session,
            actor,
            action=action,
            business_object_id=attempt_id,
            summary={
                "outcome": body.outcome,
                "status": result["status"],
                "resourceStatus": result["resourceStatus"],
                "version": result["version"],
                "executionRevision": result["executionRevision"],
                "purchaseBatchCreated": "purchaseBatch" in result,
                "unchanged": result["unchanged"],
            },
            result=result,
        )

    @app.post("/v1/procurement/checkout-attempts/{attempt_id}/cleanup-result")
    def record_checkout_cleanup_result(
        request: Request,
        attempt_id: uuid.UUID,
        body: CheckoutCleanupResultBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "procurement.checkout_attempt.cleanup_result"
        actor = authorize_request(
            request,
            session,
            permission="procurement.execution.manage",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        try:
            result = ProcurementCheckoutService(session).record_cleanup_result(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                attempt_id=attempt_id,
                expected_version=body.expectedVersion,
                environment_result=body.environmentResult,
                buyer_result=body.buyerResult,
                reason=body.reason,
            )
        except PurchaseServiceError as exc:
            purchase_error(request, session, actor, action, exc, business_object_id=attempt_id)
        return checkout_success(
            request,
            session,
            actor,
            action=action,
            business_object_id=attempt_id,
            summary={
                "environmentResult": body.environmentResult,
                "buyerResult": body.buyerResult,
                "status": result["status"],
                "resourceStatus": result["resourceStatus"],
                "pendingTerminalStatus": result["pendingTerminalStatus"],
                "version": result["version"],
                "executionRevision": result["executionRevision"],
            },
            result=result,
        )

    @app.post("/v1/procurement/purchase-batches/{purchase_batch_id}/shipments")
    def upsert_purchase_batch_shipment(
        request: Request,
        purchase_batch_id: uuid.UUID,
        body: ShipmentUpsertBody,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        action = "procurement.purchase_batch.shipment_upsert"
        actor = authorize_request(
            request,
            session,
            permission="procurement.execution.manage",
            session_token=session_token,
            authorization=authorization,
            audit_action=action,
        )
        try:
            result = ProcurementCheckoutService(session).upsert_shipment(
                tenant_id=actor.tenant.id,
                actor_user_id=actor.user.id,
                purchase_batch_id=purchase_batch_id,
                shipment=body.model_dump(mode="python"),
            )
        except PurchaseServiceError as exc:
            purchase_error(
                request,
                session,
                actor,
                action,
                exc,
                business_object_id=purchase_batch_id,
            )
        shipment = result["shipment"]
        return checkout_success(
            request,
            session,
            actor,
            action=action,
            business_object_id=purchase_batch_id,
            summary={
                "batchStatus": result.get("batchStatus"),
                "shipmentStatus": shipment["status"],  # type: ignore[index]
                "shipmentVersion": shipment["version"],  # type: ignore[index]
                "unchanged": result["unchanged"],
            },
            result=result,
        )

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
            audit_action="purchase_order.execution.list",
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

    def business_log_actor(
        request: Request,
        session: Session,
        *,
        session_token: str | None,
        authorization: str | None,
    ) -> tuple[AdminActor, bool]:
        raw_token = _request_session_token(session_token, authorization)
        session_record, user, tenant = _authenticated_identity(session, raw_token)
        bind_request_identity(request, user=user, tenant=tenant)
        _ensure_system_catalog(session, tenant=tenant)
        tenant_wide = "system.audit.read" in _permission_code_set(session, user)
        session_record.last_seen_at = utcnow()
        return AdminActor(session_record=session_record, user=user, tenant=tenant), tenant_wide

    @app.get("/v1/business-logs")
    def list_business_logs(
        request: Request,
        session: SessionDep,
        started_at: Annotated[datetime | None, Query(alias="startTime")] = None,
        ended_at: Annotated[datetime | None, Query(alias="endTime")] = None,
        module: Annotated[str | None, Query(max_length=64)] = None,
        operator: Annotated[str | None, Query(max_length=255)] = None,
        log_result: Annotated[
            Literal[
                "success",
                "validation_failed",
                "permission_denied",
                "business_conflict",
                "not_found",
                "external_service_failed",
                "failure",
            ]
            | None,
            Query(alias="result"),
        ] = None,
        business_no: Annotated[str | None, Query(alias="businessNo", max_length=255)] = None,
        operation_type: Annotated[
            str | None, Query(alias="operationType", max_length=160)
        ] = None,
        request_id: Annotated[str | None, Query(alias="requestId", max_length=64)] = None,
        page: Annotated[int, Query(ge=1, le=100_000)] = 1,
        page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        normalized_started_at = as_utc(started_at) if started_at is not None else None
        normalized_ended_at = as_utc(ended_at) if ended_at is not None else None
        if (
            normalized_started_at is not None
            and normalized_ended_at is not None
            and normalized_started_at > normalized_ended_at
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "business_log_time_range_invalid"},
            )
        actor, tenant_wide = business_log_actor(
            request,
            session,
            session_token=session_token,
            authorization=authorization,
        )
        result = BusinessLogService(session).list_events(
            tenant_id=actor.tenant.id,
            viewer_user_id=actor.user.id,
            tenant_wide=tenant_wide,
            started_at=normalized_started_at,
            ended_at=normalized_ended_at,
            module=module,
            operator=operator,
            outcome=log_result,
            business_no=business_no,
            operation_type=operation_type,
            request_id=request_id,
            page=page,
            page_size=page_size,
        )
        session.commit()
        return {"ok": True, "data": result}

    @app.get("/v1/business-logs/{business_log_id}")
    def get_business_log_detail(
        business_log_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor, tenant_wide = business_log_actor(
            request,
            session,
            session_token=session_token,
            authorization=authorization,
        )
        result = BusinessLogService(session).get_event(
            tenant_id=actor.tenant.id,
            viewer_user_id=actor.user.id,
            event_id=business_log_id,
            tenant_wide=tenant_wide,
        )
        if result is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "business_log_not_found"},
            )
        session.commit()
        return {"ok": True, "data": result}

    @app.get("/v1/system-logs")
    def list_system_logs(
        request: Request,
        session: SessionDep,
        started_at: Annotated[datetime | None, Query(alias="startTime")] = None,
        ended_at: Annotated[datetime | None, Query(alias="endTime")] = None,
        category: Annotated[
            Literal["system_runtime", "system_error"] | None, Query()
        ] = None,
        level: Annotated[
            Literal["info", "warning", "error", "critical"] | None, Query()
        ] = None,
        service: Annotated[str | None, Query(max_length=64)] = None,
        component: Annotated[str | None, Query(max_length=64)] = None,
        event_type: Annotated[
            str | None, Query(alias="eventType", max_length=160)
        ] = None,
        status_code: Annotated[
            int | None, Query(alias="statusCode", ge=100, le=599)
        ] = None,
        request_id: Annotated[
            str | None, Query(alias="requestId", max_length=64)
        ] = None,
        keyword: Annotated[str | None, Query(max_length=255)] = None,
        page: Annotated[int, Query(ge=1, le=100_000)] = 1,
        page_size: Annotated[int, Query(alias="pageSize", ge=1, le=200)] = 50,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        normalized_started_at = as_utc(started_at) if started_at is not None else None
        normalized_ended_at = as_utc(ended_at) if ended_at is not None else None
        if (
            normalized_started_at is not None
            and normalized_ended_at is not None
            and normalized_started_at > normalized_ended_at
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "system_log_time_range_invalid"},
            )
        actor = authorize_request(
            request,
            session,
            permission="system.runtime_log.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="system_log.read",
        )
        result = SystemLogService(session).list_events(
            tenant_id=actor.tenant.id,
            started_at=normalized_started_at,
            ended_at=normalized_ended_at,
            category=category,
            level=level,
            service=service,
            component=component,
            event_type=event_type,
            status_code=status_code,
            request_id=request_id,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
        result["retentionDays"] = settings.system_log_retention_days
        result["maxRowsPerTenant"] = settings.system_log_max_rows_per_tenant
        session.commit()
        return {"ok": True, "data": result}

    @app.get("/v1/system-logs/{system_log_id}")
    def get_system_log_detail(
        system_log_id: uuid.UUID,
        request: Request,
        session: SessionDep,
        session_token: Annotated[
            str | None, Cookie(alias=settings.cookie_name)
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        actor = authorize_request(
            request,
            session,
            permission="system.runtime_log.read",
            session_token=session_token,
            authorization=authorization,
            audit_action="system_log.detail.read",
        )
        result = SystemLogService(session).get_event(
            tenant_id=actor.tenant.id,
            event_id=system_log_id,
        )
        if result is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "system_log_not_found"},
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


def _slide_session_expiry(
    record: SessionRecord,
    *,
    settings: Settings,
    now: datetime,
) -> int | None:
    """Extend an active browser session inside a fixed absolute lifetime.

    The caller must also re-issue the HttpOnly cookie.  Returning ``None``
    avoids a database write and Set-Cookie churn until the idle expiry enters
    the configured refresh window.
    """

    current_expiry = as_utc(record.expires_at)
    if (
        current_expiry - now
        > timedelta(seconds=settings.session_refresh_threshold_seconds)
    ):
        return None
    absolute_expiry = as_utc(record.created_at) + timedelta(
        seconds=settings.session_absolute_ttl_seconds
    )
    renewed_expiry = min(
        now + timedelta(seconds=settings.session_ttl_seconds),
        absolute_expiry,
    )
    if renewed_expiry <= current_expiry:
        return None
    record.expires_at = renewed_expiry
    return max(1, int((renewed_expiry - now).total_seconds()))


def _validation_log_target(method: str, path: str) -> tuple[str | None, str | None]:
    method = method.upper()
    if path.startswith("/v1/assistant/procurement-import/"):
        suffix = path.removeprefix("/v1/assistant/procurement-import/")
        action = {
            ("POST", "parse"): "assistant.procurement_import.parse",
            ("POST", "target/inspect"): "assistant.procurement_import.target_inspect",
            ("POST", "target/validate"): "assistant.procurement_import.target_validate",
            ("POST", "sheet-sync"): "assistant.procurement_import.sheet_sync_start",
        }.get((method, suffix))
        if action:
            return action, None
    if method == "PUT" and path == "/v1/operations/environment-creation-runs":
        return "resource.environment.result_ingest", None
    if method == "PUT" and path == "/v1/operations/logistics-query-runs":
        return "fulfillment.logistics.result_ingest", None
    if method == "POST" and path == "/v1/operation-runs/environment-creation":
        return "resource.environment.run.create", None
    if method == "POST" and path == "/v1/operation-runs/logistics-query":
        return "fulfillment.logistics.run.create", None
    if method == "POST" and path == "/v1/environment-plans/parse":
        return "resource.environment.plan.parse", None
    retry_run_match = re.fullmatch(
        r"/v1/operation-runs/environment-creation/([0-9a-fA-F-]{36})/retry",
        path,
    )
    if method == "POST" and retry_run_match:
        return "resource.environment.run.retry", retry_run_match.group(1)
    if method == "PUT" and path == "/v1/resources/buyer-accounts/snapshot":
        return "resource.buyer_account.snapshot_sync", None
    if method == "POST" and path == "/v1/resources/buyer-accounts/preflight":
        return "resource.buyer_account.import_preflight", None
    if method == "POST" and path == "/v1/purchase-orders/draft":
        return "purchase_order.draft.save", None
    if method == "POST" and path == "/v1/purchase-orders/submit":
        return "purchase_order.submit", None
    if method == "POST" and path == "/v1/procurement/claims":
        return "purchase_order.lines.claim", None
    return_match = re.fullmatch(
        r"/v1/procurement/orders/([0-9a-fA-F-]{36})/return", path
    )
    if method == "POST" and return_match:
        return "purchase_order.lines.return", return_match.group(1)
    split_match = re.fullmatch(
        r"/v1/procurement/orders/([0-9a-fA-F-]{36})/splits", path
    )
    if method == "POST" and split_match:
        return "purchase_order.split_plan.save", split_match.group(1)
    checkout_create_match = re.fullmatch(
        r"/v1/procurement/orders/([0-9a-fA-F-]{36})/checkout-attempts", path
    )
    if method == "POST" and checkout_create_match:
        return "procurement.checkout_attempt.create", checkout_create_match.group(1)
    checkout_action_match = re.fullmatch(
        r"/v1/procurement/checkout-attempts/([0-9a-fA-F-]{36})/"
        r"(revise|begin|abandon|payment-result|cleanup-result)",
        path,
    )
    if method == "POST" and checkout_action_match:
        action_by_suffix = {
            "revise": "procurement.checkout_attempt.revise",
            "begin": "procurement.checkout_attempt.begin",
            "abandon": "procurement.checkout_attempt.abandon",
            "payment-result": "procurement.checkout_attempt.payment_result",
            "cleanup-result": "procurement.checkout_attempt.cleanup_result",
        }
        return (
            action_by_suffix[checkout_action_match.group(2)],
            checkout_action_match.group(1),
        )
    shipment_match = re.fullmatch(
        r"/v1/procurement/purchase-batches/([0-9a-fA-F-]{36})/shipments", path
    )
    if method == "POST" and shipment_match:
        return "procurement.purchase_batch.shipment_upsert", shipment_match.group(1)
    if method == "GET" and path == "/v1/procurement/orders":
        return "purchase_order.workspace.list", None
    detail_match = re.fullmatch(
        r"/v1/procurement/orders/([0-9a-fA-F-]{36})", path
    )
    if method == "GET" and detail_match:
        return "purchase_order.workspace.detail.read", detail_match.group(1)
    return None, None


def _request_log_context(request: Request) -> dict[str, str | None]:
    return {
        "trace_id": getattr(request.state, "trace_id", request.state.request_id),
        "source": getattr(request.state, "log_source", "web_api"),
        "client_version": getattr(request.state, "client_version", None) or None,
    }


def _record_validation_failure(
    session: Session,
    *,
    request: Request,
    raw_token: str | None,
    action: str,
    business_object_id: str | None,
) -> None:
    _record, user, tenant = _authenticated_identity(session, raw_token)
    request.state.tenant_id = tenant.id
    request.state.actor_user_id = user.id
    request.state.actor_name = user.display_name
    _add_audit(
        session,
        request_id=request.state.request_id,
        action=action,
        result="denied",
        outcome="validation_failed",
        tenant_id=tenant.id,
        actor_user_id=user.id,
        business_object_id=business_object_id,
        failure_reason="request_validation_failed",
        details={"reason": "request_validation_failed"},
        **_request_log_context(request),
    )


def _add_audit(
    session: Session,
    *,
    request_id: str,
    action: str,
    result: str,
    tenant_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    trace_id: str | None = None,
    outcome: str | None = None,
    module: str | None = None,
    category: str | None = None,
    business_object_type: str | None = None,
    business_object_id: str | uuid.UUID | None = None,
    business_object_no: str | None = None,
    failure_reason: str | None = None,
    change_summary: dict[str, Any] | None = None,
    source: str = "api",
    client_version: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    BusinessLogService(session).append(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        result=result,
        request_id=request_id,
        trace_id=trace_id,
        outcome=outcome,
        module=module,
        category=category,
        business_object_type=business_object_type,
        business_object_id=business_object_id,
        business_object_no=business_object_no,
        failure_reason=failure_reason,
        change_summary=change_summary,
        source=source,
        client_version=client_version,
        details=details,
    )
