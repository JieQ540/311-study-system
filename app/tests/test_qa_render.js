/* 共用组件 qa-answer.js 的渲染回归测试（纯字符串断言，不需要浏览器、不联网）。
 *
 * 为什么用 node 直跑：这两个 bug 都出在**拼字符串**的逻辑上（小问分组、折叠门），
 * 用浏览器截图只能看到"看起来对"，无法断言"每个采分点在全文中只出现一次"。
 * 组件本身不依赖 DOM（injectCSS 在无 document 时直接返回），所以能这样直接加载。
 *
 * 跑法：
 *     node tests/test_qa_render.js
 * 退出码非 0 表示有断言失败（可挂到 CI / 提交前手跑）。
 *
 * 覆盖：
 *   ① 每个小问里**只渲染自己的采分点**（用户截图报的"两道题答案重复"）
 *   ② 不属于任何小问的采分点不丢，单列「未归入小问的采分点」
 *   ③ 小问标题右侧的分数按本小问的点算（0 / 8 分、0 / 7 分）
 *   ④ gated:true → 答案折进默认收起的折叠门；不传 → 平铺（日志/复盘页行为不变）
 *   ⑤ 分值不齐 / 没有小问结构时不炸，且不假装有分数
 */
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'static', 'qa-answer.js');
global.window = global;                      // 组件是 (function(global){...})(window)
(0, eval)(fs.readFileSync(SRC, 'utf8'));     // 直接求值，不走模块系统（文件不是模块）
const QA = global.QA;

const FAILED = [];
function check(name, cond, extra) {
  console.log((cond ? '  [PASS] ' : '  [FAIL] ') + name + (extra ? `  ${extra}` : ''));
  if (!cond) FAILED.push(name);
}
const count = (hay, needle) => hay.split(needle).length - 1;

// ---------- 夹具：就是截图里那道「篮球队」题的结构（4 点 8 分 + 3 点 7 分）----------
const SECTIONS = [
  {key: '1', label: '有效练习的条件', score: 8, desc: '结合材料指出并简要说明方案中体现的有效练习条件'},
  {key: '2', label: '阶段判断与特点', score: 7, desc: '先判断阶段，再说明该阶段动作的主要特点'}
];
const POINTS = [
  {seq: 1, claim: '练习前讲解并示范动作要领', evidence: '材料里的讲解示范', section: '1', score: 2},
  {seq: 2, claim: '练习时间适当分散', evidence: '每天两次', section: '1', score: 2},
  {seq: 3, claim: '每次练习后及时提供反馈', evidence: '短反馈', section: '1', score: 2},
  {seq: 4, claim: '练习要求明确且有计划和持续性', evidence: '连续一个月', section: '1', score: 2},
  {seq: 5, claim: '队员已进入操作技能的自动化阶段', evidence: '一个月后', section: '2', score: 3},
  {seq: 6, claim: '动作高度协调、稳定、灵活', evidence: '', section: '2', score: 2},
  {seq: 7, claim: '动觉控制增强，视觉监督减弱', evidence: '一边运球一边观察', section: '2', score: 2}
];
const STATS = {
  n: 7, n_scored: 7, scored: true, sum_score: 15, full_score: 15,
  match: true, elective: false, options: [], has_sections: true
};
const Q = {
  id: 1219, qtype: 'short', source: 'AI生成', stem: '某校篮球队教练……',
  full_score: 15, sections: SECTIONS, points: POINTS, score_stats: STATS,
  answer_note: '第（1）问答出四点即可；第（2）问须结合材料。',
  answer_text: '（1）有效练习的条件：……\n（2）该队员处于自动化阶段……',
  explain: '本题考查操作技能的形成与训练。'
};

