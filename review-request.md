# 评审请求 · 采购售后（丢件退款）模块

> 本文件是给评审方（Cursor）的提示词，可整段复制。

---

你是**未参与实现的评审方**。请对下面的分支做最终评审——仓规约要求「合并或发布前由未参与实现的一方完成最终评审」。

**只读评审，不要改代码。** 输出：总体结论（可否合并）+「必须改」+「建议改」+「可忽略」，每条给出文件:行号与依据。

## 评审对象

- 仓库：`wrangler1024/xynigo-sourcing`（私有）
- 分支：`codex/after-sale-claim-v1`，HEAD `1454ee9`，基线 `origin/main` `6e42c37`（v0.17.20）
- 规模：43 files / +40048 / −5（其中两个约 950KB 的原型 HTML 占绝大部分行数；真实源码约 4000 行）
- 如需本地核对：worktree `~/Documents/xynigo-worktrees/after-sale-claim`（可直接切到该分支看代码、跑测试）

## 背景

采购部需要给「已下单买家号的、包裹已送达但没收到货」的订单，在 SHEIN 买家端逐个发起退款申请（理由固定「El paquete completo entregado pero no recibido」，退款原路退回）。人工动线是：开 HubStudio 环境 → 进订单列表 → 找到带售后入口的卡片 → 点进去 → 勾包裹 → 选退款路径 → 提交。

本次把它产品化为 **Xynigo 采购中心 › 采购售后**（二级菜单，两步式：扫描 → 勾选 → 提交），并加了后续的**退款跟踪**（只读回访平台侧处理进度）。

## 改动范围（按层）

**执行器（本地，`src/purchase_tool/`）**
- 新增 `after_sale_claim.py`（`AfterSaleClaimer`）：扫描（只读）/ 提交（写）/ 回访（只读）三条批次链，环境占用「谁开谁关」策略，异常截图，单间随机停顿（提交 5~15 秒、回访 2~5 秒），环境之间串行
- `main.py`：`/api/after-sale/{scan,submit,track,progress,stop,screenshot}` 六个路由（权限沿用 `assistant.access`）
- `operation_executor.py`：三个业务任务 `after.sale.{scan,claim,track}.v1` + 行投影闭集 + 汇总口径
- `executor_channel.py`：三个能力登记（SUPPORTED + MODERN_ONLY）

**云端（`cloud/auth-service/`）**
- `models.py`：`AfterSaleClaimRun` / `AfterSaleClaimResult` / `AfterSaleRefundTracking`（跟踪表按 `(tenant_id, refund_bill_id)` 唯一）
- 迁移 `0035`（两表 + task_type）、`0036`（结果表三个展示字段）、`0037`（跟踪表 + task_type）
- `operation_contract.py` / `operation_service.py` / `executor_service.py` / `executor_contract.py` / `main.py`：契约、Run 服务与快照、进度回写（提交行 upsert 进结果表、回访行 upsert 进跟踪表）、能力 Literal、路由

**Web 权威源（`src/purchase_tool/web/index.html`）**
- 二级菜单「采购售后」（`data-parent="procurement"`，module key `aftersale`）、三张卡片 + ④ 退款跟踪卡
- 运行状态条（sticky + 终态自动收起 + 完整进度标签含「预计还需」）
- ②③④ 三张表含商品图列（复用 `.procurement-image-thumb` + `safeProcurementImageUrl` 白名单）

**测试**：`tests/test_after_sale_claim.py`（本地）、`cloud/auth-service/tests/test_after_sale_claim.py`（云端）、`tests/test_executor_workspace_web.py` 里新增的 web 契约用例

## 已完成的验证（可复现）

```bash
# 本地（Python 3.9）
cd <worktree> && PYTHONPATH=src /Users/jeff/Documents/xynigo-sourcing/.venv/bin/python -m pytest tests -q
# → 1030 passed / 5 skipped

# 云端（Python 3.12，venv 已建在 cloud/auth-service/.venv）
cd cloud/auth-service && .venv/bin/python -m pytest tests -p no:warnings
# → 263 passed

# 迁移链与模型一致性（程序化核对过）
# 0033 → 0034 → 0035 → 0036 → 0037 线性；
# models.py 的 ck_executor_task_type 字符串与迁移 NEW_TASK_TYPE_CHECK 逐字一致
```

**真机验证（HubStudio 环境 + 真实 SHEIN 买家号）**
- 提交链：**5 单真实提交成功**（环境 4904/4905/4585/4586/4588），退款路径均为「原路退回」，单耗时 18~32 秒，提交后 `pre_info` 复核包裹进入不可退区（幂等成立）
- 回访链（只读）：2 单回访，阶段「审核中」、24 小时倒计时、退款信用卡掩码（`****7935` / `****2813`）、金额全部取到，6~7 秒/单
- 扫描链：多环境扫描，含「可申请 / 不可申请 / 无订单 / 未登录 / 已退款」各类判定的实机样本

