# -*- coding: utf-8 -*-
"""采分点挂载迁移的回归测试。

守住三件事：
  1. 空挂载会被继承父题考点；已有的更细粒度挂载**不被覆盖**
  2. 迁移幂等（跑第二遍不再改动任何行）
  3. 挂载补齐后，**主观题采分点确实进入薄弱点统计**（不是只改了字段没人用）
以及：全程在临时副本库上跑，真实库零写入。

跑法：
    cd app
    python tests/test_point_outline.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

REAL_DB = APP.parent / "data" / "kaoyan.db"
TMP = Path(tempfile.mkdtemp(prefix="kaoyan_pts_"))
TEST_DB = TMP / "kaoyan.db"
shutil.copy2(REAL_DB, TEST_DB)
BEFORE = (REAL_DB.stat().st_size, REAL_DB.stat().st_mtime_ns)

os.environ["DSH_DB"] = str(TEST_DB)
import server  # noqa: E402

assert server.DB == TEST_DB, f"没连到副本库：{server.DB}"

FAILED = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


con = sqlite3.connect(TEST_DB)
con.row_factory = sqlite3.Row


def stat():
    return {
        "points_null": con.execute(
            "SELECT COUNT(*) FROM points WHERE outline_id IS NULL").fetchone()[0],
        "tags": {r[0]: r[1] for r in con.execute(
            "SELECT outline_id, COUNT(*) FROM points "
            "WHERE outline_id IS NOT NULL GROUP BY outline_id")},
    }


print("=" * 68)
print("① 迁移前的现状（构造「未挂载」场景）")
print("=" * 68)
# 采分点总数从库里数，别在测试里写死（补题后从 888 变成了 912，硬编码会误报）
n_points = con.execute("SELECT COUNT(*) FROM points").fetchone()[0]
# 把已挂载的都清掉，模拟重建数据库后的原始状态
con.execute("UPDATE points SET outline_id=NULL")
con.commit()
s0 = stat()
check("已清空挂载（构造缺失场景）", s0["points_null"] == n_points,
      f"null={s0['points_null']}，总数={n_points}")

# 记录一个「已有更细粒度挂载」的采分点，稍后验证不被覆盖。
# 注意：只碰这一个点，且它的父题挂载要记下来 —— 不能把它的 outline_id 改成假的，
# 否则会污染后面「采分点进入统计」那一段（第一版就栽在这，判定为测试缺陷）。
detail = con.execute(
    "SELECT p.id, q.outline_id AS parent_oid FROM points p JOIN questions q ON q.id=p.question_id "
    "WHERE q.outline_id IS NOT NULL LIMIT 1").fetchone()
con.execute("UPDATE points SET outline_id=9999 WHERE id=?", (detail["id"],))
con.commit()
check("已埋一个细粒度挂载 outline_id=9999",
      True, f"point id={detail['id']}（父题考点 {detail['parent_oid']}）")

print()
print("=" * 68)
print("② 跑迁移")
print("=" * 68)
fixed = server.backfill_point_outline(con)
check("迁移报告了补齐行数", fixed > 0, f"fixed={fixed}")
s1 = stat()
check("已无空的挂载", s1["points_null"] == 0, f"null={s1['points_null']}")
still = con.execute("SELECT outline_id FROM points WHERE id=?", (detail["id"],)).fetchone()[0]
check("已有的细粒度挂载未被覆盖", still == 9999, f"实际={still}")
con.execute("UPDATE points SET outline_id=? WHERE id=?", (detail["parent_oid"], detail["id"]))
con.commit()
check("（已还原该点，避免污染后续断言）", True)

print()
print("=" * 68)
print("③ 幂等：再跑一遍不应改动任何行")
print("=" * 68)
before_tags = stat()["tags"]
again = server.backfill_point_outline(con)
after_tags = stat()["tags"]
check("第二遍补齐 0 行", again == 0, f"fixed={again}")
check("挂载分布完全没变", before_tags == after_tags)

print()
print("=" * 68)
print("④ 挂载补齐后，主观题采分点确实进入薄弱点统计")
print("=" * 68)
# 造一次真实的主观题作答：一道真题，勾 2 个采分点里的 1 个
# 关键：**不要手动改挂载**（那会同步改掉所有采分点），要选一道题、其采分点已经继承了题目考点的
sub = con.execute(
    "SELECT q.id AS qid, q.outline_id FROM questions q "
    "WHERE q.qtype IN ('short','essay','analysis') AND q.outline_id IS NOT NULL "
    "AND (SELECT COUNT(*) FROM points p WHERE p.question_id=q.id) >= 2 "
    "AND (SELECT COUNT(*) FROM points p WHERE p.question_id=q.id "
    "     AND p.outline_id=q.outline_id) >= 2 "
    "LIMIT 1").fetchone()
pt = con.execute("SELECT id, seq FROM points WHERE question_id=? ORDER BY seq",
                 (sub["qid"],)).fetchall()
con.execute("INSERT INTO sessions (date,mode,created_at) VALUES ('1900-01-01','测试','x')")
sid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
con.execute(
    "INSERT INTO attempts (question_id,date,mode,hits,total,cause,note,session_id) "
    "VALUES (?,?,?,?,?,?,?,?)", (sub["qid"], "1900-01-01", "主观自评", 1, len(pt), None, None, sid))
aid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
con.execute("INSERT INTO point_hits (attempt_id,point_id,hit) VALUES (?,?,1)", (aid, pt[0]["id"]))
con.commit()
check("已造一次主观作答（勾 1/2 个采分点）", True,
      f"question_id={sub['qid']}，2 个采分点都挂在 oid={sub['outline_id']}")
# 诊断口径：直接查 DB 确认 point_hits 确实写进去了
ph = con.execute("SELECT COUNT(*) FROM point_hits WHERE attempt_id=?", (aid,)).fetchone()[0]
check("point_hits 写入成功", ph == 1, f"{ph} 条")

w = server.weakness()["items"]
qid_of_attempt = sub["qid"]
node = next((x for x in w if x["oid"] == sub["outline_id"]), None)
check("该考点出现在薄弱点里", node is not None, f"oid={sub['outline_id']}")
if node:
    # 分母口径 = 该考点下、这道题的**全部**采分点（从库里数，别在测试里写死）
    n_expect = con.execute(
        "SELECT COUNT(*) FROM points p JOIN questions q ON q.id=p.question_id "
        "WHERE p.outline_id=? AND q.id=?", (sub["outline_id"], qid_of_attempt)).fetchone()[0]
    check(f"分母 = 该题全部采分点（{n_expect} 个样本）", node["total_n"] == n_expect,
          f"hit={node['hit_n']}/total={node['total_n']}")
    check("分子 = 命中的 1 个采分点", node["hit_n"] == 1, f"hit_n={node['hit_n']}")
    check("比率 = 命中/总，未超过 100%",
          abs(node["rate"] - 1 / n_expect) < 0.001,   # 接口对 rate 做了三位取整
          f"rate={node['rate']}（1/{n_expect}）")
    check("分母不是只数命中（旧口径的证据）", node["total_n"] > node["hit_n"],
          f"total={node['total_n']} > hit={node['hit_n']}")

print()
print("=" * 68)
print("⑤ 复盘报告的点名依赖挂载：明细里应带出考点名")
print("=" * 68)
detail_rows = server._session_attempts(sid)
check("明细能取到", len(detail_rows) == 1, f"{len(detail_rows)} 条")
check("明细带考点名", bool(detail_rows and detail_rows[0].get("node_name")),
      str(detail_rows[0].get("node_name") if detail_rows else None))

print()
print("=" * 68)
print("⑥ 统一挂载层级：point→section / goal→board，section 与 chapter 不动")
print("=" * 68)


def level_of(table):
    return {r["level"]: r["n"] for r in con.execute(
        f"SELECT o.level, COUNT(*) n FROM {table} t JOIN outline_nodes o ON o.id=t.outline_id "
        f"WHERE t.outline_id IS NOT NULL GROUP BY o.level")}


# 显式播两种「必须保留」的形态：chapter 级与 board 级。
# 不能只依赖前几段留下的残值——否则 level 缺失时断言会静默通过（第一版就是这样）。
# 还要注意：播种用的题目/采分点必须**没有被作答过**，否则会污染「用户作答归属不变」的断言。
seed_ch = con.execute(
    "SELECT id FROM outline_nodes WHERE level='chapter' LIMIT 1").fetchone()[0]
seed_bd = con.execute(
    "SELECT id FROM outline_nodes WHERE level='board' LIMIT 1").fetchone()[0]
seed_pt = con.execute(
    "SELECT id FROM outline_nodes WHERE level='point' LIMIT 1").fetchone()[0]
free_q = con.execute(
    "SELECT id FROM questions WHERE id NOT IN (SELECT question_id FROM attempts) "
    "ORDER BY id LIMIT 1").fetchone()[0]
free_p = con.execute(
    "SELECT p.id FROM points p WHERE NOT EXISTS "
    "(SELECT 1 FROM point_hits ph WHERE ph.point_id=p.id) ORDER BY p.id LIMIT 2").fetchall()
con.execute("UPDATE points SET outline_id=? WHERE id=?", (seed_ch, free_p[0]["id"]))
con.execute("UPDATE points SET outline_id=? WHERE id=?", (seed_bd, free_p[1]["id"]))
con.execute("UPDATE questions SET outline_id=? WHERE id=?", (seed_pt, free_q))
con.commit()
before_q, before_p = level_of("questions"), level_of("points")
check("已播种 chapter/board/point 三种挂载", True,
      f"chapter={seed_ch} board={seed_bd} point={seed_pt}（用无作答的题 q#{free_q}）")

# 用户的作答当前挂在哪个节点 —— 统一层级不能把它改走
attempt_nodes_before = {r[0] for r in con.execute(
    "SELECT DISTINCT q.outline_id FROM attempts a JOIN questions q ON q.id=a.question_id "
    "WHERE q.outline_id IS NOT NULL")}

moved = server.unify_outline_levels(con)
check("迁移报告了移动行数", moved > 0, f"moved={moved}")
after_q, after_p = level_of("questions"), level_of("points")
print(f"     题目层级 {before_q} → {after_q}")
print(f"     采分点层级 {before_p} → {after_p}")

check("题目已无 point 级", "point" not in after_q, str(after_q))
check("采分点已无 point 级", "point" not in after_p, str(after_p))
check("采分点已无 goal 级", "goal" not in after_p, str(after_p))
check("chapter 级被保留（没被下推丢信息）",
      after_p.get("chapter", 0) == before_p.get("chapter", 0) and after_p.get("chapter", 0) > 0,
      f"{before_p.get('chapter')} → {after_p.get('chapter')}")
check("board 级被保留（没被散进 35 个章）",
      after_p.get("board", 0) == before_p.get("board", 0) and after_p.get("board", 0) > 0,
      f"{before_p.get('board')} → {after_p.get('board')}")

attempt_nodes_after = {r[0] for r in con.execute(
    "SELECT DISTINCT q.outline_id FROM attempts a JOIN questions q ON q.id=a.question_id "
    "WHERE q.outline_id IS NOT NULL")}
check("用户已有作答的归属节点没被改走", attempt_nodes_before == attempt_nodes_after,
      f"{attempt_nodes_before} → {attempt_nodes_after}")

moved2 = server.unify_outline_levels(con)
check("统一层级是幂等的", moved2 == 0, f"第二遍 moved={moved2}")

print()
print("=" * 68)
print("⑦ 启动路径：run_migrations() 必须能在真实启动形态下跑通")
print("=" * 68)
# 这一节是为一个真实崩溃加的：unify_outline_levels 被传入「没设 row_factory 的连接」时
# 抛 TypeError（tuple indices must be integers），服务直接起不来；而旧测试自己设了
# row_factory，把这个 bug 掩盖了。所以这里用默认连接形态（和启动时同一种）。
# 注意：必须先关掉本测试自己的写连接，否则第二个连接会 database is locked。
con.close()
check("模块级 DB 指向副本（不带参调用的前提）", server.DB == TEST_DB, str(server.DB))
try:
    fixed2, moved2 = server.run_migrations()
    check("run_migrations() 未抛异常", True, f"补齐={fixed2} 统一={moved2}")
except Exception as e:
    check("run_migrations() 未抛异常", False, f"{type(e).__name__}: {e}")

print()
print("=" * 68)
print("⑧ 复盘报告接口在迁移后的库上仍可用")
print("=" * 68)


def fake_review(session, attempts_detail, weak_points, history_summary):
    return ("## 本次结果\n测试报告", {"prompt_tokens": 1, "completion_tokens": 1})


server.ai_mod.review_session = fake_review
out = server.review_generate({"session_id": sid})
check("生成复盘 ok", out.get("ok") is True, str(out)[:120])
lst = server.reports_list()
check("报告列表可读", lst["summary"]["sessions"] >= 1, str(lst["summary"]))

after = (REAL_DB.stat().st_size, REAL_DB.stat().st_mtime_ns)
check("真实库未被动过", BEFORE == after, f"before={BEFORE} after={after}")

shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全部通过。临时副本已删除。")
