# Xynigo 云端认证服务

这是与本地采购执行器隔离的云端服务，负责飞书登录、组织身份、角色权限、可撤销会话和登录审计。它不连接 HubStudio，也不改变当前本地服务的依赖或端口。

## 为什么需要云端数据库

用户、角色、权限、会话和审计需要被所有采购电脑共享，并支持即时停用、会话撤销和追责，因此必须有云端事实源。首期使用与认证服务同机的 PostgreSQL；后续业务量或可用性要求提高时，可迁移到腾讯云托管 PostgreSQL。

这些表不放在飞书多维表，也不放在采购员电脑。飞书负责 OAuth 身份来源和协同数据，PostgreSQL 负责 Xynigo 的身份状态与权限裁决。

## 首版边界

- 飞书 OAuth v3，`state` 防 CSRF，并启用 PKCE S256；授权码回调状态只允许使用一次。
- 用户身份主键使用 `tenant_key + open_id`，不使用邮箱或手机号作为登录主键。
- 新用户默认 `pending`，不会自动获得业务权限。
- 超级管理员通过环境变量显式指定飞书 `open_id`，不采用“第一个登录的人自动成为管理员”。
- 浏览器只保存 HttpOnly 会话 Cookie；数据库只保存 SHA-256 会话摘要。
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
| `audit_events` | 登录成功、拒绝及后续敏感操作审计 |

## 本地合成测试

要求 Python 3.12 和 `uv`，不会访问真实飞书或生产数据库：

```bash
uv sync --extra test
uv run pytest
```

## 腾讯轻量服务器部署前置

1. 准备正式域名和 HTTPS 证书，并在飞书应用后台登记完全一致的回调地址。
2. 将 `.env.example` 复制为服务器上的 `.env`，替换所有占位值；`.env` 已被 Git 忽略。
3. 安全组只开放 `80/443` 和受限来源的运维端口，不开放 `5432`、`8080`。
4. 先执行数据库备份与恢复演练，再运行 `docker compose up -d --build`。
5. 由 Caddy/Nginx 将公网 HTTPS 请求反向代理到 `127.0.0.1:8080`。

当前仓库只完成代码、迁移和合成测试，不代表已经在生产服务器部署或与真实飞书应用联调。
