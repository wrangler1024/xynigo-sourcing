# 评审请求 · 本机配置向所有登录成员放开

> 本文件是给评审方（Cursor）的提示词，可整段复制。

---

请评审一个新改动：**桌面客户端「本机配置」权限放开（运行参数 + 物流查询高级设置 + HubStudio Key）**。

**只读评审，不要改代码、不要提交。** 输出：结论（可合并 / 改后合并 / 不可合并）+ 必须改项（带文件:行）+ 可以合并后补项。

## 评审对象（本地直评）

- 分支 `codex/local-config-open-members`
- 代码提交 `d9b9049`、交接文档 `2708c28`（其后还有本评审请求的文档提交，可只看内容差异）；基线 `origin/main` `ab4c65a`
- worktree：`~/Documents/xynigo-worktrees/local-config-open`（**本任务专用，当前无其他写入者**）
- **本地直评：不要 fetch、不要新建 worktree，直接在该 worktree 评审**；评审期间只读，不切分支、不提交。

## 背景

志恒、康德在云端工作台是管理员，但桌面客户端「本机配置」显示“普通采购员仅可查看设备级设置”，运行参数与物流查询高级设置只读。诊断结论是三重闸门叠加加一处误导文案：

1. 桌面 `canConfigure()` 要求 `super_admin` 且持有 `system.integration.manage`（该权限在云端属超管专属 `SUPER_ADMIN_ONLY_PERMISSIONS`，管理员结构性拿不到）；
2. 执行器 POST `/api/config` 内联 `require('system.integration.manage', role='super_admin')`；
3. 执行器 `/api/hub-api-key` 经 `AUTH_PERMISSION_BY_PATH` 表驱动命中超管专属权限；
4. 锁定态徽章/提示条硬编码“采购员”，管理员看到后误以为角色未生效。

Jeff 20260917 拍板方案 B 并扩大范围：**本机运行参数不设角色门槛，所有登录成员可配置**（设置仅存本机、只影响本机执行器行为）。

## 改动清单（+60/−15，另加交接文档）

| 文件 | 内容 |
|---|---|
| `src/purchase_tool/main.py` | POST `/api/config` 改为仅登录校验；`AUTH_PERMISSION_BY_PATH` 移除 `/api/hub-api-key` 条目，同样落到仅登录校验（`_require_auth` 对不在表中的路径返回 `require(None)`）；`hub-core-repair` 保持超管专属不变 |
| `src/purchase_tool/web/desktop.js`（权威源） | `canConfigure()` 由 `roleInfo().superAdmin && hasPermission(...)` 改为 `Boolean(state.identity)`；“采购员 · 只读”徽章与“普通采购员仅可查看设备级设置”提示条随之不再渲染 |
| `tests/test_cloud_auth.py` | 原 `test_regular_member_cannot_write_device_runtime_config` 反转为 `test_regular_member_device_config_only_requires_login` |
| `tests/test_env_config.py` | 新增 `_switch_auth_to_member` 辅助 + `test_member_can_save_device_runtime_config`、`test_member_can_save_hub_api_key` 两条成员真实保存成功用例 |
| `docs/20260917_本机配置全员可编辑权限放开.md` | 交接文档（背景/决策/边界/验证） |

## 请重点看

1. **闸门清理完整性（别只看我的 diff）**：请独立在全仓搜 `require(`、`AUTH_PERMISSION_BY_PATH`、`AUTH_PERMISSION_BY_PREFIX`、`SUPER_ADMIN_ONLY_PERMISSIONS`，确认没有第四条能写这些设置的路径；并判断「表里没有该路径 = 仅登录」这个隐式 fallback 是否足够稳（未来有人往表里加回条目就会静默重新上锁）。
2. **保留边界是否自洽**：HubStudio 内核修复（`hub-core-repair`）仍超管专属、云端远程下发执行器的 `executor.config.write` 仍管理员限、桌面「飞书企业应用连接」卡仍超管可见——这三处与“本机运行参数全员”的边界是否合理？有没有“同一张卡里一半解锁一半锁着”的观感矛盾？给出判断。
3. **locked 死代码**：`canConfigure()` 现在只在未登录时为 false（设置页仅在已登录后渲染），即 `locked` 恒 false，403/404 行的两段锁定文案永不渲染。保留锁定机制（未来可回收）还是删除？我们倾向保留以减少 diff，请给意见。
4. **测试有效性**：两条新用例能否在旧代码上失败（真回归价值）？`test_cloud_auth` 那条用 `try urlopen / except HTTPError 非 401、403` 的写法是否足够严格（会不会把将来的 500 型回归放过）？既有行为（`config_revision_conflict`、后台任务运行中拒改）是否仍被覆盖。
5. **安全面**：放开到“所有登录成员”后，同一台电脑多个成员共用时互相覆盖本机配置（单一 `config.json`）是否可接受？既有 `config_revision_conflict` 乐观锁是否已覆盖并发场景？需要 UI/文档提示吗？
6. **生成物与同步**：我改了 `desktop.js` 但**没有跑** `sync_web_workspace.py`，依据是该脚本 `FILES` 清单只含 `index.html` 与图片、不含 `desktop.js`，且 `desktop.js` 全仓仅一份、运行时由执行器直读。请独立核对这一判断；若清单实际含它则本次是缺陷。

## 已知取舍（非缺陷，供判断）

- 只放开“本机设备级设置”；组织级动作（发布本机配置到组织、云端远程下发）权限不变。
- 未部署、未发版、未真机验收：桌面端需随执行器新版本发布才能在志恒/康德机器上生效。
- 未删除 `locked` 死代码（见重点 3）。

## 怎么跑

```bash
cd ~/Documents/xynigo-worktrees/local-config-open

# 核对候选 SHA（期望：2708c28 / d9b9049 / ab4c65a）
git log --oneline -3
git diff origin/main...HEAD --stat

# 全量回归（期望 1257 passed, 5 skipped）
PYTHONPATH=$PWD/src ~/Documents/xynigo-sourcing/.venv/bin/python -m pytest tests -q

# 受影响专项（期望 72 passed）
PYTHONPATH=$PWD/src ~/Documents/xynigo-sourcing/.venv/bin/python -m pytest tests/test_cloud_auth.py tests/test_env_config.py -q
```

注意：借用的 venv 里 `xynigo_sourcing` 是 editable 安装、指向**主检出** `~/Documents/xynigo-sourcing/src`，所以 `PYTHONPATH=$PWD/src` 必须加，否则跑的是别的源码（本单开发时就踩过这个坑）。
