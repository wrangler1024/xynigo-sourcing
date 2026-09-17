# 评审请求 · SHEIN 开放平台店铺授权模块（原型定版 + P0 后端全链）

> 本文件是给评审方（Cursor）的提示词，可整段复制。

---

请评审一个新模块：**店铺授权（SHEIN 开放平台凭证管理）**，含静态原型、后端换钥/加密存储/列表/验证/改名/删除/事件全链。

**只读评审，不要改代码。** 输出：结论（可合并 / 改后合并 / 不可合并）+ 必须改项（带文件:行）+ 可以合并后补项。

## 评审对象

- 分支 `codex/shein-store-auth-module-design`（已推送 origin），提交范围 `a455663..42dc8d5`（11 个提交，基线 `origin/main`）
- **开发在主检出 `/Users/jeff/Documents/xynigo-sourcing` 上进行**（本分支独占该工作区，已推送）。请在独立 worktree 检出评审，勿动主检出。

## 背景

SHEIN 开放平台卖家自研资质与应用「Xynigo ERP（自运营）」均已过审，第一家真店（观潮，商家ID 18301880）已手动授权并验证全部接口。本模块把手动授权流程产品化：管理员生成授权链接 → 店铺主账号在卖家中心浏览器同意 → 回调自动换钥（tempToken → openKeyId + AES 加密 secretKey）→ 密文落库 → 列表/验证/重新授权/删除。规划与 10 项真机实测契约见 `docs/20260917_需求_SHEIN开放平台店铺授权模块.md`（§0 是实现落位摘要）。

## 改动清单（+2600 行左右）

| 文件 | 内容 |
|---|---|
| `cloud/auth-service/migrations/versions/0043_shein_store_auth.py`（新增） | 三张表：`shein_authorized_stores`（凭证+密文）/ `shein_auth_links`（一次性 state）/ `shein_auth_events`（事件，令牌只存掩码）；时间列全 server_default |
| `cloud/auth-service/src/xynigo_auth/shein_openapi_client.py`（新增） | HMAC 签名（应用级 x-lt-appid / 店铺级 x-lt-openKeyId）、`exchange_temp_token`、AES-128-CBC 解密（IV=`space-station-de`）、`query_store_info` |
| `cloud/auth-service/src/xynigo_auth/shein_store_auth_crypto.py`（新增） | secretKey Fernet 加密（复用部署密钥 `buyer_credential_encryption_key`，照 BuyerCredentialCipher 先例） |
| `cloud/auth-service/src/xynigo_auth/shein_store_auth_service.py`（新增） | 业务逻辑：链接生成/回调换钥 upsert（键=tenant+merchant_id+mode）/列表搜索分页/验证/改名/删除/事件 |
| `cloud/auth-service/src/xynigo_auth/shein_store_auth_contract.py`（新增） | Pydantic 请求契约 |
| `cloud/auth-service/src/xynigo_auth/main.py`（+230） | 7 个路由 + 权限目录 +2 + `GET /shein-auth/callback` 页面路由 |
| `cloud/auth-service/src/xynigo_auth/models.py`（+130） | 三个模型（列序被测试钉死） |
| `cloud/auth-service/src/xynigo_auth/config.py` / `.env.example` | SHEIN 五项配置（app id/secret/gateway/empower host/redirect base），不配置模块降级 503 |
| `src/purchase_tool/web/index.html`（+430，权威源） | 系统管理二级页全交互 + 云端双模式（mock 兜底） |
| `cloud/auth-service/src/xynigo_auth/web/index.html` | 同步副本（sync_web_workspace.py，逐字节一致） |
| `cloud/auth-service/tests/test_shein_store_auth.py`（新增，12 条） | 签名结构/AES 往返/回调全链含密钥轮换/state 一次性+过期/权限分级/列序断言 |
| `docs/prototypes/20260917-store-auth/`（新增） | 原型截图 + `make_preview.py`（本地解锁预览副本，解锁脚本只在副本不进正式源码） |
| `docs/20260917_需求_SHEIN开放平台店铺授权模块.md` | 规划+决策记录（含 §0 实现落位、决策 #8 多平台前瞻） |

