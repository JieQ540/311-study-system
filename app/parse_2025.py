# -*- coding: utf-8 -*-
"""解析 2025 真题单选（源：source/真题部分.md，来自考纲 docx，格式较规整）。

格式特征：
  题号行   ^数字. 题干……
  选项行   同一行内含 A. … B. … C. … D. …
  偶有      题干与选项挤在同一行（如「……这三大维度是A. …B. …」）

策略：先按题号切块，再在块内用正则切分四个选项。
只收录「恰好切出 A/B/C/D 四项」的题，其余跳过并报告 —— 宁缺毋滥。
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kaoyan.db"
SRC = ROOT / "source" / "真题部分.md"

RE_Q = re.compile(r"^(\d{1,2})[.．]\s*")
RE_OPT = re.compile(r"([A-D])[.．、]\s*")
SKIP_PAT = ("全国硕士研究生", "考试大纲", "=====", "夸克扫描", "极速扫描", "教育学专业基础试题")


def blocks_from(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    # 关键：文件前半是「大纲自带的题型示例」，题号与真题撞号。
    # 必须从真题标题行之后开始解析，否则会把样题当成真题。
    start = 0
    for i, raw in enumerate(lines):
        s = raw.strip()
        if "2025" in s and "教育学专业基础试题" in s and "参考答案" not in s:
            start = i
            break
    print(f"真题起始行: #{start}  ->  {lines[start].strip()[:50] if lines else ''}")

    blocks, cur = [], None
    for raw in lines[start:]:
        line = raw.strip()
        if not line or any(p in line for p in SKIP_PAT):
            continue
        m = RE_Q.match(line)
        if m:
            num = int(m.group(1))
            # 允许小幅跳跃（OCR 可能吞掉某个题号），但不允许倒退
            if cur is None or (num > int(cur["no"]) and num <= int(cur["no"]) + 3):
                if cur:
                    blocks.append(cur)
                cur = {"no": m.group(1), "buf": [line[m.end():]]}
                continue
        if cur is not None:
            cur["buf"].append(line)
    if cur:
        blocks.append(cur)
    return blocks


def split_block(block):
    text = "".join(block["buf"])
    text = re.sub(r"\s+", "", text)
    parts = RE_OPT.split(text)
    stem = parts[0]
    opts = {}
    for i in range(1, len(parts) - 1, 2):
        key = parts[i]
        val = parts[i + 1]
        if key not in opts:
            opts[key] = val
    return stem, opts


def main():
    blocks = blocks_from(SRC)
    print(f"切出题块 {len(blocks)} 个")

    good, bad = [], []
    for b in blocks:
        if not b["no"].isdigit() or int(b["no"]) > 45:
            continue
        stem, opts = split_block(b)
        if set(opts) == {"A", "B", "C", "D"} and len(stem) > 5:
            good.append((b["no"], stem, opts))
        else:
            bad.append((b["no"], sorted(opts), len(stem)))

    print(f"可用（A/B/C/D 齐全）: {len(good)} 道")
    if bad:
        print(f"跳过 {len(bad)} 道，明细（题号, 切出的选项, 题干长度）:")
        for x in bad[:15]:
            print("   ", x)

    con = sqlite3.connect(DB)
    con.execute("DELETE FROM questions WHERE source='2025真题·单选'")
    for no, stem, opts in good:
        con.execute(
            "INSERT INTO questions (year,source,qtype,number,stem,options) VALUES (?,?,?,?,?,?)",
            (2025, "2025真题·单选", "single", no, stem, json.dumps(opts, ensure_ascii=False)),
        )
    con.commit()
    print(f"\n已入库单选 {len(good)} 道")

    for probe in ("1", "10", "45"):
        row = con.execute(
            "SELECT number,stem,options FROM questions WHERE source='2025真题·单选' AND number=?", (probe,)
        ).fetchone()
        if row:
            print(f"\n[{row[0]} 题] {row[1][:64]}")
            print("   ", " | ".join(f"{k}.{v[:18]}" for k, v in json.loads(row[2]).items()))
    con.close()


if __name__ == "__main__":
    main()
