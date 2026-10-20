"""把已保存的作答与批改整理成可追溯的复盘；不计分、不写库、不调用 AI。"""
import hashlib
import json
import re
import marks


VERSION = 1
ORIGINS = {
    "ai_evidence": "AI 判断 · 有原文引用",
    "ai_inference": "AI 推测 · 待核实",
    "recorded": "已保存错因 · 来源未单独记录",
    "scoring": "已记录的采分点结果",
    "objective": "客观作答事实 · 原因待判断",
}


def text(value, limit=2000):
    return value.strip()[:limit] if isinstance(value, str) else ""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def quote_matches(quote, answer):
    clean = lambda s: re.sub(r"\s+", "", text(s, 100000))
    return bool(clean(quote)) and clean(quote) in clean(answer)


def question_key(q):
    return f"a{q['attempt_id']}" if q.get("attempt_id") else f"q{q['question_id']}"


def question_references(questions, question_order=None):
    """本场题单的题号与内部身份分开；重复作答不挤占下一题的题号。"""
    order = list(dict.fromkeys(list(question_order or []) + [q["question_id"] for q in questions]))
    numbers = {qid: i + 1 for i, qid in enumerate(order)}
    grouped, refs = {}, {}
    for q in questions:
        grouped.setdefault(q["question_id"], []).append(q)
    for qid, records in grouped.items():
        for ordinal, q in enumerate(records, 1):
            key = question_key(q)
            label = f"第 {numbers[qid]} 题"
            if not question_order:
                label += "（记录顺序）"
            if len(records) > 1:
                label += f"（第 {ordinal} 次作答）"
            refs[key] = {"key": key, "number": numbers[qid], "label": label,
                         "question_id": qid, "attempt_id": q.get("attempt_id"),
                         "title": text(q.get("stem"), 200)}
        # q123 在同题多次作答时指向不唯一，不能擅自指到其中一次。
        if len(records) == 1:
            refs.setdefault(f"q{qid}", refs[question_key(records[0])])
    return refs


def organize(questions, dismissed=()):
    """保留每次作答的身份；同题重复作答和用户否定的判断都不静默丢弃。"""
    dismissed = set(dismissed)
    findings = []

    def add(q, kind, problem, action, origin, quote="", point_seqs=None):
        item = {"question_key": question_key(q), "attempt_id": q["attempt_id"],
                "question_id": q["question_id"], "kind": text(kind, 80),
                "problem": text(problem), "action": text(action), "origin": origin,
                "origin_label": ORIGINS[origin], "quote": text(quote),
                "point_seqs": point_seqs or []}
        item["id"] = digest(item)[:24]
        item["dismissed"] = item["id"] in dismissed
        if not any(f["id"] == item["id"] for f in findings):
            findings.append(item)

    for q in questions:
        if not q.get("answered") or q.get("missing"):
            continue
        grade = q.get("grade") if isinstance(q.get("grade"), dict) else {}
        diagnoses = grade.get("diagnosis")
        diagnoses = diagnoses if isinstance(diagnoses, list) else []
        start = len(findings)
        valid_seqs = {p.get("seq") for p in q.get("points", [])}
        for d in diagnoses[:4]:
            if not isinstance(d, dict) or not text(d.get("problem")):
                continue
            quote = text(d.get("quote"))
            grounded = quote_matches(quote, q.get("my_answer"))
            seqs = d.get("point_seqs") if isinstance(d.get("point_seqs"), list) else []
            add(q, d.get("kind") or "作答诊断", d["problem"],
                d.get("action") or "对照题目要求与批改依据，改写这一处，再核对是否回应了题目。",
                "ai_evidence" if grounded else "ai_inference",
                quote if grounded else "", [n for n in seqs if isinstance(n, int) and n in valid_seqs])
        if len(findings) == start:
            steps = grade.get("next_steps")
            steps = [text(x) for x in steps if text(x)] if isinstance(steps, list) else []
            if steps:
                add(q, "批改中的改进建议", grade.get("one_line") or "批改建议进一步完善作答。",
                    steps[0], "ai_inference")
            elif q.get("qtype") == "single" and q.get("is_right") is False:
                add(q, "选择题答错", f"本次选择 {q.get('my_answer') or '未记录'}，"
                    f"参考答案为 {q.get('correct_answer') or '未记录'}。仅凭选项不能确定心理原因。",
                    "对照解析，说明原选项不符合题意的地方，再重新选择。", "objective")
            else:
                result = q.get("score_result") or {}
                sr = result.get("point_score") or result
                missed = [p for p in q.get("points", [])
                          if p.get("seq") not in (sr.get("hit_seqs") or [])
                          and (not q.get("selected_option")
                               or marks.opt_of_section(p.get("section")) == q["selected_option"])]
                if sr.get("scored") and sr.get("source") in ("self_confirmed", "ai_confirmed") \
                        and (sr.get("score") or 0) < (sr.get("full") or 0) and missed:
                    add(q, "采分点未命中", "这次已确认的评定中，部分采分点未命中；是否属于遗忘或审题问题仍需核实。",
                        "对照未命中的采分点，补写一段能回应题目要求的答案。", "scoring",
                        point_seqs=[p["seq"] for p in missed])
        cause = text(q.get("cause"), 100)
        if cause:
            add(q, cause, "本次作答保存的错因：" + cause + "。旧字段没有单独记录是谁判断的。",
                "核对这一错因是否符合实际，再对照题目与解析订正。", "recorded")

    active = [f for f in findings if not f["dismissed"]]
    groups = {}
    for f in active:
        group = groups.setdefault(f["kind"], {"kind": f["kind"], "findings": [], "question_keys": []})
        group["findings"].append(f)
        if f["question_key"] not in group["question_keys"]:
            group["question_keys"].append(f["question_key"])
    groups = sorted(groups.values(), key=lambda g: -len(g["question_keys"]))
    for g in groups:
        g["count"] = len(g["question_keys"])
    ranks = {"ai_evidence": 0, "scoring": 1, "objective": 1, "ai_inference": 2, "recorded": 3}
    counts = {g["kind"]: g["count"] for g in groups}
    candidates = sorted(active, key=lambda f: (-counts[f["kind"]], ranks[f["origin"]]))
    tasks, seen = [], set()
    for f in candidates:
        if f["question_key"] in seen:
            continue
        seen.add(f["question_key"])
        criterion = ("能说明原选项为什么不符合题意，并重新选出有依据的答案。"
                     if f["origin"] == "objective" else
                     "已按这条建议改写原答，并对照题目要求和对应采分点核对；这次订正不代表已长期掌握。")
        tasks.append({"finding_id": f["id"], "question_key": f["question_key"],
                      "attempt_id": f["attempt_id"], "question_id": f["question_id"],
                      "title": f["kind"], "instruction": f["action"],
                      "criterion": criterion, "origin_label": f["origin_label"]})
        if len(tasks) == 3:
            break
    return {"version": VERSION, "findings": findings, "groups": groups,
            "tasks": tasks, "focus_keys": [t["question_key"] for t in tasks]}


