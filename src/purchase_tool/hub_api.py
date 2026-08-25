# -*- coding: utf-8 -*-
"""HubStudio Local API 客户端（标准库 urllib，无鉴权，本机 127.0.0.1）。

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
import time
import urllib.request

from .task_runtime import HubRuntimeGate

DEFAULT_PORT = 6873

# 强制直连不走系统代理：同事电脑开着 Clash 类系统代理时，urllib 默认会把
# 127.0.0.1 的请求也送进代理导致"连接失败"（PowerShell/浏览器则默认绕过本地）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HubApiError(Exception):
    pass


class HubStudioApi(object):
    def __init__(self, port=DEFAULT_PORT, timeout=30, retries=3, api_key=None,
                 runtime_gate=None):
        self.base = 'http://127.0.0.1:%s/api/v1' % port
        self.timeout = timeout
        self.retries = retries
        self.runtime_gate = runtime_gate or HubRuntimeGate()
        self.headers = {'Content-Type': 'application/json'}
        if api_key:
            self.headers['local-api-key'] = api_key

    def _post(self, path, body):
        """POST JSON，重试递增间隔；HubStudio 未启动时抛 ConnectionError。"""
        data = json.dumps(body).encode('utf-8')
        last_err = None
        for i in range(self.retries):
            try:
                req = urllib.request.Request(
                    self.base + path, data=data, headers=self.headers,
                    method='POST')
                with self.runtime_gate.request():
                    with OPENER.open(req, timeout=self.timeout) as resp:
                        j = json.loads(resp.read().decode('utf-8'))
                if j.get('code') == 0:
                    return j.get('data')
                last_err = HubApiError('%s 返回 code=%s: %s' % (
                    path, j.get('code'), j.get('msg')))
            except urllib.error.URLError as e:
                last_err = ConnectionError('无法连接 HubStudio Local API: %s' % e)
                break   # 客户端没开，重试无意义
            except Exception as e:   # 超时等，可重试
                last_err = HubApiError('%s 调用异常: %s' % (path, e))
            time.sleep(0.4 * (i + 1))
        raise last_err

    # ---- 查询类 ----

    def ping(self):
        """探测 HubStudio 客户端是否在线（轻量调用）。"""
        try:
            self._post('/group/list', {'current': 1, 'size': 1})
            return True
        except Exception:
            return False

    def ping_detail(self):
        """探测并返回失败原因（前端展示用，便于远程排查）。"""
        try:
            self._post('/group/list', {'current': 1, 'size': 1})
            return True, ''
        except Exception as e:
            return False, ('%s: %s' % (type(e).__name__, e))[:300]

    def group_list(self):
        data = self._post('/group/list', {'current': 1, 'size': 100})
        return [g.get('tagName') for g in (data or []) if g.get('tagName')]

    def env_list(self, tag_name=None, page_size=200):
        """取环境列表；带分组名则按分组过滤，自动翻页。"""
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
        with self.runtime_gate.browser():
            return self._post('/browser/start', body) or {}

    def browser_stop(self, container_code):
        with self.runtime_gate.browser():
            return self._post('/browser/stop',
                              {'containerCode': str(container_code)}) or {}

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
