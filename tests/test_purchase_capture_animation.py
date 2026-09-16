"""Execute the real capture scripts with deliberately interleaved animations."""
import json
import shutil
import subprocess

import pytest
from purchase_tool.purchase_details import HIDE_PANEL, RESTORE_PANEL


def test_capture_owner_and_late_animation_cannot_validate_or_hide_old_capture():
    node=shutil.which('node')
    if not node:pytest.skip('Node is required for capture script regression')
    script = 'const hide='+json.dumps(HIDE_PANEL)+';const restore='+json.dumps(RESTORE_PANEL)+';'+r'''
const assert=require('node:assert/strict');
let reduced=true;
const values={visibility:'visible'};
const animations=[];
const element={style:{getPropertyValue:k=>values[k]||'',getPropertyPriority:()=>'',
 setProperty:(k,v)=>{values[k]=v;},removeProperty:k=>{delete values[k];}},
 animate:()=>{let resolve;const finished=new Promise(r=>{resolve=r;});
 const a={finished,resolve,cancel:()=>{}};animations.push(a);return a;}};
global.window=global;
global.document={querySelector:()=>element};
global.matchMedia=()=>({matches:reduced});
global.getComputedStyle=()=>({opacity:'1'});
const H=t=>eval(hide.replaceAll('__CAPTURE_TOKEN__',t));
const R=t=>eval(restore.replaceAll('__CAPTURE_TOKEN__',t));
(async()=>{
 await H('A');await H('B');
 assert.equal(R('B'),false);
 assert.equal(R('A'),true);
 assert.equal(values.visibility,'visible');
 reduced=false;
 const old=H('C');const oldAnimation=animations.at(-1);
 const current=H('D');const currentAnimation=animations.at(-1);
 currentAnimation.resolve();await current;
 assert.equal(R('D'),false);
 oldAnimation.resolve();await old;
 assert.equal(values.visibility,'visible');
 assert.equal(R('C'),true);
 assert.equal(R('D'),true); // even the same token cannot accept a second capture
 console.log('capture ownership preserved');
})().catch(error=>{console.error(error);process.exit(1);});
'''
    result=subprocess.run([node,'-e',script],capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'capture ownership preserved' in result.stdout
