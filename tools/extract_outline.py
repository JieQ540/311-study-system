# -*- coding: utf-8 -*-
"""
从 WPS 转出的 docx 中提取 311 考纲的干净正文、层级骨架，并分离真题部分。

为什么要走 docx 而不是 PDF：
  - PDF 文字层是夸克扫描王 OCR 的坏结果（且 3-43 页是跨页拼版）
  - docx 是 WPS 自己的 OCR + 版面重建产物，段落已切分、页眉页脚独立存放
  - docx 保留了字号与加粗，可用于辅助判断层级

流程：
  1. 过滤页眉页脚（字号 <= 8.5）与推广水印
  2. 检测并剔除重复段落块（扫描时重复了页面）
  3. 状态机还原层级：板块 > 章 > 节 > 考点（并单独识别「考查目标」条目）
  4. 规范化标题里的杂散空格（源文档格式不统一，如「（ 一 ）教育目的」）
  5. 输出：大纲正文 / 骨架 / 真题
"""
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
from docx import Document
from paths import OUTLINE_DOCX, OUTLINE_PDF  # noqa: E402

DOCX = OUTLINE_DOCX
OUT_DIR = Path(__file__).resolve().parents[1] / "source"

WATERMARKS = ("夸克扫描王", "极速扫描", "后续关注", "永久微信", "扫码关注", "公众号")
BOARDS = ("教育学原理", "中外教育史", "教育心理学", "教育研究方法")

# 注意：源文档排版不统一，编号内部可能混入空格，因此一律容许 \s*
RE_CHAPTER = re.compile(r"^[一二三四五六七八九十]+\s*、\s*")
RE_SECTION = re.compile(r"^[（(]\s*[一二三四五六七八九十]+\s*[）)]\s*")
RE_POINT = re.compile(r"^\d+\s*[.．、]\s*")
RE_GOAL_MARK = re.compile(r"^\[\s*考查目标\s*\]$")


def load_paragraphs():
    doc = Document(DOCX)
    out = []
    for p in doc.paragraphs:
        text = " ".join(p.text.split())
        if not text:
            continue
        sizes = [r.font.size.pt for r in p.runs if r.font.size is not None]
        bold = any(r.bold for r in p.runs if r.bold)
        out.append({"sz": max(sizes) if sizes else 0.0, "bold": bold, "text": text})
    return out


def clean(paras):
    kept, dropped_footer, dropped_water = [], 0, 0
    for p in paras:
        if p["sz"] <= 8.5:
            dropped_footer += 1
            continue
        if any(w in p["text"] for w in WATERMARKS):
            dropped_water += 1
            continue
        kept.append(p)
    return kept, dropped_footer, dropped_water


def find_duplicate_blocks(paras, min_run=5):
    """找出重复出现的连续段落块，返回应从第二次出现起删除的下标集合。"""
    texts = [p["text"] for p in paras]
    n = len(texts)
    drop = set()
    for i in range(n):
        if i in drop:
            continue
        for j in range(i + 1, n):
            if texts[j] != texts[i]:
                continue
            run = 0
            while j + run < n and i + run < j and texts[i + run] == texts[j + run]:
                run += 1
            if run >= min_run:
                drop.update(range(j, j + run))
                break
    return drop


def normalize_title(text):
    """规范编号里的杂散空格：'（ 一 ）教育目的' -> '（一）教育目的'"""
    t = text
    t = re.sub(r"^([（(])\s*", r"（", t)
    t = re.sub(r"\s*([）)])", "）", t)
    t = re.sub(r"^([一二三四五六七八九十]+)\s*、\s*", r"\1、", t)
    t = re.sub(r"^(\d+)\s*[.．、]\s*", r"\1. ", t)
    # 合并被 OCR 插入的字间空格（如「八、教 学」->「八、教学」）
    t = re.sub(r"(?<=[\u4e00-\u9fa5])[ \t]+(?=[\u4e00-\u9fa5])", "", t)
    return t.strip()


def classify(paras):
    """状态机：板块/章会结束「考查目标」状态。"""
    out = []
    in_goal = False
    for p in paras:
        t = p["text"]
        if t in BOARDS:
            in_goal, kind = False, "board"
        elif RE_GOAL_MARK.match(t):
            in_goal, kind = True, "goalmark"
        elif RE_CHAPTER.match(t):
            in_goal, kind = False, "chapter"
        elif RE_SECTION.match(t):
            in_goal, kind = False, "section"
        elif RE_POINT.match(t):
            kind = "goal" if in_goal else "point"
        else:
            kind = "text"
        out.append({"kind": kind, "p": p, "title": normalize_title(t)})
    return out


def locate(node_list, needle, start=0):
    for i in range(start, len(node_list)):
        if needle in node_list[i]["p"]["text"]:
            return i
    return -1


END_PUNCT = "。！？…）】》”"
STRUCT_HEAD = re.compile(
    r"^(#{2,4} |[一二三四五六七八九十]+\s*、|[（(]\s*[一二三四五六七八九十]+\s*[）)]"
    r"|\d+\s*[.．、]|>|\*\*|\[\s*考查目标)"
)

# 这些整行本身就是结构行，任何情况下都不允许被合并进上一段。
# 漏掉板块名会导致「教育学原理」被粘进正文，进而 board 精确匹配失败 —— 已踩过这个坑。
PROTECTED = set(BOARDS) | {
    "考查内容",
    "题型示例",
    "考试性质",
    "考查目标",
    "考试形式和试卷结构",
}


