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
import random
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


# ---------------------------------------------------------------- ②b 全 AI 模拟组

HOT_TOPICS_PATH = ROOT / "source" / "教育热点.md"


def hot_topics(limit=4):
    """从 source/教育热点.md 取最近的若干条热点（供 AI 当论述题材料）。

    为什么要走文件：模型有知识截止时间，无法知道"用户眼里的近期"。
    文件由用户维护、只收官方来源，AI 只许引用其中的内容，不许自己编政策名/年份/文号。
    优先取**没有 `[已用于出题]` 标记**、且日期较新的条目；`【本周重点】` 优先。
    """
    if not HOT_TOPICS_PATH.exists():
        return []
    txt = HOT_TOPICS_PATH.read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^##\s+", txt)[1:]
    items = []
    for b in blocks:
        head = b.splitlines()[0].strip()
        if not head or head.startswith("维护提示"):
            continue
        m = re.match(r"(\d{4}-\d{2}-\d{2})\s*[｜|]\s*(.+)", head)
        date = m.group(1) if m else ""
        title = (m.group(2) if m else head).strip()
        used = "[已用于出题]" in b
        star = "【本周重点】" in b
        items.append({"date": date, "title": title, "used": used,
                      "star": star, "body": b.strip()})
    items.sort(key=lambda x: (x["star"], x["used"], x["date"]), reverse=True)
    # 正文给足：截断会让 AI 看不到完整背景，可能在材料里"补"出原文没有的文号（误报为编造）
    for it in items:
        it["body"] = it["body"][:6000]
    return items[:limit]


AI_SET_SINGLE_SYS = """你是考研 311（教育学专业基础）统考的命题专家。本组题**全部由你原创**，
不得使用任何历年真题原题，也不得照抄真题的题干与选项。

【本次任务的硬性要求】
1. **难度对标 311 真题的偏难区间**。命题方式必须像真题：
   - **情境化**：先给一个具体情境（教学片段、课堂对话、学校做法、研究场景、政策文本片段、
     学者观点、实验或调查描述），再问「这体现了/属于/说明/最适合用……解释」。
   - **严禁纯识记题**：不许出「XX 的定义是」「XX 是谁提出的」「XX 发表于哪一年」这种
     背了就会、没背就不会的题。至少要有**辨析、比较、应用、归因**这四类认知操作中的一种。
   - **反面例子（这些写法实测就是识记题，一律不要）**：
     ✗「裴斯泰洛齐的要素教育对课程与教学论的主要贡献在于」——四个选项各贴一个标签，记住标签就会做。
     ✗「斯腾豪斯过程模式最突出的特点是」——人物—主张连线题。
     ✗「泰勒目标模式的核心意图是」——背过"三个来源两道筛子"就能选。
     ✓ 正确做法：给一段**真实的课堂/学校场景**（谁做了什么、出现了什么结果），
       问「这主要体现了哪种取向 / 最可能的原因是什么 / 要改进应先调整哪一环」，
       选项分别对应不同理论解释，**必须理解理论才能判断哪个更贴合情境**。
   - **干扰项必须是真实的常见误解**：来自同一流派的其他观点、易混概念、常见的过度推广，
     四个选项处于同一抽象层次，不能一眼排除，也不能出现「以上都对/都错」。
   - **题干信息量足够**：一般 2–5 句；允许引用学者原话、数据、政策表述。
   - **严禁与历年真题雷同**：不许直接照搬真题里出现过的原句（如「教师可以少教，学生可以多学」
     「大自然希望儿童在成人以前就要像儿童的样子」这类被反复考的句子），
     也不要复刻真题的设问方式；要换角度、换情境、换设问。
2. **主题统一**：全部围绕用户给出的「今日学习内容」所覆盖的考点来出，不要跑到别的板块。
   **识记题占比不得超过 20%（20 道里最多 4 道）**：教育史与教育思想史类主题难免涉及史实，
   但绝大多数题目必须包装成情境判断，让"背过标签"不足以作答。
3. 每题必须给出 `explain`（解析）：说清正确项为什么对、**至少两个干扰项为什么错**。
   **绝对禁止在解析里写「A/B/C/D 项」「故选 B」这类字母引用** —— 因为出题后系统会重新打乱
   选项顺序，字母会对不上（踩过：解析写「故 B 正确」而答案已变成 C）。
   要指代选项时，用**它说的是什么**来表示，例如「把要素教育等同于要素主义的那一项」「主张教育即生活的那一项」。
4. 每题标出它考的大纲考点 id（从给定清单里选）。
5. 答案分布要均衡（A/B/C/D 各约 5 道，不要集中在某一字母）。
6. 输出纯 JSON，不要解释文字、不要 markdown 代码块。

输出格式：
{"single": [
  {"outline_id": 307, "stem": "题干（可含换行）",
   "options": {"A": "…", "B": "…", "C": "…", "D": "…"},
   "answer": "A", "explain": "解析：正确项之所以正确是因为……；把 X 与 Y 混为一谈的那一项错在……",
   "cognitive": "应用", "difficulty": 4}
]}
其中 `cognitive` ∈ {理解, 应用, 分析, 评价}（**不允许填「识记」**），`difficulty` 为 1–5 的整数。
"""

