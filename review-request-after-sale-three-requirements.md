# 评审请求 · 采购售后三个需求（补提失败 / 直接提交指定单 / 提交历史）

> 本文件是给评审方（Cursor）的提示词，可整段复制。

---

请评审采购售后模块的第二批改动：**三个需求一次交付**。

**只读评审，不要改代码。** 输出：结论（可合并 / 改后合并 / 不可合并）+ 必须改项（带文件:行）+ 可合并后补项。
口径以 `docs/20260916_售后需求排期与暂缓.md` §1 与两份 `docs/20260916_需求_*.md` 为准（已合并在 main 上）。

## 评审对象

- 分支 `codex/after-sale-three-requirements`，本分支只有两个提交：
  - `4bd73ff` feat: 补提失败 + 直接提交指定单 + 提交历史（**主体，请重点看这个**）
  - `baa0824` docs: 本评审请求提示词
- 基线 `origin/main` `b482e1f`。**基线里已经有的东西不属于本次评审对象**（它们是上一轮按 Jeff 指令合并的收口）：
  ④ 退款跟踪导出、运行状态条按阶段落点、③ 补齐送达时间/商品图 + 环境序号移到最前、
  迁移 `0039` 补跟踪表时间列默认值、以及执行器桥接层透传 `deliveredAt`/`goodsImg`。
  若要核对它们，直接看 `origin/main` 的 `b482e1f` 及之前的四个 merge 提交。
- 复核命令：`git diff origin/main...codex/after-sale-three-requirements`（三点，只看本分支引入的差异）
- worktree：`~/Documents/xynigo-worktrees/merge-pf2`（当前就在这个分支上）

## 口径要点（这四条最容易走偏，请逐条核）

1. **三处提交只走一个建 Run 入口**：`asSubmitItems(items, {label, retryFromRunId})`。
   ③ 勾选提交 / 补提失败单 / 直接提交指定单 / 历史详情重提，四处都必须走它；不得出现第二条提交路径。
2. **补提范围＝可恢复失败**：`fail`/`login`/`inuse`/`stopped`，**排除 `blocked`**
   （平台判定已无可退包裹，重提只会白跑一遍写操作，界面不给入口）。
3. **历史列表租户内互相可见**：**刻意偏离**物流/建环境的「本人 + 管理员」口径，
   实现里不应出现 `history_admin`／`_user_has_role` 那类过滤；审计照常记操作人。
4. **不新增第二条链路**：从历史发起的回访灌进 ④（`asTrack(items)`）、重提复用需求①的动作、
   导出复用 `after_sale_export.py` 口径（表头样式、空值留空不写「—」、商品图列写链接不内嵌）。

## 改动清单（+2466 / −111）

| 文件 | 内容 |
|---|---|
| `cloud/.../after_sale_export.py` | 抽 `_build_workbook`；新增 ③ 批次导出（11 列、状态文案表） |
| `cloud/.../operation_service.py` | `after_sale_claim_history`（列表，租户内可见）/ `_history_snapshot`（详情）/ 操作人与执行器名回填；`request_summary` 带上 `deliveredAt`/`goodsImg` 与 `retryFromRunId` |
| `cloud/.../operation_contract.py` | `AfterSaleClaimRunCreateBody.retryFromRunId`（可选，不参与幂等） |
| `cloud/.../main.py` | 三条历史路由（**注册在 `/{run_id}` 之前**）+ 创建审计补 `retryFromRunId` |
| `src/purchase_tool/web/index.html`（权威源） | `asSubmitItems` 共用入口；补提失败按钮（含二次确认）；指定单粘贴解析；历史弹层（列表/详情/回访本批/重提/导出）；行模板抽 `asClaimRowHtml` |
| `cloud/.../web/index.html` | 同上（`sync_web_workspace.py` 同步，逐字节一致） |
| 测试 | 本地 +8、云端 +5（见下） |

## 请重点看

1. **共用入口是否真的只有一条**：`asSubmitItems` 之外还有没有别的地方 POST
   `/v1/operation-runs/after-sale-claim`？四处的调用是否都传了正确的 `retryFromRunId`？
2. **blocked 是否真的被挡住**：`AS_RECOVERABLE_CLAIM_STATUS` 与按钮禁用逻辑；历史详情里的重提入口同理。
3. **可见范围**：`after_sale_claim_history` 是否只按 `tenant_id` 过滤？有没有从别处（如 `userId` 参数）
   意外收窄或放大范围？跨租户是否仍然隔离？
4. **路由注册顺序**：`/history` 必须排在 `/{run_id}` 之前（否则被 UUID 路径参数吃掉）。我加了断言，
   但请确认 FastAPI 的实际匹配行为与断言一致。
5. **分页正确性**：cursor 语义（`cursor` 是「产生当前页的游标」，`next` 推栈、`prev` 出栈）在删改批次后
   会不会重/漏？`created_at` 相同的情况下用 `id` 兜底比较是否足够？
6. **跨边界字段**：历史列表/详情读的每个字段，云端 `_after_sale_claim_history_item` 都必须返回
   （我加了机械比对测试，但请独立核对一遍）；批次导出的状态文案是否与页面 `AS_CLAIM_PILL` 完全一致。
7. **`retryFromRunId` 的落点**：写进 `request_summary`（既有 JSON 列，不加迁移）是否可接受？
   契约里是自由字符串（未校验存在性/格式），会不会被前端传成脏数据后原样回显？
8. **写操作保护**：补提与指定单都有 `confirm()`，文案是否说清了「会真实提交退款、新批次、原批次不变」？

## 已知取舍（非缺陷，供判断）

- `retryFromRunId` 不校验 UUID 格式、不校验来源批次存在（纯展示用）。若要收紧，改契约即可。
- 历史「操作人」筛选对所有人可用（因为列表本来就全可见），不是管理员专属——与需求 §3 的
  「按操作人筛选（管理员）」表述略有出入，按定稿 §6.3 的可见范围实现。
- 指定单入口不做「订单号是否属于该环境」的预校验：真伪由执行器现场体检（不能退判 `blocked`）。
- 批次导出为纯文本链接，不含图片内嵌（与 ④ 导出一致）。

## 怎么跑

```bash
cd ~/Documents/xynigo-worktrees/merge-pf2
PYTHONPATH=$PWD/src ~/Documents/xynigo-sourcing/.venv/bin/python -m pytest tests/ -q      # 1077 passed, 5 skipped

cd cloud/auth-service
PYTHONPATH=$PWD/src ~/Documents/xynigo-sourcing/cloud/auth-service/.venv/bin/python -m pytest tests/ -q
```

注意：借用的 venv 里 `xynigo_auth` 是 editable 指向**主检出**，`PYTHONPATH` 必须指到本 worktree 的 `src`，
否则跑的是别的源码。

## 浏览器实测（真实页面 + mock，非单测）

- 解析器：空格分组 / 换行+顿号 / 字段不足（报出 `['4589']`）/ 重复（去重提示）四类输入行为正确。
- 补提按钮：默认禁用；**只有 blocked 时仍禁用**；含 fail+inuse+blocked 时显示「补提失败单（2）」。
- 历史详情：11 列列序与 ③ 一致；「回访本批退款单（1）」「重提失败单（1）」可用性正确。