// 把渲染结果按 data-sec 切成块，便于逐块断言
function blocksOf(html) {
  const out = {};
  html.split('<div class="qa-sec" data-sec="').slice(1).forEach(seg => {
    const key = seg.slice(0, seg.indexOf('"'));
    out[key] = seg;
  });
  return out;
}
// data-model 是整块的**状态快照**（applyGrade 重渲染要用），采分点在里面也存了一份。
// 断言"页面上出现几次"时必须先把它摘掉，否则每个点都会被算成两次 —— 这不是重复渲染。
const visibleOf = html => html.replace(/data-model="[^"]*"/g, '');

console.log('① 小问分组：每个小问只渲染自己的采分点');
const html = QA.render(Q, {pick: true, gated: true});
const blocks = blocksOf(html);
check('渲染出两个小问块', Object.keys(blocks).length === 2, Object.keys(blocks).join(','));
check('小问 1 含自己的 4 个点', POINTS.slice(0, 4).every(p => blocks['1'].includes(p.claim)));
check('小问 1 **不含**小问 2 的点', !POINTS.slice(4).some(p => blocks['1'].includes(p.claim)));
check('小问 2 含自己的 3 个点', POINTS.slice(4).every(p => blocks['2'].includes(p.claim)));
check('小问 2 **不含**小问 1 的点（就是截图里的重复）',
      !POINTS.slice(0, 4).some(p => blocks['2'].includes(p.claim)));
check('每个采分点在全文中只出现一次',
      POINTS.every(p => count(visibleOf(html), p.claim) === 1),
      POINTS.map(p => `${p.seq}:${count(visibleOf(html), p.claim)}`).join(' '));

console.log('② 小问分数按本小问的点算');
check('小问 1 显示 0 / 8 分', blocks['1'].includes('0 / 8 分'));
check('小问 2 显示 0 / 7 分', blocks['2'].includes('0 / 7 分'));

console.log('③ 没有小问归属的点不能丢');
const q2 = {...Q, points: POINTS.concat([{seq: 8, claim: '孤立的一点', section: null, score: 0}])};
const html2 = QA.render(q2, {pick: true, gated: true});
const blocks2 = blocksOf(html2);
check('多出一个「未归入小问」块', !!blocks2['__other__'], Object.keys(blocks2).join(','));
check('孤立点在那一块里', (blocks2['__other__'] || '').includes('孤立的一点'));
check('孤立点没有被重复渲染', count(visibleOf(html2), '孤立的一点') === 1);

console.log('④ 折叠门（gated）');
check('gated:true → 有 .qa-gate', html.includes('<details class="qa-gate">'));
check('折叠门**默认收起**（没有 open 属性）', !/<details class="qa-gate"[^>]*\sopen/.test(html));
// 两边都要用同一个字符串取下标：visibleOf 摘掉了 data-model，位置会整体前移
check('采分点在折叠门里面',
      visibleOf(html).indexOf(POINTS[0].claim) > visibleOf(html).indexOf('qa-gate-body'));
check('标题写明要点数与满分', html.includes('采分点自评（7 个要点 · 15 分）'), '');
check('参考答案与解析也在门里',
      html.includes('参考答案（做完再对照）') && html.includes('解析'));
check('不传 gated（日志/复盘页）→ 平铺，没有折叠门',
      !QA.render(Q, {pick: false, showHit: true}).includes('qa-gate'));
check('不传 gated 时采分点仍然完整可见',
      POINTS.every(p => QA.render(Q, {pick: false}).includes(p.claim)));

console.log('⑤ 分值不齐 / 无小问结构时不炸');
const q3 = {...Q, points: POINTS.map(p => ({...p, score: null})),
            score_stats: {...STATS, n_scored: 0, scored: false, sum_score: 0}};
const html3 = QA.render(q3, {pick: true, gated: true});
check('分值待核时仍渲染出全部 7 个点', POINTS.every(p => html3.includes(p.claim)));
check('标题如实写「分值待核」', html3.includes('分值待核'));
check('顶部不假装有分数', html3.includes('<b>—</b>'));
const q4 = {...Q, sections: [], points: POINTS.map(p => ({...p, section: '*'})),
            score_stats: {...STATS, has_sections: false}};
