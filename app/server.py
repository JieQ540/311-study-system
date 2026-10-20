# -*- coding: utf-8 -*-
"""311 备考系统 · 最小可用服务

功能（MVP 第一期）：
  GET /           考试页面（计时 + 答题）
  GET /api/paper  生成试卷（2025 真题：单选 + 主观题 + 采分点）
  POST /api/save  保存作答记录（客观题对错、主观题自评命中）

启动：
  python -m uvicorn server:app --host 127.0.0.1 --port 8765
然后浏览器打开 http://127.0.0.1:8765
"""
import json
import hashlib
import os
import random
import sqlite3
import sys
import threading as _threading
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

ROOT = Path(__file__).resolve().parents[1]   # 公开副本：相对路径
# 让 app/ 一定可导入：这样 `cd app && uvicorn server:app` 和
# `uvicorn app.server:app`（从项目根）两种启动方式都能跑
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 库的位置统一由 dbpath 决定（server 与 ai 共用，避免"服务读 A 库、AI 写 B 库"）：
# 优先用户自己的 data/kaoyan.db；没有或不是可用的库时，从随包的 kaoyan-seed.db
# 复制一份出来用 —— **绝不把练习记录写进被 git 跟踪的随包文件**。
# DSH_DB 环境变量只给测试用：把库指向副本，跑完删掉，绝不动真实数据（见 docs/交接文档.md §5.1）
from dbpath import DB  # noqa: E402

# 分值形态与选做题归组的**唯一实现**。以前这里自己有一份 `_opt_of_section` /
# `_is_elective`，`split_points.py` 里又有一份，而「分值形态是否自洽」只在那边有 ——
# 于是"页面说分值齐全、数据管线说这题不合法"这种自相矛盾迟早出现（设计 §2.1 要求
# 两者同源）。现在两边都 import marks。
import marks  # noqa: E402
import pricing as pricing_mod  # noqa: E402
import providers as providers_mod  # noqa: E402
import review as review_mod  # noqa: E402
import retest as retest_mod  # noqa: E402
import pilot as pilot_mod  # noqa: E402
import curriculum as curriculum_mod  # noqa: E402
from uuid import uuid4
from contextlib import contextmanager
from marks import is_elective as _is_elective, opt_of_section as _opt_of_section  # noqa: E402

STATIC = ROOT / "app" / "static"
INDEX = STATIC / "index.html"

app = FastAPI(title="311 备考系统")


