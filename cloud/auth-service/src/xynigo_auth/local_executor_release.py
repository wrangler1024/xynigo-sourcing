"""Trusted local-executor release catalog exposed to the cloud Web workspace.

Installer downloads stay on the authenticated Xynigo system origin. A new
application version must update this module in the same reviewed release
change; otherwise the import-time guard prevents advertising a stale build.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from urllib.parse import quote

from . import __version__

RELEASE_VERSION = "0.17.19"
RELEASE_CHANNEL = "test"
RELEASE_PUBLISHED_AT = "2026-09-09T11:26:28Z"


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
        "runtimeId": "0.17.18-a0e450d25366",
        "assetName": "Xynigo_Sourcing_Windows_Setup_v0.17.18.exe",
        "sha256": "d9a2064e47b2ff70c0de8695a6c2aeb982d5b6c55a5ca59f380c62dc5ac46e23",
        "size": 15317763,
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
        "runtimeId": "0.17.18-a0e450d25366",
        "assetName": "Xynigo_Sourcing_macOS_Standard_v0.17.18.pkg",
        "sha256": "4e463da89cfb4fc67db48f06c95b2d684436a7839c6dfdce55387b30867e9331",
        "size": 11632164,
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
    require_synchronized_runtime: bool = False,
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
    if require_synchronized_runtime:
        standard_runtime_ids = {
            str(source.get("runtimeId") or "").strip()
            for source in platforms.values()
            if str(source.get("installMode") or "").startswith("standard")
        }
        if len(standard_runtime_ids) > 1:
            raise RuntimeError(
                "Windows and macOS release artifacts must share one runtimeId"
            )


validate_release_platforms(
    _PLATFORMS,
    allow_unsigned_internal_test=RELEASE_CHANNEL == "test",
    require_synchronized_runtime=True,
)


class ReleaseAssetIntegrityCache:
    """Verify immutable release assets once without serializing downloads.

    Standard installers are mounted read-only in production.  The file
    identity and timestamps therefore form a safe cache key together with the
    reviewed catalog digest.  Concurrent first requests coalesce around one
    SHA-256 pass; after verification every request can immediately hand its
    own FileResponse to Starlette for independent streaming.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._verified: dict[str, tuple[object, ...]] = {}
        self._inflight: set[tuple[object, ...]] = set()

    @staticmethod
    def _fingerprint(
        path: Path,
        *,
        expected_size: int,
        expected_hash: str,
    ) -> tuple[object, ...]:
        stat = path.stat()
        if stat.st_size != expected_size:
            raise OSError("asset size does not match the release catalog")
        return (
            str(path),
            int(getattr(stat, "st_dev", 0)),
            int(getattr(stat, "st_ino", 0)),
            int(stat.st_size),
            int(stat.st_mtime_ns),
            int(stat.st_ctime_ns),
            expected_hash,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def verify(
        self,
        path: Path,
        *,
        expected_size: int,
        expected_hash: str,
    ) -> None:
        fingerprint = self._fingerprint(
            path,
            expected_size=expected_size,
            expected_hash=expected_hash,
        )
        path_key = str(path)
        with self._condition:
            while fingerprint in self._inflight:
                self._condition.wait()
            if self._verified.get(path_key) == fingerprint:
                return
            self._inflight.add(fingerprint)

        verified = False
        try:
            if self._sha256(path) != expected_hash:
                raise OSError("asset digest does not match the release catalog")
            # Reject a file replaced while it was being hashed.  A later
            # request may safely retry against the replacement fingerprint.
            if self._fingerprint(
                path,
                expected_size=expected_size,
                expected_hash=expected_hash,
            ) != fingerprint:
                raise OSError("asset changed during release verification")
            verified = True
        finally:
            with self._condition:
                self._inflight.discard(fingerprint)
                if verified:
                    self._verified[path_key] = fingerprint
                self._condition.notify_all()


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
            "新增「小犀助手 › 店铺结算查询」：批量自动登录采集 SHEIN 店铺后台结算与资金数据（在途/累计未结算/下次结算/已完成结算收入/不可提现），支持无头模式与并发调节，全程只读。",
            "巡检结果一键导出《店铺结算汇总表》xlsx（标准六列与运营手工表一致，可直接交财务；另提供含附加字段与异常说明的完整导出）。",
            "登录态失效的店铺自动重新登录（含短信验证码自动接码与密码二次验证），登录态有效的店铺直接复用，失败店铺自动截图并支持一键补采。",
            "执行器与云端新增 store.finance.inspect.v1 任务通道：进度实时上报、取消、截图随 Run 归档；巡检数据表迁移 0033（需部署后生效）。",
            "无头模式下异常节点自动截图，云端可人工核验；采集对页面被人工切换的场景具备冲突保护（标记环境占用，不反复打扰）。"
        ],
    }
