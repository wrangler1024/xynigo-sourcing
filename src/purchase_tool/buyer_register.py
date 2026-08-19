# -*- coding: utf-8 -*-
"""SHEIN 墨西哥/美国买家号注册状态机与 HubStudio 编排。"""
from dataclasses import dataclass, field
import os
import random
import re
import time

from .cdp import CdpClient
from .coupon import CouponInspector
from .outlook_login import (OutlookCodeReader, OutlookError,
                            OutlookManualActionRequired)
from .redaction import mask_email, scrub_text
from .verification import HttpCodeProvider


SHEIN_LOGIN_URL = 'https://www.shein.com.mx/user/auth/login'
SHEIN_LOGIN_URLS = {
    'MX': SHEIN_LOGIN_URL,
    'US': 'https://us.shein.com/user/auth/login',
}
ALIAS_SELECTOR = '#continue-alias-input'
REGISTER_EMAIL_SELECTOR = '#email-pannel-email-input'
REGISTER_PASSWORD_SELECTOR = '#email-pannel-password-input'
SUCCESS_TEXT = 'Después de entrar y verificar el número de celular'
VERIFY_TEXT = 'Introduce el código de verificación enviado a tu correo'
SECURITY_EMAIL_VERIFY_TEXT = 'Por razones de seguridad de tu cuenta'
SECURITY_CODE_LABEL = 'Código de confirmación'
SECURITY_SUBMIT_TEXT = 'ENTREGAR'
SECURITY_RESEND_TEXT = 'Reenviar'
SECURITY_FAILED_TEXT = 'Verificación fallida'
PHONE_VERIFY_TEXT = 'Para garantizar la seguridad de tu cuenta'
REGISTER_TAG = os.environ.get(
    'XYNIGO_REGISTER_TAG', 'SHEIN Mexico Registration')
REGISTER_TAGS = {
    'MX': os.environ.get('XYNIGO_REGISTER_TAG_MX', REGISTER_TAG),
    'US': os.environ.get('XYNIGO_REGISTER_TAG_US', 'SHEIN US Registration'),
}
PROXY_LINK = os.environ.get('XYNIGO_PROXY_LINK', '')
REGISTRATION_TEXT = {
    'MX': {
        'continue': 'CONTINUAR', 'register': 'Regístrate',
        'terms': ('Términos y condiciones',),
        'success': (SUCCESS_TEXT,),
        'verify': (VERIFY_TEXT,),
        'securityEmail': (SECURITY_EMAIL_VERIFY_TEXT,),
        'securityCode': (SECURITY_CODE_LABEL,),
        'securitySubmit': SECURITY_SUBMIT_TEXT,
        'securityFailed': (SECURITY_FAILED_TEXT,),
        'phone': (PHONE_VERIFY_TEXT,),
        'phoneCountry': 'MX +52',
    },
    'US': {
        'continue': 'CONTINUE', 'register': 'Register',
        'terms': ('Terms and Conditions', 'Terms & Conditions'),
        'success': (
            'After login and verify the mobile number',
            'After signing in and verifying your mobile number',
        ),
        'verify': ('Enter the verification code sent to your email',),
        'securityEmail': ('For your account security',),
        'securityCode': ('Confirmation code', 'Verification code'),
        'securitySubmit': 'SUBMIT',
        'securityFailed': ('Verification failed',),
        'phone': ('To ensure the security of your account',),
        'phoneCountry': 'US +1',
    },
}


def normalize_registration_site(value):
    site = str(value or 'MX').strip().upper()
    if site not in ('MX', 'US'):
        raise ValueError('注册站点仅支持 MX（墨西哥）或 US（美国）')
    return site


class RegistrationError(Exception):
    pass


