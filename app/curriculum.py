"""私有全科资料的安装、覆盖读模型和显式题面预览。

题目关联只向祖先汇总；原挂载和暂定关联只作为候选，不产生叶考点覆盖。
本模块不创建表、不写题库或成绩、不提交事务，也不调用模型。
"""
import json

import materials
import retest


Error = retest.RetestError
SUBJECTS = ("教育学原理", "中国教育史", "外国教育史", "教育心理学", "教育研究方法")
REFERENCE_STATES = {"usable_qualitative", "needs_source", "disputed"}
QUESTION_TYPES = {"analysis", "short", "essay"}


def _rows(con, sql, parameters=()):
    cursor = con.execute(sql, parameters)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _decode(value, label):
    try:
        result = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise Error(label + "格式损坏，请重新核对资料版本。") from exc
    if not isinstance(result, dict):
        raise Error(label + "应为对象。")
    return result


def _integer(value):
    return type(value) is int and value > 0


def _ids(values, nodes=None):
    return isinstance(values, list) and all(_integer(i) and (nodes is None or i in nodes) for i in values)


def _validate(bundle):
    """磁盘和数据库中的资料都检查结构；损坏内容不能静默变成零覆盖。"""
    if not isinstance(bundle, dict) or not isinstance(bundle.get("version"), str) or not bundle["version"].strip():
        raise Error("资料版本缺失。")
    values = bundle.get("nodes")
    if not isinstance(values, list) or not values:
        raise Error("资料缺少目录节点。")
    nodes = {}
    for node in values:
        if not isinstance(node, dict) or not _integer(node.get("id")) or node["id"] in nodes \
                or not all(isinstance(node.get(k), str) and node[k].strip() for k in ("name", "level", "path")):
            raise Error("资料目录节点格式不正确或重复。")
        nodes[node["id"]] = node
    for node in values:
        parent = node.get("parent_id")
        if parent is not None and (not _integer(parent) or parent not in nodes):
            raise Error("资料目录父节点不存在。")
        seen, current = set(), node["id"]
        while current is not None:
            if current in seen:
                raise Error("资料目录包含循环关系。")
            seen.add(current)
            current = nodes[current].get("parent_id")
    items = bundle.get("items")
    if not isinstance(items, list):
        raise Error("资料题目列表缺失。")
    if not _ids(bundle.get("protected_question_ids", [])):
        raise Error("保护题记录格式不正确。")
    protected_ids = set(bundle.get("protected_question_ids", []))
    identities = set()
    for item in items:
        if not isinstance(item, dict) or not _integer(item.get("question_id")) \
                or item.get("stable_identity") != "bank:" + str(item["question_id"]) \
                or item["stable_identity"] in identities:
            raise Error("资料题目身份缺失或重复。")
        identities.add(item["stable_identity"])
        if item["question_id"] in protected_ids and item.get("mapping_status") == "reserved" \
                and item.get("reference_status") == "reserved":
            if item.get("qtype") not in QUESTION_TYPES or not isinstance(item.get("stem"), str) or not item["stem"].strip():
                raise Error("保护题占位记录格式不正确。")
            continue
        if item.get("qtype") not in QUESTION_TYPES or not isinstance(item.get("stem"), str) or not item["stem"].strip() \
                or item.get("mapping_status") not in ("grounded", "tentative") \
                or item.get("reference_status") not in REFERENCE_STATES:
            raise Error("题型、题面或资料状态不正确。")
        if not _ids(item.get("primary_node_ids"), nodes) or not item["primary_node_ids"] \
                or not _ids(item.get("related_node_ids"), nodes):
            raise Error("题目引用的目录节点不正确。")
        tasks = item.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise Error("题目缺少逐问任务。")
        keys = set()
        for task in tasks:
            if not isinstance(task, dict) or not all(isinstance(task.get(k), str) and task[k].strip()
                    for k in ("key", "text", "kind", "evidence_quote")) \
                    or task["key"] in keys or type(task.get("required")) is not bool \
                    or not _ids(task.get("concept_ids"), nodes):
                raise Error("题目任务或任务关联格式不正确。")
            keys.add(task["key"])
        card = item.get("reference_card")
        if not isinstance(card, dict) or not isinstance(item.get("gaps"), list) \
                or not all(isinstance(gap, dict) and isinstance(gap.get("kind"), str) for gap in item["gaps"]):
            raise Error("参考资料或缺口记录格式不正确。")
        if item["reference_status"] == "usable_qualitative" and not all(
                isinstance(card.get(k), list) and card[k] for k in ("tasks", "units", "sources")):
            raise Error("可练习题目的参考任务、要点或来源缺失。")
        branches = item.get("elective_branches")
        if not isinstance(branches, list) or not all(isinstance(branch, dict) for branch in branches):
            raise Error("选做分支格式不正确。")
    batches = bundle.get("batches")
    if not isinstance(batches, list) or not all(isinstance(batch, dict)
            and all(isinstance(batch.get(k), str) and batch[k].strip() for k in ("version", "name", "purpose", "relative_path"))
            and _ids(batch.get("question_ids")) for batch in batches):
        raise Error("训练批次清单格式不正确。")
    return nodes


