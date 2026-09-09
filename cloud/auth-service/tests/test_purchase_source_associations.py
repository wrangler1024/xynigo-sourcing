from copy import deepcopy
import json
from pathlib import Path

import pytest
from test_purchase_api import authenticated_client, sample_draft
from xynigo_auth.purchase_contract import PurchaseDraft, canonical_draft_dict, validate_formal_submit

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/source_associations.json').read_text())


def test_extension_draft_and_submit_preserve_confirmed_sources_and_changed_purchase_quantities(tmp_path):
    client, _, headers = authenticated_client(tmp_path)
    with client:
        draft = deepcopy(FIXTURE['draft'])
        saved = client.post('/v1/purchase-orders/draft', json=draft, headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()['data']['draft']['items'][2]['sourceRef'] == draft['items'][2]['sourceRef']
        submitted = client.post('/v1/purchase-orders/submit', json=draft, headers=headers)
        assert submitted.status_code == 200, submitted.text
        result = submitted.json()['data']['draft']
        assert result['items'][2]['purchaseQty'] == 3
        assert result['items'][2]['sourceRef']['quantity'] == 1
        assert result['items'][3]['sourceRef']['mode'] == 'extra'
        assert not result['items'][3]['sourceAmountOwner']
        repeated = client.post('/v1/purchase-orders/submit', json=draft, headers=headers)
        assert repeated.status_code == 200 and repeated.json()['data']['unchanged'] is True
        # Changing a procurement link preserves its sales relation and revises the same draft.
        draft['items'][2]['purchaseLink'] = draft['items'][2]['purchaseLink'].replace('BUY-SPLIT', 'BUY-CHANGED')
        draft['items'][2]['skuCode'] = 'BUY-CHANGED'
        revised = client.post('/v1/purchase-orders/submit', json=draft, headers=headers)
        assert revised.status_code == 200, revised.text
        assert revised.json()['data']['purchaseOrderId'] == submitted.json()['data']['purchaseOrderId']
        assert revised.json()['data']['draft']['items'][2]['sourceRef'] == draft['items'][2]['sourceRef']


def test_unconfirmed_source_can_be_saved_but_not_submitted():
    raw = deepcopy(FIXTURE['draft'])
    raw['items'][1]['sourceRef']['mode'] = 'unconfirmed'
    draft = PurchaseDraft.model_validate(raw)
    assert canonical_draft_dict(draft)['items'][1]['sourceRef']['mode'] == 'unconfirmed'
    with pytest.raises(ValueError, match='请选择来源'):
        validate_formal_submit(draft)


@pytest.mark.parametrize('bad', ['key', 'package', 'extra_owner', 'duplicate_owner', 'missing_owner'])
def test_source_contract_prevents_cross_package_refs_and_duplicate_money_ownership(bad):
    raw = deepcopy(FIXTURE['draft'])
    if bad == 'key':
        raw['items'][1]['sourceRef']['sku'] = 'DIFFERENT-SKU'
    elif bad == 'package':
        raw['items'][1]['sourceRef']['orderKey'] = raw['systemOrderKey']
    elif bad == 'extra_owner':
        raw['items'][3]['sourceAmountOwner'] = True
    elif bad == 'duplicate_owner':
        raw['items'][2]['sourceAmountOwner'] = True
    else:
        raw['items'][1]['sourceAmountOwner'] = False
    with pytest.raises(ValueError):
        validate_formal_submit(PurchaseDraft.model_validate(raw))


def test_legacy_drafts_do_not_gain_optional_fields_or_change_legacy_validation():
    draft = PurchaseDraft.model_validate(sample_draft())
    canonical = canonical_draft_dict(draft)
    assert 'sourceRef' not in canonical['items'][0]
    assert 'sourceAmountOwner' not in canonical['items'][0]
    validate_formal_submit(draft)
