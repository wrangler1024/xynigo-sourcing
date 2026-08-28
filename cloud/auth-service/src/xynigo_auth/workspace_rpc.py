"""Allowlisted cloud-Web operations executed by the paired local executor."""

from __future__ import annotations

from urllib.parse import urlsplit


class WorkspaceRpcError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


GET_PERMISSIONS = {
    "/api/hub-status": "fulfillment.order.read",
    "/api/groups": "fulfillment.order.read",
    "/api/group-envs": "fulfillment.order.read",
    "/api/progress": "fulfillment.order.read",
    "/api/tasks": "operations.access",
    "/api/register/progress": "resource.buyer.import",
    "/api/resources/stores": "resource.store.read",
    "/api/resources/stores/export": "resource.store.read",
    "/api/resources/proxies": "resource.ip.read",
    "/api/resources/proxies/export": "resource.ip.read",
    "/api/resources/proxies/check/history": "resource.ip.read",
    "/api/resources/proxies/check/progress": "resource.ip.test",
    "/api/envbatch/progress": "resource.environment.create",
    "/api/envbatch/preflight": "resource.environment.create",
    "/api/envbatch/template": "resource.environment.create",
    "/api/envbatch/export-mapping": "resource.environment.create",
    "/api/envbatch/backup/preview": "resource.environment.create",
    "/api/envbatch/backup/progress": "resource.environment.create",
    "/api/envbatch/backup/result": "resource.environment.create",
    "/api/export": "fulfillment.order.export",
    "/api/screenshot": "fulfillment.order.read",
    "/api/lark/status": "system.lark_connection.manage",
    "/api/lark/config": "system.lark_connection.manage",
    "/api/lark/template": "system.lark_connection.manage",
    "/api/lark/target-url": "system.lark_connection.manage",
}

POST_PERMISSIONS = {
    "/api/query": "fulfillment.order.read",
    "/api/stop": "fulfillment.order.read",
    "/api/requery": "fulfillment.order.read",
    "/api/requery-failed": "fulfillment.order.read",
    "/api/register/validate": "resource.buyer.import",
    "/api/register/start": "resource.buyer.import",
    "/api/buyer-library/import/parse": "resource.buyer.import",
    "/api/buyer-library/import/commit": "resource.buyer.import",
    "/api/resources/proxies/check/start": "resource.ip.test",
    "/api/resources/proxies/check/stop": "resource.ip.test",
    "/api/envbatch/parse": "resource.environment.create",
    "/api/envbatch/preview": "resource.environment.create",
    "/api/envbatch/start": "resource.environment.create",
    "/api/envbatch/retry-row": "resource.environment.create",
    "/api/envbatch/backup/start": "resource.environment.create",
    "/api/lark/config": "system.lark_connection.manage",
    "/api/lark/preflight": "system.lark_connection.manage",
    "/api/hub-api-key": "system.integration.manage",
}


def workspace_rpc_permission(method: str, target: str) -> str:
    method = str(method or "").upper()
    parsed = urlsplit(str(target or ""))
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise WorkspaceRpcError("workspace_rpc_path_invalid")
    if parsed.path.startswith("//") or not parsed.path.startswith("/api/"):
        raise WorkspaceRpcError("workspace_rpc_path_invalid")
    permissions = GET_PERMISSIONS if method == "GET" else POST_PERMISSIONS
    permission = permissions.get(parsed.path)
    if permission is None:
        raise WorkspaceRpcError("workspace_rpc_operation_unsupported")
    return permission