AI_SET_SINGLE_USER = """今日学习内容（主题由它决定）：
{study}

可用的考点清单（id → 大纲路径，请为每题选一个）：
{points}

请出 **{n} 道单项选择题**，全部围绕上述考点，风格与难度对标 311 真题。
"""

AI_SET_SUBJ_SYS = """你是考研 311（教育学专业基础）统考的命题专家。本组题**全部由你原创**，
不得使用任何历年真题原题。

【本次任务的硬性要求】
1. 三道主观题主题与单选题保持一致，且**难度对标 311 真题**：考要点组织、比较分析、综合运用，
   不要出可以一句话答完的题。
2. **辨析题(analysis, 15分)**：给一个**可判断正误且含陷阱**的命题
   （半对半错、概念偷换、以偏概全、把两个层次混为一谈），不能是显而易见的对或错。
3. **简答题(short, 15分)**：问"简述/比较/分析……"，答案是 3–5 个要点，不是填空。
4. **分析论述题(essay, 30分)——这是本组最重要的一道，必须满足**：
   - **必须包含材料**：把下面提供的**真实政策热点**作为材料（可摘引其表述，但不要整段照抄），
     以「阅读下列材料，按要求回答问题」开头，材料后给出 2–3 个小问。
   - **只许使用给定热点材料里的政策名称、年份、机构与提法**，**严禁自己编造**文件名、
     文号、年份或数据。
   - 小问要能落到大纲考点上（如教育与社会发展、课程改革、教师专业发展、教育评价等），
     并体现"用教育学理论分析现实问题"的统考取向。
5. 每道题拆 **4–6 个采分点**（claim 核心论断 + evidence 判分依据），按论点/维度拆，不要按句子拆。
6. 输出纯 JSON，不要解释文字、不要 markdown 代码块。

输出格式：
{"subjective": [
  {"outline_id": 307, "qtype": "analysis", "full_score": 15,
   "stem": "题干（材料题用 \\n 分段，小问各占一行）",
   "points": [{"seq": 1, "claim": "核心论断", "evidence": "判分依据"}],
   "difficulty": 4}
]}
"""

AI_SET_SUBJ_USER = """今日学习内容（主题由它决定）：
{study}

可用的考点清单（id → 大纲路径）：
{points}

【可用的真实热点材料 —— 论述题**只能**从这里取材，不许编造】
{topics}

请出 3 道主观题：1 道辨析题(analysis)、1 道简答题(short)、1 道分析论述题(essay, 带材料)。
"""

