# 复审请求 · 采购售后（丢件退款）· 增量复核

> 本文件是给评审方（Cursor）的提示词，可整段复制。

---

你上一轮对 `codex/after-sale-claim-v1` 的评审结论是**改后合并**，三项必须改。我已全部处理并推送，请复核增量。

**仍只读评审，不要改代码。** 输出：三项必须改是否真的闭合 + 增量是否引入新问题 + 剩余欠账（「Web ④ 串跑」）能否合并后补的判断。

## 复核对象

- 分支 `codex/after-sale-claim-v1`，**新 HEAD `758bd34`**（上一轮你评的是 `1454ee9`）
- 增量两个提交：
  - `cbbfb2d` fix: 修三项必须改
  - `758bd34` test: 补云端 track 单测
- 基线仍是 `origin/main` `6e42c37`

## 三项必须改的处理（逐条）

**1. ④ POST/GET 调用了不存在的 `authorize()` → 请求 500**

- 已改为 `authorize_request`，参数形态与 scan 路由一致（`main.py` 两处：create / get）
- 新增覆盖：`cloud/auth-service/tests/test_after_sale_claim.py::test_after_sale_track_create_progress_and_whitelist` 走正常鉴权路径建单（若函数名再写错，该用例会直接 500 失败）

**2. `after.sale.track.v1` 未进云端 `BUSINESS_TASK_TYPES`**

- 已加入 `executor_service.BUSINESS_TASK_TYPES`（本地与云端现已一致）
- **由此连带的三处也一并改了**：
  - `phaseCounts` 纳入 `BUSINESS_RESULT_KEYS`（进 BUSINESS 后终态回执否则 422）
  - GET 的清单改走服务侧解密入口：新增 `after_sale_track_requested_bills()`（内部用 `_request_payload`），不再直读 `payload_envelope["items"]`——一旦加密，直读会得到空清单、表永远为空
  - 测试夹具的能力清单补 `after.sale.track.v1`（原缺，会让 create 直接 409 `executor_capability_missing`）

**请重点复核这条**：你上一轮明确写过「若 ④ 契约或 busy/加密行为有变，再扩一轮」。现在**确实变了**——track 进入 BUSINESS 后，payload 由明文变加密、busy 判定与优先级（100→10）都改了语义。按你定的触发条件，这一轮应该扩。

**3. ③ 提交结果表列错位（表头 11 / 行 12）**

- 按你的建议删掉行模板里的 `storeName`（表头按设计稿本就没有该列，是我改了表头忘了改行）
- ③ 的两处 `colspan="9"` → `11`（另一处 `asClearResult` 本就是 11，现在一致）
- **测试从「断言 colspan=9 存在」改成结构断言**：三张表逐一比对表头 `th` 数与行模板单元格数（含 `asThumbCell` 这类自带 `td` 的辅助函数）。你指出原测试把错误钉死了，这条我按你的意见重写了。
  - 写这条测试时我自己也踩了两次计数错（`<thead>` 被当成 `<th`、④ 的 timeline 被重复计入），已修。
- 权威源改完 → 跑 `sync_web_workspace.py` → 重生成两个原型，均已完成

## 本轮验证（可复现）

```bash
cd <worktree> && PYTHONPATH=src /Users/jeff/Documents/xynigo-sourcing/.venv/bin/python -m pytest tests -q
# → 1032 passed / 5 skipped（+2 结构断言）

cd cloud/auth-service && .venv/bin/python -m pytest tests -p no:warnings
# → 264 passed（+1 track 用例）
```

## 未做 / 未处理（请据此判断）

1. **「Web ④ 刷新 → 轮询出阶段」串跑仍未做**。你上轮列为「必须先补」。它需要同时在线的云端 + 执行器 + 工作台页面（本地 dev 实例或测试服务器部署），不是纯代码可验。除「浏览器点到云端」这一跳外，链路其余各段已有依据：执行器回访是真机只读验证过的（2 单，阶段/倒计时/卡掩码/金额全对），云端从接单到落表由本轮新用例覆盖。**请判断这一条能否合并后补。**
2. **你「建议改」清单里的各项本轮一律未动**，避免一次改太多掩盖回归，逐项列明以示不是遗漏：
   - 商品图/送达时间在 Web→桥接→本地路由三层被剥掉（`asSelectedItems`、`operation_executor` 清洗、`/api/after-sale/submit`）
   - ④ 只认本页内存里的 ③（刷新后无法回访；`asInit` 不拉 latest）；`asStop` 无 `mode === 'track'` 分支；云端无 track cancel 路由
   - Presentar 的**检测**用文案正则、**点击**用 footer 第一个按钮，改版风险未消除
   - 多包裹中途失败整单 fail、已成功包的退款单号不落行
   - 回写白名单 `if allowed and ...` 空清单 fail-open
   - `_track_one` 未登录标 `inuse`（claim 用 `login`）
   - 运行状态条 sticky / 终态收起只在原型里，正式面板没该节点
   - 测试夹具疑似真机原文（`GSH1RV329000RBM`、`Gwen Dijie`）
3. **你「可忽略」清单里的各项本轮也未动**（幂等闸门、谁开谁关、汇总口径、跟踪表唯一键、导出未做、多类型不可点、四项云端偏差、双副本、公开仓红线、老执行器部署约束）。

## 输出要求

1. **三项必须改是否闭合**（逐条：闭合 / 未闭合 + 依据）。
2. **增量是否引入新问题**（尤其 track 进 BUSINESS 后的加密与 busy 语义）。
3. **剩余欠账判断**：串跑能否合并后补；若不能，说清最小可验路径。
4. 若认为可合并，请给出合并前最后一项必做（如有）。