## 请重点看

1. **安全红线（最高优先）**：secretKey 只密文落库——请独立核对所有出口：API 响应（`_store_payload`）、事件 note、异常 message、日志绑定。测试断言了 `secret` 不出现在响应/事件 JSON，请别只信测试，逐个出口过一遍。tempToken 是否只在 `exchange_temp_token` 调用期间存在？`_mask_temp_token` 掩码是否够？
2. **回调端点不要求登录态**（`POST /v1/shein-auth/callback`）：设计依据=授权动作发生在店铺主账号浏览器（可能未登录工作台），state 即一次性能力凭证（15 分钟、绑定租户与发起人、消费即失效）。请攻击这个设计：state 泄露窗口、重放、跨租户、暴力枚举（30 位小写字母数字）。`bind_request_identity` 没绑身份，系统日志走 `getattr(..., None)` 容忍——确认无其他中间件假设身份存在。
3. **upsert 语义**：重新授权按 `(tenant_id, merchant_id, mode)` 匹配。`merchant_id` 取自 `query-store-info` 的 `merchantId` 字段——**这是合成假设，真机返回字段名未验证**；缺失时回退 `open_key_id[:16]` 作匹配键。这个回退是否会造成同店分裂成两行（先授权时信息接口失败、后授权时成功）？可否接受/要不要先验证？
4. **两个待真机校准的契约假设**：① `get-by-token` 响应结构 `code/msg/data{openKeyId,secretKey}`；② 应用级签名 VALUE 是否为 `appid&timestamp&path`（店铺级 `openKeyId&ts&path` 已生产实证）。合成测试无法证真——请确认代码里这两处有清晰标注，联调失败时能快速定位。
5. **权限分级**：`system.shein_store.read`（查看+验证）/ `manage`（授权/改名/删除）。member 是系统锁定角色（权限集固定空）——「验证查看不限」靠自定义角色授予 read 实现。admin 自动获得两项。核对路由的 permission 归属是否有错挂。
6. **SQLite/PG 时间差异**：`_as_utc()` 规范 naive 时间后再比较（SQLite 测试库读回 naive）。生产 PG 读回 aware——确认该 helper 不吞掉真实的时区错误。
7. **前端双模式探测**（index.html `sheinAuthCloud`）：200/401/403/503 判云端，404/网络错回退 mock。刚修过一个判定反转 bug（42dc8d5），请复核当前逻辑有无残余边界（如 504/超时）。

## 已知取舍（非缺陷，供判断）

- **半托管是占位**：半托管应用未创建，选半托管生成的链接实际用自营 appid（`SHEIN_AUTH_APPS.semi.appid` 仅为前端展示占位）；正式支持需加 semi 应用配置。联调阶段应只用自营。
- query-store-info 失败不阻断绑定（换钥成功即入库，status=pending 待验证补齐）。
- 未部署测试机：`.env` 五项未配，部署后模块才可用；部署顺序（build migrate → run → build auth）按既有手册。
- IP 白名单 Clash 临时项（179.253.249.72）等部署验收后清理，测试/生产服务器 IP 已在列。

## 怎么跑

```bash
# 云端套件（含新增 12 条；全量 390 passed）
cd /path/to/worktree/cloud/auth-service
uv sync --extra test
uv run pytest -q

# 前端语法
# 解出 index.html 内联 <script> 跑 node --check（或直接评审 diff）

# 本地原型预览（可选）
python3 docs/prototypes/20260917-store-auth/make_preview.py
# 双击生成的 本地预览-店铺授权.html（Chrome）
```

## 评审输出要求

结论 + 必须改项（文件:行）+ 可合并后补项。特别欢迎对第 1、2 点（安全红线与回调鉴权设计）的独立攻击性检查。
