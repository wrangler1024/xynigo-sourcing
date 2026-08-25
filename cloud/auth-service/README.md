# Xynigo 云端认证服务

这是与本地采购执行器隔离的云端服务，负责飞书登录、组织身份、角色权限、可撤销会话和登录审计。它不连接 HubStudio，也不改变当前本地服务的依赖或端口。

## 为什么需要云端数据库

用户、角色、权限、会话和审计需要被所有采购电脑共享，并支持即时停用、会话撤销和追责，因此必须有云端事实源。首期使用与认证服务同机的 PostgreSQL；后续业务量或可用性要求提高时，可迁移到腾讯云托管 PostgreSQL。

这些表不放在飞书多维表，也不放在采购员电脑。飞书负责 OAuth 身份来源和协同数据，PostgreSQL 负责 Xynigo 的身份状态与权限裁决。

## 首版边界

- 飞书 OAuth v3，`state` 防 CSRF，并默认启用 PKCE S256；支持受控切换到飞书兼容所需的 PKCE plain 或禁用模式，授权码回调状态只允许使用一次。
- 用户身份主键使用 `tenant_key + open_id`，不使用邮箱或手机号作为登录主键。
- 新用户默认 `pending`，不会自动获得业务权限。
- 超级管理员通过环境变量显式指定飞书 `open_id`，不采用“第一个登录的人自动成为管理员”。
- 云端网页只保存 HttpOnly 会话 Cookie；本地执行器采用一次性登录桥换取 Bearer 会话，最终令牌仅保存在 macOS 钥匙串或 Windows CurrentUser DPAPI 中，浏览器页面拿不到轮询令牌或会话令牌。数据库始终只保存 SHA-256 会话摘要。
- 飞书 `user_access_token` 仅用于当次读取身份，用完即丢弃，不写数据库。
- PostgreSQL 不发布宿主机端口；PostgreSQL 18 数据卷挂载到其版本感知父目录 `/var/lib/postgresql`。认证服务只绑定 `127.0.0.1:8080`，由同机 HTTPS 反向代理转发。
- 认证服务校验 Host 白名单并返回 no-store、CSP、Referrer-Policy、nosniff 等安全响应头；容器使用非 root、只读根文件系统、能力清空、内存上限和日志轮转。

## 数据表

| 表 | 作用 |
|---|---|
| `tenants` | 飞书企业与 Xynigo 租户映射 |
| `users` | 飞书用户映射、启用/停用状态 |
| `roles`、`permissions` | 租户角色和系统权限码 |
| `user_roles`、`role_permissions` | 用户授权关系 |
| `sessions` | 短期、可撤销的登录会话摘要 |
| `oauth_login_attempts` | 5 分钟内有效的一次性 OAuth state/PKCE 上下文 |
| `local_login_requests` | 本地执行器 5 分钟内有效、单次消费的飞书登录交换请求 |
| `audit_events` | 登录成功、拒绝及后续敏感操作审计 |
| `purchase_orders` | 租户隔离的运营采购单、草稿版本与提交身份 |
| `purchase_order_lines` | 采购明细快照、稳定行键和采购流转状态 |
| `purchase_sync_outbox` | PostgreSQL 事务内生成的飞书镜像同步事件 |

`0003_purchase_request_foundation` 及上述三张采购表已于 2026-08-25 迁移到隔离测试实例。首次手工提交验收后，测试实例有 1 张主单、1 条明细和 1 条待处理 Outbox；飞书同步 Worker 尚未实现，`syncStatus=pending` 不代表 Base 已写入。

Web 采购中心 P0 读取与采购执行测试接口已部署到隔离测试实例：除 `GET /v1/procurement/overview`、`GET /v1/procurement/orders` 和 `GET /v1/procurement/orders/{purchaseOrderId}` 外，还包括 `POST /v1/procurement/claims`、`POST /v1/procurement/orders/{purchaseOrderId}/splits` 与 `GET /v1/procurement/execution/splits`。接口契约见 [`../../docs/采购中心接口-P0.md`](../../docs/采购中心接口-P0.md)。

## 本地执行器登录桥

