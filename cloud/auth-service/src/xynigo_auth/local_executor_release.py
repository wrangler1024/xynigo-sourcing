"""Trusted local-executor release catalog exposed to the cloud Web workspace.

The catalog deliberately points to an immutable GitHub tag instead of the
moving ``latest`` alias.  A new application version must update this module in
the same reviewed release change; otherwise the import-time version guard
prevents the cloud UI from advertising a stale installer.
"""

from __future__ import annotations

from urllib.parse import quote

from . import __version__


REPOSITORY = "wrangler1024/xynigo-sourcing"
RELEASE_VERSION = "0.12.7"
RELEASE_CHANNEL = "test"
RELEASE_PUBLISHED_AT = "2026-08-28T11:06:19Z"


if RELEASE_VERSION != __version__:
    raise RuntimeError(
        "local executor release catalog must match the cloud application version"
    )


_PLATFORMS = {
    "windows-x86_64": {
        "label": "Windows x86_64",
        "operatingSystem": "windows",
        "architecture": "x86_64",
        "minimumSystem": "Windows 10/11 64 位",
        "runtimeId": "0.12.7-2abf86732761",
        "assetName": "Xynigo_Sourcing_Windows_Setup_v0.12.7_hotfix10.exe",
        "sha256": "ddde1902b84f79954edccdd1a1db0f76b09ded63c08a9d3e57ab16e45b18aeba",
        "size": 15_059_897,
        "installMode": "standard_per_user",
        "internalUnsignedTest": True,
        "authenticodeSigned": False,
        "authenticodeTimestamped": False,
        "statusCenter": True,
        "trayMenu": True,
        "launcherFile": "Xynigo.exe",
        "greenFallback": {
            "assetName": "Xynigo_Sourcing_Windows_20260828_v0.12.7.zip",
            "sha256": "256efb5d4d0b947a48883dbd905e6b0d5035daa305126dc3857d899be4d3c3b4",
            "size": 18_158_605,
            "installMode": "green_package",
            "launcherFile": "Xynigo.exe",
            "statusCenter": True,
            "trayMenu": True,
        },
    },
    "macos-arm64": {
        "label": "macOS Apple Silicon",
        "operatingSystem": "macos",
        "architecture": "arm64",
        "minimumSystem": "macOS 13 及以上",
        "assetName": "Xynigo_Sourcing_macOS_Standard_v0.12.7_hotfix7.pkg",
        "sha256": "35cc3a77a27090ed00726ea26ea40fcdf503b7e69178ac4144620ac7146166b0",
        "size": 19_399_200,
        "installMode": "standard_system_application",
        "internalUnsignedTest": True,
        "developerIdApplicationSigned": False,
        "developerIdInstallerSigned": False,
        "notarized": False,
        "stapled": False,
        "greenFallback": {
            "assetName": "Xynigo_Sourcing_macOS_arm64_20260828_v0.12.7.zip",
            "sha256": "11a5039fa658d0dfc228cb2583bba73e8307344341223fd7ed0caa55e0248919",
            "size": 10_263_121,
            "installMode": "green_package",
            "launcherFile": "启动-Mac.command",
        },
    },
}


def _validate_green_asset(
    platform_key: str,
    source: dict[str, object],
    *,
    field_name: str,
) -> None:
    if str(source.get("installMode") or "") != "green_package":
        raise RuntimeError(f"{platform_key} {field_name} must be a green package")
    asset_name = str(source.get("assetName") or "").strip()
    sha256 = str(source.get("sha256") or "").strip().lower()
    size = source.get("size")
    if not asset_name.endswith(".zip"):
        raise RuntimeError(f"{platform_key} {field_name} requires a ZIP asset")
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise RuntimeError(f"{platform_key} {field_name} requires SHA-256")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise RuntimeError(f"{platform_key} {field_name} requires a positive size")


