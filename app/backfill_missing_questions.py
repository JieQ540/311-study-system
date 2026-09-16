# -*- coding: utf-8 -*-
"""定向补题：把 4 个年份漏掉的 5 道主观题补进库，并拆采分点。

**为什么不用 `ingest_subjective.py --force` 重跑**：那次是**整年删掉重来**，
而漏题的原因是 AI 提取不稳（同一位置很可能再漏）。重跑既可能补不上，
也可能把已正确的题换掉。所以这里**只补缺的那几道**，已有的题目一个字不动。

题干来源：以真题文件为准；真题文件 OCR 缺页的（2019 第 46 题），
用该年解析文件里的答案所指命题重建，并在脚本里注明。

采分点：逐题调 AI（参考答案取自 `source/真题库/解析/`），
沿用 ingest_subjective.py 的「3–6 个论点/维度」口径。

用法：
    python backfill_missing_questions.py --dry-run   # 只看要补什么，不调 AI、不写库
    python backfill_missing_questions.py             # 真做
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))
import ai

ROOT = Path(__file__).resolve().parents[1]   # 公开副本：相对路径
# 与 server.py 一致：DSH_DB 可把库指向副本，用于「先在副本上验证」（docs/交接文档.md §5.1）
DB = Path(os.environ.get("DSH_DB") or (ROOT / "data" / "kaoyan.db"))
DIR_A = ROOT / "source" / "真题库" / "解析"

QTYPE_SCORE = {"analysis": 15, "short": 15, "essay": 30}

# 要补的题：题干以真题文件原文为准（2019 第 46 题真题 OCR 缺页，见 note）
# kw_words 用来校验「取到的参考答案」和这道题是同一道（防止多年文件里串年）
TARGETS = [
    dict(year=2009, number=46, qtype="analysis",
         stem="人是教育的产物。",
         answer_kw="46", kw_words=["外铄论"],
         note="源：07-09年教育学311真题.md 原文"),
    dict(year=2009, number=51, qtype="short",
         stem="简述墨家教育的特色",
         answer_kw="51", kw_words=["墨家", "农民", "小工商业者"],
         note="源：07-09年教育学311真题.md 原文"),
    dict(year=2012, number=48, qtype="analysis",
         stem="心智技能的学习过程就是练习的过程。",
         answer_kw="48", kw_words=["心智技能"],
         note="源：2012年真题只列到 47（源文件本身缺 48 题干），"
              "题干按解析第 48 题答案所评述的命题重建"),
    dict(year=2018, number=49, qtype="short",
         stem="简述我国现行学制。",
         answer_kw="49", kw_words=["学制", "六三三"],
         note="源：2018年教育学311真题.md 原文"),
    dict(year=2019, number=46, qtype="analysis",
         stem="道德不可教。",
         answer_kw="46", kw_words=["道德"],
         note="源：2019年真题 OCR 缺页（第 46 题只剩「道德不可教。」），"
              "该命题本身即题干，据解析第 46 题答案重建"),
]

SYS = """你是考研 311 教育学专业基础的阅卷与命题专家。

