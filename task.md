# Xynigo 当前开发任务

更新时间：2026-08-28 23:40 CST

本文件只记录当前有效任务。历史 Bug、修复过程和防复发经验见 [`docs/里程碑/20260828-v0.12.7统一入口执行器热修复与状态收口.md`](docs/里程碑/20260828-v0.12.7统一入口执行器热修复与状态收口.md)，本次发版证据见 [`docs/里程碑/20260828-v0.12.8统一执行器版本发布与共享测试部署.md`](docs/里程碑/20260828-v0.12.8统一执行器版本发布与共享测试部署.md)。

## 当前基线

- 共享测试：`0.12.8 / 0017_executor_workspace_rpc`，健康、就绪、页面和 OpenAPI 正常。
- 发布主线：`codex/release-v0.12.8`，HEAD / 标签目标 `3fab5d9`。
- CI：分支最终运行 `33185453176`、标签运行 `33185768103`，均为七任务全绿。
- GitHub：`v0.12.8` 为 prerelease；Latest 仍为 `v0.12.6`。
- 云端默认包：Windows `Xynigo_Sourcing_Windows_Setup_v0.12.8.exe`；macOS `Xynigo_Sourcing_macOS_Standard_v0.12.8.pkg`；两个平台都保留同版本绿色包回退。
- 云端镜像：`sha256:b4b863b47d2cb627b0ee5e5e9c659e01daacd6e9d9e8275f37e80b972f66b52f`。

## 已完成的版本收口

- [x] 冻结 v0.12.7 hotfix 列车，未创建 hotfix12、未移动旧标签、未覆盖旧资产。
- [x] 从完整 hotfix11 主线建立 v0.12.8，统一源码、Web、OpenAPI、安装包和资源目录版本。
- [x] Windows 标准安装、覆盖升级、安全卸载、状态中心和更新器自动验证通过。
- [x] macOS 标准包、应用载荷、绿色包和冻结运行时自动验证通过。
- [x] 修复“同一版本多次构建 Mac 包时旧顶层 manifest 哈希不刷新”，并增加回归测试。
- [x] 标签 CI 全绿后创建 v0.12.8 prerelease，10 个资产大小和 SHA-256 与锁定清单一致。
- [x] 部署前完成源码、PostgreSQL dump 和旧镜像回滚点；共享测试已切到 v0.12.8。
- [x] 验证公网健康/就绪、OpenAPI、Web 版本、Alembic、资源哈希和匿名下载 401。

## P0：真实设备切换验收

### 1. Windows 采购电脑覆盖安装

- [ ] 从云端系统入口下载 v0.12.8 标准包，核对 SHA-256 `1b821cc450232a38fd72b2d7e715ffe71bb5397386c0e7f1459b186cc69aab56` 后覆盖安装。
- [ ] 验证原设备配对、`config.json`、日志、运行数据和 CurrentUser DPAPI 凭证保留。
- [ ] 验证状态中心、托盘菜单、HubStudio 分组、物流查询、买家号文件解析、墨西哥/美国建环境和任务进度。
- [ ] 验证不会周期性闪出黑色控制台窗口，执行器退出后云端按 TTL 正确离线。
- [ ] 完成一次“发现更新 → 用户确认 → 安装 → 新运行时上线 → 旧进程退出”闭环。

验收：至少一台采购同事 Windows 电脑完成真实工作流；失败时保留脱敏日志、任务 ID、错误码和时间，不以重装掩盖原因。

### 2. macOS Apple Silicon 覆盖安装

- [ ] 核对标准包 SHA-256 `aa2b85ff5a48f1b34bbe2b296d531ab0c00fd5e53101a7b17537dea9487a3641`，按内部未签名教程手动放行并覆盖安装。
- [ ] 确认 Keychain 凭证、设备配对和本地数据保留，不再反复弹出同一钥匙串授权。
- [ ] 验证冻结运行时使用随包 CA，通过 HTTPS 连云端并持续心跳。
- [ ] 验证 HubStudio 分组读取和至少一个只读 `workspace.rpc.v1` 任务。

### 3. 真实故障与回滚演练

- [ ] 断网、执行器退出、HubStudio 主程序退出但 Local API 子进程存活三个场景分别验证状态语义。
- [ ] 安装失败时验证绿色包显式回退，不自动覆盖已安装标准版数据。
- [ ] 在不触发真实采购外部写入的前提下演练服务镜像回滚和数据库备份可读取。

## P0：主工作区收口

- [ ] 逐项盘点 `codex/new-ui-migration` 未提交的系统订单键、采购助手、HubStudio、采购契约、文档、原型和输出目录。
- [ ] 区分“已进入 v0.12.8”“仍为内部候选”“纯业务输出/历史备份”，逐文件提交，不整树覆盖。
- [ ] `doc-fetch-resources/`、`outputs/`、品牌资产和历史备份按 `.gitignore` 与交付规则处理。
- [ ] 将复盘中的状态生命周期、权限负向矩阵、DPI、冻结运行时 TLS 和发布一致性继续转为自动化门禁。

## P1：稳定化任务

- [ ] Windows 安装包增加 Authenticode 可信签名和时间戳；Mac 增加 Developer ID、公证与 stapling。
- [ ] 将 Windows 在线更新的安装器交接、父子进程退出和失败回滚做成独立端到端测试。
- [ ] 为 HubStudio Local API 的端口、认证、限流、父子进程状态和错误码建立长期兼容矩阵。
- [ ] 持续监控 `workspace.rpc.v1` 租约、重复读取、进度合并、任务不确定态和敏感正文加密。
- [ ] 真实设备验收稳定后再决定是否把 v0.12.8 提升为 Latest；不得用“已部署共享测试”替代稳定版批准。

## 当前卡点

1. **未签名安装包**：Windows Smart App Control 无单应用放行能力；当前只能内部手动处理，长期必须签名。
2. **真实设备测试资源有限**：只有两位实际使用同事承担生产式测试，没有独立测试人员。
3. **主工作区较脏**：内部候选、已上线代码和业务输出并存，盲目合并或 `git add -A` 风险高。
4. **Latest 暂未提升**：v0.12.8 是内部切换 prerelease；真实设备验收完成前 Latest 保持 v0.12.6。
5. **生产仍未授权**：当前域名、服务器和数据库都是共享测试；不能把真实测试通过解释为生产上线。

## 禁止事项

- 不再为 v0.12.7 增加 hotfix12，不移动旧标签，不覆盖或删除历史 Release 资产。
- 不把阶段性的 `status-center-v2` 安装包转发为升级包；统一使用 v0.12.8 目录中的锁定资产。
- 不把密码、Cookie、App Secret、Token、代理链接或完整收件信息写入仓库、任务、日志或截图。
- 不自动重跑外部写入结果不确定的物流、建环境、飞书或采购任务。
- 不在未确认真实设备验收、回滚点和生产闸门时把 v0.12.8 宣称为生产稳定版。

## 每次接手的最小检查

```bash
cd /Users/jeff/Documents/xynigo-sourcing
git status --short
git log -5 --oneline --decorate
git log -1 --oneline codex/release-v0.12.8
gh release view v0.12.8
curl -fsS https://xynigo.samforo.icu/healthz
curl -fsS https://xynigo.samforo.icu/readyz
```

随后再通过受控 SSH 核对 OpenAPI `0.12.8`、Alembic `0017`、容器镜像和四个服务器资产的文件名、大小与 SHA-256。
