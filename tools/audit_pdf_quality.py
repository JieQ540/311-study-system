# -*- coding: utf-8 -*-
"""对真题/解析 PDF 做文字层质量体检。

指标：抽出文本中「中文字符占比」。
  - 占比 >= 0.50 ：质量好，可直接抽取
  - 0.20 ~ 0.50 ：内容可能在，但夹杂乱码或字间空格，需清洗
  - < 0.20      ：乱码/纯图像，需 OCR

为什么要看这个而不是字节数：字节数只说明"有文字层"，
2010 年真题每页 1858 字节看着正常，实际全是乱码。
"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import pymupdf

DIRS = [
    str(Path(__file__).resolve().parent.parent / "01-311教育学真题(07-26)"),
    str(Path(__file__).resolve().parent.parent / "02-311教育学解析(07-26）"),
]


def inspect(pdf_path):
    doc = pymupdf.open(pdf_path)
    text = []
    for page in doc:
        text.append(page.get_text())
    doc.close()
    s = "".join(text)
    total = len(s)
    cjk = len(re.findall(r"[\u4e00-\u9fa5]", s))
    spaces = s.count(" ")
    ratio = cjk / total if total else 0.0
    space_ratio = spaces / total if total else 0.0
    return total, cjk, ratio, space_ratio


def verdict(ratio, space_ratio):
    if ratio >= 0.50:
        return "好 · 可直接抽"
    if ratio >= 0.20:
        return "中 · 需清洗"
    return "差 · 需OCR"


def main():
    for d in DIRS:
        p = Path(d)
        if not p.exists():
            print(f"[跳过] 目录不存在: {d}")
            continue
        print(f"\n===== {p.name} =====")
        print(f"{'文件':<38}{'页':>4}{'字符':>8}{'中文':>8}{'中文占比':>9}{'空格率':>8}   判定")
        for f in sorted(p.glob("*.pdf")):
            try:
                total, cjk, ratio, sratio = inspect(f)
            except Exception as e:
                print(f"{f.name:<38}  读取失败: {e}")
                continue
            name = f.name if len(f.name) <= 36 else f.name[:34] + ".."
            print(f"{name:<38}{'':>4}{total:>8}{cjk:>8}{ratio:>9.2%}{sratio:>8.2%}   {verdict(ratio, sratio)}")


if __name__ == "__main__":
    main()
