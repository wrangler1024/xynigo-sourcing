# -*- coding: utf-8 -*-
"""Outlook Web 登录与 SHEIN 验证码读取。

本模块固化 2026-08-19 账号5实测链路：
- "Usar la contraseña" 是 span[role=button]，必须鼠标点击；
- 密码路径可能返回"密码登录不可用"，改走辅助邮箱；
- 安全码是 6 个输入框，填满自动提交，没有提交按钮；
- 首次登录可能出现隐私通知、通行密钥和"保持登录"页。
"""
import time

from .verification import extract_shein_code


LOGIN_URL = 'https://login.live.com/'
INBOX_URL = 'https://outlook.live.com/mail/0/inbox'
VISIBLE_INPUT = 'input:not([type=hidden])'


class OutlookError(Exception):
    pass


class OutlookManualActionRequired(OutlookError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class OutlookCodeReader(object):
    def __init__(self, cdp, provider, acknowledge_privacy_notice=False,
                 sleep=time.sleep):
        self.cdp = cdp
        self.provider = provider
        self.acknowledge_privacy_notice = bool(acknowledge_privacy_notice)
        self.sleep = sleep

    @staticmethod
    def _has(page, *needles):
        text = page.inner_text()
        return any(x in text for x in needles)

    def _wait_text(self, page, needles, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = page.inner_text()
            if any(x in text for x in needles):
                return text
            self.sleep(0.5)
        return page.inner_text()

    def _finish_first_login_prompts(self, page):
        text = self._wait_text(page, (
            'Una nota rápida sobre su cuenta de Microsoft',
            '¿Quieres mantener la sesión iniciada?',
            'Configurando la clave de paso', 'Outlook'), timeout=35)
        if 'Una nota rápida sobre su cuenta de Microsoft' in text:
            if not self.acknowledge_privacy_notice:
                raise OutlookManualActionRequired(
                    'microsoft_privacy_notice',
                    '微软首次登录隐私通知需人工确认')
            page.click_text('ACEPTAR')
            text = self._wait_text(page, (
                'Configurando la clave de paso',
                '¿Quieres mantener la sesión iniciada?'), timeout=35)

        # 不创建通行密钥：HubStudio Chromium 无系统安全窗口时
        # 通常会自动返回 PasskeyEnrollResult=user_cancel。
        if 'Configurando la clave de paso' in text:
            deadline = time.time() + 35
            while time.time() < deadline:
                if 'PasskeyEnrollResult=user_cancel' in page.url:
                    break
                if '¿Quieres mantener la sesión iniciada?' in page.inner_text():
                    break
                self.sleep(0.5)
            else:
                raise OutlookManualActionRequired(
                    'passkey_prompt', '请人工取消微软通行密钥创建窗口')

        text = self._wait_text(
            page, ('¿Quieres mantener la sesión iniciada?', 'Outlook'),
            timeout=20)
        if '¿Quieres mantener la sesión iniciada?' in text:
            # 默认不额外保存认证信息；当前会话 Cookie 仍可读邮件。
            page.click_text('No')
            self.sleep(3)

    def _login(self, page, task):
        page.goto(LOGIN_URL, settle_seconds=3)
        if not page.wait_selector(VISIBLE_INPUT, timeout=25):
            raise OutlookError('微软登录页未出现账号输入框')
        page.fill(VISIBLE_INPUT, task.email)
        page.click_text('Siguiente')
        text = self._wait_text(
            page, ('Verifica tu correo', 'Escribe una contraseña'),
            timeout=25)

        if 'Verifica tu correo' in text:
            page.click_text('Usar la contraseña')
            text = self._wait_text(page, ('Escribe una contraseña',), 20)

        if 'Escribe una contraseña' in text:
            if not page.wait_selector('input[type=password]', timeout=15):
                raise OutlookError('微软密码框未出现')
            page.fill('input[type=password]', task.outlook_password)
            page.click_text('Siguiente')
            text = self._wait_text(page, (
                'El inicio de sesión con contraseña no está disponible',
                'Una nota rápida sobre su cuenta de Microsoft',
                '¿Quieres mantener la sesión iniciada?'), timeout=20)
            if 'El inicio de sesión con contraseña no está disponible' in text:
                page.click_text('Enviar un código a', exact=False)
                text = self._wait_text(page, ('Verifica tu correo',), 15)

        if 'Verifica tu correo' in text:
            if not task.auxiliary_email:
                raise OutlookError('微软要求辅助邮箱，任务未提供')
            page.fill(VISIBLE_INPUT, task.auxiliary_email)
            previous_code = self.provider.peek_outlook_security_code()
            page.click_text('Enviar código')
            text = self._wait_text(page, ('Escribe tu código',), 20)
            if 'Escribe tu código' not in text:
                raise OutlookError('微软安全码页未出现')
            code = self.provider.get_outlook_security_code(
                previous_code=previous_code, timeout=120)
            page.focus_code_input()
            page.type_keys(code)
            self.sleep(4)

        self._finish_first_login_prompts(page)

    def _read_latest_shein_code(self, page, timeout=60):
        page.goto(INBOX_URL, settle_seconds=6)
        text = self._wait_text(
            page, ('Verificación de correo de SHEIN',), timeout=timeout)
        if 'Verificación de correo de SHEIN' not in text:
            raise OutlookError('收件箱未找到 SHEIN 验证邮件')
        if hasattr(page, 'click_text_in_ancestor'):
            page.click_text_in_ancestor(
                'Verificación de correo de SHEIN', '[role=option]')
        else:
            page.click_text('Verificación de correo de SHEIN')
        self.sleep(3)
        code = extract_shein_code(page.inner_text())
        if not code:
            raise OutlookError('SHEIN 验证邮件未解析到验证码')
        return code

    def get_shein_code(self, task):
        page = self.cdp.new_page()
        try:
            # 优先复用现有 Outlook 会话；未登录才走完整验证。
            page.goto(INBOX_URL, settle_seconds=5)
            if 'outlook.live.com/mail' not in page.url:
                self._login(page, task)
            return self._read_latest_shein_code(page)
        finally:
            page.close()
