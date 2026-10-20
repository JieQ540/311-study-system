# -*- coding: utf-8 -*-
"""复盘链路回归测试。

规矩（docs/交接文档.md §5.1）：**绝不在用户的真实库上做测试**。
本测试把 data/kaoyan.db 复制成临时副本，用 DSH_DB 让 server 连副本，跑完删掉。
AI 调用用桩替代，因此本测试不花钱、不联网。

跑法：
    cd app
    python tests/test_review.py
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

REAL_DB = APP.parent / "data" / "kaoyan.db"

TMP = Path(tempfile.mkdtemp(prefix="kaoyan_test_"))
TEST_DB = TMP / "kaoyan.db"
shutil.copy2(REAL_DB, TEST_DB)
# 记录真实库指纹，测试结束用它确认真实库没被动过
BEFORE = (REAL_DB.stat().st_size, REAL_DB.stat().st_mtime_ns)

os.environ["DSH_DB"] = str(TEST_DB)

import server  # noqa: E402  （必须在设好 DSH_DB 之后导入）

assert server.DB == TEST_DB, f"server 没有连到副本库：{server.DB}"

FAILED = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


def seed_attempts(count=6, subj=1):
    """往副本库里塞几次「作答」，模拟用户练过几组。"""
    con = sqlite3.connect(TEST_DB)
    con.row_factory = sqlite3.Row
    singles = con.execute(
        "SELECT id FROM questions WHERE qtype='single' AND answer IS NOT NULL LIMIT ?",
        (count,)).fetchall()
    subs = con.execute(
        "SELECT id FROM questions WHERE qtype IN ('short','analysis','essay') LIMIT ?",
        (subj,)).fetchall()
    con.close()
    return [r["id"] for r in singles], [r["id"] for r in subs]


print("=" * 68)
print("① 建表 / 迁移（副本库）")
print("=" * 68)
server.ensure_schema()
con = sqlite3.connect(TEST_DB)
tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
cols = {r[1] for r in con.execute("PRAGMA table_info(attempts)")}
con.close()
check("reports 表已建出", "reports" in tables)
check("attempts.session_id 已迁移", "session_id" in cols)

print()
print("=" * 68)
print("② /api/save 写练习日志 + 把作答挂到 session")
print("=" * 68)
# 副本库里的历史作答**数量与状态都不固定** —— 用户自己练过就会有带 session 的新记录。
# 所以只记录基线、断言"新增了多少 + 老的没被动"，不要写死数量（写死过一次，用户一练习测试就红）。
con = sqlite3.connect(TEST_DB)
old = con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
orphans_before = {r[0] for r in con.execute(
    "SELECT id FROM attempts WHERE session_id IS NULL")}
base_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
base_reports = con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
con.close()
# 「还没生成报告」的练习数也要取基线：用户练完不一定马上生成报告，
# 写死 pending==0 会被他自己的正常使用弄红（2026-09-18 真踩到：他练了第 3 组但没生成报告）
base_pending = server.reports_list()["summary"]["pending"]
print(f"  基线：{old} 条历史作答（未挂 session 的 {len(orphans_before)} 条）、"
      f"{base_sessions} 条练习日志、{base_reports} 份报告（其中待生成 {base_pending} 份）")

sids, subids = seed_attempts(6, 1)
r1 = server.save({
    "date": "2026-09-16", "mode": "日常练习", "note": "测试第 1 组",
    "single": [{"id": sids[0], "answer": "A"}, {"id": sids[1], "answer": "B"},
               {"id": sids[2], "answer": "C"}, {"id": sids[3], "answer": "D"},
               {"id": sids[4], "answer": "A"}, {"id": sids[5], "answer": "B"}],
    "subjective": [{"id": subids[0], "hits": [1, 2], "cause": "记混"}],
})
sid1 = r1["saved"].get("session_id")
check("save 返回 session_id", bool(sid1), f"session_id={sid1}")
check("无 log_error", "log_error" not in r1["saved"], r1["saved"].get("log_error", ""))
con = sqlite3.connect(TEST_DB)
n_linked = con.execute("SELECT COUNT(*) FROM attempts WHERE session_id=?", (sid1,)).fetchone()[0]
orphans_after = {r[0] for r in con.execute(
    "SELECT id FROM attempts WHERE session_id IS NULL")}
total_after = con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
con.close()
check("本次作答都挂到了 session", n_linked == 7, f"linked={n_linked}（6 客观 + 1 主观）")
check("历史孤立作答没被误挂", orphans_after == orphans_before,
      f"孤立 {len(orphans_before)} → {len(orphans_after)}")
check("本次只新增 7 条作答", total_after == old + 7, f"{old} → {total_after}")
con = sqlite3.connect(TEST_DB)
sentinels = con.execute(
    "SELECT COUNT(*) FROM attempts WHERE mode='_本次提交起点_'").fetchone()[0]
con.close()
check("边界哨兵没有残留", sentinels == 0, f"残留 {sentinels} 条")

print()
print("=" * 68)
print("③ /api/review/generate 生成报告（AI 用桩）")
print("=" * 68)
CALLS = {}


def fake_review(session, attempts_detail, weak_points, history_summary):
    CALLS["session"] = session
    CALLS["attempts"] = attempts_detail
    CALLS["weak"] = weak_points
    CALLS["history"] = history_summary
    body = ("## 本次结果\n客观 3/6（50%）。\n\n## 暴露的问题\n"
            "- 教育学原理 › 教育目的：辨析题采分点漏 2 个\n\n"
            "## 与历史对比\n样本太少，暂无法判断趋势。\n\n"
            "## 下次练什么\n1. 重做教育目的相关辨析题")
    return body, {"prompt_tokens": 900, "completion_tokens": 300}


# 真函数先留一份：⑦b 要**真跑** review_session（只桩掉传输层）才守得住预算与截断这两件事
_REAL_REVIEW_FN = server.ai_mod.review_session
server.ai_mod.review_session = fake_review
out = server.review_generate({"session_id": sid1})
check("接口返回 ok", out.get("ok") is True, str(out)[:160])
check("报告内容非空", bool((out.get("content") or "").strip()))
check("拿到 report_id", bool(out.get("report_id")))
m = out.get("metrics") or {}
check("metrics 有客观正确率",
      isinstance(m.get("objective_rate"), (int, float)), str(m.get("objective_rate")))
check("metrics 落到了数据库", bool(server.rows("SELECT metrics FROM reports WHERE id=?",
                                                (out["report_id"],))[0]["metrics"]))
check("喂给 AI 的明细含考点名", any(a.get("node_name") for a in CALLS["attempts"]),
      f"{len(CALLS['attempts'])} 条明细")
check("喂给 AI 的明细只有本次（7 条）", len(CALLS["attempts"]) == 7,
      f"实际 {len(CALLS['attempts'])} 条")

print()
print("=" * 68)
print("④ 第二次练习 → 报告能看到历史（趋势对比的前提）")
print("=" * 68)
r2 = server.save({
    "date": "2026-09-17", "mode": "模拟考试", "note": "测试第 2 组",
    "single": [{"id": sids[0], "answer": "A"}, {"id": sids[1], "answer": "B"}],
    "subjective": [],
})
sid2 = r2["saved"].get("session_id")
out2 = server.review_generate({"session_id": sid2})
check("第二份报告生成成功", out2.get("ok") is True, str(out2)[:160])
# 历史条数取决于副本库原有 session 数（用户练过就有），所以按 baseline 相对断言。
# 但 `report_history()` **只喂最近 10 次**（server.py 里写明的上限），所以会话攒到 10 次以上时
# 这里必然只有 10 条。2026-10-09 实测：真实库到第 10 次练习这天，旧断言（== base+1）就红了，
# 与当轮改动无关（在干净 HEAD 上复现过）。断言按上限收敛。
expect_history = min(base_sessions + 1, 10)
check("第二份报告能看到全部历史（受「只喂最近 10 次」的既有上限约束）",
      len(CALLS["history"]) == expect_history,
      f"history={len(CALLS['history'])}（原有 {base_sessions} + 本次新建的 1 次，上限 10）")
check("第二份报告只有本次 2 条明细", len(CALLS["attempts"]) == 2,
      f"实际 {len(CALLS['attempts'])} 条")

print()
print("=" * 68)
print("⑤ GET /api/reports 列表 + 趋势")
print("=" * 68)
lst = server.reports_list()
expect_sessions = base_sessions + 2          # 原有 + 本测试新建 2 次
check("列表条数 = 练习次数", len(lst["items"]) == expect_sessions,
      f"items={len(lst['items'])}，期望 {expect_sessions}")
check("本测试的 2 份报告都在", lst["summary"]["reports"] >= 2, str(lst["summary"]))
check("待生成份数 = 基线（本测试的 2 次都生成了报告）",
      lst["summary"]["pending"] == base_pending,
      f'pending={lst["summary"]["pending"]}，基线={base_pending}')
check("趋势点数 = 已有报告的练习数", len(lst["trend"]) == lst["summary"]["reports"],
      f"trend={len(lst['trend'])} reports={lst['summary']['reports']}")
# `objective_rate` 与"这次有没有客观题"必须一致：**续练只重做主观题时，客观题 0 道、
# 正确率本来就该是 None**（2026-10-10 用户那次续练就是这种，旧断言假设"每次都有客观题"
# 才红的 —— 断言错在假设，不在数据）。
_con = sqlite3.connect(TEST_DB)
_con.row_factory = sqlite3.Row
_obj_total = {r["id"]: (r["objective_total"] or 0)
              for r in _con.execute("SELECT id, objective_total FROM sessions")}
_con.close()
_rate_ok = all((t["objective_rate"] is None) == (_obj_total.get(t["session_id"], 0) == 0)
               for t in lst["trend"])
check("趋势点的 objective_rate 与「这次有没有客观题」一致（没客观题才允许 None）",
      _rate_ok,
      str([(t["session_id"], _obj_total.get(t["session_id"], 0), t["objective_rate"])
           for t in lst["trend"]]))
check("metrics 已解析成 dict", isinstance(lst["items"][0].get("metrics"), (dict, type(None))))

print()
print("=" * 68)
print("⑥ 缺 session_id 时兜底取最近一次；删除报告")
print("=" * 68)
out3 = server.review_generate({})
check("不给 session_id 也能生成（取最近一次）", out3.get("ok") is True, str(out3)[:160])
check("生成的是最近那次练习", out3.get("session_id") == sid2,
      f"{out3.get('session_id')} vs {sid2}")
n_rep = server.rows("SELECT COUNT(*) c FROM reports")[0]["c"]
check("同一 session 覆盖而不是重复插入", n_rep == base_reports + 2,
      f"reports={n_rep}，期望 原有 {base_reports} + 本测试 2")
server.report_delete(out3["report_id"])
check("删除生效", server.rows("SELECT COUNT(*) c FROM reports")[0]["c"] == base_reports + 1)
check("删报告不动作答记录",
      server.rows("SELECT COUNT(*) c FROM attempts")[0]["c"] == old + 9)

print()
print("=" * 68)
print("⑦ 空数据态（新建一个空库跑一遍，确认不报错）")
print("=" * 68)
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
    e1 = server.review_generate({})
    e2 = server.reports_list()
    check("空库：生成报告给出友好提示", e1.get("ok") is False and "练习" in (e1.get("error") or ""),
          str(e1.get("error")))
    check("空库：列表接口不炸", e2["summary"]["sessions"] == 0, str(e2["summary"]))
finally:
    server.DB = orig_db

print()
print("=" * 68)
print("⑦b 复盘预算与「截断/空正文不入库」（2026-10-10 事故的回归）")
print("=" * 68)
# 事故现场：复盘把 max_tokens 写死 1500，用户把全局思考开到 effort=max →
# 推理 token 与正文共享那 1500，输出恰好 1500、正文 0 字符、finish_reason=length；
# 而 review_generate() 不看 finish_reason、也不看正文是否为空，**把空报告存进了库**
# （report 11 = 0 字符、report 10 = 10 字符），还会覆盖掉原来的好报告。
_REAL_REVIEW = _REAL_REVIEW_FN                       # 上面存下来的真函数（本段要真跑它）
_saved_cfg_rev = server.ai_mod._CFG
_saved_chat_rev = server.ai_mod.chat
CHAT_KW = []


def _review_chat(messages, **kw):
    """桩在**传输层**：prompt/预算/思考解析全部真跑，只把网络调用换掉。"""
    CHAT_KW.append(kw)
    return _REVIEW_REPLY[0], _REVIEW_REPLY[1]


_REVIEW_REPLY = ["(还没设置)", {"finish_reason": "stop"}]


def _with_thinking(mode, effort=None):
    cfg = dict(_saved_cfg_rev or {})
    api = dict(cfg.get("api") or {})
    api["thinking"] = mode
    if effort:
        api["thinking_effort"] = effort
    cfg["api"] = api
    server.ai_mod._CFG = cfg


try:
    server.ai_mod.review_session = _REAL_REVIEW
    server.ai_mod.chat = _review_chat

    # ① 预算按思考分档：开思考必须显著放宽（否则推理一开就把正文挤没）
    _with_thinking("disabled")
    _REVIEW_REPLY = ["## 报告\n" + "正文。 " * 40, {"finish_reason": "stop",
                                                   "prompt_tokens": 100, "completion_tokens": 200}]
    CHAT_KW.clear()
    _r = server.review_generate({"session_id": sid1, "save": False})
    _plain_budget = CHAT_KW[-1].get("max_tokens")
    _with_thinking("enabled", "max")
    CHAT_KW.clear()
    _r2 = server.review_generate({"session_id": sid1, "save": False})
    _think_budget = CHAT_KW[-1].get("max_tokens")
    check("复盘预算按思考分档（开思考时显著放宽）",
          (_plain_budget or 0) >= 256 and (_think_budget or 0) >= 4000
          and _think_budget > _plain_budget,
          f"关思考 {_plain_budget} / 开思考 {_think_budget}")
    check("复盘超时取本场景的值（那个旋钮不是假的）",
          CHAT_KW[-1].get("timeout") == server.ai_mod.scenario_value("review",
                                                                    "timeout_seconds", 180),
          str(CHAT_KW[-1].get("timeout")))

    # ② 被截断的正文：报错、说清病因、**不写库**（原有报告原样保留）
    _good_content = "## 好报告\n" + "这是原来那份写完的报告。 " * 20
    _good = server.review_generate({"session_id": sid1, "save": True})
    check("先存一份正常的报告（对照组）", _good.get("ok") is True, str(_good)[:120])
    _before_row = server.rows("SELECT id, content, created_at FROM reports WHERE session_id=?",
                              (sid1,))
    _REVIEW_REPLY = ["# 学习报告（202", {"finish_reason": "length", "prompt_tokens": 900,
                                        "completion_tokens": 1500, "reasoning_tokens": 1500}]
    _bad = server.review_generate({"session_id": sid1, "save": True})
    check("被截断 → 接口返回 ok=False（不再假装成功）", _bad.get("ok") is False, str(_bad)[:120])
    check("被截断 → 报错说清是截断＋怎么救（调预算或关思考）",
          "截断" in (_bad.get("error") or "") and "预算" in (_bad.get("error") or "")
          and "思考" in (_bad.get("error") or ""), (_bad.get("error") or "")[:90])
    check("被截断 → 标记 truncated 供页面区分", _bad.get("truncated") is True, "")
    _after_row = server.rows("SELECT id, content, created_at FROM reports WHERE session_id=?",
                             (sid1,))
    check("被截断 → **没有写库**：原有报告一字未改",
          bool(_before_row) and bool(_after_row)
          and _after_row[0]["content"] == _before_row[0]["content"]
          and _after_row[0]["created_at"] == _before_row[0]["created_at"],
          f"长度 {len(_after_row[0]['content']) if _after_row else None}")

    # ③ 空正文（哪怕 finish_reason=stop）也不入库
    _REVIEW_REPLY = ["   ", {"finish_reason": "stop", "prompt_tokens": 900,
                             "completion_tokens": 0}]
    _empty = server.review_generate({"session_id": sid1, "save": True})
    check("空正文 → ok=False 且不入库", _empty.get("ok") is False, str(_empty)[:120])
    _after2 = server.rows("SELECT content FROM reports WHERE session_id=?", (sid1,))
    check("空正文 → 原有报告仍然没被动过",
          _after2[0]["content"] == _before_row[0]["content"], "")

    # ④ 连通性自检必须强制关思考：只给 32 个输出 token，开思考必然返回空串
    _with_thinking("enabled", "max")
    CHAT_KW.clear()
    server.ai_mod.chat = _review_chat
    _REVIEW_REPLY = ["连通", {"finish_reason": "stop"}]
    server.ai_mod.ping()
    check("「测试连接」强制关思考（否则思考吃光 32 token，页面显示连接正常但回复是空的）",
          CHAT_KW[-1].get("thinking") is False, str(CHAT_KW[-1].get("thinking")))
finally:
    server.ai_mod.review_session = fake_review
    server.ai_mod.chat = _saved_chat_rev
    server.ai_mod._CFG = _saved_cfg_rev

print()
print("=" * 68)
print("⑧ 确认真实库一字未动")
print("=" * 68)
after = (REAL_DB.stat().st_size, REAL_DB.stat().st_mtime_ns)
check("kaoyan.db 大小/修改时间未变", BEFORE == after,
      f"before={BEFORE} after={after}")

shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全部通过。临时库已删除：", TMP)
