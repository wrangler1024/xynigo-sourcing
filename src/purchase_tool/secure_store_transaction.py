# -*- coding: utf-8 -*-
"""Rollback guard for local secure-store mutations.

Keychain and DPAPI do not share a transaction with config.json.  This guard
captures the previous secure value, marks the mutation boundary before the
write starts, and restores the snapshot if any later step fails.
"""

from __future__ import annotations


class SecureStoreRollbackError(RuntimeError):
    code = 'secure_store_rollback_failed'


class SecureStoreTransaction(object):
    def __init__(self, loader, restorer, label='安全配置'):
        if not callable(loader) or not callable(restorer):
            raise TypeError('安全存储事务参数无效')
        self.loader = loader
        self.restorer = restorer
        self.label = str(label or '安全配置')[:64]
        self.snapshot = None
        self.mutation_started = False
        self.committed = False

    def __enter__(self):
        self.snapshot = self.loader()
        return self

    def mutate(self, mutation):
        if not callable(mutation):
            raise TypeError('安全存储更新器无效')
        # Mark first because an OS credential write may partially succeed and
        # still return an error.  Rollback must be attempted in that case.
        self.mutation_started = True
        return mutation()

    def commit(self):
        self.committed = True

    def rollback(self):
        if not self.mutation_started or self.committed:
            return False
        try:
            self.restorer(self.snapshot)
        except Exception as exc:
            raise SecureStoreRollbackError(
                '%s回滚失败，请停止继续修改并检查本机安全存储' %
                self.label) from exc
        self.mutation_started = False
        return True

    def __exit__(self, exception_type, exception, traceback):
        del exception_type, traceback
        if exception is not None and not self.committed:
            try:
                self.rollback()
            except SecureStoreRollbackError as rollback_error:
                raise rollback_error from exception
        return False
