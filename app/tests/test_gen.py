import sys, json
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))
import ai

points = [
    {"id": 307, "path": "中外教育史 › 五、外国近代教育 › （三）西欧近代教育思想与教育思潮 › 3. 裴斯泰洛齐的教育思想"},
    {"id": 306, "path": "中外教育史 › 五、外国近代教育 › （三）西欧近代教育思想与教育思潮 › 2. 卢梭的教育思想"},
]
print("=== 出题测试（3 单选 + 1 辨析）===")
data, usage = ai.generate_questions(points, n_single=3, n_analysis=1, n_short=0, n_essay=0)
print(f"用量: 输入 {usage.get('prompt_tokens')} / 输出 {usage.get('completion_tokens')} tokens")
print()
for i, q in enumerate(data.get("single", []), 1):
    print(f"[单选 {i}] 考点={q.get('outline_id')}  答案={q.get('answer')}")
    print(f"   {q.get('stem','')[:76]}")
    for k, v in (q.get("options") or {}).items():
        print(f"     {k}. {v[:56]}")
print()
for q in data.get("subjective", []):
    print(f"[{q.get('qtype')} {q.get('full_score')}分] 考点={q.get('outline_id')}")
    print(f"   {q.get('stem','')[:80]}")
    for p in q.get("points", []):
        print(f"     {p.get('seq')}. {p.get('claim','')[:50]}")
