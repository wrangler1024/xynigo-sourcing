# -*- coding: utf-8 -*-
"""生成「采购售后（丢件退款）」可交互原型。

做法：**直接以真实工作台页面为底**（src/purchase_tool/web/index.html 逐字复制），
只在主脚本之前注入一层 mock 数据适配器，把 /v1/* 请求换成脚本化的模拟响应。
因此 CSS、DOM 结构、交互逻辑与线上完全同源，不存在风格漂移。

原型数据全部为**合成数据**（环境序号/订单号/买家号都是编造的），不含任何真实
业务信息。

用法：
    python docs/prototypes/build_procurement_after_sale_prototype.py
    # 然后用 HTTP 打开（必须走 HTTP：file:// 下工作台的 api() 会直接拒绝）
    python -m http.server 8899 --directory docs/prototypes
    # 浏览器访问 http://127.0.0.1:8899/20260915-procurement-after-sale-v1.html?runtime=cloud
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'src' / 'purchase_tool' / 'web' / 'index.html'
TARGET = ROOT / 'docs' / 'prototypes' / '20260915-procurement-after-sale-v1.html'

MOCK = r'''
<script id="prototype-mock">
/* ============================================================
 * 采购售后（丢件退款）· 可交互原型 —— 仅替换数据层
 * 本页其余部分（样式 / DOM / 交互）与 src/purchase_tool/web/index.html 完全同源。
 * 页面内所有环境序号、环境名、订单号、买家号均为合成数据，不对应任何真实业务。
 * ============================================================ */
(function () {
  const SYNTHETIC_NOTE = '合成数据';
  const PERMISSIONS = __PERMISSIONS__;

  const SCAN_ROWS = [
    {
      environmentSerial: '5121', storeName: 'ZH-MX-0902-011',
      accountName: 'buyer0011@example.test', orderNo: 'GSH1RV90A001B2',
      deliveredAt: '04 Sep 2026 10:37:20', amount: '108.22', status: 'ok',
      claimable: true, packageCount: 1, trackingNo: 'JMX300959285918',
      errorSummary: null, screenshotSha256: null,
    },
    {
      environmentSerial: '5122', storeName: 'ZH-MX-0902-012',
      accountName: 'buyer0012@example.test', orderNo: 'GSH1RV90A002C7',
      deliveredAt: '03 Sep 2026 18:08:37', amount: '132.62', status: 'ok',
      claimable: true, packageCount: 1, trackingNo: '49411547468070',
      errorSummary: null, screenshotSha256: null,
    },
    {
      environmentSerial: '5123', storeName: 'ZH-MX-0902-013',
      accountName: 'buyer0013@example.test', orderNo: 'GSH1RV90A003D1',
      deliveredAt: '05 Sep 2026 09:12:04', amount: '96.80', status: 'ok',
      claimable: true, packageCount: 2, trackingNo: '49416004492454',
      errorSummary: null, screenshotSha256: null,
    },
    {
      environmentSerial: '5124', storeName: 'ZH-MX-0902-014',
      accountName: 'buyer0014@example.test', orderNo: 'GSH1RV90A004E9',
      deliveredAt: '01 Sep 2026 15:19:09', amount: '120.47', status: 'ok',
      claimable: false, packageCount: 1, trackingNo: '49415946103334',
      errorSummary: null, screenshotSha256: null,
    },
    {
      environmentSerial: '5125', storeName: 'ZH-MX-0902-015',
      accountName: 'buyer0015@example.test', orderNo: '',
      deliveredAt: '', amount: '', status: 'empty', claimable: false,
      packageCount: 0, trackingNo: '',
      errorSummary: '该环境订单列表没有可申请售后的已送达订单',
      screenshotSha256: null,
    },
    {
      environmentSerial: '5126', storeName: 'ZH-MX-0902-016',
      accountName: 'buyer0016@example.test', orderNo: '',
      deliveredAt: '', amount: '', status: 'login', claimable: false,
      packageCount: 0, trackingNo: '',
      errorSummary: '买家端未登录（环境登录态缺失，请先登录该环境）',
      screenshotSha256: null,
    },
  ];

  const CLAIM_SEQUENCE = [
    { status: 'running', rows: [] },
    {
      status: 'running',
      rows: [{
        orderNo: 'GSH1RV90A001B2', environmentSerial: '5121',
        storeName: 'ZH-MX-0902-011', status: 'ok', packageNo: 'C26090200011045',
        refundBillId: '2390765181147136', refundPath: 'Cuenta original de pago',
        durationSeconds: 22, submittedAt: '2026-09-15T10:12:03+00:00',
        note: '', errorSummary: null, screenshotSha256: null,
      }],
    },
    {
      status: 'running',
      rows: [
        {
          orderNo: 'GSH1RV90A001B2', environmentSerial: '5121',
          storeName: 'ZH-MX-0902-011', status: 'ok',
          packageNo: 'C26090200011045', refundBillId: '2390765181147136',
          refundPath: 'Cuenta original de pago', durationSeconds: 22,
          submittedAt: '2026-09-15T10:12:03+00:00', note: '',
          errorSummary: null, screenshotSha256: null,
        },
        {
          orderNo: 'GSH1RV90A002C7', environmentSerial: '5122',
          storeName: 'ZH-MX-0902-012', status: 'ok',
          packageNo: 'C26090200012017', refundBillId: '2390765181147201',
          refundPath: 'Cuenta original de pago', durationSeconds: 19,
          submittedAt: '2026-09-15T10:12:31+00:00', note: '',
          errorSummary: null, screenshotSha256: null,
        },
        {
          orderNo: 'GSH1RV90A003D1', environmentSerial: '5123',
          storeName: 'ZH-MX-0902-013', status: 'running', packageNo: '',
          refundBillId: '', refundPath: '', durationSeconds: null,
          submittedAt: null, note: '', errorSummary: null,
          screenshotSha256: null,
        },
      ],
    },
    {
      status: 'partial_failure',
      rows: [
        {
          orderNo: 'GSH1RV90A001B2', environmentSerial: '5121',
          storeName: 'ZH-MX-0902-011', status: 'ok',
          packageNo: 'C26090200011045', refundBillId: '2390765181147136',
          refundPath: 'Cuenta original de pago', durationSeconds: 22,
          submittedAt: '2026-09-15T10:12:03+00:00', note: '',
          errorSummary: null, screenshotSha256: null,
        },
        {
          orderNo: 'GSH1RV90A002C7', environmentSerial: '5122',
          storeName: 'ZH-MX-0902-012', status: 'ok',
          packageNo: 'C26090200012017', refundBillId: '2390765181147201',
          refundPath: 'Cuenta original de pago', durationSeconds: 19,
          submittedAt: '2026-09-15T10:12:31+00:00', note: '',
          errorSummary: null, screenshotSha256: null,
        },
        {
          orderNo: 'GSH1RV90A003D1', environmentSerial: '5123',
          storeName: 'ZH-MX-0902-013', status: 'blocked', packageNo: '',
          refundBillId: '', refundPath: '', durationSeconds: 6,
          submittedAt: null, note: '',
          errorSummary: '该订单已无可申请售后的包裹（可能已提交过）',
          screenshotSha256: null,
        },
      ],
    },
  ];

  const state = { scanPolls: 0, claimPolls: 0, scanStartedAt: 0, claimStartedAt: 0 };

  // 提交历史（合成）：三行＝三个批次；第二行「重提自」第一批，用来演示关联列
  const CLAIM_HISTORY = [
    {
      runId: 'a1f0c3d2-0001-4000-8000-000000000001', status: 'partial_failure',
      createdAt: '2026-09-16T02:12:31+00:00', actorName: '胡康凯',
      actorUserId: 'proto-user-1',
      executorName: '本机执行器（原型）', environmentCount: 3, totalCount: 3,
      successCount: 2, skippedCount: 1, stoppedCount: 0, failedCount: 0,
      retryFromRunId: '',
    },
    {
      runId: 'a1f0c3d2-0002-4000-8000-000000000002', status: 'completed',
      createdAt: '2026-09-16T02:31:07+00:00', actorName: '胡康凯',
      actorUserId: 'proto-user-1',
      executorName: '本机执行器（原型）', environmentCount: 1, totalCount: 1,
      successCount: 1, skippedCount: 0, stoppedCount: 0, failedCount: 0,
      retryFromRunId: 'a1f0c3d2-0001-4000-8000-000000000001',
    },
    {
      runId: 'a1f0c3d2-0003-4000-8000-000000000003', status: 'partial_failure',
      createdAt: '2026-09-15T09:40:52+00:00', actorName: '熊新刚',
      actorUserId: 'proto-user-2',
      executorName: '同事的 Windows 执行器', environmentCount: 2, totalCount: 4,
      successCount: 2, skippedCount: 1, stoppedCount: 0, failedCount: 1,
      retryFromRunId: '',
    },
  ];
  const CLAIM_HISTORY_ROWS = {
    'a1f0c3d2-0001-4000-8000-000000000001': [
      {
        orderNo: 'GSH1RV90A001B2', environmentSerial: '5121',
        storeName: 'ZH-MX-0902-011', status: 'ok', packageNo: 'C26090200011045',
        refundBillId: '2390765181147136', refundPath: 'Cuenta original de pago',
        refundAccount: 'Tarjeta ****7935', deliveredAt: '04 Sep 2026 10:37:20',
        submittedAt: '2026-09-16T02:12:03+00:00', note: '',
        errorSummary: null, goodsImg: '',
      },
      {
        orderNo: 'GSH1RV90A002C7', environmentSerial: '5122',
        storeName: 'ZH-MX-0902-012', status: 'ok', packageNo: 'C26090200012017',
        refundBillId: '2390765181147201', refundPath: 'Cuenta original de pago',
        refundAccount: 'Tarjeta ****7935', deliveredAt: '03 Sep 2026 18:08:37',
        submittedAt: '2026-09-16T02:12:31+00:00', note: '',
        errorSummary: null, goodsImg: '',
      },
      {
        orderNo: 'GSH1RV90A003D1', environmentSerial: '5123',
        storeName: 'ZH-MX-0902-013', status: 'blocked', packageNo: '',
        refundBillId: '', refundPath: '', refundAccount: '',
        deliveredAt: '05 Sep 2026 09:12:04', submittedAt: null, note: '',
        errorSummary: '该订单已无可申请售后的包裹（可能已提交过）',
        goodsImg: '',
      },
    ],
    'a1f0c3d2-0002-4000-8000-000000000002': [
      {
        orderNo: 'GSH1RV90A003D1', environmentSerial: '5123',
        storeName: 'ZH-MX-0902-013', status: 'ok', packageNo: 'C26090200013008',
        refundBillId: '2390765181147399', refundPath: 'Cuenta original de pago',
        refundAccount: 'Tarjeta ****7935', deliveredAt: '05 Sep 2026 09:12:04',
        submittedAt: '2026-09-16T02:31:07+00:00', note: '',
        errorSummary: null, goodsImg: '',
      },
    ],
    'a1f0c3d2-0003-4000-8000-000000000003': [
      {
        orderNo: 'GSH1RV90A004E9', environmentSerial: '5124',
        storeName: 'ZH-MX-0902-014', status: 'ok', packageNo: 'C26090100014002',
        refundBillId: '2390755181102884', refundPath: 'Cuenta original de pago',
        refundAccount: 'Tarjeta ****4127', deliveredAt: '01 Sep 2026 15:19:09',
        submittedAt: '2026-09-15T09:40:12+00:00', note: '',
        errorSummary: null, goodsImg: '',
      },
      {
        orderNo: 'GSH1RV90A005F3', environmentSerial: '5125',
        storeName: 'ZH-MX-0902-015', status: 'fail', packageNo: '',
        refundBillId: '', refundPath: '', refundAccount: '', deliveredAt: '',
        submittedAt: null, note: '',
        errorSummary: '打开申请页超时（30 秒内未出现包裹列表）',
        goodsImg: '',
      },
    ],
  };

  const json = (body, status = 200) => new Response(
    JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });

  const ok = data => json({ ok: true, data });

  function routeScanStatus() {
    // 扫描：6 行结果分四批出现，让进度条与行状态可见地推进
    state.scanPolls += 1;
    const plan = [0, 2, 4, 6];
    const shown = plan[Math.min(state.scanPolls - 1, plan.length - 1)];
    const done = state.scanPolls >= plan.length;
    return ok({
      taskId: 'proto-scan-1',
      status: done ? 'succeeded' : 'queued',
      executorId: 'proto-exec-1',
      summary: {
        rows: SCAN_ROWS.slice(0, shown),
        totalCount: 6,
        claimableCount: SCAN_ROWS.filter(r => r.claimable).length,
      },
      lastRuns: {},
    });
  }

  function routeClaimStatus() {
    state.claimPolls += 1;
    const idx = Math.min(state.claimPolls, CLAIM_SEQUENCE.length) - 1;
    const step = CLAIM_SEQUENCE[Math.max(0, idx)];
    const total = 3;
    const doneCount = step.rows.filter(
      r => !['queued', 'running'].includes(String(r.status || ''))).length;
    return ok({
      runId: 'proto-run-1',
      status: step.status,
      phase: 'after_sale.' + step.status,
      browserMode: 'visible',
      progressCompleted: doneCount,
      progressTotal: total,
      totalCount: total,
      successCount: step.rows.filter(r => r.status === 'ok').length,
      failedCount: step.rows.filter(
        r => ['fail', 'login', 'inuse'].includes(String(r.status || ''))).length,
      skippedCount: step.rows.filter(
        r => ['blocked', 'skip', 'empty'].includes(String(r.status || ''))).length,
      stoppedCount: 0,
      stopRequested: false,
      rows: step.rows,
    });
  }

  const EXECUTOR = {
    id: '11111111-2222-3333-4444-555555555555',
    displayName: '本机执行器（原型）',
    platform: 'macos',
    architecture: 'arm64',
    clientVersion: '0.17.20',
    lastSeenAt: new Date().toISOString(),
    connectivity: 'online',
    status: 'active',
    capabilities: [
      'after.sale.scan.v1', 'after.sale.claim.v1',
      'store.finance.inspect.v1', 'store.finance.lookup.v1',
      'logistics.query.v1', 'logistics.auto-site.v1',
      'environment.preview-bound.v1', 'environment.cloud-plan.v1',
      'environment.cloud-inventory.v1', 'environment.create-bound.v1',
      'environment.create-backup.v1', 'environment.retry-row.v1',
      'environment.retry-failed.v1', 'workspace.rpc.v1',
      'workspace.snapshot.v1', 'config.summary.v2',
      'local.config.desktop.v1',
    ],
  };

  try {
    localStorage.setItem('xynigo.runtime-executor-id.v1', EXECUTOR.id);
    sessionStorage.setItem('xynigo.runtime-executor-session-id.v1', EXECUTOR.id);
  } catch (e) {}

  function handle(path, method, body) {
    if (path.startsWith('/v1/auth/web/status')) {
      return json({
        authenticated: true,
        identity: {
          user: { id: 'proto-user-1', name: '胡康凯' },
          tenant: { id: 'proto-tenant-1', key: 'demo' },
          roles: ['super_admin'],
          permissions: PERMISSIONS,
          workspaceAccess: true,
        },
      });
    }
    if (path.startsWith('/v1/executors')) {
      return json({ items: [EXECUTOR] });
    }
    if (path.startsWith('/v1/after-sale/scan/')) {
      if (path.endsWith('/cancel')) {
        state.scanPolls = 0;
        return ok({ taskId: 'proto-scan-1', status: 'cancelled',
                    executorId: 'proto-exec-1',
                    summary: { rows: [], totalCount: 0, claimableCount: 0 },
                    lastRuns: {} });
      }
      return routeScanStatus();
    }
    if (path.startsWith('/v1/after-sale/scan')) {
      state.scanPolls = 0;
      return json({ ok: true, data: { taskId: 'proto-scan-1', status: 'queued',
                                      executorId: 'proto-exec-1' } }, 202);
    }
    if (/^\/v1\/operation-runs\/after-sale-claim\/history/.test(path)) {
      // 提交历史：列表 / 详情（合成数据；导出按钮在原型里只回空壳）
      const query = new URLSearchParams(
        path.includes('?') ? path.slice(path.indexOf('?') + 1) : '');
      const detailPath = path.split('?')[0].split('/history/')[1];
      if (detailPath) {
        const runId = detailPath.replace(/\/export$/, '');
        const batch = CLAIM_HISTORY.find(item => item.runId === runId);
        if (!batch) {
          return json({ ok: false, detail: { code: 'after_sale_claim_run_not_found',
                                              message: '售后批次不存在' } }, 404);
        }
        if (detailPath.endsWith('/export')) {
          return json({ ok: true, data: { file: '售后提交结果_原型.xlsx' } });
        }
        return ok({ runId: batch.runId, status: batch.status,
                    totalCount: batch.totalCount,
                    successCount: batch.successCount,
                    skippedCount: batch.skippedCount,
                    stoppedCount: batch.stoppedCount,
                    failedCount: batch.failedCount, stopRequested: false,
                    batch, rows: CLAIM_HISTORY_ROWS[batch.runId] || [] });
      }
      const status = query.get('status') || '';
      const actor = query.get('userId') || '';
      const items = CLAIM_HISTORY.filter(
        item => (!status || item.status === status)
          && (!actor || item.actorUserId === actor));
      return ok({
        items, nextCursor: null, hasMore: false,
        actors: [
          { userId: 'proto-user-1', displayName: '胡康凯', status: 'active' },
          { userId: 'proto-user-2', displayName: '熊新刚', status: 'active' },
        ],
      });
    }
    if (/^\/v1\/operation-runs\/after-sale-claim/.test(path)) {
      if (path.endsWith('/cancel')) {
        return ok({ runId: 'proto-run-1', status: 'cancelled', rows: [],
                    progressTotal: 3, progressCompleted: 0, totalCount: 3,
                    successCount: 0, failedCount: 0, skippedCount: 0,
                    stoppedCount: 0, stopRequested: true });
      }
      if (path.endsWith('/after-sale-claim')) {
        state.claimPolls = 0;
        return ok({ runId: 'proto-run-1', status: 'queued', rows: [],
                    progressTotal: 3, progressCompleted: 0, totalCount: 3,
                    successCount: 0, failedCount: 0, skippedCount: 0,
                    stoppedCount: 0, stopRequested: false });
      }
      return routeClaimStatus();
    }
    // 其余接口一律给空壳，保证外层工作台正常渲染
    return json({ ok: true, data: {}, items: [], total: 0 });
  }

  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const path = url.replace(/^https?:\/\/[^/]+/, '');
    if (!path.startsWith('/v1/') && !path.startsWith('/api/')) {
      return nativeFetch(input, init);
    }
    const method = String((init && init.method) || 'GET').toUpperCase();
    let body = null;
    try { body = init && init.body ? JSON.parse(init.body) : null; } catch (e) {}
    console.log('[prototype-mock]', method, path, body || '');
    return Promise.resolve(handle(path, method, body));
  };

  console.log('%c采购售后原型：数据层已替换为合成数据（' + SYNTHETIC_NOTE + '）',
              'color:#078487;font-weight:600');
})();
</script>

<script id="prototype-autopen">
/* 原型便利：启动完成后自动停在「采购 › 采购售后」 */
(function () {
  let tries = 0;
  const timer = setInterval(function () {
    tries += 1;
    if (document.body && !document.body.classList.contains('auth-locked')) {
      clearInterval(timer);
      // 走真实点击路径：工作台要求页签先在 openFeatureTabs 里才可激活
      const tab = document.querySelector('#secondaryTabs [data-module="aftersale"]');
      if (tab && tab.offsetParent !== null) {
        tab.click();
      } else {
        try { setPrimaryModule('procurement'); } catch (e) {}
        try { openFeatureTab('aftersale'); } catch (e) { console.warn(e); }
      }
      try { if (typeof closeRuntimeDrawer === 'function') closeRuntimeDrawer(); } catch (e) {}
    }
    if (tries > 120) clearInterval(timer);
  }, 150);
})();
</script>
'''


def collect_permissions(html: str) -> list[str]:
    """从 FEATURE_MODULES 抽出全部权限码，保证原型里所有页签都可见。"""
    codes = set(re.findall(r"requiredPermission:\s*'([^']+)'", html))
    for group in re.findall(r"requiredPermissions(?:All|Any):\s*\[([^\]]+)\]", html):
        codes.update(re.findall(r"'([^']+)'", group))
    codes.add('assistant.access')
    return sorted(codes)


def main() -> None:
    html = SOURCE.read_text(encoding='utf-8')
    permissions = collect_permissions(html)
    mock = MOCK.replace('__PERMISSIONS__', repr(permissions).replace("'", '"'))
    # 主脚本之前注入：链接、按钮等原生交互不受影响
    marker = '<script>\nconst $ = id => document.getElementById(id);'
    if marker not in html:
        raise SystemExit('未找到主脚本锚点，需按当前 index.html 结构调整')
    html = html.replace(marker, mock + '\n' + marker, 1)
    html = html.replace(
        '<title>',
        '<title>采购售后（丢件退款）· 可交互原型 · ', 1)
    TARGET.write_text(html, encoding='utf-8')
    print('生成:', TARGET)
    print('大小: %.1f KB' % (TARGET.stat().st_size / 1024))
    print('权限码 %d 个:' % len(permissions), ', '.join(permissions[:6]), '…')


if __name__ == '__main__':
    main()
