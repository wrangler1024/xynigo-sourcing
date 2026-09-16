# 评审请求 · 采购售后 ④ 退款跟踪导出 Excel

> 本文件是给评审方（Cursor）的提示词，可整段复制。

---

请评审一个新改动：**采购售后 ④ 退款跟踪的导出**。

**只读评审，不要改代码。** 输出：结论（可合并 / 改后合并 / 不可合并）+ 必须改项（带文件:行）+ 可以合并后补项。

## 评审对象

- 分支 `codex/after-sale-track-export`
- 唯一提交 `a418e85`（基线 `origin/main` `b7799e1`，即已上线的 v0.18.0 线）
- worktree：`~/Documents/xynigo-worktrees/merge-pf2`

## 背景

采购售后（丢件退款）模块已上线测试服务器：② 扫描 → ③ 提交 → ④ 退款跟踪回访。④ 一直缺导出——运营要把回访结果落到 Excel（发给财务/对账）。本次把导出做完，含后端路由与前端按钮。

## 改动清单（+312 行，无删除）

| 文件 | 内容 |
|---|---|
| `cloud/auth-service/src/xynigo_auth/after_sale_export.py`（新增，93 行） | `build_after_sale_track_export(rows)` → `(content, filename, mime)`；10 列表头 + 阶段中文回退 + 时间格式化 |
| `cloud/auth-service/src/xynigo_auth/main.py`（+55） | `GET /v1/after-sale/track/{task_id}/export` |
| `src/purchase_tool/web/index.html`（+33，权威源） | ④ 卡片加「导出 Excel」按钮 + `asExportTrack()` |
| `cloud/auth-service/src/xynigo_auth/web/index.html` | 同上（`sync_web_workspace.py` 同步，逐字节一致） |
| `cloud/auth-service/tests/test_after_sale_export.py`（新增，6 条） | 模块列契约 |
| `cloud/auth-service/tests/test_after_sale_claim.py`（+93，1 条） | 路由端到端（含 404 与未登录 401） |
| `tests/test_executor_workspace_web.py`（+74，6 条） | Web 接线 + 列序/URL 与云端比对 |
| `docs/20260915_采购售后模块交接.md` | 补第 8 节（合并/上线/④/导出/运维教训） |

数据来源：导出读的是**该回访任务清单内**的退款单在 `after_sale_refund_tracking` 表的当前态（复用既有 `after_sale_tracking_snapshot`），不触发回访、不重跑。

## 请重点看

1. **列序漂移**：`after_sale_export.HEADERS` 与工作台 ④ 表头必须逐列一致。我的测试是从两边正则解析后比对（`test_track_export_columns_match_workbench_table`）——**请独立核对一遍**，别只看测试绿。
2. **任务范围**：导出必须只含本任务清单内的行。`after_sale_track_requested_bills(task)` + 任务类型过滤是否足以挡住「别人的行」「别的任务的行」？有没有越权读同租户其他用户数据的路径？
3. **授权**：路由权限用 `assistant.access`（与该组其他路由一致），是否合适？审计动作 `assistant.after_sale.track.export` 是否需要一并进权限目录/文档？
4. **前端复用**：`asExportTrack()` 是否复用了既有助手（`workspaceDownloadName` / `cloudApiError`），有没有自造一套下载或错误文案？按钮该不该在运行中禁用？
5. **空值语义**：空列落成真空单元格（openpyxl 读回是 `None`），不写「—」。这是刻意的（Excel 可筛选求和），但 ④ 页面显示的是「—」——这个不一致是否可接受？
6. **测试自洽风险**：我上一轮的教训是「错误代码与测试会互相自洽」。请检查这几条新测试是否只是把我写的实现复述了一遍：特别是 `test_export_row_values_follow_header_order` 的期望值，是从工作台表头推出来的，还是从我的实现抄的。

## 已知取舍（非缺陷，供判断）

- 「商品图」列写 CDN 链接，**不内嵌图片**：导出不依赖服务端外网取图（测试机是小 VPS）。要内嵌需另做并承担取图失败面。
- 导出不刷新数据：拿到的是最近一次回访的落库态（与页面 ④ 当前显示一致）。想拿最新值得先点「刷新退款进度」。
- 没有「导出全部历史」入口：只导出本页 ④ 这一次回访。

## 怎么跑

```bash
# 本地（执行器 + Web）套件
cd ~/Documents/xynigo-worktrees/merge-pf2
PYTHONPATH=$PWD/src ~/Documents/xynigo-sourcing/.venv/bin/python -m pytest tests/ -q
# → 1062 passed, 5 skipped

# 云端套件（解释器借用主检出的 auth venv，源码用本 worktree）
cd cloud/auth-service
PYTHONPATH=$PWD/src ~/Documents/xynigo-sourcing/cloud/auth-service/.venv/bin/python -m pytest tests/ -q
# → 全绿
```

注意：借用的 venv 里 `xynigo_auth` 是 editable 安装、指向**主检出**，所以 `PYTHONPATH` 必须指到本 worktree 的 `src`，否则跑的是别的源码。
