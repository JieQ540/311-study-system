"""复测的时间规则和冻结数据比较；不写数据库、不调用 AI。"""
import hashlib
import json
from datetime import datetime, timedelta, timezone

DEFAULT_INTERVALS = (1, 3, 7)
UTC = timezone.utc
LOCAL = timezone(timedelta(hours=8))


class RetestError(ValueError):
    pass


def now():
    return datetime.now(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="seconds")


def moment(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(UTC)
    except (ValueError, TypeError):
        raise RetestError("时间格式不正确，请刷新后重试。")


def intervals(value=None):
    value = list(DEFAULT_INTERVALS) if value is None else value
    if not isinstance(value, list) or not 1 <= len(value) <= 8 \
            or any(isinstance(x, bool) or not isinstance(x, int) or not 1 <= x <= 90 for x in value):
        raise RetestError("请输入 1 至 8 个间隔天数，每个为 1–90 的整数。")
    return value


def due_after(at, days):
    return stamp(at + timedelta(days=days))


def local_date(at):
    return at.astimezone(LOCAL).date().isoformat()


def date_due(value):
    try:
        chosen = datetime.strptime(value, "%Y-%m-%d").replace(hour=9, tzinfo=LOCAL)
    except (ValueError, TypeError):
        raise RetestError("下次日期应为 YYYY-MM-DD。")
    if chosen.date() < now().astimezone(LOCAL).date():
        raise RetestError("下次复测日期不能早于今天。")
    return stamp(chosen)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def criteria(question, selected_option=None):
    """内容与判分定义进指纹；作答和命中状态不进入题目依据。"""
    extra = question.get("extra") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    return digest({"question_id": question.get("question_id") or question.get("id"),
                   "qtype": question.get("qtype"), "stem": question.get("stem"),
                   "options": question.get("options"), "answer": question.get("correct_answer"),
                   "reference": question.get("reference"), "explain": question.get("explain"),
                   "full_score": question.get("full_score"), "sections": question.get("sections"),
                   "answer_note": question.get("answer_note"), "selected_option": selected_option,
                   "points": [{k: p.get(k) for k in ("id", "seq", "claim", "evidence", "score", "section")}
                              for p in question.get("points", [])]})


def compare(before, after):
    """只比较同一度量、同一依据；缺记录与 0 分严格分开。"""
    out = {"before": before.get("attempt_id"), "after": after.get("attempt_id"),
           "comparable": before.get("criteria") == after.get("criteria"), "scores": {},
           "new_hits": [], "lost_hits": [], "reason": ""}
    if not out["comparable"]:
        out["reason"] = "题目、分值、判分依据或所选分支发生变化，暂不计算分数差。"
        return out
    for series, label in (("confirmed", "已确认采分点"), ("ai_reference", "AI 参考分"), ("objective", "客观题得分")):
        a, b = before.get("scores", {}).get(series), after.get("scores", {}).get(series)
        if a is not None and b is not None and a.get("full") == b.get("full"):
            out["scores"][series] = {"label": label, "before": a["score"], "after": b["score"],
                                      "full": b["full"], "delta": round(b["score"] - a["score"], 2)}
    if "confirmed" in out["scores"]:
        a, b = set(before.get("hit_seqs", [])), set(after.get("hit_seqs", []))
        out["new_hits"], out["lost_hits"] = sorted(b - a), sorted(a - b)
    if not out["scores"]:
        out["reason"] = "尚未评定、评分来源不同或旧来源无法追溯，暂不能计算可比变化。"
    earlier = {x.get("kind") for x in before.get("diagnoses", []) if isinstance(x, dict)}
    later = {x.get("kind") for x in after.get("diagnoses", []) if isinstance(x, dict)}
    out["repeated_diagnoses"] = sorted(x for x in earlier & later if isinstance(x, str))
    return out


def public_plan(plan, at):
    intervals_ = json.loads(plan["intervals"])
    due = moment(plan["due_at"]) if plan.get("due_at") else None
    return {"id": plan["id"], "source_attempt_id": plan["source_attempt_id"],
            "source_session_id": plan["source_session_id"], "question_id": plan["question_id"],
            "status": plan["status"], "round_index": plan["round_index"], "intervals": intervals_,
            "due_at": plan.get("due_at"), "due": plan["status"] == "active" and due is not None and at >= due,
            "generation": plan["generation"], "created_at": plan["created_at"]}
