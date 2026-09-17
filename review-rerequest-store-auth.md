# 复审请求 · SHEIN 店铺授权模块（必须改 6 项修复）

> 本文件是给评审方（Cursor）的复审提示词，可整段复制。

---

请复审上一轮「改后合并」结论中 **6 项必须改**的修复。

**只读评审，不要改代码。** 输出：结论（可合并 / 改后合并 / 不可合并）+ 必须改项（带文件:行）+ 可以合并后补项。

## 复审对象

- 分支 `codex/shein-store-auth-module-design`（origin 已更新）
- 修复提交 `369aaad`（必须改 6 项 + 高价值后补）与 `3ef1980`（处置文档 + 预览副本注入权限码）
- 上一轮评审点 `ed8e5c9`；主检出 `/Users/jeff/Documents/xynigo-sourcing` 就停在该分支，直接只读评审
- 处置对照表：`review-response-store-auth.md`

## 请逐项独立验证（不要只跑测试）

### 1. 裂行修复（上一轮最严重项）

- 位置：`shein_store_auth_service.py` 的 `_match_store_row` / `complete_callback` / `verify_store`；迁移 `0043` 与 `models.py` 的唯一约束
- 核对点：
  - 代码里是否还存在任何把 `open_key_id` 片段写进 `merchant_id` 的路径；
  - 匹配顺序（target_store_id → openKeyId → 真实商家ID）在「首绑信息失败→再授权成功」「带 storeId 且 openKeyId 轮换」两条路径下的实际行为；
  - 重新授权全链是否真的带上了 `storeId`（契约 `SheinAuthLinkBody`、路由 `main.py`、前端 `index.html` reauth 分支）；
  - **越权**：伪造他人店铺的 `storeId` 请求 link 是否被 `_require_store` 的租户校验挡住；
  - verify 的「重复行合并」逻辑：删除的是哪一行、事件如何记录、是否有误删（例如两行其实是不同模式/不同应用）。

### 2. state 原子消费

- 位置：`_claim_link`
- 核对点：`UPDATE ... WHERE consumed_at IS NULL` 的 rowcount 语义在 PostgreSQL 行锁下的正确性（两个并发事务）；失败路径（换钥失败/过期）是否同样计入一次性；短事务后重读 link 的属性是否可能过期（`expire_on_commit`）导致误判。

### 3. 前端探测（CLOUD_WEB_MODE）

- 位置：`index.html` `setFeaturePanel` 的 `sheinstoreauth` 分支
- 核对点：CLOUD_WEB_MODE 为真时是否任何情况都不落 Mock；非云端判定 `typeof e.status === 'number' && e.status !== 404` 的边界（网关超时通常无 status、公司代理可能返回 407/451 等）；`file://` 分支。

### 4. 云端分页重取

- 位置：`index.html` 下一页与 `sizeSel.onchange`
- 核对点：云端下翻页是否确实重新请求；页码越界保护；mock 分支是否不受影响。

### 5. UNVERIFIED 标注

- 位置：`shein_openapi_client.py` 模块头 + `docs/20260917_需求_*.md` §2 #3
- 核对点：标注是否覆盖了上一轮点名的四处（应用级签名 VALUE、get-by-token 响应结构、33051002、query-store-info 字段名）。

### 6. 权限收口

- 位置：`index.html` `FEATURE_MODULES.sheinstoreauth` / `sheinAuthCanManage()` / 各按钮判定
- 核对点：入口与「生成链接/改名/删除/重新授权」是否都按权限码收口；mock 预览（无权限体系）回退是否只在本地方案生效。

## 新增回归测试请独立判断有效性

4 条新用例（`tests/test_shein_store_auth.py` 尾部）：

- `test_store_info_failure_then_reauth_stays_single_row`
- `test_reauth_with_store_id_updates_target_row_even_if_openkey_rotates`
- `test_failed_exchange_still_consumes_state_once`
- `test_zero_order_placeholder_merchant_and_verify_backfills`

请判断它们断言的是行为还是复述实现；特别是第一条能否真的抓住「裂行」（构造的闪断 transport 是否只影响店铺信息接口、不影响换钥）。

## 已采后补项（8 项，供确认无回归）

渲染 esc 转义 · `redirect_base` 空配置拒绝（503）· store_info 白名单入库 · tempToken 掩码首2尾2 · 生成事件去 state 尾号 · 换钥失败只回稳定文案 · `PERMISSION_MENU_GROUPS` 收录两项 · 回调页 `history.replaceState` 清 URL。

## 未采（留部署阶段，非本轮范围）

- 回调入口 IP 限流 → nginx
- 回调 GET 的 query 日志 → 部署清单（应用日志本就只记路由模板）

## 怎么跑

```bash
cd /Users/jeff/Documents/xynigo-sourcing/cloud/auth-service
uv run pytest -q          # → 394 passed
```

## 输出要求

结论 + 必须改项（文件:行）+ 可合并后补项。若 6 项中仍有未改净的，请给最小复现路径（调用顺序/参数）。
