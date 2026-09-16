"""扫描只读核验：坏响应不能伪装成 blocked，迟到响应不能串到下一单。"""
import json
import shutil
import subprocess
import unittest
from unittest.mock import Mock

from purchase_tool import after_sale_claim as module


def response():
    return {'httpStatus': 200, 'code': '0', 'reasonId': 83,
            'eligible': [{'packageNo': 'SYNTHPKG', 'shippingNo': 'SYNTHSHIP',
                          'title': 'Paquete 1', 'goodsImg': ''}], 'blocked': []}


class ScanPreInfoValidationTests(unittest.TestCase):
    def test_accepts_only_confirmed_lost_package_eligibility(self):
        self.assertEqual(module.validate_scan_pre_info(response()), response())
        value = response()
        value.update(code=0, reasonId='83', eligible=[], blocked=['SYNTHPKG'])
        self.assertEqual(module.validate_scan_pre_info(value)['eligible'], [])

    def test_http_business_reason_and_shape_errors_are_not_empty_eligibility(self):
        cases = [None, {}, {'httpStatus': 403}, {'httpStatus': 429},
                 {'code': '10001'}, {'code': False}, {'reasonId': 70},
                 {'reasonId': None}, {'eligible': None}, {'eligible': {}},
                 {'blocked': None}, {'eligible': [None]},
                 {'eligible': [{'packageNo': ''}]},
                 {'eligible': [{'packageNo': 123}]}, {'blocked': [None]},
                 {'blocked': ['SYNTHPKG']},
                 {'eligible': [{'packageNo': 'SYNTHPKG'}, {'packageNo': 'SYNTHPKG'}]}]
        for patch in cases:
            with self.subTest(patch=patch):
                value = {**response(), **patch} if patch else patch
                with self.assertRaises(RuntimeError):
                    module.validate_scan_pre_info(value)

    def test_scan_requests_clean_up_on_success_error_and_timeout(self):
        for ready, result in [(True, {'error': '', 'response': response()}),
                              (True, {'error': 'request_failed'}),
                              (True, None), (False, None)]:
            with self.subTest(ready=ready, result=result):
                page = Mock()
                page.wait_for.return_value = ready
                page.js_evaluate.side_effect = [True, result, None] if ready else [True, None]
                scanner = module.AfterSaleClaimer(None)
                if ready and result and not result.get('error'):
                    self.assertEqual(scanner._scan_pre_info(page, 'SYNTH001'), response())
                else:
                    with self.assertRaises(RuntimeError):
                        scanner._scan_pre_info(page, 'SYNTH001')
                expressions = [call.args[0] for call in page.js_evaluate.call_args_list]
                self.assertIn('refund_only/pre_info', expressions[0])
                self.assertNotIn('refund_only/create', expressions[0])
                self.assertIn('controller.abort()', expressions[-1])
                self.assertIn('delete window.__xyScanPre', expressions[-1])

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for async request tests')
    def test_late_response_is_isolated_and_wrong_origin_never_fetches(self):
        first = module._JS_SCAN_PRE_INFO % json.dumps(
            {'id': 'first', 'origin': module.ORIGIN, 'orderNo': 'SYNTH001'})
        second = module._JS_SCAN_PRE_INFO % json.dumps(
            {'id': 'second', 'origin': module.ORIGIN, 'orderNo': 'SYNTH002'})
        js = '''
const assert=require('node:assert/strict');
global.window={}; global.location={origin:%s};
const pending=[];
global.fetch=(url, options)=>new Promise(resolve=>pending.push({url,options,resolve}));
const body={code:'0',info:{reason_module:{reason_id:83},package_module:{
  package_list:[{package_no:'SYNTHPKG',shipping_no:'SYNTHSHIP',item_list:[]}],
  disable_package_list:[]}}};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  %s;
  const old=window.__xyScanPre;
  %s;
  assert.equal(window.__xyScanPre.id,'second');
  assert.deepEqual(pending.map(x=>JSON.parse(x.options.body).billno),['SYNTH001','SYNTH002']);
  assert(pending.every(x=>x.url.includes('/refund_only/pre_info?')));
  pending[0].resolve({status:200,json:async()=>body}); await tick();
  assert.equal(old.done,true);
  assert.equal(window.__xyScanPre.done,false);
  pending[1].resolve({status:200,json:async()=>body}); await tick();
  assert.equal(window.__xyScanPre.done,true);
  assert.equal(window.__xyScanPre.response.eligible[0].packageNo,'SYNTHPKG');
  assert.equal(window.__xyScanPre.response.reasonId,83);
  location.origin='https://unrelated.example';
  %s;
  assert.equal(pending.length,2);
  assert.equal(window.__xyScanPre.error,'page_changed');
})().catch(e=>{console.error(e);process.exitCode=1});
''' % (json.dumps(module.ORIGIN), first, second, first)
        subprocess.run(['node', '-e', js], check=True, capture_output=True, text=True)
