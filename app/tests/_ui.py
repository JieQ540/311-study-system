# -*- coding: utf-8 -*-
"""四个 UI 验收共享的生命周期：副本、固定题目、已验身份的服务、截图、清理。"""
import argparse
import asyncio
import contextlib
import json
import os
import sqlite3
import traceback
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from _safety import (APP, Checker, copy_db, live_server, real_db_fingerprint,
                     release_db, require_test_db)


def seed_ui(db):
    """通过生产入库/保存入口造固定场景，避免依赖用户的题号、日期和作答历史。"""
    require_test_db(db)
    import server

    with contextlib.closing(sqlite3.connect(db)) as con, con:
        for table in ("learning_exposures", "pilot_feedback", "pilot_question_links", "curriculum_packages", "pilot_diagnoses", "pilot_runs", "pilot_packages", "retest_runs", "retest_plans", "point_hits", "attempts", "reports", "review_notes", "sessions", "papers"):
            con.execute(f"DELETE FROM {table}")
        oid = con.execute("SELECT id FROM outline_nodes WHERE level='point' ORDER BY id LIMIT 1").fetchone()[0]

    def subject(stem, scores, sections):
        return {"qtype": "short", "stem": stem, "outline_id": oid, "full_score": 15,
                "note": "测试评分说明：按各小问的要点计分。", "sections": sections,
                "points": [{"seq": i, "claim": f"{stem}的第{i}个独立要点",
                            "evidence": "测试判分依据", "score": score,
                            "section": "1" if i <= 2 else "2"}
                           for i, score in enumerate(scores, 1)]}

    single = [{"stem": f"验收样题{i}：课堂中重视学生主动参与，最符合哪一种主张？",
               "outline_id": oid, "options": {"A": "主动学习", "B": "机械背诵", "C": "完全放任", "D": "取消反馈"},
               "answer": "A", "explain": "测试解析：主动参与有助于理解。"} for i in (1, 2)]
    done = subject("请说明反馈对学习的作用", [3, 6, 6], [])
    multi = subject("材料题：（1）说明练习原则。（2）说明反馈方法。", [3, 3, 4, 5],
                    [{"key": "1", "label": "练习原则", "score": 6},
                     {"key": "2", "label": "反馈方法", "score": 9}])
    pending = subject("请说明尚待核定分值的学习原则", [None, None, None], [])
    saved = server._save_ai_questions({"single": single, "subjective": [done, multi, pending]})
    s_done, s_undone = [q["id"] for q in saved["single"]]
    subj_done, subj_undone, subj_noscore = [q["id"] for q in saved["subjective"]]
    # 人工编写的参考答案供折叠/展开断言使用；分值仍仅由统一 AI 题入库入口写入。
    with contextlib.closing(sqlite3.connect(db)) as con, con:
        for q in saved["subjective"]:
            extra = json.loads(q["row"]["extra"])
            extra["answer_text"] = "测试参考答案：先明确学习目标，再安排练习与及时反馈。"
            con.execute("UPDATE questions SET extra=? WHERE id=?", (json.dumps(extra, ensure_ascii=False), q["id"]))
    date = "2099-01-01"

    def save(note, qids=None):
        body = {"date": date, "mode": "日常练习", "note": note,
                "single": [{"id": s_done, "answer": "B"}],
                "subjective": [{"id": subj_done, "hits": [1], "answer_text": "上次写的作答原文"}]}
        if qids is not None:
            body["question_ids"] = qids
        return server.save(body)["saved"]["session_id"]

    done_sid = save("验收：全部作答", [s_done, subj_done])
    old_sid = save("验收：旧记录没有题单")
    ids = [s_done, s_undone, subj_undone, subj_done, subj_noscore]
    sid = save("验收：完整题单与部分作答", ids)
    with contextlib.closing(sqlite3.connect(db)) as con, con:
        # 固定模拟卷：页面仍通过真实 /api/paper 加载、持久化逻辑回读。
        con.execute("INSERT INTO papers (created_at,label,single_ids,subjective_ids,status) VALUES (?,?,?,?,?)",
                    (date, "UI 验收卷", json.dumps([s_done, s_undone]),
                     json.dumps([subj_undone, subj_done, subj_noscore]), "active"))
        cur = con.execute("INSERT INTO reports (session_id,date,mode,content,metrics,created_at) VALUES (?,?,?,?,?,?)",
                          (done_sid, date, "日常练习", "## 验收报告\n\n测试复盘：复习反馈与练习的关系。",
                           json.dumps({"objective_rate": 0, "subjective_score": 3, "subjective_full": 15}), date))
        report_id = cur.lastrowid
    return {"sid": sid, "done_sid": done_sid, "old_sid": old_sid, "report_id": report_id,
            "date": date, "s_done": s_done, "s_undone": s_undone, "subj_done": subj_done,
            "subj_undone": subj_undone, "subj_noscore": subj_noscore, "total": 5, "unanswered": 3}


class UI:
    def __init__(self, name, srv, ck):
        self.srv, self.ck = srv, ck
        self.base, self.db = srv.base, srv.db
        root = os.environ.get("REGRESSION_ARTIFACT_DIR")
        if root is None:
            root = APP.parent / "source" / "_shots" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
        self.shots = Path(root) / name
        self.shots.mkdir(parents=True, exist_ok=True)
        self.fixture = seed_ui(self.db)

    def check(self, name, cond, extra=""):
        return self.ck.check(name, cond, extra)

    def equal(self, name, got, want):
        return self.check(name, got == want, f"得到 {got!r}，期望 {want!r}")

    async def shot(self, page, name):
        path = (self.shots / name).resolve()
        await page.screenshot(path=path, full_page=False)
        print(f"[ARTIFACT] {path}")

    @contextlib.asynccontextmanager
    async def page(self, viewport=None):
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context = await browser.new_context(viewport=viewport or {"width": 1240, "height": 1000})
                errors = []

                async def local_only(route):
                    if route.request.url.startswith(self.base + "/"):
                        await route.continue_()
                    else:
                        errors.append("拒绝外部请求：" + route.request.url.split("?")[0])
                        await route.abort()

                await context.route("**/*", local_only)
                page = await context.new_page()
                page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
                page.on("console", lambda m: errors.append("console: " + m.text) if m.type == "error" else None)
                try:
                    yield page
                except BaseException:
                    with contextlib.suppress(Exception):
                        await self.shot(page, "failure.png")
                    raise
                finally:
                    self.check("页面无 console/pageerror 或外部请求", not errors, str(errors[:3]))
            finally:
                await browser.close()


def run_ui(name, scenario):
    """唯一启动入口。失败、超时、异常同样清理服务与副本，并核对真实库。"""
    argparse.ArgumentParser(description="自动创建副本库与独立测试服务，不接受外部服务地址").parse_args()
    before = real_db_fingerprint()
    ck = Checker(name)
    db = None
    try:
        db = copy_db(name)
        with live_server(db) as srv:
            srv.identity_checks(ck)
            ui = UI(name, srv, ck)
            asyncio.run(scenario(ui))
            ck.check("验收没有尝试调用付费 AI", not srv.ai_calls, f"{len(srv.ai_calls)} 次")
    except Exception as e:
        ck.check("验收执行完成", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        if db is not None:
            try:
                release_db(db)
                ck.check("临时数据库已清理", not db.parent.exists())
            except OSError as e:
                ck.check("临时数据库已清理", False, str(e))
        ck.check("真实库及事务日志指纹未变", before == real_db_fingerprint())
    return ck.finish()
