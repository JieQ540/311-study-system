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
# 让 app/ 一定可导入（ai.py 可能被 server.py 以任意 cwd 导入）
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 库的位置必须和 server.py 完全一致，否则会出现「服务读 A 库、AI 写 B 库」。
# 统一走 dbpath，顺便也就认了 DSH_DB（旧版本这里不认，和 server.py 不一致）。
from dbpath import DB  # noqa: E402

CFG_PATH = ROOT / "config.json"

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


def chat(messages, *, thinking=None, max_tokens=2048, temperature=None, json_mode=False,
         timeout=None):
    """底层调用。thinking: None=用配置默认；True/False=强制开关。

    timeout: 秒。默认取 config 的 timeout_seconds；**出题调用必须单独放宽** ——
    开思考后一次 20 题的请求实测远超旧的 180 秒（见 gen_limits 的说明）。
    """
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
    with urllib.request.urlopen(
            req, timeout=c.get("timeout_seconds", 180) if timeout is None else timeout) as r:
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

# ---- ②a 难度档位 ----------------------------------------------------------
# 五档难度，**每一档都给"可观察的命题特征"**，而不是"难一点/简单一点"这种形容词：
# 实测模型对抽象难度词基本无感（"难度要对标真题"写了它也当耳边风）。
# 程序层唯一能校验的是模型**自评的 difficulty 数字**，所以真正起作用的是下面这套描述，
# 数字校验只当兜底（校验不过就带着问题重出）。
DIFFICULTY_LEVELS = {
    1: {"name": "偏易 · 打基础",
        "scene": "一两句直白的短情境，条件与人物都摆在明面上",
        "steps": "认准考的是哪个概念即可（1 步）",
        "distractor": "四个选项分属明显不同的类别或时期，学过就能排除",
        "recall": "识记题最多三成",
        "note": "用来检查基础概念有没有记住、有没有混淆"},
    2: {"name": "中等",
        "scene": "一段简短情境（课堂片段、学校做法、教育现象），要读完再判断",
        "steps": "先认出概念、再对号入座（1–2 步）",
        "distractor": "干扰项是与正确项相邻的易混概念",
        "recall": "识记题最多三成",
        "note": "常规练习用"},
    3: {"name": "对标真题（默认）",
        "scene": "真题式情境：课堂对话、学校做法、学者观点、研究场景或政策片段，一般 2–5 句",
        "steps": "读懂情境 → 匹配理论 → 排除近似项（2 步）",
        "distractor": "同一抽象层次上的近义概念，必须理解理论才能排除",
        "recall": "识记题最多三成",
        "note": "与 311 真题的常见难度一致"},
    4: {"name": "偏难",
        "scene": "多主体、多变量的长情境（两种做法对比、一组调查数据、一段政策争议），常含无关信息",
        "steps": "先剥离无关信息，再做两步以上推断（2–3 步）",
        "distractor": "来自**同一理论流派**的相近主张，或对同一现象的两种合理解释，"
                      "只有一条最贴合题干的限定条件",
        "recall": "识记题不超过一成",
        "note": "用来拉开区分度"},
    5: {"name": "极难",
        "scene": "跨章节综合的长材料（可含数据、图表描述、相互冲突的观点），信息要自行取舍",
        "steps": "3 步以上：定位考点 → 比较多种理论解释 → 用题干限定条件排除",
        "distractor": "每一项都像常见的错误理解，必须精确辨析概念边界才能选出唯一最贴合的一项",
        "recall": "不要出识记题",
        "note": "冲刺用，错得多很正常"},
}
DEFAULT_DIFFICULTY = 3


def normalize_difficulty(v):
    """把外部传进来的难度收敛到 1–5；非法值一律回默认档。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_DIFFICULTY
    return min(5, max(1, n))


def difficulty_block(level):
    """把难度档渲染成要插进出题 system prompt 的命题要求。"""
    level = normalize_difficulty(level)
    d = DIFFICULTY_LEVELS[level]
    return f"""【本次难度档位：{level} / 5 —— {d['name']}】
