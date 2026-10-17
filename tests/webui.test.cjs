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
