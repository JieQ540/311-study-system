# -*- coding: utf-8 -*-
"""端到端测试：取卷 → 提交一次模拟作答 → 查薄弱点 → 清理测试数据。"""
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8765"
DB = Path(Path(__file__).resolve().parent.parent.parent / "data" / "kaoyan.db")
TEST_DATE = "1900-01-01"  # 测试标记，便于清理


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


paper = get("/api/paper")
qid_single = paper["singles"][0]["id"]
subj = paper["subjectives"][0]
qid_subj = subj["id"]
seqs = [p["seq"] for p in subj["points"]]

print("=== 试卷接口 ===")
print(f"  单选 {paper['counts']['single']} 题，主观 {paper['counts']['subjective']} 题，采分点 {paper['counts']['points']} 个")
print(f"  首题(id={qid_single}): {paper['singles'][0]['stem'][:36]}")
print(f"  首主观题(id={qid_subj}): {subj['stem'][:36]}  采分点 {len(seqs)} 个 -> {seqs}")

# 模拟：答对第 1 题；第 1 道主观题答到前 2 个采分点
payload = {
    "date": TEST_DATE,
    "single": [{"id": qid_single, "answer": "A"}],
    "subjective": [{"id": qid_subj, "hits": seqs[:2], "cause": "记混"}],
}
res = post("/api/save", payload)
print("\n=== 提交接口 ===")
print("  ", res)

weak = get("/api/weakness")
print("\n=== 薄弱点接口 ===")
if weak["items"]:
    for it in weak["items"][:5]:
        print(f"  {it['hit_points']}/{it['total_points']}  {it['rate']:.0%}  {it['path'][:66]}")
else:
    print("  （无数据）")

# 清理测试数据
con = sqlite3.connect(DB)
aids = [r[0] for r in con.execute("SELECT id FROM attempts WHERE date=?", (TEST_DATE,))]
for aid in aids:
    con.execute("DELETE FROM point_hits WHERE attempt_id=?", (aid,))
con.execute("DELETE FROM attempts WHERE date=?", (TEST_DATE,))
con.commit()
left = con.execute("SELECT COUNT(*) FROM attempts WHERE date=?", (TEST_DATE,)).fetchone()[0]
total = con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
con.close()
print(f"\n=== 清理 ===\n  已删除测试记录 {len(aids)} 条，残留 {left} 条；数据库现有正式作答记录 {total} 条")
