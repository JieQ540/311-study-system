import sys, re
from collections import Counter
sys.stdout.reconfigure(encoding="utf-8")
from docx import Document

path = str(Path(__file__).resolve().parent.parent / "01 311考纲（扫描，仅作教学使用）_可搜索(1).docx")
doc = Document(path)

WATER = ["夸克扫描王", "极速扫描", "后续关注", "永久微信", "研大"]
def is_water(t):
    return any(w in t for w in WATER)

paras = []
for p in doc.paragraphs:
    t = p.text.strip()
    if not t:
        continue
    sizes = [r.font.size.pt for r in p.runs if r.font.size is not None]
    bold = any(r.bold for r in p.runs if r.bold)
    paras.append((max(sizes) if sizes else 0.0, bold, t))

body = [(sz, b, t) for sz, b, t in paras if sz > 8 and not is_water(t)]
print(f"原始非空段 {len(paras)}  ->  过滤页脚/水印后 {len(body)}")
print("字号分布:", Counter(round(sz) for sz, b, t in body).most_common(12))
print()
print("=== 板块标题定位 ===")
for kw in ["教育学原理", "中外教育史", "教育心理学", "教育研究方法", "考查内容", "题型示例", "附录"]:
    hits = [(i, round(sz), b, t) for i, (sz, b, t) in enumerate(body) if t.strip() == kw]
    print(f"  {kw:<8} 精确匹配 {len(hits)} 处: {[(i, sz, 'B' if b else '') for i, sz, b, t in hits]}")
print()
chs = [(i, round(sz), t) for i, (sz, b, t) in enumerate(body) if re.match(r"^[一二三四五六七八九十]+、", t)]
print(f"=== 章标题（N、）共 {len(chs)} 个，前 20 ===")
for i, sz, t in chs[:20]:
    print(f"  #{i:<5} {sz:>3}  {t[:40]}")
print()
secs = [(i, t) for i, (sz, b, t) in enumerate(body) if re.match(r"^（[一二三四五六七八九十]+）", t)]
print(f"=== 节标题（（N））共 {len(secs)} 个，前 12 ===")
for i, t in secs[:12]:
    print(f"  #{i:<5} {t[:40]}")
pts = [(i, t) for i, (sz, b, t) in enumerate(body) if re.match(r"^\d+\s*[.．]", t)]
print(f"\n=== 考点条目（N.）共 {len(pts)} 个，前 12 ===")
for i, t in pts[:12]:
    print(f"  #{i:<5} {t[:45]}")
