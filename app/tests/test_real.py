import json, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8765"
def post(path, payload, timeout=600):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

print("=== 验证「真题优先」：考点 307（裴斯泰洛齐，父节 304）===")
r = post("/api/daily/generate", {"point_ids":[307], "n_single":12, "n_analysis":1, "n_short":0, "n_essay":0})
print(f"  ok={r.get('ok')}  真题 {r.get('real_count')} 道  AI 生成 {r.get('generated_count')} 道")
for q in r["paper"]["real"]["single"][:4]:
    print(f"    [真题 {q['number']}] {q['stem'][:56]}")
for q in r["paper"]["real"]["subjective"][:2]:
    print(f"    [真题 {q['number']} {q['qtype']}] {q['stem'][:48]}")
