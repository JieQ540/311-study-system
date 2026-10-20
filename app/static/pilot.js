/* 试点编排；参考与答案由 QA 共用组件显示，后端负责锁定及暴露记录。 */
(function () {
  'use strict';
  const $ = id => document.getElementById(id), esc = QA.esc;
  const roles = {A:'初测',B:'针对训练',C:'迁移练习',D:'保留终测',P:'自主练习'};
  const kinds = {initial:'首答',practice:'再练',correction:'订正',retest:'到期复测',early:'提前练习'};
  const dims = {task:'审题与任务覆盖',knowledge:'知识准确性',organization:'组织与展开',application:'论证与材料应用'};
  let run, busy=false, draftQueue=Promise.resolve(), timer, paused=false, phase='answer_seconds';
  let timing={answer_seconds:0,outline_seconds:0,reading_seconds:0}, lastDraft='';
  let dashboard, selectedVersion='';
  const when = s => s ? new Date(s).toLocaleString('zh-CN',{hour12:false}) : '—';
  const key = () => 'kaoyan-pilot-' + run.id;
  function say(message, error=false) {$('pilotStatus').textContent=message;$('pilotStatus').className=error?'notice pilot-error':'notice';}
  async function request(url, body) {
    const response=await fetch(url,body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if (!response.ok) throw new Error('请求未完成，请稍后重试。');
    const data=await response.json();if (!data.ok) throw new Error(data.error||'操作未完成');return data;
  }
  function localDraft() {
    if (!run || run.state!=='answering' || !$('answer')) return;
    try {localStorage.setItem(key(),JSON.stringify({answer_text:$('answer').value,outline_text:$('outline').value,selected_option:selectedOption(),timing,revision:run.revision,interrupted:$('interrupted')?.checked}));} catch (_) {}
  }
  async function flush() {
    if (!run || run.state!=='answering') return;
    const body={answer_text:$('answer').value,outline_text:$('outline').value,selected_option:selectedOption()};
    const serial=JSON.stringify(body);localDraft();
    draftQueue=draftQueue.catch(()=>{}).then(async()=>{
      if (serial===lastDraft) return;
      const data=await request(`/api/pilot/runs/${run.id}/draft`,{...body,revision:run.revision});
      run.revision=data.revision;lastDraft=serial;localDraft();say('草稿已保存。');
    });
    return draftQueue;
  }
  function startClock() {
    clearInterval(timer);
    timer=setInterval(()=>{
      if (paused || document.hidden || !run) return;
      if (run.state==='answering') timing[phase]++;
      else if ($('pilotReference')?.textContent) timing.reading_seconds++;
      if ($('clock')) $('clock').textContent=`作答 ${Math.floor(timing.answer_seconds/60)} 分 · 提纲 ${Math.floor(timing.outline_seconds/60)} 分 · 阅读 ${Math.floor(timing.reading_seconds/60)} 分`;
      localDraft();
      if (timing.reading_seconds && timing.reading_seconds % 10 === 0 && run.state!=='answering') {
        request(`/api/pilot/runs/${run.id}/effort`,{reading_seconds:timing.reading_seconds}).catch(()=>{});
      }
    },1000);
  }
  function count(value) {return value===null||value===undefined?'—':String(value);}
  function currentBatch() {return dashboard?.packages.find(p=>p.version===selectedVersion);}
  function renderHome() {
    const packages=dashboard.packages||[];
    if(!packages.length){
      $('pilotHome').innerHTML='<section class="card"><h2>尚未启用训练资料</h2><p>本服务还没有安装冻结题包。可以先查看<a href="/coverage">全科覆盖与资料缺口</a>。</p></section>';
      return;
    }
    const p=currentBatch()||packages[0];selectedVersion=p.version;
    const trial=p.purpose!=='practice',topics=p.topics||[],plans=p.plans||[],counts=p.counts||{},feedback=p.feedback||{};
    const topicCount=p.topic_count??counts.roster??topics.length;
    const questionCount=p.question_count??counts.distinct_questions??topics.reduce((n,t)=>n+(t.items||[]).length,0);
    $('pilotScope').textContent=`${trial?'验证批次':'自主练习批次'} · ${topicCount} 个主题 · ${questionCount} 道题`;
    $('pilotIntro').textContent=trial?'第一批按冻结名册保留初答、订正、延后复测和新题记录；全科复习可同时进行。每天建议 30–60 分钟，可按负担延长。':'按当前学习需要选择题目，可完整作答或只练一个小问。参考与诊断在锁定原答后提供，练习记录单独保留。';
    $('pilotHome').innerHTML=`<section class="card pilot-batch"><label for="pilotBatch">选择训练批次</label>
      <select id="pilotBatch">${packages.map(item=>`<option value="${esc(item.version)}" ${item.version===p.version?'selected':''}>${esc(item.name||item.version)}${item.purpose==='practice'?' · 自主练习':' · 验证'}</option>`).join('')}</select>
      <p class="pilot-meta">${esc(p.name||p.version)} · ${esc(p.version)} · ${topicCount} 个主题、${questionCount} 道题</p></section>
      <p class="notice">${esc(dashboard.note||'未测与待核均不记作 0 分。')} 已保存整题 ${count(counts.full_answers)} 次，订正 ${count(counts.corrections)} 次。${trial?'验证结论只按本批冻结名册判断。':'自主练习次数不代表已经掌握。'}</p>
      <div class="pilot-grid">${p.topics.map(t=>`<article class="card pilot-topic"><h2>${esc(t.name)}</h2><p>${esc(t.target)}</p>
      ${t.items.map(q=>`<div class="pilot-item" ${q.role==='P'?`id="pilot-item-${esc(q.slot)}"`:''}><b>${q.role} · ${roles[q.role]}${q.role==='P'?` · ${esc(q.slot)}`:''}</b>
        ${q.runs.length?q.runs.map(r=>`<p><a href="/pilot?run=${encodeURIComponent(r.id)}">${kinds[r.kind]||'作答'} · ${r.state==='answering'?'继续作答':r.state==='locked'?'待保存':'查看记录'}</a> ${r.locked_at?esc(when(r.locked_at)):''}</p>`).join(''):
        `<p class="notice">${esc(q.gate || (q.role==='D'?'开始前记录外部接触情况。':'准备好后开始。'))}</p>
        ${q.role==='D'?`<label>此前是否可能接触过这道保留题？<select id="exposure-${esc(q.slot)}"><option value="possible">不确定，按可能看过</option><option value="unseen">确认未接触</option><option value="seen">已接触</option></select></label>`:''}
        <button type="button" data-start="${esc(q.slot)}" data-version="${esc(p.version)}" ${q.gate?'disabled':''}>开始${roles[q.role]||'练习'}</button>`}</div>`).join('')}
      </article>`).join('')}</div>
      ${trial||plans.length?`<section class="card"><h2>${trial?'A 题间隔复测':'间隔复测'}</h2><p>${trial?'订正后自动安排，间隔为 1、6 天；提前练习不推进轮次。':'按实际计划安排复测；提前练习不推进轮次。'}</p>
      ${plans.map(plan=>`<p>${esc(plan.slot)} · 完成 ${plan.round_index}/${plan.intervals.length} 轮 · 下次 ${esc(when(plan.due_at))}
        ${plan.status==='active'?`<button type="button" data-plan="${plan.id}" data-early="${plan.due?'0':'1'}">${plan.due?'开始到期复测':'提前练习'}</button>`:''}
        <a href="/reports?plan=${encodeURIComponent(plan.id)}">调整安排</a></p>`).join('')||'<p>完成 A 题订正后显示计划。</p>'}</section>`:''}
      <section class="card"><h2>阶段记录</h2><p>${trial?'按冻结主题保留缺测、提示和争议情况；未满两轮复测或缺少有效终测时，不判断训练成效。':'汇总自主练习、订正与提示情况，按具体任务核对，不直接判断学习效果。'}</p><button type="button" data-evidence="${esc(p.version)}">查看定性对照</button><div id="pilotEvidence"></div></section>
      <section class="card pilot-feedback"><h2>这批训练的负担</h2><p class="muted">选一个感受，可加一句说明；无需另写长篇复盘。</p>
      <label for="pilotBurden">目前感受</label><select id="pilotBurden"><option value="unknown">还不确定</option><option value="acceptable">可以接受</option><option value="heavy">负担偏重，需要调整</option></select>
      <label for="pilotBurdenNote">补充说明（可选）</label><textarea id="pilotBurdenNote" maxlength="1000" placeholder="例如：整题过多，希望多练局部回答。"></textarea>
      <button type="button" id="savePilotFeedback">保存负担反馈</button><p id="pilotFeedbackMessage" class="muted" role="status"></p></section>`;
    $('pilotBurden').value=['unknown','acceptable','heavy'].includes(feedback.burden)?feedback.burden:'unknown';
    $('pilotBurdenNote').value=feedback.note||'';
    const params=new URLSearchParams(location.search),slot=params.get('slot');
    const target=params.get('version')===p.version&&slot?$('pilot-item-'+slot):null;
    if(target){target.classList.add('pilot-focus');target.scrollIntoView({block:'center'});}
  }
  async function home() {
    dashboard=await request('/api/pilot');
    let remembered='';try{remembered=localStorage.getItem('kaoyan-pilot-batch')||'';}catch(_){}
    const requested=new URLSearchParams(location.search).get('version')||remembered;
    selectedVersion=(dashboard.packages||[]).some(p=>p.version===requested)?requested:(dashboard.packages?.[0]?.version||'');
    renderHome();
  }
  function selectedOption() {
    const input=$('selectedOption');
    return input?(input.value||null):(run?.selected_option||run?.metadata?.selected_option||null);
  }
  function electiveControl(editing) {
    const options=run.elective_options||[],selected=run.selected_option||run.metadata?.selected_option||'';
    if(!options.length)return '';
    const fixed=!editing||!['initial','practice'].includes(run.kind);
    if(fixed)return `<div class="pilot-elective"><b>本次选做：${esc(options.find(o=>o.key===selected)?.label||selected||'尚未记录')}</b><input type="hidden" id="selectedOption" value="${esc(selected)}"><p class="muted">订正及同题复测沿用原来选项。</p></div>`;
    return `<div class="pilot-elective"><label for="selectedOption">本次选做分支</label><select id="selectedOption"><option value="">请选择</option>${options.map(o=>`<option value="${esc(o.key)}" ${o.key===selected?'selected':''}>${esc(o.label)}</option>`).join('')}</select><p class="muted">按所选分支回应任务；锁定后保留此次选择。</p></div>`;
  }
  function evidenceView(data) {
    const summary=data.summary,labels={pending:'证据待补',invalid:'当前记录不可比较',improved:'出现改善信号',maintained:'保持',no_signal:'暂未形成改善信号',practice:'自主练习过程'};
    let intro='';
    if(summary){
      intro=`<div class="pilot-evidence-summary"><h3>${summary.purpose==='practice'?'自主练习过程':esc(summary.decision||'效果尚未判定')}</h3>
        ${summary.purpose==='practice'?'<p>以下记录用于安排后续练习，不据此宣布学习效果。</p>':`<div class="pilot-counts"><span>冻结主题 ${count(summary.roster)}</span><span>有效配对 ${count(summary.valid_topics)}</span><span>改善信号 ${count(summary.improved_topics)}</span><span>保持 ${count(summary.maintained_topics)}</span></div>`}
        <p>${esc(summary.reason||'尚需实际作答和延后记录。')}</p><p class="muted">负担反馈：${esc({acceptable:'可以接受',heavy:'负担偏重',unknown:'还不确定'}[summary.burden]||'还不确定')}</p></div>`;
    }
    return intro+(data.topics||[]).map(t=>{
      const c=t.comparison;
      const comparison=c?`<div class="pilot-comparison"><h3>${esc(c.label||labels[c.status]||'定性对照')}</h3>
        ${c.target?`<p>主要目标：${esc(dims[c.target]||c.target)}</p>`:''}
        ${c.status!=='practice'?`<p>初答：${esc(c.target_before??'尚无可用评定')} · 第二次延后复测：${esc(c.target_retest??'尚无可用评定')} · 新题终测：${esc(c.target_final??'尚无可用评定')}</p>`:''}
        ${c.reasons?.length?`<ul>${c.reasons.map(reason=>`<li>${esc(reason)}</li>`).join('')}</ul>`:''}</div>`:'';
      return `<h3>${esc(t.name)}</h3><p>${esc(t.note||'')}</p>${comparison}<ul class="pilot-evidence-records">${(t.records||[]).map(r=>`<li><a href="/pilot?run=${encodeURIComponent(r.id)}">${esc(r.stage)}</a> · ${esc(r.levels)} · ${esc(r.eligibility)}</li>`).join('')||'<li>尚无作答，保留为缺测。</li>'}</ul>`;
    }).join('');
  }
  function fmtScore(value) {return value===null||value===undefined?'—':String(value);}
  function scoreCard() {
    const s=run.score;
    if(!s) return '';
    const srcName={ai_unconfirmed:'AI 实得分（待确认）',ai_confirmed:'AI 实得分（已确认）',self_confirmed:'自评',unassessed:'尚未评定'}[s.point_source]||'来源未记录';
    const verified=s.verification==='v2_ok'?'逐点对账通过':s.override?'人工确认':'待对账';
    const total=(s.score===null||s.score===undefined||!s.full_score)?'分值待核':fmtScore(s.score)+' / '+fmtScore(s.full_score)+' 分';
    return '<section class="card pilot-score"><h2>本次得分</h2><p class="pilot-score-num">'+esc(total)+'</p>'+
      '<p class="muted">'+esc(srcName)+' · '+esc(verified)+'</p>'+
      (s.point_source==='ai_unconfirmed'?'<div class="pilot-controls"><button type="button" id="confirmScore">采用这次 AI 批改（计入掌握率）</button></div>':'')+
      '<p class="notice">训练实得分用于比较本次与复测，不等同模拟考分数；有争议可保留，不删记录。</p></section>';
  }
  function skeleton() {
    const secs=run.sections||[], pts=run.points||[], s=run.score||{};
    if(!secs.length&&!pts.length) return '';
    const awards=new Map((s.point_awards||[]).map(a=>[a.seq,a.awarded]));
    const secScores=new Map((s.section_scores||[]).map(x=>[String(x.key),x]));
    const groups=secs.length?secs.map(x=>({key:String(x.key),label:x.label,score:x.score,
        pts:pts.filter(p=>String(p.section)===String(x.key))})):
      [{key:'*',label:'本题采分点',score:pts.reduce((n,p)=>n+(p.score||0),0),pts}];
    let sum=0;
    const blocks=groups.map(g=>{
      const sc=secScores.get(g.key);
      const got=sc&&sc.got!=null?sc.got:g.pts.reduce((n,p)=>n+(awards.get(p.seq)||0),0);
      sum+=got;
      const ratio=g.score?got/g.score:null;
      const cls=ratio===null?'none':ratio>=0.85?'good':ratio>0?'part':'miss';
      return '<div class="pilot-block '+cls+'"><b>'+esc(g.label||g.key)+'</b><span>'+fmtScore(got)+' / '+fmtScore(g.score)+' 分</span></div>';
    }).join('');
    const totalScore=s.score!=null?s.score:null;
    const extra=totalScore!=null?Math.round((totalScore-sum)*100)/100:0;
    const extraBlock=Math.abs(extra)>=0.01?
      '<div class="pilot-block '+(extra>0?'good':'miss')+'"><b>'+(extra>0?'加分/调整':'扣分/调整')+'</b><span>'+(extra>0?'+':'')+extra+' 分</span></div>':'';
    const total=totalScore!=null?fmtScore(totalScore)+' / '+fmtScore(s.full_score):'得分待算';
    return '<section class="card pilot-skeleton"><h2>答题骨架</h2>'+
      '<p class="muted">先看结构：每块是一组设问/采分点，颜色表示这组拿到了多少；下面再看逐点细节。</p>'+
      '<div class="pilot-tree"><div class="pilot-root">本题 · '+esc(total)+'</div><div class="pilot-blocks">'+blocks+extraBlock+'</div></div></section>';
  }
  async function gradeRun(prefix) {
    const base=prefix||'';
    say(base+'正在批改并计算实得分…');
    const d=await request('/api/pilot/runs/'+run.id+'/grade',{});
    await loadRun(run.id);
    say(base+(d.reused?'已载入已有得分。':'AI 实得分 '+fmtScore(d.score)+' / '+fmtScore(d.full_score)+' 分。'));
  }
  function render() {
    $('pilotHome').hidden=true;$('pilotWork').hidden=false;
    const editing=run.state==='answering';
    $('pilotScope').textContent=`${roles[run.role]||'主观题练习'} · ${run.slot}`;
    $('pilotWork').innerHTML=`<section class="card"><p class="pilot-meta">${esc(run.slot)} · ${roles[run.role]||'主观题练习'} · ${kinds[run.kind]||'作答'}</p>
      <h2>本次作答</h2><div class="pilot-stem">${esc(run.stem)}</div>
      ${electiveControl(editing)}
      ${run.hints.length?`<p class="notice">已记录最高 ${Math.max(...run.hints.map(h=>h.level))} 级提示，本次为辅助训练。</p>`:''}
      ${run.target?`<p class="notice">初测固定目标：${dims[run.target]}</p>`:''}
      <div class="pilot-controls"><span id="clock"></span><button type="button" id="pause">暂停计时</button></div>
      ${editing?`${run.kind==='correction'?`<h3>订正前原答</h3><div class="pilot-answer">${esc(run.original_answer||'')}</div>`:''}<label class="pilot-field">自己的提纲（可选）<textarea id="outline" class="pilot-editor pilot-outline" maxlength="8000"></textarea></label>
      <label class="pilot-field">作答正文<textarea id="answer" class="pilot-editor" maxlength="30000"></textarea></label>
      <div class="pilot-controls"><label>是否查阅资料或接受帮助<select id="external"><option value="">请选择</option><option value="none">没有</option><option value="used">有</option><option value="unknown">不确定</option></select></label>
      ${run.role==='B'||run.role==='P'?'<label>练习范围<select id="scope"><option value="full">完整作答</option><option value="partial">局部练习</option></select></label>':''}
      ${run.role==='D'?'<label>看过题干后，确认此前接触情况<select id="exposureAfter"><option value="possible">不确定，按可能看过</option><option value="unseen">确认未接触</option><option value="seen">已接触</option></select></label>':''}
      ${run.role==='A'&&run.kind==='initial'?`<label>看反馈前固定一个主要目标<select id="target"><option value="">请选择</option>${Object.entries(dims).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></label>`:''}</div>
      <div class="pilot-controls"><label><input id="interrupted" type="checkbox">本次有中断</label><label><input id="reliable" type="checkbox">计时能反映实际投入（纸笔录入等不勾选）</label></div>
      <div class="pilot-controls"><button type="button" id="saveDraft">保存草稿</button><button type="button" id="lock">锁定并保存原答</button><button type="button" class="ghost" id="abandonRun">结束本次，保留草稿</button></div>
      <details><summary>需要帮助（使用后本次记为辅助训练）</summary><div class="pilot-controls">${[1,2,3,4].map((n,i)=>`<button type="button" class="ghost" data-hint="${n}">${['审题提醒','组织提示','关键概念','参考要点示例'][i]}</button>`).join('')}</div><div id="hintText"></div></details>`:
      `<p class="notice">${run.validity.independent?'本次记录符合独立作答条件。':esc(run.validity.reasons.join('；'))} ${esc(run.validity.score_note)}</p>
      ${run.history_hidden?'<p>本考点正在独立作答，旧答案暂不展示。</p>':`<h3>已锁定原答</h3><div class="pilot-answer">${esc(run.answer_text||'')}</div>`}
      <div class="pilot-controls">${run.state==='abandoned'?`<p>本次已结束，草稿和锁定原答仍保留。恢复后按中断过程记录处理。</p>${['initial','practice','correction'].includes(run.kind)?'<button type="button" id="resumeRun">恢复本次</button>':'<a href="/pilot">从计划开始新轮</a>'}`:run.state==='locked'?'<button type="button" id="retrySave">重试保存记录</button>':`<button type="button" id="correct">另存订正</button>${run.role==='P'?'<button type="button" id="repeatRun">另开一次练习或换分支</button>':''}${run.state==='saved'&&!run.score?'<button type="button" id="gradeRun">AI 批改并计分</button>':''}<button type="button" id="reference">查看参考与诊断记录</button><button type="button" id="diagnose">请求 AI 定性诊断</button>`}</div>
      <p class="notice">请求 AI 诊断会使用已配置的模型，可能产生费用；读题和查看已存记录不调用模型。</p>`}</section>
      ${run.state!=='answering'?scoreCard():''}${run.state!=='answering'?skeleton():''}
      <section id="pilotReference" class="card" hidden></section><section id="pilotDiagnosis" class="card" hidden></section><div id="pilotImage"></div>`;
    if(editing){
      let local={};try{local=JSON.parse(localStorage.getItem(key())||'{}');}catch(_){}
      const serverDraft=run.draft||{};
      const chosen=local.revision===run.revision?local:serverDraft;
      $('answer').value=chosen.answer_text||'';$('outline').value=chosen.outline_text||'';
      if($('selectedOption')&&['initial','practice'].includes(run.kind))$('selectedOption').value=chosen.selected_option||run.selected_option||'';
      if(local.revision===run.revision&&local.timing)timing=local.timing;
      if(local.interrupted)$('interrupted').checked=true;
      lastDraft=JSON.stringify({answer_text:serverDraft.answer_text||'',outline_text:serverDraft.outline_text||'',selected_option:serverDraft.selected_option||run.selected_option||null});
    }
    startClock();
  }
  async function loadRun(id){const data=await request('/api/pilot/runs/'+encodeURIComponent(id));run=data.run;timing={...timing,...(run.metadata.timing||{})};render();}
  function dimensionBlocks(c) {
    const items=(c&&c.dimensions)||[]; if(!items.length) return '';
    const clsOf=l=>l==='充分完成'?'good':l==='部分完成'?'part':l==='未完成'?'miss':'none';
    const blocks=items.map(x=>'<div class="pilot-block '+clsOf(x.level)+'"><b>'+esc(dims[x.key]||x.key)+'：'+esc(x.level)+'</b><span>'+esc(x.action||x.reason||'')+'</span></div>').join('');
    return '<div class="pilot-dim-map"><h3>诊断骨架</h3><p class="muted">先看四块完成度，下面再看逐条原文依据与改法。</p><div class="pilot-blocks">'+blocks+'</div></div>';
  }
  async function showReference(){
    const data=await request(`/api/pilot/runs/${run.id}/reference`,{});$('pilotReference').hidden=false;
    $('pilotReference').innerHTML=QA.renderPilotReference(data);
    const pane=$('pilotDiagnosis');pane.hidden=false;
    pane.innerHTML='<h2>定性诊断</h2>'+ (data.diagnoses.length?data.diagnoses.map(d=>{
      const c=JSON.parse(d.content_json),e=JSON.parse(d.evaluator_json);
      return `<article data-diagnosis="${d.id}"><p class="notice">AI 建议 · ${esc(e.model||'模型未记录')} · ${esc(e.prompt_version)} · ${esc(d.status==='suggested'?'待阅读':d.status==='disputed'?'有争议，待核':'已记录用户修正，待复核')}</p>
      ${dimensionBlocks(c)}${c.dimensions.map(x=>`<div class="pilot-diagnosis"><h3>${dims[x.key]}：${esc(x.level)}</h3><p>原答：${esc(x.quote||'缺失任务：'+x.missing_task)}</p><p>依据：${esc(x.reference)}</p><p>${esc(x.reason)}</p><p>下一步：${esc(x.action)}</p></div>`).join('')}
      <h3>优先练习</h3><ul>${c.actions.map(x=>`<li>${esc(x)}</li>`).join('')}</ul><p class="notice">${esc(c.note)}</p>
      <label>这条诊断的争议或修正<textarea id="note-${d.id}" class="pilot-editor pilot-outline">${esc(d.user_note)}</textarea></label>
      <div class="pilot-controls"><button type="button" data-dispute="${d.id}">标记待核</button><button type="button" data-correct-diagnosis="${d.id}">保存我的修正</button></div></article>`;
    }).join(''):'<p>还没有模型诊断；可以直接依据参考卡订正，也可主动请求一次诊断。</p>');
  }
  document.addEventListener('input',e=>{if(['answer','outline','selectedOption'].includes(e.target.id)){localDraft();}});
  document.addEventListener('change',e=>{
    if(e.target.id==='pilotBatch'&&!busy){
      selectedVersion=e.target.value;
      try{localStorage.setItem('kaoyan-pilot-batch',selectedVersion);}catch(_){}
      renderHome();say('已切换训练批次。');
    }
    if(e.target.id==='selectedOption')localDraft();
  });
  document.addEventListener('focusin',e=>{if(e.target.id==='outline')phase='outline_seconds';if(e.target.id==='answer')phase='answer_seconds';});
  document.addEventListener('pointerdown',e=>{if(e.target.closest('#pilotReference,#hintText'))phase='reading_seconds';});
  document.addEventListener('visibilitychange',()=>{if(document.hidden&&run?.state==='answering'){if($('interrupted'))$('interrupted').checked=true;localDraft();}});
  document.addEventListener('click',async e=>{
    const button=e.target.closest('button');if(!button||busy)return;
    busy=true;button.disabled=true;
    try{
      if(button.dataset.start){const r=await request('/api/pilot/start',{version:button.dataset.version,slot:button.dataset.start,prior_exposure:$('exposure-'+button.dataset.start)?.value});location.href='/pilot?run='+r.run.id;}
      else if(button.dataset.plan){const r=await request(`/api/retest/plans/${button.dataset.plan}/start`,{early:button.dataset.early==='1'});location.href=r.run.pilot_url;}
      else if(button.id==='pause'){paused=!paused;button.textContent=paused?'继续计时':'暂停计时';}
      else if(button.id==='saveDraft')await flush();
      else if(button.id==='abandonRun'){await flush();await request(`/api/pilot/runs/${run.id}/abandon`,{});location.href='/pilot?batch='+encodeURIComponent(run.package);}
      else if(button.id==='resumeRun'){const data=await request('/api/pilot/start',{version:run.package,slot:run.slot,resume_run_id:run.id});await loadRun(data.run.id);say('已恢复草稿，本次保留为中断过程记录。');}
      else if(button.id==='lock'){
        await flush();
        const data=await request(`/api/pilot/runs/${run.id}/lock`,{answer_text:$('answer').value,outline_text:$('outline').value,revision:run.revision,
          selected_option:selectedOption(),external_help:$('external').value,prior_exposure_after:$('exposureAfter')?.value,scope:$('scope')?.value||'full',target:$('target')?.value,interrupted:$('interrupted').checked,timing_reliable:$('reliable').checked,timing});
        run=data.run;render();
        await request(`/api/pilot/runs/${run.id}/save`,{});
        await loadRun(run.id);
        say('原答已锁定并保存。可查看参考，再另存订正。');
        try{localStorage.removeItem(key());}catch(_){}
        try{await gradeRun('原答已锁定并保存。');}catch(error){say('原答已锁定并保存；AI 批改未完成：'+error.message,true);}
      }
      else if(button.id==='retrySave'){await request(`/api/pilot/runs/${run.id}/save`,{});await loadRun(run.id);say('保存成功。');}
      else if(button.id==='repeatRun'){const d=await request('/api/pilot/start',{version:run.package,slot:run.slot,new_attempt:true});location.href='/pilot?run='+d.run.id;}
      else if(button.dataset.hint){const h=await request(`/api/pilot/runs/${run.id}/hint`,{level:Number(button.dataset.hint),selected_option:selectedOption()});$('hintText').innerHTML=`<p>${esc(h.label)}</p><ul>${h.content.map(x=>`<li>${esc(typeof x==='string'?x:x.text||x.claim||'')}</li>`).join('')}</ul>`;say('已记录提示使用；本次保留为辅助训练。');}
      else if(button.id==='confirmScore'){await request(`/api/pilot/runs/${run.id}/confirm-score`,{});await loadRun(run.id);say('已采用这次 AI 批改。');}
      else if(button.id==='gradeRun')await gradeRun();
      else if(button.id==='reference')await showReference();
      else if(button.id==='diagnose'){say('正在依据原答和参考卡整理诊断…');await request(`/api/pilot/runs/${run.id}/diagnose`,{});await showReference();say('诊断已保存，可标记争议或修正。');}
      else if(button.id==='correct'){await request(`/api/pilot/runs/${run.id}/effort`,{reading_seconds:timing.reading_seconds});const d=await request('/api/pilot/start',{version:run.package,slot:run.slot,parent_id:run.id});location.href='/pilot?run='+d.run.id;}
      else if(button.dataset.segment!==undefined){$('pilotImage').innerHTML=`<section class="card"><h2>原文切片</h2><img class="pilot-source" alt="与当前考点对应的原书切片" src="/api/pilot/runs/${run.id}/segments/${Number(button.dataset.segment)}"></section>`;}
      else if(button.dataset.dispute||button.dataset.correctDiagnosis){const id=button.dataset.dispute||button.dataset.correctDiagnosis;await request(`/api/pilot/diagnoses/${id}/feedback`,{status:button.dataset.dispute?'disputed':'corrected',note:$('note-'+id).value});await showReference();say('已保留你的意见，原诊断仍可追溯。');}
      else if(button.dataset.evidence){const d=await request('/api/pilot/evidence?version='+encodeURIComponent(button.dataset.evidence));$('pilotEvidence').innerHTML=evidenceView(d);}
      else if(button.id==='savePilotFeedback'){
        const feedback={burden:$('pilotBurden').value,note:$('pilotBurdenNote').value};
        const d=await request('/api/pilot/feedback',{version:selectedVersion,...feedback});
        if(currentBatch())currentBatch().feedback=d.feedback||feedback;
        $('pilotFeedbackMessage').textContent='已保存，后续判断会保留这项负担反馈。';say('负担反馈已保存。');
      }
    }catch(error){say(error.message,true);}finally{busy=false;button.disabled=false;}
  });
  window.addEventListener('beforeunload',localDraft);
  const id=new URLSearchParams(location.search).get('run');
  (id?loadRun(id):home()).then(async()=>{if(run?.kind==='correction')await showReference();}).catch(e=>say(e.message,true));
})();
