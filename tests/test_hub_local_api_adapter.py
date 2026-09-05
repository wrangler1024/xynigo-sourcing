# -*- coding: utf-8 -*-
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest import mock
import unittest
from urllib.error import URLError

from purchase_tool import hub_api
from purchase_tool.hub_api import HubApiError, HubStudioLocalApiAdapter
from purchase_tool.hub_api_key import (
    HubApiKeyStoreError, MemoryHubApiKeyStore, SystemHubApiKeyStore, _wrap_key)
from purchase_tool.main import AppState
from purchase_tool.task_runtime import HubRuntimeGate


class FakeResponse(object):
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode('utf-8')


class FakeLocalApiOpener(object):
    def __init__(self, unavailable_ports=None, response_code=0,
                 response_message='Success', raw_payload=None,
                 open_browsers=None):
        self.unavailable_ports = set(unavailable_ports or [])
        self.response_code = response_code
        self.response_message = response_message
        self.raw_payload = raw_payload
        self.open_browsers = list(open_browsers or [])
        self.calls = []
        self.timeouts = []
        self.environments = [{
            'containerCode': 'container-test-1',
            'serialNumber': 4254,
            'containerName': '脱敏测试环境',
            'tagName': '测试分组',
            'remark': '不得返回到插件',
        }]

    def open(self, request, timeout=None):
        self.timeouts.append(timeout)
        url = request.full_url
        port = int(url.split(':')[2].split('/')[0])
        body = json.loads((request.data or b'{}').decode('utf-8'))
        path = '/' + url.split('/api/v1/', 1)[1]
        self.calls.append((port, path, body, dict(request.headers)))
        if port in self.unavailable_ports:
            raise URLError(ConnectionRefusedError('refused'))
        if self.raw_payload is not None:
            return FakeResponse(self.raw_payload)
        if self.response_code != 0:
            return FakeResponse({
                'code': self.response_code,
                'msg': self.response_message,
                'data': None,
            })
        if path == '/group/list':
            data = [{'tagName': '测试分组', 'tagCode': 'tag-test'}]
        elif path == '/env/list':
            data = {'list': list(self.environments), 'total': 1}
        elif path == '/browser/all-browser-status':
            wanted = {
                str(value) for value in body.get('containerCodes') or []}
            containers = list(self.open_browsers)
            if wanted:
                containers = [
                    item for item in containers
                    if str(item.get('containerCode') or '') in wanted]
            data = {'containers': containers}
        elif path in {'/browser/start', '/browser/stop'}:
            data = {'accepted': True}
        elif path == '/env/del':
            wanted = {str(value) for value in body.get('containerCodes') or []}
            self.environments = [
                env for env in self.environments
                if str(env.get('containerCode') or '') not in wanted
            ]
            data = True
        else:
            data = {}
        return FakeResponse({'code': 0, 'msg': 'Success', 'data': data})


class MemoryTokenBackend(object):
    def __init__(self):
        self.value = None

    def load(self):
        return self.value

    def save(self, value):
        self.value = value

    def clear(self):
        self.value = None


