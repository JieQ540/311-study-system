import re, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

root = Path(Path(__file__).resolve().parent.parent / "source" / "真题库")
# 汉字之间被插入空格（低质 OCR 的典型特征）
RE_GAP = re.compile(r"(?<=[\u4e00-\u9fa5]) +(?=[\u4e00-\u9fa5])")
# 非常规字符：既不是汉字/ASCII，也不是常见中英标点
COMMON = set("，。；：？！、（）《》【】“”‘’—…·％℃　°+-=*/\\<>[]{}()\"'.,:;!?@#$%^&_|~`0123456789"
             "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ \n\r\t")
rows = []
for sub in ("真题", "解析"):
    for f in sorted((root / sub).glob("*.md")):
        t = f.read_text(encoding="utf-8")
        han = len(re.findall(r"[\u4e00-\u9fa5]", t))
        gaps = len(RE_GAP.findall(t))
        weird = sum(1 for ch in t if ch not in COMMON and not ("\u4e00" <= ch <= "\u9fa5"))
        rows.append((gaps / han if han else 0, weird / len(t) if t else 0, sub, f.name, han, gaps, weird))
print(f"{'目录':<5}{'文件':<40}{'汉字':>7}{'字间空格':>9}{'空格率':>8}{'异常字':>7}{'异常率':>8}  判定")
for gapr, weirr, sub, name, han, gaps, weird in sorted(rows, reverse=True):
    if gapr > 0.05 or weirr > 0.01:
        v = "⚠ 脏"
    elif gapr > 0.01 or weirr > 0.003:
        v = "· 略脏"
    else:
        v = "ok"
    nm = name if len(name) <= 38 else name[:36] + ".."
    print(f"{sub:<5}{nm:<40}{han:>7}{gaps:>9}{gapr:>8.1%}{weird:>7}{weirr:>8.2%}  {v}")
