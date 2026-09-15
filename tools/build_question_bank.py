# -*- coding: utf-8 -*-
"""把 20 年真题与解析批量转成文本。

文字层是否可用，用三条规则判定 —— 任一不满足就走 OCR：

  1. 乱码型   ：中文占比 < 0.5（嵌入字体无 ToUnicode）
  2. 残缺型   ：每页字符数 < 300（只有前几页有文字层）
  3. 水印型   ：最高频行占比 > 20%（有中文，但全是"时代云图"这类水印）

  ⚠ 教训：最初只判了第 1 条，于是「全水印」和「只有前 4 题」的文件
    都被当成"直接抽取成功"，实际上内容为零或残缺。

输出：source/真题库/真题/*.md、source/真题库/解析/*.md

用法：
    python build_question_bank.py               # 跳过已完成的
    python build_question_bank.py --force       # 忽略已有输出，全部重做
    python build_question_bank.py 2011          # 只处理文件名含 "2011" 的
    python build_question_bank.py --check       # 只体检不转换，列出需 OCR 的文件
"""
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import cv2
import numpy as np
import pymupdf
from rapidocr_onnxruntime import RapidOCR

ENGINE = None  # 延迟初始化：--check 模式不需要加载模型
ROW_TOL = 25

SRC = [
    ("真题", str(Path(__file__).resolve().parent.parent / "01-311教育学真题(07-26)")),
    ("解析", str(Path(__file__).resolve().parent.parent / "02-311教育学解析(07-26）")),
]
OUT_ROOT = Path(Path(__file__).resolve().parent.parent / "source" / "真题库")

MIN_CJK_RATIO = 0.50      # 中文占比下限
MIN_CHARS_PER_PAGE = 300  # 每页字符数下限
MAX_TOP_LINE_RATIO = 0.20  # 最高频行占比上限


def get_engine():
    global ENGINE
    if ENGINE is None:
        ENGINE = RapidOCR()
    return ENGINE


def cjk_ratio(text):
    if not text:
        return 0.0
    return len(re.findall(r"[\u4e00-\u9fa5]", text)) / len(text)


def layer_text(pdf):
    doc = pymupdf.open(pdf)
    t = "".join(p.get_text() for p in doc)
    doc.close()
    return t


def quality_check(text, page_count):
    """返回 (是否可用, 原因列表, 指标字典)"""
    total = len(text)
    ratio = cjk_ratio(text)
    per_page = total / page_count if page_count else 0
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    top_ratio = (Counter(lines).most_common(1)[0][1] / len(lines)) if lines else 0.0

    reasons = []
    if ratio < MIN_CJK_RATIO:
        reasons.append(f"中文占比仅 {ratio:.1%}")
    if per_page < MIN_CHARS_PER_PAGE:
        reasons.append(f"每页仅 {per_page:.0f} 字符")
    if top_ratio > MAX_TOP_LINE_RATIO:
        reasons.append(f"高频行占 {top_ratio:.0%}(疑水印)")
    metrics = {"ratio": ratio, "per_page": per_page, "top_line_ratio": top_ratio}
    return (not reasons), reasons, metrics


def rows_to_lines(items, row_tol=ROW_TOL):
    items = [i for i in items if i["text"]]
    if not items:
        return []
    items.sort(key=lambda i: i["cy"])
    rows, cur = [], [items[0]]
    for it in items[1:]:
        if it["cy"] - cur[-1]["cy"] <= row_tol:
            cur.append(it)
        else:
            rows.append(cur)
            cur = [it]
    rows.append(cur)
    out = []
    for r in rows:
        r.sort(key=lambda i: i["x0"])
        out.append(" ".join(i["text"] for i in r))
    return out


def ocr_page(page):
    pix = page.get_pixmap(dpi=300)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    else:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    result, _ = get_engine()(img)
    if not result:
        return []
    items = []
    for box, text, _score in result:
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        items.append({"cx": sum(xs) / 4, "cy": sum(ys) / 4, "x0": min(xs), "text": (text or "").strip()})
    h, w = img.shape[:2]
    if w > h * 1.15:  # 跨页拼版：先左半页，再右半页
        left = [i for i in items if i["cx"] < w / 2]
        right = [i for i in items if i["cx"] >= w / 2]
        return rows_to_lines(left) + rows_to_lines(right)
    return rows_to_lines(items)


def ocr_pdf(pdf, label):
    doc = pymupdf.open(pdf)
    blocks = []
    for i, page in enumerate(doc):
        blocks.append(f"===== 第 {i + 1} 页 =====")
        blocks.extend(ocr_page(page))
        print(f"    [{label}] 第 {i + 1}/{doc.page_count} 页", flush=True)
    doc.close()
    return "\n".join(blocks)


def iter_sources(filt):
    for label, d in SRC:
        p = Path(d)
        if not p.exists():
            print(f"[跳过] 目录不存在: {d}")
            continue
        for f in sorted(p.glob("*.pdf")):
            if filt and filt not in f.name:
                continue
            yield label, f


def main():
    check_only = "--check" in sys.argv
    force = "--force" in sys.argv
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    filt = positional[0] if positional else None

    if check_only:
        print(f"{'目录':<6}{'文件':<40}{'页':>4}{'每页字':>8}{'中文':>8}{'高频行':>8}   判定")
        need = []
        for label, f in iter_sources(filt):
            doc = pymupdf.open(f)
            pages = doc.page_count
            doc.close()
            raw = layer_text(f)
            ok, reasons, m = quality_check(raw, pages)
            name = f.name if len(f.name) <= 38 else f.name[:36] + ".."
            verdict = "直接抽取" if ok else "**需OCR** " + "; ".join(reasons)
            print(f"{label:<6}{name:<40}{pages:>4}{m['per_page']:>8.0f}{m['ratio']:>8.1%}{m['top_line_ratio']:>8.1%}   {verdict}")
            if not ok:
                need.append((label, f, reasons))
        print(f"\n共 {len(need)} 个文件需要 OCR")
        return

    for label, f in iter_sources(filt):
        outdir = OUT_ROOT / label
        outdir.mkdir(parents=True, exist_ok=True)
        target = outdir / (f.stem.strip() + ".md")
        if not force and target.exists() and target.stat().st_size > 100:
            print(f"[跳过·已完成] {f.name}", flush=True)
            continue
        t0 = time.time()
        doc = pymupdf.open(f)
        pages = doc.page_count
        doc.close()
        raw = layer_text(f)
        ok, reasons, m = quality_check(raw, pages)
        if ok:
            content = raw
            mode = f"直接抽取(中文 {m['ratio']:.0%}, 每页 {m['per_page']:.0f} 字)"
        else:
            content = ocr_pdf(f, f.stem[:14])
            mode = f"OCR({' ; '.join(reasons)})"
        target.write_text(content, encoding="utf-8")
        print(f"[完成] {f.name} -> {mode}  用时 {time.time() - t0:.0f}s  输出 {target.name}", flush=True)
    print("\n全部完成", flush=True)


if __name__ == "__main__":
    main()