def install(con, directory):
    """调用方负责同一事务安装资料及训练批次；重复版本仅允许相同内容。"""
    try:
        bundle, _manifest, root = materials.load_bundle(directory, "curriculum.json", "curriculum_manifest.json")
    except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
        raise Error("全科资料校验失败：" + str(exc)) from exc
    _validate(bundle)
    digest = bundle.get("freeze_sha256")
    if not isinstance(digest, str) or retest.digest({k: v for k, v in bundle.items() if k != "freeze_sha256"}) != digest:
        raise Error("全科资料冻结指纹不一致。")
    old = con.execute("SELECT digest FROM curriculum_packages WHERE version=?", (bundle["version"],)).fetchone()
    if old:
        if old[0] != digest:
            raise Error("同一资料版本不能替换内容，请建立新版本。")
        return {"ok": True, "version": bundle["version"], "reused": True}
    con.execute("INSERT INTO curriculum_packages (version,digest,bundle_json,root,installed_at) VALUES (?,?,?,?,?)",
                (bundle["version"], digest, json.dumps(bundle, ensure_ascii=False), str(root), retest.stamp(retest.now())))
    return {"ok": True, "version": bundle["version"], "reused": False}


def _package(con):
    row = con.execute("SELECT digest,bundle_json FROM curriculum_packages ORDER BY installed_at DESC,rowid DESC LIMIT 1").fetchone()
    if not row:
        return None
    bundle = _decode(row[1], "全科资料")
    _validate(bundle)
    digest = retest.digest({k: v for k, v in bundle.items() if k != "freeze_sha256"})
    if bundle.get("freeze_sha256") != row[0] or digest != row[0]:
        raise Error("已安装资料与冻结指纹不一致。")
    return bundle


def _installed(con):
    packages = []
    for row in _rows(con, "SELECT version,content_json FROM pilot_packages ORDER BY installed_at DESC,rowid DESC"):
        bundle = _decode(row["content_json"], "训练批次")
        if not isinstance(bundle.get("active_questions"), list):
            raise Error("训练批次缺少题目列表。")
        if any(not isinstance(question, dict) or question.get("role") not in ("A", "B", "C", "D", "P")
               or not isinstance(question.get("slot"), str) or not question["slot"]
               for question in bundle["active_questions"]):
            raise Error("训练批次中的题目身份或角色格式损坏。")
        packages.append((row["version"], bundle))
    return packages


def _protected(con, bundle, packages):
    import pilot
    ids = set(bundle.get("protected_question_ids", []))
    identities, canonical = {"bank:" + str(qid) for qid in ids}, set()
    for _version, package in packages:
        for question in package["active_questions"]:
            if not isinstance(question, dict):
                raise Error("已安装训练批次中的题目格式损坏。")
            if question.get("role") == "D":
                ident = pilot.identity(question)
                if ident:
                    identities.add(ident)
                    canonical.add(ident)
                if _integer(question.get("question_id")):
                    ids.add(question["question_id"])
    # 新材料终测在保存时才物化；物化后的题库ID同样不能成为预览旁路。
    for run in _rows(con, "SELECT question_json,materialized_qid FROM pilot_runs WHERE materialized_qid IS NOT NULL"):
        question = _decode(run["question_json"], "训练记录题面")
        if question.get("role") == "D":
            ids.add(run["materialized_qid"])
    identities.update("bank:" + str(qid) for qid in ids)
    return identities, ids, len(canonical | {"bank:" + str(qid) for qid in bundle.get("protected_question_ids", [])})


def _context(con):
    bundle = _package(con)
    if bundle is None:
        return None
    packages = _installed(con)
    protected, protected_ids, protected_count = _protected(con, bundle, packages)
    items = [item for item in bundle["items"] if item["stable_identity"] not in protected
             and item["question_id"] not in protected_ids]
    return bundle, packages, items, protected_count


def _ancestors(nodes, node_id):
    values = []
    while node_id is not None:
        values.append(node_id)
        node_id = nodes[node_id].get("parent_id")
    return values


def _subject(nodes, node_id):
    chain = [nodes[i] for i in _ancestors(nodes, node_id)]
    board = next((n["name"] for n in chain if n["level"] == "board"), "未归属")
    if board == "中外教育史":
        chapter = next((n["name"] for n in chain if n["level"] == "chapter"), "")
        if "中国" in chapter:
            return "中国教育史"
        if "外国" in chapter:
            return "外国教育史"
    return board