def validate_release_platforms(
    platforms: dict[str, dict[str, object]],
    *,
    allow_unsigned_internal_test: bool = False,
) -> None:
    """Validate installer trust, with one explicit internal-test escape hatch."""

    for platform_key, source in platforms.items():
        install_mode = str(source.get("installMode") or "")
        if install_mode.startswith("standard"):
            trusted = False
            if platform_key == "windows-x86_64":
                runtime_id = str(source.get("runtimeId") or "").strip()
                if (
                    not runtime_id.startswith(RELEASE_VERSION + "-")
                    or len(runtime_id) > 96
                    or any(
                        character not in
                        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                        for character in runtime_id
                    )
                ):
                    raise RuntimeError(
                        "Windows standard installer requires runtimeId"
                    )
                trusted = bool(
                    source.get("authenticodeSigned") is True
                    and source.get("authenticodeTimestamped") is True
                    and str(source.get("publisher") or "").strip()
                )
                if not trusted and not (
                    allow_unsigned_internal_test
                    and source.get("internalUnsignedTest") is True
                ):
                    raise RuntimeError(
                        "Windows standard installer requires Authenticode, "
                        "an RFC 3161 timestamp, and a publisher"
                    )
            elif platform_key == "macos-arm64":
                trusted = bool(
                    source.get("developerIdInstallerSigned") is True
                    and source.get("notarized") is True
                    and source.get("stapled") is True
                    and str(source.get("publisher") or "").strip()
                )
                if not trusted and not (
                    allow_unsigned_internal_test
                    and source.get("internalUnsignedTest") is True
                ):
                    raise RuntimeError(
                        "macOS standard installer requires Developer ID Installer, "
                        "notarization, stapling, and a publisher"
                    )
            else:
                raise RuntimeError(
                    f"unsupported standard installer platform: {platform_key}"
                )
        else:
            _validate_green_asset(platform_key, source, field_name="primary asset")
        fallback = source.get("greenFallback")
        if fallback is not None:
            if not isinstance(fallback, dict):
                raise RuntimeError(f"{platform_key} greenFallback must be an object")
            _validate_green_asset(
                platform_key,
                fallback,
                field_name="greenFallback",
            )


validate_release_platforms(
    _PLATFORMS,
    allow_unsigned_internal_test=RELEASE_CHANNEL == "test",
)


def _release_download_url(asset_name: str) -> str:
    encoded_asset = quote(asset_name, safe="._-")
    return (
        f"https://github.com/{REPOSITORY}/releases/download/"
        f"v{RELEASE_VERSION}/{encoded_asset}"
    )


def _system_download_path(platform_key: str, variant: str) -> str:
    encoded_platform = quote(platform_key, safe="_-.")
    encoded_variant = quote(variant, safe="_-")
    return (
        f"/v1/local-executor/releases/{encoded_platform}/"
        f"{encoded_variant}/download"
    )


def resolve_local_executor_release_asset(
    platform_key: str,
    variant: str,
) -> dict[str, object] | None:
    """Resolve one reviewed asset without accepting a caller-supplied URL."""

    platform = _PLATFORMS.get(platform_key)
    if platform is None or variant not in {"primary", "green"}:
        return None
    source: dict[str, object] | None
    if variant == "primary":
        source = platform
    else:
        fallback = platform.get("greenFallback")
        source = fallback if isinstance(fallback, dict) else None
    if source is None:
        return None
    asset_name = str(source.get("assetName") or "")
    if not asset_name:
        return None
    return {
        **source,
        "platformKey": platform_key,
        "variant": variant,
    }


def latest_local_executor_release() -> dict[str, object]:
    """Return a fresh JSON-safe copy of the immutable release catalog."""

    platforms: dict[str, dict[str, object]] = {}
    for platform_key, source in _PLATFORMS.items():
        platform = {
            **source,
            "downloadUrl": _system_download_path(platform_key, "primary"),
        }
        fallback = source.get("greenFallback")
        if isinstance(fallback, dict):
            platform["greenFallback"] = {
                **fallback,
                "downloadUrl": _system_download_path(platform_key, "green"),
            }
        platforms[platform_key] = platform
    return {
        "schemaVersion": 1,
        "product": "Xynigo Sourcing 本地执行器",
        "version": RELEASE_VERSION,
        "channel": RELEASE_CHANNEL,
        "publishedAt": RELEASE_PUBLISHED_AT,
        "releaseUrl": (
            f"https://github.com/{REPOSITORY}/releases/tag/v{RELEASE_VERSION}"
        ),
        "manifestUrl": _release_download_url(
            f"Xynigo_Sourcing_v{RELEASE_VERSION}_update.json"
        ),
        "platforms": platforms,
        "notesZh": [
            "Windows hotfix10 是在线升级引导版；hotfix9 及更早标准版需要最后手动覆盖安装一次。",
            "安装引导版后，可在云端 Web 右上角检查并确认更新，执行器会自动下载、校验、静默安装和重启。",
            "在线升级同时核对固定下载入口、文件大小与 SHA-256，并保留本地配置、查询日志和运行数据。",
            "本次按内部快速迭代策略提供未签名标准安装包；下载前必须核对 SHA-256，并按飞书开发群教程手动确认系统安全提示。",
            "当前先支持 Windows 标准安装包在线更新；macOS 标准安装包仍按原流程覆盖安装。",
        ],
    }
