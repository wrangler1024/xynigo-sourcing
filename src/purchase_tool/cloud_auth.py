# -*- coding: utf-8 -*-
"""Cloud login bridge for the local Xynigo executor.

The browser receives only public identity state and the Feishu authorization
URL. Poll tokens and bearer sessions stay in the local Python process; the
final session is persisted with macOS Keychain or Windows CurrentUser DPAPI.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import HTTPSHandler, Request, build_opener

import certifi

from . import __version__


DEFAULT_AUTH_BASE_URL = 'https://xynigo.samforo.icu'
KEYCHAIN_SERVICE = 'io.xynigo.sourcing.auth'
KEYCHAIN_ACCOUNT = 'xynigo-cloud-session'
TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9_-]{32,256}$')
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_FEISHU_PROXY_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_PROCUREMENT_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_BUYER_ACCOUNT_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_OPERATION_RESULT_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_BUSINESS_LOG_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SYSTEM_LOG_RESPONSE_BYTES = 4 * 1024 * 1024
ALLOWED_LOGIN_HOSTS = frozenset({'accounts.feishu.cn'})


ERROR_MESSAGES = {
    'authentication_required': '请先使用飞书登录',
    'session_invalid': '登录已失效，请重新登录',
    'user_disabled': '当前成员已被停用',
    'tenant_disabled': '当前组织已被停用',
    'user_pending_approval': '账号已识别，等待超级管理员启用',
    'oauth_denied': '已取消飞书授权',
    'oauth_provider_failed': '飞书登录暂时失败，请稍后重试',
    'local_login_invalid': '登录请求无效，请重新发起',
    'local_login_expired': '登录请求已过期，请重新发起',
    'local_login_consumed': '登录请求已使用，请重新发起',
    'cloud_unreachable': '无法连接 Xynigo 云端认证服务',
    'permission_denied': '当前账号没有此功能权限',
    'member_not_found': '成员不存在或不属于当前组织',
    'role_not_found': '角色不存在或不属于当前组织',
    'session_not_found': '登录会话不存在或不属于当前组织',
    'member_status_conflict': '成员状态已变化，请刷新后重试',
    'feishu_member_not_found': '未找到该手机号绑定的可用飞书成员，请检查号码或应用通讯录范围',
    'feishu_member_ineligible': '该飞书成员未激活、已冻结或已离职，不能邀请',
    'feishu_directory_permission_missing': '“小犀代采”尚未开通通讯录成员 ID 与基本信息权限',
    'feishu_directory_unavailable': '飞书通讯录暂不可用，请检查应用权限和通讯录数据范围',
    'member_already_exists': '该飞书成员已经在 Xynigo 成员列表中',
    'cannot_disable_self': '不能停用当前登录账号',
    'cannot_remove_own_super_admin': '不能移除自己的超级管理员角色',
    'super_admin_required': '此操作仅允许超级管理员执行',
    'super_admin_only_permission': '云端服务配置仅允许超级管理员访问',
    'tenant_feishu_not_configured': '组织尚未配置飞书企业应用，请联系超级管理员',
    'tenant_feishu_credential_unavailable': '组织飞书企业应用凭证暂不可用，请联系超级管理员',
    'tenant_feishu_credential_invalid': '组织飞书企业应用凭证无效，请联系超级管理员',
    'tenant_feishu_verification_failed': '飞书企业应用验证失败，请检查 App ID、App Secret 和应用状态',
    'tenant_feishu_response_invalid': '飞书开放平台返回了无效响应，请稍后重试',
    'tenant_feishu_response_too_large': '飞书只读响应超过安全限制',
    'tenant_feishu_proxy_path_denied': '该飞书接口不在执行器只读授权范围内',
    'tenant_feishu_proxy_query_invalid': '飞书只读请求参数无效',
    'tenant_feishu_revision_conflict': '组织飞书配置已被其他窗口更新，请刷新后重试',
    'tenant_feishu_credential_encrypt_failed': '组织飞书凭证暂时无法安全保存',
    'cloud_managed': '该配置已迁移到云端，由超级管理员统一维护',
    'system_role_immutable': '系统角色及系统权限由后端维护，不能修改',
    'role_name_invalid': '角色名称不能为空',
    'role_name_conflict': '当前组织已存在同名角色',
    'role_in_use': '角色已分配给成员，请先解除成员授权',
    'permission_code_invalid': '包含系统未定义的权限码',
    'permission_grant_exceeds_actor': '不能授予当前账号自身不具备的权限',
    'purchase_order_not_found': '未找到该采购单',
    'purchase_order_locked': '采购单已正式提交，不能直接覆盖',
    'purchase_submit_invalid': '采购单未满足正式提交要求',
    'purchase_contract_invalid': '采购单数据未通过云端契约校验',
    'purchase_contract_version_unsupported': (
        '店小秘提单助手与云端采购契约版本不一致，请先更新云端服务'),
    'purchase_claim_selection_required': '请至少选择一张采购单或一条采购明细',
    'purchase_claim_empty': '所选采购单没有可认领的有效明细',
    'purchase_line_not_found': '未找到该采购明细',
    'purchase_line_claim_conflict': '部分采购明细已被其他采购员认领或已进入采购流程',
    'purchase_execution_revision_conflict': '采购分单已被更新，请刷新后重试',
    'purchase_split_no_claimed_lines': '请先认领采购明细，再创建采购分单',
    'purchase_split_line_unavailable': '采购分单包含未由当前采购员认领的明细',
    'purchase_split_resource_site_mismatch': 'Hub 环境、买家号与采购单站点不一致',
    'purchase_split_resource_duplicate': '采购计划不能重复占用相同资源组合',
    'purchase_split_allocation_incomplete': '已认领明细必须全部分配到采购分单',
    'purchase_split_quantity_mismatch': '采购分单数量与采购明细数量不一致',
    'purchase_split_started': '采购分单已进入执行流程，不能整体重建',
    'checkout_idempotency_conflict': '下单请求标识已被其他内容使用，请刷新后重试',
    'checkout_legacy_split_exists': '当前采购单仍有旧分单，请先人工确认迁移',
    'checkout_line_not_found': '下单尝试包含不存在或已失效的采购明细',
    'checkout_line_not_owned': '只能操作本人已认领且未完成的采购明细',
    'checkout_quantity_unavailable': '采购数量已被其他下单尝试占用，请刷新后重试',
    'checkout_resource_site_mismatch': '采购资源与采购单站点不一致',
    'checkout_resource_conflict': 'Hub 环境或买家号已被其他下单尝试占用',
    'checkout_resource_retained': 'Hub 环境或买家号已绑定成功采购批次，当前不可复用',
    'checkout_resource_binding_mismatch': '买家号已绑定其他 Hub 环境，请刷新资源后重试',
    'buyer_account_not_found': '未找到该买家号',
    'buyer_account_unavailable': '买家号当前不可用于本次下单',
    'buyer_account_credential_unavailable': '买家号凭证尚未验证或已经失效',
    'buyer_account_filter_invalid': '买家号筛选条件无效',
    'operation_run_idempotency_conflict': '任务结果标识已被不同数据使用，请人工核对',
    'checkout_attempt_not_found': '未找到该下单尝试',
    'checkout_attempt_not_owned': '只能操作本人创建的下单尝试',
    'checkout_attempt_version_conflict': '下单尝试已被更新，请刷新后重试',
    'checkout_attempt_not_editable': '下单尝试已开始结算，不能再修改组合',
    'checkout_attempt_not_ready': '请先完整绑定同站点 Hub 环境和买家号',
    'checkout_attempt_cannot_abandon': '当前下单尝试不能直接放弃',
    'checkout_payment_state_invalid': '当前下单尝试不能记录付款结果',
    'checkout_payment_conflict': '该下单尝试已记录不同的付款结果',
    'checkout_resource_missing': '付款成功前必须绑定采购资源',
    'checkout_cleanup_state_invalid': '当前下单尝试没有待确认的资源清理动作',
    'purchase_batch_platform_order_conflict': '采购平台订单号已绑定其他批次',
    'purchase_batch_not_found': '未找到该采购批次',
    'purchase_batch_not_owned': '只能回填本人采购批次的物流信息',
    'shipment_tracking_conflict': '物流单号已绑定同批次的其他包裹',
    'shipment_version_conflict': '物流包裹已被更新，请刷新后重试',
    'business_log_not_found': '业务日志不存在或不在当前数据范围',
    'business_log_time_range_invalid': '日志开始时间不能晚于结束时间',
    'system_log_not_found': '系统日志不存在或不在当前租户范围',
    'system_log_time_range_invalid': '系统日志开始时间不能晚于结束时间',
    'credential_store_failed': '无法安全保存登录会话',
    'pairing_code_invalid': '配对码无效，请在云端重新生成',
    'pairing_code_expired': '配对码已过期，请在云端重新生成',
    'pairing_code_consumed': '配对码已经使用，请在云端重新生成',
    'executor_authentication_required': '本地执行器尚未完成设备配对',
    'executor_credential_invalid': '本地执行器设备凭证无效，请重新配对',
    'executor_identity_mismatch': '当前登录用户与本地执行器配对用户不一致',
    'executor_revoked': '本地执行器设备已被撤销，请重新配对',
    'executor_lease_invalid': '本地执行器任务租约无效',
    'executor_lease_expired': '本地执行器任务租约已过期',
    'executor_task_cancel_requested': '云端已请求取消当前任务',
    'config_revision_conflict': '本地配置已变化，请重新读取后再保存',
}


class LocalAuthError(Exception):
    def __init__(self, code, message=None, status=400):
        self.code = str(code or 'auth_failed')
        self.status = int(status)
        super().__init__(message or ERROR_MESSAGES.get(self.code) or '登录失败')


def _validated_token(value):
    value = str(value or '').strip()
    if not TOKEN_PATTERN.fullmatch(value):
        raise LocalAuthError('credential_store_failed')
    return value


class CloudAuthClient(object):
    def __init__(self, base_url=None, timeout=10.0, opener=None):
        base_url = str(base_url or DEFAULT_AUTH_BASE_URL).strip().rstrip('/')
        parsed = urlparse(base_url)
        local_http = parsed.scheme == 'http' and parsed.hostname in {
            '127.0.0.1', 'localhost'}
        if ((parsed.scheme != 'https' and not local_http)
                or not parsed.netloc or not parsed.hostname):
            raise ValueError('云端认证地址必须使用 HTTPS')
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError('云端认证地址不能包含路径、查询或片段')
        self.base_url = base_url
        self.timeout = float(timeout)
        if opener is None:
            context = ssl.create_default_context(cafile=certifi.where())
            opener = build_opener(HTTPSHandler(context=context)).open
        self.opener = opener

    def _request(self, path, method='GET', payload=None, token=None,
                 executor_credential=None,
                 max_response_bytes=MAX_RESPONSE_BYTES,
                 source='local_executor'):
        data = None
        request_id = uuid.uuid4().hex
        headers = {
            'Accept': 'application/json',
            'User-Agent': 'Xynigo-Local-Executor/' + __version__,
            'X-Request-ID': request_id,
            'X-Trace-ID': request_id,
            'X-Xynigo-Source': str(source or 'local_executor')[:64],
            'X-Xynigo-Client-Version': __version__,
        }
        if payload is not None:
            data = json.dumps(payload, separators=(',', ':')).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if token:
            headers['Authorization'] = 'Bearer ' + _validated_token(token)
        if executor_credential:
            headers['X-Xynigo-Executor-Credential'] = _validated_token(
                executor_credential)
        request = Request(
            urljoin(self.base_url + '/', path.lstrip('/')),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            response = self.opener(request, timeout=self.timeout)
            with response:
                raw = response.read(max_response_bytes + 1)
                if len(raw) > max_response_bytes:
                    raise LocalAuthError('cloud_response_invalid', '云端认证响应过大', 502)
                if not raw:
                    return {}
                return json.loads(raw.decode('utf-8'))
        except HTTPError as exc:
            code = 'auth_failed'
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                detail = payload.get('detail') if isinstance(payload, dict) else None
                response_code = (
                    str(payload.get('code') or '').strip()
                    if isinstance(payload, dict) else '')
                if isinstance(detail, dict):
                    code = str(detail.get('code') or code)
                elif (isinstance(detail, list)
                      and path.startswith('/v1/purchase-orders/')):
                    issue_fields = {
                        str(part)
                        for issue in detail
                        if isinstance(issue, dict)
                        for part in issue.get('loc', [])
                    }
                    if issue_fields & {
                            'schemaVersion', 'storeBaseName', 'operatorName'}:
                        code = 'purchase_contract_version_unsupported'
                    else:
                        code = 'purchase_contract_invalid'
                elif response_code:
                    code = response_code
            except Exception:
                pass
            raise LocalAuthError(
                code,
                ERROR_MESSAGES.get(code) or '云端认证请求失败',
                exc.code,
            ) from None
        except (URLError, TimeoutError, OSError):
            raise LocalAuthError('cloud_unreachable', status=503) from None
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise LocalAuthError('cloud_response_invalid', '云端认证响应无效', 502) from None

    def start_login(self):
        payload = self._request('/v1/auth/local/start', method='POST', payload={})
        login_url = str(payload.get('loginUrl') or '').strip()
        poll_token = _validated_token(payload.get('pollToken'))
        parsed = urlparse(login_url)
        expected_hosts = ALLOWED_LOGIN_HOSTS | {
            urlparse(self.base_url).hostname}
        if (parsed.scheme != 'https' or parsed.hostname not in expected_hosts
                or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            raise LocalAuthError('cloud_response_invalid', '云端登录地址无效', 502)
        try:
            expires_in = int(payload.get('expiresIn') or 0)
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in < 60 or expires_in > 600:
            raise LocalAuthError('cloud_response_invalid', '云端登录有效期无效', 502)
        return {
            'loginUrl': login_url,
            'pollToken': poll_token,
            'expiresIn': expires_in,
        }

    def poll_login(self, poll_token):
        return self._request(
            '/v1/auth/local/poll',
            method='POST',
            payload={'pollToken': _validated_token(poll_token)},
        )

    def me(self, session_token):
        return self._request('/v1/auth/me', token=session_token)

    def logout(self, session_token):
        return self._request('/v1/auth/logout', method='POST', token=session_token)

    def local_executor_release_catalog(self, session_token):
        return self._request(
            '/v1/local-executor/releases/latest', token=session_token)

    def download_local_executor_release(
            self, path, session_token, target, *, expected_size,
            expected_hash, progress=None):
        parsed = urlparse(str(path or ''))
        if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                or not re.fullmatch(
                    r'/v1/local-executor/releases/'
                    r'(?:windows-x86_64|macos-arm64)/primary/download',
                    parsed.path)):
            raise LocalAuthError(
                'cloud_response_invalid', '云端安装包下载地址无效', 500)
        expected_size = int(expected_size)
        expected_hash = str(expected_hash or '').strip().lower()
        if (expected_size < 1_000_000 or expected_size > 300 * 1024 * 1024
                or len(expected_hash) != 64
                or any(ch not in '0123456789abcdef' for ch in expected_hash)):
            raise LocalAuthError(
                'cloud_response_invalid', '云端安装包校验信息无效', 500)
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + '.part')
        request_id = uuid.uuid4().hex
        request = Request(
            urljoin(self.base_url + '/', parsed.path.lstrip('/')),
            headers={
                'Accept': 'application/octet-stream',
                'Authorization': 'Bearer ' + _validated_token(session_token),
                'User-Agent': 'Xynigo-Local-Executor/' + __version__,
                'X-Request-ID': request_id,
                'X-Trace-ID': request_id,
                'X-Xynigo-Source': 'local_executor_standard_updater',
                'X-Xynigo-Client-Version': __version__,
            },
            method='GET',
        )
        digest = hashlib.sha256()
        received = 0
        try:
            response = self.opener(request, timeout=max(60.0, self.timeout))
            with response:
                content_length = int(
                    response.headers.get('Content-Length') or 0)
                response_hash = str(
                    response.headers.get('X-Xynigo-Asset-SHA256') or ''
                ).strip().lower()
                if content_length and content_length != expected_size:
                    raise LocalAuthError(
                        'cloud_response_invalid', '云端安装包大小不一致', 502)
                if response_hash and response_hash != expected_hash:
                    raise LocalAuthError(
                        'cloud_response_invalid', '云端安装包摘要不一致', 502)
                with open(partial, 'wb') as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > expected_size:
                            raise LocalAuthError(
                                'cloud_response_invalid',
                                '云端安装包超过可信大小', 502)
                        handle.write(chunk)
                        digest.update(chunk)
                        if progress:
                            progress(received, expected_size)
            if received != expected_size or digest.hexdigest() != expected_hash:
                raise LocalAuthError(
                    'cloud_response_invalid', '云端安装包校验失败', 502)
            os.replace(str(partial), str(target))
            return target
        except HTTPError as exc:
            code = 'session_invalid' if exc.code == 401 else 'cloud_unreachable'
            raise LocalAuthError(code, status=exc.code) from None
        except LocalAuthError:
            raise
        except (URLError, TimeoutError, OSError, ValueError):
            raise LocalAuthError('cloud_unreachable', status=503) from None
        finally:
            if partial.exists():
                try:
                    partial.unlink()
                except OSError:
                    pass

    def admin_request(self, path, session_token, method='GET', payload=None):
        parsed = urlparse(str(path or ''))
        if (parsed.scheme or parsed.netloc or parsed.fragment
                or not parsed.path.startswith('/v1/admin/')):
            raise LocalAuthError(
                'cloud_response_invalid', '云端管理接口地址无效', 500)
        method = str(method or 'GET').upper()
        if method not in ('GET', 'POST', 'PUT', 'DELETE'):
            raise LocalAuthError(
                'cloud_response_invalid', '云端管理接口方法无效', 500)
        return self._request(
            path,
            method=method,
            payload=payload if method in ('POST', 'PUT') else None,
            token=session_token,
        )

    def purchase_request(self, action, session_token, payload):
        paths = {
            'draft': '/v1/purchase-orders/draft',
            'submit': '/v1/purchase-orders/submit',
            'get': '/v1/purchase-orders/get',
        }
        path = paths.get(str(action or ''))
        if path is None:
            raise LocalAuthError(
                'cloud_response_invalid', '云端采购接口动作无效', 500)
        return self._request(
            path,
            method='POST',
            payload=payload,
            token=session_token,
            source='extension_bridge',
        )

    def procurement_workspace_request(
            self, path, session_token, method='GET', payload=None):
        parsed = urlparse(str(path or ''))
        uuid_part = (
            r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
            r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
        )
        order_write = re.fullmatch(
            r'/v1/procurement/orders/' + uuid_part
            + r'/(?:splits|return|checkout-attempts)',
            parsed.path,
        )
        attempt_write = re.fullmatch(
            r'/v1/procurement/checkout-attempts/' + uuid_part
            + r'/(?:revise|begin|abandon|payment-result|cleanup-result)',
            parsed.path,
        )
        shipment_write = re.fullmatch(
            r'/v1/procurement/purchase-batches/' + uuid_part + r'/shipments',
            parsed.path,
        )
        allowed_path = parsed.path in {
            '/v1/procurement/overview',
            '/v1/procurement/orders',
            '/v1/procurement/claims',
            '/v1/procurement/execution/splits',
        } or re.fullmatch(
            r'/v1/procurement/orders/' + uuid_part,
            parsed.path,
        ) or order_write or attempt_write or shipment_write
        if (parsed.scheme or parsed.netloc or parsed.fragment
                or not allowed_path):
            raise LocalAuthError(
                'cloud_response_invalid', '云端采购中心接口地址无效', 500)
        method = str(method or 'GET').upper()
        if method not in ('GET', 'POST'):
            raise LocalAuthError(
                'cloud_response_invalid', '云端采购中心接口方法无效', 500)
        is_write_path = (
            parsed.path == '/v1/procurement/claims'
            or bool(order_write or attempt_write or shipment_write)
        )
        if (is_write_path and method != 'POST') or (
                not is_write_path and method != 'GET'):
            raise LocalAuthError(
                'cloud_response_invalid', '云端采购中心接口方法与地址不匹配', 500)
        return self._request(
            path,
            method=method,
            payload=payload if method == 'POST' else None,
            token=session_token,
            max_response_bytes=MAX_PROCUREMENT_RESPONSE_BYTES,
            source='local_workspace',
        )

    def buyer_account_request(
            self, path, session_token, method='GET', payload=None):
        parsed = urlparse(str(path or ''))
        allowed = parsed.path in {
            '/v1/resources/buyer-accounts',
            '/v1/resources/buyer-accounts/preflight',
            '/v1/resources/buyer-accounts/snapshot',
        }
        if parsed.scheme or parsed.netloc or parsed.fragment or not allowed:
            raise LocalAuthError(
                'cloud_response_invalid', '云端买家号接口地址无效', 500)
        method = str(method or 'GET').upper()
        expected_method = {
            '/v1/resources/buyer-accounts': 'GET',
            '/v1/resources/buyer-accounts/preflight': 'POST',
            '/v1/resources/buyer-accounts/snapshot': 'PUT',
        }[parsed.path]
        if method != expected_method:
            raise LocalAuthError(
                'cloud_response_invalid', '云端买家号接口方法与地址不匹配', 500)
        if expected_method != 'GET' and parsed.query:
            raise LocalAuthError(
                'cloud_response_invalid', '云端买家号同步接口不接受查询参数', 500)
        return self._request(
            path,
            method=method,
            payload=payload if method in ('POST', 'PUT') else None,
            token=session_token,
            max_response_bytes=MAX_BUYER_ACCOUNT_RESPONSE_BYTES,
            source='local_workspace',
        )

    def operation_result_request(
            self, path, session_token, method='PUT', payload=None,
            executor_credential=None):
        parsed = urlparse(str(path or ''))
        allowed = parsed.path in {
            '/v1/operations/environment-creation-runs',
            '/v1/operations/logistics-query-runs',
        }
        if (parsed.scheme or parsed.netloc or parsed.fragment
                or parsed.query or not allowed):
            raise LocalAuthError(
                'cloud_response_invalid', '云端业务结果接口地址无效', 500)
        if str(method or '').upper() != 'PUT':
            raise LocalAuthError(
                'cloud_response_invalid', '云端业务结果接口方法无效', 500)
        return self._request(
            path,
            method='PUT',
            payload=payload,
            token=session_token,
            executor_credential=executor_credential,
            max_response_bytes=MAX_OPERATION_RESULT_RESPONSE_BYTES,
            source='local_executor',
        )

    def feishu_read_request(
            self, session_token, path, query, permission):
        return self._request(
            '/v1/integrations/feishu/read',
            method='POST',
            payload={
                'permission': str(permission or ''),
                'path': str(path or ''),
                'query': {
                    str(key): str(value)
                    for key, value in dict(query or {}).items()
                },
            },
            token=session_token,
            max_response_bytes=MAX_FEISHU_PROXY_RESPONSE_BYTES,
            source='local_executor_feishu_proxy',
        )

    def business_log_request(self, path, session_token):
        parsed = urlparse(str(path or ''))
        allowed_path = parsed.path == '/v1/business-logs' or re.fullmatch(
            r'/v1/business-logs/[0-9a-fA-F]{8}-'
            r'[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
            r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}',
            parsed.path,
        )
        if (parsed.scheme or parsed.netloc or parsed.fragment
                or not allowed_path):
            raise LocalAuthError(
                'cloud_response_invalid', '云端业务日志接口地址无效', 500)
        return self._request(
            path,
            token=session_token,
            max_response_bytes=MAX_BUSINESS_LOG_RESPONSE_BYTES,
            source='local_workspace',
        )

    def system_log_request(self, path, session_token):
        parsed = urlparse(str(path or ''))
        allowed_path = parsed.path == '/v1/system-logs' or re.fullmatch(
            r'/v1/system-logs/[0-9a-fA-F]{8}-'
            r'[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
            r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}',
            parsed.path,
        )
        if (parsed.scheme or parsed.netloc or parsed.fragment
                or not allowed_path):
            raise LocalAuthError(
                'cloud_response_invalid', '云端系统日志接口地址无效', 500)
        return self._request(
            path,
            token=session_token,
            max_response_bytes=MAX_SYSTEM_LOG_RESPONSE_BYTES,
            source='local_workspace',
        )


class MemoryAuthSessionStore(object):
    def __init__(self, token=None):
        self.token = token

    def load(self):
        return self.token

    def save(self, token):
        self.token = _validated_token(token)

    def clear(self):
        self.token = None


class MacKeychainAuthSessionStore(object):
    def __init__(self, runner=subprocess.run, security_bin='/usr/bin/security',
                 account=KEYCHAIN_ACCOUNT, service=KEYCHAIN_SERVICE):
        self.runner = runner
        self.security_bin = security_bin
        self.account = str(account)
        self.service = str(service)

    def load(self):
        proc = self.runner(
            [self.security_bin, 'find-generic-password',
             '-a', self.account, '-s', self.service, '-w'],
            capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        return _validated_token((proc.stdout or '').strip())

    def save(self, token):
        token = _validated_token(token)
        command = (
            'add-generic-password -a %s -s %s -U -X %s\n' %
            (self.account, self.service, token.encode('utf-8').hex()))
        try:
            proc = self.runner(
                [self.security_bin, '-i'], input=command,
                capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            raise LocalAuthError('credential_store_failed') from None
        if proc.returncode != 0:
            raise LocalAuthError('credential_store_failed')

    def clear(self):
        proc = self.runner(
            [self.security_bin, 'delete-generic-password',
             '-a', self.account, '-s', self.service],
            capture_output=True, text=True)
        if proc.returncode not in (0, 44):
            error = (proc.stderr or '').casefold()
            if 'could not be found' not in error:
                raise LocalAuthError('credential_store_failed')


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.wintypes.DWORD),
                ('pbData', ctypes.POINTER(ctypes.c_byte))]


def _as_blob(data):
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_protect(data):
    if os.name != 'nt':
        raise LocalAuthError('credential_store_failed')
    source, source_buffer = _as_blob(data)
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    crypt32.CryptProtectData.restype = ctypes.wintypes.BOOL
    if not crypt32.CryptProtectData(
            ctypes.byref(source), 'Xynigo cloud session', None, None,
            None, 0x01, ctypes.byref(target)):
        raise LocalAuthError('credential_store_failed')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))
        del source_buffer


def _dpapi_unprotect(data):
    if os.name != 'nt':
        raise LocalAuthError('credential_store_failed')
    source, source_buffer = _as_blob(data)
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.POINTER(ctypes.wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL
    if not crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0x01,
            ctypes.byref(target)):
        raise LocalAuthError('credential_store_failed')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32 = ctypes.windll.kernel32
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))
        del source_buffer


def default_windows_session_path():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise LocalAuthError('credential_store_failed')
    return Path(base) / 'Xynigo' / 'credentials' / 'cloud-session.bin'


class WindowsDpapiAuthSessionStore(object):
    def __init__(self, path=None, protect_fn=_dpapi_protect,
                 unprotect_fn=_dpapi_unprotect):
        self.path = Path(path) if path else default_windows_session_path()
        self.protect = protect_fn
        self.unprotect = unprotect_fn

    def load(self):
        if not self.path.is_file():
            return None
        try:
            return _validated_token(
                self.unprotect(self.path.read_bytes()).decode('utf-8'))
        except LocalAuthError:
            raise
        except Exception:
            raise LocalAuthError('credential_store_failed') from None

    def save(self, token):
        encrypted = self.protect(_validated_token(token).encode('utf-8'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix='.cloud-session-', suffix='.tmp', dir=str(self.path.parent))
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(fd, 'wb') as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise LocalAuthError('credential_store_failed') from None

    def clear(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def system_auth_session_store():
    if sys.platform == 'darwin':
        return MacKeychainAuthSessionStore()
    if os.name == 'nt':
        return WindowsDpapiAuthSessionStore()
    return MemoryAuthSessionStore()


def _public_identity(payload):
    if not isinstance(payload, dict):
        raise LocalAuthError('cloud_response_invalid', '云端用户信息无效', 502)
    user = payload.get('user')
    tenant = payload.get('tenant')
    roles = payload.get('roles')
    permissions = payload.get('permissions')
    if not isinstance(user, dict) or not isinstance(tenant, dict):
        raise LocalAuthError('cloud_response_invalid', '云端用户信息无效', 502)
    if not isinstance(roles, list) or not isinstance(permissions, list):
        raise LocalAuthError('cloud_response_invalid', '云端权限信息无效', 502)
    user_id = str(user.get('id') or '').strip()
    name = str(user.get('name') or '').strip()
    if not user_id or not name or user.get('status') != 'active':
        raise LocalAuthError('cloud_response_invalid', '云端用户状态无效', 502)
    return {
        'user': {
            'id': user_id,
            'name': name[:255],
            'avatarUrl': str(user.get('avatarUrl') or '')[:2048],
            'status': 'active',
        },
        'tenant': {
            'id': str(tenant.get('id') or ''),
            'name': str(tenant.get('name') or '')[:255],
        },
        'roles': sorted(set(str(item) for item in roles if item)),
        'permissions': sorted(set(str(item) for item in permissions if item)),
    }


class LocalAuthService(object):
    def __init__(self, client=None, store=None, refresh_interval=30.0,
                 clock=time.monotonic):
        self.client = client or CloudAuthClient(
            os.environ.get('XYNIGO_AUTH_BASE_URL') or DEFAULT_AUTH_BASE_URL)
        self.store = store or system_auth_session_store()
        self.refresh_interval = float(refresh_interval)
        self.clock = clock
        self.lock = threading.RLock()
        self.storage_error = None
        try:
            stored_token = self.store.load()
            self.session_token = (
                _validated_token(stored_token) if stored_token else None)
        except LocalAuthError as exc:
            self.session_token = None
            try:
                self.store.clear()
            except Exception:
                self.storage_error = str(exc)
        self.identity = None
        self.last_verified = 0.0
        self.pending = None

    @staticmethod
    def _status(authenticated=False, identity=None, cloud_reachable=True,
                code='', message='', login_pending=False):
        return {
            'authenticated': bool(authenticated),
            'cloudReachable': bool(cloud_reachable),
            'identity': identity if authenticated else None,
            'code': code,
            'message': message,
            'loginPending': bool(login_pending and not authenticated),
        }

    def _pending_login_active(self):
        if not self.pending:
            return False
        try:
            active = (
                bool(self.pending.get('loginUrl'))
                and self.clock() < float(self.pending['expiresAt'])
            )
        except (KeyError, TypeError, ValueError):
            active = False
        if not active:
            self.pending = None
        return active

    def status(self, force=True):
        with self.lock:
            if self.storage_error:
                return self._status(
                    code='credential_store_failed',
                    message=self.storage_error,
                )
            if not self.session_token:
                return self._status(
                    code='authentication_required',
                    login_pending=self._pending_login_active(),
                )
            now = self.clock()
            if (not force and self.identity is not None
                    and now - self.last_verified < self.refresh_interval):
                return self._status(True, self.identity)
            try:
                identity = _public_identity(self.client.me(self.session_token))
            except LocalAuthError as exc:
                if exc.status in (401, 403):
                    try:
                        self.store.clear()
                    except Exception:
                        self.storage_error = ERROR_MESSAGES[
                            'credential_store_failed']
                        return self._status(
                            code='credential_store_failed',
                            message=self.storage_error,
                        )
                    self.session_token = None
                    self.identity = None
                    self.last_verified = 0.0
                return self._status(
                    cloud_reachable=exc.code != 'cloud_unreachable',
                    code=exc.code,
                    message=str(exc),
                )
            self.identity = identity
            self.last_verified = now
            return self._status(True, identity)

    def install_executor_session(self, token):
        """Install the owner session issued to an authenticated device."""
        token = _validated_token(token)
        with self.lock:
            try:
                self.store.save(token)
            except Exception:
                raise LocalAuthError('credential_store_failed') from None
            self.session_token = token
            self.identity = None
            self.last_verified = 0.0
            self.storage_error = None
        return True

    def start_login(self):
        with self.lock:
            if self.storage_error:
                raise LocalAuthError(
                    'credential_store_failed', self.storage_error, 500)
            if self._pending_login_active():
                return {
                    'started': False,
                    'resumed': True,
                    'loginUrl': self.pending['loginUrl'],
                    'expiresIn': max(
                        1, int(self.pending['expiresAt'] - self.clock())),
                }
            started = self.client.start_login()
            self.pending = {
                'pollToken': started['pollToken'],
                'loginUrl': started['loginUrl'],
                'expiresAt': self.clock() + started['expiresIn'],
            }
            return {
                'started': True,
                'resumed': False,
                'loginUrl': started['loginUrl'],
                'expiresIn': started['expiresIn'],
            }

    def poll_login(self):
        with self.lock:
            if not self.pending:
                raise LocalAuthError('local_login_invalid')
            if self.clock() >= self.pending['expiresAt']:
                self.pending = None
                raise LocalAuthError('local_login_expired', status=410)
            try:
                result = self.client.poll_login(self.pending['pollToken'])
            except LocalAuthError as exc:
                if exc.code != 'cloud_unreachable':
                    self.pending = None
                raise
            if result.get('status') == 'pending':
                return {'status': 'pending'}
            if result.get('status') != 'authenticated':
                self.pending = None
                raise LocalAuthError('cloud_response_invalid', '云端登录状态无效', 502)
            token = _validated_token(result.get('sessionToken'))
            identity = _public_identity(result.get('identity'))
            try:
                self.store.save(token)
            except Exception:
                self.pending = None
                try:
                    self.client.logout(token)
                except Exception:
                    pass
                raise LocalAuthError('credential_store_failed') from None
            self.session_token = token
            self.storage_error = None
            self.identity = identity
            self.last_verified = self.clock()
            self.pending = None
            return {'status': 'authenticated', 'identity': identity}

    def require(self, permission=None, role=None):
        with self.lock:
            state = self.status(force=False)
            if not state['authenticated']:
                code = state.get('code') or 'authentication_required'
                status = 503 if code == 'cloud_unreachable' else 401
                raise LocalAuthError(code, state.get('message') or None, status)
            identity = state['identity']
            if permission and permission not in identity['permissions']:
                raise LocalAuthError('permission_denied', status=403)
            if role and role not in identity['roles']:
                raise LocalAuthError('super_admin_required', status=403)
            return identity

    def admin_request(self, path, method='GET', payload=None):
        with self.lock:
            state = self.status(force=False)
            if not state['authenticated'] or not self.session_token:
                code = state.get('code') or 'authentication_required'
                status = 503 if code == 'cloud_unreachable' else 401
                raise LocalAuthError(code, state.get('message') or None, status)
            try:
                return self.client.admin_request(
                    path,
                    self.session_token,
                    method=method,
                    payload=payload,
                )
            except LocalAuthError as exc:
                if exc.status == 401:
                    try:
                        self.store.clear()
                    except Exception:
                        self.storage_error = ERROR_MESSAGES[
                            'credential_store_failed']
                    self.session_token = None
                    self.identity = None
                    self.last_verified = 0.0
                raise

    def purchase_request(self, action, payload, permission):
        with self.lock:
            identity = self.require(permission)
            if not self.session_token:
                raise LocalAuthError('authentication_required', status=401)
            try:
                result = self.client.purchase_request(
                    action,
                    self.session_token,
                    payload,
                )
            except LocalAuthError as exc:
                if exc.status == 401:
                    try:
                        self.store.clear()
                    except Exception:
                        self.storage_error = ERROR_MESSAGES[
                            'credential_store_failed']
                    self.session_token = None
                    self.identity = None
                    self.last_verified = 0.0
                raise
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise LocalAuthError(
                    'cloud_response_invalid', '云端采购接口响应无效', 502)
            data = result.get('data')
            if not isinstance(data, dict):
                raise LocalAuthError(
                    'cloud_response_invalid', '云端采购接口数据无效', 502)
            return {'identity': identity, 'data': data}

    def procurement_workspace_request(
            self, path, method='GET', payload=None,
            permission='procurement.request.read'):
        with self.lock:
            self.require(permission)
            if not self.session_token:
                raise LocalAuthError('authentication_required', status=401)
            try:
                result = self.client.procurement_workspace_request(
                    path,
                    self.session_token,
                    method=method,
                    payload=payload,
                )
            except LocalAuthError as exc:
                if exc.status == 401:
                    try:
                        self.store.clear()
                    except Exception:
                        self.storage_error = ERROR_MESSAGES[
                            'credential_store_failed']
                    self.session_token = None
                    self.identity = None
                    self.last_verified = 0.0
                raise
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise LocalAuthError(
                    'cloud_response_invalid', '云端采购中心接口响应无效', 502)
            if not isinstance(result.get('data'), dict):
                raise LocalAuthError(
                    'cloud_response_invalid', '云端采购中心接口数据无效', 502)
            return result

    def buyer_account_request(
            self, path, method='GET', payload=None,
            permission='resource.buyer.read'):
        with self.lock:
            self.require(permission)
            parsed = urlparse(str(path or ''))
            include_credentials = (
                method == 'GET'
                and (parse_qs(parsed.query).get('includeCredentials') or [''])[0]
                .strip().casefold() in {'1', 'true', 'yes'}
            )
            if include_credentials:
                self.require('resource.buyer.credential.read')
            if not self.session_token:
                raise LocalAuthError('authentication_required', status=401)
            try:
                result = self.client.buyer_account_request(
                    path,
                    self.session_token,
                    method=method,
                    payload=payload,
                )
            except LocalAuthError as exc:
                if exc.status == 401:
                    try:
                        self.store.clear()
                    except Exception:
                        self.storage_error = ERROR_MESSAGES[
                            'credential_store_failed']
                    self.session_token = None
                    self.identity = None
                    self.last_verified = 0.0
                raise
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise LocalAuthError(
                    'cloud_response_invalid', '云端买家号接口响应无效', 502)
            if not isinstance(result.get('data'), dict):
                raise LocalAuthError(
                    'cloud_response_invalid', '云端买家号接口数据无效', 502)
            return result

    def operation_result_request(
            self, path, payload, permission, executor_credential=None):
        with self.lock:
            self.require(permission)
            if not self.session_token:
                raise LocalAuthError('authentication_required', status=401)
            try:
                result = self.client.operation_result_request(
                    path,
                    self.session_token,
                    method='PUT',
                    payload=payload,
                    executor_credential=executor_credential,
                )
            except LocalAuthError as exc:
                if (
                    exc.status == 401
                    and exc.code not in {
                        'executor_authentication_required',
                        'executor_credential_invalid',
                        'executor_revoked',
                    }
                ):
                    try:
                        self.store.clear()
                    except Exception:
                        self.storage_error = ERROR_MESSAGES[
                            'credential_store_failed']
                    self.session_token = None
                    self.identity = None
                    self.last_verified = 0.0
                raise
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise LocalAuthError(
                    'cloud_response_invalid', '云端业务结果响应无效', 502)
            if not isinstance(result.get('data'), dict):
                raise LocalAuthError(
                    'cloud_response_invalid', '云端业务结果数据无效', 502)
            return result

    def feishu_read_request(self, path, query, permission):
        with self.lock:
            self.require(permission)
            if not self.session_token:
                raise LocalAuthError('authentication_required', status=401)
            result = self.client.feishu_read_request(
                self.session_token, path, query, permission
            )
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise LocalAuthError(
                    'cloud_response_invalid', '云端飞书代理响应无效', 502
                )
            data = result.get('data')
            if not isinstance(data, dict):
                raise LocalAuthError(
                    'cloud_response_invalid', '云端飞书代理数据无效', 502
                )
            return data

    def business_log_request(self, path):
        with self.lock:
            self.require()
            if not self.session_token:
                raise LocalAuthError('authentication_required', status=401)
            try:
                result = self.client.business_log_request(
                    path,
                    self.session_token,
                )
            except LocalAuthError as exc:
                if exc.status == 401:
                    try:
                        self.store.clear()
                    except Exception:
                        self.storage_error = ERROR_MESSAGES[
                            'credential_store_failed']
                    self.session_token = None
                    self.identity = None
                    self.last_verified = 0.0
                raise
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise LocalAuthError(
                    'cloud_response_invalid', '云端业务日志响应无效', 502)
            if not isinstance(result.get('data'), dict):
                raise LocalAuthError(
                    'cloud_response_invalid', '云端业务日志数据无效', 502)
            return result

    def system_log_request(self, path):
        with self.lock:
            self.require('system.runtime_log.read')
            if not self.session_token:
                raise LocalAuthError('authentication_required', status=401)
            try:
                result = self.client.system_log_request(
                    path,
                    self.session_token,
                )
            except LocalAuthError as exc:
                if exc.status == 401:
                    try:
                        self.store.clear()
                    except Exception:
                        self.storage_error = ERROR_MESSAGES[
                            'credential_store_failed']
                    self.session_token = None
                    self.identity = None
                    self.last_verified = 0.0
                raise
            if not isinstance(result, dict) or result.get('ok') is not True:
                raise LocalAuthError(
                    'cloud_response_invalid', '云端系统日志响应无效', 502)
            if not isinstance(result.get('data'), dict):
                raise LocalAuthError(
                    'cloud_response_invalid', '云端系统日志数据无效', 502)
            return result

    def local_executor_release_catalog(self):
        with self.lock:
            self.require()
            if not self.session_token:
                raise LocalAuthError('authentication_required', status=401)
            try:
                result = self.client.local_executor_release_catalog(
                    self.session_token)
            except LocalAuthError as exc:
                if exc.status == 401:
                    self.session_token = None
                    self.identity = None
                    self.last_verified = 0.0
                raise
            if not isinstance(result, dict):
                raise LocalAuthError(
                    'cloud_response_invalid', '云端安装包清单无效', 502)
            return result

    def download_local_executor_release(
            self, path, target, *, expected_size, expected_hash,
            progress=None):
        # Only protect the mutable authentication state while selecting the
        # credential.  Holding this lock for the full installer stream used to
        # make local identity/status requests wait behind a slow download,
        # which desktop launchers surfaced as a misleading connection state.
        with self.lock:
            self.require()
            if not self.session_token:
                raise LocalAuthError('authentication_required', status=401)
            session_token = self.session_token
        try:
            return self.client.download_local_executor_release(
                path,
                session_token,
                target,
                expected_size=expected_size,
                expected_hash=expected_hash,
                progress=progress,
            )
        except LocalAuthError as exc:
            if exc.status == 401:
                with self.lock:
                    # Do not erase a replacement login that completed while
                    # this download was in flight with the previous token.
                    if self.session_token == session_token:
                        self.session_token = None
                        self.identity = None
                        self.last_verified = 0.0
            raise

    def logout(self):
        with self.lock:
            token = self.session_token
            remote_error = None
            try:
                if token:
                    self.client.logout(token)
            except Exception as exc:
                remote_error = exc
            storage_error = None
            try:
                self.store.clear()
            except Exception:
                storage_error = LocalAuthError(
                    'credential_store_failed', status=500)
            self.session_token = None
            self.storage_error = str(storage_error) if storage_error else None
            self.identity = None
            self.last_verified = 0.0
            self.pending = None
            if storage_error:
                raise storage_error
            if remote_error:
                raise remote_error
            return {'loggedOut': True}
