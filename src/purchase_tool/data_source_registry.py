# -*- coding: utf-8 -*-
"""Local multi-user and multi-environment data-source registry."""

from __future__ import annotations

import copy
import hashlib
import re
import uuid

from .local_config_service import LocalConfigService


REGISTRY_SCHEMA_VERSION = 1
REGISTRY_FIELDS = frozenset({
    'schemaVersion',
    'dataSources',
    'buyerProfiles',
    'environmentBindings',
    'teamDefaultDataSourceId',
    'legacyMigration',
})
SOURCE_ID_PATTERN = re.compile(r'^ds_[0-9a-f]{24}$')
PRIVATE_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{2,200}$')
CELL_RANGE_PATTERN = re.compile(
    r'^[A-Z]{1,3}[1-9][0-9]*:[A-Z]{1,3}(?:[1-9][0-9]*)?$')
CONTAINER_CODE_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')


class DataSourceRegistryError(ValueError):
    code = 'data_source_registry_invalid'


class DataSourceMappingRequired(DataSourceRegistryError):
    code = 'data_source_mapping_required'

    def __init__(self):
        super().__init__('当前采购员或环境尚未配置可用数据源')


def default_registry():
    return {
        'schemaVersion': REGISTRY_SCHEMA_VERSION,
        'dataSources': [],
        'buyerProfiles': [],
        'environmentBindings': [],
        'teamDefaultDataSourceId': '',
        'legacyMigration': {},
    }


def _plain_text(value, label, maximum, allow_blank=False):
    text = str(value or '').strip()
    if not text and allow_blank:
        return ''
    if (not text or len(text) > maximum
            or any(character in text for character in '\r\n\t\x00')):
        raise DataSourceRegistryError('%s格式无效' % label)
    return text


def _member_id(value, allow_blank=False):
    text = str(value or '').strip()
    if not text and allow_blank:
        return ''
    try:
        return str(uuid.UUID(text))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DataSourceRegistryError('成员 UUID 格式无效') from exc


def _source_id(value):
    text = str(value or '').strip().lower()
    if not SOURCE_ID_PATTERN.fullmatch(text):
        raise DataSourceRegistryError('数据源编号格式无效')
    return text


def _private_id(value, label):
    text = str(value or '').strip()
    if not PRIVATE_ID_PATTERN.fullmatch(text):
        raise DataSourceRegistryError('%s格式无效' % label)
    return text


def _cell_range(value):
    text = str(value or '').strip().upper()
    if not CELL_RANGE_PATTERN.fullmatch(text):
        raise DataSourceRegistryError('数据源表格范围格式无效')
    return text


def _container_code(value):
    text = str(value or '').strip()
    if not CONTAINER_CODE_PATTERN.fullmatch(text):
        raise DataSourceRegistryError('HubStudio containerCode 格式无效')
    return text


def _legacy_source_id(scope, token, sheet_id, cell_range):
    digest = hashlib.sha256(('|'.join((
        str(scope), str(token), str(sheet_id), str(cell_range),
    ))).encode('utf-8')).hexdigest()[:24]
    return 'ds_' + digest


def _member_source_id(member_id, token, sheet_id, cell_range):
    return _legacy_source_id(
        'personal:' + _member_id(member_id), token, sheet_id, cell_range)


def _team_source_id(token, sheet_id, cell_range):
    return _legacy_source_id('team', token, sheet_id, cell_range)


def _normalize_source(item):
    if not isinstance(item, dict):
        raise DataSourceRegistryError('数据源记录必须是对象')
    scope = str(item.get('scope') or '').strip().lower()
    if scope not in {'personal', 'team'}:
        raise DataSourceRegistryError('数据源范围无效')
    owner = _member_id(item.get('ownerMemberId'), allow_blank=True)
    if scope == 'team' and owner:
        raise DataSourceRegistryError('团队数据源不能绑定个人所有者')
    state = str(item.get('migrationState') or 'ready').strip()
    if state not in {'ready', 'needs_owner_confirmation'}:
        raise DataSourceRegistryError('数据源迁移状态无效')
    if scope == 'personal' and not owner:
        state = 'needs_owner_confirmation'
    elif state == 'needs_owner_confirmation':
        state = 'ready'
    enabled = item.get('enabled', True)
    if not isinstance(enabled, bool):
        raise DataSourceRegistryError('数据源启用状态必须是布尔值')
    return {
        'id': _source_id(item.get('id')),
        'scope': scope,
        'ownerMemberId': owner,
        'label': _plain_text(item.get('label'), '数据源名称', 120),
        'spreadsheetToken': _private_id(
            item.get('spreadsheetToken'), '飞书 Spreadsheet Token'),
        'sheetId': _private_id(item.get('sheetId'), '飞书 Sheet ID'),
        'cellRange': _cell_range(item.get('cellRange')),
        'sheetName': _plain_text(
            item.get('sheetName'), '工作表名称', 255, allow_blank=True),
        'enabled': enabled,
        'migrationState': state,
    }


