# -*- coding: utf-8 -*-
"""AI 客户端：统一封装 DeepSeek 调用。

三个业务环节都走这里：
  ① parse_study_log  自然语言学习记录 → 大纲考点
  ② generate_questions  按考点生成新题（真题不够时）
  ③ grade_answer     主观题批改（按 311 提示词 + 言之有理加分）

关键设计：
  - 默认关闭「思考模式」：实测同一问题输出 1 token vs 44 token，思考按输出计费
  - 批改场景可单独开启思考（质量优先）
  - 所有返回强制 JSON（用 response_format），避免解析自由文本
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]   # 公开副本：相对路径
CFG_PATH = ROOT / "config.json"
DB = ROOT / "data" / ("kaoyan.db" if (ROOT / "data" / "kaoyan.db").exists()
                       else "kaoyan-seed.db")

_CFG = None



class ConfigMissing(RuntimeError):
    """没有配置好 API。给使用者一句人话，而不是 traceback。"""


def cfg():
    """读取 config.json。缺文件或缺 key 时抛 ConfigMissing（比如刚 clone、还没配 key）。"""
    global _CFG
    if _CFG is None:
        if not CFG_PATH.exists():
            raise ConfigMissing(
                "还没有配置文件：请把 config.example.json 复制成 config.json，"
                "并填入你自己的 DeepSeek API key（AI 出题 / 批改 / 复盘都需要它）。")
        _CFG = json.loads(CFG_PATH.read_text(encoding="utf-8"))
        key = (_CFG.get("api") or {}).get("api_key") or ""
        if not key or key.startswith("在这里") or key == "sk-你的-key":
            raise ConfigMissing("config.json 里的 api_key 还是占位符，请填入真实 key。")
    return _CFG


def chat(messages, *, thinking=None, max_tokens=2048, temperature=None, json_mode=False):
    """底层调用。thinking: None=用配置默认；True/False=强制开关。"""
    c = cfg()["api"]
    use_thinking = (c.get("thinking", "disabled") == "enabled") if thinking is None else thinking
    payload = {
        "model": c["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": c.get("temperature", 0.3) if temperature is None else temperature,
        "stream": False,
    }
    if not use_thinking:
        payload["thinking"] = {"type": "disabled"}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        c["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + c["api_key"],
        },
    )
    with urllib.request.urlopen(req, timeout=c.get("timeout_seconds", 180)) as r:
        res = json.loads(r.read().decode("utf-8"))
    content = res["choices"][0]["message"]["content"]
    usage = res.get("usage") or {}
    return content, usage


def chat_json(messages, **kw):
    """要求模型返回 JSON，并容错解析。"""
    kw["json_mode"] = True
    text, usage = chat(messages, **kw)
    return _loads(text), usage


def _loads(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"[\{\[].*[\}\]]", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


# ---------------------------------------------------------------- 数据访问

def outline_brief():
    """给 AI 看的大纲目录：板块 › 章 › 节的层级（不含 441 个考点，控制 token）。"""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id,parent_id,level,name,path FROM outline_nodes "
        "WHERE level IN ('board','chapter','section') ORDER BY id"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def points_of(section_ids):
    """取指定节下的全部考点。

    注意：当前运行时未使用（出题挂载走 section 级）。保留供后续精细化挂载使用。
    """
    if not section_ids:
        return []
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    qs = ",".join("?" * len(section_ids))
    rows = con.execute(
        f"SELECT id,parent_id,level,name,path FROM outline_nodes "
        f"WHERE level='point' AND parent_id IN ({qs}) ORDER BY id",
        list(section_ids),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- ① 解析学习记录

PARSE_SYS = """你是考研 311 教育学的学习记录解析器。
用户会用自然语言描述今天学了什么。你要把它映射到给定的大纲目录上。

规则：
1. 只从给定目录里选，不要编造节点。
2. 用户说「第X章」这类教材章节号，按**内容**判断对应大纲的哪一节，不要机械对号。
3. 返回 JSON：{"matches":[{"id":节点id,"name":"节点名","confidence":0.0-1.0,"reason":"依据"}],"unmatched":["无法定位的内容"]}
4. confidence 低于 0.6 的也返回，但要在 reason 里说明不确定。
"""


def parse_study_log(text, max_sections=60):
    """自然语言 → 候选大纲节。先粗筛，避免一次塞入全部节点。"""
    nodes = outline_brief()
    # 两阶段：先用「板/章/节」清单粗定位
    catalog = "\n".join(f"{n['id']}\t[{n['level']}]\t{n['path']}" for n in nodes)
    user = f"""今天的记录：
{text}