- 情境复杂度：{d['scene']}
- 需要几步推理：{d['steps']}
- 干扰项要求：{d['distractor']}
- 认知层次：{d['recall']}（**这是本档位的硬指标，不是建议**）
- 全组每道题都按这一档来，自评 `difficulty` 一律贴近 {level}（允许 ±1，不要整组飘到别的档位）。
- 本档用途：{d['note']}"""


def gen_limits(thinking):
    """出题调用的 max_tokens / 超时（按是否开思考取不同的值）。

    **开思考必须放宽 max_tokens**：推理 token 也算在 completion 里，20 题的正文约 8k，
    加上推理很容易撞上旧的 16000 上限 —— 一旦截断，返回的 JSON 就不完整，
    整个出题会以"解析失败"告终。超时同理，旧的 180 秒对开思考的出题调用不够。

    （探测实测，同一个"出 3 道单选题"的任务：关思考输出 154 tokens；
      开思考输出 1661 tokens，其中推理占 1456。）
    """
    try:
        g = cfg().get("generate") or {}
    except Exception:
        # 这个函数**只是读配置**，不该因为缺 config.json / key 就抛异常 ——
        # 公开副本里刚 clone、还没配 key 时就是这种状态，一抛就会让调用方以为
        # "出题逻辑坏了"，而其实是"还没配 key"（该报的是后者）。
        g = {}
    if thinking:
        return {"single": g.get("single_max_tokens_thinking", 32000),
                "subj": g.get("subj_max_tokens_thinking", 20000),
                # 质检也要给足：实测开思考时它会把 4000 全用在推理上、正文返回空串，
                # 于是 JSON 解析失败、整份质检报告拿不到（真实生成里踩到过）。
                "check": g.get("check_max_tokens_thinking", 12000),
                "points": g.get("points_max_tokens_thinking", 20000),
                "timeout": g.get("timeout_seconds", 600),
                "plain_check": g.get("check_max_tokens", 2500),
                "plain_timeout": g.get("timeout_seconds_plain", 180)}
    return {"single": g.get("single_max_tokens", 16000),
            "subj": g.get("subj_max_tokens", 10000),
            "check": g.get("check_max_tokens", 2500),
            "points": g.get("points_max_tokens", 8000),
            "timeout": g.get("timeout_seconds_plain", 180),
            "plain_check": g.get("check_max_tokens", 2500),
            "plain_timeout": g.get("timeout_seconds_plain", 180)}


def add_usage(total, u):
    """累加用量。**单独统计推理 token** —— 开思考后它就是主要成本，
    不统计的话"贵在哪"完全看不见（实测同一任务推理占输出的 88%）。"""
    for k in ("prompt_tokens", "completion_tokens"):
        total[k] = total.get(k, 0) + (u or {}).get(k, 0)
    r = ((u or {}).get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    if r:
        total["reasoning_tokens"] = total.get("reasoning_tokens", 0) + r
    return total


def estimate_cost(usage):
    """按 DeepSeek 价目粗估花费（输入 1 元/百万、输出 4 元/百万）。"""
    u = usage or {}
    return u.get("prompt_tokens", 0) / 1e6 + u.get("completion_tokens", 0) / 1e6 * 4

GEN_SYS = """你是考研 311 教育学专业基础的命题专家，严格依据统考大纲命题。

@@DIFFICULTY@@

【无论哪一档都必须做到】
1. **题干必须有情境**：先给一个具体情境（教学片段、课堂对话、学校做法、教育现象、研究场景、
   政策文件、学者观点），再问「这体现了/属于/说明/最适合用……解释」。
   **严禁**直接问「XX 的定义是」「XX 是谁提出的」「XX 发表于哪一年」这类背了就会的题。
