# Xynigo Sourcing

[简体中文](README.md) | [English](README_EN.md)

Xynigo Sourcing 是一套面向跨境电商团队的开源代采协同系统，目前处于早期开发阶段。项目通过本地 Web 界面整合 HubStudio Local API 与浏览器自动化能力，帮助团队逐步规范采购环境、买家账号、订单和物流等工作流程。

> 本项目与 SHEIN、HubStudio、Microsoft、飞书或 Lark 不存在隶属、合作或官方背书关系。请仅操作你有权使用的账号和系统，并遵守适用的平台规则及法律法规。

项目由 **Velane Technology** 维护，由 **Xynigo** 负责实践验证，业务流程由 **Samforo** 贡献。

## 当前模块

- 墨西哥站和美国站订单及物流查询，包括限定隐私范围的物流截图。
- 买家账号注册，包含条款确认，以及遇到无法识别的验证流程时转交人工处理。
- 批量创建 HubStudio 环境，支持预演、写入确认、断点续作及不含凭证的映射结果导出。
- 可选的 Lark Base 台账集成，相关参数全部在运行时配置。

当前稳定版为 `v0.6.3`。项目正在持续开发，暂未提供托管式 SaaS 服务。

v0.6.3 为订单及物流查询补齐 SHEIN 美国站能力，支持美国站路由、英文订单状态、承运商商业名称、物流单号、轨迹截图、风险验证订单和环境浏览器当地时间；同时保留墨西哥站兼容性，并在站点与环境命名不一致时阻止误查。

## 环境要求

- Python 3.9 或更高版本。
- 本地已安装并登录 HubStudio 客户端。
- `websocket-client` 和 `openpyxl`。
- 可选：使用 Lark Base 适配器时需要 `lark-cli`。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m purchase_tool
```

本地界面默认打开 `http://127.0.0.1:8765`。如果端口已被占用，程序会自动尝试下一个可用端口。

在 macOS 上，也可以通过 `启动-Mac.command` 启动本地开发版本。

## 运行时配置

本仓库不保存部署标识、API Key、Cookie、账号凭证、代理链接或 Lark 记录 ID。

| 环境变量 | 用途 |
|---|---|
| `XYNIGO_PROXY_LINK` | 创建新环境时使用的 HubStudio 动态代理提取链接 |
| `XYNIGO_PURCHASE_TAG` | HubStudio 采购环境分组 |
| `XYNIGO_REGISTER_TAG` | HubStudio 注册环境分组 |
| `XYNIGO_LARK_BASE_TOKEN` | 可选的 Lark Base Token |
| `XYNIGO_LARK_TABLE_ID` | 可选的 Lark Base 数据表 ID |
| `XYNIGO_LARK_OPERATOR_OPEN_ID` | 台账回填命令使用的可选操作人 ID |

本地界面偏好保存在 `config.json` 中，该文件已被 Git 忽略。包含敏感信息的输入工作簿必须放在仓库之外。

## 安全设计

- 真实凭证仅在运行时接收，并在进度信息及日志中脱敏。
- 操作系统支持时，敏感临时数据使用限制性文件权限。
- 对真实平台执行写入操作前必须明确确认。
- 批次续作文件仅保存不可逆的账号标识及非敏感进度信息。
- 更新包和发行包不得包含本地 `config.json`、日志或用户输入文件。

部署或报告安全问题前，请先阅读 [SECURITY.md](SECURITY.md)。

## 测试

```bash
python -m pip install -e .
python -m unittest discover -s tests
```

仓库内所有测试均使用合成账号、示例域名和虚拟 Cookie。

## Windows 与 macOS 绿色包

```bash
bash 组装Windows绿色包.sh
bash 组装macOS绿色包.sh
```

构建产物会写入 `dist/`，包含对应平台的完整 ZIP、SHA-256 文件和机器可读的双平台更新清单。Windows 包含官方嵌入式 Python；macOS 包使用 PyInstaller 构建 Apple Silicon `arm64` 自包含运行时。macOS Intel 不在维护范围内。

### 双平台在线更新

- v0.5.0 第一次仍需人工下载绿色包并完整解压。
- macOS 从 v0.5.1 开始提供同级别绿色包与在线更新。
- Windows 双击 `启动.bat`，macOS 打开 `启动-Mac.command`；系统启动后在 WebUI 右上角检查新版本。
- 发现新版本时点击版本提醒，运行黑窗口会自动置前；在窗口输入 `Y` 更新或 `N` 暂不更新，页面不会静默安装。
- 不需要 GitHub 账号或 Git。下载继承操作系统默认网络配置；Windows 兼容系统代理和 TUN 模式。
- 更新包通过 SHA-256 校验后才替换；替换前备份当前程序，失败时自动回滚。
- 更新只替换程序受管文件，始终保留 `config.json`、本地数据、日志和用户导入文件。更新检查失败不会阻止工具启动。

## 路线图

- 分离预览版和稳定版更新通道。
- 完善 Windows 与 macOS 双端自动化发行构建。
- 持续增加代采、订单、履约和报表模块。

## 开源协议

本项目采用 Apache License 2.0，详情参见 [LICENSE](LICENSE)。