可选的本科目大纲目录（格式：id [层级] 路径）：
{catalog}

请选出与该记录相关的**节(section)级**节点，最多 {max_sections} 个。"""
    data, usage = chat_json(
        [{"role": "system", "content": PARSE_SYS}, {"role": "user", "content": user}],
        max_tokens=1500,
    )
    return data, usage


# ---------------------------------------------------------------- ② 出题

GEN_SYS = """你是考研 311 教育学专业基础的命题专家，严格依据统考大纲命题。

【难度要求 —— 最关键，不接受降低标准】
311 真题的客观题极少直接问定义。真实命题特征：
1. **情境化**：先给一个小情境（教学片段、教育现象、研究场景、政策文件、学者观点），
   再问「这体现了/属于/说明」——而不是「XX 的定义是」。
2. **认知层次**：主要考**理解、应用、分析**。**严禁**出「XX 是什么」这类纯识记题。
3. **干扰项质量**：四个选项必须处于同一抽象层次；干扰项要是**真实的常见误解**或**相近概念**
   （如同属一个理论流派的其他观点、易混的教育家主张），不能一眼排除。
4. **辨异**：高频考法是「下列选项中，符合/不符合……的是」，或「甲乙两人观点分别属于……」。
5. 题干信息量要够（2–5 句），必要时给学者原话、实验描述或数据。

【题型规范】
- 单选题：4 个选项，只有一个最符合要求；不要「以上都对」这类废选项。
- 辨析题(analysis, 15分)：给一个**可判断正误且含陷阱**的命题（常见型式：半对半错、
  概念偷换、以偏概全）。
- 简答题(short, 15分)：考要点组织，不是填空。
- 分析论述题(essay, 30分)：可含材料或多小问，考综合运用。

【其他】
- 每道题都要给出采分点（claim 核心论断 + evidence 判分依据），主观题 4-6 个。
- 严禁照抄历年真题原题；可参考风格，但必须改变情境与设问。
- 输出纯 JSON，不要解释文字。
"""

GEN_USER = """考点清单（id 为大纲节点 id，请为每道题标注它考的考点 id）：
{points}

请按以下配比出题：
- 单选题 {n_single} 道
- 辨析题 {n_analysis} 道、简答题 {n_short} 道、分析论述题 {n_essay} 道

