# 评审请求 · 财务中心「对账结算（结算看板）」（纯开放平台接口方案）

> 本文件是给评审方（Cursor）的提示词，可整段复制。**本地直评**：直接在本机 worktree 只读评审，
> 不用 GitHub PR、不用 fetch、不改代码。

请评审一个新模块：**财务中心 › 对账结算（结算看板）**——用 SHEIN 开放平台财务/订单接口
汇总所有已授权店铺的「在途 / 待结算 / 下次结算+预计打款日 / 已结算」四类资金指标，
监控公司 SHEIN 项目的资金周转。含静态原型、取数客户端、落库、聚合口径、接口、前端接线、
定时调度与 xlsx 导出。

**只读评审，不要改代码。** 输出：结论（可合并 / 改后合并 / 不可合并）+ 必须改项（带文件:行）+
建议改项，以及「必须真机验证」清单。

**本单没有可用的真机验收路径**（原因见「未做真机验收」一节），所以请**把重心放在口径与边界上**：
数字算错了、或者某个边界下静默少算/多算，比代码风格问题严重得多。

## 评审对象

- 分支 `codex/finance-settlement-dashboard`，**已合并最新 `origin/main`**（含售后「按环境单遍直提」）。
- worktree：`~/Documents/xynigo-worktrees/finance-settlement-dashboard`（当前就在这个分支上；**别在其它检出里评**）
- 复核命令：`git diff origin/main...codex/finance-settlement-dashboard`（三点，只看本分支引入的差异）
- **迁移链自检**（已处理过一次撞号，务必确认）：
  `alembic heads` 必须只有单头 `0046_shein_settlement`，链路
  `0043_after_sale_phase_evidence → 0044_shein_store_auth → 0045_after_sale_env_results → 0046_shein_settlement`。
  本分支迁移原编号 0045，与并行线撞号后顺延为 0046。
- 改动规模：源码约 3.4k 行（11 个文件），测试 1.8k 行（8 个文件）。

## 口径要点（走偏即缺陷，请逐条核对实现）

1. **四指标定义**（需求文档 §3.2）：
   - 在途资金 = Σ `estimatedGrossIncome` × `orderStatus=4`（已发货未签收，**订单级**）
   - 待结算资金 = 对账单 `checkStatus=1` **全部**按收支轧差净额
   - 下次结算金额 = 同上净额中**仅最近一个 `estimatePayTime` 的那一批**（不是全量）
   - 已结算资金 = 报账单 `reportStatus=2` **历史全量累计**（非本期、非本年度）
2. **「待结算」与「下次结算」必须是不同的数**：两者都取全量就会变成同一个数，是刻意的区分点。
3. **打款日不对齐时不得把全部金额配一个最早打款日**：`estimatePayTime` 是逐批的，
   同一店铺自身也会有多批（实测同店有 09-21 / 09-28 两批），汇总必须按日期切开。
   校验恒等式：**各批次金额之和 ≡ 待结算总额**（有回归位）。
4. **缺汇率的币种不得按 1:1 计入**：应整组返回 `None` → 前端显示「—」并在说明里点名币种。
   按 1:1 算会让总数错得离谱且不报错，是最难发现的错。
5. **失败店铺整店不计入汇总**，但要出现在异常区并带失败原因；失败店的历史批次也不得混入。
6. **前端不做任何聚合**：四指标、分批、折算全部由服务端算好，前端只做格式化与分页。
   若发现客户端有第二份口径实现即为缺陷（原型的旧版聚合代码已整段删除）。
7. **金额落到分**：一律 Decimal 并 `toFixed`/`quantize`；接口序列化为**两位小数字符串**
   （避开 JS 浮点），xlsx 里则是**真正的数值单元格 + `#,##0.00` 格式**（财务要能求和）。
   这两处序列化不同是刻意的，不要「统一」。
8. **窗口约束**（两条都是真机实测出来的，改之前先看文档 §0）：
   - 对账单窗口**必须严格小于 7 天**（恰好 7 天整会被平台拒，报 `gsfs99401`）
   - 订单列表窗口 ≤48h：**这是漏单的根源**——5 天前发货、这两天没更新过的单不会出现在窗口里，
     所以在途不能「查一次」，必须按 48h 分片回溯建本地台账，之后拉增量（带 2 小时重叠）。
9. **同店互斥**：手动刷新与定时任务共用同一个闸门（`run()` 开跑前 `active_run` 检查），
   撞车时后者收到 409 / worker 安静跳过；超过 stale 阈值（默认 2h）的 running 视为异常中断可被接管。
10. **「到期才跑」**：worker 按租户判断距上次成功同步是否已过间隔，未到即跳过——
    这是容器重启不重复同步、重复调度不多打平台的依据。

## 改动清单（按层）

