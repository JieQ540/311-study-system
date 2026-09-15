import sys, re
from collections import Counter
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

root = Path(Path(__file__).resolve().parent.parent / "source" / "真题库")
print(f"{'文件':<40}{'行数':>6}{'最高频行':>14}{'占比':>8}  判定")
for sub in ("真题", "解析"):
    print(f"\n----- {sub} -----")
    for f in sorted((root / sub).glob("*.md")):
        lines = [l.strip() for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            print(f"{f.name:<40}  空文件")
            continue
        c = Counter(lines)
        top, cnt = c.most_common(1)[0]
        ratio = cnt / len(lines)
        verdict = "⚠ 水印型" if ratio > 0.20 else "ok"
        print(f"{f.name:<40}{len(lines):>6}{cnt:>14}{ratio:>8.1%}  {verdict}   高频行={top[:18]}")
