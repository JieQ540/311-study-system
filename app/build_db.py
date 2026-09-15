# -*- coding: utf-8 -*-
"""建库 + 导入大纲骨架。

数据库：data/kaoyan.db（SQLite 单文件，零部署）

设计要点：
  - outline_nodes 用 parent_id + path 双轨：parent_id 用于递归，path 用于直接查询
  - level 区分 board/chapter/section/point/detail/goal，与骨架.md 的层级一一对应
  - 采分点(points)挂在 question 上，并可再挂 outline_id，形成
        point_hits → points → questions → outline_nodes → 薄弱点排名
"""
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kaoyan.db"
SKELETON = ROOT / "source" / "骨架.md"

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS outline_nodes (
    id        INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES outline_nodes(id),
    level     TEXT NOT NULL,          -- board/chapter/section/point/detail/goal
    name      TEXT NOT NULL,
    seq       INTEGER NOT NULL,       -- 同级顺序
    path      TEXT NOT NULL,          -- 完整路径，查询用
    code      TEXT                    -- 编号（一、/（一）/1.），便于显示
);

CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY,
    year        INTEGER,
    source      TEXT,                 -- 真题年份来源 / 模拟
    qtype       TEXT NOT NULL,        -- single/analysis/short/essay
    number      TEXT,                 -- 卷面题号
    stem        TEXT NOT NULL,
    options     TEXT,                 -- JSON 字符串 {"A":..,"B":..}
    answer      TEXT,                 -- 客观题答案
    outline_id  INTEGER REFERENCES outline_nodes(id),
    extra       TEXT                  -- 备用：材料、必选题标识等
);

CREATE TABLE IF NOT EXISTS points (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    seq         INTEGER NOT NULL,
    claim       TEXT NOT NULL,        -- 核心论断（勾选用）
    evidence    TEXT,                 -- 判分依据原文
    outline_id  INTEGER REFERENCES outline_nodes(id),
    score       REAL                  -- 该点分值（可空）
);

CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    date        TEXT NOT NULL,
    mode        TEXT,                 -- 识记/评析/对策...
    hits        INTEGER,
    total       INTEGER,
    cause       TEXT,                 -- 不会/记混/看漏条件/时间不够
    note        TEXT
);

CREATE TABLE IF NOT EXISTS point_hits (
    attempt_id INTEGER NOT NULL REFERENCES attempts(id),
    point_id   INTEGER NOT NULL REFERENCES points(id),
    hit        INTEGER NOT NULL,
    PRIMARY KEY (attempt_id, point_id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_parent  ON outline_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_path    ON outline_nodes(path);
CREATE INDEX IF NOT EXISTS idx_q_year        ON questions(year);
CREATE INDEX IF NOT EXISTS idx_q_type        ON questions(qtype);
CREATE INDEX IF NOT EXISTS idx_points_q      ON points(question_id);
"""

RE_CODE = re.compile(r"^([一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[）)]|\d+\.)\s*")


def parse_skeleton(md_path):
    """把骨架.md 解析成节点列表。"""
    nodes = []
    stack = {}          # level -> 最近的节点
    counters = {}
    order = {"board": 0, "chapter": 1, "section": 2, "point": 3, "detail": 4, "goal": 5}

    def push(level, name, parent_level):
        parent = stack.get(parent_level)
        parent_id = parent["id"] if parent else None
        seq = counters.get((parent_id, level), 0) + 1
        counters[(parent_id, level)] = seq
        if parent:
            path = parent["path"] + " › " + name
        else:
            path = name
        m = RE_CODE.match(name)
        node = {
            "id": len(nodes) + 1,
            "parent_id": parent_id,
            "level": level,
            "name": name,
            "seq": seq,
            "path": path,
            "code": m.group(1).strip() if m else None,
        }
        nodes.append(node)
        stack[level] = node
        # 清掉更深层级
        for deeper in list(stack):
            if order.get(deeper, -1) > order.get(level, -1):
                del stack[deeper]
        return node

    for raw in md_path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("## "):
            push("board", line[3:].strip(), "root")
        elif line.startswith("### "):
            push("chapter", line[4:].strip(), "board")
        elif line.startswith("#### "):
            push("section", line[5:].strip(), "chapter")
        elif line.startswith("- "):
            push("point", line[2:].strip(), "section")
        elif line.startswith("  - "):
            push("detail", line[4:].strip(), "point")
        elif line.startswith("> "):
            push("goal", line[2:].strip(), "board")
        # 其它行（标题、**考查目标**）忽略
    return nodes


def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)

    # 骨架重导（幂等）
    con.execute("DELETE FROM outline_nodes")
    nodes = parse_skeleton(SKELETON)
    con.executemany(
        "INSERT INTO outline_nodes (id,parent_id,level,name,seq,path,code) VALUES (:id,:parent_id,:level,:name,:seq,:path,:code)",
        nodes,
    )
    con.commit()

    from collections import Counter
    c = Counter(n["level"] for n in nodes)
    print("已导入大纲节点:", dict(c))
    print("\n各级抽样:")
    for lv in ("board", "chapter", "section", "point"):
        sample = [n for n in nodes if n["level"] == lv][:2]
        for s in sample:
            print(f"  [{lv:<7}] {s['path'][:78]}")

    # 验证查询能力
    print("\n按名称检索『裴斯泰洛齐』:")
    for r in con.execute("SELECT id,level,path FROM outline_nodes WHERE name LIKE '%裴斯泰洛齐%'"):
        print(f"  #{r[0]} [{r[1]}] {r[2]}")
    con.close()
    print(f"\n数据库: {DB}  ({DB.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
