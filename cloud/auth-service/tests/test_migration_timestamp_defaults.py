# -*- coding: utf-8 -*-
"""迁移里的非空时间列必须带库端默认值。

踩过的坑（20260916 真机）：0038 建表把 created_at/updated_at 写成 nullable=False
却没给 server_default，而模型侧声明了 server_default=func.now() —— 于是
SQLAlchemy 的 INSERT 省略这两列，Postgres 直接 NOT NULL 违约：

    null value in column "created_at" of relation "after_sale_refund_tracking"

后果是 ④ 退款回访每轮上报都 500（执行器把上报异常吞掉，任务还显示 succeeded、
跟踪表一行没有）。**测试全绿也拦不住**：用例用 models 元数据建表，server_default
在 DDL 里生效，只有「迁移建表 + 真库」这条路径才炸。故常驻此静态检查。
"""
from __future__ import annotations

import pathlib
import re

MIGRATIONS = pathlib.Path(__file__).parents[1] / "migrations" / "versions"
COLUMN_CALL = re.compile(r'sa\.Column\(\s*"(created_at|updated_at)"')


def _call_text(text: str, start: int) -> str:
    """截出这一段 sa.Column( ... )（按括号配平，不受换行与后续列影响）。"""
    index = text.index("(", start)
    depth = 0
    for position in range(index, len(text)):
        if text[position] == "(":
            depth += 1
        elif text[position] == ")":
            depth -= 1
            if depth == 0:
                return text[start:position + 1]
    return text[start:]


def test_non_null_timestamps_declare_server_default() -> None:
    offenders = []
    for path in sorted(MIGRATIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in COLUMN_CALL.finditer(text):
            call = _call_text(text, match.start())
            if "nullable=False" in call and "server_default" not in call:
                offenders.append(f"{path.name}: {match.group(1)}")
    assert not offenders, (
        "非空时间列缺 server_default（ORM 插入会省略该列 → NOT NULL 违约）："
        + "；".join(offenders))


def test_after_sale_tracking_table_has_timestamp_defaults() -> None:
    """点名的回归位：0038 建表漏写、0039 补上。"""
    migration = (MIGRATIONS
                 / "0039_tracking_timestamp_defaults.py")
    assert migration.exists(), "补默认值的迁移不在了"
    text = migration.read_text(encoding="utf-8")
    assert 'down_revision = "0038_after_sale_refund_tracking"' in text
    for column in ("created_at", "updated_at"):
        assert f'"{column}"' in text
    assert text.count("server_default=sa.func.now()") >= 1
    # alembic_version.version_num 是 VARCHAR(32)，超长会让迁移在
    # 写版本号时失败（20260916 部署实测 StringDataRightTruncation）。
    revision_id = re.search(r'^revision = "([^"]+)"', text, re.M).group(1)
    assert len(revision_id) <= 32, f"revision id 超过 alembic_version 列宽：{revision_id}"