const html4 = QA.render(q4, {pick: true});
check('没有小问结构 → 平铺，且 7 个点都在', POINTS.every(p => html4.includes(p.claim)));
check('没有小问结构 → 没有小问块', !html4.includes('qa-sec-head'));

console.log('⑥ 勾选命中的折合分');
const q5 = {...Q, points: POINTS.map(p => ({...p, hit: p.section === '2'}))};
check('只勾小问 2 的 3 个点 → 7 分', QA.sumHit(QA.buildModel(q5)) === 7, String(QA.sumHit(QA.buildModel(q5))));
// 「答出任意 N 点」：逐点之和可能大于满分，折合要按满分封顶（与服务端 attempt_score 一致）
const qCap = {...Q, full_score: 5,
              points: [{seq: 1, claim: 'a', section: '*', score: 5, hit: true},
                       {seq: 2, claim: 'b', section: '*', score: 5, hit: true}],
              sections: [], score_stats: {elective: false, fold_ok: true}};
check('逐点之和超过满分时按满分封顶（不会算出 10/5）',
      QA.sumHit(QA.buildModel(qCap)) === 5, String(QA.sumHit(QA.buildModel(qCap))));

console.log('⑦ 评分来源行（设计 §2.2）');
const q6 = {...Q, points: []};
const h6 = QA.render(q6, {pick: true, gated: true});
check('没有采分点时明确写「暂不能逐点自评」', h6.includes('暂不能逐点自评'));
check('没有采分点时**不给**「0 点命中，完成自评」按钮（免得把"没法自评"记成 0 分）',
      !h6.includes('qa-zero-btn'));
check('没有采分点时提示可以先 AI 批改', h6.includes('AI 批改'));
const h7 = QA.render(Q, {pick: true, gated: true});
check('有采分点时才给两个来源动作',
      h7.includes('qa-zero-btn') && h7.includes('data-srcrow'));
check('未评定时来源行写「尚未评定」', h7.includes('尚未评定'));
check('回看页（pick:false）不给编辑按钮',
      !QA.render({...Q, point_source: 'legacy_unknown'}, {pick: false}).includes('qa-zero-btn'));
check('回看页如实显示来源',
      QA.render({...Q, point_source: 'legacy_unknown'}, {pick: false}).includes('来源不明（旧记录）'));

// ------------- 对账结果与人工确认出口（2026-10-09，设计 §3 修订 / §5 D）-------------
console.log('⑦b 对账失败的重批记录与「人工确认」按钮');
// 没通过对账：红条 + 确认按钮都在，且把重批记录说出来（分数变了、钱也多花了，不能是黑箱）
const qBad = {...Q, grade: {schema_version: 2, score: 12, full_score: 15,
  verification: {status: 'needs_review', errors: ['总分对不上：声称 12，应为 10'],
                 retry: {attempted: true, second_status: 'needs_review',
                         first_errors: ['总分对不上：声称 13，应为 10']}}}};
const hBad = QA.render(qBad, {pick: true, gated: true});
check('对账失败 → 红条明说「不计入可信参考分」',
      hBad.includes('AI 分数未通过对账') && hBad.includes('不计入可信参考分'));
check('对账失败 → 给出「我确认这份分账」按钮', hBad.includes('qa-override-btn'));
check('对账失败 → 说出「已自动重批一次」与首轮问题',
      hBad.includes('已自动重批一次') && hBad.includes('声称 13'));
check('重批那次没返回可用结果时如实说（下面是首轮的批改）',
      QA.render({...Q, grade: {schema_version: 2, score: 12, full_score: 15,
        verification: {status: 'needs_review', errors: ['总分对不上'],
                       retry: {attempted: true, second_status: 'json_error',
                               first_errors: ['总分对不上']}}}},
        {pick: true}).includes('没返回可用结果'));
