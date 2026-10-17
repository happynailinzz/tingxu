// Pure rendering tests: no browser, network, or external packages.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const source=readFileSync(require('node:path').join(__dirname,'../webui/app.js'),'utf8');
const esc=source.slice(source.indexOf('const esc ='),source.indexOf('\nconst state ='));
const render=source.slice(source.indexOf('function stripFront('),source.indexOf('\nasync function openRecording'));
const sandbox={};vm.createContext(sandbox);vm.runInContext(esc+'\n'+render+'\nthis.renderMarkdown=markdown;',sandbox);
test('transcript and summary markup remains escaped',()=>{
 const result=sandbox.renderMarkdown('---\napi_key: hidden\n---\n### 纪要\n\n- **行动**：复核\n[12:00:01] <img src=x onerror=alert(1)>');
 assert.match(result,/<h3>纪要<\/h3>/);
 assert.match(result,/<strong>行动<\/strong>/);
 assert.match(result,/class="timestamp"/);
 assert.ok(!result.includes('api_key'));
 assert.ok(!result.includes('<img'));
 assert.match(result,/&lt;img/);
});
test('code and quotes do not execute HTML',()=>{
 const result=sandbox.renderMarkdown('> 引用\n```\n<script>alert(1)</script>\n```');
 assert.match(result,/<blockquote>引用<\/blockquote>/);
 assert.match(result,/<pre><code>/);
 assert.ok(!result.includes('<script>'));
});

const bluetoothContext={};vm.createContext(bluetoothContext);
vm.runInContext(readFileSync(require('node:path').join(__dirname,'../webui/ble-flow.js'),'utf8'),bluetoothContext);
const {createFlow,explainError}=bluetoothContext.TingxuBluetooth;
function fixture(){
 const calls=[],states=[],files=[],errors=[],devices=[];
 const flow=createFlow({request:async payload=>{calls.push(payload);return {job_id:'job-'+calls.length};},onState:s=>states.push(s),onFiles:(list,meta)=>files.push({list,meta}),onError:e=>errors.push(e),onDevices:d=>devices.push(d)});
 return {flow,calls,states,files,errors,devices};
}
test('scan waits in place, connects one device, then reveals file management',async()=>{
 const f=fixture();await f.flow.start('scan');
 assert.equal(f.calls.length,1);assert.equal(f.files.length,0);
 await f.flow.update([{id:'job-1',status:'running',message:'Scanning'}]);
 assert.equal(f.states.at(-1).phase,'scanning');
 await f.flow.update([{id:'job-1',status:'done',result:{devices:[{address:'AA:BB:CC:DD:EE:FF',name:'Recorder'}]}}]);
 assert.equal(f.calls[1].op,'connect');assert.equal(f.files.length,0);
 await f.flow.update([{id:'job-2',status:'done',result:{list_ok:true,connection_verified:true,files:[{name:'meeting.wav'}]}}]);
 assert.equal(f.files.length,1);assert.equal(f.files[0].meta.reveal,true);assert.equal(f.states.at(-1).phase,'ready');
});
test('multiple devices require selection; empty scan gives one actionable dialog',async()=>{
 const f=fixture();await f.flow.start('scan');
 await f.flow.update([{id:'job-1',status:'done',result:{devices:[{address:'a'},{address:'b'}]}}]);
 assert.equal(f.states.at(-1).phase,'choose');assert.equal(f.calls.length,1);assert.equal(f.errors.length,0);
 const empty=fixture();await empty.flow.start('scan');const tasks=[{id:'job-1',status:'done',result:{devices:[]}}];
 await empty.flow.update(tasks);await empty.flow.update(tasks);
 assert.equal(empty.errors.length,1);assert.match(empty.errors[0].title,/没有找到/);assert.ok(empty.errors[0].advice.length);
});
test('connection failure stays out of file manager and can retry',async()=>{
 const f=fixture();await f.flow.start('connect');
 await f.flow.update([{id:'job-1',status:'failed',message:'BLE is not authorized'}]);
 assert.equal(f.files.length,0);assert.match(f.errors[0].title,/权限/);
 await f.flow.retry();assert.equal(f.calls.length,2);assert.equal(f.calls[1].op,'connect');
});
test('repeated clicks do not submit parallel connections and unverified success is rejected',async()=>{
 const f=fixture();await f.flow.start('connect');await f.flow.start('scan');assert.equal(f.calls.length,1);
 await f.flow.update([{id:'job-1',status:'done',result:{list_ok:true}}]);
 assert.equal(f.files.length,0);assert.equal(f.errors.length,1);
});
test('refresh restores prior files without replaying errors or moving focus',async()=>{
 const f=fixture();await f.flow.update([{kind:'ble',status:'done',result:{list_ok:true,files:[]}}]);
 assert.equal(f.files[0].meta.reveal,false);assert.equal(f.errors.length,0);
 assert.match(explainError('Bluetooth device is turned off').title,/未开启/);
});
