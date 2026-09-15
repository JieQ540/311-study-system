import sys, re
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path

ocr  = Path(Path(__file__).resolve().parent.parent / "source" / "03-OCR-full.txt").read_text(encoding="utf-8")
docx = Path(Path(__file__).resolve().parent.parent / "source" / "大纲-正文.md").read_text(encoding="utf-8")
norm = lambda s: re.sub(r"\s+", "", s)
o, d = norm(ocr), norm(docx)
print(f"OCR 文本 {len(o)} 字 | docx 文本 {len(d)} 字")

terms = ["裴斯泰洛齐","赫尔巴特","杜威","陶行知","赞科夫","布鲁纳","布卢姆","苏格拉底",
         "夸美纽斯","卢梭","康德","福禄贝尔","蒙台梭利","维果茨基","皮亚杰","科尔伯格",
         "晏阳初","梁漱溟","黄炎培","蔡元培","第斯多惠","乌申斯基","马卡连柯","斯宾塞",
         "教育研究方法","教育心理学","中外教育史","教育学原理","立德树人","元教育学"]
print(f"\n{'术语':<14}{'docx':>6}{'OCR':>6}   判定")
bad = []
for t in terms:
    a, b = d.count(t), o.count(t)
    if (a > 0) == (b > 0):
        verdict = "一致"
    else:
        verdict = "*** 不一致 ***"
        bad.append((t, a, b))
    print(f"{t:<14}{a:>6}{b:>6}   {verdict}")

print(f"\n不一致术语数: {len(bad)}")
for t, a, b in bad:
    print(f"  {t}: docx={a} OCR={b}")

out = Path(Path(__file__).resolve().parent.parent / "docs" / "交叉校验-快速.md")
lines = ["# 快速交叉校验（docx vs RapidOCR）", "",
         f"- docx 文本 {len(d)} 字，OCR 文本 {len(o)} 字", "",
         "| 术语 | docx | OCR | 判定 |", "| --- | --- | --- | --- |"]
for t in terms:
    a, b = d.count(t), o.count(t)
    ok = "一致" if (a > 0) == (b > 0) else "**不一致**"
    lines.append(f"| {t} | {a} | {b} | {ok} |")
lines += ["", f"不一致术语数：**{len(bad)}**", "",
          "> 说明：本校验只比对术语是否在两边出现，用于定位某一方识别失败的字。",
          "> 完整逐段比对尚未做——需要你在场判断。"]
out.write_text("\n".join(lines), encoding="utf-8")
print(f"\n已写出: {out}")
