from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_auth_flow import build_test_app, start_local_login, start_login

from xynigo_auth.models import AuditEvent, SessionRecord, Tenant, User


def sign_in(client):
    state, poll_token = start_local_login(client)
    assert client.get('/v1/auth/feishu/callback', params={
        'code': 'authorization-code', 'state': state,
    }, follow_redirects=False).status_code == 303
    issued = client.post('/v1/auth/local/poll', json={'pollToken': poll_token})
    assert issued.status_code == 200
    return issued.json()['sessionToken']


def test_bearer_renewal_extends_same_session_and_preserves_identity(tmp_path):
    app, database, _ = build_test_app(tmp_path)
    with TestClient(app) as client:
        token = sign_in(client)
        headers = {'Authorization': 'Bearer ' + token}
        with database.session_factory() as session:
            record = session.scalar(select(SessionRecord))
            record.expires_at = datetime.now(UTC) + timedelta(minutes=30)
            previous = record.expires_at
            session_id, digest = record.id, record.token_hash
            session.commit()
        response = client.post('/v1/auth/session/refresh', headers=headers)
        assert response.status_code == 200, response.text
        result = response.json()
        expiry = datetime.fromisoformat(result['sessionExpiresAt'])
        assert result['renewed'] is True
        assert previous < expiry <= datetime.now(UTC) + timedelta(hours=8)
        assert expiry > datetime.now(UTC) + timedelta(hours=7, minutes=59)
        assert datetime.fromisoformat(result['sessionRefreshAfter']) == expiry - timedelta(hours=4)
        assert token not in response.text
        assert 'set-cookie' not in response.headers
        assert response.headers['cache-control'] == 'no-store'
        assert client.get('/v1/auth/me', headers=headers).status_code == 200
        # Repeated refreshes outside the window do not keep extending expiry.
        repeated = client.post('/v1/auth/session/refresh', headers=headers).json()
        assert repeated['renewed'] is False
        assert repeated['sessionExpiresAt'] == result['sessionExpiresAt']
        with database.session_factory() as session:
            records = list(session.scalars(select(SessionRecord)))
            assert len(records) == 1
            assert records[0].id == session_id and records[0].token_hash == digest
            events = list(session.scalars(select(AuditEvent).where(
                AuditEvent.action == 'auth.session.refresh')))
            assert len(events) == 1
            assert token not in str(events[0].details)


def test_renewal_stops_at_original_absolute_lifetime(tmp_path):
    app, database, _ = build_test_app(tmp_path)
    with TestClient(app) as client:
        token = sign_in(client)
        with database.session_factory() as session:
            record = session.scalar(select(SessionRecord))
            record.created_at = datetime.now(UTC) - timedelta(days=6, hours=23)
            record.expires_at = datetime.now(UTC) + timedelta(minutes=10)
            absolute = record.created_at + timedelta(days=7)
            session.commit()
        response = client.post('/v1/auth/session/refresh', headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 200
        result = response.json()
        assert datetime.fromisoformat(result['sessionExpiresAt']) == absolute
        assert result['sessionRefreshAfter'] == result['sessionExpiresAt']
        assert result['sessionAbsoluteExpiresAt'] == result['sessionExpiresAt']


@pytest.mark.parametrize('invalid', ['expired', 'revoked', 'absolute', 'user', 'tenant'])
def test_renewal_cannot_restore_invalid_or_disabled_sessions(tmp_path, invalid):
    app, database, _ = build_test_app(tmp_path)
    with TestClient(app) as client:
        token = sign_in(client)
        with database.session_factory() as session:
            record = session.scalar(select(SessionRecord))
            if invalid == 'expired':
                record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif invalid == 'revoked':
                record.revoked_at = datetime.now(UTC)
            elif invalid == 'absolute':
                record.created_at = datetime.now(UTC) - timedelta(days=8)
            elif invalid == 'user':
                session.scalar(select(User)).status = 'disabled'
            else:
                session.scalar(select(Tenant)).status = 'disabled'
            session.commit()
            old_expiry = record.expires_at
        response = client.post('/v1/auth/session/refresh', headers={'Authorization': 'Bearer ' + token})
        assert response.status_code in (401, 403)
        with database.session_factory() as session:
            assert session.scalar(select(SessionRecord)).expires_at.replace(tzinfo=UTC) == old_expiry.replace(tzinfo=UTC)


def test_refresh_requires_bearer_and_never_uses_web_cookie(tmp_path):
    app, _database, _ = build_test_app(tmp_path)
    with TestClient(app) as client:
        assert client.post('/v1/auth/session/refresh').status_code == 401
        token = sign_in(client)
        state, _ = start_login(client)
        client.get('/v1/auth/feishu/callback', params={
            'code': 'web-code', 'state': state,
        }, follow_redirects=False)
        assert client.post('/v1/auth/session/refresh', headers={
            'X-Xynigo-Web-CSRF': 'same-origin',
        }).status_code == 401
        assert client.post('/v1/auth/session/refresh', headers={
            'Authorization': 'Bearer ' + token,
        }).status_code == 401
