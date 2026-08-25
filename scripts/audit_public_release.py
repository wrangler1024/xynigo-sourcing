#!/usr/bin/env python3
"""Fail CI when public source contains known private-operation markers."""
from pathlib import Path
import hashlib
import ipaddress
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    '.git', '.cache', '.venv', 'build', 'dist', '__pycache__',
    '查询日志', '运行数据', '日志', 'logs', 'imports', '导入文件',
}
TEXT_SUFFIXES = {
    '.py', '.md', '.html', '.sh', '.ps1', '.yml', '.yaml', '.toml',
    '.json', '.command', '.txt', '.gitignore',
}
# Hashes keep private provider and infrastructure names out of the public tree
# while still rejecting them if they appear in a future change.
FORBIDDEN_TOKEN_HASHES = {
    '92b9d7a4bfa5a9f756c35a55effcf5f2680f7c333acebec700e220349ce04ea9',
    'bfa24f809d5432b660b612db4796cb349a9a23c7795237a566c89890c470f410',
    '939f1ac066c356835fdd01b60f3c6a721150cec4843525c233b3dd08553819d4',
    '99ad7cddd1136443b0db1f1f6b92a1551a4fb7da677ba5e9de0a4b8e26768686',
    '6a334d4591f57be51388540642b9e55880656d469fa379ce382dd694b20d3080',
}
ALLOWED_PUBLIC_PROVIDER_TOKENS = {'feishu.cn', 'larksuite.com'}
TOKEN_CANDIDATE_PATTERN = re.compile(
    r'(?i)\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b|\b[a-z][a-z0-9]{7,}\b')
CHINESE_LEGAL_ENTITY_PATTERN = re.compile(
    r'[\u4e00-\u9fff]{2,30}(?:有限公司|有限责任公司|集团公司)')
TOKEN_PATTERNS = (
    re.compile(r'\bcli_[a-z0-9]{12,}\b', re.I),
    re.compile(r'\bou_[a-z0-9]{12,}\b', re.I),
    re.compile(r'\brec(?=[a-z0-9]{12,}\b)(?=[a-z0-9]*\d)[a-z0-9]+\b', re.I),
    re.compile(r'(?i)https?://[^\s\"\']+(?:password|passwd|pwd|secret|token|key)='),
)
IP_PATTERN = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
ALLOWED_IPS = {'127.0.0.1', '0.0.0.0'}
DOCUMENTATION_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '192.0.2.0/24',
    '198.51.100.0/24',
    '203.0.113.0/24',
))


def is_allowed_ip(value):
    if value in ALLOWED_IPS:
        return True
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in network for network in DOCUMENTATION_NETWORKS)


def iter_text_files():
    for path in ROOT.rglob('*'):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name == '.gitignore':
            yield path


def main():
    findings = []
    for path in iter_text_files():
        text = path.read_text(encoding='utf-8-sig', errors='replace')
        rel = path.relative_to(ROOT)
        for token in TOKEN_CANDIDATE_PATTERN.findall(text):
            if token.lower() in ALLOWED_PUBLIC_PROVIDER_TOKENS:
                continue
            digest = hashlib.sha256(token.lower().encode('utf-8')).hexdigest()
            if digest in FORBIDDEN_TOKEN_HASHES:
                findings.append('%s: forbidden private provider marker' % rel)
        if CHINESE_LEGAL_ENTITY_PATTERN.search(text):
            findings.append('%s: Chinese legal-entity name' % rel)
        for pattern in TOKEN_PATTERNS:
            for match in pattern.finditer(text):
                if 'example.test' in match.group(0).lower():
                    continue
                findings.append('%s: forbidden identifier or credential URL' % rel)
        for ip in IP_PATTERN.findall(text):
            if not is_allowed_ip(ip):
                findings.append('%s: non-local IP address %s' % (rel, ip))
    if findings:
        print('\n'.join(sorted(set(findings))))
        return 1
    print('Public-source safety audit passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
