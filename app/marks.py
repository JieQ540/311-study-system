# -*- coding: utf-8 -*-
"""分值形态与选做题归组的**唯一实现**（`server.py` 与 `split_points.py` 共用）。

## 为什么值得单独一个模块

这些判断原本在 `server.py` 里有一份（`_opt_of_section` / `_is_elective` / `_roman_key`），
在 `split_points.py` 里**又有另一份**；而「这道题的分值形态是否自洽」只在
`split_points.py` 里有（`_question_shape_ok`）。两处实现必然走偏 —— 项目已经因为
「四个页面各写一份渲染」「三个入口各写一份 INSERT」吃过两次**静默**错误。

这里的后果更直接：页面判「分值待核」与数据管线判「形态不合法」必须是同一套逻辑，
否则会出现「页面显示分值齐全、`split_points.py --report` 说这题不合法」这种自相矛盾，
而用户只会看到两个互相打架的数字，还找不到谁对。

## 边界

**纯函数**：不连库、不读配置、不 import ai。所以 server、数据管线脚本、
测试都能随意复用，也不会引入导入副作用。

## 两层的区别（别混）

| 函数 | 回答的问题 | 允许「有的点还没分值」吗 |
| --- | --- | --- |
| `shape_ok()` | 已写的分值**自洽**吗？ | 允许（数据管线体检用） |
| `fold_ready()` | 现在能**折算成分数**吗？ | 不允许（页面与统计用） |

分两层是因为 `split_points.py --repair` 靠 `shape_ok()` 决定要不要把分值清零重做：
它要抓的是**写错的**分值。若把「有点还没打分」也算进 `shape_ok()`，一道只是**没补完**
的题会被判定为错，`repair` 就会把已经算对的分值清掉 —— 那是破坏性行为。
"""
import re

__all__ = [
    "roman_key", "opt_of_section", "is_elective",
    "any_n_ok", "any_n_rule", "free_hit_n",
    "shape_ok", "fold_ready",
]

#: 「答出其中任意三点即可」「答出 3 点即可」「任意 3 点即可」→ 3
PAT_ANY_N = re.compile(r"任意\s*([1-9１-９])\s*[点项个]|([1-9１-９])\s*[点项个]\s*即可")
#: 「每点 3 分」「每个要点 5 分」「每小点 2–3 分」（取区间下限）
PAT_PER_POINT = re.compile(r"每\s*[点项个条]\s*(?:满分)?\s*(\d+(?:\.\d+)?)\s*分")

#: 选项罗马数字的规范写法。真题里 ASCII（`I(1)`）与 Unicode（`Ⅰ(1)`）混着出现，
#: 而 `attempts.selected_option` 只存 Unicode —— 不做归一，ASCII 分支永远匹配不上。
_ROMAN_CANON = {"Ⅰ": "Ⅰ", "Ⅱ": "Ⅱ", "I": "Ⅰ", "II": "Ⅱ",
                "i": "Ⅰ", "ii": "Ⅱ", "1": "Ⅰ", "2": "Ⅱ"}


def roman_key(section_key):
    """从 `Ⅰ(1)` / `Ⅱ(3)` 这类标签里取出选项罗马数字；取不到返回 None。

    只认**带编号**的形态（`Ⅰ(2)`）。光秃秃的 `Ⅰ` 认不出来 —— 那种要靠
    `opt_of_section()` 的 label 回退，两者分工不同。

    **返回值一律是 Unicode 的 `Ⅰ` / `Ⅱ`**：真题里 ASCII 的 `I(1)` / `II(1)` 与
    Unicode 的 `Ⅰ(1)` 混着出现，而 `attempts.selected_option` 存的是 Unicode。
    不归一的话，ASCII 写法的分支永远匹配不上学生的选择（复审发现）。
    """
    m = re.match(r"\s*(Ⅰ|Ⅱ|I|II|1|2)\s*[（(]", str(section_key or ""))
    return _canon_roman(m.group(1)) if m else None


def _canon_roman(value):
    """把 `I` / `II` / `1` / `2` 与 Unicode 罗马数字统一成 `Ⅰ` / `Ⅱ`；认不出返回 None。"""
    return _ROMAN_CANON.get(str(value or "").strip())