| 层 | 文件 | 内容 |
|---|---|---|
| 取数客户端 | `cloud/.../shein_openapi_client.py` | 新增财务/订单域 6 个方法（报账单/对账单/对账单详情(GET)/订单列表/订单详情/站点币种）；新增 `_request` 支持 GET；**响应封装按接口族区分**（换钥/店铺信息走 `data`，财务与订单域走 `info`），两段都接受、info 优先，都缺时把真实顶层键名带进错误信息 |
| 数据层 | `models.py`、`migrations/versions/0046_shein_settlement.py` | 五张表：订单状态台账 / 结算快照（含 fx 汇率快照与收款方式）/ 打款批次 / 汇率表 / 同步运行记录 |
| 聚合口径 | `shein_settlement_service.py` | 纯函数、无副作用：四指标、按币种分组、按打款日分批、人民币折算、金额落分、失败店铺剔除 |
| 窗口分片 | `shein_settlement_windows.py` | 纯函数：对账单严格 <7 天分片、订单台账 48h 回溯/增量计划 |
| 同步编排 | `shein_settlement_sync.py` | 逐店取数 → 落库 → 落快照；**SAVEPOINT 按店隔离失败**、每店提交一次、幂等；互斥闸门与 stale 恢复；`build_summary` 从库组装（含币种筛选）；定时 worker |
| 接口序列化 | `shein_settlement_contract.py` | 金额两位小数字符串、缺汇率 `null`、`syncedAt` |
| 导出 | `shein_settlement_export.py` | 服务端 openpyxl 生成 xlsx（明细 → 汇总 → 结算排期 → 页脚） |
| 路由 | `main.py` | `GET /v1/finance/settlement/summary?currency=`、`POST /v1/finance/settlement/sync`、`GET /v1/finance/settlement/export?currency=`；worker 装配进 lifespan |
| 配置 | `config.py` | `SETTLEMENT_SYNC_ENABLED`（默认 **false**）/ `_INTERVAL_SECONDS`（默认 6h，下限 60s）/ `_STALE_SECONDS`（默认 2h） |
| 前端 | `src/purchase_tool/web/index.html`（同步云端副本） | 由 Mock 改为调用真实接口；币种筛选走服务端；刷新触发真实同步；导出云端走 xlsx、本地预览出 CSV；**整段删除客户端聚合代码** |
| 文档/原型 | `docs/20260917_需求_财务中心结算看板.md`、`docs/prototypes/20260917-settlement-dashboard/` | 需求文档（§0 实现进度 + §3.2 口径规则 + §9 决策记录 13 条）、原型 HTML、生成脚本、3 张截图 |

## 怎么跑

```bash
# 云端套件（本模块测试 92 项）
cd ~/Documents/xynigo-worktrees/finance-settlement-dashboard/cloud/auth-service
PYTHONPATH=src:. ~/Documents/xynigo-sourcing/cloud/auth-service/.venv/bin/python -m pytest \
  tests/test_shein_settlement_service.py tests/test_shein_settlement_windows.py \
  tests/test_shein_settlement_client.py tests/test_shein_settlement_sync.py \
  tests/test_shein_settlement_worker.py tests/test_shein_settlement_export.py \
  tests/test_shein_settlement_api.py tests/test_shein_signature.py -q

# 全量（本地 1277 passed/5 skipped；云端全量亦通过）
cd ~/Documents/xynigo-worktrees/finance-settlement-dashboard
PYTHONPATH=src ~/Documents/xynigo-sourcing/.venv/bin/python -m pytest tests -q
PYTHONPATH=src ~/Documents/xynigo-sourcing/.venv/bin/python scripts/audit_public_release.py

# 迁移单头
cd cloud/auth-service && PYTHONPATH=src python -m alembic heads

# 原型（本地预览走样例数据，双击即看）
python3 docs/prototypes/20260917-settlement-dashboard/make_preview.py
```

## 已做的验证

- **真机**：五个接口全部打通（生产 HTTP 200 code=0）；响应结构、封装、路径方法均已记录在文档 §0。
- **权限回归位**：`finance.access` 缺失时 summary/sync/export 三接口均 403。
- **空态**：未同步过 → `尚未同步` 且金额为 `null`（不是 `"0.00"`）。
- **失败隔离**：一家店挂掉 → 整轮 `partial`，运行记录存活（曾因整会话 rollback 被一起撤销，已改为 SAVEPOINT）。
- **幂等**：连跑两次金额不翻倍；订单台账不产生重复行。
- **导出**：接口返回合法 xlsx，金额为数值单元格、格式 `#,##0.00`。

## 未做真机验收（请一并评估风险）

1. **本模块从没用真数据跑过一轮完整同步**。原因：Keychain 里的店铺密钥在排障期间失效过一次，
   修复后又因「RandomKey 必须 5 位」的真机签名问题耽误，等签名修好时本模块的同步编排已经开始写。
   因此 `sync_store` 全链路（订单台账回溯 → 批次 → 累计 → 快照）**只有合成测试覆盖**。
2. **首次全量回溯的实际规模未标定**：单店 60 天 × 48h 分片 ≈ 30 次订单列表 + 在途单的详情批量调用，
   真实耗时与是否触发限流（报账单 20 次/秒）未知。
3. **对账单详情的权限未确认**：该接口（逐 SKU 费用拆分）是否在现有 API 权限包内，需实测。
4. **半托管店铺的财务接口返回结构**是否与自营一致，无实测样本。
5. 中行汇率自动取数**未接**（牌价口径待财务指定），当前汇率表只能手工写。

## 请重点看

1. `shein_settlement_sync.py` 的**事务与并发**：SAVEPOINT 隔离是否真的只回滚单店；每店提交是否会在
   异常路径下留下半写状态；`active_run` 的 naive/aware 时间处理是否稳妥（SQLite 返回 naive）。
2. 窗口分片的**边界**：切片是否可能重叠或留空洞；增量重叠 2 小时是否足够覆盖平台写入延迟。
3. `build_summary` 的**币种筛选**放在服务端是否正确（理由：跨币种打款日不同时「最近一批」依赖全局排期）。
4. 前端是否真的**没有第二份聚合实现**（重点看导出的汇总块与卡片渲染）。
5. 迁移 `0046` 与 main 上 `0045` 的**先后关系**是否成立。
