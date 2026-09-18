# 评审请求 · 采购售后「按环境单遍直提」（严格执行器评审）

> 本文件是给评审方（Cursor）的提示词，可整段复制。**本地直评**：直接在本机 worktree 只读评审，
> 不用 GitHub PR、不用 fetch、不改代码。
>
> **首轮评审已完成（结论「改后合并」），四项必修与建议项整改记录见
> `docs/20260917_售后按环境单遍直提.md` §7；复评看相对功能提交 `07de7c6` 的增量即可。**

请评审本轮改动：③「按环境提交」从「先扫描 → 人工核对清单 → 再提交」**改为单遍直提**。
这同时是**写操作语义变更 + 本地执行器新增一条运行模式 + 云端协议扩展 + 一次迁移**。

**只读评审，不要改代码。** 输出：结论（可合并 / 改后合并 / 不可合并）+ 必须改项（带文件:行）+
建议改项，以及「必须真机验证」清单（本单未做真机验收，部署条件受限）。

**本单涉及 Xynigo 执行器（本地浏览器动作、桥接、桌面端能力位）：按项目约定，执行器改动一律严格执行器
评审。** 除读 diff 外，必须沿运行时路径追一遍——谁开/关环境、写操作前后各做了什么、失败与停止时状态
落在哪——并对「老桌面端 / 老云端 / 并发 / 中断」四类边界各问一遍。不接受只凭单测通过下结论。

## 评审对象

- 分支 `codex/after-sale-claim-env-direct`，已 rebase 到 `origin/main`（含并行线「SHEIN 店铺授权」合并）。
- **功能提交 `07de7c6`**（本单全部代码）。其后提交只改文档、迁移编号与这份评审请求本身；
  评审以三点 diff 为准，不必追 HEAD。
- 复核命令：`git diff origin/main...codex/after-sale-claim-env-direct`（三点，只看本分支引入的差异）
- worktree：`~/Documents/xynigo-worktrees/as-env-direct`（当前就在这个分支上；**别在其它检出里评**）
- **迁移链自检**：`alembic heads` 必须只有一个头 `0045_after_sale_env_results`，链路
  `0043_after_sale_phase_evidence → 0044_shein_store_auth → 0045_after_sale_env_results`。
  并行线曾撞号，本单迁移已由 0044 顺延为 0045，请确认这条链在新 main 上成立。

## 口径要点（走偏即缺陷）

1. **按环境提交＝单遍直提（写操作）**：一次确认后直接进环境；读到带丢件退款入口的订单就现场
   `pre_info` 体检并提交；**没有入口的环境跳过（`skip`），不产生订单行**，环境级结果单独回报。
2. **幂等唯一闸门＝提交前 `pre_info` 体检**（在 `_claim_one` 内，与按单提交同一条链）；扫描不再是前置。
   已退包裹平台侧不可再退，重复运行最多产生 `blocked`，不产生重复退款。
3. **建 Run 只有一处入口**：Web 的 `asSubmitItems`（环境分支是它的条件分支，`asSubmitByEnvironment`
   只是确认与解析的包装），不得出现第二条 POST `/v1/operation-runs/after-sale-claim` 的路径。
4. **进度按环境计、成败计数按订单计**：`successCount` 可以大于 `totalCount`（环境数），**这是刻意的**，
   不能当缺陷去「修正」；对应云端 `_sync_after_sale_claim_run` 对环境模式不做 `min(total)` 截断。
5. **老执行器不得收到直提任务**：能力位 `after.sale.claim-environment.v1` 在云端派发前拦截（409
   `executor_after_sale_environment_upgrade_required`）；能力位四处成对维护
   （`executor_channel` 两份清单 / 云端 `executor_contract` / 测试夹具 `AS_CAPABILITIES`）。
6. **老幂等键哈希不变**：按单请求的 `_payload_hash` 规范形式与升级前逐字节一致（新增字段为 `None` 时剔除）。

## 改动清单（按层）

