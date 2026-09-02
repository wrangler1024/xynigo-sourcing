# -*- coding: utf-8 -*-
"""Execute formal cloud business tasks through the loopback workspace API.

Only credential-free progress snapshots leave the executor.  Buyer passwords,
cookies, proxy links and HubStudio credentials remain inside the local process.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from .redaction import scrub_text


BUSINESS_TASK_TYPES = frozenset({
    'logistics.query.v1',
    'environment.create-bound.v1',
    'environment.create-backup.v1',
    'environment.retry-row.v1',
    'environment.retry-failed.v1',
})
ENVIRONMENT_TERMINAL_STATES = frozenset({
    'done', 'failed', 'stopped', 'rolled_back', 'cleanup_failed',
})
LOGISTICS_TERMINAL_STATES = frozenset({
    'ok', 'fail', 'login', 'inuse', 'stopped',
})


def backup_account_ref(run_key, environment_name):
    """Return a stable credential-free row identity for backup/test runs."""
    digest = hashlib.sha256(
        ('%s\x00%s' % (run_key, environment_name)).encode('utf-8')
    ).hexdigest()
    return 'backup-' + digest[:40]


class OperationExecutionError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = str(code or 'operation_execution_failed')


class LocalOperationExecutor(object):
    """Bridge one leased formal task to the existing local business engine."""

    def __init__(self, rpc_executor, poll_interval=1.0, sleep_fn=time.sleep):
        if not callable(rpc_executor):
            raise ValueError('执行器内部业务接口尚未就绪')
        self.rpc_executor = rpc_executor
        self.poll_interval = max(0.05, float(poll_interval))
        self.sleep = sleep_fn

    def execute(self, task_type, payload, report,
                cancellation_event=None):
        if task_type not in BUSINESS_TASK_TYPES:
            raise OperationExecutionError(
                'executor_capability_missing', '不支持的正式业务任务')
        if not isinstance(payload, dict):
            raise OperationExecutionError(
                'operation_payload_invalid', '正式业务任务参数无效')
        cancellation_event = cancellation_event or threading.Event()
        if task_type == 'logistics.query.v1':
            return self._execute_logistics(
                payload, report, cancellation_event)
        return self._execute_environment(
            task_type, payload, report, cancellation_event)

    def _execute_environment(self, task_type, payload, report,
                             cancellation_event):
        run_key = self._required_text(payload, 'runKey')
        site = self._site(payload)
        total = self._positive_int(payload, 'totalCount')
        group = self._required_text(payload, 'environmentGroup')
        purchase_date = self._required_text(payload, 'purchaseDate')
        verify_count = self._nonnegative_int(
            payload.get('verifySampleCount'), 'verifySampleCount')
        selected_refs = None
        if task_type in (
                'environment.retry-row.v1',
                'environment.retry-failed.v1'):
            raw_refs = payload.get('accountRefs')
            if not isinstance(raw_refs, list) or not raw_refs:
                raise OperationExecutionError(
                    'operation_payload_invalid', '环境重试任务缺少失败账号')
            selected_refs = {
                str(item or '').strip() for item in raw_refs
                if str(item or '').strip()}
            if len(selected_refs) != total:
                raise OperationExecutionError(
                    'operation_payload_invalid', '环境重试账号数量无效')
            start_path = (
                '/api/envbatch/retry-row'
                if task_type == 'environment.retry-row.v1'
                else '/api/envbatch/retry-failed')
            progress_path = '/api/envbatch/progress'
            stop_path = '/api/envbatch/stop'
            start_body = {'operationRunKey': run_key}
            if task_type == 'environment.retry-row.v1':
                start_body['accountId'] = next(iter(selected_refs))
            backup = False
        elif task_type == 'environment.create-bound.v1':
            assignments = payload.get('assignments')
            if not isinstance(assignments, list) or not assignments:
                raise OperationExecutionError(
                    'operation_payload_invalid', '正式建环境任务缺少采购员分配')
            assignment = self._assignment_text(assignments, total)
            cloud_plan_id = str(
                payload.get('cloudPlanId') or payload.get('planRef') or '').strip()
            if not cloud_plan_id:
                raise OperationExecutionError(
                    'operation_payload_invalid', '正式建环境任务缺少云端计划编号')
            raw_cleanup_blocked = payload.get(
                'cleanupBlockedAccountRefs') or []
            if not isinstance(raw_cleanup_blocked, list):
                raise OperationExecutionError(
                    'operation_payload_invalid', '待清理账号引用格式无效')
            cleanup_blocked_refs = []
            for value in raw_cleanup_blocked:
                account_ref = str(value or '').strip()
                if (not 8 <= len(account_ref) <= 128
                        or any(character not in
                               'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-'
                               for character in account_ref)):
                    raise OperationExecutionError(
                        'operation_payload_invalid', '待清理账号引用无效')
                cleanup_blocked_refs.append(account_ref)
            if (len(cleanup_blocked_refs) != len(set(cleanup_blocked_refs))
                    or len(cleanup_blocked_refs) > total):
                raise OperationExecutionError(
                    'operation_payload_invalid', '待清理账号引用数量无效')
            local_plan_id = ''
            plan_accounts = payload.get('planAccounts')
            if plan_accounts is not None:
                if not isinstance(plan_accounts, list) or len(plan_accounts) != total:
                    raise OperationExecutionError(
                        'operation_payload_invalid', '云端解析计划账号数量无效')
                imported = self._request('POST', '/api/envbatch/cloud-plan', {
                    'cloudPlanId': cloud_plan_id,
                    'site': site,
                    'accounts': plan_accounts,
                    'filename': '云端加密解析计划.xlsx',
                })
                returned_cloud_plan_id = str(
                    imported.get('cloudPlanId') or '').strip()
                if returned_cloud_plan_id != cloud_plan_id:
                    raise OperationExecutionError(
                        'operation_payload_invalid', '本地导入的云端计划编号不一致')
                local_plan_id = self._required_text(imported, 'planId')
            else:
                # Temporary compatibility for tasks created before the cloud
                # plan contract was renamed. Their planRef was a local token.
                local_plan_id = cloud_plan_id
            start_path = '/api/envbatch/start'
            progress_path = '/api/envbatch/progress'
            stop_path = '/api/envbatch/stop'
            start_body = {
                'planId': local_plan_id,
                'assignment': assignment,
                'purchaseDate': purchase_date,
                'verifySampleCount': verify_count,
                'confirmWrite': True,
                'site': site,
                'environmentGroup': group,
                'cleanupBlockedAccountRefs': cleanup_blocked_refs,
                'operationRunKey': run_key,
            }
            backup = False
        else:
            mode = str(payload.get('mode') or 'backup').strip().casefold()
            if mode not in ('backup', 'test'):
                raise OperationExecutionError(
                    'operation_payload_invalid', '备用环境任务模式无效')
            start_path = '/api/envbatch/backup/start'
            progress_path = '/api/envbatch/backup/progress'
            stop_path = '/api/envbatch/backup/stop'
            start_body = {
                'buyer': self._required_text(payload, 'buyerLabel'),
                'count': total,
                'type': '测试' if mode == 'test' else '备用',
                'purchaseDate': purchase_date,
                'verifySampleCount': verify_count,
                'confirmWrite': True,
                'site': site,
                'environmentGroup': group,
                'operationRunKey': run_key,
            }
            backup = True
        self._request('POST', start_path, start_body)
        stop_sent = False
        previous = None
        snapshot = {'running': True, 'phase': 'preparing', 'rows': []}
        while True:
            snapshot = self._request('GET', progress_path)
            if cancellation_event.is_set() and not stop_sent:
                try:
                    self._request('POST', stop_path, {})
                except OperationExecutionError:
                    pass
                stop_sent = True
            phase = self._environment_phase(snapshot)
            rows = self._environment_rows(
                snapshot, run_key=run_key,
                purchaser_label=start_body.get('buyer') or '',
                backup=backup, selected_refs=selected_refs)
            completed = sum(
                row['status'] in ('success', 'failed', 'stopped')
                for row in rows)
            current = min(total, completed)
            event = {
                'phase': phase,
                'current': current,
                'total': total,
                'snapshot': {'rows': rows},
            }
            serialized = json.dumps(
                event, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'))
            if serialized != previous:
                self._safe_report(report, **event)
                previous = serialized
            if not bool(snapshot.get('running')):
                break
            self.sleep(self.poll_interval)
        summary = self._environment_summary(snapshot, total, rows)
        if selected_refs is not None:
            summary['ipOkCount'] = 0
            summary['ipTotalCount'] = 0
        return self._terminal_result('environment', summary)

    def _execute_logistics(self, payload, report, cancellation_event):
        run_key = self._required_text(payload, 'runKey')
        site = self._site(payload)
        serials = payload.get('environmentSerials')
        if (not isinstance(serials, list) or not serials
                or any(not str(item or '').strip() for item in serials)):
            raise OperationExecutionError(
                'operation_payload_invalid', '物流查询任务缺少环境序号')
        serials = [str(item).strip() for item in serials]
        query_mode = str(payload.get('queryMode') or 'initial').strip()
        if query_mode == 'initial':
            start_path = '/api/query'
            start_body = {
                'serials': serials,
                'site': site,
                'operationRunKey': run_key,
            }
        elif query_mode == 'single_retry' and len(serials) == 1:
            start_path = '/api/requery'
            start_body = {
                'serial': serials[0],
                'force': bool(payload.get('force')),
                'operationRunKey': run_key,
            }
        elif query_mode == 'failed_retry':
            start_path = '/api/requery-failed'
            start_body = {'operationRunKey': run_key}
        else:
            raise OperationExecutionError(
                'operation_payload_invalid', '物流查询模式或环境数量无效')
        self._request('POST', start_path, start_body)
        total = len(serials)
        stop_sent = False
        previous = None
        snapshot = {'running': True, 'rows': []}
        rows = []
        while True:
            snapshot = self._request('GET', '/api/progress')
            if cancellation_event.is_set() and not stop_sent:
                try:
                    self._request('POST', '/api/stop', {})
                except OperationExecutionError:
                    pass
                stop_sent = True
            rows = self._logistics_rows(snapshot)
            completed = sum(
                row['status'] in LOGISTICS_TERMINAL_STATES for row in rows)
            current = min(total, completed)
            phase = ('logistics.running' if snapshot.get('running')
                     else 'logistics.completed')
            event = {
                'phase': phase,
                'current': current,
                'total': total,
                'snapshot': {'rows': rows},
            }
            serialized = json.dumps(
                event, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'))
            if serialized != previous:
                self._safe_report(report, **event)
                previous = serialized
            if not bool(snapshot.get('running')):
                break
            self.sleep(self.poll_interval)
        summary = self._logistics_summary(total, rows)
        return self._terminal_result('logistics', summary)

    def _request(self, method, path, body=None):
        result = self.rpc_executor({
            'method': method,
            'path': path,
            'body': body if method == 'POST' else None,
        })
        if not isinstance(result, dict):
            raise OperationExecutionError(
                'operation_local_response_invalid', '本地业务接口响应无效')
        status = int(result.get('httpStatus') or 0)
        response = result.get('body')
        if result.get('responseType') != 'json' or not isinstance(response, dict):
            raise OperationExecutionError(
                'operation_local_response_invalid', '本地业务接口未返回 JSON')
        if status < 200 or status >= 300:
            message = scrub_text(response.get('error') or '本地业务接口执行失败')
            inferred_code = (
                'environment_retry_source_expired'
                if '凭证内存已清理' in message
                else 'operation_local_request_failed')
            raise OperationExecutionError(
                str(response.get('code') or inferred_code),
                message[:300])
        return response

    @staticmethod
    def _safe_report(report, **event):
        try:
            report(**event)
        except Exception:
            # A transient progress upload failure must not abandon a local
            # HubStudio write midway. Lease renewal/final finish still retry.
            pass

    @staticmethod
    def _required_text(payload, key):
        value = str(payload.get(key) or '').strip()
        if not value:
            raise OperationExecutionError(
                'operation_payload_invalid', '正式业务任务缺少 %s' % key)
        return value

    @staticmethod
    def _site(payload):
        site = str(payload.get('site') or '').strip().upper()
        if site not in ('MX', 'US'):
            raise OperationExecutionError(
                'operation_payload_invalid', '正式业务任务站点无效')
        return site

    @staticmethod
    def _positive_int(payload, key):
        try:
            value = int(payload.get(key))
        except (TypeError, ValueError):
            value = 0
        if value < 1:
            raise OperationExecutionError(
                'operation_payload_invalid', '正式业务任务数量无效')
        return value

    @staticmethod
    def _nonnegative_int(value, key):
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            parsed = -1
        if parsed < 0:
            raise OperationExecutionError(
                'operation_payload_invalid', '%s 必须是非负整数' % key)
        return parsed

    @staticmethod
    def _assignment_text(assignments, total):
        parts = []
        assigned = 0
        for item in assignments:
            if not isinstance(item, dict):
                raise OperationExecutionError(
                    'operation_payload_invalid', '采购员分配格式无效')
            purchaser = str(item.get('purchaserLabel') or '').strip()
            try:
                count = int(item.get('count'))
            except (TypeError, ValueError):
                count = 0
            if not purchaser or any(mark in purchaser for mark in (',', ':')):
                raise OperationExecutionError(
                    'operation_payload_invalid', '采购员名称包含不支持的分隔符')
            if count < 1:
                raise OperationExecutionError(
                    'operation_payload_invalid', '采购员分配数量无效')
            assigned += count
            parts.append('%d:%s' % (count, purchaser))
        if assigned != total:
            raise OperationExecutionError(
                'operation_payload_invalid', '采购员分配数量与任务总数不一致')
        return ','.join(parts)

    @staticmethod
    def _environment_phase(snapshot):
        phase = str(snapshot.get('phase') or '').strip().casefold()
        allowed = {
            'idle', 'preparing', 'creating', 'ip_checking', 'finalizing',
            'rolling_back', 'completed', 'failed', 'stopped',
        }
        if phase not in allowed:
            phase = 'creating' if snapshot.get('running') else 'completed'
        return 'environment.' + phase

    @staticmethod
    def _environment_rows(
            snapshot, run_key, purchaser_label, backup,
            selected_refs=None):
        ip_by_name = {
            str(item.get('envName') or ''): item
            for item in snapshot.get('ipChecks') or []
            if isinstance(item, dict)
        }
        result = []
        for index, source in enumerate(snapshot.get('rows') or []):
            if not isinstance(source, dict):
                continue
            source_ref = str(source.get('accountId') or '').strip()
            if selected_refs is not None and source_ref not in selected_refs:
                continue
            state = str(source.get('state') or 'pending').strip().casefold()
            status = {
                'done': 'success',
                'failed': 'failed',
                'stopped': 'stopped',
                'rolled_back': 'stopped',
                'cleanup_failed': 'failed',
                'pending': 'queued',
            }.get(state, 'running')
            env_name = str(source.get('envName') or '').strip()
            ip_check = ip_by_name.get(env_name) or {}
            if backup:
                account_ref = backup_account_ref(run_key, env_name)
                account_label = '备用环境-%03d' % (index + 1)
                purchaser = purchaser_label
                completed_steps = (
                    ['environment_created', 'remark_written']
                    if status == 'success' else [])
                current_step = state
            else:
                account_ref = source_ref
                account_label = str(source.get('emailMasked') or '').strip()
                purchaser = str(source.get('buyer') or '').strip()
                completed_steps = [
                    str(item) for item in source.get('completedSteps') or []]
                current_step = str(
                    source.get('errorStep') or state or '').strip()
            result.append({
                'accountRef': account_ref,
                'accountLabel': account_label,
                'purchaserLabel': purchaser,
                'environmentName': env_name,
                'environmentRef': (
                    str(source.get('containerCode'))
                    if source.get('containerCode') not in (None, '') else None),
                'environmentSerial': (
                    str(source.get('serialNumber'))
                    if source.get('serialNumber') not in (None, '') else None),
                'status': status,
                'currentStep': current_step[:64],
                'completedSteps': completed_steps[:20],
                'errorStep': str(source.get('errorStep') or '')[:64],
                'errorSummary': scrub_text(source.get('error') or '')[:300],
                'recoveredExisting': bool(source.get('recoveredExisting')),
                'createdInRun': bool(source.get('createdInRun')),
                'cleanupStatus': str(
                    source.get('cleanupStatus') or 'not_required')[:32],
                'cleanupErrorCode': str(
                    source.get('cleanupErrorCode') or '')[:128],
                'cleanupErrorSummary': scrub_text(
                    source.get('cleanupError') or '')[:300],
                'ipAddress': str(ip_check.get('ip') or '')[:64],
                'ipCountry': str(ip_check.get('country') or '')[:100],
                'ipErrorCode': str(
                    ip_check.get('errorCode') or '')[:128],
                'ipErrorSummary': scrub_text(
                    ip_check.get('error') or '')[:300],
                'ipVerified': (
                    bool(ip_check.get('ok')) if ip_check else None),
            })
        return result

    @staticmethod
    def _logistics_rows(snapshot):
        result = []
        for source in snapshot.get('rows') or []:
            if not isinstance(source, dict):
                continue
            state = str(source.get('state') or 'pending').strip().casefold()
            if state not in LOGISTICS_TERMINAL_STATES | {'pending', 'running'}:
                state = 'fail'
            completed_steps = (
                ['query_completed'] if state == 'ok'
                else ['query_attempted'] if state in LOGISTICS_TERMINAL_STATES
                else [])
            utc_offset = LocalOperationExecutor._utc_offset_minutes(
                source.get('utcOffsetMinutes'))
            result.append({
                'environmentSerial': str(source.get('serial') or '')[:64],
                'environmentName': str(source.get('envName') or '')[:255],
                'status': state,
                'currentStep': ('querying' if state == 'running' else state),
                'completedSteps': completed_steps,
                'platformOrderNo': str(source.get('orderNo') or '')[:160],
                'orderTime': str(source.get('orderTime') or '')[:64],
                'amount': str(source.get('amount') or '')[:64],
                'platformStatus': str(source.get('status') or '')[:100],
                'statusLabel': str(source.get('statusCn') or '')[:100],
                'fulfillmentStage': str(source.get('stage') or '')[:100],
                'trackingNumbers': [
                    str(item)[:200] for item in source.get('tracks') or []],
                'packageNumbers': [
                    str(item)[:200] for item in source.get('pkgs') or []],
                'carrier': str(source.get('carrier') or '')[:100],
                'cancelled': bool(source.get('kanDan')),
                'riskOrder': bool(source.get('riskOrder')),
                'riskSummary': str(source.get('riskMessage') or '')[:300],
                'ipAddress': str(source.get('ip') or '')[:64],
                'timeZone': str(source.get('timeZone') or '')[:100],
                'utcOffsetMinutes': utc_offset,
                'queriedAt': LocalOperationExecutor._local_timestamp(
                    source.get('time'), utc_offset),
                'errorSummary': scrub_text(source.get('error') or '')[:300],
                'screenshotStatus': str(
                    source.get('screenshotState') or '')[:32],
            })
        return result

    @staticmethod
    def _utc_offset_minutes(value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if -840 <= parsed <= 840 else None

    @staticmethod
    def _local_timestamp(value, utc_offset_minutes):
        text = str(value or '').strip()
        if not text:
            return None
        try:
            parsed = datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
            zone = (
                timezone(timedelta(minutes=utc_offset_minutes))
                if utc_offset_minutes is not None else timezone.utc)
            return parsed.replace(tzinfo=zone).isoformat()
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _environment_summary(snapshot, total, rows):
        success = sum(row['status'] == 'success' for row in rows)
        failed = sum(row['status'] == 'failed' for row in rows)
        stopped = sum(row['status'] == 'stopped' for row in rows)
        raw = snapshot.get('summary') or {}
        ip_ok = int(raw.get('ipOk') or 0)
        ip_total = int(raw.get('ipTotal') or 0)
        fatal = scrub_text(snapshot.get('fatalError') or '')[:300]
        if fatal:
            run_status = 'failed'
        elif stopped:
            run_status = 'cancelled'
        elif failed and success:
            run_status = 'partial_failure'
        elif failed:
            run_status = 'failed'
        else:
            run_status = 'completed'
        return {
            'runStatus': run_status,
            'phase': 'environment.' + run_status,
            'progressCompleted': min(total, success + failed + stopped),
            'progressTotal': total,
            'totalCount': total,
            'successCount': success,
            'failedCount': failed,
            'stoppedCount': stopped,
            'cleanupTotal': int(raw.get('cleanupTotal') or 0),
            'cleanupDone': int(raw.get('cleanupDone') or 0),
            'cleanupFailed': int(raw.get('cleanupFailed') or 0),
            'ipOkCount': ip_ok,
            'ipTotalCount': ip_total,
        }

    @staticmethod
    def _logistics_summary(total, rows):
        success = sum(row['status'] == 'ok' for row in rows)
        stopped = sum(row['status'] == 'stopped' for row in rows)
        failed = sum(
            row['status'] in ('fail', 'login', 'inuse') for row in rows)
        if stopped:
            run_status = 'cancelled'
        elif failed and success:
            run_status = 'partial_failure'
        elif failed:
            run_status = 'failed'
        else:
            run_status = 'completed'
        return {
            'runStatus': run_status,
            'phase': 'logistics.' + run_status,
            'progressCompleted': min(total, success + failed + stopped),
            'progressTotal': total,
            'totalCount': total,
            'successCount': success,
            'failedCount': failed,
            'stoppedCount': stopped,
        }

    @staticmethod
    def _terminal_result(kind, summary):
        run_status = summary['runStatus']
        outcome = 'failed' if run_status == 'failed' else 'succeeded'
        code = '%s_%s' % (kind, run_status)
        return outcome, code, summary
