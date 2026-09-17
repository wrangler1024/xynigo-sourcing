# 评审处置 · SHEIN 店铺授权模块（针对 20260917 Cursor 评审「改后合并」）

- 评审结论：改后合并（必须改 6 项 + 可合并后补若干）
- 处置提交：`369aaad`（必须改与高价值后补）+ 本条提交（预览副本回归修复）
- 验证：云端全量 `394 passed / 0 failed`（新增 4 条评审回归）；本地预览实测恢复正常

## 必须改 6 项：全部已改

| # | 问题 | 处置 |
|---|---|---|
| 1 | upsert 用 openKeyId 前缀冒充商家ID 会裂行 | 唯一键改 `(tenant_id, open_key_id)`（换钥必返且同店同应用稳定）；商家ID 暂缺存空串。重新授权链接带 `storeId`（契约/路由/前端全链），回调优先更新目标行；表单外再按 openKeyId、最后按真实商家ID 匹配。verify 成功回填商家ID/店名，同商家重复行合并删除。并发插入 IntegrityError 回滚后走更新路径 |
| 2 | state 消费非原子 | `_claim_link`：短事务 `UPDATE ... WHERE consumed_at IS NULL` 按 rowcount 判定胜者，联网换钥移出事务；失败路径同样计入一次性消费 |
| 3 | 5xx/超时掉进 Mock | `CLOUD_WEB_MODE` 强制云端；本地预览仅 `file://` 与「拿到 HTTP 状态码且非 404」判定，其余回退 Mock |
| 4 | 云端下一页/每页条数不重新拉数 | 下一页与 pageSize 变更均走 `sheinAuthCloudRefreshStores()`（并加页码越界保护） |
| 5 | 未实证契约写成已实证 | client 模块头新增 UNVERIFIED 清单（应用级签名 VALUE、get-by-token 响应结构、33051002 码、query-store-info 字段名）；规划稿 §2 #3 同步标注 |
| 6 | 前端入口/按钮未接权限码 | `FEATURE_MODULES.sheinstoreauth.requiredPermissionsAny` + `sheinAuthCanManage()`（云端按 `system.shein_store.manage`，mock 预览回退管理员角色）；删除/生成链接/改名/重新授权全部收口 |

回归测试（4 条新增）：信息接口闪断后重授权仍一行并回填商家ID；带 storeId 且 openKeyId 轮换仍更新目标行；换钥失败后重放 409；占位店名与 verify 回填。

## 可合并后补：本轮已采 8 项

渲染 esc 转义（店名/商家ID/事件）· `redirect_base` 空配置拒绝生成链接（503）· store_info 白名单字段入库 · tempToken 掩码收紧（首2尾2）· 生成链接事件去掉 state 尾号 · 换钥失败只回稳定文案（平台原文留服务端事件）· `PERMISSION_MENU_GROUPS` 收录两项 · 回调页 `history.replaceState` 清 URL

## 未采（留联调/部署阶段）

- 回调入口 IP 限流：部署层（nginx）加，避免应用层新增依赖
- 回调 GET 的 query 日志：部署清单中列入 nginx 配置（应用日志本就只记路由模板）
- 合并 main 时**不带** `docs/prototypes/20260917-store-auth/`（预览副本 1.8 万行）与 `review-request/response-*.md`；预览生成器说明保留在模块文档
