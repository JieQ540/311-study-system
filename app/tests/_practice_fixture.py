"""专项测试夹具：仅在已验明的副本库中，使用统一入库入口建立可控题池。"""
import contextlib
import json
import sqlite3

from _safety import require_test_db


def seed_practice(db):
    require_test_db(db)
    import server

    with contextlib.closing(sqlite3.connect(db)) as con, con:
        def node(parent, level, name):
            return con.execute(
                "INSERT INTO outline_nodes (parent_id,level,name,seq,path) VALUES (?,?,?,?,?)",
                (parent, level, name, 999, "专项验收/" + name)).lastrowid

        board = node(None, "board", "专项验收科目")
        chapter = node(board, "chapter", "专项验收章节")
        empty = node(board, "chapter", "没有真题的章节")
        point = node(chapter, "section", "专项验收节")
        other_board = node(None, "board", "另一验收科目")
        other_chapter = node(other_board, "chapter", "另一验收章节")

    generated = {"single": [], "subjective": []}
    specs = []
    for qtype in ("analysis", "short", "essay"):
        for index, oid in enumerate((point, point, other_chapter, point)):
            full = 30 if qtype == "essay" else 15
            scores = [3, 4, 4, 4] if qtype == "analysis" else [full / 4] * 4
            stem = ("专项验收材料：教师设置明确目标，并通过反馈帮助学生改进。\n"
                    "请结合材料说明有效学习需要的条件。")
            generated["subjective"].append({
                "qtype": qtype, "stem": f"{stem}（{qtype}-{index}）", "outline_id": oid,
                "full_score": full, "note": "验收评分说明：按本题四个要点分别给分。",
                "sections": [{"key": "*", "label": "整题", "score": full}],
                "points": [{"seq": i, "claim": "判断：错误" if qtype == "analysis" and i == 1 else f"有效学习条件{i}",
                            "evidence": "验收判分依据", "section": "*", "score": value}
                           for i, value in enumerate(scores, 1)]})
            specs.append((qtype, index))
    saved = server._save_ai_questions(generated)["subjective"]
    ids = {}
    with contextlib.closing(sqlite3.connect(db)) as con, con:
        for spec, q in zip(specs, saved):
            extra = json.loads(q["row"]["extra"])
            extra["answer_text"] = "验收参考答案：明确目标，并安排练习和及时反馈。"
            # 第四题保留 AI 来源，专门检验真题视图的排除规则。分值不在这里写。
            con.execute("UPDATE questions SET year=?,source=?,extra=? WHERE id=?",
                        (2021, "AI生成" if spec[1] == 3 else "验收真题", json.dumps(extra, ensure_ascii=False), q["id"]))
            ids[spec] = q["id"]
    return {"board": board, "chapter": chapter, "empty": empty, "other_board": other_board,
            "other_chapter": other_chapter, "ids": ids}


def detailed_grade(points, full=30, student="目标明确，但没有解释反馈如何起作用。"):
    awards = [{"seq": p["seq"], "awarded": p["score"] if p["seq"] in (1, 2) else 0,
               "reason": "已说明该要点" if p["seq"] in (1, 2) else "未出现该要点",
               "quote": "目标明确" if p["seq"] in (1, 2) else ""} for p in points]
    score = sum(a["awarded"] for a in awards)
    return {"schema_version": 2, "score": score, "full_score": full, "judgment": None,
            "point_awards": awards, "hit_seqs": [1, 2], "miss_seqs": [3, 4],
            "extra_credit": [], "adjustments": [], "section_scores": [{"key": "*", "got": score, "full": full}],
            "one_line": "答到了学习目标，尚需建立理论与材料的联系。", "cause": "看漏条件",
            "task_analysis": [{"key": "*", "task": "结合材料说明有效学习的条件", "focus": "需要解释条件如何在材料中发挥作用"}],
            "diagnosis": [{"kind": "论证不足", "quote": "目标明确", "problem": "没有说明目标如何引导练习。",
                           "action": "补充目标对练习方向和反馈标准的作用。", "point_seqs": [1]}],
            "next_steps": ["用一句话连接目标与练习安排。", "补一条材料中的反馈证据。"],
            "per_paragraph": [{"text": student, "mark": "按逐点得分计", "comment": "保留准确要点，补充解释。"}],
            "optimized": "目标明确，**据此安排练习，并通过及时反馈调整学习。**"}