// 已人工确认（库里存的盖章）：说清"已人工确认"并原样摆出被覆盖的问题
const hOv = QA.render({...qBad, grade_override: true}, {pick: true, gated: true});
check('已确认 → 按钮变成「已确认」（可取消）', hOv.includes('已确认计入可信参考分'));
check('已确认 → 明说与「机器对账通过」分开统计', hOv.includes('分开统计'));
const hStamped = QA.render({...Q, grade: {schema_version: 2, score: 12, full_score: 15,
  verification: {status: 'needs_review', errors: ['总分对不上']},
  override: {status: 'human_ok', at: '2026-10-09T10:00:00',
             errors_overridden: ['总分对不上：声称 12，应为 10']}}}, {pick: false});
check('回看已盖章的 → 写「这份分账已人工确认」并列出被覆盖的错误原文',
      hStamped.includes('这份分账已人工确认') && hStamped.includes('声称 12，应为 10'));
// 机器已对账通过：没有可确认的东西，不给按钮（免得变成随手一点的习惯动作）
const hOk = QA.render({...Q, grade: {schema_version: 2, score: 10, full_score: 15,
  verification: {status: 'v2_ok', expected: 10}}}, {pick: true, gated: true});
check('已对账通过 → 不给「人工确认」按钮', !hOk.includes('qa-override-btn'));
check('已对账通过 → 照旧写「分数已对账」', hOk.includes('分数已对账'));
check('collectOverrides 只收下确认为 true 的题',
      (() => { const f = {querySelectorAll: () => [{dataset: {qid: '1',
                  model: JSON.stringify({grade_override: true})}},
                {dataset: {qid: '2', model: JSON.stringify({grade_override: false})}}]};
        const r = QA.collectOverrides(f);
        return r['1'] === true && r['2'] === undefined; })(),
      '');

// ---------------- 费用文案（批次 4）：token 必有、金额只在配置了价目时才有 ----------------
// 为什么由服务端给金额：以前四个页面各自写 `tokens/1e6*1 + tokens/1e6*4`，
// 把 ¥1/¥4 硬编码在前端 —— 官方调价或换模型后，页面会继续报一个看着很精确的错数字。
check('未配置价目：只显示 token、不出现任何金额',
   (() => { const s = QA.usageText({prompt_tokens: 1234, completion_tokens: 567,
                                    pricing_configured: false, cost: null});
            return s.includes('1234/567') && !s.includes('≈') && !s.includes('CNY'); })(),
   QA.usageText({prompt_tokens: 1234, completion_tokens: 567, pricing_configured: false, cost: null}));
check('配置了价目：写出金额、币种与生效日期（标明是估算）',
   (() => { const s = QA.usageText({prompt_tokens: 1000000, completion_tokens: 0,
                                    pricing_configured: true, cost: 2.0, currency: 'CNY',
                                    pricing_effective_date: '2026-09-01'});
            return s.includes('≈ 2.0000 CNY') && s.includes('2026-09-01'); })(),
   QA.usageText({prompt_tokens: 1000000, completion_tokens: 0, pricing_configured: true,
                 cost: 2.0, currency: 'CNY', pricing_effective_date: '2026-09-01'}));
check('推理 token 只在明确要求时才显示（避免每处都堆一长串）',
   !QA.usageText({prompt_tokens: 1, completion_tokens: 2, reasoning_tokens: 99}).includes('推理')
   && QA.usageText({prompt_tokens: 1, completion_tokens: 2, reasoning_tokens: 99}, true).includes('推理 99'));
check('cost 是 0 也要显示（0 元 ≠ 没配置）',
   QA.usageText({prompt_tokens: 0, completion_tokens: 0, pricing_configured: true,
                 cost: 0, currency: 'CNY'}).includes('≈ 0.0000 CNY'));