| 层 | 文件 | 内容 |
|---|---|---|
| 执行器本地 | `src/purchase_tool/after_sale_claim.py` | 新增 `start_claim_environments` / `_run_claim_environments` / `_claim_env_one` / `_env_claim_item` / `_count_env_result` / `_finish_env_row`；`snapshot()` 增 `claimEnvRows`；`_claim_env` 的 per-item 异常隔离抽成 `_claim_item_guarded` 共用；`_publish_claim` 补 `orderNo` 身份字段 |
| 执行器本地路由 | `src/purchase_tool/main.py` | `POST /api/after-sale/claim-environments`（≤300 环境）+ 权限表 |
| 桥接 | `src/purchase_tool/operation_executor.py` | 同 `after.sale.claim.v1` 按报文体分流（`environmentSerials` 且无 `items`）→ `_execute_after_sale_claim_environments`；环境行投影 `_after_sale_env_rows`；终态 `_after_sale_environment_summary` |
| 能力位 | `src/purchase_tool/executor_channel.py`、`cloud/.../executor_contract.py` | 新执行器声明 `after.sale.claim-environment.v1` |
| 云端契约 | `cloud/.../operation_contract.py` | 建单体 `items` 与 `environmentSerials` 二选一 + 去重/上限；新增 `AfterSaleClaimEnvironmentRow` 闭集 |
| 云端服务 | `cloud/.../operation_service.py` | 环境模式建 Run（total＝环境数、`request_summary.mode`）；快照/历史输出 `submitMode`+`environments`；`_payload_hash` 兼容 |
| 云端任务 | `cloud/.../executor_service.py` | 派发前能力位检查；终态白名单加 `environments`；进度行校验放宽为「环境序号在计划清单内、订单号动态发现」+ `AFTER_SALE_ENV_ROW_CAP=2000`；环境结果落库；计数不截断 |
| 云端路由 | `cloud/.../main.py` | 同路由按报文体下发 `environmentSerials`；审计 `submitMode` |
| 迁移/模型 | `cloud/.../migrations/versions/0045_after_sale_env_results.py`、`models.py` | `after_sale_claim_runs.environment_results` JSON `server_default='[]'` |
| Web | `src/purchase_tool/web/index.html`（同步云端副本） | `asSubmitItems` 环境分支；`asRenderEnvOutcomes` 环境结果条；轮询/恢复/历史详情感知环境模式；删除扫描优先路径（`directSubmitScan`/`asFinishEnvironmentSubmit`/`asConfirmEnvironmentClaims`/`asEnvironmentClaimPreview`/「提交前扫描中」）；帮助文案带 300 环境上限 |
| 测试/原型 | `tests/`、`cloud/auth-service/tests/`、两份原型 | 见「怎么跑」 |

## 请重点看

### A. 执行器本地（严格，逐项追运行路径）

1. **环境开关配对**：`_claim_env_one` 的每个返回路径（登录缺失、空列表、异常、停止）是否都由
   `finally` 里的「谁开谁关」正确收尾？`_open_env` 复用已被人工打开的环境时（`opened_by_me=False`）
   是否会误关别人的环境？`_run_environment_jobs` 的并发（2/3/5）与同环境串行化是否覆盖别名环境。
2. **写操作保护有没有因为抽函数而回归**：`_claim_item_guarded` 与原 `_claim_env` 内联逻辑是否逐字等价
   （`_writeAttempted` → `_uncertain_claim`，否则 `_fail_claim`；`durationSeconds`/`operationCompletedAt`
   的 finally 发布）？单遍直提新增的**顺序**（先 `_publish_claim` 身份字段 → 再提交）有没有改变
   `_claim_one` 对行状态的假设（`verifying`/`uncertain` 路径）？
3. **停止/取消语义**：候选循环、`_run_environment_jobs`、批末收尾三处对 `_stop_event` 的处理是否一致？
   未处理完的环境/订单分别落什么状态（`stopped`？会不会有行永远停在 `running`）？
4. **归因不能伪装**：订单列表读取失败（`_read_all_order_cards` 抛 `RuntimeError`）与"确实没有入口"
   必须区分——前者是环境级 `fail` + 原因，后者才是 `skip`。空列表、翻页失败、登录失效三条路径逐一核对。
5. **动态行身份与顺序**：环境模式的行是执行中新建的（`_publish_claim` 的 `setdefault('orderNo')` 是否足够？
   `environmentSerial`/`storeName` 何时落？）。`claimRows` 的插入顺序 = 发现顺序，云端按
   （环境, 订单号）重排展示——两侧对不上时页面会怎样？截图键是否仍以订单号为键。
6. **环境行计数口径**：`_count_env_result` 把 `uncertain`/`verifying` 计入 `failedCount`——这在页面上
   会不会误导（「失败 N」其实是待核对）？`blocked` 环境只记 `blockedCount`、不算失败，是否与按单口径一致。
7. **本地路由校验**：`/api/after-sale/claim-environments` 的空值/超限/非列表输入是否与 `scan`、`submit`
   一致；权限表是否漏改（测试有断言，但请独立核对 `do_POST` 与 `AUTH_PERMISSION_BY_PATH`）。

### B. 桥接（严格）

1. **分流规则**：`payload.get('environmentSerials') and not payload.get('items')`——有没有形状会误分流
   （空列表、`items=[]`、两者都给）？老云端（只会发 `items`）与新云端两种报文各自走到正确的 handler。
2. **上行闭集**：`_after_sale_env_rows` 的字段/长度/状态兜底是否与云端 `AfterSaleClaimEnvironmentRow`
   完全一致（有一条机械比对测试，但请独立核对）；本地私有字段（如 `orders`）是否确实不外传。
3. **终态判定**：`_after_sale_environment_summary` 的四种终态（completed / partial_failure / failed /
   cancelled / uncertain）在「全 skip」「部分环境未终态」「订单 fail 而环境 ok」等组合下是否符合预期；
   `progressCompleted` 用环境单位、`successCount` 用订单单位，两者混用会不会让云端约束或页面进度跳变。
4. **取消下发**：bridge 收到取消后是否把 stop POST 到本地（与按单提交一致）；本地停止后环境行/订单行
   是否都进终态，避免云端 run 卡在 `running`。
