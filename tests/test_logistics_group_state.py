"""Run the actual group loader with a minimal DOM and synthetic responses."""
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_group_refresh_keeps_selection_and_never_touches_query_parameters():
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = fs.readFileSync('src/purchase_tool/web/index.html', 'utf8');
assert(!html.includes('querySite'));
assert(!html.includes('inferQuerySite'));
const group = {
  value: '美国采购测试组', options: [],
  set innerHTML(value) { this.options = []; this.value = ''; },
  appendChild(option) { this.options.push(option); if (this.options.length === 1) this.value = option.value; },
  insertBefore(option) { this.options.unshift(option); },
};
const nodes = {groupSelect: group, btnGroupAll: {disabled: false}};
const $ = id => { assert(id in nodes, `unexpected query-state write: ${id}`); return nodes[id]; };
const document = {createElement: () => ({})};
const CLOUD_WEB_MODE = false;
const BUYER_GROUP_PATTERN = /采购|买家号|Registration/i;
const toast = () => {};
let response = async () => ({groups:['墨西哥买家号注册', '美国采购测试组']});
const api = () => response();
eval(html.slice(html.indexOf('async function loadGroupsOnce()'), html.indexOf("$('groupSelect').onchange")));
(async () => {
  await loadGroupsOnce();
  assert.equal(group.value, '美国采购测试组');
  assert.equal(nodes.btnGroupAll.disabled, false);
  // Selecting a different group while an asynchronous refresh is pending wins.
  let finish;
  response = () => new Promise(resolve => finish = resolve);
  const pending = loadGroupsOnce();
  group.value = '墨西哥买家号注册';
  finish({groups:['美国采购测试组', '墨西哥买家号注册']});
  await pending;
  assert.equal(group.value, '墨西哥买家号注册');
  response = async () => { throw Error('offline'); };
  await loadGroupsOnce();
  assert.equal(group.value, '墨西哥买家号注册');
  response = async () => ({groups:['美国采购测试组']});
  await loadGroupsOnce();
  assert.equal(group.value, '');
  assert.equal(nodes.btnGroupAll.disabled, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run(['node', '-e', script], cwd=ROOT, check=True, capture_output=True, text=True)
