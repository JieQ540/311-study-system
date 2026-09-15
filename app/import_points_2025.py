# -*- coding: utf-8 -*-
"""导入 2025 主观题与采分点。

源：
  source/拆点-2025主观题全集.md   （46-48 辨析、50-53 简答、54-56 分析论述）
  source/拆点模板-2025简答49.md    （第 49 题）

格式（我自己写的，稳定）：
  ## 题号. 标题
  | # | 采分点 | 判分依据 | 挂载考点 |
  | 1 | **核心论断** | 判分依据 | 中外教育史 › 中国近代教育 |

挂载考点用「模糊匹配」落到 outline_nodes.path 上：取路径的首段与末段做双向包含判断，
匹配不到就留空并报告（不猜）。
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kaoyan.db"
SRC_DIR = ROOT / "source"

RE_HEAD = re.compile(r"^#{2,3}\s*(\d{1,2})[.．]\s*(.+)$")
RE_ROW = re.compile(r"^\|\s*(\d{1,2})\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")
RE_STOP = re.compile(r"^#+\s*[四五六]、\s*(汇总|汇总表|附录|小结)")
# 第 49 题单独文件用的格式：### 采分点 N · 标题 / - **核心论断**：… / - **判分依据（…）**：… / - **挂载考点**：…
RE_PT_HEAD = re.compile(r"^#{3,4}\s*采分点\s*(\d+)\s*[·・.、]\s*(.+)$")
RE_PT_CLAIM = re.compile(r"^-\s*\*\*核心论断\*\*[：:]\s*(.+)$")
RE_PT_EVID = re.compile(r"^-\s*\*\*判分依据[^：:]*\*\*[：:]\s*(.+)$")
RE_PT_OUT = re.compile(r"^-\s*\*\*挂载考点\*\*[：:]\s*(.+)$")

# 题号 -> (题型, 满分)
QTYPE = {}
for n in (46, 47, 48):
    QTYPE[n] = ("analysis", 15)      # 辨析题
for n in range(49, 54):
    QTYPE[n] = ("short", 15)         # 简答题
for n in (54, 55, 56):
    QTYPE[n] = ("essay", 30)         # 分析论述题

BOLD = re.compile(r"\*\*(.+?)\*\*")


def clean(s):
    s = BOLD.sub(r"\1", s)
    s = s.replace("<br>", " ").replace("｜", "|")
    return re.sub(r"\s+", " ", s).strip()


def find_outline(con, path_text):
    """把 '中外教育史 › 中国近代教育' 这类文本落到 outline_nodes 上。"""
    if not path_text or path_text in ("—", "-", "–"):
        return None
    parts = [p.strip() for p in re.split(r"[›>]", path_text) if p.strip()]
    if not parts:
        return None
    # 用最后一段做最精确匹配，逐级回退
    for frag in reversed(parts):
        frag = re.sub(r"^[（(]?[一二三四五六七八九十\d]+[）)．.]?\s*", "", frag).strip()
        if len(frag) < 2:
            continue
        row = con.execute(
            "SELECT id,path FROM outline_nodes WHERE name LIKE ? ORDER BY seq LIMIT 1", (f"%{frag}%",)
        ).fetchone()
        if row:
            return row[0]
    return None


def parse_49(path):
    """第 49 题文件用的是「### 采分点 N · 标题」+ 属性行 的格式。"""
    items, cur = [], None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        m = RE_PT_HEAD.match(line)
        if m:
            cur = (int(m.group(1)), clean(m.group(2)), "", "")
            items.append(cur)
            continue
        if cur is None:
            continue
        for rx, idx in ((RE_PT_CLAIM, 1), (RE_PT_EVID, 2), (RE_PT_OUT, 3)):
            mm = rx.match(line)
            if mm:
                cur = (cur[0], cur[1] if idx != 1 else clean(mm.group(1)), clean(mm.group(1)) if idx == 2 else cur[2], clean(mm.group(1)) if idx == 3 else cur[3])
                items[-1] = cur
                break
    return [(49, '为普通高中选修课开发与实施中存在的问题提出改进对策',
             [(a, b, c, d) for a, b, c, d in items])]


def parse_md(path):
    """返回 [(题号, 标题, [(序号, 论断, 依据, 考点文本)])]"""
    out, cur = [], None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if RE_STOP.match(line):
            break  # 汇总表不是采分点，必须在这里截断
        m = RE_HEAD.match(line)
        if m:
            num = int(m.group(1))
            if num in QTYPE:
                cur = (num, clean(m.group(2)), [])
                out.append(cur)
            else:
                cur = None
            continue
        if cur is None:
            continue
        mr = RE_ROW.match(line.strip())
        if mr:
            cur[2].append((int(mr.group(1)), clean(mr.group(2)), clean(mr.group(3)), clean(mr.group(4))))
    return [x for x in out if x[2]]


def main():
    con = sqlite3.connect(DB)
    files = [SRC_DIR / "拆点-2025主观题全集.md", SRC_DIR / "拆点模板-2025简答49.md"]
    parsed = []
    for f in files:
        if not f.exists():
            print(f"[缺失] {f}")
            continue
        got = parse_49(f) if "49" in f.name else parse_md(f)
        print(f"{f.name}: 解析出 {len(got)} 道主观题 -> {[g[0] for g in got]}")
        parsed.extend(got)

    con.execute("DELETE FROM point_hits")
    con.execute("DELETE FROM attempts")
    con.execute("DELETE FROM points WHERE question_id IN (SELECT id FROM questions WHERE source='2025真题·主观')")
    con.execute("DELETE FROM questions WHERE source='2025真题·主观'")

    n_q = n_p = n_linked = 0
    unmatched = []
    for num, title, rows in sorted(parsed):
        qtype, full = QTYPE[num]
        cur = con.execute(
            "INSERT INTO questions (year,source,qtype,number,stem,extra) VALUES (?,?,?,?,?,?)",
            (2025, "2025真题·主观", qtype, str(num), title, json.dumps({"full_score": full}, ensure_ascii=False)),
        )
        qid = cur.lastrowid
        n_q += 1
        for seq, claim, evidence, outline_txt in rows:
            oid = find_outline(con, outline_txt)
            if oid:
                n_linked += 1
            elif outline_txt not in ("—", "-", ""):
                unmatched.append((num, seq, outline_txt))
            con.execute(
                "INSERT INTO points (question_id,seq,claim,evidence,outline_id) VALUES (?,?,?,?,?)",
                (qid, seq, claim, evidence, oid),
            )
            n_p += 1
    con.commit()

    print(f"\n入库主观题 {n_q} 道，采分点 {n_p} 个，其中挂上考点的 {n_linked} 个")
    if unmatched:
        print(f"\n未匹配到考点的采分点 {len(unmatched)} 个（需人工确认）:")
        for x in unmatched[:12]:
            print("   ", x)

    print("\n--- 验证：按考点查采分点 ---")
    for row in con.execute("""
        SELECT o.path, COUNT(*) FROM points p
        JOIN outline_nodes o ON o.id = p.outline_id
        GROUP BY o.id ORDER BY COUNT(*) DESC LIMIT 5
    """):
        print(f"  {row[1]:>2} 个采分点 <- {row[0][:70]}")
    con.close()


if __name__ == "__main__":
    main()
