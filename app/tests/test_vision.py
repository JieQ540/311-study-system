import base64, json, sys, urllib.request, urllib.error
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
import pymupdf

# 用一页 PDF 渲染成图片，当作「手写照片」测多模态链路
src = Path(Path(__file__).resolve().parent.parent.parent / "01-311教育学真题(07-26)" / "2025教育学考研真题.pdf")
doc = pymupdf.open(src)
pix = doc[9].get_pixmap(dpi=90)
img_path = Path(Path(__file__).resolve().parent.parent.parent / "source" / "_test_handwriting.png")
pix.save(img_path)
doc.close()
b64 = base64.b64encode(img_path.read_bytes()).decode()
data_url = "data:image/png;base64," + b64
print(f"测试图片 {pix.width}x{pix.height}, base64 {len(b64)//1024} KB")

# 拿一道主观题
def call(path, payload=None, timeout=600):
    url = "http://127.0.0.1:8765" + path
    if payload is None:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

paper = call("/api/paper")
subj = paper["subjectives"][0]
print(f"题目 id={subj['id']}  类型={subj['qtype']}  采分点={len(subj['points'])}")
print("调多模态批改…")
r = call("/api/daily/grade", {"question_id": subj["id"], "image": data_url})
print(f"  ok={r.get('ok')}")
if r.get("ok"):
    d = r["result"]
    print(f"  recognized 字段存在: {'✅' if 'recognized' in d else '❌'}")
    print(f"  识别内容前 100 字: {str(d.get('recognized'))[:100]}")
    print(f"  得分: {d.get('score')}/{d.get('full_score')}")
    print(f"  用量: {r.get('usage',{}).get('prompt_tokens')}/{r.get('usage',{}).get('completion_tokens')}")
else:
    print("  ", r.get("error"))
