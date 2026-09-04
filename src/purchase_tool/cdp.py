# -*- coding: utf-8 -*-
"""CDP 直连客户端：不经 Playwright，用 HTTP + WebSocket 直接操控 HubStudio 打开的浏览器。

事实依据（2026-08-18 实测，Chrome 148 内核）：
- /json/new 必须用 PUT，GET 会被拒（"supports only PUT verb"）
- /json/new 的 url 参数不生效，返回的是 about:blank，导航必须走 Page.navigate
- 页面级操作只需 Runtime.evaluate 即可覆盖现有 .mjs 脚本的全部取数需求

用法：
    cdp = CdpClient(61241)
    page = cdp.new_page()
    page.goto('https://www.shein.com.mx/user/orders/list')   # 等 domcontentloaded + 静置
    text = page.inner_text()      # 等价 playwright locator('body').innerText()
    html = page.outer_html()      # 等价 playwright page.content()
    url  = page.url               # 当前地址（登录跳转判定用）
    page.close()
"""
import base64
import json
import time
import urllib.request

import websocket

# 直连不走系统代理（理由同 hub_api：系统代理会把本机回环请求送进代理）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CdpError(Exception):
    pass


class CdpClient(object):
    def __init__(self, port, http_timeout=10):
        self.base = 'http://127.0.0.1:%s' % port
        self.port = port
        self.http_timeout = http_timeout

    def _http(self, path, method='GET'):
        req = urllib.request.Request(self.base + path, method=method)
        with _OPENER.open(req, timeout=self.http_timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))

    def new_page(self):
        """PUT /json/new 开新标签页，返回 PageTarget。"""
        info = self._http('/json/new', method='PUT')
        return PageTarget(self, info['id'], info['webSocketDebuggerUrl'])

    def list_pages(self):
        """列出当前普通页签（不包扩展 background page）。"""
        return [x for x in self._http('/json/list') if x.get('type') == 'page']

    def version_info(self):
        """Return the running browser's non-sensitive CDP version summary."""
        raw = self._http('/json/version')
        return {
            'browser': str(raw.get('Browser') or '')[:80],
            'protocolVersion': str(raw.get('Protocol-Version') or '')[:32],
        }

    def attach_page(self, target_id=None, url_contains=None):
        """连接已存在页签，用于注册流在 SHEIN/Outlook 之间切换。"""
        matches = self.list_pages()
        if target_id:
            matches = [x for x in matches if x.get('id') == target_id]
        if url_contains:
            matches = [x for x in matches
                       if url_contains in (x.get('url') or '')]
        if not matches:
            raise CdpError('未找到匹配的页签')
        info = matches[0]
        return PageTarget(self, info['id'], info['webSocketDebuggerUrl'])

    def close(self):
        """无浏览器级资源需要释放，占位保持与调用方约定。"""
        pass


