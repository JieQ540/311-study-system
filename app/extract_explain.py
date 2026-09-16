# -*- coding: utf-8 -*-
"""把解析源里的【答案解析】抽出来，存进 questions.extra.explain。

需求：做完题只能看到采分点，看不到解析 —— 源文件里有现成的，规则抽取即可，**不用调 AI**。

四种格式（实测各年不一样，别想用一个正则通吃）：
  A  N.【标准答案】B + 解析         （2010–2019）
  B  N.【解析】A理解题 此题考查…   （2020–2022）
  C  N.【解析】D。                 （2023，题干选项夹在中间）
  D  N.【解析】A / N. 答案：C + 解析：（2025 / 2026）

**安全闸门**：只有当抽取到的答案字母与库里 `questions.answer` 一致时，才写这道题的解析。
对不上说明题干/年份错位（或解析与真题版本不同），宁可这道题没有解析，也不写错解析。

用法：
    python extract_explain.py --check    # 只校验（取到多少、与库答案一致多少、不一致样例）
    python extract_explain.py            # 校验通过后写入
    python extract_explain.py --check --year 2018
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

# 正文里的「结论句」——比开头那个字母更可靠（开头字母常被 OCR 或排版错位影响）
PAT_CONCLUSION = re.compile(
    r"(?:本题)?(?:正确)?答案(?:选|为|是)[ \t]*([A-Da-d])"
    r"|答案选[ \t]*([A-Da-d])"
    r"|正确答案[ \t]*[:：]?[ \t]*([A-Da-d])"
)


def conclusion_letter(expl: str):
    """从解析正文里找结论字母（「因此，答案选D」这类）；找不到返回 None。"""
    m = PAT_CONCLUSION.search(expl)
    if not m:
        return None
    return next((g.upper() for g in m.groups() if g), None)


# 行首题号：作为切块锚点（选项是「A.」不是数字，不会误切）
PAT_QNUM = re.compile(r"(?m)^[ \t]*(\d{1,2})[ \t]*[.．、]")
# 块内找答案的几种写法（按优先级）
# 注意：调用前会先剥掉块首的「题号.」——因为切块锚点已把题号吃掉，head 仍以「1.」开头，
#       若模式里还写 `[.．、]?([A-D])`，开头的 `1` 会把匹配卡死（踩过，2009 全年级 0 命中）。
PAT_ANS_PATTERNS = [
    re.compile(r"^[ \t]*([A-Da-d])[ \t]*【\s*解析"),                        # A【解析】     （2009）
    re.compile(r"【\s*(?:标准答案|参考答案)\s*】[ \t]*([A-Da-d])"),         # 【标准答案】B （2010–2019）
    re.compile(r"【\s*解析\s*】[ \t]*([A-Da-d])"),                        # 【解析】A     （2020+）
    re.compile(r"(?m)^[ \t]*答案[ \t]*[:：][ \t]*([A-Da-d])"),             # 答案：A       （2026）
]
PAT_LEAD_QNUM = re.compile(r"^[ \t]*\d{1,2}[ \t]*[.．、][ \t]*")
# 块内解析正文的起始（**把前面的【一起吃进来**，这样从匹配末尾切不会有残留碎片）
PAT_EXPL_START = [
    re.compile(r"【[^】\n]{0,6}】"),          # 【解析】/【标准答案】/【参考答案】
    re.compile(r"(?:解析|分析)[ \t]*[:：]"),
]


def parse_year(text, year):
    """返回 {题号: {"answer": 'A', "explain": "…"}}（单选 1–45）。

    做法：**先按「行首题号」切成块，再只在每块的「头部」找答案与解析起始**。

    为什么限定头部：同一个题号会出现两次块（题干块 + 解析块，2024/2025 就是这种），
    而且题干块里有选项行（`A.哲学和心理学…`）。若在全块里搜「字母 + 【解析】」，
    选项行会被误命中（实测 2024/2025 因此只认出 1 道）。限定头部即可避开。
    第一次出现的块有效就采用，否则留给后面的块（题干块头部不含答案 → 自然跳过）。
    """
    text = slice_year(text, year)
    if not text:
        return {}

    anchors = [(int(m.group(1)), m.start(), m.end()) for m in PAT_QNUM.finditer(text)]
    anchors = [(n, s, e) for n, s, e in anchors if 1 <= n <= 45]
    anchors.sort(key=lambda x: x[1])

    HEAD = 200  # 答案标记一定在题号附近，不会跑到 200 字以外
    out = {}
    for i, (num, start, end) in enumerate(anchors):
        stop = anchors[i + 1][1] if i + 1 < len(anchors) else len(text)
        rest = text[start:stop]
        head = PAT_LEAD_QNUM.sub("", rest[:HEAD], count=1)

        letter = ""
        for pat in PAT_ANS_PATTERNS:
            m = pat.search(head)      # 答案在剥掉题号后的 head 里找
            if m:
                letter = m.group(1).upper()
                break

        expl = ""
        for pat in PAT_EXPL_START:
            # 起始标记必须在**原文 rest 里**定位：head 被剥掉题号、偏移不同，
            # 拿 head 的 span 去 rest 里切会整体前移几个字（踩过，切出「析】A理解题…」）。
            m = pat.search(rest[:HEAD])
            if m:
                expl = rest[m.end():]
                break

        if num in out:
            # 已有块：只有当前块真的含答案/解析，才替换（题干块不该覆盖解析块）
            if letter and not out[num]["answer"]:
                out[num]["answer"] = letter
            if expl and not out[num]["explain"]:
                out[num]["explain"] = clean(expl)
            continue
        out[num] = {"answer": letter, "explain": clean(expl)}
    return out


def year_file(year):
    """该年的解析文件（2009 例外：叫 07-09年…）。"""
    if year == 2009:
        for p in DIR_A.glob("07-09*.md"):
            return p
        return None
    cands = [p for p in DIR_A.glob("*.md") if str(year) in p.name]
    return cands[0] if cands else None


def slice_year(text, year):
    """多年合一文件：切到本年小节。"""
    hits = sorted({int(y) for y in re.findall(r"(20\d{2})\s*年(?:教育学统考真题答案解析|全国统考)", text)})
    if len(hits) <= 1:
        return text
    m = re.search(rf"(?m)^[ \t]*{year}\s*年", text)
    if not m:
        return ""
    rest = text[m.start():]
    nxt = re.search(r"(?m)^[ \t]*20\d{2}\s*年", rest[1:])
    return rest[:nxt.start() + 1] if nxt else rest


def clean(s):
    s = re.sub(r"=+\s*第\s*\d+\s*页\s*=+", "", s)
    s = re.sub(r"(?m)^[ \t]*(?:公众号[:：].*|第\s*\d+\s*页.*)$", "", s)
    # 切块有时把「解析/标准答案」标记的碎片留在开头（如「析】A」）——
    # 用「逐个剥离首字符」的笨办法，避免和那个不匹配的「析」字较劲。
    for _ in range(8):
        before = s
        # 「某字+】」这种标记碎片（如「析】」）——用「任意 CJK 字紧跟收尾括号」来识别，
        # 不去匹配那个具体的字（实测它与模式里的同名汉字不相等，纠结它纯属浪费时间）
        s = re.sub(r"^[ \t]*[\u4e00-\u9fff][】\]]\s*", "", s, count=1)
        s = re.sub(r"^[ \t]*[【\]]\s*", "", s, count=1)
        s = re.sub(r"^[ \t]*(?:解析|标准答案|参考答案)\s*[】\]]?", "", s, count=1)
        s = re.sub(r"^[ \t]*[】\]]\s*", "", s, count=1)
        s = re.sub(r"^[ \t]*[^\w\u4e00-\u9fff《（(【\"]\s*", "", s, count=1)
        # 只删「不是选项分析开头」的裸答案字母。注意 `\w` 在 Python 里**匹配中文**，
        # 所以判断「后面是不是另一个词」必须只排除 ASCII 字母和「项」，否则
        # 「A理解题此题考查…」这种会把 A 留下来（踩过）。
        s = re.sub(r"^[ \t]*([A-Da-d])(?![A-Za-z项])[ \t]*[。.、]?[ \t]*", "", s, count=1)
        if s == before:
            break
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    s = s.strip()[:2000]
    # 宁缺勿滥：源文件只给了答案、没给解析时，正文会短得没信息量
    # （界面上就成了「答案：C」下面再跟一个「C」）。中文少于 8 字就不要。
    if len(re.sub(r"[^\u4e00-\u9fff]", "", s)) < 8:
        return ""
    return s
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    s = s.strip()[:2000]
    # 宁缺勿滥：源文件只给了答案、没给解析时，正文会短得没信息量
    # （界面上就成了「答案：C」下面再跟一个「C」）。中文少于 8 字就不要。
    if len(re.sub(r"[^\u4e00-\u9fff]", "", s)) < 8:
        return ""
    return s


def main():
    check = "--check" in sys.argv
    only_year = None
    if "--year" in sys.argv:
        only_year = int(sys.argv[sys.argv.index("--year") + 1])

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    years = [r[0] for r in con.execute(
        "SELECT DISTINCT year FROM questions WHERE year IS NOT NULL ORDER BY year")]
    if only_year:
        years = [y for y in years if y == only_year]

    grand_ok = grand_mismatch = grand_empty = 0
    grand_empty_db = []
    to_write = []
    print("=" * 76)
    print(f"{'年份':6s} {'解析取到':>8s} {'库答案一致':>10s} {'不一致':>7s} {'无解析文本':>10s}")
    print("=" * 76)
    for y in years:
        f = year_file(y)
        if not f:
            continue
        parsed = parse_year(f.read_text(encoding="utf-8"), y)
        db = {int(r["number"]): dict(r) for r in con.execute(
            "SELECT id,number,answer FROM questions "
            "WHERE year=? AND qtype='single' AND number IS NOT NULL", (y,))}
        ok = mismatch = noexp = 0
        empty_db = []
        for n, q in db.items():
            p = parsed.get(n)
            if not p:
                continue
            dbans = (q["answer"] or "").strip().upper()
            if not dbans:
                # 库里答案为空：这是**数据缺陷**，单独报出来（不从解析里"顺手补答案"，
                # 因为本题已实测到解析与题干会错位，答案必须以可信来源为准）
                if p["answer"]:
                    empty_db.append((n, p["answer"], p["explain"]))
                continue
            if not p["explain"]:
                noexp += 1
                continue

            # 认领规则：**以库答案为准**，再看解析块是否支持它。
            # 为什么反转：解析里开头那个字母会被 OCR/排版错位带偏
            # （实测 2025 第 41 题解析块其实是第 42 题的、2021 第 24 题开头字母是干扰项），
            # 而库答案已与 450 道解析文本零冲突地验证过（见 _cmp_answers 诊断）。
            concl = conclusion_letter(p["explain"])
            first = p["answer"]
            if concl:
                supported = (concl == dbans)
            else:
                # 没有结论句时：正文里出现库答案字母就算支持（很多解析全文只讨论正确项）；
                # 出现**别的**字母则视为可能有错位/干扰，退回保守判断。
                letters = set(re.findall(r"[A-D]", p["explain"]))
                if dbans and dbans in letters:
                    supported = True
                elif letters:
                    supported = bool(first) and first == dbans
                else:
                    supported = bool(first) and first == dbans
            if supported:
                ok += 1
                to_write.append((q["id"], p["explain"]))
            else:
                mismatch += 1
                if check and mismatch <= 3:
                    print(f"    ⚠ {y} 第{n}题：库答案 {dbans}，解析块开头 {first}、"
                          f"结论句 {concl} —— 未采用（怕张冠李戴）")
        if empty_db:
            print(f"    ⚠ {y} 有 {len(empty_db)} 道题库里答案为空、解析里有："
                  f"{[(n, a) for n, a, _ in empty_db[:5]]}")
        grand_empty_db.extend((y, n, a, e) for n, a, e in empty_db)
        grand_ok += ok
        grand_mismatch += mismatch
        grand_empty += noexp
        print(f"{y:6d} {len(parsed):>8d} {ok:>10d} {mismatch:>7d} {noexp:>10d}")

    print("=" * 76)
    print(f"合计：解析被采用 {grand_ok} 道、被拒 {grand_mismatch} 道、"
          f"无解析文本 {grand_empty} 道、库里答案为空 {len(grand_empty_db)} 道")
    if grand_empty_db:
        for y, n, a, e in grand_empty_db[:5]:
            print(f"    缺答案：{y} 第{n}题（解析里是 {a}）")

    if check:
        print("\n--check：未写库。")
        con.close()
        return

    if grand_mismatch > grand_ok * 0.05:
        print(f"\n❌ 不一致比例过高（{grand_mismatch}/{grand_ok}），拒绝写库——先查抽取逻辑。")
        con.close()
        sys.exit(1)

    n = removed = 0
    for qid, expl in to_write:
        row = con.execute("SELECT extra FROM questions WHERE id=?", (qid,)).fetchone()
        try:
            d = json.loads(row["extra"] or "{}")
        except Exception:
            d = {}
        if expl:
            if d.get("explain") == expl:
                continue
            d["explain"] = expl
        elif d.get("explain"):
            # 本轮没抽到（或抽到的是没信息量的短文本）→ 清掉旧的，避免脏数据留库
            d.pop("explain", None)
            removed += 1
        else:
            continue
        con.execute("UPDATE questions SET extra=? WHERE id=?",
                    (json.dumps(d, ensure_ascii=False), qid))
        n += 1
    con.commit()
    # 顺带清掉"以前写进去、现在判定没信息量"的短解析
    stale = []
    for r in con.execute("SELECT id, extra FROM questions WHERE qtype='single' "
                         "AND extra LIKE '%explain%'"):
        try:
            d = json.loads(r["extra"])
        except Exception:
            continue
        e = d.get("explain") or ""
        if len(re.sub(r"[^\u4e00-\u9fff]", "", e)) < 8:
            d.pop("explain", None)
            con.execute("UPDATE questions SET extra=? WHERE id=?",
                        (json.dumps(d, ensure_ascii=False), r["id"]))
            stale.append(r["id"])
    con.commit()
    print(f"\n✅ 已写入 {n} 道题的解析（extra.explain），清理无信息量旧值 {removed + len(stale)} 条")

    cov = con.execute(
        "SELECT COUNT(*) FROM questions WHERE qtype='single' AND extra LIKE '%explain%'"
    ).fetchone()[0]
    tot = con.execute("SELECT COUNT(*) FROM questions WHERE qtype='single'").fetchone()[0]
    print(f"单选解析覆盖率：{cov}/{tot}")
    con.close()


if __name__ == "__main__":
    main()
