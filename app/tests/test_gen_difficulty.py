# -*- coding: utf-8 -*-
"""AI 出题「难度档 + 思考模式」回归测试（**全部用桩，不调 AI、不花钱**）。

覆盖：
  ① 五档难度渲染进提示词（档位号/描述都在，且占位符不残留）
  ② normalize_difficulty 的边界收敛
  ③ gen_limits：开思考必须放宽 max_tokens 与超时（否则 JSON 会被截断）
  ④ chat 的 thinking 开关：关→发 thinking=disabled；开→**不发**该字段（模型默认就是思考）
  ⑤ audit_ai_set 四条新校验的正反样例：
     难度越档 / 难度均值偏离 / 正确项明显过长 → 判不合格（触发重出）
     疑似缺情境 → **只报不拦**（不进 hard_fail）
  ⑥ generate_ai_set：难度档进了 system prompt；不合格会带着问题重出一轮
  ⑦ add_usage 统计推理 token
  ⑧ 接口层：/api/daily/gen-options 有 5 档；ai-set 把 difficulty/thinking 透传下去并回显

跑法：
    cd app
    python tests/test_gen_difficulty.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

_LIVE_DB = APP.parent / "data" / "kaoyan.db"
_SEED_DB = APP.parent / "data" / "kaoyan-seed.db"
REAL_DB = _LIVE_DB if _LIVE_DB.exists() else _SEED_DB

TMP = Path(tempfile.mkdtemp(prefix="kaoyan_gen_"))
TEST_DB = TMP / "kaoyan.db"
shutil.copy2(REAL_DB, TEST_DB)
BEFORE = (REAL_DB.stat().st_size, REAL_DB.stat().st_mtime_ns)
os.environ["DSH_DB"] = str(TEST_DB)

import ai      # noqa: E402
import server  # noqa: E402

assert server.DB == TEST_DB, f"server 没连到副本库：{server.DB}"

FAILED = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILED.append(name)


def opt(text):
    return text


def make_single(i, difficulty=3, long_correct=False, scene=True, stem_len=40):
    base = "在某中学的课堂上，教师组织学生讨论" if scene else "教育目的"
    pad = "并结合教材中的案例展开分析" if stem_len > 25 else ""
    correct = ("这一做法体现了让学生在做中学、通过实际活动获得直接经验的教育主张，"
               "与杜威的经验主义教育观一致" if long_correct else "活动课程")
    wrong = ["学科课程", "隐性课程", "综合课程"]
    # 正确答案轮换 A/B/C/D：全塞在同一个字母会被「答案集中」合理判负（那是真的问题）
    letter = "ABCD"[(i - 1) % 4]
    opts, wi = {}, 0
    for k in "ABCD":
        if k == letter:
            opts[k] = correct
        else:
            opts[k] = wrong[wi]
            wi += 1
    return {"outline_id": 304, "stem": f"{base}第{i}个问题，学生随后完成了小组作业{pad}。",
            "options": opts, "answer": letter,
            "explain": "正确项说的是以活动为中心；把课程等同于科目划分的那一项错在窄化概念。",
            "cognitive": "应用", "difficulty": difficulty}


def make_paper(n=4, difficulty=3, long_correct=False, scene=True, stem_len=40):
    return {"single": [make_single(i, difficulty, long_correct, scene, stem_len)
                       for i in range(1, n + 1)],
            "subjective": [
                {"outline_id": 304, "qtype": "analysis", "full_score": 15,
                 "stem": "有观点认为「只要尊重儿童兴趣，教育就不需要任何引导」。请判断。",
                 "points": [{"seq": 1, "claim": "该说法错误：尊重兴趣不等于放弃引导", "evidence": "…"},
                            {"seq": 2, "claim": "兴趣与努力相互促进", "evidence": "…"},
                            {"seq": 3, "claim": "教师应发挥主导作用", "evidence": "…"}],
                 "difficulty": difficulty},
                {"outline_id": 304, "qtype": "short", "full_score": 15,
                 "stem": "简述活动课程与学科课程的关系。",
                 "points": [{"seq": 1, "claim": "二者互补", "evidence": "…"},
                            {"seq": 2, "claim": "各有优势", "evidence": "…"},
                            {"seq": 3, "claim": "应结合使用", "evidence": "…"}],
                 "difficulty": difficulty},
                {"outline_id": 304, "qtype": "essay", "full_score": 30,
                 "stem": "阅读下列材料，按要求回答问题。\n材料：……\n（1）……（2）……",
                 "points": [{"seq": 1, "claim": "要点一", "evidence": "…"},
                            {"seq": 2, "claim": "要点二", "evidence": "…"},
                            {"seq": 3, "claim": "要点三", "evidence": "…"}],
                 "difficulty": difficulty},
            ]}


print("=" * 68)
print("① 五档难度渲染进提示词")
print("=" * 68)
for lv in range(1, 6):
    block = ai.difficulty_block(lv)
    name = ai.DIFFICULTY_LEVELS[lv]["name"]
    check(f"档 {lv} 的块含档位号与名称", f"{lv} / 5" in block and name in block, name)
    for const in (ai.GEN_SYS, ai.AI_SET_SINGLE_SYS, ai.AI_SET_SUBJ_SYS):
        filled = const.replace("@@DIFFICULTY@@", block)
        cond = "@@DIFFICULTY@@" not in filled and name in filled
        if not cond:
            check(f"档 {lv} 占位符替换干净", False, "有残留或描述没进去")
            break
else:
    check("三个出题 system prompt 的难度占位符都能替换干净", True)
check("三个 prompt 都保留了占位符（否则难度根本没进提示词）",
      all("@@DIFFICULTY@@" in c for c in
          (ai.GEN_SYS, ai.AI_SET_SINGLE_SYS, ai.AI_SET_SUBJ_SYS)))
check("提示词里写了「正确项不得明显更长」这条硬规则",
      "正确项不得明显比其他选项长" in ai.GEN_SYS
      and "正确项不得明显比其他选项长" in ai.AI_SET_SINGLE_SYS)
check("提示词里写了「题干必须有情境」这条硬规则",
      "题干必须有情境" in ai.GEN_SYS and "情境化（任何档位都必须做到）" in ai.AI_SET_SINGLE_SYS)
# 这条是按 2026-09-17 的真实生成结果补的：模型会把识记题套上情境外壳（"某教师受 X 启发…问体现了 X 的什么思想"）
check("提示词里有「情境外壳测试」这条自查规则（按实测补的）",
      "情境外壳测试" in ai.GEN_SYS and "情境外壳测试" in ai.AI_SET_SINGLE_SYS)
check("提示词里点名了实测出现的那两种换壳写法",
      "受到裴斯泰洛齐的启发" in ai.GEN_SYS and "不预设行为目标" in ai.AI_SET_SINGLE_SYS)

print()
print("=" * 68)
print("② normalize_difficulty 边界")
print("=" * 68)
check("0 → 1", ai.normalize_difficulty(0) == 1)
check("9 → 5", ai.normalize_difficulty(9) == 5)
check("'4' → 4（字符串也认）", ai.normalize_difficulty("4") == 4)
check("None → 默认档", ai.normalize_difficulty(None) == ai.DEFAULT_DIFFICULTY)
check("乱码 → 默认档", ai.normalize_difficulty("难") == ai.DEFAULT_DIFFICULTY)

print()
print("=" * 68)
print("③ gen_limits：开思考必须放宽 max_tokens 与超时")
print("=" * 68)
on, off = ai.gen_limits(True), ai.gen_limits(False)
check("开思考的单选 max_tokens 更大", on["single"] > off["single"],
      f'{on["single"]} vs {off["single"]}')
check("开思考的主观题 max_tokens 更大", on["subj"] > off["subj"], f'{on["subj"]} vs {off["subj"]}')
check("开思考的超时更长", on["timeout"] > off["timeout"], f'{on["timeout"]} vs {off["timeout"]}')

print()
print("=" * 68)
print("④ chat 的 thinking 开关（看真实发出的 payload）")
print("=" * 68)
CAPTURED = []
_real_urlopen = ai.urllib.request.urlopen


class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    CAPTURED.append({"payload": json.loads(req.data.decode()), "timeout": timeout})
    return FakeResp({"choices": [{"message": {"content": "{}"}}], "usage": {}})


ai.urllib.request.urlopen = fake_urlopen
# 公开副本里没有 config.json（刚 clone、还没配 key）—— chat() 要读 api 段，
# 这里直接注入一份**桩配置**，让"请求长什么样"这件事在任何环境下都能验。
# _CFG 是模块级缓存，cfg() 见到非 None 就不再读文件。
_orig_cfg = ai._CFG
ai._CFG = {"api": {"model": "stub-model", "base_url": "http://127.0.0.1:1",
                   "api_key": "sk-stub", "temperature": 0.3}}
try:
    ai.chat([{"role": "user", "content": "x"}], thinking=False)
    ai.chat([{"role": "user", "content": "x"}], thinking=True, timeout=600)
finally:
    ai.urllib.request.urlopen = _real_urlopen
    ai._CFG = _orig_cfg
check("关思考 → payload 带 thinking=disabled",
      CAPTURED[0]["payload"].get("thinking") == {"type": "disabled"}, str(CAPTURED[0]["payload"].get("thinking")))
check("开思考 → **不发** thinking 字段（模型默认就是思考）",
      "thinking" not in CAPTURED[1]["payload"], str(CAPTURED[1]["payload"].get("thinking")))
check("timeout 能透传（600 秒）", CAPTURED[1]["timeout"] == 600, str(CAPTURED[1]["timeout"]))

print()
print("=" * 68)
print("⑤ audit_ai_set 三条新校验（正反样例）")
print("=" * 68)
ok_paper = make_paper(4, difficulty=3)
a = ai.audit_ai_set(ok_paper, difficulty=3)
types = [i["type"] for i in a["issues"]]
check("合规样卷：难度相关校验不报问题",
      not any(t in ("难度越档", "难度均值偏离", "正确项明显过长") for t in types), str(types))
check("合规样卷：不判不合格", a["hard_fail"] is False, str(types))
check("难度分布已回传", a["difficulty_dist"] == {3: 4}, str(a["difficulty_dist"]))
check("难度均值已回传", a["difficulty_mean"] == 3.0, str(a["difficulty_mean"]))

bad_diff = make_paper(4, difficulty=5)
a2 = ai.audit_ai_set(bad_diff, difficulty=1)
check("难度越档被抓出", "难度越档" in [i["type"] for i in a2["issues"]], str([i["type"] for i in a2["issues"]]))
check("难度越档 → 判不合格（触发重出）", a2["hard_fail"] is True)

mean_off = make_paper(4, difficulty=2)          # 目标 3，均值 2.0 → 偏 1.0
a3 = ai.audit_ai_set(mean_off, difficulty=3)
check("难度均值偏离被抓出", "难度均值偏离" in [i["type"] for i in a3["issues"]],
      str([i["type"] for i in a3["issues"]]))
check("均值偏离 → 判不合格", a3["hard_fail"] is True, str(a3["difficulty_mean"]))

long_paper = make_paper(4, difficulty=3, long_correct=True)
a4 = ai.audit_ai_set(long_paper, difficulty=3)
check("正确项明显过长被抓出", "正确项明显过长" in [i["type"] for i in a4["issues"]],
      str([i["type"] for i in a4["issues"]]))
check("正确项过长 → 判不合格", a4["hard_fail"] is True)

no_scene = make_paper(4, difficulty=3, scene=False, stem_len=10)
a5 = ai.audit_ai_set(no_scene, difficulty=3)
check("疑似缺情境会被报告", "疑似缺情境" in [i["type"] for i in a5["issues"]],
      str([i["type"] for i in a5["issues"]]))
check("疑似缺情境**只报不拦**（不触发重出）", a5["hard_fail"] is False,
      "这是刻意的：机器判断有没有情境不可靠，不能靠它把题反复打回")
check("不传 difficulty 就不做难度校验",
      not any(i["type"].startswith("难度") for i in ai.audit_ai_set(make_paper(4), None)["issues"]))

print()
print("=" * 68)
print("⑥ generate_ai_set：难度进 prompt + 不合格重出")
print("=" * 68)
SEEN = {"sys": [], "user": [], "n": 0}


def fake_chat(messages, **kw):
    sys_txt = messages[0]["content"]
    user_txt = messages[1]["content"]
    SEEN["sys"].append(sys_txt)
    SEEN["user"].append(user_txt)
    SEEN["n"] += 1
    usage = {"prompt_tokens": 100, "completion_tokens": 200,
             "completion_tokens_details": {"reasoning_tokens": 150}}
    if "命题质量审核员" in sys_txt:
        return json.dumps({"pass": True, "issues": [], "must_retry": False, "comment": "ok"}), usage
    # 靠 system prompt 里声明的输出格式区分两批调用（user 文案里没有 single/subjective 字样）
    if '"single"' in sys_txt:
        # 第一轮故意出「正确项过长」的题，看会不会带着问题重出
        bad = SEEN["n"] <= 2
        return json.dumps({"single": make_paper(4, difficulty=4, long_correct=bad)["single"]}), usage
    return json.dumps({"subjective": make_paper()["subjective"]}), usage


_real_chat = ai.chat
ai.chat = fake_chat
try:
    paper, usage, rep = ai.generate_ai_set("今天背了杜威", [{"id": 304, "path": "教育学 › 课程"}],
                                           n_single=4, topics=[], difficulty=4, thinking=True)
finally:
    ai.chat = _real_chat

check("难度档进了出题 system prompt",
      any("4 / 5" in s for s in SEEN["sys"]), f"{len(SEEN['sys'])} 次调用")
check("第一轮不合格 → 带着问题重出（共 2 轮）", rep["attempts"] == 2, f"attempts={rep['attempts']}")
check("重出反馈进了第二轮发给 AI 的 user 消息",
      any("上一轮生成被判定不合格" in u for u in SEEN["user"]),
      f"共 {len(SEEN['user'])} 条 user 消息")
check("重出反馈点名了具体问题（正确项过长）",
      any("正确项明显过长" in u for u in SEEN["user"]), "把问题清单甩回给 AI 才算有效重出")
check("最终判定为通过（第二轮已改正）", rep["final_ok"] is True,
      str([i["type"] for i in rep["program_audit"]["issues"]]))
check("报告回显难度档与思考开关",
      rep["difficulty"] == 4 and rep["thinking"] is True and "偏难" in rep["difficulty_name"],
      f'{rep["difficulty"]}/{rep["difficulty_name"]}/{rep["thinking"]}')
check("报告回传难度分布", rep["program_audit"]["difficulty_dist"] == {4: 4},
      str(rep["program_audit"]["difficulty_dist"]))

print()
print("=" * 68)
print("⑥b 质检兜底：开思考把 token 吃光导致 JSON 失败时，关思考重跑一次")
print("=" * 68)
# 真实生成里出现过：质检调用 4000 max_tokens 全被推理吃掉、正文返回空串 → JSONDecodeError。
# 这里用桩复现那个失败，确认会**自动关思考重跑**并拿到报告。
FALLBACK = {"check_calls": 0, "think_flags": []}


def fake_chat2(messages, **kw):
    sys_txt = messages[0]["content"]
    usage = {"prompt_tokens": 10, "completion_tokens": 20,
             "completion_tokens_details": {"reasoning_tokens": 15}}
    if "命题质量审核员" in sys_txt:
        FALLBACK["check_calls"] += 1
        FALLBACK["think_flags"].append(kw.get("thinking"))
        if kw.get("thinking"):
            raise json.JSONDecodeError("Expecting value", "", 0)   # 复现"正文为空"
        return json.dumps({"pass": True, "issues": [], "must_retry": False,
                           "comment": "关思考重跑成功"}), usage
    if '"single"' in sys_txt:
        return json.dumps({"single": make_paper(4, difficulty=4)["single"]}), usage
    return json.dumps({"subjective": make_paper()["subjective"]}), usage


_real_chat2 = ai.chat
ai.chat = fake_chat2
try:
    _, _, rep2 = ai.generate_ai_set("今天背了杜威", [{"id": 304, "path": "教育学 › 课程"}],
                                    n_single=4, topics=[], difficulty=4, thinking=True)
finally:
    ai.chat = _real_chat2
check("质检失败后自动重跑（共调用 2 次）", FALLBACK["check_calls"] == 2, str(FALLBACK["check_calls"]))
check("第一次带思考、第二次关思考",
      FALLBACK["think_flags"] == [True, False], str(FALLBACK["think_flags"]))
check("最终拿到了质检报告（不是那句看不懂的报错）",
      (rep2.get("ai_check") or {}).get("comment") == "关思考重跑成功",
      str((rep2.get("ai_check") or {}).get("comment")))
check("报告里标明了这次是兜底跑的",
      (rep2.get("ai_check") or {}).get("fallback_no_thinking") is True)
check("质检预算在开思考时更大", ai.gen_limits(True)["check"] >= 8000,
      str(ai.gen_limits(True)["check"]))

print()
print("=" * 68)
print("⑦ add_usage 统计推理 token")
print("=" * 68)
tot = {}
ai.add_usage(tot, {"prompt_tokens": 10, "completion_tokens": 20,
                   "completion_tokens_details": {"reasoning_tokens": 7}})
ai.add_usage(tot, {"prompt_tokens": 1, "completion_tokens": 2,
                   "completion_tokens_details": {"reasoning_tokens": 3}})
check("输入/输出累加正确", tot["prompt_tokens"] == 11 and tot["completion_tokens"] == 22, str(tot))
check("推理 token 单独累加", tot["reasoning_tokens"] == 10, str(tot))
_bare = ai.add_usage({}, {"prompt_tokens": 1})
check("没有推理字段也不报错、也不会凭空造出推理数",
      _bare.get("prompt_tokens") == 1 and "reasoning_tokens" not in _bare, str(_bare))

print()
print("=" * 68)
print("⑧ 接口层：gen-options + ai-set 透传")
print("=" * 68)
opts = server.gen_options()
check("gen-options 返回 5 档", len(opts["levels"]) == 5, str(list(opts["levels"].keys())))
check("档位描述来自 ai.DIFFICULTY_LEVELS（与提示词同源）",
      opts["levels"]["4"]["name"] == ai.DIFFICULTY_LEVELS[4]["name"],
      opts["levels"]["4"]["name"])
check("默认档与默认思考开关正确",
      opts["default_difficulty"] == 3 and opts["default_thinking"] is True, str(opts["default_difficulty"]))

PASSED = {}


def fake_gen_ai_set(study_text, points, **kw):
    PASSED.update(kw)
    return make_paper(4, difficulty=kw.get("difficulty") or 3), \
        {"prompt_tokens": 5, "completion_tokens": 5, "reasoning_tokens": 4}, \
        {"attempts": 1, "history": [], "program_audit": ai.audit_ai_set(make_paper(4), 3),
         "ai_check": {"pass": True, "issues": [], "comment": "ok"}, "final_ok": True,
         "ai_flagged": False, "difficulty": kw.get("difficulty"), "thinking": kw.get("thinking")}


_real_gen = server.ai_mod.generate_ai_set
server.ai_mod.generate_ai_set = fake_gen_ai_set
try:
    con = sqlite3.connect(TEST_DB)
    con.row_factory = sqlite3.Row
    pt = con.execute("SELECT id,path FROM outline_nodes WHERE level='section' LIMIT 1").fetchone()
    con.close()
    out = server.daily_ai_set({"point_ids": [pt["id"]], "n_single": 4,
                               "difficulty": 5, "thinking": False, "dry_run": True})
finally:
    server.ai_mod.generate_ai_set = _real_gen

check("ai-set 接口成功", out.get("ok") is True, str(out)[:120])
check("difficulty 透传到生成函数", PASSED.get("difficulty") == 5, str(PASSED.get("difficulty")))
check("thinking 透传到生成函数", PASSED.get("thinking") is False, str(PASSED.get("thinking")))
check("报告回显难度档名", out["report"]["difficulty_name"] == ai.DIFFICULTY_LEVELS[5]["name"],
      out["report"]["difficulty_name"])
check("报告回显思考开关", out["report"]["thinking"] is False)
check("报告回显推理 token", "reasoning_tokens" in out["report"], str(out["report"].get("reasoning_tokens")))
check("dry_run 不往库里写题（副本库题数不变）", out.get("dry_run") is True)

print()
print("=" * 68)
print("⑧b 「按考点出题」也一样透传（那条路是真题优先 + AI 补缺）")
print("=" * 68)
PASSED2 = {}


def fake_gen_questions(points, **kw):
    PASSED2.update(kw)
    return make_paper(4, difficulty=kw.get("difficulty") or 3), \
        {"prompt_tokens": 7, "completion_tokens": 8, "reasoning_tokens": 6}, \
        {"issues": [], "answer_dist": {}, "remember_n": 0, "difficulty_target": kw.get("difficulty"),
         "difficulty_mean": 5.0, "difficulty_dist": {5: 4}, "scene_suspect_n": 0, "hard_fail": False}


_real_gen_q = server.ai_mod.generate_questions
server.ai_mod.generate_questions = fake_gen_questions
try:
    out2 = server.daily_generate({"point_ids": [pt["id"]], "n_single": 99,   # 故意要远超真题量，逼出 AI 补题
                                  "n_analysis": 0, "n_short": 0, "n_essay": 0,
                                  "difficulty": 5, "thinking": False})
finally:
    server.ai_mod.generate_questions = _real_gen_q
check("按考点出题接口成功", out2.get("ok") is True, str(out2)[:120])
check("difficulty 透传到 generate_questions", PASSED2.get("difficulty") == 5, str(PASSED2.get("difficulty")))
check("thinking 透传到 generate_questions", PASSED2.get("thinking") is False, str(PASSED2.get("thinking")))
check("回显本次设置与程序层校验结果",
      out2.get("gen_settings", {}).get("difficulty") == 5
      and out2.get("gen_audit", {}).get("difficulty_dist") == {5: 4},
      str(out2.get("gen_settings")))
src = (APP / "server.py").read_text(encoding="utf-8")
check("弱项组卷把设置转发给了 daily_generate（静态核对接线）",
      '"difficulty": payload.get("difficulty")' in src
      and '"thinking": payload.get("thinking", True)' in src,
      "daily_weak_paper 内部转调 daily_generate，转发漏了就会静默用默认档")

print()
print("=" * 68)
print("⑨ 其余回归测试没被这次改动带坏（签名/调用点）")
print("=" * 68)
check("generate_questions 返回三元组",
      ai.generate_questions.__code__.co_argcount >= 5, "含 difficulty/thinking 参数")
check("server 里没有残留的二元解包", "gen, _ = ai_mod.generate_questions" not in src
      and "generated, usage = ai_mod.generate_questions" not in src)
check("两个调用点都传了 difficulty", src.count("difficulty=difficulty") >= 2,
      str(src.count("difficulty=difficulty")))

print()
print("=" * 68)
print("⑩ 确认真实库未被动过")
print("=" * 68)
after = (REAL_DB.stat().st_size, REAL_DB.stat().st_mtime_ns)
check(f"{REAL_DB.name} 大小/修改时间未变", BEFORE == after, f"before={BEFORE} after={after}")

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILED:
    print(f"❌ 失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✅ 全部通过。临时库已删除：", TMP)