5. **截图**：订单键截图复用是否安全；环境级失败没有截图（只有 `errorSummary`）——是否必须在本轮补齐
   （还是记录为限制）。

### C. 云端

1. **契约边界**：`items`/`environmentSerials` 二选一（都给/都不给/重复/空串/超 300/超 64 字符）逐一核对；
   `AfterSaleClaimEnvironmentRow` 的 `extra="forbid"` 与执行器投影是否一致。
2. **进度行校验放宽的边界**：订单号动态发现后，`order_outside_task` 与环境清单校验如何分工？串批次
   环境会不会被写进别人的 run？`AFTER_SALE_ENV_ROW_CAP=2000` 是唯一上限、会不会被绕过？
   截图子集校验是否仍成立（环境模式没有请求清单作参照）。
3. **回写与计数**：环境模式不截断计数的副作用——历史列表/详情展示、`ck_after_sale_run_counts` 约束、
   失败重试语义（`failedCount` > `totalCount` 时「补提失败单」是否仍正确）。
4. **`_payload_hash` 兼容性**：按单请求的规范形式是否与升级前逐字节一致（老幂等键重放不能 409）；
   直提请求剔除 `items=None` 后是否稳定（同一批重复提交必须命中同一 run）。
5. **迁移 0045**：`server_default='[]'` 对既有行/新行是否都成立；回退是否干净；revision id ≤32 字符；
   与店铺授权 0044 的先后关系是否唯一头。
6. **能力位**：检查是否在 `create_config_task` 的唯一咽喉处、是否覆盖所有派发入口（重试/补发若绕开
   这个函数就是漏洞）；缺能力位时报错码与前端提示是否闭环。

### D. Web

1. `asSubmitItems` 环境分支与按单分支的**唯一差别**是否只有「范围字段 + 能力位 + 进度单位 + 视图初始态」；
   `asSubmitByEnvironment` 是否真的没有第二条建 Run 路径（有测试钉，但请独立核对）。
2. 删除扫描优先路径后有没有残留引用/死代码（`directSubmitScan`、`asFinishEnvironmentSubmit` 等）；
   其它入口（勾选提交/补提/历史重提）行为未变。
3. 环境结果条 `asRenderEnvOutcomes` 的渲染/隐藏/转义（`title` 里的 `note`/`errorSummary` 都来自平台与
   执行器文本）；恢复运行中批次与历史详情在环境模式下的展示是否正确（单位、计数、跳过数）。
4. 双副本逐字节一致（`sync_web_workspace.py`）、两份原型已重跑；对齐测试是否真的能做机械比对
   （不是只断言字符串存在）。

## 已知取舍（非缺陷，供判断）

- 直提不预知订单号：`create_after_sale_claim_run` 里对 `uncertain/verifying` 历史行的**逐单**拦截只覆盖
  按单提交；直提依赖提交前 `pre_info` 体检防重复（已回传过的结果行仍参与历史防护）。
- 环境级失败（如订单列表翻页失败）只有文字原因，没有截图。
- 导出 Excel 仍只含订单行，环境级跳过只在页面与历史详情展示。
- 单批上限 300 环境 / 2000 订单行；发起前保留一次 `confirm()` 点击（未做「秒发」）。
- 环境行把 `uncertain/verifying` 计入 `failedCount`（页面上「失败」含待核对）。

## 怎么跑

```bash
cd ~/Documents/xynigo-worktrees/as-env-direct
PYTHONPATH=$PWD/src ~/Documents/xynigo-sourcing/.venv/bin/python -m pytest tests/ -q -p no:warnings

cd cloud/auth-service
PYTHONPATH=$PWD/src ~/Documents/xynigo-sourcing/cloud/auth-service/.venv/bin/python -m pytest tests/ -q -p no:warnings
```

注意：借用的两个 venv 里 `purchase_tool`/`xynigo_auth` 是 editable 指向**主检出**（当前还停在别的分支），
`PYTHONPATH` 必须指到本 worktree 的 `src`，否则评的是别的源码（本单开发期就踩过：能力位枚举对不上、
测试挂起）。

**CI 现状**：分支已 rebase 到含脱敏修复的 `origin/main`，`python scripts/audit_public_release.py`
本地通过、CI（push 触发：3.9/3.11/3.12 单测、cloud-tests、双平台打包）在本分支最新提交上运行。
评审只看代码时可直接采用本机两套全量的结果（见上）。

## 真机验证清单（本单未做，评审通过后按序执行）

1. 云端（含 `build migrate` + 迁移 0045）→ 执行器新版桌面包，缺一不可（老执行器会被 409 拦下）。
2. 真机跑「按环境提交」：**有可申请订单的环境**（应提交成功并回退款单号）、**无入口环境**（应 `skip`
   且页面环境结果条给出原因）、**已全退环境**（应全 `blocked`、零写入）。
3. 运行中取消：在途订单完成后停止，未处理环境落 `stopped`，云端 run 终态与页面一致。
4. 老桌面端回归：不升级执行器时，按环境提交应给出「需升级执行器」提示，其余四个入口不受影响。