console.log('⑨ 详细批改：任务、诊断、优先修改与逐点证据；旧结果兼容');
const detailed = {score:1, full_score:15, schema_version:2, one_line:'回到题目要求',
  hit_seqs:[1], miss_seqs:[2,3,4,5,6,7],
  task_analysis:[{key:'1', task:'结合材料解释', focus:'说明反馈如何起作用'}],
  diagnosis:[{kind:'论证不足', quote:'<script>学生原句</script>', quote_status:'unmatched',
    problem:'缺少理论与材料的联系', action:'补一条反馈影响练习的解释'}],
  next_steps:['补充联系','删去重复背景'],
  section_scores:[{key:'1', got:1, full:8, comment:'该问缺少解释'}, {key:'2', got:0, full:7, comment:'该问未作答'}],
  point_awards:[{seq:1, awarded:1, quote:'学生原句', reason:'只完成部分解释'}],
  feedback_verification:{unmatched_quotes:1}};
const hd = visibleOf(QA.render({...Q, grade:detailed}, {pick:false}));
check('展示题目任务与作答范围', hd.includes('题目要你完成什么') && hd.includes('说明反馈如何起作用'));
check('逐小问展示分数与失分说明', hd.includes('各小问得分') && hd.includes('有效练习的条件：1 / 8 分') && hd.includes('该问未作答'));
check('来源未核实的参考答案不冠以官方', !hd.includes('官方参考答案') && !hd.includes('官方给分松紧'));
check('展示诊断、证据和具体改法', hd.includes('论证不足') && hd.includes('怎么改：') && hd.includes('补一条反馈影响练习的解释'));
check('优先建议只展示实际给出的两条', count(hd, '<li>') === 2);
check('引文不对应原文时明确待核', hd.includes('引文待核：未在本次作答中找到这句话。'));
check('AI 引文中的 HTML 被转义', hd.includes('&lt;script&gt;学生原句&lt;/script&gt;') && !hd.includes('<script>学生原句'));
check('逐点分账显示部分得分，不冒充满分', hd.includes('第 1 点 · 1 / 2 分') && hd.includes('实得 1 / 2 分'));
check('旧批改没有新字段也能展示',
  QA.render({...Q, grade:{score:7, full_score:15, one_line:'旧记录'}}, {pick:false}).includes('旧记录'));
check('新字段形状异常时仍能展示原批改',
  QA.render({...Q, grade:{score:7, task_analysis:'无效', diagnosis:null, next_steps:{}, point_awards:[]}}, {pick:false}).includes('qa-gscore'));

const aiDetailed = {...detailed, schema_version:2, score:1.5, full_score:15,
  verification:{status:'v2_ok'}, section_scores:[{key:'1',got:1.5},{key:'2',got:0}],
  point_awards: POINTS.map(p => ({seq:p.seq,awarded:p.seq === 1 ? 1.5 : 0}))};
for (const point_source of ['ai_unconfirmed', 'self_confirmed', 'ai_confirmed']) {
  const html = QA.render({...Q, grade:aiDetailed, point_source}, {pick:true});
  check(point_source + '：顶部采用 AI 实得 1.5 分', html.includes('class="qa-got">1.5</b>'));
  check(point_source + '：明确显示 AI 得分来源', html.includes('得分来源：AI 详细批改（已对账）'));
  check(point_source + '：小问也显示 AI 实得分', html.includes('1.5 / 8 分'));
}
const authoritative = QA.render({...Q, grade:aiDetailed,
  score_result:{scored:true,score:2.5,full:15,score_source:'ai_graded',score_source_label:'AI 详细批改（已对账）'}}, {pick:false});
check('回看采用服务端现算成绩', authoritative.includes('class="qa-got">2.5</b>'));
check('提交结果能显示有效零分', QA.practiceScoreText({subjective_score:0,subjective_full:15,
  subjective_source:'ai_graded'}).includes('0 / 15 分（AI 详细批改实得分）'));

console.log('');
if (FAILED.length) {
  console.log(`✗ ${FAILED.length} 项失败：` + FAILED.join('；'));
  process.exit(1);
}
console.log('✓ 全部通过');
