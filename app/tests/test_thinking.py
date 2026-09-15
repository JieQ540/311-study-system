import json, sys, urllib.request
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
cfg = json.loads(Path(Path(__file__).resolve().parent.parent.parent / "config.json").read_text(encoding="utf-8"))["api"]

def call(payload):
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["api_key"]},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))

print("=== 尝试关闭思考模式（extra_body: thinking=disabled）===")
for body_key in ("thinking", "reasoning"):
    try:
        res = call({
            "model": cfg["model"],
            "messages": [{"role": "user", "content": "只回答两个字：连通"}],
            "max_tokens": 64,
            body_key: {"type": "disabled"} if body_key == "thinking" else {"effort": "none"},
        })
        msg = res["choices"][0]["message"]
        print(f"  {body_key}=disabled -> 内容={msg.get('content')!r}  用量={res.get('usage')}")
    except Exception as e:
        print(f"  {body_key} 失败: {e}")
