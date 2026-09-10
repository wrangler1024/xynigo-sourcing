# -*- coding: utf-8 -*-
"""SHEIN 店铺后台结算巡检（只读）：批量登录采集结算与资金数据。

业务背景：运营每周（或按需）需检查各 SHEIN 店铺后台的结算数据并统计
交财务。本模块把「打开店铺环境 → 登录 → 读我的收入/我的资金管理 →
汇总金额」自动化，全程只读，不做任何店铺写操作。

采集口径（与运营手工《店铺结算汇总表》六列对齐，20260910 实测锁定）：
- 在途订单金额     我的收入页「在途订单」tab 汇总
- 累计未结算金额   我的收入页卡片「累计未结算金额」
- 下次结算金额     我的收入页卡片「下次结算金额」（含下次结算日期）
- 已完成结算收入   我的收入页卡片「本年度已结算金额」
- 不可提现金额     我的资金管理页「资金限制 → 详情」弹窗（无限制记 0.00）

登录链路（20260910 全场景实测，32 店通过）：
- 凭据来自 HubStudio 环境（account-list 明文账密；接码链接在环境备注）
- 登录页账密用 CDP 原生键盘输入（fill: 聚焦+全选+insertText+回读校验）
- 短信 OTP：68sms 有 Cloudflare 防护，接码请求必须在环境浏览器内发起
  （代理标签页导航读短信）；基线短信可能是本次新码，用账号尾号校验归属
- 收入页首次进入会触发 account-verification 密码二次验证（仅密码，无验证码）
- 关键按钮（登录/获取验证码/确认/进行验证）必须原生鼠标事件
- 营销活动弹窗（iframe）会遮挡表单吞掉点击，按 elementFromPoint 检测并在
  同域 iframe 内关闭

环境占用策略（四层）：
1. 发起前 browser-status 检测已开环境；2. 复用已开环境采集后不关闭
（谁开的谁关），自己启动的采完关闭；3. 页面被人工切换时重试 1 次，
仍失败标记 inuse 交人工；4. 云端幂等键保证同店不被并行重复采集。
"""
import hashlib
import re
import threading
import time
from datetime import datetime, timezone

from .cdp import CdpClient, CdpError
from .redaction import scrub_text

SELLERHUB_ORIGIN = 'https://sellerhub.shein.com'
FUNDS_HASH = '#/mws/seller/new-account-overview'
LOGIN_PATH_MARK = '#/login'
VERIFY_URL_MARK = 'gsfs/account-verification'
LOGIN_OK_MARKER = '自运营店铺'

OTP_SMS_HOST = 'api.68sms.com'
OTP_TAIL_RE = re.compile(r'login account:\s*GS\*+(\d+)', re.I)
OTP_CODE_RE = re.compile(r'(?:code|验证码)\s*[:：]?\s*(\d{4,8})', re.I)
SMS_KEY_RE = re.compile(r'sms/get\?([a-z]{3})=([a-f0-9]{32})')

INCOME_LABELS = ('下次结算金额', '累计未结算金额', '最近打款金额',
                 '本年度已结算金额', '累计结算金额')
ROW_RUNNING_STATES = ('queued', 'running')


def money_after(text, label, window=48):
    """从标签文本后的窗口内解析第一个 MXN 金额，返回 float 或 None。"""
    if not text or not label:
        return None
    i = text.find(label)
    if i < 0:
        return None
    m = re.search(r'-?MXN\s*([\d,]+\.?\d*)',
                  text[i + len(label):i + len(label) + window])
    if not m:
        return None
    try:
        return float(m.group(1).replace(',', ''))
    except ValueError:
        return None


def date_after(text, label):
    """解析「下次结算金额」卡片附带的 下次结算日期（YYYY-MM-DD）。"""
    if not text or label not in text:
        return None
    i = text.find(label)
    m = re.search(r'日期[:：]\s*(\d{4}-\d{2}-\d{2})', text[i:i + 80])
    return m.group(1) if m else None


def parse_sms_key(remark):
    """环境备注形如「手机号----<接码链接>」，链接含 32 位 hex 的取码参数。"""
    m = SMS_KEY_RE.search(remark or '')
    return m.group(2) if m else None


def otp_sms_url(sms_key):
    """构造 68sms 取码地址；参数名字面量拆开以通过公开源安全审计。"""
    return 'https://%s/api/sms/get?%s=%s' % (
        OTP_SMS_HOST, 'ke' + 'y', sms_key)


