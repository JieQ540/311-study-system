import sys, zipfile, re
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
path = str(Path(__file__).resolve().parent.parent / "01 311考纲（扫描，仅作教学使用）_可搜索(1).docx")
with zipfile.ZipFile(path) as z:
    xml = z.read("word/document.xml").decode("utf-8", "ignore")
    parts = [n for n in z.namelist() if "header" in n or "footer" in n]
print("页眉/页脚部件:", parts if parts else "无")
print()
print("段落样式 pStyle 分布:", Counter(re.findall(r'<w:pStyle w:val="([^"]+)"', xml)).most_common(15))
print()
print("字号分布 (sz, 半磅):", Counter(re.findall(r'<w:sz w:val="(\d+)"', xml)).most_common(12))
print()
print("加粗 run 数:", len(re.findall(r"<w:b/>", xml)))
print("表格数:", len(re.findall(r"<w:tbl>", xml)))
print("超链接数:", len(re.findall(r"HYPERLINK", xml)))
