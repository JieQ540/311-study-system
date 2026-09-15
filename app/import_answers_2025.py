# -*- coding: utf-8 -*-
"""从 2025 解析文件中提取客观题答案，回填 questions.answer。

格式（2025 解析）：
    5.【解析】A          题号与答案同行
    【解析】A            题号在前文，需沿用最近出现的题号
"""
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kaoyan.db"
SRC = ROOT / "source" / "真题库" / "解析" / "2025教育学基础综合311统考真题与答案解析.md"

RE_WITH_NO = re.compile(r"^(\d{1,2})\s*[.．]?\s*【解析】\s*([A-D])")
RE_BARE = re.compile(r"^【解析】\s*([A-D])")
RE_ANY_NO = re.compile(r"^(\d{1,2})\s*[.．]")


def main():
    answers = {}
    last_seen = None
    for raw in SRC.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        m1 = RE_WITH_NO.match(line)
        if m1:
            answers[int(m1.group(1))] = m1.group(2)
            last_seen = int(m1.group(1))
            continue
        m2 = RE_BARE.match(line)
        if m2 and last_seen is not None:
            answers.setdefault(last_seen, m2.group(1))
            continue
        m3 = RE_ANY_NO.match(line)
        if m3:
            n = int(m3.group(1))
            if 1 <= n <= 45:
                last_seen = n

    print(f"提取到答案 {len(answers)} 个，题号范围 {min(answers)}–{max(answers)}")
    missing = [n for n in range(1, 46) if n not in answers]
    if missing:
        print(f"缺失题号（{len(missing)} 个）: {missing}")

    con = sqlite3.connect(DB)
    updated = 0
    still_empty = []
    for n, a in sorted(answers.items()):
        cur = con.execute(
            "UPDATE questions SET answer=? WHERE source='2025真题·单选' AND number=?",
            (a, str(n)),
        )
        updated += cur.rowcount
    con.commit()

    for row in con.execute("SELECT number FROM questions WHERE source='2025真题·单选' AND (answer IS NULL OR answer='') ORDER BY CAST(number AS INTEGER)"):
        still_empty.append(row[0])
    print(f"\n回填成功 {updated} 道")
    if still_empty:
        print(f"仍无答案的题号（{len(still_empty)} 个）: {still_empty}")

    print("\n--- 抽样核对 ---")
    for n in ("1", "5", "10", "43"):
        row = con.execute(
            "SELECT number,answer,substr(stem,1,34) FROM questions WHERE source='2025真题·单选' AND number=?", (n,)
        ).fetchone()
        if row:
            print(f"  {row[0]:>2}. 答案={row[1] or '(空)'}   {row[2]}")
    con.close()


if __name__ == "__main__":
    main()
