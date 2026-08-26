#!/usr/bin/env python3
"""Atomically update a supported non-secret runtime setting."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
import tempfile


SETTING_KEYS = {
    "login_success_path": "XYNIGO_AUTH_LOGIN_SUCCESS_PATH",
    "feishu_pkce_method": "XYNIGO_AUTH_FEISHU_PKCE_METHOD",
    "feishu_operation_sync_enabled": "XYNIGO_AUTH_FEISHU_OPERATION_SYNC_ENABLED",
    "feishu_purchase_sync_enabled": "XYNIGO_AUTH_FEISHU_PURCHASE_SYNC_ENABLED",
    "feishu_operation_base_token": "XYNIGO_AUTH_FEISHU_OPERATION_BASE_TOKEN",
    "feishu_purchase_base_token": "XYNIGO_AUTH_FEISHU_PURCHASE_BASE_TOKEN",
    "feishu_purchase_order_table_id": "XYNIGO_AUTH_FEISHU_PURCHASE_ORDER_TABLE_ID",
    "feishu_purchase_line_table_id": "XYNIGO_AUTH_FEISHU_PURCHASE_LINE_TABLE_ID",
    "feishu_buyer_account_table_id": "XYNIGO_AUTH_FEISHU_BUYER_ACCOUNT_TABLE_ID",
    "feishu_environment_result_table_id": (
        "XYNIGO_AUTH_FEISHU_ENVIRONMENT_RESULT_TABLE_ID"
    ),
    "feishu_logistics_result_table_id": (
        "XYNIGO_AUTH_FEISHU_LOGISTICS_RESULT_TABLE_ID"
    ),
}


def update_env(path: Path, setting: str, value: str) -> None:
    if (
        setting == "login_success_path"
        and (not value.startswith("/") or value.startswith("//") or any(char.isspace() for char in value))
    ):
        raise ValueError("invalid same-origin login success path")
    if setting == "feishu_pkce_method" and value not in {"S256", "plain", "disabled"}:
        raise ValueError("invalid Feishu PKCE method")
    if setting in {
        "feishu_operation_sync_enabled",
        "feishu_purchase_sync_enabled",
    } and value not in {"true", "false"}:
        raise ValueError("invalid Feishu sync flag")
    if setting in {
        "feishu_operation_base_token",
        "feishu_purchase_base_token",
    } and re.fullmatch(r"[A-Za-z0-9]{20,128}", value) is None:
        raise ValueError("invalid Feishu Base token")
    if setting in {
        "feishu_environment_result_table_id",
        "feishu_logistics_result_table_id",
        "feishu_buyer_account_table_id",
        "feishu_purchase_order_table_id",
        "feishu_purchase_line_table_id",
    } and not value.startswith("tbl"):
        raise ValueError("invalid Feishu operation table ID")

    env_key = SETTING_KEYS[setting]
    lines = path.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    matches = 0
    for line in lines:
        key, separator, _current_value = line.partition("=")
        if separator and key == env_key:
            matches += 1
            updated.append(f"{env_key}={value}")
        else:
            updated.append(line)
    if matches > 1:
        raise ValueError("runtime env contains the setting more than once")
    if matches == 0:
        updated.append(f"{env_key}={value}")

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".env.xynigo-setting.", dir=path.parent
    )
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(updated) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--setting", choices=sorted(SETTING_KEYS), required=True)
    parser.add_argument("--value", required=True)
    arguments = parser.parse_args()
    update_env(arguments.env_file, arguments.setting, arguments.value)
    print(f"updated_setting={arguments.setting}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
