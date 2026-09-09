"""Same-tenant submitted orders support versioned edits by multiple operators."""
from copy import deepcopy
import uuid

import pytest
from sqlalchemy import delete, func, select
from test_plugin_access import setup_operator
from test_purchase_api import sample_draft
from xynigo_auth.models import (
    AuditEvent, Permission, PurchaseOrder, PurchaseOrderLine, PurchaseSplit,
    PurchaseSyncOutbox, RolePermission, Tenant,
)
from xynigo_auth.purchase_contract import PurchaseDraft, canonical_draft_dict, draft_content_hash
from xynigo_auth.purchase_service import PurchaseOrderService, PurchaseServiceError


def shared_order(tmp_path):
    client, db, headers, role_id, operator_id, admin_id = setup_operator(tmp_path)
    draft = sample_draft()
    with db.session_factory() as session:
        tenant = session.scalar(select(Tenant))
        first = PurchaseOrderService(session).submit(tenant_id=tenant.id,
            actor_user_id=admin_id, draft=PurchaseDraft.model_validate(draft))
        tenant_id = tenant.id
        session.commit()
    return client, db, headers, role_id, operator_id, admin_id, tenant_id, draft, first


def changed_draft(draft, revision=1, price=19.0):
    result = deepcopy(draft)
    result['items'][0]['guidePrice'] = price
    result['guideTotalsByCurrency'] = {'USD': price}
    result['expectedDraftRevision'] = revision
    return result


def test_other_operator_can_load_and_revise_without_taking_original_submission(tmp_path):
    client, db, headers, _, operator_id, admin_id, tenant_id, draft, first = shared_order(tmp_path)
    with client:
        loaded = client.post('/v1/purchase-orders/get', headers=headers,
            json={'orderKey': draft['orderKey']})
        assert loaded.status_code == 200
        assert loaded.json()['data']['draftRevision'] == 1
        changed = changed_draft(draft)
        edited = client.post('/v1/purchase-orders/submit', headers=headers, json=changed)
        assert edited.status_code == 200, edited.text
        data = edited.json()['data']
        assert data['revised'] and data['draftRevision'] == 2
        assert data['purchaseOrderId'] == first['purchaseOrderId']
        assert data['submittedBy']['id'] == str(admin_id)
        assert data['submittedAt'] == loaded.json()['data']['submittedAt']
        assert data['lastEditedBy']['id'] == str(operator_id)
        assert 'expectedDraftRevision' not in data['draft']
        retry = client.post('/v1/purchase-orders/submit', headers=headers, json=changed)
        assert retry.status_code == 200 and retry.json()['data']['unchanged']
        assert retry.json()['data']['draftRevision'] == 2
        # Sharing an exact submitted order does not grant list/workspace access.
        assert client.get('/v1/procurement/orders', headers=headers).status_code == 403

    with db.session_factory() as session:
        record = session.scalar(select(PurchaseOrder))
        assert record.created_by_user_id == record.submitted_by_user_id == admin_id
        assert record.last_edited_by_user_id == operator_id
        assert session.scalar(select(func.count(PurchaseSyncOutbox.id))) == 2
        event = session.scalar(select(AuditEvent).where(
            AuditEvent.actor_user_id == operator_id,
            AuditEvent.action == 'purchase_order.submit', AuditEvent.result == 'success'))
        assert event is not None
        # Original submitter can edit the newest version again.
        final = PurchaseOrderService(session).submit(tenant_id=tenant_id,
            actor_user_id=admin_id, draft=PurchaseDraft.model_validate(changed_draft(draft, 2, 20.0)))
        assert final['draftRevision'] == 3 and final['lastEditedBy']['id'] == str(admin_id)
        assert final['submittedBy']['id'] == str(admin_id)