## 未验证 / 明确未做（请据此设定置信度）

1. **Web ④ 退款跟踪卡片没有端到端串跑过**：执行器、桥接、云端、Web 各自验证过，但「Web 点刷新 → 云端下发 → 执行器回访 → 落跟踪表 → Web 渲染」这条完整链路没串起来跑过一次。**这是本次最该被质疑的一点。**
2. **导出未实现**：设计稿里有「导出 Excel（标准/完整/待跟进）」，服务端 `after_sale_track_export.py` 与路由都没建，Web 上也没有按钮（避免 404）。
3. **多类型框架未实现**：设计稿展示「催促发货 / 取消订单」两个规划类型，但只有丢件退款是真做的；那两类需要先定业务规则（催发货频次/冷却、取消订单允许条件）。
4. **未部署**：分支未合并、未发布、测试服务器与生产服务器都没动；迁移 0037 只在隔离临时库验过升降级，实例上没执行。
5. 提交链里「退款信用卡」字段是在真机提交 3 单之后才修好的（前两单的卡为空），修复后**没有新的真机提交再次验证**——只在其退款页上只读验证过取值。
6. 老执行器不会 advertise `after.sale.*` 能力，所以部署时必须同步升级执行器，否则任务下不去。这一条是部署约束，不是代码缺陷，但请不要漏。

## 评审重点（按风险排序）

1. **幂等闸门**：`after_sale_claim.py` 的 `_submit_package` / `_claim_one`——已提交过的包裹靠什么拦住？`_select_refund_path` 失败时是否真的**不会点 Presentar**？（真机第一次跑两单就是靠这层拦住没写坏数据，请确认逻辑意图与实现一致）
2. **进度回写的越权/串批**：`executor_service._upsert_after_sale_track` 是否确保回访行只能落在该任务请求清单内（否则会覆盖别人批次的行）？
3. **汇总口径**：`_after_sale_summary` 把 `blocked/skip/empty` 计 skipped 而非 failed（幂等重跑不该把整批报成失败）——判断是否合理、边界是否有漏洞。
4. **阶段判定**：`classify_track_phase` 的优先级。退款单页的时间轴**会列出尚未到达的步骤**，若按「某串是否出现」判会误判（我们踩过：审核中被判成处理中）。请确认当前实现能抵抗这类文案变化。
5. **Web 契约**：`index.html` 里 ②③④ 三张表的表头列数与行单元格数是否恒等（我们踩过两次：改表结构时丢 `id` 或漏列）；双副本字节一致。
6. **权限**：整模块沿用 `assistant.access` 一把梭。**取消订单这类不可逆动作将来要接进来时，必须按类型拆权限**——请判断现在这个粒度是否可接受、以及是否该在本次就预留。
7. **迁移**：0035/0036/0037 的 upgrade/downgrade 是否对称；`ck_executor_task_type` 的重建是否会锁表影响线上（发布时要评估）。

## 仓规约必须检查项

- **权威源与生成物**：`src/purchase_tool/web/index.html` 是权威源，云端副本必须由 `cloud/auth-service/deploy/sync_web_workspace.py` 生成且**字节一致**（有测试守着）；不得直接修补生成副本
- **迁移与模型一致性**：`models.py` 的约束字符串与迁移的最终态必须逐字一致
- **公开仓红线**：不得含真实凭证、服务器地址、内部链接或业务隐私（原型与测试里的订单号/买家号均应为合成数据，请抽查）
- **提交风格**：本分支 25 个提交，是否都是独立可交付单元、提交信息是否说清了「为什么」

## 已知偏差（我方主动做的，请判断可接受性）

1. **云端 4 项**（为让前端与本地投影跑通）：`executor_tasks` 加 `progress_summary` 列；Run 表加 `skipped_count`/`stopped_count`；提交进度行额外接纳 `packageCount`；`BUSINESS_RESULT_KEYS` 加 `skippedCount`。回退会掉对应功能。
2. **③ 的「提交时间」改名为「操作时间」**：语义相同，避免两个同义列。
3. **回访行契约刻意不含 `timeline`**（平台时间轴原文）：它很长，不进每轮进度上报的体积，留给导出侧回读。
4. **窗口默认可见（非 headless）**：售后是写操作，出问题同事要能直接看着接管。

## 输出要求

1. **总体结论**：可否合并（可合并 / 改后合并 / 不可合并）。
2. **必须改**：每条给 `文件:行号` + 问题 + 依据 + 建议改法。
3. **建议改**：同上，但可不阻塞合并。
4. **可忽略**：说明为什么不需要改（避免下一轮重复讨论）。
5. **对「未验证」清单的判断**：哪一项必须在合并前补验，哪一项可以合并后补。
