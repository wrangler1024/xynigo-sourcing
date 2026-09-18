# 复评请求 · 财务中心「对账结算（结算看板）」整改增量

> 本文件是给评审方（Cursor）的提示词，可整段复制。**本地直评**：直接在本机 worktree 只读评审，
> 不用 GitHub PR、不用 fetch、不改代码。

**首轮评审已完成（结论「改后合并」），本文件只请复评整改增量。**

- 首轮评审点：`9c8795f`
- 整改提交：`feeff36`（本分支 HEAD）
- **只看这一段**：`git diff 9c8795f..feeff36`（单个提交，11 个文件，+559/−192）
- worktree：`~/Documents/xynigo-worktrees/finance-settlement-dashboard`

**只读评审，不要改代码。** 输出：结论（可合并 / 改后合并 / 不可合并）+ 仍未闭合的必须改项（带文件:行）+ 建议项。
**请按你首轮的要求只看增量**，不要重复评首轮已通过的部分（四指标、分批恒等式、缺汇率不按 1:1、前端无第二份聚合、
迁移单头等）；但如果整改**破坏了**首轮通过的点，请指出。

## 五项必须改的处置

| # | 首轮问题 | 处置 | 位置 |
|---|---|---|---|
| 1 | 增量窗口返回空时在途被写成 `None` | 删掉 `seen` 为空时的提前返回；**无论本轮是否有订单，在途一律回读台账求和** | `shein_settlement_sync.py` `_sync_order_ledger` |
| 2 | 待结算只拉近 21 天 + 全删重建 | **每次回溯完整区间**（默认 180 天）；按「最早窗口是否还有账单」判完整性，不完整即整店失败；关键字段缺失同样整店失败 | 同文件 `_fetch_pending_batches` / `_accumulate_pending_row` / `_collect_payout_batches` |
| 3 | 对账单按行加总、无单据去重 | **按对账单号去重**后再轧差 | 同文件 `_accumulate_pending_row` |
| 4 | 失败店历史批次仍进明细与导出 | `snapshot.status != 'ok'` 时批次清空、金额置空；导出再按状态兜一次 | `shein_settlement_sync.py` `build_summary`、`shein_settlement_export.py` |
| 5 | worker 构造后被 `= None` 覆盖 | 初始化移到构造之前 | `main.py:507` 附近 |

### 整改过程中我自己先写错了两版，请重点看这里

**第 2 项不是一次写对的，两处都是"看起来对、实际静默失效"：**

- **第一版按你建议的「连续空窗就停」实现，被我自己的用例打回**：一张 40 天前生成、至今未结算的账单，
  前面隔着若干空窗（那段时间没生成新账单），启发式会在够到它之前停下 → 永久少算。
  所以**放弃了空窗启发式**，改用「最早那个窗口是否还有账单」这个精确判据。
  —— 如果你认为该启发式在某种配置下更合适，请说明；目前的取舍是"宁可整店失败也不静默少算"。
- **第二版判据成了死代码**：窗口原先是从 `now` 往回切的，**最早那个窗口永远不贴着下界**，
  于是「最早窗口有账单」永远为假。改成**从下界（now − lookback）向上切**，
  最早窗口起点才恰好等于下界，判据才成立。
  —— 这条**测试没抓到**（没有构造"最老窗口恰好有账单"的用例），是复查实现时发现的；
  现有一条 `test_incomplete_backwalk_fails_instead_of_truncating` 直接覆盖它。

请复核：`_fetch_pending_batches` 的窗口切法、去重与完整性判据三者是否自洽；`slice_time_windows` 的
左闭右开在"从下界向上切"时是否仍不重叠、无空洞。

## 一并处理的建议项（同属「会静默出错」）

