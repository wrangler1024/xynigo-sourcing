# -*- coding: utf-8 -*-
"""Strict parsing of Feishu/Lark Base links for local target configuration."""
from dataclasses import dataclass
import re
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit, urlunsplit


TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{8,160}$')
TABLE_RE = re.compile(r'^tbl[A-Za-z0-9_-]{5,157}$')
ALLOWED_HOSTS = ('feishu.cn', 'larksuite.com')


class LarkLinkError(ValueError):
    pass


@dataclass(frozen=True)
class LarkLinkReference:
    kind: str
    document_token: str
    table_id: str
    hostname: str


@dataclass(frozen=True)
class LarkLedgerTargetConfig:
    base_token: str
    table_id: str
    source_kind: str
    source_hostname: str


def _allowed_host(hostname):
    hostname = str(hostname or '').lower().rstrip('.')
    return any(hostname == root or hostname.endswith('.' + root)
               for root in ALLOWED_HOSTS)


def _one_table_id(query):
    values = [str(item or '').strip()
              for item in parse_qs(query, keep_blank_values=True).get(
                  'table', [])]
    values = [item for item in values if item]
    if len(values) != 1 or not TABLE_RE.fullmatch(values[0]):
        raise LarkLinkError('飞书链接必须明确包含一个 tbl 开头的数据表 ID')
    return values[0]


def parse_lark_base_link(value):
    """Parse a direct Base or Wiki URL without performing network access."""
    raw = str(value or '').strip()
    if not raw or len(raw) > 2048 or any(char in raw for char in '\r\n\t'):
        raise LarkLinkError('飞书多维表格链接格式无效')
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise LarkLinkError('飞书多维表格链接格式无效') from exc
    try:
        port = parsed.port
    except ValueError as exc:
        raise LarkLinkError('飞书多维表格链接格式无效') from exc
    if (parsed.scheme.lower() != 'https' or not _allowed_host(parsed.hostname)
            or parsed.username or parsed.password or port not in (None, 443)):
        raise LarkLinkError('只接受飞书或 Lark 官方 HTTPS 多维表格链接')
    segments = [unquote(item) for item in parsed.path.split('/') if item]
    if len(segments) < 2 or segments[0] not in {'base', 'wiki'}:
        raise LarkLinkError('请粘贴 /base/ 或 /wiki/ 类型的多维表格链接')
    token = segments[1].strip()
    if not TOKEN_RE.fullmatch(token):
        raise LarkLinkError('飞书链接中的文档标识格式无效')
    table_id = _one_table_id(parsed.query)
    return LarkLinkReference(
        segments[0], token, table_id, parsed.hostname.lower())


def build_lark_base_link(base_token, table_id, hostname=''):
    """Build a safe user-facing Base link from validated routing data."""
    base_token = str(base_token or '').strip()
    table_id = str(table_id or '').strip()
    hostname = str(hostname or '').strip().lower().rstrip('.')
    if not TOKEN_RE.fullmatch(base_token):
        raise LarkLinkError('飞书 Base Token 格式无效')
    if not TABLE_RE.fullmatch(table_id):
        raise LarkLinkError('飞书数据表 ID 格式无效')
    if hostname and not _allowed_host(hostname):
        raise LarkLinkError('飞书多维表格域名格式无效')
    # Older local configs did not persist the validated source hostname.
    # Feishu's canonical web entry remains a safe backward-compatible target.
    hostname = hostname or 'www.feishu.cn'
    return urlunsplit((
        'https', hostname, '/base/' + quote(base_token, safe=''),
        urlencode({'table': table_id}), ''))


def resolve_lark_ledger_link(value, client=None):
    """Resolve a user-facing link into the Base app_token and table ID.

    Direct ``/base/`` links resolve locally.  ``/wiki/`` links require one
    read-only Wiki node lookup through an authenticated OpenAPI client.
    """
    reference = parse_lark_base_link(value)
    if reference.kind == 'base':
        return LarkLedgerTargetConfig(
            reference.document_token, reference.table_id, 'base',
            reference.hostname)
    if client is None:
        raise LarkLinkError('Wiki 链接需要先配置应用凭证才能自动解析')
    try:
        node = client.get_wiki_node(reference.document_token)
    except Exception as exc:
        raise LarkLinkError('Wiki 链接只读解析失败，请检查应用权限') from exc
    if str(node.get('obj_type') or '').lower() != 'bitable':
        raise LarkLinkError('该 Wiki 链接指向的不是多维表格')
    base_token = str(node.get('obj_token') or '').strip()
    if not TOKEN_RE.fullmatch(base_token):
        raise LarkLinkError('Wiki 节点未返回有效的多维表格标识')
    return LarkLedgerTargetConfig(
        base_token, reference.table_id, 'wiki', reference.hostname)
