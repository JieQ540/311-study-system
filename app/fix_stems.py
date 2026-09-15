# -*- coding: utf-8 -*-
"""修复主观题题干：把「拆点用短标题」换成考场原文（含材料与小问）。

背景：拆点文件里每题只有一个概括性标题，解析时被直接当成题干入库，
      导致第 54 题这类材料题丢失了全部材料和三个小问。

数据源：source/真题部分.md（来自考纲 docx，质量优于 OCR）
"""
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kaoyan.db"
SRC = ROOT / "source" / "真题部分.md"

RE_Q = re.compile(r"^(\d{1,2})\s*[.．]\s*(.*)$")
SKIP = ("全国硕士研究生", "考试大纲", "夸克扫描", "极速扫描", "=====")


def extract_stems():
    """从真题区提取 46–56 的完整题干（含跨行内容）。"""
    lines = SRC.read_text(encoding="utf-8").splitlines()
    # 定位真题起点：标题行含「教育学专业基础试题」且不是参考答案
    start = 0
    for i, s in enumerate(lines):
        if "教育学专业基础试题" in s and "参考答案" not in s and "2025" in s:
            start = i
            break
    print(f"真题起始行 #{start}: {lines[start].strip()[:48]}")

    blocks, cur = {}, None
    for raw in lines[start:]:
        line = raw.strip()
        if not line or any(p in line for p in SKIP):
            continue
        if line.startswith("参考答案") or "参考答案" in line or "【答案要点】" in line:
            break  # 答案区开始，后面都不属于题干
        # 题型说明行（如「三、简答题：第49~53小题…」）不是题干，必须剔除
        if re.match(r"^[一二三四五六七八九十]+\s*、\s*(单项选择题|辨析题|简答题|分析论述题)", line):
            continue
        if re.match(r"^[一二三四五六七八九十]+\s*、\s*第\s*\d+", line):
            continue
        m = RE_Q.match(line)
        if m:
            num = int(m.group(1))
            if 46 <= num <= 56:
                # 允许小幅跳跃
                if cur is None or (num > cur and num <= cur + 3):
                    cur = num
                    blocks.setdefault(num, [])
                    rest = m.group(2).strip()
                    if rest:
                        blocks[num].append(rest)
                    continue
        if cur is not None and cur in blocks:
            blocks[cur].append(line)
    return blocks


def prettify(s, num):
    """把挤在一起的题干拆成可读段落（对照 PDF 原版式）。"""
    # 「请回答：」独立成行
    s = re.sub(r"[ \t]*请回答\s*[：:]\s*", "\n请回答：\n", s)
    # 每个小问（1）（2）… 独立成行。
    # 注意：必须要求前面是句末标点或行首，否则「三（2）班」这类
    # 材料中的编号会被误拆 —— 已踩过这个坑。
    s = re.sub(r"([。！？；])\s*(（\d+）)", r"\1\n\2", s)
    s = re.sub(r"^\s*(（\d+）)", r"\1", s, flags=re.M)
    # 合并多余空行
    s = re.sub(r"\n{3,}", "\n\n", s)
    # 第 56 题的 Ⅰ 标识在 OCR 中丢失，按原图补回
    if num == 56:
        s = s.replace("计分。\n阅读材料", "计分。\nⅠ.阅读材料", 1)
    return s.strip()


def main():
    blocks = extract_stems()
    print(f"提取到 {len(blocks)} 道主观题: {sorted(blocks)}\n")

    con = sqlite3.connect(DB)
    for num in sorted(blocks):
        parts = blocks[num]
        # 保留段落结构：只压掉行内空白，**保留换行**
        # （材料题的材料、选项、小问必须分段，否则 4000 字挤成一坨）
        stem = "\n".join(
            re.sub(r"[ \t\u3000]+", "", p).strip() for p in parts if p.strip()
        )
        stem = prettify(stem, num)
        row = con.execute(
            "SELECT id, length(stem) FROM questions WHERE source='2025真题·主观' AND number=?",
            (str(num),),
        ).fetchone()
        if not row:
            print(f"  [{num}] 库中无此题，跳过")
            continue
        old_len = row[1]
        con.execute("UPDATE questions SET stem=? WHERE id=?", (stem, row[0]))
        print(f"  [{num}] 题干 {old_len:>3} -> {len(stem):>4} 字   {stem[:52]}…")
    con.commit()

    # 验证 54 题（最典型的材料题）
    print("\n=== 第 54 题完整题干 ===")
    r = con.execute(
        "SELECT stem FROM questions WHERE source='2025真题·主观' AND number='54'"
    ).fetchone()
    if r:
        s = r[0]
        print(f"总长度 {len(s)} 字")
        print("  开头:", s[:120])
        print("  结尾:", s[-120:])
        # 检查三个小问是否都在
        for k in ("（1）", "（2）", "（3）"):
            print(f"  含 {k} : {'✅' if k in s else '❌'}")
    con.close()


if __name__ == "__main__":
    main()
