/* 全科元数据目录；题干仅由用户明确预览取得，参考交给锁定后的训练流程。 */
(function () {
  'use strict';
  const $=id=>document.getElementById(id);
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num=value=>value===null||value===undefined?'—':String(value);
  const types={analysis:'辨析题',short:'简答题',essay:'分析论述题'};
  const sourceKinds={real:'真题库原标记',ai:'AI生成原标记'};
  const references={usable_qualitative:'可作定性参考',needs_source:'待补可靠来源',disputed:'有争议，待核'};
  const taskNames={judge:'判断辨析',explain:'解释说明',list:'列举概括',compare:'比较异同',evaluate:'分析评价',apply:'结合材料应用',recall:'回忆概述',analyze:'分析论证',design:'方案设计',identify:'识别概念',illustrate:'举例说明',summarize:'概括归纳',define:'界定概念',compute:'计算分析'};
  const gapNames={source_provenance:'原出处待核',reference_body_missing:'缺参考正文',independent_reference_missing:'缺独立参考依据',mapping_uncertain:'任务映射待核',reference_conflict:'参考有争议',reference_body_integrity:'参考文字残缺',reference_body_incomplete:'参考不完整',stem_integrity:'题面待校',policy_source_unverified:'政策材料待核',historical_chronology:'历史年代待核',semantic_duplicate:'相似任务需去重',stem_terminology:'题面术语待校',bank_body_missing_local_text_found:'库正文缺失，已有局部文字依据'};
  let data,byId=new Map(),nodeById=new Map(),itemPage=0,nodePage=0,busy=false;
  const ITEM_SIZE=12,NODE_SIZE=20;
  function say(message,error=false){$('coverageStatus').textContent=message;$('coverageStatus').className=error?'notice coverage-error':'notice';}
  async function request(url,body){
    const response=await fetch(url,body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    let result;try{result=await response.json();}catch(_){throw new Error('读取未完成，请稍后重试。');}
    if(!response.ok||!result.ok)throw new Error(result.error||'操作未完成，请稍后重试。');
    return result;
  }
  function subjectsOf(item){return Array.isArray(item.subjects)&&item.subjects.length?item.subjects:[item.subject].filter(Boolean);}
  function selected(){return {subject:$('coverageSubject').value,node:$('coverageNode').value,type:$('coverageType').value,task:$('coverageTask').value,reference:$('coverageReference').value,gap:$('coverageGap').value,search:$('coverageSearch').value.trim().toLocaleLowerCase()};}
  function within(id,ancestor){
    const seen=new Set();let node=nodeById.get(String(id));
    while(node&&!seen.has(String(node.id))){
      if(String(node.id)===String(ancestor))return true;
      seen.add(String(node.id));node=nodeById.get(String(node.parent_id));
    }
    return false;
  }
  function textOf(item){return [item.title,...(item.mapped_names||[]),...subjectsOf(item),references[item.reference_status]||''].join(' ').toLocaleLowerCase();}
  function filteredItems(){
    const f=selected();
    return (data.items||[]).filter(item=>
      (!f.subject||subjectsOf(item).includes(f.subject))&&
      (!f.node||(item.primary_node_ids||[]).some(id=>within(id,f.node)))&&
      (!f.type||item.qtype===f.type)&&(!f.reference||item.reference_status===f.reference)&&
      (!f.task||(item.task_kinds||[]).includes(f.task))&&
      (!f.gap||f.gap==='mapping'&&item.mapping_status==='tentative'||f.gap==='any'&&Number(item.gap_count||item.gap_kinds?.length||0)>0||f.gap==='practice'&&item.can_practice)&&
      (!f.search||textOf(item).includes(f.search)));
  }
  function fillNodes(){
    const subject=$('coverageSubject').value,prior=$('coverageNode').value;
    const nodes=(data.nodes||[]).filter(n=>['board','chapter','section','point'].includes(n.level)&&(!subject||n.subject===subject));
    $('coverageNode').replaceChildren(new Option('全部大纲范围',''));
    nodes.forEach(n=>$('coverageNode').add(new Option(n.path||n.name,String(n.id))));
    $('coverageNode').value=nodes.some(n=>String(n.id)===prior)?prior:'';
  }
  function renderSummary(){
    const s=data.summary||{};
    const stats=[['关联主观题',s.question_count],['可定性参考',s.reference_ready_count],['待补可靠来源',s.needs_source_count],['参考有争议',s.disputed_count],['映射待核题',s.tentative_question_count],['实际作答记录',s.answer_count]];
    $('coverageSummary').innerHTML=stats.map(([label,n])=>`<div class="coverage-stat"><strong>${esc(num(n))}</strong><span>${esc(label)}</span></div>`).join('');
    $('coverageNotes').innerHTML=[s.coverage_note||'题目覆盖按实际设问与节点关系核对，不能从上层标签推断全节都有题。',s.reference_note||'参考资料状态表示来源与整理情况，不等同于官方评分已经核准。',s.evidence_note||'作答、订正和复测分别保留；没有作答不算错误。',s.protected_count?`有 ${s.protected_count} 道保留题的内容受保护，不能从目录预览或练习。`:''].filter(Boolean).map(note=>`<p>${esc(note)}</p>`).join('');
  }
  function renderSubjects(){
    const active=$('coverageSubject').value;
    $('coverageSubjects').innerHTML=(data.subjects||[]).map(s=>`<button type="button" class="coverage-subject" data-subject="${esc(s.name)}" aria-pressed="${active===s.name}"><strong>${esc(s.name)}</strong><span>题目 ${esc(num(s.question_count))} · 可定性参考 ${esc(num(s.reference_ready_count))}</span><span>已答题目 ${esc(num(s.answered_question_count))} · 作答记录 ${esc(num(s.answer_count))}</span></button>`).join('');
  }
  function renderNodes(){
    const f=selected(),state=$('coverageNodeState').value;
    const list=(data.nodes||[]).filter(n=>['board','chapter','section','point'].includes(n.level)&&
      (!f.subject||n.subject===f.subject)&&(!f.node||within(n.id,f.node))&&
      (!f.search||[n.path,n.name,n.subject].join(' ').toLocaleLowerCase().includes(f.search))&&
      (!state||state==='no_task'&&!n.task_question_count||state==='no_reference'&&n.task_question_count>0&&!n.reference_ready_count));
    const pages=Math.max(1,Math.ceil(list.length/NODE_SIZE));nodePage=Math.min(nodePage,pages-1);
    $('coverageNodeCount').textContent=`当前 ${list.length} 个大纲条目。题型、题目资料状态与题目缺口筛选仅作用于下方题目目录。`;
    $('coverageNodes').innerHTML=list.slice(nodePage*NODE_SIZE,(nodePage+1)*NODE_SIZE).map(n=>`<tr><td><button type="button" class="coverage-node-link" data-node="${esc(n.id)}">${esc(n.name)}</button><span class="coverage-node-path">${esc(n.path||n.subject)}</span></td><td>${esc(num(n.task_question_count))}</td><td>${esc(num(n.ancestor_candidate_count))}</td><td>${esc(num(n.reference_ready_count))}</td><td>${esc(num(n.answer_count))}</td></tr>`).join('')||'<tr><td colspan="5">当前范围没有符合条件的大纲条目。</td></tr>';
    $('coverageNodePage').textContent=`第 ${nodePage+1} / ${pages} 页`;
    $('coverageNodesPrevious').disabled=nodePage===0;$('coverageNodesNext').disabled=nodePage>=pages-1;
  }
  function itemView(item){
    const gaps=[...new Set(item.gap_kinds||[])].map(k=>gapNames[k]||'资料细节待核');
    return `<article class="coverage-item" data-question-id="${esc(item.question_id)}"><h3>${esc(item.title||'题目目录条目')}</h3>
      <p><span class="tag">${esc(subjectsOf(item).join(' / ')||'科目待核')}</span><span class="tag">${esc(types[item.qtype]||'主观题')}</span><span class="tag">${esc(item.source_label||sourceKinds[item.source_kind]||'库内题目')}</span><span class="tag ${item.reference_status==='usable_qualitative'?'ok':'warn'}">${esc(references[item.reference_status]||'资料状态待核')}</span>${item.mapping_status==='tentative'?'<span class="tag warn">任务映射待核</span>':''}</p>
      <p class="muted">对应范围：${esc((item.mapped_names||[]).join('；')||'待细分')}<br>任务：${esc((item.task_kinds||[]).map(k=>taskNames[k]||'其他任务').join('、')||'待核')} · ${esc(num(item.task_count))} 项设问任务</p>
      ${gaps.length?`<p class="muted">待处理：${esc(gaps.join('；'))}</p>`:''}
      <div class="coverage-actions"><button type="button" class="ghost" data-preview="${esc(item.question_id)}">预览题干（记录接触）</button>${item.can_practice?`<button type="button" data-practice="${esc(item.question_id)}">开始自主练习</button>`:'<span class="muted">暂不开放自主练习；先核对资料或保留范围。</span>'}</div></article>`;
  }
  function renderItems(){
    const list=filteredItems(),pages=Math.max(1,Math.ceil(list.length/ITEM_SIZE));itemPage=Math.min(itemPage,pages-1);
    $('coverageItemCount').textContent=`当前目录 ${list.length} 道题；可开始练习 ${list.filter(q=>q.can_practice).length} 道。`;
    $('coverageItems').innerHTML=list.slice(itemPage*ITEM_SIZE,(itemPage+1)*ITEM_SIZE).map(itemView).join('')||'<p class="empty">此范围暂没有符合条件的候选题。可以调整筛选，或保留为后续补题/补资料任务；不据此判断已经薄弱。</p>';
    $('coveragePage').textContent=`第 ${itemPage+1} / ${pages} 页`;
    $('coveragePrevious').disabled=itemPage===0;$('coverageNext').disabled=itemPage>=pages-1;
  }
  function renderBatches(){
    $('coverageBatches').innerHTML=(data.batches||[]).map(b=>`<p class="coverage-batch"><a href="/pilot?version=${encodeURIComponent(b.version)}">${esc(b.name||b.version)}</a> · ${b.purpose==='practice'?'自主练习':'验证批次'}${b.topic_count!==undefined?` · ${esc(b.topic_count)} 个主题`:''}${b.question_count!==undefined?` · ${esc(b.question_count)} 道题`:''}</p>`).join('')||'<p>尚无已安装批次。题目覆盖与资料缺口仍可先行查看。</p>';
  }
  function clearPreview(){$('coveragePreview').hidden=true;$('coveragePreview').replaceChildren();}
  function render(){renderSubjects();renderNodes();renderItems();}
  async function showPreview(id){
    clearPreview();say('正在预览题干，并记录本次接触…');
    const result=await request('/api/curriculum/preview',{question_id:id}),q=result.question;
    if(!q||typeof q.stem!=='string')throw new Error('当前题目不提供题干预览。');
    $('coveragePreview').innerHTML=`<h2 id="coveragePreviewTitle">题干预览</h2><p class="notice">本次预览已记录为接触。这里仅显示题干与设问任务；参考仍在锁定作答后提供。</p>
      <p class="muted">${esc(q.subject||subjectsOf(byId.get(String(id))||{}).join(' / '))} · ${esc(types[q.qtype]||'主观题')} · ${esc((q.mapped_names||[]).join('；'))}</p><div class="coverage-stem">${esc(q.stem)}</div>
      ${(q.tasks||[]).length?`<h3>本题设问任务</h3><ul>${q.tasks.map(t=>`<li>${esc(typeof t==='string'?t:t.text||'任务待核')}${typeof t==='object'&&t.required===false?'（按题干选做要求处理）':''}</li>`).join('')}</ul>`:''}
      <div class="coverage-actions">${result.can_practice?`<button type="button" data-practice="${esc(id)}">开始自主练习</button>`:'<p class="muted">当前题目尚未达到自主练习开放条件。</p>'}<button type="button" id="coverageClosePreview" class="ghost">收起预览</button></div>`;
    $('coveragePreview').hidden=false;$('coveragePreview').scrollIntoView({behavior:'smooth',block:'start'});say('题干预览已打开，接触记录已保留。');
  }
  async function practice(id){
    say('正在准备本次自主练习…');const result=await request('/api/curriculum/practice',{question_id:id});
    if(!result.run?.id)throw new Error('本次练习尚未准备完成，请重试。');
    location.href='/pilot?run='+encodeURIComponent(result.run.id);
  }
  document.addEventListener('change',event=>{
    if(!event.target.id.startsWith('coverage')||busy)return;
    if(event.target.id==='coverageSubject')fillNodes();
    itemPage=0;nodePage=0;clearPreview();render();
  });
  document.addEventListener('input',event=>{if(event.target.id==='coverageSearch'&&!busy){itemPage=0;nodePage=0;clearPreview();render();}});
  document.addEventListener('click',async event=>{
    const button=event.target.closest('button');if(!button||busy||!data)return;
    if(button.dataset.subject!==undefined){$('coverageSubject').value=button.dataset.subject;fillNodes();itemPage=0;nodePage=0;clearPreview();render();return;}
    if(button.dataset.node!==undefined){$('coverageNode').value=button.dataset.node;itemPage=0;nodePage=0;clearPreview();render();$('coverageItems').scrollIntoView({behavior:'smooth',block:'start'});return;}
    if(button.id==='coverageReset'){
      ['coverageSubject','coverageNode','coverageType','coverageTask','coverageReference','coverageGap','coverageNodeState','coverageSearch'].forEach(id=>$(id).value='');
      itemPage=0;nodePage=0;fillNodes();clearPreview();render();return;
    }
    if(button.id==='coveragePrevious'){itemPage--;renderItems();return;}
    if(button.id==='coverageNext'){itemPage++;renderItems();return;}
    if(button.id==='coverageNodesPrevious'){nodePage--;renderNodes();return;}
    if(button.id==='coverageNodesNext'){nodePage++;renderNodes();return;}
    if(button.id==='coverageClosePreview'){clearPreview();return;}
    if(button.dataset.preview===undefined&&button.dataset.practice===undefined)return;
    const id=Number(button.dataset.preview??button.dataset.practice);
    if(!Number.isSafeInteger(id)||!byId.has(String(id))){say('题目目录已经变化，请刷新后重试。',true);return;}
    busy=true;button.disabled=true;
    try{if(button.dataset.preview!==undefined)await showPreview(id);else await practice(id);}
    catch(error){say(error.message,true);}finally{busy=false;button.disabled=false;}
  });
  request('/api/curriculum').then(result=>{
    data=result;byId=new Map((data.items||[]).map(q=>[String(q.question_id),q]));nodeById=new Map((data.nodes||[]).map(n=>[String(n.id),n]));
    (data.subjects||[]).forEach(s=>$('coverageSubject').add(new Option(s.name,s.name)));
    [...new Set((data.items||[]).flatMap(q=>q.task_kinds||[]))].sort().forEach(k=>$('coverageTask').add(new Option(taskNames[k]||'其他设问任务',k)));
    fillNodes();renderSummary();renderBatches();render();say('已读取全科题目、资料和作答记录；可按科目查看具体缺口。');
  }).catch(error=>say(error.message,true));
})();
