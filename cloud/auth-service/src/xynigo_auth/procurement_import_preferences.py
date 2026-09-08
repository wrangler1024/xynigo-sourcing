"""Per-account import destinations, recorded only after successful validation."""

from datetime import UTC, datetime

from .models import WorkspaceViewPreference

VIEW_KEY = 'procurement-import.targets.v1'


def _record(session, tenant_id, user_id):
    return session.get(WorkspaceViewPreference, (tenant_id, user_id, VIEW_KEY))


def read_preferences(session, tenant_id, user_id):
    record = _record(session, tenant_id, user_id)
    items = (record.settings or {}).get('targets', []) if record else []
    return {'targets': [dict(item) for item in items if isinstance(item, dict)][:5]}


def _save(session, tenant_id, user_id, targets):
    record = _record(session, tenant_id, user_id)
    if record is None:
        record = WorkspaceViewPreference(tenant_id=tenant_id, user_id=user_id,
                                         view_key=VIEW_KEY, schema_version=1)
        session.add(record)
    record.settings = {'targets': targets[:5]}
    record.updated_at = datetime.now(UTC)
    session.flush()
    return {'targets': targets[:5]}


def remember_target(session, tenant_id, user_id, target):
    items = read_preferences(session, tenant_id, user_id)['targets']
    key = (target['spreadsheetUrl'], target['sheetId'])
    previous = next((item for item in items
                     if (item['spreadsheetUrl'], item['sheetId']) == key), {})
    saved = {name: str(target.get(name) or '')[:1024] for name in
             ('spreadsheetUrl', 'spreadsheetName', 'sheetId', 'sheetName')}
    saved['fillOrderBackground'] = previous.get('fillOrderBackground', True)
    saved['lastUsedAt'] = datetime.now(UTC).isoformat()
    remaining = [item for item in items
                 if (item['spreadsheetUrl'], item['sheetId']) != key]
    return _save(session, tenant_id, user_id, [saved] + remaining)


def update_preference(session, tenant_id, user_id, body):
    items = read_preferences(session, tenant_id, user_id)['targets']
    key = (body.spreadsheetUrl, body.sheetId)
    result = []
    for item in items:
        if (item['spreadsheetUrl'], item['sheetId']) == key:
            if body.action == 'remove':
                continue
            item['fillOrderBackground'] = body.fillOrderBackground
        result.append(item)
    # This endpoint can update/remove an existing validated destination only.
    return _save(session, tenant_id, user_id, result)


def touch_target(session, tenant_id, user_id, url, sheet_id):
    items = read_preferences(session, tenant_id, user_id)['targets']
    current = next((item for item in items
                    if (item['spreadsheetUrl'], item['sheetId']) == (url, sheet_id)), None)
    if current is not None:
        current['lastUsedAt'] = datetime.now(UTC).isoformat()
        _save(session, tenant_id, user_id, [current] + [item for item in items if item is not current])
