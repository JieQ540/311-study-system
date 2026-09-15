import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))
import ai

points = [{"id": 304, "path": "中外教育史 › 五、外国近代教育 › （三）西欧近代教育思想与教育思潮"}]
print("=== 改进后的出题（难度要求已写入 prompt）===")
data, usage = ai.generate_questions(points, n_single=5, n_analysis=0, n_short=1, n_essay=0)
print(f"用量: {usage.get('prompt_tokens')}/{usage.get('completion_tokens')} tokens\n")
for i, q in enumerate(data.get("single", []), 1):
    print(f"[{i}] 答案={q.get('answer')}")
    print(f"  题干：{q.get('stem','')[:120]}")
    for k, v in (q.get("options") or {}).items():
        print(f"     {k}. {v[:70]}")
    print()
for q in data.get("subjective", []):
    print(f"[{q.get('qtype')}] {q.get('stem','')[:100]}")
