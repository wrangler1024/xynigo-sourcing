# -*- coding: utf-8 -*-
import unittest

from purchase_tool.buyer_register import (
    BuyerRegistrationTask, ManualActionRequired,
    RegistrationOrchestrator, SheinRegistrationFlow,
    REGISTER_EMAIL_SELECTOR, REGISTER_PASSWORD_SELECTOR,
    PHONE_VERIFY_TEXT, SECURITY_CODE_LABEL, SECURITY_EMAIL_VERIFY_TEXT,
    SECURITY_SUBMIT_TEXT, SUCCESS_TEXT, VERIFY_TEXT)


class FakePage(object):
    def __init__(self, verification=False, security_verification=False,
                 phone_verification=False, persistent_phone=False):
        self.text = ''
        self.values = {}
        self.submit_count = 0
        self.verification = verification
        self.security_verification = security_verification
        self.phone_verification = phone_verification
        self.persistent_phone = persistent_phone
        self.phone_close_count = 0
        self.typed_code = ''
        self.security_submit_count = 0

    def goto(self, _url, settle_seconds=0):
        self.text = (
            'Identifícate/Regístrate\nTérminos y condiciones')

    def wait_selector(self, _selector, timeout=0):
        return True

    def fill(self, selector, value):
        self.values[selector] = value

    def value(self, selector):
        return self.values.get(selector)

    def inner_text(self):
        return self.text

    def shadow_visible_text(self, _host):
        return self.text if self.security_verification else ''

    def check_all_visible(self):
        return {'total': 7, 'checked': 7}

    def click_text(self, text, exact=True):
        if text == 'CONTINUAR':
            self.text = '注册面板'
        elif text.startswith('Regístrate'):
            self.submit_count += 1
            if (self.phone_verification and
                    (self.persistent_phone or self.submit_count == 1)):
                self.text = PHONE_VERIFY_TEXT
            elif self.verification:
                self.text = VERIFY_TEXT
            elif self.security_verification:
                self.text = (SECURITY_EMAIL_VERIFY_TEXT + '\n' +
                             SECURITY_CODE_LABEL + '\n' +
                             SECURITY_SUBMIT_TEXT)
            elif self.submit_count >= 2:
                self.text = SUCCESS_TEXT
        elif text == SECURITY_SUBMIT_TEXT:
            self.security_submit_count += 1
            self.text = SUCCESS_TEXT

    def focus_code_input(self):
        pass

    def focus_shadow_code_input(self, _host):
        pass

    def click_selector(self, selector):
        if selector == '.sui-dialog__closebtn':
            self.phone_close_count += 1
            self.text = '注册面板'

    def click_shadow_text(self, text, _host):
        self.click_text(text)

    def type_keys(self, value):
        self.typed_code = value
        if not self.security_verification:
            self.text = SUCCESS_TEXT


class FakeOutlook(object):
    def get_shein_code(self, _task):
        return '654321'


def task():
    return BuyerRegistrationTask(
        email='buyer123@outlook.com', shein_password='shein-pass',
        outlook_password='mail-pass', auxiliary_email='aux@example.test',
        code_api_url='https://example.test/get?key=secret', env_serial='1002')


class RegistrationFlowTests(unittest.TestCase):
    def test_terms_require_explicit_flag(self):
        with self.assertRaises(ManualActionRequired) as ctx:
            SheinRegistrationFlow(
                accept_terms=False, sleep=lambda _: None).run(FakePage(), task())
        self.assertEqual(ctx.exception.code, 'shein_terms')

    def test_silent_first_submit_retries(self):
        page = FakePage()
        result = SheinRegistrationFlow(
            accept_terms=True, state_timeout=0.01,
            sleep=lambda _: None).run(page, task())
        self.assertEqual(result, 'success')
        self.assertEqual(page.submit_count, 2)
        self.assertEqual(page.values[REGISTER_EMAIL_SELECTOR], task().email)
        self.assertEqual(page.values[REGISTER_PASSWORD_SELECTOR],
                         task().shein_password)

    def test_email_verification_uses_outlook_reader(self):
        page = FakePage(verification=True)
        result = SheinRegistrationFlow(
            outlook_reader=FakeOutlook(), accept_terms=True,
            state_timeout=0.01, sleep=lambda _: None).run(page, task())
        self.assertEqual(result, 'success')
        self.assertEqual(page.typed_code, '654321')

    def test_security_email_verification_clicks_submit(self):
        page = FakePage(security_verification=True)
        result = SheinRegistrationFlow(
            outlook_reader=FakeOutlook(), accept_terms=True,
            state_timeout=0.01, sleep=lambda _: None).run(page, task())
        self.assertEqual(result, 'success')
        self.assertEqual(page.typed_code, '654321')
        self.assertEqual(page.security_submit_count, 1)

    def test_phone_verification_closes_waits_and_retries(self):
        page = FakePage(phone_verification=True)
        result = SheinRegistrationFlow(
            accept_terms=True, state_timeout=0.01,
            sleep=lambda _: None).run(page, task())
        self.assertEqual(result, 'success')
        self.assertEqual(page.phone_close_count, 1)
        self.assertEqual(page.submit_count, 2)

    def test_persistent_phone_verification_requires_manual_takeover(self):
        with self.assertRaises(ManualActionRequired) as ctx:
            SheinRegistrationFlow(
                accept_terms=True, state_timeout=0.01,
                sleep=lambda _: None).run(
                    FakePage(phone_verification=True,
                             persistent_phone=True), task())
        self.assertEqual(ctx.exception.code, 'shein_phone_verification')

    def test_task_repr_and_env_template_do_not_contain_secrets(self):
        item = task()
        self.assertNotIn(item.shein_password, repr(item))
        self.assertNotIn(item.outlook_password, repr(item))
        body = RegistrationOrchestrator.env_create_body('safe-env')
        self.assertEqual(body['tagName'], 'SHEIN Mexico Registration')
        self.assertEqual(body['advancedBo']['width'], 1920)


if __name__ == '__main__':
    unittest.main()
