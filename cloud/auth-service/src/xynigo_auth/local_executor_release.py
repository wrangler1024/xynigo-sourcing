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
RELEASE_VERSION = "0.12.9"
RELEASE_CHANNEL = "test"
RELEASE_PUBLISHED_AT = "2026-08-31T05:33:21Z"


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
        "runtimeId": "0.12.9-ee64f0f09957",
        "assetName": "Xynigo_Sourcing_Windows_Setup_v0.12.9.exe",
        "sha256": "97ee3017261b0bebf262665e5977bb7f2261657cf8acae8f3dc2cde1b92cc7e6",
        "size": 15_071_916,
        "installMode": "standard_per_user",
        "internalUnsignedTest": True,
        "authenticodeSigned": False,
        "authenticodeTimestamped": False,
        "statusCenter": True,
        "trayMenu": True,
        "launcherFile": "Xynigo.exe",
        "greenFallback": {
            "assetName": "Xynigo_Sourcing_Windows_20260831_v0.12.9.zip",
            "sha256": "28be2166dcaa540eb93ba3ff82d50e618c79f0974c850eb153ae7f8d6c008f99",
            "size": 18_337_575,
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
        "assetName": "Xynigo_Sourcing_macOS_Standard_v0.12.9.pkg",
        "sha256": "7284b21990ed48613e5359ce37cc944527ad96e6876ffdb7338eda6c84acfe42",
        "size": 19_417_316,
        "installMode": "standard_system_application",
        "internalUnsignedTest": True,
        "developerIdApplicationSigned": False,
        "developerIdInstallerSigned": False,
        "notarized": False,
        "stapled": False,
        "greenFallback": {
            "assetName": "Xynigo_Sourcing_macOS_arm64_20260831_v0.12.9.zip",
            "sha256": "6f179b4559589809944c8ae1635ca3c42c5dac9ea1eb3166dd14521e073607ba",
            "size": 18_306_528,
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
            "v0.12.9 严格兼容号商 /api?type=html&mail=邮箱 接码链接，邮箱必须与账号同行一致。",
            "云端 Web 通过 workspace.rpc.v1 调度当前用户配对的执行器，设备与任务保持 owner 隔离。",
            "号商文件解析失败时清空旧统计，避免把上一次成功批次误显示为当前结果。",
            "Web 工作台不下发更新任务；Windows 在线更新继续由执行器桌面状态中心负责。",
            "Windows/macOS 标准包和绿色包均包含本次解析兼容修复。",
            "覆盖安装保留配置、日志、运行数据和设备配对；绿色包继续作为显式回退。",
            "本次按内部快速迭代策略提供未签名标准安装包；下载前必须核对 SHA-256，并按飞书开发群教程手动确认系统安全提示。",
        ],
    }
