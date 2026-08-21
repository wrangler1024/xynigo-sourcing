# -*- coding: utf-8 -*-
"""Runtime wiring for the configured Xynigo Feishu custom app."""
from .buyer_ledger_sync import BuyerLedgerSyncService
from .lark_openapi import LarkOpenApiClient


def build_buyer_ledger_service(config, credential_store, transport=None):
    config = config or {}
    client = LarkOpenApiClient(
        credential_provider=credential_store.load,
        base_token=config.get('larkBuyerBaseToken'),
        table_id=config.get('larkBuyerTableId'),
        transport=transport)
    return BuyerLedgerSyncService(client)
