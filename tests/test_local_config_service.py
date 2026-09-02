# -*- coding: utf-8 -*-
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from purchase_tool.local_config_service import (
    LocalConfigReadError,
    LocalConfigRevisionConflict,
    LocalConfigService,
    local_config_revision,
)


class LocalConfigServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / 'config.json'
        self.service = LocalConfigService(
            self.path,
            allowed_fields={'hubPort', 'safeParallelTasks', 'privateTarget'},
            default_factory=lambda: {
                'hubPort': 6873,
                'safeParallelTasks': True,
                'privateTarget': '',
            },
            normalizer=lambda value: dict(value),
            summary_projector=lambda value: {
                'hubPort': value['hubPort'],
                'safeParallelTasks': value['safeParallelTasks'],
                'privateTargetConfigured': bool(value['privateTarget']),
            },
            audit_value_fields={'hubPort', 'safeParallelTasks'},
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_legacy_load_filters_unknown_fields(self):
        self.path.write_text(json.dumps({
            'hubPort': 6999,
            'unknown': 'must-not-enter-runtime',
        }), encoding='utf-8')

        loaded = self.service.load()

        self.assertEqual(loaded['hubPort'], 6999)
        self.assertTrue(loaded['safeParallelTasks'])
        self.assertNotIn('unknown', loaded)

    def test_commit_is_atomic_revisioned_and_value_safe(self):
        before = self.service.load()
        expected = self.service.revision(before)

        result = self.service.commit({
            'hubPort': 6999,
            'safeParallelTasks': False,
            'privateTarget': 'sensitive-routing-id',
        }, expected_revision=expected, source='desktop')

        self.assertEqual(result['source'], 'desktop')
        self.assertEqual(result['configRevision'], local_config_revision(
            result['config']))
        self.assertEqual(set(result['changedFields']), {
            'hubPort', 'safeParallelTasks', 'privateTarget'})
        private_change = next(
            item for item in result['auditDiff']
            if item['field'] == 'privateTarget')
        self.assertNotIn('before', private_change)
        self.assertNotIn('after', private_change)
        self.assertFalse(private_change['beforeConfigured'])
        self.assertTrue(private_change['afterConfigured'])
        rendered = json.dumps(result['auditDiff'], ensure_ascii=False)
        self.assertNotIn('sensitive-routing-id', rendered)
        self.assertEqual(self.service.load(), result['config'])
        if os.name != 'nt':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.path.parent.glob('.config-*.tmp')))

    def test_stale_revision_is_rejected_without_writing(self):
        first = self.service.commit({
            'hubPort': 6874,
            'safeParallelTasks': True,
            'privateTarget': '',
        })
        before = self.path.read_bytes()

        with self.assertRaises(LocalConfigRevisionConflict) as caught:
            self.service.commit({
                'hubPort': 6875,
                'safeParallelTasks': True,
                'privateTarget': '',
            }, expected_revision='0' * 64)

        self.assertEqual(caught.exception.actual_revision,
                         first['configRevision'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_commit_patch_runs_domain_validator_inside_revision_gate(self):
        expected = self.service.revision()

        def update(old, submitted):
            updated = dict(old)
            updated['hubPort'] = int(submitted['hubPort'])
            return updated

        result = self.service.commit_patch(
            {'hubPort': '7001'}, update,
            expected_revision=expected, source='desktop')

        self.assertEqual(result['config']['hubPort'], 7001)
        self.assertEqual(result['changedFields'], ['hubPort'])

    def test_corrupt_existing_file_blocks_commit_instead_of_overwrite(self):
        self.path.write_text('{broken json', encoding='utf-8')
        self.assertEqual(self.service.load()['hubPort'], 6873)
        before = self.path.read_bytes()

        with self.assertRaises(LocalConfigReadError):
            self.service.commit({
                'hubPort': 7000,
                'safeParallelTasks': True,
                'privateTarget': '',
            })

        self.assertEqual(self.path.read_bytes(), before)

    def test_replace_failure_preserves_existing_file_and_cleans_temp(self):
        self.service.commit({
            'hubPort': 6873,
            'safeParallelTasks': True,
            'privateTarget': '',
        })
        before = self.path.read_bytes()
        with patch('purchase_tool.local_config_service.os.replace',
                   side_effect=OSError('simulated replacement failure')):
            with self.assertRaises(OSError):
                self.service.commit({
                    'hubPort': 7000,
                    'safeParallelTasks': True,
                    'privateTarget': '',
                })
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.path.parent.glob('.config-*.tmp')))

    def test_summary_is_versioned_allowlisted_and_non_sensitive(self):
        config = {
            'hubPort': 6999,
            'safeParallelTasks': False,
            'privateTarget': 'sensitive-routing-id',
        }
        summary = self.service.summary(config)

        self.assertEqual(summary['schemaVersion'], 2)
        self.assertEqual(summary['configRevision'],
                         local_config_revision(config))
        self.assertEqual(summary['runtimeConfig'], {
            'hubPort': 6999,
            'safeParallelTasks': False,
            'privateTargetConfigured': True,
        })
        rendered = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn('sensitive-routing-id', rendered)
        self.assertNotIn('privateTarget"', rendered)


if __name__ == '__main__':
    unittest.main()
