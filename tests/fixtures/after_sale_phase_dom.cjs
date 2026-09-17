// Execute the real DOM reader against synthetic element states, never a buyer page.
const fs=require('node:fs'),vm=require('node:vm');
const {js,titles}=JSON.parse(fs.readFileSync(0,'utf8'));
const results=[];
for(const mode of ['enabled','disabled','aria-ancestor','fieldset','hidden','no-control']) {
  const button={tag:'button',innerText:'Subir comprobante',disabled:mode==='disabled',
    getClientRects:()=>mode==='hidden'?[]:[{}],
    matches:selector=>selector===':disabled' && mode==='fieldset',
    closest:selector=>mode==='aria-ancestor'?{}:null,
    getAttribute:()=>null};
  const wrapper={...button,tag:'div',disabled:false,matches:()=>false,closest:()=>null,
    getClientRects:()=>[{}]};
  const steps=titles.map((textContent,i)=>({
    querySelector:selector=>selector.endsWith('__title')?{textContent,
      classList:{contains:c=>c===(i===0?'is-finish':i===1?'is-active':'is-wait')},
      getClientRects:()=>[{}]}:{innerText:i===1?'Revisión fallida\nReembolso rechazado: Comprobantes insuficientes\nSubir comprobante\nHistorial de negociación':'',getClientRects:()=>[{}]},
    querySelectorAll:selector=>i!==1?[]:[...(mode==='no-control'?[]:[button]),
      ...(selector.includes('[class*=btn]')?[wrapper]:[])]
  }));
  const context={location:{pathname:'/orders/refundLabel/ORDER'},document:{
    body:{innerText:'Código del Reembolso: 12345'},
    querySelectorAll:()=>[{getClientRects:()=>[{}],querySelectorAll:()=>steps}]
  }};
  results.push(vm.runInNewContext(js,context));
}
console.log(JSON.stringify(results));