1. 本地 Python 服务向 `/v1/auth/local/start` 创建一次性请求；认证服务返回飞书授权地址和高熵轮询令牌。
2. 本地服务只把授权地址交给页面并在系统浏览器打开，轮询令牌留在 Python 进程内。
3. 飞书回调只把请求标记为已批准，不在授权页面写入本地会话。
4. 本地服务通过 `/v1/auth/local/poll` 单次换取会话，保存到系统安全存储；页面只收到脱敏用户、角色和权限摘要。
5. 登录完成页会尝试自动关闭；本地主页面在成功领取会话后也会主动关闭其持有的飞书授权窗口。浏览器不允许关闭手动打开的标签时，完成页只显示安全的手动关闭提示。
6. 本地业务 API 再校验云端会话与具体权限；退出时云端撤销会话并清理本机安全存储。

## 成员、角色与会话管理 API

以下 `/v1/admin/*` 接口已部署到隔离测试实例；它们仍属于测试运行能力，不代表正式生产开放：

- 成员：列表/状态筛选、详情、按飞书绑定手机号精确查找并新增、批准、停用、恢复；停用成员同时撤销其有效会话。后台新增的成员仍固定为 `pending`，不会因为被管理员录入而绕过审批。
- 角色：角色列表、自定义角色创建/重命名/安全删除、系统权限目录、角色权限配置、成员角色分配。
- 会话：当前租户有效会话摘要、单会话撤销、按成员撤销全部会话。

管理员 API 继续使用同一短期可撤销会话，并在云端执行 `system.member.manage`、`system.role.manage` 最终裁决。跨租户目标按不可见处理并记录拒绝审计；响应不包含会话令牌、令牌摘要、飞书 Open ID 或其他登录凭证。

成员新增采用两段式流程：先以应用身份调用飞书通讯录 API，将管理员输入的绑定手机号精确解析为当前应用可见的飞书成员，再由确认接口重新解析并创建 Xynigo 用户。手机号只在请求期间发往飞书，不写入 Xynigo 数据库、响应或审计；解析结果也不把飞书 Open ID 暴露给浏览器。该能力按最小权限需要“小犀代采”开通 `contact:user.id:readonly`、`contact:contact.base:readonly`、`contact:user.base:readonly`、`contact:user.employee:readonly`，并配置与实际使用成员相匹配的通讯录数据权限范围和应用可用范围；无需读取手机号字段权限。

后端固定维护三个系统角色：`super_admin`（超级管理员）、`admin`（管理员）和默认无权限的 `member`（成员）。系统角色不能重命名或删除；`super_admin` 与 `admin` 的权限集由后端锁定。`admin` 自动获得目录中的全部日常业务与常规管理权限，但明确不包含 `system.lark_connection.manage`、`system.integration.manage` 和代理凭证高敏权限 `resource.ip.credential.manage`；这三项只能由 `super_admin` 持有，不能通过角色策略授予其他角色。具有 `system.role.manage` 的管理员可以创建、重命名自定义角色，角色代码由后端生成；已分配成员的角色必须先解除授权才能删除。非超级管理员的角色管理员不能授予自身没有的权限，也不能把超级管理员角色分配给任何成员。

当前目录共 28 项权限，其中 `workbench.access`、`procurement.access`、`operations.access`、`finance.access`、`assistant.access`、`analytics.access` 分别控制工作台、采购中心、运营中心、财务中心、小犀助手和数据分析的一级模块入口；8 项 `resource.store.*`、`resource.ip.*` 权限裁决店铺与代理 IP 能力，3 项 `procurement.request.*` 权限裁决运营采购单的读取、草稿保存与正式提交。`admin` 固定拥有其中 25 项，`super_admin` 拥有全部 28 项。它们只裁决已实现能力及入口可见性；尚未开发的写入功能与数据范围不会因获得权限而自动开放。

公网 Nginx 对登录创建接口单独限流。轮询请求只接受摘要匹配、未过期且尚未消费的令牌，并使用数据库行锁保证同一登录请求最多签发一个会话。

## 本地合成测试

要求 Python 3.12 和 `uv`，不会访问真实飞书或生产数据库：

