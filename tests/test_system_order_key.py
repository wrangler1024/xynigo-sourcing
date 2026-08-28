from purchase_tool.system_order_key import (
    create_system_order_key,
    is_system_order_key,
    legacy_order_key,
)


def test_ok1_system_order_key_is_stable_ascii_and_normalized() -> None:
    expected = "OK1-EKF8Q-ZHK40-NMC1C-ZKEH0"
    assert create_system_order_key("测试店铺", "GSH-DEMO", "XMWU-DEMO") == expected
    assert create_system_order_key(
        "  测试店铺  ", " gsh-demo ", " xmwu-demo "
    ) == expected
    assert is_system_order_key(expected)
    assert not is_system_order_key("测试店铺|GSH-DEMO|XMWU-DEMO")


def test_legacy_order_key_remains_available_only_for_compatibility() -> None:
    assert legacy_order_key(
        " Store  Name ", " gsh-demo ", " xmwu-demo "
    ) == "store name|GSH-DEMO|XMWU-DEMO"
