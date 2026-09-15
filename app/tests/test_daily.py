import json, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8765"

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return r.status, r.read()
def post(path, payload, timeout=600):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

st, body = get("/daily")
print(f"GET /daily -> {st}, {len(body)} 字节  {'✅' if b'311' in body else '?'}")
st, body = get("/")
print(f"GET /     -> {st}, {len(body)} 字节  含日常练习入口: {'✅' if 'daily'.encode() in body else '❌'}")

print("\n=== 出题测试（5 客观 + 1 简答，小规模省 token）===")
r = post("/api/daily/generate", {"point_ids": [307], "n_single": 5, "n_analysis": 0, "n_short": 1, "n_essay": 0})
print(f"  ok={r.get('ok')}  真题 {r.get('real_count')} 道  AI 生成 {r.get('generated_count')} 道")
g = r["paper"]["generated"]
for q in g.get("single", []):
    print(f"  [单选 id={q.get('id')}] {q.get('stem','')[:52]}  答案={q.get('answer')}")
for q in g.get("subjective", []):
    print(f"  [{q.get('qtype')} id={q.get('id')}] {q.get('stem','')[:52]}  采分点 {len(q.get('points',[]))} 个")
