# -*- coding: utf-8 -*-
"""Production HubStudio adapter backed directly by the loopback Local API.

事实依据（2026-08-18 实测）：
- POST http://127.0.0.1:6873/api/v1/*，响应 {code, msg, data}，code==0 为成功
- group/list：data 直接是 [{tagName, tagCode}] 数组
- env/list：body 传 {"tagNames": [分组名], "current": 1, "size": 200}，
  data.list[] 含 containerName / containerCode / serialNumber / remark 等
- all-browser-status：data.containers[] 为当前已打开环境（containerCode 类型是
  字符串，而 env/list 里是数字——本模块统一转为字符串）
- browser/start：data 含 debuggingPort（字符串）和 ip（出口IP），支持
  isHeadless=true 的无头启动
"""
import errno
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from .task_runtime import HubRuntimeGate

DEFAULT_PORT = 6873
KNOWN_FALLBACK_PORTS = (6873, 6874, 6875)
RATE_LIMIT_CODE = 'E010205'
INSUFFICIENT_RESOURCES_CODE = '-10008'
START_PENDING_CODE = '-10005'
ALREADY_RUNNING_CODE = '-10013'
RUNTIME_FAILURE_TTL_SECONDS = 120.0
DEFAULT_TRANSPORT_ATTEMPTS = 8
BROWSER_LIFECYCLE_STATES = {
    '0': 'open',
    '1': 'opening',
    '2': 'closing',
    '3': 'closed',
}
READ_ONLY_PATHS = frozenset({
    '/group/list',
    '/env/list',
    '/browser/all-browser-status',
    '/env/export-cookie',
})
IDEMPOTENT_MUTATION_PATHS = frozenset({
    '/browser/stop',
    '/env/update',
    '/env/import-cookie',
})
AMBIGUOUS_MUTATION_PATHS = frozenset({
    '/browser/start',
    '/browser/download-core',
    '/env/create',
    '/env/del',
    '/container/add-account',
})
KNOWN_LOCAL_API_PATHS = (
    READ_ONLY_PATHS | IDEMPOTENT_MUTATION_PATHS | AMBIGUOUS_MUTATION_PATHS)
CORE_VERSION_RE = re.compile(r'^[1-9][0-9]{1,2}$')
MISSING_CORE_RE = re.compile(
    r'\b(Chrome|Firefox)\s*\[\s*([0-9]{2,3})\s*\]\s*Core\b', re.I)
MISSING_CORE_VERSION_CN_RE = re.compile(
    r'(?:需(?:要)?使用|请使用).*?[“"‘\[]?\s*([0-9]{2,3})\s*'
    r'[”"’\]]?\s*版本内核')

# 强制直连不走系统代理：同事电脑开着 Clash 类系统代理时，urllib 默认会把
# 127.0.0.1 的请求也送进代理导致"连接失败"（PowerShell/浏览器则默认绕过本地）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HubApiError(Exception):
    def __init__(self, message, reason_code='hubstudio_local_api_error',
                 api_code=None, browser_type='', core_version='',
                 operation='', transport_kind='', outcome_uncertain=False):
        self.reason_code = str(reason_code)
        self.api_code = None if api_code is None else str(api_code)
        self.browser_type = str(browser_type or '').casefold()
        self.core_version = str(core_version or '')
        self.operation = str(operation or '')
        self.transport_kind = str(transport_kind or '')
        self.outcome_uncertain = bool(outcome_uncertain)
        super().__init__(str(message))


def _safe_api_message(value):
    text = str(value or '').replace('\r', ' ').replace('\n', ' ')[:180]
    return re.sub(
        r'(?i)(api[-_ ]?key|authorization|token|secret|cookie)\s*[:=]\s*\S+',
        r'\1=[REDACTED]', text)


def _missing_core_details(message):
    match = MISSING_CORE_RE.search(str(message or ''))
    if match:
        return match.group(1).casefold(), match.group(2)
    # HubStudio 3.58 may return ``code=-1`` with a localized message such as
    # “此环境需使用‘150’版本内核才可打开”, instead of the documented
    # ``-10007 / Chrome[150]Core`` contract.  HubStudio's numbered environment
    # cores are Chromium cores, so keep the repair target actionable while
    # preserving the exact version from the response.
    match = MISSING_CORE_VERSION_CN_RE.search(str(message or ''))
    if match:
        return 'chrome', match.group(1)
    return '', ''


def _windows_hidden_process_kwargs():
    """Prevent diagnostic console tools from flashing a visible window."""
    if os.name != 'nt':
        return {}
    kwargs = {}
    creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    if creation_flags:
        kwargs['creationflags'] = creation_flags
    startup_info_type = getattr(subprocess, 'STARTUPINFO', None)
    if startup_info_type is not None:
        startup_info = startup_info_type()
        startup_info.dwFlags |= getattr(subprocess, 'STARTF_USESHOWWINDOW', 0)
        startup_info.wShowWindow = getattr(subprocess, 'SW_HIDE', 0)
        kwargs['startupinfo'] = startup_info
    return kwargs


