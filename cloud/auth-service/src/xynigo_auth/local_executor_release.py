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
RELEASE_VERSION = "0.13.0"
RELEASE_CHANNEL = "test"
RELEASE_PUBLISHED_AT = "2026-09-01T10:56:39Z"


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
        "runtimeId": "0.13.0-6f217bc3b33c",
        "assetName": "Xynigo_Sourcing_Windows_Setup_v0.13.0.exe",
        "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
        "size": 1_000_001,
        "installMode": "standard_per_user",
        "internalUnsignedTest": True,
        "authenticodeSigned": False,
        "authenticodeTimestamped": False,
        "statusCenter": True,
        "trayMenu": True,
        "launcherFile": "Xynigo.exe",
        "greenFallback": {
            "assetName": "Xynigo_Sourcing_Windows_20260901_v0.13.0.zip",
            "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            "size": 1,
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
        "assetName": "Xynigo_Sourcing_macOS_Standard_v0.13.0.pkg",
        "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
        "size": 1_000_001,
        "installMode": "standard_system_application",
        "internalUnsignedTest": True,
        "developerIdApplicationSigned": False,
        "developerIdInstallerSigned": False,
        "notarized": False,
        "stapled": False,
        "greenFallback": {
            "assetName": "Xynigo_Sourcing_macOS_arm64_20260901_v0.13.0.zip",
            "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            "size": 1,
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
            "v0.13.0 将环境创建与物流查询迁移到可持久化云端业务 Run。",
            "刷新、切页或重新登录后从云端恢复任务生命周期、进度、结果和最后心跳。",
            "买家号文件通过专用加密任务通道交由所选本地执行器解析，云端不保存明文文件。",
            "工作区快照恢复站点、采购分组、预检和非敏感设备运行配置。",
            "环境创建页面按当前站点和分组统一预检、预览与正式执行，并持续显示出口 IP 状态。",
            "新建环境不再硬编码 Chrome 148；内核缺失时立即提示安装或切换可用内核。",
            "安全停止、单行重试和失败项批量重试继续保留，并统一由云端 Run 状态驱动。",
            "覆盖安装保留配置、日志、运行数据和设备配对；绿色包继续作为显式回退。",
            "本次按内部快速迭代策略提供未签名标准安装包；下载前必须核对 SHA-256，并按飞书开发群教程手动确认系统安全提示。",
        ],
    }
