# -*- coding: utf-8 -*-
"""生成「采购售后 · 多类型三级菜单」设计样稿原型。

**这是设计样稿，不是已实现的功能。** 当前代码里只实现了「丢件退款」一种类型；
本样稿在真实工作台页面上叠加一层设计补丁，用来确认「多类型三级菜单」的交互形态，
催促发货 / 取消订单 一律标注「规划」。

做法与主原型一致：逐字复制 src/purchase_tool/web/index.html，先注入数据 mock
（复用 build_procurement_after_sale_prototype 的那一层），再注入设计补丁——
补丁只做三件事：把①的类型胶囊换成 chip 分段控件、按类型切换两张表的列与说明、
给高危类型演示二次确认弹层。所有元素使用工作台既有 class（.chip/.modal-mask/
.card/.pill 等），不引入新配色。

用法：
    python docs/prototypes/build_procurement_after_sale_multitype_prototype.py
    python -m http.server 8899 --directory docs/prototypes
    # http://127.0.0.1:8899/20260915-procurement-after-sale-v2-multitype.html?runtime=cloud
"""
from __future__ import annotations

import importlib.util
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SOURCE = ROOT / 'src' / 'purchase_tool' / 'web' / 'index.html'
TARGET = HERE / '20260915-procurement-after-sale-v2-multitype.html'