def _default_client_running():
    """Best-effort interactive-client check without reading arguments/secrets."""
    try:
        if sys.platform == 'darwin':
            commands = (
                ['/usr/bin/pgrep', '-x', 'Hubstudio'],
                ['/usr/bin/pgrep', '-x', 'HubStudio'],
            )
            return any(subprocess.run(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=2).returncode == 0
                for command in commands)
        if os.name == 'nt':
            # HubStudio may leave several same-named helper processes behind
            # after its desktop window closes.  Process-name-only tasklist
            # detection therefore reports a false online state. PowerShell's
            # MainWindowHandle uses the same signal exposed by Task Manager:
            # at least one HubStudio process must own a real desktop window.
            script = (
                "$window = Get-Process -Name Hubstudio "
                "-ErrorAction SilentlyContinue | "
                "Where-Object { $_.MainWindowHandle -ne 0 } | "
                "Select-Object -First 1; "
                "if ($null -ne $window) { exit 0 }; exit 1"
            )
            result = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive',
                 '-Command', script],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=5,
                **_windows_hidden_process_kwargs())
            return result.returncode == 0
        result = subprocess.run(
            ['pgrep', '-x', 'Hubstudio'], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=2)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class HubStudioAdapter(object):
    """Narrow production contract used by Xynigo business services."""

    def capability_snapshot(self):
        raise NotImplementedError

    def env_list(self, tag_name=None, page_size=200):
        raise NotImplementedError

    def env_lookup(self, container_code=None, container_name=None,
                   tag_name=None):
        raise NotImplementedError

    def list_environment_summaries(self, query='', limit=100):
        raise NotImplementedError

    def locate_environment(self, identifier):
        raise NotImplementedError

    def browser_start(self, container_code, headless=False):
        raise NotImplementedError

    def browser_status(self, container_code=None, timeout=None):
        raise NotImplementedError

    def browser_lifecycle_status(self, container_code, timeout=None):
        raise NotImplementedError

    def browser_stop(self, container_code):
        raise NotImplementedError

    def download_core(self, browser_type, version):
        raise NotImplementedError

    def core_requirement_snapshot(self):
        raise NotImplementedError

    def clear_core_requirement(self):
        raise NotImplementedError

    def env_delete(self, container_codes):
        raise NotImplementedError

    def batch_browser_control(self, action, identifiers, headless=False):
        raise NotImplementedError