class HubStudioLocalApiAdapterTests(unittest.TestCase):
    @staticmethod
    def recovery_gate():
        return HubRuntimeGate(
            max_requests=1, min_request_interval=0,
            sleep_fn=lambda _seconds: None,
            transport_backoff=(0,))

    def test_every_supported_local_api_endpoint_has_a_retry_policy(self):
        self.assertEqual(hub_api.KNOWN_LOCAL_API_PATHS, {
            '/group/list', '/env/list', '/browser/all-browser-status',
            '/env/export-cookie', '/browser/stop', '/env/update',
            '/env/import-cookie', '/browser/start',
            '/browser/download-core', '/env/create', '/env/del',
            '/container/add-account',
        })
        self.assertFalse(
            hub_api.READ_ONLY_PATHS & hub_api.IDEMPOTENT_MUTATION_PATHS)
        self.assertFalse(
            hub_api.READ_ONLY_PATHS & hub_api.AMBIGUOUS_MUTATION_PATHS)
        self.assertFalse(
            hub_api.IDEMPOTENT_MUTATION_PATHS &
            hub_api.AMBIGUOUS_MUTATION_PATHS)

    def test_read_recovers_after_shared_transport_circuit_probe(self):
        class FlakyOpener(FakeLocalApiOpener):
            def __init__(self):
                super().__init__()
                self.env_attempts = 0

            def open(self, request, timeout=None):
                path = '/' + request.full_url.split('/api/v1/', 1)[1]
                if path == '/env/list':
                    self.env_attempts += 1
                    if self.env_attempts == 1:
                        raise URLError(ConnectionRefusedError('refused'))
                return super().open(request, timeout=timeout)

        opener = FlakyOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            runtime_gate=self.recovery_gate(), transport_attempts=3)

        rows = adapter.env_list()

        self.assertEqual(len(rows), 1)
        self.assertEqual(opener.env_attempts, 2)
        self.assertIn('/group/list', [call[1] for call in opener.calls])

    def test_business_call_rediscovers_fallback_port_after_listener_moves(self):
        opener = FakeLocalApiOpener(unavailable_ports={7000})
        adapter = HubStudioLocalApiAdapter(
            port=7000, known_ports=(6873,), opener=opener,
            client_running_getter=lambda: True,
            runtime_gate=self.recovery_gate(), transport_attempts=3)

        rows = adapter.env_list()

        self.assertEqual(len(rows), 1)
        self.assertEqual(adapter.port, 6873)
        self.assertEqual(adapter._base_snapshot(),
                         'http://127.0.0.1:6873/api/v1')
        env_ports = [
            port for port, path, _body, _headers in opener.calls
            if path == '/env/list']
        self.assertEqual(env_ports[-1], 6873)

    def test_idempotent_write_recovers_after_timeout(self):
        class FlakyOpener(FakeLocalApiOpener):
            def __init__(self):
                super().__init__()
                self.update_attempts = 0

            def open(self, request, timeout=None):
                path = '/' + request.full_url.split('/api/v1/', 1)[1]
                if path == '/env/update':
                    self.update_attempts += 1
                    if self.update_attempts == 1:
                        raise URLError(socket.timeout('timed out'))
                return super().open(request, timeout=timeout)

        opener = FlakyOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            runtime_gate=self.recovery_gate(), transport_attempts=3)

        adapter.env_update('container-test-1', '脱敏测试环境', '已完成')

        self.assertEqual(opener.update_attempts, 2)

    def test_non_idempotent_create_timeout_is_reconciled_not_replayed(self):
        class TimedOutCreateOpener(FakeLocalApiOpener):
            def __init__(self):
                super().__init__()
                self.create_attempts = 0

            def open(self, request, timeout=None):
                path = '/' + request.full_url.split('/api/v1/', 1)[1]
                if path == '/env/create':
                    self.create_attempts += 1
                    raise URLError(socket.timeout('timed out'))
                return super().open(request, timeout=timeout)

        opener = TimedOutCreateOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            runtime_gate=self.recovery_gate(), transport_attempts=4)

        with self.assertRaises(HubApiError) as caught:
            adapter.env_create({
                'containerName': '不存在的新环境',
                'tagName': '测试分组',
            })

        self.assertEqual(opener.create_attempts, 1)
        self.assertTrue(caught.exception.outcome_uncertain)
        self.assertEqual(caught.exception.operation, '/env/create')

    def test_browser_start_timeout_reconciles_exact_open_environment(self):
        class TimedOutStartOpener(FakeLocalApiOpener):
            def __init__(self):
                super().__init__(open_browsers=[{
                    'containerCode': 'container-test-1',
                    'debuggingPort': '59591',
                    'ip': '203.0.113.20',
                }])
                self.start_attempts = 0

            def open(self, request, timeout=None):
                path = '/' + request.full_url.split('/api/v1/', 1)[1]
                if path == '/browser/start':
                    self.start_attempts += 1
                    raise URLError(socket.timeout('timed out'))
                return super().open(request, timeout=timeout)

        opener = TimedOutStartOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            runtime_gate=self.recovery_gate(), transport_attempts=4)

        result = adapter.browser_start('container-test-1', headless=True)

        self.assertEqual(result['debuggingPort'], '59591')
        self.assertEqual(opener.start_attempts, 1)

    def test_core_download_timeout_moves_to_verification_without_replay(self):
        class TimedOutDownloadOpener(FakeLocalApiOpener):
            def __init__(self):
                super().__init__()
                self.download_attempts = 0

            def open(self, request, timeout=None):
                path = '/' + request.full_url.split('/api/v1/', 1)[1]
                if path == '/browser/download-core':
                    self.download_attempts += 1
                    raise URLError(socket.timeout('timed out'))
                return super().open(request, timeout=timeout)

        opener = TimedOutDownloadOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            runtime_gate=self.recovery_gate(), transport_attempts=4)

        result = adapter.download_core('chrome', '148')

        self.assertTrue(result['responseLost'])
        self.assertEqual(opener.download_attempts, 1)

    def test_environment_delete_accepts_only_explicit_numeric_codes(self):
        opener = FakeLocalApiOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            retries=1, timeout=1)

        self.assertTrue(adapter.env_delete(['132725138']))
        delete_calls = [body for _port, path, body, _headers in opener.calls
                        if path == '/env/del']
        self.assertEqual(delete_calls, [{'containerCodes': [132725138]}])
        with self.assertRaises(HubApiError):
            adapter.env_delete(['环境名称'])

    def test_missing_browser_core_is_stable_and_not_retried(self):
        opener = FakeLocalApiOpener(
            response_code=-10007,
            response_message='Chrome[148]Core does not exist')
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            retries=3, timeout=1)

        with self.assertRaises(HubApiError) as context:
            adapter.browser_start('container-test-1', headless=True)

        self.assertEqual(
            context.exception.reason_code,
            'hubstudio_browser_core_missing')
        self.assertEqual(context.exception.api_code, '-10007')
        self.assertEqual(len(opener.calls), 1)

        capability = adapter.capability_snapshot()
        self.assertFalse(capability['available'])
        self.assertTrue(capability['clientRunning'])
        self.assertEqual(
            capability['reasonCode'], 'hubstudio_browser_core_missing')
        self.assertEqual(capability['requiredCore'], {
            'browserType': 'chrome', 'version': '148'})
        self.assertEqual(adapter.core_requirement_snapshot(), {
            'known': True,
            'browserType': 'chrome',
            'version': '148',
            'containerCode': 'container-test-1',
        })
        # The stronger runtime failure must override a superficially healthy
        # group/list heartbeat without another Local API request.
        self.assertEqual(len(opener.calls), 1)

    def test_insufficient_resources_is_reason_coded_without_fast_retries(self):
        opener = FakeLocalApiOpener(
            response_code=-10008,
            response_message='系统资源不足(Insufficient system resources)')
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            retries=3, timeout=1)

        with self.assertRaises(HubApiError) as context:
            adapter.browser_start('container-test-1', headless=True)

        self.assertEqual(
            context.exception.reason_code,
            'hubstudio_system_resources_insufficient')
        self.assertEqual(context.exception.api_code, '-10008')
        self.assertEqual(len(opener.calls), 1)
        capability = adapter.capability_snapshot()
        self.assertFalse(capability['available'])
        self.assertTrue(capability['clientRunning'])
        self.assertTrue(capability['localApiEnabled'])
        self.assertEqual(
            capability['reasonCode'],
            'hubstudio_system_resources_insufficient')
        self.assertEqual(len(opener.calls), 1)

    def test_localized_code_minus_one_missing_core_is_actionable(self):
        opener = FakeLocalApiOpener(
            response_code=-1,
            response_message=(
                '此环境需使用“150”版本内核才可打开，请将软件升级到最新版本'))
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            retries=3, timeout=1)

        with self.assertRaises(HubApiError) as context:
            adapter.browser_start('container-localized-core', headless=True)

        self.assertEqual(
            context.exception.reason_code,
            'hubstudio_browser_core_missing')
        self.assertEqual(context.exception.api_code, '-1')
        self.assertEqual(context.exception.browser_type, 'chrome')
        self.assertEqual(context.exception.core_version, '150')
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(adapter.core_requirement_snapshot(), {
            'known': True,
            'browserType': 'chrome',
            'version': '150',
            'containerCode': 'container-localized-core',
        })

    def test_download_core_uses_official_local_api_contract(self):
        opener = FakeLocalApiOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            retries=1, timeout=1)

        adapter.download_core('chrome', '148')

        self.assertEqual(opener.calls[-1][1:3], (
            '/browser/download-core',
            {'Cores': [{'BrowserType': 1, 'Version': '148'}]},
        ))
        self.assertGreaterEqual(opener.timeouts[-1], 1200)
        with self.assertRaises(HubApiError) as caught:
            adapter.download_core('webkit', 'latest')
        self.assertEqual(
            caught.exception.reason_code,
            'hubstudio_core_download_target_invalid')

    def test_healthy_api_requires_running_client_process(self):
        process_checks = []
        adapter = HubStudioLocalApiAdapter(
            opener=FakeLocalApiOpener(),
            client_running_getter=lambda: process_checks.append(True) or True,
            retries=1, timeout=1)

        capability = adapter.capability_snapshot()

        self.assertTrue(capability['available'])
        self.assertEqual(process_checks, [True])

    def test_stale_local_api_listener_cannot_override_stopped_client(self):
        opener = FakeLocalApiOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener,
            client_running_getter=lambda: False,
            retries=1, timeout=1)

        capability = adapter.capability_snapshot()

        self.assertFalse(capability['available'])
        self.assertFalse(capability['clientRunning'])
        self.assertEqual(
            capability['reasonCode'], 'hubstudio_client_not_running')
        self.assertEqual(opener.calls, [])

    def test_unreachable_api_checks_client_process_once(self):
        process_checks = []

        def client_running():
            process_checks.append(True)
            return True

        adapter = HubStudioLocalApiAdapter(
            opener=FakeLocalApiOpener(unavailable_ports={6873, 6874, 6875}),
            client_running_getter=client_running, retries=1, timeout=1)

        capability = adapter.capability_snapshot()

        self.assertFalse(capability['available'])
        self.assertEqual(process_checks, [True])

    def test_windows_process_diagnostic_is_created_without_a_console(self):
        class StartupInfo(object):
            def __init__(self):
                self.dwFlags = 0
                self.wShowWindow = 1

        with mock.patch.object(hub_api.sys, 'platform', 'win32'), \
                mock.patch.object(hub_api.os, 'name', 'nt'), \
                mock.patch.object(
                    hub_api.subprocess, 'CREATE_NO_WINDOW', 0x08000000,
                    create=True), \
                mock.patch.object(
                    hub_api.subprocess, 'STARTF_USESHOWWINDOW', 0x00000001,
                    create=True), \
                mock.patch.object(
                    hub_api.subprocess, 'SW_HIDE', 0, create=True), \
                mock.patch.object(
                    hub_api.subprocess, 'STARTUPINFO', StartupInfo,
                    create=True):
            kwargs = hub_api._windows_hidden_process_kwargs()

        self.assertEqual(kwargs['creationflags'], 0x08000000)
        self.assertEqual(kwargs['startupinfo'].dwFlags, 0x00000001)
        self.assertEqual(kwargs['startupinfo'].wShowWindow, 0)

    def test_windows_ignores_background_hubstudio_processes_without_window(self):
        completed = mock.Mock(returncode=1)
        with mock.patch.object(hub_api.sys, 'platform', 'win32'), \
                mock.patch.object(hub_api.os, 'name', 'nt'), \
                mock.patch.object(hub_api.subprocess, 'run',
                                  return_value=completed) as run:
            self.assertFalse(hub_api._default_client_running())

        command = run.call_args.args[0]
        self.assertEqual(command[0], 'powershell.exe')
        self.assertIn('MainWindowHandle -ne 0', command[-1])
        self.assertNotIn('tasklist', command)

    def test_windows_accepts_hubstudio_process_with_main_window(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(hub_api.sys, 'platform', 'win32'), \
                mock.patch.object(hub_api.os, 'name', 'nt'), \
                mock.patch.object(hub_api.subprocess, 'run',
                                  return_value=completed):
            self.assertTrue(hub_api._default_client_running())

    def test_available_adapter_lists_locates_opens_and_closes_mock_environment(self):
        opener = FakeLocalApiOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            retries=1, timeout=1)
        capability = adapter.capability_snapshot()
        self.assertTrue(capability['available'])
        self.assertEqual(capability['reasonCode'], 'ok')

        listed = adapter.list_environment_summaries('4254')
        self.assertEqual(listed, [{
            'containerCode': 'container-test-1',
            'serialNumber': '4254',
            'containerName': '脱敏测试环境',
            'tagName': '测试分组',
        }])
        self.assertNotIn('remark', listed[0])
        self.assertEqual(
            adapter.locate_environment('container-test-1')['serialNumber'],
            4254)
        adapter.browser_start('container-test-1')
        adapter.browser_stop('container-test-1')
        paths = [path for _port, path, _body, _headers in opener.calls]
        self.assertIn('/browser/start', paths)
        self.assertIn('/browser/stop', paths)

    def test_browser_status_can_reconcile_one_timed_out_start(self):
        opener = FakeLocalApiOpener(open_browsers=[{
            'containerCode': 'container-test-1',
            'debuggingPort': '59591',
            'ip': '203.0.113.20',
        }, {
            'containerCode': 'container-other',
            'debuggingPort': '59592',
        }])
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            retries=1, timeout=30)

        statuses = adapter.browser_status(
            'container-test-1', timeout=2.5)

        self.assertEqual(statuses, [{
            'containerCode': 'container-test-1',
            'debuggingPort': '59591',
            'ip': '203.0.113.20',
        }])
        self.assertEqual(opener.calls[-1][1:3], (
            '/browser/all-browser-status',
            {'containerCodes': ['container-test-1']},
        ))
        self.assertEqual(opener.timeouts[-1], 2.5)

    def test_browser_lifecycle_status_uses_documented_numeric_states(self):
        opener = FakeLocalApiOpener(open_browsers=[{
            'containerCode': 'opening', 'status': 1,
        }, {
            'containerCode': 'open', 'status': 0,
        }, {
            'containerCode': 'closing', 'status': 2,
        }, {
            'containerCode': 'closed', 'status': 3,
        }])
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            retries=1, timeout=1)

        self.assertEqual(
            adapter.browser_lifecycle_status('opening')['state'], 'opening')
        self.assertEqual(
            adapter.browser_lifecycle_status('open')['state'], 'open')
        self.assertEqual(
            adapter.browser_lifecycle_status('closing')['state'], 'closing')
        self.assertEqual(
            adapter.browser_lifecycle_status('closed')['state'], 'closed')
        self.assertEqual(
            adapter.browser_lifecycle_status('missing')['state'], 'absent')
        self.assertEqual(adapter.open_container_codes(), {
            'opening', 'open', 'closing',
        })

    def test_start_pending_business_error_is_never_fast_replayed(self):
        opener = FakeLocalApiOpener(
            response_code=-10005,
            response_message='last request for startBrowser is unfinished')
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            retries=3, timeout=1)

        with self.assertRaises(HubApiError) as caught:
            adapter.browser_start('container-test-1', headless=True)

        self.assertEqual(
            caught.exception.reason_code, 'hubstudio_browser_start_pending')
        self.assertEqual(caught.exception.api_code, '-10005')
        start_calls = [
            call for call in opener.calls if call[1] == '/browser/start']
        self.assertEqual(len(start_calls), 1)

    def test_environment_lookup_uses_single_exact_filtered_page(self):
        opener = FakeLocalApiOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True,
            retries=1, timeout=1)

        by_code = adapter.env_lookup(container_code='container-test-1')
        by_name = adapter.env_lookup(
            container_name='脱敏测试环境', tag_name='测试分组')

        self.assertEqual(by_code['serialNumber'], 4254)
        self.assertEqual(by_name['containerCode'], 'container-test-1')
        env_calls = [body for _port, path, body, _headers in opener.calls
                     if path == '/env/list']
        self.assertEqual(env_calls, [
            {'current': 1, 'size': 2,
             'containerCodes': ['container-test-1']},
            {'current': 1, 'size': 2,
             'containerName': '脱敏测试环境',
             'tagNames': ['测试分组']},
        ])

    def test_batch_management_uses_only_explicit_mock_targets(self):
        opener = FakeLocalApiOpener()
        adapter = HubStudioLocalApiAdapter(
            opener=opener, client_running_getter=lambda: True, retries=1)
        results = adapter.batch_browser_control('open', ['4254'])
        self.assertEqual(results, [{
            'identifier': '4254',
            'containerCode': 'container-test-1',
            'ok': True,
            'reasonCode': 'ok',
        }])

    def test_client_not_running_has_distinct_reason(self):
        adapter = HubStudioLocalApiAdapter(
            opener=FakeLocalApiOpener(unavailable_ports={6873, 6874, 6875}),
            client_running_getter=lambda: False, retries=1)
        capability = adapter.capability_snapshot()
        self.assertFalse(capability['available'])
        self.assertFalse(capability['clientRunning'])
        self.assertEqual(
            capability['reasonCode'], 'hubstudio_client_not_running')

    def test_running_client_with_unreachable_api_reports_api_disabled(self):
        adapter = HubStudioLocalApiAdapter(
            opener=FakeLocalApiOpener(unavailable_ports={6873, 6874, 6875}),
            client_running_getter=lambda: True, retries=1)
        capability = adapter.capability_snapshot()
        self.assertTrue(capability['clientRunning'])
        self.assertFalse(capability['localApiEnabled'])
        self.assertEqual(
            capability['reasonCode'], 'hubstudio_local_api_disabled')

    def test_limited_known_port_fallback_updates_endpoint(self):
        opener = FakeLocalApiOpener(unavailable_ports={7000})
        adapter = HubStudioLocalApiAdapter(
            port=7000, known_ports=(6873,), opener=opener,
            client_running_getter=lambda: True, retries=1)
        capability = adapter.capability_snapshot()
        self.assertTrue(capability['available'])
        self.assertEqual(
            capability['endpoint'], 'http://127.0.0.1:6873/api/v1')
        attempted_ports = [port for port, path, _body, _headers in opener.calls
                           if path == '/group/list']
        self.assertEqual(attempted_ports, [7000, 6873])

    def test_missing_or_wrong_key_is_reason_coded_without_secret_leak(self):
        secret = 'local-api-key-secret-for-test'
        missing = HubStudioLocalApiAdapter(
            opener=FakeLocalApiOpener(
                response_code='E010403',
                response_message='API Key required'),
            client_running_getter=lambda: True, retries=1)
        missing_capability = missing.capability_snapshot()
        self.assertEqual(
            missing_capability['reasonCode'],
            'hubstudio_local_api_authentication_required')

        wrong = HubStudioLocalApiAdapter(
            api_key=secret,
            opener=FakeLocalApiOpener(
                response_code='E010403',
                response_message='API Key=' + secret),
            client_running_getter=lambda: True, retries=1)
        wrong_capability = wrong.capability_snapshot()
        self.assertEqual(
            wrong_capability['reasonCode'],
            'hubstudio_local_api_authentication_failed')
        self.assertNotIn(secret, json.dumps(wrong_capability))

    def test_incompatible_api_response_has_distinct_reason(self):
        adapter = HubStudioLocalApiAdapter(
            opener=FakeLocalApiOpener(raw_payload={'unexpected': True}),
            client_running_getter=lambda: True, retries=3, timeout=30)
        capability = adapter.capability_snapshot()
        self.assertFalse(capability['available'])
        self.assertTrue(capability['clientRunning'])
        self.assertTrue(capability['localApiEnabled'])
        self.assertEqual(
            capability['reasonCode'], 'hubstudio_local_api_incompatible')
        # Capability discovery is a single short probe, not a business retry.
        self.assertEqual(len(adapter.opener.calls), 1)
        self.assertLessEqual(adapter.opener.timeouts[0], 2.5)

    def test_api_business_error_proves_local_api_is_enabled(self):
        adapter = HubStudioLocalApiAdapter(
            opener=FakeLocalApiOpener(
                response_code='E010500', response_message='test failure'),
            client_running_getter=lambda: True, retries=1)
        capability = adapter.capability_snapshot()
        self.assertFalse(capability['available'])
        self.assertTrue(capability['localApiEnabled'])
        self.assertTrue(capability['authenticated'])
        self.assertEqual(
            capability['reasonCode'], 'hubstudio_local_api_error')

    def test_api_key_store_wraps_key_before_secure_backend(self):
        backend = MemoryTokenBackend()
        store = SystemHubApiKeyStore(backend)
        secret = 'hub-local-key-for-test'
        store.save(secret)
        self.assertNotIn(secret, backend.value)
        self.assertEqual(store.load(), secret)
        store.clear()
        self.assertIsNone(store.load())
        self.assertGreaterEqual(len(_wrap_key('short')), 32)

    def test_api_key_length_matches_secure_backend_envelope_limit(self):
        backend = MemoryTokenBackend()
        store = SystemHubApiKeyStore(backend)
        store.save('k' * 180)
        self.assertLessEqual(len(backend.value), 256)
        with self.assertRaisesRegex(HubApiKeyStoreError, '格式无效'):
            store.save('k' * 181)
        with self.assertRaisesRegex(HubApiKeyStoreError, '格式无效'):
            store.save('密' * 61)

    def test_api_key_rolls_back_when_adapter_reconnect_raises(self):
        store = MemoryHubApiKeyStore('old-hub-key')
        reconnect_calls = []

        def reconnect():
            reconnect_calls.append(store.load())
            if len(reconnect_calls) == 1:
                raise RuntimeError('simulated adapter failure')
            return True

        state = SimpleNamespace(
            hub_api_key_store=store,
            reconnect_hub=reconnect,
            hub_capabilities=lambda force=False: {'available': True},
        )

        with self.assertRaisesRegex(RuntimeError, 'adapter failure'):
            AppState.save_hub_api_key(state, 'new-hub-key')

        self.assertEqual(store.load(), 'old-hub-key')
        self.assertEqual(reconnect_calls, ['new-hub-key', 'old-hub-key'])

    def test_api_key_success_keeps_new_secure_value(self):
        store = MemoryHubApiKeyStore('old-hub-key')
        state = SimpleNamespace(
            hub_api_key_store=store,
            reconnect_hub=lambda: True,
            hub_capabilities=lambda force=False: {'available': True},
        )

        result = AppState.save_hub_api_key(state, 'new-hub-key')

        self.assertTrue(result['saved'])
        self.assertTrue(result['configured'])
        self.assertEqual(store.load(), 'new-hub-key')

    def test_source_and_installers_have_no_hubstudio_cli_dependency(self):
        root = Path(__file__).resolve().parents[1]
        files = []
        for source_root in (root / 'src', root / 'packaging', root / 'scripts'):
            if source_root.is_dir():
                files.extend(path for path in source_root.rglob('*')
                             if path.is_file() and path.suffix in {
                                 '.py', '.sh', '.go', '.command', '.ps1'})
        files.extend(path for path in root.glob('*.sh') if path.is_file())
        combined = '\n'.join(path.read_text(
            encoding='utf-8', errors='ignore') for path in files)
        self.assertNotIn('hubstudio-cli', combined.casefold())


if __name__ == '__main__':
    unittest.main()
