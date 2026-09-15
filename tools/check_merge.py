import sys, re
sys.stdout.reconfigure(encoding="utf-8")
txt = open(str(Path(__file__).resolve().parent.parent / "source" / "大纲-正文.md"), encoding="utf-8").read().splitlines()
rows = [(len(l.strip()), l.strip()) for l in txt if l.strip()]
rows.sort(reverse=True)
print("=== 最长的 6 个段落（检查是否过度合并）===")
for n, l in rows[:6]:
    print(f"\n[{n} 字] {l[:160]}{'...' if n > 160 else ''}")
print()
over = [r for r in rows if r[0] > 300]
print(f"超过 300 字的段落数: {len(over)}")
print(f"平均段长: {sum(r[0] for r in rows)//len(rows)} 字")
