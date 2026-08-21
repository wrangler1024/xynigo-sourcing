# Xynigo Sourcing

[简体中文](README.md) | [English](README_EN.md)

Xynigo Sourcing 是一套面向跨境电商团队的开源代采协同系统，目前处于早期开发阶段。项目通过本地 Web 界面整合 HubStudio Local API 与浏览器自动化能力，帮助团队逐步规范采购环境、买家账号、订单和物流等工作流程。

> 本项目与 SHEIN、HubStudio、Microsoft、飞书或 Lark 不存在隶属、合作或官方背书关系。请仅操作你有权使用的账号和系统，并遵守适用的平台规则及法律法规。

项目由 **Velane Technology** 维护，由 **Xynigo** 负责实践验证，业务流程由 **Samforo** 贡献。

## 当前模块

- 墨西哥站和美国站订单及物流查询，包括限定隐私范围的物流截图。
- 墨西哥站和美国站买家账号注册任务，包含条款确认，以及遇到无法识别的验证流程时转交人工处理。
- 批量创建墨西哥站和美国站 HubStudio 环境，支持按站点记忆采购分组、预演、写入确认、断点续作及不含凭证的映射结果导出。采购员名单写死四人（新刚-XG / 志恒-ZH / 康德-KD / 宇航-YH），环境名使用英文代号（如 `XG-MX-0819-001`）；另有备用/测试环境模式，只建环境并写备注，不导 Cookie、不绑号、不写台账。
- 可选的飞书多维表格 OpenAPI 集成：通过企业自建应用直连统一买家号台账，建环境前执行双业务键与站点冲突预检，成功后写入并回读确认；同事电脑不需要安装 `lark-cli`。

当前稳定版为 `v0.8.0`。项目正在持续开发，暂未提供托管式 SaaS 服务。

v0.8.0 将买家号建环境后的飞书台账处理从手工 TSV 升级为可选的企业自建应用 OpenAPI 直连：系统在 HubStudio 写入前执行统一台账双键与站点冲突预检，建环境成功后按行写入并回读确认；部分失败可仅重试待同步行，不会重复执行 HubStudio 步骤。TSV 仍作为人工应急留档，不是 API 成功判据。

v0.7.3 扩展买家号建环境的严格 xlsx 解析：除既有 `orderNo` 接码链接外，兼容新号商交付的 `id + email` 与仅 `email` 链接。新格式必须满足固定路径和参数集合，且链接邮箱必须与账号邮箱一致；系统为缺少业务订单号的行生成不可逆、稳定的内部去重号，继续用于跨分组查重和幂等恢复。

v0.7.2 修复买家号建环境后的飞书台账直贴 TSV：根据 MX/US 默认 Grid View 分别输出列序，去掉表头和旧备注列，为「购买日期」公式字段保留空占位，并明确提示必须从第一空行的「邮箱账号」列开始粘贴。系统下载的号商入库 xlsx 模板仍固定带表头。

v0.7.1 将订单物流查询导出的轨迹截图改为 Excel 真正的“图片置于单元格”，不再使用漂浮在网格上方的绘图对象。采购同事可以连续框选含图片的单元格并批量复制、粘贴；图片仍直接封装在 `.xlsx` 内，不依赖外部文件或链接。该能力面向 Microsoft 365 和 Excel 2024，旧版 Excel 或 WPS 的显示与复制兼容性不作保证。

v0.7.0 为买家号建环境模块带来采购员名单与备用环境体系：采购员写死四人并使用英文代号命名环境（如 XG-MX-0819-001），新增备用/测试环境模式（不绑号、只建环境写备注），建环境并行执行，内置默认代理提取链接实现同事端零配置，并以 Cookie 登录域自动校验账号站点，错文件错站整批拒收。

## 环境要求

