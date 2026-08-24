# -*- coding: utf-8 -*-
"""Release contract tests for Xynigo Sourcing v0.9.0."""

from pathlib import Path
import unittest

from purchase_tool import __version__


class ReleaseV090Tests(unittest.TestCase):
    def test_version_and_packaging_are_aligned(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(__version__, '0.9.0')
        pyproject = (root / 'pyproject.toml').read_text(encoding='utf-8')
        self.assertIn('version = "0.9.0"', pyproject)
        script = (root / '组装Windows绿色包.sh').read_text(encoding='utf-8')
        self.assertIn('v${VERSION}.zip', script)
        self.assertIn('Xynigo Sourcing v%s 启动中', script)
        self.assertIn("os.environ.setdefault('XYNIGO_INSTALL_DIR', ROOT)", script)
        self.assertNotIn(
            'from purchase_tool.updater import check_for_updates_at_startup',
            script)
        self.assertTrue((root / 'packaging' / 'windows' /
                         'update-helper.ps1').is_file())
        mac_script = (root / '组装macOS绿色包.sh').read_text(
            encoding='utf-8')
        self.assertIn('Xynigo_Sourcing_macOS_', mac_script)
        mac_entry = (root / 'packaging' / 'macos' / 'entry.py').read_text(
            encoding='utf-8')
        self.assertIn("os.environ.setdefault('XYNIGO_INSTALL_DIR'", mac_entry)
        self.assertNotIn('check_for_updates_at_startup', mac_entry)
        self.assertTrue((root / 'packaging' / 'macos' /
                         'update-helper.sh').is_file())
        self.assertTrue((root / 'src' / 'purchase_tool' /
                         'updater.py').is_file())

    def test_web_bundle_contains_module_three_and_template(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / 'src' / 'purchase_tool' / 'web' / 'index.html').read_text(
            encoding='utf-8')
        self.assertIn('订单物流查询', html)
        self.assertNotIn('物流订单查询', html)
        self.assertIn('买家号建环境', html)
        self.assertIn('/api/envbatch/start', html)
        self.assertIn('/api/envbatch/preflight', html)
        self.assertIn('id="envSiteGroup"', html)
        self.assertNotIn('id="envSiteGroup" disabled', html)
        self.assertIn('async function loadEnvGroups()', html)
        self.assertIn('id="envSite"', html)
        self.assertIn('purchaseTags:{[$(\'envSite\').value]:selected}', html)
        self.assertIn('site:$(\'envSite\').value', html)
        self.assertIn('option.textContent = group', html)
        self.assertNotIn('option.textContent = `墨西哥站 · ${group}`', html)
        # 模块三：模式切换 + 采购员卡片 + 备用·测试环境（2026-08-19 设计定稿）
        self.assertIn('id="envModeBar"', html)
        self.assertIn('data-mode="bound"', html)
        self.assertIn('data-mode="backup"', html)
        self.assertIn('id="envCardParse"', html)
        self.assertIn('id="envAssignBound"', html)
        self.assertIn('id="envAssignBackup"', html)
        self.assertIn('id="envBuyerGrid"', html)
        self.assertIn('id="envBackupBuyerGrid"', html)
        self.assertIn('id="envAssignSum"', html)
        self.assertIn('id="envSpecOut"', html)
        self.assertIn('id="btnEnvEven"', html)
        self.assertIn('id="envBackupCount"', html)
        self.assertIn('name="envBackupType"', html)
        self.assertIn('id="envBackupPattern"', html)
        self.assertIn('id="envBoundExports"', html)
        self.assertIn('id="envBackupExports"', html)
        self.assertIn('id="btnEnvBackupResult"', html)
        self.assertIn('下载飞书直贴 TSV（无表头/含凭证）', html)
        self.assertIn('从第一空行的「站点」列粘贴', html)
        self.assertIn('若飞书列顺序被改动请停止粘贴', html)
        self.assertIn('composeAssignmentSpec()', html)
        self.assertIn('/api/envbatch/backup/preview', html)
        self.assertIn('/api/envbatch/backup/start', html)
        self.assertIn('/api/envbatch/backup/progress', html)
        self.assertIn('/api/envbatch/backup/result', html)
        self.assertIn('function setEnvMode(next)', html)
        self.assertIn('function renderBackupBatch(snap)', html)
        # 分组默认选中站点标准分组 + 日期控件（2026-08-19 Jeff 指示）
        self.assertIn('type="date" id="envDate"', html)
        self.assertIn('SITE_DEFAULT_GROUPS', html)
        self.assertIn("MX: '希音墨西哥采购'", html)
        self.assertIn("US: '美国采购分组'", html)
        self.assertIn('function envPurchaseDate()', html)
        # 采购分组下拉过滤店铺分组（2026-08-20 Jeff 指示）
        self.assertIn('BUYER_GROUP_PATTERN', html)
        self.assertIn('/采购|买家号|Registration/i', html)
        self.assertIn(".filter(name => BUYER_GROUP_PATTERN.test(name));", html)
        self.assertIn('— 暂无采购/买家号环境分组 —', html)
        self.assertNotIn('id="envAssign"', html)
        self.assertNotIn('assignmentTotal', html)
        # ④ 批量进度：限高内部滚动 + 吸顶表头（大批量不拉长页面）
        self.assertIn('id="envTableWrap"', html)
        self.assertIn('#envTableWrap { max-height: 480px; overflow-y: auto; }', html)
        self.assertIn('#envThead th { position: sticky; top: 0;', html)
        self.assertIn('id="querySite"', html)
        self.assertIn('<option value="US">美国站 · us.shein.com</option>', html)
        self.assertIn('JSON.stringify({ serials, site })', html)
        self.assertIn("'Shipped': 'st-enviado'", html)
        self.assertIn("'Paid': 'st-procesando'", html)
        self.assertIn("'Risk verification': 'st-reembolsando'", html)
        self.assertIn('风险订单，待验证', html)
        self.assertIn('风险订单 <b id="cntRisk">0</b>', html)
        self.assertIn("if (r.riskOrder) cnt.risk++;", html)
        self.assertIn("else cnt.ok++;", html)
        self.assertIn("r.state === 'ok' && !r.riskOrder", html)
        self.assertIn("r.state === 'ok' && r.riskOrder", html)
        self.assertIn('id="cfgPurchaseTagMX"', html)
        self.assertIn('id="cfgPurchaseTagUS"', html)
        self.assertIn('id="cfgProxyLink"', html)
        self.assertIn('type="password" id="cfgProxyLink"', html)
        self.assertIn('id="cfgProxyClear"', html)
        self.assertIn('proxyLink, proxyClear', html)
        self.assertIn('可选：自定义链接；留空保持现状', html)
        self.assertIn("cfg.proxySource === 'custom'", html)
        self.assertIn('系统模板固定带表头', html)
        self.assertNotIn('https://proxy.example.test', html)
        self.assertIn('Xynigo Sourcing v0.9.0', html)
        self.assertIn('Xyni, GO!', html)
        self.assertIn('Xynigo 品牌字标', html)
        self.assertIn('小犀与 Xynigo 完整品牌一体图形', html)
        self.assertIn('src="xynigo-logo.png"', html)
        self.assertIn('src="xynigo-logo.png?v=6"', html)
        self.assertNotIn('src="xynigo-mascot-x.png"', html)
        self.assertIn('跨境采购协同系统', html)
        self.assertNotIn('跨境代采协同系统', html)
        self.assertIn('src="xynigo-x.ico?v=3"', html)
        self.assertIn('href="/xynigo-x.png?v=5"', html)
        self.assertIn('href="/favicon.ico?v=5"', html)
        self.assertNotIn('class="logo-x"', html)
        self.assertIn('小犀提示', html)
        self.assertIn('品牌表达', html)
        self.assertIn('持续迭代中', html)
        # 新系统 UI 第一阶段：一级产品层级 + 可扩展二级导航，现有功能入口保持不变。
        for primary in (
                'workbench', 'procurement', 'fulfillment', 'resources',
                'analytics', 'system'):
            self.assertIn('data-primary="%s"' % primary, html)
        for label in (
                '工作台', '采购中心', '履约追踪', '资源中心',
                '数据分析', '系统管理'):
            self.assertIn('<b>%s</b>' % label, html)
        self.assertIn('id="secondaryTabs"', html)
        self.assertIn('data-parent="fulfillment" data-module="query"', html)
        self.assertIn('data-parent="resources" data-module="buyerlib"', html)
        self.assertIn('data-parent="resources" data-module="vendorimport"', html)
        self.assertIn('data-parent="resources" data-module="register"', html)
        self.assertIn('data-parent="resources" data-module="envbatch"', html)
        for label in ('买家号库', '号商入库', '账号注册', '采购环境'):
            self.assertIn('>%s<span class="secondary-status">' % label, html)
        self.assertIn('id="buyerLibraryPanel"', html)
        self.assertIn('id="vendorImportPanel"', html)
        self.assertIn('/api/buyer-library', html)
        self.assertIn('后台无头验证全部新建环境出口 IP', html)
        self.assertIn('不会打开可见的 HubStudio 环境窗口', html)
        self.assertIn('data-parent="system" data-module="settings"', html)
        self.assertIn('id="planningPanel"', html)
        self.assertIn('功能规划中 · 不影响现有模块', html)
        self.assertIn('function setPrimaryModule(primary)', html)
        self.assertIn('function setFeaturePanel(module)', html)
        self.assertIn('id="sidebarToggle"', html)
        self.assertIn('id="btnRetryFail"', html)
        self.assertIn('id="updateNotice"', html)
        self.assertIn('id="updateCheck"', html)
        self.assertIn('/api/update/check', html)
        self.assertIn('/api/update/status', html)
        self.assertIn('/api/update/prompt', html)
        self.assertIn("location.protocol === 'file:'", html)
        self.assertIn('当前页面是本地文件预览', html)
        self.assertIn('无法连接 Xynigo 本地服务', html)
        self.assertIn('cfgHideEnvName', html)
        self.assertIn("activeModule !== 'query'", html)
        self.assertIn('data-module="settings"', html)
        self.assertIn('id="settingsPanel"', html)
        self.assertNotIn('id="btnSettings"', html)
        self.assertNotIn('id="settingsMask"', html)
        self.assertIn('id="cfgLarkAppId"', html)
        self.assertIn('id="cfgLarkAppSecret"', html)
        self.assertIn('id="cfgLarkLedgerUrl"', html)
        self.assertIn('id="cfgLarkAppIdState"', html)
        self.assertIn('id="cfgLarkAppSecretState"', html)
        self.assertIn('id="cfgLarkLedgerUrlState"', html)
        self.assertIn('id="cfgLarkConnectedTarget"', html)
        self.assertIn('id="larkSaveFeedback"', html)
        self.assertIn('function saveLarkConfig()', html)
        self.assertIn("button.textContent = '保存中…'", html)
        self.assertIn("setLarkSaveFeedback('bad', '⚠ 保存失败：'", html)
        self.assertIn('配置已保存，但连接名称获取失败', html)
        self.assertIn('重新验证连接与字段', html)
        self.assertIn('正在获取当前连接名称并只读检查统一台账字段', html)
        self.assertIn('await loadLarkConfigStatus()', html)
        self.assertIn('已安全保存 App Secret', html)
        self.assertIn('台账目标已配置；留空保持不变', html)
        self.assertIn('function renderLarkConnectedTarget(', html)
        self.assertIn('function createLarkTargetLink(text)', html)
        self.assertIn("link.href = '/api/lark/open-target'", html)
        self.assertIn("link.target = '_blank'", html)
        self.assertIn("link.rel = 'noopener noreferrer'", html)
        self.assertIn("link.className = 'config-target-link'", html)
        self.assertIn("event => event.stopPropagation()", html)
        self.assertIn('已支持重新配置', html)
        self.assertIn('/api/lark/template', html)
        self.assertIn('下载买家号统一台账完整模板', html)
        self.assertIn('确认重新配置？', html)
        self.assertNotIn('id="cfgLarkBaseToken"', html)
        self.assertNotIn('id="cfgLarkTableId"', html)
        self.assertIn('包含 table=tbl...', html)
        self.assertIn('body:JSON.stringify({appId, appSecret, ledgerUrl, clearCredential, clearLedgerTarget})', html)
        self.assertIn('/api/lark/config', html)
        self.assertIn('/api/lark/preflight', html)
        self.assertIn('/api/lark/target-metadata', html)
        self.assertIn('refreshPending:true', html)
        self.assertIn('正在自动获取当前连接名称', html)
        self.assertIn('id="envWriteLedger" disabled', html)
        self.assertIn('id="envExecutionState" role="status" aria-live="polite"', html)
        self.assertIn('id="envExecutionTitle"', html)
        self.assertIn('id="envExecutionDetail"', html)
        self.assertIn('let envSubmitting = false', html)
        self.assertIn('function beginEnvSubmission(title, detail)', html)
        self.assertIn('function paintEnvSubmissionState()', html)
        self.assertIn('function finishEnvSubmission()', html)
        self.assertIn('function renderEnvExecutionState(snap, mode)', html)
        self.assertIn("'⏳ 正在提交…'", html)
        self.assertIn('执行指令已确认，正在提交买家号建环境任务', html)
        self.assertIn('执行指令已确认，正在检查飞书台账并提交任务', html)
        self.assertIn('await paintEnvSubmissionState()', html)
        self.assertIn('HubStudio 已完成，正在回写飞书台账', html)
        self.assertIn('const ledgerFailed =', html)
        self.assertIn('${taskName}部分失败：HubStudio', html)
        self.assertIn('任务状态暂时无法获取', html)
        self.assertIn('页面每 1.5 秒自动刷新，请勿重复提交', html)
        self.assertIn('id="envWriteLedgerLabel"', html)
        self.assertIn('id="regWriteLedgerLabel"', html)
        self.assertIn('function larkTargetDisplayName()', html)
        self.assertIn('function renderLarkWriteTargetText()', html)
        self.assertIn('larkTargetConfigured\n      ? createLarkTargetLink(target)', html)
        self.assertIn('larkTargetBaseName = targetBaseName', html)
        self.assertIn('回写${larkTargetDisplayName()}', html)
        self.assertNotIn('回写飞书「买家号（统一）」', html)
        self.assertIn('id="btnEnvRetryLedger" disabled', html)
        self.assertIn('writeLarkLedger, confirmLarkWrite', html)
        self.assertIn('补写本批次飞书台账', html)
        self.assertIn('body:JSON.stringify({confirmLarkWrite:true})', html)
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         '采购工具买家号入库模板.xlsx').is_file())
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         '买家号统一台账模板.xlsx').is_file())
        from openpyxl import load_workbook
        ledger_template = load_workbook(
            root / 'src' / 'purchase_tool' / 'web' /
            '买家号统一台账模板.xlsx', read_only=True)
        self.assertEqual(
            ledger_template.sheetnames,
            ['买家号统一台账', '字段配置说明'])
        self.assertEqual(
            [cell.value for cell in ledger_template['买家号统一台账'][1]],
            ['账号ID', '站点', '邮箱账号', '密码', '接码Key链接', 'Cookie',
             '号商购买单号', '购买日期', '账号状态', '绑定环境', '环境分组名',
             '环境序号', '采购员', '绑定时间', '首次登录日期', '最后使用日期',
             '创建时间', '备注', '累计下单数', '异常记录', '创建人',
             '迁移状态', '操作人'])
        guide_values = [
            cell.value for row in ledger_template['字段配置说明'].iter_rows()
            for cell in row if cell.value]
        self.assertIn('新刚、志恒、康德、宇航、熊、德、恒', guide_values)
        self.assertIn('正常、待复核（源表错列）', guide_values)
        self.assertIn('yyyy-mm-dd hh:mm', guide_values)
        self.assertIn('环境分组名', guide_values)
        guide = ledger_template['字段配置说明']
        self.assertIn('连续13列', guide['A2'].value)
        first_login = next(
            [cell.value for cell in row]
            for row in guide.iter_rows(
                min_row=1, max_row=guide.max_row, min_col=1, max_col=6)
            if row[0].value == '首次登录日期')
        self.assertEqual(first_login[4], '字段预检必需')
        self.assertIn('当前买家号建环境 API 不写入', first_login[5])
        ledger_template.close()
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         'xynigo-logo.png').is_file())
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         'xynigo-mascot-x.png').is_file())
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         'xynigo-x.ico').is_file())
        self.assertTrue((root / 'src' / 'purchase_tool' / 'web' /
                         'xynigo-x.png').is_file())
        ico = (root / 'src' / 'purchase_tool' / 'web' /
               'xynigo-x.ico').read_bytes()
        self.assertEqual(ico[:4], b'\x00\x00\x01\x00')
        self.assertGreaterEqual(int.from_bytes(ico[4:6], 'little'), 7)
        main_py = (root / 'src' / 'purchase_tool' / 'main.py').read_text(
            encoding='utf-8')
        self.assertIn("self._file(X_ICON_ICO, 'image/x-icon')", main_py)
        self.assertNotIn('FAVICON_ICO', main_py)
        export_py = (root / 'src' / 'purchase_tool' /
                     'excel_export.py').read_text(encoding='utf-8')
        self.assertIn('embed_cell_images', export_py)
        self.assertNotIn('TwoCellAnchor', export_py)
        self.assertNotIn('ws.add_image', export_py)
        cell_images_py = (root / 'src' / 'purchase_tool' /
                          'xlsx_cell_images.py').read_text(encoding='utf-8')
        self.assertIn("{'n': '_rvRel:LocalImageIdentifier', 't': 'i'}",
                      cell_images_py)
        self.assertIn("{'n': 'CalcOrigin', 't': 'i'}", cell_images_py)


if __name__ == '__main__':
    unittest.main()
