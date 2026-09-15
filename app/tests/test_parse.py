import sys, json
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))
import ai

log = "2026年9月14日，背诵外国教育史第四章，近代国家教育体制及其教育思想；第五章部分内容：夸美纽斯的教育思想、卢梭的教育思想、裴斯泰洛齐的教育思想。做题，完成了中国教育史第五章理学教育思想的形成和学校教育制度的发展章节真题和习题"

print("=== 输入记录 ===")
print(" ", log[:70], "…")
print()
data, usage = ai.parse_study_log(log)
print("=== AI 定位结果 ===")
for m in data.get("matches", []):
    print(f"  {m.get('confidence',0):.2f}  [{m['id']:>4}] {m['name']}   ← {m.get('reason','')[:40]}")
if data.get("unmatched"):
    print("  未定位:", data["unmatched"])
print(f"\n用量: 输入 {usage.get('prompt_tokens')} / 输出 {usage.get('completion_tokens')} tokens")