- Python 3.9 或更高版本。
- 本地已安装并登录 HubStudio 客户端。
- `websocket-client` 和 `openpyxl`。
- 可选：使用飞书 OpenAPI 时，需要有权访问目标多维表格的企业自建应用。应用凭证从左侧“系统设置”录入，统一台账只需粘贴包含 `table=tbl...` 的 `/base/` 或 `/wiki/` 完整链接，系统自动解析，不依赖 `lark-cli`。

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
| `XYNIGO_PROXY_LINK` | 可选：首次运行默认值。工具已内置默认动态代理提取链接，无需配置即可使用；设置页可保存自定义链接覆盖，清除即恢复内置默认 |
| `XYNIGO_PURCHASE_TAG` | HubStudio 采购环境分组 |
| `XYNIGO_PURCHASE_TAG_MX` / `XYNIGO_PURCHASE_TAG_US` | 可选：按 MX/US 站点分别指定采购环境分组 |
| `XYNIGO_REGISTER_TAG` | HubStudio 注册环境分组 |
| `XYNIGO_REGISTER_TAG_MX` / `XYNIGO_REGISTER_TAG_US` | 可选：按 MX/US 站点分别指定注册环境分组 |
| `XYNIGO_LARK_BASE_TOKEN` | 可选：首次运行时迁移统一买家号台账 Base Token；正常使用请在系统设置中配置 |
| `XYNIGO_LARK_TABLE_ID` | 可选：首次运行时迁移统一买家号台账 Table ID |
| `XYNIGO_LARK_TABLE_ID_MX` / `XYNIGO_LARK_TABLE_ID_US` | 仅供旧版管理员补账命令兼容使用；Web OpenAPI 主链路不按站点拆表 |
| `XYNIGO_LARK_OPERATOR_OPEN_ID` | 仅供旧版管理员补账命令使用的可选操作人 ID |

旧版模块三 Mac 管理员补账命令仍需 `lark-cli`，并且必须显式核对站点；美国站使用
`python -m purchase_tool backfill --site US ...`，并要求独立配置
`XYNIGO_LARK_TABLE_ID_US`。该命令只作为历史应急工具，不是同事电脑或 Web 主链路的依赖。

设置页不会要求同事手工拆分 Base Token / Table ID：普通 `/base/` 链接在本机解析；`/wiki/` 链接通过应用身份执行一次只读节点解析，因此应用还需开通 Wiki 节点读取权限并可访问该知识空间。台账目标支持重新配置，粘贴新链接并确认后替换旧目标；设置页同时提供“买家号统一台账完整模板”下载，前 14 列保持 API/TSV 契约，后 8 列镜像现有飞书运营字段。换表后应按模板设置系统字段、字段类型、显示样式和单选项，再执行只读字段检查。完整链接不会落盘，系统只把解析后的 Base Token / Table ID 保存在被 Git 忽略的 `config.json` 中。飞书 App ID / App Secret 保存在当前用户的 macOS 钥匙串或 Windows DPAPI 加密文件中，`tenant_access_token` 仅驻留进程内存；Web API 只返回是否已配置和脱敏 App ID，不回显 Secret、完整链接、Base Token 或 Table ID。App Secret 可以保存在获准使用系统的同事电脑上，但不应写进源码、发行包、日志或截图。包含敏感信息的输入工作簿必须放在仓库之外。

一个企业自建应用可以在其获授权范围内访问多张多维表格；Xynigo 当前只配置一个“买家号（统一）”写入目标，后续其他代采业务表可增加独立路由配置，共用同一应用凭证和鉴权客户端。备份表不参与自动写入。

## 安全设计

- 真实凭证仅在运行时接收，并在进度信息及日志中脱敏。
- 操作系统支持时，敏感临时数据使用限制性文件权限。
- 对真实平台执行写入操作前必须明确确认。
- 飞书回写默认关闭，并与 HubStudio 写入分开确认；写入前全表双键查重，部分失败不会回滚或重复执行已成功的 HubStudio 步骤。
- 飞书写入只有在写后回读确认字段一致时才计为成功；超时或结果不确定时标记为待重试。
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

- 扩展更多代采业务多维表格路由，共用企业自建应用凭证并保持每张表独立授权。
- 分离预览版和稳定版更新通道。
- 完善 Windows 与 macOS 双端自动化发行构建。
- 持续增加代采、订单、履约和报表模块。

## 开源协议

本项目采用 Apache License 2.0，详情参见 [LICENSE](LICENSE)。
