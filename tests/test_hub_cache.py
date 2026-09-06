import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error
import urllib.request

import pytest

from purchase_tool import main
from purchase_tool.cloud_auth import LocalAuthError
from purchase_tool.hub_cache import (
    HubCacheError, HubCacheManager, configured_cache, discover_roots,
    inspect_cache, require_hub_closed, single_link,
)
from purchase_tool.task_runtime import LocalTaskCoordinator, TaskConflict


@pytest.fixture
def fixture(tmp_path):
    base = tmp_path.resolve()
    root = base / 'hubstudio-client'
    cache = root / 'sdk/cache'
    profile = cache / 'chromium_100001'
    for relative, content in {
        'Local State': 'synthetic-profile',
        'Default/Cache/Cache_Data/resource': 'x' * 128,
        'Default/Code Cache/js/code': 'x' * 64,
        'Default/GPUCache/data_0': 'x' * 32,
        'Default/Cookies': 'synthetic-login-marker',
        'Default/Local Storage/leveldb/value': 'synthetic-state',
        'Default/IndexedDB/db': 'synthetic-state',
        'Default/Extensions/extension/file': 'synthetic-extension',
        'Default/Service Worker/CacheStorage/value': 'retained-site-storage',
        'Default/History': 'retained-history',
    }.items():
        path = profile / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (root / 'Cache').mkdir()
    (root / 'Cache/client').write_bytes(b'x' * 16)
    db = sqlite3.connect(root / 'ELECTRON_DB_LOCAL_STORAGE')
    db.execute('create table localstorage (key text, value text)')
    db.execute('insert into localstorage values (?,?)', (
        'SYSTEM_CONFIG', json.dumps({'cachePath': str(cache)})))
    db.execute('insert into localstorage values (?,?)', ('ACCOUNT', 'never-read-this'))
    db.commit()
    db.close()
    roots = [(root, 'client', False), (cache, 'profiles', False)]
    tasks = LocalTaskCoordinator(lambda: True)
    manager = HubCacheManager(tasks, discover=lambda _: (roots, []), closed_check=lambda: None)
    return SimpleNamespace(base=base, root=root, cache=cache, profile=profile,
                           roots=roots, tasks=tasks, manager=manager)


def wait(manager):
    deadline = time.monotonic() + 5
    while manager.snapshot()['running'] and time.monotonic() < deadline:
        time.sleep(.005)
    result = manager.snapshot()
    assert not result['running']
    return result


def scanned(f):
    f.manager.scan()
    result = wait(f.manager)
    assert result['state'] == 'ready'
    return result


def test_scan_is_read_only_and_deduplicates_roots(fixture):
    f = fixture
    before = {p: p.read_bytes() for p in f.root.rglob('*') if p.is_file()}
    report, _ = inspect_cache(f.roots * 2)
    assert {g['id']: g['bytes'] for g in report['groups']} == {'web': 128, 'code': 96, 'client': 16}
    assert report['cleanableBytes'] == 240
    assert report['profileDirectoryCount'] == 1
    assert 'never-read-this' not in json.dumps(report)
    assert before == {p: p.read_bytes() for p in f.root.rglob('*') if p.is_file()}


def test_selected_cleanup_preserves_all_account_state_and_other_caches(fixture):
    f = fixture
    web_cache = f.profile / 'Default/Cache'
    protected = {p: p.read_bytes() for p in f.profile.rglob('*')
                 if p.is_file() and web_cache not in p.parents}
    scan = scanned(f)
    f.manager.clear(scan['scanId'], ['web'], confirmed=True)
    result = wait(f.manager)
    assert result['state'] == 'complete', result
    assert result['selectedGroups'] == ['web']
    assert result['selectedBytes'] == 128
    assert result['selectedFileCount'] == 1
    assert result['removedBytes'] == 128
    assert result['removedFiles'] == 1
    assert not (f.profile / 'Default/Cache/Cache_Data/resource').exists()
    assert all(path.read_bytes() == data for path, data in protected.items())
    assert (f.root / 'Cache/client').exists()
    assert not f.tasks.running()
    assert not result['scanId']
    assert scanned(f)['cleanableBytes'] == 112


@pytest.mark.parametrize('groups,confirmed', [([], True), (['cookie'], True),
                                           ([{}], True), (['web'], False)])
def test_invalid_selection_never_deletes(fixture, groups, confirmed):
    scan = scanned(fixture)
    with pytest.raises(HubCacheError):
        fixture.manager.clear(scan['scanId'], groups, confirmed=confirmed)
    assert (fixture.profile / 'Default/Cache/Cache_Data/resource').exists()
    assert not fixture.tasks.running()


def test_expired_or_replaced_scan_rejected(fixture):
    old = scanned(fixture)
    fresh = scanned(fixture)
    with pytest.raises(HubCacheError, match='失效'):
        fixture.manager.clear(old['scanId'], ['web'], confirmed=True)
    fixture.manager.scanned_at -= 1801
    with pytest.raises(HubCacheError, match='失效'):
        fixture.manager.clear(fresh['scanId'], ['web'], confirmed=True)


