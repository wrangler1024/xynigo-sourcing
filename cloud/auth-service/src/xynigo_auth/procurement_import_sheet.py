"""Cloud-native Feishu ordinary-Sheet gateway for procurement imports.

The local application uses ``lark-cli --as user``.  The cloud service must
not inherit a developer workstation profile, so this adapter calls the
official Sheets OpenAPI with the tenant application's identity.  A target
spreadsheet is still usable only after the document owner grants that
application access.
"""

from __future__ import annotations

import base64
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import quote, urlparse

import httpx

TOKEN_ENDPOINT = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
OPEN_API_ORIGIN = "https://open.feishu.cn"
SHEET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
SHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
ALLOWED_SHEET_HOSTS = ("feishu.cn", "larksuite.com")
RANGE_ROWS_RE = re.compile(r"![A-Z]+(\d+):[A-Z]+(\d+)$")


class LarkSheetSyncError(ValueError):
    """Stable, redacted error safe to return to a workspace user."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class LarkSheetReference:
    url: str
    spreadsheet_token: str
    hostname: str


@dataclass(frozen=True)
class SheetTable:
    headers: tuple[Any, ...]
    rows: tuple[tuple[int, tuple[Any, ...]], ...]
    revision: object = None


def _column_name(index: int) -> str:
    result = ""
    value = int(index)
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _column_index(name: str) -> int:
    value = 0
    for character in str(name or "").strip().upper():
        if character < "A" or character > "Z":
            raise LarkSheetSyncError("飞书列坐标无效")
        value = value * 26 + ord(character) - 64
    if value < 1:
        raise LarkSheetSyncError("飞书列坐标无效")
    return value


def parse_lark_sheet_url(value: object) -> LarkSheetReference:
    source = str(value or "").strip()
    try:
        parsed = urlparse(source)
    except ValueError as exc:
        raise LarkSheetSyncError("飞书电子表格链接格式无效") from exc
    hostname = str(parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or not any(
            hostname == suffix or hostname.endswith("." + suffix)
            for suffix in ALLOWED_SHEET_HOSTS
        )
    ):
        raise LarkSheetSyncError("只接受飞书或 Lark 官方 HTTPS 电子表格链接")
    parts = [item for item in parsed.path.split("/") if item]
    if len(parts) != 2 or parts[0] != "sheets":
        raise LarkSheetSyncError("请粘贴 /sheets/ 类型的普通飞书电子表格链接")
    token = parts[1]
    if not SHEET_TOKEN_RE.fullmatch(token):
        raise LarkSheetSyncError("飞书电子表格标识格式无效")
    return LarkSheetReference(
        url=f"https://{hostname}/sheets/{token}",
        spreadsheet_token=token,
        hostname=hostname,
    )


def normalize_sheet_id(value: object) -> str:
    sheet_id = str(value or "").strip()
    if not SHEET_ID_RE.fullmatch(sheet_id):
        raise LarkSheetSyncError("飞书工作表标识格式无效")
    return sheet_id


def _cell_contains_image(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key or "").replace("_", "-").casefold()
            if normalized in {"file-token", "image-token"} and child:
                return True
            if normalized in {"type", "kind"} and "image" in str(child).casefold():
                return True
            if _cell_contains_image(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_cell_contains_image(item) for item in value)
    if isinstance(value, str):
        return value.strip().replace("_", "-").casefold() in {
            "embed-image",
            "embedded-image",
            "[image]",
        }
    return False


def _cell_contains_link(value: object, expected_url: str = "") -> bool:
    wanted = str(expected_url or "").strip()
    if isinstance(value, dict):
        normalized = {
            str(key or "").replace("_", "-").casefold(): child
            for key, child in value.items()
        }
        link = str(normalized.get("link") or "").strip()
        item_type = str(normalized.get("type") or "").casefold()
        if link and item_type in {"url", "link"}:
            return not wanted or link == wanted
        return any(_cell_contains_link(child, wanted) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_cell_contains_link(item, wanted) for item in value)
    return False


def _plain_cell(value: object) -> object:
    if isinstance(value, dict):
        if _cell_contains_image(value):
            return ""
        if "text" in value and isinstance(value.get("text"), (str, int, float)):
            return value.get("text")
        if "value" in value:
            return _plain_cell(value.get("value"))
        return ""
    if isinstance(value, list):
        if len(value) == 1:
            return _plain_cell(value[0])
        return "".join(str(_plain_cell(item) or "") for item in value)
    return value


def _excel_date_serial(value: object) -> object:
    if isinstance(value, datetime):
        target = value.date()
    elif isinstance(value, date):
        target = value
    else:
        matched = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value or "").strip())
        if not matched:
            return value
        target = date(*(int(part) for part in matched.groups()))
    return (target - date(1899, 12, 30)).days


def _feishu_number_formatter(value: object) -> str:
    """Translate shared Excel-style formats to the Sheets v2 formatter dialect."""

    formatter = str(value or "").strip()
    return {
        "yyyy-mm-dd": "yyyy-MM-dd",
        "yyyy/mm/dd": "yyyy/MM/dd",
    }.get(formatter, formatter)


class FeishuSheetsGateway:
    """Tenant-identity Sheets client implementing the local gateway contract."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "")
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport
        self._token_lock = threading.Lock()
        self._token_value = ""
        self._token_expires_at = 0.0
        self._sheet_cache: dict[tuple[str, str], str] = {}
        self._new_rows: set[tuple[str, str, int]] = set()
        self._background_cache: dict[tuple[str, str, int], str] = {}
        self._image_cache: set[tuple[str, str, int, str]] = set()
        self._link_cache: dict[tuple[str, str, int, str], str] = {}
        if not self.app_id or not self.app_secret:
            raise LarkSheetSyncError("云端飞书应用凭证未配置")

    def _tenant_token(self, *, force: bool = False) -> str:
        now = time.monotonic()
        with self._token_lock:
            if not force and self._token_value and now < self._token_expires_at:
                return self._token_value
            try:
                with httpx.Client(
                    timeout=self.timeout_seconds, transport=self.transport
                ) as client:
                    response = client.post(
                        TOKEN_ENDPOINT,
                        json={"app_id": self.app_id, "app_secret": self.app_secret},
                        headers={"Accept": "application/json"},
                    )
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise LarkSheetSyncError("获取云端飞书应用凭证失败") from exc
            if (
                response.status_code >= 400
                or not isinstance(payload, dict)
                or payload.get("code", 0) != 0
                or not isinstance(payload.get("tenant_access_token"), str)
            ):
                raise LarkSheetSyncError("获取云端飞书应用凭证失败")
            self._token_value = payload["tenant_access_token"]
            try:
                expires = max(60, int(payload.get("expire") or 7200))
            except (TypeError, ValueError):
                expires = 7200
            self._token_expires_at = now + max(30, expires - 60)
            return self._token_value

    @staticmethod
    def _provider_error(
        payload: dict[str, Any], status_code: int
    ) -> LarkSheetSyncError:
        code = payload.get("code", status_code)
        try:
            stable_code = int(code)
        except (TypeError, ValueError):
            stable_code = None
        if stable_code == 99991672:
            return LarkSheetSyncError(
                "当前应用尚未开通飞书电子表格应用身份权限，请开通 "
                "sheets:spreadsheet 并发布应用版本",
                code=stable_code,
            )
        if stable_code == 90204:
            return LarkSheetSyncError(
                "飞书暂未接受电子表格写入请求参数（错误码 90204）",
                code=stable_code,
            )
        if stable_code in {90213, 91403}:
            return LarkSheetSyncError(
                "当前应用没有该飞书电子表格的编辑权限，请将“小犀代采”文档应用设为可编辑",
                code=stable_code,
            )
        if stable_code == 90218:
            return LarkSheetSyncError(
                "目标工作表包含受保护单元格，当前应用无法修改",
                code=stable_code,
            )
        if code == 1310213 or status_code == 403:
            return LarkSheetSyncError(
                "当前应用无权访问该飞书电子表格，请在表格中添加“小犀代采”文档应用",
                code=stable_code,
            )
        if code in {1310214, 1310215, 1310249}:
            return LarkSheetSyncError("飞书电子表格或工作表不存在", code=stable_code)
        if status_code == 429 or code in {1310217, 1254290, 1254291}:
            return LarkSheetSyncError(
                "飞书电子表格请求过于频繁，请稍后重试", code=stable_code
            )
        return LarkSheetSyncError(
            f"飞书电子表格请求失败（错误码 {code}）", code=stable_code
        )

    @staticmethod
    def _retry_idempotent_write(operation):
        """Retry one transient 90204 only for writes safe to repeat verbatim."""

        for attempt in range(2):
            try:
                return operation()
            except LarkSheetSyncError as exc:
                if exc.code != 90204 or attempt:
                    raise
                time.sleep(0.25)
        raise LarkSheetSyncError("飞书电子表格幂等写入重试失败")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        for attempt in range(2):
            token = self._tenant_token(force=attempt > 0)
            try:
                with httpx.Client(
                    timeout=self.timeout_seconds, transport=self.transport
                ) as client:
                    response = client.request(
                        method,
                        OPEN_API_ORIGIN + path,
                        params=params,
                        json=json_body,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/json",
                            "Content-Type": "application/json; charset=utf-8",
                        },
                    )
                if len(response.content) > 10 * 1024 * 1024:
                    raise LarkSheetSyncError("目标工作表数据超过云端安全读取上限")
                payload = response.json()
            except LarkSheetSyncError:
                raise
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise LarkSheetSyncError("飞书电子表格请求超时，请稍后重试") from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise LarkSheetSyncError("飞书电子表格返回结构无效") from exc
            if not isinstance(payload, dict):
                raise LarkSheetSyncError("飞书电子表格返回结构无效")
            code = payload.get("code", 0)
            if code in {99991663, 99991671} and attempt == 0:
                continue
            if response.status_code >= 400 or code != 0:
                raise self._provider_error(payload, response.status_code)
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        raise LarkSheetSyncError("飞书电子表格认证已失效")

    @staticmethod
    def _token(url: object) -> tuple[LarkSheetReference, str]:
        reference = parse_lark_sheet_url(url)
        return reference, quote(reference.spreadsheet_token, safe="")

    def inspect(self, url: object) -> dict[str, object]:
        reference, token = self._token(url)
        data = self._request(
            "GET", f"/open-apis/sheets/v3/spreadsheets/{token}/sheets/query"
        )
        sheets = []
        for item in data.get("sheets") or ():
            if not isinstance(item, dict) or item.get("resource_type") != "sheet":
                continue
            sheet_id = normalize_sheet_id(item.get("sheet_id"))
            name = str(item.get("title") or "").strip()
            grid = item.get("grid_properties") or {}
            if not name or not isinstance(grid, dict):
                continue
            self._sheet_cache[(reference.spreadsheet_token, name)] = sheet_id
            sheets.append(
                {
                    "sheetId": sheet_id,
                    "sheetName": name,
                    "rowCount": int(grid.get("row_count") or 0),
                    "columnCount": int(grid.get("column_count") or 0),
                    "hidden": bool(item.get("hidden")),
                }
            )
        if not sheets:
            raise LarkSheetSyncError("该飞书电子表格没有可用工作表")
        return {
            "url": reference.url,
            "spreadsheetToken": reference.spreadsheet_token,
            "revision": data.get("revision"),
            "sheets": sheets,
        }

    def _sheet_id_for_name(self, url: object, sheet_name: object) -> str:
        reference = parse_lark_sheet_url(url)
        name = str(sheet_name or "").strip()
        cached = self._sheet_cache.get((reference.spreadsheet_token, name))
        if cached:
            return cached
        matches = [
            item["sheetId"]
            for item in self.inspect(reference.url)["sheets"]
            if item["sheetName"] == name
        ]
        if len(matches) != 1:
            raise LarkSheetSyncError("飞书工作表名称为空、重复或已变化")
        return str(matches[0])

    def _read_range(
        self,
        url: object,
        sheet_id: object,
        cell_range: str,
        *,
        raw: bool = False,
    ) -> dict[str, Any]:
        _reference, token = self._token(url)
        sheet = normalize_sheet_id(sheet_id)
        full_range = f"{sheet}!{cell_range}"
        return self._request(
            "GET",
            f"/open-apis/sheets/v2/spreadsheets/{token}/values/{quote(full_range, safe='')}",
            params=(
                None
                if raw
                else {
                    "valueRenderOption": "ToString",
                    "dateTimeRenderOption": "FormattedString",
                }
            ),
        )

    def read_table(self, url: object, sheet_id: object) -> SheetTable:
        sheet = normalize_sheet_id(sheet_id)
        info = self.inspect(url)
        selected = next(
            (item for item in info["sheets"] if item["sheetId"] == sheet), None
        )
        if not selected:
            raise LarkSheetSyncError("所选飞书工作表已不存在")
        row_count = max(1, min(int(selected["rowCount"] or 1), 100_000))
        data = self._read_range(url, sheet, f"A1:AZ{row_count}")
        value_range = data.get("valueRange") or {}
        values = value_range.get("values") or []
        if not isinstance(values, list) or not values:
            raise LarkSheetSyncError("目标工作表缺少第 1 行表头")
        normalized: list[tuple[object, ...]] = []
        for raw_row in values:
            row = tuple(_plain_cell(value) for value in (raw_row or []))
            while row and row[-1] in (None, ""):
                row = row[:-1]
            normalized.append(row)
        if not normalized[0]:
            raise LarkSheetSyncError("目标工作表缺少第 1 行表头")
        rows = tuple(
            (index, row)
            for index, row in enumerate(normalized[1:], start=2)
            if any(value not in (None, "") for value in row)
        )
        return SheetTable(
            headers=normalized[0],
            rows=rows,
            revision=data.get("revision") or value_range.get("revision"),
        )

    def _write_values(
        self, url: object, sheet_id: object, cell_range: str, values: list[list[object]]
    ) -> dict[str, Any]:
        _reference, token = self._token(url)
        sheet = normalize_sheet_id(sheet_id)
        return self._request(
            "PUT",
            f"/open-apis/sheets/v2/spreadsheets/{token}/values",
            json_body={
                "valueRange": {"range": f"{sheet}!{cell_range}", "values": values}
            },
        )

    def _write_value_ranges(
        self, url: object, ranges: list[dict[str, object]]
    ) -> dict[str, Any]:
        _reference, token = self._token(url)
        return self._request(
            "POST",
            f"/open-apis/sheets/v2/spreadsheets/{token}/values_batch_update",
            json_body={"valueRanges": ranges},
        )

    def _style_ranges(
        self, url: object, data: list[dict[str, object]]
    ) -> dict[str, Any]:
        if not data:
            return {"skipped": True}
        normalized = []
        for item in data:
            style = dict(item.get("style") or {})
            if "formatter" in style:
                style["formatter"] = _feishu_number_formatter(style["formatter"])
            normalized.append({**item, "style": style})
        _reference, token = self._token(url)
        return self._request(
            "PUT",
            f"/open-apis/sheets/v2/spreadsheets/{token}/styles_batch_update",
            json_body={"data": normalized},
        )

    def _resize(
        self,
        url: object,
        sheet_id: object,
        *,
        major: str,
        start: int,
        end: int,
        size: int,
    ) -> dict[str, Any]:
        _reference, token = self._token(url)
        return self._request(
            "PUT",
            f"/open-apis/sheets/v2/spreadsheets/{token}/dimension_range",
            json_body={
                "dimension": {
                    "sheetId": normalize_sheet_id(sheet_id),
                    "majorDimension": major,
                    "startIndex": int(start),
                    "endIndex": int(end),
                },
                "dimensionProperties": {"visible": True, "fixedSize": int(size)},
            },
        )

    def _insert_columns(
        self, url: object, sheet_id: object, start_index: int, count: int
    ) -> None:
        _reference, token = self._token(url)
        self._request(
            "POST",
            f"/open-apis/sheets/v2/spreadsheets/{token}/insert_dimension_range",
            json_body={
                "dimension": {
                    "sheetId": normalize_sheet_id(sheet_id),
                    "majorDimension": "COLUMNS",
                    "startIndex": int(start_index),
                    "endIndex": int(start_index + count),
                },
                "inheritStyle": "BEFORE",
            },
        )

    def _delete_column(
        self, url: object, sheet_id: object, one_based_index: int
    ) -> None:
        _reference, token = self._token(url)
        self._request(
            "DELETE",
            f"/open-apis/sheets/v2/spreadsheets/{token}/dimension_range",
            json_body={
                "dimension": {
                    "sheetId": normalize_sheet_id(sheet_id),
                    "majorDimension": "COLUMNS",
                    "startIndex": int(one_based_index),
                    "endIndex": int(one_based_index),
                }
            },
        )

    def append_table_rows(
        self,
        url: object,
        sheet_name: object,
        columns: object,
        rows: object,
        dtypes: dict[str, str] | None = None,
        formats: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = [str(item or "").strip() for item in columns or ()]
        values = [list(row) for row in rows or ()]
        if not values:
            return {"skipped": True, "reason": "empty"}
        if not headers or any(len(row) != len(headers) for row in values):
            raise LarkSheetSyncError("待写入的飞书数据列数与表头不一致")
        sheet_id = self._sheet_id_for_name(url, sheet_name)
        table = self.read_table(url, sheet_id)
        start_row = max([row for row, _values in table.rows] or [1]) + 1
        converted = []
        for raw in values:
            row = []
            for index, value in enumerate(raw):
                header = headers[index]
                if str((dtypes or {}).get(header) or "").startswith("datetime"):
                    value = _excel_date_serial(value)
                row.append(value)
            converted.append(row)
        last_column = _column_name(len(headers))
        end_row = start_row + len(converted) - 1
        _reference, token = self._token(url)
        data = self._request(
            "POST",
            f"/open-apis/sheets/v2/spreadsheets/{token}/values_append",
            params={"insertDataOption": "INSERT_ROWS"},
            json_body={
                "valueRange": {
                    "range": f"{sheet_id}!A{start_row}:{last_column}{end_row}",
                    "values": converted,
                }
            },
        )
        updated = (data.get("updates") or {}).get("updatedRange")
        matched = RANGE_ROWS_RE.search(str(updated or ""))
        actual_start = int(matched.group(1)) if matched else start_row
        actual_end = int(matched.group(2)) if matched else end_row
        reference = parse_lark_sheet_url(url)
        for row_number in range(actual_start, actual_end + 1):
            self._new_rows.add((reference.spreadsheet_token, sheet_id, row_number))
        style_data = []
        for index, header in enumerate(headers, start=1):
            formatter = str((formats or {}).get(header) or "").strip()
            if formatter:
                column = _column_name(index)
                style_data.append(
                    {
                        "ranges": [
                            f"{sheet_id}!{column}{actual_start}:{column}{actual_end}"
                        ],
                        "style": {"formatter": formatter},
                    }
                )
        self._style_ranges(url, style_data)
        return {"updated_rows_count": len(converted), "updatedRange": updated}

    def normalize_collaboration_headers(
        self,
        url: object,
        sheet_id: object,
        sheet_name: object,
        headers: object,
        last_row: int = 1,
        rows: object = (),
    ) -> dict[str, object]:
        sheet = normalize_sheet_id(sheet_id)
        actual = [str(item or "").strip() for item in headers or ()]
        row_values = [(int(number), list(values)) for number, values in rows or ()]
        aliases = {
            "分单标记": "分单日期",
            "分单时间": "分单日期",
            "采购单号": "包裹号",
            "销售金额": "销售订单金额",
            "平台订单号": "采购订单号",
            "收件人": "收货人姓名",
            "国家": "收货人国家",
            "收件地址": "地址1",
            "电话": "收货人电话",
        }
        operations = 0
        for index, value in enumerate(tuple(actual), start=1):
            replacement = aliases.get(value)
            if replacement and value in {
                "分单标记",
                "分单时间",
                "采购单号",
                "销售金额",
                "平台订单号",
            }:
                column = _column_name(index)
                self._write_values(url, sheet, f"{column}1:{column}1", [[replacement]])
                actual[index - 1] = replacement
                operations += 1

        canonical = [aliases.get(value, value) for value in actual]
        if "分单批次" in canonical:
            split_index = canonical.index("分单批次")
            if "导入批次" not in canonical:
                raise LarkSheetSyncError("目标表缺少导入批次，不能安全删除分单批次")
            import_index = canonical.index("导入批次")
            for row_number, raw in row_values:
                padded = raw + [""] * max(0, len(canonical) - len(raw))
                split_value = str(padded[split_index] or "").strip()
                import_value = str(padded[import_index] or "").strip()
                if split_value and split_value != import_value:
                    raise LarkSheetSyncError(
                        f"第 {row_number} 行分单批次未被导入批次等值覆盖，已停止删列"
                    )
            self._delete_column(url, sheet, split_index + 1)
            canonical.pop(split_index)
            actual.pop(split_index)
            for _number, raw in row_values:
                if split_index < len(raw):
                    raw.pop(split_index)
            operations += 1

        def insert_column(index: int, header: str) -> None:
            nonlocal operations
            self._insert_columns(url, sheet, index, 1)
            column = _column_name(index + 1)
            self._write_values(url, sheet, f"{column}1:{column}1", [[header]])
            canonical.insert(index, header)
            actual.insert(index, header)
            for _number, raw in row_values:
                raw.insert(index, "")
            operations += 2

        if "分单日期" not in canonical:
            insert_column(0, "分单日期")
        if "商品金额" not in canonical:
            if "销售订单金额" not in canonical:
                raise LarkSheetSyncError("目标表缺少销售订单金额，无法安全插入商品金额")
            insert_column(canonical.index("销售订单金额") + 1, "商品金额")
        if "导入操作人" not in canonical:
            if "系统订单键" not in canonical:
                raise LarkSheetSyncError("目标表缺少系统订单键，无法安全插入导入操作人")
            insert_column(canonical.index("系统订单键") + 1, "导入操作人")

        receiver_fields = [
            "收货人姓名",
            "收货人国家",
            "收货人州/省",
            "收货人城市",
            "地址1",
            "地址2",
            "邮编",
            "收货人电话",
        ]
        guide_index = canonical.index("采购指导价")
        if (
            canonical[guide_index + 1 : guide_index + 1 + len(receiver_fields)]
            != receiver_fields
        ):
            legacy_names = [
                name
                for name in ("收件人", "国家", "收件地址", "邮编", "电话")
                if name in actual
            ]
            if not legacy_names:
                raise LarkSheetSyncError(
                    "目标表收货人字段不完整或位置错误，无法自动安全迁移"
                )
            captured: dict[str, list[object]] = {}
            for name in legacy_names:
                index = actual.index(name)
                captured[aliases.get(name, name)] = [
                    raw[index] if index < len(raw) else ""
                    for _number, raw in row_values
                ]
            insert_at = guide_index + 1
            self._insert_columns(url, sheet, insert_at, len(receiver_fields))
            start_column = _column_name(insert_at + 1)
            end_column = _column_name(insert_at + len(receiver_fields))
            writes = [
                {
                    "range": f"{sheet}!{start_column}1:{end_column}1",
                    "values": [receiver_fields],
                }
            ]
            for row_index, (row_number, _raw) in enumerate(row_values):
                writes.append(
                    {
                        "range": (
                            f"{sheet}!{start_column}{row_number}:"
                            f"{end_column}{row_number}"
                        ),
                        "values": [
                            [
                                captured.get(name, [""] * len(row_values))[row_index]
                                for name in receiver_fields
                            ]
                        ],
                    }
                )
            for offset in range(0, len(writes), 100):
                self._write_value_ranges(url, writes[offset : offset + 100])
            actual[insert_at:insert_at] = receiver_fields
            canonical[insert_at:insert_at] = receiver_fields
            for _number, raw in row_values:
                raw[insert_at:insert_at] = [""] * len(receiver_fields)
            operations += 2
            for name in sorted(
                legacy_names, key=lambda item: actual.index(item), reverse=True
            ):
                index = actual.index(name)
                self._delete_column(url, sheet, index + 1)
                actual.pop(index)
                canonical.pop(index)
                for _number, raw in row_values:
                    if index < len(raw):
                        raw.pop(index)
                operations += 1
        return {"operations": operations}

    def reorder_collaboration_headers(
        self,
        url: object,
        sheet_id: object,
        sheet_name: object,
        headers: object,
        desired_headers: object,
    ) -> dict[str, object]:
        del sheet_name
        sheet = normalize_sheet_id(sheet_id)
        actual = [str(item or "").strip() for item in headers or ()]
        _reference, token = self._token(url)
        results = []
        for target_index, header in enumerate(tuple(desired_headers or ())):
            if header not in actual:
                raise LarkSheetSyncError(
                    f"目标表缺少“{header}”，无法规范采购需求区顺序"
                )
            source_index = actual.index(header)
            if source_index == target_index:
                continue
            results.append(
                self._request(
                    "POST",
                    f"/open-apis/sheets/v3/spreadsheets/{token}/sheets/{quote(sheet, safe='')}/move_dimension",
                    json_body={
                        "source": {
                            "major_dimension": "COLUMNS",
                            "start_index": source_index,
                            "end_index": source_index,
                        },
                        "destination_index": target_index,
                    },
                )
            )
            value = actual.pop(source_index)
            actual.insert(target_index, value)
        return {"operations": len(results), "results": results}

    def normalize_date_column(
        self,
        url: object,
        sheet_name: object,
        headers: object,
        rows: object,
        header: str = "分单日期",
        number_format: str = "yyyy-mm-dd",
    ) -> dict[str, object]:
        names = [str(item or "").strip() for item in headers or ()]
        if header not in names:
            raise LarkSheetSyncError("目标表缺少分单日期")
        column_index = names.index(header)
        parsed = []
        for row_number, raw in rows or ():
            value = raw[column_index] if column_index < len(raw) else ""
            if value in (None, ""):
                continue
            serial = _excel_date_serial(value)
            if serial == value:
                raise LarkSheetSyncError(
                    f"第 {int(row_number)} 行分单日期不是有效日期，已停止类型转换"
                )
            parsed.append((int(row_number), serial))
        if not parsed:
            return {"operations": 0, "skipped": True}
        sheet_id = self._sheet_id_for_name(url, sheet_name)
        column = _column_name(column_index + 1)
        ranges = [
            {"range": f"{sheet_id}!{column}{row}:{column}{row}", "values": [[value]]}
            for row, value in parsed
        ]
        self._retry_idempotent_write(lambda: self._write_value_ranges(url, ranges))
        style_data = [
            {
                "ranges": [
                    f"{sheet_id}!{column}{min(row for row, _ in parsed)}:{column}{max(row for row, _ in parsed)}"
                ],
                "style": {"formatter": number_format},
            }
        ]
        self._retry_idempotent_write(lambda: self._style_ranges(url, style_data))
        return {"operations": 2, "rows": len(parsed)}

    def apply_header_presentation(
        self, url: object, sheet_id: object, sheet_name: object, zones: object
    ) -> dict[str, object]:
        del sheet_name
        sheet = normalize_sheet_id(sheet_id)
        items = list(zones or ())
        if not items:
            return {"operations": 0, "skipped": True}
        style_data = []
        for item in items:
            style_data.append(
                {
                    "ranges": [
                        f"{sheet}!{str(item['start']).upper()}1:{str(item['end']).upper()}1"
                    ],
                    "style": {
                        "font": {"bold": True},
                        "foreColor": "#FFFFFF",
                        "backColor": str(item["color"]),
                        "hAlign": 1,
                        "vAlign": 1,
                    },
                }
            )
        self._retry_idempotent_write(lambda: self._style_ranges(url, style_data))
        self._retry_idempotent_write(
            lambda: self._resize(url, sheet, major="ROWS", start=1, end=1, size=36)
        )
        _reference, token = self._token(url)
        freeze_body = {
            "requests": [
                {
                    "updateSheet": {
                        "properties": {
                            "sheetId": sheet,
                            "frozenRowCount": 1,
                            "frozenColCount": 5,
                        }
                    }
                }
            ]
        }
        self._retry_idempotent_write(
            lambda: self._request(
                "POST",
                f"/open-apis/sheets/v2/spreadsheets/{token}/sheets_batch_update",
                json_body=freeze_body,
            )
        )
        return {"operations": 3}

    def row_backgrounds(
        self, url: object, sheet_id: object, row_numbers: object
    ) -> dict[int, str]:
        reference = parse_lark_sheet_url(url)
        sheet = normalize_sheet_id(sheet_id)
        result = {}
        for row in sorted({int(item) for item in row_numbers if int(item) >= 2}):
            key = (reference.spreadsheet_token, sheet, row)
            if key in self._background_cache:
                result[row] = self._background_cache[key]
            elif key in self._new_rows:
                result[row] = ""
            else:
                # The public API cannot read background styles.  Preserve all
                # pre-existing rows rather than risking an operator task color.
                result[row] = "#PRESERVE"
        return result

    def apply_row_presentation(
        self,
        url: object,
        sheet_name: object,
        background_bands: object,
        row_ranges: object,
        row_height: int = 52,
        last_column: str = "AH",
    ) -> dict[str, object]:
        sheet = self._sheet_id_for_name(url, sheet_name)
        reference = parse_lark_sheet_url(url)
        ranges = list(row_ranges or ())
        style_data = []
        for item in background_bands or ():
            start = int(item["start"])
            end = int(item["end"])
            color = str(item["color"]).upper()
            style_data.append(
                {
                    "ranges": [f"{sheet}!A{start}:{str(last_column).upper()}{end}"],
                    "style": {"backColor": color, "vAlign": 1},
                }
            )
            for row in range(start, end + 1):
                self._background_cache[(reference.spreadsheet_token, sheet, row)] = (
                    color
                )
        self._retry_idempotent_write(lambda: self._style_ranges(url, style_data))
        for start, end in ranges:
            self._retry_idempotent_write(
                lambda start=start, end=end: self._resize(
                    url,
                    sheet,
                    major="ROWS",
                    start=int(start),
                    end=int(end),
                    size=int(row_height),
                )
            )
        return {"operations": len(style_data) + len(ranges)}

    def _raw_column_values(
        self, url: object, sheet_id: object, column: str, row_numbers: list[int]
    ) -> dict[int, object]:
        if not row_numbers:
            return {}
        start, end = min(row_numbers), max(row_numbers)
        data = self._read_range(
            url, sheet_id, f"{column}{start}:{column}{end}", raw=True
        )
        values = (data.get("valueRange") or {}).get("values") or []
        result = {row: None for row in row_numbers}
        for offset, row_values in enumerate(values):
            row = start + offset
            if row in result and isinstance(row_values, list) and row_values:
                result[row] = row_values[0]
        return result

    def hyperlink_presence(
        self, url: object, sheet_id: object, expected_links: object, column: str = "N"
    ) -> dict[int, bool]:
        links = {
            int(row): str(link or "").strip()
            for row, link in dict(expected_links or {}).items()
            if int(row) >= 2 and str(link or "").strip()
        }
        if not links:
            return {}
        reference = parse_lark_sheet_url(url)
        sheet = normalize_sheet_id(sheet_id)
        raw = self._raw_column_values(url, sheet, str(column).upper(), sorted(links))
        result = {}
        for row, link in links.items():
            cached = self._link_cache.get(
                (reference.spreadsheet_token, sheet, row, str(column).upper())
            )
            result[row] = cached == link or _cell_contains_link(raw.get(row), link)
        return result

    def set_hyperlinks(
        self, url: object, sheet_id: object, links: object, column: str = "N"
    ) -> dict[str, object]:
        reference = parse_lark_sheet_url(url)
        sheet = normalize_sheet_id(sheet_id)
        ranges = []
        for row, link in links or ():
            number = int(row)
            target = str(link or "").strip()
            if number < 2 or not target:
                continue
            ranges.append(
                {
                    "range": f"{sheet}!{str(column).upper()}{number}:{str(column).upper()}{number}",
                    "values": [
                        [{"text": "打开采购链接", "link": target, "type": "url"}]
                    ],
                }
            )
            self._link_cache[
                (reference.spreadsheet_token, sheet, number, str(column).upper())
            ] = target
        for offset in range(0, len(ranges), 100):
            self._write_value_ranges(url, ranges[offset : offset + 100])
        return {"operations": (len(ranges) + 99) // 100, "skipped": not ranges}

    def image_presence(
        self, url: object, sheet_id: object, row_numbers: object, column: str = "M"
    ) -> dict[int, bool]:
        numbers = sorted({int(item) for item in row_numbers if int(item) >= 2})
        if not numbers:
            return {}
        reference = parse_lark_sheet_url(url)
        sheet = normalize_sheet_id(sheet_id)
        normalized_column = str(column).upper()
        raw = self._raw_column_values(url, sheet, normalized_column, numbers)
        return {
            row: (
                (reference.spreadsheet_token, sheet, row, normalized_column)
                in self._image_cache
                or _cell_contains_image(raw.get(row))
            )
            for row in numbers
        }

    def set_image(
        self,
        url: object,
        sheet_id: object,
        row_number: int,
        image_bytes: bytes,
        mime: str,
        column: str = "M",
    ) -> dict[str, Any]:
        row = int(row_number)
        if row < 2:
            raise LarkSheetSyncError("拒绝覆盖飞书表头图片单元格")
        suffix = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
        }.get(str(mime or "").casefold())
        if not suffix:
            raise LarkSheetSyncError("订单商品图片格式不受飞书补图支持")
        reference, token = self._token(url)
        sheet = normalize_sheet_id(sheet_id)
        normalized_column = str(column).upper()
        cell = f"{sheet}!{normalized_column}{row}:{normalized_column}{row}"
        result = self._request(
            "POST",
            f"/open-apis/sheets/v2/spreadsheets/{token}/values_image",
            json_body={
                "range": cell,
                "image": base64.b64encode(bytes(image_bytes)).decode("ascii"),
                "name": "order-image" + suffix,
            },
        )
        self._image_cache.add(
            (reference.spreadsheet_token, sheet, row, normalized_column)
        )
        return result

    def verify_image(
        self, url: object, sheet_id: object, row_number: int, column: str = "M"
    ) -> bool:
        return bool(
            self.image_presence(url, sheet_id, [row_number], column=column).get(
                int(row_number)
            )
        )
