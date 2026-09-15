import re, sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
norm = lambda s: re.sub(r"\s+", "", s)
root = Path(Path(__file__).resolve().parent.parent / "source")
outline = norm((root/"大纲-正文.md").read_text(encoding="utf-8"))
exam    = norm((root/"真题部分.md").read_text(encoding="utf-8"))
ocr     = norm((root/"03-OCR-full.txt").read_text(encoding="utf-8"))
print("=== 字数复核（去空白）===")
print(f"  docx 大纲正文        : {len(outline):>6}")
print(f"  docx 样题+真题       : {len(exam):>6}")
print(f"  docx 合计            : {len(outline)+len(exam):>6}")
print(f"  OCR 全文(43页)       : {len(ocr):>6}")
print(f"  差额 = OCR - docx合计: {len(ocr)-(len(outline)+len(exam)):>6}")
