# -*- coding: utf-8 -*-
"""买家号注册 CLI：默认只校验计划，--apply 才写入真实平台。"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from .buyer_register import BuyerRegistrationTask, RegistrationOrchestrator
from .hub_api import DEFAULT_PORT, HubStudioApi
from .lark_ledger import LarkLedgerSink


def _inside(path, root):
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        return False


def load_tasks(path):
    input_path = Path(path).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[4]
    if _inside(input_path, repo_root):
        raise ValueError('凭证 JSON 不得放在项目目录内，请使用临时目录')
    with input_path.open(encoding='utf-8') as handle:
        raw = json.load(handle)
    if not isinstance(raw, list) or not raw:
        raise ValueError('输入必须是非空 JSON 数组')
    return [BuyerRegistrationTask.from_dict(item) for item in raw]


def build_parser():
    parser = argparse.ArgumentParser(
        prog='python -m purchase_tool register',
        description='SHEIN 墨西哥/美国买家号低并发注册')
    parser.add_argument('--input', required=True,
                        help='项目目录外的运行时凭证 JSON')
    parser.add_argument('--apply', action='store_true',
                        help='执行真实注册；不加时只做脱敏计划校验')
    parser.add_argument('--accept-terms', action='store_true',
                        help='明确确认接受 SHEIN 页面展示的条款')
    parser.add_argument('--acknowledge-ms-privacy', action='store_true',
                        help='允许首次登录时确认微软隐私通知（不创建通行密钥）')
    parser.add_argument('--hub-port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--keep-open', action='store_true',
                        help='注册成功后不关闭环境（默认关闭并归档登录态）')
    parser.add_argument('--write-lark-ledger', action='store_true',
                        help='完成后回写飞书台账；每个任务必须有 record_id')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        tasks = load_tasks(args.input)
    except Exception as exc:
        print('输入校验失败：%s' % exc)
        return 2

    print('计划校验通过：%d 个账号（串行，账号已脱敏）' % len(tasks))
    for task in tasks:
        print('  - %s [%s] -> %s' %
              (task.safe_name, task.site,
               task.env_serial or task.env_name))
    if not args.apply:
        print('dry-run 完成；未调用 HubStudio/SHEIN/Outlook。')
        return 0
    if not args.accept_terms:
        print('拒绝执行：真实注册必须同时传 --accept-terms。')
        return 2
    if args.write_lark_ledger and any(not t.record_id for t in tasks):
        print('拒绝执行：--write-lark-ledger 要求每个任务都有 record_id。')
        return 2
    if args.write_lark_ledger and not shutil.which('lark-cli'):
        print('拒绝执行：本机未安装 lark-cli，无法回写台账。')
        return 2

    hub = HubStudioApi(port=args.hub_port)
    if not hub.ping():
        print('HubStudio 未连接。')
        return 2
    runner = RegistrationOrchestrator(
        hub, accept_terms=True,
        acknowledge_ms_privacy=args.acknowledge_ms_privacy,
        ledger_sink=LarkLedgerSink() if args.write_lark_ledger else None,
        close_on_success=not args.keep_open)
    results = runner.run_batch(tasks)
    failed = False
    for item in results:
        print('%s  %s  环境%s  %s' % (
            item.state.upper(), item.email_masked,
            item.env_serial or '-', item.message))
        if item.manual_code:
            print('  需人工接管：%s' % item.manual_code)
        failed = failed or item.state != 'success'
    return 2 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