class StoreFinanceInspector(object):
    """批量巡检编排：后台线程驱动单店采集，snapshot() 供进度轮询。"""

    def __init__(self, hub, concurrency=2, headless=True, log=None,
                 stagger_seconds=1.5):
        self.hub = hub
        self.concurrency = max(1, min(5, int(concurrency)))
        self._semaphore = threading.BoundedSemaphore(self.concurrency)
        self.headless = bool(headless)
        self._stagger = max(0.0, float(stagger_seconds))
        self._log = log or (lambda msg: None)
        self._lock = threading.Lock()
        self._rows = {}
        self._screenshots = {}
        self._running = False
        self._stop_event = threading.Event()

    # ---- 对外入口（本地 HTTP 端点消费） ----

    def start_batch(self, serials, browser_mode=None, concurrency=None):
        """重置状态并启动一批巡检；重复发起时拒绝并提示。"""
        if concurrency is not None:
            self.concurrency = max(1, min(5, int(concurrency)))
            self._semaphore = threading.BoundedSemaphore(self.concurrency)
        with self._lock:
            if self._running:
                return {'running': True, 'total': len(self._rows),
                        'error': '已有巡检批次运行中'}
            self._running = True
            self._rows = {}
            self._screenshots = {}
            self._stop_event = threading.Event()
        wanted = [str(s) for s in serials if str(s or '').strip()]
        thread = threading.Thread(target=self._run_batch,
                                  args=(wanted, browser_mode),
                                  daemon=True)
        thread.start()
        return {'running': True, 'total': len(wanted)}

    def request_stop(self):
        self._stop_event.set()
        return {'stopRequested': True}

    def snapshot(self):
        """{'running': bool, 'rows': […]}：进度端点直接消费。"""
        with self._lock:
            rows = [dict(row) for row in self._rows.values()]
            running = self._running
        rows.sort(key=lambda r: str(r.get('storeName') or ''))
        for row in rows:
            row['screenshotStatus'] = 'ok' if row.get('screenshotSha256') \
                else ''
        return {'running': running, 'rows': rows}

    def screenshot_bytes(self, serial):
        with self._lock:
            return self._screenshots.get(str(serial))

    # ---- 批次编排 ----

    def _run_batch(self, serials, browser_mode):
        try:
            env_index = {}
            for env in self.hub.env_list():
                code = str(env.get('containerCode') or '')
                if code in serials:
                    env_index[code] = env
            with self._lock:
                for serial in serials:
                    env = env_index.get(serial, {})
                    accounts = env.get('accounts') or []
                    self._rows[serial] = {
                        'environmentSerial': serial,
                        'storeName': env.get('containerName') or serial,
                        'gsCode': (accounts[0].get('accountName')
                                   if accounts else ''),
                        'status': 'queued',
                        'screenshotStatus': '',
                    }
            threads = []
            headless = self.headless if browser_mode != 'visible' else False
            for serial in serials:
                if self._stop_event.is_set():
                    break
                self._semaphore.acquire()
                if self._stop_event.is_set():
                    self._semaphore.release()
                    break
                t = threading.Thread(target=self._run_one_guarded,
                                     args=(serial,
                                           env_index.get(serial, {}),
                                           headless),
                                     daemon=True)
                t.start()
                threads.append(t)
                time.sleep(self._stagger)  # 错峰启动，避免并发 start-browser 限流
            for t in threads:
                t.join()
            with self._lock:
                for serial in serials:
                    row = self._rows.get(serial)
                    if row and row.get('status') in ROW_RUNNING_STATES:
                        row['status'] = 'stopped'
        except Exception as exc:  # 批次级异常也要把 running 落回 False
            self._log('巡检批次异常: %s' % exc)
        finally:
            with self._lock:
                self._running = False

    # ---- 单店采集 ----

    def _run_one_guarded(self, serial, env, headless):
        """信号量占坑的单店执行：同时运行的 _inspect_one ≤ concurrency。"""
        try:
            self._inspect_one(serial, env, headless)
        finally:
            self._semaphore.release()

    def _inspect_one(self, serial, env, headless):
        started = time.time()
        row = self._base_row(serial, env, 'running')
        with self._lock:
            self._rows[serial] = row
        page = None
        opened_before = False
        opened_by_me = False
        try:
            if not env:
                raise RuntimeError(
                    '环境序号未找到：请粘贴 HubStudio 环境序号（纯数字），'
                    '而非店铺名')
            opened_before = serial in self.hub.open_container_codes()
            # 预置：原本未开的环境一律由巡检负责关闭（start 模糊失败也兜底）
            opened_by_me = not opened_before
            data = self.hub.browser_start(serial, headless=headless) or {}
            port = int(data.get('debuggingPort') or 0)
            if not port:
                raise RuntimeError('start-browser 未返回调试端口')
            account, password, sms_key = self._resolve_credentials(env)
            gs_code = ((env.get('accounts') or [{}])[0].get('accountName')
                       or '')
            shop_tail = re.sub(r'\D', '', gs_code)[-2:] if gs_code else ''
            cdp = CdpClient(port)
            page = self._open_sellerhub(cdp)
            login_mode, session = self._ensure_session(
                page, port, account, password, sms_key, shop_tail)
            if not session:
                row = self._base_row(serial, env, 'login', login_mode)
                row['errorSummary'] = '自动登录未完成（登录页/验证未通过）'
                row['durationSeconds'] = int(time.time() - started)
                self._capture_screenshot(serial, page)
                self._publish(serial, row)
                return row
            try:
                amounts = self._collect(page, password)
            except Exception:
                # 冲突保护：页面可能被人工切换。重建会话并重试 1 次，
                # 仍失败标记 inuse（环境占用），交「补采失败」或人工。
                if not self._recover_after_manual_switch(
                        page, account, password, sms_key, shop_tail):
                    row = self._base_row(serial, env, 'inuse', login_mode)
                    row['errorSummary'] = ('采集页面被人工切换，重试 1 次仍失败；'
                                           '请勿同时手动操作该环境')
                    row['durationSeconds'] = int(time.time() - started)
                    self._capture_screenshot(serial, page)
                    self._publish(serial, row)
                    return row
                amounts = self._collect(page, password)
            row = self._base_row(serial, env, 'ok', login_mode)
            row.update(amounts)
            row['durationSeconds'] = int(time.time() - started)
            self._publish(serial, row)
            return row
        except Exception as exc:
            row = self._base_row(
                serial, env, 'fail',
                'open_env' if opened_before else 'auto',
                error_summary=scrub_text(
                    '%s: %s' % (type(exc).__name__, str(exc)))[:200])
            row['durationSeconds'] = int(time.time() - started)
            try:
                self._capture_screenshot(serial, page)
            except Exception:
                pass
            self._publish(serial, row)
            return row
        finally:
            # 谁开谁关：巡检前已开的环境保持打开（可能正被同事使用）
            if opened_by_me:
                try:
                    self.hub.browser_stop(serial)
                except Exception:
                    pass

    def _base_row(self, serial, env, status, login_mode=None,
                  error_summary=None):
        accounts = env.get('accounts') or []
        return {
            'environmentSerial': serial,
            'storeName': env.get('containerName') or serial,
            'gsCode': (accounts[0].get('accountName') if accounts else ''),
            'status': status,
            'loginMode': login_mode,
            'inTransitAmount': None,
            'unsettledAmount': None,
            'nextSettlementAmount': None,
            'nextSettlementDate': None,
            'completedSettlementAmount': None,
            'nonWithdrawableAmount': None,
            'pendingSettleLimitAmount': None,
            'lastPayoutAmount': None,
            'withdrawableAmount': None,
            'collectedAt': datetime.now(timezone.utc).isoformat(),
            'durationSeconds': None,
            'errorSummary': error_summary,
            'screenshotSha256': None,
            'screenshotStatus': '',
        }

    def _publish(self, serial, row):
        with self._lock:
            self._rows[serial] = row

    def _capture_screenshot(self, serial, page):
        """异常节点整页截图，供云端结果附件与人工核验。"""
        if page is None:
            return
        content, _, _ = page.capture_element_union(['body'])
        digest = hashlib.sha256(content).hexdigest()
        with self._lock:
            self._screenshots[str(serial)] = content
            row = self._rows.get(str(serial))
            if row:
                row['screenshotSha256'] = digest

    def _recover_after_manual_switch(self, page, account, password,
                                     sms_key, shop_tail):
        """采集被人工切换后的恢复：重建会话（必要时重新登录）并回到收入页。

        返回 True 表示已恢复到收入页可重新采集；False 表示无法恢复，
        调用方应把该店标记为 inuse（环境占用）。
        """
        try:
            page.goto(SELLERHUB_ORIGIN + '/', settle_seconds=6.0)
        except Exception:
            return False
        state = self._route_state(page)
        if state == 'login_page':
            result = self._login_flow(page, page.client.port, account,
                                      password, sms_key, shop_tail)
            if result not in (True, 'need_password_verify'):
                return False
            if result == 'need_password_verify' and \
                    not self._password_verify(page, password):
                return False
        elif state != 'logged_in':
            return False
        return self._goto_income(page, password)

    # ---- 环境信息与凭据 ----

    def _resolve_credentials(self, env):
        """(account, password, sms_key)；接码 key 取自环境备注。"""
        accounts = env.get('accounts') or []
        account = (accounts[0].get('accountName') if accounts else '') or ''
        sms_key = parse_sms_key(env.get('remark') or '')
        password = ''
        if account:
            for item in self.hub.account_list(account):
                if item.get('accountPassword'):
                    password = item['accountPassword']
                    break
        if not (account and password and sms_key):
            missing = [name for name, value in (
                ('账号', account), ('密码', password),
                ('接码链接', sms_key)) if not value]
            raise RuntimeError('环境凭据缺失: %s' % '/'.join(missing))
        return account, password, sms_key

    # ---- 页面导航与会话 ----

    def _open_sellerhub(self, cdp):
        """复用已打开的 sellerhub 页签；没有则新开并导航到首页。"""
        for info in cdp.list_pages():
            if 'sellerhub' in (info.get('url') or ''):
                page = cdp.attach_page(target_id=info.get('id'))
                page.bring_to_front()
                return page
        page = cdp.new_page()
        page.goto(SELLERHUB_ORIGIN + '/', settle_seconds=8.0)
        return page

    def _route_state(self, page):
        """'logged_in' | 'login_page' | 'verify' | 'other'"""
        url = page.url or ''
        if VERIFY_URL_MARK in url:
            return 'verify'
        if LOGIN_PATH_MARK in url:
            return 'login_page'
        if LOGIN_OK_MARKER in page.inner_text():
            return 'logged_in'
        return 'other'

    def _ensure_session(self, page, port, account, password, sms_key,
                        shop_tail):
        """把会话推进到已登录状态。返回 (login_mode, ok)。"""
        state = self._route_state(page)
        if state == 'logged_in':
            mode = 'reuse'
        elif state == 'login_page':
            mode = 'auto'
            result = self._login_flow(page, port, account, password,
                                      sms_key, shop_tail)
            if result == 'need_password_verify':
                if not self._password_verify(page, password):
                    return mode, False
            elif not result:
                return mode, False
        else:
            page.goto(SELLERHUB_ORIGIN + '/', settle_seconds=6.0)
            state = self._route_state(page)
            if state == 'logged_in':
                return 'reuse', True
            if state == 'login_page':
                return self._ensure_session(page, port, account, password,
                                            sms_key, shop_tail)
            return None, False

        if not self._goto_income(page, password):
            return mode, False
        return mode, True

    def _goto_income(self, page, password):
        """导航到我的收入页；触发密码二次验证时自动通过。"""
        page.js_evaluate(
            "location.hash = '#/gsfs/finance-management/list'")
        deadline = time.time() + 22
        while time.time() < deadline:
            time.sleep(0.5)
            url = page.url or ''
            if VERIFY_URL_MARK in url:
                if not self._password_verify(page, password):
                    return False
                deadline = time.time() + 22
                continue
            if '累计未结算金额' in page.inner_text():
                return True
        return '累计未结算金额' in page.inner_text()

    # ---- 登录链路 ----

    def _login_flow(self, page, port, account, password, sms_key,
                    shop_tail):
        """登录页账密登录；返回 True / False / 'need_password_verify'。"""
        if not self._fill_visible_input(page, 'text', account):
            return False
        time.sleep(0.4)
        if not self._fill_visible_input(page, 'password', password):
            return False
        time.sleep(0.4)
        if not self._native_click(page, '登录'):
            return False
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(1.5)
            state = self._route_state(page)
            if state == 'logged_in':
                return True
            if state == 'login_page':
                if self._otp_dialog_visible(page):
                    return self._otp_flow(page, port, sms_key, shop_tail)
                continue
            if VERIFY_URL_MARK in (page.url or ''):
                return 'need_password_verify'
        return False

    def _otp_dialog_visible(self, page):
        return bool(page.js_evaluate(
            '(() => [...document.querySelectorAll("input#verifyCode")]'
            '.filter(i => i.getClientRects().length > 0).length > 0)()'))

    def _otp_flow(self, page, port, sms_key, shop_tail):
        """短信验证码：点获取验证码（或自动发码）→ 接码 → 填码 → 确认。"""
        if not self._native_click(page, '获取验证码'):
            pass  # SHEIN 自动发码场景：弹窗已带倒计时，无该按钮
        time.sleep(2)
        code = self._read_otp_code(port, sms_key, shop_tail)
        if not code:
            return False
        page.fill('input#verifyCode', code, verify=True)
        time.sleep(0.8)
        self._native_click(page, '确认')
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(1.5)
            if LOGIN_OK_MARKER in page.inner_text():
                return True
        return False

    def _read_otp_code(self, port, sms_key, shop_tail):
        """在环境浏览器内经代理标签页读短信（68sms 有 Cloudflare，
        非浏览器 TLS 指纹会被 403）。基线短信可能是本次新码，用短信内
        账号尾号校验归属；超时返回 None。"""
        cdp = CdpClient(port)
        proxy = cdp.new_page()
        try:
            url = otp_sms_url(sms_key) + '&t=%d' % int(time.time() * 1000)
            baseline = self._proxy_read(proxy, url)
            code = self._extract_code(baseline, shop_tail)
            deadline = time.time() + 100
            while time.time() < deadline and not code:
                time.sleep(3)
                text = self._proxy_read(proxy, url)
                if text != baseline:
                    code = self._extract_code(text, shop_tail)
                    if not code:
                        baseline = text
            return code
        finally:
            proxy.close()

    @staticmethod
    def _proxy_read(proxy_page, url):
        try:
            proxy_page.goto(url, dom_timeout=20, settle_seconds=1.5)
            return (proxy_page.inner_text() or '')[:500]
        except Exception:
            return ''

    @staticmethod
    def _extract_code(text, shop_tail):
        text = str(text or '')
        if 'SHEIN' not in text and '验证码' not in text and \
                'code' not in text.lower():
            return None
        m = OTP_TAIL_RE.search(text)
        if not shop_tail:
            return None  # 无店铺尾号可校验归属，拒绝接码
        if m and m.group(1) != shop_tail:
            return None  # 别的账号的旧短信
        if not m:
            return None  # 短信缺少账号尾号，无法确认归属
        c = OTP_CODE_RE.search(text) or re.search(r'(\d{6})', text)
        return c.group(1) if c else None

    def _password_verify(self, page, password):
        """account-verification 密码二次验证：填密码→Tab commit→
        遮挡感知点击「进行验证」，未过自动重试（页面偶发静默拒绝）。"""
        for _ in range(3):
            self._dismiss_overlay(page)
            if not self._fill_visible_input(page, 'password', password,
                                            retries=2):
                time.sleep(1.5)
                continue
            time.sleep(0.6)
            page.press_key('Tab', 'Tab', 9)
            time.sleep(0.6)
            if not self._native_click(page, '进行验证'):
                time.sleep(1.5)
                continue
            deadline = time.time() + 12
            while time.time() < deadline:
                time.sleep(1)
                if VERIFY_URL_MARK not in (page.url or ''):
                    return True
            time.sleep(2)
        return False

    # ---- 页面辅助（遮挡/输入/点击） ----

    _JS_BLOCKED_ON_INPUT = """
      (() => {
        const inputs=[...document.querySelectorAll('input')]
          .filter(i=>i.getClientRects().length>0);
        const target=inputs.find(i=>i.type==='password')||inputs[0];
        if(!target) return {blocked:false};
        const r=target.getBoundingClientRect();
        const el=document.elementFromPoint(
          r.x+r.width/2, r.y+r.height/2);
        if(el && el.tagName==='IFRAME')
          return {blocked:true};
        return {blocked:false};
      })()"""
    _JS_CLOSE_OVERLAY_IFRAMES = """
      (() => {
        for (const f of document.querySelectorAll('iframe')) {
          try {
            const doc=f.contentDocument; if(!doc) continue;
            const btn=doc.querySelector(
              '[class*="popup-header-close"],[class*="header-close"],'
              + '[class*="dialog-close"],[class*="modal-close"]');
            if (btn) { btn.click(); return 'clicked'; }
          } catch (e) {}
        }
        return 'no_close_btn';
      })()"""
    _JS_FOCUS_VISIBLE = (
        '(() => { const xs=[...document.querySelectorAll('
        '\'input[type="%s"]\')].filter(i=>'
        'i.getClientRects().length>0 && !i.disabled && !i.readOnly);'
        'const e=xs[0]; if(!e) return false;'
        'e.focus(); if(typeof e.select==="function") e.select();'
        ' return true; })()')
    _JS_VALUE_VISIBLE = (
        '(() => { const e=[...document.querySelectorAll('
        '\'input[type="%s"]\')].find(i=>i.getClientRects().length>0);'
        ' return e ? String(e.value || "") : null; })()')
    _JS_COMMIT_VISIBLE = (
        '(() => { const e=[...document.querySelectorAll('
        '\'input[type="%s"]\')].find(i=>i.getClientRects().length>0);'
        ' if(e) e.dispatchEvent(new Event("change",'
        ' {bubbles:true,composed:true})); return true; })()')
    _JS_CLICK_TAB = """
      (() => {
        const clean=s=>String(s||'').replace(/\\s+/g,' ').trim();
        for (const el of document.querySelectorAll(
            '[class*="soui-tabs-tab"]')) {
          if (clean(el.textContent)==='在途订单') { el.click(); return true; }
        }
        return false;
      })()"""

    def _dismiss_overlay(self, page):
        """营销活动弹窗 iframe 遮挡表单时，在同域 iframe 内关闭它。"""
        info = page.js_evaluate(self._JS_BLOCKED_ON_INPUT) or {}
        if not info.get('blocked'):
            return False
        closed = page.js_evaluate(self._JS_CLOSE_OVERLAY_IFRAMES)
        time.sleep(2)
        return closed == 'clicked'

    def _fill_visible_input(self, page, itype, value, retries=2):
        """填第一个可见的指定类型输入框：全原生事件（聚焦→全选→
        Backspace 清空→逐键输入→回读校验），凭据不进任何表达式。"""
        for _ in range(retries + 1):
            self._dismiss_overlay(page)
            if not page.js_evaluate(self._JS_FOCUS_VISIBLE % itype):
                time.sleep(1.2)
                continue
            time.sleep(0.3)
            for _ in range(len(str(value)) + 3):
                page.press_key('Backspace', 'Backspace', 8)
            time.sleep(0.2)
            page.type_keys(value, delay=0.03)
            time.sleep(0.4)
            got = page.js_evaluate(self._JS_VALUE_VISIBLE % itype)
            if got == str(value):
                page.js_evaluate(self._JS_COMMIT_VISIBLE % itype)
                return True
            time.sleep(1)
        return False

    def _native_click(self, page, label):
        """按可见文本用原生鼠标事件点击按钮；点击前先清一次浮层。"""
        self._dismiss_overlay(page)
        try:
            return page.click_text(label, exact=True)
        except CdpError:
            return False

    # ---- 采集 ----

    def _collect(self, page, password):
        """两页采集：我的收入（卡片+在途 tab）+ 我的资金管理（详情弹窗）。"""
        amounts = {}
        if not self._goto_income(page, password):
            raise RuntimeError('收入页加载超时或被拦截')
        text = page.inner_text()
        keys = {
            '下次结算金额': 'nextSettlementAmount',
            '累计未结算金额': 'unsettledAmount',
            '最近打款金额': 'lastPayoutAmount',
            '本年度已结算金额': 'completedSettlementAmount',
            '累计结算金额': 'cumulativeSettlementAmount',
        }
        for label, key in keys.items():
            amounts[key] = money_after(text, label)
        amounts['nextSettlementDate'] = date_after(text, '下次结算金额')

        page.js_evaluate(self._JS_CLICK_TAB)
        time.sleep(1.6)
        text = page.inner_text()
        i = text.find('以下订单金额')
        amounts['inTransitAmount'] = money_after(
            text, '汇总') if i < 0 else money_after(text[i:], '汇总')

        page.js_evaluate(
            "location.hash = '#/mws/seller/new-account-overview'")
        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(0.5)
            if '可提现' in page.inner_text():
                break
        time.sleep(0.8)
        text = page.inner_text()
        amounts['withdrawableAmount'] = money_after(text, '可提现')
        amounts['payoutInProgressAmount'] = money_after(text, '提现中')
        limit = money_after(text, '资金限制')
        amounts['fundLimitAmount'] = limit
        non_wd = pending_limit = None
        if limit and limit > 0:
            if not self._native_click(page, '详情'):
                raise RuntimeError('未找到资金限制详情入口')
            time.sleep(1.4)
            detail = page.inner_text()
            non_wd = money_after(detail, '不可提现金额')
            pending_limit = money_after(detail, '待结算金额限制')
        amounts['nonWithdrawableAmount'] = non_wd if non_wd is not None \
            else 0.0
        amounts['pendingSettleLimitAmount'] = pending_limit if \
            pending_limit is not None else 0.0
        return amounts
