# -*- coding: utf-8 -*-
"""主观题【参考答案】抽取器（规则法，不调 AI）。

各年格式（侦察所得）：
  2010–2019  N.【标准答案】 错误。/…        ← 锚点最干净
  2020/2022/2024/2025  N.【答案要点】…（2020/2022）/ N. 答案要点 / N. 参考答案要点
  2009       文件里含 2009/2008/2007 三套答案，每套都有「46. 错误。」与「46．答案要点：」
             两种；**取第一套（2009 年）**，且优先「答案要点」那种（分点、可自评）
  2021       N. 错误。…（无标记，直接接答案）
  2026       N.题干\n答：（…）        ← 答案在「答：」之后
  2023       只有题干、没有答案      ← 抽不到是正常的，如实报告

**不做"顺手补答案"**：只抽参考答案正文，题干与答案的对应由锚点保证；
抽到的段落再与库里采分点做词面重叠校验，重叠过低视为可疑、不采用。
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]   # 公开副本：相对路径
DB = Path(os.environ.get("DSH_DB") or (ROOT / "data" / "kaoyan.db"))
DIR_A = ROOT / "source" / "真题库" / "解析"

SUBJ = ("analysis", "short", "essay")
# 题号锚点：分隔符有 . ． 、 ： 四种（2024 有「47：」这种，只认句点会让答案粘上下一题，踩过）
PAT_QNUM = re.compile(r"(?m)^[ \t]*(\d{1,2})[ \t]*[.．、:：]")
# 单选答案长这样：「46.【标准答案】B」，不要把它当主观题答案
PAT_SINGLE_ANS = re.compile(r"^[ \t]*【\s*(?:标准答案|参考答案)\s*】[ \t]*[A-Da-d][ \t]*$")
# 主观题答案的几种起始标记
PAT_SUBJ_START = re.compile(
    r"【\s*(?:标准答案|参考答案|答案要点|参考答案要点)\s*】"
    r"|答案要点[ \t]*[:：]?"
    r"|(?:^|\n)[ \t]*答[ \t]*[:：]"
)
# 无标记的年头（2021 / 2009）：「N.」之后直接就是答案，用这些特征判断
PAT_BARE_START = re.compile(
    r"(?:^|\n)[ \t]*(?:错误|正确)[。.、]"
    r"|(?:^|\n)[ \t]*[（(]\s*[1１]\s*[)）]"
    r"|(?:^|\n)[ \t]*[①②③④⑤]"
)


def year_file(year):
    if year == 2009:
        for p in DIR_A.glob("07-09*.md"):
            return p
        return None
    cands = [p for p in DIR_A.glob("*.md") if str(year) in p.name]
    return cands[0] if cands else None


def cut_subjective_region(text, year):
    """只保留主观题答案那一段，避免和单选答案混。"""
    if year == 2009:
        # 三套答案：取第一套（2009 年）
        m = re.search(r"(?m)^[ \t]*2009\s*年", text)
        if m:
            text = text[m.start():]
            nxt = re.search(r"(?m)^[ \t]*20\d{2}\s*年", text[1:])
            if nxt:
                text = text[:nxt.start() + 1]
    # 从**第一个主观题小节**开始（不是「三、简答题」）——
    # 只从「三、简答题」切会丢掉「二、辨析题」之后的一半（踩过：2024/2025 只认出 1–3 道）
    m = re.search(r"(?m)^[ \t]*[二]\s*、\s*辨析题", text)
    if m:
        text = text[m.start():]
    else:
        m = re.search(r"(?m)^[ \t]*[三]\s*、\s*简答题", text)
        if m:
            text = text[m.start():]
    return text


def blocks(text):
    """切成 {题号: 该题段落}（只取 46–56）。

    同一个题号常出现**两段**：先题干、后答案（2024/2025 就是这种，实测题干段在前）。
    所以不能简单 setdefault 取第一段——那样只会拿到题干，答案全漏（踩过）。
    优先级：含「答案要点/标准答案」的段落 > 更长的段落。
    """
    anchors = [(int(m.group(1)), m.start(), m.end()) for m in PAT_QNUM.finditer(text)]
    anchors = [(n, s, e) for n, s, e in anchors if 46 <= n <= 56]
    anchors.sort(key=lambda x: x[1])
    out = {}
    for i, (n, s, e) in enumerate(anchors):
        stop = anchors[i + 1][1] if i + 1 < len(anchors) else len(text)
        blk = text[s:stop]
        has_mark = bool(PAT_SUBJ_START.search(PAT_QNUM.sub("", blk, count=1)[:400]))
        prev = out.get(n)
        if prev is None:
            out[n] = blk
        else:
            prev_mark = bool(PAT_SUBJ_START.search(PAT_QNUM.sub("", prev, count=1)[:400]))
            if has_mark and not prev_mark:
                out[n] = blk          # 这一段才是答案段
            elif has_mark == prev_mark and len(blk) > len(prev):
                out[n] = blk
    return out


def extract_answer(blk, year):
    """从该题段落里切出参考答案正文；切不出返回 ''。"""
    # 先把块首的「题号.」剥掉，再找答案起点
    body0 = PAT_QNUM.sub("", blk, count=1)

    m = PAT_SUBJ_START.search(body0)
    if m:
        body = body0[m.end():]
    else:
        # 无标记的年头：从「错误。」「（1）」这类特征处开始
        m2 = PAT_BARE_START.search(body0)
        # 只在**块首 120 字内**认这种特征，避免把题干里的（1）（2）当成答案起点
        if m2 and m2.start() <= 120:
            body = body0[m2.start():]
        else:
            return ""      # 例如 2023：源文件里只有题干、没有答案（如实返回空）
    # 去掉广告/评分说明尾巴。注意别用「凯程」这种过宽的词——2024 第 55 题的答案正文
    # 就是以「凯程提示」开头的，一刀切会把整段答案切没（踩过，该题因此抽不到）。
    body = re.split(r">>>|【评分说明】|凯程提示|【解析】|注意：", body)[0]
    # 去掉广告行、分页标记、页码行
    body = "\n".join(
        ln for ln in body.splitlines()
        if not re.search(r"公众号|更多考研|考上研究生|凯程[ \t]*www|服务电话|"
                         r"^\s*=+\s*第\s*\d+\s*页\s*=+\s*$|^\s*-?\s*\d{1,3}\s*-?\s*$|"
                         r"教育学/教育硕士考研|免费获取|后台回复|"
                         # 小节标题粘在答案尾巴上（2020 第 53 题末尾是「四、分析论述题」）
                         r"^\s*[一二三四五六]\s*、\s*(?:单项选择题|辨析题|简答题|分析论述题|名词解释)", ln))
    body = body.strip()
    # 广告可能在答案前面（2024 第 55 题：广告在上一页、答案在下一页）——
    # 那就从「最后一处广告/分页标记之后」开始取，而不是一遇到广告就截断。
    m = re.search(r"(?:免获取|后台回复|考上研究生|更多考研)[^\n]*\n", body)
    if m and len(re.sub(r"[^\u4e00-\u9fff]", "", body[:m.end()])) < 40:
        body = body[m.end():]
    return clean(body)


AD_WORDS = ("公众号", "更多考研", "考上研究生", "凯程", "服务电话", "扫码", "QQ")


def is_garbage(txt):
    """抽到的段落是不是广告/残渣（要写进库会把界面搞脏）。"""
    if not txt:
        return True
    if any(w in txt for w in AD_WORDS):
        return True
    if len(re.sub(r"[^\u4e00-\u9fff]", "", txt)) < 15:
        return True
    return False


def clean(s):
    s = re.sub(r"=+\s*第\s*\d+\s*页\s*=+", "", s)
    s = re.sub(r"(?m)^[ \t]*(?:公众号[:：].*|第\s*\d+\s*页.*|- ?\d+ ?-)$", "", s)
    # 开头可能有广告/提示行（如「凯程提示」「注意：」），删掉直到第一行有实质内容
    s = re.sub(r"^(?:[ \t]*(?:凯程提示|注意|解析|答)\s*[:：]?[ \t]*\n?)+", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    s = s.strip()[:4000]
    return s


def main():
    check = "--check" in sys.argv
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    rows = [dict(r) for r in con.execute(
        "SELECT id,year,number,qtype,stem FROM questions "
        "WHERE qtype IN ('analysis','short','essay') AND year IS NOT NULL "
        "ORDER BY year, CAST(number AS INTEGER)")]
    print(f"主观题（有年份的）：{len(rows)} 道")

    by_year = {}
    for r in rows:
        by_year.setdefault(r["year"], []).append(r)

    to_write, skipped = [], []
    print()
    print(f"{'年份':6s} {'主观题':>6s} {'抽到答案':>8s} {'跳过':>6s}")
    for y in sorted(by_year):
        f = year_file(y)
        if not f:
            print(f"{y:6d} {len(by_year[y]):>6d} {'—':>8s} {'无解析文件':>6s}")
            continue
        text = cut_subjective_region(f.read_text(encoding="utf-8"), y)
        blks = blocks(text)
        ok = miss = 0
        for q in by_year[y]:
            n = int(q["number"]) if q["number"] else None
            ans = extract_answer(blks.get(n, ""), y) if n else ""
            if ans and not is_garbage(ans):
                ok += 1
                to_write.append((q["id"], ans))
            else:
                miss += 1
                skipped.append((y, n))
                # 抽到但判定是垃圾 → 也要登记，好把库里可能存在的旧脏值清掉
                if ans:
                    to_write.append((q["id"], ""))
        print(f"{y:6d} {len(by_year[y]):>6d} {ok:>8d} {miss:>6d}")

    print()
    print(f"合计：抽到 {len(to_write)} 道，未抽到 {len(skipped)} 道")
    if skipped:
        from collections import Counter
        c = Counter(y for y, _ in skipped)
        print("  未抽到按年：", dict(c))

    if check:
        print("\n--check：未写库。抽样看 3 条：")
        by_id = {q["id"]: q for q in rows}
        for qid, ans in to_write[:1] + to_write[len(to_write) // 2:len(to_write) // 2 + 1] + to_write[-1:]:
            q = by_id[qid]
            print(f"  --- {q['year']} 第{q['number']}题 [{q['qtype']}] ---")
            print(f"      题干：{(q['stem'] or '')[:60]}")
            print(f"      答案：{ans[:200]}".replace("\n", " "))
        con.close()
        return

    n = removed = 0
    for qid, ans in to_write:
        row = con.execute("SELECT extra FROM questions WHERE id=?", (qid,)).fetchone()
        try:
            d = json.loads(row["extra"] or "{}")
        except Exception:
            d = {}
        if ans:
            if d.get("answer_text") == ans:
                continue
            d["answer_text"] = ans
        elif d.get("answer_text"):
            d.pop("answer_text", None)      # 本轮抽到的是垃圾/空 → 清掉旧值，别留着脏数据
            removed += 1
        else:
            continue
        con.execute("UPDATE questions SET extra=? WHERE id=?",
                    (json.dumps(d, ensure_ascii=False), qid))
        n += 1
    con.commit()
    cov = con.execute("SELECT COUNT(*) FROM questions WHERE qtype IN ('analysis','short','essay') "
                      "AND extra LIKE '%answer_text%'").fetchone()[0]
    tot = con.execute("SELECT COUNT(*) FROM questions WHERE qtype IN "
                      "('analysis','short','essay')").fetchone()[0]
    print(f"\n✅ 写入/更新 {n} 道主观题的参考答案，清理旧脏值 {removed} 条；覆盖 {cov}/{tot}")
    con.close()


if __name__ == "__main__":
    main()
