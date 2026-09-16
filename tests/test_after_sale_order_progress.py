"""顺序、回访进度和扫描手动折叠回归：只用替身，不操作真实订单。"""
from pathlib import Path
import shutil
import subprocess
import threading
import unittest

from purchase_tool.after_sale_claim import AfterSaleClaimer


class ExecutionOrderTests(unittest.TestCase):
    def test_claim_and_track_keep_display_order_with_grouped_execution(self):
        items = [dict(environmentSerial=env, orderNo=order, refundBillId='B'+order)
                 for env, order in [('A', 'Z'), ('A', 'Q'), ('B', 'M'), ('A', 'C')]]
        for mode in ['claim', 'track']:
            with self.subTest(mode=mode):
                claimer = AfterSaleClaimer(None)
                claimer._env_index = lambda _: {}
                groups = []
                def visit(serial, env, group, headless):
                    groups.append((serial, [r['orderNo'] for r in group]))
                    for row in group:
                        if mode == 'claim':
                            claimer._publish_claim(row['orderNo'], {'status': 'ok'})
                        else:
                            claimer._publish_track(row['refundBillId'], {'status': 'ok'})
                if mode == 'claim':
                    claimer._claim_env_guarded = visit
                    claimer._run_claim(items, False)
                    rows = claimer.snapshot()['claimRows']
                else:
                    claimer._track_env = visit
                    claimer._run_track(items, False)
                    rows = claimer.snapshot()['trackRows']
                self.assertCountEqual(groups, [('A', ['Z', 'Q', 'C']), ('B', ['M'])])
                self.assertEqual([r['orderNo'] for r in rows], ['Z', 'Q', 'M', 'C'])


    def test_completion_can_differ_while_rows_keep_input_order(self):
        items = [dict(environmentSerial=env, orderNo=env, refundBillId='B'+env)
                 for env in ['A', 'B']]
        for mode in ['claim', 'track']:
            with self.subTest(mode=mode):
                claimer = AfterSaleClaimer(None)
                claimer._env_index = lambda _: {}
                first_started = threading.Event()
                second_done = threading.Event()
                completed = []
                def visit(serial, env, group, headless):
                    if serial == 'A':
                        first_started.set()
                        self.assertTrue(second_done.wait(2))
                    else:
                        self.assertTrue(first_started.wait(2))
                    row = group[0]
                    if mode == 'claim':
                        claimer._publish_claim(row['orderNo'], {'status': 'ok'})
                    else:
                        claimer._publish_track(row['refundBillId'], {'status': 'ok'})
                    completed.append(serial)
                    if serial == 'B':
                        second_done.set()
                setattr(claimer, '_' + mode + '_env_guarded', visit)
                getattr(claimer, '_run_' + mode)(items, True)
                self.assertEqual(completed, ['B', 'A'])
                self.assertEqual([r['orderNo'] for r in
                                  claimer.snapshot()[mode + 'Rows']], ['A', 'B'])


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for Web behavior tests')
class WebOrderProgressTests(unittest.TestCase):
    def test_actual_web_functions_keep_order_progress_and_scan_expansion(self):
        html = (Path(__file__).resolve().parents[1] / 'src/purchase_tool/web/index.html').read_text(encoding='utf-8')
        functions = []
        for name in ['asOrderIdentity', 'asSubmissionForRow','asSubmissionProtections','asSubmissionHasEvidence','asOrderRefundRows', 'asSubmissionBlocksSelection', 'asCanSelectScanRow',
                     'asItemCount', 'asFilteredRows', 'asSelectedItems', 'asOrderedRows', 'asProgress', 'asStripToggle',
                     'asSetPhase', 'asRenderTrackRows', 'asPoll']:
            signature = ('async function ' if name == 'asPoll' else 'function ') + name + '('
            start = html.index(signature)
            functions.append(html[start:html.index('\n}', start)+2])
        setup = r'''
const assert=require('node:assert/strict');
const nodes=new Map();
const $=id=>{
  if (!nodes.has(id)) nodes.set(id,{hidden:false,disabled:false,innerHTML:'',textContent:'',style:{},
    classList:{collapsed:false,toggle(name,value){this.collapsed=value;}}});
  return nodes.get(id);
};
const timers=[];
const setTimeout=cb=>{timers.push(cb);return timers.length;};
const clearTimeout=()=>{};
const asStripTerminal=title=>/完成|失败|已停止|不可用/.test(title);
let asStripCollapseTimer=null;
const AS_STATE={mode:'',rows:[],selected:new Set(),claimItems:[],trackItems:[],polling:false};
const asEtaText=()=>'';
const esc=v=>String(v);
const asThumbCell=()=>'<td></td>';
const asTimeline=()=>'<span>timeline</span>';
const AS_TL_LABEL={reviewing:'审核中'};
const asRenderScanRows=rows=>{AS_STATE.rows=rows;};
const asRenderClaimRows=rows=>{AS_STATE.claimRows=rows;};
const asSupplementClaimAccounts=()=>{};
const asSyncRetryButton=()=>{};
const asSyncRuntimeControls=()=>{};
let response,fetches=0;
const cloudFetchJson=async()=>{fetches++;return {data:response};};
'''
        checks = r'''
(async()=>{
  AS_STATE.rows=['Z','A','M'].map(orderNo=>({orderNo,environmentSerial:'ENV',status:'ok',claimable:true}));
  AS_STATE.selected=new Set(['M','Z']);
  assert.deepEqual(asSelectedItems().map(r=>r.orderNo),['Z','M']);
  const ordered=asOrderedRows([{orderNo:'Z'},{orderNo:'A'}],[{orderNo:'A',status:'ok'}],'orderNo');
  assert.deepEqual(ordered.map(r=>r.orderNo),['Z','A']);
  assert.deepEqual(ordered.map(r=>r.status),['queued','ok']);

  AS_STATE.mode='scan';asSetPhase('扫描运行中','',true);
  assert.equal($('asRunStrip').classList.collapsed,false);
  asSetPhase('扫描完成','',false);
  assert.equal(timers.length,0);
  assert.equal($('asRunStrip').classList.collapsed,false);
  asStripToggle(true);asSetPhase('扫描完成','',false);
  assert.equal($('asRunStrip').classList.collapsed,true);
  asStripToggle(false);
  AS_STATE.scanTaskId='SCAN';AS_STATE.running=true;
  response={status:'succeeded',summary:{totalCount:1,rows:[
    {environmentSerial:'ENV',orderNo:'Z',status:'ok'},
    {environmentSerial:'ENV',orderNo:'A',status:'ok'}]}};
  await asPoll();
  assert.match($('asProgressText').textContent,/1 \/ 1 个环境/);
  assert.equal($('asBar').style.width,'100%');
  assert.equal(timers.length,0);
  assert.equal(AS_STATE.mode,'');
  const count=fetches;await asPoll();assert.equal(fetches,count);

  AS_STATE.mode='track';AS_STATE.trackTaskId='TRACK';AS_STATE.running=true;
  AS_STATE.trackItems=['Z','A','M'].map(refundBillId=>({refundBillId}));
  response={status:'running',summary:{progressCompleted:0,progressTotal:3,rows:[
    {refundBillId:'M',status:'queued'}, {refundBillId:'Z',status:'running'},
    {refundBillId:'A',status:'queued'}]}};
  await asPoll();
  assert.equal($('asBar').style.width,'0%');
  assert.deepEqual(AS_STATE.trackRows.map(r=>r.refundBillId),['Z','A','M']);
  assert.match($('asTrackRows').innerHTML,/等待/);
  assert.match($('asTrackRows').innerHTML,/回访中/);
  response.summary.progressCompleted=1;
  response.summary.rows[1]={refundBillId:'Z',status:'ok',phase:'reviewing'};
  await asPoll();assert.equal($('asBar').style.width,'33%');
  response.status='cancelled';response.summary.stoppedCount=2;
  await asPoll();
  assert.match($('asPhaseTitle').textContent,/已停止/);
  assert.match($('asPhaseDescription').textContent,/1 \/ 3/);
  assert.equal($('asBar').style.width,'33%');

  AS_STATE.mode='track';AS_STATE.trackTaskId='SECOND';AS_STATE.running=true;
  response={status:'running',summary:{rows:[{refundBillId:'Z',status:'ok'}]}};
  await asPoll();assert.equal($('asBar').style.width,'0%');
  AS_STATE.polling=true;const before=fetches;await asPoll();assert.equal(fetches,before);
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', setup+'\n'.join(functions)+checks],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
