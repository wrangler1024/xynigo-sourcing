# Xynigo Sourcing 新会话交接

交接时间：20260915（采购中心「售后处理」模块首版，待评审）
分支/提交：`codex/after-sale-claim-v1` → `0a41c03`，基线 `origin/main` `6e42c37`（v0.17.20）
状态：**已在独立 worktree 完成并自测，未推送、未合并、未发布**。worktree 路径 `~/Documents/xynigo-worktrees/after-sale-claim`。

## 0. 本次交付：买家端「包裹已送达未收到」售后申请

采购部对已下单买家号的「已送达但没收到货」订单需要在 SHEIN 买家端发起售后申请（退款原路退回）。人工动线是开环境→进订单列表→找到带售后入口的卡片→点进去→勾包裹→选路径→提交；本次把它产品化为采购中心的一个二级模块。

**形态（Jeff 拍板）**：两步式「扫描 → 勾选 → 提交」，走云端 Run 链路，首版一次做完整三步。

## 1. 改动范围（12 文件，+2563/−1）

执行器侧
- 新增 `src/purchase_tool/after_sale_claim.py`（`AfterSaleClaimer`，对标 `store_finance_inspect.py`）：扫描/提交两条批次链、环境占用四层策略、异常截图、随机单间停顿。
- `src/purchase_tool/main.py`：新增 `/api/after-sale/{scan,submit,progress,stop,screenshot}` 五个路由（`assistant.access`）+ 实例化（`afterSaleHeadless` 默认 false = 可见窗口）。
- `src/purchase_tool/operation_executor.py`：新增 `after.sale.scan.v1`（轮询上报订单级行）与 `after.sale.claim.v1`（轮询上报提交行+截图附件）两个业务任务，含行投影闭集与汇总口径（`blocked/skip/empty` 计 skipped 不计 failed）。
- `src/purchase_tool/executor_channel.py`：两个新能力登记进 `SUPPORTED_CAPABILITIES` 与 `MODERN_ONLY_CAPABILITIES`。

云端侧
- `models.py`：`AfterSaleClaimRun`（`after_sale_claim_runs`）+ `AfterSaleClaimResult`（`after_sale_claim_results`）；`ck_executor_task_type` 加入两个新任务类型。
- `migrations/versions/0035_after_sale_claim.py`：`down_revision="0034_store_finance_lookup"`，建两表 + drop/recreate task_type 约束。
- `operation_contract.py` / `operation_service.py` / `executor_service.py` / `executor_contract.py` / `main.py`：契约、Run 服务、同步回写、能力 Literal、7 条路由（扫描建任务不建 Run；提交建 Run；另有扫描取消 `/v1/after-sale/scan/{task_id}/cancel`）。
- `web/index.html`（云端副本由同步脚本生成）。

Web 权威源
- `src/purchase_tool/web/index.html`：CSS 块、二级 tab「售后处理」（`data-parent="procurement"`）、`FEATURE_MODULES.aftersale`、`afterSalePanel` 三张卡片、`as*` 前缀 JS 整段、`setFeaturePanel` 接线。
- 已执行 `python cloud/auth-service/deploy/sync_web_workspace.py`，双副本字节一致（有契约测试守着）。

测试
- `tests/test_after_sale_claim.py`（27 条）：卡片解析（含翻译插件插字）、成功页 URL 解析、批次状态机、路由归属、行投影与汇总口径、本地桥接脚本化 rpc 验证。
- `tests/test_executor_workspace_web.py`：新增 `AfterSaleClaimWiringTests`（5 条）。
- `cloud/auth-service/tests/test_after_sale_claim.py`（6 条）：扫描任务往返、提交 Run 幂等与落库、截图、取消。

## 2. 验证结果

| 范围 | 命令 | 结果 |
|---|---|---|
| 本地 | `PYTHONPATH=src python -m pytest tests -q` | **1013 passed / 5 skipped** |
| 云端 | `cloud/auth-service/.venv/bin/python -m pytest tests -q` | **全绿**（0 失败，含新增 6 条） |
| 网页语法 | 抽出内联脚本 `node --check` | rc=0 |
| 副本一致 | 字节比对 + `test_cloud_copy_is_synced_from_single_ui_source` | 一致 |
| 迁移一致 | `models.py` 的 task_type check 字符串 vs 迁移 `NEW_TASK_TYPE_CHECK` | 逐字一致 |

**真机验证（HubStudio + 真实买家号，只读）**
- 扫描路径在环境 `4902`（ZH-MX-0829-077）、`4903/4904/4905`（078/079/081）跑通；`4904`、`4905` 命中可申请订单（各有 1 个可退包裹），验证了 `hasEntry` 识别与 `pre_info` 判定；脚本自开的环境扫完自动关闭，先前手动开着的环境保持打开（谁开谁关生效）。
- 提交路径做了一次**只读演练**：在 `4904` 的订单 `GSH1RV13Y00NQUV` 上走完「进申请页 → 勾包裹 → Confirmar → 切到原路退回（checked=True）→ Presentar 可点」，**在点击 Presentar 前停住**，随后返回订单列表并关环境——未产生任何提交。

## 3. 限制与未做

1. **模块的提交路径未做真机落库验证**（演练止于 Presentar 前一步）。手动流程已于同日真机跑通并提交成功（环境 4902，退款单号 2390765181147136），模块用的是同一套选择器与步骤，但「点 Presentar → 跳 refundLabel → 成功识别」这一段在模块形态下仍未实跑。
2. 多包裹订单的「循环再提」分支未真机验证（现有样本都是单包裹）。
3. 未跑隔离 PostgreSQL 的迁移升降级验证（CONTRIBUTING 要求迁移用隔离 PG，普通单测不覆盖）。
4. 未加导出（Excel/CSV）与「补提失败」重跑；未做 `failed_retry` 合并视图（店铺结算有，售后首版没做）。
5. 未推送远端、未发布；线上/测试服务器均未部署。
6. 已知既有测试波动：`tests/test_updater.py::test_network_or_github_failure_does_not_block_startup` 在本机全套连跑时偶发失败（单独跑与基线全量均通过，疑与真实 `~/Library/Application Support/XynigoSourcing` 状态目录及用例顺序有关），与本次改动无因果关系，未修。

## 4. 评审要点（请未参与实现的一方重点看）

1. `after_sale_claim.py` 的提交链：单包裹提交循环（`guard < 5`）、`_submit_package` 的每一步断言与失败提示是否够精确。
2. `operation_executor.py` 的汇总口径：`blocked/skip/empty` 归 `skippedCount` 而非 `failedCount`——避免幂等重跑把整批误报失败。
3. 云端 `_upsert_after_sale_claim_progress` 的行白名单校验（orderNo 必须在建 Run 清单内）。
4. Web 侧「已提交过的订单不可勾选」这条幂等闸门（`canPick = orderNo && claimable && status==='ok'`）。
5. 退款路径固定值：界面默认是 SHEIN 钱包，代码强制切「原路退回」，若业务口径变化需改 `REFUND_PATH_LABEL`。

## 5. 关联记录

业务侧经验（页面结构、四个坑、实测记录）已沉淀到私有业务仓：
`shein-dropshipping-ops/自动化工具/HubStudio采购环境自动化/06-已送达未收到售后申请.md`，并更新了该目录 README 索引与 `04-踩坑速查表.md`。
