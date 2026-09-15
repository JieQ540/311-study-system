# -*- coding: utf-8 -*-
"""
对扫描版 PDF 渲染出的页面 PNG 做 OCR，并按版面还原正确阅读顺序。

背景：源 PDF 第 3-43 页是跨页拼版扫描（摊开的书一次扫两页），
直接抽取会把左右两页的文字交错在一起，因此这里按文本框中心 x 坐标
切分左右页，再各自按 y 排序，最后拼接为「左页 -> 右页」。

用法:
    python ocr_pages.py <pages_dir> <out_txt> [first_page] [last_page]
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import cv2
from rapidocr_onnxruntime import RapidOCR

ENGINE = RapidOCR()

# 同一行的容差（像素）。300DPI 下正文行高约 40-60px，取 25 可稳妥合并同一行。
ROW_TOL = 25


def read_page(img_path):
    """返回 (宽, 高, [{'cx','cy','x0','text'}...])"""
    img = cv2.imread(str(img_path))
    if img is None:
        raise RuntimeError(f"无法读取图片: {img_path}")
    h, w = img.shape[:2]
    result, _ = ENGINE(str(img_path))
    items = []
    if result:
        for box, text, _score in result:
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            t = (text or "").strip()
            if not t:
                continue
            items.append(
                {
                    "cx": sum(xs) / 4.0,
                    "cy": sum(ys) / 4.0,
                    "x0": min(xs),
                    "text": t,
                }
            )
    return w, h, items


def sort_rows(items):
    """先按 y 分行，行内按 x 排。"""
    if not items:
        return []
    items = sorted(items, key=lambda i: i["cy"])
    rows, cur = [], [items[0]]
    for it in items[1:]:
        if it["cy"] - cur[-1]["cy"] <= ROW_TOL:
            cur.append(it)
        else:
            rows.append(cur)
            cur = [it]
    rows.append(cur)
    out = []
    for row in rows:
        row.sort(key=lambda i: i["x0"])
        out.append(" ".join(i["text"] for i in row))
    return out


def read_order(w, h, items, is_spread):
    if is_spread:
        mid = w / 2.0
        left = [i for i in items if i["cx"] < mid]
        right = [i for i in items if i["cx"] >= mid]
        return sort_rows(left) + sort_rows(right)
    return sort_rows(items)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    pages_dir = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    first = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    last = int(sys.argv[4]) if len(sys.argv) > 4 else 99999

    files = []
    for f in sorted(pages_dir.glob("*.png")):
        try:
            num = int(f.stem.split("-")[-1])
        except ValueError:
            continue
        if first <= num <= last:
            files.append((num, f))

    blocks = []
    for num, f in files:
        w, h, items = read_page(f)
        is_spread = w > h * 1.15
        lines = read_order(w, h, items, is_spread)
        blocks.append(f"===== PAGE {num} ({'spread' if is_spread else 'single'}) =====")
        blocks.extend(lines)
        print(f"page {num:>3} ok  布局={'跨页' if is_spread else '单页'}  文本块={len(lines)}", flush=True)

    out_path.write_text("\n".join(blocks), encoding="utf-8")
    print(f"\n已写出: {out_path}  共 {len(files)} 页")


if __name__ == "__main__":
    main()