def test_busy_executor_and_running_hub_block_cleanup(fixture):
    f = fixture
    scan = scanned(f)
    task_id = f.tasks.begin('query')
    with pytest.raises(TaskConflict):
        f.manager.clear(scan['scanId'], ['web'], confirmed=True)
    f.tasks.finish(task_id)
    with patch('purchase_tool.hub_cache.process_paths', return_value=['Hubstudio.exe']):
        f.manager.closed_check = require_hub_closed
        with pytest.raises(HubCacheError, match='退出 HubStudio'):
            f.manager.clear(scan['scanId'], ['web'], confirmed=True)
    assert not f.tasks.running()
    assert (f.profile / 'Default/Cache/Cache_Data/resource').exists()


def test_cleanup_excludes_new_business_tasks(fixture):
    f = fixture
    scan = scanned(f)
    held, release = threading.Event(), threading.Event()
    calls = []
    def guard():
        calls.append(1)
        if len(calls) > 1:
            held.set()
            assert release.wait(3)
    f.manager.closed_check = guard
    f.manager.clear(scan['scanId'], ['web'], confirmed=True)
    assert held.wait(3)
    try:
        with pytest.raises(TaskConflict):
            f.tasks.begin('env_batch')
        with pytest.raises(HubCacheError):
            f.manager.scan()
    finally:
        release.set()
    wait(f.manager)
    assert not f.tasks.running()


def test_reopening_hub_stops_cleanup_before_deleting(fixture):
    f = fixture
    scan = scanned(f)
    with patch.object(f.manager, 'closed_check', side_effect=[None, HubCacheError('running', 'HubStudio 已重新启动')]):
        f.manager.clear(scan['scanId'], ['web'], confirmed=True)
        result = wait(f.manager)
    assert result['state'] == 'partial'
    assert result['removedBytes'] == 0
    assert result['errorCode'] == 'running'


def test_links_and_swapped_directories_are_not_followed(fixture):
    f = fixture
    outside = f.base / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    link = f.profile / 'Default/Cache/external'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    scan = scanned(f)
    assert scan['warnings']
    target = f.profile / 'Default/Code Cache'
    target.rename(target.with_name('original-code'))
    target.symlink_to(outside, target_is_directory=True)
    f.manager.clear(scan['scanId'], ['web', 'code'], confirmed=True)
    result = wait(f.manager)
    assert result['state'] == 'partial'
    assert (outside / 'keep').read_text() == 'keep'
    assert (f.profile / 'Default/original-code/js/code').exists()


def test_windows_junction_is_not_traversed(fixture):
    from purchase_tool.hub_cache import linked
    import stat
    assert linked(SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400))
    assert not linked(SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0))


def test_single_link_rejects_hardlinked_files(tmp_path):
    original = tmp_path / 'original'
    duplicate = tmp_path / 'duplicate'
    original.write_text('cache')
    assert single_link(original, original.lstat())
    try:
        os.link(original, duplicate)
    except OSError:
        pytest.skip('hard links unavailable')
    assert not single_link(original, original.lstat())


def test_locked_file_reports_partial_instead_of_fake_success(fixture):
    f = fixture
    scan = scanned(f)
    with patch.object(Path, 'unlink', side_effect=PermissionError):
        f.manager.clear(scan['scanId'], ['web'], confirmed=True)
        result = wait(f.manager)
    assert result['state'] == 'partial'
    assert result['removedBytes'] == 0
    assert result['skippedFiles'] == 1


def test_configured_custom_path_and_windows_install_discovery(fixture):
    f = fixture
    assert configured_cache(f.root) == f.cache
    roots, installs = discover_roots(home=f.base, platform='win32',
                                    environ={'APPDATA': str(f.base)}, installations=[f.root])
    assert (f.cache, 'profiles', False) in roots
    assert str(f.root) in installs
    roots, _ = discover_roots(str(f.cache), home=f.base, platform='linux', environ={})
    assert (f.cache, 'profiles', False) in roots
    with pytest.raises(HubCacheError):
        discover_roots(str(f.base), home=f.base)


def test_mac_external_cache_profile_without_local_state(fixture):
    f = fixture
    (f.profile / 'Local State').unlink()
    report, _ = inspect_cache([(f.cache, 'profiles', False)])
    assert report['cleanableBytes'] == 0
    report, _ = inspect_cache([(f.cache, 'profiles', True)])
    assert report['cleanableBytes'] == 224


def test_process_failures_block_cleanup_and_unrelated_chrome_is_allowed():
    with patch('purchase_tool.hub_cache.process_paths', return_value=['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']):
        require_hub_closed()
    with patch('purchase_tool.hub_cache.process_paths', side_effect=HubCacheError('unknown', '状态未知')):
        with pytest.raises(HubCacheError):
            require_hub_closed()


def test_scan_failure_is_visible_and_invalidates_previous_scan(fixture):
    f = fixture
    scanned(f)
    f.manager.discover = lambda _: (_ for _ in ()).throw(OSError('private-file-value'))
    f.manager.scan()
    result = wait(f.manager)
    assert result['state'] == 'failed'
    assert not result['scanId']
    assert 'private-file-value' not in json.dumps(result)