返回 JSON，格式：
{{
  "single": [
    {{"outline_id": 307, "stem": "题干", 
      "options": {{"A": "…", "B": "…", "C": "…", "D": "…"}}, 
      "answer": "A", "explain": "解析"}}
  ],
  "subjective": [
    {{"outline_id": 307, "qtype": "short", "full_score": 15,
      "stem": "题干（材料题请用 \\n 分段）",
      "points": [{{"seq": 1, "claim": "核心论断", "evidence": "判分依据"}}]}}
  ]
}}"""


def real_questions(point_ids, limit_single=20, limit_subjective=3):
    """从真题库抽题。

    注意：真题的 outline_id 挂在 **section** 级，而调用方传进来的是 **point id**，
    因此必须先向上取父节点再一起匹配，否则一道都抽不到（已踩过）。
    """
    if not point_ids:
        return {"single": [], "subjective": []}
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    qs = ",".join("?" * len(point_ids))
    parents = [r["parent_id"] for r in con.execute(
        f"SELECT DISTINCT parent_id FROM outline_nodes WHERE id IN ({qs}) AND parent_id IS NOT NULL",
        list(point_ids))]
    ids = list({int(x) for x in list(point_ids) + parents})
    qs2 = ",".join("?" * len(ids))
    singles = con.execute(
        f"SELECT id,number,stem,options,answer,outline_id FROM questions "
        f"WHERE qtype='single' AND outline_id IN ({qs2}) "
        f"AND (source IS NULL OR source NOT LIKE 'AI%') LIMIT ?",
        ids + [limit_single],
    ).fetchall()
    subs = con.execute(
        f"SELECT id,number,qtype,stem,extra,outline_id FROM questions "
        f"WHERE qtype IN ('analysis','short','essay') AND outline_id IN ({qs2}) "
        f"AND (source IS NULL OR source NOT LIKE 'AI%') LIMIT ?",
        ids + [limit_subjective],
    ).fetchall()
    con.close()
    return {"single": [dict(r) for r in singles], "subjective": [dict(r) for r in subs]}


def shuffle_options(q):
    """打乱选项顺序并同步更正答案。

    为什么必须做：实测 AI 生成的单选题答案高度集中在某个字母（连出 3 道全 B），
    考生会看出规律。prompt 里要求「均衡分布」并不可靠，程序打乱才稳。
    """
    import random
    opts = q.get("options") or {}
    keys = sorted(opts.keys())
    if len(keys) != 4:
        return q
    correct = opts.get(q.get("answer"))
    vals = [opts[k] for k in keys]
    random.shuffle(vals)
    q["options"] = dict(zip(keys, vals))
    if correct is not None:
        q["answer"] = next((k for k, v in q["options"].items() if v == correct), q.get("answer"))
    return q


def generate_questions(points, n_single=20, n_analysis=1, n_short=1, n_essay=1):
    """按考点让 AI 出题。points: [{'id':..,'name':..,'path':..}]"""
    plist = "\n".join(f"{p['id']}\t{p.get('path') or p.get('name')}" for p in points)
    user = GEN_USER.format(
        points=plist, n_single=n_single, n_analysis=n_analysis,
        n_short=n_short, n_essay=n_essay,
    )
    data, usage = chat_json(
        [{"role": "system", "content": GEN_SYS}, {"role": "user", "content": user}],
        max_tokens=8000,
        temperature=0.6,
    )
    for q in data.get("single", []):
        shuffle_options(q)
    return data, usage


# ---------------------------------------------------------------- ③ 批改

_GRADE_SYS_CACHE = None


def grade_system_prompt():
    """读 docs/311主观题批改提示词.md 作为批改规则。"""
    global _GRADE_SYS_CACHE
    if _GRADE_SYS_CACHE is None:
        p = ROOT / "docs" / "311主观题批改提示词.md"
        _GRADE_SYS_CACHE = p.read_text(encoding="utf-8") if p.exists() else "你是 311 阅卷老师，踩点给分。"
    return _GRADE_SYS_CACHE


GRADE_EXTRA = """
【本次批改的额外要求 —— 必须遵守】
1. 除了按采分点给分外，还要单独判断「**言之有理加分项**」：
   学生答案中若有采分点之外、但符合大纲与教材、且论述成立的合理内容，
   列入 extra_credit 字段，**不并入命中采分点**，也不计入命中率。
2. 输出必须是纯 JSON，字段如下（不要 markdown 代码块）：
{
  "score": 11.5,
  "full_score": 15,
  "hit_seqs": [1, 2, 4],
  "miss_seqs": [3, 5],
  "extra_credit": [{"text": "学生多写的合理内容", "suggest_score": 1.0}],
  "cause": "记混",
  "one_line": "一句话总评",
  "per_paragraph": [{"text": "原文段落", "mark": "(得3分)", "comment": "段落点评"}],
  "optimized": "基于学生框架的优化版作答（补充处用 **粗体**）",
  "standard_points": [{"seq": 1, "claim": "采分点", "score": 3}]
}
3. cause 只能取：不会 / 记混 / 看漏条件 / 时间不够。
4. hit_seqs 中的序号必须来自题目给出的采分点序号。
"""


def grade_answer(question_stem, points, student_text=None, full_score=15,
                 use_thinking=True, image_data_url=None):
    """批改主观题。

    image_data_url: "data:image/jpeg;base64,..." —— 手写照片。
    有图片时走多模态：先识别手写内容，再按规则批改。
    """
    plist = "\n".join(
        f"{p['seq']}. {p['claim']}" + (f"（判分依据：{p['evidence']}）" if p.get("evidence") else "")
        for p in points
    )
    head = f"""【题目】（满分 {full_score} 分）
{question_stem}

【本题采分点】
{plist}
"""
    if image_data_url:
        tail = """
【学生作答】见下方图片（手写）。

