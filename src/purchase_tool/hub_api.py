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
RUNTIME_FAILURE_TTL_SECONDS = 120.0

# 强制直连不走系统代理：同事电脑开着 Clash 类系统代理时，urllib 默认会把
# 127.0.0.1 的请求也送进代理导致"连接失败"（PowerShell/浏览器则默认绕过本地）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HubApiError(Exception):
    def __init__(self, message, reason_code='hubstudio_local_api_error',
                 api_code=None):
        self.reason_code = str(reason_code)
        self.api_code = None if api_code is None else str(api_code)
        super().__init__(str(message))


def _safe_api_message(value):
    text = str(value or '').replace('\r', ' ').replace('\n', ' ')[:180]
    return re.sub(
        r'(?i)(api[-_ ]?key|authorization|token|secret|cookie)\s*[:=]\s*\S+',
        r'\1=[REDACTED]', text)


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

    def browser_stop(self, container_code):
        raise NotImplementedError

    def env_delete(self, container_codes):
        raise NotImplementedError

    def batch_browser_control(self, action, identifiers, headless=False):
        raise NotImplementedError


class HubStudioLocalApiAdapter(HubStudioAdapter):
    def __init__(self, port=DEFAULT_PORT, timeout=30, retries=3, api_key=None,
                 runtime_gate=None, known_ports=None, opener=None,
                 client_running_getter=None):
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
        self.runtime_gate = runtime_gate or HubRuntimeGate()
        self.opener = opener
        self.client_running_getter = (
            client_running_getter or _default_client_running)
        self.headers = {'Content-Type': 'application/json'}
        self._runtime_failure_lock = threading.Lock()
        self._runtime_failure = None
        if api_key:
            self.headers['local-api-key'] = api_key

    @staticmethod
    def _base_for_port(port):
        return 'http://127.0.0.1:%s/api/v1' % int(port)

    def mark_runtime_failure(self, reason_code, message):
        """Temporarily downgrade a superficially healthy Local API.

        ``group/list`` can remain available after HubStudio loses its browser
        core.  A real browser launch is therefore a stronger runtime signal
        than the read-only heartbeat probe and must win for a short window.
        """
        reason = str(reason_code or '')
        if reason not in {
                'hubstudio_browser_core_missing',
                'hubstudio_browser_launch_invalid'}:
            return False
        with self._runtime_failure_lock:
            self._runtime_failure = {
                'reasonCode': reason,
                'message': _safe_api_message(message),
                'expiresAt': time.monotonic() + RUNTIME_FAILURE_TTL_SECONDS,
            }
        return True

    def clear_runtime_failure(self):
        with self._runtime_failure_lock:
            self._runtime_failure = None

    def _runtime_failure_snapshot(self):
        with self._runtime_failure_lock:
            failure = dict(self._runtime_failure or {})
            if failure and time.monotonic() >= float(
                    failure.get('expiresAt') or 0):
                self._runtime_failure = None
                failure = {}
        if not failure:
            return None
        return {
            'available': False,
            'clientRunning': True,
            'localApiEnabled': True,
            'authenticated': True,
            'apiVersion': 'v1',
            'endpoint': self.base,
            'reasonCode': failure['reasonCode'],
            'message': failure['message'],
        }

    def _post(self, path, body, retries=None, timeout=None):
        """POST JSON；普通异常短退避，限流则触发跨线程共享冷却。"""
        data = json.dumps(body).encode('utf-8')
        last_err = None
        attempts = max(1, int(self.retries if retries is None else retries))
        request_timeout = self.timeout if timeout is None else float(timeout)
        for i in range(attempts):
            deferred_by_gate = False
            retry_delay = 0.4 * (i + 1)
            try:
                req = urllib.request.Request(
                    self.base + path, data=data, headers=self.headers,
                    method='POST')
                with self.runtime_gate.request():
                    with (self.opener or OPENER).open(
                            req, timeout=request_timeout) as resp:
                        raw = resp.read()
                try:
                    j = json.loads(raw.decode('utf-8'))
                except (UnicodeError, ValueError) as exc:
                    raise HubApiError(
                        'HubStudio Local API 返回格式不兼容',
                        'hubstudio_local_api_incompatible') from exc
                if not isinstance(j, dict) or 'code' not in j:
                    raise HubApiError(
                        'HubStudio Local API 返回协议不兼容',
                        'hubstudio_local_api_incompatible')
                if j.get('code') == 0:
                    return j.get('data')
                api_code = str(j.get('code') or '')
                safe_message = _safe_api_message(j.get('msg'))
                browser_core_missing = api_code == '-10007'
                auth_failed = (
                    api_code in {'401', '403', 'E010401', 'E010403'}
                    or any(marker in safe_message.casefold() for marker in (
                        'unauthorized', 'forbidden', 'api key', 'api-key',
                        '鉴权', '认证', '密钥')))
                last_err = HubApiError(
                    'HubStudio 浏览器内核不存在' if browser_core_missing else
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
                      'hubstudio_local_api_error')),
                    api_code=api_code)
                if api_code == RATE_LIMIT_CODE:
                    # HubStudio 的 E010205 是时间窗限流；原 0.4/0.8 秒
                    # 单线程重试会被并行 worker 继续打断。把 2/4/8 秒
                    # 冷却写入共享闸门，所有 Local API 调用一起避让。
                    retry_delay = min(8.0, 2.0 * (2 ** i))
                    defer = getattr(self.runtime_gate,
                                    'defer_requests', None)
                    if callable(defer):
                        defer(retry_delay)
                        deferred_by_gate = True
                elif browser_core_missing:
                    break
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    last_err = HubApiError(
                        'HubStudio Local API 认证失败',
                        'hubstudio_local_api_authentication_failed',
                        api_code=exc.code)
                elif exc.code in (404, 405, 410, 426):
                    last_err = HubApiError(
                        'HubStudio Local API 版本不兼容',
                        'hubstudio_local_api_incompatible',
                        api_code=exc.code)
                else:
                    last_err = HubApiError(
                        'HubStudio Local API HTTP 错误（%s）' % exc.code,
                        'hubstudio_local_api_http_error', api_code=exc.code)
                break
            except urllib.error.URLError as exc:
                reason = getattr(exc, 'reason', None)
                timed_out = isinstance(reason, (socket.timeout, TimeoutError))
                last_err = HubApiError(
                    ('HubStudio Local API 请求超时' if timed_out else
                     '无法连接 HubStudio Local API'),
                    ('hubstudio_local_api_timeout' if timed_out else
                     'hubstudio_local_api_unreachable'))
                break   # 客户端没开，重试无意义
            except (socket.timeout, TimeoutError):
                last_err = HubApiError(
                    'HubStudio Local API 请求超时',
                    'hubstudio_local_api_timeout')
                break
            except HubApiError as exc:
                last_err = exc
                if exc.reason_code in {
                        'hubstudio_local_api_incompatible',
                        'hubstudio_local_api_authentication_failed',
                        'hubstudio_browser_core_missing'}:
                    break
            except Exception:   # 不对外回显未知异常内容
                last_err = HubApiError(
                    'HubStudio Local API 调用异常',
                    'hubstudio_local_api_error')
            if i + 1 < attempts and not deferred_by_gate:
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
            self.port = self.configured_port
            self.base = self._base_for_port(self.port)
            return {
                'available': False,
                'clientRunning': False,
                'localApiEnabled': False,
                'authenticated': False,
                'apiVersion': '',
                'endpoint': self.base,
                'reasonCode': 'hubstudio_client_not_running',
                'message': '未检测到 HubStudio 客户端主窗口',
            }
        runtime_failure = self._runtime_failure_snapshot()
        if runtime_failure is not None:
            return runtime_failure
        failures = []
        probe_ports = [self.port]
        probe_ports.extend(
            port for port in self.candidate_ports if port not in probe_ports)
        for port in probe_ports:
            self.port = int(port)
            self.base = self._base_for_port(self.port)
            try:
                # Startup/capability checks are read-only and deliberately
                # short. Business operations retain their configured timeout
                # and retry policy once the endpoint has been discovered.
                self._post(
                    '/group/list', {'current': 1, 'size': 1},
                    retries=1, timeout=min(2.5, float(self.timeout)))
                return {
                    'available': True,
                    'clientRunning': True,
                    'localApiEnabled': True,
                    'authenticated': True,
                    'apiVersion': 'v1',
                    'endpoint': self.base,
                    'reasonCode': 'ok',
                    'message': 'HubStudio Local API 已就绪',
                }
            except HubApiError as exc:
                failures.append((self.port, exc))
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
                        'endpoint': self.base,
                        'reasonCode': reason,
                        'message': message,
                    }
        self.port = self.configured_port
        self.base = self._base_for_port(self.port)
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
            'endpoint': self.base,
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

    def open_container_codes(self):
        """当前已打开浏览器的 containerCode 集合（字符串）。"""
        data = self._post('/browser/all-browser-status', {}) or {}
        return set(str(c.get('containerCode'))
                   for c in data.get('containers', [])
                   if c.get('containerCode') is not None)

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
            self.mark_runtime_failure(exc.reason_code, str(exc))
            raise
        self.clear_runtime_failure()
        return result

    def browser_stop(self, container_code):
        with self.runtime_gate.browser():
            return self._post('/browser/stop',
                              {'containerCode': str(container_code)}) or {}

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
        return self._post('/env/create', dict(body)) or {}

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