- **在途单缺预计收入 → 整店失败**：`SUM` 会跳过 NULL，静默把该店在途算少（极端情况显示 0）。
- **订单列表翻页超限 → 显式失败**：原为静默截断（`MAX_ORDER_LIST_PAGES_PER_WINDOW = 200`），与报账单口径对齐。
- **手动 `/sync` 先 `recover_stale_runs`**：进程被杀后原本 2 小时内一直 409。
- **`Settings` 补间隔下限校验**：`settlement_sync_interval_seconds` / `_stale_seconds` 均须 ≥60。
- **CNY/RMB 折算默认 1**：汇率表通常不含 CNY 自身，否则一个人民币店会让整张卡的人民币合计变成「—」。
- **打款日只挂「下次结算」卡**：原实现四张卡都带 `nearestPayDate`，卡片头都渲染「预计 MM-DD」。
- **明细与告警补 HTML 转义**：店名与平台错误文案进 DOM（同文件巡检表本来就用 `esc()`）。

## 明确未处理、已记入文档的建议项

以下按你的口径**未声称已交付**，已写入需求文档「评审整改」小节：

- 互斥闸门非原子（无唯一约束/行锁，多 worker 下可能双打平台）。
- 首次全量同步跑在 HTTP 请求内（店铺多会超时；P1 改异步 + 进度）。
- 当前视图用实时汇率而非快照汇率（日粒度下等价；接中行自动取数时需复核）。
- `report-sales-detail` 权限包未确认（P0 未用到）。
- **前端 `kind=diff` 区块仍在、同步层从未计算该差额** —— 文档原先容易读成已交付，**已改为明确标未交付并指向 §8 P1**。

## 你点名要补的三条回归位（已加）

| 用例 | 覆盖 |
|---|---|
| `test_incremental_empty_window_keeps_in_transit` | 第二次同步平台返回空 → 在途仍为 88.80（假网关 `query_orders` 已改为按 `orderUpdateTime` 过滤，才能真实模拟"窗口在、结果空"） |
| `test_failed_after_success_does_not_show_stale_batches` | 先成功后失败 → 明细不含上一轮批次，卡片不含该店 |
| `test_worker_is_installed_when_enabled` / `..._when_disabled` | `create_app(settlement_sync_enabled=True)` → `app.state.settlement_sync_worker is not None`；默认关闭时为 `None` |

另补：`test_check_order_duplicate_rows_are_deduped`（同单号两次只计一次）、
`test_older_pending_bill_beyond_naive_window_is_still_picked_up`（40 天前账单仍入桶）、
`test_check_order_missing_field_fails_store_instead_of_silently_dropping`、
`test_incomplete_backwalk_fails_instead_of_truncating`。

## 怎么跑

```bash
cd ~/Documents/xynigo-worktrees/finance-settlement-dashboard/cloud/auth-service
PYTHONPATH=src:. ~/Documents/xynigo-sourcing/cloud/auth-service/.venv/bin/python -m pytest \
  tests/test_shein_settlement_service.py tests/test_shein_settlement_windows.py \
  tests/test_shein_settlement_client.py tests/test_shein_settlement_sync.py \
  tests/test_shein_settlement_worker.py tests/test_shein_settlement_export.py \
  tests/test_shein_settlement_api.py tests/test_shein_signature.py -q
# 本模块 100 项（首轮 92 + 新增 8）
```

全量门禁（均已通过）：本地 1277 passed / 5 skipped；云端全量套件 exit 0；`audit_public_release.py` 通过；
权威源 `src/purchase_tool/web/index.html` 与云端副本哈希一致；`alembic heads` 单头 `0046_shein_settlement`。

## 真机验收清单维持不变

首轮给的 13 条真机验收**一条都还没做**（本模块仍没用真数据跑过完整同步）。整改只动了取数与回滚路径，
**没有降低真机验收的必要性**——尤其第 2 条（四指标对后台）与第 5 条（有没有生成超过回溯上限仍
`checkStatus=1` 的账单，用于标定 `check_order_lookback_days` 到底该设多少）。