```bash
uv sync --extra test
uv run pytest
```

当前本地工作区为 35 项通过，包含旧库新增权限时的目录升级回归。覆盖待审批默认值、手机号精确解析、新增成员的角色预分配与权限上限、内置管理员高敏权限边界、手机号/飞书标识不落响应和审计、跨租户拒绝、自定义角色生命周期、会话撤销、采购单租户隔离、幂等与提交锁定，以及 Web 采购中心概览/列表/详情、隐私字段与详情审计；测试不会访问真实飞书或生产数据库。

## 腾讯轻量服务器测试部署

当前隔离测试实例运行在广州多项目共享测试服务器 `shared-test-gz-01`（实例 ID `lhins-b6nu398t`），入口为 `https://xynigo.samforo.icu`。服务器端 PostgreSQL 已迁移，HTTPS、回环端口隔离、备份恢复和容器重启均已验证；它没有接管本地采购服务。

该服务器永久只作为测试环境使用，不得在其上创建生产容器、生产数据库、生产数据卷或生产密钥，也不得因 Xynigo 部署修改 n8n 和其他既有服务。生产环境只能部署到独立实例 `xynigo-prod-gz-01`，使用 `/opt/xynigo-auth-prod`、Compose 项目 `xynigo-auth-prod`、独立网络/数据库/账号/密钥/Cookie/日志/备份和正式飞书 Base/Table。生产 PostgreSQL 必须为空库并执行已批准的 Alembic 迁移，禁止复制测试数据库、测试数据卷或测试数据。完整决策见 [`../../docs/架构决策-20260825-广州腾讯轻量服务器测试与生产双环境隔离.md`](../../docs/架构决策-20260825-广州腾讯轻量服务器测试与生产双环境隔离.md)。

“小犀代采”飞书应用已在安全设置登记完全一致的回调地址：

```text
https://xynigo.samforo.icu/v1/auth/feishu/callback
```

部署要求：

1. 准备域名和 HTTPS 证书，并在飞书应用后台登记完全一致的回调地址。
2. 将 `.env.example` 复制为服务器上的 `.env`，替换所有占位值；`.env` 已被 Git 忽略。
3. 安全组只开放 `80/443` 和受限来源的运维端口，不开放 `5432`、`8080`。
4. 首次启动后执行数据库备份与恢复演练；当前测试备份只保存在服务器 root-only 目录，正式使用前需增加异机副本和保留策略。
5. 由 Caddy/Nginx 将公网 HTTPS 请求反向代理到 `127.0.0.1:8080`。

服务器首次从 PyPI 构建镜像可能很慢；当前镜像层已缓存，后续仅在依赖清单变化时重新下载。测试实例已经切换到“小犀代采”应用，真实验证已覆盖飞书回调、超级管理员激活、会话落库、`/v1/auth/me` 权限摘要和登录审计。

当前租户在授权端未实际登记 PKCE challenge，S256 与 plain 的令牌交换均返回飞书 `20049`。因此测试实例受控设置为 `XYNIGO_AUTH_FEISHU_PKCE_METHOD=disabled`；代码默认值仍为 S256，且 disabled 模式仍启用随机 `state`、五分钟有效期、摘要存储和单次消费。飞书侧恢复兼容后应优先切回 S256。

测试实例不代表已经生产上线。2026-08-25 已部署到 `0004_procurement_execution_test`，权限目录为 `super_admin` 29/29、`admin` 26/29、`member` 0/29；采购基础、Web 采购中心读取、认领、分单保存和采购执行队列接口均已进入测试实例。本机 8771 加载当前工作区并复用既有登录，8767 保持原样。首轮受控联调后 PostgreSQL 有 1 张主单、1 条已认领明细、1 张待绑定资源分单、1 条分单明细和 1 条待处理 Outbox；Hub 环境、买家号与平台采购单号均未绑定，也未写真实 Base。普通成员新增/审批/登录/停用闭环、Webshare 普通表格授权、飞书同步 Worker、真实下单状态写入和正式绿色包仍未完成，Xynigo 数据范围功能暂不开发。
