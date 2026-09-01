# -*- coding: utf-8 -*-
"""Outbound-only P1 channel between the local executor and Xynigo cloud.

The durable device credential lives in macOS Keychain or Windows CurrentUser
DPAPI.  The JSON state file contains identifiers and diagnostics only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform as platform_module
import random
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

from . import __version__
from .cloud_auth import (
    CloudAuthClient,
    DEFAULT_AUTH_BASE_URL,
    LocalAuthError,
    MacKeychainAuthSessionStore,
    MemoryAuthSessionStore,
    WindowsDpapiAuthSessionStore,
    _validated_token,
)
from .operation_executor import (
    BUSINESS_TASK_TYPES, OperationExecutionError,
)


EXECUTOR_KEYCHAIN_SERVICE = 'io.xynigo.sourcing.executor'
EXECUTOR_KEYCHAIN_ACCOUNT = 'xynigo-device-credential'
SUPPORTED_CAPABILITIES = (
    'config.read.v1',
    'config.write.v1',
    'workspace.rpc.v1',
    'environment.parse.v1',
    'logistics.query.v1',
    'environment.create-bound.v1',
    'environment.create-backup.v1',
)
MAX_WORKSPACE_RPC_BYTES = 32 * 1024 * 1024
CHANNEL_STATE_FIELDS = frozenset({
    'executorId', 'displayName', 'platform', 'architecture', 'pairedAt',
    'lastPollAt', 'lastErrorCode', 'status', 'configRevision',
})


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def default_executor_state_path():
    data_dir = os.environ.get('XYNIGO_DATA_DIR') or os.getcwd()
    return Path(data_dir) / '运行数据' / 'executor-channel.json'


def default_windows_executor_credential_path():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise LocalAuthError('credential_store_failed')
    return Path(base) / 'Xynigo' / 'credentials' / 'executor-device.bin'


def system_executor_credential_store():
    if sys.platform == 'darwin':
        return MacKeychainAuthSessionStore(
            account=EXECUTOR_KEYCHAIN_ACCOUNT,
            service=EXECUTOR_KEYCHAIN_SERVICE,
        )
    if os.name == 'nt':
        return WindowsDpapiAuthSessionStore(
            path=default_windows_executor_credential_path())
    return MemoryAuthSessionStore()


class ExecutorChannelStateStore(object):
    def __init__(self, path=None):
        self.path = Path(path) if path else default_executor_state_path()
        self.lock = threading.RLock()

    def load(self):
        with self.lock:
            try:
                payload = json.loads(self.path.read_text(encoding='utf-8'))
            except (FileNotFoundError, OSError, ValueError):
                return {}
            if not isinstance(payload, dict):
                return {}
            return {key: payload[key] for key in CHANNEL_STATE_FIELDS
                    if key in payload}

    def save(self, payload):
        if not isinstance(payload, dict) or set(payload) - CHANNEL_STATE_FIELDS:
            raise ValueError('执行器通道状态包含不允许保存的字段')
        # Defense in depth: device/lease/session secrets must never enter this
        # recoverable diagnostics file.
        lowered = ' '.join(str(key).casefold() for key in payload)
        if any(word in lowered for word in ('credential', 'token', 'password',
                                             'cookie', 'authorization')):
            raise ValueError('执行器通道状态不能包含凭证')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix='.executor-channel-', suffix='.tmp',
            dir=str(self.path.parent))
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def update(self, **changes):
        payload = self.load()
        payload.update(changes)
        self.save(payload)
        return payload


def config_revision(config):
    canonical = json.dumps(
        config if isinstance(config, dict) else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def local_platform():
    if sys.platform == 'darwin':
        system = 'macos'
    elif os.name == 'nt':
        system = 'windows'
    else:
        raise ValueError('P1 本地执行器仅支持 Windows 和 macOS')
    machine = platform_module.machine().casefold()
    architecture = 'arm64' if machine in ('arm64', 'aarch64') else 'x86_64'
    if system == 'macos' and architecture != 'arm64':
        raise ValueError('当前安装包仅支持 Apple Silicon macOS')
    return system, architecture


class CloudExecutorClient(object):
    def __init__(self, client=None):
        self.client = client or CloudAuthClient(
            os.environ.get('XYNIGO_AUTH_BASE_URL') or DEFAULT_AUTH_BASE_URL,
            timeout=35.0,
        )

    def pair(self, pairing_code, display_name, system, architecture):
        payload = self.client._request(
            '/v1/executor-channel/pair',
            method='POST',
            payload={
                'pairingCode': str(pairing_code or '').strip(),
                'displayName': str(display_name or '').strip(),
                'platform': system,
                'architecture': architecture,
                'clientVersion': __version__,
                'protocolVersion': 1,
                'capabilities': list(SUPPORTED_CAPABILITIES),
            },
            source='local_executor_device',
        )
        executor_id = str(payload.get('executorId') or '').strip()
        credential = _validated_token(payload.get('deviceCredential'))
        if not executor_id or str(payload.get('credentialType')) != 'Bearer':
            raise LocalAuthError(
                'cloud_response_invalid', '云端配对响应无效', 502)
        return {'executorId': executor_id, 'deviceCredential': credential}

    def poll(self, credential, revision, hub_status, wait_seconds=25):
        return self.client._request(
            '/v1/executor-channel/poll',
            method='POST',
            payload={
                'waitSeconds': max(0, min(25, int(wait_seconds))),
                'configRevision': revision,
                'hubStatus': hub_status,
                'clientVersion': __version__,
                'protocolVersion': 1,
                'capabilities': list(SUPPORTED_CAPABILITIES),
            },
            token=credential,
            source='local_executor_device',
            max_response_bytes=MAX_WORKSPACE_RPC_BYTES,
        )

    def issue_user_session(self, credential):
        payload = self.client._request(
            '/v1/executor-channel/session',
            method='POST',
            payload={},
            token=credential,
            source='local_executor_device',
        )
        return {
            'sessionToken': _validated_token(payload.get('sessionToken')),
            'sessionExpiresAt': str(payload.get('sessionExpiresAt') or ''),
        }

    def start(self, credential, task_id, lease_token):
        return self._task_request(
            credential, task_id, 'start', 'POST',
            {'leaseToken': lease_token})

    def renew(self, credential, task_id, lease_token):
        return self._task_request(
            credential, task_id, 'lease', 'PUT',
            {'leaseToken': lease_token})

    def progress(self, credential, task_id, lease_token, phase,
                 current=None, total=None, stable_code=None, snapshot=None):
        payload = {'leaseToken': lease_token, 'phase': phase}
        if current is not None:
            payload['current'] = current
        if total is not None:
            payload['total'] = total
        if stable_code:
            payload['stableCode'] = stable_code
        if snapshot is not None:
            payload['snapshot'] = snapshot
        return self._task_request(
            credential, task_id, 'progress', 'POST', payload)

    def finish(self, credential, task_id, lease_token, outcome,
               result_code, result_summary):
        return self._task_request(
            credential, task_id, 'finish', 'POST', {
                'leaseToken': lease_token,
                'outcome': outcome,
                'resultCode': result_code,
                'resultSummary': result_summary,
            }, max_response_bytes=MAX_WORKSPACE_RPC_BYTES)

    def _task_request(self, credential, task_id, suffix, method, payload,
                      max_response_bytes=1024 * 1024):
        task_id = str(task_id or '').strip()
        if not task_id or '/' in task_id:
            raise LocalAuthError(
                'cloud_response_invalid', '云端执行器任务编号无效', 500)
        return self.client._request(
            '/v1/executor-channel/tasks/%s/%s' % (task_id, suffix),
            method=method,
            payload=payload,
            token=credential,
            source='local_executor_device',
            max_response_bytes=max_response_bytes,
        )


class ExecutorChannelWorker(object):
    def __init__(self, client, credential_store, state_store,
                 config_getter, public_config_getter, config_writer,
                 task_coordinator, hub_status_getter,
                 workspace_rpc_executor=None,
                 operation_task_executor=None,
                 user_session_installer=None,
                 sleep_fn=time.sleep, random_fn=random.random):
        self.client = client
        self.credential_store = credential_store
        self.state_store = state_store
        self.config_getter = config_getter
        self.public_config_getter = public_config_getter
        self.config_writer = config_writer
        self.task_coordinator = task_coordinator
        self.hub_status_getter = hub_status_getter
        self.workspace_rpc_executor = workspace_rpc_executor
        self.operation_task_executor = operation_task_executor
        self.user_session_installer = user_session_installer
        self.sleep = sleep_fn
        self.random = random_fn
        self.stop_event = threading.Event()
        self.thread = None
        self.pending_finish = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return True
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._run, name='xynigo-executor-channel', daemon=True)
        self.thread.start()
        return True

    def stop(self, timeout=5.0):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=max(0.0, float(timeout)))

    def _run(self):
        backoff = 1.0
        consecutive_failures = 0
        credential = None
        user_session_ready = False
        user_session_refresh_at = 0.0
        while not self.stop_event.is_set():
            try:
                if not credential:
                    credential = self.credential_store.load()
                    if not credential:
                        state = self.state_store.load()
                        state_status = str(state.get('status') or '')
                        paired = bool(state.get('executorId')) and (
                            state_status != 'revoked')
                        desired_status = (
                            'credential_error' if paired
                            else ('revoked' if state_status == 'revoked'
                                  else 'not_paired'))
                        desired_error = (
                            'executor_credential_unavailable'
                            if paired else str(state.get('lastErrorCode') or ''))
                        if (state_status != desired_status
                                or str(state.get('lastErrorCode') or '')
                                != desired_error):
                            self.state_store.update(
                                status=desired_status,
                                lastErrorCode=desired_error,
                            )
                        # An already-running executor must notice a later
                        # xynigo://pair process. Missing unpaired credentials
                        # are cheap to recheck; a paired-but-unavailable
                        # Keychain item backs off to avoid repeated prompts.
                        self._wait(30.0 if paired else 1.0)
                        continue
                    self.state_store.update(
                        status='connecting', lastErrorCode='')
                if (callable(self.user_session_installer)
                        and (not user_session_ready
                             or time.monotonic() >= user_session_refresh_at)):
                    issued = self.client.issue_user_session(credential)
                    self.user_session_installer(issued['sessionToken'])
                    user_session_ready = True
                    user_session_refresh_at = time.monotonic() + 4 * 60 * 60
                if self.pending_finish:
                    self._flush_pending_finish(credential)
                cfg = self.config_getter()
                revision = config_revision(self.public_config_getter(cfg))
                hub_ok, _hub_message = self.hub_status_getter(False)
                response = self.client.poll(
                    credential, revision,
                    'ready' if hub_ok else 'offline', wait_seconds=25)
                self.state_store.update(
                    status='online', lastPollAt=_now_iso(),
                    lastErrorCode='', configRevision=revision)
                backoff = 1.0
                consecutive_failures = 0
                task = response.get('task') if isinstance(response, dict) else None
                if task:
                    self._execute_task(credential, task)
            except LocalAuthError as exc:
                if exc.code in ('executor_revoked',
                                'executor_credential_invalid'):
                    try:
                        self.credential_store.clear()
                    finally:
                        self.state_store.update(
                            status='revoked', lastErrorCode=exc.code)
                    credential = None
                    user_session_ready = False
                    user_session_refresh_at = 0.0
                    backoff = 1.0
                    self._wait(1.0)
                    continue
                consecutive_failures += 1
                self.state_store.update(
                    status=('offline' if consecutive_failures >= 3
                            else 'reconnecting'),
                    lastErrorCode=exc.code)
                self._wait(backoff + self.random() * min(1.0, backoff / 4.0))
                backoff = min(30.0, backoff * 2.0)
            except Exception:
                consecutive_failures += 1
                self.state_store.update(
                    status=('error' if consecutive_failures >= 3
                            else 'reconnecting'),
                    lastErrorCode='executor_channel_failed')
                self._wait(backoff)
                backoff = min(30.0, backoff * 2.0)

    def _execute_task(self, credential, task):
        task_id = str(task.get('id') or '')
        task_type = str(task.get('type') or '')
        lease_token = _validated_token(task.get('leaseToken'))
        payload = task.get('payload')
        if not task_id or not isinstance(payload, dict):
            raise LocalAuthError(
                'cloud_response_invalid', '云端执行器任务无效', 502)
        self.client.start(credential, task_id, lease_token)
        renewal_stop = threading.Event()
        renewal_error = []
        cancellation_event = threading.Event()
        if bool(task.get('cancellationRequested')):
            cancellation_event.set()

        def renew_loop():
            while not renewal_stop.wait(15.0):
                try:
                    renewed = self.client.renew(
                        credential, task_id, lease_token)
                    renewed_task = (
                        renewed.get('task')
                        if isinstance(renewed, dict) else None)
                    if (isinstance(renewed_task, dict)
                            and renewed_task.get('cancellationRequested')):
                        cancellation_event.set()
                except Exception as exc:
                    renewal_error.append(exc)
                    return

        renew_thread = threading.Thread(
            target=renew_loop, name='xynigo-executor-lease', daemon=True)
        renew_thread.start()
        local_task_id = None
        task_kind = (
            'operation' if task_type in BUSINESS_TASK_TYPES
            else 'config' if task_type.startswith('config.') else '')
        phase_prefix = task_kind or 'workspace'

        def report(phase, current=None, total=None, stable_code=None,
                   snapshot=None):
            return self.client.progress(
                credential, task_id, lease_token, phase,
                current, total, stable_code, snapshot)

        try:
            if task_kind == 'config':
                local_task_id = self.task_coordinator.begin(task_kind)
            if task_kind != 'operation':
                report(phase_prefix + '.executing', 0, 1)
            outcome, result_code, result_summary = self._apply_task(
                task_type, payload, progress_report=report,
                cancellation_event=cancellation_event)
            if task_kind != 'operation':
                report(
                    phase_prefix + '.completed', 1, 1,
                    result_code)
        except Exception as exc:
            outcome = 'failed'
            if isinstance(exc, LocalAuthError):
                result_code = exc.code
            elif isinstance(exc, OperationExecutionError):
                result_code = exc.code
            elif isinstance(exc, ValueError):
                result_code = (
                    'config_write_rejected' if task_kind
                    else 'workspace_rpc_rejected')
            else:
                result_code = (
                    'config_task_failed' if task_kind
                    else 'workspace_rpc_failed')
            result_summary = (
                {'configRevision': config_revision(self.config_getter())}
                if task_kind == 'config' else {
                    'runStatus': 'failed',
                    'phase': phase_prefix + '.failed',
                } if task_kind == 'operation' else {})
        finally:
            if local_task_id:
                self.task_coordinator.finish(local_task_id)
            renewal_stop.set()
            renew_thread.join(timeout=1.0)
        if renewal_error:
            self.state_store.update(lastErrorCode='executor_lease_renew_failed')
        self.pending_finish = (
            credential, task_id, lease_token, outcome,
            result_code, result_summary)
        self._flush_pending_finish(credential)

    def _apply_task(self, task_type, payload, progress_report=None,
                    cancellation_event=None):
        if task_type in BUSINESS_TASK_TYPES:
            if not callable(self.operation_task_executor):
                raise LocalAuthError(
                    'executor_capability_missing',
                    '正式业务任务执行能力尚未就绪', 409)
            return self.operation_task_executor(
                task_type, payload, progress_report,
                cancellation_event=cancellation_event)
        if task_type == 'environment.parse.v1':
            if not callable(self.workspace_rpc_executor):
                raise LocalAuthError(
                    'executor_capability_missing',
                    '买家号文件解析能力尚未就绪', 409)
            result = self.workspace_rpc_executor({
                'method': 'POST',
                'path': '/api/envbatch/parse',
                'body': payload,
            })
            if (not isinstance(result, dict)
                    or result.get('responseType') != 'json'
                    or not isinstance(result.get('body'), dict)):
                raise LocalAuthError(
                    'environment_parse_response_invalid',
                    '买家号文件解析响应无效', 502)
            status = int(result.get('httpStatus') or 0)
            if status < 200 or status >= 300:
                body = result['body']
                raise LocalAuthError(
                    str(body.get('code') or 'environment_parse_failed'),
                    str(body.get('error') or '买家号文件解析失败'),
                    status or 500)
            return (
                'succeeded', 'environment_parse_completed',
                result['body'])
        if task_type == 'workspace.rpc.v1':
            if not callable(self.workspace_rpc_executor):
                raise LocalAuthError(
                    'executor_capability_missing',
                    '云端工作台执行能力尚未就绪', 409)
            return (
                'succeeded', 'workspace_rpc_completed',
                self.workspace_rpc_executor(payload))
        current = self.config_getter()
        current_public = self.public_config_getter(current)
        current_revision = config_revision(current_public)
        if task_type == 'config.read.v1':
            return (
                'succeeded', 'config_read_succeeded', {
                    'configRevision': current_revision,
                    'config': current_public,
                })
        if task_type != 'config.write.v1':
            raise LocalAuthError(
                'executor_capability_missing', '不支持的云端执行器任务', 409)
        expected = str(payload.get('expectedRevision') or '')
        if expected != current_revision:
            raise LocalAuthError(
                'config_revision_conflict', '本地配置已变化，请刷新后重试', 409)
        submitted = payload.get('config')
        if not isinstance(submitted, dict):
            raise ValueError('配置任务缺少配置对象')
        updated = self.config_writer(submitted)
        updated_public = self.public_config_getter(updated)
        revision = config_revision(updated_public)
        return (
            'succeeded', 'config_write_succeeded', {
                'configRevision': revision,
                'config': updated_public,
            })

    def _flush_pending_finish(self, credential):
        pending = self.pending_finish
        if not pending:
            return
        (stored_credential, task_id, lease_token, outcome,
         result_code, result_summary) = pending
        if stored_credential != credential:
            raise LocalAuthError('executor_credential_invalid', status=401)
        self.client.finish(
            credential, task_id, lease_token, outcome,
            result_code, result_summary)
        self.pending_finish = None

    def _wait(self, seconds):
        self.stop_event.wait(max(0.0, float(seconds)))


def pair_executor(pairing_code, display_name=None, client=None,
                  credential_store=None, state_store=None):
    system, architecture = local_platform()
    display_name = str(display_name or socket.gethostname() or '采购电脑').strip()
    client = client or CloudExecutorClient()
    credential_store = credential_store or system_executor_credential_store()
    state_store = state_store or ExecutorChannelStateStore()
    result = client.pair(pairing_code, display_name, system, architecture)
    credential = result['deviceCredential']
    try:
        credential_store.save(credential)
        state_store.save({
            'executorId': result['executorId'],
            'displayName': display_name[:128],
            'platform': system,
            'architecture': architecture,
            'pairedAt': _now_iso(),
            'lastPollAt': None,
            'lastErrorCode': '',
            'status': 'paired',
            'configRevision': None,
        })
    except Exception:
        try:
            credential_store.clear()
        except Exception:
            pass
        raise
    return {key: value for key, value in result.items()
            if key != 'deviceCredential'}


def pair_cli(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m purchase_tool pair',
        description='使用云端生成的一次性配对码绑定本地执行器。')
    parser.add_argument('pairing_code', help='云端显示的 8 位一次性配对码')
    parser.add_argument('--name', dest='display_name',
                        help='这台采购电脑在云端显示的名称')
    args = parser.parse_args(argv)
    try:
        result = pair_executor(args.pairing_code, args.display_name)
    except (LocalAuthError, ValueError) as exc:
        code = getattr(exc, 'code', 'executor_pair_failed')
        print('配对失败：%s（%s）' % (str(exc), code), file=sys.stderr)
        return 1
    print('配对成功：%s' % result['executorId'])
    print('重新启动 Xynigo 本地执行器后，云端会显示在线。')
    return 0