@app.middleware("http")
async def private_pilot_cache(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/api/pilot", "/api/curriculum")):
        response.headers["Cache-Control"] = "no-store"
    return response

EXAM_MINUTES = 180  # 311 考试时长


# ---------------------------------------------------------------- 题量档位
#
# **为什么要有这一层**：/daily 的流程改成「先定位考点 → 再选组卷方式」之后，
# 页面上要选的不只是"真题还是 AI"，还有"练多少"。以前题量写死在 config.json 的
# daily 段（20+3），页面上看不见也改不了；现在给三个档位，由**这里**统一解释，
# 三个接口（generate / ai-set / weak-paper）共用一份 —— 各写一份必然对不上。
#
# 主观题配比与 311 卷面同型：辨析 + 简答 + 论述。
#
# ⚠️ **全 AI 组（`/api/daily/ai-set`）只认 `n_single`**：它的主观题配比写死在
# `ai.AI_SET_SUBJ_USER`（辨析/简答/论述各 1），而它的质量自检
# （`audit_ai_set` 里「论述题必须带材料」那几条）也是照着这个配比写的。
# 为了改个题量档去动那条链路不划算 —— 所以 AI 组的主观题**固定 3 道**，
# 只有单选数跟着档位走。真题组（real）则完整用下面的配比。
GROUPS = {
    "light":    {"name": "轻练", "single": 10, "subjective": 2,
                 "mix": {"analysis": 1, "short": 1, "essay": 0},
                 "minutes": "约 15 分钟"},
    "standard": {"name": "标准", "single": 20, "subjective": 3,
                 "mix": {"analysis": 1, "short": 1, "essay": 1},
                 "minutes": "约 30 分钟"},
    "strong":   {"name": "加强", "single": 30, "subjective": 4,
                 "mix": {"analysis": 1, "short": 2, "essay": 1},
                 "minutes": "约 45 分钟"},
}
DEFAULT_GROUP = "standard"
#: AI 组主观题固定 3 道，配比**只有一处定义**（`ai.AI_SET_MIX`）：
#: 生成方按它裁剪、页面标签按它显示、题量透明按它核算，三处必须是同一个数字。
#: （本轮之前这个常量只在 server 里，`ai.generate_ai_set` 自己另写死了一份"各 1"。
#:  `import ai` 在本文件里是延后导入的，所以这里也用函数取，别在模块顶层直接引用。）
def _ai_set_mix():
    return ai_mod.AI_SET_MIX
#: 模拟考的卷面结构（**原样保留**，设计 §4 明确要求这一档不动）
MOCK_EXAM = {"single": 45, "analysis": 3, "short": 5, "essay": 3, "minutes": 180}
#: 各题型一分钟能推进多少（用于把题量折成"预计用时"）。
#: 依据：311 模拟考 45 单选 + 3 辨析 + 5 简答 + 3 论述 = 180 分钟，
#: 按这组数折算 54 + 24 + 50 + 54 = 182 分钟（差 2 分钟）—— 够精确到"能用来安排时间"，
#: 又不是拿秒表量出来的假精确（所以页面写"预计"，并标明是按模拟考节奏折算）。
EST_MINUTES = {"single": 1.2, "analysis": 8, "short": 10, "essay": 18}
QT_LABEL = {"single": "单选", "analysis": "辨析", "short": "简答", "essay": "分析论述"}


def paper_composition(real, generated, want_single, want_by_type, trimmed=None):
    """组卷结果的「题量透明」：**实际**各题型数量、真题/AI 构成、预计用时、与目标的差异原因。

    为什么必须回传并展示（设计 §4）：页面上的档位标签（"轻练 10＋2"）是**目标**，
    实际那份卷子可能因为真题池不够而补进 AI 题、或因模型多给而被裁。以前只回
    `real_count`/`generated_count` 两个总数，页面就把目标当成了实际 ——
    多一道论述题，40 分钟的预期会变成 60 分钟，而用户是在做完之后才发现的。
    """
    rs = list((real or {}).get("single") or [])
    gs = list((generated or {}).get("single") or [])
    rsub = list((real or {}).get("subjective") or [])
    gsub = list((generated or {}).get("subjective") or [])
    by_type = {}
    for q in rsub + gsub:
        k = q.get("qtype") or "unknown"
        by_type[k] = by_type.get(k, 0) + 1
    n_single, n_subj = len(rs) + len(gs), len(rsub) + len(gsub)
    minutes = round(n_single * EST_MINUTES["single"]
                    + sum(EST_MINUTES.get(k, 10) * v for k, v in by_type.items()), 1)
    want_by_type = dict(want_by_type or {})
    reasons = []
    for t in ("analysis", "short", "essay"):
        want, got = int(want_by_type.get(t) or 0), by_type.get(t, 0)
        if want and got > want:
            reasons.append(f"{QT_LABEL[t]}比目标多 {got - want} 道：真题池里这类题不够，"
                           f"AI 补缺时多给了一道。")
        elif want and got < want:
            reasons.append(f"{QT_LABEL[t]}比目标少 {want - got} 道：真题与 AI 都没凑够"
                           f"（真题池不足或生成结果被裁）。")
    if want_single and n_single > want_single:
        reasons.append(f"单选比目标多 {n_single - want_single} 道：真题补缺时多抽了题。")
    if trimmed and (trimmed.get("single") or trimmed.get("subjective")):
        reasons.append(f"模型多给了 {trimmed.get('single', 0)} 道单选、"
                       f"{trimmed.get('subjective', 0)} 道主观题，已按目标题量截取（没有多塞给你）。")
    return {
        "single": {"n": n_single, "real": len(rs), "ai": len(gs), "want": want_single},
        "subjective": {"n": n_subj, "real": len(rsub), "ai": len(gsub),
                       "want": sum(int(v or 0) for v in want_by_type.values()),
                       "by_type": by_type, "want_by_type": want_by_type},
        "total_n": n_single + n_subj,
        "estimated_minutes": minutes,
        "estimate_basis": "按模拟考节奏折算：单选 1.2 分钟、辨析 8、简答 10、分析论述 18",
        "extra_reasons": reasons,
        "trimmed": trimmed or {},
    }


def resolve_group(payload):
    """从请求里解出题量配比。

    优先用 `group`（档位名）；**同时保留旧的 `n_single`/`n_analysis`/… 显式传参**，
    这样老调用方（测试、脚本、`weak-paper` 的内部转发）不用改也不会走错。
    两者都没给就用默认档位。
    """
    g = GROUPS.get(str(payload.get("group") or "").strip().lower())
    if g:
        return {"group": str(payload["group"]).strip().lower(), "name": g["name"],
                "n_single": g["single"], "n_analysis": g["mix"]["analysis"],
                "n_short": g["mix"]["short"], "n_essay": g["mix"]["essay"],
                "n_subjective": g["subjective"], "minutes": g["minutes"]}
    n_single = int(payload.get("n_single", GROUPS[DEFAULT_GROUP]["single"]))
    n_analysis = int(payload.get("n_analysis", 1))
    n_short = int(payload.get("n_short", 1))
    n_essay = int(payload.get("n_essay", 1))
    return {"group": None, "name": "自定义", "n_single": n_single,
            "n_analysis": n_analysis, "n_short": n_short, "n_essay": n_essay,
            "n_subjective": n_analysis + n_short + n_essay, "minutes": None}


@app.on_event("startup")
def _startup():
    """启动入口：只负责调用（逻辑放进 run_migrations，这样测试能直接测启动路径）。"""
    fixed, moved = run_migrations()
    if fixed or moved:
        print(f"[schema] 补齐采分点挂载 {fixed} 条；统一层级 {moved} 条")


def run_migrations():
    """跑全部建表/迁移，返回 (补齐挂载数, 统一层级数)。

    单独抽出来是为了能被测试直接调用：启动路径曾因为「调用方传进来的连接没设
    row_factory」而在真实启动时崩掉，而测试因为自己设了 row_factory 没发现。
    """
    ensure_schema()
    return backfill_point_outline(), unify_outline_levels()


def rows(sql, args=(), con=None):
    if con is not None:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


# ---------------------------------------------------------------- 答案解析的统一形状
#
# **为什么集中在一个函数里**：以前 /api/paper、/api/logs/day、/api/daily/grade 各自
# 拼一遍采分点字段，加一个字段就要改三处、漏一处就**静默**少显示（README 里
# 「前后端字段名不一致」那一行就是这么来的：后端给 answered、前端读 unanswered，
# 页面不报错，只是标签全部失效）。现在只在这里拼。

#: 采分点 SQL 片段。`section` / `score` 允许为空（老数据、AI 全量补分之前的题）——
#: 前端据此显示「分值待核」，**不假装有分值**。
POINT_COLS = "seq,claim,evidence,section,score"


def points_of_question(qid, con=None):
    """取一道题的采分点（含小问分组与逐点分值）。"""
    sql = f"SELECT {POINT_COLS} FROM points WHERE question_id=? ORDER BY seq"
    if con is not None:
        return [dict(r) for r in con.execute(sql, (qid,))]
    return rows(sql, (qid,))


def sections_of(extra):
    """从 questions.extra 里取小问结构（AI 补分值那一步写的）。"""
    if not isinstance(extra, dict):
        return []
    secs = extra.get("sections") or []
    return secs if isinstance(secs, list) else []


def score_stats(points, full_score=None, sections=None, stem=None, note=None):
    """按「分值形态」给出可折算的分数信息（纯展示结构，判据来自 marks）。

    为什么不让前端自己算：四个页面各算一遍，迟早算得不一样（`scored` 判定、
    选做题的分组求和、分值待核的显示条件都很容易走偏）。

    2026-10-08 起多两个字段：
      · `fold_ok` / `fold_reason` —— 交给 `marks.fold_ready()` 判「现在能不能折合成分数」。
        以前这里只看「每个点都有分值」（`scored`），**不看形态是否自洽、也不看分值是否为正**，
        于是 `sum_score ≠ 满分` 的题照样折出一个数，与数据管线 `--report` 的结论打架。
        现在页面的「分值待核」徽标直接读 `fold_ok`，与 `split_points.py` 同源。
      · `free_hit` —— 「答出任意 N 点即可满分」的 N。以前前端有这段提示逻辑
        （`qa-answer.js` 的 `model.freeHit`）但**服务端从不返回这个字段**，是死代码；
        于是 2009 第 53 题（4×5 合计 20、满分 15）被标成刺眼的「分值不符」，
        而它其实是官方的正常形态。现在如实说明。
    """
    pts = list(points or [])
    scored = [p for p in pts if isinstance(p.get("score"), (int, float))]
    total = round(sum(p["score"] for p in scored), 2)

    # 每个小问各归到哪个选项（选做题用）。key 里没有罗马数字时退回 label。
    sec_opt = {str(s.get("key")): _opt_of_section(s.get("key"), s.get("label"))
               for s in (sections or [])}
    elective = _is_elective(stem) or (
        bool(sections) and all(sec_opt.values()))
    groups = {}
    if elective:
        for p in scored:
            k = sec_opt.get(str(p.get("section"))) or _opt_of_section(p.get("section"))
            if k:
                groups.setdefault(k, {"key": k, "got": 0.0, "full": None})
                groups[k]["got"] = round(groups[k]["got"] + p["score"], 2)
        for s in sections or []:
            k = _opt_of_section(s.get("key"), s.get("label"))
            if k and k in groups:
                try:
                    groups[k]["full"] = round(groups[k]["full"] or 0, 2) + float(s.get("score") or 0)
                except (TypeError, ValueError):
                    pass
    fold_ok, fold_reason = marks.fold_ready(pts, full_score, stem, sections)
    return {
        "n": len(pts),
        "n_scored": len(scored),
        "scored": len(scored) == len(pts) and len(pts) > 0,
        "sum_score": total,           # 逐点分值之和（选做题会是 60，因为两个选项各 30）
        "full_score": full_score,
        "match": (full_score is None or elective or abs(total - float(full_score)) < 0.01),
        "elective": elective,
        "options": [groups[k] for k in sorted(groups)],   # 选做题每个选项的得分/满分
        "has_sections": any((p.get("section") or "*") != "*" for p in pts),
        "fold_ok": fold_ok,
        "fold_reason": fold_reason,
        "free_hit": marks.free_hit_n(pts, full_score, note),
        # 这个 N 是**官方说明**里写的，还是靠分值形态推出来的 —— 页面据此决定措辞
        # （不得把本系统的练习口径说成"官方评分"）
        "free_hit_source": marks.free_hit_source(note),
    }



def attach_subjective_meta(rows):
    """给一批主观题行补上计分/展示需要的字段（就地改并返回）。

    补的是 `sections`（小问结构）、`answer_note`（官方评分说明）、`full_score`（满分）
    与 `score_stats`（分值形态判定）。**只有服务端能给 `score_stats`** ——
    页面顶部的「分值齐全 / 待核 / 任意 N 点即可」徽标必须与服务端同一判据
    （`marks.fold_ready`），否则又会出现"页面说齐全、报告说待核"。

    谁用：`/api/daily/generate` 的真题分支。以前那条路连 `points` 都没有，
    真题主观题在页面上一个勾选框都渲染不出来（见 `ai.real_questions` 的说明）。
    """
    for s in (rows or []):
        ex = _json_or(s.get("extra"), {}) or {}
        secs = sections_of(ex)
        s["sections"] = secs
        s["answer_note"] = ex.get("answer_note") or None
        s["full_score"] = ex.get("full_score")
        s["score_stats"] = score_stats(s.get("points") or [], s["full_score"],
                                       secs, s.get("stem"), s["answer_note"])
    return rows


# ============================================================ 按作答计分（唯一口径）
#
# **为什么要把计分收成一个函数**：同一个"这题得了几分"曾经有四处各算一份 ——
#   · `_build_metrics()` 读采分点的 `hit`，但取点的 `points_of_question()` 从不返回
#     这个字段 → 折合分**恒为 0**（实测：2026-10-07 那两道应当是 14/15 + 15/15 = 29/30，
#     报告里存的是 0/30，并写进报告卡与趋势）；
#   · `_session_subjective_scores()` 按 `session_id + question_id` 合并，
#     同一次练习里同题作答两次会互相覆盖/重复计数；
#   · 前端 `qa-answer.js` 的 `sumHit()` 再自己加一遍。
# 三处口径不同，就会出现「日志明细 29/30、报告 0/30」这种自相矛盾。
# 现在唯一入口是 `attempt_score()`，按 **attempt.id** 粒度算，其余都来调它。

#: `attempts.point_source` 的合法取值（设计 §2.2）。没填 / NULL 一律视为"来源不明"。
POINT_SOURCES = ("self_confirmed", "ai_confirmed", "ai_unconfirmed", "unassessed")
#: 算进「已确认折合分」的来源。
CONFIRMED_SOURCES = ("self_confirmed", "ai_confirmed")
SOURCE_LABEL = {
    "self_confirmed": "用户自评确认",
    "ai_confirmed": "用户采纳 AI 建议",
    "ai_unconfirmed": "仅收到 AI 建议",
    "unassessed": "尚未评定",
    "legacy_unknown": "来源不明（旧记录）",
}

#: 选做题的「作答的是哪一支」。前端可能传罗马数字的几种写法，一律归成 Ⅰ / Ⅱ。
_OPTION_ALIASES = {"Ⅰ": "Ⅰ", "Ⅱ": "Ⅱ", "I": "Ⅰ", "II": "Ⅱ",
                   "i": "Ⅰ", "ii": "Ⅱ", "1": "Ⅰ", "2": "Ⅱ"}


def _normalize_option(value):
    """把前端传来的"作答的是哪一支"归成 `Ⅰ` / `Ⅱ`；认不出来返回 None（不猜）。"""
    if value is None:
        return None
    return _OPTION_ALIASES.get(str(value).strip())


def _attempt_points(attempt_ids, con=None):
    """`{attempt_id: [该题全部采分点 + 这一次是否命中]}`。

    **必须按 attempt_id 取**（不能用 session+question）：同一次练习里同一道题可能有
    两次作答（重复练、续练补做），按题合并会让两次的命中互相覆盖。

    `ph.hit = 1` 写在 `LEFT JOIN` 的 ON 里 —— 这是踩过的坑：直接 JOIN 不带这个过滤，
    会把 `point_hits` 里 `hit=0` 的历史中间状态当成命中，命中率就是这么算错的。
    """
    ids = [int(i) for i in (attempt_ids or []) if i is not None]
    out = {}
    for chunk in _chunks(ids, 400):
        qs = ",".join("?" * len(chunk))
        for r in rows(
                "SELECT a.id AS attempt_id, p.id AS point_id, p.seq, p.claim, p.evidence, "
                "       p.section, p.score, p.outline_id, "
                "       CASE WHEN ph.point_id IS NULL THEN 0 ELSE 1 END AS hit "
                "FROM attempts a "
                "JOIN points p ON p.question_id = a.question_id "
                "LEFT JOIN point_hits ph "
                "       ON ph.attempt_id = a.id AND ph.point_id = p.id AND ph.hit = 1 "
                f"WHERE a.id IN ({qs}) ORDER BY a.id, p.seq", chunk, con=con):
            out.setdefault(r["attempt_id"], []).append(r)
    return out


def _ai_reference(att, points=None, full=None, sections=None, qtype=None, judge_cap=None):
    """这道题的 AI 分数算不算「有效 AI 参考分」。

    设计 §2.1/§3 的口径：只有**新版逐点分账、且服务端对账通过**的 AI 分数才进
    `ai_reference_score/full`。旧结果（没有逐点分账，无法验证它自己加得对不对）
    一律 `legacy_unverifiable`：照旧在单题明细里显示 AI 说了多少分，
    但**不进可信趋势**，也不冒充学生的得分。

    **对账在计分时重算一遍**（不信任前端回传的 `verification` 字段）：
    `grade` 是随作答一起存进库的，客户端理论上能改；重算才站得住。
    """
    g = _json_or(att.get("grade"), None) if att else None
    if not isinstance(g, dict):
        return {"status": "none", "score": None, "full": None, "raw_score": None}
    raw = g.get("score")
    raw_full = g.get("full_score")
    # ---- 人工确认出口（2026-10-09，设计 §5/D）----
    # 用户核对过、并在作答页明确确认这份分账 → 记 `human_ok`：计入可信 AI 参考分，
    # 但**状态与 v2_ok 分开**，汇总里另给 `ai_reference_manual_n` ——
    # 「机器对账通过」与「人工担保」不是一回事，不能悄悄混成一条线。
    ov = g.get("override")
    if (isinstance(ov, dict) and ov.get("status") == "human_ok"
            and g.get("schema_version") == 2 and _num(raw) is not None):
        return {"status": "human_ok", "score": _num(raw),
                "full": _num(raw_full) or _num(full),
                "raw_score": raw, "raw_full": raw_full, "checked": True,
                "manual": True,
                "errors_overridden": list(ov.get("errors_overridden") or []),
                "override_at": ov.get("at")}
    ver = g.get("schema_version")
    if not (isinstance(ver, int) and not isinstance(ver, bool) and ver >= 2):
        return {"status": "legacy_unverifiable", "score": None, "full": None,
                "raw_score": raw, "raw_full": raw_full}
    if points is None:
        # 没给采分点就没法对账 → 不当可信分数（宁可空着）
        return {"status": "needs_review", "score": None, "full": None,
                "raw_score": raw, "raw_full": raw_full,
                "errors": ["计分时拿不到采分点，无法对账"]}
    chk = verify_grade_v2(g, points, full, sections, qtype, judge_cap)
    if chk["status"] == "v2_ok":
        return {"status": "v2_ok", "score": _num(raw), "full": _num(raw_full) or _num(full),
                "raw_score": raw, "raw_full": raw_full, "checked": True}
    return {"status": "needs_review", "score": None, "full": None,
            "raw_score": raw, "raw_full": raw_full, "errors": chk["errors"]}


def _point_score(att, points=None):
    """采分点确认结果；只供统一计分入口保留自评与命中统计。

    判定顺序（每一步都给得出"为什么算不出来"，页面照它显示，不猜）：
      1. 题目查不到 → `题目已不在题库`；
      2. 来源是 `ai_unconfirmed` / `unassessed` → `尚未确认采分点结果`，
         分数是 `null` **不是 0**：没评定与"一个点都没答到"是两件事；
      3. 选做题：没记下作答的是 Ⅰ 还是 Ⅱ → `选做分支待核`（两支各自 30，
         不推断、也不相加）；记下了就只按那一支算；
      4. `marks.fold_ready()` 不过 → 原样给出原因（几个点没分值 / 分值非正 /
         形态与满分不符）。**绝不回退成"题型一律 15 分"**——那会把 30 分论述题算成 15 分。

    命中点的分值之和就是得分；「答出任意 N 点即可」的题（逐点合计 > 满分）按满分封顶。
    `hit=0` 的行与"没有命中行"都是未命中 —— 全部未命中且来源已确认时，**0 分是有效分数**。
    """
    out = {
        # 日志页的行用 `attempt_id`（SQL 里 a.id AS attempt_id），计分查询里叫 `id` —— 两个都认。
        "attempt_id": att.get("id") or att.get("attempt_id"),
        "question_id": att.get("question_id"),
        "qtype": att.get("qtype"),
        "node_name": att.get("node_name") or att.get("outline_name"),
        "source": att.get("point_source") or "legacy_unknown",
        "selected_option": att.get("selected_option"),
        "n_points": 0, "n_hits": 0,
        "score": None, "full": None, "scored": False, "reason": None,
    }
    out["source_label"] = SOURCE_LABEL.get(out["source"], out["source"])
    out["reason_kind"] = None
    pts = list(points if points is not None else [])
    all_pts = list(pts)          # 全部采分点（选做题分支收窄后仍要留一份给 AI 对账）
    out["n_points"] = len(pts)
    out["n_hits"] = sum(1 for p in pts if p.get("hit"))
    # 命中的具体序号：正文过期判断要能发现"命中点换了但总分没变"（复审问题 3）
    out["hit_seqs"] = sorted(int(p["seq"]) for p in pts
                             if p.get("hit") and p.get("seq") is not None)
    out["ai"] = _ai_reference(att)

    if att.get("qtype") is None:
        out["reason"] = "题目已不在题库"
        out["reason_kind"] = "missing_question"
        return out
    if out["source"] in ("ai_unconfirmed", "unassessed"):
        out["reason"] = "尚未确认采分点结果"
        # 与"已自评但分值待核"**必须分开**（复审问题 3）：一个是人没做判断，
        # 一个是做了判断但题目分值不齐；页面与 AI 正文不能都说成"尚未评定"。
        out["reason_kind"] = ("ai_unconfirmed" if out["source"] == "ai_unconfirmed"
                              else "unassessed")
        return out

    extra = _json_or(att.get("extra"), {}) or {}
    full = extra.get("full_score")
    sections = sections_of(extra)
    stem = att.get("stem")

    if _is_elective(stem):
        opt = att.get("selected_option")
        if opt not in ("Ⅰ", "Ⅱ"):
            out["reason"] = "选做分支待核（这道题要任选 Ⅰ/Ⅱ 之一作答）"
            out["reason_kind"] = "pending_elective"
            return out
        # 题目**声明的**满分（选做题是"每题 30 分"）。下面的关键一步：分支的小问分值
        # 只能拿来**核对**它，不能拿来当满分 —— 否则"题目说 30、Ⅰ 分支的小问和采分点
        # 都只有 20"会被自己验证成 20/20（复审反例），一个假分数看起来完全正常。
        if not isinstance(full, (int, float)) or isinstance(full, bool) or full <= 0:
            out["reason"] = "题目满分缺失或不是正数"
            out["reason_kind"] = "pending_marks"
            return out
        branch_secs = [s for s in sections
                       if _opt_of_section(s.get("key"), s.get("label")) == opt]
        keys = [str(s.get("key")) for s in branch_secs]
        branch = [p for p in pts if str(p.get("section")) in keys]
        if not branch:
            out["reason"] = f"没有找到 {opt} 分支的采分点"
            out["reason_kind"] = "pending_marks"
            return out
        branch_sum = round(sum(float(s.get("score") or 0) for s in branch_secs), 2)
        if abs(branch_sum - float(full)) > 0.01:
            out["reason"] = (f"所选分支（{opt}）的小问分值之和 {branch_sum:g} "
                             f"≠ 题目满分 {float(full):g}：分值待核")
            out["reason_kind"] = "pending_marks"
            return out
        # 把选中的那一支**当成一道普通题**校验：不传 stem，避免又走回"按选项分组"，
        # 那会让"这一支是否等于满分"永远判为通过（等于没校验）。
        # 传进去的满分是**题目声明的那个**，不是分支自己算出来的。
        ok, why = marks.fold_ready(branch, float(full), None, branch_secs)
        pts, full = branch, full
        out["n_points"], out["n_hits"] = len(branch), sum(1 for p in branch if p.get("hit"))
        out["hit_seqs"] = sorted(int(p["seq"]) for p in branch
                                 if p.get("hit") and p.get("seq") is not None)
    else:
        ok, why = marks.fold_ready(pts, full, stem, sections)

    if not ok:
        out["reason"] = why
        out["reason_kind"] = "pending_marks"      # 分值待核（含满分缺失、形态不符）
        return out
    got = round(sum(float(p["score"]) for p in pts if p.get("hit")), 2)
    out["score"] = round(min(got, float(full)), 2)   # 「任意 N 点」形态不会超过满分
    out["full"] = round(float(full), 2)
    out["scored"] = True
    # 折合分算完之后再核 AI 参考分：这一步要拿**全部采分点**（不是分支子集）与题目满分，
    # 这样选做题也能核"逐点实得是否落在作答的那一支上"。
    jcap = analysis_judge_cap(all_pts)[0] if att.get("qtype") == "analysis" else None
    out["ai"] = _ai_reference(att, points=all_pts, full=extra.get("full_score"),
                              sections=sections, qtype=att.get("qtype"), judge_cap=jcap)
    return out


def attempt_score(att, points=None):
    """统一成绩：可信 AI 详细批改实得分优先，自评/采纳命中另账保留。

    读取时重算，不重写旧作答。没有可用的 AI 分账时保留原采分点折合口径；
    AI 分数仍执行原有分值、选做分支及服务端对账校验，不用命中点满分替代部分得分。
    """
    point = _point_score(att, points)
    out = dict(point)
    out["point_score"] = {k: v for k, v in point.items() if k != "ai"}
    out["score_source"] = point["source"]
    out["score_source_label"] = point["source_label"]
    # 是否勾选/采纳只影响采分点账，不应阻止已对账 AI 成绩的核算。
    basis = (_point_score({**att, "point_source": "self_confirmed"}, points)
             if point["source"] in ("ai_unconfirmed", "unassessed") else point)
    out["ai"] = basis["ai"]
    ai = out["ai"]
    if basis["scored"] and ai.get("status") in ("v2_ok", "human_ok"):
        out.update(score=ai["score"], full=basis["full"], scored=True,
                   reason=None, reason_kind=None, score_source="ai_graded",
                   score_source_label="AI 详细批改" + ("（人工核对）" if ai.get("manual") else "（已对账）"))
    return out


def _score_source_kind(details):
    sources = {d.get("score_source", d["source"]) for d in details if d.get("scored")}
    if "ai_graded" in sources:
        return "ai_graded" if len(sources) == 1 else "mixed_ai"
    if sources & set(CONFIRMED_SOURCES):
        return "mixed" if "legacy_unknown" in sources else "confirmed"
    return "legacy_unknown" if "legacy_unknown" in sources else "unscored"


def score_attempts(atts, points_by_attempt=None):
    """一批主观题作答 → `(逐条明细, 汇总)`。报告、趋势、日志汇总都用它。

    练习成绩逐题优先取 AI 实得分，不重复累加自评与 AI。旁账仍分开记：
      · `confirmed_point_*` 用户确认过的采分点折合分；
      · `legacy_point_*`    旧记录（来源不明，但以前就算在掌握率里，不能凭改造把它抹掉）；
      · `ai_reference_*`    新版分账且对账通过或人工核对的 AI 实得分。
    `subjective_score/full` 是实际采用的成绩，`subjective_source` 标明来源。
    """
    det, groups = [], {
        "point_hits": 0, "point_total": 0,
        "confirmed_point_score": 0.0, "confirmed_point_full": 0.0, "confirmed_n": 0,
        "legacy_point_score": 0.0, "legacy_point_full": 0.0, "legacy_n": 0,
        "ai_reference_score": 0.0, "ai_reference_full": 0.0, "ai_reference_n": 0,
        "ai_reference_manual_n": 0,
        "primary_score": 0.0, "primary_full": 0.0, "primary_n": 0,
        "unscored": [], "unscored_kinds": {},
    }
    for a in atts or []:
        pts = (points_by_attempt or {}).get(a["id"]) if points_by_attempt is not None else None
        d = attempt_score(a, pts)
        det.append(d)
        point = d["point_score"]
        if point["scored"]:
            groups["point_hits"] += point["n_hits"]
            groups["point_total"] += point["n_points"]
            if point["source"] in CONFIRMED_SOURCES:
                groups["confirmed_point_score"] += point["score"]
                groups["confirmed_point_full"] += point["full"]
                groups["confirmed_n"] += 1
            elif point["source"] == "legacy_unknown":
                groups["legacy_point_score"] += point["score"]
                groups["legacy_point_full"] += point["full"]
                groups["legacy_n"] += 1
        if not d["scored"]:
            groups["unscored"].append({
                "attempt_id": d["attempt_id"], "question_id": d["question_id"],
                "source": d["source"], "reason": d["reason"],
                "reason_kind": d.get("reason_kind") or "other"})
            groups["unscored_kinds"][d.get("reason_kind") or "other"] = \
                groups["unscored_kinds"].get(d.get("reason_kind") or "other", 0) + 1
            continue
        groups["primary_score"] += d["score"]
        groups["primary_full"] += d["full"]
        groups["primary_n"] += 1
        ai = d.get("ai") or {}
        # `human_ok`（人工确认过的）与 `v2_ok`（机器对账通过的）**都计入**这笔记分，
        # 但人工的那部分另记 `ai_reference_manual_n`，报告里要说得出成色。
        if ai.get("status") in ("v2_ok", "human_ok") and isinstance(ai.get("score"), (int, float)):
            groups["ai_reference_score"] += ai["score"]
            groups["ai_reference_full"] += ai.get("full") or 0
            groups["ai_reference_n"] += 1
            if ai.get("status") == "human_ok":
                groups["ai_reference_manual_n"] += 1

    for k in ("confirmed_point_score", "confirmed_point_full", "legacy_point_score",
              "legacy_point_full", "ai_reference_score", "ai_reference_full"):
        groups[k] = round(groups[k], 2)
    scored_n = groups["primary_n"]
    point_n = groups["confirmed_n"] + groups["legacy_n"]
    source_kind = _score_source_kind(det)

    # **一笔账都没有时给 null，不给 0**（设计 §2.1 / 复审问题 5）。
    # `0` 的含义是"评定了、但一个点都没答到"；"还没评定"必须是 `null`，
    # 否则报告卡上会出现一个看起来很确定的 0/0，学生以为系统给他打了 0 分。
    # 每一笔也各按"有没有这类题"决定给不给值。
    def _money(total, n):
        return round(total, 2) if n else None

    summary = {
        "subjective_count": len(det),
        "subjective_scored_n": scored_n,
        "subjective_source": source_kind,
        "subjective_score": _money(groups["primary_score"], scored_n),
        "subjective_full": _money(groups["primary_full"], scored_n),
        # 命中数：一条都没判定时也是 null（页面显示"尚未评定"而不是 0/0）
        "point_hits": groups["point_hits"] if point_n else None,
        "point_total": groups["point_total"] if point_n else None,
        "point_rate": (round(groups["point_hits"] / groups["point_total"], 3)
                       if groups["point_total"] else None),
        "unscored_n": len(groups["unscored"]),
        "unscored_reasons": groups["unscored"][:10],
        # 未计分的**原因分类**（复审问题 3）：页面与 AI 正文靠它区分
        # "尚未评定"（人没判断）与"已自评但分值待核"（判断了、题目分值不齐）。
        "unscored_kinds": dict(groups["unscored_kinds"]),
        "subjective_detail": det,
    }
    for k in ("confirmed_point_score", "confirmed_point_full", "confirmed_n",
              "legacy_point_score", "legacy_point_full", "legacy_n",
              "ai_reference_score", "ai_reference_full", "ai_reference_n",
              "ai_reference_manual_n"):
        summary[k] = groups[k]
    # 每一笔"没有这类题"时给 null（同上，不给 0）
    for score_k, full_k, n_k in (("confirmed_point_score", "confirmed_point_full", "confirmed_n"),
                                 ("legacy_point_score", "legacy_point_full", "legacy_n"),
                                 ("ai_reference_score", "ai_reference_full", "ai_reference_n")):
        if not summary[n_k]:
            summary[score_k] = None
            summary[full_k] = None
    return det, summary


def subjective_attempts(session_id=None, con=None):
    """主观题作答 + 计分需要的元数据（题型 / 题干 / 小问结构 / 满分 / 来源 / 所选分支）。

    题目满分只存在 `questions.extra.full_score` 里，**必须一起取回来** ——
    `_session_attempts()` 以前漏了 `q.extra`，于是 `_build_metrics()` 拿到的 extra 恒为 `{}`，
    满分回退成 15（30 分论述题按 15 算），这是 P0 的另一半。
    题目可能已被删：LEFT JOIN，让"查不到"可见，而不是静默少一条。
    """
    where, args = "1=1", []
    if session_id is not None:
        where, args = "a.session_id = ?", [session_id]
    return rows(
        "SELECT a.id, a.session_id, a.question_id, a.date, a.mode, a.hits, a.total, "
        "       a.cause, a.point_source, a.selected_option, a.grade, "
        "       q.qtype, q.stem, q.extra, o.name AS node_name "
        "FROM attempts a "
        "LEFT JOIN questions q ON q.id = a.question_id "
        "LEFT JOIN outline_nodes o ON o.id = q.outline_id "
        f"WHERE {where} AND (q.qtype IS NULL OR q.qtype <> 'single') "
        "ORDER BY a.id", args, con=con)


def session_attempt_basis(session_id):
    """给**正文依据**用的逐条作答事实（客观 + 主观都算）。

    复审问题 2：AI 提示词里逐条列了「题型 → 考点 → 结果（错因）」，其中客观题也在内，
    但 `content_basis` 只记了主观题计分明细与客观汇总 —— 于是**只改一条客观题的错因**，
    正文依据其实变了，指纹却完全相等，报告仍标"不过期"（复审在副本里复现过）。

    所以这里按 AI 真正读到的事实取：题型、命中、错因、考点名、题目是否还在。
    生成正文与判断是否过期都调它（同一次查询、同一份事实，指纹才可比）。
    """
    return [[r["id"], r["question_id"], r["qtype"], r["hits"], r["cause"] or "",
             r["node_name"] or "", 1 if r["qtype"] is None else 0]
            for r in rows(
                "SELECT a.id, a.question_id, a.hits, a.cause, q.qtype, o.name AS node_name "
                "FROM attempts a "
                "LEFT JOIN questions q ON q.id = a.question_id "
                "LEFT JOIN outline_nodes o ON o.id = q.outline_id "
                "WHERE a.session_id = ? ORDER BY a.id", (session_id,))]


def report_history(sid, sessions_by_id, scores):
    """喂给 AI 的「历史练习」——**按现算值**，不用 `sessions` 表里的旧快照。

    复审问题 4：以前这里直接读 `sessions.point_hits/point_total`，那是发卷当刻的快照
    （session 3 存着 3/14，明细现算是 3/21）。AI 拿到一个和明细对不上的数字，
    复盘结论就建在错事实上。生成正文与"正文是否过期"的判断都走这一个函数，
    两边的历史必须是同一份。

    返回最近 10 次（按 id 升序，与旧行为一致）。字段要**够正文依据比对**：
    复审问题 2 指出只存率与分数不够——同率不同分子分母（3/14 与 3/21）必须能区分出来。
    """
    out = []
    for s in sorted((x for x in sessions_by_id.values() if x["id"] != sid),
                    key=lambda x: x["id"], reverse=True)[:10]:
        sc = scores.get(s["id"]) or {}
        ot = s.get("objective_total") or 0
        orr = s.get("objective_right") or 0
        out.append({
            "id": s["id"], "date": s.get("date"), "mode": s.get("mode"),
            "objective_total": ot, "objective_right": orr,
            "objective_rate": round(orr / ot, 3) if ot else None,
            "objective_score": s.get("objective_score") or orr * 2,
            "point_hits": sc.get("point_hits"), "point_total": sc.get("point_total"),
            "point_rate": sc.get("point_rate"),
            "subjective_score": sc.get("subjective_score"),
            "subjective_full": sc.get("subjective_full"),
            "subjective_source": sc.get("subjective_source", "unscored"),
        })
    return list(reversed(out))


def content_basis(sess, sc, history, weak_points, attempts_basis=None):
    """**正文依据**：正文里可能引用到的每一项事实（可复核、可逐项对比）。

    复审问题 3（上一轮）：以前"正文是否过期"只比正确率、命中总数和主观总分/满分 ——
    把来源从"来源不明"改成"已确认"、或者把命中点从 1/3 换成 2/4（总分不变），
    旧比法都看不出差别，于是正文里的建议已经失真却仍标"是最新的"。
    复审问题 2（本轮）：客观题逐题的命中与**错因**、主观题的错因与考点名、
    历史的日期/模式/现算分子分母，AI 都会读到，也必须进指纹。

    ⚠️ 薄弱点快照是"当时的产物"（无法重算，页面本来就标它"当时"），所以它进指纹
    只为留痕与防篡改，**不承担"过期"判定**——真正参与比较的是可重算的那些。
    """
    return {
        "sess": [sess.get("date"), sess.get("mode"), sess.get("note") or "",
                 sess.get("objective_total") or 0, sess.get("objective_right") or 0],
        "point": [sc.get("point_hits"), sc.get("point_total"), sc.get("point_rate")],
        "subj": [sc.get("subjective_score"), sc.get("subjective_full"),
                 sc.get("subjective_source"), sc.get("confirmed_n"), sc.get("legacy_n"),
                 sc.get("ai_reference_n"), sc.get("unscored_n")],
        # 逐条作答（客观+主观）：题型、命中、**错因**、考点名、题目是否还在
        "attempts": list(attempts_basis or []),
        # 主观题的计分事实（来源、所选分支、命中序号、得分）—— 这些是"同分不同依据"的核心
        "scoring": [[d.get("question_id"), d.get("qtype"), d.get("source"),
                     d.get("selected_option"), d.get("hit_seqs"), d.get("score"),
                     d.get("full"), d.get("reason")]
                    for d in (sc.get("subjective_detail") or [])],
        # 历史：日期/模式/现算分子分母都要在（同率不同分子分母必须能区分）
        "history": [[h.get("id"), h.get("date"), h.get("mode"), h.get("point_hits"),
                     h.get("point_total"), h.get("objective_score"),
                     h.get("subjective_score"), h.get("subjective_full")]
                    for h in (history or [])],
        "weak": [[w.get("name"), w.get("path"), w.get("hit"), w.get("total")]
                 for w in (weak_points or [])],
    }


def weak_basis_of(metrics):
    """从（存下来或刚算出的）metrics 里取"薄弱点"那一部分，供 `content_basis()` 复用。

    **字段必须与生成时那一份完全一致**（含 `path`）：少一个字段，生成与读取算出的
    指纹就不相等，报告会被永久误判成"依据已变化"（本轮就踩了一次：漏了 path）。
    """
    return [{"name": w.get("name"), "path": w.get("path"),
             "hit": w.get("hit"), "total": w.get("total")}
            for w in ((metrics or {}).get("weak_points") or [])]


def analysis_judge_cap(points):
    """辨析题的封顶分值 = **该题判断点的分值**；取不到返回 `(None, 原因)`。

    判断点 = 第 1 个采分点（`split_points.py` 的校验保证它写的是「判断：该说法错误/正确」）。
    **为什么不能写死 3 分**（设计 §3）：判断这一维的分值随卷面变 —— 2007–2009 的辨析题
    是每题 10 分（官方解析写「判断 2 分 + 理由按点分」），2010 起是 15 分（判断 3 分）。
    实测当前库 62 道辨析题的首点都是 3 分，所以写死 3 今天"看不出错"，但一旦按官方口径
    修正早期数据，封顶就会算错，而且是**静默**算错（分数照样显示，只是多扣/少扣）。
    这里的 3 处硬编码（`daily_grade` 的 cap、批改提示词的两句话）由此收成一处判据。
    """
    pts = list(points or [])
    if not pts:
        return None, "没有采分点，无法确定判断点分值"
    sc = pts[0].get("score")
    if isinstance(sc, (int, float)) and not isinstance(sc, bool) and sc > 0:
        return float(sc), None
    return None, "判断点没有分值"


def verify_grade_v2(data, points, full, sections=None, qtype=None, judge_cap=None):
    """新版（`schema_version=2`）批改结果能不能进「可信 AI 参考分」—— 服务端自己加一遍。

    返回 `{"status", "errors", "sum_awards", "extra", "delta", "expected"}`：
      · `status="v2_ok"`       逐点分账自洽 → 这个 AI 分数可以进趋势；
      · `status="needs_review"` 对不上 → 仍保存评语与原始结果，但**不进可信趋势**，
        也**不会自动再调一次 AI**（设计 §3：语义对账失败不重试）。

    查什么（设计 §3 逐条）：
      1. `point_awards` 里序号**唯一且存在**于该题采分点；
      2. 每个点 `awarded` 在 `0 ~ 该点分值` 之间；
      3. 逐点实得 + 加分 + 调整 == 声称的总分（误差 ≤ 0.01），再按 `0 ~ 满分` 夹一次；
      4. 辨析题判断错/未判断时，总分不超过**该题判断点的分值**；
      5. 小问分数与所属逐点分数对得上（每问 ≤ 该问满分，且各问之和与账对得上 ——
         **等于逐点实得之和、或等于含加分/调整的总分**都算自洽；只有 `key="*"` 一个
         「整题小问」的题**不做这条**，见下方 2026-10-09 的说明）；
      6. 选做题：逐点实得只能落在**学生作答的那一支**上。
    **自评与 AI 参考分不同是正常的对照信息，不触发待核** —— 这里只核 AI 自己是否自洽。
    """
    errs = []
    pts = [p for p in (points or []) if isinstance(p, dict)]
    by_seq = {str(p.get("seq")): p for p in pts}
    full_v = _num(full)

    def _result(errs_, **kw):
        """**每条路径都返回同一组字段**：少一个 key 就会在调用方 KeyError
        （本轮就踩了：早退分支只给 status/errors，`daily_grade` 取 sum_awards 直接崩）。"""
        out = {"status": "v2_ok" if not errs_ else "needs_review", "errors": errs_[:8],
               "sum_awards": 0.0, "extra": 0.0, "delta": 0.0, "raw": None,
               "expected": None, "judge_cap": judge_cap}
        out.update(kw)
        return out

    if not isinstance(data, dict):
        return _result(["批改结果不是对象"])
    if data.get("schema_version") != 2:
        return _result(["不是新版分账（schema_version≠2）"])
    if full_v is None or full_v <= 0:
        return _result(["题目满分缺失，无法对账"])

    awards = data.get("point_awards")
    if not isinstance(awards, list) or not awards:
        return _result(["缺少 point_awards（逐点实得）"])
    seen, sum_awards = set(), 0.0
    for a in awards:
        if not isinstance(a, dict):
            errs.append("point_awards 里有非对象项")
            continue
        seq = str(a.get("seq"))
        if seq in seen:
            errs.append(f"序号 {seq} 重复")
            continue
        seen.add(seq)
        p = by_seq.get(seq)
        if p is None:
            errs.append(f"序号 {seq} 不在本题采分点里")
            continue
        aw = _num(a.get("awarded"))
        cap_pt = _num(p.get("score"))
        if aw is None or aw < 0:
            errs.append(f"第 {seq} 点的实得分不是非负数")
            continue
        if cap_pt is not None and aw > cap_pt + 0.01:
            errs.append(f"第 {seq} 点实得 {aw:g} 超过该点满分 {cap_pt:g}")
            continue
        sum_awards = round(sum_awards + aw, 2)
    missing = [s for s in by_seq if s not in seen]
    if missing:
        errs.append(f"有 {len(missing)} 个采分点没有逐点实得（{missing[:4]}）")

    extra = 0.0
    for x in (data.get("extra_credit") or []):
        if isinstance(x, dict):
            v = _num(x.get("awarded"))
            if v is None:
                errs.append("加分项缺 awarded（实得加分）")
            else:
                extra = round(extra + v, 2)
        else:
            errs.append("加分项不是对象")
    delta = 0.0
    for x in (data.get("adjustments") or []):
        if isinstance(x, dict):
            v = _num(x.get("delta"))
            if v is None:
                errs.append("调整项缺 delta（有符号金额）")
            else:
                delta = round(delta + v, 2)
        else:
            errs.append("调整项不是对象")

    raw = round(sum_awards + extra + delta, 2)
    expected = min(max(raw, 0.0), full_v)
    if qtype == "analysis" and isinstance(judge_cap, (int, float)) and not isinstance(judge_cap, bool):
        j = data.get("judgment") or {}
        if not (j.get("explicit") and j.get("matched")):
            expected = min(expected, float(judge_cap))
    got = _num(data.get("score"))
    if got is None:
        errs.append("没有总分")
    elif abs(got - expected) > 0.01:
        errs.append(f"总分对不上：声称 {got:g}，逐点+加分+调整+夹取后应为 {expected:g}")

    # 小问：每问不超该问满分，且各问之和 == 逐点实得之和（或 == 含加分的总分）
    secs = [s for s in (sections or []) if isinstance(s, dict) and str(s.get("key") or "")]
    if secs:
        sec_of = {str(s.get("key")): _num(s.get("score")) for s in secs}
        got_by_sec = {}
        for a in awards:
            if not isinstance(a, dict):
                continue
            p = by_seq.get(str(a.get("seq")))
            if not p:
                continue
            aw = _num(a.get("awarded"))
            if aw is None:
                continue
            k = str(p.get("section") or "")
            if k:
                got_by_sec[k] = round(got_by_sec.get(k, 0.0) + aw, 2)
        for k, v in got_by_sec.items():
            want = sec_of.get(k)
            if want is None:
                errs.append(f"小问 {k} 的得分对应不到声明的小问")
            elif v > want + 0.01:
                errs.append(f"小问 {k} 得 {v:g} 超过该问满分 {want:g}")
        # 「整题小问」的题（`sections` 只有一个 key="*"）：各问之和等于把总分检查换个
        # 基准再做一遍，而这个基准**不含言之有理加分项** —— AI 把加分记进小问就必然误判。
        # 2026-10-09 真实案例 q963：逐点 12.5 ＋ 加分 1 ＝ 总分 13.5，小问也写 13.5，
        # 被判「各小问得分之和 13.5 ≠ 逐点实得之和 12.5」而不进可信参考分（库里 attempt 210
        # 同型）。真实库 149 道题是这种形态，所以：这种题整题只保留上面的总分对账。
        star_only = len(secs) == 1 and str(secs[0].get("key")) == "*"
        listed = data.get("section_scores")
        if isinstance(listed, list) and listed:
            tot = 0.0
            for s in listed:
                v = _num((s or {}).get("got")) if isinstance(s, dict) else None
                if v is None:
                    errs.append("section_scores 里有缺 got 的项")
                else:
                    tot = round(tot + v, 2)
            if star_only:
                # 整题小问不再比记账基准（比了必然误判），但「报得比满分还多」是胡说，
                # 留着这条最硬的检查，免得页面显示一个 99/15 的小问分。
                if tot > full_v + 0.01:
                    errs.append(f"各小问得分之和 {tot:g} 超过题目满分 {full_v:g}")
            # 两种记账都算自洽：等于逐点实得之和（旧口径），或等于含加分/调整并夹取后的
            # 总分（多小问题更自然的记法）。硬卡一边就是上面那个误判。
            elif abs(tot - sum_awards) > 0.01 and abs(tot - expected) > 0.01:
                errs.append(f"各小问得分之和 {tot:g} 既不等于逐点实得之和 {sum_awards:g}，"
                            f"也不等于对账后的总分 {expected:g}")

    return _result(errs, sum_awards=sum_awards, extra=extra, delta=delta,
                   raw=raw, expected=expected)


def exec_sql(sql, args=()):
    con = sqlite3.connect(DB)
    try:
        cur = con.execute(sql, args)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def ensure_schema():
    """建表 / 迁移（幂等）；启动和保存等入口复用。"""
    con = sqlite3.connect(DB, timeout=30)
    try:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id               INTEGER PRIMARY KEY,
            date             TEXT NOT NULL,
            mode             TEXT,
            note             TEXT,
            objective_total  INTEGER DEFAULT 0,
            objective_right  INTEGER DEFAULT 0,
            objective_score  REAL    DEFAULT 0,
            subjective_count INTEGER DEFAULT 0,
            point_hits       INTEGER DEFAULT 0,
            point_total      INTEGER DEFAULT 0,
            weak_snapshot    TEXT,
            created_at       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);

        -- 试卷持久化：默认一直用同一份卷子，只有显式「重新组卷」才换
        CREATE TABLE IF NOT EXISTS papers (
            id             INTEGER PRIMARY KEY,
            created_at     TEXT,
            label          TEXT,
            single_ids     TEXT,      -- JSON 数组，保持顺序
            subjective_ids TEXT,
            status         TEXT DEFAULT 'active'
        );
        CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);

        -- 学习复盘报告：每次练习生成一份，累积后做长期分析
        CREATE TABLE IF NOT EXISTS reports (
            id          INTEGER PRIMARY KEY,
            session_id  INTEGER REFERENCES sessions(id),
            date        TEXT,
            mode        TEXT,
            content     TEXT,      -- AI 生成的 Markdown 报告
            metrics     TEXT,      -- JSON：本次量化指标快照
            created_at  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(date);

        -- 用户的复盘修正独立保存；重新生成或删除 AI 正文都不会覆盖它。
        CREATE TABLE IF NOT EXISTS review_notes (
            session_id INTEGER PRIMARY KEY REFERENCES sessions(id),
            summary TEXT,
            dismissed_ids TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS retest_plans (
            id INTEGER PRIMARY KEY,
            source_attempt_id INTEGER NOT NULL UNIQUE REFERENCES attempts(id),
            source_session_id INTEGER NOT NULL REFERENCES sessions(id),
            question_id INTEGER NOT NULL,
            initial_json TEXT NOT NULL,
            intervals TEXT NOT NULL,
            round_index INTEGER NOT NULL DEFAULT 0,
            due_at TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            generation INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS retest_runs (
            id TEXT PRIMARY KEY,
            plan_id INTEGER NOT NULL REFERENCES retest_plans(id),
            kind TEXT NOT NULL,
            round_index INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            due_at TEXT,
            state TEXT NOT NULL,
            started_at TEXT NOT NULL,
            locked_at TEXT,
            answer_text TEXT,
            selected_option TEXT,
            question_json TEXT NOT NULL,
            criteria TEXT,
            saved_attempt_id INTEGER UNIQUE REFERENCES attempts(id),
            saved_session_id INTEGER REFERENCES sessions(id),
            snapshot_json TEXT,
            completed_at TEXT,
            counted INTEGER NOT NULL DEFAULT 0,
            note TEXT,
            commit_hash TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_retest_due ON retest_plans(status,due_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_retest_open ON retest_runs(plan_id)
            WHERE state IN ('answering','grading');
        CREATE TABLE IF NOT EXISTS pilot_packages (
            version TEXT PRIMARY KEY, digest TEXT NOT NULL, content_json TEXT NOT NULL,
            sources_json TEXT NOT NULL, knowledge_json TEXT NOT NULL, root TEXT NOT NULL, installed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pilot_runs (
            id TEXT PRIMARY KEY, package TEXT NOT NULL, slot TEXT NOT NULL, kind TEXT NOT NULL,
            parent_id TEXT, retest_run_id TEXT UNIQUE, state TEXT NOT NULL, question_json TEXT NOT NULL,
            outline_text TEXT, answer_text TEXT, draft_json TEXT NOT NULL DEFAULT '{}',
            hints_json TEXT NOT NULL DEFAULT '[]', meta_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, locked_at TEXT, feedback_at TEXT, target TEXT,
            revision INTEGER NOT NULL DEFAULT 0, materialized_qid INTEGER,
            session_id INTEGER UNIQUE, attempt_id INTEGER UNIQUE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pilot_initial ON pilot_runs(package,slot) WHERE kind='initial';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pilot_correction ON pilot_runs(parent_id) WHERE kind='correction';
        CREATE TABLE IF NOT EXISTS pilot_diagnoses (
            id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, content_json TEXT NOT NULL,
            evaluator_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'suggested',
            user_note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS curriculum_packages (
            version TEXT PRIMARY KEY, digest TEXT NOT NULL, bundle_json TEXT NOT NULL,
            root TEXT NOT NULL, installed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pilot_question_links (
            identity TEXT PRIMARY KEY, question_id INTEGER NOT NULL,
            stem_sha256 TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS learning_exposures (
            id INTEGER PRIMARY KEY, question_identity TEXT NOT NULL, question_id INTEGER,
            concept_ids_json TEXT NOT NULL DEFAULT '[]', kind TEXT NOT NULL,
            package TEXT, run_id TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_learning_exposure_identity ON learning_exposures(question_identity,created_at);
        CREATE TABLE IF NOT EXISTS pilot_feedback (
            package TEXT PRIMARY KEY, burden TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
        );
        """)

        # 迁移：把作答记录挂到具体的 session 上。
        # 在此之前只能靠「日期 + 模式」猜是哪一次练习，复盘报告里的「本次作答明细」会串场。
        cols = {r[1] for r in con.execute("PRAGMA table_info(attempts)")}
        if "session_id" not in cols:
            con.execute("ALTER TABLE attempts ADD COLUMN session_id INTEGER")

        # 迁移：采分点的「小问分组 + 逐点分值」。
        # **必须放在 server 的迁移里**，不能只写在 app/split_points.py（数据管线脚本）中 ——
        # 已实测踩到：用 DSH_DB 指向一个「points 表还没有 score 列」的旧库启动，
        # `/api/reports` 直接 500（`no such column: p.score`），而这个错误
        # 只在**走空库/旧库那条测试路径**时才暴露，拿真实库跑一切正常。
        # 建表语句（build_db.py）里本来就有 score 列，中间补的这两列全靠迁移。
        pcols = {r[1] for r in con.execute("PRAGMA table_info(points)")}
        for col, ddl in (("score", "REAL"), ("section", "TEXT"),
                         ("score_src", "TEXT"), ("score_note", "TEXT")):
            if col not in pcols:
                con.execute(f"ALTER TABLE points ADD COLUMN {col} {ddl}")
        con.commit()
        con.execute("CREATE INDEX IF NOT EXISTS idx_attempts_session ON attempts(session_id)")

        # 迁移：主观题的「作答原文」与「AI 批改结果」落库。
        # 为什么必须存：在此之前只存"自评命中了几个采分点"，作答原文与批改详情
        # **批完渲染到页面上就丢了**（`daily.html` 从不回传）。练习日志要展开到
        # 题目级明细（题干 / 我的作答 / 批改情况），没有这两列就只能显示"命中 3/5"。
        # 两列都只对主观题写值；客观题的选项沿用 `note`（历史数据不能动）。
        if "student_answer" not in cols:
            con.execute("ALTER TABLE attempts ADD COLUMN student_answer TEXT")
        if "grade" not in cols:
            con.execute("ALTER TABLE attempts ADD COLUMN grade TEXT")
        con.commit()

        # 迁移：主观题结果的「来源」与选做题「作答的是哪一支」（设计 §2.2）。
        #
        # `point_source` 为什么必须有：AI 批改以前会**自动替用户勾选**自评框并写进
        # point_hits，日常页却写着"AI 批改结果不入命中率统计" —— 文案与实现相反，
        # 而旧记录又没存每次命中的来源，事后再也分不清"学生自己勾的"和"AI 建议的"。
        # 取值固定四种（别的值一律当没填）：
        #   self_confirmed 用户完成自评 / ai_confirmed 用户明确采纳 AI 建议 /
        #   ai_unconfirmed 只收到 AI 建议 / unassessed 尚未评定。
        # **旧行的 NULL 就是 legacy_unknown**：不回填成 self_confirmed（那是编造历史），
        # 读取时按"来源不明"单独分账并标注。
        #
        # `selected_option` 只对 Ⅰ/Ⅱ 任选一题的 30 分选做题写值（'Ⅰ' 或 'Ⅱ'）：
        # 两支各自满分 30，不知道学生答的是哪一支就没法折分（加一起会变 60）。
        if "point_source" not in cols:
            con.execute("ALTER TABLE attempts ADD COLUMN point_source TEXT")
        if "selected_option" not in cols:
            con.execute("ALTER TABLE attempts ADD COLUMN selected_option TEXT")
        con.commit()

        # 迁移：把「这一次练习发的整份题」记在 session 上（JSON 数组，保持出题顺序）。
        # 为什么必须存：attempts 里**只有做过的题** —— 没作答的题没有任何行，
        # 日志里就永远看不到它们（用户要的恰恰是"包括没做的"完整一份卷子）。
        # 为什么不另建 session_questions 表：一次练习就是一张题单，跟 session 一对一，
        # 单独建表只多一次 JOIN，换不来任何东西。
        scols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        if "question_ids" not in scols:
            con.execute("ALTER TABLE sessions ADD COLUMN question_ids TEXT")
        # 同一次提交的重试只返回第一次的结果；NULL 兼容旧客户端和旧记录。
        for col in ("submission_id", "submission_hash", "save_response"):
            if col not in scols:
                con.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_submission_id "
                    "ON sessions(submission_id) WHERE submission_id IS NOT NULL")
        con.commit()

        # 迁移：补齐采分点的大纲挂载（继承所属题目的考点）
        backfill_point_outline(con)
        # 迁移：统一挂载层级（point/goal → section/board）
        unify_outline_levels(con)
        # 视图：把「真题 ≠ AI 生成」这个过滤固化下来。
        # 为什么：此前吃过一次亏——查询里漏加 `source NOT LIKE 'AI%'`，
        # 结果 AI 生成的题混进了「真题优先」的抽取结果，而且不报错、只是数据悄悄变脏。
        # 视图名用 v_ 前缀，可被任何消费方（脚本 / 前端接口 / 报告）复用。
        # 保存入口也会确保结构。DROP/CREATE若逐句自动提交，并发调用可先后DROP
        # 同一视图、再重复CREATE；整组DDL用数据库写事务串行，失败亦不留半组视图。
        con.executescript("""
        BEGIN IMMEDIATE;
        DROP VIEW IF EXISTS v_real_questions;
        CREATE VIEW v_real_questions AS
            SELECT * FROM questions
            WHERE (source IS NULL OR source NOT LIKE 'AI%')
              AND (source IS NULL OR source NOT LIKE '试点私有%');

        -- 选做 II 拆出来的派生题：练习/专项可用，但不能进模拟考。
        -- 第 56 题原卷是一题两分支，拆出的 Ⅱ 分支若进模拟卷，会和原题重复出现。
        DROP VIEW IF EXISTS v_exam_questions;
        CREATE VIEW v_exam_questions AS
            SELECT * FROM v_real_questions
            WHERE (source IS NULL OR source NOT LIKE '真题·选做II%');

        DROP VIEW IF EXISTS v_questions_tagged;
        CREATE VIEW v_questions_tagged AS
            SELECT q.*, o.level AS outline_level, o.path AS outline_path, o.name AS outline_name
            FROM questions q JOIN outline_nodes o ON o.id = q.outline_id;

        DROP VIEW IF EXISTS v_questions_ready;
        CREATE VIEW v_questions_ready AS
            SELECT id, year, qtype, number, stem, answer, outline_id, source,
                   (extra LIKE '%explain%')     AS has_explain,
                   (extra LIKE '%answer_text%') AS has_answer_text
            FROM questions;
        COMMIT;
        """)
        con.commit()
    finally:
        con.close()


def _outline_levels():
    return {r["id"]: r for r in rows("SELECT id,parent_id,level FROM outline_nodes")}


def unify_outline_levels(con=None):
    """统一挂载层级：题目/采分点的考点一律归到 `section` 或 `chapter`。

    为什么需要：「薄弱点」按考点聚合，挂在哪个层级决定了它显示成考点名还是章名。
    实测问题：21 道题挂在同一个 `point`（「3. 裴斯泰洛齐的教育思想」）——**太细**，
    这些题考的是整节内容；另有采分点挂在 `goal`（考查目标，根本不是考点）。

    规则（大纲树实测结构：board ← chapter ← section ← point；goal 挂 board 下）：
      - `point` → 上溯到所属 `section`
      - `goal`  → 上溯到所属 `board`
      - `chapter` → 保留（章级是合法粒度，下推到节会**丢信息**）
      - `board` → 保留（4 个采分点挂在板块上；硬改会散进 35 个章，反而更乱）
    只动「层级不对」的行，`section`/`chapter` 的行原样不动，因此幂等。
    """
    own = con is None
    if own:
        con = sqlite3.connect(DB)
    # 无条件设 row_factory：调用方（如 ensure_schema 启动路径）传进来的连接可能没设，
    # 那样 r["id"] 会抛 TypeError: tuple indices must be integers。
    # 这个 bug 被测试掩盖过——因为测试自己建连接时设了 row_factory（教训：启动路径要单独测）。
    con.row_factory = sqlite3.Row
    try:
        info = {r["id"]: dict(r) for r in con.execute(
            "SELECT id,parent_id,level FROM outline_nodes")}

        def up(nid, stop_level, limit=20):
            cur = info.get(nid)
            for _ in range(limit):
                if cur is None:
                    return None
                if cur["level"] == stop_level:
                    return cur["id"]
                cur = info.get(cur["parent_id"])
            return None

        fixed = 0
        for table in ("questions", "points"):
            bad = con.execute(
                f"SELECT DISTINCT outline_id FROM {table} "
                f"WHERE outline_id IS NOT NULL AND outline_id IN "
                f"(SELECT id FROM outline_nodes WHERE level NOT IN ('section','chapter'))"
            ).fetchall()
            moves = []
            for r in bad:
                oid = r[0]
                lv = info[oid]["level"]
                if lv == "point":
                    tgt = up(oid, "section")
                elif lv == "goal":
                    tgt = up(oid, "board")
                else:                     # board（以及未知层级）：保持不动
                    tgt = None
                if tgt and tgt != oid:
                    moves.append((tgt, oid))
            for tgt, oid in moves:
                cur = con.execute(
                    f"UPDATE {table} SET outline_id=? WHERE outline_id=?", (tgt, oid))
                fixed += cur.rowcount
        return fixed
    finally:
        if own:
            con.commit()
            con.close()


def backfill_point_outline(con=None):
    """补齐采分点的大纲挂载：`points.outline_id` 为空时，继承所属题目的考点。

    为什么该继承而不是让 AI 逐个判：
      - 采分点是某道题的判分依据，天然属于这道题考的考点；真题（847/888）里
        801 个没挂，但它们的父题 **100% 都有挂载** —— 纯派生数据，不需要 AI 判断。
      - 不继承的后果（实测）：主观题样本只有 9.8% 进得了薄弱点统计，
        能收到主观题样本的考点只有 19 个；继承后是 100% / 78 个。
    为什么做成幂等迁移而不是一次性脚本：否则重建数据库（build_db → ingest_*）后又会退回去。

    只补空值，不覆盖已有的更细粒度挂载。
    """
    own = con is None
    if own:
        con = sqlite3.connect(DB)
    try:
        rows = con.execute(
            "SELECT p.id, q.outline_id FROM points p JOIN questions q ON q.id = p.question_id "
            "WHERE p.outline_id IS NULL AND q.outline_id IS NOT NULL"
        ).fetchall()
        if rows:
            con.executemany("UPDATE points SET outline_id=? WHERE id=?",
                            [(r[1], r[0]) for r in rows])
        return len(rows)
    finally:
        if own:
            con.commit()
            con.close()


#: 本地单机应用：HTML 与共用组件每次现读磁盘，并**明确禁止缓存**。
#: 为什么：这两个文件没有 ETag/Last-Modified 时浏览器会**启发式缓存**，
#: 改完前端刷新还可能拿到旧的 qa-answer.js —— 表现就是"修了但看起来没修"。
NO_CACHE = {"Cache-Control": "no-store, must-revalidate"}


def page(name):
    return HTMLResponse((STATIC / name).read_text(encoding="utf-8"), headers=NO_CACHE)


@app.get("/static/{name}")
def static_file(name: str):
    """共用导航、样式、答案组件与专项页面脚本；只开放明确的文件白名单。

    为什么要给共用组件开这个路由：四个页面都要渲染「采分点 + 分值 + 参考答案 +
    批改结果」，各写一遍必然走偏（README 里记着：改了一个页面漏了另一个，
    表现只是标签静默失效）。放成一个文件、一处实现。
    只允许白名单文件名，避免路径穿越。
    """
    if name not in ("qa-answer.js", "qa-answer.css", "practice.js", "app-ui.css", "app-shell.js", "review.js", "review.css", "retest.js", "retest.css", "pilot.js", "pilot.css", "coverage.js", "coverage.css"):
        return JSONResponse({"ok": False, "error": "不存在的静态资源"}, status_code=404)
    p = STATIC / name
    if not p.exists():
        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
    media = "application/javascript" if name.endswith(".js") else "text/css"
    return Response(p.read_text(encoding="utf-8"), media_type=media, headers=NO_CACHE)


def load_paper_by_row(prow):
    """按 papers 记录还原整份试卷。"""
    try:
        sids = json.loads(prow["single_ids"] or "[]")
        subids = json.loads(prow["subjective_ids"] or "[]")
    except Exception:
        return None
    if not sids or not subids:
        return None

    def fetch(ids):
        qs = ",".join("?" * len(ids))
        got = {r["id"]: r for r in rows(
            f"SELECT id,number,qtype,stem,options,answer,outline_id,extra,year,source "
            f"FROM questions WHERE id IN ({qs})", ids)}
        return [got[i] for i in ids if i in got]   # 保持入库时的顺序

    singles = fetch(sids)
    subs = fetch(subids)
    for s in singles:
        try:
            s["options"] = json.loads(s.get("options") or "{}")
        except Exception:
            s["options"] = {}
    for s in subs:
        try:
            ex = json.loads(s.get("extra") or "{}")
        except Exception:
            ex = {}
        s["full_score"] = ex.get("full_score", 15)
        s["answer_note"] = ex.get("answer_note") or None
        # 参考答案要显式带出来：组件靠 `reference` 渲染「官方参考答案」那一折，
        # 以前 paper 接口根本不返回 answer_text（只有 logs 接口有），
        # 于是模拟卷页主观题下面**没有**参考答案可对照 —— 验收时才发现。
        s["reference"] = ex.get("answer_text") or None
        s["explain"] = ex.get("explain") or None
        s["sections"] = sections_of(ex)
        s["points"] = points_of_question(s["id"])
        s["score_stats"] = score_stats(s["points"], s["full_score"], s["sections"], s["stem"])

    from collections import Counter
    yc = Counter(x.get("year") for x in singles + subs if x.get("year"))
    real_n = sum(1 for x in singles + subs if (x.get("source") or "") != "AI生成")
    return {
        "paper_id": prow["id"],
        "created_at": prow["created_at"],
        "minutes": EXAM_MINUTES,
        "counts": {
            "single": len(singles),
            "analysis": sum(1 for x in subs if x["qtype"] == "analysis"),
            "short": sum(1 for x in subs if x["qtype"] == "short"),
            "essay": sum(1 for x in subs if x["qtype"] == "essay"),
            "subjective": len(subs),
            "points": sum(len(x["points"]) for x in subs),
            "real": real_n,
            "ai": len(singles) + len(subs) - real_n,
            "years": len(yc),
        },
        "singles": singles,
        "subjectives": subs,
    }


def _same_file(a, b) -> bool:
    """同一个文件？用 inode/文件索引比，别比字符串。

    Windows 的 8.3 短名（`C:\\Users\\JIEQIN~1\\…`）与长名指向同一个文件，
    字符串比较会漏判 —— 测试的 `DSH_DB` 判断早就踩过这一条。
    """
    try:
        return os.path.samefile(a, b)
    except (OSError, TypeError):
        try:
            return Path(a).resolve() == Path(b).resolve()
        except (OSError, TypeError):
            return False


@app.get("/api/health")
def health(request: Request):
    """自省：这个服务进程读的是哪份库、跑在哪个端口、pid 是多少。

    **为什么要有这个接口**：测试必须能*证明*"我 HTTP 打的那个服务连的是副本库"，
    而不是靠"我以为我起对了端口"。以前 `verify_resume_ui.py` 只能断言**自己进程**里
    的 `server.DB`，而真正被请求打的那个服务是**人手动起的** —— 没有任何东西能证明
    它连的是哪份库（连接错了也不报错，只是静默写到真实数据上）。

    顺带也方便排查"服务怎么连到别的库去了"：浏览器打开 /api/health 就有答案。

    只回路径与布尔值，**不含 API key、不含任何配置内容**。
    """
    from dbpath import LIVE_DB          # 真实库的规范路径（未设 DSH_DB 时用的就是它）
    bound_server = request.scope.get("server")
    return {"ok": True,
            "db": str(DB),
            "db_is_real": _same_file(DB, LIVE_DB),
            "port": bound_server[1] if bound_server else None,
            "pid": os.getpid()}


@app.get("/api/paper")
def paper(rebuild: int = 0, difficulty: int = 3, thinking: int = 1):
    """取当前试卷。

    默认返回**正在使用的那一份** —— 刷新页面、关掉重开都拿到同一份卷子，
    作答不会白做。只有传 rebuild=1 才归档旧卷并重新组卷。

    difficulty / thinking 只影响**组卷时 AI 补的那几道题**（真题足够时用不到）。
    模拟考试页没有设置窗口，它读的是和日常练习同一份浏览器设置（同源 localStorage）。
    """
    ensure_schema()
    if not rebuild:
        prow = rows("SELECT * FROM papers WHERE status='active' ORDER BY id DESC LIMIT 1")
        if prow:
            existing = load_paper_by_row(prow[0])
            if existing:
                return existing

    PLAN = {"single": 45, "analysis": 3, "short": 5, "essay": 3}

    def take(qtype, limit):
        return rows(
            "SELECT id,number,qtype,stem,options,answer,outline_id,extra,year,source FROM v_exam_questions "
            "WHERE qtype=? "
            "ORDER BY RANDOM() LIMIT ?",
            (qtype, limit),
        )

    picks = {k: take(k, v) for k, v in PLAN.items()}
    shortfall = {k: PLAN[k] - len(picks[k]) for k in PLAN}
    print(f"[组卷] 真题命中: " + ", ".join(f"{k}={len(picks[k])}/{PLAN[k]}" for k in PLAN))

    # 缺口用 AI 补：随机取一批已挂考点的 section 作为命题范围
    if any(v > 0 for v in shortfall.values()):
        scope = rows(
            "SELECT id,path FROM outline_nodes WHERE level='section' "
            "AND id IN (SELECT DISTINCT outline_id FROM questions WHERE outline_id IS NOT NULL) "
            "ORDER BY RANDOM() LIMIT 12"
        )
        if not scope:
            scope = rows("SELECT id,path FROM outline_nodes WHERE level='section' ORDER BY RANDOM() LIMIT 12")
        try:
            gen, _, gen_audit = ai_mod.generate_questions(
                scope,
                n_single=shortfall["single"],
                n_analysis=shortfall["analysis"],
                n_short=shortfall["short"],
                n_essay=shortfall["essay"],
                difficulty=difficulty,
                thinking=bool(thinking),
            )
            # 入库走**同一个函数**（含分值可信度校验与小问结构落库）。
            # 以前这里自己写了一份 INSERT：points 的 score/section 没写、extra 也没有
            # sections/评分说明，于是同一道 AI 题"刚出出来有分值、刷新后变分值待核"。
            saved = _save_ai_questions(gen, strict_points=False,
                                       difficulty=difficulty, thinking=bool(thinking))
            for q in saved["single"]:
                picks["single"].append(q["row"])
            for q in saved["subjective"]:
                picks[q.get("qtype", "short")].append(q["row"])
        except Exception as e:
            print(f"[组卷] AI 补题失败（仅用真题）: {e}")

    # 组装
    singles = picks["single"]
    for s in singles:
        try:
            s["options"] = json.loads(s.get("options") or "{}")
        except Exception:
            s["options"] = {}

    subs = []
    for k in ("analysis", "short", "essay"):
        for s in picks[k]:
            default_full = 15 if k != "essay" else 30
            try:
                ex = json.loads(s.get("extra") or "{}")
            except Exception:
                ex = {}
            s["full_score"] = ex.get("full_score", default_full)
            s["answer_note"] = ex.get("answer_note") or None
            s["reference"] = ex.get("answer_text") or None
            s["explain"] = ex.get("explain") or None
            s["sections"] = sections_of(ex)
            s["points"] = points_of_question(s["id"])
            s["score_stats"] = score_stats(s["points"], s["full_score"], s["sections"], s["stem"])
            subs.append(s)

    real_n = sum(1 for s in singles + subs if s.get("source") != "AI生成")

    # 归档旧卷 + 持久化新卷（这样刷新页面拿到的是同一份）
    con = sqlite3.connect(DB)
    con.execute("UPDATE papers SET status='archived' WHERE status='active'")
    cur = con.execute(
        "INSERT INTO papers (created_at,label,single_ids,subjective_ids,status) VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"),
         datetime.now().strftime("%Y-%m-%d 模拟卷"),
         json.dumps([q["id"] for q in singles]),
         json.dumps([q["id"] for q in subs]), "active"),
    )
    pid = cur.lastrowid
    con.commit()
    con.close()

    return {
        "paper_id": pid,
        "minutes": EXAM_MINUTES,
        "target": PLAN,
        "counts": {
            "single": len(singles),
            "analysis": len(picks["analysis"]),
            "short": len(picks["short"]),
            "essay": len(picks["essay"]),
            "subjective": len(subs),
            "points": sum(len(s["points"]) for s in subs),
            "real": real_n,
            "ai": len(singles) + len(subs) - real_n,
        },
        "singles": singles,
        "subjectives": subs,
    }


def _attach_grade_override(grade_obj, qid, con=None):
    """人工确认出口（设计 §5/D）：给这份批改结果盖 `override` 章，写进库、可审计。

    **前端能声明的只有「人确认过」，不能声明「机器对上了」** —— 这里由服务端**自己重算
    一遍对账**，并把被覆盖的错误原文一并写进 `grade`。理由与 `_ai_reference()` 一样：
    `grade` 是随作答存库的，若认前端传的盖章内容，客户端就能自造一个可信分数。
    另外只给**新版分账**（`schema_version=2`）盖章：人工确认是"替机器确认它自洽"，
    不能给一份没有分账的旧结果凭空造一个可信分。

    返回 `(grade_obj, override, why)`；`override` 为 None 表示没盖章，`why` 说明原因。
    """
    if not isinstance(grade_obj, dict):
        return grade_obj, None, "批改结果不是对象"
    if grade_obj.get("schema_version") != 2:
        return grade_obj, None, "只有新版逐点分账能被人工确认"
    q = rows("SELECT qtype, stem, extra FROM questions WHERE id=?", (qid,), con=con)
    if not q:
        return grade_obj, None, "题目已不在题库"
    try:
        qex = json.loads(q[0]["extra"] or "{}")
    except Exception:
        qex = {}
    if qex.get("private_pilot"):
        return grade_obj, None, "试点定性记录不核定数值分账"
    pts = points_of_question(qid, con=con)
    full = qex.get("full_score")
    secs = sections_of(qex)
    qtype = q[0]["qtype"]
    jcap, _why = analysis_judge_cap(pts) if qtype == "analysis" else (None, None)
    chk = verify_grade_v2(grade_obj, pts, full, secs, qtype, jcap)
    if chk["status"] == "v2_ok":
        # 机器已经对上了，没东西可"确认" —— 盖章反而会把一个 v2_ok 记成"人工担保"，
        # 让汇总里的人数与事实不符。照实拒绝（页面本来也不会给这个按钮）。
        return grade_obj, None, "这份分账已被机器对账通过，无需人工确认"
    ov = {
        "status": "human_ok",
        "at": datetime.now().isoformat(timespec="seconds"),
        # 被覆盖的错误原文：页面上也原样显示，事后能看出"人确认的是什么毛病"
        "errors_overridden": list(chk["errors"]),
        "verification_status": chk["status"],
        "sum_awards": chk["sum_awards"], "extra": chk["extra"], "delta": chk["delta"],
        "expected": chk["expected"],
    }
    out = dict(grade_obj)
    out["override"] = ov
    return out, ov, None


@app.post("/api/save")
def save(payload: dict):
    return _save_submission(payload)


def _save_submission(payload: dict, retest_run_id=None, pilot_run_id=None):
    """保存一次练习记录。

    payload = {
      "date": "2026-09-15",
      "single": [{"id":1,"answer":"A"}, ...],
      "subjective": [{"id":50,"hits":[1,3],"cause":"记混","note":"",
                      "point_source":"self_confirmed","selected_option":"Ⅰ"}, ...],
      "question_ids": [1, 2, 3, ...]   # 本次发的整份卷子的题目 id（含没作答的），可选
      "submission_id": "唯一提交 ID"  # 新页面会传，重试返回首次结果；旧调用方可不传
    }

    `question_ids` 是「完整练习内容」的**唯一依据**：attempts 只记做过的题，
    没有它，没作答的题在日志里根本不存在。老调用方（如早期测试）不传时，
    日志退化成"只有做过的题"，不会报错。

    整次提交使用一个事务：先建 session，再让 attempts / point_hits 直接带上所属 id，
    快照也在同一连接现算。任何一步失败就回滚，不返回假成功；同一 submission_id
    的同一请求只保存一次，内容变了却复用同一 ID 则明确拒绝。

    `point_source` / `selected_option` 是 2026-10-08 新增（设计 §2.2）：
      · 来源不传 = 旧调用方 → 按"来源不明"（legacy_unknown）记，**不猜成用户自评**；
      · 只有 `self_confirmed` / `ai_confirmed` / 不传 才写 `point_hits`；
        `ai_unconfirmed`（仅收到 AI 建议）/ `unassessed` 的建议序号只留在 `grade` 里；
      · `selected_option` 只对 Ⅰ/Ⅱ 选做题有意义，取值 `Ⅰ` / `Ⅱ`。
      · `grade_override=true`（2026-10-09 新增，设计 D）：用户在作答页确认「这份分账我核对过」
        → 服务端**自己重算对账**并把 `override` 写进 `grade`（见 `_attach_grade_override()`），
        该 AI 分数此后计入可信参考分、状态是 `human_ok`（与机器对账通过的 `v2_ok` 分开统计）。
    """
    date = payload.get("date") or datetime.now().strftime("%Y-%m-%d")
    qids = _clean_question_ids(payload.get("question_ids"))
    submission_id = payload.get("submission_id")
    if submission_id is not None:
        if not isinstance(submission_id, str) or not (1 <= len(submission_id.strip()) <= 128):
            return {"ok": False, "error": "submission_id 必须是 1–128 字符的字符串"}
        submission_id = submission_id.strip()
    try:
        hash_payload = dict(payload, _retest_run_id=retest_run_id) if retest_run_id else payload
        submission_hash = (hashlib.sha256(json.dumps(
            hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest() if submission_id else None)
        ensure_schema()
        con = sqlite3.connect(DB, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            # BEGIN IMMEDIATE 让同一个 submission_id 的并发重试串行检查唯一约束。
            # 整份作答只用这一个连接，异常时一次回滚，不留下半份练习。
            con.execute("BEGIN IMMEDIATE")
            if submission_id:
                old = con.execute(
                    "SELECT submission_hash,save_response FROM sessions WHERE submission_id=?",
                    (submission_id,)).fetchone()
                if old:
                    con.rollback()
                    if old["submission_hash"] != submission_hash:
                        return {"ok": False, "error": "这次提交 ID 已用于另一份作答，请刷新后重新提交。"}
                    return json.loads(old["save_response"])

            pilot_context = None
            if pilot_run_id:
                pilot_context, payload, prior = pilot_mod.prepare_save(con, pilot_run_id, _save_ai_questions)
                if prior:
                    con.rollback()
                    return prior
                qids = payload["question_ids"]
                date = payload["date"]
            else:
                for item in payload.get("single", []) + payload.get("subjective", []):
                    row = con.execute("SELECT extra FROM questions WHERE id=?", (item.get("id"),)).fetchone()
                    if row and (_json_or(row[0], {}) or {}).get("private_pilot"):
                        raise retest_mod.RetestError("私有试点题请在试点入口保存，不能带入普通计分。")
            learning = _prepare_learning_save(con, payload, retest_run_id)
            if learning and learning.get("saved_response"):
                con.rollback()
                return learning["saved_response"]
            saved = {"single": 0, "subjective": 0, "point_hits": 0,
                     "answer_text_saved": 0, "grade_saved": 0,
                     "unconfirmed": 0, "unknown_point_source": 0, "unknown_selected_option": 0,
                     "grade_override": 0, "grade_override_skipped": 0}
            cur = con.execute(
                "INSERT INTO sessions (date,mode,note,created_at,question_ids,"
                "submission_id,submission_hash) VALUES (?,?,?,?,?,?,?)",
                (date, payload.get("mode") or "未标注", payload.get("note"),
                 datetime.now().isoformat(timespec="seconds"),
                 json.dumps(qids, ensure_ascii=False) if qids else None,
                 submission_id, submission_hash))
            sid = cur.lastrowid
            saved["session_id"] = sid
            saved["question_ids"] = len(qids)
            single_right = 0

            for item in payload.get("single", []):
                qid = item.get("id")
                if not qid:
                    continue
                row = con.execute("SELECT answer FROM questions WHERE id=?", (qid,)).fetchone()
                correct = (row["answer"] if row else None) or None
                picked = item.get("answer")
                ok = 1 if (correct and picked and picked == correct) else 0
                single_right += ok
                con.execute(
                    "INSERT INTO attempts (question_id,date,mode,hits,total,cause,note,session_id) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (qid, date, "单选", ok, 1, None, picked, sid))
                saved["single"] += 1

            for item in payload.get("subjective", []):
                qid = item.get("id")
                if not qid:
                    continue
                tot = con.execute("SELECT COUNT(*) FROM points WHERE question_id=?", (qid,)).fetchone()[0]
                hits = item.get("hits") or []
                # grade 兼容对象和 JSON 字符串；客户端带来的 override 一律丢弃。
                # 唯一盖章入口是下面显式的 grade_override 意图 + 服务端重新对账。
                grade_in = item.get("grade")
                try:
                    g_obj = json.loads(grade_in) if isinstance(grade_in, str) else grade_in
                except (TypeError, ValueError):
                    g_obj = None
                if isinstance(g_obj, dict):
                    g_obj = dict(g_obj)
                    g_obj.pop("override", None)
                    grade = json.dumps(g_obj, ensure_ascii=False)
                elif isinstance(grade_in, str):
                    grade = grade_in
                elif grade_in is not None:
                    grade = json.dumps(grade_in, ensure_ascii=False)
                else:
                    grade = None
                answer_text = (item.get("answer_text") or "").strip() or None

                if item.get("grade_override"):
                    g_new, ov, _why = _attach_grade_override(g_obj, qid, con=con)
                    if ov:
                        grade = json.dumps(g_new, ensure_ascii=False)
                        saved["grade_override"] += 1
                    else:
                        saved["grade_override_skipped"] += 1

                src = item.get("point_source")
                if src not in POINT_SOURCES:
                    if src not in (None, "", "legacy_unknown"):
                        saved["unknown_point_source"] += 1
                    src = None
                opt = _normalize_option(item.get("selected_option"))
                if item.get("selected_option") and not opt:
                    saved["unknown_selected_option"] += 1
                write_hits = hits if (src in CONFIRMED_SOURCES or src is None) else []
                if src in ("ai_unconfirmed", "unassessed"):
                    saved["unconfirmed"] += 1

                cur = con.execute(
                    "INSERT INTO attempts (question_id,date,mode,hits,total,cause,note,"
                    "student_answer,grade,point_source,selected_option,session_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (qid, date, "主观自评", len(write_hits), tot, item.get("cause"),
                     item.get("note"), answer_text, grade, src, opt, sid))
                aid = cur.lastrowid
                if answer_text:
                    saved["answer_text_saved"] += 1
                if grade:
                    saved["grade_saved"] += 1
                for seq in write_hits:
                    pid = con.execute("SELECT id FROM points WHERE question_id=? AND seq=?",
                                      (qid, seq)).fetchone()
                    if pid:
                        con.execute(
                            "INSERT OR REPLACE INTO point_hits (attempt_id,point_id,hit) VALUES (?,?,1)",
                            (aid, pid["id"]))
                        saved["point_hits"] += 1
                saved["subjective"] += 1

            saved["single_right"] = single_right
            saved["single_score"] = single_right * 2
            subj_ids = [s["id"] for s in payload.get("subjective", []) if s.get("id")]
            ptotal = 0
            if subj_ids:
                qs = ",".join("?" * len(subj_ids))
                ptotal = con.execute(
                    f"SELECT COUNT(*) FROM points WHERE question_id IN ({qs})", subj_ids
                ).fetchone()[0]
            # 同一连接能看见本事务尚未提交的作答，快照与练习记录同一时刻落库。
            snap = weak_snapshot_payload(con=con)
            con.execute(
                "UPDATE sessions SET objective_total=?,objective_right=?,objective_score=?,"
                "subjective_count=?,point_hits=?,point_total=?,weak_snapshot=? WHERE id=?",
                (saved["single"], single_right, float(saved["single_score"]),
                 saved["subjective"], saved["point_hits"], ptotal,
                 json.dumps(snap, ensure_ascii=False), sid))
            subjectives = subjective_attempts(sid, con=con)
            _, scores = score_attempts(subjectives, _attempt_points([a["id"] for a in subjectives], con=con))
            saved.update({k: scores[k] for k in ("subjective_score", "subjective_full",
                                                "subjective_source", "subjective_scored_n", "unscored_kinds")})
            result = {"ok": True, "date": date, "saved": saved}
            if learning:
                result["learning"] = _finish_learning_save(con, learning, sid)
            if pilot_context:
                result["pilot"] = pilot_mod.finish_save(con, pilot_context, sid, _create_retest_plan,
                                                       _learning_snapshot, _finish_learning_save)
            con.execute("UPDATE sessions SET save_response=? WHERE id=?",
                        (json.dumps(result, ensure_ascii=False), sid))
            con.commit()
            return result
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
    except retest_mod.RetestError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        print(f"[save] 提交失败并已回滚：{type(e).__name__}: {e}")
        return {"ok": False, "error": "保存失败，未写入作答或练习记录；请重试。"}


@app.get("/api/weakness")
def weakness():
    return _weakness()


def _weakness(con=None):
    """薄弱点：客观题对错 + **已确认来源**的主观题采分点命中，口径统一地统计。

    每个「样本」= 一次判定机会：
      - 客观题：一次作答 = 1 个样本（对=1 错=0）
      - 主观题：一次作答 × 一个采分点 = 1 个样本（命中=1 未命中=0）

    旧版分子的主观部分来自 point_hits（只记命中），分母来自 points（全部采分点），
    两边口径不同，导致出现过 2/1 = 200% 这种不可能的结果。

    2026-10-08 起主观题**按来源分账**（设计 §2.2），因为以前 AI 批改会自动替用户勾选，
    于是"AI 说命中"直接变成了"学生掌握了"，而日常页还写着"AI 批改结果不入命中率统计"：
      · `items`        客观题 + `self_confirmed` / `ai_confirmed` 的主观题；
      · `legacy_items` 旧记录（`point_source IS NULL`，来源无法追溯）——
        它们以前就在掌握率里，改造不把它们凭空抹掉，但**单独列出来**标明来源不明；
      · `ai_unconfirmed` / `unassessed` 一律不进掌握率（分子分母都不进）；
      · 选做题只统计学生作答的那一支；分支不明的**不推断**，计入 `pending_elective`。

    实现在 Python 而不是一段大 SQL：选做题的"这一点属于 Ⅰ 还是 Ⅱ"要用
    `marks.opt_of_section()`（正则），SQL 里算不出来；把口径拆两处写必然走偏。
    """
    items_bucket, legacy_bucket = {}, {}      # oid -> {"hit_n", "total_n"}
    counts = {"objective": 0, "confirmed": 0, "legacy": 0,
              "unconfirmed": 0, "unassessed": 0, "pending_elective": 0, "missing_q": 0}

    def acc(bucket, oid, hit):
        d = bucket.setdefault(oid, {"hit_n": 0, "total_n": 0})
        d["hit_n"] += 1 if hit else 0
        d["total_n"] += 1

    # ---------- 客观题：每答一次算一个样本（与历史口径完全一致） ----------
    for r in rows(
            "SELECT q.outline_id AS oid, a.hits AS hits FROM attempts a "
            "JOIN questions q ON q.id = a.question_id "
            "WHERE q.qtype = 'single' AND q.outline_id IS NOT NULL", con=con):
        acc(items_bucket, r["oid"], r["hits"] == 1)
        counts["objective"] += 1

    # ---------- 主观题：一次作答 × 一个采分点 ----------
    subs = subjective_attempts(con=con)
    pts_by_att = _attempt_points([a["id"] for a in subs], con=con)
    for a in subs:
        src = a.get("point_source") or "legacy_unknown"
        if a.get("qtype") is None:
            counts["missing_q"] += 1
            continue
        if src in ("ai_unconfirmed", "unassessed"):
            counts["unconfirmed" if src == "ai_unconfirmed" else "unassessed"] += 1
            continue
        pts = pts_by_att.get(a["id"]) or []
        if _is_elective(a.get("stem")):
            opt = a.get("selected_option")
            if opt not in ("Ⅰ", "Ⅱ"):
                # 两支持各自 30 分，混在一起算等于给没作答的那一支记 0 分样本 ——
                # 宁可标"分支待核"也不推断（设计 §2.1）。
                counts["pending_elective"] += 1
                continue
            pts = [p for p in pts if _opt_of_section(p.get("section")) == opt]
        if src in CONFIRMED_SOURCES:
            counts["confirmed"] += 1
            bucket = items_bucket
        else:
            counts["legacy"] += 1
            bucket = legacy_bucket
        for p in pts:
            if p.get("outline_id"):
                acc(bucket, p["outline_id"], bool(p.get("hit")))

    meta = {r["id"]: r for r in rows("SELECT id,path,level,name FROM outline_nodes", con=con)}

    def render(bucket):
        out = []
        for oid, got in bucket.items():
            if not got["total_n"]:
                continue
            node = meta.get(oid) or {}
            out.append({"oid": oid, "path": node.get("path"), "level": node.get("level"),
                        "name": node.get("name"), "hit_n": got["hit_n"],
                        "total_n": got["total_n"],
                        "rate": round(got["hit_n"] / got["total_n"], 3)})
        out.sort(key=lambda x: (x["rate"], -x["total_n"]))
        return out

    items = render(items_bucket)
    legacy_items = render(legacy_bucket)
    return {
        "items": items,
        "legacy_items": legacy_items,
        "summary": {
            "nodes": len(items),
            "weak_nodes": sum(1 for x in items if x["rate"] < 0.6),
            "legacy_nodes": len(legacy_items),
            **counts,
        },
    }


#: 薄弱点快照的当前版本。v0 = 改造前的平铺列表（**口径未知**）。
WEAK_SNAPSHOT_VERSION = 2


def weak_snapshot_payload(limit=12, con=None):
    """当前薄弱点快照 —— **带来源构成**（设计 §2.2 / 复审问题 6）。

    为什么必须记来源：这份快照会随报告存下来、给 AI 当依据。旧版只写
    `name/path/hit/total/rate`，看不出这些数字是"学生确认过的"还是"来源不明的旧记录"；
    而两者的可信度完全不同，AI 却会一样地拿它下结论。新版连`source_counts`一起存，
    旧快照由 `parse_weak_snapshot()` 标成"口径未知"，页面与提示词都不许把两者混为一谈。
    """
    w = _weakness(con=con)

    def pack(rows_):
        return [{"name": i["name"], "path": i["path"], "hit": i["hit_n"],
                 "total": i["total_n"], "rate": i["rate"], "oid": i.get("oid")}
                for i in rows_[:limit]]

    return {
        "version": WEAK_SNAPSHOT_VERSION,
        "source_counts": dict(w["summary"]),
        "items": pack(w["items"]),
        # 旧来源不明的另存一份：不混进掌握率，但也不丢
        "legacy_items": pack(w["legacy_items"]),
    }


def parse_weak_snapshot(value):
    """读回 `sessions.weak_snapshot`，兼容两种形态。

    返回 `{"version", "source_counts", "items", "legacy_items", "legacy_shape"}`：
      · v2 对象 → 原样读，`legacy_shape=False`；
      · v0 平铺列表（改造前写的）→ `version=0`、`source_counts=None`、
        `legacy_shape=True`，页面据此标「口径未知」；
      · 空/坏数据 → 空列表，不炸。
    """
    data = _json_or(value, None)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return {"version": data.get("version") or WEAK_SNAPSHOT_VERSION,
                "source_counts": data.get("source_counts"),
                "items": data["items"],
                "legacy_items": data.get("legacy_items") or [],
                "legacy_shape": False}
    if isinstance(data, list):
        return {"version": 0, "source_counts": None, "items": data,
                "legacy_items": [], "legacy_shape": True}
    return {"version": 0, "source_counts": None, "items": [],
            "legacy_items": [], "legacy_shape": False}


QTYPE_LABEL = {"single": "单选", "analysis": "辨析", "short": "简答", "essay": "论述"}


def _json_or(value, default):
    """容错解析库里的 JSON 文本：坏数据/空值一律给 default，不让一个字段炸掉整个接口。"""
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


MAX_SESSION_QUESTIONS = 200      # 一次练习的题单上限：311 整卷 56 道，200 足够且能挡住异常入参


def _clean_question_ids(value):
    """把前端提交的「本次试卷题目 id 列表」洗成干净的有序去重 int 列表。

    为什么要洗：这个列表直接进 JSON 列、又决定日志里显示哪些题 ——
    脏数据（字符串 id / 重复 / None / 超长）会让日志多出莫名其妙的行。
    顺序**必须保持**：它就是卷面上的出题顺序，日志按它排。
    """
    out, seen = [], set()
    for v in (value or []):
        try:
            qid = int(v)
        except (TypeError, ValueError):
            continue
        if qid <= 0 or qid in seen:
            continue
        seen.add(qid)
        out.append(qid)
        if len(out) >= MAX_SESSION_QUESTIONS:
            break
    return out


def _session_question_ids(value):
    """读回 sessions.question_ids（JSON 文本），坏数据一律当空列表。"""
    ids = _json_or(value, [])
    return _clean_question_ids(ids if isinstance(ids, list) else [])


def _chunks(seq, size):
    """把 id 列表切块：SQL 的 `IN (?)` 参数个数有上限（SQLite 默认 999），一次全塞进去会炸。"""
    seq = list(seq)
    return [seq[i:i + size] for i in range(0, len(seq), size)]


@app.get("/api/logs")
def logs(limit: int = 200):
    """练习日志：**按天分组**，每天下面挂当天练的每一组，并标出是否已生成复盘。

    为什么按天（而不是按次）：用户要的是"每天的学习记录"。一天可能练好几组
    （模拟考试 + 日常练习），更关键的是**早期有 20 条作答直接写库、没有 session**
    （见 docs/交接文档.md §4 P0）—— 按次分组它们永远显示不出来，按天才能顺带收进
    `loose_attempts`，这些数据在页面上才看得见。

    `items` 保留旧的扁平结构（按次一条），给老调用方兜底。
    """
    ensure_schema()

    sessions = rows(
        "SELECT s.*, s.id AS session_id, r.id AS report_id, r.created_at AS report_created_at, "
        "       (SELECT COUNT(*) FROM attempts a WHERE a.session_id = s.id) AS attempts_n "
        "FROM sessions s LEFT JOIN reports r ON r.session_id = s.id "
        "ORDER BY s.id DESC LIMIT ?", (limit,))
    for s in sessions:
        s["weak_snapshot"] = _json_or(s.get("weak_snapshot"), [])
        # 这一次练习发的整份卷子（含没作答的题）。老 session 没有这个字段 → 空列表，
        # 页面据此说明"这次没记下题单"，而不是假装它只有做过的那些题。
        s["question_ids"] = _session_question_ids(s.get("question_ids"))

    # 采分点命中数一律**现算**（唯一口径），不用 sessions 表里那个"发卷当刻的快照"：
    # 采分点后来被补过分值、补过点（2026-09-24 那次改造就给旧题加过点），
    # 于是同一个数字在「日志明细」里是 3/21、「练习日志列表」里却是 3/14 —— 自相矛盾。
    # 库里那两个列原样保留（`*_stored`），只是不再拿来展示。
    day_scores = _session_scores([s["id"] for s in sessions])
    for s in sessions:
        sc = day_scores.get(s["id"]) or {}
        s["point_hits_stored"] = s.get("point_hits")
        s["point_total_stored"] = s.get("point_total")
        # 一条都没判定时给 None（页面显示"尚未评定"），不是 0/0（复审问题 5）
        s["point_hits"] = sc.get("point_hits")
        s["point_total"] = sc.get("point_total")
        s["subjective_score"] = sc.get("subjective_score")
        s["subjective_full"] = sc.get("subjective_full")
        s["subjective_source"] = sc.get("subjective_source", "unscored")
        s["subjective_scored_n"] = sc.get("subjective_scored_n", 0)
        s["unscored_n"] = sc.get("unscored_n", 0)
        s["unscored_kinds"] = sc.get("unscored_kinds") or {}

    # 没挂 session 的作答：按天汇总，让它们也能出现在日志里
    loose_rows = rows(
        "SELECT a.date, COUNT(*) AS attempts_n, "
        "       SUM(CASE WHEN q.qtype = 'single' THEN 1 ELSE 0 END) AS objective_total, "
        "       SUM(CASE WHEN q.qtype = 'single' AND a.hits = 1 THEN 1 ELSE 0 END) AS objective_right, "
        "       SUM(CASE WHEN q.qtype <> 'single' OR q.qtype IS NULL THEN 1 ELSE 0 END) AS subjective_count "
        "FROM attempts a LEFT JOIN questions q ON q.id = a.question_id "
        "WHERE a.session_id IS NULL GROUP BY a.date")

    days = {}
    for s in sessions:
        days.setdefault(s["date"], _new_day(s["date"]))["sessions"].append(s)
    for l in loose_rows:
        days.setdefault(l["date"], _new_day(l["date"]))["loose"] = l

    for d in days.values():
        d["loose_attempts"] = (d["loose"] or {}).get("attempts_n") or 0
        d["sessions_n"] = len(d["sessions"])
        d["attempts_n"] = sum(s["attempts_n"] or 0 for s in d["sessions"]) + d["loose_attempts"]
        d["totals"] = _day_totals(d)
        # 「展开能看到几道题」= 这一天各次练习的题单并集，
        # 与 attempts_n（作答几道）分开 —— 页面要同时说清这两件事，
        # 否则用户会以为日志又漏题了（他看到的题数比 attempts_n 多）。
        d["questions_n"] = len({qid for s in d["sessions"]
                                for qid in (s.get("question_ids") or [])}) or d["attempts_n"]
        d["unanswered_n"] = max(0, d["questions_n"] - d["attempts_n"])

    ordered = sorted(days.values(), key=lambda x: x["date"], reverse=True)
    summary = {
        "days": len(ordered),
        "sessions": len(sessions),
        "reports": sum(1 for s in sessions if s["report_id"]),
        "pending": sum(1 for s in sessions if not s["report_id"]),
        "attempts": sum(d["attempts_n"] for d in ordered),
        # 「作答数」与「题单数」是两个口径，页面上都要说清楚：
        # 前者是真正做过的题，后者是各次练习发过的题（含没作答的）。
        "questions": sum(d["questions_n"] for d in ordered),
        "unanswered": sum(d["unanswered_n"] for d in ordered),
        "objective_total": sum(d["totals"]["objective_total"] for d in ordered),
        "objective_right": sum(d["totals"]["objective_right"] for d in ordered),
        "objective_score": sum(d["totals"]["objective_score"] for d in ordered),
    }
    return {"days": ordered, "items": sessions, "count": len(sessions), "summary": summary}


def _new_day(date):
    return {"date": date, "sessions": [], "loose": None, "loose_attempts": 0,
            "sessions_n": 0, "attempts_n": 0}


def _day_totals(day):
    """把一天里各组的分数/命中数加总。**没挂 session 的旧作答也算进来**，
    否则日志顶部那行"累计"会和下面展开看到的题对不上。"""
    t = {"objective_total": 0, "objective_right": 0, "objective_score": 0.0,
         "subjective_count": 0, "point_hits": 0, "point_total": 0, "reports": 0,
         "subjective_score": None, "subjective_full": None, "subjective_scored_n": 0,
         "unscored_kinds": {}}
    for s in day["sessions"]:
        t["objective_total"] += s.get("objective_total") or 0
        t["objective_right"] += s.get("objective_right") or 0
        t["objective_score"] += s.get("objective_score") or 0
        t["subjective_count"] += s.get("subjective_count") or 0
        t["point_hits"] += s.get("point_hits") or 0
        t["point_total"] += s.get("point_total") or 0
        if s.get("subjective_scored_n"):
            t["subjective_score"] = round((t["subjective_score"] or 0) + s["subjective_score"], 2)
            t["subjective_full"] = round((t["subjective_full"] or 0) + s["subjective_full"], 2)
            t["subjective_scored_n"] += s["subjective_scored_n"]
        for k, v in (s.get("unscored_kinds") or {}).items():
            t["unscored_kinds"][k] = t["unscored_kinds"].get(k, 0) + v
        if s.get("report_id"):
            t["reports"] += 1
    l = day.get("loose") or {}
    t["objective_total"] += l.get("objective_total") or 0
    t["objective_right"] += l.get("objective_right") or 0
    t["objective_score"] += (l.get("objective_right") or 0) * 2   # 311 单选每题 2 分
    t["subjective_count"] += l.get("subjective_count") or 0
    return t


@app.get("/api/logs/day/{date}")
def logs_day(date: str):
    """某一天的作答明细：题干 / 我的作答 / 批改情况，按「哪一组练习」分块。

    与复盘的联动就在这里：每块带 `report_id` —— 有值说明这次已生成复盘报告，
    页面给"查看复盘报告"，没值则给"生成 AI 复盘"，两个板块互相跳得通。
    """
    ensure_schema()
    return _day_detail(date)


def _attempt_detail(a, points):
    """把一条作答整理成日志明细需要的样子。

    题目可能已被删或换过库 —— 字段全部走 LEFT JOIN，取不到时 `missing=True`，
    让"题目不在题库里"在页面上**可见**，而不是静默少一行（交接文档 §5.1.2）。

    `points` 会被**逐点复制**：调用方传进来的是"按题缓存"的采分点定义，
    直接挂到行上再被后续逐点覆盖的话，改的其实是缓存本体（实测：某次作答的命中
    会漏到同一道题的另一行、甚至"没作答"的行上 —— 命中率就这么算错了）。
    """
    with _learning_connection() as con:
        pr = con.execute("SELECT * FROM pilot_runs WHERE attempt_id=?", (a.get("attempt_id"),)).fetchone()
        if pr:
            pr = dict(pr)
            frozen = pilot_mod.question(pr)
            hidden = bool(pilot_mod.blockers(con, pr))
            d = _unanswered_detail({**a, "stem": frozen["question_text"], "extra": "{}", "source": frozen["source_label"]}, [])
            d.update(attempt_id=a.get("attempt_id"), answered=True, my_answer=None if hidden else pr["answer_text"],
                     point_source="unassessed", point_source_label="试点定性记录", pilot_url="/pilot?run=" + pr["id"],
                     answer_note="试点原答与定性诊断请在试点页面查看；不计入考试分数。")
            return d
    points = [dict(p) for p in (points or [])]
    extra = _json_or(a.get("extra"), {}) or {}
    d = {
        "attempt_id": a.get("attempt_id"),
        "question_id": a["question_id"],
        "qtype": a["qtype"],
        "qtype_label": QTYPE_LABEL.get(a["qtype"], a["qtype"] or "未知"),
        "number": a["number"],
        "stem": a["stem"],
        "year": a["year"],
        "source": a["source"],
        "outline_name": a["outline_name"],
        "outline_path": a["outline_path"],
        "missing": a["qtype"] is None,
        # answered=False 表示"这一次发的卷子里有这道题，但这次没作答"。
        # 它与 missing（题目已不在题库）是两件事，页面上的文案也完全不同。
        "answered": True,
        "cause": a.get("cause"),
        "hits": a.get("hits"),
        "total": a.get("total"),
        # 评分来源与所选分支（设计 §2.2）：日志页要如实标出"这个命中是自评还是仅 AI 建议"。
        # 旧行没有这两列 → NULL，页面按"来源不明（旧记录）"显示，不猜。
        "point_source": a.get("point_source") or "legacy_unknown",
        "point_source_label": SOURCE_LABEL.get(a.get("point_source") or "legacy_unknown"),
        "selected_option": a.get("selected_option"),
        "is_right": None,
        "score": None,
        "full_score": None,
        "my_answer": None,
        "correct_answer": None,
        "options": {},
        "points": points,
        "grade": _json_or(a.get("grade"), None),
        "explain": extra.get("explain") or None,
        "reference": extra.get("answer_text") or None,
        # 由 `_attempt_detail_with_points()` 在**命中覆盖之后**填上：
        # 折合分必须按这条作答自己的命中算，早了就是在算"没有命中"的版本。
        "score_result": None,
    }
    if a["qtype"] == "single":
        d["options"] = _json_or(a.get("options"), {}) or {}
        d["my_answer"] = a.get("note")           # 客观题：选中的选项字母存在 note 里
        d["correct_answer"] = a.get("answer")
        d["is_right"] = (a.get("hits") == 1)
        d["score"] = 2 if d["is_right"] else 0
        d["full_score"] = 2
    else:
        # 满分**不再回退成 15**：题目满分只存在 `extra.full_score` 里，缺了就是"待核"。
        # 以前写 `extra.get("full_score", 15)`，于是 30 分论述题在缺字段时被当成 15 分
        # 算出一个假分数（设计 §2.1 明确禁止"凭题型一律回退 15 分"）。
        d["full_score"] = extra.get("full_score")
        d["sections"] = sections_of(extra)
        d["answer_note"] = extra.get("answer_note") or None
        d["score_stats"] = score_stats(points, d["full_score"], d.get("sections"),
                                       a.get("stem"), d["answer_note"])
        # 主观题：作答原文（早期记录为空）。**题目已不存在时退回 note** ——
        # 旧数据把客观题的选项字母存在 note 里（实测 attempts#1 note='A'），
        # 题目查不到就没法判断题型，只能靠它把"我选了什么"显示出来。
        d["my_answer"] = a.get("student_answer") or (a.get("note") if d["missing"] else None)
    return d


def _unanswered_detail(q, points):
    """把一道"发了但没作答"的题整理成明细行：题干/选项/答案照给，作答与判分留空。

    为什么复用题目行的形状（而不是另做一个精简结构）：前端只多一个 unanswered 标记，
    其余渲染路径（题干、选项、采分点、解析、参考答案）完全共用，不会两套代码走偏。
    `q["missing"]` 由调用方给定（题单里的题已被删时也是 True），不在这里重算。
    """
    extra = _json_or(q.get("extra"), {}) or {}
    points = [dict(p) for p in (points or [])]     # 同上：不把缓存本体挂到行上
    is_single = q["qtype"] == "single"
    d = {
        "attempt_id": None,
        "question_id": q["question_id"],
        "qtype": q["qtype"],
        "qtype_label": QTYPE_LABEL.get(q["qtype"], q["qtype"] or "未知"),
        "number": q.get("number"),
        "stem": q.get("stem"),
        "year": q.get("year"),
        "source": q.get("source"),
        "outline_name": q.get("outline_name"),
        "outline_path": q.get("outline_path"),
        "missing": bool(q.get("missing")),
        "answered": False,
        "cause": None,
        "hits": None,
        "total": None,
        "is_right": None,        # 没作答就不能算对/算错，页面上也不给"答对/答错"标签
        "score": None,
        "full_score": 2 if is_single else extra.get("full_score"),
        "my_answer": None,
        "correct_answer": q.get("answer") if is_single else None,
        "options": (_json_or(q.get("options"), {}) or {}) if is_single else {},
        "points": points,
        "grade": None,
        "explain": extra.get("explain") or None,
        "reference": extra.get("answer_text") or None,
    }
    if not is_single:
        d["sections"] = sections_of(extra)
        d["answer_note"] = extra.get("answer_note") or None
        d["full_score"] = extra.get("full_score")      # 同上：不回退 15
        d["score_stats"] = score_stats(points, d["full_score"], d.get("sections"),
                                       q.get("stem"), d["answer_note"])
    return d


def _unanswered_placeholder(qid):
    """题单里指向的题**已不在题库**（换过库/被删）时，仍然把那一格显示出来。

    与 `_unanswered_detail` 一样是"卷子有、库里没有"的可见化处理 ——
    否则这一次练习显示出来的题数会比当时实际发的少，用户会以为日志又漏题了。
    missing 在这里就定死为 True（题确实查不到了），这样前端会走"题目已不在题库"那套渲染。
    """
    return {
        "question_id": qid, "attempt_id": None, "date": None, "mode": None,
        "hits": None, "total": None, "cause": None, "note": None, "session_id": None,
        "student_answer": None, "grade": None,
        "qtype": None, "number": None, "stem": None, "options": None, "answer": None,
        "year": None, "source": None, "extra": None,
        "outline_name": None, "outline_path": None, "missing": True,
    }


def _points_and_hits(date, question_ids):
    """一次取回当天的采分点定义与命中情况，返回 (pts_of, hits_of)。

    - `pts_of[question_id]`：这道题的采分点（**按题取** —— 没作答的主观题在 attempts
      里没有行，只按作答取就取不到它的采分点）
    - `hits_of[attempt_id]`：这条作答答到了哪几个采分点（point_hits 是按作答关联的）

    早前这里刻意不取 `points.score`（当时全库 946 行没有一个写过它，取它只是白白
    多一个 schema 依赖）。现在分值已经是真实数据了 —— 取它，页面才能把
    「命中 3/6」折合成「7.5/15 分」。
    """
    pts_of, hits_of = {}, {}
    qids = list(question_ids)
    for chunk in _chunks(qids, 400):
        qs = ",".join("?" * len(chunk))
        for p in rows(
            "SELECT p.question_id, p.seq, p.claim, p.evidence, p.section, p.score "
            "FROM points p WHERE p.question_id IN (" + qs + ") "
            "ORDER BY p.question_id, p.seq", chunk):
            pts_of.setdefault(p["question_id"], []).append(
                {"seq": p["seq"], "claim": p["claim"], "evidence": p["evidence"],
                 "section": p["section"], "score": p["score"], "hit": False})

    # 只认**真的命中**那几行：`point_hits` 里可能存在 hit=0 的行（旧写入路径的中间状态），
    # 直接 `JOIN` 不带 `ph.hit = 1` 会把"没答到的点"也算成命中 —— 实测就是这么翻车的。
    # 这道过滤也顺带解决占位行：题单补出来的行 attempt_id=None，`a.id = NULL` 恒为假，不会误挂。
    for r in rows(
        "SELECT a.id AS attempt_id, p.seq AS seq "
        "FROM attempts a "
        "JOIN points p ON p.question_id = a.question_id "
        "JOIN point_hits ph ON ph.point_id = p.id AND ph.attempt_id = a.id "
        "WHERE a.date = ? AND ph.hit = 1", (date,)):
        hits_of.setdefault(r["attempt_id"], set()).add(r["seq"])
    return pts_of, hits_of


def _attempt_detail_with_points(a, pts_of, hits_of):
    """把一条作答整理成明细行，并把它自己的采分点命中情况覆盖上去。

    采分点定义来自 `_points_and_hits`（按题目取，含没作答的题），
    但**谁答到了哪一点只有这条作答知道** —— 所以命中情况按 attempt_id 取。

    覆盖完命中之后立刻算这条作答的折合分（`score_result`），交给页面直接显示 ——
    以前这一层只把带 `hit` 的点抛给前端，由 `qa-answer.js` 的 `sumHit()` 自己加一遍；
    后端再算一遍就是两套口径（选做题分支、任意 N 点封顶、分值待核都容易走偏）。
    """
    d = _attempt_detail(a, pts_of.get(a["question_id"], []))
    hit_seqs = hits_of.get(a["attempt_id"]) or set()
    for p in d["points"]:
        p["hit"] = p["seq"] in hit_seqs
    if a.get("qtype") not in (None, "single"):
        # 只对主观题算：客观题没有采分点，给它算一次只会得到"没有采分点"的空结论，
        # 白占字段还容易让人以为客观题也走这套折分逻辑。
        d["score_result"] = attempt_score(a, d["points"])
    return d


def _day_detail(date):
    """某一天的明细：先按「哪一次练习」分块，每块里是**这一次发的整份卷子**。

    关键点：`sessions.question_ids` 是"完整练习内容"的唯一来源 ——
    attempts 只记做过的题，只按 attempts 出题就永远看不到没作答的题
    （用户明确要求"包括没做的"）。老 session（这个字段之前建的）没有题单，
    就照旧只显示做过的题，并在返回值里标明 `has_question_set=False`，让页面说清楚。
    """
    arows = rows(
        "SELECT a.id AS attempt_id, a.question_id, a.date, a.mode, a.hits, a.total, "
        "       a.cause, a.note, a.session_id, a.student_answer, a.grade, "
        "       a.point_source, a.selected_option, "
        "       q.qtype, q.number, q.stem, q.options, q.answer, q.year, q.source, q.extra, "
        "       o.name AS outline_name, o.path AS outline_path "
        "FROM attempts a "
        "LEFT JOIN questions q ON q.id = a.question_id "
        "LEFT JOIN outline_nodes o ON o.id = q.outline_id "
        "WHERE a.date = ? "
        "ORDER BY (a.session_id IS NULL), a.session_id, a.id", (date,))

    smeta = {s["id"]: s for s in rows(
        "SELECT s.*, r.id AS report_id, r.created_at AS report_created_at "
        "FROM sessions s LEFT JOIN reports r ON r.session_id = s.id "
        "WHERE s.date = ?", (date,))}

    # ---------- 先按练习分组，并收集"这次实际作答了哪些题" ----------
    groups = []

    def group_of(sid):
        for x in groups:
            if x["session_id"] == sid:
                return x
        s = smeta.get(sid) or {}
        g = {
            "session_id": sid,
            "loose": sid is None,
            "mode": s.get("mode") or ("未归属记录" if sid is None else "未标注"),
            "note": s.get("note"),
            "created_at": s.get("created_at"),
            "report_id": s.get("report_id"),
            "report_created_at": s.get("report_created_at"),
            "question_ids": _session_question_ids(s.get("question_ids")),
            "answered_ids": [],       # 这一次真的留下了作答的题（保持顺序）
            "attempts": {},           # question_id → 作答行（含题干）
            "extra_attempts": [],     # 作答了、但不在题单里的题（老数据 / 题单外补做的）
            "questions": [],
        }
        groups.append(g)
        return g

    for a in arows:
        g = group_of(a["session_id"])
        qid = a["question_id"]
        if qid in g["attempts"]:
            # 同一次练习里同一道题出现多行（旧数据脏了）：保留第一行，其余如实收进
            # extra_attempts —— 宁可多显示一行，也不静默丢数据（§5.1.2 的同一条规矩）。
            g["extra_attempts"].append(a)
            continue
        g["attempts"][qid] = a
        g["answered_ids"].append(qid)

    # ---------- 把每一组的题目凑齐：题单顺序优先，题单外的作答补在后面 ----------
    # 采分点与命中情况一次取回（含没作答的主观题），避免每道题查一次库
    need_points = set()
    for g in groups:
        need_points.update(g["answered_ids"])
        need_points.update(g["question_ids"])
    pts_of, hits_of = _points_and_hits(date, sorted(need_points))
    qrows = _question_rows(sorted({qid for g in groups for qid in g["question_ids"]}))

    for g in groups:
        ordered = _ordered_ids(g)
        q_list, unanswered_n = [], 0
        for qid in ordered:
            a = g["attempts"].get(qid)
            if a is not None:
                q_list.append(_attempt_detail_with_points(a, pts_of, hits_of))
                continue
            q = qrows.get(qid)
            if q is None:
                q_list.append(_unanswered_detail(_unanswered_placeholder(qid), []))
            else:
                q_list.append(_unanswered_detail(q, pts_of.get(qid, [])))
            unanswered_n += 1
        for a in g["extra_attempts"]:
            q_list.append(_attempt_detail_with_points(a, pts_of, hits_of))
        g["questions"] = q_list
        g["has_question_set"] = bool(g["question_ids"])
        g["questions_n"] = len(g["question_ids"]) or len(g["answered_ids"])
        g["unanswered_n"] = unanswered_n if g["has_question_set"] else 0
        g.pop("attempts", None)
        g.pop("extra_attempts", None)
        g.pop("answered_ids", None)
        q_ids = [q["question_id"] for q in q_list]
        g["question_ids"] = q_ids

        # 小结口径**保持不变**（只统计作答过的题）：薄弱点、总分、命中率都建立在
        # 真实作答上，把没作答的题算进去会让"正确率"凭空变低、和历史数据不可比。
        # 主观题的命中数与折合分直接取 `attempt_score()` 的结果（唯一口径）：
        # 只算**已判定来源**的作答（自评确认 / 采纳 AI 建议 / 旧来源不明），
        # 仅收到 AI 建议与未评定的题既不进分子也不进分母；选做题只算作答的那一支。
        singles = [q for q in q_list if q["qtype"] == "single" and q["answered"]]
        subjs = [q for q in q_list if q["qtype"] not in (None, "single") and q["answered"]]
        right = sum(1 for q in singles if q["is_right"])
        judged = [q["score_result"] for q in subjs
                  if (q.get("score_result") or {}).get("scored")]
        point_judged = [q["score_result"]["point_score"] for q in subjs
                        if (q.get("score_result") or {}).get("point_score", {}).get("scored")]
        # 未折合的原因分类（复审问题 3）：页面靠它区分"尚未评定"与"已自评但分值待核"
        kinds = {}
        for q in subjs:
            sr = q.get("score_result") or {}
            if not sr.get("scored"):
                k = sr.get("reason_kind") or "other"
                kinds[k] = kinds.get(k, 0) + 1
        subj_scores = [x["score"] for x in judged]
        subj_fulls = [x["full"] for x in judged]
        sources = sorted({x["score_source"] for x in judged})
        g["summary"] = {
            "objective_total": len(singles), "objective_right": right,
            "objective_score": right * 2, "subjective_count": len(subjs),
            # 一条都没判定 → None（页面显示"尚未评定"），不是 0/0
            "point_hits": sum(x["n_hits"] for x in point_judged) if point_judged else None,
            "point_total": sum(x["n_points"] for x in point_judged) if point_judged else None,
            "subjective_score": round(sum(subj_scores), 2) if judged else None,
            "subjective_full": round(sum(subj_fulls), 2) if judged else None,
            "subjective_unscored_n": len(subjs) - len(judged),
            "subjective_sources": sources,
            "subjective_source": _score_source_kind(judged),
            "subjective_scored_n": len(judged),
            "unscored_kinds": kinds,
        }

    all_qs = [q for g in groups for q in g["questions"]]
    return {
        "ok": True, "date": date, "groups": groups,
        "count": len(all_qs),                       # 这一天展开后能看到的**全部**题（含没作答的）
        "attempted_n": len(arows),
        "unanswered_n": sum(g["unanswered_n"] for g in groups),
        "missing_n": sum(1 for q in all_qs if q["missing"]),
    }


def _ordered_ids(g):
    """一次练习要显示的题目 id 顺序：题单原顺序 → 题单外做过的题 → 没挂 session 的旧记录。"""
    out, seen = [], set()
    for qid in list(g["question_ids"]) + list(g["answered_ids"]):
        if qid not in seen:
            seen.add(qid)
            out.append(qid)
    for a in g["extra_attempts"]:
        if a["question_id"] not in seen:
            seen.add(a["question_id"])
            out.append(a["question_id"])
    return out


def _question_rows(qids):
    """按 id 取题目 + 考点名（题单里没作答、甚至已不在题库的题也要能取到）。"""
    out = {}
    for chunk in _chunks(qids, 400):
        qs = ",".join("?" * len(chunk))
        for r in rows(
            "SELECT q.id AS question_id, q.qtype, q.number, q.stem, q.options, q.answer, "
            "       q.year, q.source, q.extra, "
            "       o.name AS outline_name, o.path AS outline_path "
            "FROM questions q LEFT JOIN outline_nodes o ON o.id = q.outline_id "
            "WHERE q.id IN (" + qs + ")", chunk):
            out[r["question_id"]] = r
    return out


@app.get("/api/session/{session_id}/paper")
def session_paper(session_id: int):
    """把某一次练习的**整份原卷**还原出来（练习日志「继续练习」用）。

    为什么要有这个接口：`sessions.question_ids` 是"这一次发了哪些题"的唯一依据，
    但日志接口是按**日期**分组的，没法直接拿一次练习。续练要的又恰恰是"那一次的卷子"：
    没作答的题补做，已作答的带上上次的作答（可改）—— 所以按 session id 取。

    与 `/api/paper` 的分工：那个是"给我一份新卷子"，这个**不组卷、不写库**，
    纯粹把历史那一次还原出来；提交时新建一条「续练」记录（原记录一个字段都不动）。

    返回的 singles/subjectives 与 `/api/daily/generate` 的题目同形（含 extra，
    所以前端那套渲染路径不用改）；已不在题库的题只记 id，如实列在 missing_ids 里。
    """
    ensure_schema()
    srows = rows("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not srows:
        return {"ok": False, "error": f"没有 id={session_id} 的练习记录（可能已被删除）。"}
    s = srows[0]
    if s.get("mode") == "主观题试点":
        return {"ok": False, "error": "试点的原答不可覆盖，请从主观题试点页面另存订正。", "pilot_url": "/pilot"}
    qids = _session_question_ids(s.get("question_ids"))
    if not qids:
        return {"ok": False, "has_question_set": False,
                "error": "这一次练习没有记下完整题单（功能上线前的旧记录），"
                         "只能看日志里做过的题，没法续练。"}

    arows = rows(
        "SELECT a.id AS attempt_id, a.question_id, a.date, a.mode, a.hits, a.total, "
        "       a.cause, a.note, a.session_id, a.student_answer, a.grade, "
        "       q.qtype, q.number, q.stem, q.options, q.answer, q.year, q.source, q.extra, "
        "       o.name AS outline_name, o.path AS outline_path "
        "FROM attempts a "
        "LEFT JOIN questions q ON q.id = a.question_id "
        "LEFT JOIN outline_nodes o ON o.id = q.outline_id "
        "WHERE a.session_id = ? "
        "ORDER BY a.id", (session_id,))
    attempts, dup_ids = {}, []
    for a in arows:
        if a["question_id"] in attempts:
            # 同一道题在这次练习里有好几条作答（重复练 / 旧数据脏了）。计分**不受影响**
            # （`attempt_score()` 是按 attempt 逐条算的），但续练只还原第一条 ——
            # 所以这里把被略过的题号如实报出去，不静默丢（项目的老规矩：宁可见，不可藏）。
            dup_ids.append(a["question_id"])
            continue
        attempts[a["question_id"]] = a

    pts_of, hits_of = _points_and_hits(s["date"], qids)
    qrows = _question_rows(qids)

    singles, subs, missing, unanswered = [], [], [], []
    for qid in qids:
        a = attempts.get(qid)
        if a is not None:
            d = _attempt_detail_with_points(a, pts_of, hits_of)
        else:
            q = qrows.get(qid)
            if q is None:
                missing.append(qid)             # 题已不在题库：如实报出来，不静默少一格
                continue
            d = _unanswered_detail(q, pts_of.get(qid, []))
            unanswered.append(qid)
        d["id"] = d["question_id"]              # daily 页的渲染路径读 q.id
        # extra 原样带上：前端 normSingle/normSubj 会从它取解析、小问结构、评分说明、难度
        d["extra"] = (a or qrows.get(qid) or {}).get("extra")
        if d["qtype"] == "single":
            d["answer"] = d.get("correct_answer")   # 页面上仍默认 hidden，提交/点按钮才显示
        (singles if d["qtype"] == "single" else subs).append(d)

    return {
        "ok": True,
        "has_question_set": True,
        "session": {"id": s["id"], "date": s["date"], "mode": s.get("mode"),
                    "note": s.get("note"), "created_at": s.get("created_at")},
        "singles": singles,
        "subjectives": subs,
        "counts": {"single": len(singles), "subjective": len(subs),
                   "answered": len(attempts), "unanswered": len(unanswered),
                   "missing": len(missing), "total": len(qids)},
        "unanswered_ids": unanswered,
        "missing_ids": missing,
        # 同题作答多行、这里只还原了第一条的题号（计分仍按每条作答分别算）
        "duplicate_ids": sorted(set(dup_ids)),
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return page("index.html")


@app.get("/daily", response_class=HTMLResponse)
def daily_page():
    """日常练习模式：输入记录 → 确认考点 → 出题 → 批改。"""
    return page("daily.html")


@app.get("/practice", response_class=HTMLResponse)
def practice_page():
    """按主观题型练真题，答题、批改与保存复用现有链路。"""
    return page("practice.html")


@app.get("/logs", response_class=HTMLResponse)
def logs_page():
    """练习日志：每次模拟 / 日常练习的得分与薄弱点快照。"""
    return page("logs.html")


# ============================================================ 主观题专项（只抽真题，不调用出题 AI）

PRACTICE_TYPES = ("analysis", "short", "essay")


def _practice_id(value, label):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{label}须为有效的大纲节点编号")
    if not str(value).isdigit() or int(value) <= 0:
        raise ValueError(f"{label}须为有效的大纲节点编号")
    return int(value)


def _practice_pool(qtype, board_id=None, chapter_id=None):
    """筛选与题量预览共用同一题池；按父链归属，兼容题目挂在板块/节上。"""
    if qtype not in PRACTICE_TYPES:
        raise ValueError("请选择辨析、简答或分析论述题")
    board_id = _practice_id(board_id, "科目")
    chapter_id = _practice_id(chapter_id, "章节")
    nodes = rows("SELECT id,parent_id,level,name FROM outline_nodes ORDER BY id")
    by_id = {n["id"]: n for n in nodes}
    for ident, level, label in ((board_id, "board", "科目"), (chapter_id, "chapter", "章节")):
        if ident is not None and (ident not in by_id or by_id[ident]["level"] != level):
            raise ValueError(f"所选{label}不存在，请重新选择")

    def ancestors(ident):
        found, seen = {}, set()
        while ident in by_id and ident not in seen:
            seen.add(ident)
            node = by_id[ident]
            found.setdefault(node["level"], ident)
            ident = node["parent_id"]
        return found

    lineage = {n["id"]: ancestors(n["id"]) for n in nodes}
    if chapter_id and board_id and lineage[chapter_id].get("board") != board_id:
        raise ValueError("所选章节不属于当前科目，请重新选择")
    candidates = rows(
        "SELECT id,qtype,outline_id FROM v_real_questions "
        "WHERE qtype IN ('analysis','short','essay') ORDER BY id")
    type_counts = {t: sum(q["qtype"] == t for q in candidates) for t in PRACTICE_TYPES}
    typed = [q for q in candidates if q["qtype"] == qtype]
    boards, chapters = [], []
    for node in nodes:
        ident = node["id"]
        if node["level"] == "board":
            boards.append({"id": ident, "name": node["name"], "count": sum(
                lineage.get(q["outline_id"], {}).get("board") == ident for q in typed)})
        elif node["level"] == "chapter":
            parent_board = lineage[ident].get("board")
            if board_id is None or parent_board == board_id:
                chapters.append({"id": ident, "name": node["name"], "board_id": parent_board,
                                 "board_name": by_id.get(parent_board, {}).get("name", ""),
                                 "count": sum(lineage.get(q["outline_id"], {}).get("chapter") == ident
                                              for q in typed)})
    selected = [q["id"] for q in typed
                if (board_id is None or lineage.get(q["outline_id"], {}).get("board") == board_id)
                and (chapter_id is None or lineage.get(q["outline_id"], {}).get("chapter") == chapter_id)]
    return {"qtype": qtype, "filters": {"board_id": board_id, "chapter_id": chapter_id},
            "types": [{"value": t, "label": QT_LABEL[t], "count": type_counts[t]} for t in PRACTICE_TYPES],
            "boards": boards, "chapters": chapters, "available": len(selected), "ids": selected}


@app.get("/api/practice/options")
def practice_options(qtype: str = "essay", board_id: str = "", chapter_id: str = ""):
    try:
        result = _practice_pool(qtype, board_id, chapter_id)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    result.pop("ids")
    return {"ok": True, **result}


@app.post("/api/practice/generate")
def practice_generate(payload: dict):
    try:
        count = payload.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 5:
            raise ValueError("每组请选择 1～5 道题")
        pool = _practice_pool(payload.get("qtype", "essay"), payload.get("board_id"),
                              payload.get("chapter_id"))
        excluded = payload.get("exclude_ids", [])
        if excluded is None:
            excluded = []
        if not isinstance(excluded, list) or len(excluded) > 2000:
            raise ValueError("本轮已练题目清单无效，请重新开始")
        excluded = {_practice_id(value, "已练题目") for value in excluded}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    candidates = [qid for qid in pool.pop("ids") if qid not in excluded]
    chosen = random.sample(candidates, min(count, len(candidates)))
    questions = []
    if chosen:
        placeholders = ",".join("?" for _ in chosen)
        questions = rows(
            "SELECT q.id,q.year,q.qtype,q.number,q.stem,q.extra,q.source,q.outline_id,"
            "o.name AS outline_name,o.path AS outline_path FROM v_real_questions q "
            "LEFT JOIN outline_nodes o ON o.id=q.outline_id "
            f"WHERE q.id IN ({placeholders}) AND q.qtype=?", chosen + [pool["qtype"]])
        order = {qid: i for i, qid in enumerate(chosen)}
        questions.sort(key=lambda q: order[q["id"]])
        for q in questions:
            q["points"] = points_of_question(q["id"])
        attach_subjective_meta(questions)
    notice = ""
    if len(questions) < count:
        notice = (f"当前条件下本轮还可抽取 {len(questions)} 道{QT_LABEL[pool['qtype']]}真题，"
                  f"已提供 {len(questions)} 道。可调整科目/章节，或重新开始本轮。")
    return {"ok": True, "qtype": pool["qtype"], "filters": pool["filters"],
            "available": pool["available"], "requested": count, "actual": len(questions),
            "remaining": max(0, len(candidates) - len(questions)),
            "shortage": len(questions) < count, "notice": notice, "questions": questions}


# ============================================================ 日常练习模式（AI）

# AI 模块延迟导入：ai.py 在导入时会读取 config.json，
# 放在模块顶部会让「没配 key 也想看题库」的场景直接起不来服务。
import ai as ai_mod  # noqa: E402


@app.post("/api/ai/test")
def ai_test():
    """连通性自检，前端可点按钮调用。"""
    try:
        text, usage = ai_mod.ping()
        return {"ok": True, "reply": text, "usage": usage,
                "usage_info": ai_mod.usage_report(usage)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================ AI 模型与参数设置
#
# 这一节的三个设计约束（都来自用户拍板的四条选择）：
#
# ① **配置写到 config.local.json 覆盖层，config.json 保持只读**。config.json 里是
#    明文密钥，接口进程不该去改写它；覆盖层随时可删、删了就回默认。
# ② **本机限定**：写接口只接受来自 127.0.0.1/localhost 的请求。服务默认就绑回环，
#    但"绑回环"和"接口自己判断"是两回事 —— 后者才是将来换绑定时的那道闸。
# ③ **响应里绝不出现 api_key**：只回"配没配、从哪来、尾 4 位"。整份配置也不回。

def _is_local(request=None):
    """请求是不是来自本机。取不到客户端信息时**按本机处理**（本地工具，别自锁）。"""
    try:
        client = getattr(request, "client", None)
        host = getattr(client, "host", None)
    except Exception:
        host = None
    return host in (None, "", "127.0.0.1", "::1", "localhost")


def _local_guard(request):
    if not _is_local(request):
        return {"ok": False, "error": "拒绝：该接口只允许本机访问（127.0.0.1）。"}
    return None


# 可写字段白名单：(类型, 最小值, 最大值)。不在表里的键**一律拒绝**，
# 而不是"不认识就忽略" —— 静默忽略会让用户以为设置生效了。
_API_FIELDS = {
    "provider": ("str", None, None),
    "base_url": ("url", None, None),
    "model": ("str", None, None),
    "api_key": ("key", None, None),
    "timeout_seconds": ("int", 5, 3600),
    "temperature": ("float", 0.0, 2.0),
    "thinking": ("enum", ["enabled", "disabled"], False),
    "thinking_effort": ("enum", ["low", "high", "max"], True),
    "peak_hours": ("hours", None, None),
    "offpeak_dates": ("dates", None, None),
    "peak_dates": ("dates", None, None),
}
_PRICING_FIELDS = {"currency", "input_per_million", "output_per_million",
                   "input_cached_per_million", "effective_date"}


class _FieldError(ValueError):
    """字段校验失败，消息直接给用户看。"""


def _scenario_fields(section):
    """某个场景段允许写的键 + 它们的范围。**从 SCENARIOS 推**，不另抄一份。"""
    allowed = {}
    for spec in ai_mod.SCENARIOS.values():
        if spec.get("section") != section:
            continue
        for ckey, _label, _default, _hint in spec["keys"]:
            allowed[ckey] = (16, 500000)          # 预算/超时：16 起步，够任何一次调用
        allowed["thinking"] = None                 # 枚举，单独判
        allowed["thinking_effort"] = None
    return allowed


def _coerce(section, key, value):
    """按白名单校验并归一化一个值。**坏值一律抛错，不猜、不默认。**

    为什么"不认识就拒绝"而不是忽略：静默忽略会让用户以为改成了 —— 设置页尤其不能这样。
    """
    if section != "api":
        allowed = _scenario_fields(section)
        if key not in allowed:
            raise _FieldError(f"不认识的设置项：{section}.{key}"
                              + (f"（可改：{'、'.join(sorted(allowed))}）" if allowed else ""))
        if key == "thinking":
            if str(value).lower() not in ("enabled", "disabled"):
                raise _FieldError(f"{section}.thinking 只能是 enabled / disabled")
            return str(value).lower()
        if key == "thinking_effort":
            if value in (None, ""):
                return None
            if str(value) not in ("low", "high", "max"):
                raise _FieldError(f"{section}.thinking_effort 只能是 low / high / max")
            return str(value)
        if key == "retry_reconcile":
            # 布尔型的场景参数：写成 on/off（页面渲染成下拉框，不是数字输入框）
            if str(value).lower() not in ("on", "off"):
                raise _FieldError(f"{section}.retry_reconcile 只能是 on / off")
            return str(value).lower()
        return _int_field(f"{section}.{key}", value, *allowed[key])

    if key not in _API_FIELDS:
        raise _FieldError(f"不认识的设置项：api.{key}"
                          f"（可改的字段：{'、'.join(sorted(_API_FIELDS))}）")
    kind, spec, allow_none = _API_FIELDS[key]
    if value is None:
        if allow_none:                 # 显式 null = 清除该键，回落默认
            return None
        raise _FieldError(f"api.{key} 不能清空")
    if kind == "int":
        return _int_field(f"api.{key}", value, spec, 3600)
    if kind == "float":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise _FieldError(f"api.{key} 必须是数字") from None
        if not (spec <= v <= 2.0):
            raise _FieldError(f"api.{key} 必须在 {spec}–2.0 之间")
        return v
    if kind == "enum":
        if str(value) not in [str(x) for x in spec]:
            raise _FieldError(f"api.{key} 只能是 {' / '.join(str(x) for x in spec)}")
        return str(value)
    if kind == "str":
        v = str(value).strip()
        if not v:
            raise _FieldError(f"api.{key} 不能为空")
        return v
    if kind == "url":
        v = str(value).strip().rstrip("/")
        if not (v.startswith("http://") or v.startswith("https://")):
            raise _FieldError("api.base_url 必须以 http:// 或 https:// 开头")
        return v
    if kind == "key":
        v = str(value)
        if len(v) < 8:
            raise _FieldError("api.api_key 看起来太短（少于 8 个字符），没有保存")
        return v
    if kind == "hours":
        return _hours_field(value)
    if kind == "dates":
        return _dates_field(key, value)
    raise _FieldError(f"不认识的设置项：api.{key}")


def _int_field(label, value, lo, hi):
    if isinstance(value, bool) or value is None:
        raise _FieldError(f"{label} 必须是整数")
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise _FieldError(f"{label} 必须是整数") from None
    if isinstance(value, float) and value != v:
        raise _FieldError(f"{label} 必须是整数")
    if lo is not None and not (lo <= v <= hi):
        raise _FieldError(f"{label} 必须在 {lo}–{hi} 之间")
    return v


def _hours_field(value):
    """高峰时段：`[[9,12],[14,18]]` 这种整点区间。"""
    if value in (None, []):
        return None
    out = []
    for pair in value:
        try:
            a, b = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            raise _FieldError("api.peak_hours 要写成 [[9,12],[14,18]] 这样的整点区间") from None
        if not (0 <= a < b <= 24):
            raise _FieldError(f"api.peak_hours 的区间不合法：{a}–{b}")
        out.append([a, b])
    return out


def _dates_field(key, value):
    if value in (None, []):
        return None
    out = []
    for v in value:
        try:
            datetime.strptime(str(v).strip(), "%Y-%m-%d")
        except (TypeError, ValueError):
            raise _FieldError(f"api.{key} 的日期要写成 YYYY-MM-DD，收到的是 {v!r}") from None
        out.append(str(v).strip())
    return sorted(set(out))


def _to_float(value, label):
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise _FieldError(f"{label} 必须是数字") from None
    if v < 0:
        raise _FieldError(f"{label} 不能是负数")
    return v


def _stale_pricing(models_rates, active):
    """显式配置的价目与"官方表里当前模型的价格"不一致时，返回对比文本。

    为什么必须提示：`api.pricing` 是**用户显式覆盖**，它对所有模型生效 ——
    于是"切到 Pro 却仍按 Flash 的价格算"这种错**不会有任何报错**，只是金额悄悄错 4.5 倍。
    这正是本项目最忌讳的那类静默错误，所以这里主动比对并说出来。
    """
    if not models_rates or not active:
        return ""
    for period in ("peak", "offpeak"):
        row = models_rates.get(period) or {}
        if active.get("period") == period:
            official = row
            break
    else:
        return ""
    try:
        diff = []
        for label, key in (("输入（未命中）", "input_miss_per_million"),
                           ("输出", "output_per_million")):
            if official.get(key) is None or active.get(key) is None:
                continue
            if abs(float(official[key]) - float(active[key])) > 1e-9:
                diff.append(f"{label}：配置 {active[key]:g} / 官方表 {official[key]:g}")
    except (TypeError, ValueError):
        return ""
    if not diff:
        return ""
    return "；".join(diff)


def _settings_payload():
    """设置页需要的全部事实（**不含密钥、不含整份配置**）。"""
    api = ai_mod.api_cfg()
    catalog = providers_mod.get_catalog(api)
    model = api.get("model")
    mi = ai_mod.model_info(model)
    rates = ai_mod.pricing(api=api, model=model)
    pnow = pricing_mod.period_info(api_cfg=api)
    warnings = []

    if not providers_mod.describe_auth(api, local_has_key=ai_mod.has_local_key())["configured"]:
        warnings.append("没有可用密钥：出题、批改、复盘都会失败（题库相关功能不受影响）。"
                        "在 config.json 或下面的输入框里填一个。")
    if not mi.get("known"):
        warnings.append(f"模型 `{model}` 不在模型目录里：能力未知，"
                        "金额也只会在你显式配置价目后才显示。")
    if rates is None:
        warnings.append(f"模型 `{model}` 没有可用价目：**用量照常显示，金额不显示**。"
                        "（这是设计默认 —— 宁可不说，也不报一个看起来精确的错数字。）")
    elif rates.get("source") == "config":
        # 显式覆盖优先于官方表，但**对所有模型生效**：切模型后忘了同步就会静默算错。
        stale = _stale_pricing(pricing_mod.rate_rows(model), rates)
        if stale:
            warnings.append(f"金额用的是 `api.pricing` 里**你显式配置**的价目，"
                            f"它与当前模型的官方价不一致 —— {stale}"
                            f"（每百万 tokens，当前为"
                            f"{'高峰' if rates.get('period') == 'peak' else '空闲'}时段）。"
                            "换模型后请同步价目，或清掉覆盖以改用官方表。")
    if str(api.get("thinking")) == "enabled":
        warnings.append("全局默认开着思考模式：**思考按输出计费**（实测同问题 1 vs 44 tokens），"
                        "且思考模式下 temperature 不生效。批改本来就会单独开思考，"
                        "全局开着主要影响解析与复盘。")
    if catalog.get("source") != "live":
        warnings.append(f"模型目录来自 {catalog.get('source')}（{catalog.get('reason') or '未联网查询'}）："
                        "点「刷新模型目录」可以问一次服务端。")

    return {
        "ok": True,
        "config": {
            "provider": api.get("provider"), "base_url": api.get("base_url"),
            "model": model, "temperature": api.get("temperature"),
            "timeout_seconds": api.get("timeout_seconds"),
            "thinking": api.get("thinking"), "thinking_effort": api.get("thinking_effort"),
            "peak_hours": api.get("peak_hours") or list(pricing_mod.DEFAULT_PEAK_HOURS),
            "offpeak_dates": api.get("offpeak_dates") or [],
            "peak_dates": api.get("peak_dates") or [],
            "pricing": api.get("pricing") or None,
        },
        "auth": providers_mod.describe_auth(api, local_has_key=ai_mod.has_local_key()),
        "model": {k: mi.get(k) for k in ("id", "name", "known", "vision", "reasoning",
                                         "reasoning_levels", "reasoning_default",
                                         "context_window", "max_output_tokens", "input",
                                         "catalog_source", "reason")},
        "models": catalog.get("models") or [],
        "catalog": {"source": catalog.get("source"), "reason": catalog.get("reason"),
                    "fetched_at": catalog.get("fetched_at"),
                    "provider": catalog.get("provider"),
                    "verified_at": pricing_mod.TABLE_VERIFIED_AT},
        "pricing": {
            "active": rates and {k: rates[k] for k in
                                 ("period", "source", "currency", "input_miss_per_million",
                                  "input_cached_per_million", "output_per_million",
                                  "effective_date", "note")},
            "table": pricing_mod.rate_rows(model),
            "table_verified_at": pricing_mod.TABLE_VERIFIED_AT,
            "table_source": pricing_mod.TABLE_SOURCE,
            "peak_hours": list(pricing_mod.DEFAULT_PEAK_HOURS),
            "period_now": pnow.get("period"),
            "period_note": pnow.get("note", ""),
            "period_source": pnow.get("source"),
            "beijing_time": pnow.get("beijing_time"),
            "overridden": bool(api.get("pricing")),
        },
        "thinking": ai_mod.resolve_thinking(scenario="grade"),
        "scenarios": ai_mod.scenario_list(),
        "overlay": {
            "path": str(ai_mod.local_cfg_path()),
            "exists": ai_mod.local_cfg_path().exists(),
            "keys": sorted(k for k in (ai_mod.local_override().get("api") or {})
                           if not k.startswith("_")),
        },
        "warnings": warnings,
    }


@app.get("/api/ai/settings")
def ai_settings_get():
    """当前 AI 模型与参数：配置（脱敏）+ 模型目录 + 价目 + 场景预算。"""
    try:
        return _settings_payload()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: "
                                      f"{providers_mod.redact(e, ai_mod.api_cfg().get('api_key'))}"}


@app.put("/api/ai/settings")
def ai_settings_put(payload: dict, request: Request):
    """改模型与参数：校验 → 写 config.local.json 覆盖层 → 立即生效（不用重启）。

    请求体（都可选，只写你给的键）：
        {"api": {"model": "...", "temperature": 0.3, "thinking": "disabled", ...},
         "scenarios": {"generate": {"timeout_seconds": 900}},
         "pricing": {"input_per_million": 1.0, "output_per_million": 4.0},
         "clear_pricing": true,
         "clear_api_key": true,
         "confirm_model": "deepseek-v4-pro"}

    `confirm_model` 用于"这个模型不在目录里"的场景：改成一个没编目的模型名是有风险的
    （能力未知、价目未知），所以要么它本来就在目录里，要么调用方显式确认一次。
    """
    blocked = _local_guard(request)
    if blocked:
        return blocked
    if not isinstance(payload, dict):
        return {"ok": False, "error": "请求体必须是 JSON 对象"}

    api = ai_mod.api_cfg()
    warnings, cleared = [], []
    try:
        patch, sections = {}, {}

        api_in = payload.get("api") or {}
        if not isinstance(api_in, dict):
            raise _FieldError("api 必须是对象")
        for k in api_in:
            if k not in _API_FIELDS:
                raise _FieldError(f"不认识的设置项：api.{k}（可改的字段："
                                  f"{'、'.join(sorted(_API_FIELDS))}）")
        for k, v in api_in.items():
            patch[k] = _coerce("api", k, v)

        # 价格：合并式更新（只给一个数不会把另一个数抹掉），要整段删就 clear_pricing
        pricing_in = payload.get("pricing")
        if pricing_in is not None:
            if not isinstance(pricing_in, dict):
                raise _FieldError("pricing 必须是对象")
            bad = set(pricing_in) - _PRICING_FIELDS
            if bad:
                raise _FieldError(f"价目里不认识的字段：{'、'.join(sorted(bad))}")
            merged = dict(api.get("pricing") or {})
            for k, v in pricing_in.items():
                if k == "currency":
                    merged["currency"] = str(v)
                elif k == "effective_date":
                    merged["effective_date"] = str(v)
                else:
                    got = _to_float(v, f"pricing.{k}")
                    if got is None:
                        merged.pop(k, None)
                    else:
                        merged[k] = got
            if "input_per_million" not in merged or "output_per_million" not in merged:
                raise _FieldError("价目至少要同时给「每百万输入」与「每百万输出」两个单价，"
                                  "只给一个会让金额算错")
            patch["pricing"] = merged
        if payload.get("clear_pricing"):
            patch["pricing"] = None
            cleared.append("api.pricing")

        if payload.get("clear_api_key"):
            if not ai_mod.has_local_key():
                return {"ok": False, "error": "密钥不在覆盖层里（它来自 config.json 或环境变量），"
                                              "这里不能清除它 —— 请直接改那个来源。"}
            patch["api_key"] = None
            cleared.append("api.api_key（覆盖层里的那把已清除，回落到 config.json/环境变量）")

        sec_in = payload.get("scenarios") or {}
        if not isinstance(sec_in, dict):
            raise _FieldError("scenarios 必须是对象")
        known_sections = {s["section"] for s in ai_mod.SCENARIOS.values() if s.get("section")}
        for sec, kv in sec_in.items():
            if sec not in known_sections:
                raise _FieldError(f"不认识的场景段：{sec}")
            if not isinstance(kv, dict):
                raise _FieldError(f"scenarios.{sec} 必须是对象")
            sections[sec] = {k: _coerce(sec, k, v) for k, v in kv.items()}

        # 换模型的安全性检查：不在目录里、或不支持图像 → 说清楚，必要时要求显式确认
        new_model = patch.get("model")
        if new_model and new_model != api.get("model"):
            catalog = providers_mod.get_catalog(api)
            found = providers_mod.find_model(new_model, catalog)
            if not found and payload.get("confirm_model") != new_model:
                return {"ok": False,
                        "error": f"模型 `{new_model}` 不在模型目录里（当前目录来自 "
                                 f"{catalog.get('source')}）。它可能存在但能力未知，"
                                 f"金额也不会显示。确认要用它就把 confirm_model 一并传成 "
                                 f"\"{new_model}\"。",
                        "models": [m.get("id") for m in catalog.get("models") or []]}
            if found and found.get("vision") is False:
                warnings.append(f"`{new_model}` **不支持图像输入**：手写照片批改会失败，"
                                "纯文字的批改与出题不受影响。")
            if found and found.get("reasoning") and str(
                    patch.get("thinking", api.get("thinking"))) == "enabled":
                warnings.append(f"`{new_model}` 支持思考模式，当前默认开着："
                                "思考按输出计费，出题/批改的 token 预算要留够。")
    except _FieldError as e:
        return {"ok": False, "error": str(e)}

    if not patch and not sections and not cleared:
        return {"ok": False, "error": "没有要改的内容"}

    ok, msg, written = ai_mod.save_local_settings(
        api_patch=patch, sections=sections,
        note=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if not ok:
        return {"ok": False, "error": msg}

    out = _settings_payload()
    out["written"] = written + cleared
    out["message"] = ("已保存到覆盖层，**下一次调用即生效**（不需要重启服务）。"
                      if written else "已保存。")
    out["warnings"] = warnings + (out.get("warnings") or [])
    return out


@app.post("/api/ai/settings/refresh")
def ai_settings_refresh(request: Request):
    """问一次服务端 `/models`，刷新模型目录（并报出与上一次相比的增删改）。"""
    blocked = _local_guard(request)
    if blocked:
        return blocked
    api = ai_mod.api_cfg()
    if not api.get("api_key"):
        return {"ok": False, "error": "没有密钥，无法查询服务端的模型清单"}
    try:
        catalog, diff = providers_mod.refresh(api, force=True)
    except Exception as e:
        return {"ok": False,
                "error": f"{type(e).__name__}: {providers_mod.redact(e, api.get('api_key'))}"}
    note = ""
    if catalog.get("source") != "live":
        note = f"没能查到服务端（{catalog.get('reason')}），目录仍是 {catalog.get('source')}。"
    elif not any(diff.values()):
        note = "与服务端一致，模型清单没有变化。"
    else:
        parts = []
        if diff.get("added"):
            parts.append("新增 " + "、".join(diff["added"]))
        if diff.get("removed"):
            parts.append("消失 " + "、".join(diff["removed"]))
        if diff.get("changed"):
            parts.append("能力有变化 " + "、".join(diff["changed"]))
        note = "；".join(parts) + "。"
    return {"ok": True, "catalog": catalog, "diff": diff, "note": note,
            "settings": _settings_payload()}


@app.post("/api/ai/settings/test")
def ai_settings_test(request: Request):
    """连接自检：用**当前生效的配置**真发一次最小请求（会产生极小的费用）。"""
    blocked = _local_guard(request)
    if blocked:
        return blocked
    api = ai_mod.api_cfg()
    trace = {}
    try:
        text, usage = ai_mod.chat([{"role": "user", "content": "只回答两个字：连通"}],
                                  max_tokens=32, scenario="parse", trace=trace)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: "
                                      f"{providers_mod.redact(e, api.get('api_key'))}",
                "trace": trace or None}
    return {"ok": True, "reply": (text or "").strip()[:200], "usage": usage,
            "usage_info": ai_mod.usage_report(usage, api=api, model=trace.get("model")),
            "trace": trace}


@app.get("/api/ai/scenarios")
def ai_scenarios():
    """可试跑的 AI 场景清单（单一事实来源：`ai.SCENARIOS`）。"""
    return {"ok": True, "scenarios": ai_mod.scenario_list(),
            "scenario_prompts": {k: v for k, v in ai_mod.SCENARIO_PROMPTS.items()}}


@app.post("/api/ai/settings/run")
def ai_settings_run(payload: dict, request: Request):
    """按场景**极短地**真跑一次，用来验证"设置到底生效没有"。

    为什么必须真跑而不是只回显配置：配置写进去了不等于**有效**（官方明确说
    "思考模式下 temperature 不生效"、"非法 effort 静默忽略"）。所以这里把实际发出去的
    请求参数（`trace`）与真实用量一并回给页面，让用户看到的就是发生的事。
    """
    blocked = _local_guard(request)
    if blocked:
        return blocked
    payload = payload or {}
    ok, result = ai_mod.run_scenario(payload.get("scenario"),
                                     thinking=payload.get("thinking"),
                                     effort=payload.get("effort"))
    result["ok"] = ok
    if not ok:
        result["usage_info"] = None
    return result



def _match_ids(data):
    """从解析结果里取考点 id，**容错**：AI 可能把 id 返回成字符串 "304"。

    为什么必须容错：以前只接受 `isinstance(x, int)`，若模型回了 `"304"` 就会被静默丢掉，
    最后报「请先写今天学了什么」——用户明明写了，却被说没写（这个 bug 真出现过）。
    """
    out = []
    for m in (data or {}).get("matches", []) or []:
        v = m.get("id")
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


@app.post("/api/daily/parse")
def daily_parse(payload: dict):
    """① 自然语言学习记录 → 候选考点（供你增删确认）。"""
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "记录为空"}
    try:
        data, usage = ai_mod.parse_study_log(text)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 补齐每个候选节点的路径与下属考点数，便于前端展示
    ids = _match_ids(data)
    info = {}
    if ids:
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        qs = ",".join("?" * len(ids))
        for r in con.execute(f"SELECT id,path,level FROM outline_nodes WHERE id IN ({qs})", ids):
            info[r["id"]] = dict(r)
        con.close()
    for m in data.get("matches", []):
        m["path"] = info.get(m["id"], {}).get("path", "")
    return {
        "ok": True,
        "matches": data.get("matches", []),
        "unmatched": data.get("unmatched", []),
        "usage": usage,
        "usage_info": ai_mod.usage_report(usage),
    }


@app.get("/api/daily/gen-options")
def gen_options():
    """出题设置的可选项（难度五档 + 题量档位 + 默认值），供页面渲染设置窗口。

    为什么走接口、而不是把说明写死在 HTML 里：**单一事实来源**。
    档位描述就是提示词里实际用的那一套（`ai.DIFFICULTY_LEVELS`）；
    题量档位就是 `GROUPS`。在页面上再复述一份，迟早会和后端对不上 ——
    那时用户看到的档位说明就是假的。
    """
    return {
        "ok": True,
        "levels": {str(k): v for k, v in ai_mod.DIFFICULTY_LEVELS.items()},
        "default_difficulty": ai_mod.DEFAULT_DIFFICULTY,
        "default_thinking": True,
        "groups": [
            {"key": k, "name": v["name"], "single": v["single"],
             "subjective": v["subjective"], "minutes": v["minutes"], "mix": v["mix"],
             # **点击前**就要说清各档的题量，而且两种组卷方式分开写（设计 §4）：
             # 真题优先是"目标 + 可能补缺"，全 AI 原创是"固定"。共用一个含糊标签，
             # 用户就没法在花钱之前知道会出几道题。
             "label_real": (f"目标 {v['single']} 单选＋{v['subjective']} 主观"
                            f"（{'/'.join(QT_LABEL[t] for t, n in v['mix'].items() if n for _ in range(n))}），"
                            f"真题不足时会用 AI 补缺，可能多 1 道主观"),
             "label_ai": (f"固定 {v['single']} 单选＋"
                          f"{sum(_ai_set_mix().values())} 主观"
                          f"（{'、'.join(f'{QT_LABEL[t]}{n}' for t, n in _ai_set_mix().items())}），"
                          f"全部 AI 原创、不含真题"),
             "mix_label": "、".join(f"{QT_LABEL[t]}{n}" for t, n in v["mix"].items() if n)}
            for k, v in GROUPS.items()
        ],
        "default_group": DEFAULT_GROUP,
        # 模拟考档：结构与时长**原样保留**（设计 §4），但同样在这里给出唯一事实来源
        "mock_exam": {
            "label": (f"{MOCK_EXAM['single']} 单选＋{MOCK_EXAM['analysis']} 辨析"
                      f"＋{MOCK_EXAM['short']} 简答＋{MOCK_EXAM['essay']} 分析论述，"
                      f"{MOCK_EXAM['minutes']} 分钟"),
            **MOCK_EXAM,
        },
        "estimate_basis": "预计用时按模拟考节奏折算：单选 1.2 分钟、辨析 8、简答 10、分析论述 18",
    }


@app.post("/api/daily/generate")
def daily_generate(payload: dict):
    """按确认后的考点出题。

    `mode`：
      - `"real"`（默认）**真题优先 + AI 补缺** —— 抽真题能保证考的就是统考原题的路子；
      - `"ai"`   **全部 AI 原创、刻意不用真题** —— 用来练真题没覆盖到的角度（转给 ai-set）。

    为什么把两条路并到一个入口：页面上的「选组卷方式」是一步动作，
    分成两个接口会让前端自己判断该调哪个（判断写错就是静默走错路），
    而且考点确认那一页拿到的参数两边都一样，没必要各传一遍。

    `group`：题量档位（light/standard/strong），见 `GROUPS`；
    也兼容旧的显式 `n_single`/`n_analysis`/`n_short`/`n_essay`。
    difficulty / thinking 作用于**AI 出的那部分**（真题抽够时用不到）。
    """
    ids = payload.get("point_ids") or []
    cfg = resolve_group(payload)
    n_single, n_analysis, n_short, n_essay = (cfg["n_single"], cfg["n_analysis"],
                                              cfg["n_short"], cfg["n_essay"])
    difficulty = ai_mod.normalize_difficulty(payload.get("difficulty"))
    thinking = bool(payload.get("thinking", True))
    mode = str(payload.get("mode") or "real").strip().lower()
    if mode == "ai":
        # 全 AI 组：主题由学习记录 + 已确认的考点一起决定。
        # 主观题固定 3 道（见 AI_SET_MIX 的说明），只有单选数跟档位走。
        return daily_ai_set({**payload, "point_ids": ids, "group": cfg.get("group"),
                             "n_single": n_single,
                             "n_analysis": _ai_set_mix()["analysis"],
                             "n_short": _ai_set_mix()["short"],
                             "n_essay": _ai_set_mix()["essay"]})
    if not ids:
        return {"ok": False, "error": "未指定考点"}

    # 考点详情
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    qs = ",".join("?" * len(ids))
    points = [dict(r) for r in con.execute(
        f"SELECT id,name,path FROM outline_nodes WHERE id IN ({qs})", ids)]
    con.close()

    # 主观题按档位要几道就抽几道（以前写死 3，加强档要 4 道时会被悄悄截掉一道）
    n_subj_want = n_analysis + n_short + n_essay
    real = ai_mod.real_questions(ids, limit_single=n_single, limit_subjective=n_subj_want)
    # 真题主观题补上采分点与分值形态（AI 生成的那部分由 `_save_ai_questions()` 落库时已带全）
    attach_subjective_meta(real.get("subjective"))
    used_real = len(real["single"]) + len(real["subjective"])

    need_single = max(0, n_single - len(real["single"]))
    need_subj = max(0, n_subj_want - len(real["subjective"]))

    generated, usage, gen_audit = {"single": [], "subjective": []}, None, None
    if need_single or need_subj:
        # 按缺口比例分配主观题类型
        try:
            generated, usage, gen_audit = ai_mod.generate_questions(
                points,
                n_single=need_single,
                n_analysis=max(0, n_analysis - sum(1 for s in real["subjective"] if s["qtype"] == "analysis")),
                n_short=max(0, n_short - sum(1 for s in real["subjective"] if s["qtype"] == "short")),
                n_essay=max(0, n_essay - sum(1 for s in real["subjective"] if s["qtype"] == "essay")),
                difficulty=difficulty,
                thinking=thinking,
            )
        except Exception as e:
            # 真题不够、又没配好 AI 时会走到这里（刚 clone、还没填 key 就是这种状态）。
            # 这里以前没有兜底，ConfigMissing 会直接冒泡成 HTTP 500，
            # 前端拿不到 error 字段，只能显示「失败: undefined」。
            return {"ok": False,
                    "error": f"这几个考点的真题不够，需要用 AI 补题，但 AI 现在不可用："
                             f"{type(e).__name__}: {e}",
                    "real_count": used_real}
        # 生成的题必须入库：否则没有 id，无法批改、无法记录成绩。
        # 走**同一个入库函数** —— 这里以前少写了 points.score/section 与 extra.sections/评分说明，
        # 于是这些 AI 题"刚出出来分值齐全、一刷新就变分值待核"（用户截图那道篮球队题）。
        _save_ai_questions(generated, strict_points=False,
                           difficulty=difficulty, thinking=thinking)
    return {
        "ok": True,
        "mode": "real",
        "group": cfg,
        "real_count": used_real,
        "generated_count": len(generated.get("single", [])) + len(generated.get("subjective", [])),
        # 题量透明（设计 §4）：实际各题型数量 / 真题·AI 构成 / 预计用时 / 差异原因
        "composition": paper_composition(
            real, generated, n_single,
            {"analysis": n_analysis, "short": n_short, "essay": n_essay}),
        "paper": {"real": real, "generated": generated},
        "usage": usage,
        # 回显本次出题设置与程序层校验结果（页面用它显示"难度是否达标"）
        "gen_settings": {"difficulty": difficulty,
                         "difficulty_name": ai_mod.DIFFICULTY_LEVELS[difficulty]["name"],
                         "thinking": thinking},
        "gen_audit": gen_audit,
    }


def _num(v):
    """AI 给的分值：只认真数字（bool 不算），其它一律 None。

    为什么单独判：`isinstance(True, int)` 在 Python 里是 True，AI 偶尔给 `"score": true`
    或字符串 "3.5"，直接 float() 会抛，直接比较又会把布尔当 1 分算。
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _ai_marks_trusted(points, full_score, sections):
    """AI 给的「逐点分值 + 小问结构」能不能信 → (score_ok, section_ok)。

    **不过就留空不写**（与 `split_points.py` 同一口径，见 README 的三条硬校验）：
    页面显示「分值待核」远好过显示一个假分数 —— 折合分会进复盘与薄弱点统计，
    一个假分值会把"我这题得了 7.5 分"这种结论算错，而且**看起来完全正常**。

    判据与页面/数据体检**同源**（都走 `marks.shape_ok`），外加写库这一关特有的完整性要求：

    | 检查 | 为什么 |
    | --- | --- |
    | 每个点都给了分值且 > 0 | `shape_ok` 只看**已写**的分值（没写的点它不参与判断）—— 那对读路径对，对"要写进库"不够 |
    | **满分必须存在且为正** | 原实现写 `if score_ok and full:`，满分缺失时**跳过**"和 == 满分"校验，于是一个没有满分的题也能放行（复审指出的缺口） |
    | 逐点/逐小问形态自洽 | 交给 `marks.shape_ok`：**接受「答出任意 N 点即可」**（原来一律按"和 == 满分"判，把官方正常形态拒了），同时逐小问核 |
    | 每个点都要能对上一个小问 | AI 偶尔写 "（1）" 这类自由文本，分组分就算不出来 |

    `section_ok` 单列：它管的是"点能不能归到某个小问"，与分值形态是两件事。
    """
    pts = [p for p in (points or []) if isinstance(p, dict)]
    secs = [s for s in (sections or []) if isinstance(s, dict)]
    if not pts:
        return False, False

    sec_keys = [str(s.get("key")) for s in secs]
    sec_of = [(None if p.get("section") in (None, "") else str(p.get("section"))) for p in pts]
    section_ok = all(v is None or v in sec_keys for v in sec_of)
    if sec_keys and any(v is None for v in sec_of):
        section_ok = False          # 明明分了小问，却有点没归属 → 结构不可信

    scores = [_num(p.get("score")) for p in pts]
    if not all(s is not None and s > 0 for s in scores):
        return False, section_ok    # 有点没分值/非正 → 整题分值待核

    full = _num(full_score)
    if full is None or full <= 0:
        return False, section_ok    # **满分缺失不放过**（原实现的漏洞）

    ok, _why = marks.shape_ok(
        [{"section": v, "score": s} for v, s in zip(sec_of, scores)], full, None, secs)
    return bool(ok), section_ok


def _ai_question_row(q, qid, extra_json, is_single):
    """把 AI 返回的题整理成 `questions` 行的形状。

    /api/paper 组卷时直接把这份塞进卷子里，省一次回查库；也保证「刚出出来的题」
    与「从库里读出来的题」字段完全一致（extra 里带 explain/sections/评分说明）。
    """
    if is_single:
        return {"id": qid, "number": None, "qtype": "single", "stem": q.get("stem"),
                "options": json.dumps(q.get("options") or {}, ensure_ascii=False),
                "answer": q.get("answer"), "outline_id": q.get("outline_id"),
                "extra": extra_json, "year": None, "source": "AI生成"}
    return {"id": qid, "number": None, "qtype": q.get("qtype", "short"), "stem": q.get("stem"),
            "options": None, "answer": None, "outline_id": q.get("outline_id"),
            "extra": extra_json, "year": None, "source": "AI生成"}


def _save_ai_questions(generated, strict_points=True, difficulty=None, thinking=None, *, con=None, private_pilot=False):
    """把 AI 生成的题写库（单选带解析；主观题连小问结构、评分说明、逐点分值）。

    为什么三处入口（/api/paper 补题、/api/daily/generate 补题、全 AI 组卷）**只留这一个函数**：
    它们本来各写一份 INSERT，实测已经走偏 —— 同一道 AI 主观题，走 /api/daily/generate
    入库时 `points.score`/`points.section` 与 `extra.sections` 都没写，于是
    「刚出出来时是分值齐全 + 分小问，一刷新／进练习日志／续练就变成分值待核 + 分组消失」。
    用户截图里那道「篮球队」题（id=1219）就是这个状态。三份实现必然走偏。

    返回 {"single": [...], "subjective": [...], "skipped": [...]}；
    入参 `generated` 里的题**会被就地规范化**（分值不可信就置空），
    这样前端拿到的"刚生成的题"与之后从库里读出来的题是同一份数据。
    """
    own = con is None
    if private_pilot and own:
        raise ValueError("私有试点改编必须由试点保存事务调用")
    con = con if con is not None else sqlite3.connect(DB)
    out = {"single": [], "subjective": [], "skipped": []}

    def extra_of(q, base):
        base.update({"difficulty": q.get("difficulty"),
                     "difficulty_target": difficulty,
                     "thinking": thinking})
        return json.dumps(base, ensure_ascii=False)

    try:
        for q in generated.get("single", []):
            extra_json = extra_of(q, {"explain": q.get("explain", ""),
                                      "cognitive": q.get("cognitive", "")})
            cur = con.execute(
                "INSERT INTO questions (year,source,qtype,number,stem,options,answer,outline_id,extra) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (None, "AI生成", "single", None, q.get("stem", ""),
                 json.dumps(q.get("options") or {}, ensure_ascii=False),
                 q.get("answer"), q.get("outline_id"), extra_json))
            q["id"] = cur.lastrowid
            q["row"] = _ai_question_row(q, q["id"], extra_json, True)
            out["single"].append(q)

        for q in generated.get("subjective", []):
            pts = [p for p in (q.get("points") or []) if isinstance(p, dict)]
            if strict_points and len(pts) < 2 and not private_pilot:
                out["skipped"].append({"stem": (q.get("stem") or "")[:24],
                                       "why": f"采分点只有 {len(pts)} 个，无法自评"})
                continue          # 没采分点的主观题无法自评，不入库（免得又造死题）
            if private_pilot:
                pts = []
            full = None if private_pilot else q.get("full_score", 15)
            secs = [s for s in (q.get("sections") or []) if isinstance(s, dict)]
            score_ok, section_ok = _ai_marks_trusted(pts, full, secs)

            base = {"full_score": full}
            if private_pilot:
                base.update(private_pilot=True, origin="source_based_adaptation")
            note = (q.get("note") or "").strip()
            if note:
                base["answer_note"] = note
            if secs:
                # 分值不可信时，小问分值一起留空 —— 否则小问标题会显示「0 / 8 分」
                # 这种看着正常、其实和要点对不上的数（就是"假分数"）
                base["sections"] = [
                    {"key": str(s.get("key")), "label": s.get("label") or "",
                     "score": (_num(s.get("score")) if score_ok else None),
                     "desc": s.get("desc") or ""} for s in secs]
            extra_json = extra_of(q, base)

            cur = con.execute(
                "INSERT INTO questions (year,source,qtype,number,stem,extra,outline_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (None, "AI试点私有" if private_pilot else "AI生成", q.get("qtype", "short"), None, q.get("stem", ""),
                 extra_json, q.get("outline_id")))
            qid = cur.lastrowid
            q["id"] = qid
            for i, p in enumerate(pts, 1):
                sec = p.get("section")
                con.execute(
                    "INSERT INTO points (question_id,seq,claim,evidence,outline_id,"
                    "score,section,score_src) VALUES (?,?,?,?,?,?,?,?)",
                    (qid, p.get("seq") or i, p.get("claim", ""), p.get("evidence"),
                     q.get("outline_id"),
                     (_num(p.get("score")) if score_ok else None),
                     (str(sec) if (section_ok and sec not in (None, "")) else None),
                     ("ai" if score_ok else None)))
            # 就地规范化：前端"刚生成"的视图与库里的视图必须是同一份数据
            for p in pts:
                if not score_ok:
                    p["score"] = None
                if not section_ok:
                    p["section"] = None
            if secs:
                q["sections"] = base["sections"]
            if note:
                q["answer_note"] = note
            q["row"] = _ai_question_row(q, qid, extra_json, False)
            out["subjective"].append(q)

        if own:
            con.commit()
    finally:
        if own:
            con.close()
    if out["skipped"]:
        print(f"[出题] {len(out['skipped'])} 道主观题未入库：" +
              "；".join(s["why"] for s in out["skipped"]))
    return out


# 旧名字：早期脚本/验收脚本可能还在调它（改动只改名不换语义）
_insert_ai_questions = _save_ai_questions


def _mark_topics_used(topics):
    """在热点清单里给用过的条目打 [已用于出题]，下次优先换别的（只标记标题那一行）。"""
    p = ROOT / "source" / "教育热点.md"
    if not p.exists() or not topics:
        return
    txt = p.read_text(encoding="utf-8")
    for t in topics[:2]:                       # 论述题通常只用一条
        head = f"## {t['date']} ｜ {t['title']}"
        if head in txt and "[已用于出题]" not in head:
            txt = txt.replace(head, head + " [已用于出题]", 1)
    p.write_text(txt, encoding="utf-8")


@app.post("/api/daily/ai-set")
def daily_ai_set(payload: dict):
    """③b 全 AI 模拟组：主题由「今天学了什么」决定，题目**全部 AI 原创、不含真题**。

    与 `/api/daily/generate` 的区别：那个是「真题优先 + AI 补缺」，这个是**刻意不用真题**，
    用来练真题还没覆盖到的角度；且最后一题论述题必须带材料、取材于真实政策热点。

    payload.user_text 给了就自己解析考点（不用先走 /parse）；也可直接传 point_ids。
    payload.difficulty（1–5）与 payload.thinking（默认 true）控制出题时的难度档与思考模式。
    """
    text = (payload.get("user_text") or "").strip()
    ids = payload.get("point_ids") or []
    cfg = resolve_group(payload)
    n_single = cfg["n_single"]
    difficulty = ai_mod.normalize_difficulty(payload.get("difficulty"))
    thinking = bool(payload.get("thinking", True))
    parse_error = None

    if not ids and text:
        try:
            data, _ = ai_mod.parse_study_log(text)
        except Exception as e:
            return {"ok": False, "error": f"定位考点失败：{type(e).__name__}: {e}"}
        ids = _match_ids(data)
        if not ids:
            # 不要再甩「请先写今天学了什么」——文本明明收到了。把真实原因说清楚。
            preview = text[:40] + ("…" if len(text) > 40 else "")
            unmatched = (data or {}).get("unmatched") or []
            parse_error = (f"没从这段记录里定位到考点（收到的文本：「{preview}」）。"
                           f"可以写得更具体些（如「背诵外国教育史第五章：夸美纽斯、卢梭」），"
                           f"或改用上面的「解析并定位考点」手动勾选。"
                           + (f" 未识别内容：{unmatched[:3]}" if unmatched else ""))
    if not ids:
        return {"ok": False,
                "error": parse_error or "没有可用的考点：请先写今天学了什么，或在请求里指定考点。",
                "received_text_len": len(text),
                "hint": "如果这段文字是你写的，说明是定位环节失败；请截图这段文字反馈。"}

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    qs = ",".join("?" * len(ids))
    points = [dict(r) for r in con.execute(
        f"SELECT id,name,path FROM outline_nodes WHERE id IN ({qs})", ids)]
    con.close()
    if not points:
        # 定位到了 id 但库里查不到（例如解析给了不存在的节点）——
        # 这里若不拦住，AI 会拿到空考点清单，出一套跑题的题且不报错
        return {"ok": False,
                "error": f"定位到的考点 id 在库里不存在：{ids[:5]}。请改用上面的「解析并定位考点」手动确认。",
                "matched_ids": ids[:10]}

    topics = ai_mod.hot_topics()
    try:
        paper, usage, check = ai_mod.generate_ai_set(
            text, points, n_single=n_single, topics=topics,
            difficulty=difficulty, thinking=thinking)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 试跑模式：只生成、**不入库**（给自动化验证用，避免测试往真实库写题）
    dry_run = bool(payload.get("dry_run")) or os.environ.get("AI_SET_DRY_RUN") == "1"
    if dry_run:
        saved = {"single": [dict(q, id=None) for q in paper["single"]],
                 "subjective": [dict(q, id=None) for q in paper["subjective"]]}
    else:
        saved = _save_ai_questions(paper, difficulty=difficulty, thinking=thinking)

    # 做题前的自检报告：程序层硬校验（不信 AI 自评）+ AI 质检 + 重出记录
    from collections import Counter
    cog = [q.get("cognitive") for q in saved["single"]]
    n_remember = sum(1 for c in cog if c in ("识记", "记忆", None, ""))
    answers = [(q.get("answer") or "").upper() for q in saved["single"]]
    dist = Counter(a for a in answers if a)
    n_pts = [len(q.get("points") or []) for q in saved["subjective"]]
    essay = [q for q in saved["subjective"] if q.get("qtype") == "essay"]
    audit = (check or {}).get("program_audit") or {}

    report = {
        "single_n": len(saved["single"]),
        "subjective_n": len(saved["subjective"]),
        "cognitive": dict(Counter(c or "未标" for c in cog)),
        "remember_n": n_remember,
        "answer_dist": dict(dist),
        "answer_top_share": round(max(dist.values()) / max(1, sum(dist.values())), 2) if dist else 0,
        "points_per_subjective": n_pts,
        "essay_has_material": bool(essay and ("材料" in (essay[0].get("stem") or "")
                                             or "\n" in (essay[0].get("stem") or ""))),
        "topics_used": [{"date": t["date"], "title": t["title"]} for t in topics[:1]],
        # 程序层硬校验结果（确定性的那几项）
        "program_issues": audit.get("issues", []),
        "attempts": (check or {}).get("attempts"),
        "final_ok": (check or {}).get("final_ok"),
        "retry_history": [
            {"attempt": h["attempt"], "ai_pass": h.get("ai_pass"),
             "ai_must_retry": h.get("ai_must_retry"),
             "issue_n": len(h.get("issues") or []),
             "issues": [f"[{i.get('type')}] {i.get('target')}：{str(i.get('detail'))[:60]}"
                        for i in (h.get("issues") or [])[:6]]}
            for h in ((check or {}).get("history") or [])
        ],
        "ai_check": (check or {}).get("ai_check") or {},
        # 本次出题设置 + 难度达成情况（页面靠它显示"难度有没有贴住你选的档"）
        "difficulty": difficulty,
        "difficulty_name": ai_mod.DIFFICULTY_LEVELS[difficulty]["name"],
        "thinking": thinking,
        "reasoning_tokens": (usage or {}).get("reasoning_tokens", 0),
        "difficulty_mean": audit.get("difficulty_mean"),
        "difficulty_dist": audit.get("difficulty_dist") or {},
        "scene_suspect_n": audit.get("scene_suspect_n", 0),
    }
    if payload.get("mark_topics", True):
        try:
            _mark_topics_used(topics)
        except Exception as e:
            report["mark_topics_error"] = f"{type(e).__name__}: {e}"

    return {
        "ok": True,
        "mode": "ai",
        "group": cfg,
        "focus": [{"id": p["id"], "name": p["name"], "path": p["path"]} for p in points],
        "paper": {"real": {"single": [], "subjective": []}, "generated": saved},
        # 题量透明（设计 §4）：全 AI 原创这一档是**固定** 10＋3（辨析/简答/论述各 1），
        # 实际数量与模型多给被裁掉的数量一并报出去 —— 与"真题优先"档用的是同一个算法。
        "composition": paper_composition(
            {"single": [], "subjective": []}, saved, n_single, _ai_set_mix(),
            trimmed=(check or {}).get("trimmed")),
        # 前端 renderPaper 会读这两个计数（第一版漏了，页面上显示成 "undefined 道"）
        "real_count": 0,
        "generated_count": len(saved["single"]) + len(saved["subjective"]),
        "counts": {
            "single": len(saved["single"]),
            "analysis": sum(1 for q in saved["subjective"] if q.get("qtype") == "analysis"),
            "short": sum(1 for q in saved["subjective"] if q.get("qtype") == "short"),
            "essay": sum(1 for q in saved["subjective"] if q.get("qtype") == "essay"),
            "subjective": len(saved["subjective"]),
        },
        "report": report,
        "usage": usage,
        "usage_info": ai_mod.usage_report(usage),
        "dry_run": dry_run,
    }


@app.get("/api/points")
def points_by_ids(ids: str = ""):
    """按 id 取考点（名称/路径 + 已有的命中率），供**复盘页的「去练这一组」**跳转用。

    为什么需要它：复盘页点「去练这一组」只带得走 id（`/daily?points=1,2,3`），
    而确认页要显示名称、路径与"为什么建议练它"。让页面自己拼这些字段 = 又一份会走样的
    事实来源；所以统一从这里取，命中率有就带上、没有就不编。
    """
    want = [int(x) for x in str(ids or "").replace("，", ",").split(",") if x.strip().isdigit()]
    if not want:
        return {"ok": False, "error": "没有给考点 id"}
    qs = ",".join("?" * len(want))
    rows_ = rows(f"SELECT id, name, path, level FROM outline_nodes WHERE id IN ({qs})", want)
    stats = {}
    try:
        w = weakness()
        for x in (w.get("items") or []) + (w.get("legacy_items") or []):
            if x.get("oid"):
                stats[x["oid"]] = x
    except Exception:
        pass                      # 薄弱点算不出来也不该挡住跳转
    out = []
    for r in rows_:
        st = stats.get(r["id"]) or {}
        out.append({"id": r["id"], "name": r["name"], "path": r["path"], "level": r["level"],
                    "rate": st.get("rate"), "hit": st.get("hit_n"), "total": st.get("total_n"),
                    "origin": ("legacy_unknown" if st.get("origin") == "legacy_unknown" else
                               ("confirmed" if st else None))})
    return {"ok": True, "points": out,
            "missing_ids": [i for i in want if i not in {o["id"] for o in out}]}


@app.post("/api/daily/weak-points")
def daily_weak_points(payload: dict):
    """只**挑出**最弱的几个考点（parsed），不组卷。

    为什么单独开这个：/daily 的流程已改成「先定位考点 → 再选组卷方式」，
    薄弱点这条路也得先让用户看到并确认考点。以前 `/api/daily/weak-paper` 是
    「挑考点 + 立刻组卷」一步做完，中间没有确认的机会 —— 考点是系统算的，
    但**该不该今天练这些**只有用户知道。组卷那一步复用同一个
    `/api/daily/generate`，不必再有一条平行链路。
    """
    min_total = int(payload.get("min_total", 2))
    top_n = int(payload.get("top_n", 8))
    w = weakness()
    conf = [x for x in w["items"] if x["total_n"] >= min_total and x["oid"]]
    conf.sort(key=lambda x: (x["rate"], -x["total_n"]))
    picked = conf[:top_n]

    # 已确认的样本不够时，用**来源不明的旧记录**补位 —— 但必须逐条标出来。
    # 设计 §2.2：「当前薄弱点不足时，可以把历史提示单列给用户选考点，
    # 但不能伪装成确认过的掌握率，也不能据此自动付费组卷。」
    # 所以：origin 字段如实写 legacy_unknown，页面照它显示警告；挑完仍要用户自己确认考点。
    legacy_used = []
    if len(picked) < top_n:
        # **同一个考点不能在两份名单里各出现一次**：实测真实库里有 11 个考点同时出现在
        # `items`（含已确认作答）与 `legacy_items`（只有旧记录）里 —— 因为一个考点既可能有
        # 已确认的作答、也可能有来源不明的作答。不排除的话，页面会出现"已确认 50%"与
        # "旧记录·来源不明 50%"两条一模一样的候选（点「去练这一组」还会传成 `points=8,8`）。
        already = {x["oid"] for x in picked}
        leg = [x for x in w["legacy_items"]
               if x["total_n"] >= min_total and x["oid"] and x["oid"] not in already]
        leg.sort(key=lambda x: (x["rate"], -x["total_n"]))
        legacy_used = leg[:top_n - len(picked)]

    if not picked and not legacy_used:
        return {
            "ok": False,
            "error": f"还没有足够的作答数据（需要某考点至少答过 {min_total} 次）。先做几组练习再来。",
            "weak_nodes": w["summary"]["nodes"],
        }

    def item(x, origin, rank):
        """一条候选考点：命中/总数、来源、**为什么选它**（设计 §4 要求附选取理由）。

        理由必须**从数据里推**，不能编：命中率、样本量、以及"它在已确认里排第几"。
        说不出理由就别写 —— 编一句"你掌握得不好"比不写更糟。
        """
        rate = x["rate"]
        if origin == "confirmed":
            why = (f"已确认作答 {x['hit_n']}/{x['total_n']} 点（{round(rate * 100)}%），"
                   f"是你确认过的考点里命中率第 {rank} 低的")
        else:
            why = (f"旧记录 {x['hit_n']}/{x['total_n']} 点（{round(rate * 100)}%），"
                   f"来源不明，只能当线索")
        return {"id": x["oid"], "name": x["name"], "path": x["path"], "rate": rate,
                "hit": x["hit_n"], "total": x["total_n"], "origin": origin, "reason": why}

    return {
        "ok": True,
        "source": "weak",
        # 已确认的排在前面（页面按顺序展示，用户一眼看出哪些可信）
        "points": [item(x, "confirmed", i + 1) for i, x in enumerate(picked)]
                  + [item(x, "legacy_unknown", i + 1) for i, x in enumerate(legacy_used)],
        "confirmed_n": len(picked),
        "legacy_n": len(legacy_used),
        # 「数据不足」要说出来，而不是给一条编出来的个性化结论（设计 §4）：
        # 已确认样本一个都没有时，页面必须显示"数据不足"，legacy 候选单列。
        "insufficient": len(picked) == 0,
        "min_total": min_total,
        "note": (f"已确认样本不足（每个考点至少答过 {min_total} 次才算），"
                 f"下面几个只有旧记录、来源不明，仅供参考"
                 if legacy_used else ""),
        "weak_nodes": w["summary"]["nodes"],
    }


@app.post("/api/daily/weak-paper")
def daily_weak_paper(payload: dict):
    """按**薄弱点**组卷：优先考你命中率最低的考点。

    与 /api/daily/generate 的区别：那个按「今天学了什么」出题，
    这个按「你哪里弱」出题 —— 不需要输入学习记录。

    ⚠️ **页面上已经不走这个接口了**（改用 /api/daily/weak-points 拿考点、
    确认后再调 /api/daily/generate）。保留它是为了兼容老调用方与测试：
    它的「挑考点 → 直接组卷」行为本身没错，只是不适合"要先确认"的新流程。
    """
    n_single = int(payload.get("n_single", GROUPS[DEFAULT_GROUP]["single"]))
    n_analysis = int(payload.get("n_analysis", 1))
    n_short = int(payload.get("n_short", 1))
    n_essay = int(payload.get("n_essay", 1))
    min_total = int(payload.get("min_total", 2))
    top_n = int(payload.get("top_n", 8))

    picked = daily_weak_points({"min_total": min_total, "top_n": top_n})
    if not picked.get("ok"):
        return picked
    picked = picked["points"]
    ids = [x["id"] for x in picked]
    label = "、".join(x["name"][:14] for x in picked[:3])

    # 复用 generate 的组卷逻辑（难度/思考设置一并透传：这条路也是 AI 补题）
    inner = daily_generate({
        "point_ids": ids,
        "group": payload.get("group"),
        "n_single": n_single, "n_analysis": n_analysis,
        "n_short": n_short, "n_essay": n_essay,
        "difficulty": payload.get("difficulty"),
        "thinking": payload.get("thinking", True),
    })
    inner["focus"] = [
        {"name": x["name"], "path": x["path"], "rate": x["rate"],
         "hit": x["hit"], "total": x["total"]}
        for x in picked
    ]
    inner["focus_summary"] = label
    return inner


def _cap_analysis_judgment(data, qtype, pts):
    """辨析题「判断错/未判断 → 全题不超过判断点分值」的服务端封顶（原地改 `data`）。

    抽成函数是为了让**首轮与重批走同一套口径**（见 `_grade_with_reconcile()`）：
    封顶若只做一次，重批那一次的对账就会拿未封顶的 score 去比，判它"总分对不上"。
    """
    cap_note = None
    if qtype == "analysis":
        j = data.get("judgment") or {}
        explicit = bool(j.get("explicit"))
        matched = bool(j.get("matched"))
        score = data.get("score")
        if not explicit or not matched:
            cap, cap_why = analysis_judge_cap(pts)
            if cap is None:
                # 取不到判断点分值 → **不猜**，如实说无法封顶并交人工复核
                cap_note = f"无法确定判断点分值（{cap_why}），未能按规则封顶，请人工复核"
            elif isinstance(score, (int, float)) and score > cap:
                data["score"] = cap
                cap_note = (f"辨析题判断{'缺失' if not explicit else '错误'} → "
                            f"按规则封顶 {cap:g} 分（理由分不再累加）")
            else:
                cap_note = (f"已按规则检查：判断{'缺失' if not explicit else '错误'}，"
                            f"得分未超过封顶 {cap:g} 分")
        data["judgment_cap_applied"] = cap_note
    return cap_note


def _reconcile_retryable(chk, pts, full):
    """这次对账失败**值不值得再花一次调用**。

    「题目侧」的毛病（满分缺失、采分点为空）重批也无用 —— 那是题的错、不是模型的错，
    重批只会照原样再失败一次，纯烧钱。模型侧的记账问题（字段漏、加不上、小问对不上）才值得重试。
    """
    if chk.get("status") == "v2_ok":
        return False
    if not pts:
        return False
    if not isinstance(full, (int, float)) or isinstance(full, bool) or full <= 0:
        return False
    return not any(("满分缺失" in e) or ("无法对账" in e) for e in (chk.get("errors") or []))


def _reconcile_retry_instruction(errors):
    """重批时把**具体错在哪**说给模型听（比单纯"再来一次"有效），并要求只改记账、不改给分。"""
    return ("【上一次的输出没有通过服务端对账，请重新输出一次】\n"
            "服务端自己把账加了一遍，发现问题：\n"
            + "\n".join(f"- {e}" for e in (errors or [])[:6])
            + "\n请按下面的口径重出**完整 JSON**（不是补丁）：\n"
              "1. `point_awards` 每个采分点一行（含没答到的，awarded 写 0），seq 用我给的序号；\n"
              "2. `score` 必须等于 `sum(point_awards.awarded) + sum(extra_credit.awarded) "
              "+ sum(adjustments.delta)`，再按 0～满分夹取；\n"
              "3. `section_scores` 各小问得分之和要等于**含加分的总分**；\n"
              "4. **只把账记对，不要为了凑数去改 awarded**（那是改分，不是对账）："
              "该给的分照原判断给，把理由写清楚。")


def _grade_retry_enabled():
    """`grade.retry_reconcile` 开关（默认 on）。关掉省钱，代价是对账失败只提示、不重批。"""
    try:
        v = ai_mod.scenario_value("grade", "retry_reconcile", "on")
    except Exception:
        return True
    return str(v).lower() not in ("off", "0", "false", "no")


def _grade_with_reconcile(q, pts, full, secs, note, student, image_data_url):
    """批改一次；对账不通过时（可重批 ＋ 开关开着）**自动再批一次**，上限 1 次。

    **为什么重批编排在服务端、不在 `ai.py`**：辨析题的封顶是服务端做的
    （`_cap_analysis_judgment`）。若把重批塞进 ai.py 内部，模型对"判断错"的题给出的
    `score` 未封顶 → 每次都会被判「总分对不上」→ 每次重批都白烧钱。放这里才能让两次调用
    共用同一套「封顶 ＋ 对账」。

    返回 `(data, usage_total, chk, cap_note, retry_info, err)`：
      · `err` 非 None = **首轮调用就失败**，调用方照旧返回 `ok=False`（文案与以前一致）；
      · 重批那一次失败**不丢首轮结果**：记进 `retry_info`，评语照旧给用户看。
    """
    usage_total, instruction = {}, None
    data = chk = cap_note = retry_info = None
    for attempt in (1, 2):
        try:
            data_i, usage_i = ai_mod.grade_answer(
                q["stem"], pts,
                student_text=student or None,
                full_score=full,
                use_thinking=True,
                image_data_url=image_data_url,
                qtype=q["qtype"],
                sections=secs,
                answer_note=note,
                extra_instruction=instruction,
            )
        except ai_mod.JSONReplyError as e:
            # 说清是"被截断"还是"格式坏"，并附上原始返回的尾巴 —— 以前这里只甩一句
            # JSONDecodeError，用户和接手的人都看不出病因（真实反馈过）。
            kind = "被 token 上限截断" if e.truncated else "不是合法 JSON（格式有误）"
            if attempt == 1:
                return None, usage_total, None, None, None, {
                    "ok": False,
                    "error": f"AI 返回的内容{kind}（已自动重试一次仍未成功）。",
                    "raw_tail": e.raw[-200:],
                    "json_error": str(e.original),
                    "truncated": e.truncated}
            retry_info.update({"second_status": "json_error",
                               "second_error": f"{kind}：{e.original}"})
            break
        except Exception as e:
            if attempt == 1:
                return None, usage_total, None, None, None, {
                    "ok": False, "error": f"{type(e).__name__}: {e}"}
            retry_info.update({"second_status": "error",
                               "second_error": f"{type(e).__name__}: {e}"})
            break
        # 计费如实合并：两次调用都算进这次批改（页面按合计显示，不能让人以为只花了一次的钱）
        usage_total["grade_attempts"] = ((usage_total.get("grade_attempts") or 0)
                                         + (usage_i.get("grade_attempts") or 1))
        ai_mod.add_usage(usage_total, usage_i)
        cap_note = _cap_analysis_judgment(data_i, q["qtype"], pts)
        jcap, _why = analysis_judge_cap(pts) if q["qtype"] == "analysis" else (None, None)
        chk_i = verify_grade_v2(data_i, pts, full, secs, q["qtype"], jcap)
        data, chk = data_i, chk_i
        if chk_i["status"] == "v2_ok" or attempt == 2:
            break
        if not _grade_retry_enabled() or not _reconcile_retryable(chk_i, pts, full):
            break
        retry_info = {"attempted": True, "first_errors": chk_i["errors"],
                      "second_status": None}
        instruction = _reconcile_retry_instruction(chk_i["errors"])
    if retry_info and retry_info.get("second_status") is None:
        retry_info["second_status"] = chk["status"] if chk else None
    return data, usage_total, chk, cap_note, retry_info, None


def _ground_grade_feedback(data, student):
    """只核对原文引句是否存在，不把数学对账等同于学科判断已被校准。"""
    source = "".join(student.split()) if isinstance(student, str) else ""
    unmatched = 0
    for field in ("point_awards", "diagnosis"):
        items = data.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or "quote" not in item:
                continue
            quote = item.get("quote")
            needle = "".join(quote.split()) if isinstance(quote, str) else ""
            status = "matched" if needle and needle in source else "unmatched" if needle else "not_provided"
            item["quote_status"] = status
            unmatched += status == "unmatched"
    data["feedback_verification"] = {"unmatched_quotes": unmatched,
                                      "note": "仅核对引句存在性；学科判断与图片识别仍需核对。"}


@app.post("/api/daily/grade")
def daily_grade(payload: dict):
    """③ 批改主观题。文本或图片二选一。"""
    qid = payload.get("question_id")
    student = (payload.get("student_text") or "").strip()
    image_data_url = payload.get("image")  # data:image/png;base64,...
    if not qid:
        return {"ok": False, "error": "缺少题目 id"}

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    q = con.execute("SELECT stem,extra,qtype,number,year,source FROM questions WHERE id=?",
                    (qid,)).fetchone()
    if not q:
        con.close()
        return {"ok": False, "error": "题目不存在"}
    pts = [dict(r) for r in con.execute(
        f"SELECT {POINT_COLS} FROM points WHERE question_id=? ORDER BY seq", (qid,))]
    con.close()
    try:
        qex = json.loads(q["extra"] or "{}")
    except Exception:
        qex = {}
    full = qex.get("full_score")
    secs = sections_of(qex)
    note = qex.get("answer_note") or None

    if qex.get("private_pilot"):
        return {"ok": False, "error": "试点题使用定性诊断，请从试点页面继续。"}
    if not student and not image_data_url:
        return {"ok": False, "error": "请填写答案文本，或上传手写照片"}

    # 批改 ＋ （对账不通过时）自动重批一次：封顶与对账两次共用同一套口径，细节见该函数。
    data, usage, _chk, cap_note, retry_info, err = _grade_with_reconcile(
        q, pts, full, secs, note, student, image_data_url)
    if err:
        return err

    _ground_grade_feedback(data, data.get("recognized") if image_data_url else student)

    # 新版逐点分账的服务端对账（设计 §3）。三种结果各有各的待遇：
    #   · v2_ok        → 这个 AI 分数可以进「可信 AI 参考分」；
    #   · needs_review → 评语与原始结果照常给用户看、也照常存，但**不进可信趋势**；
    #                    （2026-10-09 起：首轮不通过会先自动重批一次，仍不通过才是这个状态）
    #   · 旧结果       → 单题明细里照旧显示，状态是 legacy_unverifiable。
    data["verification"] = {k: _chk[k] for k in
                            ("status", "errors", "sum_awards", "extra", "delta",
                             "raw", "expected", "judge_cap")}
    if retry_info and retry_info.get("attempted"):
        data["verification"]["retry"] = retry_info
    if _chk["status"] != "v2_ok":
        data["needs_review"] = _chk["errors"]

    score_result = attempt_score({**dict(q), "question_id": qid, "grade": json.dumps(data, ensure_ascii=False),
                                  "point_source": "ai_unconfirmed",
                                  "selected_option": _normalize_option(payload.get("selected_option"))}, pts)
    return {"ok": True, "result": data, "usage": usage, "score_result": score_result,
            "usage_info": ai_mod.usage_report(usage),
            # 把这道题的采分点（含小问与分值）一起回给前端：批改结果里的
            # 「命中 1/3/5」要能就地折合成「+3 +4 −8」的分数阶梯，
            # 前端不该再为了这件事回查一次题目。
            "points": pts, "sections": secs, "answer_note": note,
            "full_score": full, "score_stats": score_stats(pts, full, secs, q["stem"], note),
            "verification": data["verification"],
            **({"score_cap_note": cap_note} if cap_note else {})}


# ============================================================ ⑤ 学习复盘报告


def _session_attempts(session_id):
    """取某一次练习的作答明细（客观 + 主观），并带上计分与展示需要的元数据。

    用 LEFT JOIN：题目被删/不存在时也要能取到这条件，否则「有作答却查不到」
    会被误报成「没有作答记录」，排查时看不出真正原因（已踩过）。

    2026-10-08 补 `q.extra` / `q.stem` / `a.point_source` / `a.selected_option` / `a.grade`：
      · 少了 `q.extra`，`_build_metrics()` 拿到的 extra 恒为 `{}` → 满分回退 15（P0 的一半）；
      · 少了 `q.stem`，选做题判不出来（`marks.is_elective` 要看题干）；
      · 少了来源与批改结果，折合分无法按可信度分账（P0 的另一半）。
    """
    return rows(
        "SELECT a.id, a.id AS attempt_id, a.question_id, a.hits, a.total, a.cause, a.mode, "
        "       a.point_source, a.selected_option, a.grade, a.date, a.session_id, "
        "       a.note, a.student_answer, q.number, q.options, q.answer, q.year, q.source, "
        "       q.outline_id, q.qtype, q.stem, q.extra, o.name AS node_name, o.path AS node_path, "
        "       o.name AS outline_name, o.path AS outline_path "
        "FROM attempts a "
        "LEFT JOIN questions q ON q.id = a.question_id "
        "LEFT JOIN outline_nodes o ON o.id = q.outline_id "
        "WHERE a.session_id = ? ORDER BY a.id",
        (session_id,),
    )


def _review_context(sess, attempts=None):
    """按具体作答构建复盘证据；复用日志与计分实现，保持完整题单及重复作答。"""
    attempts = _session_attempts(sess["id"]) if attempts is None else attempts
    listed = _session_question_ids(sess.get("question_ids"))
    ordered = list(dict.fromkeys(listed + [a["question_id"] for a in attempts]))
    pts, hits = _points_and_hits(sess["date"], ordered)
    qrows = _question_rows(ordered)
    by_question = {}
    for a in attempts:
        by_question.setdefault(a["question_id"], []).append(a)
    questions = []
    for qid in ordered:
        records = by_question.get(qid)
        if records:
            for a in records:
                q = _attempt_detail_with_points(a, pts, hits)
                q.update(extra=a.get("extra"), outline_id=a.get("outline_id"))
                if isinstance(q.get("grade"), dict):
                    _ground_grade_feedback(q["grade"], q.get("my_answer"))
                questions.append(q)
        else:
            row = qrows.get(qid) or _unanswered_placeholder(qid)
            q = _unanswered_detail(row, pts.get(qid, []))
            q.update(extra=row.get("extra"), outline_id=row.get("outline_id"))
            questions.append(q)
    notes_rows = rows("SELECT summary,dismissed_ids,updated_at FROM review_notes WHERE session_id=?", (sess["id"],))
    notes = notes_rows[0] if notes_rows else {"summary": None, "updated_at": None}
    notes["dismissed_ids"] = _json_or(notes.get("dismissed_ids"), [])
    analysis = review_mod.organize(questions, notes["dismissed_ids"])
    # 完整事实参与指纹，包括截取段落之外的改动。用户总结是独立文字，不喂给 AI。
    basis_data = {"version": review_mod.VERSION, "questions": questions,
                  "dismissed": sorted(notes["dismissed_ids"]), "listed": listed}
    retests = _retest_summary_for_session(sess["id"])
    if retests:
        basis_data["retests"] = retests
    basis = review_mod.digest(basis_data)
    # 题号是展示信息，不加入旧正文依据指纹，也不改变错因/订正的内部身份。
    references = review_mod.question_references(questions, listed)
    questions = [{**q, "question_label": references[review_mod.question_key(q)]["label"]} for q in questions]
    prompt = review_mod.prompt_context(questions, analysis, bool(listed))
    if retests:
        prompt["retests"] = retests
    return {"questions": questions, "notes": notes, "analysis": analysis,
            "prompt": prompt, "basis": basis, "has_question_set": bool(listed), "question_refs": references}


@app.get("/api/review/session/{session_id}")
def review_detail(session_id: int):
    found = rows("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not found:
        return {"ok": False, "error": "练习记录不存在。"}
    sess = found[0]
    if sess.get("mode") == "主观题试点":
        return {"ok": False, "error": "请从主观题试点页面订正，保留冻结依据与阶段记录。", "pilot_url": "/pilot"}
    context = _review_context(sess)
    report_rows = rows("SELECT id FROM reports WHERE session_id=? ORDER BY id DESC LIMIT 1", (session_id,))
    report = report_one(report_rows[0]["id"])["report"] if report_rows else None
    return {"ok": True, "session": {k: sess.get(k) for k in ("id", "date", "mode", "note")},
            "metrics": _fresh_session_metrics(sess), "report": report,
            "questions": context["questions"], "analysis": context["analysis"],
            "notes": context["notes"], "has_question_set": context["has_question_set"],
            "question_refs": context["question_refs"]}


@app.post("/api/review/session/{session_id}/notes")
def review_notes_save(session_id: int, payload: dict):
    found = rows("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not found:
        return {"ok": False, "error": "练习记录不存在。"}
    if not payload or set(payload) - {"summary", "finding_id", "dismissed"}:
        return {"ok": False, "error": "复盘修正字段不正确。"}
    if "summary" in payload and payload["summary"] is not None:
        if not isinstance(payload["summary"], str) or len(payload["summary"]) > 6000:
            return {"ok": False, "error": "总结须为不超过 6000 字的文字。"}
    if "finding_id" in payload or "dismissed" in payload:
        context = _review_context(found[0])
        allowed = {f["id"] for f in context["analysis"]["findings"]}
        if not isinstance(payload.get("finding_id"), str) or payload["finding_id"] not in allowed \
                or not isinstance(payload.get("dismissed"), bool):
            return {"ok": False, "error": "这条判断不属于本次复盘，或判断依据已变化，请刷新后重试。"}
    con = sqlite3.connect(DB)
    try:
        con.execute("BEGIN IMMEDIATE")
        prior = con.execute("SELECT summary,dismissed_ids FROM review_notes WHERE session_id=?", (session_id,)).fetchone()
        summary, dismissed = (prior[0], set(_json_or(prior[1], []))) if prior else (None, set())
        if "summary" in payload:
            summary = payload["summary"]
        if "finding_id" in payload:
            if payload["dismissed"]:
                dismissed.add(payload["finding_id"])
            else:
                dismissed.discard(payload["finding_id"])
        con.execute("INSERT INTO review_notes (session_id,summary,dismissed_ids,updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET summary=excluded.summary,"
                    "dismissed_ids=excluded.dismissed_ids,updated_at=excluded.updated_at",
                    (session_id, summary, json.dumps(sorted(dismissed)), datetime.now().isoformat(timespec="seconds")))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return {"ok": True}


@app.get("/api/review/session/{session_id}/practice/{attempt_id}")
def review_practice(session_id: int, attempt_id: int, finding: str = ""):
    found = rows("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not found:
        return {"ok": False, "error": "练习记录不存在。"}
    sess = found[0]
    if sess.get("mode") == "主观题试点":
        return {"ok": False, "error": "请从主观题试点页面订正，保留冻结依据与阶段记录。", "pilot_url": "/pilot"}
    context = _review_context(sess)
    q = next((q for q in context["questions"] if q.get("attempt_id") == attempt_id), None)
    if not q or q.get("missing"):
        return {"ok": False, "error": "未找到本次作答对应的题目，无法载入订正。"}
    task = next((t for t in context["analysis"]["tasks"] if t["finding_id"] == finding
                 and t["attempt_id"] == attempt_id), None)
    if finding and not task:
        return {"ok": False, "error": "这条改进任务已变化或被排除，请回到复盘页刷新。"}
    q = dict(q, id=q["question_id"])
    if q["qtype"] == "single":
        q["answer"] = q.get("correct_answer")
    return {"ok": True, "session": {k: sess.get(k) for k in ("id", "date", "mode", "note")},
            "singles": [q] if q["qtype"] == "single" else [],
            "subjectives": [] if q["qtype"] == "single" else [q],
            "counts": {"total": 1, "answered": 1, "unanswered": 0, "missing": 0},
            "review_task": task or {"instruction": "对照题目要求与批改，订正本题。",
                                    "criterion": "核对改写后的答案是否回应题目要求。"},
            "source_attempt_id": attempt_id}


@contextmanager
def _learning_connection(write=False):
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        if write:
            con.execute("BEGIN IMMEDIATE")
        yield con
        if write:
            con.commit()
    except Exception:
        if write:
            con.rollback()
        raise
    finally:
        con.close()


def _learning_action(action, write=False):
    try:
        with _learning_connection(write) as con:
            return action(con)
    except retest_mod.RetestError as error:
        return {"ok": False, "error": str(error)}


def _learning_question(con, qid):
    row = con.execute(
        "SELECT q.*,q.id AS question_id,o.name AS outline_name,o.path AS outline_path "
        "FROM questions q LEFT JOIN outline_nodes o ON o.id=q.outline_id WHERE q.id=?", (qid,)).fetchone()
    if not row:
        raise retest_mod.RetestError("题目已不在题库，不能开始新的复测；已有记录仍保留。")
    points = [dict(p) for p in con.execute("SELECT * FROM points WHERE question_id=? ORDER BY seq", (qid,))]
    q = _unanswered_detail(dict(row), points)
    q.update(id=qid, extra=row["extra"], outline_id=row["outline_id"])
    return q


def _learning_snapshot(con, aid):
    pilot_snapshot = pilot_mod.snapshot(con, aid)
    if pilot_snapshot:
        return pilot_snapshot
    row = con.execute(
        "SELECT a.*,a.id AS attempt_id,q.qtype,q.number,q.stem,q.options,q.answer,q.year,q.source,q.extra,"
        "q.outline_id,o.name AS outline_name,o.path AS outline_path "
        "FROM attempts a LEFT JOIN questions q ON q.id=a.question_id "
        "LEFT JOIN outline_nodes o ON o.id=q.outline_id WHERE a.id=?", (aid,)).fetchone()
    if not row or row["qtype"] is None or row["session_id"] is None:
        raise retest_mod.RetestError("原始作答或题目不存在，无法建立复测关联。")
    raw = dict(row)
    pts = [dict(p) for p in con.execute("SELECT * FROM points WHERE question_id=? ORDER BY seq", (row["question_id"],))]
    hit_seqs = {p[0] for p in con.execute(
        "SELECT p.seq FROM point_hits h JOIN points p ON p.id=h.point_id WHERE h.attempt_id=? AND h.hit=1", (aid,))}
    q = _attempt_detail_with_points(raw, {row["question_id"]: pts}, {aid: hit_seqs})
    q.update(id=q["question_id"], extra=row["extra"], outline_id=row["outline_id"])
    scores = {}
    sr = q.get("score_result") or {}
    if q["qtype"] == "single":
        scores["objective"] = {"score": q["score"], "full": 2, "source": "客观题判分"}
    else:
        point = sr.get("point_score") or sr
        if sr.get("scored"):
            scores["practice"] = {"score": sr["score"], "full": sr["full"], "source": sr["score_source_label"]}
        if point.get("scored"):
            series = "confirmed" if point["source"] in CONFIRMED_SOURCES else "legacy"
            scores[series] = {"score": point["score"], "full": point["full"], "source": point["source_label"]}
    if q["qtype"] != "single":
        ai = _ai_reference(raw, points=q["points"], full=q["full_score"], sections=q.get("sections"),
                           qtype=q["qtype"], judge_cap=analysis_judge_cap(q["points"])[0] if q["qtype"] == "analysis" else None)
        if ai.get("status") in ("v2_ok", "human_ok"):
            scores["ai_reference"] = {"score": ai["score"], "full": ai["full"],
                                       "source": "AI 参考分 · " + ("人工核对" if ai.get("manual") else "分账对账通过")}
    grade = q.get("grade") if isinstance(q.get("grade"), dict) else {}
    return {"attempt_id": aid, "session_id": row["session_id"], "question": q,
            "criteria": retest_mod.criteria(q, q.get("selected_option")), "scores": scores,
            "hit_seqs": sr.get("hit_seqs", []), "diagnoses": grade.get("diagnosis") or [],
            "answer_date": row["date"], "captured_at": retest_mod.stamp(retest_mod.now())}


def _plan_for_source(con, aid):
    row = con.execute("SELECT * FROM retest_plans WHERE source_attempt_id=? OR id IN "
                      "(SELECT plan_id FROM retest_runs WHERE saved_attempt_id=?) ORDER BY id LIMIT 1", (aid, aid)).fetchone()
    return dict(row) if row else None


def _create_retest_plan(con, snapshot, values=None):
    values = retest_mod.intervals(values)
    existing = _plan_for_source(con, snapshot["attempt_id"])
    if existing:
        return existing
    at = retest_mod.now()
    cur = con.execute("INSERT INTO retest_plans (source_attempt_id,source_session_id,question_id,initial_json,"
                      "intervals,due_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                      (snapshot["attempt_id"], snapshot["session_id"], snapshot["question"]["question_id"],
                       json.dumps(snapshot, ensure_ascii=False), json.dumps(values), retest_mod.due_after(at, values[0]),
                       retest_mod.stamp(at), retest_mod.stamp(at)))
    return dict(con.execute("SELECT * FROM retest_plans WHERE id=?", (cur.lastrowid,)).fetchone())


def _prepare_learning_save(con, payload, run_id):
    if not run_id and not payload.get("review_source"):
        return None
    entries = [x for x in payload.get("single", []) + payload.get("subjective", []) if x.get("id")]
    if len(entries) != 1:
        raise retest_mod.RetestError("关联复盘时每次只保存指定的一道题。")
    canonical = retest_mod.digest({k: v for k, v in payload.items() if k != "submission_id"})
    if run_id:
        row = con.execute("SELECT * FROM retest_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise retest_mod.RetestError("复测轮次不存在。")
        run = dict(row)
        if run["state"] == "saved":
            if run["commit_hash"] != canonical:
                raise retest_mod.RetestError("这次复测已经保存，不能覆盖原记录。")
            saved = con.execute("SELECT save_response FROM sessions WHERE id=?", (run["saved_session_id"],)).fetchone()
            return {"saved_response": json.loads(saved[0])}
        if run["state"] != "grading":
            raise retest_mod.RetestError("请先提交并锁定本次独立作答，再保存评定。")
        question = _learning_question(con, entries[0]["id"])
        if retest_mod.criteria(question, run["selected_option"]) != run["criteria"]:
            raise retest_mod.RetestError("本次题目或评分依据已变化，作答已保留；请回到计划重新开始这一轮。")
        return {"run": run, "commit_hash": canonical}
    source = payload["review_source"]
    if not isinstance(source, dict) or set(source) != {"session_id", "attempt_id"} \
            or any(isinstance(x, bool) or not isinstance(x, int) for x in source.values()):
        raise retest_mod.RetestError("订正来源格式不正确。")
    snapshot = _learning_snapshot(con, source["attempt_id"])
    if snapshot.get("pilot"):
        raise retest_mod.RetestError("试点订正请从试点入口继续，保留冻结参考与阶段记录。")
    if snapshot["session_id"] != source["session_id"] or snapshot["question"]["question_id"] != entries[0]["id"]:
        raise retest_mod.RetestError("订正题目与原始作答不对应。")
    if payload.get("mode") != "复盘订正":
        raise retest_mod.RetestError("该来源只用于原题订正。")
    return {"plan": _create_retest_plan(con, snapshot), "commit_hash": canonical}


def _finish_learning_save(con, context, sid):
    attempts = con.execute("SELECT id FROM attempts WHERE session_id=? ORDER BY id", (sid,)).fetchall()
    if len(attempts) != 1:
        raise retest_mod.RetestError("复测关联必须对应一条实际作答。")
    aid = attempts[0][0]
    snapshot = _learning_snapshot(con, aid)
    at = retest_mod.now()
    when = retest_mod.stamp(at)
    if context.get("plan"):
        plan = context["plan"]
        values = json.loads(plan["intervals"])
        status = "paused" if plan["status"] == "paused" else "active"
        con.execute("UPDATE retest_plans SET round_index=0,due_at=?,status=?,generation=generation+1,updated_at=? WHERE id=?",
                    (retest_mod.due_after(at, values[0]), status, when, plan["id"]))
        con.execute("UPDATE retest_runs SET state='abandoned',note='新订正已重排计划，原复测草稿保留' "
                    "WHERE plan_id=? AND state IN ('answering','grading')", (plan["id"],))
        run_id = uuid4().hex
        con.execute("INSERT INTO retest_runs (id,plan_id,kind,round_index,generation,state,started_at,locked_at,"
                    "answer_text,selected_option,question_json,criteria,saved_attempt_id,saved_session_id,snapshot_json,"
                    "completed_at,commit_hash) VALUES (?,?,?,-1,?,'saved',?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, plan["id"], "correction", plan["generation"], when, when,
                     snapshot["question"].get("my_answer"), snapshot["question"].get("selected_option"),
                     json.dumps(snapshot["question"], ensure_ascii=False), snapshot["criteria"], aid, sid,
                     json.dumps(snapshot, ensure_ascii=False), when, context["commit_hash"]))
        return {"plan_id": plan["id"], "run_id": run_id, "counted": False}
    run = context["run"]
    plan = dict(con.execute("SELECT * FROM retest_plans WHERE id=?", (run["plan_id"],)).fetchone())
    counted = (run["kind"] == "retest" and plan["status"] == "active" and
               run["generation"] == plan["generation"] and run["round_index"] == plan["round_index"])
    note = "本轮到期复测已完成" if counted else "提前练习或计划已调整，本次保存不推进到期轮次"
    con.execute("UPDATE retest_runs SET state='saved',saved_attempt_id=?,saved_session_id=?,snapshot_json=?,"
                "completed_at=?,counted=?,note=?,commit_hash=? WHERE id=?",
                (aid, sid, json.dumps(snapshot, ensure_ascii=False), when, int(counted), note, context["commit_hash"], run["id"]))
    if counted:
        step = plan["round_index"] + 1
        values = json.loads(plan["intervals"])
        due = retest_mod.due_after(at, values[step]) if step < len(values) else None
        con.execute("UPDATE retest_plans SET round_index=?,due_at=?,status=?,updated_at=? WHERE id=?",
                    (step, due, "active" if due else "complete", when, plan["id"]))
    return {"plan_id": plan["id"], "run_id": run["id"], "counted": bool(counted)}


def _retest_plan_view(con, plan, history=False):
    result = retest_mod.public_plan(plan, retest_mod.now())
    initial = json.loads(plan["initial_json"])
    if initial.get("pilot"):
        result.update(title="主观题试点 · " + initial["pilot"]["slot"], pilot=True,
                      pilot_url="/pilot", open_run=None, stages=[], comparisons={},
                      comparison_note="在试点页查看定性记录；不与考试分数混算。")
        return result
    q = initial["question"]
    result.update(title=q.get("outline_name") or q.get("qtype_label") or "复测题目", stem=q.get("stem"),
                  qtype=q.get("qtype"))
    pending = con.execute("SELECT id,state FROM retest_runs WHERE plan_id=? AND state IN ('answering','grading')", (plan["id"],)).fetchone()
    result["open_run"] = dict(pending) if pending else None
    if not history:
        return result
    stages = [{"kind": "initial", "snapshot": initial, "completed_at": initial.get("answer_date"),
               "counted": False, "note": "初答按加入计划时的题库规则回看，原始时点未冻结的依据无法追溯。"}]
    for run in con.execute("SELECT * FROM retest_runs WHERE plan_id=? ORDER BY started_at,rowid", (plan["id"],)):
        item = dict(run)
        item["snapshot"] = _json_or(item.pop("snapshot_json"), None)
        item.pop("question_json", None)
        item.pop("commit_hash", None)
        stages.append(item)
    anchor = retest_mod.moment(plan["created_at"])
    for stage in stages[1:]:
        stage["elapsed_days"] = round(max(0, (retest_mod.moment(stage["started_at"]) - anchor).total_seconds()) / 86400, 2)
        if stage.get("completed_at") and (stage["kind"] == "correction" or stage.get("counted")):
            anchor = retest_mod.moment(stage["completed_at"])
    result["stages"] = stages
    corrections = [s for s in stages if s["kind"] == "correction" and s.get("snapshot")]
    last_correction = max((i for i, s in enumerate(stages) if s["kind"] == "correction"), default=0)
    retests = [s for s in stages[last_correction + 1:] if s.get("counted") and s.get("snapshot")]
    result["comparisons"] = {}
    if corrections:
        result["comparisons"]["correction"] = retest_mod.compare(initial, corrections[-1]["snapshot"])
    if retests:
        result["comparisons"]["initial_to_retest"] = retest_mod.compare(initial, retests[-1]["snapshot"])
        if corrections:
            result["comparisons"]["correction_to_retest"] = retest_mod.compare(corrections[-1]["snapshot"], retests[-1]["snapshot"])
    result["comparison_note"] = "只对比最新订正之后的到期复测，更早的复测保留在记录中。完成轮次或单次涨分不代表长期掌握。AI 参考分单独展示。"
    return result


def _retest_summary_for_session(sid):
    with _learning_connection() as con:
        plans = con.execute("SELECT * FROM retest_plans WHERE source_session_id=? OR id IN "
                            "(SELECT plan_id FROM retest_runs WHERE saved_session_id=?) ORDER BY id", (sid, sid)).fetchall()
        result = []
        for raw in plans:
            p = _retest_plan_view(con, dict(raw), True)
            result.append({"plan_id": p["id"], "source_attempt_id": p["source_attempt_id"],
                           "status": p["status"], "completed_rounds": p["round_index"], "due_at": p["due_at"],
                           "comparisons": p["comparisons"],
                           "stages": [{"kind": s["kind"], "counted": bool(s.get("counted")),
                                       "date": s.get("completed_at"),
                                       "scores": (s.get("snapshot") or {}).get("scores", {}),
                                       "note": s.get("note")} for s in p["stages"]]})
        return result


@app.get("/api/retest/plans")
def retest_plans():
    return _learning_action(lambda con: {"ok": True, "now": retest_mod.stamp(retest_mod.now()),
        "default_intervals": list(retest_mod.DEFAULT_INTERVALS),
        "plans": [_retest_plan_view(con, dict(p)) for p in con.execute("SELECT * FROM retest_plans ORDER BY status,due_at,id")]})


@app.post("/api/retest/plans")
def retest_enroll(payload: dict):
    def action(con):
        aid = payload.get("attempt_id")
        if isinstance(aid, bool) or not isinstance(aid, int):
            raise retest_mod.RetestError("请选择一条原始作答。")
        snapshot = _learning_snapshot(con, aid)
        if snapshot.get("pilot"):
            return {"ok": False, "error": "试点 A 订正后自动安排复测，请从试点入口继续。"}
        plan = _create_retest_plan(con, snapshot, payload.get("intervals"))
        return {"ok": True, "plan": _retest_plan_view(con, plan, True)}
    return _learning_action(action, True)


@app.get("/api/retest/plans/{plan_id}")
def retest_plan_one(plan_id: int):
    def action(con):
        row = con.execute("SELECT * FROM retest_plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise retest_mod.RetestError("复测计划不存在。")
        return {"ok": True, "plan": _retest_plan_view(con, dict(row), True)}
    return _learning_action(action)


@app.post("/api/retest/plans/{plan_id}/schedule")
def retest_schedule(plan_id: int, payload: dict):
    def action(con):
        row = con.execute("SELECT * FROM retest_plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise retest_mod.RetestError("复测计划不存在。")
        p = dict(row)
        values = retest_mod.intervals(payload.get("intervals", json.loads(p["intervals"])))
        status = payload.get("status", p["status"])
        if status not in ("active", "paused") or p["round_index"] >= len(values):
            raise retest_mod.RetestError("计划轮次已完成，若继续安排请增加间隔轮数。")
        due = retest_mod.date_due(payload["due_on"]) if payload.get("due_on") else p["due_at"]
        if values != json.loads(p["intervals"]) or status != p["status"] or due != p["due_at"]:
            con.execute("UPDATE retest_plans SET intervals=?,status=?,due_at=?,generation=generation+1,updated_at=? WHERE id=?",
                        (json.dumps(values), status, due, retest_mod.stamp(retest_mod.now()), plan_id))
            if json.loads(p["initial_json"]).get("pilot"):
                con.execute("UPDATE pilot_runs SET state='abandoned' WHERE retest_run_id IN (SELECT id FROM retest_runs WHERE plan_id=? AND state IN ('answering','grading'))", (plan_id,))
                con.execute("UPDATE retest_runs SET state='abandoned',note='计划调整，试点草稿已保留' WHERE plan_id=? AND state IN ('answering','grading')", (plan_id,))
        return {"ok": True}
    return _learning_action(action, True)


def _retest_run_public(run):
    q = json.loads(run["question_json"])
    if q.get("pilot_run_id"):
        return {"id": run["id"], "plan_id": run["plan_id"], "state": run["state"],
                "pilot_url": "/pilot?run=" + q["pilot_run_id"]}
    if run["state"] == "saved" and run.get("snapshot_json"):
        q = json.loads(run["snapshot_json"])["question"]
    if run["state"] == "answering":
        q = {k: q.get(k) for k in ("id", "question_id", "qtype", "qtype_label", "stem", "options", "year", "number", "source", "outline_name", "full_score")}
    else:
        q["my_answer"] = run["answer_text"]
        q["selected_option"] = run["selected_option"]
        q["answered"] = True
    q["elective"] = _is_elective(q.get("stem"))
    return {"id": run["id"], "plan_id": run["plan_id"], "kind": run["kind"], "state": run["state"],
            "started_at": run["started_at"], "locked_at": run.get("locked_at"), "due_at": run.get("due_at"),
            "round_index": run["round_index"], "selected_option": run.get("selected_option"), "question": q,
            "answer_text": run["answer_text"] if run["state"] != "answering" else None,
            "saved_session_id": run.get("saved_session_id"), "note": run.get("note")}


@app.post("/api/retest/plans/{plan_id}/start")
def retest_start(plan_id: int, payload: dict):
    def action(con):
        row = con.execute("SELECT * FROM retest_plans WHERE id=?", (plan_id,)).fetchone()
        if not row or row["status"] == "paused":
            raise retest_mod.RetestError("计划不存在或已暂停，请先恢复计划。")
        p = dict(row)
        if json.loads(p["initial_json"]).get("pilot"):
            pilot_run = pilot_mod.start_retest(con, p, payload.get("early") is True)
            return {"ok": True, "run": {"id": pilot_run["id"], "pilot_url": "/pilot?run=" + pilot_run["id"]}}
        existing = con.execute("SELECT * FROM retest_runs WHERE plan_id=? AND state IN ('answering','grading')", (plan_id,)).fetchone()
        if existing:
            return {"ok": True, "run": _retest_run_public(dict(existing))}
        at = retest_mod.now()
        scheduled = p["status"] == "active" and p["due_at"] and at >= retest_mod.moment(p["due_at"])
        if not scheduled and payload.get("early") is not True:
            raise retest_mod.RetestError("还没到期；若现在练习，请选择提前练习，它不会推进到期轮次。")
        q = _learning_question(con, p["question_id"])
        selected = json.loads(p["initial_json"])["question"].get("selected_option")
        run_id = uuid4().hex
        con.execute("INSERT INTO retest_runs (id,plan_id,kind,round_index,generation,due_at,state,started_at,"
                    "selected_option,question_json) VALUES (?,?,?,?,?,?,'answering',?,?,?)",
                    (run_id, plan_id, "retest" if scheduled else "early", p["round_index"], p["generation"], p["due_at"],
                     retest_mod.stamp(at), selected, json.dumps(q, ensure_ascii=False)))
        return {"ok": True, "run": _retest_run_public(dict(con.execute("SELECT * FROM retest_runs WHERE id=?", (run_id,)).fetchone()))}
    return _learning_action(action, True)


@app.get("/api/retest/runs/{run_id}")
def retest_run_one(run_id: str):
    def action(con):
        row = con.execute("SELECT * FROM retest_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise retest_mod.RetestError("复测轮次不存在。")
        return {"ok": True, "run": _retest_run_public(dict(row))}
    return _learning_action(action)


@app.post("/api/retest/runs/{run_id}/lock")
def retest_lock(run_id: str, payload: dict):
    def action(con):
        row = con.execute("SELECT * FROM retest_runs WHERE id=?", (run_id,)).fetchone()
        if not row or row["state"] not in ("answering", "grading"):
            raise retest_mod.RetestError("这轮复测已经结束，不能覆盖原答。")
        run = dict(row)
        answer = payload.get("answer_text")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 30000:
            raise retest_mod.RetestError("请提交 1–30000 字的本次独立作答。")
        answer = answer.strip()
        q = json.loads(run["question_json"])
        if q.get("pilot_run_id"):
            raise retest_mod.RetestError("试点复测请从试点页面锁定原答。")
        selected = _normalize_option(payload.get("selected_option"))
        if _is_elective(q.get("stem")) and (not selected or run.get("selected_option") and selected != run["selected_option"]):
            raise retest_mod.RetestError("请沿用原作答的选做分支；原分支未知时请选择本次分支。")
        if q["qtype"] == "single" and answer not in (q.get("options") or {}):
            raise retest_mod.RetestError("请选择一个有效选项。")
        if run["state"] == "grading":
            if answer != run["answer_text"] or selected != run["selected_option"]:
                raise retest_mod.RetestError("本次原答已锁定；看过答案后的改写应另作订正。")
        else:
            con.execute("UPDATE retest_runs SET state='grading',answer_text=?,selected_option=?,locked_at=?,criteria=? WHERE id=?",
                        (answer, selected, retest_mod.stamp(retest_mod.now()), retest_mod.criteria(q, selected), run_id))
        return {"ok": True, "run": _retest_run_public(dict(con.execute("SELECT * FROM retest_runs WHERE id=?", (run_id,)).fetchone()))}
    return _learning_action(action, True)


@app.post("/api/retest/runs/{run_id}/abandon")
def retest_abandon(run_id: str):
    def action(con):
        con.execute("UPDATE pilot_runs SET state='abandoned' WHERE retest_run_id=? AND state IN ('answering','locked')", (run_id,))
        con.execute("UPDATE retest_runs SET state='abandoned',note='用户结束本轮，原答保留' WHERE id=? AND state IN ('answering','grading')", (run_id,))
        return {"ok": True}
    return _learning_action(action, True)


@app.post("/api/retest/runs/{run_id}/save")
def retest_save(run_id: str, payload: dict):
    with _learning_connection() as con:
        row = con.execute("SELECT * FROM retest_runs WHERE id=?", (run_id,)).fetchone()
        if not row or not row["locked_at"]:
            return {"ok": False, "error": "请先提交并锁定本次作答。"}
        run = dict(row)
    q = json.loads(run["question_json"])
    if q.get("pilot_run_id"):
        return {"ok": False, "error": "试点复测请从试点页面保存，保留暴露及定性记录。"}
    item = {k: payload.get(k) for k in ("grade", "point_source", "grade_override")}
    hits = payload.get("hits", [])
    if not isinstance(hits, list) or len(hits) > 300 or any(isinstance(x, bool) or not isinstance(x, int) for x in hits):
        return {"ok": False, "error": "采分点格式不正确。"}
    item.update(id=q["question_id"], answer_text=run["answer_text"], selected_option=run["selected_option"], hits=hits)
    single = q["qtype"] == "single"
    body = {"date": retest_mod.local_date(retest_mod.moment(run["locked_at"])),
            "mode": "间隔复测" if run["kind"] == "retest" else "提前练习",
            "note": f"复测计划 #{run['plan_id']} / 本轮 {run_id}", "submission_id": "retest:" + run_id,
            "question_ids": [q["question_id"]], "single": [{"id":q["question_id"],"answer":run["answer_text"]}] if single else [],
            "subjective": [] if single else [item]}
    return _save_submission(body, run_id)


@app.get("/retest", response_class=HTMLResponse)
def retest_page():
    return page("retest.html")


@app.get("/pilot", response_class=HTMLResponse)
def pilot_page():
    return page("pilot.html")


def install_pilot(directory):
    """显式本地操作；不会在启动或页面读取时自动安装私有资料。"""
    return _learning_action(lambda con: pilot_mod.install(con, directory), True)


def install_learning(directory):
    """全科资料及其批次在同一事务中安装，重复版本只复用，不覆盖。"""
    def action(con):
        result = curriculum_mod.install(con, directory)
        row = con.execute("SELECT bundle_json FROM curriculum_packages WHERE version=?", (result["version"],)).fetchone()
        data = json.loads(row[0])
        batches = []
        root = Path(directory).resolve()
        for b in data.get("batches", []):
            path = (root / b["relative_path"]).resolve()
            if not path.is_relative_to(root):
                raise retest_mod.RetestError("批次目录越界。")
            batches.append(pilot_mod.install(con, path))
        return {"ok": True, "version": result["version"], "reused": result.get("reused", False), "batches": batches}
    return _learning_action(action, True)


@app.get("/coverage", response_class=HTMLResponse)
def coverage_page():
    return page("coverage.html")


@app.get("/api/curriculum")
def curriculum_overview():
    return _learning_action(curriculum_mod.overview)


@app.post("/api/curriculum/preview")
def curriculum_preview(payload: dict):
    return _learning_action(lambda con: curriculum_mod.preview(con, payload.get("question_id")), True)


@app.post("/api/curriculum/practice")
def curriculum_practice(payload: dict):
    def action(con):
        target = curriculum_mod.practice_target(con, payload.get("question_id"))
        return {"ok": True, "run": pilot_mod.start(con, target)}
    return _learning_action(action, True)


@app.post("/api/pilot/feedback")
def pilot_feedback(payload: dict):
    return _learning_action(lambda con: pilot_mod.feedback(con, payload), True)


@app.get("/api/pilot")
def pilot_dashboard():
    return _learning_action(pilot_mod.dashboard)


@app.get("/api/pilot/evidence")
def pilot_evidence(version: str):
    return _learning_action(lambda con: pilot_mod.evidence(con, version))


@app.post("/api/pilot/runs/{run_id}/effort")
def pilot_effort(run_id: str, payload: dict):
    def action(con):
        r = pilot_mod.one(con, "pilot_runs", "id", run_id)
        seconds = payload.get("reading_seconds")
        if type(seconds) not in (int, float) or not 0 <= seconds <= 86400:
            raise retest_mod.RetestError("阅读用时格式不正确。")
        meta = json.loads(r["meta_json"])
        meta.setdefault("timing", {})["reading_seconds"] = max(meta.get("timing", {}).get("reading_seconds", 0), seconds)
        con.execute("UPDATE pilot_runs SET meta_json=? WHERE id=?", (pilot_mod.encode(meta), run_id))
        return {"ok": True}
    return _learning_action(action, True)


@app.post("/api/pilot/start")
def pilot_start(payload: dict):
    return _learning_action(lambda con: {"ok": True, "run": pilot_mod.start(con, payload)}, True)


@app.get("/api/pilot/runs/{run_id}")
def pilot_run(run_id: str):
    return _learning_action(lambda con: {"ok": True, "run": pilot_mod.public_run(con, pilot_mod.one(con, "pilot_runs", "id", run_id))})


@app.post("/api/pilot/runs/{run_id}/draft")
def pilot_draft(run_id: str, payload: dict):
    return _learning_action(lambda con: pilot_mod.draft(con, run_id, payload), True)


@app.post("/api/pilot/runs/{run_id}/hint")
def pilot_hint(run_id: str, payload: dict):
    return _learning_action(lambda con: pilot_mod.hint(con, run_id, payload), True)


@app.post("/api/pilot/runs/{run_id}/lock")
def pilot_lock(run_id: str, payload: dict):
    return _learning_action(lambda con: {"ok": True, "run": pilot_mod.lock(con, run_id, payload)}, True)


@app.post("/api/pilot/runs/{run_id}/save")
def pilot_save(run_id: str):
    # 客户端不能替换已锁定的题目、原答、评分来源或数值分账。
    return _save_submission({}, pilot_run_id=run_id)

@app.post("/api/pilot/runs/{run_id}/grade")
def pilot_grade(run_id: str):
    """覆盖/试点练习的 AI 详细批改。

    只在「锁定并保存原答」之后调用，结果写回同一条 attempt，不新建练习记录。
    复用普通练习的同一套批改与逐点对账；分值待核的题明确拒绝，不猜满分。
    """
    with _learning_connection() as con:
        r = pilot_mod.one(con, "pilot_runs", "id", run_id)
        if r["state"] != "saved":
            raise retest_mod.RetestError("请先锁定并保存原答，再批改。")
        aid, qid = r["attempt_id"], r["materialized_qid"]
        if not aid or not qid:
            raise retest_mod.RetestError("本次作答还没有保存，无法批改。")
        q = con.execute("SELECT stem,extra,qtype,number,year,source FROM questions WHERE id=?",
                        (qid,)).fetchone()
        if not q:
            raise retest_mod.RetestError("题目已不在题库。")
        qex = _json_or(q["extra"], {}) or {}
        pts = [dict(x) for x in con.execute(
            f"SELECT {POINT_COLS} FROM points WHERE question_id=? ORDER BY seq", (qid,))]
        answer = r["answer_text"] or ""
        old = con.execute("SELECT id,grade FROM attempts WHERE id=?", (aid,)).fetchone()
        if not old:
            raise retest_mod.RetestError("找不到本次作答记录。")
        if old["grade"]:
            reused = _json_or(old["grade"], None)
            if isinstance(reused, dict) and reused.get("score") is not None:
                return {"ok": True, "reused": True, "attempt_id": aid,
                        "score": reused.get("score"), "full_score": reused.get("full_score")}
    if not pts:
        return {"ok": False, "error": "本题没有采分点，暂时无法给出实得分。"}
    full = qex.get("full_score")
    if not isinstance(full, (int, float)) or isinstance(full, bool) or full <= 0:
        return {"ok": False, "error": "本题分值待核，暂不折合总分。"}
    data, usage, chk, cap_note, retry_info, err = _grade_with_reconcile(
        q, pts, full, sections_of(qex), qex.get("answer_note"), answer, None)
    if err:
        return err
    _ground_grade_feedback(data, answer)
    data["verification"] = {k: chk.get(k) for k in
                            ("status", "errors", "sum_awards", "extra", "delta", "expected")} if chk else None
    if cap_note:
        data["judgment_cap_applied"] = cap_note
    if retry_info:
        data["retry"] = retry_info
    with _learning_connection(True) as con:
        con.execute("UPDATE attempts SET grade=?, point_source=? WHERE id=?",
                    (json.dumps(data, ensure_ascii=False), "ai_unconfirmed", aid))
    return {"ok": True, "attempt_id": aid, "score": data.get("score"), "full_score": full,
            "verification": (chk or {}).get("status"), "usage": ai_mod.usage_report(usage)}


@app.post("/api/pilot/runs/{run_id}/confirm-score")
def pilot_confirm_score(run_id: str):
    """用户确认这次 AI 分账：标记 ai_confirmed，并按逐点实得写入命中。"""
    def action(con):
        r = pilot_mod.one(con, "pilot_runs", "id", run_id)
        if not r["attempt_id"]:
            raise retest_mod.RetestError("本次作答还没有保存。")
        a = con.execute("SELECT id,question_id,grade FROM attempts WHERE id=?",
                        (r["attempt_id"],)).fetchone()
        if not a or not a["grade"]:
            raise retest_mod.RetestError("还没有可确认的批改。")
        g = _json_or(a["grade"], {})
        if not isinstance(g, dict):
            raise retest_mod.RetestError("批改结果不完整。")
        g2, ov, _why = _attach_grade_override(g, a["question_id"], con=con)
        if ov:
            g = g2
        con.execute("UPDATE attempts SET grade=?, point_source='ai_confirmed' WHERE id=?",
                    (json.dumps(g, ensure_ascii=False), a["id"]))
        for aw in g.get("point_awards") or []:
            seq = aw.get("seq")
            if seq is None or float(aw.get("awarded") or 0) <= 0:
                continue
            pid = con.execute("SELECT id FROM points WHERE question_id=? AND seq=?",
                              (a["question_id"], seq)).fetchone()
            if pid:
                con.execute("INSERT OR REPLACE INTO point_hits (attempt_id,point_id,hit) VALUES (?,?,1)",
                            (a["id"], pid["id"]))
        return {"ok": True, "score": g.get("score"), "full_score": g.get("full_score"),
                "override": bool(ov)}
    return _learning_action(action, True)


@app.post("/api/pilot/runs/{run_id}/abandon")
def pilot_abandon(run_id: str):
    return _learning_action(lambda con: pilot_mod.abandon(con, run_id), True)


@app.post("/api/pilot/runs/{run_id}/reference")
def pilot_reference(run_id: str):
    return _learning_action(lambda con: pilot_mod.reference(con, run_id), True)


@app.get("/api/pilot/runs/{run_id}/segments/{segment_index}")
def pilot_segment(run_id: str, segment_index: int):
    def action(con):
        r = pilot_mod.one(con, "pilot_runs", "id", run_id)
        pilot_mod.can_reference(con, r)
        p = pilot_mod.one(con, "pilot_packages", "version", r["package"])
        knowledge = pilot_mod.select_knowledge(json.loads(p["knowledge_json"]), pilot_mod.question(r)["topic_slot"])
        segments = [s for k in knowledge for s in k.get("original_segments", [])]
        if not 0 <= segment_index < len(segments):
            raise retest_mod.RetestError("原文片段不存在。")
        root = Path(p["root"]).resolve()
        relative = segments[segment_index]["crop_file"]
        path = (root / relative).resolve()
        expected = segments[segment_index].get("crop_sha256")
        if not path.is_relative_to(root) or path.suffix.lower() != ".png" or not expected:
            raise retest_mod.RetestError("原文片段路径或清单不合法。")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise retest_mod.RetestError("原文片段已变化，须重新审校。")
        con.execute("UPDATE pilot_runs SET feedback_at=? WHERE id=?", (retest_mod.stamp(retest_mod.now()), run_id))
        return Response(data, media_type="image/png", headers={**NO_CACHE, "Cache-Control": "no-store"})
    return _learning_action(action, True)


# 同一原答的显式 AI 诊断不并发付费；已有结果默认复用。实际外部调用在事务之外。
_pilot_diagnostic_lock = _threading.Lock()
_pilot_diagnostic_busy = set()


@app.post("/api/pilot/runs/{run_id}/diagnose")
def pilot_diagnose(run_id: str):
    with _pilot_diagnostic_lock:
        if run_id in _pilot_diagnostic_busy:
            return {"ok": False, "error": "本次诊断正在进行，请稍后刷新。"}
        _pilot_diagnostic_busy.add(run_id)
    try:
        with _learning_connection() as con:
            r = pilot_mod.one(con, "pilot_runs", "id", run_id)
            if r["state"] != "saved":
                raise retest_mod.RetestError("请先保存锁定原答，再请求诊断。")
            pilot_mod.can_reference(con, r)
            old = con.execute("SELECT id FROM pilot_diagnoses WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
            if old:
                return {"ok": True, "diagnosis_id": old[0], "reused": True}
        q = pilot_mod.question(r)
        trace = {}
        limits = ai_mod.grade_limits(ai_mod.resolve_thinking(scenario="grade")["thinking"])
        data, usage = ai_mod.chat_json([
            {"role": "system", "content": pilot_mod.DIAGNOSTIC_PROMPT},
            {"role": "user", "content": pilot_mod.encode({"question": q["question_text"], "reference": pilot_mod.active_card(r),
                                                         "selected_option": json.loads(r["meta_json"]).get("selected_option"),
                                                         "criterion": q["completion_criterion"], "answer": r["answer_text"]})}],
            scenario="grade", **limits, trace=trace)
        validated = pilot_mod.validate_diagnosis(data, r)
        evaluator = {"prompt_version": pilot_mod.PROMPT_VERSION, "criteria": pilot_mod.question_criteria(q, json.loads(r["meta_json"]).get("selected_option")),
                     "prompt_sha256": hashlib.sha256(pilot_mod.DIAGNOSTIC_PROMPT.encode()).hexdigest(),
                     "model": trace.get("model_echo") or trace.get("model"), "usage": usage,
                     "stage_blinded": True}
        with _learning_connection(True) as con:
            pilot_mod.can_reference(con, r)
            when = retest_mod.stamp(retest_mod.now())
            cur = con.execute("INSERT INTO pilot_diagnoses (run_id,content_json,evaluator_json,created_at) VALUES (?,?,?,?)",
                              (run_id, pilot_mod.encode(validated), pilot_mod.encode(evaluator), when))
            con.execute("UPDATE pilot_runs SET feedback_at=? WHERE id=?", (when, run_id))
            return {"ok": True, "diagnosis_id": cur.lastrowid}
    except retest_mod.RetestError as error:
        return {"ok": False, "error": str(error)}
    except Exception:
        return {"ok": False, "error": "诊断未完成，已保存的原答不受影响；可稍后重试。"}
    finally:
        with _pilot_diagnostic_lock:
            _pilot_diagnostic_busy.discard(run_id)


@app.post("/api/pilot/diagnoses/{diagnosis_id}/feedback")
def pilot_diagnosis_feedback(diagnosis_id: int, payload: dict):
    def action(con):
        d = pilot_mod.one(con, "pilot_diagnoses", "id", diagnosis_id)
        pilot_mod.can_reference(con, pilot_mod.one(con, "pilot_runs", "id", d["run_id"]))
        status = payload.get("status")
        if status not in ("suggested", "disputed", "corrected"):
            raise retest_mod.RetestError("请选择待核或修正状态。")
        note = pilot_mod.text(payload.get("note", ""), 4000, status == "corrected")
        con.execute("UPDATE pilot_diagnoses SET status=?,user_note=? WHERE id=?", (status, note, diagnosis_id))
        return {"ok": True}
    return _learning_action(action, True)


def _try_link_orphans(session_id=None):
    """把「还没挂 session」的历史作答补挂到某一次练习上。

    只用于**兼容早期数据**（那时 attempts 还没有 session_id 列）：
    只在你要给那次练习生成报告、而它一条明细都没有时才补，
    匹配口径是「同一天 + 同一模式」，绝不乱挂到别次练习上。
    """
    ensure_schema()
    if session_id:
        s = rows("SELECT * FROM sessions WHERE id=?", (session_id,))
    else:
        s = rows("SELECT * FROM sessions ORDER BY id DESC LIMIT 1")
    if not s:
        return 0
    sess = s[0]
    orphans = rows(
        "SELECT id FROM attempts WHERE session_id IS NULL AND date=? AND mode=?",
        (sess["date"], sess["mode"]))
    if not orphans:
        return 0
    con = sqlite3.connect(DB)
    try:
        con.executemany("UPDATE attempts SET session_id=? WHERE id=?",
                        [(sess["id"], a["id"]) for a in orphans])
        con.commit()
    finally:
        con.close()
    return len(orphans)


def _build_metrics(sess, attempts, weak_snapshot, history=None):
    """本次练习的量化指标快照（报告与趋势图都用它，保证报告有数字可依据）。

    主观题部分**同时给两种口径**，因为它们的意义不同：
      · `point_rate`（命中率）= 一次作答 × 一个采分点 = 1 样本 —— 用于**薄弱点聚合**；
      · `subjective_score` / `subjective_full`（折合分）= 命中点的分值之和 —— 这才是
        **学生真正关心的那个数字**。以前只有命中率，报告里说"你采分点命中 50%"，
        学生换算不出"这题得了 7.5 分"。

    2026-10-08 起折合分**不再在这里自己算**，而是交给 `score_attempts()`（唯一口径）：
    这里的旧实现读 `p.get("hit")`，可 `points_of_question()` 从不返回 `hit` 字段，
    于是折合分恒为 0 —— 2026-10-07 那两道应是 29/30，报告里存的却是 0/30。
    """
    single = [a for a in attempts if a["qtype"] == "single"]
    subj = [a for a in attempts if a["qtype"] != "single"]
    sr = sum(1 for a in single if a["hits"] == 1)

    pts_by_att = _attempt_points([a["id"] for a in subj])
    detail, sc = score_attempts(subj, pts_by_att)

    metrics = {
        "metrics_version": 2,          # 旧快照没有这个字段 → 页面标「正文基于旧口径」
        "session_id": sess["id"],
        "date": sess["date"],
        "mode": sess["mode"],
        "objective_total": len(single),
        "objective_right": sr,
        "objective_score": sr * 2,
        "objective_rate": round(sr / len(single), 3) if single else None,
        # 主观题的两种口径 + 三笔分账，全部来自同一份计算
        "subjective_count": len(subj),
        "point_hits": sc["point_hits"],
        "point_total": sc["point_total"],
        "point_rate": sc["point_rate"],
        "subjective_score": sc["subjective_score"],
        "subjective_full": sc["subjective_full"],
        "subjective_source": sc["subjective_source"],
        "subjective_scored_n": sc["subjective_scored_n"],
        "confirmed_point_score": sc["confirmed_point_score"],
        "confirmed_point_full": sc["confirmed_point_full"],
        "confirmed_n": sc["confirmed_n"],
        "legacy_point_score": sc["legacy_point_score"],
        "legacy_point_full": sc["legacy_point_full"],
        "legacy_n": sc["legacy_n"],
        "ai_reference_score": sc["ai_reference_score"] if sc["ai_reference_n"] else None,
        "ai_reference_full": sc["ai_reference_full"] if sc["ai_reference_n"] else None,
        "ai_reference_n": sc["ai_reference_n"],
        # 其中「人工确认过」的条数（设计 D）：可信参考分的成色要说得出
        "ai_reference_manual_n": sc.get("ai_reference_manual_n", 0),
        "unscored_n": sc["unscored_n"],
        "unscored_reasons": sc["unscored_reasons"],
        # 逐题明细：**原样存 `score_attempts()` 的那一份**，与报告读取路径同一个形状。
        # 以前这里另做了一个 {qtype,node,got,full,hit_n,point_n,...} 的映射版本，
        # 于是"生成时算的正文依据"和"读取时算的正文依据"字段对不上，指纹永远不相等。
        "subjective_detail": detail,
        "weak_points": [
            {"name": w.get("name"), "path": w.get("path"),
             "hit": w.get("hit"), "total": w.get("total"), "rate": w.get("rate")}
            for w in weak_snapshot[:10]
        ],
    }
    return metrics


@app.post("/api/review/generate")
def review_generate(payload: dict):
    """给某一次练习生成 AI 复盘报告，并入库。

    payload = {"session_id": 1, "save": true}

    只读用真实库也不会出问题；写测试请用 DSH_DB 指向副本（见 docs/交接文档.md §5.1）。
    """
    ensure_schema()
    sid = payload.get("session_id")
    if not sid:
        latest = rows("SELECT id FROM sessions ORDER BY id DESC LIMIT 1")
        if not latest:
            return {"ok": False, "error": "还没有练习记录。先提交一次模拟或日常练习。"}
        sid = latest[0]["id"]

    s = rows("SELECT * FROM sessions WHERE id=?", (sid,))
    if not s:
        return {"ok": False, "error": f"练习记录 #{sid} 不存在"}
    sess = s[0]

    attempts = _session_attempts(sid)
    legacy = False
    if not attempts:
        # 兼容早期数据（那时还没有 session_id 列）：同一天 + 同一模式才补挂
        if _try_link_orphans(sid):
            attempts = _session_attempts(sid)
            legacy = True
    if not attempts:
        return {"ok": False, "error": f"练习 #{sid} 没有找到作答记录，无法生成报告。"}
    # 题目被删/不存在时仍能出报告，但要在 metrics 里标出来，避免明细静默变少
    missing_q = sum(1 for a in attempts if not a.get("qtype"))

    snap_info = parse_weak_snapshot(sess.get("weak_snapshot"))
    snap = snap_info["items"]
    # 快照缺失（早期数据）时现算一份，保证报告能点名考点
    if not snap:
        snap_info = weak_snapshot_payload()
        snap = snap_info["items"]

    # 历史：**现算值**（复审问题 4）。以前这里直接读 sessions 表的 point_hits/point_total，
    # 那是发卷当刻的快照 —— session 3 存着 3/14、明细现算是 3/21，AI 拿到的事实自相矛盾。
    sessions_by_id = {x["id"]: x for x in rows("SELECT * FROM sessions")}
    all_scores = _session_scores()
    history = report_history(sid, sessions_by_id, all_scores)

    # 先算指标（依赖原始 qtype 值），再换成中文题型给 AI 看 —— 顺序反过来会算出全 0（踩过）
    metrics = _build_metrics(sess, attempts, snap, history=history)
    metrics["legacy_match"] = legacy          # 明细是按「日期+模式」补挂的早期数据
    metrics["history_count"] = len(history)
    metrics["missing_question"] = missing_q   # 明细里题目已不存在（被删）的条数
    metrics["weak_snapshot_version"] = snap_info["version"]
    metrics["weak_source_counts"] = snap_info["source_counts"]
    metrics["weak_legacy_items"] = snap_info["legacy_items"][:6]
    # 正文依据指纹：正文写完之后用来判"依据有没有变"（复审问题 3 / 2）。
    # 逐条作答事实用**同一次查询**取（客观+主观），生成与读取才会得到同一枚指纹。
    metrics["content_basis"] = content_basis(
        sess, metrics, history, metrics["weak_points"], session_attempt_basis(sid))

    review_context = _review_context(sess, attempts)
    metrics["review_version"] = review_mod.VERSION
    metrics["review_basis"] = review_context["basis"]
    metrics["review_snapshot"] = review_context["prompt"]

    for a in attempts:
        a["qtype"] = {"single": "单选", "analysis": "辨析", "short": "简答",
                      "essay": "分析论述"}.get(a["qtype"], a["qtype"] or "题目已不存在")

    # 把算好的指标**真正交给 AI**（设计 §2.1）。以前只把 metrics 存进 reports.metrics，
    # 而 `ai.review_session()` 读的是 `session.get("subjective_score")` —— sess 是 sessions
    # 表原样的一行，没有这些 key，于是"主观题折合得分 X/Y"那一段**从来没进过提示词**，
    # 报告正文因此永远不提学生真正关心的那个分数。这里把两份合成一份交过去，
    # 不改 `review_session()` 的签名（它本来就按"带这些 key 的 session 字典"读）。
    sess_view = {**sess, **metrics, "review_context": review_context["prompt"]}
    try:
        content, usage = ai_mod.review_session(sess_view, attempts, snap, history)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---- 拿到正文先验一遍：**截断/空的正文绝不入库**（2026-10-10 事故）----
    # 复盘原来是把返回值直接存库，不看 `finish_reason`、也不看正文是否为空。
    # 于是「复盘写死 1500 预算 + 用户把思考开到 max」这一组合下，推理把预算吃光、
    # 正文 0 字符，页面上就出现了一份「学习报告（202」的空报告（report 11 = 0 字符、
    # report 10 = 10 字符），而且**旧的好报告会被它覆盖掉**。
    # 现在：报错说清病因与怎么救，**不写库**（已有的报告原样保留）。
    content = (content or "").strip()
    usage = usage or {}
    truncated = usage.get("finish_reason") == "length"
    if not content or truncated:
        why = "正文被 token 上限截断" if truncated else "模型没有返回正文"
        return {
            "ok": False, "truncated": truncated, "content": content,
            "error": (f"复盘{why}（finish_reason={usage.get('finish_reason')}）："
                      f"这次调用用掉输出 {usage.get('completion_tokens')} tokens"
                      f"（其中推理 {usage.get('reasoning_tokens')}，推理也算在里面）。"
                      f"多半是「思考」把预算吃完了 —— 到 /settings 的「学习复盘报告」里"
                      f"调高「预算（开思考）」，或把该场景的思考关掉，然后重新生成。"
                      f"这次没有写入数据库，原有的报告没有被覆盖。"),
            "usage": usage, "usage_info": ai_mod.usage_report(usage),
        }

    report_id = None
    if payload.get("save", True):
        ex = rows("SELECT id FROM reports WHERE session_id=?", (sid,))
        if ex:
            exec_sql("UPDATE reports SET content=?, metrics=?, date=?, mode=? WHERE id=?",
                     (content, json.dumps(metrics, ensure_ascii=False),
                      sess["date"], sess["mode"], ex[0]["id"]))
            report_id = ex[0]["id"]
        else:
            report_id = exec_sql(
                "INSERT INTO reports (session_id,date,mode,content,metrics,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (sid, sess["date"], sess["mode"], content,
                 json.dumps(metrics, ensure_ascii=False),
                 datetime.now().isoformat(timespec="seconds")))

    return {"ok": True, "report_id": report_id, "session_id": sid,
            "content": content, "metrics": metrics, "usage": usage,
            "usage_info": ai_mod.usage_report(usage)}


def _session_scores(session_ids=None):
    """现算每次练习的主观题计分汇总 → `{session_id: summary}`（口径 = `score_attempts`）。

    **为什么现算而不是只读 `reports.metrics`**：
      1. 报告只在用户点「生成」后才有 metrics，没生成报告的那几次练习在复盘页上
         就会缺「主观题得了几分」这一维 —— 而它恰恰是学生最想看的数字；
      2. 旧 metrics 里存的折合分**是错的**（`_build_metrics` 取不到 `hit`，恒 0）。
         读取时一律用现算值覆盖**对外展示**的数字；库里的旧 metrics 与正文原样保留，
         不批量改写（设计 §2.1：让用户自己决定要不要重新生成）。

    按 session 分组、组内再按 attempt 逐条算 —— 不按 `session_id + question_id` 合并，
    否则同一次练习里同题作答两次会互相覆盖（旧 `_session_subjective_scores()` 的错法）。
    """
    atts = [a for a in subjective_attempts() if a.get("session_id") is not None]
    if session_ids is not None:
        keep = set(session_ids)
        atts = [a for a in atts if a["session_id"] in keep]
    pts_by_att = _attempt_points([a["id"] for a in atts])
    by_sid = {}
    for a in atts:
        by_sid.setdefault(a["session_id"], []).append(a)
    return {sid: score_attempts(group, pts_by_att)[1] for sid, group in by_sid.items()}


def _fresh_session_metrics(sess_row, sc=None, history=None, weak_basis=None, weak_counts=None):
    """对外展示用的**现算**指标（列表、详情、趋势共用这一份，保证三处数字一致）。

    `sess_row` 是 sessions 表的一行（客观题部分以它为准：它记的就是那一次提交的
    对/错数）；主观题部分来自 `_session_scores()`（唯一口径）。
    `history` / `weak_basis` 只用于拼 `content_basis`（正文依据指纹）——
    生成正文时与读取报告时必须用**同一份**，否则过期判断会自己骗自己。
    """
    if sc is None:
        sc = _session_scores([sess_row["id"]]).get(sess_row["id"]) or {}
    ot = sess_row.get("objective_total") or 0
    orr = sess_row.get("objective_right") or 0
    ai_n = sc.get("ai_reference_n", 0)
    return {
        "metrics_version": 2,
        "objective_total": ot,
        "objective_right": orr,
        "objective_score": sess_row.get("objective_score") or orr * 2,
        "objective_rate": round(orr / ot, 3) if ot else None,
        "point_hits": sc.get("point_hits"),
        "point_total": sc.get("point_total"),
        "point_rate": sc.get("point_rate"),
        "subjective_count": sc.get("subjective_count", 0),
        "subjective_score": sc.get("subjective_score"),
        "subjective_full": sc.get("subjective_full"),
        "subjective_source": sc.get("subjective_source", "unscored"),
        "subjective_scored_n": sc.get("subjective_scored_n", 0),
        "confirmed_point_score": sc.get("confirmed_point_score"),
        "confirmed_point_full": sc.get("confirmed_point_full"),
        "confirmed_n": sc.get("confirmed_n", 0),
        "legacy_point_score": sc.get("legacy_point_score"),
        "legacy_point_full": sc.get("legacy_point_full"),
        "legacy_n": sc.get("legacy_n", 0),
        # 只有"新版分账且对账通过"的 AI 分数才给值；一笔都没有时是 None 而不是 0
        "ai_reference_score": sc.get("ai_reference_score") if ai_n else None,
        "ai_reference_full": sc.get("ai_reference_full") if ai_n else None,
        "ai_reference_n": ai_n,
        "ai_reference_manual_n": sc.get("ai_reference_manual_n", 0),
        "unscored_n": sc.get("unscored_n", 0),
        "unscored_reasons": sc.get("unscored_reasons", []),
        "unscored_kinds": sc.get("unscored_kinds") or {},
        "subjective_detail": sc.get("subjective_detail", []),
        "weak_source_counts": weak_counts,
    }


def _metrics_with_basis(sess_row, sc, history, weak_points, weak_counts=None, attempts_basis=None):
    """现算指标 + **正文依据指纹**（生成与读取两条路都走它，指纹才可比）。"""
    m = _fresh_session_metrics(sess_row, sc, history, weak_points, weak_counts)
    m["content_basis"] = content_basis(sess_row, sc, history, weak_points, attempts_basis)
    return m


def _content_stale(stored, fresh):
    """这份报告的正文还能不能信？→ `(是否过期, 原因)`

    原因分三种，**页面提示要分开写**（复审问题 3）：
      · `legacy`        缺 `metrics_version` 或 < 2 —— 生成于旧算法，当时的折合分是错的；
      · `unknown_basis` 是 v2 但没存正文依据指纹（本批修正之前的 v2 正文）——
                       没法核对，保守标成待更新；
      · `basis_changed` 指纹对不上 —— 正文依据的**逐条作答/来源/分支/历史**变了。

    比较的是 `content_basis()` 那个可复核的指纹，而不是"再比一遍总分"：
    来源从"来源不明"改成"已确认"、命中点从 1/3 换成 2/4，总分都没变，旧比法看不出来。
    **不在浏览报告时自动调用付费 AI**，也不批量改写旧作答与旧 metrics。
    """
    if not isinstance(stored, dict):
        return True, "legacy"
    ver = stored.get("metrics_version")
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 2:
        return True, "legacy"
    if not isinstance(stored.get("content_basis"), dict):
        return True, "unknown_basis"
    if stored.get("content_basis") != (fresh or {}).get("content_basis"):
        return True, "basis_changed"
    if stored.get("review_basis") and stored["review_basis"] != (fresh or {}).get("review_basis"):
        return True, "basis_changed"
    return False, None


@app.get("/api/reports")
def reports_list(limit: int = 200):
    """复盘报告列表 + 总体趋势。含「已练过但还没生成报告」的练习，供前端补生成。

    主观题数字一律**现算**（`_session_scores()`，唯一口径）覆盖对外展示；旧
    `reports.metrics` 原样从库里读出来放进 `metrics_stored`，供对照与审查。
    """
    ensure_schema()
    session_scores = _session_scores()
    # 历史与"当时"的判断都基于同一份 sessions 行 + 现算值（`report_history`）
    all_sessions = {s["id"]: s for s in rows("SELECT * FROM sessions")}

    items = []
    # session_id / date / mode 一律取 **sessions 的**，不取 reports 的：这里是 LEFT JOIN，
    # 还没生成报告的那次 `r.*` 全是 NULL —— 复盘页那张卡片会没有日期，更要命的是
    # `session_id` 为空会让「生成 AI 复盘」按钮把报告生成到**错误的那一次练习**上
    # （后端对空 session_id 的兜底是"取最近一次"）。这两个 bug 都被
    # "点按钮时它恰好就是最近一次练习"和"所有练习都已生成报告"的数据掩盖了，
    # 直到在真 clone 上跑前端验收才暴露。
    for r in rows(
        "SELECT r.id AS report_id, s.id AS session_id, s.date AS date, s.mode AS mode, "
        "       r.content, r.metrics, r.created_at, "
        "       s.objective_total, s.objective_right, s.objective_score, "
        "       s.subjective_count, s.point_hits, s.point_total, s.note, s.weak_snapshot "
        "FROM sessions s LEFT JOIN reports r ON r.session_id = s.id "
        "ORDER BY s.id DESC LIMIT ?", (limit,)):
        try:
            stored = json.loads(r["metrics"] or "null")
        except Exception:
            stored = None
        snap = parse_weak_snapshot(r.get("weak_snapshot"))
        r["weak_snapshot"] = snap["items"]
        r["weak_snapshot_version"] = snap["version"]
        r["weak_snapshot_legacy"] = snap["legacy_shape"]
        r["weak_snapshot_counts"] = snap["source_counts"]
        sc = session_scores.get(r["session_id"]) or {}
        # 正文依据里的"薄弱点"用**存下来的那份**（它是当时的产物、无法重算）；
        # 没有报告时用当前快照，反正那时 content_stale 是 None。
        weak = weak_basis_of(stored) if isinstance(stored, dict) else snap["items"]
        r["metrics"] = _metrics_with_basis(
            r, sc, report_history(r["session_id"], all_sessions, session_scores), weak,
            weak_counts=snap["source_counts"],
            attempts_basis=session_attempt_basis(r["session_id"]))
        r["metrics_stored"] = stored                   # 旧快照原样留着，便于对照审查
        review_context = _review_context(all_sessions[r["session_id"]]) if r["report_id"] else None
        r["question_refs"] = review_context["question_refs"] if review_context else {}
        if isinstance(stored, dict) and stored.get("review_basis"):
            r["metrics"]["review_basis"] = review_context["basis"]
        stale, kind = _content_stale(stored, r["metrics"]) if stored is not None else (None, None)
        r["content_stale"] = stale
        r["content_stale_kind"] = kind
        r["subjective_score"] = sc.get("subjective_score")
        r["subjective_full"] = sc.get("subjective_full")
        r["subjective_source"] = sc.get("subjective_source", "unscored")
        r["subjective_scored_n"] = sc.get("subjective_scored_n", 0)
        r["subjective_n"] = sc.get("subjective_count", 0)
        items.append(r)

    trend = []
    for r in reversed([x for x in items if x["report_id"]]):
        m = r["metrics"] or {}
        conf_full, leg_full = m.get("confirmed_point_full") or 0, m.get("legacy_point_full") or 0
        ai_full = m.get("ai_reference_full") or 0
        trend.append({
            "report_id": r["report_id"], "session_id": r["session_id"],
            "date": r["date"], "mode": r["mode"],
            "objective_rate": m.get("objective_rate"),
            "point_rate": m.get("point_rate"),
            # 兼容值：折合得分率 =（已确认 + 旧来源不明）/ 对应满分
            "subjective_rate": (round(r["subjective_score"] / r["subjective_full"], 3)
                                if r.get("subjective_full") else None),
            # 来源标记：三者不能画成同一条"成绩线"，页面按来源分别呈现
            "subjective_source": m.get("subjective_source"),
            "confirmed_point_rate": (round(m["confirmed_point_score"] / conf_full, 3)
                                     if conf_full else None),
            "legacy_point_rate": (round(m["legacy_point_score"] / leg_full, 3)
                                  if leg_full else None),
            "ai_reference_rate": (round(m["ai_reference_score"] / ai_full, 3)
                                  if ai_full else None),
            "confirmed_point_score": m.get("confirmed_point_score"),
            "confirmed_point_full": m.get("confirmed_point_full"),
            "legacy_point_score": m.get("legacy_point_score"),
            "legacy_point_full": m.get("legacy_point_full"),
            "ai_reference_score": m.get("ai_reference_score"),
            "ai_reference_full": m.get("ai_reference_full"),
            "ai_reference_manual_n": m.get("ai_reference_manual_n") or 0,
            "content_stale": r.get("content_stale"),
        })

    # 平均正确率按全部练习算（没生成报告的那次也算，否则会与「累计答对」对不上）
    rates = []
    for x in items:
        m = x["metrics"] or {}
        v = m.get("objective_rate")
        if v is None and x["objective_total"]:
            v = round((x["objective_right"] or 0) / x["objective_total"], 3)
        if v is not None:
            rates.append(v)
    summary = {
        "sessions": len(items),
        "reports": sum(1 for x in items if x["report_id"]),
        "pending": sum(1 for x in items if not x["report_id"]),
        "avg_objective_rate": round(sum(rates) / len(rates), 3) if rates else None,
        "total_objective": sum(x["objective_total"] or 0 for x in items),
        "total_objective_right": sum(x["objective_right"] or 0 for x in items),
        "stale_contents": sum(1 for x in items if x.get("content_stale")),
    }
    return {"items": items, "trend": trend, "summary": summary}


@app.get("/api/reports/{report_id}")
def report_one(report_id: int):
    """单份报告详情：正文原样，数字**现算**（旧 metrics 原样放进 `metrics_stored`）。"""
    r = rows("SELECT * FROM reports WHERE id=?", (report_id,))
    if not r:
        return JSONResponse({"ok": False, "error": "报告不存在"}, status_code=404)
    out = r[0]
    try:
        stored = json.loads(out["metrics"] or "null")
    except Exception:
        stored = None
    fresh = None
    out["question_refs"] = {}
    if out.get("session_id") is not None:
        srow = rows("SELECT * FROM sessions WHERE id=?", (out["session_id"],))
        if srow:
            review_context = _review_context(srow[0])
            out["question_refs"] = review_context["question_refs"]
            scores = _session_scores()
            all_sessions = {s["id"]: s for s in rows("SELECT * FROM sessions")}
            weak = (weak_basis_of(stored) if isinstance(stored, dict)
                    else parse_weak_snapshot(srow[0].get("weak_snapshot"))["items"])
            fresh = _metrics_with_basis(
                srow[0], scores.get(out["session_id"]) or {},
                report_history(out["session_id"], all_sessions, scores), weak,
                weak_counts=parse_weak_snapshot(srow[0].get("weak_snapshot"))["source_counts"],
                attempts_basis=session_attempt_basis(out["session_id"]))
    if fresh is not None:
        if isinstance(stored, dict) and stored.get("review_basis"):
            fresh["review_basis"] = review_context["basis"]
        out["metrics"] = fresh
        out["content_stale"], out["content_stale_kind"] = _content_stale(stored, fresh)
    else:
        out["metrics"] = stored          # 练习记录已不在 → 只能用存下来的
        out["content_stale"] = None
        out["content_stale_kind"] = None
    out["metrics_stored"] = stored
    return {"ok": True, "report": out}


@app.delete("/api/reports/{report_id}")
def report_delete(report_id: int):
    """删报告（只删报告，不动作答记录与练习日志）。"""
    ensure_schema()
    exec_sql("DELETE FROM reports WHERE id=?", (report_id,))
    return {"ok": True}


@app.get("/reports", response_class=HTMLResponse)
def reports_page():
    """学习复盘：报告列表 + 趋势。"""
    return page("reports.html")


@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    """AI 模型与参数：看当前用什么模型、改参数、按场景试跑。

    这一页是**唯一**该去改模型的地方：以前改模型要手改 config.json（含明文密钥）
    并重启服务，而且换模型后金额估算会静默沿用旧价目。
    """
    return page("settings.html")
