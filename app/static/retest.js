/* 到期队列、冻结快照对比与独立作答编排；批改及命中仍使用 QA。 */
(function(global) {
  'use strict';
  const esc = value => QA.esc(value == null ? '' : String(value));
  const $ = id => document.getElementById(id);
  let activePlan = null, run = null, grade = null, busy = false;
  const grading = new Set();
  const statusLabel = {active:'待复测',paused:'已暂停',complete:'本阶段轮次已完成'};
  const time = value => !value ? '—' : /^\d{4}-\d{2}-\d{2}$/.test(value) ? value
    : new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  const localDay = value => value ? new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date(value)) : '';
  const draftKey = () => 'kaoyan-retest-' + run.id;
  const scoreText = value => value ? `${QA.fmt(value.score)} / ${QA.fmt(value.full)}` : '—';
  async function request(url, body) {
    const response = await fetch(url, body === undefined ? {} : {
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if (!response.ok) throw new Error('读取失败，请稍后重试。');
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || '操作未完成。');
    return data;
  }
  function message(text, error=false) {
    const el = $('retestStatus') || $('retestQueueStatus');
    if (el) { el.textContent = text; el.className = error ? 'notice retest-error' : 'notice'; }
  }
  function planCard(p) {
    const begin = p.open_run ? `<a class="linkbtn mini" href="/retest?run=${esc(p.open_run.id)}">${p.open_run.state === 'grading' ? '继续评定' : '继续作答'}</a>`
      : p.status !== 'paused' ? `<button type="button" class="mini" data-start-plan="${p.id}" data-early="${p.due ? '0':'1'}">${p.due ? '开始到期复测' : '提前练习'}</button>` : '';
    return `<article class="retest-plan ${p.due ? 'is-due' : ''}" data-plan="${p.id}">
      <span class="tag">${p.due ? '已到期' : statusLabel[p.status]}</span><h3>${esc(p.title)}</h3>
      <p>完成 ${p.round_index} / ${p.intervals.length} 轮 · 间隔 ${p.intervals.join('、')} 天<br>下次：${time(p.due_at)}</p>
      <div class="row">${begin}<button type="button" class="ghost mini" data-open-plan="${p.id}">前后对比与安排</button></div></article>`;
  }
  async function loadQueue() {
    if (!$('retestQueue')) return;
    try {
      const data = await request('/api/retest/plans');
      const due = data.plans.filter(p => p.due).length;
      $('retestQueue').innerHTML = `<h2>间隔复测</h2><p class="notice">当前到期 ${due} 项。先独立作答，再核对结果；提前练习会保存，但不会推进到期轮次。</p>
        <p id="retestQueueStatus" class="notice" role="status"></p>
        <div class="retest-list">${data.plans.map(planCard).join('')}</div>
        ${data.plans.length ? '' : '<p class="notice">尚未安排复测。可以在下方逐题回看中加入计划；新订正保存后也会自动安排。</p>'}
        <div id="retestComparison"></div>`;
      const chosen = activePlan || Number(new URLSearchParams(location.search).get('plan'));
      if (chosen) await openPlan(chosen);
    } catch (error) { $('retestQueue').innerHTML = `<p class="retest-error">${esc(error.message)}</p>`; }
  }
  function stageHtml(s) {
    const names = {initial:'初答',correction:'原题订正',early:'提前练习',retest:`第 ${(s.round_index || 0)+1} 轮复测`};
    const snapshot = s.snapshot;
    const scores = snapshot?.scores || {};
    const q = snapshot?.question;
    return `<article class="retest-stage"><h4>${names[s.kind] || '复测记录'}${s.kind === 'retest' && !s.counted ? '（未推进）' : ''}</h4>
      <p class="notice">${time(s.completed_at || s.started_at)}${s.elapsed_days != null ? `<br>距上次安排或完成约 ${s.elapsed_days} 天` : ''}</p>
      ${snapshot ? `<p>练习得分：<b>${scoreText(scores.practice || scores.ai_reference || scores.objective || scores.confirmed || scores.legacy)}</b><br>已确认采分点：<b>${scoreText(scores.confirmed)}</b><br>AI 详细批改：<b>${scoreText(scores.ai_reference)}</b></p>
        ${scores.legacy ? `<p class="notice">旧来源不明：${scoreText(scores.legacy)}，不据此计算进步。</p>` : ''}
        ${!Object.keys(scores).length ? '<p class="notice">尚未评定或分值待核，暂不能判断分数变化。</p>' : ''}
        <details><summary>查看这次原答</summary><div class="ans">${esc(q.my_answer || '这次没有留下作答原文')}</div>
          <p><a href="/reports?session=${snapshot.session_id}">查看这次完整作答与批改</a></p></details>`
        : `<p class="notice">${s.state === 'answering' ? '本次作答尚未提交' : s.state === 'grading' ? '原答已锁定，尚未保存评定' : '本轮已结束'}。</p>
          ${s.answer_text ? `<details><summary>已锁定的原答</summary><div class="ans">${esc(s.answer_text)}</div></details>` : ''}`}
      ${s.note ? `<p class="notice">${esc(s.note)}</p>` : ''}</article>`;
  }
  function changeHtml(title, c) {
    const scores = Object.values(c.scores || {});
    return `<div class="retest-change"><b>${title}</b>
      ${scores.map(s => `<p>${esc(s.label)}：${QA.fmt(s.before)} → ${QA.fmt(s.after)} / ${QA.fmt(s.full)}，变化 ${s.delta > 0 ? '+' : ''}${QA.fmt(s.delta)} 分</p>`).join('')}
      ${c.reason ? `<p class="notice">${esc(c.reason)}</p>` : ''}
      ${c.new_hits?.length ? `<p>新增命中：第 ${c.new_hits.join('、')} 点。</p>` : ''}
      ${c.lost_hits?.length ? `<p>本次未再命中：第 ${c.lost_hits.join('、')} 点。</p>` : ''}
      ${c.repeated_diagnoses?.length ? `<p class="notice">两次 AI 批改都提示：${c.repeated_diagnoses.map(esc).join('、')}。未再次提示某问题，也不足以证明它已解决。</p>` : ''}</div>`;
  }
  async function openPlan(id, scroll=false) {
    activePlan = Number(id);
    const result = await request('/api/retest/plans/' + activePlan);
    const p = result.plan;
    const titles = {correction:'初答 → 最新订正',initial_to_retest:'初答 → 最近到期复测',correction_to_retest:'订正 → 最近到期复测'};
    $('retestComparison').innerHTML = `<section class="retest-comparison" data-comparison-plan="${p.id}"><h3>${esc(p.title)} · 前后对比</h3>
      <p class="notice">${esc(p.comparison_note)}</p>
      ${Object.entries(p.comparisons).map(([key,c]) => changeHtml(titles[key],c)).join('') || '<p class="notice">完成订正或到期复测后，这里会分别展示变化。</p>'}
      <div class="retest-stages">${p.stages.map(stageHtml).join('')}</div>
      ${p.status !== 'complete' ? `<details class="retest-options"><summary>调整复测安排</summary><div class="retest-schedule">
        <label>每轮间隔天数<input id="retestIntervals" value="${p.intervals.join(',')}" aria-label="每轮间隔天数"></label>
        <label>下次日期（上海时间 09:00）<input id="retestDue" type="date" value="${localDay(p.due_at)}" data-original="${localDay(p.due_at)}"></label>
        <button type="button" data-save-schedule="${p.id}">保存安排</button>
        <button type="button" class="ghost" data-pause-plan="${p.id}" data-status="${p.status === 'paused' ? 'active' : 'paused'}">${p.status === 'paused' ? '恢复计划' : '暂停计划'}</button></div>
        <p class="notice">例如 1,3,7 表示依次再隔 1、3、7 天；不是通用最优间隔。更改安排时，已开始的原答保留，本轮不会推进新计划。</p></details>` : ''}
      </section>`;
    if (scroll) $('retestComparison').scrollIntoView({behavior:'smooth',block:'start'});
  }
  function persistAssessment() {
    if (!run || run.state !== 'grading') return;
    const qid = run.question.id;
    const value = {grade,hits:[...document.querySelectorAll(`.qa[data-qid="${qid}"] input[data-q]:checked`)].map(x => Number(x.dataset.seq)),
      point_source:QA.sourceOf(qid),grade_override:QA.overrideOf(qid)};
    try { localStorage.setItem(draftKey() + '-assessment',JSON.stringify(value)); }
    catch (error) { message('本机无法保存评定草稿，请及时保存。'); }
  }
  function renderRun() {
    const q = run.question;
    const intro = `<p class="notice">${run.kind === 'early' ? '本次为提前练习，不推进到期轮次' : `第 ${run.round_index+1} 轮到期复测`} · 开始于 ${time(run.started_at)}</p>`;
    if (run.state === 'saved') {
      $('retestPage').innerHTML = `<h2>本次复测已保存</h2><p>${esc(run.note || '')}</p><p>原答和评定已另存为新记录。</p>
        <a class="linkbtn" href="/reports?plan=${run.plan_id}&session=${run.saved_session_id}">查看初答、订正与复测对比</a>`;
      return;
    }
    if (run.state === 'abandoned') {
      $('retestPage').innerHTML = '<p>本轮已结束，已提交的原答仍保留在计划记录中。</p><a href="/reports?plan=' + run.plan_id + '">回到计划</a>';
      return;
    }
    if (run.state === 'answering') {
      $('retestPage').innerHTML = intro + `<h2>${esc(q.qtype_label || '本次复测')}</h2><div class="stem">${esc(q.stem)}</div>
        ${q.elective ? `<label>本次选做分支 <select id="retestOption" ${run.selected_option ? 'disabled' : ''}>
          <option value="">请选择</option><option value="Ⅰ" ${run.selected_option === 'Ⅰ' ? 'selected' : ''}>Ⅰ</option><option value="Ⅱ" ${run.selected_option === 'Ⅱ' ? 'selected' : ''}>Ⅱ</option></select></label>` : ''}
        ${q.qtype === 'single' ? Object.entries(q.options || {}).map(([k,v]) => `<label class="option"><input type="radio" name="retestChoice" value="${esc(k)}"><span><b>${esc(k)}.</b> ${esc(v)}</span></label>`).join('')
          : '<label for="retestAnswer">本次独立作答</label><textarea id="retestAnswer" maxlength="30000" placeholder="可以先在纸上独立写完，再把原答录入这里。提交前不显示旧答案和批改。"></textarea>'}
        <div class="row"><button type="button" id="lockRetest">完成作答，解锁批改</button><button type="button" class="ghost" data-abandon-run>结束本轮</button></div>
        <p class="notice">提交后原答锁定；看过答案后的修改应另作订正。尚未提交的草稿仅保存在当前浏览器。</p>`;
      try {
        const draft = JSON.parse(localStorage.getItem(draftKey()) || 'null');
        if (draft && $('retestAnswer')) $('retestAnswer').value = draft.answer_text || '';
        if (draft && q.qtype === 'single') [...document.querySelectorAll('[name=retestChoice]')].forEach(el => {el.checked = el.value === draft.answer_text;});
        if (draft && $('retestOption') && !run.selected_option) $('retestOption').value = draft.selected_option || '';
      } catch (error) { /* 坏草稿不影响新作答 */ }
      return;
    }
    let draft = null;
    try { draft = JSON.parse(localStorage.getItem(draftKey() + '-assessment') || 'null'); } catch (error) {}
    grade = draft?.grade || null;
    const model = {...q,answered:true,my_answer:run.answer_text,grade,point_source:draft?.point_source || 'unassessed',
      points:(q.points || []).map(p => ({...p,hit:(draft?.hits || []).includes(p.seq)})),grade_override:draft?.grade_override === true};
    $('retestPage').innerHTML = intro + `<h2>原答已锁定 · 核对本次结果</h2><div class="stem">${esc(q.stem)}</div>
      ${q.qtype === 'single' ? QA.renderReview({...q,my_answer:run.answer_text,answered:true,is_right:run.answer_text===q.correct_answer})
        : QA.render(model,{pick:true,gated:false,title:q.qtype_label}) + QA.renderWork(model)}
      <div class="row"><button type="button" id="saveRetest">保存本次复测</button><button type="button" class="ghost" data-abandon-run>结束本轮</button></div>
      <p class="notice">练习得分优先采用通过对账的 AI 详细批改实得分，无需自评勾选。采分点确认单独记录；未评定或分值待核时不据此判断进步。</p>`;
    const textarea = $('ans-' + q.id);
    if (textarea) textarea.readOnly = true;
    document.querySelectorAll('#retestPage .qa-photo-btn,#retestPage input[type=file]').forEach(el => {el.disabled=true;});
    QA.bind(document);
  }
  function persistAnswer() {
    if (!run || run.state !== 'answering') return;
    const answer = $('retestAnswer')?.value ?? document.querySelector('[name=retestChoice]:checked')?.value ?? '';
    try { localStorage.setItem(draftKey(),JSON.stringify({answer_text:answer,selected_option:$('retestOption')?.value || run.selected_option})); }
    catch (error) { message('本机草稿无法保存，请及时提交本次原答。'); }
  }
  async function loadRun() {
    const id = new URLSearchParams(location.search).get('run');
    if (!id) { $('retestPage').textContent = '请从学习复盘页选择一项复测。'; return; }
    try { const data = await request('/api/retest/runs/' + encodeURIComponent(id)); if(data.run.pilot_url){location.href=data.run.pilot_url;return;} run = data.run; renderRun(); }
    catch (error) { message(error.message,true); $('retestPage').textContent = '无法读取本轮复测。'; }
  }
  document.addEventListener('input', () => {persistAnswer();});
  document.addEventListener('change', () => {persistAnswer(); setTimeout(persistAssessment,0);});
  global.addEventListener('qa-grade-started', event => {if(run) {grading.add(event.detail.qid); if($('saveRetest')) $('saveRetest').disabled=true;}});
  global.addEventListener('qa-grade-finished', event => {if(run) {grading.delete(event.detail.qid); if($('saveRetest')) $('saveRetest').disabled=busy || grading.size>0;}});
  global.addEventListener('qa-graded', event => {if(run) {grade=event.detail.result;persistAssessment();}});
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-enroll-retest],[data-open-plan],[data-start-plan],[data-save-schedule],[data-pause-plan],[data-abandon-run],#lockRetest,#saveRetest');
    if (!button) {if(run?.state==='grading') setTimeout(persistAssessment,0); return;}
    if (busy || grading.size) return;
    busy=true; button.disabled=true; message('正在处理…');
    try {
      if (button.dataset.enrollRetest) {
        const data = await request('/api/retest/plans',{attempt_id:Number(button.dataset.enrollRetest)});
        activePlan=data.plan.id; await loadQueue(); await openPlan(activePlan,true); message('已安排复测，可在计划中调整间隔。');
      } else if (button.dataset.openPlan) await openPlan(button.dataset.openPlan,true);
      else if (button.dataset.startPlan) {
        const data=await request(`/api/retest/plans/${button.dataset.startPlan}/start`,{early:button.dataset.early==='1'});
        location.href=data.run.pilot_url || '/retest?run='+encodeURIComponent(data.run.id);
      } else if (button.dataset.saveSchedule || button.dataset.pausePlan) {
        const id=button.dataset.saveSchedule || button.dataset.pausePlan;
        const body=button.dataset.pausePlan ? {status:button.dataset.status} : {intervals:$('retestIntervals').value.split(/[,，、\s]+/).filter(Boolean).map(Number)};
        if (button.dataset.saveSchedule && $('retestDue').value !== $('retestDue').dataset.original) body.due_on=$('retestDue').value;
        await request(`/api/retest/plans/${id}/schedule`,body); await loadQueue(); message('已更新安排。');
      } else if (button.id==='lockRetest') {
        const answer=$('retestAnswer')?.value ?? document.querySelector('[name=retestChoice]:checked')?.value ?? '';
        const data=await request(`/api/retest/runs/${run.id}/lock`,{answer_text:answer,selected_option:$('retestOption')?.value || run.selected_option});
        run=data.run; renderRun(); message('原答已锁定，现在可以批改或自评。');
      } else if (button.id==='saveRetest') {
        persistAssessment();
        const qid=run.question.id;
        const body=run.question.qtype==='single' ? {} : {grade,point_source:QA.sourceOf(qid),grade_override:QA.overrideOf(qid),
          hits:[...document.querySelectorAll(`.qa[data-qid="${qid}"] input[data-q]:checked`)].map(x=>Number(x.dataset.seq))};
        const data=await request(`/api/retest/runs/${run.id}/save`,body);
        try {localStorage.removeItem(draftKey());localStorage.removeItem(draftKey()+'-assessment');} catch (error) {}
        const saved=await request(`/api/retest/runs/${run.id}`);run=saved.run;renderRun();message('保存成功，原答与计划已同时登记。');
      } else if (button.hasAttribute('data-abandon-run')) {
        await request(`/api/retest/runs/${run.id}/abandon`,{}); await loadRun();
      }
    } catch (error) {message(error.message,true);}
    finally {
      busy=false;if(button.isConnected) button.disabled=false;
      const status=$('retestStatus') || $('retestQueueStatus');
      if(status?.textContent==='正在处理…') message('');
    }
  });
  global.Retests={loadQueue,openPlan};
  if ($('retestPage')) loadRun();
})(window);
