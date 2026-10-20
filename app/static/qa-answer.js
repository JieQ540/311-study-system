/* 311 备考系统 · 答案解析与批改展示（四个页面共用）
 *
 * 为什么做成一个共用文件：以前 / 和 /daily 各写一遍渲染，改一个字段要改两处，
 * 漏一处**不报错**、只是标签静默失效（README 里「前后端字段名不一致」那条就是这么来的）。
 *
 * 展示的三条原则（对应这次改造要解决的问题）：
 *   ① **分数置顶**：优先显示 AI 详细批改实得分；采分点确认与练习成绩分开。
 *   ② **按小问分组、每点带分值**：官方是「按小问给分、小问内按要点给分」，
 *      平铺成一串看不出 10 分的小问里哪几点值钱。
 *   ③ **官方参考答案完整给、评分说明不丢**：以前参考答案被抽成一段纯文本，
 *      【评分说明】（「答出任意三点即可」「只答要点未展开者给 1～2 分」）被切掉了 ——
 *      而它恰恰是判分松紧的依据。
 *
 * 无框架（原生 JS）：本项目前端一直是原生 HTML/JS，不引构建工具链。
 */
(function (global) {
  'use strict';

  // ---------------------------------------------------------------- 小工具

  function esc(s) {
    return (s === null || s === undefined ? '' : String(s))
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function num(v) {
    const n = parseFloat(v);
    return isNaN(n) ? null : n;
  }
  // 分值显示：整数不带小数点，3.5 照样显示
  function fmt(v) {
    const n = num(v);
    if (n === null) return '—';
    return (Math.round(n * 100) / 100).toString();
  }

  const QTYPE_CN = { analysis: '辨析题', short: '简答题', essay: '分析论述题', single: '单项选择题' };

  /** 参考答案按段落切分：官方答案的「（1）（2）」「①②」要能各占一行，不能挤成一坨。 */
  function paragraphs(text) {
    if (!text) return [];
    let t = String(text).replace(/\r\n?/g, '\n').trim();
    // 在「（数字）」/「①」前断行 —— 只认行首或句末标点之后的编号，
    // 否则会把正文里的「三（2）班」这种拆开（这个坑踩过，见 docs/进展-第2期.md）
    t = t.replace(/([。；！？\n])\s*([（(]\s*[1-9１-９]\s*[)）])/g, '$1\n$2');
    t = t.replace(/([。；！？\n])\s*([①②③④⑤⑥⑦⑧⑨⑩])/g, '$1\n$2');
    t = t.replace(/([。；！？])\s*(Ⅰ|Ⅱ|Ⅰ|Ⅱ)\s*[.．、]/g, '$1\n$2.');
    return t.split('\n').map(x => x.trim()).filter(Boolean);
  }

  // ---------------------------------------------------------------- 数据整形

  /**
   * 把后端给的一道主观题整理成统一的展示模型。
   * 兼容三种入口：/api/paper（points 平铺）、/api/logs/day（points 带 hit）、
   * 以及 AI 批改结果里自带的 standard_points。
   */
  function buildModel(q) {
    q = q || {};
    const full = num(q.full_score);
    const pts = (q.points || []).map((p, i) => ({
      seq: p.seq === undefined ? i + 1 : p.seq,
      claim: p.claim || '',
      evidence: p.evidence || '',
      section: (p.section || '*') || '*',
      score: num(p.score),
      hit: p.hit === true,
      hitKnown: p.hit === true || p.hit === false
    }));
    const secs = (q.sections && q.sections.length ? q.sections : []).map(s => ({
      key: String(s.key), label: s.label || '', score: num(s.score), desc: s.desc || ''
    }));
    // 没有声明小问但点里有 section → 用点里的 section 兜底，保证分组不会丢
    if (!secs.length && pts.some(p => p.section !== '*')) {
      const seen = [];
      pts.forEach(p => { if (p.section !== '*' && seen.indexOf(p.section) < 0) seen.push(p.section); });
      seen.forEach(k => secs.push({ key: k, label: '', score: null, desc: '' }));
    }
    const stats = q.score_stats || {};
    // 选做题（Ⅰ/Ⅱ 各 30 分）：总分要按选项分别算 —— 直接相加会得到 60，
    // 那个数字**看起来像数据错误**，其实是官方结构。必须分开显示。
    const elective = stats.elective === true;
    const options = (stats.options || []).map(o => ({
      key: o.key, got: num(o.got) || 0, full: num(o.full)
    }));
    // 「能不能折合成分数」以**服务端** `fold_ok` 为准（唯一判据在 marks.fold_ready：
    // 逐点分值齐全且为正 + 满分存在 + 形态与满分自洽）。旧版这里只看"每个点都有数字"，
    // 于是"分值之和 ≠ 满分"的题照样折出一个数，与 `split_points.py --report` 的结论打架。
    // 服务端没给（老调用方/离线桩）时才退回旧判据，保证不炸。
    const localOk = pts.length > 0 && pts.every(p => p.score !== null) && full !== null;
    const foldOk = (stats.fold_ok === undefined) ? localOk : stats.fold_ok === true;
    return {
      full: full,
      points: pts,
      sections: secs,
      grouped: secs.length > 0,
      elective: elective,
      options: options,
      note: q.answer_note || '',
      // 参考答案：paper 接口给 `reference`，AI 结果里叫 `answer_text` —— 两个都认，
      // 否则换一个接口进来这一折就**静默消失**（验收时真的发生过）。
      reference: q.reference || q.answer_text || '',
      explain: q.explain || '',
      grade: q.grade || null,
      scoreResult: q.score_result || null,
      answered: q.answered !== false,
      myAnswer: q.my_answer || '',
      cause: q.cause || '',
      hasScore: foldOk,
      foldOk: foldOk,
      foldReason: stats.fold_reason || '',
      // 结果来源与所选分支（设计 §2.2）。null = 这一题还没评定过（新卷子就是这样）。
      pointSource: q.point_source || null,
      selectedOption: q.selected_option || null,
      // 人工确认出口（设计 §5/D）：用户已确认"这份分账我核对过" → 随作答一起入库，
      // 服务端重算对账后盖章；此后这个 AI 分数计入可信参考分（状态 human_ok）。
      gradeOverride: q.grade_override === true,
      // 「答出任意 N 点即可满分」的服务端判定（以前前端有这段提示逻辑、服务端从不返回）
      freeHit: (stats.free_hit !== undefined ? stats.free_hit : q.free_hit) || null,
      freeHitSource: stats.free_hit_source || q.free_hit_source || null,
      sumScore: num(stats.sum_score) !== null ? num(stats.sum_score)
        : (pts.every(p => p.score !== null) ? Math.round(pts.reduce((a, p) => a + p.score, 0) * 100) / 100 : null)
    };
  }

  /** 把一个小问归到它所属的选项（Ⅰ / Ⅱ）：key 里没有就看 label（后端同源逻辑）。 */
  function optOfSection(secKey, label) {
    const m = String(secKey || '').match(/^\s*(Ⅰ|Ⅱ|I|II)/)
      || String(label || '').match(/^\s*(Ⅰ|Ⅱ|I|II)/);
    return m ? m[1] : null;
  }

  /** 当前勾选（或已命中）能折合多少分。选做题返回 {total, byOption}。 */
  function sumHit(model) {
    const byOption = {};
    let total = 0;
    const secOpt = {};
    (model.sections || []).forEach(s => { secOpt[s.key] = optOfSection(s.key, s.label); });
    // 选做题只算**作答的那一支**（两支各自 30 分，加起来 60 不是成绩）
    const only = model.elective ? model.selectedOption : null;
    model.points.forEach(p => {
      if (!p.hit || p.score === null) return;
      const k = model.elective ? (secOpt[p.section] || optOfSection(p.section) || '其他') : null;
      if (only && k && k !== only) return;
      total += p.score;
      if (model.elective) byOption[k] = Math.round(((byOption[k] || 0) + p.score) * 100) / 100;
    });
    total = Math.round(total * 100) / 100;
    // 「答出任意 N 点即可」的题：逐点之和可能大于满分，得分按满分封顶
    // （与服务端 `attempt_score()` 的封顶一致，否则页面比报告多出几分）。
    if (!model.elective && typeof model.full === 'number' && total > model.full) {
      total = Math.round(model.full * 100) / 100;
    }
    return model.elective ? {total: total, byOption: byOption} : total;
  }

  /** 已核算的 AI 实得分；勾选/采纳只改变采分点确认账。 */
  function aiScore(model) {
    if (!model.answered) return null;
    const sr = model.scoreResult;
    if (sr) return sr.scored && sr.score_source === 'ai_graded'
      ? {score: sr.score, full: sr.full, label: sr.score_source_label} : null;
    const g = model.grade;
    if (!g || g.schema_version !== 2 || !model.hasScore
        || (model.elective && !model.selectedOption)) return null;
    const manual = g.override?.status === 'human_ok';
    if (g.verification?.status !== 'v2_ok' && !manual) return null;
    const score = num(g.score), full = num(g.full_score);
    if (score === null || full === null || full <= 0 || score < 0 || score > full
        || full !== model.full) return null;
    return {score, full, label: 'AI 详细批改' + (manual ? '（人工核对）' : '（已对账）')};
  }

  /** 顶部成绩优先取 AI 实得分，未批改时显示采分点折合分。 */
  function scoreLabel(model) {
    const ai = aiScore(model);
    if (ai) return {main: fmt(ai.score), sub: `/ ${fmt(ai.full)} 分`, raw: ai.score};
    const r = sumHit(model);
    // 满分未知时不给假分母：以前这里 `num(q.full_score) || 15`，一道缺 full_score 的
    // 30 分论述题会被写成「x / 15 分」—— 那个数字是系统编的，不是题目的。
    const subTxt = (model.full === null) ? '（满分待核）' : `/ ${fmt(model.full)} 分`;
    if (!model.hasScore) return {main: '—', sub: subTxt, raw: 0};
    if (!model.elective) {
      return {main: fmt(r), sub: subTxt, raw: r};
    }
    // 选做题：**顶栏直接给两个选项各自的分数**。
    // 以前顶栏显示「— / 30 分」，等于把最关键的信息丢掉 —— 学生第一眼就想知道
    // 「我这题拿了多少」，而不是去看下面那行小字。
    const parts = model.options.map(o => {
      const got = (r.byOption || {})[o.key] || 0;
      const mark = (model.selectedOption === o.key) ? '▶ ' : '';
      return `${mark}${o.key} ${fmt(got)}${o.full !== null ? '/' + fmt(o.full) : ''}`;
    });
    return {
      main: parts.join('　'),
      sub: '分',
      raw: r.total,
      electiveLine: `选做题：${parts.map(p => `${p} 分`).join('　')}`
        + `（两个选项各自满分 ${fmt(model.full)} 分，只按你作答的那一个计分）`
    };
  }

  // ---------------------------------------------------------------- 样式

  const CSS = `
  .qa{border:1px solid #e3e3e3;border-radius:10px;background:#fff;overflow:hidden;margin:10px 0}
  .qa-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 14px;
    background:#f7fafc;border-bottom:1px solid #e3e3e3;font-size:13px}
  .qa-head .qa-title{font-weight:600;font-size:14px}
  .qa-head .qa-src{color:#777;font-size:12px}
  .qa-score{margin-left:auto;display:flex;align-items:baseline;gap:6px}
  .qa-score b{font-size:22px;color:#2b6cb0;font-variant-numeric:tabular-nums}
  .qa-score b.wide{font-size:16px}
  .qa-score .qa-full{color:#777;font-size:13px}
  .qa-badge{font-size:12px;padding:1px 8px;border-radius:10px;background:#eef3f8;color:#2b6cb0}
  .qa-badge.warn{background:#fdf3e7;color:#b7791f}
  .qa-badge.ok{background:#e8f5ec;color:#2f855a}
  /* 结果来源与「所选分支」：来源必须一眼可见 —— AI 说命中 vs 学生自己确认是两回事 */
  .qa-src-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 8px;
    padding:6px 10px;border-radius:6px;background:#f7fafc;border:1px solid #e9edf1;font-size:12px}
  .qa-src-row .qa-src-name{font-weight:600;color:#2b6cb0}
  .qa-src-row .qa-src-name.none{color:#b7791f}
  .qa-src-row button{font-size:12px;padding:2px 9px;border-radius:5px;border:1px solid #2b6cb0;
    background:#fff;color:#2b6cb0;cursor:pointer}
  .qa-src-row button:hover{background:#eef3f8}
  .qa-src-row .qa-optpick{margin-left:auto;display:flex;align-items:center;gap:4px}
  .qa-src-row label{cursor:pointer}
  .qa-src-only{font-size:12px;padding:1px 8px;border-radius:10px;background:#f0f0f0;color:#555}
  .qa-body{padding:12px 14px}
  .qa-track{height:6px;border-radius:3px;background:#edf1f5;overflow:hidden;margin:2px 0 10px}
  .qa-track i{display:block;height:100%;background:#2b6cb0;transition:width .18s}
  .qa-track.sec{height:4px;margin:6px 0 8px}

  .qa-sec{border:1px solid #e9edf1;border-radius:8px;margin:10px 0;overflow:hidden}
  .qa-sec-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:8px 12px;
    background:#fbfcfd;border-bottom:1px solid #eef1f4;font-size:13px}
  .qa-sec-head .qa-key{font-weight:700;color:#2b6cb0;font-variant-numeric:tabular-nums}
  .qa-sec-head .qa-desc{color:#777;font-size:12px}
  .qa-sec-head .qa-pts{margin-left:auto;color:#777;font-size:12px;font-variant-numeric:tabular-nums}
  .qa-sec-body{padding:6px 12px 8px}

  .qa-pt{display:flex;gap:8px;align-items:flex-start;padding:7px 6px;border-radius:6px;
    border-left:3px solid transparent}
  .qa-pt + .qa-pt{border-top:1px dashed #eef1f4}
  .qa-pt.pick{cursor:pointer}
  .qa-pt.pick:hover{background:#f6f9fc}
  .qa-pt.hit{border-left-color:#2f855a;background:#f5fbf7}
  .qa-pt.miss{border-left-color:#e3e3e3}
  .qa-pt input{margin-top:4px;width:15px;height:15px;flex:none}
  .qa-pt .qa-claim{flex:1;font-size:14px;line-height:1.7}
  .qa-pt .qa-ev{display:block;color:#8a8a8a;font-size:12px;line-height:1.6;margin-top:2px}
  .qa-pt .qa-val{flex:none;font-size:12px;color:#2f855a;font-weight:700;
    font-variant-numeric:tabular-nums;padding:1px 7px;border-radius:9px;background:#e8f5ec}
  .qa-pt .qa-val.no{background:#f1f3f5;color:#888;font-weight:400}
  .qa-pt.noscore .qa-val{background:#fdf3e7;color:#b7791f}

  .qa-note{margin:10px 0 0;padding:9px 12px;background:#fffdf5;border-left:3px solid #ecc94b;
    border-radius:4px;font-size:13px;line-height:1.7}
  .qa-note b{color:#975a16}
  .qa-extra{margin:8px 0 0;padding:8px 12px;background:#f0f9f3;border-left:3px solid #2f855a;
    border-radius:4px;font-size:13px;line-height:1.7}

  .qa-fold{margin-top:10px;border-top:1px solid #eef1f4;padding-top:8px}
  .qa-fold>summary{cursor:pointer;font-size:13px;color:#2b6cb0;font-weight:600;padding:3px 0}
  .qa-fold>summary:hover{text-decoration:underline}

  /* 答案折叠门：答题前不许先看到采分点（采分点就是答案要点）。
     为什么要有它：客观题的解析一直是「默认 hidden、提交或点按钮才显示」，
     主观题的采分点却是无条件渲染的 —— 同一份卷子里两套规矩，等于开卷做主观题。 */
  .qa-gate{margin-top:10px;border:1px dashed #d9e2ec;border-radius:8px;background:#fcfdfe}
  .qa-gate>summary{cursor:pointer;font-size:13.5px;color:#2b6cb0;font-weight:600;padding:9px 12px}
  .qa-gate>summary:hover{text-decoration:underline}
  .qa-gate[open]>summary{border-bottom:1px solid #eef1f4}
  .qa-gate .qa-gate-body{padding:8px 12px 10px}
  .qa-gate .qa-hint{color:#b7791f;font-size:12.5px;font-weight:400;margin-left:6px}
  .qa-answer{margin-top:8px;padding:11px 14px;background:#f8fafc;border-radius:6px;
    font-size:13.5px;line-height:1.85;max-height:460px;overflow:auto}
  .qa-answer p{margin:0 0 7px}
  .qa-answer p.idx{padding-left:2px}
  .qa-answer .qa-secmark{display:inline-block;min-width:26px;font-weight:700;color:#2b6cb0}

  /* AI 批改结果 */
  .qa-grade{margin-top:10px;border-top:1px solid #eef1f4;padding-top:10px}
  .qa-grade .qa-gscore{font-size:24px;font-weight:700;color:#2b6cb0;font-variant-numeric:tabular-nums}
  .qa-grade .qa-gsub{color:#777;font-size:12.5px}
  .qa-judge{margin:8px 0;padding:9px 12px;border-radius:5px;font-size:13.5px;line-height:1.7;
    border-left:3px solid #999;background:#fafafa}
  .qa-judge.ok{border-left-color:#2f855a;background:#f0f9f3}
  .qa-judge.bad{border-left-color:#c53030;background:#fdf0f0}
  .qa-ladder{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
  .qa-ladder span{font-size:12.5px;padding:3px 10px;border-radius:12px;background:#eef3f8;color:#2b6cb0}
  .qa-ladder span.minus{background:#fdf0f0;color:#c53030}
  .qa-para{border:1px solid #eef1f4;border-radius:6px;padding:9px 11px;margin:8px 0}
  .qa-para .qa-ptext{font-size:13.5px;line-height:1.8;white-space:pre-wrap}
  .qa-para .qa-pmark{margin-top:6px;font-size:12.5px;font-weight:700;color:#2f855a}
  .qa-para .qa-pmark.minus{color:#c53030}
  .qa-para .qa-pcmt{font-size:12.5px;color:#777;line-height:1.7;margin-top:3px}
  .qa-feedback{margin:14px 0;padding:12px 14px;background:#f8fafc;border:1px solid #e8edf3;border-radius:7px;font-size:13.5px;line-height:1.8}
  .qa-feedback p,.qa-diagnostic p{margin:5px 0;overflow-wrap:anywhere;white-space:pre-wrap}
  .qa-diagnostic{padding:10px 0;border-bottom:1px solid #e8edf3;font-size:13.5px;line-height:1.8}
  .qa-diagnostic:last-child{border-bottom:0}
  .qa-quote{margin:7px 0;padding:7px 11px;border-left:3px solid #94a3b8;background:#fff;color:#475569;white-space:pre-wrap;overflow-wrap:anywhere}
  .qa-next-steps ol{margin:7px 0 0;padding-left:22px}
  .qa-award-list{padding:4px 12px}
  .qa-opt{border:1px dashed #d9e2ec;border-radius:8px;padding:10px 12px;margin-top:8px;background:#fcfdfe}
  .qa-opt h5{margin:0 0 6px;font-size:13px;color:#2b6cb0}
  .qa-opt .qa-otext{font-size:13.5px;line-height:1.85;white-space:pre-wrap}
  .qa-opt .qa-otext b{color:#b7791f}
  .qa-empty{color:#999;font-size:13px;padding:6px 0}
  .qa-tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
  .qa-tools button{font-size:12.5px;padding:5px 12px;border-radius:14px;border:1px solid #d9e2ec;
    background:#fff;color:#2b6cb0;cursor:pointer}
  .qa-tools button:hover{background:#f2f6fa}
  .qa-tools button:disabled{opacity:.5;cursor:not-allowed}
  .qa-muted{color:#777;font-size:12.5px}

  /* 作答与批改（三个页面共用同一套） */
  .qa-work{margin-top:10px;border-top:1px dashed #e3e3e3;padding-top:10px}
  .qa-work textarea{width:100%;min-height:64px;padding:8px 10px;font-size:13.5px;
    font-family:inherit;line-height:1.7;border:1px solid #dfe4ea;border-radius:6px;resize:vertical}
  .qa-pick{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px}
  .qa-region{margin:8px 0 0}
  .qa-region textarea{width:100%;min-height:90px;padding:9px 11px;font-size:13.5px;
    font-family:inherit;line-height:1.75;border:1px solid #ecc94b;border-radius:6px;
    resize:vertical;background:#fffdf5}
  .qa-img{margin-top:8px;padding:8px 12px;background:#f8fafc;border-radius:6px;font-size:12.5px;color:#555}
  `;

  let cssInjected = false;
  function injectCSS() {
    if (cssInjected || typeof document === 'undefined') return;
    const s = document.createElement('style');
    s.textContent = CSS;
    document.head.appendChild(s);
    cssInjected = true;
  }

  // ---------------------------------------------------------------- 渲染

  function renderTrack(got, full) {
    if (full === null || full === undefined || full <= 0) return '';
    const pct = Math.max(0, Math.min(100, (got / full) * 100));
    return `<div class="qa-track"><i style="width:${pct.toFixed(1)}%"></i></div>`;
  }

  function renderPoint(p, opt) {
    const opt_ = opt || {};
    const cls = ['qa-pt'];
    if (opt_.pick) cls.push('pick');
    if (opt_.showHit) cls.push(p.hit ? 'hit' : 'miss');
    if (p.score === null) cls.push('noscore');
    const val = p.score === null
      ? `<span class="qa-val no" title="这一题还没有逐点分值（AI 未补到）">待核</span>`
      : `<span class="qa-val">${fmt(p.score)} 分</span>`;
    const box = opt_.pick
      ? `<input type="checkbox" data-q="${opt_.qid}" data-seq="${p.seq}"
           value="${esc(p.seq)}"${p.hit ? ' checked' : ''}>`
      : (opt_.showHit
        ? `<span style="flex:none;width:15px;text-align:center;color:${p.hit ? '#2f855a' : '#bbb'}">${p.hit ? '✓' : '·'}</span>`
        : '');
    // AI 认定命中、但学生**还没采纳**的点：在行上标一句"AI 建议命中"。
    // 以前是直接把框勾上（看起来就像学生自己勾的）；现在只是建议，采纳要点按钮。
    const sug = (opt_.suggested && opt_.suggested.has(Number(p.seq)))
      ? `<span class="qa-badge" title="AI 批改认为这一点击中了；采纳后才计入掌握率">AI 建议命中</span>` : '';
    const ev = p.evidence
      ? `<span class="qa-ev">判分依据：${esc(p.evidence).slice(0, 220)}</span>` : '';
    return `<label class="${cls.join(' ')}">${box}
        <span class="qa-claim">${esc(p.claim)}${ev}</span>${sug}${val}</label>`;
  }

  /** 把一段采分点包成一个小问块：标题栏给「本小问得分 / 满分」，正文只放属于它的点。 */
  function sectionScore(model, key, points) {
    const ai = model && aiScore(model);
    if (ai) {
      const g = model.grade || {};
      if (key === '*' && model.sections.length === 1) return ai.score;
      const sections = Array.isArray(g.section_scores) ? g.section_scores : [];
      const score = sections.find(s => s && String(s.key) === String(key));
      if (score && num(score.got) !== null) return num(score.got);
      const seqs = new Set(points.map(p => String(p.seq)));
      return Math.round((g.point_awards || []).filter(a => a && seqs.has(String(a.seq)))
        .reduce((sum, a) => sum + (num(a.awarded) || 0), 0) * 100) / 100;
    }
    return Math.round(points.filter(p => p.hit && p.score !== null)
      .reduce((sum, p) => sum + p.score, 0) * 100) / 100;
  }

  function wrap(inner, sec, mine, model) {
    const pts = mine || [];
    const got = sectionScore(model, sec.key, pts);
    const sc = sec.score;
    const head = `<div class="qa-sec-head">
        <span class="qa-key">${esc(sec.key === '*' ? '整题' : sec.key)}</span>
        ${sec.label ? `<span>${esc(sec.label)}</span>` : ''}
        ${sec.desc ? `<span class="qa-desc">${esc(sec.desc)}</span>` : ''}
        <span class="qa-pts">${sc !== null ? `${got} / ${fmt(sc)} 分` : `${got} 分`}</span>
      </div>`;
    const track = sc !== null && sc > 0 ? renderTrack(got, sc) : '';
    return `<div class="qa-sec" data-sec="${esc(sec.key)}">${head}
        <div class="qa-sec-body">${track}${inner}</div></div>`;
  }

  /** 「不属于任何小问」的采分点归到这一块；key 用不可能与小问重名的值。 */
  const OTHER_SEC = '__other__';

  /**
   * 渲染采分点与分数。
   * opt: {pick, showHit, qid, scoreOut（回调，勾选变化时给分数）}
   */
  function renderPoints(model, opt) {
    opt = opt || {};
    if (!model.points.length) {
      return `<div class="qa-empty">这道题还没有采分点${opt.pick
        ? ' —— <b>暂不能逐点自评</b>（可以先 AI 批改，或把作答原文写下来保存）' : ''}。</div>`;
    }
    // AI 建议的命中点：**只在还没被确认时**标注（已自评/已采纳时行上的 ✓ 才是结论）
    const confirmed = model.pointSource === 'self_confirmed' || model.pointSource === 'ai_confirmed';
    const grade = model.grade || null;
    if (grade && !confirmed && (grade.hit_seqs || []).length) {
      opt = {...opt, suggested: new Set((grade.hit_seqs || []).map(Number))};
    }
    const renderList = list => list.map(p => renderPoint(p, opt)).join('');
    // 没有小问结构：平铺（老数据、没有小问的题）
    if (!model.grouped) return renderList(model.points);

    // ⚠️ 每个小问里**只能渲染自己的采分点**。
    // 以前这一行传的是 `model.points`（全量），于是每个小问的正文都把整题的采分点
    // 重列一遍：截图里的「2 阶段判断与特点 0/7 分」底下先冒出小问 1 的 4 个点，
    // 再冒出自己的 3 个点 —— 看起来就是"答案重复了两遍"。
    // 小问标题右侧的分子一直是按 `mine` 算的，所以只有正文错、分数不错，更难发现。
    const used = [];
    const blocks = model.sections.map(sec => {
      const mine = model.points.filter(p => p.section === sec.key);
      if (!mine.length) return '';
      mine.forEach(p => used.push(p));
      return wrap(renderList(mine), sec, mine, model);
    }).join('');

    // 不属于任何已声明小问的点**不能静默丢掉**（section 写成别的值、或只有部分小问有分组）——
    // 丢掉的话「这题有几个采分点」就少算了，宁可单列一块让人看见。
    const rest = model.points.filter(p => used.indexOf(p) < 0);
    const restBlock = rest.length
      ? wrap(renderList(rest),
             {key: OTHER_SEC, label: '未归入小问的采分点', score: null, desc: ''}, rest, model)
      : '';
    return blocks + restBlock;
  }

  function renderAnswer(model, id) {
    if (!model.reference) return '';
    const body = paragraphs(model.reference)
      .map(x => {
        const m = x.match(/^([（(]\s*[1-9１-９]\s*[)）]|[①②③④⑤⑥⑦⑧⑨⑩]|Ⅰ|Ⅱ)/);
        return m
          ? `<p class="idx"><span class="qa-secmark">${esc(m[1])}</span>${esc(x.slice(m[1].length))}</p>`
          : `<p>${esc(x)}</p>`;
      }).join('');
    return `<details class="qa-fold"><summary>参考答案（做完再对照）</summary>
        <div class="qa-answer">${body}</div></details>`;
  }

  function markClass(mark) {
    return /扣|-\s*\d|0\s*分/.test(String(mark || '')) ? 'minus' : '';
  }

  function renderGrade(model) {
    const g = model.grade;
    if (!g) return '';
    const out = [];
    out.push(`<div class="qa-grade">`);
    // 服务端对账结果（设计 §3）：新版逐点分账由服务端**自己加一遍**。
    // 对不上时分数照样显示（AI 确实这么判的），但必须明说它不进趋势 ——
    // 以前没有任何标记，一个"加不起来"的分数会被当成准数看。
    const ver = g.verification || null;
    // 自动重批（2026-10-09）：服务端在对账不过时会**自己再批一次**。这里必须说出来，
    // 否则"分数变了、钱也多花了"对用户是黑箱（用量那块已按两次合计）。
    const retry = (ver && ver.retry) || null;
    const retryNote = retry && retry.attempted
      ? `<div class="qa-muted">已自动重批一次${(retry.second_status === 'json_error'
          || retry.second_status === 'error')
          ? '（重批那次没返回可用结果，下面是首轮的批改）' : ''}
          ｜首轮对账问题：${esc((retry.first_errors || []).join('；') || '—')}</div>`
      : '';
    // 人工确认过的（回看时读库里存下的盖章，设计 §5/D）：状态与"机器对账通过"分开说，
    // 并把**被覆盖的错误原文**原样摆出来 —— 不然"已人工确认"就成了一个没有信息量的话。
    const ov = (g.override && g.override.status === 'human_ok') ? g.override : null;
    if (ov) {
      out.push(`<div class="qa-judge ok"><b>这份分账已人工确认</b>（计入可信参考分）
        <div class="qa-muted">原对账问题：${esc((ov.errors_overridden || []).join('；') || '—')}
        ${ov.at ? `｜确认时间 ${esc(ov.at)}` : ''}｜报告里与「机器对账通过」分开统计。</div></div>`);
    } else if (ver && ver.status && ver.status !== 'v2_ok') {
      out.push(`<div class="qa-judge bad"><b>AI 分数未通过对账</b>：
        ${esc((ver.errors || []).join('；') || '分数构成对不上')}
        <div class="qa-muted">评语与逐段批改照常可看；这个分数<b>不计入可信参考分</b>，也不进趋势。
          你可以核对上面的账：对的话点来源行里的「我确认这份分账，计入可信参考分」
          （会明文记下你确认的是什么问题），或者自己勾采分点自评、再手动批改一次。</div>
        ${retryNote}</div>`);
    } else if (ver && ver.status === 'v2_ok') {
      out.push(`<div class="qa-muted" style="margin:4px 0">分数已对账：
        逐点实得 + 加分 + 调整 = ${fmt(ver.expected)} 分</div>${retryNote}`);
    }
    out.push(`<div class="qa-gscore">${fmt(g.score)} <span class="qa-gsub">/ ${fmt(g.full_score || model.full)}</span></div>`);
    if (g.one_line) out.push(`<p style="margin:6px 0;font-size:13.5px"><b>一句话总评：</b>${esc(g.one_line)}</p>`);
    out.push(renderFeedback(g, model));

    // 判断正误（辨析题）：独立维度，单独摆出来
    if (g.judgment) {
      const j = g.judgment;
      out.push(`<div class="qa-judge ${j.matched ? 'ok' : 'bad'}">
          <b>判断正误（独立得分维度）</b>：你判「${esc(j.student || '未作判断')}」
          ／正确应为「${esc(j.correct || '?')}」→
          <b>${j.matched ? '判断正确' : '判断错误或未判断'}</b>
          ${j.note ? `<div class="qa-muted">${esc(j.note)}</div>` : ''}</div>`);
    }
    if (g.judgment_cap_applied) {
      out.push(`<div class="qa-judge bad"><b>已按官方规则封顶</b>：${esc(g.judgment_cap_applied)}
        <div class="qa-muted">辨析题必须先给出明确判断；判断错或没判断，理由写得再好也不超过 3 分。</div></div>`);
    }

    // 分数阶梯：一眼看出分从哪来、又扣在哪
    if (g.hit_seqs || g.miss_seqs || g.extra_credit) {
      const hits = (g.hit_seqs || []).map(s => {
        const p = model.points.find(x => String(x.seq) === String(s));
        const a = Array.isArray(g.point_awards) ? g.point_awards.find(x => x && String(x.seq) === String(s)) : null;
        const score = a ? ` 实得 ${fmt(a.awarded)} / ${fmt(p && p.score)} 分`
          : p && p.score !== null ? ` +${fmt(p.score)}` : '';
        return `<span>✓ ${p ? esc(p.claim.slice(0, 22)) : '第 ' + esc(s) + ' 点'}${score}</span>`;
      });
      const miss = (g.miss_seqs || []).map(s => {
        const p = model.points.find(x => String(x.seq) === String(s));
        return `<span class="minus">✗ ${p ? esc(p.claim.slice(0, 22)) + (p.score !== null ? ` −${fmt(p.score)}` : '') : '第 ' + s + ' 点'}</span>`;
      });
      out.push(`<div class="qa-ladder">${hits.join('')}${miss.join('')}</div>`);
    }
    (g.extra_credit || []).forEach(x => {
      // v2 写 `awarded`（实得加分），旧结果写 `suggest_score`（建议分）—— 两个都认，
      // 但标签要区分：实得的分与"建议给多少"不是一回事。
      const v = x.awarded !== undefined ? x.awarded : x.suggest_score;
      const isReal = x.awarded !== undefined;
      out.push(`<div class="qa-extra">言之有理加分项：${esc(x.text)}
        ${v !== undefined ? `<b>（${isReal ? '' : '建议 '}${fmt(v)} 分）</b>` : ''}</div>`);
    });
    (g.adjustments || []).forEach(x => {
      out.push(`<div class="qa-extra">评分规则调整：<b>${x.delta > 0 ? '+' : ''}${fmt(x.delta)} 分</b>
        ${esc(x.reason || '')}</div>`);
    });
    if (g.recognized) {
      out.push(`<details class="qa-fold" open><summary>我识别到的手写内容（有误请指出）</summary>
        <div class="qa-answer">${esc(g.recognized)}
        ${g.recognize_note ? `<div class="qa-muted">识别存疑：${esc(g.recognize_note)}</div>` : ''}</div></details>`);
    }

    // 逐段批改：原文 → 得分/扣分 → 点评
    if (g.per_paragraph && g.per_paragraph.length) {
      out.push(`<details class="qa-fold"><summary>逐段批改（${g.per_paragraph.length} 段）</summary>`);
      g.per_paragraph.forEach(p => {
        out.push(`<div class="qa-para">
            <div class="qa-ptext">${esc(p.text)}</div>
            <div class="qa-pmark ${markClass(p.mark)}">${esc(p.mark || '')}</div>
            ${p.comment ? `<div class="qa-pcmt">${esc(p.comment)}</div>` : ''}
          </div>`);
      });
      out.push(`</details>`);
    }

    // 标准采分点（AI 当场给的，可能与库里的分值略有差异 → 两列都显示，别让人以为矛盾）
    if (g.standard_points && g.standard_points.length) {
      out.push(`<details class="qa-fold"><summary>批改时用的标准采分点</summary>
        <div class="qa-answer">` + g.standard_points.map(p => {
        const lib = model.points.find(x => String(x.seq) === String(p.seq));
        const libSc = lib && lib.score !== null ? `　（库中 ${fmt(lib.score)} 分）` : '';
        return `<p class="idx"><span class="qa-secmark">${esc(p.seq)}.</span>${esc(p.claim)}
            <b style="color:#2f855a">${p.score !== undefined ? `　${fmt(p.score)} 分` : ''}</b>
            <span class="qa-muted">${libSc}</span></p>`;
      }).join('') + `</div></details>`);
    }

    if (g.optimized) {
      out.push(`<details class="qa-fold"><summary>优化版作答（<b>加粗</b>处是补的）</summary>
        <div class="qa-answer">${mdBold(g.optimized)}</div></details>`);
    }
    out.push(`</div>`);
    return out.join('');
  }

  /** 所有作答与回看入口共用；新字段缺失时旧批改仍按原样展示。 */
  function renderFeedback(g, model) {
    const out = [];
    const tasks = Array.isArray(g.task_analysis) ? g.task_analysis.filter(x => x && typeof x === 'object') : [];
    const diagnoses = Array.isArray(g.diagnosis) ? g.diagnosis.filter(x => x && typeof x === 'object') : [];
    const awards = Array.isArray(g.point_awards) ? g.point_awards.filter(x => x && typeof x === 'object') : [];
    const sections = Array.isArray(g.section_scores) ? g.section_scores.filter(x => x && typeof x === 'object') : [];
    const steps = Array.isArray(g.next_steps) ? g.next_steps.filter(x => typeof x === 'string') : [];
    const quote = x => x.quote
      ? `<blockquote class="qa-quote">${esc(x.quote)}${x.quote_status === 'unmatched'
          ? '<div class="qa-muted">引文待核：未在本次作答中找到这句话。</div>' : ''}</blockquote>` : '';
    if (sections.length > 1 || sections.some(s => s.key && s.key !== '*')) {
      out.push(`<div class="qa-feedback qa-section-awards"><b>各小问得分</b>${sections.map(s => {
        const meta = model.sections.find(x => String(x.key) === String(s.key));
        return `<p><b>${esc(meta && meta.label ? meta.label : '小问 ' + s.key)}：${fmt(s.got)} / ${fmt(s.full)} 分</b>
          ${s.comment ? `<span class="qa-muted"> · ${esc(s.comment)}</span>` : ''}</p>`;
      }).join('')}</div>`);
    }
    if (tasks.length) out.push(`<div class="qa-feedback"><b>题目要你完成什么</b>${tasks.map(t =>
      `<p>${t.key && t.key !== '*' ? `<b>小问 ${esc(t.key)}：</b>` : ''}${esc(t.task)}
        ${t.focus ? `<span class="qa-muted"> · ${esc(t.focus)}</span>` : ''}</p>`).join('')}</div>`);
    if (g.feedback_verification && g.feedback_verification.unmatched_quotes > 0) {
      out.push('<div class="qa-judge bad">有批改引句未能对应本次原文，请先核对下方标注；分数对账不核验学科判断。</div>');
    }
    if (diagnoses.length) out.push(`<div class="qa-feedback qa-diagnosis"><b>作答诊断与具体改法</b>${diagnoses.slice(0, 4).map(d =>
      `<div class="qa-diagnostic"><b>${esc(d.kind || '作答诊断')}</b>${quote(d)}
        <p>${esc(d.problem || '')}</p>${d.action ? `<p><b>怎么改：</b>${esc(d.action)}</p>` : ''}</div>`).join('')}</div>`);
    if (steps.length) out.push(`<div class="qa-feedback qa-next-steps"><b>优先改这几处</b><ol>${steps.slice(0, 3)
      .map(step => `<li>${esc(step)}</li>`).join('')}</ol></div>`);
    if (awards.length) out.push(`<details class="qa-fold qa-awards"><summary>逐点得分与原文依据（${awards.length} 个点）</summary>
      <div class="qa-award-list">${awards.map(a => {
        const p = model.points.find(x => String(x.seq) === String(a.seq));
        return `<div class="qa-diagnostic"><b>第 ${esc(a.seq)} 点 · ${fmt(a.awarded)} / ${fmt(p && p.score)} 分</b>
          ${p ? `<p>${esc(p.claim)}</p>` : ''}${quote(a)}<p class="qa-muted">${esc(a.reason || '')}</p></div>`;
      }).join('')}</div></details>`);
    return out.join('');
  }

  /** 优化版作答里的 **粗体** 要真的粗 —— 以前只显示成两个星号。 */
  function mdBold(s) {
    return esc(s)
      .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
      .replace(/\n/g, '<br>');
  }

  // ---------------------------------------------------------------- 结果来源（设计 §2.2）
  //
  // 为什么要有这一层：AI 批改以前**自动替用户勾选**自评框，那些"AI 说的命中"就直接进了
  // point_hits，被当成学生自己确认的掌握情况；而日常页还写着"AI 批改结果不入命中率统计"。
  // 现在两者分开：
  //   · 勾选框 = 学生自己的判断，动它就等于"这一题我自评了"（self_confirmed）；
  //   · AI 结果只作**建议**展示，「采用 AI 建议」是一个单独的确认动作（ai_confirmed）；
  //   · 批改完没采纳就提交 → ai_unconfirmed（建议保留在批改结果里，不进掌握率）；
  //   · 什么都没做 → unassessed（不进分子也不进分母，而不是记 0 分）。
  const SOURCE_CN = {
    self_confirmed: '用户自评确认',
    ai_confirmed: '用户采纳 AI 建议',
    ai_unconfirmed: '仅收到 AI 建议（未采纳）',
    unassessed: '尚未评定',
    legacy_unknown: '来源不明（旧记录）'
  };

  function _modelOf(qid) {
    const box = document.querySelector(`.qa[data-qid="${qid}"]`);
    if (!box) return null;
    try { return JSON.parse(box.dataset.model || '{}'); } catch (e) { return {}; }
  }

  function _writeModel(qid, model) {
    const box = document.querySelector(`.qa[data-qid="${qid}"]`);
    if (box) box.dataset.model = JSON.stringify(model);
  }

  /** 这一题的结果来源；没评定过返回 null。 */
  function sourceOf(qid) {
    const m = _modelOf(qid);
    return (m && m.point_source) || null;
  }

  /** 记下这一题的结果来源（写进 data-model，整块重渲染后不丢）。 */
  function setSource(qid, src) {
    const m = _modelOf(qid);
    if (!m) return;
    m.point_source = src;
    _writeModel(qid, m);
    repaintSourceRow(qid);
  }

  /** 选做题：学生作答的是哪一支（Ⅰ / Ⅱ）；没选返回 null。 */
  function optionOf(qid) {
    const m = _modelOf(qid);
    return (m && m.selected_option) || null;
  }

  function setOption(qid, opt) {
    const m = _modelOf(qid);
    if (!m) return;
    m.selected_option = opt || null;
    _writeModel(qid, m);
    const box = document.querySelector(`.qa[data-qid="${qid}"]`);
    if (box) refreshScore(box);
  }

  /** 页面上全部题目的来源 / 所选分支 → 提交时随作答一起入库。 */
  function collectSources(root) {
    const out = {};
    (root || document).querySelectorAll('.qa[data-qid]').forEach(box => {
      let m; try { m = JSON.parse(box.dataset.model || '{}'); } catch (e) { m = {}; }
      if (m.point_source) out[box.dataset.qid] = m.point_source;
    });
    return out;
  }

  /**
   * 人工确认出口（设计 §5/D）：哪些题被用户确认「这份分账我核对过」→ 随作答一起提交。
   *
   * 只提交**确认意图**，不提交任何"已对账"的结论 —— 盖章由服务端
   * `_attach_grade_override()` 自己重算对账后写入，客户端伪造不了可信分数。
   */
  function collectOverrides(root) {
    const out = {};
    (root || document).querySelectorAll('.qa[data-qid]').forEach(box => {
      let m; try { m = JSON.parse(box.dataset.model || '{}'); } catch (e) { m = {}; }
      if (m.grade_override === true) out[box.dataset.qid] = true;
    });
    return out;
  }

  function collectOptions(root) {
    const out = {};
    (root || document).querySelectorAll('.qa[data-qid]').forEach(box => {
      let m; try { m = JSON.parse(box.dataset.model || '{}'); } catch (e) { m = {}; }
      if (m.selected_option) out[box.dataset.qid] = m.selected_option;
    });
    return out;
  }

  /**
   * 「人工确认」按钮那块 HTML（设计 §5/D）。
   *
   * 只有**对账没通过**（status 存在且不是 v2_ok）时才给按钮：机器已经对上的没有可确认的
   * 东西，给按钮只会变成随手一点的习惯动作，把"可信"稀释成"我说了算"。
   */
  function overrideControl(m, qid) {
    const ver = (m.grade && m.grade.verification) || null;
    if (!ver || !ver.status || ver.status === 'v2_ok') return '';
    // **两种模型形状都要认**：整块渲染时进来的是 buildModel 的驼峰 `gradeOverride`，
    // 就地刷新（repaintOverride）时进来的是 data-model 解析结果 `grade_override`。
    // 只认一种就会出现"点了按钮状态变了、标签却没变"（2026-10-09 真实踩过，浏览器验收抓住）。
    const on = (m.gradeOverride === true) || (m.grade_override === true);
    return `<button type="button" class="qa-override-btn${on ? ' on' : ''}" data-q="${esc(qid)}"
        title="上面写着对账差在哪；你核对过就点这里。服务端会把被覆盖的错误原文一并存档。"
        >${on ? '✓ 已确认计入可信参考分（点此取消）' : '我确认这份分账，计入可信参考分'}</button>
      ${on ? `<span class="qa-muted">保存后这个 AI 分数会计入可信参考分，并在报告里标出「人工确认」
        —— 它与「机器对账通过」分开统计。</span>` : ''}`;
  }

  /** 来源行：作答页可改（按钮），回看页只显示。 */
  function renderSourceRow(m, qid, editable) {
    const src = m.pointSource;
    const name = SOURCE_CN[src] || '';
    if (!editable) {
      if (!name) return '';
      return `<div class="qa-src-row"><span>采分点确认：</span>
        <span class="qa-src-only">${esc(name)}</span></div>`;
    }
    const hasGrade = !!m.grade;
    // 人工确认出口（设计 §5/D）：**只有"没通过对账"的批改才需要人工确认** ——
    // 机器已经对上的（v2_ok）没有可确认的东西，不给按钮，免得变成随手一点的习惯动作。
    // 单独包一层外壳：点按钮后就地换这一块，不整行重渲染（否则勾选状态会丢）。
    const confirmBtn = `<span class="qa-ovrow" data-ovrow="${esc(qid)}">${overrideControl(m, qid)}</span>`;
    // 题库里没有采分点 → **不能逐点自评**。这时不给「0 点命中，完成自评」按钮：
    // 点了会存下一条"自评 0 点"的记录，而学生其实根本没有可勾的要点，
    // 那等于把"没法自评"误记成"一个点都没答到"（设计 §2.2 新增条款）。
    if (!m.points.length) {
      return `<div class="qa-src-row" data-srcrow="${esc(qid)}">
        <span>采分点确认：</span>
        <span class="qa-src-name none">暂不能逐点自评</span>
        <span class="qa-muted">题库里这道题还没有采分点。可以先「AI 批改」，或把作答原文写下来保存；
          采分点补齐后再回来勾选。</span>
        ${hasGrade ? `<button type="button" class="qa-adopt-btn" data-q="${esc(qid)}">采用 AI 建议</button>` : ''}
        ${confirmBtn}
      </div>`;
    }
    return `<div class="qa-src-row" data-srcrow="${esc(qid)}">
      <span>采分点确认：</span>
      <span class="qa-src-name${src ? '' : ' none'}" data-srcname="${esc(qid)}">${esc(name || '尚未评定')}</span>
      <span class="qa-muted" data-srchint="${esc(qid)}">${esc(_sourceHint(src, hasGrade))}</span>
      ${hasGrade ? `<button type="button" class="qa-adopt-btn" data-q="${esc(qid)}">采用 AI 建议</button>` : ''}
      <button type="button" class="qa-zero-btn" data-q="${esc(qid)}">0 点命中，完成自评</button>
      ${confirmBtn}
      ${m.elective ? `<span class="qa-optpick">我答的是：
        ${['Ⅰ', 'Ⅱ'].map(k => `<label><input type="radio" name="opt-${esc(qid)}"
            data-opt="${esc(qid)}" value="${k}"${m.selectedOption === k ? ' checked' : ''}> ${k}</label>`).join(' ')}
        <span class="qa-muted" title="两个选项各自满分 30 分，必须知道答的是哪一支才能折分">（选做题必选）</span>
      </span>` : ''}
    </div>`;
  }

  function _sourceHint(src, hasGrade) {
    if (src === 'self_confirmed') return '你自己勾的采分点，计入掌握率';
    if (src === 'ai_confirmed') return '你采纳了 AI 建议，计入掌握率';
    if (src === 'ai_unconfirmed') return '无需勾选即可保存 AI 实得分；采分点尚未确认';
    if (src === 'legacy_unknown') return '旧记录，无法追溯是谁判的；按"来源不明"单独统计';
    return hasGrade ? '已收到 AI 建议，可「采用」或自己勾选' : '勾选采分点或点「0 点命中」即算完成自评';
  }

  /** 就地刷新来源行（点按钮后不整块重渲染，避免勾选状态与焦点丢失）。 */
  function repaintSourceRow(qid) {
    const m = _modelOf(qid);
    const row = document.querySelector(`[data-srcrow="${qid}"]`);
    if (!m || !row) return;
    const nameEl = row.querySelector(`[data-srcname="${qid}"]`);
    if (nameEl) {
      nameEl.textContent = SOURCE_CN[m.point_source] || '尚未评定';
      nameEl.className = 'qa-src-name' + (m.point_source ? '' : ' none');
    }
    const hintEl = row.querySelector(`[data-srchint="${qid}"]`);
    if (hintEl) hintEl.textContent = _sourceHint(m.point_source, !!m.grade);
  }

  /** 勾选框被手动改动 = 对整题结果的明确自评（设计 §2.2）。 */
  function markSelfConfirmed(qid) {
    setSource(qid, 'self_confirmed');
  }

  /** 「采用 AI 建议」：把 AI 认定的命中点勾上，并把来源记成 ai_confirmed。 */
  function adoptGrade(qid) {
    const m = _modelOf(qid);
    const box = document.querySelector(`.qa[data-qid="${qid}"]`);
    if (!m || !box) return;
    const grade = m.grade || {};
    const hits = (grade.hit_seqs || []).map(Number);
    box.querySelectorAll('input[type=checkbox][data-q]').forEach(cb => {
      cb.checked = hits.indexOf(parseInt(cb.dataset.seq)) >= 0;
    });
    // 勾选同步进 data-model（不重渲染，避免刚勾上的状态又被别处的旧模型覆盖）
    m.points = m.points.map(p => ({...p, hit: hits.indexOf(parseInt(p.seq)) >= 0}));
    m.point_source = 'ai_confirmed';
    _writeModel(qid, m);
    _clearSuggestBadges(box);      // 已采纳 → 行上的「AI 建议命中」标记完成使命
    repaintSourceRow(qid);
    refreshScore(box);
  }

  /** 这一题有没有被用户人工确认「这份分账我核对过」（设计 §5/D）。 */
  function overrideOf(qid) {
    const m = _modelOf(qid);
    return !!(m && m.grade_override === true);
  }

  /** 记下人工确认（写进 data-model，整块重渲染后不丢）。 */
  function setOverride(qid, on) {
    const m = _modelOf(qid);
    if (!m) return;
    m.grade_override = on === true;
    _writeModel(qid, m);
    repaintOverride(qid);
  }

  /**
   * 人工确认出口的开关（设计 §5/D）：点一下确认「这份分账我核对过」，再点取消。
   *
   * 只改状态、**不在这里改分数**：盖章与对账重算都在服务端保存时做
   * （`_attach_grade_override()`），页面连"已对账"这个结论都不产生。
   */
  function toggleOverride(qid) {
    const m = _modelOf(qid);
    if (!m || !m.grade) return;
    setOverride(qid, !(m.grade_override === true));
  }

  /**
   * 就地刷新「人工确认」那块（点按钮后不整块重渲染，避免勾选状态与焦点丢失）。
   *
   * 为什么要块外壳：renderSourceRow 是**整行生成**的，重渲染整行会把勾选框丢掉
   * （同 `repaintSourceRow` 的理由）。所以那块单独包一个 `data-ovrow` 外壳，只换它。
   */
  function repaintOverride(qid) {
    const m = _modelOf(qid);
    const host = document.querySelector(`[data-ovrow="${qid}"]`);
    if (!m || !host) return;
    host.innerHTML = overrideControl(m, qid);
  }

  /** 「0 点命中，完成自评」：一个点都没答到也是**有效结果**，要能明确表达并入库。 */
  function markZeroHits(qid) {
    const box = document.querySelector(`.qa[data-qid="${qid}"]`);
    if (!box) return;
    box.querySelectorAll('input[type=checkbox][data-q]').forEach(cb => { cb.checked = false; });
    const m = _modelOf(qid) || {};
    m.points = (m.points || []).map(p => ({...p, hit: false}));
    m.point_source = 'self_confirmed';
    _writeModel(qid, m);
    _clearSuggestBadges(box);      // 自己已经下了结论，AI 建议不再是待办
    repaintSourceRow(qid);
    refreshScore(box);
  }

  /** 去掉采分点行上的「AI 建议命中」标记（采纳或自评之后）。 */
  function _clearSuggestBadges(box) {
    box.querySelectorAll('.qa-pt .qa-badge').forEach(el => el.remove());
  }


  /**
   * 一次练习"主观题为什么没有折合分"的统一措辞（复审问题 3）。
   *
   * 以前报告卡、日志、AI 正文都把 `subjective_full == null` 说成"尚未评定" ——
   * 但"尚未评定"（人没下判断）与"**已自评，只是题目分值待核**"（判断做了、折合不出分数）
   * 是两件完全不同的事，混着说会把学生的判断当成没发生。
   * 分类由服务端 `unscored_kinds` 给，这里只负责措辞（四处共用一份）。
   */
  function subjectiveStateText(info) {
    info = info || {};
    const k = info.unscored_kinds || info.unscoredKinds || {};
    const pending = (k.pending_marks || 0) + (k.pending_elective || 0);
    const unassessed = (k.ai_unconfirmed || 0) + (k.unassessed || 0);
    const missing = k.missing_question || 0;
    // 这里用 <b> 而不是 **粗体**：报告卡与日志这两处是直接插 HTML 的，
    // 不经过 Markdown 渲染，写星号会原样显示成两个星号（已在浏览器里现眼一次）。
    if (pending && !unassessed) {
      return `主观题 ${info.n || pending} 题已自评，但<b>该题分值待核</b>（分值不齐，折合不出分数，不是 0 分）`;
    }
    if (pending && unassessed) {
      return `主观题 ${pending} 题已自评但<b>分值待核</b>、另有 ${unassessed} 题尚未评定（都不要当成答错或 0 分）`;
    }
    if (unassessed) return `主观题 ${info.n || unassessed} 题尚未评定（没自评、也没采纳 AI 建议）`;
    if (missing) return `主观题 ${missing} 题：题目已不在题库（分值无从核对）`;
    return `主观题 ${info.n || 0} 题尚未评定`;
  }

  function practiceScoreText(scores) {
    const score = num(scores.subjective_score), full = num(scores.subjective_full);
    if (score === null || full === null) return subjectiveStateText({n: scores.subjective,
      unscored_kinds: scores.unscored_kinds});
    const source = {ai_graded: 'AI 详细批改实得分', mixed_ai: 'AI 详细批改优先，其余按采分点折合',
      confirmed: '已确认采分点折合', legacy_unknown: '旧来源不明的采分点折合', mixed: '采分点折合，含旧记录'};
    return `主观题得分：${fmt(score)} / ${fmt(full)} 分（${source[scores.subjective_source] || '按已保存评分'}）`;
  }

  /**
   * 用量文案（设计 §4）：**token 永远显示**，金额只在配置了价目时才显示"估算"。
   *
   * 以前四个页面各自写一遍 `tokens/1e6*1 + tokens/1e6*4`，把 ¥1/¥4 硬编码在页面里 ——
   * 官方调价或换模型后，页面会继续按旧价报一个看起来精确的错数字。
   * 现在金额一律由服务端按 `config.json` 的 `api.pricing` 算好（`usage_info.cost`），
   * 没配置就是 null，页面据此只说 token。
   */
  function usageText(info, withReasoning) {
    const u = info || {};
    let s = `tokens ${u.prompt_tokens || 0}/${u.completion_tokens || 0}`;
    if (withReasoning && u.reasoning_tokens) s += `（含推理 ${u.reasoning_tokens}）`;
    if (u.pricing_configured && u.cost !== null && u.cost !== undefined) {
      s += ` ≈ ${Number(u.cost).toFixed(4)} ${u.currency || ''}`;
      if (u.pricing_effective_date) s += `（按 ${u.pricing_effective_date} 生效的价目估算）`;
    }
    return s;
  }

  // ---------------------------------------------------------------- 公开 API

  /**
   * 渲染整块「答案解析与批改」。
   * @param {object} q    题目（/api/paper 或 /api/logs/day 的一道主观题）
   * @param {object} opt  {pick:bool 可勾选自评, showHit:bool 显示命中, title:str,
   *                        gated:bool 把答案折进默认收起的折叠门（答题页用）}
   */
  function render(q, opt) {
    injectCSS();
    opt = opt || {};
    const m = buildModel(q);
    const title = opt.title || (QTYPE_CN[q.qtype] || q.qtype || '主观题');
    const parts = [];
    const qid = q.id || q.question_id;

    let badge = '';
    if (!m.hasScore) {
      // 「分值待核」的原因由服务端给（`fold_reason`），页面照抄而不是自己猜 ——
      // 判据只有一份（marks.fold_ready），页面与 `split_points.py --report` 同源。
      const why = m.foldReason || (m.full === null ? '题目满分缺失' : '逐点分值不齐或不自洽');
      badge = `<span class="qa-badge warn" title="${esc(why)}；这一题无法折合分数，勾选不计入折合分">分值待核</span>`;
    } else if (m.elective) {
      badge = `<span class="qa-badge" title="官方：Ⅰ/Ⅱ 任选一题，两题都答只按第 Ⅰ 题计分；两个选项各自满分 ${fmt(m.full)} 分">选做题</span>`;
    } else if (m.freeHit) {
      // 「答出任意 N 点即可」是官方正常形态（逐点合计会大于满分），
      // 以前这里按 `sumScore ≠ full` 判成刺眼的「分值不符」—— 那是误报。
      // 措辞按**依据来源**分：官方说明原话 → 说"官方评分说明"；
      // 靠分值形态（等值且整除）推出来的 → 说"按分值形态判断"。
      // 不能把本系统推出来的练习口径说成"官方评分"（设计 §3）。
      const fromNote = (m.freeHitSource || '') === 'note';
      const title = fromNote
        ? `官方评分说明：答出任意 ${esc(m.freeHit)} 点即可得满分；列出的可接受要点比需要的多，所以逐点合计大于满分`
        : `按分值形态判断：所有要点等值、且满分能被它整除（${esc(m.freeHit)} 个点）—— 这是本系统的练习口径，不是官方原话`;
      badge = `<span class="qa-badge" title="${title}">任意 ${esc(m.freeHit)} 点即可</span>`;
    } else {
      badge = `<span class="qa-badge ok">分值齐全</span>`;
    }

    const lab = scoreLabel(m);
    const scoreHtml = m.hasScore
      ? `<div class="qa-score"><b class="qa-got${m.elective ? ' wide' : ''}">${esc(lab.main)}</b>
           <span class="qa-full">${esc(lab.sub)}</span></div>`
      : `<div class="qa-score"><b>—</b><span class="qa-full">/ ${fmt(m.full)} 分</span></div>`;

    // ⚠️ data-model 是这块的**全部状态**：applyGrade() 批改后就是拿它整块重渲染的。
    // 所以 render() 用到的东西**一个都不能漏**，漏一个就是静默失效：
    //   · 漏 `id` → 重渲染出来的块变成 `data-qid=""`、复选框变成 `data-q="undefined"`，
    //     于是 AI 认定命中的点按 data-qid 找不到复选框（勾不上）、提交时
    //     `parseInt("undefined")` 被跳过（hits 为空）——「批改显示已批改、总结里主观题 0 分」
    //     就是这么来的（2026-09-24 引入，09-26 用户报的 bug，回归见 verify_subj_hits_ui.py）。
    //   · 漏考点名/路径 → 重渲染后头部那行考点会消失。
    parts.push(`<div class="qa" id="qa-${esc(opt.instanceId || qid)}" data-qid="${esc(qid)}"
        data-title="${esc(title)}"
        data-model="${esc(JSON.stringify({
          id: qid, question_id: qid,
          qtype: q.qtype, number: q.number, year: q.year, source: q.source,
          outline_name: q.outline_name, outline_path: q.outline_path,
          full_score: m.full, sections: q.sections || [], answer_note: m.note,
          reference: m.reference, explain: m.explain,
          points: (q.points || []).map(p => ({
            seq: p.seq, claim: p.claim, evidence: p.evidence,
            section: p.section, score: p.score, hit: p.hit === true
          })),
          answered: m.answered, my_answer: m.myAnswer,
          score_stats: q.score_stats || {}, free_hit: m.freeHit,
          // 来源与所选分支也进 data-model —— applyGrade() 整块重渲染时靠它恢复，
          // 漏在这里就等于"批改一次，来源被打回未评定"（同类坑见上面那段注释）。
          point_source: m.pointSource, selected_option: m.selectedOption,
          grade_override: m.gradeOverride === true,
          // `grade` 同样必须进 data-model：采纳按钮、"AI 建议命中"标注、来源行的提示
          // 都要读它。**漏掉过一次**（改完当场被浏览器验收抓住）：批改→重渲染→
          // data-model 里没有 grade →「采用 AI 建议」点了没反应（来源改了、框没勾上）。
          grade: m.grade || null
        }))}">
      <div class="qa-head">
        <span class="qa-title">${esc(title)}</span>
        ${q.number ? `<span class="qa-src">原第 ${esc(q.number)} 题</span>` : ''}
        ${q.year ? `<span class="qa-src">${esc(q.year)} 年真题</span>` : (q.source === 'AI生成' ? `<span class="qa-src">AI 生成</span>` : '')}
        ${badge}
        ${q.outline_name ? `<span class="qa-src" title="${esc(q.outline_path || '')}">${esc(q.outline_name)}</span>` : ''}
        ${scoreHtml}
      </div>
      <div class="qa-body">`);

    // 结果来源行：作答页可改（勾选框 / 采纳 AI 建议 / 0 点自评），回看页只显示。
    parts.push(renderSourceRow(m, qid, !!opt.pick));
    const primaryAI = aiScore(m);
    parts.push(`<div class="qa-score-source qa-muted">得分来源：${primaryAI
      ? esc(primaryAI.label) + '；成绩按详细批改实得分保存，无需自评勾选。'
      : '采分点折合分；通过对账的 AI 详细批改优先。'}</div>`);

    if (!m.answered) {
      parts.push(`<div class="qa-muted" style="margin-bottom:6px">这一次没有作答这道题（下面是对照用的答案与采分点）。</div>`);
    }
    if (m.elective && lab.electiveLine) {
      parts.push(`<div class="qa-note">${esc(lab.electiveLine)}</div>`);
    }
    parts.push(renderTrackOrHint(m, lab));

    // ---- 答案区（采分点 / 评分说明 / 参考答案 / 解析）----
    // `opt.gated`（答题页用）：整块塞进默认收起的折叠门 —— 采分点就是答案要点，
    // 不折起来等于开卷做主观题。日志与复盘页**不传** gated（那里就是回看答案的地方）。
    const ansParts = [renderPoints(m, {...opt, qid: qid})];
    if (m.note) {
      ansParts.push(`<div class="qa-note"><b>评分说明</b>（按本题要求给分）：${esc(m.note)}</div>`);
    }
    if (m.freeHit) {
      ansParts.push(`<div class="qa-note">官方是「<b>答出任意 ${esc(m.freeHit)} 点即可得满分</b>」——
        所以上面列出的可接受要点比需要的多，答够 ${esc(m.freeHit)} 点就是满分。</div>`);
    }
    ansParts.push(renderAnswer(m, qid));
    if (m.explain) {
      ansParts.push(`<details class="qa-fold"><summary>解析</summary>
        <div class="qa-answer">${mdBold(m.explain)}</div></details>`);
    }

    if (opt.gated) {
      parts.push(`<details class="qa-gate"><summary>写完这题再看：${gateSummary(m)}</summary>
          <div class="qa-gate-body">${ansParts.join('')}</div></details>`);
    } else {
      parts.push(ansParts.join(''));
    }
    parts.push(renderGrade(m));
    parts.push(`</div></div>`);
    return parts.join('');
  }

  /** 顶部那条分数进度条（选做题与「分值待核」各有各的写法）。 */
  function renderTrackOrHint(m, lab) {
    if (aiScore(m)) return renderTrack(lab.raw, m.full);
    if (m.hasScore && !m.elective) return renderTrack(lab.raw, m.full);
    if (!m.hasScore) {
      return `<div class="qa-muted" style="margin-bottom:8px">
        这道题分值待核（${esc(m.foldReason || '满分或逐点分值不齐')}），折合不出分数；
        你确认的采分点仍会计入掌握率，但**不进折合分**。</div>`;
    }
    return '';
  }

  /** 折叠门标题：让人一眼知道点开能拿到什么，而不用先点开看。 */
  function gateSummary(m) {
    const bits = [];
    if (m.points.length) {
      bits.push('采分点自评' + (m.hasScore
        ? `（${m.points.length} 个要点 · ${fmt(m.full)} 分）`
        : `（${m.points.length} 个要点 · 分值待核）`));
    }
    if (m.reference) bits.push('参考答案');
    if (m.explain) bits.push('解析');
    return bits.length ? bits.join(' · ') : '答案与解析';
  }

  /** 勾选变化时刷新分数显示（只改数字，不重渲染，避免复选框失焦）。 */
  function refreshScore(root) {
    if (!root) return;
    const box = root.closest ? root.closest('.qa') : null;
    if (!box) return;
    let model;
    try { model = JSON.parse(box.dataset.model || '{}'); } catch (e) { model = {}; }
    const base = buildModel(model);
    // 直接从 DOM 读勾选状态，避免维护第二份状态
    const pts = base.points.map(p => {
      const el = box.querySelector(`input[data-seq="${p.seq}"]`);
      return el ? {...p, hit: el.checked} : p;
    });
    const m2 = {...base, points: pts,
      options: base.options.map(o => ({...o, got: 0}))};
    const lab = scoreLabel(m2);
    const gotEl = box.querySelector('.qa-got');
    if (gotEl) gotEl.textContent = lab.main;
    const subEl = box.querySelector('.qa-full');
    if (subEl) subEl.textContent = lab.sub;
    // 选做题：把「Ⅰ x/30 ｜ Ⅱ y/30」那行也刷新
    if (m2.elective && lab.electiveLine) {
      const note = box.querySelector('.qa-note');
      if (note) note.textContent = lab.electiveLine;
    }
    const mainTrack = box.querySelector('.qa-body > .qa-track > i');
    if (mainTrack && m2.full > 0 && !m2.elective) {
      mainTrack.style.width = Math.min(100, lab.raw / m2.full * 100).toFixed(1) + '%';
    }
    // 小问的进度条与「x / y 分」
    box.querySelectorAll('.qa-sec').forEach(sec => {
      const key = sec.dataset.sec;
      const mine = pts.filter(p => p.section === key);
      const g = sectionScore(m2, key, mine);
      const label = sec.querySelector('.qa-pts');
      const want = parseFloat((label && label.textContent.split('/')[1]) || '');
      const i = sec.querySelector('.qa-track i');
      if (i && want > 0) i.style.width = Math.min(100, g / want * 100).toFixed(1) + '%';
      if (label && want > 0) label.textContent = `${g} / ${want} 分`;
    });
  }

  /**
   * 一次放出容器里所有被折叠的答案：
   *   · 主观题的采分点折叠门（.qa-gate，答题页默认收起）
   *   · 客观题的「正确答案 + 解析」（.qans，本来就是 hidden）
   * 谁用它：答题页提交后（做过就该看答案）、以及「显示答案解析」按钮。
   */
  function revealAll(root) {
    const host = root || document;
    if (!host || !host.querySelectorAll) return 0;
    const gates = host.querySelectorAll('details.qa-gate');
    gates.forEach(d => { d.open = true; });
    const qans = host.querySelectorAll('details.qans');
    qans.forEach(d => { d.hidden = false; d.open = true; });
    return gates.length + qans.length;
  }

  /** 收起所有答案（与 revealAll 对称；「收起答案解析」用）。 */
  function hideAll(root) {
    const host = root || document;
    if (!host || !host.querySelectorAll) return 0;
    const gates = host.querySelectorAll('details.qa-gate');
    gates.forEach(d => { d.open = false; });
    const qans = host.querySelectorAll('details.qans');
    qans.forEach(d => { d.hidden = true; d.open = false; });
    return gates.length + qans.length;
  }

  /** 还有没有折着的答案（判断按钮该显示「显示」还是「收起」）。 */
  function anyHidden(root) {
    const host = root || document;
    if (!host || !host.querySelectorAll) return false;
    return [...host.querySelectorAll('details.qa-gate')].some(d => !d.open)
      || [...host.querySelectorAll('details.qans')].some(d => d.hidden);
  }

  /** 读取某个容器里所有题目的勾选结果 → {question_id: [seq,...]} */
  function collectHits(root) {
    const map = {};
    (root || document).querySelectorAll('.qa input[type=checkbox][data-q]').forEach(i => {
      const q = parseInt(i.dataset.q);
      if (!q) return;
      (map[q] = map[q] || []).push(parseInt(i.dataset.seq));
    });
    return map;
  }

  /** 绑定勾选事件（用事件委托，重渲染后不用重新绑定）。 */
  function bind(root, onChange) {
    const host = root || document;
    if (host.__qaBound) return;
    host.__qaBound = true;
    host.addEventListener('change', e => {
      const t = e.target;
      if (!t || !t.matches) return;
      if (t.matches('.qa input[type=checkbox][data-q]')) {
        // 手动改勾选 = 对整题结果的**明确自评**（设计 §2.2）：
        // 页面不再"猜"来源，用户动了框就记 self_confirmed，进掌握率。
        markSelfConfirmed(parseInt(t.dataset.q));
        refreshScore(t);
        if (typeof onChange === 'function') onChange(t);
        return;
      }
      if (t.matches('.qa input[type=radio][data-opt]')) {
        // 选做题：记下作答的是哪一支（两支各自 30 分，不知道分支无法折分）
        setOption(parseInt(t.dataset.opt), t.value);
      }
    });
    // 批改按钮用事件委托：题目块会被整块重渲染，逐个绑定必然漏
    host.addEventListener('click', e => {
      const t = e.target;
      if (!t || !t.dataset) return;
      if (t.classList && t.classList.contains('qa-grade-btn')) {
        t.disabled = true;
        gradeText(t.dataset.q).then(function () { t.disabled = false; });
      } else if (t.classList && t.classList.contains('qa-photo-btn')) {
        const f = host.querySelector(`input[data-file="${t.dataset.q}"]`);
        if (f) f.click();
      } else if (t.classList && t.classList.contains('qa-adopt-btn')) {
        adoptGrade(parseInt(t.dataset.q));
      } else if (t.classList && t.classList.contains('qa-zero-btn')) {
        markZeroHits(parseInt(t.dataset.q));
      } else if (t.classList && t.classList.contains('qa-override-btn')) {
        toggleOverride(parseInt(t.dataset.q));
      }
    });
  }

  // ---------------------------------------------------------------- 作答与批改

  // 手写照片在前端压缩后再上传：原图 3–5 MB，多模态接口传不动也不必要
  function compressImage(file, maxW, quality) {
    maxW = maxW || 1600;
    quality = quality || 0.85;
    return new Promise(function (resolve, reject) {
      const img = new Image();
      img.onload = function () {
        const scale = Math.min(1, maxW / img.width);
        const cv = document.createElement('canvas');
        cv.width = Math.round(img.width * scale);
        cv.height = Math.round(img.height * scale);
        cv.getContext('2d').drawImage(img, 0, 0, cv.width, cv.height);
        resolve(cv.toDataURL('image/jpeg', quality));
      };
      img.onerror = reject;
      img.src = URL.createObjectURL(file);
    });
  }

  /**
   * 作答 + AI 批改区。三个页面共用，避免各写一遍走偏。
   * 批改完成后重新渲染该题的 .qa 块（把批改结果并入），而不是在下面另起一块 ——
   * 以前就是"自评一块、批改一块"两套样式，看起来像两个不同的功能。
   */
  function renderWork(q, opt) {
    injectCSS();
    opt = opt || {};
    const qid = q.id || q.question_id;
    return `<div class="qa-work" data-work="${esc(qid)}">
      <textarea id="ans-${esc(qid)}" placeholder="（可选）把这道题的作答文本敲进来，或点「📷 上传手写照片」；也可以直接在纸上写完，勾上面的采分点自评。"
        >${esc(q.my_answer || '')}</textarea>
      <div class="qa-pick">
        <button type="button" class="qa-grade-btn" data-q="${esc(qid)}">AI 批改（用上面的文本）</button>
        <button type="button" class="qa-photo-btn" data-q="${esc(qid)}">📷 上传手写照片批改</button>
        <input type="file" accept="image/*" hidden data-file="${esc(qid)}"
          onchange="QA.onPick(${qid}, this)">
        <span class="qa-muted" data-msg="${esc(qid)}"></span>
      </div>
      <div data-graderegion="${esc(qid)}"></div>
    </div>`;
  }

  /** 把批改结果并进该题的展示块。 */
  function applyGrade(qid, result, extra) {
    const box = document.querySelector(`.qa[data-qid="${qid}"]`);
    if (!box) return;
    let model;
    try { model = JSON.parse(box.dataset.model || '{}'); } catch (e) { model = {}; }
    // **先把学生自己的勾选读出来**（整块重渲染前唯一的机会）。
    // 以前这里直接用批改接口回传的 points（它不带 hit）覆盖模型，于是"用户先勾、
    // 再点批改 → 手动勾选被整体清空"，然后又被 AI 命中强行勾上 —— 两步都错。
    const checked = new Set();
    box.querySelectorAll('input[type=checkbox][data-q]').forEach(cb => {
      if (cb.checked) checked.add(parseInt(cb.dataset.seq));
    });
    model.grade = result;
    // 回看页使用服务端现算结果；刚批改则使用本次返回的结果，避免沿用旧分。
    delete model.score_result;
    // 新一次批改 → **上一次的人工确认作废**（确认的是那一份分账，不是这道题）。
    // 不重置的话，重批后那道题会带着旧的"已确认"状态保存，等于把没过对账的新分数
    // 也一并盖章进可信参考分。
    model.grade_override = false;
    if (extra && extra.points) {
      // points 换成服务端最新的（含分值/小问），但命中状态一律保留学生的那份
      model.points = extra.points.map(p => ({...p, hit: checked.has(parseInt(p.seq))}));
    } else {
      model.points = (model.points || []).map(p => ({...p, hit: checked.has(parseInt(p.seq))}));
    }
    if (extra && extra.sections) model.sections = extra.sections;
    if (extra && extra.answer_note) model.answer_note = extra.answer_note;
    if (extra && extra.score_stats) model.score_stats = extra.score_stats;
    // 收到建议但还没采纳 → 来源记 ai_unconfirmed：建议留在批改结果里（可回看），
    // **不写进 point_hits**，因此也不进掌握率。已经自评/已采纳过的不覆盖。
    if (!model.point_source || model.point_source === 'unassessed') {
      model.point_source = 'ai_unconfirmed';
    }
    box.dataset.model = JSON.stringify(model);
    // 重渲染时**强制带上题目 id**：两道保险 —— 就算哪天 data-model 又漏了 id，
    // 也不会再出现"批改后块失去身份 → 命中勾不上 → 保存 0 分"这种静默失效。
    // `gated:false`：批改的前提就是已经作答了，这一题没必要再折着答案。
    const fresh = render({...model, id: qid, question_id: qid},
                         {pick: true, showHit: true, gated: false, title: box.dataset.title || ''});
    box.outerHTML = fresh;
    // 重渲染后把学生的勾选原样恢复（AI 建议**不**自动勾上：采纳是单独的动作）
    const after = document.querySelector(`.qa[data-qid="${qid}"]`);
    if (after) {
      after.querySelectorAll('input[type=checkbox][data-q]').forEach(cb => {
        cb.checked = checked.has(parseInt(cb.dataset.seq));
      });
      refreshScore(after);
    }
  }

  /** 上传照片前的准备（页面里 onchange 直接调它）。 */
  function onPick(qid, input) {
    const f = input.files && input.files[0];
    if (!f) return;
    const msg = document.querySelector(`[data-msg="${qid}"]`);
    if (msg) msg.textContent = '压缩图片…';
    compressImage(f).then(function (dataUrl) {
      if (msg) msg.textContent = `图片 ${Math.round(dataUrl.length / 1024)} KB，识别并批改中（约 15–30 秒）…`;
      return gradeRequest(qid, {image: dataUrl}, msg);
    }).catch(function (e) {
      if (msg) msg.textContent = '图片处理失败：' + e;
    }).then(function () { input.value = ''; });
  }

  /** 文本批改（页面里的按钮调它）。 */
  function gradeText(qid) {
    const ta = document.getElementById('ans-' + qid);
    const msg = document.querySelector(`[data-msg="${qid}"]`);
    const text = ta ? (ta.value || '').trim() : '';
    if (!text) {
      if (msg) msg.textContent = '先把答案文本填进上面的框，或改用「上传手写照片」。';
      return Promise.resolve();
    }
    if (msg) msg.textContent = 'AI 批改中（思考模式，约 10–30 秒）…';
    return gradeRequest(qid, {student_text: text}, msg);
  }

  function gradeRequest(qid, body, msg) {
    body.question_id = qid;
    body.selected_option = optionOf(qid);
    global.dispatchEvent(new CustomEvent('qa-grade-started', {detail: {qid: qid}}));
    return fetch('/api/daily/grade', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    }).then(r => r.json()).then(function (j) {
      if (!j.ok) {
        // 失败原因要能看懂：以前只甩一句 JSONDecodeError
        if (msg) {
          msg.innerHTML = `<span style="color:#c53030">批改失败：${esc(j.error || '未知错误')}</span>`
            + (j.truncated ? '　建议把作答写短一些，或调大 config.json 的 grade.max_tokens。' : '');
        }
        return;
      }
      if (msg) msg.textContent = '';
      applyGrade(qid, j.result, j);
      // 批改结果留给提交时入库（练习日志要能回看这次批改）
      (global.__QA_GRADES = global.__QA_GRADES || {})[qid] = j.result;
      global.dispatchEvent(new CustomEvent('qa-graded', {detail: {qid: qid, result: j.result,
        usage: j.usage, points: j.points, cap: j.score_cap_note}}));
    }).catch(function (e) {
      if (msg) msg.textContent = '出错：' + e;
    }).finally(function () {
      global.dispatchEvent(new CustomEvent('qa-grade-finished', {detail: {qid: qid}}));
    });
  }

  /** 日志和复盘共用的只读作答回看；评分、采分点及详细批改仍由 render 负责。 */
  function renderReview(q) {
    if (q.pilot_url) return `<div class="qa-review"><div class="stem">${esc(q.stem)}</div><div class="ans">${esc(q.my_answer || '该考点正在独立作答，旧原答暂不展示。')}</div><p>试点定性训练 · 分值待核</p><a href="${esc(q.pilot_url)}">查看本次试点诊断与订正</a></div>`;
    const blank = q.answered === false;
    const mine = blank ? '这次未作答' : (q.my_answer || '这次没有留下作答原文');
    let body = `<div class="stem">${esc(q.stem || '题目已不在题库，原始记录仍保留。')}</div>`;
    if (q.qtype === 'single') {
      body += Object.entries(q.options || {}).map(([k,v]) =>
        `<div class="opt ${k === q.correct_answer ? 'right' : ''} ${k === q.my_answer ? 'picked' : ''}">
          <b>${esc(k)}.</b> ${esc(v)}</div>`).join('');
      body += `<div class="mine"><span class="lbl">我的作答：</span><b>${esc(mine)}</b>
        <span class="lbl">｜ 参考答案：</span><b>${esc(q.correct_answer || '未记录')}</b></div>`;
      if (q.explain) body += `<details class="src"><summary>解析</summary><div class="ans">${esc(q.explain)}</div></details>`;
    } else {
      body += `<div class="mine"><span class="lbl">我的作答：</span><div class="ans">${esc(mine)}</div></div>`;
      const id = q.attempt_id ? 'review-a' + q.attempt_id : 'review-q' + q.question_id;
      body += render(q, {pick:false, showHit:!blank, instanceId:id, title:QTYPE_CN[q.qtype] || '主观题'});
    }
    return `<div class="qa-review">${body}</div>`;
  }

  /** 试点定性参考卡共用展示；调用方必须先通过服务端原答锁定门。 */
  function renderPilotReference(data) {
    data = data || {};
    if (data.locked === false) return '<p class="notice">请先锁定原答，再查看参考与订正。</p>';
    const c = data.card || {};
    const provenance = data.provenance || {};
    const array = xs => Array.isArray(xs) ? xs : [];
    const str = x => {
      if (typeof x === 'string') return x;
      if (x === null || x === undefined) return '';
      if (typeof x === 'object') return x.text || x.task || x.claim || x.label || x.content || JSON.stringify(x);
      return String(x);
    };
    const list = xs => `<ul>${array(xs).map(x => `<li>${esc(str(x))}</li>`).join('')}</ul>`;
    // 旧题包的卡片保留来源ID，新资料可携带结构化来源；统一在锁定后的出口展示。
    const sources = [];
    const sourceIndex = new Map();
    array(data.sources).concat(array(c.sources)).forEach(source => {
      const item = typeof source === 'object' && source ? source : {id: source};
      const id = String(item.id || item.source_id || 'source-' + sources.length);
      if (sourceIndex.has(id)) {
        const index = sourceIndex.get(id);
        sources[index] = Object.assign({}, sources[index], item);
      } else {
        sourceIndex.set(id, sources.length);
        sources.push(item);
      }
    });
    const sourceLinks = refs => array(refs).map(ref => {
      const key = String(typeof ref === 'object' && ref ? ref.id || ref.source_id : ref);
      return sourceIndex.has(key) ? '来源 ' + (sourceIndex.get(key) + 1) : '来源记录 ' + key;
    }).join('、');
    const units = `<ul>${array(c.units).map(unit => {
      const item = typeof unit === 'object' && unit ? unit : {};
      const taskNames = array(item.task_keys).length ? '对应任务 ' + item.task_keys.join('、') : '';
      const refs = sourceLinks(item.source_refs);
      const detail = [taskNames, refs].filter(Boolean).join(' · ');
      return `<li>${esc(str(unit))}${detail ? `<div class="qa-muted">${esc(detail)}</div>` : ''}</li>`;
    }).join('')}</ul>`;
    const sourceKind = {bank_reference: '库内参考正文', bank_points: '库内采分点'};
    const sourceDetails = sources.map((source, index) => {
      const file = (source.path || '').split(/[\\/]/).pop();
      const label = source.title || source.source_label || file || source.id || source.source_id || '资料记录';
      const pages = source.pdf_page ? `PDF 页 ${source.pdf_page}${source.printed_page ? ' · 印刷页 ' + source.printed_page : ''}` : '';
      return `<li><b>来源 ${index + 1} · ${esc(label)}</b>
        ${source.kind ? `<p>${esc(sourceKind[source.kind] || source.kind)}</p>` : ''}
        ${pages ? `<p>${esc(pages)}</p>` : ''}
        ${source.locator ? `<p>定位：${esc(source.locator)}</p>` : ''}
        ${source.evidence_quote ? `<blockquote>${esc(source.evidence_quote)}</blockquote>` : ''}
        ${source.review_status ? `<p>${esc(source.review_status)}</p>` : ''}</li>`;
    }).join('');
    const status = data.reference_status || provenance.reference_status || c.reference_status;
    const quality = {usable_qualitative: '参考可用于定性练习', needs_source: '资料来源待补', disputed: '参考依据存在争议'}[status] || '';
    const gaps = array(data.gaps || provenance.gaps || c.gaps);
    const limitations = gaps.map(gap => typeof gap === 'string' ? gap :
      [gap.detail, gap.next_action].filter(Boolean).join('；'));
    let segment = 0;
    return `<h2>参考与订正</h2><p class="notice">AI 整理的定性参考，分值依据待核。等效表述可成立，不要求照抄标题或所有可选要点。</p>
      ${quality ? `<p class="notice">${esc(quality)}；来源限制见下方说明。</p>` : ''}
      <h3>本题任务</h3>${list(c.tasks)}<h3>参考要点</h3>${units}
      ${data.criterion ? `<p>${esc(str(data.criterion))}</p>` : ''}<p>${esc(c.rubric_note || '')}</p>
      <details><summary>组织建议与等效表达</summary>${list(c.structure)}${list(c.alternatives)}${list(c.avoid)}</details>
      ${limitations.length ? `<details><summary>待核与来源限制</summary>${list(limitations)}</details>` : ''}
      <details><summary>出处与审校说明</summary><p>${esc(provenance.source_label || '')}</p>
      <p>${esc(provenance.review_kind || '')}${provenance.reviewer ? ' · ' + esc(provenance.reviewer) : ''}</p>${list(provenance.edits)}
      <p>AI 整理与库内解析不等于官方评分标准；原页核验状态以每条来源说明为准。</p><ol>${sourceDetails}</ol></details>
      ${array(data.knowledge).map(k => `<details><summary>${esc(k.title)} · AI 摘要</summary><p>${esc(k.summary)}</p>${list(k.outline)}<p class="notice">${esc(k.boundary)}</p>
        ${array(k.original_segments).map(s => `<button type="button" class="ghost" data-segment="${segment++}">查看原文切片 · PDF ${esc(s.pdf_page)} 页</button>`).join('')}</details>`).join('')}`;
  }

  global.QA = {
    renderPilotReference,
    render, renderWork, renderReview, buildModel, refreshScore, collectHits, bind,
    applyGrade, onPick, gradeText, compressImage, revealAll, hideAll, anyHidden,
    paragraphs, esc, fmt, sumHit, QTYPE_CN,
    // 评分来源与所选分支（设计 §2.2）：页面提交时读这几个
    sourceOf, setSource, optionOf, setOption, collectSources, collectOptions,
    // 人工确认出口（设计 §5/D）：作答页提交时读 collectOverrides
    collectOverrides, toggleOverride, overrideOf, setOverride,
    adoptGrade, markZeroHits, SOURCE_CN, subjectiveStateText, practiceScoreText, usageText
  };
})(window);