def test_stale_editor_cannot_overwrite_the_other_accounts_changes(tmp_path):
    client, db, headers, _, _, admin_id, tenant_id, draft, _ = shared_order(tmp_path)
    with client:
        first = client.post('/v1/purchase-orders/submit', headers=headers, json=changed_draft(draft))
        assert first.status_code == 200
        stale = client.post('/v1/purchase-orders/submit', headers=headers,
            json=changed_draft(draft, 1, 21.0))
        assert stale.status_code == 409
        assert stale.json()['detail']['code'] == 'purchase_revision_conflict'
    with db.session_factory() as session:
        record = session.scalar(select(PurchaseOrder))
        assert record.draft_revision == 2 and record.draft_payload['items'][0]['guidePrice'] == 19.0
        assert session.scalar(select(func.count(PurchaseSyncOutbox.id))) == 2
        with pytest.raises(PurchaseServiceError) as caught:
            PurchaseOrderService(session).submit(tenant_id=tenant_id, actor_user_id=admin_id,
                draft=PurchaseDraft.model_validate(changed_draft(draft, 1, 22.0)))
        assert caught.value.code == 'purchase_revision_conflict'


def test_existing_changed_submission_requires_a_version_but_unchanged_retries_work(tmp_path):
    client, _, headers, _, _, _, _, draft, _ = shared_order(tmp_path)
    with client:
        same = client.post('/v1/purchase-orders/submit', headers=headers, json=draft)
        assert same.status_code == 200 and same.json()['data']['unchanged']
        changed = changed_draft(draft)
        del changed['expectedDraftRevision']
        rejected = client.post('/v1/purchase-orders/submit', headers=headers, json=changed)
        assert rejected.status_code == 409
        assert rejected.json()['detail']['code'] == 'purchase_revision_required'


@pytest.mark.parametrize('state', ['claimed', 'purchasing', 'ordered', 'returned', 'split'])
def test_shared_revision_cannot_bypass_procurement_execution(tmp_path, state):
    client, db, headers, _, operator_id, _, tenant_id, draft, _ = shared_order(tmp_path)
    with db.session_factory() as session:
        order = session.scalar(select(PurchaseOrder))
        if state == 'split':
            session.add(PurchaseSplit(tenant_id=tenant_id, purchase_order_id=order.id,
                purchaser_user_id=operator_id, split_no='SYNTH-SPLIT', site='US'))
        else:
            line = session.scalar(select(PurchaseOrderLine))
            line.workflow_status = state
            line.claimed_by_user_id = operator_id
        session.commit()
    with client:
        result = client.post('/v1/purchase-orders/submit', headers=headers, json=changed_draft(draft))
        assert result.status_code == 409
        assert result.json()['detail']['code'] == 'purchase_order_in_progress'
    with db.session_factory() as session:
        assert session.scalar(select(PurchaseOrder)).draft_revision == 1


def test_read_permission_does_not_grant_submit_and_tenant_boundary_remains(tmp_path):
    client, db, headers, role_id, operator_id, _, _, draft, _ = shared_order(tmp_path)
    with db.session_factory() as session:
        permission = session.scalar(select(Permission).where(Permission.code == 'procurement.request.submit'))
        session.execute(delete(RolePermission).where(RolePermission.role_id == role_id,
            RolePermission.permission_id == permission.id))
        session.commit()
    with client:
        assert client.post('/v1/purchase-orders/get', headers=headers,
            json={'orderKey': draft['orderKey']}).status_code == 200
        assert client.post('/v1/purchase-orders/submit', headers=headers,
            json=changed_draft(draft)).status_code == 403
    with db.session_factory() as session:
        with pytest.raises(PurchaseServiceError) as caught:
            PurchaseOrderService(session).get(tenant_id=uuid.uuid4(), order_key=draft['orderKey'],
                actor_user_id=operator_id, own_only=True)
        assert caught.value.code == 'purchase_order_not_found'


def test_concurrency_token_does_not_change_purchase_content_or_legacy_hash():
    legacy = PurchaseDraft.model_validate(sample_draft())
    versioned = PurchaseDraft.model_validate({**sample_draft(), 'expectedDraftRevision': 5})
    assert canonical_draft_dict(legacy) == canonical_draft_dict(versioned)
    assert draft_content_hash(legacy) == draft_content_hash(versioned)
    for invalid in (True, 0, -1, '1', 1.5):
        with pytest.raises(ValueError):
            PurchaseDraft.model_validate({**sample_draft(), 'expectedDraftRevision': invalid})
