import pytest

import xynigo_auth.local_executor_release as release_catalog
from xynigo_auth.local_executor_release import validate_release_platforms


def test_windows_standard_installer_requires_signature_timestamp_and_publisher():
    valid = {
        "windows-x86_64": {
            "installMode": "standard_per_user",
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


def test_standard_installer_can_keep_a_valid_green_fallback(monkeypatch):
    platform = {
        "label": "Windows x86_64",
        "installMode": "standard_per_user",
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
    assert windows["downloadUrl"].endswith("/Xynigo_Setup.exe")
    assert windows["greenFallback"]["downloadUrl"].endswith(
        "/Xynigo_Green.zip"
    )
    assert windows["greenFallback"]["launcherFile"] == "Xynigo.exe"


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
                "authenticodeSigned": True,
                "authenticodeTimestamped": True,
                "publisher": "Xynigo Internal",
                "greenFallback": fallback,
            }
        })
