import sys, re
sys.stdout.reconfigure(encoding="utf-8")
p = str(Path(__file__).resolve().parent.parent / "source" / "骨架.md")
lines = open(p, encoding="utf-8").read().splitlines()
for i, l in enumerate(lines):
    if "教育目的与培养目标" in l:
        for j in range(i, min(i+12, len(lines))):
            print(f"  {lines[j]}")
        break
print()
print("=== 验证修复：考查目标条目是否已归类 ===")
n_goal = sum(1 for l in lines if l.startswith("> "))
n_badpoint = sum(1 for l in lines if l.startswith("- ") and "。" in l and len(l) < 40)
print(f"  考查目标条目(> 前缀): {n_goal}  (应为 12 = 4 板块 x 3 条)")
print(f"  仍像目标却算考点的行 : {n_badpoint}")
print()
print("=== 残留格式问题检查 ===")
print("  含「（ 一 」等空格编号:", sum(1 for l in lines if re.search(r"[（(]\s+[一二三四五六七八九十]", l)))
print("  含「N 、」空格:", sum(1 for l in lines if re.search(r"^#+ [一二三四五六七八九十]+\s+、", l)))
