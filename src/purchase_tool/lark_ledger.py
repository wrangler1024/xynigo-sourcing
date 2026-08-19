# -*- coding: utf-8 -*-
"""飞书买家号台账回写适配器。

只更新已知 record_id，不按邮箱自动创建，避免重复记录。Cookie 通过
权限 0600 的临时 JSON 传给 lark-cli，调用后立即清理，不进项目。
"""
import json
import os
import subprocess
import tempfile
import time


BASE_TOKEN = os.environ.get('XYNIGO_LARK_BASE_TOKEN', '')
TABLE_MX = (os.environ.get('XYNIGO_LARK_TABLE_ID_MX') or
            os.environ.get('XYNIGO_LARK_TABLE_ID', ''))
TABLE_US = os.environ.get('XYNIGO_LARK_TABLE_ID_US', '')
REQUIRED_FIELDS = {
    '账号状态': 'select', '绑定环境': 'text', '环境序号': 'number',
    '绑定时间': 'datetime', 'Cookie': 'text',
}


class LarkLedgerError(Exception):
    pass


class LarkLedgerSink(object):
    def __init__(self, lark_bin='lark-cli', base_token=BASE_TOKEN,
                 table_id=None, table_ids=None, profile=None):
        self.lark_bin = lark_bin
        self.base_token = base_token
        self.table_ids = {'MX': TABLE_MX, 'US': TABLE_US}
        if table_id is not None:
            # 旧的单表参数只兼容 MX，避免 US 任务误写到墨西哥台账。
            self.table_ids['MX'] = table_id
        if table_ids:
            unknown = set(table_ids) - {'MX', 'US'}
            if unknown:
                raise LarkLedgerError(
                    '不支持的台账站点：%s' % ', '.join(sorted(unknown)))
            self.table_ids.update(table_ids)
        self.profile = profile
        self._schema_validated = set()

    @staticmethod
    def _cookie_text(cookie):
        value = cookie
        if isinstance(cookie, dict) and 'cookie' in cookie:
            value = cookie['cookie']
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

    def build_payload(self, task, env, cookie):
        serial = env.get('serialNumber')
        if serial is None:
            raise LarkLedgerError('环境缺少 serialNumber，无法回写台账')
        payload = {
            '账号状态': '已绑定',
            '绑定环境': env.get('containerName') or task.env_name,
            '环境序号': int(serial),
            '绑定时间': time.strftime('%Y-%m-%d %H:%M:%S'),
            '首次登录日期': time.strftime('%Y-%m-%d %H:%M:%S'),
            'Cookie': self._cookie_text(cookie),
        }
        if getattr(task, 'buyer', ''):
            payload['采购员'] = task.buyer
        return payload

    def _table_id(self, task):
        site = getattr(task, 'site', 'MX') or 'MX'
        site = str(site).strip().upper()
        if site not in self.table_ids:
            raise LarkLedgerError('不支持的飞书台账站点：%s' % site)
        table_id = self.table_ids.get(site)
        if not table_id:
            raise LarkLedgerError(
                '未配置 %s 站飞书台账；请设置 XYNIGO_LARK_TABLE_ID_%s' %
                (site, site))
        return table_id

    def _argv(self, command, table_id):
        if not self.base_token:
            raise LarkLedgerError(
                '未配置飞书台账；请设置 XYNIGO_LARK_BASE_TOKEN')
        argv = [self.lark_bin, 'base', command,
                '--base-token', self.base_token,
                '--table-id', table_id, '--as', 'user',
                '--format', 'json']
        if self.profile:
            argv += ['--profile', self.profile]
        return argv

    @staticmethod
    def _parse_result(proc):
        raw = (proc.stdout or proc.stderr or '').strip()
        try:
            result = json.loads(raw)
        except Exception:
            raise LarkLedgerError('lark-cli 返回了非 JSON 结果')
        if proc.returncode != 0 or result.get('ok') is not True:
            error = result.get('error') or {}
            raise LarkLedgerError(
                '飞书回写失败：%s' %
                (error.get('message') or error.get('subtype') or '未知错误'))
        return result

    def _validate_schema(self, table_id):
        if table_id in self._schema_validated:
            return
        proc = subprocess.run(
            self._argv('+field-list', table_id),
            capture_output=True, text=True,
            env=dict(os.environ,
                     LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',
                     LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1'))
        result = self._parse_result(proc)
        fields = (result.get('data') or {}).get('fields') or []
        actual = {f.get('name'): f.get('type') for f in fields}
        bad = [name for name, kind in REQUIRED_FIELDS.items()
               if actual.get(name) != kind]
        if bad:
            raise LarkLedgerError('飞书台账字段不匹配：%s' % ', '.join(bad))
        self._schema_validated.add(table_id)

    def __call__(self, task, env, cookie):
        if not task.record_id:
            raise LarkLedgerError('缺少 record_id，不自动创建或猜测台账记录')
        table_id = self._table_id(task)
        self._validate_schema(table_id)
        payload = self.build_payload(task, env, cookie)
        with tempfile.TemporaryDirectory(prefix='purchase-ledger-') as tmp:
            path = os.path.join(tmp, 'payload.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            argv = self._argv('+record-upsert', table_id)
            argv += ['--record-id', task.record_id,
                     '--json', '@payload.json']
            proc = subprocess.run(
                argv, cwd=tmp, capture_output=True, text=True,
                env=dict(os.environ,
                         LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',
                         LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1'))
            return self._parse_result(proc)
