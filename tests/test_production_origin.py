"""Production clients and generated Web assets must not target the test host."""

from pathlib import Path

from purchase_tool.cloud_auth import DEFAULT_AUTH_BASE_URL


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