class HubStudioLocalApiAdapter(HubStudioAdapter):
    def __init__(self, port=DEFAULT_PORT, timeout=30, retries=3, api_key=None,
                 runtime_gate=None, known_ports=None, opener=None,
                 client_running_getter=None,
                 transport_attempts=DEFAULT_TRANSPORT_ATTEMPTS):
        self.configured_port = int(port)
        candidates = [self.configured_port]
        for candidate in (known_ports or KNOWN_FALLBACK_PORTS):
            candidate = int(candidate)
            if candidate not in candidates:
                candidates.append(candidate)
        self.candidate_ports = tuple(candidates[:4])
        self.port = self.configured_port
        self.base = self._base_for_port(self.port)
        self.timeout = timeout
        self.retries = retries
        self.transport_attempts = max(1, int(transport_attempts))
        self.runtime_gate = runtime_gate or HubRuntimeGate()
        self.opener = opener
        self.client_running_getter = (
            client_running_getter or _default_client_running)
        self.headers = {'Content-Type': 'application/json'}
        self._runtime_failure_lock = threading.Lock()
        self._endpoint_lock = threading.Lock()
        self._transport_recovery_lock = threading.Lock()
        self._runtime_failure = None
        self._core_requirement = None
        if api_key:
            self.headers['local-api-key'] = api_key

    @staticmethod
    def _base_for_port(port):
        return 'http://127.0.0.1:%s/api/v1' % int(port)

    def mark_runtime_failure(self, reason_code, message, browser_type='',
                             core_version='', container_code=''):
        """Temporarily downgrade a superficially healthy Local API.

        ``group/list`` can remain available after HubStudio loses its browser
        core.  A real browser launch is therefore a stronger runtime signal
        than the read-only heartbeat probe and must win for a short window.
        """
        reason = str(reason_code or '')
        if reason not in {
                'hubstudio_browser_core_missing',
                'hubstudio_browser_launch_invalid',
                'hubstudio_system_resources_insufficient'}:
            return False
        parsed_type, parsed_version = _missing_core_details(message)
        browser_type = str(browser_type or parsed_type or '').casefold()
        core_version = str(core_version or parsed_version or '')
        container_code = str(container_code or '')
        with self._runtime_failure_lock:
            previous = dict(self._runtime_failure or {})
            self._runtime_failure = {
                'reasonCode': reason,
                'message': _safe_api_message(message),
                'browserType': browser_type or previous.get('browserType', ''),
                'coreVersion': core_version or previous.get('coreVersion', ''),
                'containerCode': (
                    container_code or previous.get('containerCode', '')),
                'expiresAt': time.monotonic() + RUNTIME_FAILURE_TTL_SECONDS,
            }
            if (reason == 'hubstudio_browser_core_missing'
                    and self._runtime_failure.get('browserType')
                    and self._runtime_failure.get('coreVersion')
                    and self._runtime_failure.get('containerCode')):
                self._core_requirement = {
                    'browserType': self._runtime_failure['browserType'],
                    'coreVersion': self._runtime_failure['coreVersion'],
                    'containerCode': self._runtime_failure['containerCode'],
                }
        return True

    def clear_runtime_failure(self):
        with self._runtime_failure_lock:
            self._runtime_failure = None

    def _runtime_failure_snapshot(self):
        with self._runtime_failure_lock:
            failure = dict(self._runtime_failure or {})
            if (failure and self._core_requirement is None
                    and time.monotonic() >= float(
                        failure.get('expiresAt') or 0)):
                self._runtime_failure = None
                failure = {}
        if not failure:
            return None
        result = {
            'available': False,
            'clientRunning': True,
            'localApiEnabled': True,
            'authenticated': True,
            'apiVersion': 'v1',
            'endpoint': self._base_snapshot(),
            'reasonCode': failure['reasonCode'],
            'message': failure['message'],
        }
        if failure.get('browserType') and failure.get('coreVersion'):
            result['requiredCore'] = {
                'browserType': failure['browserType'],
                'version': failure['coreVersion'],
            }
        return result

    def core_requirement_snapshot(self):
        """Return an internal repair target captured from a real launch error."""
        with self._runtime_failure_lock:
            failure = dict(self._core_requirement or {})
        known = bool(
            failure.get('browserType') in {'chrome', 'firefox'}
            and CORE_VERSION_RE.fullmatch(
                str(failure.get('coreVersion') or ''))
            and str(failure.get('containerCode') or ''))
        return {
            'known': known,
            'browserType': str(failure.get('browserType') or ''),
            'version': str(failure.get('coreVersion') or ''),
            'containerCode': (
                str(failure.get('containerCode') or '') if known else ''),
        }

    def clear_core_requirement(self):
        with self._runtime_failure_lock:
            self._core_requirement = None

    def _base_snapshot(self):
        with self._endpoint_lock:
            return self.base

    def _activate_port(self, port):
        with self._endpoint_lock:
            self.port = int(port)
            self.base = self._base_for_port(self.port)

    @staticmethod
    def _transport_error(path, exc):
        reason = getattr(exc, 'reason', exc)
        timed_out = isinstance(reason, (socket.timeout, TimeoutError))
        number = getattr(reason, 'errno', None)
        definitely_not_sent = (
            isinstance(reason, ConnectionRefusedError)
            or number in {
                errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH,
            })
        kind = (
            'timeout' if timed_out else
            'connection_refused' if definitely_not_sent else
            'connection_reset' if isinstance(reason, ConnectionResetError)
            else 'unreachable')
        return HubApiError(
            ('HubStudio Local API 请求超时' if timed_out else
             '无法连接 HubStudio Local API'),
            ('hubstudio_local_api_timeout' if timed_out else
             'hubstudio_local_api_unreachable'),
            operation=path, transport_kind=kind,
            outcome_uncertain=not definitely_not_sent)

    def _record_transport_failure(self):
        reporter = getattr(self.runtime_gate, 'record_transport_failure', None)
        if callable(reporter):
            return reporter()
        defer = getattr(self.runtime_gate, 'defer_requests', None)
        if callable(defer):
            defer(1.0)
        return {'consecutiveFailures': 1, 'delaySeconds': 1.0}

    def _record_transport_success(self):
        reporter = getattr(self.runtime_gate, 'record_transport_success', None)
        if callable(reporter):
            reporter()

    def _transport_is_retriable(self, path, error):
        # Reads and state-replacement writes are safe to repeat even if the
        # previous response was lost.  Non-idempotent writes are only repeated
        # when TCP was definitely never established; otherwise their caller
        # must reconcile the external state before deciding what to do.
        return (
            path in READ_ONLY_PATHS
            or path in IDEMPOTENT_MUTATION_PATHS
            or not error.outcome_uncertain)

    def _client_is_running(self):
        try:
            return bool(self.client_running_getter())
        except Exception:
            # A failure in the diagnostic command is not evidence that the
            # interactive client exited.  Preserve the recovery path.
            return True

    def _probe_ports_for_recovery(self, request_timeout):
        current_base = self._base_snapshot()
        current_port = int(current_base.split(':')[2].split('/')[0])
        ports = [current_port]
        ports.extend(port for port in self.candidate_ports if port not in ports)
        for port in ports:
            try:
                self._post(
                    '/group/list', {'current': 1, 'size': 1},
                    retries=1, timeout=min(2.5, request_timeout),
                    transport_retries=1, recover_transport=False,
                    base_url=self._base_for_port(port),
                    record_transport=False)
            except HubApiError:
                continue
            self._activate_port(port)
            self._record_transport_success()
            return True
        return False

    def _recover_transport(self, request_timeout):
        if not self._client_is_running():
            raise HubApiError(
                '未检测到 HubStudio 客户端运行',
                'hubstudio_client_not_running')
        with self._transport_recovery_lock:
            snapshotter = getattr(
                self.runtime_gate, 'transport_snapshot', None)
            if callable(snapshotter):
                snapshot = snapshotter()
                if int(snapshot.get('consecutiveFailures') or 0) == 0:
                    return True
            return self._probe_ports_for_recovery(request_timeout)

    def _post(self, path, body, retries=None, timeout=None,
              transport_retries=None, recover_transport=True,
              base_url=None, record_transport=True):
        """POST JSON with one process-wide transport circuit.

        Business errors retain the short retry policy.  Loopback transport
        failures use a shared escalating cooldown so all modules stop
        pressuring HubStudio together.  Ambiguous non-idempotent writes are
        never blindly replayed; their domain orchestrator must reconcile the
        external state first.
        """
        data = json.dumps(body).encode('utf-8')
        last_err = None
        api_attempts = max(
            1, int(self.retries if retries is None else retries))
        if transport_retries is None:
            transport_attempts = (
                api_attempts if retries is not None
                else self.transport_attempts)
        else:
            transport_attempts = max(1, int(transport_retries))
        request_timeout = self.timeout if timeout is None else float(timeout)
        api_index = 0
        transport_index = 0
        while True:
            deferred_by_gate = False
            retry_delay = 0.4 * (api_index + 1)
            try:
                request_base = base_url or self._base_snapshot()
                req = urllib.request.Request(
                    request_base + path, data=data, headers=self.headers,
                    method='POST')
                with self.runtime_gate.request():
                    with (self.opener or OPENER).open(
                            req, timeout=request_timeout) as resp:
                        raw = resp.read()
                if record_transport:
                    self._record_transport_success()
                try:
                    j = json.loads(raw.decode('utf-8'))
                except (UnicodeError, ValueError) as exc:
                    raise HubApiError(
                        'HubStudio Local API 返回格式不兼容',
                        'hubstudio_local_api_incompatible',
                        operation=path) from exc
                if not isinstance(j, dict) or 'code' not in j:
                    raise HubApiError(
                        'HubStudio Local API 返回协议不兼容',
                        'hubstudio_local_api_incompatible', operation=path)
                if j.get('code') == 0:
                    return j.get('data')
                api_index += 1
                api_code = str(j.get('code') or '')
                safe_message = _safe_api_message(j.get('msg'))
                core_browser_type, core_version = _missing_core_details(
                    safe_message)
                browser_core_missing = (
                    api_code == '-10007'
                    or bool(core_browser_type and core_version))
                auth_failed = (
                    api_code in {'401', '403', 'E010401', 'E010403'}
                    or any(marker in safe_message.casefold() for marker in (
                        'unauthorized', 'forbidden', 'api key', 'api-key',
                        '鉴权', '认证', '密钥')))
                last_err = HubApiError(
                    ('HubStudio %s %s 浏览器内核不存在' % (
                        (core_browser_type or 'browser').capitalize(),
                        core_version or '未知版本'))
                    if browser_core_missing else
                    'HubStudio Local API 认证失败' if auth_failed else
                    ('HubStudio Local API 返回 code=%s%s' % (
                        api_code,
                        (': ' + safe_message) if safe_message else '')),
                    ('hubstudio_browser_core_missing'
                     if browser_core_missing else
                     'hubstudio_local_api_authentication_failed'
                     if auth_failed else
                     ('hubstudio_local_api_rate_limited'
                      if api_code == RATE_LIMIT_CODE else
                      'hubstudio_browser_start_pending'
                      if api_code == START_PENDING_CODE else
                      'hubstudio_browser_already_running'
                      if api_code == ALREADY_RUNNING_CODE else
                      'hubstudio_system_resources_insufficient'
                      if api_code == INSUFFICIENT_RESOURCES_CODE else
                      'hubstudio_local_api_error')),
                    api_code=api_code, operation=path,
                    browser_type=core_browser_type,
                    core_version=core_version)
                if api_code == RATE_LIMIT_CODE:
                    retry_delay = min(8.0, 2.0 * (2 ** (api_index - 1)))
                    defer = getattr(self.runtime_gate,
                                    'defer_requests', None)
                    if callable(defer):
                        defer(retry_delay)
                        deferred_by_gate = True
                elif api_code in {
                        START_PENDING_CODE,
                        ALREADY_RUNNING_CODE,
                        INSUFFICIENT_RESOURCES_CODE}:
                    # Browser capacity requires lifecycle-aware recovery in
                    # the caller.  In particular, -10005 explicitly means a
                    # previous startBrowser is still running; replaying it as
                    # a generic business error only grows HubStudio's queue.
                    break
                elif browser_core_missing:
                    break
            except urllib.error.HTTPError as exc:
                if record_transport:
                    self._record_transport_success()
                if exc.code in (401, 403):
                    last_err = HubApiError(
                        'HubStudio Local API 认证失败',
                        'hubstudio_local_api_authentication_failed',
                        api_code=exc.code, operation=path)
                elif exc.code in (404, 405, 410, 426):
                    last_err = HubApiError(
                        'HubStudio Local API 版本不兼容',
                        'hubstudio_local_api_incompatible',
                        api_code=exc.code, operation=path)
                else:
                    last_err = HubApiError(
                        'HubStudio Local API HTTP 错误（%s）' % exc.code,
                        'hubstudio_local_api_http_error', api_code=exc.code,
                        operation=path)
                break
            except urllib.error.URLError as exc:
                last_err = self._transport_error(path, exc)
                transport_index += 1
                if record_transport:
                    self._record_transport_failure()
                if (not recover_transport
                        or transport_index >= transport_attempts
                        or not self._transport_is_retriable(path, last_err)):
                    break
                try:
                    self._recover_transport(request_timeout)
                except HubApiError as recovery_error:
                    if recovery_error.reason_code == \
                            'hubstudio_client_not_running':
                        last_err = recovery_error
                        break
                continue
            except (socket.timeout, TimeoutError) as exc:
                last_err = self._transport_error(path, exc)
                transport_index += 1
                if record_transport:
                    self._record_transport_failure()
                if (not recover_transport
                        or transport_index >= transport_attempts
                        or not self._transport_is_retriable(path, last_err)):
                    break
                try:
                    self._recover_transport(request_timeout)
                except HubApiError as recovery_error:
                    if recovery_error.reason_code == \
                            'hubstudio_client_not_running':
                        last_err = recovery_error
                        break
                continue
            except HubApiError as exc:
                last_err = exc
                api_index += 1
                if exc.reason_code in {
                        'hubstudio_local_api_incompatible',
                        'hubstudio_local_api_authentication_failed',
                        'hubstudio_browser_core_missing'}:
                    break
            except Exception:   # 不对外回显未知异常内容
                last_err = HubApiError(
                    'HubStudio Local API 调用异常',
                    'hubstudio_local_api_error', operation=path)
                api_index += 1
            if api_index >= api_attempts:
                break
            if not deferred_by_gate:
                time.sleep(retry_delay)
        raise last_err

    # ---- 查询类 ----

    def capability_snapshot(self):
        """Return a non-sensitive, reason-coded Local API capability view."""
        # The interactive desktop client is the authoritative liveness signal.
        # HubStudio can leave helper processes and a loopback listener behind
        # after its main window exits; accepting those first makes the cloud
        # show a stale green state. Keep the OS-local check ahead of API probes.
        try:
            client_running = bool(self.client_running_getter())
        except Exception:
            client_running = False
        if not client_running:
            self.clear_runtime_failure()
            self._activate_port(self.configured_port)
            return {
                'available': False,
                'clientRunning': False,
                'localApiEnabled': False,
                'authenticated': False,
                'apiVersion': '',
                'endpoint': self._base_snapshot(),
                'reasonCode': 'hubstudio_client_not_running',
                'message': '未检测到 HubStudio 客户端主窗口',
            }
        runtime_failure = self._runtime_failure_snapshot()
        if runtime_failure is not None:
            return runtime_failure
        failures = []
        current_base = self._base_snapshot()
        current_port = int(current_base.split(':')[2].split('/')[0])
        probe_ports = [current_port]
        probe_ports.extend(
            port for port in self.candidate_ports if port not in probe_ports)
        for port in probe_ports:
            probe_base = self._base_for_port(port)
            try:
                # Startup/capability checks are read-only and deliberately
                # short. Business operations retain their configured timeout
                # and retry policy once the endpoint has been discovered.
                self._post(
                    '/group/list', {'current': 1, 'size': 1},
                    retries=1, timeout=min(2.5, float(self.timeout)),
                    transport_retries=1, recover_transport=False,
                    base_url=probe_base, record_transport=False)
                self._activate_port(port)
                self._record_transport_success()
                return {
                    'available': True,
                    'clientRunning': True,
                    'localApiEnabled': True,
                    'authenticated': True,
                    'apiVersion': 'v1',
                    'endpoint': probe_base,
                    'reasonCode': 'ok',
                    'message': 'HubStudio Local API 已就绪',
                }
            except HubApiError as exc:
                failures.append((port, exc))
                if exc.reason_code not in {
                        'hubstudio_local_api_unreachable',
                        'hubstudio_local_api_timeout'}:
                    reason = exc.reason_code
                    if (reason == 'hubstudio_local_api_authentication_failed'
                            and 'local-api-key' not in self.headers):
                        reason = 'hubstudio_local_api_authentication_required'
                    if reason.endswith('_required'):
                        message = 'HubStudio Local API 需要本机安全密钥'
                    elif 'authentication' in reason:
                        message = 'HubStudio Local API 认证失败'
                    elif reason == 'hubstudio_local_api_incompatible':
                        message = 'HubStudio Local API 版本不兼容'
                    elif reason == 'hubstudio_local_api_http_error':
                        message = 'HubStudio Local API 返回 HTTP 错误'
                    elif reason == 'hubstudio_local_api_rate_limited':
                        message = 'HubStudio Local API 请求频率受限'
                    elif reason == 'hubstudio_system_resources_insufficient':
                        message = 'HubStudio 浏览器资源不足，正在等待恢复'
                    else:
                        message = 'HubStudio Local API 返回业务错误'
                    return {
                        'available': False,
                        'clientRunning': True,
                        'localApiEnabled': True,
                        'authenticated': not (
                            reason.endswith('_required')
                            or 'authentication' in reason),
                        'apiVersion': 'v1',
                        'endpoint': probe_base,
                        'reasonCode': reason,
                        'message': message,
                    }
        self._activate_port(self.configured_port)
        timeout_seen = any(
            error.reason_code == 'hubstudio_local_api_timeout'
            for _port, error in failures)
        if not client_running:
            reason = 'hubstudio_client_not_running'
            message = '未检测到 HubStudio 客户端运行'
        elif timeout_seen:
            reason = 'hubstudio_local_api_timeout'
            message = 'HubStudio Local API 请求超时'
        else:
            reason = 'hubstudio_local_api_disabled'
            message = 'HubStudio 已运行，但 Local API 未开启或端口不可达'
        return {
            'available': False,
            'clientRunning': client_running,
            'localApiEnabled': False,
            'authenticated': False,
            'apiVersion': '',
            'endpoint': self._base_snapshot(),
            'reasonCode': reason,
            'message': message,
        }

    def ping(self):
        """探测 HubStudio 客户端是否在线（轻量调用）。"""
        return bool(self.capability_snapshot()['available'])

    def ping_detail(self):
        """探测并返回失败原因（前端展示用，便于远程排查）。"""
        snapshot = self.capability_snapshot()
        return (
            bool(snapshot['available']),
            '' if snapshot['available'] else str(snapshot['message'])[:300])

    def group_list(self):
        data = self._post('/group/list', {'current': 1, 'size': 100})
        return [g.get('tagName') for g in (data or []) if g.get('tagName')]

    def env_list(self, tag_name=None, page_size=200):
        """取环境列表；带分组名则按分组过滤，自动翻页。"""
        page_size = max(1, min(200, int(page_size)))
        result, current = [], 1
        while True:
            body = {'current': current, 'size': page_size}
            if tag_name:
                body['tagNames'] = [tag_name]
            data = self._post('/env/list', body) or {}
            page = data.get('list', [])
            result.extend(page)
            total = data.get('total', 0)
            if current * page_size >= total or not page:
                break
            current += 1
        return result

    def env_lookup(self, container_code=None, container_name=None,
                   tag_name=None):
        """按环境 ID 或完整环境名定向查询，避免为单条回读全量翻页。"""
        wanted_code = str(container_code or '').strip()
        wanted_name = str(container_name or '').strip()
        if bool(wanted_code) == bool(wanted_name):
            raise HubApiError(
                '环境定向查询必须且只能指定环境 ID 或环境名',
                'hubstudio_environment_identifier_invalid')
        if len(wanted_code) > 160 or len(wanted_name) > 160:
            raise HubApiError(
                '环境定向查询条件无效',
                'hubstudio_environment_identifier_invalid')

        body = {'current': 1, 'size': 2}
        if wanted_code:
            body['containerCodes'] = [wanted_code]
        else:
            body['containerName'] = wanted_name
        if tag_name:
            body['tagNames'] = [str(tag_name)]
        data = self._post('/env/list', body) or {}
        matched = []
        for env in data.get('list', []):
            if wanted_code:
                exact = str(env.get('containerCode') or '') == wanted_code
            else:
                exact = str(env.get('containerName') or '') == wanted_name
            if exact and (not tag_name or
                          str(env.get('tagName') or '') == str(tag_name)):
                matched.append(env)
        unique = {}
        for env in matched:
            identity = str(env.get('containerCode') or '').strip()
            if not identity:
                identity = 'name:' + str(env.get('containerName') or '')
            unique[identity] = env
        if not unique:
            return None
        if len(unique) != 1:
            raise HubApiError(
                '环境定向查询匹配到多个环境',
                'hubstudio_environment_ambiguous')
        return next(iter(unique.values()))

    def browser_status(self, container_code=None, timeout=None):
        """读取本机浏览器状态；可按 containerCode 做启动结果对账。"""
        body = {}
        if container_code is not None:
            body['containerCodes'] = [str(container_code)]
        data = self._post(
            '/browser/all-browser-status', body,
            retries=1 if timeout is not None else None,
            timeout=timeout) or {}
        return [
            dict(item) for item in data.get('containers', [])
            if isinstance(item, dict)
        ]

    @staticmethod
    def browser_lifecycle_state(item):
        """Normalize HubStudio's documented 0/1/2/3 browser state.

        The status endpoint normally returns only ``containerCode`` and
        ``status``.  Older clients/test doubles sometimes omit ``status`` and
        include a debugging port instead, so keep that narrow compatibility
        fallback without mistaking an explicit closed state for an open one.
        """
        item = item if isinstance(item, dict) else {}
        raw_status = item.get('status')
        normalized = BROWSER_LIFECYCLE_STATES.get(str(raw_status))
        if normalized:
            return normalized
        try:
            debugging_port = int(item.get('debuggingPort') or 0)
        except (TypeError, ValueError):
            debugging_port = 0
        return 'open' if debugging_port > 0 else 'unknown'

    def browser_lifecycle_status(self, container_code, timeout=None):
        """Return one exact environment's normalized lifecycle status."""
        wanted = str(container_code)
        for item in self.browser_status(wanted, timeout=timeout):
            if str(item.get('containerCode') or '') != wanted:
                continue
            return {
                'containerCode': wanted,
                'state': self.browser_lifecycle_state(item),
                'data': dict(item),
            }
        return {
            'containerCode': wanted,
            'state': 'absent',
            'data': {},
        }

    def open_container_codes(self):
        """Return active/opening/closing browser codes, excluding closed."""
        return set(
            str(item.get('containerCode'))
            for item in self.browser_status()
            if (item.get('containerCode') is not None
                and self.browser_lifecycle_state(item) != 'closed'))

    def env_by_serial(self, serial_number, tag_name=None):
        wanted = str(serial_number)
        for env in self.env_list(tag_name):
            if str(env.get('serialNumber')) == wanted:
                return env
        return None

    @staticmethod
    def environment_summary(env):
        """Return only fields safe for localhost plugin environment controls."""
        env = env if isinstance(env, dict) else {}
        return {
            'containerCode': str(env.get('containerCode') or ''),
            'serialNumber': str(env.get('serialNumber') or ''),
            'containerName': str(env.get('containerName') or '')[:160],
            'tagName': str(env.get('tagName') or '')[:80],
        }

    def list_environment_summaries(self, query='', limit=100):
        wanted = str(query or '').strip().casefold()
        maximum = max(1, min(200, int(limit)))
        result = []
        for env in self.env_list():
            summary = self.environment_summary(env)
            values = [str(value).casefold() for value in summary.values()]
            if wanted and not any(wanted in value for value in values):
                continue
            result.append(summary)
            if len(result) >= maximum:
                break
        return result

    def locate_environment(self, identifier):
        wanted = str(identifier or '').strip()
        if not wanted or len(wanted) > 160:
            raise HubApiError(
                '请提供有效的环境序号或 containerCode',
                'hubstudio_environment_identifier_invalid')
        matched = []
        for env in self.env_list():
            if (str(env.get('serialNumber') or '') == wanted
                    or str(env.get('containerCode') or '') == wanted):
                matched.append(env)
        unique = {
            str(env.get('containerCode') or ''): env for env in matched
            if env.get('containerCode') is not None
        }
        if not unique:
            raise HubApiError(
                '未找到对应的 HubStudio 环境',
                'hubstudio_environment_not_found')
        if len(unique) != 1:
            raise HubApiError(
                '环境序号与 containerCode 匹配到多个环境',
                'hubstudio_environment_ambiguous')
        return next(iter(unique.values()))

    # ---- 浏览器控制 ----

    def browser_start(self, container_code, headless=False):
        """启动环境浏览器，返回 data（含 debuggingPort / ip）。

        ``headless`` 只供不需要人工交互的只读检测使用。订单查询和账号
        注册仍走默认可见模式，避免改变现有人工接管流程。
        """
        body = {'containerCode': str(container_code)}
        if headless:
            body.update({
                'isHeadless': True,
                'isWebDriverReadOnlyMode': True,
                # HubStudio 官方兼容建议：部分内核仅传 isHeadless 时
                # 仍可能无法按无头模式连接。
                'args': ['--headless=new'],
            })
        # 只串行提交 start/stop 控制 RPC，不持有整个浏览器会话；不同环境
        # 仍可同时运行，避免 HubStudio 同时收到 start 时返回 -10005。
        try:
            with self.runtime_gate.browser():
                result = self._post('/browser/start', body) or {}
        except HubApiError as exc:
            if exc.reason_code == 'hubstudio_local_api_timeout':
                # A lost response does not mean HubStudio cancelled the
                # launch.  Reconcile the exact container before any caller is
                # allowed to submit a duplicate browser/start request.
                try:
                    lifecycle = self.browser_lifecycle_status(
                        container_code, timeout=min(5.0, float(self.timeout)))
                except HubApiError:
                    lifecycle = {'state': 'unknown', 'data': {}}
                matched = None
                item = lifecycle.get('data') or {}
                if lifecycle.get('state') == 'open':
                    try:
                        debugging_port = int(item.get('debuggingPort') or 0)
                    except (TypeError, ValueError):
                        debugging_port = 0
                    if debugging_port > 0:
                        matched = item
                if matched is not None:
                    self.clear_runtime_failure()
                    return matched
            self.mark_runtime_failure(
                exc.reason_code, str(exc),
                browser_type=exc.browser_type,
                core_version=exc.core_version,
                container_code=container_code)
            raise
        with self._runtime_failure_lock:
            repair_pending = self._core_requirement is not None
        if not repair_pending:
            self.clear_runtime_failure()
        return result

    def browser_stop(self, container_code):
        with self.runtime_gate.browser():
            return self._post('/browser/stop',
                              {'containerCode': str(container_code)}) or {}

    def download_core(self, browser_type, version):
        """Ask HubStudio itself to download one explicitly identified core."""
        normalized_type = str(browser_type or '').strip().casefold()
        normalized_version = str(version or '').strip()
        browser_code = {'chrome': 1, 'firefox': 2}.get(normalized_type)
        if browser_code is None or not CORE_VERSION_RE.fullmatch(
                normalized_version):
            raise HubApiError(
                'HubStudio 内核下载参数无效',
                'hubstudio_core_download_target_invalid')
        body = {'Cores': [{
            'BrowserType': browser_code,
            'Version': normalized_version,
        }]}
        try:
            with self.runtime_gate.browser():
                return self._post(
                    '/browser/download-core', body,
                    retries=1, timeout=max(1200.0, float(self.timeout)))
        except HubApiError as exc:
            if exc.reason_code != 'hubstudio_local_api_timeout':
                raise
            # The download may continue inside HubStudio after its response is
            # lost.  Returning an explicit uncertain acceptance lets the core
            # repair coordinator verify the requested version instead of
            # submitting a duplicate long-running download.
            return {
                'accepted': True,
                'responseLost': True,
            }

    def env_delete(self, container_codes):
        """Delete explicitly identified environments through the Local API.

        HubStudio documents ``/env/del`` as accepting at most 1000 numeric
        container codes.  Keep this adapter deliberately narrower: callers
        must already have proved ownership and may delete at most one batch of
        explicit IDs.  Names, serial numbers and empty identifiers are never
        accepted as deletion targets.
        """
        if not isinstance(container_codes, (list, tuple)) or not container_codes:
            raise HubApiError(
                '删除环境必须指定 containerCode',
                'hubstudio_environment_delete_targets_invalid')
        if len(container_codes) > 1000:
            raise HubApiError(
                '单次最多删除 1000 个 HubStudio 环境',
                'hubstudio_environment_delete_targets_exceeded')
        normalized = []
        for value in container_codes:
            text = str(value or '').strip()
            if not text.isdigit():
                raise HubApiError(
                    '删除环境的 containerCode 无效',
                    'hubstudio_environment_delete_targets_invalid')
            number = int(text)
            if number < 1:
                raise HubApiError(
                    '删除环境的 containerCode 无效',
                    'hubstudio_environment_delete_targets_invalid')
            normalized.append(number)
        if len(normalized) != len(set(normalized)):
            raise HubApiError(
                '删除环境目标包含重复 containerCode',
                'hubstudio_environment_delete_targets_invalid')
        result = self._post('/env/del', {'containerCodes': normalized})
        if result is not True:
            raise HubApiError(
                'HubStudio 未确认环境删除成功',
                'hubstudio_environment_delete_unconfirmed')
        return True

    def batch_browser_control(self, action, identifiers, headless=False):
        """Open or close at most 20 explicitly identified environments."""
        action = str(action or '').strip().lower()
        if action not in {'open', 'close'}:
            raise HubApiError(
                '批量操作只支持 open 或 close',
                'hubstudio_batch_action_invalid')
        if not isinstance(identifiers, list) or not identifiers:
            raise HubApiError(
                '批量操作必须指定环境',
                'hubstudio_batch_targets_invalid')
        if len(identifiers) > 20:
            raise HubApiError(
                '单次最多处理 20 个 HubStudio 环境',
                'hubstudio_batch_targets_exceeded')
        results = []
        for identifier in identifiers:
            safe_identifier = str(identifier or '').strip()[:160]
            try:
                env = self.locate_environment(safe_identifier)
                code = str(env.get('containerCode') or '')
                if action == 'open':
                    self.browser_start(code, headless=bool(headless))
                else:
                    self.browser_stop(code)
                results.append({
                    'identifier': safe_identifier,
                    'containerCode': code,
                    'ok': True,
                    'reasonCode': 'ok',
                })
            except HubApiError as exc:
                results.append({
                    'identifier': safe_identifier,
                    'containerCode': '',
                    'ok': False,
                    'reasonCode': exc.reason_code,
                })
        return results

    # ---- 注册模块写操作（上层必须显式 --apply） ----

    def env_create(self, body):
        body = dict(body)
        try:
            return self._post('/env/create', body) or {}
        except HubApiError as exc:
            if (exc.reason_code not in {
                    'hubstudio_local_api_timeout',
                    'hubstudio_local_api_unreachable'}
                    or not exc.outcome_uncertain):
                raise
            # env/create may commit before its HTTP response is lost.  Exact
            # name+group lookup turns that ambiguity into an idempotent result
            # for every caller (batch creation, registration and backup envs).
            name = str(body.get('containerName') or '').strip()
            tag = str(body.get('tagName') or '').strip()
            if not name:
                raise
            existing = self.env_lookup(
                container_name=name, tag_name=tag or None)
            if existing is None:
                raise
            return {
                'containerCode': str(existing.get('containerCode') or ''),
                'serialNumber': existing.get('serialNumber'),
                'reconciledAfterTransportFailure': True,
            }

    def env_update(self, container_code, container_name, remark=None):
        body = {'containerCode': str(container_code),
                'containerName': container_name}
        if remark is not None:
            body['remark'] = remark
        return self._post('/env/update', body) or {}

    def container_add_account(self, container_code, email, password,
                              site='MX'):
        site = str(site or 'MX').strip().upper()
        if site not in ('MX', 'US'):
            raise ValueError('绑号站点仅支持 MX 或 US')
        is_mx = site == 'MX'
        body = {
            'containerCode': str(container_code),
            'siteName': '自定义平台',
            'siteAlias': '希音墨西哥站' if is_mx else '希音美国站',
            'domainName': ('https://www.shein.com.mx' if is_mx
                           else 'https://us.shein.com'),
            'accountName': email,
            'accountPassword': password,
            'name': email.split('@')[0].lower(),
        }
        return self._post('/container/add-account', body) or {}

    def env_export_cookie(self, container_code):
        """导出标准 cookie 数组；返回值是敏感数据，禁止打印日志。"""
        return self._post('/env/export-cookie',
                          {'containerCode': str(container_code)})

    def env_import_cookie(self, container_code, cookie_text):
        """导入 Cookie 原文；cookie 必须保持为序列化 JSON 字符串。"""
        if not isinstance(cookie_text, str):
            raise TypeError('cookie_text 必须是序列化 JSON 字符串')
        return self._post('/env/import-cookie', {
            'containerCode': str(container_code),
            'cookie': cookie_text,
        }) or {}


# Backward-compatible name used throughout existing business modules.  It is
# now explicitly the Local API production adapter, not a CLI wrapper.
HubStudioApi = HubStudioLocalApiAdapter