2. **认知层次**：主要考**理解、应用、分析**，至少含辨析、比较、应用、归因之一。
3. **干扰项质量**：四个选项必须处于同一抽象层次；干扰项要是**真实的常见误解**或**相近概念**
   （如同属一个理论流派的其他观点、易混的教育家主张），不能一眼排除。
4. **情境外壳测试（必做自查）**：把情境去掉以后，如果这道题退化成
   「某人的主张是什么」「XX 的定义/特点是什么」这种**人物—主张配对或背标签题**，
   那它就是**披着情境外壳的识记题**，必须重新设计。
   - 实测反例（2026-09-17 真实生成里被质检点名的写法，一律不要）：
     ✗「一位教师受到裴斯泰洛齐的启发，先让儿童数物品、画线条、说单词……问这体现了裴斯泰洛齐的什么思想？」
       —— 去掉情境就是「要素教育是什么」，记住标签就会做。
     ✗「一位教师不预设行为目标、围绕有争议议题组织课程……问这属于哪种课程开发模式？」
       —— 去掉情境就是「斯腾豪斯过程模式的特点」。
   - ✓ 正确做法：情境里的**具体条件必须参与判断**（谁做了什么、出现了什么结果、哪一点与理论相冲突），
     让人"背得出理论定义"仍然不足以作答，必须拿情境去比对理论。
5. **正确项不得明显比其他选项长或"更全面"**：学生不该靠"哪项最长最完整"就蒙对。
   正确项与其余三项的平均字数差控制在 10 字以内 ——
   **这一条有程序层校验，违反会被判不合格并整组重出**。
6. **辨异**：高频考法是「下列选项中，符合/不符合……的是」，或「甲乙两人观点分别属于……」。
7. 题干信息量要够（2–5 句），必要时给学者原话、实验描述或数据。

【命题流程 —— 请先在脑中走完这四步，再一次性输出 JSON】
1. 确认每道题考的是所给清单里的哪一个考点；
2. 为每题构造情境，并确认这个情境**唯一地**指向正确项；
3. 设计三个"像对但错"的干扰项，逐项确认它们到底错在哪；
4. 最后校准难度与选项字数，确认正确项没有明显更长。
**不要把推理过程写进输出，只输出 JSON。**

【题型规范】
- 单选题：4 个选项，只有一个最符合要求；不要「以上都对」这类废选项。
- 辨析题(analysis, 15分)：给一个**可判断正误且含陷阱**的命题（常见型式：
  表述看似一半合理、实则错误；概念偷换；以偏概全）。
  ⚠️ 「半对半错」说的是**命题手法**（引学生上钩），**不是答案**：
  这类命题的正确结论是「错误」或「片面」，学生必须给出**单一**判断。
- 简答题(short, 15分)：考要点组织，不是填空。
- 分析论述题(essay, 30分)：可含材料或多小问，考综合运用。

【其他】
- 每道题都要给出采分点（claim 核心论断 + evidence 判分依据），主观题 4-6 个。
  **辨析题的第 1 个采分点必须是「判断正误」**（写「该说法错误：……」或「该说法正确：……」）——
  官方判分是两层：判断正误 3 分（判断错则全题不超过 3 分）+ 阐明理由 12 分。
- 客观题与主观题都要自评 `difficulty`（1–5 的整数），并贴住上面给定的难度档位。
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


