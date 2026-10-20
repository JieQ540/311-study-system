"""AI 详细批改实得分应成为保存后的练习成绩；只写副本库。"""
import json
import os
import sys
import contextlib
import sqlite3
from copy import deepcopy
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "tests"))
from _safety import Checker, ai_transport, copy_db, real_db_fingerprint, release_db
from _practice_fixture import detailed_grade, seed_practice

before = real_db_fingerprint()
test_db = copy_db("ai_score_primary")
os.environ["DSH_DB"] = str(test_db)
import server

assert server.DB == test_db
server.ensure_schema()
ck = Checker("AI 详细批改作为练习得分")
try:
    fixture = seed_practice(test_db)
    qid = fixture["ids"][("short", 0)]
    points = server.points_of_question(qid)
    grade = detailed_grade(points, full=15)
    # 部分得分不能用“命中点满分之和”代替。
    grade["point_awards"][0]["awarded"] = 1.5
    grade["score"] = 5.25
    grade["section_scores"][0]["got"] = 5.25
    original_grade = server.ai_mod.grade_answer
    try:
        server.ai_mod.grade_answer = lambda *args, **kwargs: (deepcopy(grade), {})
        with ai_transport():
            graded = server.daily_grade({"question_id": qid, "student_text": "目标明确"})
        ck.check("批改响应已返回统一核算的 AI 实得分",
                 graded["score_result"]["score"] == 5.25 and graded["score_result"]["score_source"] == "ai_graded")
    finally:
        server.ai_mod.grade_answer = original_grade
    for source, hits in (("ai_unconfirmed", []), ("unassessed", []), ("self_confirmed", []),
                         ("ai_confirmed", [1, 2])):
        body = {"submission_id": "ai-primary-" + source, "date": "1900-10-19", "mode": "日常练习",
                             "question_ids": [qid], "subjective": [{
                                 "id": qid, "answer_text": "目标明确，但没有解释反馈如何起作用。",
                                 "grade": grade, "hits": hits, "point_source": source}]}
        with ai_transport():
            saved = server.save(body)
        ck.check(source + "：保存成功", saved.get("ok") is True)
        sid = saved["saved"]["session_id"]
        att = server.rows("SELECT a.*,q.qtype,q.stem,q.extra FROM attempts a "
                          "JOIN questions q ON q.id=a.question_id WHERE a.session_id=?", (sid,))[0]
        pts = server._attempt_points([att["id"]])[att["id"]]
        result = server.attempt_score(att, pts)
        ck.check(source + "：保存后得分采用 AI 实得 5.25/15",
                 result["scored"] and result["score"] == 5.25 and result["full"] == 15,
                 json.dumps({k: result.get(k) for k in ("score", "full", "source", "reason")}, ensure_ascii=False))
        ck.check(source + "：得分来源明确且原采分点结果保留",
                 result["score_source"] == "ai_graded" and "AI 详细批改" in result["score_source_label"]
                 and result["point_score"]["score"] == (7.5 if hits else 0 if source == "self_confirmed" else None))
        ck.check(source + "：提交响应直接返回实际成绩",
                 saved["saved"]["subjective_score"] == 5.25 and saved["saved"]["subjective_source"] == "ai_graded")
        summary = server._session_scores([sid])[sid]
        ck.check(source + "：汇总成绩不重复累加 AI 与采分点分",
                 summary["subjective_score"] == 5.25 and summary["subjective_full"] == 15
                 and summary["subjective_scored_n"] == 1 and summary["ai_reference_score"] == 5.25)
        ck.check(source + "：采分点命中仍按确认状态记录",
                 summary["point_total"] == (4 if source in ("self_confirmed", "ai_confirmed") else None))
        listing = next(x for x in server.logs()["items"] if x["id"] == sid)
        group = next(x for x in server.logs_day("1900-10-19")["groups"] if x["session_id"] == sid)
        report = next(x for x in server.reports_list()["items"] if x["session_id"] == sid)
        ck.check(source + "：日志列表、明细、复盘同分同来源",
                 all(x["subjective_score"] == 5.25 and x["subjective_source"] == "ai_graded"
                     for x in (listing, group["summary"], report)))
        with contextlib.closing(sqlite3.connect(test_db)) as con:
            con.row_factory = sqlite3.Row
            snapshot = server._learning_snapshot(con, att["id"])
        ck.check(source + "：复测快照分开保留实际成绩和采分点结果",
                 snapshot["scores"]["practice"]["score"] == 5.25
                 and snapshot["scores"].get("confirmed", {}).get("score") == result["point_score"]["score"])
        ck.check(source + "：原批改、自评来源及命中未被改写",
                 json.loads(att["grade"]) == grade and att["point_source"] == source
                 and sum(p["hit"] for p in pts) == len(hits))
        ck.check(source + "：重试仍返回第一次保存的同一成绩", server.save(body) == saved)

    # 部分得分、言之有理加分及有效零分均直接取 AI 分账，不从命中点猜。
    for name, value, extra in (("言之有理加分", 5.75, [{"awarded": 0.5}]),
                                ("有效零分", 0, [])):
        g = deepcopy(grade)
        g["score"] = value
        g["extra_credit"] = extra
        if value == 0:
            for award in g["point_awards"]:
                award["awarded"] = 0
            g["hit_seqs"] = []
        g["section_scores"][0]["got"] = value
        att["grade"] = json.dumps(g, ensure_ascii=False)
        scored = server.attempt_score(att, pts)
        ck.check(name + "：采用 AI 实得且仍保留满分",
                 scored["score"] == value and scored["full"] == 15 and scored["score_source"] == "ai_graded")
    bad = deepcopy(grade)
    bad["score"] = 14
    bad["verification"] = {"status": "v2_ok"}  # 前端声明不能冒充服务端对账。
    att["grade"] = json.dumps(bad, ensure_ascii=False)
    ck.check("坏账不被伪造的 verification 提升为 AI 成绩",
             server.attempt_score(att, pts)["score_source"] != "ai_graded")
    att["grade"] = None
    ck.check("未使用 AI 时保留采分点折合口径", server.attempt_score(att, pts)["score"] == 7.5)
    mixed = server.save({"date": "1900-10-20", "mode": "日常练习", "subjective": [
        {"id": qid, "grade": grade, "hits": [], "point_source": "self_confirmed"},
        {"id": qid, "hits": [3], "point_source": "self_confirmed"}]})["saved"]
    ck.check("同题多次作答逐条选来源，不重复累加旁账",
             mixed["subjective_score"] == 9 and mixed["subjective_full"] == 30
             and mixed["subjective_source"] == "mixed_ai")
    att["grade"] = json.dumps(grade)
    old_extra = att["extra"]
    att["extra"] = json.dumps({"full_score": None})
    ck.check("AI 成绩不能绕过满分待核", not server.attempt_score(att, pts)["scored"])
    att["extra"] = old_extra
    att["stem"] = "请在Ⅰ、Ⅱ两题中任选一题作答。"
    att["selected_option"] = None
    ck.check("AI 成绩不能绕过选做分支待核", not server.attempt_score(att, pts)["scored"])

    essay_id = fixture["ids"][("essay", 0)]
    essay_points = server.points_of_question(essay_id)
    essay_grade = detailed_grade(essay_points, full=30)
    essay_grade.update(score=15.5, extra_credit=[{"awarded": 0.5}])
    essay_grade["section_scores"][0]["got"] = 15.5
    essay = server.save({"date": "1900-10-21", "mode": "日常练习", "subjective": [
        {"id": essay_id, "answer_text": "目标明确", "grade": essay_grade,
         "hits": [], "point_source": "self_confirmed"}]})["saved"]
    ck.check("用户原型：论述题自评0点，实际保留15.5/30",
             essay["subjective_score"] == 15.5 and essay["subjective_full"] == 30)
finally:
    release_db(test_db)
    ck.check("真实库指纹不变", real_db_fingerprint() == before)
sys.exit(ck.finish())
