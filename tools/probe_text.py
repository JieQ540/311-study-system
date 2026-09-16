import sys
sys.stdout.reconfigure(encoding="utf-8")
import fitz
from paths import OUTLINE_PDF

path = OUTLINE_PDF
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