用户会给你某一道 311 主观题的题干与该题的参考答案（来自真题解析）。
请拆出**采分点**，规则：
1. 拆 3–6 个，按「论点/维度」拆，**不要按句子拆**；题目本身是 3 个维度就拆 3 个。
2. claim：一句核心论断，简短（用于勾选，控制在 30 字内）。
3. evidence：判分依据，尽量贴近参考答案原文。
4. 参考文献没提到的内容不要编。
5. 输出纯 JSON：{"points":[{"seq":1,"claim":"…","evidence":"…"}]}
"""


def answer_text(year, kw):
    """取该年解析文件里某道题的参考答案段落。

    两个坑（都踩过）：
      1. 文件名不一定以年份开头：2009 的解析叫 `07-09年311教育学解析.md`，
         里面按 2009 → 2008 → 2007 排了三年，题号重复三次。
         所以必须**先定位到该年的小节**再取题号，否则会取到别的年份的答案。
      2. 取到的段落必须和库里同年的题干对得上——对不上说明错位，宁可返回空。
    """
    cands = [p for p in sorted(DIR_A.glob("*.md")) if str(year) in p.name]
    if not cands and "07-09" in "".join(p.name for p in DIR_A.glob("*.md")):
        cands = [p for p in sorted(DIR_A.glob("*.md")) if p.name.startswith("07-09")] \
            if 2007 <= year <= 2009 else []
    if not cands:
        return ""
    t = cands[0].read_text(encoding="utf-8")

    # 多年合一文件：切到本年小节（到下一个年份标题为止）
    years_in = sorted({int(y) for y in re.findall(r"(20\d{2})\s*年教育学统考真题答案解析", t)})
    if len(years_in) > 1:
        m = re.search(rf"(?m)^\s*{year}\s*年教育学统考真题答案解析", t)
        if not m:
            return ""
        t = t[m.start():]
        nxt = re.search(r"(?m)^\s*20\d{2}\s*年教育学统考真题答案解析", t[1:])
        if nxt:
            t = t[:nxt.start() + 1]

    m = re.search(rf"(?m)(?:^|\n)\s*{kw}\s*[.．、]", t)
    if not m:
        return ""
    rest = t[m.start():]
    nxt = re.search(rf"(?m)(?:^|\n)\s*0?{int(kw) + 1}\s*[.．、]", rest[1:])
    seg = rest[:nxt.start() + 1] if nxt else rest
    return seg[:6000]


def verify_answer_matches(year, seg, sample_stem_words):
    """粗校验：取到的参考答案段落应和该题题干有共同关键词，否则视为错位。

    只做"宁可漏、不可错"的保守判断：没有任何共同关键词就拒绝。
    """
    if not seg:
        return False
    return any(w in seg for w in sample_stem_words if w)


def main():
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    print("=" * 74)
    print("待补题目核对（库里是否已存在）")
    print("=" * 74)
    todo = []
    for t in TARGETS:
        ex = con.execute(
            "SELECT id, stem FROM questions WHERE year=? AND qtype=? AND number=?",
            (t["year"], t["qtype"], str(t["number"]))).fetchone()
        ans = answer_text(t["year"], t["answer_kw"])
        matched = verify_answer_matches(t["year"], ans, t["kw_words"])
        flag = "已存在，跳过" if ex else "缺，将补"
        print(f"  {t['year']} 第{t['number']}题 [{t['qtype']}] {flag}｜"
              f"参考答案 {len(ans)} 字｜与题干相符: {'是' if matched else '否'}")
        print(f"      题干：{t['stem'][:60]}")
        print(f"      依据：{t['note']}")
        if not ex:
            if not ans:
                print("      ⚠ 没取到参考答案，跳过（不编造）")
                continue
            if not matched:
                print(f"      ⚠ 参考答案与题干对不上（可能串年/错位），跳过（不编造）")
                continue
            todo.append((t, ans))

    if not todo:
        print("\n没有要补的题。")
        con.close()
        return
    if dry:
        print(f"\n--dry-run：以上 {len(todo)} 道题将被补入，未调用 AI、未写库。")
        con.close()
        return

    print()
    print("=" * 74)
    print("逐题拆采分点并入库")
    print("=" * 74)
    tot_in = tot_out = 0
    added = []
    for t, ans in todo:
        src = f"{t['year']}真题·主观"
        user = (f"【{t['year']} 年 311 真题 · {t['qtype']} 第 {t['number']} 题】\n"
                f"题干：{t['stem']}\n\n【参考答案】\n{ans}\n\n请拆采分点并输出 JSON。")
        try:
            data, usage = ai.chat_json(
                [{"role": "system", "content": SYS}, {"role": "user", "content": user}],
                max_tokens=4000, temperature=0.2)
        except Exception as e:
            print(f"  ❌ {t['year']} 第{t['number']}题 调用失败：{type(e).__name__}: {str(e)[:100]}")
            continue
        if usage:
            tot_in += usage.get("prompt_tokens", 0)
            tot_out += usage.get("completion_tokens", 0)

        pts = []
        for i, p in enumerate(data.get("points") or [], 1):
            claim = re.sub(r"\s+", " ", (p.get("claim") or "")).strip()
            ev = re.sub(r"\s+", " ", (p.get("evidence") or "")).strip()
            if claim:
                pts.append({"seq": p.get("seq") or i, "claim": claim, "evidence": ev})
        if len(pts) < 2:
            print(f"  ❌ {t['year']} 第{t['number']}题 只拆出 {len(pts)} 个采分点，"
                  f"太少，不入库（避免又造一道没采分点的题）")
            continue

        cur = con.execute(
            "INSERT INTO questions (year,source,qtype,number,stem,extra) VALUES (?,?,?,?,?,?)",
            (t["year"], src, t["qtype"], str(t["number"]), t["stem"],
             json.dumps({"full_score": QTYPE_SCORE[t["qtype"]],
                         "backfilled": t["note"]}, ensure_ascii=False)))
        qid = cur.lastrowid
        for p in pts:
            con.execute("INSERT INTO points (question_id,seq,claim,evidence) VALUES (?,?,?,?)",
                        (qid, p["seq"], p["claim"], p["evidence"]))
        con.commit()
        added.append((t, qid, len(pts)))
        print(f"  ✅ {t['year']} 第{t['number']}题 → question id={qid}，采分点 {len(pts)} 个")

    cost = tot_in / 1e6 * 1 + tot_out / 1e6 * 4
    print(f"\nAI 用量 {tot_in}/{tot_out} tokens ≈ {cost:.4f} 元")
    print(f"补入 {len(added)} 道题")

    # 校验：每年主观题是否齐了（辨析3/简答5/论述3）
    print()
    print("=" * 74)
    print("补齐后逐年核对")
    print("=" * 74)
    ok = True
    for y in sorted({t["year"] for t in TARGETS}):
        d = {k: con.execute(
            "SELECT COUNT(*) FROM questions WHERE year=? AND qtype=?", (y, k)).fetchone()[0]
            for k in ("analysis", "short", "essay")}
        good = d["analysis"] == 3 and d["short"] == 5 and d["essay"] == 3
        ok = ok and good
        print(f"  {y}: 辨析 {d['analysis']}/3、简答 {d['short']}/5、"
              f"分析论述 {d['essay']}/3  {'✅' if good else '❌'}")
    # 新补的题必须都有采分点
    miss = con.execute("""
      SELECT COUNT(*) FROM questions q WHERE q.id IN (%s)
        AND (SELECT COUNT(*) FROM points p WHERE p.question_id=q.id)=0
    """ % ",".join(str(q) for _, q, _ in added) if added else "SELECT 0").fetchone()[0]
    print(f"  新补题目中无采分点的：{miss} 道")
    con.close()
    if not ok or miss:
        sys.exit(1)


if __name__ == "__main__":
    main()