def _concepts(item, task_keys=None):
    if item["mapping_status"] != "grounded":
        return set()
    return {i for task in item["tasks"] if task_keys is None or task["key"] in task_keys for i in task["concept_ids"]}


def _subjects(nodes, ids):
    return sorted({_subject(nodes, i) for i in ids}, key=lambda name: SUBJECTS.index(name) if name in SUBJECTS else len(SUBJECTS))


def _targets(packages, items):
    import pilot
    usable = {item["stable_identity"] for item in items if item["reference_status"] == "usable_qualitative"}
    result = {}
    for version, package in packages:
        for question in package["active_questions"]:
            if question.get("role") != "P":
                continue
            identity = pilot.identity(question)
            if identity in usable and identity not in result:
                result[identity] = {"version": version, "slot": question["slot"]}
    return result


def _answers(con):
    result = {}
    for answer in _rows(con, "SELECT a.question_id,a.student_answer,a.selected_option FROM attempts a "
                       "JOIN sessions s ON s.id=a.session_id JOIN questions q ON q.id=a.question_id "
                       "WHERE q.qtype IN ('analysis','short','essay') ORDER BY a.id"):
        value = "".join((answer["student_answer"] or "").split())
        if not value:
            continue
        key = answer["question_id"], value
        # 重复文本只有一份证据；已明确分支的记录优先用于节点关联。
        if key not in result or not result[key].get("selected_option"):
            result[key] = answer
    return list(result.values())


def _answer_concepts(item, answer):
    branches = item["elective_branches"]
    if not branches:
        return _concepts(item)
    normalize = lambda value: str(value or "").replace("Ⅰ", "I").replace("Ⅱ", "II").strip()
    selected = normalize(answer.get("selected_option"))
    branch = next((branch for branch in branches if normalize(branch.get("key")) == selected and selected), None)
    if not branch:
        return set()
    return _concepts(item, set(branch.get("task_keys", [])))