def prompt_context(questions, analysis, has_question_set):
    """所有题均列入；超长字段按题缩短且明确标注，不能按前 40 条截掉主观题。"""
    def excerpt(value, limit):
        raw = value if isinstance(value, str) else ""
        return raw if len(raw) <= limit else raw[:limit] + "\n[此字段过长，已节选，不能据此断言后文不存在]"

    facts = []
    for q in questions:
        sr = q.get("score_result") or {}
        grade = q.get("grade") if isinstance(q.get("grade"), dict) else {}
        awards = grade.get("point_awards") if isinstance(grade.get("point_awards"), list) else []
        elective = marks.is_elective(q.get("stem"))
        selected = q.get("selected_option")
        facts.append({
            "key": question_key(q), "attempt_id": q.get("attempt_id"),
            "question_label": q.get("question_label"), "citation": f"[[{question_key(q)}]]",
            "question_id": q["question_id"], "qtype": q.get("qtype_label"),
            "answered": q.get("answered"), "missing": q.get("missing"),
            "elective": elective, "selected_option": selected,
            "topic": q.get("outline_path"), "stem": excerpt(q.get("stem"), 4000),
            "my_answer": excerpt(q.get("my_answer"), 6000),
            "options": q.get("options"), "correct_answer": q.get("correct_answer"),
            "reference": excerpt(q.get("reference") or q.get("explain"), 4000),
            "result": {"score": q.get("score") if q.get("qtype") == "single" else sr.get("score"),
                       "full": q.get("full_score"), "source": sr.get("score_source_label") or q.get("point_source_label"),
                       "scored": sr.get("scored"), "reason": sr.get("reason")},
            "points": [{**{k: p.get(k) for k in ("seq", "claim", "section", "hit")},
                        "applicable": (marks.opt_of_section(p.get("section")) == selected
                                       if selected else None) if elective else True}
                       for p in q.get("points", [])],
            "grading": {"task_analysis": grade.get("task_analysis"),
                        "raw_score": grade.get("score"), "raw_full": grade.get("full_score"),
                        "next_steps": grade.get("next_steps"), "one_line": grade.get("one_line"),
                        "point_awards": [{"seq": p.get("seq"), "awarded": p.get("awarded"),
                                          "reason": p.get("reason"),
                                          "quote": p.get("quote") if quote_matches(p.get("quote"), q.get("my_answer")) else "",
                                          "quote_verified": quote_matches(p.get("quote"), q.get("my_answer"))}
                                         for p in awards if isinstance(p, dict)],
                        "note": "AI 原始建议分不等于已确认采分点得分，不可自行加总取代本次计分指标；原文引用核对不代表学科判断已验证。"},
            "diagnoses": [f for f in analysis["findings"] if f["question_key"] == question_key(q)],
        })
    labels = {question_key(q): q.get("question_label") for q in questions}
    tasks = [{**task, "question_label": labels.get(task["question_key"]),
              "citation": f"[[{task['question_key']}]]"} for task in analysis["tasks"]]
    return {"version": VERSION, "has_full_question_set": has_question_set,
            "questions": facts, "priority_tasks": tasks}
