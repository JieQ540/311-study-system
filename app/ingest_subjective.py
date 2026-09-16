# -*- coding: utf-8 -*-
"""把各年真题的**主观题**入库，并拆出采分点。

为什么单独做：ingest_years.py 只处理了单选；814 道真题里约 200 道主观题
要么没入库，要么（2025 那 11 道）有题目没采分点，导致日常练习抽到它们就是"死题"。

做法：
  对每个年份，把「真题原文的主观题部分」交给 AI，一次完成：
    ① 提取题干（含材料、小问，保留换行）
    ② 拆 3–6 个采分点（claim 核心论断 + evidence 判分依据）
    ③ 标注题型与满分

入库：questions(source='{year}真题·主观') + points

用法：
    python ingest_subjective.py            # 全部年份
    python ingest_subjective.py 2021       # 只处理 2021
    python ingest_subjective.py 2021 --force   # 忽略已有
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import ai

ROOT = Path(__file__).resolve().parents[1]   # 公开副本：相对路径
DB = ROOT / "data" / "kaoyan.db"
DIR_Q = ROOT / "source" / "真题库" / "真题"
DIR_A = ROOT / "source" / "真题库" / "解析"

# 311 主观题题型与分值
QTYPE_SCORE = {"analysis": 15, "short": 15, "essay": 30}

SYS = """你是考研 311 教育学专业基础的阅卷与命题专家。

用户会给你某年 311 真题的原文（可能含解析）。请提取其中**全部主观题**并拆解采分点。

题型判定：
- 辨析题 analysis：给出一个待判断正误的命题（第 46–48 题）
- 简答题 short：直接提问，要求分点作答（第 49–53 题）
- 分析论述题 essay：含材料或多小问，分值 30 分（第 54–56 题）

要求：
1. **题干必须完整**：材料原文、所有小问都要保留；材料与各小问之间用 \\n 分段，
   「请回答：」独立成行，每个小问（1）（2）… 独立成行。
   不要加「辨析题：」这类题型前缀。
2. **采分点拆 3–6 个**，按「论点/维度」拆，不要按句子拆：
   - claim：一句核心论断，简短（用于勾选）
   - evidence：判分依据（尽量贴近参考答案原文）
   - 当题目本身就是 3 个维度时（如比较题），就拆 3 个，不要硬凑。
3. 只提取**确实存在**的题，不要编造。找不到主观题就返回空数组。
4. 输出纯 JSON：
{"questions":[{"number":46,"qtype":"analysis","full_score":15,
  "stem":"完整题干（含换行）",
  "points":[{"seq":1,"claim":"…","evidence":"…"}]}]}
"""


def year_of(name):
    m = re.search(r"(20\d{2})", name)
    if m:
        return int(m.group(1))
    m = re.search(r"(?<!\d)(\d{2})\s*年", name)
    if m:
        n = int(m.group(1))
        return 2000 + n if n < 50 else 1900 + n
    if "07-09" in name:
        return 2009
    return None


def match_analysis(qf_name, year):
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


def subjective_part(path):
    """只取真题文本里主观题那一段，减小 token。"""
    txt = path.read_text(encoding="utf-8")
    txt = re.sub(r"=+\s*第\s*\d+\s*页\s*=+", "", txt)
    # 从「辨析题」题型标题开始
    m = re.search(r"[一二三四]\s*、\s*辨析题", txt)
    if m:
        txt = txt[m.start():]
    # 到「参考答案」为止
    m2 = re.search(r"参考答案|【答案要点】", txt)
    if m2:
        txt = txt[:m2.start()]
    return re.sub(r"\n{2,}", "\n", txt)[:14000]


def extract(qf, af, year):
    qtxt = subjective_part(qf)
    atxt = ""
    if af and af.exists():
        a = af.read_text(encoding="utf-8")
        m = re.search(r"(参考答案|【答案要点】|二、辨析题)", a)
        if m:
            a = a[m.start():]
        atxt = re.sub(r"\n{2,}", "\n", a)[:14000]
    user = f"""【{year} 年 311 真题 · 主观题部分】
{qtxt}

