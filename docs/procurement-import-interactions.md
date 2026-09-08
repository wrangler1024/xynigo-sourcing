# 导入分单交互

订单文件框支持拖入单个 `.xlsx` 文件（最大 20 MB），也可使用选择按钮。拖放只选择文件；用户点击「解析并生成」后才解析。无效文件、多文件或文件夹被拒绝时保留原文件和解析结果。有效替换使旧计划失效，保留目标表选择。

目标卡片显示最近成功校验的最多 5 个「工作簿＋工作表」组合，支持恢复及移除。云端历史存入现有 `workspace_view_preferences` 表，按组织和用户隔离，不需要新增数据库迁移。仅成功校验的目标可进入历史；选项接口只能更新或移除已有记录。云端页面刷新、重新登录或同账号换设备后可恢复；本机旧工作台使用按账号隔离的浏览器存储。

每次新文件解析通过后，自动读取最新工作表列表，按稳定工作表 ID 恢复并重新校验。工作表改名会更新显示；原工作表被删除时停止，不切换至同名页或第一张表。自动校验不触发导入。前端用用户、计划、目标和选择版本识别过期响应，避免旧请求恢复错误校验状态。重新开始只清本批订单；清除历史是单独操作。

底部的「订单背景色」使用 32 × 18 px 的滑动开关，默认开启，按用户和目标表记忆。关闭时跳过背景色读取和填充，仍处理行高、链接和图片。开关只影响本次新增明细；已有颜色和去重跳过的订单不重新配色。新任务开启开关也不补刷历史空白行。

任务开始时保存 `fillOrderBackground`，写入前保存可填色的来源明细索引。失败重试或云端进程恢复沿用原任务设置及范围，不扩大到历史记录。无该字段的旧请求默认开启；非布尔设置会被拒绝。底色处理失败继续沿用既有阻断行为，展示警告降级尚未包含在本次实现。

## 接口

- `GET /v1/assistant/procurement-import/preferences`：读取当前账号最近目标。
- `POST /v1/assistant/procurement-import/preferences`：`action=color/remove`，传入已有目标的 `spreadsheetUrl`、`sheetId` 和布尔 `fillOrderBackground`；不能插入未校验目标。
- `POST /v1/assistant/procurement-import/target/validate`：校验成功后响应包含更新的 `preferences`。
- `POST /v1/assistant/procurement-import/sheet-sync`：新增可选布尔 `fillOrderBackground`，默认 `true`。
- 任务状态包含 `fillOrderBackground` 和恢复用的 `backgroundPlanIndices`。后者仅为来源行索引，不包含订单内容。

云端接口要求 `assistant.access`，写入偏好继续经过 CSRF 校验。工作簿名称通过[飞书电子表格元数据接口](https://open.feishu.cn/document/server-docs/docs/sheets-v3/spreadsheet/get)读取；名称读取失败时仍可按已校验的链接和工作表选择，不因此中断导入。历史不保存订单文件或登录凭证。

## 回归

`tests/test_procurement_import_background.py` 验证开关、图片/链接、重试及已有行保护；`tests/fixtures/procurement_import_ui.cjs` 执行真实页面函数，覆盖拖拽、历史、自动校验、账号切换和过期响应；云端 `test_procurement_import_preferences.py` 验证鉴权、历史上限与隔离、持久化和工作进程恢复。全部使用合成数据。源 HTML 和解析器修改后分别运行 `sync_web_workspace.py`、`sync_procurement_import_core.py` 同步云端副本。
