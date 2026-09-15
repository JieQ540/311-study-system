import sys, sqlite3, json, time
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))
import ai

con = sqlite3.connect(str(Path(__file__).resolve().parent.parent.parent / "data" / "kaoyan.db"))
con.row_factory = sqlite3.Row
q = con.execute("SELECT id,stem,extra FROM questions WHERE source='2025真题·主观' AND number='49'").fetchone()
pts = [dict(r) for r in con.execute("SELECT seq,claim,evidence FROM points WHERE question_id=? ORDER BY seq", (q["id"],))]
con.close()

student = """一、提高认识。学校应当认识到选修课与必修课同等重要，从培养学生个性特长的高度来重视选修课。
二、加强督导。把选修课开设情况纳入教育督导评估，防止选修课变成变相的必修课。
三、健全机制。学校要建立选修课的申报、审核、评估机制，规范教师开课。"""

print(f"题目长度 {len(q['stem'])} 字，采分点 {len(pts)} 个")
print("学生答案：答了 3 点（应 5 点）")
print("\n批改中（思考模式）…")
t0 = time.time()
data, usage = ai.grade_answer(q["stem"], pts, student, full_score=15, use_thinking=True)
dt = time.time() - t0

print(f"耗时 {dt:.0f}s   用量: 输入 {usage.get('prompt_tokens')} / 输出 {usage.get('completion_tokens')}")
print(f"  其中思考 tokens: {usage.get('completion_tokens_details',{}).get('reasoning_tokens')}")
print()
print("=== 批改结果 ===")
print(f"  得分: {data.get('score')} / {data.get('full_score')}")
print(f"  命中采分点: {data.get('hit_seqs')}")
print(f"  漏掉采分点: {data.get('miss_seqs')}")
print(f"  错因: {data.get('cause')}")
print(f"  一句话总评: {data.get('one_line')}")
print(f"  言之有理加分项: {json.dumps(data.get('extra_credit'), ensure_ascii=False)}")
pp = data.get("per_paragraph") or []
print(f"  段落批注 {len(pp)} 条:")
for x in pp[:3]:
    print(f"     {x.get('mark')} {x.get('comment','')[:50]}")
