# 三轮评审请求 · 财务中心「对账结算（结算看板）」第二次整改增量

> 本文件是给评审方（Cursor）的提示词，可整段复制。**本地直评**：直接在本机 worktree 只读评审，
> 不用 GitHub PR、不用 fetch、不改代码。

**首轮「改后合并」、复评「改后合并」（两项必须改）均已按意见处置，本文件只请评审第二次整改增量。**

- 上轮评审点：`feeff36`
- 本次整改：`c882f0c`（本分支 HEAD）
- **只看这一段**：`git diff feeff36..c882f0c`（2 个提交，实现改动集中在 6 个文件，+289/−32；
  其中 `35ce7da` 只加了一份复评请求文档，真正的实现改动在 `c882f0c`）
- worktree：`~/Documents/xynigo-worktrees/finance-settlement-dashboard`

**只读评审，不要改代码。** 输出：结论（可合并 / 改后合并 / 不可合并）+ 仍未闭合的必须改项（带文件:行）+ 建议项。
**请只看增量**，不要重复评前两轮已通过的部分；但若本次改动**破坏了**已通过的点（四指标、分批恒等式、
缺汇率不按 1:1、前端无第二份聚合、首轮五项、复评窗口切法/完整性判据），请指出。

## 复评两项必须改的处置

| # | 复评问题 | 处置 | 位置 |
|---|---|---|---|
| 1 | 无 `checkOrderNo` 的行既不去重又入桶 | 单号**与其他关键字段同等对待**：缺号即整店失败（原 `if order_no:` 把缺号行直接放过）。去重键改为 `(对账单号, 收支类型)` | `shein_settlement_sync.py` `_accumulate_pending_row` |
| 2 | 对账单单窗口翻页超限仍静默 `break` | 与订单列表/报账单口径对齐：**超限即抛 `SheinSettlementSyncError`** | 同文件 `_fetch_check_order_window` |

### #1 为什么去重键带上了收支类型

复评提醒：真机上同一单号是否会同时出现收入行与支出行尚未确认。若只按单号去重，
**成对出现的支出行会被吃掉一行**，轧差即错——而轧差正是待结算的口径。故去重键取
`(checkOrderNo, incomeExpenditureType)`：真正的重复（跨窗口边界秒同号同类型）仍被去掉，
同号不同收支类型的两行都保留。已补回归位 `test_same_order_income_and_expense_rows_are_both_kept`。

## 一并处理的复评建议项

| 建议 | 处理 |
|---|---|
| 最早窗口已有单仍继续打后面约 25 个窗 | **立刻返回**：贴着下界那片有未结账单即判不完整、整店必失败，不再白耗平台配额 |
| 导出只按 `status` 挡住待结算/下次结算 | **所有金额列**（含在途、已结算）一律按 `status == "ok"` 留空，导出侧再兜一次 |
| 台账里已有 `order_status=4` 且金额为 NULL 的脏行 | `_sum_in_transit` 前**扫一遍**，有脏行即整店失败（增量空窗不会重拉这些详情，`SUM` 会跳过 NULL） |
| 增量空结果不推进水位 | 水位**优先用快照的 `synced_at`**（每轮都写）；安静期 `max(last_synced_at)` 不前进会让窗口一轮轮变长直到重新打满 48h 分片 |
| 打款日列 `payDates` 未 `esc()` | 已补 |

## ⚠️ 本轮暴露出的两处「测试看着在测、实际没测到」

**这两处都请复核，因为它们直接影响你对"回归位是否可信"的判断：**

1. **对账单测试样本原本没有 `checkOrderNo`** —— 也就是说这些样本从来不是真实数据的样子。
   #1 的必填校验一加上，**6 个既有用例立刻变红**。现已给全部样本补上单号。
2. **假网关原先第 1 页就返回全部** —— 于是"翻页上限"这条路径**根本走不到**，截断保护测不出来
   （订单列表的 200 页上限同样如此）。现已把 `query_orders` / `query_check_orders` 都改成**真分页**
   （按 `page_size` 切片），`test_check_order_page_cap_fails_instead_of_truncating` 才真正生效。

请判断：还有没有**别的回归位属于同类**（断言写对了、但夹具/假网关让目标路径走不到）。我自查过一遍，
没有发现第三处，但这种问题我自己看不出来才是常态。

## 你点名要补的回归位（本轮新增 4 条）

| 用例 | 覆盖 |
|---|---|
| `test_check_order_without_order_no_fails_store` | 缺单号 → 整店失败（原为静默放过） |
| `test_same_order_income_and_expense_rows_are_both_kept` | 同号收支两行都保留，轧差 100−30=70 |
| `test_check_order_page_cap_fails_instead_of_truncating` | 4000 行进桶触发 100 页上限 → 整店失败 |
| `test_dirty_in_transit_row_fails_instead_of_under_counting` | 台账预置脏行 → 整店失败 |

## 怎么跑

```bash
cd ~/Documents/xynigo-worktrees/finance-settlement-dashboard/cloud/auth-service
PYTHONPATH=src:. ~/Documents/xynigo-sourcing/cloud/auth-service/.venv/bin/python -m pytest \
  tests/test_shein_settlement_service.py tests/test_shein_settlement_windows.py \
  tests/test_shein_settlement_client.py tests/test_shein_settlement_sync.py \
  tests/test_shein_settlement_worker.py tests/test_shein_settlement_export.py \
  tests/test_shein_settlement_api.py tests/test_shein_signature.py -q
# 本模块 104 项（首轮 92 → 二轮 100 → 本轮 104）
```

全量门禁（均已通过）：本地 1277 passed / 5 skipped；云端全量套件 exit 0；`audit_public_release.py` 通过；
权威源 `src/purchase_tool/web/index.html` 与云端副本哈希一致；`alembic heads` 单头 `0046_shein_settlement`。

## 明确未处理、不声称已交付

- **互斥闸门非原子**（无唯一约束/行锁，多 worker 下可能双打平台）。
- **首次全量同步跑在 HTTP 请求内**（店铺多会超时；P1 改异步 + 进度）。
- **当前视图用实时汇率而非快照汇率**（日粒度下等价；接中行自动取数时需复核）。
- **`report-sales-detail` 权限包未确认**（P0 未用到）。
- **前端 `kind=diff` 区块仍在、同步层从未计算该差额**（文档已明确标未交付并指向 §8 P1）。

## 真机验收：仍一条未做，且有两项直接决定本轮实现是否安全

13 条清单一条都还没做。本轮特别需要真机标定的两条：

1. **对账单 list 是否必有 `checkOrderNo`、是否一行一号。**
   决定 #1 的「缺号即整店失败」会不会把正常店打成永久失败；也决定去重键带收支类型是否必要。
2. **有没有生成超过 `check_order_lookback_days`（默认 180 天）仍 `checkStatus=1` 的账单。**
   决定回溯上限该设多少；也决定「最早窗口有单就失败」的假阳性代价是否可接受——
   我目前的取舍是**宁可整店失败也不静默少算**，这个取舍的代价只能靠真机样本标定。

另外首轮清单里的第 6 条（`report-order-list` 是否强制时间窗、默认是否只回最近页）未验，已结算仍可能静默少算。
