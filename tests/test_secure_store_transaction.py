# -*- coding: utf-8 -*-
import unittest

from purchase_tool.secure_store_transaction import (
    SecureStoreRollbackError,
    SecureStoreTransaction,
)


class MemoryStore(object):
    def __init__(self, value=None):
        self.value = value
        self.restore_calls = []
        self.fail_restore = False

    def load(self):
        return self.value

    def save(self, value):
        self.value = value

    def restore(self, value):
        self.restore_calls.append(value)
        if self.fail_restore:
            raise RuntimeError('simulated rollback failure')
        self.value = value


class SecureStoreTransactionTests(unittest.TestCase):
    def test_later_failure_restores_previous_value(self):
        store = MemoryStore('old-secret')

        with self.assertRaisesRegex(RuntimeError, 'config write failed'):
            with SecureStoreTransaction(
                    store.load, store.restore, '测试凭证') as transaction:
                transaction.mutate(lambda: store.save('new-secret'))
                raise RuntimeError('config write failed')

        self.assertEqual(store.value, 'old-secret')
        self.assertEqual(store.restore_calls, ['old-secret'])

    def test_success_keeps_new_value(self):
        store = MemoryStore('old-secret')

        with SecureStoreTransaction(
                store.load, store.restore, '测试凭证') as transaction:
            transaction.mutate(lambda: store.save('new-secret'))
            transaction.commit()

        self.assertEqual(store.value, 'new-secret')
        self.assertEqual(store.restore_calls, [])

    def test_partial_mutation_error_still_rolls_back(self):
        store = MemoryStore('old-secret')

        def partial_failure():
            store.value = 'partially-written-secret'
            raise RuntimeError('os credential write failed')

        with self.assertRaisesRegex(RuntimeError, 'credential write failed'):
            with SecureStoreTransaction(
                    store.load, store.restore, '测试凭证') as transaction:
                transaction.mutate(partial_failure)

        self.assertEqual(store.value, 'old-secret')

    def test_rollback_failure_is_explicit_and_does_not_expose_values(self):
        store = MemoryStore('old-secret')
        store.fail_restore = True

        with self.assertRaises(SecureStoreRollbackError) as caught:
            with SecureStoreTransaction(
                    store.load, store.restore, '测试凭证') as transaction:
                transaction.mutate(lambda: store.save('new-secret'))
                raise RuntimeError('config write failed')

        message = str(caught.exception)
        self.assertIn('测试凭证回滚失败', message)
        self.assertNotIn('old-secret', message)
        self.assertNotIn('new-secret', message)


if __name__ == '__main__':
    unittest.main()