def generate_questions(points, n_single=20, n_analysis=1, n_short=1, n_essay=1,
                       difficulty=None, thinking=True, max_attempts=2):
    """按考点让 AI 出题。points: [{'id':..,'name':..,'path':..}]

    difficulty: 1–5 难度档（None → 默认档）；thinking: 出题时是否开思考模式。
    返回 (data, usage, audit)：audit 是程序层校验结果（难度越档 / 正确项过长 / 情境…）。

    为什么这里也要重出一轮：「难度要贴合档位」「正确项不许明显更长」这两条
    **靠提示词约束不住**（模型自评与提示词都不够可靠），必须程序层判定后
    带着问题清单重出 —— 与「全 AI 模拟组」用的是同一套机制。
    """
    difficulty = normalize_difficulty(difficulty)
    lim = gen_limits(thinking)
    plist = "\n".join(f"{p['id']}\t{p.get('path') or p.get('name')}" for p in points)
    sys_prompt = GEN_SYS.replace("@@DIFFICULTY@@", difficulty_block(difficulty))

    data, audit, usage_total = None, None, {}
    for attempt in range(1, max_attempts + 1):
        feedback = ""
        if audit and audit["hard_fail"]:
            lines = [f"- [{i['type']}] {i['target']}：{i['detail']}"
                     for i in audit["issues"][:6]]
            feedback = ("\n\n【上一轮被判不合格，必须避免下列问题后重新命题（不要只改几个字）】\n"
                        + "\n".join(lines))
        user = GEN_USER.format(
            points=plist, n_single=n_single, n_analysis=n_analysis,
            n_short=n_short, n_essay=n_essay,
        ) + feedback
        data, usage = chat_json(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
            max_tokens=lim["points"],
            temperature=0.6 + 0.1 * (attempt - 1),
            thinking=thinking,
            timeout=lim["timeout"],
        )
        add_usage(usage_total, usage)
        for q in data.get("single", []):
            shuffle_options(q)
        audit = audit_ai_set({"single": data.get("single", []),
                              "subjective": data.get("subjective", [])},
                             difficulty=difficulty)
        if not audit["hard_fail"]:
            break
    data["_audit"] = audit
    return data, usage_total, audit


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
0. @@DIFFICULTY@@
   ↑ 这是本次的难度基准：全组每道题都要贴住它，`difficulty` 自评也照它填。
1. **情境化（任何档位都必须做到）**：先给一个具体情境（教学片段、课堂对话、学校做法、
   研究场景、政策文本片段、学者观点、实验或调查描述），再问「这体现了/属于/说明/最适合用……解释」。
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
   - **情境外壳测试（必做自查）**：把情境去掉以后，如果退化成「某人的主张是什么」
     「XX 的定义/特点是什么」这种**人物—主张配对或背标签题**，那它就是**披着情境外壳的识记题**，
     必须重新设计。实测反例（真实生成里被质检点名的写法，一律不要）：
     ✗「一位教师受到裴斯泰洛齐的启发，先让儿童数物品、画线条、说单词……问这体现了裴斯泰洛齐的什么思想？」
     ✗「一位教师不预设行为目标、围绕有争议议题组织课程……问这属于哪种课程开发模式？」
     ✓ 情境里的**具体条件必须参与判断**（谁做了什么、出现了什么结果、哪一点与理论相冲突），
       让人"背得出理论定义"仍然不足以作答。
   - **正确项不得明显比其他选项长或"更全面"**：四个选项字数要接近（正确项与其余三项的
     平均字数差控制在 10 字以内）。**这一条有程序层校验，违反会整组重出。**
   - **题干信息量足够**：一般 2–5 句；允许引用学者原话、数据、政策表述。
   - **严禁与历年真题雷同**：不许直接照搬真题里出现过的原句（如「教师可以少教，学生可以多学」
     「大自然希望儿童在成人以前就要像儿童的样子」这类被反复考的句子），
     也不要复刻真题的设问方式；要换角度、换情境、换设问。
2. **主题统一**：全部围绕用户给出的「今日学习内容」所覆盖的考点来出，不要跑到别的板块。
   识记题比例**按上面的难度档位执行**（档位里写了上限），超过上限会被判不合格。
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
0. @@DIFFICULTY@@
   ↑ 主观题同样按这一档来：情境/材料的复杂度、要求的能力层次都照它。
