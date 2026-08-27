# -*- coding: utf-8 -*-
"""Copy an explicit green-package data whitelist into a standard install."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile


PRESERVED_FILES = ('config.json',)
PRESERVED_DIRECTORIES = (
    '查询日志', '日志', 'logs', '运行数据', 'data', '数据',
    'imports', '导入文件',
)


class DataMigrationError(ValueError):
    pass


def default_standard_data_dir(environ=None):
    environ = os.environ if environ is None else environ
    configured = str(environ.get('XYNIGO_DATA_DIR') or '').strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'XynigoSourcing'
    return Path.cwd()


def _validate_source(source, target):
    try:
        source = Path(source).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DataMigrationError('绿色包目录不存在或无法读取') from exc
    target = Path(target).expanduser().resolve(strict=False)
    if source == target:
        raise DataMigrationError('绿色包目录不能与标准版数据目录相同')
    if source == Path(source.anchor) or target == Path(target.anchor):
        raise DataMigrationError('不能使用磁盘根目录迁移数据')
    marker = source / 'VERSION.json'
    if not marker.is_file() or marker.is_symlink():
        raise DataMigrationError('所选目录不是有效的 Xynigo 绿色包')
    return source, target


def _ensure_safe_target_parent(target, target_root):
    relative = target.relative_to(target_root)
    current = target_root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise DataMigrationError('标准版数据目录包含不安全的路径')
        current.mkdir(exist_ok=True)


def _copy_file_without_overwrite(source, target, target_root):
    if source.is_symlink():
        raise DataMigrationError('绿色包数据包含符号链接，已停止迁移')
    if target.exists():
        return False
    _ensure_safe_target_parent(target, target_root)
    fd, temporary_name = tempfile.mkstemp(
        prefix='.%s.' % target.name, dir=str(target.parent))
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(str(source), str(temporary))
        try:
            os.link(str(temporary), str(target))
        except FileExistsError:
            return False
        return True
    finally:
        if temporary.exists():
            temporary.unlink()


def _preflight_preserved_data(source):
    for name in PRESERVED_FILES:
        candidate = source / name
        if candidate.exists() and (candidate.is_symlink() or not candidate.is_file()):
            raise DataMigrationError('%s 不是安全的普通文件' % name)
    for name in PRESERVED_DIRECTORIES:
        source_dir = source / name
        if not source_dir.exists():
            continue
        if source_dir.is_symlink() or not source_dir.is_dir():
            raise DataMigrationError('%s 不是安全的数据目录' % name)
        for root, directories, files in os.walk(str(source_dir), followlinks=False):
            root_path = Path(root)
            if any((root_path / item).is_symlink()
                   for item in directories + files):
                raise DataMigrationError('绿色包数据包含符号链接，已停止迁移')


def migrate_green_data(source, target=None):
    target = target or default_standard_data_dir()
    source, target = _validate_source(source, target)
    _preflight_preserved_data(source)
    target_was_missing = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    if target_was_missing:
        target.chmod(0o700)
    copied = 0
    skipped = 0
    for name in PRESERVED_FILES:
        candidate = source / name
        if not candidate.exists():
            continue
        if _copy_file_without_overwrite(candidate, target / name, target):
            copied += 1
        else:
            skipped += 1
    for name in PRESERVED_DIRECTORIES:
        source_dir = source / name
        if not source_dir.exists():
            continue
        for root, directories, files in os.walk(str(source_dir), followlinks=False):
            root_path = Path(root)
            for filename in files:
                candidate = root_path / filename
                relative = candidate.relative_to(source_dir)
                if _copy_file_without_overwrite(
                        candidate, target / name / relative, target):
                    copied += 1
                else:
                    skipped += 1
    return {'copied': copied, 'skipped': skipped, 'target': str(target)}


def migration_cli(argv=None):
    parser = argparse.ArgumentParser(prog='xynigo-sourcing migrate')
    parser.add_argument('source', help='已解压的 Xynigo 绿色包目录')
    parser.add_argument('--target', default='', help=argparse.SUPPRESS)
    args = parser.parse_args(list(argv or []))
    try:
        result = migrate_green_data(
            args.source, args.target or default_standard_data_dir())
    except (DataMigrationError, OSError) as exc:
        print('数据迁移失败：%s' % exc, file=sys.stderr)
        return 1
    print('数据迁移完成：复制 %s 个文件，保留 %s 个现有文件。' % (
        result['copied'], result['skipped']))
    return 0
