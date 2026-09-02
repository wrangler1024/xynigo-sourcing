"""Trusted local-executor release catalog exposed to the cloud Web workspace.

Installer downloads stay on the authenticated Xynigo system origin. A new
application version must update this module in the same reviewed release
change; otherwise the import-time guard prevents advertising a stale build.
"""

from __future__ import annotations

from urllib.parse import quote

from . import __version__


RELEASE_VERSION = "0.13.8"
RELEASE_CHANNEL = "test"
RELEASE_PUBLISHED_AT = "2026-09-02T09:19:39Z"


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
        "runtimeId": "0.13.8-025670e824db",
        "assetName": "Xynigo_Sourcing_Windows_Setup_v0.13.8.exe",
        "sha256": "f1ed56e166a27ec7e02044a55e950586d9341eecc358a3762abfce5a253a3514",
        "size": 15_158_644,
        "installMode": "standard_per_user",
        "onlineUpdate": True,
        "onlineUpdateFlow": "authenticated_download_sha256_silent_installer",
        "internalUnsignedTest": True,
        "authenticodeSigned": False,
        "authenticodeTimestamped": False,
        "statusCenter": True,
        "trayMenu": True,
        "launcherFile": "Xynigo.exe",
    },
    "macos-arm64": {
        "label": "macOS Apple Silicon",
        "operatingSystem": "macos",
        "architecture": "arm64",
        "minimumSystem": "macOS 13 及以上",
        "runtimeId": "0.13.8-025670e824db",
        "assetName": "Xynigo_Sourcing_macOS_Standard_v0.13.8.pkg",
        "sha256": "51305aff73662bf24efd76fb7d0af9c1f7e777698b7670648bbff8d712daa274",
        "size": 11_422_380,
        "installMode": "standard_system_application",
        "onlineUpdate": True,
        "onlineUpdateFlow": "authenticated_download_sha256_system_installer",
        "updateAutoRelaunch": True,
        "internalUnsignedTest": True,
        "developerIdApplicationSigned": False,
        "developerIdInstallerSigned": False,
        "notarized": False,
        "stapled": False,
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
                    f"{platform_key} standard installer requires runtimeId"
                )
            trusted = False
            if platform_key == "windows-x86_64":
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
        "releaseUrl": "",
        "manifestUrl": "",
        "platforms": platforms,
        "notesZh": [
            "数据源注册表新增安全详情、认领、名称与启用状态编辑。",
            "已有数据源可重新选择飞书表格和工作表，替换后保留默认值与环境映射。",
            "团队数据源可设置或取消团队默认，重新验证会执行真实飞书只读检查。",
            "Spreadsheet Token、Sheet ID 和表格内容继续禁止在前端回显。",
            "Windows 与 macOS 继续支持认证下载、SHA-256 校验、平台安装与自动重启。",
            "稳定通道强制平台签名门槛；内部未签名包仅允许 test 通道。",
        ],
    }
