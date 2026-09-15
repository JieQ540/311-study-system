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
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kaoyan.db"
INDEX = ROOT / "app" / "static" / "index.html"

app = FastAPI(title="311 备考系统")

EXAM_MINUTES = 180  # 311 考试时长


def rows(sql, args=()):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def exec_sql(sql, args=()):
    con = sqlite3.connect(DB)
    try:
        cur = con.execute(sql, args)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def ensure_sessions_table():
    """练习日志表：每次提交记一条，含当刻薄弱点快照。"""
    con = sqlite3.connect(DB)
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
        """)
        con.commit()
    finally:
        con.close()


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
            s["full_score"] = json.loads(s.get("extra") or "{}").get("full_score", 15)
        except Exception:
            s["full_score"] = 15
        s["points"] = rows(
            "SELECT seq,claim,evidence FROM points WHERE question_id=? ORDER BY seq", (s["id"],))

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


@app.get("/api/paper")
def paper(rebuild: int = 0):
    """取当前试卷。

    默认返回**正在使用的那一份** —— 刷新页面、关掉重开都拿到同一份卷子，
    作答不会白做。只有传 rebuild=1 才归档旧卷并重新组卷。
    """
    ensure_sessions_table()
    if not rebuild:
        prow = rows("SELECT * FROM papers WHERE status='active' ORDER BY id DESC LIMIT 1")
        if prow:
            existing = load_paper_by_row(prow[0])
            if existing:
                return existing

    PLAN = {"single": 45, "analysis": 3, "short": 5, "essay": 3}

    def take(qtype, limit):
        return rows(
            "SELECT id,number,qtype,stem,options,answer,outline_id,extra,year,source FROM questions "
            "WHERE qtype=? AND (source IS NULL OR source NOT LIKE 'AI%') "
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
            gen, _ = ai_mod.generate_questions(
                scope,
                n_single=shortfall["single"],
                n_analysis=shortfall["analysis"],
                n_short=shortfall["short"],
                n_essay=shortfall["essay"],
            )
            con = sqlite3.connect(DB)
            try:
                for q in gen.get("single", []):
                    cur = con.execute(
                        "INSERT INTO questions (source,qtype,stem,options,answer,outline_id,extra) "
                        "VALUES (?,?,?,?,?,?,?)",
                        ("AI生成", "single", q.get("stem", ""),
                         json.dumps(q.get("options") or {}, ensure_ascii=False),
                         q.get("answer"), q.get("outline_id"),
                         json.dumps({"explain": q.get("explain", "")}, ensure_ascii=False)))
                    picks["single"].append({"id": cur.lastrowid, "number": None, "qtype": "single",
                                            "stem": q.get("stem"), "options": json.dumps(q.get("options") or {}, ensure_ascii=False),
                                            "answer": q.get("answer"), "outline_id": q.get("outline_id"),
                                            "extra": None, "year": None, "source": "AI生成"})
                for q in gen.get("subjective", []):
                    cur = con.execute(
                        "INSERT INTO questions (source,qtype,stem,extra,outline_id) VALUES (?,?,?,?,?)",
                        ("AI生成", q.get("qtype", "short"), q.get("stem", ""),
                         json.dumps({"full_score": q.get("full_score", 15)}, ensure_ascii=False), q.get("outline_id")))
                    qid = cur.lastrowid
                    for p in q.get("points", []):
                        con.execute(
                            "INSERT INTO points (question_id,seq,claim,evidence,outline_id) VALUES (?,?,?,?,?)",
                            (qid, p.get("seq"), p.get("claim", ""), p.get("evidence"), q.get("outline_id")))
                    picks[q.get("qtype", "short")].append(
                        {"id": qid, "number": None, "qtype": q.get("qtype", "short"), "stem": q.get("stem"),
                         "options": None, "answer": None, "outline_id": q.get("outline_id"),
                         "extra": json.dumps({"full_score": q.get("full_score", 15)}, ensure_ascii=False),
                         "year": None, "source": "AI生成"})
                con.commit()
            finally:
                con.close()
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
            try:
                s["full_score"] = json.loads(s.get("extra") or "{}").get("full_score", 15 if k != "essay" else 30)
            except Exception:
                s["full_score"] = 15 if k != "essay" else 30
            s["points"] = rows(
                "SELECT seq,claim,evidence FROM points WHERE question_id=? ORDER BY seq", (s["id"],))
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


@app.post("/api/save")
def save(payload: dict):
    """保存一次练习记录。

    payload = {
      "date": "2026-09-15",
      "single": [{"id":1,"answer":"A"}, ...],
      "subjective": [{"id":50,"hits":[1,3],"cause":"记混","note":""}, ...]
    }
    """
    date = payload.get("date") or datetime.now().strftime("%Y-%m-%d")
    saved = {"single": 0, "subjective": 0, "point_hits": 0}
    single_right = 0

    for item in payload.get("single", []):
        qid = item.get("id")
        if not qid:
            continue
        row = rows("SELECT answer FROM questions WHERE id=?", (qid,))
        correct = (row[0]["answer"] if row else None) or None
        picked = item.get("answer")
        ok = 1 if (correct and picked and picked == correct) else 0
        single_right += ok
        exec_sql(
            "INSERT INTO attempts (question_id,date,mode,hits,total,cause,note) VALUES (?,?,?,?,?,?,?)",
            (qid, date, "单选", ok, 1, None, picked),
        )
        saved["single"] += 1

    for item in payload.get("subjective", []):
        qid = item.get("id")
        if not qid:
            continue
        total = exec_sql  # noqa
        con = sqlite3.connect(DB)
        tot = con.execute("SELECT COUNT(*) FROM points WHERE question_id=?", (qid,)).fetchone()[0]
        con.close()
        hits = item.get("hits") or []
        aid = exec_sql(
            "INSERT INTO attempts (question_id,date,mode,hits,total,cause,note) VALUES (?,?,?,?,?,?,?)",
            (qid, date, "主观自评", len(hits), tot, item.get("cause"), item.get("note")),
        )
        for seq in hits:
            pid = rows("SELECT id FROM points WHERE question_id=? AND seq=?", (qid, seq))
            if pid:
                exec_sql(
                    "INSERT OR REPLACE INTO point_hits (attempt_id,point_id,hit) VALUES (?,?,1)",
                    (aid, pid[0]["id"]),
                )
                saved["point_hits"] += 1
        saved["subjective"] += 1

    saved["single_right"] = single_right
    saved["single_score"] = single_right * 2  # 311 单选每题 2 分

    # ---------------- 写练习日志（含当刻薄弱点快照） ----------------
    try:
        ensure_sessions_table()
        subj_ids = [s["id"] for s in payload.get("subjective", []) if s.get("id")]
        ptotal = 0
        if subj_ids:
            qs = ",".join("?" * len(subj_ids))
            ptotal = rows(f"SELECT COUNT(*) AS c FROM points WHERE question_id IN ({qs})", subj_ids)[0]["c"]
        w = weakness()
        snap = [
            {"name": i["name"], "path": i["path"], "hit": i["hit_n"],
             "total": i["total_n"], "rate": i["rate"]}
            for i in w["items"][:12]
        ]
        con = sqlite3.connect(DB)
        cur = con.execute(
            "INSERT INTO sessions (date,mode,note,objective_total,objective_right,objective_score,"
            "subjective_count,point_hits,point_total,weak_snapshot,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (date, payload.get("mode") or "未标注", payload.get("note"),
             saved["single"], single_right, float(saved["single_score"]),
             saved["subjective"], saved["point_hits"], ptotal,
             json.dumps(snap, ensure_ascii=False),
             datetime.now().isoformat(timespec="seconds")),
        )
        con.commit()
        saved["session_id"] = cur.lastrowid
        con.close()
    except Exception as e:
        saved["log_error"] = f"{type(e).__name__}: {e}"

    return {"ok": True, "date": date, "saved": saved}


@app.get("/api/weakness")
def weakness():
    """薄弱点：客观题对错 + 主观题采分点命中，**口径统一**地统计。

    每个「样本」= 一次判定机会：
      - 客观题：一次作答 = 1 个样本（对=1 错=0）
      - 主观题：一次作答 × 一个采分点 = 1 个样本（命中=1 未命中=0）

    旧版分子的主观部分来自 point_hits（只记命中），分母来自 points（全部采分点），
    两边口径不同，导致出现过 2/1 = 200% 这种不可能的结果。
    """
    sql = """
    SELECT o.id AS oid, o.path AS path, o.level AS level, o.name AS name,
           SUM(t.hit) AS hit_n, SUM(t.tot) AS total_n
    FROM (
        -- 客观题：每答一次算一个样本
        SELECT q.outline_id AS oid,
               CASE WHEN a.hits = 1 THEN 1 ELSE 0 END AS hit,
               1 AS tot
        FROM attempts a
        JOIN questions q ON q.id = a.question_id
        WHERE q.qtype = 'single' AND q.outline_id IS NOT NULL

        UNION ALL

        -- 主观题：每个「作答 × 采分点」算一个样本
        SELECT p.outline_id AS oid,
               CASE WHEN ph.hit = 1 THEN 1 ELSE 0 END AS hit,
               1 AS tot
        FROM attempts a
        JOIN questions q ON q.id = a.question_id
        JOIN points p ON p.question_id = q.id
        LEFT JOIN point_hits ph
               ON ph.attempt_id = a.id AND ph.point_id = p.id
        WHERE q.qtype IN ('analysis','short','essay')
          AND p.outline_id IS NOT NULL
    ) t
    JOIN outline_nodes o ON o.id = t.oid
    GROUP BY o.id
    HAVING SUM(t.tot) > 0
    ORDER BY (CAST(SUM(t.hit) AS REAL) / SUM(t.tot)) ASC, SUM(t.tot) DESC
    """
    out = []
    for r in rows(sql):
        r["rate"] = round(r["hit_n"] / r["total_n"], 3) if r["total_n"] else 0
        out.append(r)
    return {
        "items": out,
        "summary": {
            "nodes": len(out),
            "weak_nodes": sum(1 for x in out if x["rate"] < 0.6),
        },
    }


@app.get("/api/logs")
def logs(limit: int = 50):
    """练习日志：每次提交一条，含当刻薄弱点快照。"""
    ensure_sessions_table()
    out = []
    for r in rows("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)):
        try:
            r["weak_snapshot"] = json.loads(r["weak_snapshot"] or "[]")
        except Exception:
            r["weak_snapshot"] = []
        out.append(r)
    return {"items": out, "count": len(out)}


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX.read_text(encoding="utf-8")


@app.get("/daily", response_class=HTMLResponse)
def daily_page():
    """日常练习模式：输入记录 → 确认考点 → 出题 → 批改。"""
    return (ROOT / "app" / "static" / "daily.html").read_text(encoding="utf-8")


@app.get("/logs", response_class=HTMLResponse)
def logs_page():
    """练习日志：每次模拟 / 日常练习的得分与薄弱点快照。"""
    return (ROOT / "app" / "static" / "logs.html").read_text(encoding="utf-8")


# ============================================================ 日常练习模式（AI）

# AI 模块延迟导入：ai.py 在导入时会读取 config.json，
# 放在模块顶部会让「没配 key 也想看题库」的场景直接起不来服务。
import ai as ai_mod  # noqa: E402


@app.post("/api/ai/test")
def ai_test():
    """连通性自检，前端可点按钮调用。"""
    try:
        text, usage = ai_mod.ping()
        return {"ok": True, "reply": text, "usage": usage}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
    ids = [m["id"] for m in data.get("matches", []) if isinstance(m.get("id"), int)]
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
    }


@app.post("/api/daily/generate")
def daily_generate(payload: dict):
    """② 按确认后的考点出题：真题优先，不足用 AI 补。"""
    ids = payload.get("point_ids") or []
    n_single = int(payload.get("n_single", 20))
    n_analysis = int(payload.get("n_analysis", 1))
    n_short = int(payload.get("n_short", 1))
    n_essay = int(payload.get("n_essay", 1))
    if not ids:
        return {"ok": False, "error": "未指定考点"}

    # 考点详情
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    qs = ",".join("?" * len(ids))
    points = [dict(r) for r in con.execute(
        f"SELECT id,name,path FROM outline_nodes WHERE id IN ({qs})", ids)]
    con.close()

    real = ai_mod.real_questions(ids, limit_single=n_single, limit_subjective=3)
    used_real = len(real["single"]) + len(real["subjective"])

    need_single = max(0, n_single - len(real["single"]))
    need_subj = max(0, (n_analysis + n_short + n_essay) - len(real["subjective"]))

    generated, usage = {"single": [], "subjective": []}, None
    if need_single or need_subj:
        # 按缺口比例分配主观题类型
        generated, usage = ai_mod.generate_questions(
            points,
            n_single=need_single,
            n_analysis=max(0, n_analysis - sum(1 for s in real["subjective"] if s["qtype"] == "analysis")),
            n_short=max(0, n_short - sum(1 for s in real["subjective"] if s["qtype"] == "short")),
            n_essay=max(0, n_essay - sum(1 for s in real["subjective"] if s["qtype"] == "essay")),
        )
        # 生成的题必须入库：否则没有 id，无法批改、无法记录成绩
        con = sqlite3.connect(DB)
        try:
            for q in generated.get("single", []):
                cur = con.execute(
                    "INSERT INTO questions (year,source,qtype,number,stem,options,answer,outline_id,extra) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (None, "AI生成", "single", None, q.get("stem", ""),
                     json.dumps(q.get("options") or {}, ensure_ascii=False),
                     q.get("answer"), q.get("outline_id"),
                     json.dumps({"explain": q.get("explain", "")}, ensure_ascii=False)),
                )
                q["id"] = cur.lastrowid
            for q in generated.get("subjective", []):
                cur = con.execute(
                    "INSERT INTO questions (year,source,qtype,number,stem,extra,outline_id) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (None, "AI生成", q.get("qtype", "short"), None, q.get("stem", ""),
                     json.dumps({"full_score": q.get("full_score", 15)}, ensure_ascii=False),
                     q.get("outline_id")),
                )
                qid = cur.lastrowid
                q["id"] = qid
                for p in q.get("points", []):
                    con.execute(
                        "INSERT INTO points (question_id,seq,claim,evidence,outline_id) VALUES (?,?,?,?,?)",
                        (qid, p.get("seq"), p.get("claim", ""), p.get("evidence"), q.get("outline_id")),
                    )
            con.commit()
        finally:
            con.close()
    return {
        "ok": True,
        "real_count": used_real,
        "generated_count": len(generated.get("single", [])) + len(generated.get("subjective", [])),
        "paper": {"real": real, "generated": generated},
        "usage": usage,
    }


@app.post("/api/daily/weak-paper")
def daily_weak_paper(payload: dict):
    """按**薄弱点**组卷：优先考你命中率最低的考点。

    与 /api/daily/generate 的区别：那个按「今天学了什么」出题，
    这个按「你哪里弱」出题 —— 不需要输入学习记录。

    选取规则：
      - 只考虑样本量 >= min_total 的考点（一两次作答不能定强弱）
      - 按命中率升序取前 top_n 个
    """
    n_single = int(payload.get("n_single", 20))
    n_analysis = int(payload.get("n_analysis", 1))
    n_short = int(payload.get("n_short", 1))
    n_essay = int(payload.get("n_essay", 1))
    min_total = int(payload.get("min_total", 2))
    top_n = int(payload.get("top_n", 8))

    w = weakness()
    cand = [x for x in w["items"] if x["total_n"] >= min_total and x["oid"]]
    if not cand:
        return {
            "ok": False,
            "error": f"还没有足够的作答数据（需要某考点至少答过 {min_total} 次）。先做几组练习再来。",
            "weak_nodes": w["summary"]["nodes"],
        }
    cand.sort(key=lambda x: (x["rate"], -x["total_n"]))
    picked = cand[:top_n]
    ids = [x["oid"] for x in picked]
    label = "、".join(x["name"][:14] for x in picked[:3])

    # 复用 generate 的组卷逻辑
    inner = daily_generate({
        "point_ids": ids,
        "n_single": n_single, "n_analysis": n_analysis,
        "n_short": n_short, "n_essay": n_essay,
    })
    inner["focus"] = [
        {"name": x["name"], "path": x["path"], "rate": x["rate"],
         "hit": x["hit_n"], "total": x["total_n"]}
        for x in picked
    ]
    inner["focus_summary"] = label
    return inner


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
    q = con.execute("SELECT stem,extra,qtype FROM questions WHERE id=?", (qid,)).fetchone()
    pts = [dict(r) for r in con.execute(
        "SELECT seq,claim,evidence FROM points WHERE question_id=? ORDER BY seq", (qid,))]
    con.close()
    if not q:
        return {"ok": False, "error": "题目不存在"}
    full = json.loads(q["extra"] or "{}").get("full_score", 15)

    if not student and not image_data_url:
        return {"ok": False, "error": "请填写答案文本，或上传手写照片"}
    try:
        data, usage = ai_mod.grade_answer(
            q["stem"], pts,
            student_text=student or None,
            full_score=full,
            use_thinking=True,
            image_data_url=image_data_url,
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "result": data, "usage": usage}
