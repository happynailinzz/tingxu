'use strict';
window.tingxu = (() => {
 let people={people:[],threshold:.65,margin:.08}, vocab=null, vocabDirty=false, bleFiles=[], bleSelected=new Set();
 const newId=()=>crypto.randomUUID().replaceAll('-','');
 async function loadPeople(){people=await api('/api/people');renderPeople();}
 function renderPeople(){
  $('peopleList').innerHTML=people.people.length?people.people.map(p=>`<article class="panel person-card"><div class="panel-title"><div class="person-name"><span class="person-avatar">${esc([...p.name][0])}</span><div><h2>${esc(p.name)}</h2><small>${p.samples.length} / 5 个样本</small></div></div><div><button data-person-edit="${p.id}" class="text-button">编辑</button><button data-person-delete="${p.id}" class="text-button danger">移除</button></div></div><p class="hint">${esc(p.note||'尚未添加备注')}</p><div>${p.samples.map(s=>`<div class="voice-sample"><span>${esc(s.stem)} · ${s.start}–${s.end} 秒</span><audio controls preload="none" src="/api/voice-sample?person=${p.id}&sample=${s.id}"></audio><button class="text-button danger" data-sample-delete="${p.id}:${s.id}">移除样本</button></div>`).join('')||'<p class="hint">暂无声纹，先在下方登记清晰的单人样本。</p>'}</div></article>`).join(''):'<div class="panel empty-state">新建人物，登记声纹样本后即可为录音生成匹配建议。</div>';
  const oldPerson=$('enrollPerson').value, oldAudio=$('enrollAudio').value;
  $('enrollPerson').innerHTML='<option value="">选择人物</option>'+people.people.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('');
  $('enrollAudio').innerHTML='<option value="">选择本地录音</option>'+state.local.filter(f=>f.audio).map(f=>`<option value="${esc(f.stem)}">${esc(f.title)}</option>`).join('');
  $('enrollPerson').value=oldPerson;$('enrollAudio').value=oldAudio;$('voiceThreshold').value=people.threshold;$('voiceMargin').value=people.margin;
  document.querySelectorAll('[data-person-edit]').forEach(b=>b.onclick=act(()=>editPerson(people.people.find(p=>p.id===b.dataset.personEdit))));
  document.querySelectorAll('[data-person-delete]').forEach(b=>b.onclick=act(async()=>{if(!confirm('移除该人物及其声纹样本？已确认的关联姓名将不再显示。'))return;await api('/api/people/remove',{id:b.dataset.personDelete});await loadPeople();toast('人物已移除，本地保留备份');}));
  document.querySelectorAll('[data-sample-delete]').forEach(b=>b.onclick=act(async()=>{if(!confirm('移除此声纹样本？'))return;const [id,sample]=b.dataset.sampleDelete.split(':');await api('/api/people/remove',{id,sample});await loadPeople();}));
 }
 async function editPerson(person){const name=prompt('人物姓名或角色',person?.name||'');if(name===null)return;const note=prompt('备注（可留空）',person?.note||'');if(note===null)return;await api('/api/people',{...(person?{id:person.id}:{}),name,note});await loadPeople();toast('人物档案已保存');}
 $('addPerson').onclick=act(()=>editPerson());
 $('enrollAudio').onchange=()=>{const f=state.local.find(f=>f.stem===$('enrollAudio').value);$('enrollPlayer').src=f?audioUrl(f):'';};
 $('enrollForm').onsubmit=act(async e=>{e.preventDefault();if(!$('enrollPerson').value||!$('enrollAudio').value)throw new Error('请选择人物和录音');await api('/api/voice-task',{action:'enroll',person_id:$('enrollPerson').value,stem:$('enrollAudio').value,start:Number($('enrollStart').value),end:Number($('enrollEnd').value)});navigate('tasks');await pollTasks();toast('声纹登记任务已开始');});
 $('voiceOptions').onsubmit=act(async e=>{e.preventDefault();await api('/api/people/options',{threshold:Number($('voiceThreshold').value),margin:Number($('voiceMargin').value)});await loadPeople();toast('匹配偏好已保存');});
 async function openRoles(stem){
  const [recording,data]=await Promise.all([api('/api/roles?'+new URLSearchParams({stem})),api('/api/people')]);people=data;
  let dialog=$('rolesDialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='rolesDialog';document.body.append(dialog);}
  dialog.innerHTML=`<form method="dialog"><div class="panel-title"><h2>人物与声纹</h2><button aria-label="关闭" value="cancel" class="icon-button">×</button></div><p class="hint">${esc(stem)} · 关联仅作用于当前录音。原转写保留说话人编号；确认姓名用于阅读与后续总结。</p><div id="roleRows">${recording.speakers.map(id=>{const role=recording.roles[id]||{};const suggestion=recording.suggestions.find(s=>s.speaker===id);return `<div class="role-row" data-speaker="${esc(id)}"><strong>说话人${esc(id)}</strong><select aria-label="说话人${esc(id)}关联人物"><option value="">自定义角色 / 暂不关联</option>${people.people.map(p=>`<option value="${p.id}" ${role.person_id===p.id?'selected':''}>${esc(p.name)}</option>`).join('')}</select><input maxlength="60" placeholder="或填写角色名称" value="${esc(role.name||'')}"><small>${suggestion?.candidate?`候选：${esc(suggestion.candidate.name)} · 相似度 ${suggestion.candidate.score} · ${esc(suggestion.reason)}`:esc(suggestion?.reason||'尚无匹配建议')}</small>${suggestion?.suggested?`<button type="button" class="text-button" data-accept="${esc(suggestion.candidate.person_id)}">采用候选（保存后生效）</button>`:''}</div>`;}).join('')||'<div class="empty-state">此转写没有说话人编号。请使用支持说话人分离的模型重新转写。</div>'}</div><div class="button-row"><button type="button" id="matchVoice" ${recording.has_segments?'':'disabled'}>生成声纹匹配建议</button><button type="button" class="primary" id="saveRoles">确认并保存关联</button></div><p class="hint">${recording.has_segments?'已有精确分段，可匹配本地声纹。':'旧转写缺少精确分段；使用新版重新转写后可匹配，当前仍可手动命名。'} 保存关联不会改写已有纪要，如需应用姓名请重新总结。</p></form>`;
  dialog.querySelectorAll('[data-accept]').forEach(b=>b.onclick=()=>{b.closest('.role-row').querySelector('select').value=b.dataset.accept;b.closest('.role-row').querySelector('input').value='';});
  $('saveRoles').onclick=act(async()=>{const roles={};dialog.querySelectorAll('.role-row').forEach(row=>{roles[row.dataset.speaker]={person_id:row.querySelector('select').value,name:row.querySelector('input').value};});await api('/api/roles',{stem,roles});dialog.close();await openRecording(stem);toast('已保存人物关联');});
  $('matchVoice').onclick=act(async()=>{await api('/api/voice-task',{action:'match',stem});dialog.close();navigate('tasks');await pollTasks();toast('声纹匹配任务已开始，完成后回到录音详情确认');});
  if(!dialog.open)dialog.showModal();
 }
 async function loadVocabulary(){if(vocabDirty&&vocab)return;vocab=await api('/api/vocabulary');renderVocabulary();}
 function markDirty(){vocabDirty=true;}
 function renderVocabulary(){
  $('hotwordsEnabled').checked=vocab.enabled;$('correctionsEnabled').checked=vocab.corrections_enabled;
  const q=$('wordSearch').value.trim().toLowerCase();
  $('wordList').innerHTML=vocab.words.map((w,index)=>({...w,index})).filter(w=>(w.word+' '+w.group).toLowerCase().includes(q)).map(w=>`<div class="vocab-row" data-word="${w.index}"><label class="word-toggle"><input type="checkbox" data-field="enabled" ${w.enabled?'checked':''} aria-label="启用词条"></label><input value="${esc(w.word)}" data-field="word" placeholder="热词" aria-label="热词"><input type="number" min="1" max="100" value="${w.weight}" data-field="weight" aria-label="权重"><input value="${esc(w.group)}" data-field="group" placeholder="分组" aria-label="分组"><button class="text-button danger" data-remove-word="${w.index}">移除</button></div>`).join('')||'<div class="empty-state">没有匹配的热词。</div>';
  $('wordList').querySelectorAll('input').forEach(input=>input.oninput=()=>{const row=vocab.words[Number(input.closest('[data-word]').dataset.word)];row[input.dataset.field]=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value;markDirty();});
  $('wordList').querySelectorAll('[data-remove-word]').forEach(b=>b.onclick=()=>{vocab.words.splice(Number(b.dataset.removeWord),1);markDirty();renderVocabulary();});
  $('correctionList').innerHTML=vocab.corrections.map((r,index)=>`<div class="correction-row" data-correction="${index}"><input type="checkbox" data-field="enabled" ${r.enabled?'checked':''} aria-label="启用规则"><input data-field="from" value="${esc(r.from)}" placeholder="原词" aria-label="原词"><span>→</span><input data-field="to" value="${esc(r.to)}" placeholder="规范词" aria-label="规范词"><button class="text-button danger" data-remove-correction="${index}">移除</button></div>`).join('')||'<p class="hint">暂无规则，按需添加确定的误识别词。</p>';
  $('correctionList').querySelectorAll('input').forEach(input=>input.oninput=()=>{vocab.corrections[Number(input.closest('[data-correction]').dataset.correction)][input.dataset.field]=input.type==='checkbox'?input.checked:input.value;markDirty();});
  $('correctionList').querySelectorAll('[data-remove-correction]').forEach(b=>b.onclick=()=>{vocab.corrections.splice(Number(b.dataset.removeCorrection),1);markDirty();renderVocabulary();});
 }
 $('wordSearch').oninput=()=>vocab&&renderVocabulary();
 $('hotwordsEnabled').onchange=()=>{vocab.enabled=$('hotwordsEnabled').checked;markDirty();};$('correctionsEnabled').onchange=()=>{vocab.corrections_enabled=$('correctionsEnabled').checked;markDirty();};
 $('addWord').onclick=()=>{vocab.words.push({id:newId(),word:'',weight:20,group:'',enabled:true});markDirty();$('wordSearch').value='';renderVocabulary();$('wordList').lastElementChild?.querySelector('[data-field="word"]')?.focus();};
 $('addCorrection').onclick=()=>{vocab.corrections.push({from:'',to:'',enabled:true});markDirty();renderVocabulary();};
 $('addBulkWords').onclick=act(async()=>{for(const line of $('bulkWords').value.split('\n').filter(l=>l.trim())){const [word,weight='20',group='']=line.split(/[,，\t]/);vocab.words.push({id:newId(),word:word.trim(),weight:Number(weight),group:group.trim(),enabled:true});}markDirty();$('bulkWords').value='';$('wordSearch').value='';renderVocabulary();toast('已加入草稿，保存后生效');});
 $('saveVocabulary').onclick=act(async()=>{vocab=await api('/api/vocabulary',vocab);vocabDirty=false;renderVocabulary();toast('词库已保存，新转写任务将使用此词库');});
 $('exportVocabulary').onclick=()=>{const blob=new Blob([JSON.stringify(vocab,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='tingxu-vocabulary.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
 $('importVocabulary').onclick=()=>$('vocabularyFile').click();$('vocabularyFile').onchange=act(async()=>{const f=$('vocabularyFile').files[0];if(!f)return;if(f.size>256*1024)throw new Error('词库文件超过 256 KB');const data=JSON.parse(await f.text());if(!Array.isArray(data.words)||!Array.isArray(data.corrections))throw new Error('词库格式不正确');vocab=data;markDirty();renderVocabulary();$('vocabularyFile').value='';toast('词库已导入草稿，请检查后保存');});
 function renderBle(){
  $('bleSelectedCount').textContent=bleSelected.size?`已选 ${bleSelected.size} 条`:'选择蓝牙文件';$('bleSelectAll').checked=!!bleFiles.length&&bleFiles.every(f=>bleSelected.has(f.name));
  $('bleFiles').className=bleFiles.length?'':'empty-state';$('bleFiles').innerHTML=bleFiles.length?bleFiles.map(f=>`<label class="ble-file-row"><input type="checkbox" data-ble-name="${esc(f.name)}" ${bleSelected.has(f.name)?'checked':''}><span>${esc(f.name)}</span><small>${size(f.size)} · ${state.local.some(l=>l.stem===f.stem)?'本地已有':'待导入'}</small></label>`).join(''):'最近读取的蓝牙文件列表为空。';
  $('bleFiles').querySelectorAll('input').forEach(c=>c.onchange=()=>{c.checked?bleSelected.add(c.dataset.bleName):bleSelected.delete(c.dataset.bleName);renderBle();});
 }
 const flow=window.TingxuBluetooth.createFlow({
  request: async body=>{const result=await api('/api/ble',body);return result;},
  onState: info=>{
   const titles={scanning:'正在扫描附近的录音卡…',connecting:'正在连接并读取文件…',working:'正在执行蓝牙操作…',choose:'请选择设备',ready:'已进入卡上文件管理',done:'操作完成',failed:'本次操作未完成'};
   $('bleInlineStatus').hidden=false;$('bleInlineStatus').dataset.phase=info.phase;
   $('bleStatusTitle').textContent=titles[info.phase]||'蓝牙导入';
   $('bleStatusDetail').textContent=info.message||'请留在当前页面等待，结果会直接显示在这里。';
   $('bleSpinner').hidden=!info.busy;
   if(['scanning','connecting'].includes(info.phase))$('bleFileManager').hidden=true;
   $('blePanel').querySelectorAll('button[data-ble],button[data-record],#bleImportSelected,#bleConnectSelected').forEach(b=>b.disabled=info.busy);
   if(info.log)$('bleInlineLog').textContent=info.log.join('\n');
  },
  onDevices: devices=>{
   $('bleDevicePicker').hidden=devices.length<2;
   $('bleDevices').innerHTML=devices.map(d=>`<option value="${esc(d.address)}">${esc(d.name)} · ${esc(d.address)}</option>`).join('');
  },
  onFiles: (files,meta)=>{
   bleFiles=files;bleSelected.clear();renderBle();$('bleFileManager').hidden=false;
   $('bleListHint').textContent='最近读取：'+meta.started+' · '+files.length+' 条。操作会按需重新连接设备，导入进度显示在当前页面。';
   if(meta.reveal&&state.view==='card'){$('bleFileManager').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});$('bleFileHeading').focus({preventScroll:true});}
  },
  onError: showBleError,
  onComplete: (op)=>{if(op.startsWith('download'))refreshLibrary().then(()=>{renderBle();toast('蓝牙导入完成，本地档案已更新');}).catch(e=>toast(e.message,true));}
 });
 function showBleError(error){
  let dialog=$('bleErrorDialog');if(!dialog){dialog=document.createElement('dialog');dialog.id='bleErrorDialog';document.body.append(dialog);}
  dialog.innerHTML=`<div class="panel-title"><h2>${esc(error.title)}</h2><button id="bleErrorClose" class="icon-button" aria-label="关闭">×</button></div><p class="hint">请根据下面的建议检查后重试。</p><ul class="ble-advice">${error.advice.map(a=>`<li>${esc(a)}</li>`).join('')}</ul><details><summary>查看错误详情</summary><pre>${esc(error.detail)}</pre></details><div class="button-row"><button id="bleErrorDismiss">知道了</button><button id="bleErrorRetry" class="primary">重试</button></div>`;
  $('bleErrorClose').onclick=$('bleErrorDismiss').onclick=()=>dialog.close();
  $('bleErrorRetry').onclick=()=>{dialog.close();flow.retry();};
  if(!dialog.open)dialog.showModal();
 }
 async function runBle(op,extra={}){if(state.tasks.some(t=>t.status==='running')&&!flow.isBusy()){showBleError(window.TingxuBluetooth.explainError('有任务正在运行，请等待完成'));return;}await flow.start(op,extra);await pollTasks();}
 function updateBle(tasks){flow.update(tasks).catch(e=>showBleError(window.TingxuBluetooth.explainError(e.message)));}
 $('bleSelectAll').onchange=()=>{bleFiles.forEach(f=>$('bleSelectAll').checked?bleSelected.add(f.name):bleSelected.delete(f.name));renderBle();};
 $('bleImportSelected').onclick=()=>{if(!bleSelected.size){toast('请先选择蓝牙文件',true);return;}runBle('download-selected',{names:[...bleSelected]});};
 $('bleConnectSelected').onclick=()=>runBle('connect',{address:$('bleDevices').value});
 document.querySelectorAll('[data-ble],[data-record]').forEach(b=>b.onclick=()=>runBle(b.dataset.record?'record':b.dataset.ble,{record:b.dataset.record||'start',seconds:30}));
 document.querySelectorAll('.nav').forEach(button=>{for(const node of [...button.childNodes]){if(node.nodeType===Node.TEXT_NODE&&node.textContent.trim()){const label=document.createElement('span');label.className='nav-label';label.textContent=node.textContent.trim();button.replaceChild(label,node);}}});
 document.querySelectorAll('.nav').forEach(button=>{const short={library:'档案',card:'录音卡',tasks:'任务',settings:'模型',people:'声纹',vocabulary:'热词',trash:'回收站'};button.dataset.short=short[button.dataset.view];});
 window.addEventListener('beforeunload',event=>{if(vocabDirty){event.preventDefault();event.returnValue='';}});
 return {loadPeople,loadVocabulary,openRoles,updateBle};
})();