def normalize_registry(mapping):
    if not isinstance(mapping, dict):
        raise DataSourceRegistryError('数据源注册表必须是对象')
    try:
        schema_version = int(
            mapping.get('schemaVersion') or REGISTRY_SCHEMA_VERSION)
    except (TypeError, ValueError) as exc:
        raise DataSourceRegistryError('数据源注册表版本无效') from exc
    if schema_version != REGISTRY_SCHEMA_VERSION:
        raise DataSourceRegistryError('数据源注册表版本不受支持')

    raw_sources = mapping.get('dataSources') or []
    raw_profiles = mapping.get('buyerProfiles') or []
    raw_bindings = mapping.get('environmentBindings') or []
    if not all(isinstance(value, list) for value in (
            raw_sources, raw_profiles, raw_bindings)):
        raise DataSourceRegistryError('数据源注册表列表格式无效')

    sources = [_normalize_source(item) for item in raw_sources]
    source_index = {item['id']: item for item in sources}
    if len(source_index) != len(sources):
        raise DataSourceRegistryError('数据源编号重复')

    profiles = []
    seen_members = set()
    for item in raw_profiles:
        if not isinstance(item, dict):
            raise DataSourceRegistryError('采购员配置必须是对象')
        member = _member_id(item.get('memberId'))
        source_id = _source_id(item.get('defaultDataSourceId'))
        source = source_index.get(source_id)
        if (source is None or source['scope'] != 'personal'
                or source['ownerMemberId'] != member
                or source['migrationState'] != 'ready'):
            raise DataSourceRegistryError('采购员默认数据源归属无效')
        if member in seen_members:
            raise DataSourceRegistryError('采购员默认数据源重复')
        seen_members.add(member)
        profiles.append({
            'memberId': member,
            'defaultDataSourceId': source_id,
        })

    bindings = []
    seen_bindings = set()
    for item in raw_bindings:
        if not isinstance(item, dict):
            raise DataSourceRegistryError('环境数据源绑定必须是对象')
        container = _container_code(item.get('containerCode'))
        member = _member_id(item.get('memberId'))
        source_id = _source_id(item.get('dataSourceId'))
        source = source_index.get(source_id)
        if source is None:
            raise DataSourceRegistryError('环境绑定的数据源不存在')
        if (source['scope'] == 'personal'
                and source['ownerMemberId'] != member):
            raise DataSourceRegistryError('环境绑定的个人数据源归属无效')
        key = (container, member)
        if key in seen_bindings:
            raise DataSourceRegistryError('环境与采购员绑定重复')
        seen_bindings.add(key)
        bindings.append({
            'containerCode': container,
            'memberId': member,
            'dataSourceId': source_id,
        })

    team_default = str(
        mapping.get('teamDefaultDataSourceId') or '').strip().lower()
    if team_default:
        team_default = _source_id(team_default)
        source = source_index.get(team_default)
        if source is None or source['scope'] != 'team':
            raise DataSourceRegistryError('团队默认数据源无效')

    legacy = mapping.get('legacyMigration') or {}
    if not isinstance(legacy, dict):
        raise DataSourceRegistryError('旧配置迁移状态无效')
    normalized_legacy = {}
    if legacy:
        if int(legacy.get('version') or 0) != 1:
            raise DataSourceRegistryError('旧配置迁移版本无效')
        active_mode = str(legacy.get('activeMode') or 'team').strip().lower()
        if active_mode not in {'personal', 'team'}:
            active_mode = 'team'
        normalized_legacy = {
            'version': 1,
            'activeMode': active_mode,
            'personalSourceId': str(
                legacy.get('personalSourceId') or '').strip().lower(),
            'teamSourceId': str(
                legacy.get('teamSourceId') or '').strip().lower(),
        }
        for key in ('personalSourceId', 'teamSourceId'):
            if normalized_legacy[key]:
                normalized_legacy[key] = _source_id(normalized_legacy[key])

    return {
        'schemaVersion': REGISTRY_SCHEMA_VERSION,
        'dataSources': sorted(sources, key=lambda item: item['id']),
        'buyerProfiles': sorted(profiles, key=lambda item: item['memberId']),
        'environmentBindings': sorted(
            bindings,
            key=lambda item: (item['containerCode'], item['memberId'])),
        'teamDefaultDataSourceId': team_default,
        'legacyMigration': normalized_legacy,
    }


