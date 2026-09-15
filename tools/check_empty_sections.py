import sys
sys.stdout.reconfigure(encoding="utf-8")
lines = open(str(Path(__file__).resolve().parent.parent / "source" / "骨架.md"), encoding="utf-8").read().splitlines()
secs, cur = [], None
for l in lines:
    if l.startswith("#### "):
        if cur: secs.append(cur)
        cur = [l, 0]
    elif cur and (l.startswith("- ") or l.startswith("  - ")):
        cur[1] += 1
if cur: secs.append(cur)
empty = [s for s in secs if s[1] == 0]
print(f"节总数 {len(secs)}，仍为空壳的节: {len(empty)}")
for s in empty:
    print("   ", s[0])
