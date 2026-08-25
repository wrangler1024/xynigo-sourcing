# -*- coding: utf-8 -*-
"""本地长任务准入、资源预占与 HubStudio 调用闸门。"""
from contextlib import contextmanager
import secrets
import threading
import time


TASK_LABELS = {
    'query': '订单物流查询',
    'register': '买家号注册',
    'env_batch': '买家号建环境',
    'backup_env': '备用/测试环境创建',
}


class TaskConflict(RuntimeError):
    """当前本地执行器已有不兼容任务或目标环境被占用。"""


def environment_resources(envs):
    """把 HubStudio 环境或环境计划转换为不含凭证的资源键。"""
    result = set()
    for env in envs or ():
        if isinstance(env, dict):
            name = env.get('containerName') or env.get('envName')
            code = env.get('containerCode')
        else:
            name = getattr(env, 'env_name', '')
            code = getattr(env, 'container_code', '')
        name = str(name or '').strip()
        code = str(code or '').strip()
        if name:
            result.add('name:' + name.casefold())
        if code:
            result.add('code:' + code)
    return result


class LocalTaskCoordinator(object):
    """进程级任务协调器。

    安全并行只放开物流查询与一种环境创建任务；同类任务、注册任务、
    两种环境创建任务仍保持互斥。任何模式下，同一环境资源都不能重叠。
    """

    SAFE_PAIRS = frozenset({
        frozenset(('query', 'env_batch')),
        frozenset(('query', 'backup_env')),
    })

    def __init__(self, safe_parallel_getter=lambda: False):
        self.safe_parallel_getter = safe_parallel_getter
        self.lock = threading.Lock()
        self.tasks = {}

    def _compatible(self, left, right):
        if left == right:
            return False
        if not bool(self.safe_parallel_getter()):
            return False
        return frozenset((left, right)) in self.SAFE_PAIRS

    @staticmethod
    def _label(kind):
        return TASK_LABELS.get(kind, kind)

    def begin(self, kind, resources=()):
        resources = set(resources or ())
        with self.lock:
            for task in self.tasks.values():
                if not self._compatible(kind, task['kind']):
                    raise TaskConflict(
                        '%s正在进行，请结束后再启动%s' % (
                            self._label(task['kind']), self._label(kind)))
                overlap = resources.intersection(task['resources'])
                if overlap:
                    raise TaskConflict(
                        '目标环境正被%s占用，请等待任务结束' %
                        self._label(task['kind']))
            task_id = '%s-%s' % (kind, secrets.token_hex(6))
            self.tasks[task_id] = {
                'taskId': task_id,
                'kind': kind,
                'resources': resources,
                'startedAt': time.time(),
            }
            return task_id

    def reserve(self, task_id, resources):
        resources = set(resources or ())
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise TaskConflict('任务准入已失效，请重新发起')
            for other_id, other in self.tasks.items():
                if other_id == task_id:
                    continue
                if resources.intersection(other['resources']):
                    raise TaskConflict(
                        '目标环境正被%s占用，请等待任务结束' %
                        self._label(other['kind']))
            task['resources'].update(resources)

    def finish(self, task_id):
        if not task_id:
            return
        with self.lock:
            self.tasks.pop(task_id, None)

    def running(self):
        with self.lock:
            return bool(self.tasks)

    def snapshot(self):
        with self.lock:
            now = time.time()
            return {
                'safeParallel': bool(self.safe_parallel_getter()),
                'running': bool(self.tasks),
                'tasks': [{
                    'taskId': item['taskId'],
                    'kind': item['kind'],
                    'label': self._label(item['kind']),
                    'elapsedSec': int(max(0, now - item['startedAt'])),
                    'resourceCount': len(item['resources']),
                } for item in self.tasks.values()],
            }


class HubRuntimeGate(object):
    """限制 Local API 总并发/请求速率，并串行提交浏览器控制请求。"""

    def __init__(self, max_requests=4, min_request_interval=0.3,
                 sleep_fn=time.sleep, monotonic_fn=time.monotonic):
        self.request_slots = threading.BoundedSemaphore(
            max(1, int(max_requests)))
        self.browser_control = threading.Lock()
        self.min_request_interval = max(0.0, float(min_request_interval))
        self.sleep = sleep_fn
        self.monotonic = monotonic_fn
        self.request_pacing = threading.Lock()
        self.next_request_at = 0.0

    @contextmanager
    def request(self):
        """为一次 Local API 调用分配并发槽并全局错开请求起点。"""
        self.request_slots.acquire()
        try:
            with self.request_pacing:
                now = self.monotonic()
                delay = max(0.0, self.next_request_at - now)
                if delay:
                    self.sleep(delay)
                    now = self.monotonic()
                self.next_request_at = max(
                    now, self.next_request_at) + self.min_request_interval
            yield
        finally:
            self.request_slots.release()

    def defer_requests(self, seconds):
        """收到服务端限流后，让所有共享该闸门的线程共同等待。"""
        delay = max(0.0, float(seconds))
        with self.request_pacing:
            self.next_request_at = max(
                self.next_request_at, self.monotonic() + delay)

    def browser(self):
        return self.browser_control