AI_SET_CHECK_SYS = """你是 311 命题质量审核员。用户会给你一组刚生成的模拟题（含单选与主观题）。
请按下面标准逐条审核，**严格、不要放水**：

1. 单选里有没有**纯识记题**（背定义/人名/年份就能答）？有就列出来。
2. 有没有**一眼排除**的劣质干扰项、或「以上都对/都错」这类废选项？
3. 答案分布是否严重集中在某一字母（超过一半）？
4. 主观题：辨析题是否有真陷阱；简答题是否只是一句话能答完；**论述题是否真的带材料、
   且材料是否来自给定的真实热点**（若材料里出现给定热点之外的"政策名称/年份/文号"，判定为编造）。
5. 有没有和历年真题明显雷同的题（照抄题干或选项）？
6. 有没有题干残缺、选项缺项、采分点少于 3 个的题？

输出纯 JSON：
{"pass": true/false,
 "issues": [{"type": "识记题|劣质选项|答案集中|编造热点|疑似真题|残缺", "target": "第几题/题号", "detail": "问题描述"}],
 "must_retry": true/false,
 "comment": "一句话总评"}
`must_retry` 只在**必须重出**时填 true（有识记题、编造热点、残缺、或答案严重集中）。
"""


def balance_answer_distribution(singles):
    """把一批单选题的正确答案**均衡**铺到 A/B/C/D 上（就地修改）。

    为什么不用"让 AI 自己均衡"或"答案集中就重出"：
      - prompt 里要求均衡不可靠（实测同一批题里 B 占 8/20）；
      - 随机分布下 4 选项的最大占比天然就有 ~37%，把它当"异常"会误判、白白重出。
    做法（在 shuffle_options 打乱选项**之后**调用）：
      把每题的正确选项摘出来，与其余选项按**尽量交替**的顺序重新拼回去，
      并同步改写 answer —— 这样答案分布必然均衡（差值 ≤1），且不会出现全同一字母。
    """
    LETTERS = ["A", "B", "C", "D"]
    n = len(singles)
    if n < 4:
        return singles
    order = list(range(n))
    random.shuffle(order)                      # 谁拿哪个字母也随机，避免"前几题总是 A"
    # 正确项最多分到 ceil(n/4) 个
    cap = (n + len(LETTERS) - 1) // len(LETTERS)
    assign = []
    for i, idx in enumerate(order):
        assign.append(LETTERS[min(i // cap, len(LETTERS) - 1)])
    for idx, correct_letter in zip(order, assign):
        q = singles[idx]
        opts = q.get("options") or {}
        if len(opts) != 4:
            continue
        keys = sorted(opts.keys())
        old_answer = (q.get("answer") or "").upper()
        if old_answer not in opts:
            continue
        correct_text = opts[old_answer]
        others = [opts[k] for k in keys if k != old_answer]
        random.shuffle(others)
        # 正确项放在目标位置，其余按顺序填入
        target = keys.index(correct_letter)
        new_vals = others[:target] + [correct_text] + others[target:]
        q["options"] = dict(zip(keys, new_vals))
        q["answer"] = correct_letter
    return singles


def audit_ai_set(paper):
    """程序层硬校验（不依赖 AI 自评，这几项能确定性判定）。

    为什么需要：AI 自评会漏、也会放水。下面三条都是实测踩到过的真问题：
      ① 解析里引用选项字母（如「故 B 正确」）—— 打乱选项后字母必然对不上；
      ② 答案集中在一个字母（实测 20 题里 B 占 8 道）；
      ③ 主观题采分点过少（无法自评）。
    """
    issues = []
    singles = paper.get("single", [])
    subs = paper.get("subjective", [])

    # ① 解析里的字母引用
    letter_ref = re.compile(r"(?:故|选|答案(?:是|为|选)?|正确(?:项|答案)?(?:是|为)?)\s*[ABCD]\b"
                            r"|[ABCD]\s*(?:项|选项)")
    n_ref = sum(1 for q in singles if letter_ref.search(q.get("explain") or ""))
    if n_ref:
        issues.append({"type": "解析引用字母", "target": f"{n_ref} 道单选",
                       "detail": "解析里写了选项字母，打乱选项后必然与答案矛盾"})

    # ② 答案分布（已由 balance_answer_distribution 均衡；这里只做**兜底告警**）
    #    阈值取 0.5 而不是 0.35：4 选项随机分布下最大占比天然就有 ~37%，
    #    用 35% 判"集中"会把正常卷子误判成问题（踩过：程序与 AI 双方都误报，导致无限重出）。
    from collections import Counter
    dist = Counter((q.get("answer") or "").upper() for q in singles if q.get("answer"))
    total = sum(dist.values())
    if total:
        top, cnt = dist.most_common(1)[0]
        if cnt / total > 0.5:
            issues.append({"type": "答案集中", "target": f"{top} 占 {cnt}/{total}",
                           "detail": "答案过于集中，考生能靠规律蒙"})

    # ③ 采分点数量
    for i, q in enumerate(subs, 1):
        if len(q.get("points") or []) < 3:
            issues.append({"type": "采分点过少", "target": f"主观第 {i} 题",
                           "detail": f"只有 {len(q.get('points') or [])} 个采分点，无法自评"})

    # ④ 题干/选项残缺
    for i, q in enumerate(singles, 1):
        if len((q.get("stem") or "").strip()) < 15:
            issues.append({"type": "题干过短", "target": f"单选第 {i} 题", "detail": "题干信息量不足"})
        opts = q.get("options") or {}
        if len(opts) != 4 or any(not str(v).strip() for v in opts.values()):
            issues.append({"type": "选项残缺", "target": f"单选第 {i} 题", "detail": f"选项 {len(opts)} 个"})
        if (q.get("answer") or "").upper() not in ("A", "B", "C", "D"):
            issues.append({"type": "答案缺失", "target": f"单选第 {i} 题", "detail": str(q.get("answer"))})

    # ⑤ 识记题比例：**不要求 0**。教育学史/思想史这类主题不可能完全避开史实，
    #    硬要求 0 会让 AI 反复重出仍不达标（实测两轮都在 3–4 道）。改成"不超过三成"。
    n_remember = sum(1 for q in singles if (q.get("cognitive") or "") in ("识记", "记忆"))
    if singles and n_remember / len(singles) > 0.3:
        issues.append({"type": "识记题偏多", "target": f"{n_remember}/{len(singles)} 道",
                       "detail": "识记题超过三成，需要更多情境化/应用类题目"})

    # ⑥ 组内重复：AI 实测会在同一批里出两道一模一样的题（只换了选项顺序）
    def sig(q):
        return re.sub(r"\s+", "", (q.get("stem") or ""))[:40]

    seen, dups = {}, []
    for i, q in enumerate(singles, 1):
        s = sig(q)
        if len(s) < 8:
            continue
        if s in seen:
            dups.append(f"第{seen[s]}题与第{i}题")
        else:
            seen[s] = i
    if dups:
        issues.append({"type": "组内重复", "target": "、".join(dups[:3]),
                       "detail": "同一批题里出现题干雷同的题，属于凑数"})

    return {"issues": issues, "answer_dist": dict(dist), "remember_n": n_remember,
            "hard_fail": bool([i for i in issues if i["type"] in (
                "解析引用字母", "答案集中", "采分点过少", "题干过短", "选项残缺",
                "答案缺失", "识记题偏多", "组内重复")])}


def generate_ai_set(study_text, points, n_single=20, topics=None, max_attempts=2):
    """一次生成「全 AI 模拟组」：{n_single} 单选 + 辨析/简答/论述各 1。

    分两批调用（单选 / 主观）——合成一次调用在 20 题规模上容易截断，且失败看不出原因。
    流程：生成 → 程序层硬校验 + AI 质检 → 不合格就**带着问题重出一组**（最多 max_attempts 次）。

    返回 (paper, usage合计, 质检结果)；质检结果里含 attempts 与每一轮的 issues。
    """
    plist = "\n".join(f"{p['id']}\t{p.get('path') or p.get('name')}" for p in points)
    topics = topics if topics is not None else hot_topics()
    topics_txt = "\n\n".join(f"【热点 {i+1}】{t['date']} {t['title']}\n{t['body']}"
                             for i, t in enumerate(topics)) or "（热点清单为空：请出一道经典的、不依赖时事的材料论述题）"
    usage_total = {}

    def add_usage(u):
        for k in ("prompt_tokens", "completion_tokens"):
            usage_total[k] = usage_total.get(k, 0) + (u or {}).get(k, 0)

    history = []
    paper, audit, check = None, None, None
    for attempt in range(1, max_attempts + 1):
        # 重出时把上一轮的问题甩回给 AI（比单纯"再来一次"有效）
        feedback = ""
        if history:
            prev = history[-1]
            lines = [f"- [{i['type']}] {i['target']}：{i['detail']}"
                     for i in prev["issues"][:8]]
            feedback = ("\n\n【上一轮生成被判定不合格，必须避免下列问题后重新命题】\n"
                        + "\n".join(lines) + "\n请重新出一整套新题（不要只是改几个字）。")

        user1 = AI_SET_SINGLE_USER.format(
            study=(study_text or "（未填写，按考点清单覆盖的内容出题）") + feedback,
            points=plist, n=n_single)
        singles, u1 = chat_json(
            [{"role": "system", "content": AI_SET_SINGLE_SYS}, {"role": "user", "content": user1}],
            max_tokens=16000, temperature=0.7 + 0.1 * (attempt - 1))
        add_usage(u1)

        user2 = AI_SET_SUBJ_USER.format(
            study=(study_text or "（未填写）") + feedback, points=plist, topics=topics_txt)
        subs, u2 = chat_json(
            [{"role": "system", "content": AI_SET_SUBJ_SYS}, {"role": "user", "content": user2}],
            max_tokens=10000, temperature=0.7)
        add_usage(u2)

        paper = {"single": singles.get("single", [])[:n_single],   # 只要求 20 道，多给的一律裁掉
                 "subjective": subs.get("subjective", [])}
        for q in paper["single"]:
            shuffle_options(q)
        balance_answer_distribution(paper["single"])               # 打乱后再均衡答案分布

        audit = audit_ai_set(paper)

        check = {"pass": None, "issues": [], "must_retry": False, "comment": "（未跑 AI 质检）"}
        try:
            check_txt = json.dumps(paper, ensure_ascii=False)[:24000]
            check, u3 = chat_json(
                [{"role": "system", "content": AI_SET_CHECK_SYS},
                 {"role": "user", "content": "热点清单：\n" + topics_txt[:4000] +
                                             "\n\n待审题目：\n" + check_txt}],
                max_tokens=2500, temperature=0.2)
            add_usage(u3)
        except Exception as e:
            check = {"pass": None, "issues": [], "must_retry": False,
                     "comment": f"质检调用失败：{type(e).__name__}: {e}"}

        all_issues = audit["issues"] + list(check.get("issues") or [])
        history.append({"attempt": attempt, "issues": all_issues,
                        "answer_dist": audit["answer_dist"],
                        "ai_pass": check.get("pass"), "ai_must_retry": check.get("must_retry")})

        # 重出判定**以程序层硬校验为准**（确定性的那几项）。
        # AI 审核意见只作为展示与提示 —— 否则它每轮都能挑出新毛病（实测两轮都要求重出），
        # 重出到上限后反而交出一份"程序判定更差"的题。
        need_retry = audit["hard_fail"]
        if not need_retry:
            break

    return paper, usage_total, {
        "attempts": len(history),
        "history": history,
        "program_audit": audit,
        "ai_check": check,
        "final_ok": not audit["hard_fail"],
        "ai_flagged": bool((check or {}).get("must_retry")),
    }


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
