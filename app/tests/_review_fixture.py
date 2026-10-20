"""复盘验收用作答；仅经生产保存入口写入已验证的副本。"""
from _practice_fixture import detailed_grade
from _safety import require_test_db


def seed_review(db, base):
    require_test_db(db)
    import server
    qid = base["subj_done"]
    answer = "目标明确，但没有解释反馈如何起作用。"
    grade = detailed_grade(server.points_of_question(qid), full=15, student=answer)
    grade["diagnosis"].append({"kind": "材料脱节", "quote": "这句话并不在原答中",
                               "problem": "可能尚未说明材料中的反馈如何调整练习。",
                               "action": "补一条材料中的反馈证据。", "point_seqs": [3]})
    payload = {"date": "2099-02-01", "mode": "主观题专项", "note": "复盘验收",
               "question_ids": [base["s_done"], qid, base["subj_undone"]],
               "single": [{"id": base["s_done"], "answer": "B"}],
               "subjective": [{"id": qid, "answer_text": answer, "grade": grade,
                               "hits": [1, 2], "point_source": "ai_confirmed"}]}
    saved = server.save(payload)
    assert saved.get("ok"), saved.get("error")
    sid = saved["saved"]["session_id"]
    attempt = server.rows("SELECT id FROM attempts WHERE session_id=? AND question_id=?", (sid, qid))[0]["id"]
    return {"sid": sid, "qid": qid, "attempt": attempt, "answer": answer, "grade": grade}
