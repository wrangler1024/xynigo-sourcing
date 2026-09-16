"""Receipt-specific fresh reads with shared connections, never cached sheet rows."""
from collections import OrderedDict
import hashlib
import threading
import time

import httpx

from .procurement_import_sheet import FeishuSheetsGateway, SheetTable, LarkSheetSyncError, _plain_cell


class ReceiptSheetsGateway(FeishuSheetsGateway):
    def __init__(self, *, token_source, **kwargs):
        super().__init__(**kwargs)
        self.token_source = token_source
        self.metrics = {'sheetHttpRequests': 0, 'feishuMs': 0, 'fullTableReads': 0}

    def _tenant_token(self, *, force=False):
        return self.token_source._tenant_token(force=force)

    def _request(self, *args, **kwargs):
        start = time.perf_counter()
        try:
            return super()._request(*args, **kwargs)
        finally:
            self.metrics['feishuMs'] += round((time.perf_counter()-start)*1000, 2)

    def read_image_cells(self, url, sheet_id, column, rows):
        first, last = min(rows), max(rows)
        data = self._read_range(url, sheet_id, f'{column}{first}:{column}{last}', raw=True)
        values = (data.get('valueRange') or {}).get('values') or []
        return [values[row-first][0] if row-first < len(values) and values[row-first] else None for row in rows]

    def read_money_cells(self, url, sheet_id, column, rows):
        return self.read_image_cells(url, sheet_id, column, rows)

    def read_table(self, url, sheet_id):
        # Preserve formatted identity fields (e.g. leading-zero order numbers).
        # An open-ended range includes newly inserted/moved rows. Every guard
        # remains fresh; no remembered row index or stale grid size is trusted.
        self.metrics['fullTableReads'] += 1
        data = self._read_range(url, sheet_id, 'A1:AZ')
        values = (data.get('valueRange') or {}).get('values') or []
        if not values or not values[0]:
            raise LarkSheetSyncError('目标工作表缺少第 1 行表头')
        headers = tuple(_plain_cell(x) for x in values[0])
        while headers and headers[-1] in (None, ''):
            headers = headers[:-1]
        rows = []
        for number, cells in enumerate(values[1:], start=2):
            row = tuple(_plain_cell(x) for x in cells)
            if any(x not in (None, '') for x in row):
                rows.append((number, row))
        return SheetTable(headers, tuple(rows), data.get('revision'))


class ReceiptGatewayFactory:
    """One connection pool; bounded tenant+credential-specific token caches."""
    def __init__(self, transport=None):
        self.client = httpx.Client(timeout=20, transport=transport,
                                   limits=httpx.Limits(max_connections=40, max_keepalive_connections=20))
        self.tokens = OrderedDict()
        self.lock = threading.Lock()

    def create(self, tenant_id, app_id, app_secret):
        key = (str(tenant_id), app_id, hashlib.sha256(app_secret.encode()).digest())
        with self.lock:
            source = self.tokens.pop(key, None)
            if source is None:
                source = FeishuSheetsGateway(app_id=app_id, app_secret=app_secret, http_client=self.client)
            self.tokens[key] = source
            while len(self.tokens) > 128:
                self.tokens.popitem(last=False)
        return ReceiptSheetsGateway(app_id=app_id, app_secret=app_secret,
                                    http_client=self.client, token_source=source)

    def close(self):
        self.client.close()
        self.tokens.clear()