请：
1. **先完整识别图片中的手写内容**，放进 `recognized` 字段；
2. 若个别字迹无法辨认，用 `〔?〕` 标注，并在 `recognize_note` 里说明；
3. 再按规则批改。
"""
        content = [
            {"type": "text", "text": head + tail},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]
        user_msg = {"role": "user", "content": content}
        extra = '\n5. 额外字段：`recognized`（识别出的学生原文）、`recognize_note`（识别存疑说明）。'
    else:
        user_msg = {"role": "user", "content": head + f"\n【学生作答】\n{student_text}\n\n请按规则批改，输出 JSON。"}
        extra = ""

    data, usage = chat_json(
        [
            {"role": "system", "content": grade_system_prompt() + "\n" + GRADE_EXTRA + extra},
            user_msg,
        ],
        max_tokens=8000,
        temperature=0.2,
        thinking=use_thinking,
    )
    return data, usage


# ---------------------------------------------------------------- ④ 学习复盘

REVIEW_SYS = """你是考研 311（教育学专业基础）的学习分析师。

用户会给你他**某一次练习**的完整数据：得分、做了哪些考点的题、错在哪里、历史练习记录。

请产出一份**基于数据**的学习报告，要求：

1. **只根据给你的数据说话**。数据不足时明确说"样本太少，暂无法判断"，不要编造趋势。
2. 必须包含这几块（Markdown）：
   - **本次结果**：得分、题量、正确率，用具体数字
   - **暴露的问题**：哪些**具体考点**失分（点名到考点，不要泛泛说"基础不牢"）
   - **与历史对比**：仅在与历史可比时才写趋势，否则说明为何不能比
   - **下次练什么**：2–3 条**可执行**建议，指明具体考点
3. 语气客观，**不要空泛鼓励**（"你已经很棒了"这类一律不要）。
4. 总长控制在 400 字以内，信息密度优先。
"""


def review_session(session, attempts_detail, weak_points, history_summary):
    """生成一次练习的复盘报告。

    session           sessions 表的一行
    attempts_detail   本次作答明细（含题目类型、考点名、结果、错因）
    weak_points       当前薄弱点列表
    history_summary   历史 sessions 摘要（用于趋势对比）
    """
    lines = [
        "【本次练习】",
        f"日期：{session.get('date')}　模式：{session.get('mode')}",
        f"客观题：{session.get('objective_right', 0)}/{session.get('objective_total', 0)} 对"
        f"（得分 {session.get('objective_score', 0)}）",
        f"主观自评：{session.get('subjective_count', 0)} 题，"
        f"采分点命中 {session.get('point_hits', 0)}/{session.get('point_total', 0)}",
    ]
    if session.get("note"):
        lines.append(f"备注：{session['note']}")

    lines.append("\n【本次作答明细】（题型 → 考点 → 结果）")
    for a in attempts_detail[:40]:
        h = a.get("hits")
        res = "对" if h == 1 else ("错" if h == 0 else f"命中 {h}/{a.get('total')}")
        extra = f"（错因：{a['cause']}）" if a.get("cause") else ""
        lines.append(f"- {a.get('qtype')} → {a.get('node_name') or '未挂考点'} → {res}{extra}")

    lines.append("\n【当前薄弱考点】（命中率升序前 10）")
    for w in weak_points[:10]:
        lines.append(f"- {w.get('name')}：{w.get('hit_n')}/{w.get('total_n')}"
                     f"（{w.get('rate', 0)*100:.0f}%）｜{w.get('path', '')}")

    lines.append("\n【历史练习记录】（最多 10 次）")
    if history_summary:
        for h in history_summary[:10]:
            lines.append(f"- {h.get('date')} {h.get('mode')}：客观 "
                         f"{h.get('objective_right', 0)}/{h.get('objective_total', 0)}，"
                         f"采分点 {h.get('point_hits', 0)}/{h.get('point_total', 0)}")
    else:
        lines.append("- （无历史记录。本次是第一次，请明确说明无法做趋势对比）")

    user = "\n".join(lines) + "\n\n请输出学习报告（Markdown）。"
    content, usage = chat(
        [{"role": "system", "content": REVIEW_SYS}, {"role": "user", "content": user}],
        max_tokens=1500,
        temperature=0.4,
    )
    return content.strip(), usage


# ---------------------------------------------------------------- 连通性自检

def ping():
    content, usage = chat([{"role": "user", "content": "只回答两个字：连通"}], max_tokens=32)
    return content.strip(), usage


if __name__ == "__main__":
    c = cfg()["api"]
    print(f"模型 {c['model']}  思考={c.get('thinking')}")
    txt, u = ping()
    print(f"连通性: {txt!r}  用量={u}")
    n = outline_brief()
    print(f"大纲目录节点（板/章/节）: {len(n)} 个")
