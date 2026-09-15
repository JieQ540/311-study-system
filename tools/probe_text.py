import sys
sys.stdout.reconfigure(encoding="utf-8")
import fitz

path = str(Path(__file__).resolve().parent.parent / "01 311考纲（扫描，仅作教学使用）_可搜索(1).pdf")
doc = fitz.open(path)
print("pages:", doc.page_count)
for i in (2, 3):
    page = doc[i]
    t = page.get_text()
    print(f"===== page {i+1}  {len(t)} chars =====")
    print(t[:500])
    print("--- 该页用到的字体 ---")
    for f in page.get_fonts():
        print("   ", f)
