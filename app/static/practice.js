/* 专项页面只编排题目与保存；答案、作答、批改均由 QA 共用组件实现。 */
'use strict';
const $ = id => document.getElementById(id);
const DRAFT_KEY = 'kaoyan-practice-v1';
let questions = [], seen = [], grades = {}, options = null, currentType = 'essay';
let loading = false, saving = false, dirty = false, savedFingerprint = null, submission = null;
let optionVersion = 0;
const grading = new Set();
const typeName = {analysis:'辨析题', short:'简答题', essay:'分析论述题'};

function filters() {
  return {qtype:document.querySelector('input[name=qtype]:checked').value,
    board_id:$('board').value || null, chapter_id:$('chapter').value || null, count:Number($('count').value)};
}
async function request(url, body) {
  const response = await fetch(url, body === undefined ? {} : {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  if (!response.ok) throw new Error('请求未完成，请稍后重试（' + response.status + '）');
  const result = await response.json();
  if (!result.ok) throw new Error(result.error || '请求未完成');
  return result;
}
function controls() {
  const busy = loading || saving || grading.size > 0;
  $('start').disabled = busy || !options || options.available === 0;
  $('reset').disabled = busy;
  $('save').disabled = busy || questions.length === 0;
  document.querySelectorAll('input[name=qtype],#board,#chapter,#count').forEach(el => { el.disabled = loading || saving; });
  $('start').textContent = questions.length ? '换下一组' : '开始练习';
}
function fillOptions(id, rows, chosen, allLabel) {
  $(id).replaceChildren(new Option(allLabel, ''));
  rows.forEach(row => $(id).add(new Option(
    (id === 'chapter' && !$('board').value && row.board_name ? row.board_name + ' · ' : '')
      + row.name + '（' + row.count + '）', String(row.id))));
  $(id).value = chosen || '';
}
async function refreshOptions() {
  const version = ++optionVersion, f = filters();
  options = null; controls();
  $('message').textContent = '正在读取题库…';
  try {
    const query = new URLSearchParams({qtype:f.qtype, board_id:f.board_id || '', chapter_id:f.chapter_id || ''});
    const result = await request('/api/practice/options?' + query);
    if (version !== optionVersion) return;
    options = result;
    fillOptions('board', result.boards, f.board_id, '全部科目');
    fillOptions('chapter', result.chapters, f.chapter_id, '全部章节');
    $('available').textContent = result.available;
    $('message').textContent = result.available ? '每次只抽所选题型，本轮换题不重复；重新开始后可以重练。'
      : '当前条件下没有这类真题，请调整科目或章节。';
    persist();
  } catch (e) {
    if (version !== optionVersion) return;
    $('available').textContent = '—'; $('message').textContent = e.message;
  } finally { if (version === optionVersion) controls(); }
}
function items() {
  const sources = QA.collectSources($('paper')), selected = QA.collectOptions($('paper'));
  const overrides = QA.collectOverrides($('paper'));
  return questions.map(q => ({id:q.id,
    hits:[...document.querySelectorAll(`.qa[data-qid="${q.id}"] input[data-q]:checked`)].map(el => Number(el.dataset.seq)),
    answer_text:($('ans-' + q.id)?.value || '').trim() || null, grade:grades[q.id] || null,
    point_source:sources[q.id] || null, selected_option:selected[q.id] || null,
    grade_override:overrides[q.id] === true}));
}
function persist() {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify({filters:filters(), questions, seen, grades, currentType,
      items:items(), dirty, savedFingerprint, submission}));
  } catch (e) { $('saveMessage').textContent = '浏览器未能保存草稿，请及时保存到练习日志。'; }
}
function changed() { dirty = true; $('saved').hidden = true; persist(); }
function modelQuestion(q, prior) {
  let extra = {}; try { extra = typeof q.extra === 'string' ? JSON.parse(q.extra) : q.extra || {}; } catch (e) {}
  const item = prior || {};
  return {...q, full_score:q.full_score ?? extra.full_score ?? null,
    answer_text:extra.answer_text || '', explain:extra.explain || '',
    points:(q.points || []).map(p => ({...p, hit:(item.hits || []).includes(p.seq)})),
    my_answer:item.answer_text || '', grade:item.grade || grades[q.id] || null,
    point_source:item.point_source || null, selected_option:item.selected_option || null,
    grade_override:item.grade_override === true};
}
function render(priorItems = []) {
  const byId = new Map(priorItems.map(item => [item.id, item]));
  $('paperArea').hidden = false;
  $('paperTitle').textContent = typeName[currentType] + ' · 本组 ' + questions.length + ' 道';
  $('paper').innerHTML = questions.map((q, i) => {
    const model = modelQuestion(q, byId.get(q.id));
    return `<section class="card question"><div class="question-top"><b>第 ${i + 1} 题</b>
      <span class="tag">${QA.esc(typeName[q.qtype])}</span>
      <span class="tag">${q.year ? QA.esc(q.year) + ' 年真题' : '真题 · 年份未标注'}${q.number ? ' · 原第 ' + QA.esc(q.number) + ' 题' : ''}</span>
      <span class="tag">${QA.fmt(model.full_score)} 分</span></div>
      <div class="stem">${QA.esc(q.stem)}</div>
      ${QA.render(model, {pick:true, gated:!model.grade, title:typeName[q.qtype]})}
      ${QA.renderWork(model)}</section>`;
  }).join('') || '<div class="card empty">本轮已抽完符合条件的真题。可以调整条件，或点「重新开始本轮」重新练习。</div>';
  $('savebar').hidden = questions.length === 0;
  $('toggleAnswers').hidden = questions.length === 0;
  $('toggleAnswers').textContent = '显示答案解析';
  controls();
}
async function generate(reset = false) {
  if (loading || saving || grading.size) return;
  if (dirty && !confirm('当前作答尚未保存。确定放弃这些修改并更换题目吗？')) return;
  loading = true; controls();
  $('message').textContent = '正在抽取真题…';
  try {
    const f = filters();
    const result = await request('/api/practice/generate', {...f, exclude_ids:reset ? [] : seen});
    questions = result.questions; currentType = f.qtype; grades = {};
    seen = [...new Set([...(reset ? [] : seen), ...questions.map(q => q.id)])];
    dirty = false; savedFingerprint = null; submission = null;
    $('saved').hidden = true;
    $('paperNotice').textContent = result.notice || '';
    $('paperNotice').hidden = !result.notice;
    $('message').textContent = `本组 ${result.actual} 道真题；当前条件下本轮还剩 ${result.remaining} 道未抽取。`;
    $('saveMessage').textContent = '作答与批改会一起保存在练习日志。';
    render(); persist();
  } catch (e) { $('message').textContent = e.message; }
  finally { loading = false; controls(); }
}
function localDate() {
  const now = new Date();
  return [now.getFullYear(), String(now.getMonth() + 1).padStart(2,'0'), String(now.getDate()).padStart(2,'0')].join('-');
}
async function save() {
  if (saving || loading || grading.size) return;
  const subjective = items().filter(item => item.answer_text || item.grade || item.point_source || item.hits.length);
  if (!subjective.length) { $('saveMessage').textContent = '请先填写作答、批改，或完成采分点自评。'; return; }
  const body = {mode:'主观题专项', date:localDate(), question_ids:questions.map(q => q.id), single:[], subjective,
    note:typeName[currentType] + '专项 · ' + questions.length + ' 道真题'};
  const fingerprint = JSON.stringify(body);
  if (fingerprint === savedFingerprint) { $('saveMessage').textContent = '这份作答已经保存。'; return; }
  if (!submission || submission.fingerprint !== fingerprint) {
    submission = {fingerprint, id:crypto.randomUUID()};
  }
  body.submission_id = submission.id;
  saving = true; controls(); persist(); $('saveMessage').textContent = '正在保存作答…';
  try {
    const result = await request('/api/save', body);
    savedFingerprint = fingerprint; dirty = false;
    $('saved').innerHTML = `已保存 ${result.saved.subjective} 道作答。
      ${QA.practiceScoreText(result.saved)}。
      <a href="/logs?date=${encodeURIComponent(result.date)}&sid=${result.saved.session_id}">查看本次练习日志 →</a>`;
    $('saved').hidden = false;
    $('saveMessage').textContent = '保存成功，可以继续下一组；修改本题后也可以再次保存。';
    QA.revealAll($('paper')); persist();
  } catch (e) { $('saveMessage').textContent = '保存失败：' + e.message + '，草稿已保留，可重试。'; }
  finally { saving = false; controls(); }
}
function invalidate(qid) {
  if (!grades[qid]) return;
  const box = document.querySelector(`.qa[data-qid="${qid}"]`);
  if (!box) return;
  const model = JSON.parse(box.dataset.model);
  const keepSelf = model.point_source === 'self_confirmed';
  const checked = new Set([...box.querySelectorAll('input[data-q]:checked')].map(el => Number(el.dataset.seq)));
  delete grades[qid];
  model.grade = null; model.grade_override = false;
  model.point_source = keepSelf ? 'self_confirmed' : 'unassessed';
  model.points = model.points.map(p => ({...p, hit:keepSelf && checked.has(Number(p.seq))}));
  box.outerHTML = QA.render(model, {pick:true, gated:true, title:typeName[currentType]});
  const msg = document.querySelector(`[data-msg="${qid}"]`);
  if (msg) msg.textContent = '作答已修改，请重新批改或核对采分点。';
}
QA.bind(document);
$('start').addEventListener('click', () => generate());
$('reset').addEventListener('click', () => generate(true));
$('save').addEventListener('click', save);
$('toggleAnswers').addEventListener('click', () => {
  if (QA.anyHidden($('paper'))) { QA.revealAll($('paper')); $('toggleAnswers').textContent = '收起答案解析'; }
  else { QA.hideAll($('paper')); $('toggleAnswers').textContent = '显示答案解析'; }
});
document.querySelectorAll('input[name=qtype]').forEach(el => el.addEventListener('change', refreshOptions));
$('board').addEventListener('change', () => { $('chapter').value = ''; refreshOptions(); });
$('chapter').addEventListener('change', refreshOptions);
$('count').addEventListener('change', persist);
$('paper').addEventListener('input', event => {
  if (event.target.matches('textarea[id^="ans-"]')) invalidate(Number(event.target.id.slice(4)));
  changed();
});
$('paper').addEventListener('change', () => queueMicrotask(changed));
$('paper').addEventListener('click', event => {
  if (event.target.closest('.qa-adopt-btn,.qa-zero-btn,.qa-override-btn')) queueMicrotask(changed);
});
window.addEventListener('qa-grade-started', event => {
  grading.add(event.detail.qid); controls();
  const ta = $('ans-' + event.detail.qid); if (ta) ta.readOnly = true;
});
window.addEventListener('qa-grade-finished', event => {
  grading.delete(event.detail.qid); controls();
  const ta = $('ans-' + event.detail.qid); if (ta) ta.readOnly = false;
});
window.addEventListener('qa-graded', event => {
  const {qid, result, usage} = event.detail;
  grades[qid] = result;
  if (result.recognized) $('ans-' + qid).value = result.recognized;
  if (usage) $('saveMessage').textContent = QA.usageText(usage);
  changed();
});
window.addEventListener('beforeunload', event => {
  persist();
  if (grading.size) { event.preventDefault(); event.returnValue = ''; }
});
(async function init() {
  let draft = null;
  try { draft = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null'); } catch (e) {}
  if (draft && Array.isArray(draft.questions) && draft.questions.length <= 5 && typeName[draft.currentType]) {
    const f = draft.filters || {};
    const radio = [...document.querySelectorAll('input[name=qtype]')].find(el => el.value === f.qtype);
    if (radio) radio.checked = true;
    $('count').value = [1,2,3,5].includes(f.count) ? String(f.count) : '1';
    questions = draft.questions; seen = Array.isArray(draft.seen) ? draft.seen : [];
    grades = draft.grades || {}; currentType = draft.currentType;
    dirty = draft.dirty === true; savedFingerprint = draft.savedFingerprint || null; submission = draft.submission || null;
    if (questions.length) render(draft.items || []);
    try {
      const result = await request('/api/practice/options?qtype=' + encodeURIComponent(f.qtype || 'essay'));
      fillOptions('board', result.boards, f.board_id, '全部科目');
      fillOptions('chapter', result.chapters, f.chapter_id, '全部章节');
    } catch (e) { $('message').textContent = e.message; }
  }
  await refreshOptions();
})();