def merge_broken_paragraphs(paras):
    """合并被 docx 转换从中间切断的段落。

    例：「……正面引导与纪」+「律约束相结合原则；……」本是一句话。
    仅在上段不以句末标点结尾、且上下两段都不是结构行（标题/编号）时合并，
    避免把考点标题与它的描述错误粘连。
    """
    out = []
    for p in paras:
        t = p["text"]
        if out:
            prev = out[-1]
            pt = prev["text"]
            if (
                pt
                and pt[-1] not in END_PUNCT
                and not STRUCT_HEAD.match(pt)
                and not STRUCT_HEAD.match(t)
                and pt not in PROTECTED
                and t not in PROTECTED
            ):
                prev["text"] = pt + t
                continue
        out.append(dict(p))
    return out


def mark_details(nodes):
    """找出「下面没有任何编号考点」的节，把其正文内容标记为考点细目。

    311 大纲的考点有两种呈现方式：编号式（1. xxx）与描述式（a；b；c。）。
    描述式的节在骨架里原本会变成空壳，这里补上。
    """
    detail_idx = set()
    n = len(nodes)
    boundary = ("section", "chapter", "board", "goalmark")
    for i in range(n):
        if nodes[i]["kind"] != "section":
            continue
        j = i + 1
        has_point = False
        while j < n and nodes[j]["kind"] not in boundary:
            if nodes[j]["kind"] == "point":
                has_point = True
                break
            j += 1
        if has_point:
            continue
        k = i + 1
        while k < n and nodes[k]["kind"] not in boundary:
            if nodes[k]["kind"] == "text":
                detail_idx.add(k)
            k += 1
    return detail_idx


def main():
    raw = load_paragraphs()
    print(f"docx 非空段落            : {len(raw)}")

    paras, n_footer, n_water = clean(raw)
    print(f"过滤页眉页脚(字号<=8.5)  : {n_footer}")
    print(f"过滤推广水印             : {n_water}")
    print(f"剩余段落                 : {len(paras)}")

    paras = merge_broken_paragraphs(paras)
    print(f"合并被切断的段落后       : {len(paras)}")

    drop = find_duplicate_blocks(paras)
    deduped = [p for idx, p in enumerate(paras) if idx not in drop]
    print(f"检出重复段落(块内)       : {len(drop)}")
    print(f"去重后段落               : {len(deduped)}")

    i_content = locate([{"p": p} for p in deduped], "考查内容")
    i_sample = locate([{"p": p} for p in deduped], "题型示例", i_content + 1)
    i_exam = locate([{"p": p} for p in deduped], "教育学专业基础试题", i_sample + 1)
    print(f"定位: 考查内容=#{i_content}  题型示例=#{i_sample}  真题=#{i_exam}")

    outline = deduped[i_content:i_sample] if i_sample > 0 else deduped[i_content:]
    sample = deduped[i_sample:i_exam] if i_exam > 0 else []
    exam = deduped[i_exam:] if i_exam > 0 else []

    nodes = classify(outline)
    counts = Counter(n["kind"] for n in nodes)
    print("层级统计:", dict(counts))

    details = mark_details(nodes)
    print(f"无编号考点的节，其正文补为细目: {len(details)} 段")

    skel, body = [], []
    for idx, n in enumerate(nodes):
        kind, t = n["kind"], n["title"]
        if idx in details:
            skel.append(f"  - {t}")
            body.append(f"    {t}")
            continue
        if kind == "board":
            skel.append(f"\n## {t}")
            body.append(f"\n## {t}")
        elif kind == "goalmark":
            skel.append("\n**考查目标**")
            body.append("\n**考查目标**")
        elif kind == "goal":
            skel.append(f"> {t}")
            body.append(f"> {t}")
        elif kind == "chapter":
            skel.append(f"### {t}")
            body.append(f"\n### {t}")
        elif kind == "section":
            skel.append(f"#### {t}")
            body.append(f"\n#### {t}")
        elif kind == "point":
            skel.append(f"- {t}")
            body.append(f"\n**{t}**")
        else:
            body.append(f"    {t}")

    if "--dry-run" in sys.argv:
        # 只验证「能读到 docx 且解析出结构」，不覆盖已提取好的 source/*.md
        print("\n[dry-run] 未写出文件。解析结果统计：")
        print(f"  骨架行数 {len(skel)}，正文行数 {len(body)}，"
              f"题型示例 {len(sample)} 段，真题 {len(exam)} 段")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "大纲-正文.md").write_text(
        "# 311 教育学专业基础考试大纲（正文）\n" + "\n".join(body), encoding="utf-8"
    )
    (OUT_DIR / "骨架.md").write_text(
        "# 311 考纲骨架（板块 › 章 › 节 › 考点）\n" + "\n".join(skel), encoding="utf-8"
    )
    (OUT_DIR / "真题部分.md").write_text(
        "# 真题 / 样题部分\n\n## 题型示例（大纲自带）\n"
        + "\n".join(p["text"] for p in sample)
        + "\n\n## 真题\n"
        + "\n".join(p["text"] for p in exam),
        encoding="utf-8",
    )
    print("\n已写出:")
    for f in ("大纲-正文.md", "骨架.md", "真题部分.md"):
        fp = OUT_DIR / f
        print(f"  {fp.name:<16} {fp.stat().st_size:>7} bytes")


if __name__ == "__main__":
    main()