def runtime_config_for_source(base_config, source):
    """Build a request-local legacy config view for one resolved source."""
    normalized = _normalize_source(source)
    if (not normalized['enabled']
            or normalized['migrationState'] != 'ready'):
        raise DataSourceMappingRequired()
    mapping = copy.deepcopy(dict(base_config or {}))
    for title in ('Personal', 'Team'):
        prefix = 'purchaseAssistant' + title
        for suffix in (
                'SpreadsheetToken', 'SheetId', 'CellRange', 'SheetName'):
            mapping[prefix + suffix] = ''
    mode = normalized['scope']
    profile = ('purchaseAssistantPersonal' if mode == 'personal'
               else 'purchaseAssistantTeam')
    mapping['purchaseAssistantSourceMode'] = mode
    mapping['purchaseAssistantSpreadsheetToken'] = normalized[
        'spreadsheetToken']
    mapping['purchaseAssistantSheetId'] = normalized['sheetId']
    mapping['purchaseAssistantCellRange'] = normalized['cellRange']
    mapping[profile + 'SpreadsheetToken'] = normalized[
        'spreadsheetToken']
    mapping[profile + 'SheetId'] = normalized['sheetId']
    mapping[profile + 'CellRange'] = normalized['cellRange']
    mapping[profile + 'SheetName'] = normalized['sheetName']
    return mapping


