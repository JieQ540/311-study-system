# -*- coding: utf-8 -*-
"""给未挂考点的题目补挂大纲节（section）。

背景：真题抽取依赖 questions.outline_id，但 2025 单选 43 道全是空的，
      导致「真题优先」形同虚设。

策略：分批把题干 + 大纲节清单交给 AI，让它为每题选一个 section。
      挂到 section 级（不是 point），因为单选考的是小知识点，
      section 粒度足够用于抽题，且更稳。

用法：
    python tag_questions.py                  # 全部未挂载的真题
    python tag_questions.py --ids 1123,1124  # 只挂指定题目（补题后定向挂载用）
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import ai

# 与 server.py 一致：DSH_DB 可把库指向副本
ROOT = Path(__file__).resolve().parents[1]   # 公开副本：相对路径
DB = Path(os.environ.get("DSH_DB") or (ROOT / "data" / "kaoyan.db"))
BATCH = 8

SYS = """你是 311 教育学考纲归类助手。
下面给你若干道题的题干，以及可选的**大纲节(section)清单**。
请为每道题选出它考查的**一个** section，返回 JSON。

规则：
1. 只能从清单里选，输出其 id。
2. 若实在无法判断，id 用 null。
3. 单选题、辨析题、简答题、分析论述题都按同一口径：选**这道题主要考的那一节**。
   分析论述题若跨多节，选最核心的一节。
4. 返回：{"tags":[{"qid":题目id,"section_id":节点id,"reason":"简短依据"}]}
"""


def sections():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id,name,path FROM outline_nodes WHERE level='section' ORDER BY id"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def pending(only_ids=None):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    if only_ids:
        qs = ",".join("?" * len(only_ids))
        rows = con.execute(
            f"SELECT id,number,substr(stem,1,600) AS stem FROM questions "
            f"WHERE id IN ({qs}) ORDER BY id", only_ids).fetchall()
    else:
        rows = con.execute(
            "SELECT id,number,substr(stem,1,300) AS stem FROM questions "
            "WHERE (outline_id IS NULL OR outline_id=0) AND source LIKE '%真题%' "
            "ORDER BY CAST(number AS INTEGER) NULLS FIRST"
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def main():
    only = None
    if "--ids" in sys.argv:
        raw = sys.argv[sys.argv.index("--ids") + 1]
        only = [int(x) for x in raw.split(",") if x.strip().isdigit()]
        print(f"定向挂载 {len(only)} 道：{only}")

    secs = sections()
    qs = pending(only)
    if not qs:
        print("没有待归类的题目")
        return
    print(f"待归类 {len(qs)} 道，大纲节 {len(secs)} 个，每批 {BATCH} 道")

    catalog = "\n".join(f"{s['id']}\t{s['path']}" for s in secs)
    con = sqlite3.connect(DB)
    total_in = total_out = 0
    done = 0
    for i in range(0, len(qs), BATCH):
        chunk = qs[i:i + BATCH]
        qlist = "\n".join(f"{q['id']}\t{q['stem']}" for q in chunk)
        user = f"""大纲节清单（id\t路径）：
{catalog}

待归类题目（id\t题干）：
{qlist}

请为每道题选一个 section，返回 JSON。"""
        try:
            data, usage = ai.chat_json(
                [{"role": "system", "content": SYS}, {"role": "user", "content": user}],
                max_tokens=1500,
            )
        except Exception as e:
            print(f"  批次 {i//BATCH+1} 失败: {e}")
            continue
        total_in += usage.get("prompt_tokens", 0)
        total_out += usage.get("completion_tokens", 0)

        valid = {s["id"] for s in secs}
        for t in data.get("tags", []):
            qid, sid = t.get("qid"), t.get("section_id")
            if isinstance(qid, int) and sid in valid:
                con.execute("UPDATE questions SET outline_id=? WHERE id=?", (sid, qid))
                done += 1
        con.commit()
        print(f"  批次 {i//BATCH+1}/{(len(qs)+BATCH-1)//BATCH} 完成，累计挂载 {done}")

    con.close()
    cost = total_in / 1e6 * 1 + total_out / 1e6 * 4
    print(f"\n挂载完成 {done}/{len(qs)} 道   tokens {total_in}/{total_out}  ≈ {cost:.4f} 元")

    # 复核
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    n = con.execute(
        "SELECT COUNT(*) FROM questions WHERE outline_id IS NOT NULL AND source LIKE '%真题%'"
    ).fetchone()[0]
    n_all = con.execute("SELECT COUNT(*) FROM questions WHERE source LIKE '%真题%'").fetchone()[0]
    print(f"真题库已挂考点: {n}/{n_all}")
    print("\n抽样：")
    for r in con.execute("""
        SELECT q.number, o.path FROM questions q JOIN outline_nodes o ON o.id=q.outline_id
        WHERE q.source LIKE '%真题%' ORDER BY CAST(q.number AS INTEGER) LIMIT 6"""):
        print(f"  [{r['number']:>2}] {r['path'][:66]}")
    con.close()


if __name__ == "__main__":
    main()
