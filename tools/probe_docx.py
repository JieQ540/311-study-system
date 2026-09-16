from paths import OUTLINE_DOCX
import sys, zipfile, re
sys.stdout.reconfigure(encoding="utf-8")
path = OUTLINE_DOCX
with zipfile.ZipFile(path) as z:
    names = z.namelist()
    print("zip 内条目数:", len(names))
    xml = z.read("word/document.xml").decode("utf-8", "ignore")
paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
out = []
for p in paras:
    t = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S))
    out.append(t)
text = "\n".join(out)
print("段落数:", len(paras), " 字符数:", len(text))
print()
print("### 关键词计数 ###")
for kw in ["全国硕士研究生", "午玉", "教育学", "教有学", "夸克扫描王", "研大"]:
    print(f"  {kw!r}: {text.count(kw)}")
print()
print("### 前 45 段 ###")
for line in out[:45]:
    print(line)
