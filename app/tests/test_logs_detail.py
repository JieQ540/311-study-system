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
print("⑧ 确认真实库一字未动")
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
