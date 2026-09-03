# -*- coding: utf-8 -*-
"""Controlled HubStudio browser-core download, verification, and audit."""
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from .cdp import CdpClient
from .hub_api import HubApiError


CDP_BROWSER_VERSION_RE = re.compile(
    r'(Chrome|Firefox)/([0-9]{2,3})(?:\.|\b)', re.I)


class HubCoreRepairError(RuntimeError):
    def __init__(self, code, message, status=409):
        self.code = str(code)
        self.status = int(status)
        super().__init__(str(message))


def _safe_text(value, limit=160):
    return str(value or '').replace('\r', ' ').replace('\n', ' ')[:limit]


class HubCoreRepairCoordinator(object):
    """Run one repair at a time and expose only non-sensitive state."""

    def __init__(self, hub_getter, task_coordinator, audit_path,
                 cdp_factory=CdpClient, sleep_fn=time.sleep,
                 monotonic_fn=time.monotonic, verify_timeout=900,
                 verify_interval=5, device_info_getter=lambda: {}):
        self.hub_getter = hub_getter
        self.task_coordinator = task_coordinator
        self.audit_path = os.path.abspath(audit_path)
        self.cdp_factory = cdp_factory
        self.sleep = sleep_fn
        self.monotonic = monotonic_fn
        self.verify_timeout = max(1.0, float(verify_timeout))
        self.verify_interval = max(0.01, float(verify_interval))
        self.device_info_getter = device_info_getter
        self.lock = threading.Lock()
        self.audit_lock = threading.Lock()
        self.state = {
            'state': 'idle', 'running': False,
            'browserType': '', 'coreVersion': '',
            'message': '尚未检测到需要修复的内核',
            'errorCode': '', 'startedAt': None, 'finishedAt': None,
            'auditId': '', 'auditState': 'not_started',
        }

    @staticmethod
    def _iso_now():
        return datetime.now(timezone.utc).isoformat()

    def _requirement(self):
        getter = getattr(self.hub_getter(), 'core_requirement_snapshot', None)
        return getter() if callable(getter) else {'known': False}

    def snapshot(self):
        requirement = self._requirement()
        with self.lock:
            result = dict(self.state)
        if (not result['running'] and result['state'] in {'idle', 'required'}
                and requirement.get('known')):
            result.update({
                'state': 'required',
                'browserType': str(requirement.get('browserType') or ''),
                'coreVersion': str(requirement.get('version') or ''),
                'message': '检测到 HubStudio 浏览器内核缺失',
                'errorCode': 'hubstudio_browser_core_missing',
            })
        result['repairAvailable'] = bool(
            result['state'] in {'required', 'failed'}
            and result.get('browserType') == 'chrome'
            and result.get('coreVersion'))
        return result

    def start(self, actor=None):
        requirement = self._requirement()
        if not requirement.get('known'):
            raise HubCoreRepairError(
                'hubstudio_core_requirement_unknown',
                '尚未从真实环境启动错误中识别出需要下载的内核版本')
        browser_type = str(requirement.get('browserType') or '')
        core_version = str(requirement.get('version') or '')
        container_code = str(requirement.get('containerCode') or '')
        if browser_type != 'chrome':
            raise HubCoreRepairError(
                'hubstudio_core_repair_unsupported',
                '当前执行器仅支持自动验证 Chrome 内核')
        task_id = None
        with self.lock:
            if self.state.get('running'):
                raise HubCoreRepairError(
                    'hubstudio_core_repair_in_progress',
                    'HubStudio 内核修复正在进行')
            task_id = self.task_coordinator.begin('hub_core_repair')
            audit_id = secrets.token_hex(12)
            started_at = self._iso_now()
            self.state = {
                'state': 'downloading', 'running': True,
                'browserType': browser_type,
                'coreVersion': core_version,
                'message': 'HubStudio 正在下载所需浏览器内核',
                'errorCode': '', 'startedAt': started_at,
                'finishedAt': None, 'auditId': audit_id,
                'auditState': 'recording',
            }
        actor_info = self._actor_info(actor)
        audit_context = {**actor_info, **self._device_info()}
        audit_started = self._audit({
            'event': 'hub_core_repair_requested',
            'auditId': audit_id,
            'occurredAt': started_at,
            'browserType': browser_type,
            'coreVersion': core_version,
            **audit_context,
        })
        if not audit_started:
            self.task_coordinator.finish(task_id)
            self._set_state(
                state='failed', running=False,
                message='无法写入内核修复审计日志，已阻止下载',
                errorCode='hubstudio_core_audit_unavailable',
                finishedAt=self._iso_now(), auditState='write_failed')
            raise HubCoreRepairError(
                'hubstudio_core_audit_unavailable',
                '无法写入内核修复审计日志，已阻止下载', status=503)
        threading.Thread(
            target=self._run,
            args=(task_id, audit_id, browser_type, core_version,
                  container_code, audit_context),
            name='xynigo-hub-core-repair', daemon=True).start()
        return self.snapshot()

    @staticmethod
    def _actor_info(identity):
        identity = identity if isinstance(identity, dict) else {}
        user = identity.get('user') if isinstance(identity.get('user'), dict) \
            else {}
        return {
            'actorId': _safe_text(user.get('id'), 80),
            'actorName': _safe_text(user.get('name'), 80),
        }

    def _device_info(self):
        try:
            source = self.device_info_getter()
        except Exception:
            source = {}
        source = source if isinstance(source, dict) else {}
        return {
            'deviceName': _safe_text(source.get('displayName'), 120),
            'platform': _safe_text(source.get('platform'), 24),
            'clientVersion': _safe_text(source.get('clientVersion'), 32),
        }

    def _set_state(self, **changes):
        with self.lock:
            self.state.update(changes)

    def _run(self, task_id, audit_id, browser_type, core_version,
             container_code, audit_context):
        final_state = 'failed'
        result_code = 'hubstudio_core_repair_failed'
        message = 'HubStudio 内核修复失败'
        try:
            hub = self.hub_getter()
            hub.download_core(browser_type, core_version)
            self._set_state(
                state='verifying',
                message='内核下载请求已完成，正在启动环境验证')
            self._verify(hub, browser_type, core_version, container_code)
            final_state = 'ready'
            result_code = 'ok'
            message = 'HubStudio %s %s 内核已下载并验证可用' % (
                browser_type.capitalize(), core_version)
        except HubCoreRepairError as exc:
            result_code = exc.code
            message = _safe_text(exc, 240)
        except HubApiError as exc:
            result_code = exc.reason_code
            message = _safe_text(exc, 240)
        except Exception:
            result_code = 'hubstudio_core_repair_failed'
            message = 'HubStudio 内核修复发生未知错误，请查看脱敏诊断日志'
        finally:
            finished_at = self._iso_now()
            audit_completed = self._audit({
                'event': 'hub_core_repair_completed',
                'auditId': audit_id,
                'occurredAt': finished_at,
                'browserType': browser_type,
                'coreVersion': core_version,
                'resultCode': result_code,
                'state': final_state,
                **audit_context,
            })
            if final_state == 'ready' and audit_completed:
                hub = self.hub_getter()
                hub.clear_runtime_failure()
                clear_requirement = getattr(
                    hub, 'clear_core_requirement', None)
                if callable(clear_requirement):
                    clear_requirement()
            elif final_state == 'ready':
                final_state = 'failed'
                result_code = 'hubstudio_core_audit_unavailable'
                message = '内核已验证，但审计结果写入失败，请重新执行修复确认'
            self._set_state(
                state=final_state, running=False, message=message,
                errorCode='' if final_state == 'ready' else result_code,
                finishedAt=finished_at,
                auditState=('recorded' if audit_completed else 'write_failed'))
            self.task_coordinator.finish(task_id)

    def _verify(self, hub, browser_type, core_version, container_code):
        deadline = self.monotonic() + self.verify_timeout
        while True:
            started = False
            verified = False
            try:
                data = hub.browser_start(container_code, headless=True)
                started = True
                try:
                    port = int((data or {}).get('debuggingPort'))
                except (TypeError, ValueError):
                    port = 0
                if port < 1:
                    raise HubCoreRepairError(
                        'hubstudio_browser_launch_invalid',
                        'HubStudio 验证环境未返回调试端口')
                version = self.cdp_factory(port).version_info()
                browser = str(version.get('browser') or '')
                match = CDP_BROWSER_VERSION_RE.search(browser)
                actual_type = match.group(1).casefold() if match else ''
                actual_version = match.group(2) if match else ''
                if (actual_type != browser_type
                        or actual_version != core_version):
                    raise HubCoreRepairError(
                        'hubstudio_core_verification_mismatch',
                        'HubStudio 实际启动的内核版本与目标版本不一致')
                verified = True
            except HubApiError as exc:
                if (exc.reason_code == 'hubstudio_browser_core_missing'
                        and self.monotonic() < deadline):
                    self.sleep(self.verify_interval)
                    continue
                if exc.reason_code == 'hubstudio_browser_core_missing':
                    raise HubCoreRepairError(
                        'hubstudio_core_download_timeout',
                        'HubStudio 内核下载后仍不可用，请打开更新中心检查')
                raise
            finally:
                if started:
                    try:
                        hub.browser_stop(container_code)
                    except HubApiError:
                        raise HubCoreRepairError(
                            'hubstudio_core_verification_cleanup_failed',
                            '内核验证后，测试环境未能安全关闭')
            if verified:
                return

    def _audit(self, record):
        try:
            directory = os.path.dirname(self.audit_path)
            os.makedirs(directory, exist_ok=True)
            line = json.dumps(
                record, ensure_ascii=False, sort_keys=True,
                separators=(',', ':')) + '\n'
            with self.audit_lock:
                descriptor = os.open(
                    self.audit_path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600)
                try:
                    os.chmod(self.audit_path, 0o600)
                except OSError:
                    pass
                with os.fdopen(descriptor, 'a', encoding='utf-8') as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            return True
        except OSError:
            # Audit I/O failure must not leave the HubStudio browser running.
            return False