def opt_of_section(sec_key, label=None):
    """把一个小问归到它所属的**选项**（Ⅰ / Ⅱ）上；返回值一律是 Unicode `Ⅰ` / `Ⅱ`。

    key 有两种形态都算数：带编号的 `Ⅰ(2)`，以及**光秃秃的 `Ⅰ`**
    （实测 2010 第 56 题：该选项只有一个 30 分的小问，模型就没编号）。
    key 里认不出来时退回看 label。**不做这层回退，那道题的 Ⅰ 选项会被算成 0 分**。

    同样要归一 ASCII 写法：真题里有 `I(1)` / `II(1)`，而存进 `selected_option`
    的是 `Ⅰ` / `Ⅱ`（见 `roman_key` 的说明）。
    """
    for txt in (sec_key, label):
        s = str(txt or "")
        m = re.match(r"\s*(Ⅰ|Ⅱ|I|II)\s*[（(]", s) or re.match(r"\s*(Ⅰ|Ⅱ|I|II)\s*$", s)
        if m:
            return _canon_roman(m.group(1))
    return None


def is_elective(stem):
    """是不是「Ⅰ/Ⅱ 任选一题」的 30 分选做题。

    这种题**两个选项各自满分 30 分**（官方原文：「任选一题作答，两题都答只按第 Ⅰ 题计分」），
    所以 Ⅰ 的要点之和 + Ⅱ 的要点之和 = 60，**这是对的，不是数据错误**。
    求和、判「分值相符」都必须按选项分别来，不能把两支加在一起。
    """
    return bool(re.search(r"任选|只按", stem or ""))


def any_n_ok(vals, want, tol=1e-6):
    """判断一组要点分值是否属于「答出任意 N 点即可满分」的**合法**形态。

    只有两种合法形态：

      A. **和刚好等于满分**（最常见的官方写法：3 个点各 5 分 = 15）。
      B. **所有点等值，且满分能被它整除** —— 参考答案给的可接受要点比需要的多，
         `want ÷ 每点分 = N` 就是"答够 N 点即满分"。
         实测：2009 第 53 题满分 15、官方「答出以上任意三种即可」、参考答案给了 4 种
         → 4 个点各 5 分、合计 20，这是**对的**。

    **为什么不能用「最高的 N 点加起来 ≥ 满分」这种宽松判据**（我犯过）：
    那样 `[2.5] × 16`（合计 40）对满分 20 也算"合法"（前 8 个正好 20），
    于是 2015 第 56 题被放行成 Ⅱ 合计 50 分的荒谬结果。
    等值 + 整除这两条同时成立时，「前 N 个之和」必然**正好等于** want，不多不少。
    """
    if not vals:
        return False
    if abs(sum(vals) - want) <= 0.01:                       # A
        return True
    uniq = set(round(v, 4) for v in vals)
    if len(uniq) == 1:                                      # B
        per = round(vals[0], 4)
        if per <= 0:
            return False
        n_need = want / per
        if abs(n_need - round(n_need)) > tol:
            return False
        # **数量上限（按实测定，留 1 个余量）**：可接受要点最多比需要的多 6 个。
        # 实测官方"多给"是 +1（4 个备选对 3 个必需）到 +5（15 个对 10 个），
        # 取 +6 不会误伤真数据；而"声明 20 分却铺 16×2.5=40"（多给 +8）、
        # "铺 20×2.5"（多给 +12）这类**把一个要点摊碎**的错法正好被挡住 ——
        # 摊碎会让学生随便写点边角料就凑满，等于毁掉判分松紧。
        # 「多给偏多」的情况另有 split_points.audit() 提醒人抽查，这里只挡明显不合理的。
        return len(vals) <= round(n_need) + 6
    return False


def any_n_rule(sections, note):
    """从评分说明里读出「答出任意 N 点即可满分」→ {小问 key: N}；读不到给 {}。

    注意：`any_n_ok()` 还有一条**不依赖说明文本**的判定（等值且可整除），
    因为实测模型给的 note 有时不写这一句，但分值形态已经说明它是这种题。
    """
    txt = note or ""
    out = {}
    mn = PAT_ANY_N.search(txt)
    if not mn:
        return out
    n = int(mn.group(1) or mn.group(2))
    if n <= 0:
        return out
    for s in sections or []:
        out[str(s.get("key"))] = n
    return out


def free_hit_source(note):
    """「答出任意 N 点即可」这个结论是**从哪来的** → `"note"` 或 `"shape"`。

    为什么要分：官方【评分说明】的原话是**外部依据**，页面可以说"官方评分说明"；
    而靠分值形态（等值且整除）推出来的只是**本系统的练习口径**，说成"官方"就是冒名。
    设计 §3 明确要求"页面不把未经外部核实的练习口径称为官方评分"。
    """
    return "note" if (note and PAT_ANY_N.search(note)) else "shape"