def overview(con):
    context = _context(con)
    summary = {"question_count": 0, "mapped_question_count": 0, "tentative_question_count": 0,
               "reference_ready_count": 0, "needs_source_count": 0, "disputed_count": 0,
               "answer_count": 0, "answered_question_count": 0, "protected_count": 0,
               "coverage_note": "任务关联仅按明确映射向祖先汇总；章、节候选不向叶考点广播。跨科题可重复计入各科，全库题目按稳定身份去重。",
               "reference_note": "可用参考仅表示可作定性练习，仍需尊重来源限制，不代表官方答案或已校准分值。",
               "evidence_note": "学习证据为有效场次中有原答文本的保存记录，同题同文本去重；缺测不计零分，不据此判断掌握。任选分支不明时只计全题原答，不传播至两分支全部考点。"}
    if context is None:
        return {"ok": True, "version": None, "summary": summary, "subjects": [], "nodes": [], "items": [], "batches": []}
    bundle, packages, items, protected_count = context
    nodes = {n["id"]: n for n in bundle["nodes"]}
    by_qid = {item["question_id"]: item for item in items}
    task_questions = {i: set() for i in nodes}
    candidates = {i: set() for i in nodes}
    references = {i: set() for i in nodes}
    node_answers = {i: set() for i in nodes}
    subject_data = {name: {"questions": set(), "tasks": set(), "candidates": set(), "references": set(), "answers": set(), "answered": set()} for name in SUBJECTS}
    answers = [answer for answer in _answers(con) if answer["question_id"] in by_qid]
    targets = _targets(packages, items)
    original_nodes = {r["id"]: r["outline_id"] for r in _rows(con, "SELECT id,outline_id FROM questions")}
    metadata = []
    for item in items:
        identity = item["stable_identity"]
        concepts = _concepts(item)
        candidate_ids = set(item["primary_node_ids"]) if not concepts else set()
        original = item.get("original_outline_id", original_nodes.get(item["question_id"]))
        if original in nodes:
            candidate_ids.add(original)
        for concept in concepts:
            for node_id in _ancestors(nodes, concept):
                task_questions[node_id].add(identity)
                if item["reference_status"] == "usable_qualitative":
                    references[node_id].add(identity)
        for concept in candidate_ids:
            for node_id in _ancestors(nodes, concept):
                candidates[node_id].add(identity)
        names = _subjects(nodes, concepts or item["primary_node_ids"])
        for name in names:
            data = subject_data.setdefault(name, {key: set() for key in ("questions", "tasks", "candidates", "references", "answers", "answered")})
            data["questions"].add(identity)
            data["tasks" if concepts else "candidates"].add(identity)
            if item["reference_status"] == "usable_qualitative":
                data["references"].add(identity)
        metadata.append({key: item.get(key) for key in ("question_id", "stable_identity", "qtype", "year", "number", "source_kind", "source_label", "primary_node_ids", "related_node_ids", "task_kinds", "mapping_status", "reference_status")})
        metadata[-1].update(subjects=names, mapped_names=[nodes[i]["name"] for i in item["primary_node_ids"]],
                            task_count=len(item["tasks"]), gap_count=len(item["gaps"]),
                            gap_kinds=sorted({gap["kind"] for gap in item["gaps"]}),
                            can_practice=identity in targets, practice_target=targets.get(identity))
        label = {"analysis": "辨析", "short": "简答", "essay": "分析论述"}[item["qtype"]]
        topic_names = " / ".join(metadata[-1]["mapped_names"][:2])
        metadata[-1]["title"] = str(item.get("year") or "库内题目") + " · " + label + (" · " + topic_names if topic_names else "")
        if not concepts:
            metadata[-1]["mapping_status"] = "tentative"
    for index, answer in enumerate(answers):
        item = by_qid[answer["question_id"]]
        concepts = _answer_concepts(item, answer)
        for concept in concepts:
            for node_id in _ancestors(nodes, concept):
                node_answers[node_id].add(index)
        # 分支不明或暂定映射只保留全题证据，不替具体考点作结论。
        for name in _subjects(nodes, concepts):
            if name in subject_data:
                subject_data[name]["answers"].add(index)
                subject_data[name]["answered"].add(item["stable_identity"])
    summary.update(question_count=len(items), mapped_question_count=sum(bool(_concepts(item)) for item in items),
                   tentative_question_count=sum(not _concepts(item) for item in items),
                   reference_ready_count=sum(item["reference_status"] == "usable_qualitative" for item in items),
                   needs_source_count=sum(item["reference_status"] == "needs_source" for item in items),
                   disputed_count=sum(item["reference_status"] == "disputed" for item in items),
                   answer_count=len(answers), answered_question_count=len({answer["question_id"] for answer in answers}),
                   protected_count=protected_count)
    public_nodes = []
    for node_id, node in nodes.items():
        public_nodes.append({**{key: node.get(key) for key in ("id", "parent_id", "level", "name", "path")},
                             "subject": _subject(nodes, node_id), "task_question_count": len(task_questions[node_id]),
                             "ancestor_candidate_count": len(candidates[node_id] - task_questions[node_id]),
                             "reference_ready_count": len(references[node_id]), "answer_count": len(node_answers[node_id])})
    public_subjects = [{"name": name, "question_count": len(data["questions"]), "task_question_count": len(data["tasks"]),
                        "ancestor_candidate_count": len(data["candidates"] - data["tasks"]),
                        "reference_ready_count": len(data["references"]), "answer_count": len(data["answers"]),
                        "answered_question_count": len(data["answered"])} for name, data in subject_data.items()]
    visible_ids = set(by_qid)
    batches = [{key: batch[key] for key in ("version", "name", "purpose")} | {
                "question_count": len(set(batch["question_ids"]) & visible_ids)} for batch in bundle["batches"]]
    return {"ok": True, "version": bundle["version"], "summary": summary, "subjects": public_subjects,
            "nodes": public_nodes, "items": metadata, "batches": batches}


def _item(con, qid):
    if not _integer(qid):
        raise Error("题目编号格式不正确。")
    context = _context(con)
    if context is None:
        raise Error("尚未安装全科资料。")
    bundle, packages, items, _protected_count = context
    item = next((item for item in items if item["question_id"] == qid), None)
    if item is None:
        raise Error("题目不可用于资料预览，请返回覆盖页面。")
    return bundle, packages, items, item


def preview(con, qid):
    """显式POST调用；门禁通过后只返回题面，并保存接触记录。"""
    import pilot
    bundle, packages, items, item = _item(con, qid)
    pilot.assert_material_access(con, item)
    pilot.record_exposure(con, item, "preview", package=None, run_id=None)
    nodes = {n["id"]: n for n in bundle["nodes"]}
    return {"ok": True, "question": {"question_id": item["question_id"], "stem": item["stem"], "qtype": item["qtype"],
            "subject": "、".join(_subjects(nodes, _concepts(item) or item["primary_node_ids"])),
            "tasks": item["tasks"], "mapped_names": [nodes[i]["name"] for i in item["primary_node_ids"]]},
            "can_practice": item["stable_identity"] in _targets(packages, items)}


def practice_target(con, qid):
    _bundle, packages, items, item = _item(con, qid)
    target = _targets(packages, items).get(item["stable_identity"])
    if target is None:
        raise Error("该题资料尚不适合定性训练，或对应训练批次尚未安装。")
    return target
