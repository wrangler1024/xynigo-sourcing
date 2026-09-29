"""Production clients and generated Web assets must not target the test host."""

from pathlib import Path
from subprocess import CompletedProcess

from purchase_tool.cloud_auth import (
    DEFAULT_AUTH_BASE_URL, KEYCHAIN_SERVICE,
    MacKeychainAuthSessionStore, default_windows_session_path,
    origin_storage_suffix,
)
from purchase_tool.executor_channel import (
    ExecutorChannelStateStore, default_windows_executor_credential_path,
)


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ORIGIN = "https://app.xynigo.com"
TEST_ORIGIN = "https://xynigo.samforo.icu"


def test_production_client_and_web_origins_are_consistent():
    assert DEFAULT_AUTH_BASE_URL == PRODUCTION_ORIGIN
    for relative_path in (
        "src/purchase_tool/web/desktop.js",
        "src/purchase_tool/web/index.html",
        "cloud/auth-service/src/xynigo_auth/web/index.html",
        "packaging/windows/launcher/main.go",
        "packaging/macos/desktop_client.swift",
        "packaging/windows/sign-windows-artifacts.ps1",
        "cloud/auth-service/.env.example",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert PRODUCTION_ORIGIN in source, relative_path
        assert TEST_ORIGIN not in source, relative_path


def test_local_auth_and_executor_state_keep_legacy_test_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XYNIGO_DATA_DIR", str(tmp_path))
    assert origin_storage_suffix(TEST_ORIGIN) == ""
    assert origin_storage_suffix(PRODUCTION_ORIGIN) == "-production"
    assert default_windows_session_path(TEST_ORIGIN) != default_windows_session_path(
        PRODUCTION_ORIGIN)
    assert default_windows_executor_credential_path(TEST_ORIGIN) != (
        default_windows_executor_credential_path(PRODUCTION_ORIGIN))

    test_state = ExecutorChannelStateStore(base_url=TEST_ORIGIN)
    prod_state = ExecutorChannelStateStore(base_url=PRODUCTION_ORIGIN)
    test_state.save({"executorId": "test-device"})
    assert prod_state.load() == {}
    prod_state.save({"executorId": "production-device"})
    assert test_state.load()["executorId"] == "test-device"
    assert prod_state.load()["executorId"] == "production-device"


def test_production_keychain_clear_does_not_delete_test_session():
    values = {}

    def security(args, input=None, **_):
        if args[1] == "-i":
            parts = input.strip().split()
            values[parts[parts.index("-s") + 1]] = bytes.fromhex(
                parts[parts.index("-X") + 1]).decode()
            return CompletedProcess(args, 0, "", "")
        service = args[args.index("-s") + 1]
        if args[1] == "find-generic-password":
            if service not in values:
                return CompletedProcess(args, 44, "", "")
            return CompletedProcess(args, 0, values[service], "")
        if args[1] == "delete-generic-password":
            values.pop(service, None)
            return CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    test_store = MacKeychainAuthSessionStore(
        runner=security, service=KEYCHAIN_SERVICE)
    prod_store = MacKeychainAuthSessionStore(
        runner=security,
        service=KEYCHAIN_SERVICE + origin_storage_suffix(PRODUCTION_ORIGIN))
    test_store.save("t" * 64)
    assert prod_store.load() is None
    prod_store.save("p" * 64)
    prod_store.clear()
    assert test_store.load() == "t" * 64