1. 三道主观题主题与单选题保持一致：考要点组织、比较分析、综合运用，
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
   **辨析题的第 1 个采分点必须是「判断正误」**：明确写「该说法错误：……」或「该说法正确：……」。
   为什么必须单列：官方判分是两层 —— 判断正误（3 分，判断错则全题不超过 3 分）+ 阐明理由（12 分），
   判断项混在理由里学生就无法自评这一维。
   **判断只能是"错误"或"正确"二者之一**，不许写成"半对半错/有对有错"——
   命题可以"看似半对"来设陷阱，但结论必须落到一个方向（这类命题的正确判断通常是"错误"或"片面"）。
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
4. **每题难度是否贴住题目开头的「难度档位」？** 有没有整组偏易或偏难的？
5. **正确项是否明显比其它三个选项更长、更"全面"**（学生不看内容、挑最长的就能蒙对）？
6. **题干有没有真的给情境**（课堂/学校/研究/政策等具体场景），还是直接问知识点？
7. 主观题：辨析题是否有真陷阱；简答题是否只是一句话能答完；**论述题是否真的带材料、
   且材料是否来自给定的真实热点**（若材料里出现给定热点之外的"政策名称/年份/文号"，判定为编造）。
8. 有没有和历年真题明显雷同的题（照抄题干或选项）？
9. 有没有题干残缺、选项缺项、采分点少于 3 个的题？

输出纯 JSON：
{"pass": true/false,
 "issues": [{"type": "识记题|劣质选项|答案集中|难度不符|正确项过长|缺情境|编造热点|疑似真题|残缺", "target": "第几题/题号", "detail": "问题描述"}],
 "must_retry": true/false,
 "comment": "一句话总评"}
`must_retry` 只在**必须重出**时填 true（有识记题、编造热点、残缺、答案严重集中、难度明显不符）。
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


