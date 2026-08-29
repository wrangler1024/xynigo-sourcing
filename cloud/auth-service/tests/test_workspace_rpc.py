from __future__ import annotations

import pytest

from xynigo_auth.workspace_rpc import WorkspaceRpcError, workspace_rpc_permission


@pytest.mark.parametrize(
    ("method", "path", "permission"),
    [
        ("POST", "/api/query", "fulfillment.order.read"),
        ("GET", "/api/progress", "fulfillment.order.read"),
        ("POST", "/api/envbatch/parse", "resource.environment.create"),
        ("GET", "/api/envbatch/preferences", "resource.environment.create"),
        ("POST", "/api/envbatch/preferences", "resource.environment.create"),
        ("POST", "/api/envbatch/start", "resource.environment.create"),
        ("GET", "/api/envbatch/progress", "resource.environment.create"),
        ("GET", "/api/resources/stores?refresh=1", "resource.store.read"),
        ("POST", "/api/buyer-library/import/parse", "resource.buyer.import"),
    ],
)
def test_workspace_rpc_allowlist(method: str, path: str, permission: str) -> None:
    assert workspace_rpc_permission(method, path) == permission


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/config"),
        ("GET", "/api/update/status"),
        ("POST", "/api/update/check"),
        ("POST", "/api/update/prompt"),
        ("POST", "/api/admin/members"),
        ("POST", "/api/procurement/claims"),
        ("GET", "https://attacker.invalid/api/progress"),
        ("POST", "//attacker.invalid/api/query"),
    ],
)
def test_workspace_rpc_rejects_nonlocal_or_cloud_native_paths(
    method: str, path: str
) -> None:
    with pytest.raises(WorkspaceRpcError):
        workspace_rpc_permission(method, path)
