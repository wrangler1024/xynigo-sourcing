# Xynigo Sourcing Agent 协作规则

## 开始工作前

每个新会话或新 Agent 在修改代码前必须依次完整阅读：

1. `HANDOFF.md`
2. `docs/当前开发交接.md`
3. `docs/项目记忆.md`
4. `docs/后端实施交接.md`

先执行只读检查确认当前分支、HEAD、工作区和本地监听端口。不要假设交接时的进程或端口仍然存在，也不要未经用户确认停止正在使用的采购服务。

## 开发约束

- 默认使用中文沟通和编写业务文档。
- 保持现有本地采购 API、HubStudio/CDP 操作和真实写入确认流程兼容；认证开发不得顺带重写业务模块。
- 前端隐藏菜单不能代替后端权限校验。新成员默认 `pending`，不得为了联调打开全员自动激活。
- 飞书用户 OAuth 与飞书多维表格企业应用凭证属于两个鉴权域，禁止混用。
- 不把 App Secret、会话令牌、数据库密码、Cookie、Open ID、代理链接或真实业务数据写入仓库、测试、日志和交接文档。
- 发现已有改动时先检查差异并保留，不覆盖不属于当前任务的内容。

## 验证基线

本地应用：

```bash
.venv/bin/python -m unittest discover -s tests
```

云端认证服务：

```bash
cd cloud/auth-service
uv run pytest -q
```

当前已验证基线及真实联调边界以 `docs/当前开发交接.md` 为准。