def _load_base_generator():
    spec = importlib.util.spec_from_file_location(
        'as_proto_base', HERE / 'build_procurement_after_sale_prototype.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DESIGN_PATCH = r'''
<script id="prototype-multitype-design">
/* ============================================================
 * 【设计样稿】采购售后 · 多类型三级菜单
 * 只实现「丢件退款」是当前事实；催促发货 / 取消订单 为规划，标签与横幅已标注。
 * 本补丁仅用于确认交互形态，不代表已实现能力。
 * ============================================================ */
(function () {
  const TYPES = [
    {
      thumb: 'preview-product-a.svg',
      id: 'refund', label: '丢件退款', state: 'done',
      // 图标与一级菜单同款：24×24 线性描边（退回箭头）
      svg: '<path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
      desc: '已送达但未收到 → 提交退款申请，原路退回',
      perm: 'procurement.aftersale.refund',
      rule: '已送达但未收到',
      note: '退款原路退回；两步式，提交即写库',
      action: '提交售后',
      confirm: 'none',
      scanCols: ['送达时间', '金额', '可退包裹', '物流号'],
      claimCols: ['退款单号', '退款路径', '退款信用卡'],
      rows: [
        { serial: '5121', store: 'ZH-MX-0902-011', order: 'GSH1RV90A001B2',
          pick: true, status: '可申请',
          extra: { 送达时间: '04 Sep 2026 10:37:20', 金额: '$MXN108.22', 可退包裹: '1', 物流号: 'JMX300959285918' } },
        { serial: '5122', store: 'ZH-MX-0902-012', order: 'GSH1RV90A002C7',
          pick: true, status: '可申请',
          extra: { 送达时间: '03 Sep 2026 18:08:37', 金额: '$MXN132.62', 可退包裹: '1', 物流号: '49411547468070' } },
        { serial: '5123', store: 'ZH-MX-0902-013', order: 'GSH1RV90A003D1',
          pick: false, status: '不可申请',
          extra: { 送达时间: '01 Sep 2026 15:19:09', 金额: '$MXN120.47', 可退包裹: '1', 物流号: '49415946103334' } },
      ],
      claimRows: [
        { order: 'GSH1RV90A001B2', serial: '5121', status: 'ok',
          actedAt: '09-15 18:12:03',
          extra: { 退款单号: '2390765181147136', 退款路径: 'Cuenta original de pago',
                   退款信用卡: '****2281' } },
        { order: 'GSH1RV90A002C7', serial: '5122', status: 'ok',
          actedAt: '09-15 18:12:31',
          extra: { 退款单号: '2390765181147201', 退款路径: 'Cuenta original de pago',
                   退款信用卡: '****2281' } },
      ],
    },
    {
      thumb: 'preview-product-b.svg',
      id: 'urge', label: '催促发货', state: 'plan',
      svg: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
      desc: '已付款未发货 → 批量催促发货（24 小时一次）',
      perm: 'procurement.aftersale.urge',
      rule: '已付款未发货（备货中）',
      note: '每个订单 24 小时内最多催一次，冷却期内不可再催',
      action: '批量催促发货',
      confirm: 'none',
      scanCols: ['下单时间', '金额', '已催次数', '下次可催'],
      claimCols: ['催发时间', '下次可催'],
      rows: [
        { serial: '5131', store: 'ZH-MX-0902-021', order: 'GSH1RV90B001K3',
          pick: true, status: '可催发货',
          extra: { 下单时间: '11 Sep 2026 09:20:11', 金额: '$MXN210.30', 已催次数: '0', 下次可催: '立即' } },
        { serial: '5132', store: 'ZH-MX-0902-022', order: 'GSH1RV90B002L8',
          pick: true, status: '可催发货',
          extra: { 下单时间: '12 Sep 2026 14:02:55', 金额: '$MXN88.00', 已催次数: '1', 下次可催: '13 Sep 2026 14:02' } },
        { serial: '5133', store: 'ZH-MX-0902-023', order: 'GSH1RV90B003M2',
          pick: false, status: '冷却中',
          extra: { 下单时间: '12 Sep 2026 20:31:40', 金额: '$MXN156.90', 已催次数: '2', 下次可催: '13 Sep 2026 20:31' } },
      ],
      claimRows: [
        { order: 'GSH1RV90B001K3', serial: '5131', status: 'ok',
          extra: { 催发时间: '10:12:03', 下次可催: '16 Sep 2026 10:12' } },
        { order: 'GSH1RV90B002L8', serial: '5132', status: 'ok',
          extra: { 催发时间: '10:12:31', 下次可催: '16 Sep 2026 10:12' } },
      ],
    },
    {
      thumb: 'preview-product-c.svg',
      id: 'cancel', label: '取消订单', state: 'plan',
      // 与「采购中心」的单据图标成对：那边是单据+勾，这边是单据+叉
      svg: '<path d="M6 3h12v18H6zM9 7h6"/><path d="m9.5 12.5 5 5m0-5-5 5"/>',
      desc: '未发货可取消 → 批量取消（不可逆，需二次确认）',
      perm: 'procurement.aftersale.cancel',
      rule: '未发货且平台允许取消',
      note: '不可逆：提交后订单直接取消；需二次确认',
      action: '批量取消订单',
      confirm: 'danger',
      scanCols: ['下单时间', '金额', '可否取消'],
      claimCols: ['取消结果', '退款金额'],
      rows: [
        { serial: '5141', store: 'ZH-MX-0902-031', order: 'GSH1RV90C001P7',
          pick: true, status: '可取消',
          extra: { 下单时间: '13 Sep 2026 08:11:02', 金额: '$MXN99.90', 可否取消: '可取消' } },
        { serial: '5142', store: 'ZH-MX-0902-032', order: 'GSH1RV90C002Q4',
          pick: true, status: '可取消',
          extra: { 下单时间: '13 Sep 2026 11:47:19', 金额: '$MXN143.20', 可否取消: '可取消' } },
        { serial: '5143', store: 'ZH-MX-0902-033', order: 'GSH1RV90C003R1',
          pick: false, status: '不可取消',
          extra: { 下单时间: '12 Sep 2026 07:03:58', 金额: '$MXN76.50', 可否取消: '已发货' } },
      ],
      claimRows: [
        { order: 'GSH1RV90C001P7', serial: '5141', status: 'ok',
          extra: { 取消结果: '已取消', 退款金额: '$MXN99.90' } },
        { order: 'GSH1RV90C002Q4', serial: '5142', status: 'ok',
          extra: { 取消结果: '已取消', 退款金额: '$MXN143.20' } },
      ],
    },
  ];

  // ④ 退款跟踪：以退款单号为主键，平台侧的后续处理进度（只读回访获得）
  const TRACK_PHASES = {
    submitted: ['run', '已受理'],
    reviewing: ['run', '审核中'],
    processing: ['run', '处理中'],
    refunded: ['ok', '已退款'],
    rejected: ['bad', '已拒绝'],
    overdue: ['warn', '超期未出结果'],
  };
  const TRACKING = [
    { billId: '2390765181147136', order: 'GSH1RV90A001B2', serial: '5121',
      card: '****2281', phase: 'reviewing', left: '23:12:40',
      result: '—', checkedAt: '10-12 10:00', action: '—' },
    { billId: '2390765181147201', order: 'GSH1RV90A002C7', serial: '5122',
      card: '****2281', phase: 'refunded', left: '—',
      result: '$MXN132.62', checkedAt: '10-12 10:00', action: '—' },
    { billId: '2390765181147250', order: 'GSH1RV90A003D1', serial: '5123',
      card: '—', phase: 'rejected', left: '—',
      result: '—', checkedAt: '10-12 10:00', action: '需人工申诉' },
    { billId: '2390765181147263', order: 'GSH1RV90A004E9', serial: '5124',
      card: '****3317', phase: 'processing', left: '—',
      result: '—', checkedAt: '10-12 10:00', action: '—' },
  ];

  const esc = window.esc || (s => String(s == null ? '' : s)
    .replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])));
  const picked = { refund: new Set(), urge: new Set(), cancel: new Set() };
  let current = 'refund';

  const typeOf = id => TYPES.find(t => t.id === id) || TYPES[0];

  function renderSwitch() {
    const panel = document.getElementById('afterSalePanel');
    if (!panel) return false;
    // 类型选择器照「资源中心 › 环境创建」的卡片式 mode-bar，放在①选择范围上方
    let bar = document.getElementById('asTypeBar');
    if (!bar) {
      bar = document.createElement('div');
      bar.className = 'mode-bar';
      bar.id = 'asTypeBar';
      panel.querySelector('section.card').before(bar);
      const hint = document.createElement('div');
      hint.className = 'resource-state';
      hint.id = 'asTypeHint';
      hint.style.margin = '10px 0 0';
      bar.after(hint);
    }
    // ① 里原有的类型胶囊属于「当前实现」；设计样稿把类型选择上移，故隐藏
    const legacyRow = panel.querySelector('.as-type-row');
    if (legacyRow) legacyRow.style.display = 'none';
    bar.innerHTML = TYPES.map(t =>
      `<button class="mode-tab${t.id === current ? ' active' : ''}" data-as-type="${t.id}" type="button">`
      + '<span class="mode-tab-inner">'
      + `<span class="nav-icon" aria-hidden="true"><svg viewBox="0 0 24 24">${t.svg}</svg></span>`
      + '<span class="nav-text">'
      + `<b>${esc(t.label)}</b>`
      + `<small>${t.state === 'plan' ? '规划 · ' : ''}${esc(t.desc)}</small>`
      + '</span></span></button>').join('');
    bar.querySelectorAll('[data-as-type]').forEach(btn => {
      btn.onclick = () => switchType(btn.dataset.asType);
    });
    return true;
  }

  function setHint() {
    const t = typeOf(current);
    const hint = document.getElementById('asTypeHint');
    if (!hint) return;
    hint.innerHTML = `适用：${esc(t.rule)} · ${esc(t.note)} · 权限码 <b>${esc(t.perm)}</b>`;
  }

  function planBanner() {
    let bar = document.getElementById('asPlanBanner');
    const t = typeOf(current);
    if (t.state !== 'plan') { if (bar) bar.remove(); return; }
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'asPlanBanner';
      bar.className = 'query-phase-banner';
      bar.style.marginBottom = '12px';
      const panel = document.getElementById('afterSalePanel');
      panel.querySelector('#asTypeHint').after(bar);
    }
    bar.innerHTML = '<div class="query-phase-copy" style="padding-left:0">'
      + `<b>「${esc(t.label)}」为规划类型，本次未实现</b>`
      + `<span>本样稿用于确认三级菜单交互；该类型的判定条件、动作与结果字段均为示意数据。</span></div>`;
  }

  function thumbCell(url) {
    const safe = (typeof safeProcurementImageUrl === 'function')
      ? safeProcurementImageUrl(url) : url;
    return '<td><span class="procurement-image-thumb">'
      + (safe ? `<img src="${esc(safe)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : '')
      + '</span></td>';
  }

  function renderScan() {
    const t = typeOf(current);
    const head = document.querySelector('#asScanTable thead tr');
    head.innerHTML = '<th style="width:34px"><input type="checkbox" style="accent-color:var(--rhino-600)"></th>'
      + '<th>环境序号</th><th>买家号环境</th><th>订单号</th>'
      + '<th style="width:46px">商品图</th><th>售后类型</th>'
      + t.scanCols.map(c => `<th${/金额/.test(c) ? ' class="num"' : ''}>${esc(c)}</th>`).join('')
      + '<th>状态</th>';
    const rows = t.rows;
    document.getElementById('asScanRows').innerHTML = rows.map(r => {
      const can = r.pick;
      const checked = picked[t.id].has(r.order);
      const box = can ? `<input type="checkbox" data-as-mt-order="${esc(r.order)}"${checked ? ' checked' : ''} style="accent-color:var(--rhino-600)">` : '';
      const pill = r.status === '可申请' || r.status === '可催发货' || r.status === '可取消'
        ? `<span class="pill ok">${esc(r.status)}</span>`
        : `<span class="pill ${r.status === '不可申请' || r.status === '不可取消' ? 'warn' : 'warn'}">${esc(r.status)}</span>`;
      return `<tr${checked ? ' class="as-picked"' : ''}><td>${box}</td>`
        + `<td class="sub-text">${esc(r.serial)}</td>`
        + `<td style="font-weight:700; color:var(--navy-950)">${esc(r.store)}</td>`
        + `<td class="as-order">${esc(r.order)}</td>`
        + thumbCell(t.thumb)
        + `<td style="font-weight:700; color:#078487; white-space:nowrap">${esc(t.label)}</td>`
        + t.scanCols.map(c => `<td>${esc(r.extra[c] || '—')}</td>`).join('')
        + `<td>${pill}</td></tr>`;
    }).join('');
    document.getElementById('asScanRows').querySelectorAll('[data-as-mt-order]').forEach(box => {
      box.onchange = () => {
        if (box.checked) picked[t.id].add(box.dataset.asMtOrder);
        else picked[t.id].delete(box.dataset.asMtOrder);
        renderScan(); syncFooter();
      };
    });
  }

  function renderClaim() {
    const t = typeOf(current);
    const head = document.querySelector('#asClaimTable thead tr');
    head.innerHTML = '<th>订单号</th><th style="width:46px">商品图</th><th>售后类型</th><th>环境序号</th>'
      + '<th>送达时间</th>'
      + t.claimCols.map(c => `<th>${esc(c)}</th>`).join('')
      + '<th>状态</th><th>操作时间</th><th>备注</th>';
    document.getElementById('asClaimRows').innerHTML = t.claimRows.map(r => {
      // 送达时间取自已扫描的清单（同一批数据，前端按订单号关联，不再要一次接口）
      const scanned = t.rows.find(x => x.order === r.order) || {};
      const delivered = (scanned.extra || {})['送达时间'] || '—';
      return `<tr><td class="as-order">${esc(r.order)}</td>`
      + thumbCell(t.thumb)
      + `<td style="font-weight:700; color:#078487">${esc(t.label)}</td>`
      + `<td class="sub-text">${esc(r.serial)}</td>`
      + `<td class="sub-text">${esc(delivered)}</td>`
      + t.claimCols.map(c => `<td>${esc(r.extra[c] || '—')}</td>`).join('')
      + `<td><span class="pill ok">已完成</span></td>`
      + `<td class="sub-text">${esc(r.actedAt || '—')}</td>`
      + '<td class="hint">—</td></tr>';
    }).join('');
  }

  function syncFooter() {
    const t = typeOf(current);
    const pickable = t.rows.filter(r => r.pick).length;
    const chosen = picked[t.id].size;
    const hint = document.getElementById('asSubmitHint');
    const btn = document.getElementById('asSubmit');
    if (hint) hint.textContent = `可执行 ${pickable} 单 · 已选 ${chosen} 单`;
    if (btn) {
      btn.textContent = t.action;
      // 规划类型不置灰：置灰就看不到二次确认等交互；点下去只会弹样稿提示
      btn.disabled = !chosen;
    }
    const scanBtn = document.getElementById('asScan');
    if (scanBtn) scanBtn.textContent = t.id === 'refund' ? '扫描可申请售后订单' : `扫描${t.label}订单`;
  }

  function switchType(id) {
    current = id;
    renderSwitch(); setHint(); planBanner(); renderScan(); renderClaim(); syncFooter();
    const desc = document.getElementById('asPhaseDescription');
    const title = document.getElementById('asPhaseTitle');
    if (title) title.textContent = '等待发起';
    if (desc) desc.textContent = typeOf(id).state === 'plan'
      ? '该类型为规划，仅演示列与流程差异；右侧已选数量不会真实提交。'
      : '先扫描，再勾选提交。';
    const banner = document.getElementById('asPhaseBanner');
    if (banner) banner.hidden = false;
  }

  // 高危类型：提交前二次确认（用工作台既有 modal 原语）
  function confirmDanger() {
    const t = typeOf(current);
    const chosen = [...picked[t.id]];
    const total = t.rows.filter(r => chosen.includes(r.order))
      .reduce((sum, r) => sum + Number(String((r.extra['金额'] || '0')).replace(/[^\d.]/g, '') || 0), 0);
    const mask = document.createElement('div');
    // 工作台的 .modal-mask 默认 display:none，必须带 .show 才可见
    mask.className = 'modal-mask show';
    mask.innerHTML = `<div class="modal" role="dialog" aria-modal="true" style="max-width:520px">
      <h3 style="margin:0 0 6px">确认批量取消订单？</h3>
      <p class="hint" style="margin:0 0 12px">该动作<b>不可逆</b>：提交后订单直接取消，平台按原路退回款项。</p>
      <div style="border:1px solid var(--line); border-radius:10px; padding:10px 12px; margin-bottom:12px">
        ${chosen.map(o => `<div class="as-order" style="font-size:12px">${esc(o)}</div>`).join('')}
        <div style="margin-top:8px; font-size:12px">合计金额 <b>$MXN${total.toFixed(2)}</b> · 共 <b>${chosen.length}</b> 单</div>
      </div>
      <label class="hint" style="display:block; margin-bottom:6px">输入「取消订单」以确认：</label>
      <input type="search" id="asConfirmWord" style="width:100%; padding:8px 12px; border:1px solid var(--line); border-radius:10px; font-size:12px">
      <div class="actions">
        <button class="btn sm" id="asConfirmCancel">返回</button>
        <button class="btn sm danger" id="asConfirmOk" disabled>确认取消</button>
      </div>
    </div>`;
    document.body.appendChild(mask);
    const word = mask.querySelector('#asConfirmWord');
    const ok = mask.querySelector('#asConfirmOk');
    word.oninput = () => { ok.disabled = word.value.trim() !== '取消订单'; };
    mask.querySelector('#asConfirmCancel').onclick = () => mask.remove();
    ok.onclick = () => { mask.remove(); toast('样稿演示：正式版在这里下发取消任务'); };
  }


  function renderTracking() {
    const body = document.getElementById('asTrackRows');
    const stats = document.getElementById('asTrackStats');
    if (!body || !stats) return;
    body.innerHTML = TRACKING.map(r => {
      const p = TRACK_PHASES[r.phase] || ['warn', r.phase];
      const bad = r.phase === 'rejected' || r.phase === 'overdue';
      return `<tr${bad ? ' class="tr-bad"' : (r.phase === 'refunded' ? '' : ' class="tr-run"')}>`
        + `<td class="as-order" style="font-size:12px">${esc(r.billId)}</td>`
        + `<td class="as-order">${esc(r.order)}</td>`
        + thumbCell(typeOf('refund').thumb)
        + `<td class="sub-text">${esc(r.serial)}</td>`
        + `<td class="sub-text">${esc(r.card)}</td>`
        + `<td><span class="pill ${p[0]}">${esc(p[1])}</span></td>`
        + `<td>${esc(r.left)}</td>`
        + `<td>${esc(r.result)}</td>`
        + `<td class="sub-text">${esc(r.checkedAt)}</td>`
        + `<td class="hint">${esc(r.action)}</td></tr>`;
    }).join('');
    const open = TRACKING.filter(r => ['submitted', 'reviewing', 'processing'].includes(r.phase)).length;
    const done = TRACKING.filter(r => r.phase === 'refunded').length;
    const bad = TRACKING.filter(r => r.phase === 'rejected').length;
    const over = TRACKING.filter(r => r.phase === 'overdue').length;
    const cells = stats.children;
    cells[0].querySelector('b').textContent = TRACKING.length;
    cells[1].querySelector('b').textContent = open;
    cells[2].querySelector('b').textContent = done;
    cells[3].querySelector('b').textContent = bad;
    cells[4].querySelector('b').textContent = over;
  }


  // 运行状态条：把「阶段横幅 + 进度条 + 进度文本 + 停止」并到一条，钉在类型卡下方，
  // 长列表滚动时也看得见（sticky 压在工作台顶栏下面）。元素是「搬」过来的，不重新接线，
  // 真实页面的 asSetPhase/asProgress 仍写同一批节点。
  function installRunStrip() {
    const panel = document.getElementById('afterSalePanel');
    const banner = document.getElementById('asPhaseBanner');
    if (!panel || !banner) return false;
    if (document.getElementById('asRunStrip')) return true;
    const actionRow = panel.querySelector('.action-row');
    const progressWrap = actionRow ? actionRow.querySelector('.progress-wrap') : null;
    const stop = document.getElementById('asStop');
    const strip = document.createElement('div');
    strip.id = 'asRunStrip';
    // 滚动容器是工作台的 <main>（topbar 不在其中），因此紧贴滚动区顶部即可，视觉上正好压在工作台顶栏下方
    strip.style.cssText = 'position:sticky; top:8px; z-index:6;';
    const hint = document.getElementById('asTypeHint');
    (hint || panel.querySelector('#asTypeBar')).after(strip);
    banner.style.marginBottom = '0';
    banner.style.flexWrap = 'wrap';
    strip.appendChild(banner);
    if (progressWrap) {
      progressWrap.style.marginLeft = 'auto';
      progressWrap.style.display = 'flex';
      progressWrap.style.alignItems = 'center';
      progressWrap.style.gap = '10px';
      if (stop) progressWrap.appendChild(stop);
      banner.appendChild(progressWrap);
    }
    return true;
  }

  function installTracking() {
    const panel = document.getElementById('afterSalePanel');
    if (!panel || document.getElementById('asTrackCard')) return !!panel;
    const cards = panel.querySelectorAll('section.card');
    const card = document.createElement('section');
    card.className = 'card table-card';
    card.id = 'asTrackCard';
    card.innerHTML = `<div class="card-title" style="padding:17px 19px 0;">
        <div>④ 退款跟踪 <span class="legend">退款单号为主键 · 只读回访平台侧处理进度 · 每天一次直到终态</span></div>
        <div class="table-actions"><button class="btn sm" id="asTrackRefresh">刷新退款进度</button></div>
      </div>
      <div class="business-summary" id="asTrackStats" style="padding:4px 19px 0;">
        <div class="business-stat"><small>跟踪中</small><b>—</b></div>
        <div class="business-stat"><small>未终态</small><b>—</b></div>
        <div class="business-stat stat-ok"><small>已退款</small><b>—</b></div>
        <div class="business-stat stat-bad"><small>已拒绝</small><b>—</b></div>
        <div class="business-stat"><small>超期</small><b>—</b></div>
      </div>
      <div style="padding: 0 19px;">
        <div class="table-scroll scroll-20">
          <table><thead><tr>
            <th>退款单号</th><th>订单号</th><th style="width:46px">商品图</th><th>环境序号</th><th>退款信用卡</th>
            <th>阶段</th><th>剩余倒计时</th><th>退款结果</th><th>最近检查</th><th>处置</th>
          </tr></thead><tbody id="asTrackRows"></tbody></table>
        </div>
      </div>
      <div style="padding: 12px 19px 17px;">
        <p class="xyp2-help">回访只读：复用提交时的环境，逐个退款单读平台侧阶段（已受理 → 审核中 → 处理中 → 已退款／已拒绝），
        只回访未终态的单，到终态即冻结。退款信用卡为只读回访时取到的原卡掩码，取不到显示 —。
        被拒的行请到买家端看「Historial de negociación」的拒绝理由后再决定是否申诉。</p>
      </div>`;
    cards[cards.length - 1].after(card);
    document.getElementById('asTrackRefresh').onclick = () => {
      // 样稿演示：把「处理中」推进为「已退款」，其余刷新检查时间
      const now = new Date();
      const stamp = `${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')} `
        + `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
      TRACKING.forEach(r => { r.checkedAt = stamp; });
      const next = TRACKING.find(r => r.phase === 'processing');
      if (next) { next.phase = 'refunded'; next.result = '$MXN96.80'; next.left = '—'; }
      renderTracking();
      toast('样稿演示：已只读回访 ' + TRACKING.length + ' 个退款单');
    };
    renderTracking();
    return true;
  }

  function install() {
    if (!renderSwitch()) return false;
    const submit = document.getElementById('asSubmit');
    if (submit) {
      // 覆盖真实提交：样稿不做任何真实调用
      submit.onclick = () => {
        const t = typeOf(current);
        // 高危类型先走二次确认（即便它还是规划类型，也要能演示确认强度）
        if (t.confirm === 'danger') { confirmDanger(); return; }
        if (t.state === 'plan') { toast(`样稿演示：「${t.label}」为规划类型，正式版在这里下发任务`); return; }
        toast('样稿演示：正式版在这里下发任务并轮询结果');
      };
    }
    installRunStrip();
    installTracking();
    switchType(current);
    return true;
  }

  let tries = 0;
  const timer = setInterval(function () {
    tries += 1;
    if (install() || tries > 60) clearInterval(timer);
  }, 250);
})();
</script>
'''


def main() -> None:
    base = _load_base_generator()
    html = SOURCE.read_text(encoding='utf-8')
    permissions = base.collect_permissions(html)
    mock = base.MOCK.replace('__PERMISSIONS__', repr(permissions).replace("'", '"'))
    marker = '<script>\nconst $ = id => document.getElementById(id);'
    if marker not in html:
        raise SystemExit('未找到主脚本锚点')
    html = html.replace(marker, mock + '\n' + marker, 1)
    html = html.replace('</body>', DESIGN_PATCH + '</body>', 1)
    html = html.replace('<title>', '<title>采购售后 · 多类型三级菜单 · 设计样稿 · ', 1)
    TARGET.write_text(html, encoding='utf-8')
    print('生成:', TARGET)
    print('大小: %.1f KB' % (TARGET.stat().st_size / 1024))


if __name__ == '__main__':
    main()