@pytest.fixture
def api_server(fixture, monkeypatch):
    auth = SimpleNamespace(require=lambda: {'user': {'id': 'fixture-user'}})
    monkeypatch.setattr(main, 'STATE', SimpleNamespace(hub_cache=fixture.manager, auth=auth))
    server = ThreadingHTTPServer(('127.0.0.1', 0), main.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:%d' % server.server_port
    def request(path, body=None, headers=None):
        req = urllib.request.Request(base + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)
    yield request, auth
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_real_http_scan_select_clear_and_status(api_server, fixture):
    request, _ = api_server
    assert request('/api/hub-cache/scan', {})[0] == 202
    scan = wait(fixture.manager)
    assert request('/api/hub-cache/status')[1]['cleanableBytes'] == 240
    assert request('/api/hub-cache/clear', {'scanId': scan['scanId'], 'groups': ['web'], 'confirmed': True})[0] == 202
    wait(fixture.manager)
    assert request('/api/hub-cache/status')[1]['removedBytes'] == 128


def test_http_auth_origin_and_rpc_protect_scan_and_delete(api_server, fixture):
    request, auth = api_server
    for headers in ({'Origin': 'https://external.invalid'}, {'X-Xynigo-Executor-RPC': 'x' * 40}, {'Sec-Fetch-Site': 'cross-site'}):
        assert request('/api/hub-cache/scan', {}, headers)[0] == 403
        assert request('/api/hub-cache/clear', {}, headers)[0] == 403
        assert request('/api/hub-cache/status', headers=headers)[0] == 403
    def denied():
        raise LocalAuthError('auth_required', '请先登录', 401)
    auth.require = denied
    assert request('/api/hub-cache/scan', {})[0] == 401
    assert request('/api/hub-cache/clear', {})[0] == 401
    assert request('/api/hub-cache/status')[0] == 401
    assert fixture.manager.snapshot()['state'] == 'idle'


def test_ui_selection_totals_and_path_escaping(tmp_path):
    javascript = (Path(__file__).resolve().parents[1] / 'src/purchase_tool/web/desktop.js').read_text(
        encoding='utf-8')
    javascript = javascript.replace('  initializeAuth();', '''
  window.testCache = {state:state, panel:hubCachePanel, selected:cacheSelectedBytes, progress:renderHubCacheProgressModal};
  // initializeAuth();''')
    script = '''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const node = {innerHTML:'', className:''};
const context = {URLSearchParams, location:{search:'',pathname:'/desktop/',origin:'http://127.0.0.1'},
document:{getElementById:()=>node,addEventListener:()=>{}},window:{},setTimeout,clearTimeout};
vm.runInNewContext(SOURCE, context);
const {state,panel,selected,progress} = context.window.testCache;
state.hubCache = {state:'ready',scanId:'test',cleanableBytes:3072,groups:[
{id:'web',label:'网页资源缓存',bytes:1024},{id:'code',label:'脚本与渲染缓存',bytes:2048}],
locations:[{path:'<script>unsafe</script>',cleanableBytes:3072}]};
state.cacheSelection = {web:true};
assert.equal(selected(),1024);
let html = panel();
assert.ok(html.includes('已选 1.0 KB'));
assert.ok(html.includes('&lt;script&gt;unsafe&lt;/script&gt;'));
assert.ok(!html.includes('<script>unsafe'));
state.hubCache = {state:'cleaning',running:true,message:'正在清理选中的缓存',selectedBytes:3072,selectedFileCount:3,removedBytes:1024,removedFiles:1,groups:state.hubCache.groups};
let active = panel();
assert.ok(active.includes('缓存清理正在后台进行'));
assert.ok(active.includes('data-action="open-hub-cache-progress"'));
assert.ok(active.includes('data-action="clear-hub-cache" disabled'));
state.cacheProgressOpen = true;
state.cacheClearGroupNames = ['网页资源缓存'];
progress();
assert.ok(node.innerHTML.includes('正在清理 HubStudio 缓存'));
assert.ok(node.innerHTML.includes('role="progressbar"'));
assert.ok(node.innerHTML.includes('aria-valuenow="33"'));
assert.ok(node.innerHTML.includes('1.0 KB / 3.0 KB'));
assert.ok(node.innerHTML.includes('data-action="cache-progress-background"'));
state.hubCache.running = false; state.hubCache.state = 'complete'; state.hubCache.removedBytes = 3072; state.hubCache.removedFiles = 3;
progress();
assert.ok(node.innerHTML.includes('缓存清理完成'));
assert.ok(node.innerHTML.includes('aria-valuenow="100"'));
assert.ok(node.innerHTML.includes('data-action="cache-progress-close"'));
'''.replace('SOURCE', json.dumps(javascript))
    script_path = tmp_path / 'hub-cache-ui-test.js'
    script_path.write_text(script, encoding='utf-8')
    subprocess.run(['node', str(script_path)], check=True,
                   capture_output=True, text=True)
