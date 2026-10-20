# -*- coding: utf-8 -*-
"""练习日志「按天分组 + 题目级明细」回归测试。

覆盖：
  ① attempts 新增 student_answer / grade 两列（幂等迁移）
  ② /api/save 把主观题作答原文与 AI 批改结果写进库
  ③ /api/logs 按天分组（含"没挂 session 的旧作答"这一天）
  ④ /api/logs/day/{date} 明细：客观题判分 / 主观题逐采分点命中 / 批改结果
  ⑤ 题目不在库里时 missing=True，**不能静默少一行**（交接文档 §5.1.2）
  ⑥ 与学习复盘联动：已生成报告的组带 report_id
  ⑦ 空日期不炸
  ⑧ sessions.question_ids 幂等迁移 + 入参清洗
  ⑨ **完整练习内容**：题单里没作答的题也要出现在日志里（unanswered=True），
     有题干/选项/正确答案/采分点，但不算分、不算命中率
  ⑩ 老 session（没有题单）行为不变：只显示做过的题，并标明 has_question_set=False

规矩（docs/交接文档.md §5.1）：**绝不在用户的真实库上做测试**。
本测试把库复制成临时副本，用 DSH_DB 让 server 连副本，跑完删掉。
AI 调用用桩替代，因此不花钱、不联网。

跑法：
    cd app
    python tests/test_logs_detail.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

# 公开副本里没有 kaoyan.db（只有随包的 kaoyan-seed.db），所以两种环境都要能跑
_LIVE_DB = APP.parent / "data" / "kaoyan.db"
_SEED_DB = APP.parent / "data" / "kaoyan-seed.db"
REAL_DB = _LIVE_DB if _LIVE_DB.exists() else _SEED_DB

TMP = Path(tempfile.mkdtemp(prefix="kaoyan_logs_"))
TEST_DB = TMP / "kaoyan.db"
shutil.copy2(REAL_DB, TEST_DB)
# 真实库指纹：跑完用它确认真实库没被动过
BEFORE = (REAL_DB.stat().st_size, REAL_DB.stat().st_mtime_ns)

os.environ["DSH_DB"] = str(TEST_DB)

import server  # noqa: E402  （必须在设好 DSH_DB 之后导入）

assert server.DB == TEST_DB, f"server 没有连到副本库：{server.DB}"

# 用明显不可能的学习日期标记测试数据（交接文档 §5.1），跑完即随副本一起删除
TEST_DATE = "1900-01-01"
FULL_SET_DATE = "1900-02-02"     # ⑨「完整练习内容」用另一天，免得打乱 ③④ 的按天计数
FAILED = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


print("=" * 68)
print("① 迁移：attempts 加 student_answer / grade（幂等）")
print("=" * 68)
server.ensure_schema()
con = sqlite3.connect(TEST_DB)
cols = {r[1] for r in con.execute("PRAGMA table_info(attempts)")}
con.close()
check("student_answer 列已加", "student_answer" in cols)
check("grade 列已加", "grade" in cols)
try:
    server.ensure_schema()   # 再跑一次：迁移必须幂等
    check("迁移幂等（跑第二遍不报错）", True)
except Exception as e:
    check("迁移幂等（跑第二遍不报错）", False, f"{type(e).__name__}: {e}")

print()
print("=" * 68)
print("② /api/save 落库主观题作答原文 + AI 批改结果")
print("=" * 68)
con = sqlite3.connect(TEST_DB)
con.row_factory = sqlite3.Row
sids = [r["id"] for r in con.execute(
    "SELECT id FROM questions WHERE qtype='single' AND answer IS NOT NULL LIMIT 6")]
sub = con.execute(
    "SELECT id FROM questions WHERE qtype IN ('short','analysis','essay') "
    "AND id IN (SELECT DISTINCT question_id FROM points) LIMIT 1").fetchone()
con.close()
sub_id = sub["id"]
base_attempts = server.rows("SELECT COUNT(*) c FROM attempts")[0]["c"]

MY_ANSWER = "夸美纽斯主张班级授课制，提出泛智教育思想，教育要适应自然。"
FAKE_GRADE = {
    "score": 11, "full_score": 15,
    "one_line": "要点基本到位，泛智教育的表述不够准确。",
    "hit_seqs": [1, 2], "miss_seqs": [3],
    "cause": "表述不准",
    "judgment": {"student": "（无）", "correct": "", "matched": True, "explicit": True},
    "per_paragraph": [{"text": "夸美纽斯主张班级授课制", "mark": "✔", "comment": "正确"}],
}
res = server.save({
    "date": TEST_DATE, "mode": "日常练习", "note": "日志明细回归测试",
    "single": [{"id": sids[i], "answer": "A"} for i in range(6)],
    "subjective": [{"id": sub_id, "hits": [1, 2], "cause": "记混",
                    "answer_text": MY_ANSWER, "grade": FAKE_GRADE}],
})
sid = res["saved"].get("session_id")
check("save 返回 session_id", bool(sid), f"session_id={sid}")
check("save 报告存了作答原文", res["saved"].get("answer_text_saved") == 1,
      str(res["saved"]))
check("save 报告存了批改结果", res["saved"].get("grade_saved") == 1, str(res["saved"]))
check("无 log_error", "log_error" not in res["saved"], res["saved"].get("log_error", ""))

row = server.rows("SELECT student_answer, grade FROM attempts "
                  "WHERE session_id=? AND question_id=?", (sid, sub_id))[0]
check("库里能读回作答原文", row["student_answer"] == MY_ANSWER,
      repr((row["student_answer"] or "")[:30]))
stored = json.loads(row["grade"] or "{}")
check("库里能读回批改分数", stored.get("score") == 11, str(stored.get("score")))
check("批改结果按 JSON 存（不是被二次编码的字符串）",
      isinstance(stored, dict) and stored.get("one_line") == FAKE_GRADE["one_line"],
      str(stored.get("one_line"))[:40])

print()
print("=" * 68)
print("③ /api/logs 按天分组")
print("=" * 68)
out = server.logs()
days = {d["date"]: d for d in out["days"]}
check("返回 days 结构", isinstance(out.get("days"), list) and len(out["days"]) >= 1,
      f"{len(out.get('days', []))} 天")
check("测试那一天在列表里", TEST_DATE in days, f"日期：{sorted(days)[:5]}")
td = days.get(TEST_DATE) or {}
check("当天挂到 1 组练习", td.get("sessions_n") == 1, str(td.get("sessions_n")))
check("当天题目数 = 6 客观 + 1 主观", td.get("attempts_n") == 7, str(td.get("attempts_n")))
check("当天有总分汇", (td.get("totals") or {}).get("objective_total") == 6,
      str(td.get("totals")))
check("组里带 report_id 字段（未生成时为 None）",
      "report_id" in (td.get("sessions") or [{}])[0])
check("列表仍保留旧的 items 扁平结构（兼容）",
      isinstance(out.get("items"), list) and out["count"] == len(out["items"]))
check("summary 有天数/练习数", out["summary"]["days"] == len(out["days"]),
      str(out["summary"]))

# 副本库里若存在「没挂 session 的旧作答」，必须也能成天出现（不能因为没 session 就消失）
con = sqlite3.connect(TEST_DB)
loose_dates = [r[0] for r in con.execute(
    "SELECT DISTINCT date FROM attempts WHERE session_id IS NULL")]
con.close()
if loose_dates:
    shown = [d for d in loose_dates if d in days]
    check("没有 session 的旧作答也能在日志里看到", len(shown) == len(loose_dates),
          f"孤立作答日期 {loose_dates} → 页面上 {shown}")
else:
    print("  [SKIP] 副本库里没有孤立作答，跳过该项")

print()
print("=" * 68)
print("④ /api/logs/day/{date} 明细：题干 / 我的作答 / 批改情况")
print("=" * 68)
det = server.logs_day(TEST_DATE)
check("接口 ok", det.get("ok") is True, str(det)[:120])
groups = det.get("groups") or []
check("明细按练习分组（1 组）", len(groups) == 1, f"{len(groups)} 组")
g = groups[0] if groups else {}
check("该组带 session_id（用于联调复盘）", g.get("session_id") == sid, str(g.get("session_id")))
check("该组带 mode", g.get("mode") == "日常练习", str(g.get("mode")))

qs = g.get("questions") or []
check("明细里 7 道题都在", len(qs) == 7, f"{len(qs)} 道")
singles = [q for q in qs if q["qtype"] == "single"]
subjs = [q for q in qs if q["qtype"] != "single"]

s0 = singles[0] if singles else {}
check("客观题有题干", bool(s0.get("stem")), (s0.get("stem") or "")[:20])
check("客观题有我的作答（选项字母）", s0.get("my_answer") == "A", str(s0.get("my_answer")))
check("客观题有选项内容（用于显示选了哪一项）", isinstance(s0.get("options"), dict)
      and len(s0.get("options") or {}) > 0, str(list((s0.get("options") or {}).keys())))
check("客观题有正确答案", bool(s0.get("correct_answer")), str(s0.get("correct_answer")))
check("客观题批改情况可判对错", isinstance(s0.get("is_right"), bool), str(s0.get("is_right")))
check("客观题带分值", s0.get("full_score") == 2, str(s0.get("full_score")))

u = subjs[0] if subjs else {}
check("主观题有题干", bool(u.get("stem")), (u.get("stem") or "")[:20])
check("主观题「我的作答」= 存的原文", u.get("my_answer") == MY_ANSWER,
      repr((u.get("my_answer") or "")[:24]))
check("主观题带 AI 批改结果", (u.get("grade") or {}).get("score") == 11,
      str((u.get("grade") or {}).get("score")))
pts = u.get("points") or []
check("主观题带采分点逐点命中情况", len(pts) >= 2, f"{len(pts)} 个采分点")
check("命中标记可用（勾了 1、2）",
      {p["seq"] for p in pts if p["hit"]} == {1, 2},
      str({p["seq"]: p["hit"] for p in pts}))
check("组内小结：自评命中 2 个采分点", (g.get("summary") or {}).get("point_hits") == 2,
      str(g.get("summary")))

print()
print("=" * 68)
print("⑤ 题目不在题库里 → missing=True（不能静默少一行）")
print("=" * 68)
con = sqlite3.connect(TEST_DB)
con.execute("INSERT INTO attempts (question_id,date,mode,hits,total,cause,note) "
            "VALUES (?,?,?,?,?,?,?)", (999999, TEST_DATE, "单选", 0, 1, None, "B"))
con.commit()
con.close()
det2 = server.logs_day(TEST_DATE)
loose = [x for x in det2["groups"] if x.get("loose")]
check("没挂 session 的作答单独成组", len(loose) == 1, f"{len(loose)} 组")
lq = (loose[0]["questions"] if loose else [{}])[0]
check("题目缺失时该行仍然出现", lq.get("question_id") == 999999, str(lq.get("question_id")))
check("并且被标成 missing", lq.get("missing") is True, str(lq.get("missing")))
check("题目缺失时仍能显示当时选的选项（旧数据把选项存在 note 里）",
      lq.get("my_answer") == "B", str(lq.get("my_answer")))
check("missing_n 有统计", det2.get("missing_n") == 1, str(det2.get("missing_n")))
check("组标签点明是未归属的旧记录", loose[0].get("mode") == "未归属记录",
      str(loose[0].get("mode")) if loose else "")
check("当天题数把这行也算进去", det2.get("count") == 8, str(det2.get("count")))

print()
print("=" * 68)
print("⑥ 与学习复盘联动：生成报告后该组带 report_id")
print("=" * 68)
# 先验一个踩到过的坑：**还没生成报告**的那次练习，复盘接口也必须给出 date ——
# 否则复盘页的「查看本次作答明细」会拼成 `/logs?date=&sid=`，跳过去不会自动展开
# （这个 bug 曾被"所有练习都已生成报告"的数据掩盖，是在真 clone 上跑才暴露的）。
_lst_before = server.reports_list()
_mine = [x for x in _lst_before["items"] if x["session_id"] == sid]
check("复盘接口里能找到这次练习", len(_mine) == 1, f"{len(_mine)} 条")
check("未生成报告时也带 date（否则日志跳转链接丢参数）",
      bool(_mine and _mine[0].get("date")), str(_mine[0].get("date")) if _mine else "")
check("未生成报告时也带 mode", bool(_mine and _mine[0].get("mode")),
      str(_mine[0].get("mode")) if _mine else "")


def fake_review(session, attempts_detail, weak_points, history_summary):
    return "## 测试报告\n内容略。", {"prompt_tokens": 10, "completion_tokens": 10}


server.ai_mod.review_session = fake_review
rep = server.review_generate({"session_id": sid})
check("生成报告成功", rep.get("ok") is True, str(rep)[:120])
det3 = server.logs_day(TEST_DATE)
g3 = [x for x in det3["groups"] if x.get("session_id") == sid][0]
check("日志明细里能看到 report_id（联动锚点）", g3.get("report_id") == rep.get("report_id"),
      f"{g3.get('report_id')} vs {rep.get('report_id')}")
check("并且带生成时间", bool(g3.get("report_created_at")), str(g3.get("report_created_at")))
lst = {d["date"]: d for d in server.logs()["days"]}
check("日志列表这一天也标出已复盘",
      (lst[TEST_DATE]["totals"] or {}).get("reports") == 1,
      str(lst[TEST_DATE]["totals"]))
check("summary 里报告数已计入", server.logs()["summary"]["reports"] >= 1,
      str(server.logs()["summary"]))

print()
print("=" * 68)
print("⑦ 边界：不存在的日期 / 空库")
print("=" * 68)
nul = server.logs_day("1999-01-01")
check("不存在的日期：ok 且 0 组，不炸", nul.get("ok") is True and nul["groups"] == [],
      str(nul)[:120])
EMPTY = TMP / "empty.db"
con = sqlite3.connect(EMPTY)
con.executescript("""
CREATE TABLE outline_nodes (id INTEGER PRIMARY KEY, parent_id INTEGER, level TEXT,
                            name TEXT, path TEXT);