【该年答案解析（供拆采分点参考）】
{atxt}

请提取全部主观题并拆采分点，输出 JSON。"""
    data, usage = ai.chat_json(
        [{"role": "system", "content": SYS}, {"role": "user", "content": user}],
        max_tokens=12000,
        temperature=0.2,
    )
    out = []
    for q in data.get("questions", []):
        stem = (q.get("stem") or "").strip()
        qtype = q.get("qtype")
        pts = q.get("points") or []
        if qtype not in QTYPE_SCORE or len(stem) < 10 or not pts:
            continue
        clean_pts = []
        for i, p in enumerate(pts, 1):
            claim = re.sub(r"\s+", " ", (p.get("claim") or "")).strip()
            ev = re.sub(r"\s+", " ", (p.get("evidence") or "")).strip()
            if claim:
                clean_pts.append({"seq": p.get("seq") or i, "claim": claim, "evidence": ev})
        if clean_pts:
            out.append({"number": q.get("number"), "qtype": qtype,
                        "full_score": q.get("full_score") or QTYPE_SCORE[qtype],
                        "stem": stem, "points": clean_pts})
    return out, usage


def main():
    filt = next((a for a in sys.argv[1:] if not a.startswith("--")), None)
    force = "--force" in sys.argv
    qfiles = sorted(DIR_Q.glob("*.md"))
    if filt:
        qfiles = [f for f in qfiles if filt in f.name]

    con = sqlite3.connect(DB)
    tot_in = tot_out = 0
    done_years = []
    for qf in qfiles:
        year = year_of(qf.name)
        if not year:
            continue
        src = f"{year}真题·主观"
        existing = con.execute("SELECT COUNT(*) FROM questions WHERE source=?", (src,)).fetchone()[0]
        if existing and not force:
            print(f"[跳过] {year} 已有 {existing} 道主观题")
            continue
        af = match_analysis(qf.name, year)
        print(f"\n=== {year} 年 ===")
        try:
            qs, usage = extract(qf, af, year)
        except Exception as e:
            print(f"  ❌ 失败: {type(e).__name__}: {str(e)[:110]}")
            continue
        if usage:
            tot_in += usage.get("prompt_tokens", 0)
            tot_out += usage.get("completion_tokens", 0)

        # 清旧数据
        old_ids = [r[0] for r in con.execute("SELECT id FROM questions WHERE source=?", (src,))]
        for qid in old_ids:
            con.execute("DELETE FROM points WHERE question_id=?", (qid,))
        con.execute("DELETE FROM questions WHERE source=?", (src,))

        npts = 0
        for q in qs:
            cur = con.execute(
                "INSERT INTO questions (year,source,qtype,number,stem,extra) VALUES (?,?,?,?,?,?)",
                (year, src, q["qtype"], str(q["number"]) if q["number"] else None,
                 q["stem"], json.dumps({"full_score": q["full_score"]}, ensure_ascii=False)),
            )
            qid = cur.lastrowid
            for p in q["points"]:
                con.execute(
                    "INSERT INTO points (question_id,seq,claim,evidence) VALUES (?,?,?,?)",
                    (qid, p["seq"], p["claim"], p["evidence"]),
                )
                npts += 1
        con.commit()
        print(f"  入库主观题 {len(qs)} 道，采分点 {npts} 个")
        done_years.append(year)

    cost = tot_in / 1e6 * 1 + tot_out / 1e6 * 4
    print(f"\nAI 用量: {tot_in}/{tot_out} tokens ≈ {cost:.4f} 元")
    n, y = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT year) FROM questions WHERE source LIKE '%真题·主观'"
    ).fetchone()
    print(f"主观题合计: {n} 道，覆盖 {y} 个年份")
    con.close()


if __name__ == "__main__":
    main()
