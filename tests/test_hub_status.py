# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from purchase_tool.main import HubStatusCache


class FakeHub(object):
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def ping_detail(self):
        self.calls += 1
        return self.responses.pop(0)


class HubStatusCacheTests(unittest.TestCase):
    def test_reuses_status_within_ttl(self):
        hub = FakeHub([(True, ''), (True, '')])
        cache = HubStatusCache(lambda: hub, ttl_seconds=12)
        with patch('purchase_tool.main.time.monotonic', return_value=100):
            self.assertEqual(cache.check(), (True, ''))
        with patch('purchase_tool.main.time.monotonic', return_value=110):
            self.assertEqual(cache.check(), (True, ''))
        self.assertEqual(hub.calls, 1)
        with patch('purchase_tool.main.time.monotonic', return_value=113):
            self.assertEqual(cache.check(), (True, ''))
        self.assertEqual(hub.calls, 2)

    def test_rate_limit_keeps_last_known_connected_state(self):
        hub = FakeHub([
            (True, ''),
            (False, 'HubApiError: code=E010205 rate limited'),
        ])
        cache = HubStatusCache(lambda: hub, ttl_seconds=12)
        with patch('purchase_tool.main.time.monotonic', return_value=100):
            self.assertEqual(cache.check(), (True, ''))
        with patch('purchase_tool.main.time.monotonic', return_value=113):
            self.assertEqual(cache.check(), (True, ''))

    def test_real_connection_failure_is_reported(self):
        hub = FakeHub([
            (True, ''),
            (False, 'ConnectionError: connection refused'),
        ])
        cache = HubStatusCache(lambda: hub, ttl_seconds=12)
        with patch('purchase_tool.main.time.monotonic', return_value=100):
            cache.check()
        with patch('purchase_tool.main.time.monotonic', return_value=113):
            self.assertEqual(
                cache.check(),
                (False, 'ConnectionError: connection refused'))


if __name__ == '__main__':
    unittest.main()
