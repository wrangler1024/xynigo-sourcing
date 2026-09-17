# 复评请求 · 采购售后「按环境单遍直提」整改增量（严格执行器评审）

> 本文件是给评审方（Cursor）的复评提示词，可整段复制。**本地直评**：在本机 worktree 只读评审，
> 不用 GitHub PR、不用 fetch、不改代码。

首轮结论是 **改后合并**。四项必修与建议项已全部处置（采纳 7 / 暂缓 1），本回复评只看**整改增量 +
它对既有路径的回归面**，不需要重看首轮已确认通过的部分。

**只读评审，不要改代码。** 输出：结论（可合并 / 仍有必须改）+ 必须改项（带文件:行）+ 复评未闭合项。
首轮已确认的点（环境开关配对、写操作保护逐字等价、建 Run 单入口、能力位四处成对、老哈希不变、
四类边界）如无新证据请不必重复展开，但**整改如果动了这些点的邻近代码，要重新确认没有破坏它们**。

## 评审对象

- 分支 `codex/after-sale-claim-env-direct`，worktree `~/Documents/xynigo-worktrees/as-env-direct`。
- **整改增量 ＝ 提交 `aeb2b40`**（`git show aeb2b40`，14 个文件 +458/−107）——本轮复评的核心。
- 首轮评审点的功能提交在 rebase 前是 `07de7c6`，rebase 重写后等价提交为 **`81c7c81`**；
  `6edb275` 只是文档回填，`5088856` 是 rebase 收尾（迁移改号 0045 + 评审请求文档 + 原型重跑）。
- 完整三点 diff：`git diff origin/main...codex/after-sale-claim-env-direct`
- rebase 说明：分支已建立在含「SHEIN 店铺授权」合并与公开仓脱敏修复的 `origin/main` 之上；
  `alembic heads` 单头 `0045_after_sale_env_results`（0043 → 0044 店铺授权 → 0045 本单）。
- **CI**：`aeb2b40` 的 push 运行 **8/8 job 全绿**（unit-tests 3.9/3.11/3.12、cloud-tests、package-smoke、
  macos-package、windows-standard-installer、windows-updater；run 35222107419）。

## 四条必修的整改（逐条给核查点）

### 1. 停止打断列表读取不得写成 `skip`
`src/purchase_tool/after_sale_claim.py`（`_claim_env_one`）：
读列表（`_read_all_order_cards`，被停止打断时返回残缺列表）之后**立即**查 `_stop_event`；已停止则
环境落 `stopped`、note「已请求停止，订单列表未确认；本次未提交」，不进候选判断、不新开提交。
请核对：这个判断在候选解析**之前**；没有其它返回路径绕过它；`_read_all_order_cards` 之外的停止窗口
（解析循环中）是否仍安全。

### 2. 环境内还有候选未处理时不得标 `ok`
同一函数：候选循环记录 `remaining`（`len(candidates) - index`），循环内停顿改为
`self._stop_event.wait(...)` 可被打断；离开循环后先判 `_stop_event` → 环境 `stopped`，note
「已停止，剩余 N 单未处理」（N=0 时写「已停止」），已提交计数保留。
请核对：`remaining` 的赋值点是否有 off-by-one（两处 break 分支都要对）；停止在**最后一单完成后**到达时
状态与 note 是否仍合理；`submittedCount` 保留但状态为 `stopped` 是否会让页面/历史读法产生歧义。

### 3. 环境级失败参与批次终态
`src/purchase_tool/operation_executor.py`（`_after_sale_environment_summary`）：
新增 `env_bad`（环境行 `fail`/`login`/`inuse`），与订单失败合并为 `hard`：
未终态 → `uncertain`；`env_stopped and not success and not hard` → `cancelled`；
`hard and success` → `partial_failure`；`hard` → `failed`；否则 `completed`。
请核对这张矩阵：全 `login`→`failed`、9 `skip`+1 `fail`→`failed`、环境 `fail`+订单 `ok`→`partial_failure`、
全 `blocked`/全 `skip`→`completed`、全 `stopped`→`cancelled`；以及 `_terminal_result` 对 `failed` 的
映射（任务 outcome=failed）与云端 run 终态是否按预期落库；`failedCount` 仍是订单口径（环境失败只在
`environments` 里），页面对「失败 N 单」+「N 个环境读取失败」两句是否都出现。

### 4. 环境结果在「恢复批次」与「历史详情」可见
`src/purchase_tool/web/index.html`（云端副本同步）：
- `asLoadLatestClaim` 早退条件改为「非运行中 且 无订单行 且 无环境行」才跳过；恢复后用
  `restoreEnvs.length` 与 `envMode` 决定进度单位与文案。
