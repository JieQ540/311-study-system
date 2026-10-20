/* 本次复盘的编排；答案与批改的显示只调用 QA.renderReview。 */
(function(global) {
  'use strict';
  let sid = null, serial = 0, data = null;
  const esc = value => QA.esc(value == null ? '' : String(value));
  const keyOf = q => q.attempt_id ? 'a' + q.attempt_id : 'q' + q.question_id;
  const draftKey = id => 'kaoyan-review-summary-' + id;
  const host = () => document.getElementById('reviewWorkspace');
  const questionName = key => {
    return data?.question_refs?.[key]?.label || '题目待核';
  };
  // 旧报告保留原文；仅在展示时把内部标识映射成本场题号，绝不跨场查全局题库。
  function reportHtml(text, refs={}, sessionId=null) {
    const box = document.createElement('div');
    box.innerHTML = md(text);
    const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    const lookup = key => {
      const ref = refs[key];
      return ref && /^[aq]\d+$/.test(ref.key) && typeof ref.label === 'string' ? ref : null;
    };
    function reference(key) {
      const ref = lookup(key);
      const el = document.createElement(ref ? 'a' : 'span');
      el.className = ref ? 'review-report-ref' : 'review-ref-missing';
      el.textContent = ref ? ref.label : '题目待核';
      el.title = ref ? ref.title || ref.label : '这条引用未在本次题单中找到，无法定位原题。';
      if (ref) {
        el.href = '#review-q-' + ref.key;
        el.dataset.reportRef = ref.key;
        el.dataset.session = String(sessionId);
      }
      return el;
    }
    function range(start, end) {
      const fragment = document.createDocumentFragment();
      const prefix = start[0], first = Number(start.slice(1));
      const last = Number(/^[aq]/.test(end) ? end.slice(1) : end);
      const keys = [];
      if ((!/^[aq]/.test(end) || end[0] === prefix) && last >= first && last - first <= 200) {
        for (let i=first; i<=last; i++) keys.push(prefix + i);
      }
      if (!keys.length || keys.some(key => !lookup(key))) {
        const missing = reference(''); missing.textContent = '题目范围待核';
        fragment.append(missing); return fragment;
      }
      const contiguous = keys.every((key, i) => lookup(key).number === lookup(keys[0]).number + i);
      const selected = contiguous && keys.length > 2 ? [keys[0], keys[keys.length - 1]] : keys;
      selected.forEach((key, i) => {
        if (i) fragment.append(document.createTextNode(contiguous ? '—' : '、'));
        fragment.append(reference(key));
      });
      return fragment;
    }
    nodes.forEach(node => {
      if (node.parentElement?.closest('a,code,pre')) return;
      const pattern = /\[\[([aq]\d+)\]\]|(?<![A-Za-z0-9_])([aq]\d+)(?:\s*[-–—~～至]\s*([aq]?\d+))?(?![A-Za-z0-9_])/g;
      const source = node.nodeValue, fragment = document.createDocumentFragment();
      let offset = 0, match;
      while ((match = pattern.exec(source))) {
        fragment.append(document.createTextNode(source.slice(offset, match.index)));
        fragment.append(match[3] ? range(match[2], match[3]) : reference(match[1] || match[2]));
        offset = pattern.lastIndex;
      }
      if (!offset) return;
      fragment.append(document.createTextNode(source.slice(offset)));
      node.replaceWith(fragment);
    });
    return box.innerHTML;
  }
  async function request(url, body) {
    const response = await fetch(url, body === undefined ? {} : {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if (!response.ok) throw new Error('请求失败，请稍后重试。');
    const result = await response.json();
    if (!result.ok) throw new Error(result.error || '请求未完成。');
    return result;
  }
  function sourceLink(key) {
    return `<a href="#review-q-${esc(key)}" data-evidence="${esc(key)}">${esc(questionName(key))} · 查看依据</a>`;
  }
  function taskLink(task) {
    const params = new URLSearchParams({review:String(sid), attempt:String(task.attempt_id), finding:task.finding_id});
    return '/daily?' + params;
  }
  function findingHtml(f) {
    return `<div class="review-finding ${f.dismissed ? 'is-dismissed' : ''}" data-finding="${esc(f.id)}">
      <span class="origin">${esc(f.origin_label)}</span><p>${esc(f.problem)}</p>
      ${f.quote ? `<blockquote>${esc(f.quote)}</blockquote>` : ''}
      <div class="row">${sourceLink(f.question_key)}
        <button type="button" class="ghost mini" data-dismiss="${esc(f.id)}" data-dismissed="${f.dismissed ? '0' : '1'}">${f.dismissed ? '恢复这条判断' : '判断不符合实际'}</button></div>
    </div>`;
  }
  function questionHtml(q, index) {
    const key = keyOf(q), result = q.score_result || {};
    const state = q.missing ? '题目缺失' : q.answered === false ? '未作答' : q.qtype === 'single'
      ? (q.is_right ? '答对' : '答错') : result.scored
        ? `${QA.fmt(result.score)} / ${QA.fmt(result.full)} 分 · ${result.score_source_label || q.point_source_label || '来源未记录'}`
        : (result.reason || '尚未评定');
    return `<details class="review-question" id="review-q-${esc(key)}" ${(data.analysis.focus_keys || []).includes(key) ? 'open' : ''}>
      <summary>${esc(q.question_label || '第 ' + (index + 1) + ' 题')} · ${esc(q.qtype_label || '题目')} · ${esc(q.outline_name || '考点未标注')}
        <span class="review-score">${esc(state)}</span></summary>
      ${QA.renderReview(q)}
      ${q.attempt_id && !q.missing ? `<p><a class="linkbtn mini" href="/daily?review=${sid}&attempt=${q.attempt_id}">订正本题</a>
        <button type="button" class="ghost mini" data-enroll-retest="${q.attempt_id}">安排间隔复测</button></p>` : ''}
    </details>`;
  }
  function render() {
    const a = data.analysis, notes = data.notes, report = data.report;
    const dismissed = a.findings.filter(f => f.dismissed);
    const summary = notes.summary !== null ? notes.summary : (report?.content || '');
    const m = data.metrics;
    const rawSuggestions = data.questions.filter(q => q.answered && !q.missing && q.score_result?.score_source !== 'ai_graded'
        && ['ai_unconfirmed','unassessed'].includes(q.point_source))
      .map(q => ({key:keyOf(q), value:q.score_result?.ai}))
      .filter(x => Number.isFinite(x.value?.raw_score) && Number.isFinite(x.value?.raw_full)
        && x.value.raw_full > 0 && x.value.raw_score >= 0 && x.value.raw_score <= x.value.raw_full);
    host().innerHTML = `<div class="review-head">
      <div><p class="eyebrow">本次复盘 · ${esc(data.session.date)}</p><h2>看清问题，开始下一步</h2>
        <p class="notice">${esc(data.session.mode || '练习')} · ${data.questions.length} 项题目 / 作答记录</p></div>
      <button type="button" data-generate>${report ? '重新生成 AI 总结' : '生成 AI 总结'}</button>
    </div>
    <div class="notice" role="status" id="reviewStatus"></div>
    <section class="review-section"><h3>本次概览</h3>
      <p>客观题 ${m.objective_right || 0} / ${m.objective_total || 0} 道答对
        ${subjHtml({metrics:m, subjective_full:m.subjective_full, subjective_score:m.subjective_score,
          subjective_source:m.subjective_source, subjective_n:m.subjective_count, subjective_scored_n:m.subjective_scored_n})}</p>
      ${rawSuggestions.length ? `<p class="notice">尚未确认的 AI 原始建议分（仅供参考）：${rawSuggestions.map(x =>
        `${esc(questionName(x.key))} · ${QA.fmt(x.value.raw_score)}/${QA.fmt(x.value.raw_full)}`).join('；')}。未计入已确认采分点得分。</p>` : ''}
      ${!data.has_question_set ? '<p class="notice">这份旧记录没有完整题单，下方列出已保存作答，无法判断当时还有哪些题未做。</p>' : ''}
      <p class="notice">问题与任务来自本次已有作答和批改；AI 总结按需生成。引句核对只证明原文存在，学科判断仍可能有误。</p>
    </section>
    <section class="review-section"><h3>优先改进 · ${a.tasks.length} 项</h3>
      <div class="review-tasks">${a.tasks.map((t,i) => `<article class="review-task">
        <span class="notice">任务 ${i+1} · ${esc(t.origin_label)}</span><h4>${esc(t.title)}</h4>
        <p>${esc(t.instruction)}</p><p class="criterion">完成标准：${esc(t.criterion)}</p>
        ${sourceLink(t.question_key)}<br><a class="linkbtn" data-practice href="${esc(taskLink(t))}">开始这项订正</a>
      </article>`).join('')}</div>
      ${a.tasks.length ? '<p class="notice">进入对应的那次原题作答订正并另存新记录，随后自动安排复测。到期队列和前后对比见上方“间隔复测”。</p>'
        : '<p class="notice">当前没有足够依据安排具体改进任务。可以展开原答核对，或先完成批改；不会为了凑任务推断错因。</p>'}
    </section>
    <section class="review-section"><h3>本次总结</h3>
      ${report?.content_stale ? `<div class="note">${staleTip(report.content_stale_kind)}</div>` : ''}
      ${summary ? `<div class="report">${reportHtml(summary, data.question_refs, sid)}</div>` : '<p class="notice">尚未生成 AI 总结；上面的题目、问题与任务已可直接查看。点“生成 AI 总结”会调用一次 AI。</p>'}
      ${notes.summary !== null ? '<p class="notice">正在显示你的修正版，重新生成 AI 总结不会覆盖。</p>' : ''}
      <details id="reviewEdit"><summary>修改总结（可选）</summary>
        <label for="reviewSummary">你的修正版</label><textarea id="reviewSummary" class="review-summary-text" maxlength="6000"></textarea>
        <div class="row"><button type="button" data-save-summary>保存修正版</button>
          <button type="button" class="ghost" data-reset-summary>使用 AI 原版</button></div>
        <p class="notice" id="reviewDraftStatus"></p>
      </details>
    </section>
    <section class="review-section"><h3>错因与证据</h3>
      ${a.groups.map(g => `<details ${a.groups.length < 4 ? 'open' : ''}><summary><b>${esc(g.kind)}</b> · ${g.count} 项作答</summary>
        ${g.findings.map(findingHtml).join('')}</details>`).join('') || '<p class="notice">尚无可归纳的问题记录。</p>'}
      ${dismissed.length ? `<details><summary>你已排除的判断（${dismissed.length} 条）</summary>${dismissed.map(findingHtml).join('')}</details>` : ''}
    </section>
    <section class="review-section"><h3>逐题回看 · ${data.questions.length} 项</h3>
      <p class="notice">优先任务对应的题目默认展开；每次作答分别保留，未作答题明确标记。</p>
      ${data.questions.map(questionHtml).join('') || '<p class="notice">未找到作答与题单记录。</p>'}
    </section>`;
    const textarea = document.getElementById('reviewSummary');
    textarea.value = summary;
    try {
      const draft = localStorage.getItem(draftKey(sid));
      if (draft !== null) {
        textarea.value = draft;
        document.getElementById('reviewDraftStatus').textContent = '已恢复尚未保存的总结草稿。';
      }
    } catch (e) { /* 存储不可用不影响读取服务器记录 */ }
  }
  async function open(id, scroll=false) {
    const version = ++serial;
    sid = Number(id);
    host().hidden = false;
    host().innerHTML = '<p class="notice">正在读取本次题目、原答与批改…</p>';
    try {
      const result = await request('/api/review/session/' + sid);
      if (version !== serial) return;
      data = result; render();
      if (scroll) host().scrollIntoView({behavior:'smooth', block:'start'});
    } catch (e) {
      if (version !== serial) return;
      host().innerHTML = `<p class="review-error">${esc(e.message)}</p><button type="button" data-retry>重试读取</button>`;
    }
  }
  function message(value, error=false) {
    const el = document.getElementById('reviewStatus');
    if (el) { el.textContent = value; el.className = error ? 'review-error' : 'notice'; }
  }
  document.addEventListener('input', event => {
    if (event.target.id !== 'reviewSummary') return;
    try {
      localStorage.setItem(draftKey(sid), event.target.value);
      document.getElementById('reviewDraftStatus').textContent = '草稿已保存在本机；点“保存修正版”写入这次复盘。';
    } catch (e) {
      document.getElementById('reviewDraftStatus').textContent = '本机草稿无法保存，请及时点“保存修正版”。';
    }
  });
  document.addEventListener('click', async event => {
    const reportRef = event.target.closest('[data-report-ref]');
    if (reportRef && (host()?.contains(reportRef) || document.getElementById('list')?.contains(reportRef))) {
      event.preventDefault();
      const targetSid = Number(reportRef.dataset.session), key = reportRef.dataset.reportRef;
      if (!Number.isInteger(targetSid) || targetSid <= 0) return;
      if (Number(data?.session.id) !== targetSid || !host()?.querySelector('.review-question')) await open(targetSid, true);
      if (Number(data?.session.id) !== targetSid) return;
      const question = document.getElementById('review-q-' + key);
      if (question && host().contains(question)) { question.open = true; question.scrollIntoView({behavior:'smooth', block:'start'}); }
      return;
    }
    const target = event.target.closest('[data-evidence],[data-dismiss],[data-save-summary],[data-reset-summary],[data-generate],[data-retry]');
    if (!target || !host()?.contains(target)) return;
    if (target.dataset.evidence) {
      event.preventDefault();
      const question = document.getElementById('review-q-' + target.dataset.evidence);
      if (question) { question.open = true; question.scrollIntoView({behavior:'smooth', block:'start'}); }
      return;
    }
    if (target.hasAttribute('data-retry')) { await open(sid); return; }
    if (target.hasAttribute('data-generate')) { await genReport(sid, target); return; }
    target.disabled = true;
    message('保存中…');
    const editingSid = sid;
    try {
      const body = target.dataset.dismiss ? {finding_id:target.dataset.dismiss, dismissed:target.dataset.dismissed === '1'}
        : {summary:target.hasAttribute('data-reset-summary') ? null : document.getElementById('reviewSummary').value};
      await request('/api/review/session/' + editingSid + '/notes', body);
      if ('summary' in body) { try { localStorage.removeItem(draftKey(editingSid)); } catch (e) {} }
      if (sid !== editingSid) return;
      await open(editingSid);
      message('已保存。你的修正会在重新生成后保留。');
    } catch (e) { message(e.message, true); target.disabled = false; }
  });
  global.Review = {open, current:() => sid, reportHtml};
})(window);
