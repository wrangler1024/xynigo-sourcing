from pathlib import Path

import pytest

import xynigo_auth.local_executor_release as release_catalog
from xynigo_auth.local_executor_release import (
    resolve_local_executor_release_asset,
    validate_release_platforms,
)


def test_compose_mounts_reviewed_release_assets_read_only() -> None:
    compose = (
        Path(__file__).resolve().parents[1] / "compose.yaml"
    ).read_text(encoding="utf-8")
    assert "./release-assets:/app/release-assets:ro" in compose


def test_windows_standard_installer_requires_signature_timestamp_and_publisher():
    valid = {
        "windows-x86_64": {
            "installMode": "standard_per_user",
            "runtimeId": "0.12.8-testbuild001",
            "authenticodeSigned": True,
            "authenticodeTimestamped": True,
            "publisher": "Example Trusted Publisher",
        }
    }
    validate_release_platforms(valid)
    for field in (
        "authenticodeSigned",
        "authenticodeTimestamped",
        "publisher",
    ):
        candidate = {"windows-x86_64": dict(valid["windows-x86_64"])}
        candidate["windows-x86_64"][field] = False if field != "publisher" else ""
        with pytest.raises(RuntimeError):
            validate_release_platforms(candidate)


def test_windows_standard_installer_requires_versioned_runtime_id():
    valid = {
        "windows-x86_64": {
            "installMode": "standard_per_user",
            "runtimeId": "0.12.8-build001",
            "authenticodeSigned": True,
            "authenticodeTimestamped": True,
            "publisher": "Example Trusted Publisher",
        }
    }
    validate_release_platforms(valid)
    for runtime_id in ("", "0.12.6-build001", "../bad"):
        candidate = {"windows-x86_64": dict(valid["windows-x86_64"])}
        candidate["windows-x86_64"]["runtimeId"] = runtime_id
        with pytest.raises(RuntimeError):
            validate_release_platforms(candidate)


def test_macos_standard_installer_requires_full_gatekeeper_evidence():
    valid = {
        "macos-arm64": {
            "installMode": "standard_system_application",
            "developerIdInstallerSigned": True,
            "notarized": True,
            "stapled": True,
            "publisher": "Developer ID Installer: Example",
        }
    }
    validate_release_platforms(valid)
    for field in (
        "developerIdInstallerSigned",
        "notarized",
        "stapled",
        "publisher",
    ):
        candidate = {"macos-arm64": dict(valid["macos-arm64"])}
        candidate["macos-arm64"][field] = False if field != "publisher" else ""
        with pytest.raises(RuntimeError):
            validate_release_platforms(candidate)


def test_green_fallback_does_not_claim_platform_signing():
    validate_release_platforms({
        "windows-x86_64": {
            "installMode": "green_package",
            "assetName": "windows.zip",
            "sha256": "a" * 64,
            "size": 1,
        },
        "macos-arm64": {
            "installMode": "green_package",
            "assetName": "macos.zip",
            "sha256": "b" * 64,
            "size": 1,
        },
    })


def test_unsigned_standard_installer_requires_explicit_internal_test_gate():
    platforms = {
        "windows-x86_64": {
            "installMode": "standard_per_user",
            "runtimeId": "0.12.8-testbuild001",
            "internalUnsignedTest": True,
        },
        "macos-arm64": {
            "installMode": "standard_system_application",
            "internalUnsignedTest": True,
        },
    }
    with pytest.raises(RuntimeError):
        validate_release_platforms(platforms)
    validate_release_platforms(
        platforms,
        allow_unsigned_internal_test=True,
    )


def test_internal_test_gate_does_not_accept_unmarked_unsigned_installer():
    with pytest.raises(RuntimeError):
        validate_release_platforms(
            {"windows-x86_64": {"installMode": "standard_per_user"}},
            allow_unsigned_internal_test=True,
        )


def test_standard_installer_can_keep_a_valid_green_fallback(monkeypatch):
    platform = {
        "label": "Windows x86_64",
        "installMode": "standard_per_user",
        "runtimeId": "0.12.8-testbuild001",
        "assetName": "Xynigo_Setup.exe",
        "sha256": "c" * 64,
        "size": 2,
        "authenticodeSigned": True,
        "authenticodeTimestamped": True,
        "publisher": "Xynigo Internal",
        "greenFallback": {
            "installMode": "green_package",
            "assetName": "Xynigo_Green.zip",
            "sha256": "d" * 64,
            "size": 1,
            "launcherFile": "Xynigo.exe",
        },
    }
    validate_release_platforms({"windows-x86_64": platform})
    monkeypatch.setattr(
        release_catalog,
        "_PLATFORMS",
        {"windows-x86_64": platform},
    )
    payload = release_catalog.latest_local_executor_release()
    windows = payload["platforms"]["windows-x86_64"]
    assert windows["downloadUrl"] == (
        "/v1/local-executor/releases/windows-x86_64/primary/download"
    )
    assert windows["greenFallback"]["downloadUrl"] == (
        "/v1/local-executor/releases/windows-x86_64/green/download"
    )
    assert windows["greenFallback"]["launcherFile"] == "Xynigo.exe"
    assert "github.com" not in str(windows)
    primary = resolve_local_executor_release_asset(
        "windows-x86_64", "primary"
    )
    fallback = resolve_local_executor_release_asset(
        "windows-x86_64", "green"
    )
    assert primary is not None
    assert fallback is not None
    assert primary["assetName"] == "Xynigo_Setup.exe"
    assert fallback["assetName"] == "Xynigo_Green.zip"
    assert resolve_local_executor_release_asset("unknown", "primary") is None
    assert resolve_local_executor_release_asset("windows-x86_64", "other") is None


@pytest.mark.parametrize("field,value", [
    ("installMode", "standard_per_user"),
    ("assetName", "green.exe"),
    ("sha256", "not-a-digest"),
    ("size", 0),
])
def test_invalid_green_fallback_is_rejected(field, value):
    fallback = {
        "installMode": "green_package",
        "assetName": "green.zip",
        "sha256": "e" * 64,
        "size": 1,
    }
    fallback[field] = value
    with pytest.raises(RuntimeError):
        validate_release_platforms({
            "windows-x86_64": {
                "installMode": "standard_per_user",
                "runtimeId": "0.12.8-testbuild001",
                "authenticodeSigned": True,
                "authenticodeTimestamped": True,
                "publisher": "Xynigo Internal",
                "greenFallback": fallback,
            }
        })
