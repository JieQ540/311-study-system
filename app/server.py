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
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parents[1]   # 公开副本：相对路径
# DSH_DB 覆盖只给测试用：把库指向副本，跑完删掉，绝不动真实数据（见 docs/交接文档.md §5.1）
# 公开副本：优先用户自己的库，没有就用随包的种子题库（这样 clone 即能用）
_LIVE_DB = ROOT / "data" / "kaoyan.db"
_SEED_DB = ROOT / "data" / "kaoyan-seed.db"
DB = Path(os.environ.get("DSH_DB") or (_LIVE_DB if _LIVE_DB.exists() else _SEED_DB))
STATIC = ROOT / "app" / "static"
INDEX = STATIC / "index.html"

app = FastAPI(title="311 备考系统")

EXAM_MINUTES = 180  # 311 考试时长


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


def ensure_schema():
    """建表 / 迁移（幂等）。每次进程启动调用一次。"""
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
        """)

        # 迁移：把作答记录挂到具体的 session 上。
        # 在此之前只能靠「日期 + 模式」猜是哪一次练习，复盘报告里的「本次作答明细」会串场。
        cols = {r[1] for r in con.execute("PRAGMA table_info(attempts)")}
        if "session_id" not in cols:
            con.execute("ALTER TABLE attempts ADD COLUMN session_id INTEGER")
        con.execute("CREATE INDEX IF NOT EXISTS idx_attempts_session ON attempts(session_id)")
        con.commit()

        # 迁移：补齐采分点的大纲挂载（继承所属题目的考点）
        backfill_point_outline(con)
        # 迁移：统一挂载层级（point/goal → section/board）
        unify_outline_levels(con)
        # 视图：把「真题 ≠ AI 生成」这个过滤固化下来。
        # 为什么：此前吃过一次亏——查询里漏加 `source NOT LIKE 'AI%'`，
        # 结果 AI 生成的题混进了「真题优先」的抽取结果，而且不报错、只是数据悄悄变脏。
        # 视图名用 v_ 前缀，可被任何消费方（脚本 / 前端接口 / 报告）复用。
        con.executescript("""
        DROP VIEW IF EXISTS v_real_questions;
        CREATE VIEW v_real_questions AS
            SELECT * FROM questions
            WHERE source IS NULL OR source NOT LIKE 'AI%';

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


def link_attempts_to_session(session_id, after_id=None):
    """把刚写入、还没挂 session 的作答记录，挂到这次练习上。

    为什么不能只靠「日期 + 模式」：一天可以练好几组，模式也可能重复，
    靠它们取「本次作答明细」会把别的练习串进来。
    after_id 给定时只认 id 更大的记录（本次提交的边界），避免把历史孤立数据一并卷走。
    """
    if not session_id:
        return 0
    sql = "UPDATE attempts SET session_id=? WHERE session_id IS NULL"
    args = [session_id]
    if after_id is not None:
        sql += " AND id > ?"
        args.append(after_id)
    con = sqlite3.connect(DB)
    try:
        cur = con.execute(sql, args)
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def page(name):
    return HTMLResponse((STATIC / name).read_text(encoding="utf-8"))


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

    # 本次写入的边界哨兵：记下插入前的最大 attempt id，之后只把 id 更大的记录算作「本次」。
    # 不用「日期+模式」判定——一天能练好几组、模式也可能重复，那样会把别的练习串进来。
    # question_id 用 0：真实题目 id 从 1 起，哨兵不会和任何题目关联（库里该列有 NOT NULL）。
    ensure_schema()
    marker = exec_sql(
        "INSERT INTO attempts (question_id,date,mode,hits,total,cause,note) VALUES (?,?,?,?,?,?,?)",
        (0, date, "_本次提交起点_", 0, 0, None, None))

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
        ensure_schema()
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

        # 把本次写入的作答挂到这个 session 上（哨兵之后插入的都算本次），复盘时才能精确取明细
        link_attempts_to_session(saved["session_id"], after_id=marker)
    except Exception as e:
        saved["log_error"] = f"{type(e).__name__}: {e}"
    finally:
        # 哨兵只在提交期间存在，不能留在作答记录里
        try:
            exec_sql("DELETE FROM attempts WHERE id=? AND mode='_本次提交起点_'", (marker,))
        except Exception:
            pass

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
    ensure_schema()
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
    return page("index.html")


@app.get("/daily", response_class=HTMLResponse)
def daily_page():
    """日常练习模式：输入记录 → 确认考点 → 出题 → 批改。"""
    return page("daily.html")


@app.get("/logs", response_class=HTMLResponse)
def logs_page():
    """练习日志：每次模拟 / 日常练习的得分与薄弱点快照。"""
    return page("logs.html")


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


# ============================================================ ⑤ 学习复盘报告


