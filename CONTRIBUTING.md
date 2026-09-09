# 开发与提交

协作规则见 [AGENTS.md](AGENTS.md)。本次流程修订为 20260910 草案，待联合评审。命令均相对本仓根目录，明确标注切换目录的除外。

## 工作区与提交

开工查看 `git status --short --branch`、`git log -3 --oneline` 和最近任务交接。一个交付单元使用独立分支；并行任务在独立 worktree 中工作，不切换或清理他人使用中的目录。

提交只暂存本人负责且已检查的文件；禁止用清理、还原或强推消除来源不明的改动。交接维护者可在明确交接后代为提交，并保留作者和验证来源。大型架构变化先形成可评审设计或 Issue，不将方案讨论混入无关修复。

## 测试入口

本机包要求 Python 3.9 以上；云端包要求 Python 3.12 以上。Web 相关测试使用 Node.js 22。CI 的具体矩阵以 [.github/workflows/tests.yml](.github/workflows/tests.yml) 为准。

在本仓独立虚拟环境中准备本机测试：

```bash
python -m pip install -e '.[test]'
python -m pytest tests -q --tb=short
```

云端使用另一独立虚拟环境，解释器须为 Python 3.12 以上；切换到 `cloud/auth-service` 后执行：

```bash
python -m pip install --require-hashes -r requirements.lock
python -m pip install -e '.[test]'
python -m pytest tests -q --tb=short
```

不要用 `unittest discover` 替代 pytest：项目包含 pytest 函数测试。测试使用合成数据、临时数据库和 Fake 网关，不从共享环境复制真实订单或凭证。迁移验证应使用隔离 PostgreSQL；共享测试库迁移不是普通单元测试。

开发时运行受影响专项；修改公共模块时扩大到相应测试套件。提交结果写明命令、环境、通过/失败/跳过及原因，不能把编译检查称为运行验收。Windows/macOS 安装和原生运行行为由对应平台验证；本机交叉编译不替代该验证。

## 生成物清单

下面输出均位于 `cloud/auth-service/src/xynigo_auth/`。源文件均相对 `src/purchase_tool/`。

| 同步脚本 | 输入 → 输出 |
|---|---|
| `sync_web_workspace.py` | `web/index.html`、三个品牌图标、三个预览 SVG → `web/` 同名文件；另将 `web/xynigo-x.ico` 复制为 `web/favicon.ico` |
| `sync_procurement_import_core.py` | `procurement_import.py` → `procurement_import_core.py`（增加头注并替换两个网关导入）；`xlsx_cell_images.py` → `procurement_import_xlsx.py`；`system_order_key.py`、`purchase_link_cell.py`、`procurement_image_fetch.py` → 同名副本 |
| `sync_environment_plan_core.py` | `env_batch.py` 中显式选定的 AST 节点 → `environment_plan_core.py`；`redaction.py` → `environment_plan_redaction.py` |

查看生成器获取准确列表，不把整个 Web 目录盲目复制到云端：桌面专用资源不因此变成共享资源。修改生成器时同样需要检查输出。

当前写入命令如下，按受影响范围执行：

```bash
python cloud/auth-service/deploy/sync_web_workspace.py
python cloud/auth-service/deploy/sync_procurement_import_core.py
python cloud/auth-service/deploy/sync_environment_plan_core.py
```

### 当前可执行的一致性检查

修改阶段同步并检查 diff，将权威源与输出一起提交。评审阶段在该提交的干净独立 worktree 中，使用 Python 3.12 运行受影响生成器，再检查：

```bash
git diff --exit-code -- cloud/auth-service/src/xynigo_auth
git ls-files --others --exclude-standard -- cloud/auth-service/src/xynigo_auth
```

第一条须返回 0，第二条须无新增输出。生成步骤不得在评审者正在共享使用的工作区进行；一旦出现差异，应报告缺失的提交，不能把本地补生成后的测试成功当成原提交通过。

现有采购导入测试检查解析器与 XLSX 副本；导入历史 Web 测试仅检查入口标记。这些检查尚未覆盖全部生成输出。

### 待实施的机器门禁

本次文档草案不增加命令能力。三个同步脚本后续统一增加只读 `--check`：共享生成逻辑、不写文件或创建目录、缺失或不一致时非零退出、报告差异路径与修复命令。CI 在任何生成或构建前执行；新增输出加入同一清单，不为每份复制品重复实现比较逻辑。AST 生成使用固定解释器版本，避免格式化差异。

验收至少覆盖正确、过期、缺失输出，以及检查前后工作区完全不变。生成一致性与行为回归分别验证，不能互相替代。

## 公开安全与评审

仅使用合成或脱敏夹具。不得提交真实密码、Cookie、Token、代理凭证、服务器地址、内部链接、内部标识、客户订单、原始截图或浏览器快照。真实平台写入的确认门禁不得移除。

提交前运行：

```bash
python scripts/audit_public_release.py
git diff --check
git diff --cached
```

自动审计只检测已知模式，不能替代暂存内容检查；PR 正文、附件和发布资产也按公开内容检查。当前审计遍历工作目录，若仅本地资料触发，使用干净候选 worktree 验证并确认它们未暂存，不扩大忽略范围来绕过真实问题。

评审按 [协作与交接](docs/协作与交接.md) 记录确切提交及“必须改/建议改”。合并或发布前确认必需检查实际运行且通过。文档更正采用路径、命令和一致性核对；不为了文档修改重复执行整套业务或真实平台测试。
