import re, sys
from collections import Counter
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

root = Path(Path(__file__).resolve().parent.parent / "source" / "真题库")
COMMON = set("，。；：？！、（）《》【】“”‘’—…·％℃　°+-=*/\\<>[]{}()\"'.,:;!?@#$%^&_|~`0123456789"
             "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ \n\r\t")
allbad = Counter()
for sub in ("真题", "解析"):
    for f in sorted((root / sub).glob("*.md")):
        t = f.read_text(encoding="utf-8")
        for ch in t:
            if ch not in COMMON and not ("\u4e00" <= ch <= "\u9fa5"):
                allbad[ch] += 1
print("全库异常字符 Top 40（字符 | Unicode | 次数 | 我的判断）")
for ch, c in allbad.most_common(40):
    code = f"U+{ord(ch):04X}"
    # 粗判：CJK 扩展区/兼容区的字多半是 OCR 误识别的生僻字
    if 0x3400 <= ord(ch) <= 0x4DBF or 0xF900 <= ord(ch) <= 0xFAFF:
        verdict = "生僻字(疑OCR误识)"
    elif 0x3000 <= ord(ch) <= 0x303F or 0xFF00 <= ord(ch) <= 0xFFEF:
        verdict = "中日韩标点/全角符号"
    elif ord(ch) < 0x80:
        verdict = "ASCII"
    else:
        verdict = "其它符号(可能OCR噪声)"
    print(f"  {ch!r:<6} {code:<9} {c:>5}   {verdict}")
