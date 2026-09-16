# 复审请求（第三轮）· 采购售后 · 上一轮剩余两项

> 本文件是给评审方（Cursor）的提示词，可整段复制。

---

你上一轮（复核 `758bd34`）的结论是「三项必须改尚未全部闭合」，指出剩余两项：`phaseCounts` 的 finish 校验、③ 列序。我已修复并推送，请复核。

**仍只读评审，不要改代码。** 输出：这两项是否闭合 + 增量是否引入新问题 + 串跑能否合并后补。

## 复核对象

- 分支 `codex/after-sale-claim-v1`，**新 HEAD `be44249`**（你上轮评的是 `758bd34`）
- 增量一个提交：`be44249`
- 基线仍是 `origin/main` `6e42c37`

## 1. `phaseCounts` 校验：你的判断成立，已修

你说得对——我上一轮把它放进 `BUSINESS_RESULT_KEYS` 却漏了校验器那一侧：`_validate_business_result` 的 `count_keys` 循环会判定 dict 不是 int，回访终态 finish 直接 422，任务永远 `running`、④ 停在「回访运行中」。

- `executor_service.py`：`phaseCounts` 单独校验为 `dict[str, int]`（键为 str、值为非负 int），并从 `count_keys` 排除
- `cloud/auth-service/tests/test_after_sale_claim.py`：`test_after_sale_track_create_progress_and_whitelist` **补了 `finish` 一步**——发 `outcome=succeeded` + 含 `phaseCounts` 的 resultSummary，断言 200，并断言随后 GET 的任务状态为 `succeeded`。原先该用例只覆盖 create/progress/GET/白名单，你指出的「264 passed 罩不住 finish」我按你的意见补上了。

## 2. ③ 列序：你的判断成立，已修

上一轮我只做了「删 storeName」、没做「状态放到第 9 列」，格子数对上了但列序仍错。现在行模板顺序为：

```
订单号 → 商品图 → 售后类型 → 环境序号 → 送达时间 → 退款单号 → 退款路径 → 退款信用卡 → 状态 → 操作时间 → 备注
```

与表头逐列一致。**浏览器实测逐列比对（表头 vs 行值）11/11 全对**：

```
表头: 订单号 商品图 售后类型 环境序号 送达时间 退款单号 退款路径 退款信用卡 状态 操作时间 备注
行值: GSH1RV90A001B2 — 丢件退款 5121 — 2390765181147136 Cuenta original de pag — [已受理·退款审核中] 10:12:03 —
```

- ③ 的扫描中占位 `colspan="9"` → `11`（你指出的残留）
- **测试加强为钉列序**：断言模板里 `row.refundBillId → row.refundPath → row.refundAccount → asClaimPill` 的出现顺序。你指出「只数个数正是让列序错误被合法化的原因」，这条我按你的意见改了。

## 3. 过程失误（如实记录，请你评估是否留下隐患）

改 colspan 时我先用了**无差别全局替换**，误伤店铺结算 / 业务日志 / 环境历史等 8 处无关表格的 `colspan="9"`；回退时又**过度回退**，把 `cbbfb2d` 里正确的 11 也退回了 9。

最终处理：`git checkout -- src/purchase_tool/web/index.html` 回到 HEAD，只重放该改的两处，`git diff` 收敛为 **2 处（-2/+2）**。

**请重点复核**：受影响的那几个模块（`sfShopRows` / `businessLogTbody` / `environmentHistoryResultBody` / `executionQueueList` / `storeManagementTbody`）的 colspan 是否都已回到原值——因为中间经历过两轮错误替换，虽然最终 diff 只剩两处，仍请你独立确认没有残留。

## 本轮验证（可复现）

```bash
cd cloud/auth-service && .venv/bin/python -m pytest tests -p no:warnings
# → 264 passed（track 用例含 finish）

cd <worktree> && PYTHONPATH=src /Users/jeff/Documents/xynigo-sourcing/.venv/bin/python -m pytest tests -q
# → 1032 passed / 5 skipped
```

浏览器实测 ③ 逐列比对 11/11 全对；权威源改后已跑 `sync_web_workspace.py`，双副本字节一致；原型已重生成。

## 未做（同上一轮）

1. **「Web ④ 刷新 → 轮询出阶段」串跑仍未做**。你上轮的判断是「闭合这两项之后可以合并后补」，本轮两项已闭合 —— **请确认这个判断是否仍然成立**。
2. 你「建议改」「可忽略」清单里的各项本轮一律未动（避免一次改太多掩盖回归），逐项与上一轮复审请求相同，不重复列举。

## 输出要求

1. 两项是否闭合（逐条：闭合 / 未闭合 + 依据）。
2. 第 3 节那几个模块的 colspan 是否有残留错误（请独立核对，不要只看我的 diff 结论）。
3. 串跑能否合并后补；若可以，请给出合并前最后一项必做（如有）。