def free_hit_n(points, full, note=None):
    """「答出任意 N 点即可满分」的 N；判断不出来返回 None。

    先看官方【评分说明】原话（最可靠），文本没写再靠分值形态推（等值且能整除）。
    只在 `0 < N < 点数` 时才认 —— 等号那一边就是普通的「点分值之和 == 满分」，
    不是这种形态，硬报一个 N 会让页面多出一句凭空的解释。
    """
    if note:
        m = PAT_ANY_N.search(note)
        if m:
            n = int(m.group(1) or m.group(2))
            if n > 0:
                return n
    vals = [p["score"] for p in (points or [])
            if isinstance(p.get("score"), (int, float)) and not isinstance(p.get("score"), bool)]
    if not vals or not isinstance(full, (int, float)) or isinstance(full, bool) or full <= 0:
        return None
    uniq = set(round(v, 4) for v in vals)
    if len(uniq) == 1 and vals[0] > 0:
        n = full / vals[0]
        if abs(n - round(n)) <= 1e-6 and 0 < round(n) < len(vals):
            return int(round(n))
    return None


def shape_ok(points, full, stem=None, sections=None):
    """从**已写的分值**判断一道题的分值形态是否合法。返回 `(是否合法, 说明文字)`。

    合法形态：
      ① 逐点合计 == 满分（没有小问的题走这条）；
      ② 有小问时：**每个**小问内的要点之和 == 该小问分值，且各小问分值之和 == 满分；
      ③ 「答出任意 N 点即可」—— 参考答案给的可接受要点**多于**需要的个数，
         所以**该小问内**合计**大于**该小问满分是对的（2009 第 53 题：满分 15、
         官方给了 4 种每种 5 分 = 20）。这种豁免**只对它自己的小问生效**。

    **只看已打分的点**（`score IS NOT NULL`）：这道题的判断对象是"写下的分值自不自洽"，
    还没补分的点不参与 —— 见模块文档里与 `fold_ready()` 的分工。

    ⚠️ 2026-10-08 复审修正（原实现有两处会放过假分数）：
      · 只要**整题**合计等于满分就直接通过，**不逐问检查** ——
        于是「两小问各 15，采分点分别 10 / 20」被判合法；
      · 逐问校验写成了"任意一个小问合法就 return True" ——
        于是「两小问各 10，采分点分别 10 / 15」也被判合法（15 那一问明明超了 10）。
      现在两个方向都不放过：**每个**小问各自核，超额豁免只作用于它自己那个小问。
    """
    pts = [p for p in (points or [])
           if isinstance(p.get("score"), (int, float)) and not isinstance(p.get("score"), bool)]
    if not pts:
        return False, "没有分值"
    if not isinstance(full, (int, float)) or isinstance(full, bool) or full <= 0:
        return False, "满分缺失或不是正数"

    if is_elective(stem):
        return _shape_ok_elective(pts, sections, full)

    total = round(sum(p["score"] or 0 for p in pts), 2)
    secs = [s for s in (sections or []) if str(s.get("key") or "")]

    if not secs:
        # 没有小问结构：整题一起核
        if abs(total - full) <= 0.01:
            return True, f"{total:g} = {full:g}"
        allv = sorted((p["score"] or 0) for p in pts)
        if any_n_ok(allv, full):
            return True, f"{total:g} > {full:g}（「答出任意 N 点即可」的官方形态，合法）"
        return False, f"{total:g} ≠ {full:g}"

    # ---- 有小问结构：**逐问**核，不能让全题总和或某个小问替别的问背书 ----
    problems = _check_sections(pts, secs)
    sec_sum = 0.0
    for s in secs:
        try:
            sec_sum = round(sec_sum + float(s.get("score") or 0), 2)
        except (TypeError, ValueError):
            pass
    orph = _orphan_scores(pts, secs)
    if orph:
        problems.append(f"{len(orph)} 个采分点不属于任何小问（合计 {round(sum(orph), 2):g}）")
    if abs(sec_sum - full) > 0.01:
        problems.append(f"各小问分值之和 {sec_sum:g} ≠ 满分 {full:g}")
    if problems:
        return False, "；".join(problems)
    return True, f"逐小问相符（{len(secs)} 个小问，合计 {sec_sum:g}）"