def _session_attempts(session_id):
    """取某一次练习的作答明细，并带上题目类型与考点名。

    用 LEFT JOIN：题目被删/不存在时也要能取到这条件，否则「有作答却查不到」
    会被误报成「没有作答记录」，排查时看不出真正原因（已踩过）。
    """
    return rows(
        "SELECT a.id, a.question_id, a.hits, a.total, a.cause, a.mode, "
        "       q.qtype, q.stem, o.name AS node_name, o.path AS node_path "
        "FROM attempts a "
        "LEFT JOIN questions q ON q.id = a.question_id "
        "LEFT JOIN outline_nodes o ON o.id = q.outline_id "
        "WHERE a.session_id = ? ORDER BY a.id",
        (session_id,),
    )


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


def _build_metrics(sess, attempts, weak_snapshot):
    """本次练习的量化指标快照（报告与趋势图都用它，保证报告有数字可依据）。"""
    single = [a for a in attempts if a["qtype"] == "single"]
    subj = [a for a in attempts if a["qtype"] != "single"]
    sr = sum(1 for a in single if a["hits"] == 1)
    hit = sum(int(a["hits"] or 0) for a in subj)
    tot = sum(int(a["total"] or 0) for a in subj)
    return {
        "session_id": sess["id"],
        "date": sess["date"],
        "mode": sess["mode"],
        "objective_total": len(single),
        "objective_right": sr,
        "objective_score": sr * 2,
        "subjective_count": len(subj),
        "objective_rate": round(sr / len(single), 3) if single else None,
        "point_hits": hit,
        "point_total": tot,
        "point_rate": round(hit / tot, 3) if tot else None,
        "weak_points": [
            {"name": w.get("name"), "path": w.get("path"),
             "hit": w.get("hit"), "total": w.get("total"), "rate": w.get("rate")}
            for w in weak_snapshot[:10]
        ],
    }


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

    try:
        snap = json.loads(sess.get("weak_snapshot") or "[]")
    except Exception:
        snap = []
    # 快照缺失（早期数据）时现算一份，保证报告能点名考点
    if not snap:
        w = weakness()
        snap = [{"name": i["name"], "path": i["path"], "hit": i["hit_n"],
                 "total": i["total_n"], "rate": i["rate"]} for i in w["items"][:12]]

    history = rows(
        "SELECT id,date,mode,objective_total,objective_right,objective_score,"
        "subjective_count,point_hits,point_total FROM sessions "
        "WHERE id <> ? ORDER BY id DESC LIMIT 10", (sid,))
    history = list(reversed(history))

    # 先算指标（依赖原始 qtype 值），再换成中文题型给 AI 看 —— 顺序反过来会算出全 0（踩过）
    metrics = _build_metrics(sess, attempts, snap)
    metrics["legacy_match"] = legacy          # 明细是按「日期+模式」补挂的早期数据
    metrics["history_count"] = len(history)
    metrics["missing_question"] = missing_q   # 明细里题目已不存在（被删）的条数

    for a in attempts:
        a["qtype"] = {"single": "单选", "analysis": "辨析", "short": "简答",
                      "essay": "分析论述"}.get(a["qtype"], a["qtype"] or "题目已不存在")

    try:
        content, usage = ai_mod.review_session(sess, attempts, snap, history)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

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
            "content": content, "metrics": metrics, "usage": usage}


@app.get("/api/reports")
def reports_list(limit: int = 200):
    """复盘报告列表 + 总体趋势。含「已练过但还没生成报告」的练习，供前端补生成。"""
    ensure_schema()

    items = []
    for r in rows(
        "SELECT r.id AS report_id, r.session_id, r.date, r.mode, r.content, r.metrics, "
        "       r.created_at, s.objective_total, s.objective_right, s.objective_score, "
        "       s.subjective_count, s.point_hits, s.point_total, s.note, s.weak_snapshot "
        "FROM sessions s LEFT JOIN reports r ON r.session_id = s.id "
        "ORDER BY s.id DESC LIMIT ?", (limit,)):
        try:
            r["metrics"] = json.loads(r["metrics"] or "null")
        except Exception:
            r["metrics"] = None
        try:
            r["weak_snapshot"] = json.loads(r["weak_snapshot"] or "[]")
        except Exception:
            r["weak_snapshot"] = []
        items.append(r)

    trend = []
    for r in reversed([x for x in items if x["report_id"]]):
        m = r["metrics"] or {}
        trend.append({
            "report_id": r["report_id"], "session_id": r["session_id"],
            "date": r["date"], "mode": r["mode"],
            "objective_rate": m.get("objective_rate"),
            "point_rate": m.get("point_rate"),
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
    }
    return {"items": items, "trend": trend, "summary": summary}


@app.get("/api/reports/{report_id}")
def report_one(report_id: int):
    r = rows("SELECT * FROM reports WHERE id=?", (report_id,))
    if not r:
        return JSONResponse({"ok": False, "error": "报告不存在"}, status_code=404)
    out = r[0]
    try:
        out["metrics"] = json.loads(out["metrics"] or "null")
    except Exception:
        out["metrics"] = None
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