def audit_ai_set(paper, difficulty=None):
    """程序层硬校验（不依赖 AI 自评，这几项能确定性判定）。

    为什么需要：AI 自评会漏、也会放水。下面这些条都是实测踩到过的真问题：
      ① 解析里引用选项字母（如「故 B 正确」）—— 打乱选项后字母必然对不上；
      ② 答案集中在一个字母（实测 20 题里 B 占 8 道）；
      ③ 主观题采分点过少（无法自评）；
      ④ 难度没贴住用户选的档位（自评数字越档/均值跑偏）；
      ⑤ 正确项明显比其它选项长 —— 考生不看内容、挑最长的就能蒙对。

    difficulty: 目标难度档（1–5）。给了才做难度校验。
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

    # ⑦ 辨析题必须有「判断」采分点：官方判分是两层（判断正误 3 分 + 阐明理由 12 分），
    #    首个采分点不是判断，就等于丢了这一维，学生自评时也无从勾选。
    JUDGE_KW = ("错误", "正确", "片面", "不完全", "不准确", "有失", "偏颇", "不成立")
    for i, q in enumerate(subs, 1):
        if q.get("qtype") != "analysis":
            continue
        pts = q.get("points") or []
        if not pts:
            continue
        first = str(pts[0].get("claim") or "")
        if not any(k in first for k in JUDGE_KW):
            issues.append({"type": "辨析题缺判断项", "target": f"主观第 {i} 题",
                           "detail": f"首个采分点不是「判断正误」：{first[:24]}"})

    # ⑧ 难度档位（只在指定了目标档时校验）
    #    ⚠️ 难度是**模型自评**的，程序只能校验"它自己说的数"贴不贴档。
    #    真正起作用的是提示词里那套可观察描述；这里只是兜底，并把分布暴露给人看。
    diff_vals, diff_bad = [], []
    if difficulty:
        lo, hi = max(1, difficulty - 1), min(5, difficulty + 1)
        for i, q in enumerate(singles, 1):
            d = q.get("difficulty")
            if not isinstance(d, (int, float)):
                diff_bad.append(f"第{i}题未标难度")
                continue
            diff_vals.append(float(d))
            if not (lo <= d <= hi):
                diff_bad.append(f"第{i}题={d:g}")
        if diff_bad:
            issues.append({"type": "难度越档", "target": f"{len(diff_bad)} 道",
                           "detail": f"目标档 {difficulty}（允许 {lo}–{hi}）："
                                     + "、".join(diff_bad[:6])})
        elif diff_vals:
            mean = sum(diff_vals) / len(diff_vals)
            if abs(mean - difficulty) > 0.5:
                issues.append({"type": "难度均值偏离",
                               "target": f"均值 {mean:.2f} / 目标 {difficulty}",
                               "detail": "整组难度没贴住目标档（偏了超过 0.5）"})

    # ⑨ 正确项明显更长/更全：学生不看内容、挑"最长最完整"的那个就能蒙对
    long_ones = []
    for i, q in enumerate(singles, 1):
        opts = q.get("options") or {}
        ans = (q.get("answer") or "").upper()
        if len(opts) != 4 or ans not in opts:
            continue
        n_ans = len(str(opts[ans]).strip())
        others = [len(str(v).strip()) for k, v in opts.items() if k != ans]
        if not others:
            continue
        avg = sum(others) / len(others)
        if avg > 0 and n_ans > avg * 1.5 and n_ans - avg > 12:
            long_ones.append(f"第{i}题（正确项 {n_ans} 字 vs 其余均 {avg:.0f} 字）")
    if long_ones:
        issues.append({"type": "正确项明显过长", "target": f"{len(long_ones)} 道",
                       "detail": "正确项比其它选项长得多，能靠形式蒙对："
                                 + "；".join(long_ones[:4])})

    # ⑩ 题干疑似缺情境 —— **只报不拦**
    #    机器判断"有没有情境"很不可靠（本项目踩过"用一个统计指标代替真正的检查"的坑，犯过 3 次）。
    #    所以这里用很保守的规则（题干过短、或完全没有情境标志词）只提示一句，
    #    **不进 hard_fail**；真正的判断交给 AI 质检和用户自己看。
    SCENE_KW = ("情境", "场景", "课堂", "课上", "班上", "班里", "学校", "某校", "教师", "老师",
                "学生", "家长", "校长", "教学", "课程", "调查", "实验", "研究", "材料", "案例",
                "政策", "文件", "报告", "数据", "学者", "一位", "一名", "某位", "现象")
    scene_suspect = [f"第{i}题" for i, q in enumerate(singles, 1)
                     if len((q.get("stem") or "").strip()) < 25
                     or not any(k in (q.get("stem") or "") for k in SCENE_KW)]
    if scene_suspect:
        issues.append({"type": "疑似缺情境", "target": f"{len(scene_suspect)} 道",
                       "detail": "题干偏短或看不出情境（粗筛，仅供参考、不触发重出）："
                                 + "、".join(scene_suspect[:6])})

    HARD_FAIL_TYPES = ("解析引用字母", "答案集中", "采分点过少", "题干过短", "选项残缺",
                       "答案缺失", "识记题偏多", "组内重复", "辨析题缺判断项",
                       "难度越档", "难度均值偏离", "正确项明显过长")
    diff_dist = {}
    for v in diff_vals:
        diff_dist[int(v)] = diff_dist.get(int(v), 0) + 1
    return {"issues": issues, "answer_dist": dict(dist), "remember_n": n_remember,
            "difficulty_target": difficulty,
            "difficulty_mean": round(sum(diff_vals) / len(diff_vals), 2) if diff_vals else None,
            "difficulty_dist": diff_dist,
            "scene_suspect_n": len(scene_suspect),
            "hard_fail": bool([i for i in issues if i["type"] in HARD_FAIL_TYPES])}


def generate_ai_set(study_text, points, n_single=20, topics=None, max_attempts=2,
                    difficulty=None, thinking=True):
    """一次生成「全 AI 模拟组」：{n_single} 单选 + 辨析/简答/论述各 1。

    difficulty: 1–5 难度档；thinking: 出题时是否开思考模式（更慢更贵，题目质量更高）。

    分两批调用（单选 / 主观）——合成一次调用在 20 题规模上容易截断，且失败看不出原因。
    流程：生成 → 程序层硬校验 + AI 质检 → 不合格就**带着问题重出一组**（最多 max_attempts 次）。

    返回 (paper, usage合计, 质检结果)；质检结果里含 attempts 与每一轮的 issues。
    """
    difficulty = normalize_difficulty(difficulty)
    lim = gen_limits(thinking)
    single_sys = AI_SET_SINGLE_SYS.replace("@@DIFFICULTY@@", difficulty_block(difficulty))
    subj_sys = AI_SET_SUBJ_SYS.replace("@@DIFFICULTY@@", difficulty_block(difficulty))

    plist = "\n".join(f"{p['id']}\t{p.get('path') or p.get('name')}" for p in points)
    topics = topics if topics is not None else hot_topics()
    topics_txt = "\n\n".join(f"【热点 {i+1}】{t['date']} {t['title']}\n{t['body']}"
                             for i, t in enumerate(topics)) or "（热点清单为空：请出一道经典的、不依赖时事的材料论述题）"
    usage_total = {}

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
            [{"role": "system", "content": single_sys}, {"role": "user", "content": user1}],
            max_tokens=lim["single"], temperature=0.7 + 0.1 * (attempt - 1),
            thinking=thinking, timeout=lim["timeout"])
        add_usage(usage_total, u1)

        user2 = AI_SET_SUBJ_USER.format(
            study=(study_text or "（未填写）") + feedback, points=plist, topics=topics_txt)
        subs, u2 = chat_json(
            [{"role": "system", "content": subj_sys}, {"role": "user", "content": user2}],
            max_tokens=lim["subj"], temperature=0.7,
            thinking=thinking, timeout=lim["timeout"])
        add_usage(usage_total, u2)

        paper = {"single": singles.get("single", [])[:n_single],   # 只要求 20 道，多给的一律裁掉
                 "subjective": subs.get("subjective", [])}
        for q in paper["single"]:
            shuffle_options(q)
        balance_answer_distribution(paper["single"])               # 打乱后再均衡答案分布

        audit = audit_ai_set(paper, difficulty=difficulty)

        check = {"pass": None, "issues": [], "must_retry": False, "comment": "（未跑 AI 质检）"}
        try:
            check_txt = json.dumps(paper, ensure_ascii=False)[:24000]
            check, u3 = chat_json(
                [{"role": "system", "content": AI_SET_CHECK_SYS},
                 {"role": "user", "content": "难度档位：" + difficulty_block(difficulty) +
                                             "\n\n热点清单：\n" + topics_txt[:4000] +
                                             "\n\n待审题目：\n" + check_txt}],
                max_tokens=lim["check"], temperature=0.2,
                thinking=thinking, timeout=lim["timeout"])
            add_usage(usage_total, u3)
        except Exception as e:
            # 兜底：开思考时实测出现过「推理把 max_tokens 吃光 → 正文为空 → JSON 解析失败」。
            # 质检只是**审阅已生成好的题**，不需要那么深的推理，退一步用不开思考重跑一次，
            # 保证这份质量报告一定拿得到（否则页面上只有一句看不懂的报错）。
            check = {"pass": None, "issues": [], "must_retry": False,
                     "comment": f"质检调用失败：{type(e).__name__}: {e}"}
            if thinking:
                try:
                    check_txt = json.dumps(paper, ensure_ascii=False)[:24000]
                    check, u3 = chat_json(
                        [{"role": "system", "content": AI_SET_CHECK_SYS},
                         {"role": "user", "content": "难度档位：" + difficulty_block(difficulty) +
                                                     "\n\n热点清单：\n" + topics_txt[:4000] +
                                                     "\n\n待审题目：\n" + check_txt}],
                        max_tokens=lim["plain_check"], temperature=0.2,
                        thinking=False, timeout=lim["plain_timeout"])
                    add_usage(usage_total, u3)
                    check["fallback_no_thinking"] = True
                except Exception as e2:
                    check = {"pass": None, "issues": [], "must_retry": False,
                             "comment": f"质检两次都失败：{type(e).__name__}: {e} / "
                                        f"关思考重试后 {type(e2).__name__}: {e2}"}

        all_issues = audit["issues"] + list(check.get("issues") or [])
        history.append({"attempt": attempt, "issues": all_issues,
                        "answer_dist": audit["answer_dist"],
                        "difficulty_mean": audit.get("difficulty_mean"),
                        "difficulty_dist": audit.get("difficulty_dist"),
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
        # 本次的出题设置（回显给页面，也便于事后追溯"这套题是什么档位、有没有开思考"）
        "difficulty": difficulty,
        "difficulty_name": DIFFICULTY_LEVELS[difficulty]["name"],
        "thinking": bool(thinking),
        "reasoning_tokens": usage_total.get("reasoning_tokens", 0),
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
2. **辨析题必须单独判「判断」这一维**（这是官方要求的独立得分点，别混在理由里）：
   - 学生答案里**有没有明确给出**「该说法错误 / 该说法正确」这样的判断？
   - 若有，他判的方向对不对（对照采分点第 1 条给的正确判断）？
   - **判断是二元的**：只允许"错误"或"正确"，不许出现"半对半错/有对有错"这类模糊判断；
     学生若写"有合理之处也有局限"而没落到一个结论上，算**未明确判断**。
   - 硬规则：**未作判断或判断方向错误 → 全题得分不超过 3 分**（理由写得再好也不加）。
   - 把这个结论填进下面的 `judgment` 字段，并据此给出 score。
3. 输出必须是纯 JSON，字段如下（不要 markdown 代码块）：
{
  "score": 11.5,
  "full_score": 15,
  "judgment": {"student": "错误", "correct": "错误", "matched": true,
               "explicit": true, "note": "学生开头明确写了该说法错误"},
  "hit_seqs": [1, 2, 4],
  "miss_seqs": [3, 5],
  "extra_credit": [{"text": "学生多写的合理内容", "suggest_score": 1.0}],
  "cause": "记混",
  "one_line": "一句话总评",
  "per_paragraph": [{"text": "原文段落", "mark": "(得3分)", "comment": "段落点评"}],
  "optimized": "基于学生框架的优化版作答（补充处用 **粗体**）",
  "standard_points": [{"seq": 1, "claim": "采分点", "score": 3}]
}
`judgment` 仅对辨析题填写；简答题/分析论述题把它设为 null。
3. cause 只能取：不会 / 记混 / 看漏条件 / 时间不够。
4. hit_seqs 中的序号必须来自题目给出的采分点序号。
"""

def grade_answer(question_stem, points, student_text=None, full_score=15,
                 use_thinking=True, image_data_url=None, qtype=None):
    """批改主观题。

    image_data_url: "data:image/jpeg;base64,..." —— 手写照片。
    有图片时走多模态：先识别手写内容，再按规则批改。
    qtype: 'analysis'（辨析题）时才启用「判断正误」这一维的硬规则；
           简答/论述不必判断正误，避免模型给它们硬凑一个 judgment。
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
        extra = '\n7. 额外字段：`recognized`（识别出的学生原文）、`recognize_note`（识别存疑说明）。'
    else:
        user_msg = {"role": "user", "content": head + f"\n【学生作答】\n{student_text}\n\n请按规则批改，输出 JSON。"}
        extra = ""

    # 「判断」这一维只对辨析题生效：简答/论述没有正误可判，硬套会让模型乱填
    if qtype == "analysis":
        extra += ("\n6. 本题是**辨析题**：必须填写 `judgment` 字段（见上文第 2 条），"
                  "并在理由给分之外单独判定这一维。")
    else:
        extra += ("\n6. 本题不是辨析题：`judgment` 必须为 null，不要凭空判断正误。")

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