- 新抽 `asEnvOutcomePills(environments)`（环境结果 pill 串，页面条与历史详情共用）；
  `asRenderEnvOutcomes` 改为写该串。
- 历史详情（`asRenderClaimHistoryDetail`）：新增环境结果条容器 `asHistoryEnvOutcomes`，meta 补
  「N 个环境读取失败 / N 个环境已停止」，空态在环境模式且有环境行时写「无可申请订单，环境级结果见上方」；
  打开详情/失败路径会先清空该条。
请核对：早退条件三种组合（运行中有行/已结束无行有环境/已结束无行无环境）；`asEnvOutcomePills` 抽取后
live 条行为未变（夹具已断言，但请复核隐藏逻辑与 `AS_STATE.environments` 副作用）；历史详情的
`envBox` 在非环境模式必须隐藏；`title` 里的 `note`/`errorSummary` 仍走 `esc`。

## 建议项处置（复核用）

已采纳：`_run_claim_environments` 批末安全网（`verifying→uncertain`、`queued|running→stopped`，与
`_run_claim` 同款）；`_after_sale_env_rows` 的 `durationSeconds: None` 不再收成 `0`；历史列表环境数
（`_after_sale_claim_environment_counts`）直提批次改读 `request_summary.environmentSerials`；
`executor_after_sale_environment_upgrade_required` 进 `cloudApiError` 文案表；同环境停顿可被打断；
列表读取错误文案去掉「扫描」；迁移测试 docstring 改 0045；删掉已成死参数的 `expectedExecutorId`
分支（旧扫描确认路径删除后无调用方）。

请特别核对两条副作用：
1. **批末安全网与 `_claim_item_guarded` 的 finally 是否重复收口**：安全网只应碰
   `verifying`/`queued`/`running`，不能覆盖已发布 `durationSeconds`/`operationCompletedAt` 的终态行。
2. **环境数改按计划数后**，运行中批次的「环境数」会在开跑前就等于计划数（此前是已回传环境的去重数）——
   这是刻意的口径（全 skip 批次不能显示 0），但如果列表/详情还有别处依赖旧语义，请指出。

暂缓（已入档 §6 已知限制，不计缺陷）：同环境多个别名（序号/环境ID/名称指向同一环境）不去重——
Web 入口只收数字序号，别名重复只可能由直接调 API 构造，且锁已按 `containerCode` 串行、`pre_info`
已兜住重复写入。

## 已知取舍（首轮已接受，不在本轮重复计缺陷）

- 直提不做逐单历史 `uncertain` 拦截（订单号事先未知，靠提交前 `pre_info`）。
- 环境级失败没有截图；环境级失败不进「补提失败单」（没有订单行，需人工重跑该环境）。
- 导出 Excel 只含订单行。
- 单批 300 环境 / 2000 订单行；发起前保留一次 `confirm()`。

## 怎么跑

```bash
cd ~/Documents/xynigo-worktrees/as-env-direct
PYTHONPATH=$PWD/src ~/Documents/xynigo-sourcing/.venv/bin/python -m pytest tests/ -q -p no:warnings

cd cloud/auth-service
PYTHONPATH=$PWD/src ~/Documents/xynigo-sourcing/cloud/auth-service/.venv/bin/python -m pytest tests/ -q -p no:warnings
```

`PYTHONPATH` 必须指到本 worktree 的 `src`（两个共享 venv 的 editable 指向主检出，指错会评到别的分支）。
本轮结果：本地 **1276 passed / 5 skipped**、云端 **417 项 0 失败**、四个 Web 夹具（
`after_sale_submit_environments_ui`、`after_sale_export_ui`、`after_sale_runtime_ui`、
`after_sale_reliability_ui`）全 PASS；`audit_public_release.py` 本地通过。

## 复评通过后（真机验证清单，未做）

1. 云端（`build migrate` + 迁移 0045）→ 新版桌面包；缺一不可（老执行器被 409 拦下）。
2. 有入口环境（提交成功回退款单号）、无入口环境（`skip` + 环境结果条原因）、已全退环境（全 `blocked`、零写入）。
3. 运行中取消：在途单做完再停；**列表未读完不得 `skip`**；**环境内未处理完不得 `ok`**；未启动环境 `stopped`。
4. 整批未登录或翻页失败 → 批次终态必须是 `failed`；全 skip 批次刷新后环境结果条仍在、历史详情逐条可见。
5. 老桌面端：按环境提交给出需升级提示；其余四个写入口不受影响。