def _check_sections(pts, sections):
    """逐小问核：每个小问内的要点之和 == 该小问分值（"任意 N 点"只豁免它自己那一问）。

    返回问题列表（空列表 = 全过）。**选做题与非选做题共用这一份判据** ——
    复审指出过：选做题单独走一条"只核整支合计"的宽松路径，于是
    「Ⅰ 支两小问各 15、采分点分别 10/20」（整支仍合计 30）会被判合法。
    """
    problems = []
    for s in (sections or []):
        k = str(s.get("key"))
        try:
            want = round(float(s.get("score") or 0), 2)
        except (TypeError, ValueError):
            problems.append(f"小问 {k} 的分值不是数字")
            continue
        vals = sorted(((p["score"] or 0) for p in pts if str(p.get("section")) == k), reverse=True)
        if not vals:
            if want > 0.01:
                problems.append(f"小问 {k} 有 {want:g} 分却没有采分点")
            continue
        if abs(sum(vals) - want) <= 0.01:
            continue
        if any_n_ok(vals, want):
            continue
        problems.append(f"小问 {k} 内要点之和 {round(sum(vals), 2):g} ≠ 该小问分值 {want:g}")
    return problems


def _orphan_scores(pts, sections):
    """不属于任何小问、却带着分值的点（分值自洽就要求它们有归属）。"""
    keys = {str(s.get("key")) for s in (sections or [])}
    return [p["score"] or 0 for p in pts if str(p.get("section")) not in keys]


def _shape_ok_elective(pts, sections, full):
    """选做题：**每支的每个小问**都要核，且每支小问合计要等于题目声明的满分。

    为什么不能只核整支合计（复审反例）：Ⅰ 支两小问各 15 分、采分点分别 10/20，
    整支仍然合计 30 —— 只核整支就会说"合法"，而这支内部明明错了两处；
    「题目满分 30、Ⅰ 支只有 20 分」同理会被放过。两处都会让页面徽标
    （`score_stats`）与 `split_points.py --report/--repair` 与作答计分互相矛盾。
    """
    opts = {}
    for s in (sections or []):
        k = opt_of_section(s.get("key"), s.get("label"))
        if k:
            opts.setdefault(k, []).append(s)
    if not opts:
        return False, "选做题没有可识别的 Ⅰ / Ⅱ 小问"
    problems, detail = [], {}
    for k in sorted(opts):
        secs = opts[k]
        keys = [str(s.get("key")) for s in secs]
        bpts = [p for p in pts if str(p.get("section")) in keys]
        bsum = round(sum(float(s.get("score") or 0) for s in secs), 2)
        got = round(sum(p["score"] or 0 for p in bpts), 2)
        detail[k] = got
        # ① 支内逐小问
        problems += [f"{k} 支：{msg}" for msg in _check_sections(bpts, secs)]
        # ② 这一支的小问分值合计必须等于题目声明的满分（选做题每题 30）
        if abs(bsum - float(full)) > 0.01:
            problems.append(f"{k} 支的小问分值之和 {bsum:g} ≠ 题目满分 {float(full):g}")
        # ③ 整支只按"任意 N 点"形态解释时，也必须是这一支自己成立
        elif not bpts:
            problems.append(f"{k} 支没有采分点")
    orph = _orphan_scores(pts, sections)
    if orph:
        problems.append(f"{len(orph)} 个采分点不属于任何选项（合计 {round(sum(orph), 2):g}）")
    return (not problems), ("选做题各选项 " + str(detail) if not problems else "；".join(problems))


def fold_ready(points, full, stem=None, sections=None):
    """现在能不能把命中**折算成分数**？返回 `(可否, 原因)`。

    比 `shape_ok()` 严一格，因为要让「总分」站得住：
      · 每个采分点都得有分值，且**为正数**（0 分或负分的点没有意义，折出来是脏数字）；
      · 题目满分必须存在且为正数 —— **不许回退成「题型一律 15 分」**，
        那会把 30 分论述题算成 15 分（本系统真踩过：报告里 30 分题按 15 分算）；
      · 形态还得自洽（`shape_ok()`）。

    任何一条不过，调用方都应把分数记成 `None`（页面显示「分值待核」）并说明原因，
    **而不是记 0**：0 分是"作答了但一个点都没答到"，与"算不出来"是两件事。
    """
    pts = list(points or [])
    if not pts:
        return False, "这道题没有采分点"
    if not isinstance(full, (int, float)) or isinstance(full, bool) or full <= 0:
        return False, "题目满分缺失或不是正数"
    missing = [p.get("seq") for p in pts
               if not isinstance(p.get("score"), (int, float)) or isinstance(p.get("score"), bool)]
    if missing:
        return False, f"{len(missing)} 个采分点没有分值"
    nonpos = [p.get("seq") for p in pts if p["score"] <= 0]
    if nonpos:
        return False, f"{len(nonpos)} 个采分点分值不是正数"
    ok, why = shape_ok(pts, full, stem, sections)
    if not ok:
        return False, f"分值形态与满分不符（{why}）"
    return True, "分值齐全"