class DataSourceRegistry(object):
    def __init__(self, path):
        self.service = LocalConfigService(
            path,
            allowed_fields=REGISTRY_FIELDS,
            default_factory=default_registry,
            normalizer=normalize_registry,
            summary_projector=self._count_summary,
        )

    @staticmethod
    def _count_summary(mapping):
        return {
            'dataSourceCount': len(mapping.get('dataSources') or []),
            'buyerProfileCount': len(mapping.get('buyerProfiles') or []),
            'environmentBindingCount': len(
                mapping.get('environmentBindings') or []),
            'mappingConflictCount': 0,
        }

    def migrate_legacy(self, config):
        with self.service.lock:
            current = self.service.load()
            candidate = copy.deepcopy(current)
            sources = list(candidate['dataSources'])
            source_ids = {item['id'] for item in sources}
            previous_migration = current.get('legacyMigration') or {}
            migrated_ids = {
                'personal': str(
                    previous_migration.get('personalSourceId') or ''),
                'team': str(previous_migration.get('teamSourceId') or ''),
            }
            for scope, title, fallback_range in (
                    ('personal', 'Personal', 'A1:H'),
                    ('team', 'Team', 'A1:AQ')):
                if migrated_ids[scope]:
                    continue
                prefix = 'purchaseAssistant' + title
                token = str(config.get(prefix + 'SpreadsheetToken') or '').strip()
                sheet_id = str(config.get(prefix + 'SheetId') or '').strip()
                cell_range = str(
                    config.get(prefix + 'CellRange') or fallback_range
                ).strip().upper()
                if not token or not sheet_id:
                    continue
                source_id = _legacy_source_id(
                    scope, token, sheet_id, cell_range)
                migrated_ids[scope] = source_id
                if source_id in source_ids:
                    continue
                sources.append({
                    'id': source_id,
                    'scope': scope,
                    'ownerMemberId': '',
                    'label': ('旧个人速填表（待确认归属）'
                              if scope == 'personal' else '旧团队采购执行协作表'),
                    'spreadsheetToken': token,
                    'sheetId': sheet_id,
                    'cellRange': cell_range,
                    'sheetName': str(
                        config.get(prefix + 'SheetName') or '').strip(),
                    'enabled': True,
                    'migrationState': (
                        'needs_owner_confirmation'
                        if scope == 'personal' else 'ready'),
                })
                source_ids.add(source_id)
            candidate['dataSources'] = sources
            active_mode = str(
                previous_migration.get('activeMode')
                or config.get('purchaseAssistantSourceMode') or 'team'
            ).strip().lower()
            candidate['legacyMigration'] = {
                'version': 1,
                'activeMode': active_mode,
                'personalSourceId': migrated_ids['personal'],
                'teamSourceId': migrated_ids['team'],
            }
            candidate = normalize_registry(candidate)
            if candidate == current:
                return self.snapshot(current)
            result = self.service.commit(
                candidate,
                expected_revision=self.service.revision(current),
                source='legacy_personal_team_migration',
            )
            return self.snapshot(result['config'])

    def snapshot(self, mapping=None):
        current = self.service.load() if mapping is None else mapping
        return {
            'registryRevision': self.service.revision(current),
            'registry': copy.deepcopy(current),
        }

    def public_snapshot(self, member_id, include_all=False):
        member = _member_id(member_id)
        snapshot = self.snapshot()
        registry = snapshot['registry']
        sources = []
        visible_ids = set()
        for source in registry['dataSources']:
            if (not include_all and source['scope'] == 'personal'
                    and source['ownerMemberId'] not in {'', member}):
                continue
            visible_ids.add(source['id'])
            sources.append({
                'id': source['id'],
                'scope': source['scope'],
                'ownerMemberId': source['ownerMemberId'],
                'label': source['label'],
                'sheetName': source['sheetName'],
                'cellRange': source['cellRange'],
                'enabled': source['enabled'],
                'configured': True,
                'migrationState': source['migrationState'],
                'environmentCount': sum(
                    1 for item in registry['environmentBindings']
                    if item['dataSourceId'] == source['id']),
            })
        profiles = [item for item in registry['buyerProfiles']
                    if include_all or item['memberId'] == member]
        bindings = [item for item in registry['environmentBindings']
                    if (include_all or item['memberId'] == member)
                    and item['dataSourceId'] in visible_ids]
        return {
            'schemaVersion': REGISTRY_SCHEMA_VERSION,
            'registryRevision': snapshot['registryRevision'],
            'dataSources': sources,
            'buyerProfiles': copy.deepcopy(profiles),
            'environmentBindings': copy.deepcopy(bindings),
            'teamDefaultDataSourceId': registry['teamDefaultDataSourceId'],
            'counts': self._count_summary(registry),
        }

    def source(self, source_id):
        wanted = _source_id(source_id)
        source = next((
            item for item in self.service.load()['dataSources']
            if item['id'] == wanted
        ), None)
        if source is None:
            raise DataSourceRegistryError('数据源不存在')
        return copy.deepcopy(source)

    def update_source_metadata(self, source_id, label, enabled,
                               expected_revision=None):
        wanted = _source_id(source_id)
        normalized_label = _plain_text(label, '数据源名称', 120)
        if not isinstance(enabled, bool):
            raise DataSourceRegistryError('数据源启用状态必须是布尔值')

        def update(current, _submitted):
            candidate = copy.deepcopy(current)
            source = next((
                item for item in candidate['dataSources']
                if item['id'] == wanted
            ), None)
            if source is None:
                raise DataSourceRegistryError('数据源不存在')
            source['label'] = normalized_label
            source['enabled'] = enabled
            return candidate

        return self.service.commit_patch(
            {}, update, expected_revision=expected_revision,
            source='update_data_source_metadata')

    def replace_source_target(self, source_id, target, owner_member_id='',
                              expected_revision=None):
        old_id = _source_id(source_id)
        if not isinstance(target, dict):
            raise DataSourceRegistryError('数据源目标格式无效')
        token = _private_id(
            target.get('spreadsheetToken'), '飞书 Spreadsheet Token')
        sheet_id = _private_id(target.get('sheetId'), '飞书 Sheet ID')
        cell_range = _cell_range(target.get('cellRange'))
        sheet_name = _plain_text(
            target.get('sheetName'), '工作表名称', 255, allow_blank=True)
        requested_owner = _member_id(owner_member_id, allow_blank=True)
        result_ids = {}

        def update(current, _submitted):
            candidate = copy.deepcopy(current)
            old = next((
                item for item in candidate['dataSources']
                if item['id'] == old_id
            ), None)
            if old is None:
                raise DataSourceRegistryError('数据源不存在')
            owner = ''
            if old['scope'] == 'personal':
                owner = old['ownerMemberId'] or requested_owner
                if not owner:
                    raise DataSourceRegistryError('个人数据源需要先确认归属')
                new_id = _member_source_id(
                    owner, token, sheet_id, cell_range)
            else:
                new_id = _team_source_id(token, sheet_id, cell_range)
            duplicate = next((
                item for item in candidate['dataSources']
                if item['id'] == new_id and item['id'] != old_id
            ), None)
            if duplicate is not None:
                raise DataSourceRegistryError('该飞书工作表已经登记为其他数据源')
            label = old['label']
            if label.startswith('旧'):
                label = (
                    ('个人速填表 · ' if old['scope'] == 'personal'
                     else '团队采购表 · ') + sheet_name
                    if sheet_name else
                    ('个人速填表' if old['scope'] == 'personal'
                     else '团队采购表'))
            replacement = {
                'id': new_id,
                'scope': old['scope'],
                'ownerMemberId': owner,
                'label': label[:120],
                'spreadsheetToken': token,
                'sheetId': sheet_id,
                'cellRange': cell_range,
                'sheetName': sheet_name,
                'enabled': old['enabled'],
                'migrationState': 'ready',
            }
            candidate['dataSources'] = [
                replacement if item['id'] == old_id else item
                for item in candidate['dataSources']
            ]
            for profile in candidate['buyerProfiles']:
                if profile['defaultDataSourceId'] == old_id:
                    profile['defaultDataSourceId'] = new_id
            for binding in candidate['environmentBindings']:
                if binding['dataSourceId'] == old_id:
                    binding['dataSourceId'] = new_id
            if candidate['teamDefaultDataSourceId'] == old_id:
                candidate['teamDefaultDataSourceId'] = new_id
            legacy = candidate.get('legacyMigration') or {}
            for key in ('personalSourceId', 'teamSourceId'):
                if legacy.get(key) == old_id:
                    legacy[key] = new_id
            if old['scope'] == 'personal' and not old['ownerMemberId']:
                candidate['buyerProfiles'] = [
                    item for item in candidate['buyerProfiles']
                    if item['memberId'] != owner
                ] + [{
                    'memberId': owner,
                    'defaultDataSourceId': new_id,
                }]
            result_ids.update(oldDataSourceId=old_id, dataSourceId=new_id)
            return candidate

        result = self.service.commit_patch(
            {}, update, expected_revision=expected_revision,
            source='replace_data_source_target')
        result.update(result_ids)
        return result

    def claim_legacy_personal(self, member_id, source_id,
                              expected_revision=None):
        member = _member_id(member_id)
        wanted = _source_id(source_id)

        def update(current, _submitted):
            candidate = copy.deepcopy(current)
            source = next((item for item in candidate['dataSources']
                           if item['id'] == wanted), None)
            if (source is None or source['scope'] != 'personal'
                    or source['migrationState'] != 'needs_owner_confirmation'
                    or source['ownerMemberId']):
                raise DataSourceRegistryError('旧个人数据源不可认领')
            source['ownerMemberId'] = member
            source['migrationState'] = 'ready'
            candidate['buyerProfiles'] = [
                item for item in candidate['buyerProfiles']
                if item['memberId'] != member
            ] + [{
                'memberId': member,
                'defaultDataSourceId': wanted,
            }]
            return candidate

        return self.service.commit_patch(
            {}, update, expected_revision=expected_revision,
            source='claim_legacy_personal_source')

    def upsert_personal(self, member_id, target, expected_revision=None):
        member = _member_id(member_id)
        if not isinstance(target, dict):
            raise DataSourceRegistryError('个人数据源目标格式无效')
        token = _private_id(
            target.get('spreadsheetToken'), '飞书 Spreadsheet Token')
        sheet_id = _private_id(target.get('sheetId'), '飞书 Sheet ID')
        cell_range = _cell_range(target.get('cellRange'))
        sheet_name = _plain_text(
            target.get('sheetName'), '工作表名称', 255, allow_blank=True)
        wanted = _member_source_id(member, token, sheet_id, cell_range)
        label = ('个人速填表 · ' + sheet_name) if sheet_name else '个人速填表'

        def update(current, _submitted):
            candidate = copy.deepcopy(current)
            record = {
                'id': wanted,
                'scope': 'personal',
                'ownerMemberId': member,
                'label': label[:120],
                'spreadsheetToken': token,
                'sheetId': sheet_id,
                'cellRange': cell_range,
                'sheetName': sheet_name,
                'enabled': True,
                'migrationState': 'ready',
            }
            candidate['dataSources'] = [
                item for item in candidate['dataSources']
                if item['id'] != wanted
            ] + [record]
            candidate['buyerProfiles'] = [
                item for item in candidate['buyerProfiles']
                if item['memberId'] != member
            ] + [{
                'memberId': member,
                'defaultDataSourceId': wanted,
            }]
            return candidate

        result = self.service.commit_patch(
            {}, update, expected_revision=expected_revision,
            source='upsert_personal_data_source')
        result['dataSourceId'] = wanted
        return result

    def upsert_team(self, target, set_default=False,
                    expected_revision=None):
        if not isinstance(target, dict):
            raise DataSourceRegistryError('团队数据源目标格式无效')
        token = _private_id(
            target.get('spreadsheetToken'), '飞书 Spreadsheet Token')
        sheet_id = _private_id(target.get('sheetId'), '飞书 Sheet ID')
        cell_range = _cell_range(target.get('cellRange'))
        sheet_name = _plain_text(
            target.get('sheetName'), '工作表名称', 255, allow_blank=True)
        wanted = _team_source_id(token, sheet_id, cell_range)
        label = ('团队采购表 · ' + sheet_name) if sheet_name else '团队采购表'

        def update(current, _submitted):
            candidate = copy.deepcopy(current)
            record = {
                'id': wanted,
                'scope': 'team',
                'ownerMemberId': '',
                'label': label[:120],
                'spreadsheetToken': token,
                'sheetId': sheet_id,
                'cellRange': cell_range,
                'sheetName': sheet_name,
                'enabled': True,
                'migrationState': 'ready',
            }
            candidate['dataSources'] = [
                item for item in candidate['dataSources']
                if item['id'] != wanted
            ] + [record]
            if set_default:
                candidate['teamDefaultDataSourceId'] = wanted
            return candidate

        result = self.service.commit_patch(
            {}, update, expected_revision=expected_revision,
            source='upsert_team_data_source')
        result['dataSourceId'] = wanted
        return result

    def set_buyer_default(self, member_id, source_id,
                          expected_revision=None):
        member = _member_id(member_id)
        wanted = _source_id(source_id)

        def update(current, _submitted):
            candidate = copy.deepcopy(current)
            source = next((item for item in candidate['dataSources']
                           if item['id'] == wanted), None)
            if (source is None or source['scope'] != 'personal'
                    or source['ownerMemberId'] != member
                    or not source['enabled']):
                raise DataSourceRegistryError('个人默认数据源归属无效')
            candidate['buyerProfiles'] = [
                item for item in candidate['buyerProfiles']
                if item['memberId'] != member
            ] + [{
                'memberId': member,
                'defaultDataSourceId': wanted,
            }]
            return candidate

        return self.service.commit_patch(
            {}, update, expected_revision=expected_revision,
            source='buyer_default_data_source')

    def clear_buyer_default(self, member_id, expected_revision=None):
        member = _member_id(member_id)

        def update(current, _submitted):
            candidate = copy.deepcopy(current)
            candidate['buyerProfiles'] = [
                item for item in candidate['buyerProfiles']
                if item['memberId'] != member
            ]
            return candidate

        return self.service.commit_patch(
            {}, update, expected_revision=expected_revision,
            source='clear_buyer_default_data_source')

    def use_team_default(self, member_id, expected_revision=None):
        member = _member_id(member_id)

        def update(current, _submitted):
            wanted = current['teamDefaultDataSourceId']
            source = next((
                item for item in current['dataSources']
                if item['id'] == wanted and item['scope'] == 'team'
                and item['enabled'] and item['migrationState'] == 'ready'
            ), None)
            if source is None:
                raise DataSourceMappingRequired()
            candidate = copy.deepcopy(current)
            candidate['buyerProfiles'] = [
                item for item in candidate['buyerProfiles']
                if item['memberId'] != member
            ]
            return candidate

        return self.service.commit_patch(
            {}, update, expected_revision=expected_revision,
            source='use_team_default_data_source')

    def bind_environment(self, container_code, member_id, source_id,
                         expected_revision=None):
        container = _container_code(container_code)
        member = _member_id(member_id)
        wanted = _source_id(source_id)

        def update(current, _submitted):
            candidate = copy.deepcopy(current)
            source = next((item for item in candidate['dataSources']
                           if item['id'] == wanted), None)
            if source is None or not source['enabled']:
                raise DataSourceRegistryError('环境数据源不可用')
            if (source['scope'] == 'personal'
                    and source['ownerMemberId'] != member):
                raise DataSourceRegistryError('环境数据源归属无效')
            candidate['environmentBindings'] = [
                item for item in candidate['environmentBindings']
                if (item['containerCode'], item['memberId'])
                != (container, member)
            ] + [{
                'containerCode': container,
                'memberId': member,
                'dataSourceId': wanted,
            }]
            return candidate

        return self.service.commit_patch(
            {}, update, expected_revision=expected_revision,
            source='environment_data_source_binding')

    def unbind_environment(self, container_code, member_id,
                           expected_revision=None):
        container = _container_code(container_code)
        member = _member_id(member_id)

        def update(current, _submitted):
            candidate = copy.deepcopy(current)
            candidate['environmentBindings'] = [
                item for item in candidate['environmentBindings']
                if (item['containerCode'], item['memberId'])
                != (container, member)
            ]
            return candidate

        return self.service.commit_patch(
            {}, update, expected_revision=expected_revision,
            source='remove_environment_data_source_binding')

    def set_team_default(self, source_id, expected_revision=None):
        wanted = _source_id(source_id)

        def update(current, _submitted):
            candidate = copy.deepcopy(current)
            source = next((item for item in candidate['dataSources']
                           if item['id'] == wanted), None)
            if (source is None or source['scope'] != 'team'
                    or not source['enabled']):
                raise DataSourceRegistryError('团队默认数据源无效')
            candidate['teamDefaultDataSourceId'] = wanted
            return candidate

        return self.service.commit_patch(
            {}, update, expected_revision=expected_revision,
            source='team_default_data_source')

    def clear_team_default(self, expected_revision=None):
        def update(current, _submitted):
            candidate = copy.deepcopy(current)
            candidate['teamDefaultDataSourceId'] = ''
            return candidate

        return self.service.commit_patch(
            {}, update, expected_revision=expected_revision,
            source='clear_team_default_data_source')

    def team_default(self, allowed_data_source_ids=None):
        registry = self.service.load()
        wanted = registry['teamDefaultDataSourceId']
        allowed = (None if allowed_data_source_ids is None else
                   {_source_id(item) for item in allowed_data_source_ids})
        source = next((
            item for item in registry['dataSources']
            if item['id'] == wanted and item['scope'] == 'team'
            and item['enabled'] and item['migrationState'] == 'ready'
        ), None)
        if source is None or (allowed is not None and wanted not in allowed):
            raise DataSourceMappingRequired()
        return copy.deepcopy(source)

    def resolve(self, member_id, container_code=None,
                allow_team_default=False, allowed_data_source_ids=None):
        member = _member_id(member_id)
        container = (_container_code(container_code)
                     if container_code not in (None, '') else '')
        registry = self.service.load()
        sources = {item['id']: item for item in registry['dataSources']
                   if item['enabled'] and item['migrationState'] == 'ready'}
        allowed = (None if allowed_data_source_ids is None else
                   {_source_id(item) for item in allowed_data_source_ids})

        wanted = ''
        if container:
            binding = next((item for item in registry['environmentBindings']
                            if item['containerCode'] == container
                            and item['memberId'] == member), None)
            if binding:
                wanted = binding['dataSourceId']
        if not wanted:
            profile = next((item for item in registry['buyerProfiles']
                            if item['memberId'] == member), None)
            if profile:
                wanted = profile['defaultDataSourceId']
        if (not wanted and allow_team_default
                and registry['teamDefaultDataSourceId']):
            wanted = registry['teamDefaultDataSourceId']

        source = sources.get(wanted)
        if (source is None or (allowed is not None and wanted not in allowed)):
            raise DataSourceMappingRequired()
        if (source['scope'] == 'personal'
                and source['ownerMemberId'] != member):
            raise DataSourceMappingRequired()
        return copy.deepcopy(source)