class PageTarget(object):
    def __init__(self, client, target_id, ws_url, ws_timeout=90):
        self.client = client
        self.target_id = target_id
        self._id = 0
        # Chrome 111+ 拒绝携带 Origin 头的握手（403），必须 suppress_origin
        self._ws = websocket.create_connection(
            ws_url, timeout=ws_timeout, enable_multithread=True,
            suppress_origin=True)

    def _send(self, method, params=None):
        self._id += 1
        msg_id = self._id
        payload = {'id': msg_id, 'method': method}
        if params:
            payload['params'] = params
        self._ws.send(json.dumps(payload))
        while True:
            raw = self._ws.recv()
            msg = json.loads(raw)
            if msg.get('id') == msg_id:
                if 'error' in msg:
                    raise CdpError('%s: %s' % (method, msg['error']))
                return msg.get('result', {})

    def _evaluate(self, expression):
        result = self._send('Runtime.evaluate', {
            'expression': expression, 'returnByValue': True})
        if result.get('exceptionDetails'):
            # 表达式自身抛异常时返回 None（如 body 不存在），不中断流程
            return None
        value = result.get('result', {})
        return value.get('value')

    def bring_to_front(self):
        """激活当前页签，确保 Input 域事件落到本页而非同环境旧页签。"""
        self._send('Page.bringToFront')

    def goto(self, url, dom_timeout=45, settle_seconds=6.0):
        """导航并等待 domcontentloaded（等价 waitUntil:'domcontentloaded'），
        再静置 settle_seconds——沿用已验证 .mjs 脚本的 6 秒等待。"""
        self._send('Page.navigate', {'url': url})
        deadline = time.time() + dom_timeout
        while time.time() < deadline:
            state = self._evaluate('document.readyState')
            if state in ('interactive', 'complete'):
                break
            time.sleep(0.3)
        time.sleep(settle_seconds)

    @property
    def url(self):
        return self._evaluate('location.href') or ''

    def inner_text(self):
        value = self._evaluate(
            'document.body ? document.body.innerText : ""')
        return value or ''

    def element_inner_text(self, selector):
        """Return rendered text for one element without exposing page HTML."""
        value = self._evaluate(
            '(() => { const e=document.querySelector(%s); '
            'return e ? String(e.innerText || "") : ""; })()'
            % json.dumps(selector))
        return value or ''

    def outer_html(self):
        value = self._evaluate('document.documentElement.outerHTML')
        return value or ''

    # ---- 可交互页面能力（注册模块复用） ----

    def wait_for(self, expression, timeout=25, interval=0.5):
        """轮询 JS 表达式直到 truthy；超时返回 False。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self._evaluate(expression):
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def wait_selector(self, selector, timeout=25):
        sel = json.dumps(selector)
        return self.wait_for(
            '(() => { const e=document.querySelector(%s); if(!e) return false; '
            'const r=e.getBoundingClientRect(); return r.width>0&&r.height>0; })()'
            % sel, timeout=timeout)

    def value(self, selector):
        return self._evaluate(
            '(() => { const xs=[...document.querySelectorAll(%s)]; '
            'const e=xs.find(x=>/^(INPUT|TEXTAREA|SELECT)$/.test(x.tagName))||xs[0]; '
            'return e ? String(e.value || "") : null; })()'
            % json.dumps(selector))

    def fill(self, selector, value, verify=True):
        """聚焦+全选后用 Input.insertText 输入，触发 React 真实输入链。

        禁止把 value 拼进 JS 表达式，避免凭证落入异常日志。
        """
        self.bring_to_front()
        sel = json.dumps(selector)
        focused = self._evaluate(
            '(() => { const xs=[...document.querySelectorAll(%s)]; '
            'const e=xs.find(x=>/^(INPUT|TEXTAREA)$/.test(x.tagName))||xs[0]; '
            'if(!e || typeof e.focus!=="function") return false; '
            'e.focus(); if(typeof e.select==="function") e.select(); return true; })()'
            % sel)
        if not focused:
            raise CdpError('未找到输入框: %s' % selector)
        self._send('Input.insertText', {'text': str(value)})
        if verify and self.value(selector) != str(value):
            raise CdpError('输入回读校验失败: %s' % selector)
        return True

    def type_keys(self, text, delay=0.06):
        """向当前焦点逐键输入；适合六格验证码自动跳格。"""
        for ch in str(text):
            code = ('Digit' + ch) if ch.isdigit() else (
                'Key' + ch.upper() if ch.isalpha() else '')
            params = {'key': ch, 'code': code, 'text': ch,
                      'unmodifiedText': ch,
                      'windowsVirtualKeyCode': ord(ch)}
            self._send('Input.dispatchKeyEvent',
                       dict(params, type='keyDown'))
            self._send('Input.dispatchKeyEvent',
                       {'type': 'keyUp', 'key': ch, 'code': code,
                        'windowsVirtualKeyCode': ord(ch)})
            if delay:
                time.sleep(delay)

    def focus_code_input(self):
        """聚焦当前页的验证码输入框，避开顶部搜索框。"""
        focused = self._evaluate(r'''
            (() => {
              const visible = e => {
                const r=e.getBoundingClientRect();
                return r.width>0 && r.height>0;
              };
              const xs=[...document.querySelectorAll('input')].filter(visible);
              const e=xs.find(x =>
                x.autocomplete==='one-time-code' ||
                x.inputMode==='numeric' ||
                (x.maxLength>0 && x.maxLength<=8) ||
                /code|código|验证码/i.test((x.placeholder||'')+' '+(x.ariaLabel||'')));
              if(!e) return false;
              e.focus(); if(typeof e.select==='function') e.select(); return true;
            })()
        ''')
        if not focused:
            raise CdpError('未找到可见的验证码输入框')
        return True

    def shadow_visible_text(self, host_selector='gee-captcha'):
        """读取 Shadow DOM 的可交互文本，跳过内嵌 style/script。

        SHEIN 风控认证弹窗挂在 ``gee-captcha`` 的 Shadow DOM 中；普通
        ``document.body.innerText`` 看不到它。
        """
        return self._evaluate(r'''
            (() => {
              const root=document.querySelector(%s)?.shadowRoot;
              if(!root) return '';
              const walker=document.createTreeWalker(
                root, NodeFilter.SHOW_TEXT);
              const out=[];
              for(let n=walker.nextNode(); n; n=walker.nextNode()){
                const p=n.parentElement;
                if(!p || /^(STYLE|SCRIPT|NOSCRIPT)$/.test(p.tagName)) continue;
                const t=String(n.nodeValue||'').replace(/\s+/g,' ').trim();
                if(t) out.push(t);
              }
              return out.join('\n');
            })()
        ''' % json.dumps(host_selector)) or ''

    def focus_shadow_code_input(self, host_selector='gee-captcha'):
        """聚焦 Shadow DOM 中的验证码框。"""
        focused = self._evaluate(r'''
            (() => {
              const root=document.querySelector(%s)?.shadowRoot;
              if(!root) return false;
              const visible=e=>{const r=e.getBoundingClientRect();
                return r.width>0&&r.height>0&&!e.disabled&&!e.readOnly;};
              const xs=[...root.querySelectorAll('input')].filter(visible);
              const e=xs.find(x=>x.autocomplete==='one-time-code'||
                x.inputMode==='numeric'||(x.maxLength>0&&x.maxLength<=10)||
                /code|c[oó]digo|验证码/i.test((x.placeholder||'')+' '+
                  (x.ariaLabel||'')))||xs.at(-1);
              if(!e) return false;
              e.focus(); if(typeof e.select==='function') e.select();
              return true;
            })()
        ''' % json.dumps(host_selector))
        if not focused:
            raise CdpError('未找到 Shadow DOM 验证码输入框')
        return True

    def click_shadow_text(self, text, host_selector='gee-captcha', exact=True):
        """在 Shadow DOM 中按可见文本发送真实鼠标点击。"""
        wanted = json.dumps(str(text))
        exact_js = ('t===wantedNorm' if exact
                    else 't.indexOf(wantedNorm)>=0')
        point = self._evaluate(r'''
            (() => {
              const root=document.querySelector(%s)?.shadowRoot;
              if(!root) return null;
              const wanted=%s;
              const norm=s=>String(s||'').replace(/\s+/g,' ').trim()
                .toLocaleLowerCase();
              const wantedNorm=norm(wanted);
              const els=[...root.querySelectorAll(
                'button,a,[role=button],input[type=submit]')];
              for(const e of els){
                const t=norm(e.innerText||e.value);
                const r=e.getBoundingClientRect();
                if(%s && r.width>0&&r.height>0)
                  return {x:r.left+r.width/2,y:r.top+r.height/2};
              }
              return null;
            })()
        ''' % (json.dumps(host_selector), wanted, exact_js))
        if not self._click_point(point):
            raise CdpError('未找到 Shadow DOM 可点击文本: %s' % text)
        return True

    def click_text_in_ancestor(self, text, ancestor_selector):
        """点击精确文本节点，要求它位于指定祖先中。

        Outlook 邮件行是 ``[role=option]``，主题本身只是 span，不能由
        ``click_text`` 的按钮/链接选择器命中。
        """
        point = self._evaluate(r'''
            (() => {
              const wanted=%s, ancestor=%s;
              const norm=s=>String(s||'').replace(/\s+/g,' ').trim();
              const xs=[...document.querySelectorAll('*')]
                .filter(e=>norm(e.innerText)===norm(wanted)&&
                  e.closest(ancestor)&&e.getBoundingClientRect().width>0)
                .sort((a,b)=>a.getBoundingClientRect().top-
                  b.getBoundingClientRect().top);
              const e=xs[0]; if(!e) return null;
              const r=e.getBoundingClientRect();
              return {x:r.left+r.width/2,y:r.top+r.height/2};
            })()
        ''' % (json.dumps(str(text)), json.dumps(str(ancestor_selector))))
        if not self._click_point(point):
            raise CdpError('未找到祖先内可点击文本: %s' % text)
        return True

    def _click_point(self, point):
        if not point:
            return False
        self.bring_to_front()
        for event_type in ('mousePressed', 'mouseReleased'):
            self._send('Input.dispatchMouseEvent', {
                'type': event_type, 'x': point['x'], 'y': point['y'],
                'button': 'left', 'clickCount': 1})
        return True

    def click_selector(self, selector):
        point = self._evaluate(
            '(() => { const e=document.querySelector(%s); if(!e) return null; '
            'const r=e.getBoundingClientRect(); if(r.width<=0||r.height<=0) '
            'return null; return {x:r.left+r.width/2,y:r.top+r.height/2}; })()'
            % json.dumps(selector))
        if not self._click_point(point):
            raise CdpError('未找到可点击元素: %s' % selector)
        return True

    def click_text(self, text, exact=True):
        """按可见文本找按钮/链接/角色按钮，用鼠标事件点击。"""
        wanted = json.dumps(str(text))
        exact_js = ('t===wantedNorm' if exact
                    else 't.indexOf(wantedNorm)>=0')
        point = self._evaluate(
            '(() => { const wanted=%s; const norm=s=>String(s||"").replace(/\\s+/g," ").trim(); '
            'const wantedNorm=norm(wanted).toLocaleLowerCase(); '
            'const els=[...document.querySelectorAll('
            '"button,a,[role=button],input[type=submit]")]; '
            'for(const e of els){ const t=norm(e.innerText||e.value).toLocaleLowerCase(); '
            'const r=e.getBoundingClientRect(); if(%s && r.width>0&&r.height>0) '
            'return {x:r.left+r.width/2,y:r.top+r.height/2}; } return null; })()'
            % (wanted, exact_js))
        if not self._click_point(point):
            raise CdpError('未找到可点击文本: %s' % text)
        return True

    def check_all_visible(self):
        """逐个鼠标勾选可见且未勾选的 checkbox，返回勾选数/总数。"""
        points = self._evaluate(
            '(() => [...document.querySelectorAll("input[type=checkbox]")]'
            '.filter(e=>{const r=e.getBoundingClientRect();return !e.checked&&r.width>0&&r.height>0;})'
            '.map(e=>{const r=e.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};}))()') or []
        for point in points:
            self._click_point(point)
            time.sleep(0.08)
        state = self._evaluate(
            '(() => { const xs=[...document.querySelectorAll("input[type=checkbox]")]'
            '.filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;}); '
            'return {total:xs.length,checked:xs.filter(e=>e.checked).length}; })()')
        return state or {'total': 0, 'checked': 0}

    def capture_element_union(self, selectors, image_format='jpeg', quality=75,
                              padding=8, hide_selectors=None):
        """截取多个页面元素的联合区域，返回 ``(bytes, width, height)``。

        坐标按文档绝对位置计算并允许截取视口外区域。``hide_selectors``
        使用 visibility 隐藏敏感区但不改变布局，适合物流页排除地址/商品。
        """
        if not selectors:
            raise CdpError('截图区域不能为空')
        fmt = str(image_format or 'jpeg').lower()
        if fmt not in ('jpeg', 'png', 'webp'):
            raise CdpError('不支持的截图格式: %s' % fmt)
        geometry = self._evaluate(r'''
            (() => {
              const selectors=%s, hides=%s, padding=%s;
              for(const sel of hides){
                for(const e of document.querySelectorAll(sel))
                  e.style.visibility='hidden';
              }
              const els=[];
              for(const sel of selectors)
                for(const e of document.querySelectorAll(sel)){
                  const r=e.getBoundingClientRect();
                  if(r.width>0&&r.height>0) els.push(r);
                }
              if(!els.length) return null;
              const sx=window.scrollX||0, sy=window.scrollY||0;
              const left=Math.max(0,Math.min(...els.map(r=>r.left+sx))-padding);
              const top=Math.max(0,Math.min(...els.map(r=>r.top+sy))-padding);
              const right=Math.max(...els.map(r=>r.right+sx))+padding;
              const bottom=Math.max(...els.map(r=>r.bottom+sy))+padding;
              return {x:left,y:top,width:Math.max(1,right-left),
                height:Math.max(1,bottom-top)};
            })()
        ''' % (json.dumps(list(selectors)),
               json.dumps(list(hide_selectors or [])), int(padding)))
        if not geometry:
            raise CdpError('未找到可截图的页面区域')
        params = {
            'format': fmt,
            'fromSurface': True,
            'captureBeyondViewport': True,
            'clip': dict(geometry, scale=1),
        }
        if fmt in ('jpeg', 'webp'):
            params['quality'] = max(0, min(100, int(quality)))
        result = self._send('Page.captureScreenshot', params)
        raw = result.get('data') or ''
        if not raw:
            raise CdpError('浏览器未返回截图数据')
        return (base64.b64decode(raw), int(round(geometry['width'])),
                int(round(geometry['height'])))

    def close(self):
        """关闭本标签页（GET /json/close/<targetId>）。"""
        try:
            self._ws.close()
        except Exception:
            pass
        try:
            self.client._http('/json/close/%s' % self.target_id)
        except Exception:
            pass
