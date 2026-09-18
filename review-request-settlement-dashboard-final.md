# 终评请求 · 财务中心「对账结算（结算看板）」第三次整改增量

> 本文件是给评审方（Cursor）的提示词，可整段复制。**本地直评**：直接在本机 worktree 只读评审，
> 不用 GitHub PR、不用 fetch、不改代码。

**首轮「改后合并」、复评「改后合并」、三轮「改后合并」（1 项必须改）均已按意见处置。
按你三轮的口径「修完失败快照不进水位、补上面对应回归后可以合」，请做终评。**

- 上轮评审点：`c882f0c`
- 本次整改：`ffd7f2c`（本分支 HEAD）
- **只看这一段**：`git diff c882f0c..ffd7f2c`（实现改动集中在 2 个文件，+237/−17；
  其中 `5f87e21` 只加了三轮请求文档，实现改动在 `ffd7f2c`）
- worktree：`~/Documents/xynigo-worktrees/finance-settlement-dashboard`

**只读评审，不要改代码。** 输出：最终结论（可合并 / 改后合并 / 不可合并）+ 仍未闭合项 + 是否可进入真机验收。
**只看增量**，不重复评前三轮已通过的部分；若本次改动破坏了已通过的点，请指出。

## 三轮必须改的处置

| 问题 | 处置 | 位置 |
|---|---|---|
| **失败快照被当成订单水位**：失败快照写在 SAVEPOINT 外、`synced_at` 是本轮 now，而本轮拉到的台账更新已被回滚。拿它当水位会把 `[上次成功, 本轮 − 重叠]` 整段窗口跳过 → 期间签收/新发货再也不会出现 | 水位**只认 `status == "ok"` 的快照** | `shein_settlement_sync.py` `_sync_order_ledger` |

## 一并处理的建议项

| 建议 | 处理 |
|---|---|
| 空店（台账 0 行）不走快照水位，每轮都打满 60 天回溯 | 空店同样接受成功快照水位 |
| `incomeExpenditureType` 缺省仍当成收入（`int(x or 1)`，`0` 也会变 1） | 缺失/非法即整店失败：一笔真实支出被算成正数会让轧差整体错 |
| 导出打款日列不看 `status` | 与金额列一起按 `settled_ok` 留空 |
| 报账单假网关第 1 页回全部（200 页上限是盲区） | 改**真分页**并补超限失败用例 |

## ⚠️ 本轮我自己写的回归位也出现了一次假绿（请重点看这条）

**水位那条用例第一版是假的。** 我把失败轮取在「距上次成功 1 小时」处，而增量窗自带 2 小时重叠
→ 被跳过的区间是**空集**，于是**修与不修都是绿的**。第一遍跑绿，手停下来想想不对，去掉修复再跑，照样绿。

改成「失败轮距上次成功 **> 2 小时**、且订单更新落在被跳过的那段区间」之后才真正生效。
**验证方式（可复现）**：临时删掉 `SheinSettlementSnapshot.status == "ok"` 这一行过滤，
`test_failed_round_does_not_advance_order_watermark` 立刻变红，`in_transit_amount` 虚高为 `88.80`；
恢复该行后转绿。

这与你们连续三轮指出的「测试看着在测、实际走不到」是同一类问题，只是这次发生在我刚写的新用例上。
我已把它作为方法教训记入需求文档：**构造边界用例时必须先验证它在缺陷下确实失败，光看它变绿没有意义。**
请一并判断：这条用例现在的时序构造是否足以覆盖该缺陷，还有没有别的"重叠参数恰好掩盖缺陷"的类似位置
（我自查了增量重叠、stale 阈值、页上限三处，只有水位这条原先被掩盖）。

## 本轮新增回归位（3 条）

| 用例 | 覆盖 |
|---|---|
| `test_failed_round_does_not_advance_order_watermark` | 成功 → 失败 → 成功；失败轮窗口内的签收必须仍被读到（已验证在缺陷下变红） |
| `test_check_order_invalid_expenditure_type_fails_store` | 收支类型为 0 → 整店失败（原为静默当收入） |
| `test_report_order_page_cap_fails_instead_of_truncating` | 报账单超 200 页 → 整店失败（此前因假网关不分页而测不到） |

## 怎么跑

```bash
cd ~/Documents/xynigo-worktrees/finance-settlement-dashboard/cloud/auth-service
PYTHONPATH=src:. ~/Documents/xynigo-sourcing/cloud/auth-service/.venv/bin/python -m pytest \
  tests/test_shein_settlement_service.py tests/test_shein_settlement_windows.py \
  tests/test_shein_settlement_client.py tests/test_shein_settlement_sync.py \
  tests/test_shein_settlement_worker.py tests/test_shein_settlement_export.py \
  tests/test_shein_settlement_api.py tests/test_shein_signature.py -q
# 本模块 107 项（首轮 92 → 二轮 100 → 三轮 104 → 本轮 107）

# 复现假绿验证：删掉 status=="ok" 过滤后该用例应变红
```

全量门禁（均已通过）：本地 1277 passed / 5 skipped；云端全量套件 exit 0；`audit_public_release.py` 通过；
权威源 `src/purchase_tool/web/index.html` 与云端副本哈希一致；`alembic heads` 单头 `0046_shein_settlement`。

## 四轮累计未处理、不声称已交付

- **互斥闸门非原子**（无唯一约束/行锁，多 worker 下可能双打平台）。
- **首次全量同步跑在 HTTP 请求内**（店铺多会超时；P1 改异步 + 进度）。
- **当前视图用实时汇率而非快照汇率**（日粒度下等价；接中行自动取数时需复核）。
- **`report-sales-detail` 权限包未确认**（P0 未用到）。
- **前端 `kind=diff` 区块仍在、同步层从未计算该差额**（文档已明确标未交付并指向 §8 P1）。
- **中行汇率自动取数未接**（牌价口径待财务指定，当前汇率表只能手工写）。

## 请一并给出：可否进入真机验收

**13 条真机验收一条都未做**（本模块从未用真数据跑过一轮完整 `sync_store`）。
四轮评审累计已挂出 5 条只能真机标定的问题，它们直接决定当前几处取舍是否成立：

1. **对账单 list 是否必有 `checkOrderNo`、是否一行一号** —— 决定「缺号即整店失败」会不会把正常店打成
   永久失败；也决定去重键带收支类型是否必要。
2. **有没有生成超过 `check_order_lookback_days`（默认 180 天）仍 `checkStatus=1` 的账单** ——
   决定回溯上限；也决定「最早窗口有单就失败」的假阳性代价。当前取舍是**宁可整店失败也不静默少算**。
3. **`report-order-list` 是否强制时间窗、默认是否只回最近页** —— 未验则已结算仍可能静默少算。
4. **失败一轮之后的增量，在途不能丢失败当轮窗口里的状态变更** —— 本轮刚修的路径，需真机确认。
5. **首次回溯规模**：单店 60 天 × 48h 分片 + 在途详情 + 对账单回溯 + 报账单全量翻页的耗时，
   是否触发报账单 20 次/秒限流、是否被 HTTP 超时掐断。

请判断：**上面这些是否构成"合并后必须先真机验收才能当财务口径用"的阻塞项**，
以及现有 13 条清单是否需要按四轮整改的结果调整（例如第 3 条窗口开闭，现在已有单号去重做底）。
