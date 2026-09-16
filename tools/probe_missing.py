import sys, re
sys.stdout.reconfigure(encoding="utf-8")
from docx import Document
from paths import OUTLINE_DOCX
path = OUTLINE_DOCX
doc = Document(path)
paras = []
for p in doc.paragraphs:
    t = " ".join(p.text.split())
    if not t: continue
    sizes = [r.font.size.pt for r in p.runs if r.font.size is not None]
    bold = any(r.bold for r in p.runs if r.bold)
    paras.append((max(sizes) if sizes else 0.0, bold, t))
body = [(sz,b,t) for sz,b,t in paras if sz > 8.5]

print("=== 所有含「教育目的」或「培养目标」的节标题（（N）开头）===")
for i,(sz,b,t) in enumerate(body):
    if re.match(r"^[（(][一二三四五六七八九十]+[）)]", t) and ("教育目的" in t or "培养目标" in t):
        print(f"  #{i:<5} sz={round(sz):<3} {'B' if b else ' '} {t}")
print()
print("=== 「教育目的与培养目标」章附近的原始段落 ===")
anchor = None
for i,(sz,b,t) in enumerate(body):
    if "教育目的与培养目标" in t:
        anchor = i; break
if anchor is not None:
    for i in range(max(0,anchor-2), min(len(body), anchor+20)):
        sz,b,t = body[i]
        print(f"  #{i:<5} sz={round(sz):<3} {'B' if b else ' '} {t[:60]}")
print()
print("=== 「考查目标」标记位置 ===")
for i,(sz,b,t) in enumerate(body):
    if "考查目标" in t:
        print(f"  #{i:<5} sz={round(sz):<3} {t[:50]}")
