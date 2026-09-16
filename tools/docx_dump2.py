import sys, re
sys.stdout.reconfigure(encoding="utf-8")
from docx import Document
from paths import OUTLINE_DOCX, OUTLINE_PDF  # noqa: E402
path = OUTLINE_DOCX
doc = Document(path)
WATER = ["夸克扫描王", "极速扫描", "后续关注", "永久微信"]
paras = []
for p in doc.paragraphs:
    t = p.text.strip()
    if not t: continue
    sizes = [r.font.size.pt for r in p.runs if r.font.size is not None]
    bold = any(r.bold for r in p.runs if r.bold)
    paras.append((max(sizes) if sizes else 0.0, bold, t))
body = [(sz, b, t) for sz, b, t in paras if sz > 8 and not any(w in t for w in WATER)]

def dump(a, b, label):
    print(f"===== {label}  #{a}-#{b} =====")
    for i in range(a, min(b, len(body))):
        sz, bd, t = body[i]
        print(f"#{i:<5}{round(sz):>3}{'B' if bd else ' '} {t[:62]}")
    print()

dump(745, 800, "疑似重复区")
dump(900, 960, "大纲/真题分界区")
