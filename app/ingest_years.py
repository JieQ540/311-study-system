# -*- coding: utf-8 -*-
"""批量把 18 个年份的真题入选题库。

两步：
  ① 题干与选项：从 真题库/真题/*.md 用正则切（格式较统一）
  ② 答案：从 真题库/解析/*.md 用 AI 提取（各年格式不同：故选X / 答案为X / 【答案】X，
     且常有缺漏，正则按顺序对应会错位）

用法：
    python ingest_years.py            # 全部年份
    python ingest_years.py 2021       # 只处理含 "2021" 的文件
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import ai

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "kaoyan.db"
DIR_Q = ROOT / "source" / "真题库" / "真题"
DIR_A = ROOT / "source" / "真题库" / "解析"

RE_PAGE = re.compile(r"^=+\s*第\s*\d+\s*页\s*=+$")
RE_Q = re.compile(r"^(\d{1,2})\s*[.．]\s*(.*)$")
RE_O = re.compile(r"([A-D])\s*[.．、]\s*")

ANSWER_SYS = """你是考研 311 真题结构化提取器。

用户会给你：① 某年 311 真题原文；② 该年的答案解析原文。
请你提取其中**全部单项选择题**（通常 1–45 题）。

规则：
1. 题干要**完整**（含引文、情境），但**去掉题号**。
2. 选项必须凑齐 A/B/C/D 四项，**去掉选项字母**，只留选项文字。
3. 若某题选项残缺或题干无法辨认，**跳过该题**，不要编造。
4. answer 从解析文本中判断；判断不了就填 null。
5. 原文可能存在字间空格（如「某 化 学 老 师」），请自行还原成正常中文。
6. 输出纯 JSON：
{"questions":[{"number":1,"stem":"…","options":{"A":"…","B":"…","C":"…","D":"…"},"answer":"A"}]}
"""


def extract_via_ai(qpath, apath):
    """一次调用：从真题+解析原文提取结构化单选。"""
    qtxt = qpath.read_text(encoding="utf-8")
    atxt = apath.read_text(encoding="utf-8") if apath and apath.exists() else ""
    # 压掉页标记与多余空行，控制 token
    qtxt = re.sub(r"=+\s*第\s*\d+\s*页\s*=+", "", qtxt)
    qtxt = re.sub(r"\n{2,}", "\n", qtxt)[:26000]
    atxt = re.sub(r"\n{2,}", "\n", atxt)[:16000]
    user = f"""【真题原文】
{qtxt}

【答案解析原文】
{atxt}

请提取全部单项选择题，输出 JSON。"""
    data, usage = ai.chat_json(
        [{"role": "system", "content": ANSWER_SYS}, {"role": "user", "content": user}],
        max_tokens=9000,
        temperature=0.1,
    )
    out = []
    for q in data.get("questions", []):
        opts = {k: (v or "").strip() for k, v in (q.get("options") or {}).items()}
        stem = re.sub(r"\s+", "", (q.get("stem") or ""))
        n = q.get("number")
        a = (q.get("answer") or "").strip().upper()[:1]  # 注意：可能是空串，不能先转 None 再 in
        if not isinstance(n, int) or not stem or set(opts) != {"A", "B", "C", "D"}:
            continue
        out.append({"no": n, "stem": stem, "options": opts, "answer": a if a in "ABCD" else None})
    return out, usage


def parse_questions(path):
    """从真题文本切出单选题（题干 + 四选项）。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    items, cur = [], None
    for raw in lines:
        line = raw.strip()
        if not line or RE_PAGE.match(line):
            continue
        # 单选部分通常在前半；遇到主观题标题就停
        if re.match(r"^[一二三四]\s*、\s*(辨析题|简答题|分析论述题)", line):
            break
        if re.match(r"^(参考答案|【答案)", line):
            break
        mq = RE_Q.match(line)
        if mq:
            num = int(mq.group(1))
            if cur is None or (num > cur["no"] and num <= cur["no"] + 3):
                cur = {"no": num, "buf": [mq.group(2)]}
                items.append(cur)
                continue
        if cur is not None:
            cur["buf"].append(line)
    out = []
    for it in items:
        if it["no"] > 45:
            continue
        blob = re.sub(r"\s+", "", "".join(it["buf"]))
        parts = RE_O.split(blob)
        stem, opts = parts[0], {}
        for i in range(1, len(parts) - 1, 2):
            k = parts[i]
            if k not in opts:
                opts[k] = parts[i + 1]
        if set(opts) == {"A", "B", "C", "D"} and len(stem) > 5:
            out.append({"no": it["no"], "stem": stem, "options": opts})
    return out


