# -*- coding: utf-8 -*-
"""Single-authority persistence for executor-local configuration.

The desktop settings surface, compatibility HTTP routes, and the legacy cloud
task adapter all pass through this service.  It owns revision checks, atomic
file replacement, read-back verification, summaries, and value-safe audit
diffs.  Credential stores remain separate because Keychain/DPAPI have their
own transactional boundaries.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading


class LocalConfigRevisionConflict(RuntimeError):
    """The caller edited a stale local configuration snapshot."""

    code = 'config_revision_conflict'

    def __init__(self, expected_revision, actual_revision):
        super().__init__('本机配置已变化，请刷新后重试')
        self.expected_revision = str(expected_revision or '')
        self.actual_revision = str(actual_revision or '')


class LocalConfigReadError(RuntimeError):
    """The persisted file cannot safely be used as a write base."""

    code = 'local_config_read_failed'


def local_config_revision(config):
    """Return a deterministic revision without exposing configuration data."""
    encoded = json.dumps(
        config if isinstance(config, dict) else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _configured(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


class LocalConfigService(object):
    """Load, validate, commit, summarize, and audit one local config file."""

    def __init__(self, path, allowed_fields, default_factory,
                 normalizer=None, summary_projector=None,
                 audit_value_fields=()):
        self.path = os.path.abspath(str(path))
        self.allowed_fields = frozenset(allowed_fields)
        self.default_factory = default_factory
        self.normalizer = normalizer or (lambda value: dict(value or {}))
        self.summary_projector = summary_projector or (lambda _value: {})
        self.audit_value_fields = frozenset(audit_value_fields)
        self.lock = threading.RLock()

    def load(self):
        """Load legacy config permissively for runtime compatibility."""
        with self.lock:
            return self._load_locked(strict=False)

    def revision(self, config=None):
        with self.lock:
            current = self._load_locked(False) if config is None else config
            return local_config_revision(current)

    def summary(self, config=None):
        """Return the allowlisted, versioned summary used outside the host."""
        with self.lock:
            current = (self._load_locked(False)
                       if config is None else copy.deepcopy(config))
            projected = self.summary_projector(copy.deepcopy(current))
            if not isinstance(projected, dict):
                raise ValueError('本机配置摘要必须是对象')
            return {
                'schemaVersion': 2,
                'configRevision': local_config_revision(current),
                'runtimeConfig': copy.deepcopy(projected),
            }

    def commit(self, config, expected_revision=None, source='local'):
        """Atomically persist a complete validated config and read it back."""
        with self.lock:
            before = self._load_locked(strict=True)
            self._require_revision(before, expected_revision)
            candidate = self._validate_complete(config)
            self._write_locked(candidate)
            after = self._load_locked(strict=True)
            if after != candidate:
                raise RuntimeError('本机配置写后回读不一致')
            return self._commit_result(before, after, source)

    def commit_patch(self, submitted, updater, expected_revision=None,
                     source='local'):
        """Apply one existing domain validator under the same revision lock."""
        if not callable(updater):
            raise TypeError('本机配置更新器无效')
        with self.lock:
            before = self._load_locked(strict=True)
            self._require_revision(before, expected_revision)
            candidate = updater(copy.deepcopy(before), submitted)
            candidate = self._validate_complete(candidate)
            self._write_locked(candidate)
            after = self._load_locked(strict=True)
            if after != candidate:
                raise RuntimeError('本机配置写后回读不一致')
            return self._commit_result(before, after, source)

    def safe_diff(self, before, after):
        """Describe changes without emitting private routing or secret values."""
        before = before if isinstance(before, dict) else {}
        after = after if isinstance(after, dict) else {}
        changes = []
        for field in sorted(self.allowed_fields):
            old_value = before.get(field)
            new_value = after.get(field)
            if old_value == new_value:
                continue
            item = {'field': field}
            if field in self.audit_value_fields:
                item.update({
                    'before': copy.deepcopy(old_value),
                    'after': copy.deepcopy(new_value),
                })
            else:
                item.update({
                    'beforeConfigured': _configured(old_value),
                    'afterConfigured': _configured(new_value),
                })
            changes.append(item)
        return changes

    def _load_locked(self, strict):
        defaults = self.default_factory()
        if not isinstance(defaults, dict):
            raise ValueError('本机默认配置必须是对象')
        config = {
            key: copy.deepcopy(value)
            for key, value in defaults.items()
            if key in self.allowed_fields
        }
        try:
            with open(self.path, encoding='utf-8') as handle:
                saved = json.load(handle)
        except FileNotFoundError:
            saved = {}
        except Exception as exc:
            if strict:
                raise LocalConfigReadError(
                    '本机配置文件无法读取，已停止写入以避免覆盖') from exc
            saved = {}
        if not isinstance(saved, dict):
            if strict:
                raise LocalConfigReadError(
                    '本机配置文件格式无效，已停止写入以避免覆盖')
            saved = {}
        config.update({
            key: copy.deepcopy(value)
            for key, value in saved.items()
            if key in self.allowed_fields
        })
        normalized = self.normalizer(config)
        if not isinstance(normalized, dict):
            raise ValueError('本机配置迁移结果必须是对象')
        return normalized

    def _validate_complete(self, config):
        if not isinstance(config, dict):
            raise ValueError('本机配置必须是对象')
        unknown = set(config) - self.allowed_fields
        if unknown:
            raise ValueError('配置包含不允许保存的字段')
        defaults = self.default_factory()
        if not isinstance(defaults, dict):
            raise ValueError('本机默认配置必须是对象')
        complete = {
            key: copy.deepcopy(value)
            for key, value in defaults.items()
            if key in self.allowed_fields
        }
        # Compatibility callers historically saved a valid partial object.
        # Expand it before persistence so read-back compares the effective
        # configuration rather than the sparse representation.
        complete.update(copy.deepcopy(config))
        normalized = self.normalizer(complete)
        if not isinstance(normalized, dict):
            raise ValueError('本机配置迁移结果必须是对象')
        unknown = set(normalized) - self.allowed_fields
        if unknown:
            raise ValueError('配置迁移产生了不允许保存的字段')
        return normalized

    def _require_revision(self, current, expected_revision):
        if expected_revision in (None, ''):
            return
        actual = local_config_revision(current)
        if str(expected_revision) != actual:
            raise LocalConfigRevisionConflict(expected_revision, actual)

    def _write_locked(self, config):
        parent = os.path.dirname(self.path)
        os.makedirs(parent, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix='.config-', suffix='.tmp', dir=parent)
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                fd = -1
                json.dump(config, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = ''
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._sync_parent(parent)
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    @staticmethod
    def _sync_parent(parent):
        try:
            directory_fd = os.open(parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)

    def _commit_result(self, before, after, source):
        changes = self.safe_diff(before, after)
        return {
            'config': copy.deepcopy(after),
            'configRevision': local_config_revision(after),
            'changedFields': [item['field'] for item in changes],
            'auditDiff': changes,
            'source': str(source or 'local')[:64],
        }