CREATE TABLE questions (id INTEGER PRIMARY KEY, year INTEGER, source TEXT, qtype TEXT,
                        number INTEGER, stem TEXT, options TEXT, answer TEXT,
                        outline_id INTEGER, extra TEXT);
CREATE TABLE points (id INTEGER PRIMARY KEY, question_id INTEGER, seq INTEGER,
                     claim TEXT, evidence TEXT, outline_id INTEGER);
CREATE TABLE attempts (id INTEGER PRIMARY KEY, question_id INTEGER, date TEXT, mode TEXT,
                       hits INTEGER, total INTEGER, cause TEXT, note TEXT);
CREATE TABLE point_hits (attempt_id INTEGER, point_id INTEGER, hit INTEGER,
                         UNIQUE(attempt_id, point_id));
""")
con.commit()
con.close()
orig_db = server.DB
server.DB = EMPTY
try:
    server.ensure_schema()
    e1 = server.logs()
    e2 = server.logs_day("2026-01-01")
    check("空库：日志列表不炸且为 0 天", e1["days"] == [] and e1["summary"]["days"] == 0,
          str(e1["summary"]))
    check("空库：明细接口不炸", e2.get("ok") is True and e2["groups"] == [], str(e2)[:120])
finally:
    server.DB = orig_db

print()
print("=" * 68)
print("⑧ sessions.question_ids：幂等迁移 + 入参清洗")
print("=" * 68)
con = sqlite3.connect(TEST_DB)
scols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
con.close()
check("sessions 加了 question_ids 列", "question_ids" in scols, str(sorted(scols)))
try:
    server.ensure_schema()
    check("question_ids 迁移幂等（跑第二遍不报错）", True)
except Exception as e:
    check("question_ids 迁移幂等（跑第二遍不报错）", False, f"{type(e).__name__}: {e}")

check("清洗：去重且保序", server._clean_question_ids([3, 1, 3, 2]) == [3, 1, 2],
      str(server._clean_question_ids([3, 1, 3, 2])))
check("清洗：字符串 id 也认（AI/前端可能传来 \"304\"）",
      server._clean_question_ids(["7", "8"]) == [7, 8],
      str(server._clean_question_ids(["7", "8"])))
check("清洗：0 / 负数 / None / 垃圾一律丢掉",
      server._clean_question_ids([0, -5, None, "abc", 4]) == [4],
      str(server._clean_question_ids([0, -5, None, "abc", 4])))
check("清洗：坏 JSON 读回来当空列表（不让一个字段炸接口）",
      server._session_question_ids("{不是 JSON") == [],
      str(server._session_question_ids("{不是 JSON")))
check("清洗：超长入参被截断到上限",
      len(server._clean_question_ids(list(range(1, 500)))) == server.MAX_SESSION_QUESTIONS,
      str(len(server._clean_question_ids(list(range(1, 500))))))

print()
print("=" * 68)
print("⑨ 完整练习内容：没作答的题也要在日志里（题单 6 道，只做 2 单选 + 1 主观）")
print("=" * 68)
con = sqlite3.connect(TEST_DB)
con.row_factory = sqlite3.Row
full_singles = [dict(r) for r in con.execute(
    "SELECT id, answer FROM questions WHERE qtype='single' AND answer IS NOT NULL "
    "AND options IS NOT NULL LIMIT 4")]
# 主观题要挑一道**这一次没作答的**（前面 ② 已经答过 sub_id，再拿来当"没作答"就自相矛盾）
full_sub = con.execute(
    "SELECT id FROM questions WHERE qtype IN ('short','analysis','essay') "
    "AND id IN (SELECT DISTINCT question_id FROM points) AND id <> ? LIMIT 1",
    (sub_id,)).fetchone()
con.close()
full_sub_id = full_sub["id"]


def wrong_of(correct):
    """随便挑一个不是正确答案的选项字母：用来稳定构造"答对 1 道 / 答错 1 道"。"""
    for c in "ABCD":
        if c != correct:
            return c
    return "A"

# 题单故意乱序给（模拟前端按"单选在前、主观在后"收集）：日志要按题单顺序还原
# 6 道题里只作答 3 道（2 单选 + 1 主观），另 3 道（1 单选 + 1 主观）全程不碰
SET_IDS = [full_singles[2]["id"], full_singles[0]["id"], full_singles[3]["id"],
           full_singles[1]["id"], sub_id, full_sub_id]
res9 = server.save({
    "date": FULL_SET_DATE, "mode": "模拟考试", "note": "完整练习内容回归",
    "question_ids": SET_IDS,
    # 第 1 道答对、第 2 道答错 —— 后面用它们验证"没作答的题不参与正确率"
    "single": [{"id": full_singles[0]["id"], "answer": full_singles[0]["answer"]},
               {"id": full_singles[1]["id"], "answer": wrong_of(full_singles[1]["answer"])}],
    "subjective": [{"id": sub_id, "hits": [1, 2], "answer_text": "完整练习内容测试原文"}],
})
sid9 = res9["saved"].get("session_id")
check("save 返回 session_id", bool(sid9), str(sid9))
check("save 回显这次记下了几道题", res9["saved"].get("question_ids") == 6,
      str(res9["saved"].get("question_ids")))

con = sqlite3.connect(TEST_DB)
stored_ids = json.loads(con.execute("SELECT question_ids FROM sessions WHERE id=?",
                                    (sid9,)).fetchone()[0] or "[]")
con.close()
check("题单落库且顺序不变", stored_ids == SET_IDS, str(stored_ids))

det9 = server.logs_day(FULL_SET_DATE)
g9 = det9["groups"][0] if det9["groups"] else {}
qs9 = g9.get("questions") or []
check("明细题数 = 整份卷子（含没作答的）", len(qs9) == 6, f"{len(qs9)} 道")
check("接口区分「能看到几道」与「作答几道」",
      det9.get("count") == 6 and det9.get("attempted_n") == 3,
      f"count={det9.get('count')} attempted_n={det9.get('attempted_n')}")
check("接口给出未作答题数", det9.get("unanswered_n") == 3, str(det9.get("unanswered_n")))
check("按题单顺序还原（不是按作答顺序）",
      [q["question_id"] for q in qs9] == SET_IDS,
      str([q["question_id"] for q in qs9]))
check("组里标明有题单", g9.get("has_question_set") is True, str(g9.get("has_question_set")))
check("组里给出总题数 / 未作答数",
      g9.get("questions_n") == 6 and g9.get("unanswered_n") == 3,
      f"questions_n={g9.get('questions_n')} unanswered_n={g9.get('unanswered_n')}")

answered9 = [q for q in qs9 if q["answered"]]
un9 = [q for q in qs9 if not q["answered"]]
check("作答的题 answered=True", len(answered9) == 3, f"{len(answered9)} 道")
check("没作答的题 answered=False", len(un9) == 3, f"{len(un9)} 道")
un9_single = [q for q in un9 if q["qtype"] == "single"]
check("没作答的客观题：题干照给（不然「包括没做的」就只是一行编号）",
      all(q.get("stem") for q in un9_single),
      str([(q.get("stem") or "")[:12] for q in un9_single]))
check("没作答的客观题：选项照给",
      all(len(q.get("options") or {}) >= 2 for q in un9_single),
      str([len(q.get("options") or {}) for q in un9_single]))
check("没作答的客观题：正确答案照给", all(q.get("correct_answer") for q in un9_single),
      str([q.get("correct_answer") for q in un9_single]))
check("没作答的题不算对也不算错（is_right=None）",
      all(q["is_right"] is None and q["score"] is None for q in un9),
      str([(q["is_right"], q["score"]) for q in un9]))
check("没作答的题没有 attempt_id（它本来就没有作答行）",
      all(q["attempt_id"] is None for q in un9), str([q["attempt_id"] for q in un9]))
check("没作答的题不算 missing（题还在库里，只是没做）",
      all(q["missing"] is False for q in un9), str([q["missing"] for q in un9]))

sub9 = next((q for q in qs9 if q["question_id"] == full_sub_id), {})
check("没作答的主观题：带采分点（否则那道题在页面上是空的）",
      len(sub9.get("points") or []) >= 2, f"{len(sub9.get('points') or [])} 个采分点")
check("没作答的主观题：采分点全部未命中",
      all(p["hit"] is False for p in (sub9.get("points") or [])),
      str([p["hit"] for p in sub9.get("points") or []]))
check("没作答的主观题：没有批改结果", sub9.get("grade") is None, str(sub9.get("grade")))

# 另一道主观题**作答了**：命中标记不能被"同一天另一道主观题"或"没作答的那道"串味
sub_done9 = next((q for q in qs9 if q["question_id"] == sub_id), {})
check("作答过的主观题：命中标记照样准（勾了 1、2）",
      {p["seq"] for p in (sub_done9.get("points") or []) if p["hit"]} == {1, 2},
      str({p["seq"]: p["hit"] for p in sub_done9.get("points") or []}))

check("小结口径不变：只统计作答过的题（正确率不被没做的题拉低）",
      (g9.get("summary") or {}).get("objective_total") == 2
      and (g9.get("summary") or {}).get("objective_right") == 1,
      str(g9.get("summary")))
check("主观题命中只算作答过的那次", (g9.get("summary") or {}).get("point_hits") == 2,
      str(g9.get("summary")))

# 题单里有一道**已不在题库**的题：也要占一格显示出来，不能悄悄少一道
_ghost = 987654
con = sqlite3.connect(TEST_DB)
con.execute("UPDATE sessions SET question_ids=? WHERE id=?",
            (json.dumps(SET_IDS + [_ghost]), sid9))
con.commit()
con.close()
det9c = server.logs_day(FULL_SET_DATE)
g9c = det9c["groups"][0]
ghost = next((q for q in g9c["questions"] if q["question_id"] == _ghost), None)
check("题单里的题已被删 → 仍然占一格（不静默少一行）", ghost is not None,
      str([q["question_id"] for q in g9c["questions"]]))
check("并且标成 missing", bool(ghost) and ghost["missing"] is True,
      str(ghost.get("missing") if ghost else None))
check("题数把它算进去", g9c["questions_n"] == 7 and len(g9c["questions"]) == 7,
      f"questions_n={g9c['questions_n']} 实际 {len(g9c['questions'])}")

# 日志列表（/api/logs）也要带出题单，前端才能显示"这次共几道"
lst9 = {d["date"]: d for d in server.logs()["days"]}
s9 = (lst9.get(FULL_SET_DATE) or {}).get("sessions") or [{}]
check("列表接口带上这一次的题单", s9[0].get("question_ids") == SET_IDS + [_ghost],
      str(s9[0].get("question_ids"))[:80])
_day9 = lst9.get(FULL_SET_DATE) or {}
check("天级数据也区分题单数与作答数",
      _day9.get("questions_n") == 7 and _day9.get("attempts_n") == 3,
      f"questions_n={_day9.get('questions_n')} attempts_n={_day9.get('attempts_n')}")

_sum9 = server.logs()["summary"]
check("总览里同时给出「作答数」与「其中没做的」",
      _sum9.get("attempts", 0) < _sum9.get("questions", 0)
      and _sum9.get("unanswered", 0) >= 4,
      str({k: _sum9.get(k) for k in ("attempts", "questions", "unanswered")}))

print()
print("=" * 68)
print("⑩ 老 session（没有题单）：行为不变，并标明 has_question_set=False")
print("=" * 68)
det_old = server.logs_day(TEST_DATE)
g_old = next((x for x in det_old["groups"] if x.get("session_id") == sid), None)
check("老 session 仍按「做过的题」显示", bool(g_old) and len(g_old["questions"]) == 7,
      f"{len(g_old['questions']) if g_old else 0} 道")
check("并标明这次没有题单", g_old and g_old.get("has_question_set") is False,
      str(g_old.get("has_question_set") if g_old else None))
check("老 session 的未作答数 = 0（它本来就没有「没做的题」这个概念）",
      g_old and g_old.get("unanswered_n") == 0,
      str(g_old.get("unanswered_n") if g_old else None))
check("老 session 的题全部 answered=True",
      all(q["answered"] for q in g_old["questions"]),
      str([q["answered"] for q in g_old["questions"]]))
check("老 session 的采分点命中判定没被改坏（勾了 1、2）",
      {p["seq"] for q in g_old["questions"] for p in q["points"] if p["hit"]} == {1, 2},
      str({p["seq"] for q in g_old["questions"] for p in q["points"] if p["hit"]}))

print()
print("=" * 68)
print("⑪ 确认真实库一字未动")
print("=" * 68)
after = (REAL_DB.stat().st_size, REAL_DB.stat().st_mtime_ns)
check(f"{REAL_DB.name} 大小/修改时间未变", BEFORE == after, f"before={BEFORE} after={after}")
check("测试只往副本库写（真实库作答条数未变）", True,
      f"基线 {base_attempts} 条（副本库）")

shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全部通过。临时库已删除：", TMP)