def extract_answers(path):
    """用 AI 从解析文本提取答案。文本可能较长，先压缩空白。"""
    txt = re.sub(r"[ \t\u3000]+", "", path.read_text(encoding="utf-8"))
    txt = re.sub(r"\n{2,}", "\n", txt)
    if len(txt) > 24000:
        txt = txt[:24000]
    try:
        data, usage = ai.chat_json(
            [{"role": "system", "content": ANSWER_SYS},
             {"role": "user", "content": f"解析文本：\n{txt}"}],
            max_tokens=2000,
        )
    except Exception as e:
        print(f"    [答案提取失败] {e}")
        return {}, None
    ans = {}
    for a in data.get("answers", []):
        n, v = a.get("number"), (a.get("answer") or "").strip().upper()[:1]
        if isinstance(n, int) and v in "ABCD":
            ans[n] = v
    return ans, usage


def year_of(name):
    m = re.search(r"(20\d{2})", name)
    if m:
        return int(m.group(1))
    # 支持「24年」这类简写（缺 2 位年份）
    m = re.search(r"(?<!\d)(\d{2})\s*年", name)
    if m:
        n = int(m.group(1))
        return 2000 + n if n < 50 else 1900 + n
    if "07-09" in name or "0709" in name:
        return 2009
    return None


def match_analysis(qf_name, year):
    """按年份数字匹配解析文件（兼容「2024年311教育学解析」「24年…」等写法）。"""
    cands = list(DIR_A.glob("*.md"))
    if year:
        y2 = str(year)[2:]
        for x in cands:
            if str(year) in x.name or f"{y2}年" in x.name:
                return x
    key = re.sub(r"年.*", "", Path(qf_name).stem)[:12]
    for x in cands:
        if key and key in x.name:
            return x
    return None


def main():
    filt = sys.argv[1] if len(sys.argv) > 1 else None
    qfiles = sorted(DIR_Q.glob("*.md"))
    if filt:
        qfiles = [f for f in qfiles if filt in f.name]

    con = sqlite3.connect(DB)
    total_in = total_out = 0
    for qf in qfiles:
        year = year_of(qf.name)
        af = match_analysis(qf.name, year)
        print(f"\n=== {qf.stem}  (year={year}) ===")
        try:
            if not af:
                print("  ⚠ 未找到对应解析文件，跳过")
                continue
            qs, usage = extract_via_ai(qf, af)
        except Exception as e:
            # 单个年份失败不应中断整批（网络抖动/超时都可能）
            print(f"  ❌ 失败，跳过: {type(e).__name__}: {str(e)[:120]}")
            continue
        if usage:
            total_in += usage.get("prompt_tokens", 0)
            total_out += usage.get("completion_tokens", 0)
        print(f"  AI 提取: {len(qs)} 道  （来源 {af.name}）")
        if not qs:
            continue

        src = f"{year}真题·单选" if year else f"{qf.stem}·单选"
        existing = con.execute("SELECT COUNT(*) FROM questions WHERE source=?", (src,)).fetchone()[0]
        if existing >= 40 and "--force" not in sys.argv:
            print(f"  已有 {existing} 道，跳过（--force 可强制重做）")
            continue
        con.execute("DELETE FROM questions WHERE source=?", (src,))
        for q in qs:
            con.execute(
                "INSERT INTO questions (year,source,qtype,number,stem,options,answer) "
                "VALUES (?,?,?,?,?,?,?)",
                (year, src, "single", str(q["no"]), q["stem"],
                 json.dumps(q["options"], ensure_ascii=False), q.get("answer")),
            )
        con.commit()
        have = sum(1 for q in qs if q.get("answer"))
        print(f"  已入库 {len(qs)} 道（其中有答案 {have} 道）")

    cost = total_in / 1e6 * 1 + total_out / 1e6 * 4
    print(f"\nAI 用量: {total_in}/{total_out} tokens  ≈ {cost:.4f} 元")
    tot = con.execute("SELECT COUNT(*), COUNT(DISTINCT year) FROM questions WHERE source LIKE '%真题%'").fetchone()
    print(f"题库真题总数: {tot[0]} 道，覆盖年份 {tot[1]} 个")
    con.close()


if __name__ == "__main__":
    main()
