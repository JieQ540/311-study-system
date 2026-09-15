import json, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8765"

def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))

print("=== /api/ai/test ===")
print(" ", post("/api/ai/test", {}))

print("\n=== /api/daily/parse（真实学习记录）===")
r = post("/api/daily/parse", {"text": "今天背诵夸美纽斯、卢梭、裴斯泰洛齐的教育思想，做了中国教育史理学部分的题"})
print(f"  ok={r.get('ok')}")
for m in r.get("matches", []):
    print(f"  {m.get('confidence',0):.2f} [{m.get('id')}] {m.get('path','')[:60]}")
print("  unmatched:", r.get("unmatched"))
