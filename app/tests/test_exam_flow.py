import json, sqlite3, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8765"
DB = str(Path(__file__).resolve().parent.parent.parent / "data" / "kaoyan.db")
TEST_DATE = "1900-01-02"

def get(path, timeout=600):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))
def post(path, payload, timeout=600):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

paper = get("/api/paper")
con = sqlite3.connect(DB)
# 前 5 题按正确答案填（应全对）；再取 5 题故意填错
answers = {r[0]: r[1] for r in con.execute("SELECT id, answer FROM questions WHERE qtype='single'")}
right, wrong = [], []
for q in paper["singles"][:10]:
    a = answers.get(q["id"])
    if not a: continue
    if len(right) < 5:
        right.append({"id": q["id"], "answer": a})
    else:
        bad = next(c for c in "ABCD" if c != a)
        wrong.append({"id": q["id"], "answer": bad})

# 一道主观题：勾 2 个采分点
subj = paper["subjectives"][0]
hits = [p["seq"] for p in subj["points"][:2]]

r = post("/api/save", {"date": TEST_DATE, "single": right + wrong,
                       "subjective": [{"id": subj["id"], "hits": hits, "cause": "记混"}]})
s = r["saved"]
print("=== 提交判分 ===")
print(f"  作答 {s['single']} 道单选（应全对 5 + 错 5）")
print(f"  客观题得分: {s['single_score']} / {s['single']*2}   （答对 {s['single_right']}/{s['single']}）")
print(f"  主观自评 {s['subjective']} 题，记录采分点命中 {s['point_hits']} 个")
assert s["single_right"] == 5, f"判分异常：应 5 题对，实际 {s['single_right']}"

w = get("/api/weakness")
print("\n=== 提交后薄弱点更新 ===")
for it in w["items"][:6]:
    print(f"  {it['hit_n']}/{it['total_n']}  {it['rate']*100:>5.0f}%  {it['path'][:56]}")
print(f"  共 {w['summary']['nodes']} 个考点有数据，薄弱 {w['summary']['weak_nodes']} 个")

# 清理测试数据
con2 = sqlite3.connect(DB)
aids = [x[0] for x in con2.execute("SELECT id FROM attempts WHERE date=?", (TEST_DATE,))]
for aid in aids:
    con2.execute("DELETE FROM point_hits WHERE attempt_id=?", (aid,))
con2.execute("DELETE FROM attempts WHERE date=?", (TEST_DATE,))
con2.commit()
print(f"\n已清理测试记录 {len(aids)} 条；剩余正式记录 {con2.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]} 条")
con.close(); con2.close()
