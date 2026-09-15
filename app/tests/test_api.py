# -*- coding: utf-8 -*-
"""测试 DeepSeek API 连通性 + 成本观察。

用法: python test_api.py
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import urllib.request

CFG = Path(Path(__file__).resolve().parent.parent.parent / "config.json")


def call(messages, max_tokens=64, model=None):
    cfg = json.loads(CFG.read_text(encoding="utf-8"))["api"]
    payload = {
        "model": model or cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
    )
    with urllib.request.urlopen(req, timeout=cfg.get("timeout_seconds", 120)) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    cfg = json.loads(CFG.read_text(encoding="utf-8"))["api"]
    key = cfg["api_key"]
    print(f"配置: model={cfg['model']}  base_url={cfg['base_url']}")
    print(f"key : {key[:8]}…{key[-4:]}  (长度 {len(key)})")
    print()

    try:
        res = call([{"role": "user", "content": "只回答两个字：连通"}], max_tokens=512)
    except Exception as e:
        print("❌ 调用失败:", e)
        body = getattr(e, "read", None)
        if body:
            try:
                print("   响应:", e.read().decode("utf-8")[:400])
            except Exception:
                pass
        return

    print("✅ API 连通")
    print("  实际模型:", res.get("model"))
    print("  回复内容:", res["choices"][0]["message"]["content"].strip())
    u = res.get("usage") or {}
    print(f"  用量: 输入 {u.get('prompt_tokens')} / 输出 {u.get('completion_tokens')} tokens")
    print(f"  （本次约 {u.get('prompt_tokens',0)/1e6*1 + u.get('completion_tokens',0)/1e6*4:.6f} 元，空闲时段）")


if __name__ == "__main__":
    main()
