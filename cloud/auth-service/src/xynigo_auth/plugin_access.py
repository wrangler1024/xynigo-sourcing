"""Least-privilege access for operators using only the purchase plugin."""

PLUGIN_PURCHASE_PERMISSIONS = frozenset({
    "procurement.request.read",
    "procurement.request.save",
    "procurement.request.submit",
})
PLUGIN_PURCHASE_PATHS = frozenset({
    "/v1/purchase-orders/get",
    "/v1/purchase-orders/draft",
    "/v1/purchase-orders/submit",
})
PLUGIN_ONLY_MESSAGE = "当前账号仅开通提单插件，请在店小秘提单插件中登录使用；Xynigo 系统内测需单独授权。"


def is_plugin_only(permissions) -> bool:
    permissions = set(permissions)
    return bool(permissions) and permissions <= PLUGIN_PURCHASE_PERMISSIONS