class ManualActionRequired(RegistrationError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(repr=False)
class BuyerRegistrationTask:
    email: str
    shein_password: str = field(repr=False)
    outlook_password: str = field(repr=False)
    auxiliary_email: str = field(default='', repr=False)
    code_api_url: str = field(default='', repr=False)
    env_serial: str = ''
    env_name: str = ''
    record_id: str = ''
    reuse_open: bool = False
    buyer: str = ''
    site: str = 'MX'

    def __post_init__(self):
        self.email = str(self.email or '').strip()
        self.env_serial = str(self.env_serial or '').strip()
        self.env_name = str(self.env_name or '').strip()
        self.site = normalize_registration_site(self.site)
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', self.email):
            raise ValueError('邮箱格式不正确')
        if len(str(self.shein_password or '')) < 6:
            raise ValueError('SHEIN 密码至少 6 位')
        if not self.outlook_password:
            raise ValueError('缺少 Outlook 密码')
        if not (self.env_serial or self.env_name):
            raise ValueError('必须提供 env_serial 或 env_name')
        env_site = re.search(r'-(MX|US)-', self.env_name, re.I)
        if env_site and env_site.group(1).upper() != self.site:
            raise ValueError('环境名站点与注册任务 site 不一致')

    @classmethod
    def from_dict(cls, data):
        env_name = data.get('env_name') or data.get('envName') or ''
        raw_site = data.get('site')
        if not raw_site:
            match = re.search(r'-(MX|US)-', str(env_name), re.I)
            raw_site = match.group(1) if match else 'MX'
        return cls(
            email=data.get('email'),
            shein_password=(data.get('shein_password') or
                            data.get('sheinPassword')),
            outlook_password=(data.get('outlook_password') or
                              data.get('outlookPassword')),
            auxiliary_email=(data.get('auxiliary_email') or
                             data.get('auxiliaryEmail') or ''),
            code_api_url=(data.get('code_api_url') or
                          data.get('codeApiUrl') or ''),
            env_serial=data.get('env_serial') or data.get('envSerial') or '',
            env_name=env_name,
            record_id=data.get('record_id') or data.get('recordId') or '',
            reuse_open=bool(data.get('reuse_open') or
                            data.get('reuseOpen')),
            buyer=str(data.get('buyer') or data.get('采购员') or '').strip(),
            site=raw_site)

    @property
    def safe_name(self):
        return mask_email(self.email)


@dataclass
class RegistrationResult:
    email_masked: str
    state: str
    env_serial: str = ''
    env_name: str = ''
    message: str = ''
    manual_code: str = ''
    site: str = 'MX'


class SheinRegistrationFlow(object):
    """只负责页面流程；建环境/绑号由编排层处理。"""

    def __init__(self, outlook_reader=None, accept_terms=False,
                 max_submit_attempts=3, hydration_wait=3.0,
                 state_timeout=35, sleep=time.sleep, site='MX'):
        self.outlook_reader = outlook_reader
        self.accept_terms = bool(accept_terms)
        self.max_submit_attempts = max(1, min(3, int(max_submit_attempts)))
        self.hydration_wait = hydration_wait
        self.state_timeout = max(0.01, float(state_timeout))
        self.sleep = sleep
        self.site = normalize_registration_site(site)
        self.profile = REGISTRATION_TEXT[self.site]

    @staticmethod
    def _contains_any(text, markers):
        return any(marker in text for marker in markers)

    def _wait_state(self, page, timeout=None):
        timeout = self.state_timeout if timeout is None else timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = page.inner_text()
            shadow_text = (page.shadow_visible_text('gee-captcha')
                           if hasattr(page, 'shadow_visible_text') else '')
            if self._contains_any(text, self.profile['success']):
                return 'success'
            if self._contains_any(text, self.profile['verify']):
                return 'email_verification'
            if self._contains_any(
                    shadow_text, self.profile['securityFailed']):
                return 'security_verification_failed'
            if (self._contains_any(
                    shadow_text, self.profile['securityEmail']) and
                    self._contains_any(
                        shadow_text, self.profile['securityCode'])):
                return 'security_email_verification'
            if self._contains_any(text, self.profile['phone']):
                return 'phone_verification'
            self.sleep(0.5)
        return 'unchanged'

    def _prepare_form(self, page, task):
        page.fill(REGISTER_EMAIL_SELECTOR, task.email)
        page.fill(REGISTER_PASSWORD_SELECTOR, task.shein_password)
        checks = page.check_all_visible()
        if checks['total'] <= 0 or checks['checked'] != checks['total']:
            raise RegistrationError('注册勾选项未全部勾选')
        # 提交前终检：防止 React 水合把已填值重置。
        if page.value(REGISTER_EMAIL_SELECTOR) != task.email:
            raise RegistrationError('注册邮箱被页面水合重置')
        if page.value(REGISTER_PASSWORD_SELECTOR) != task.shein_password:
            raise RegistrationError('注册密码被页面水合重置')

    def _submit_shein_code(self, page, code):
        page.focus_code_input()
        page.type_keys(code)
        state = self._wait_state(page)
        if state != 'success':
            raise RegistrationError('SHEIN 验证码提交后未出现成功信号')

    def _submit_security_code(self, page, code):
        """提交账户安全认证弹窗。

        该弹窗与普通注册验证码不同：输入验证码后不会自动提交，必须
        显式点击 ``ENTREGAR``。
        """
        if hasattr(page, 'focus_shadow_code_input'):
            page.focus_shadow_code_input('gee-captcha')
        else:
            page.focus_code_input()
        page.type_keys(code)
        if hasattr(page, 'click_shadow_text'):
            page.click_shadow_text(
                self.profile['securitySubmit'], 'gee-captcha')
        else:
            page.click_text(self.profile['securitySubmit'])
        state = self._wait_state(page)
        if state == 'security_verification_failed':
            raise RegistrationError('账户安全认证验证码被拒绝，请稍后重试')
        if state != 'success':
            raise RegistrationError('账户安全认证验证码提交后未出现成功信号')

    def run(self, page, task):
        if normalize_registration_site(task.site) != self.site:
            raise RegistrationError('注册流程站点与任务 site 不一致')
        page.goto(SHEIN_LOGIN_URLS[self.site], settle_seconds=6)
        if not page.wait_selector(ALIAS_SELECTOR, timeout=30):
            raise RegistrationError('SHEIN 登录入口未出现')

        if (self._contains_any(page.inner_text(), self.profile['terms'])
                and not self.accept_terms):
            raise ManualActionRequired(
                'shein_terms',
                'SHEIN 页面明示继续即接受条款，需显式 --accept-terms')

        entered_register = False
        for attempt in range(1, 4):
            page.fill(ALIAS_SELECTOR, task.email)
            page.click_text(self.profile['continue'])
            if page.wait_selector(REGISTER_EMAIL_SELECTOR, timeout=12):
                entered_register = True
                break
            if attempt < 3:
                self.sleep(2)
        if not entered_register:
            raise RegistrationError('未进入 SHEIN 注册面板')
        self.sleep(self.hydration_wait)

        state = 'unchanged'
        for attempt in range(1, self.max_submit_attempts + 1):
            self._prepare_form(page, task)
            page.click_text(self.profile['register'], exact=False)
            state = self._wait_state(page)
            if state == 'phone_verification' and attempt < self.max_submit_attempts:
                # 实测该弹窗可能是可恢复风控分支：关闭后留足时间，
                # 再用真实鼠标重提，后续可进入正常成功页。
                page.click_selector('.sui-dialog__closebtn')
                # 只给弹层卸载/风控状态复位留短暂窗口；重提后的
                # _wait_state 每 0.5 秒轮询，成功即返回，不固定等 20 秒。
                self.sleep(5)
                state = 'unchanged'
                continue
            if state != 'unchanged':
                break
            # 首次提交约 50% 可能静默失败；回读后重试。
            if attempt < self.max_submit_attempts:
                self.sleep(2)

        if state == 'unchanged':
            raise RegistrationError('连续 %d 次提交均无页面状态变化' %
                                    self.max_submit_attempts)
        if state == 'phone_verification':
            raise ManualActionRequired(
                'shein_phone_verification',
                '关闭手机验证弹窗并延时重试后仍要求 %s 短信验证码' %
                self.profile['phoneCountry'])
        if state in ('email_verification', 'security_email_verification'):
            if not self.outlook_reader:
                raise ManualActionRequired(
                    'shein_email_code', 'SHEIN 需邮箱验证码')
            code = self.outlook_reader.get_shein_code(task)
            if state == 'security_email_verification':
                self._submit_security_code(page, code)
            else:
                self._submit_shein_code(page, code)
        return 'success'


class RegistrationOrchestrator(object):
    """低并发注册编排；首版默认串行，失败留窗便于接管。"""

    def __init__(self, hub, accept_terms=False,
                 acknowledge_ms_privacy=False, ledger_sink=None,
                 close_on_success=True, batch_interval=(8, 15),
                 coupon_inspector=None):
        self.hub = hub
        self.accept_terms = bool(accept_terms)
        self.acknowledge_ms_privacy = bool(acknowledge_ms_privacy)
        self.ledger_sink = ledger_sink
        self.close_on_success = bool(close_on_success)
        self.batch_interval = batch_interval
        self.coupon_inspector = coupon_inspector

    @staticmethod
    def env_create_body(name, site='MX'):
        site = normalize_registration_site(site)
        return {
            'containerName': name,
            'tagName': REGISTER_TAGS[site],
            'asDynamicType': 0,
            'proxyTypeName': 'Socks5_通用api',
            'linkCode': PROXY_LINK.replace('{region}', site),
            'ipGetRuleType': 1,
            'coreVersion': 148,
            'advancedBo': {
                'width': 1920, 'height': 1080, 'languageType': 0,
                'webgl': 0, 'canvas': 0, 'audioContext': 0},
        }

    def _resolve_env(self, task):
        tag = REGISTER_TAGS[task.site]
        if task.env_serial:
            env = self.hub.env_by_serial(task.env_serial, tag)
            if not env:
                raise RegistrationError('未找到环境序号 %s' % task.env_serial)
            return env

        existing = {e.get('containerName'): e
                    for e in self.hub.env_list(tag)}
        if task.env_name in existing:
            return existing[task.env_name]
        if not PROXY_LINK:
            raise RegistrationError(
                '新建环境前必须设置 XYNIGO_PROXY_LINK')
        self.hub.env_create(self.env_create_body(task.env_name, task.site))
        deadline = time.time() + 25
        while time.time() < deadline:
            for env in self.hub.env_list(tag):
                if env.get('containerName') == task.env_name:
                    return env
            time.sleep(1)
        raise RegistrationError('新建环境后回读超时')

    def run_one(self, task):
        env = None
        code = None
        try:
            env = self._resolve_env(task)
            code = str(env.get('containerCode'))
            opened = self.hub.open_container_codes()
            if code in opened and not task.reuse_open:
                raise ManualActionRequired(
                    'env_in_use', '环境已打开，为防止操作冲突不自动接管')
            start_data = self.hub.browser_start(code)
            cdp = CdpClient(int(start_data.get('debuggingPort')))
            provider = (HttpCodeProvider(task.code_api_url)
                        if task.code_api_url else None)
            outlook = (OutlookCodeReader(
                cdp, provider,
                acknowledge_privacy_notice=self.acknowledge_ms_privacy)
                if provider else None)
            page = cdp.new_page()
            flow = SheinRegistrationFlow(
                outlook_reader=outlook, accept_terms=self.accept_terms,
                site=task.site)
            flow.run(page, task)

            # 注册成功信号出现后留出落 Cookie 时间，再立即验券；避免过早跳转
            # 打断服务端注册请求。
            time.sleep(10)
            post_errors = []
            try:
                inspector = (self.coupon_inspector or
                             CouponInspector(site=task.site))
                coupon_note = inspector.inspect(page).remark_fragment()
            except Exception as exc:
                coupon_note = '优惠券:验券失败'
                post_errors.append('验券失败：%s' % scrub_text(exc))

            self.hub.container_add_account(
                code, task.email, task.shein_password, site=task.site)
            self.hub.env_update(
                code, env.get('containerName') or task.env_name,
                remark='已注册买家号%s(%s);%s' %
                       (task.safe_name, time.strftime('%Y%m%d'), coupon_note))
            cookie = self.hub.env_export_cookie(code)
            ledger_error = None
            if self.ledger_sink:
                # Cookie 只在内存中交给台账适配器，不放进 result/日志。
                try:
                    self.ledger_sink(task, env, cookie)
                except Exception as exc:
                    ledger_error = scrub_text(exc)
                    post_errors.append('台账回写失败：%s' % ledger_error)
            if self.close_on_success:
                self.hub.browser_stop(code)
            if post_errors:
                return RegistrationResult(
                    task.safe_name, 'partial',
                    str(env.get('serialNumber') or ''),
                    env.get('containerName') or '',
                    '注册/绑号/Cookie 已完成；%s' % '；'.join(post_errors),
                    site=task.site)
            return RegistrationResult(
                task.safe_name, 'success',
                str(env.get('serialNumber') or ''),
                env.get('containerName') or '',
                '注册、验券（%s）、绑号、Cookie 导出已完成' % coupon_note,
                site=task.site)
        except (ManualActionRequired, OutlookManualActionRequired) as exc:
            return RegistrationResult(
                task.safe_name, 'manual',
                str((env or {}).get('serialNumber') or task.env_serial),
                (env or {}).get('containerName') or task.env_name,
                scrub_text(exc), getattr(exc, 'code', 'manual'),
                site=task.site)
        except Exception as exc:
            return RegistrationResult(
                task.safe_name, 'failed',
                str((env or {}).get('serialNumber') or task.env_serial),
                (env or {}).get('containerName') or task.env_name,
                scrub_text(exc), site=task.site)

    def run_batch(self, tasks):
        results = []
        for index, task in enumerate(tasks):
            results.append(self.run_one(task))
            if index + 1 < len(tasks):
                low, high = self.batch_interval
                time.sleep(random.uniform(low, high))
        return results
