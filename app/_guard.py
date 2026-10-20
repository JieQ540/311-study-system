# -*- coding: utf-8 -*-
"""真实库 / 付费 AI 的统一守卫 —— 给「需人工运行」的脚本用。

## 为什么要有这个文件

2026-10-07 的测试安全盘点发现：`app/tests/` 与 `app/_oneoff/` 里有一批脚本会

① 打 `127.0.0.1:8765`（**真实服务 → 真实库**）；
② 直接调 DeepSeek（**按量计费**，思考模式一次批改就是几千 token）；
③ 在**真实库**上写作答/试卷记录，跑完自己删（中途崩了就留垃圾）。

这些脚本本身有价值 —— 真实 AI 出题的质量、手写照片的识别效果，只有真跑才知道，
所以**不删**。但它们绝不能被"顺手跑一下"：人和 AI 都会 `python tests/test_daily.py`
一下就花了钱、脏了库（README 里记着一次污染 14 条真实 attempts 的事故）。

所以给它们加一道**门禁**：默认拒绝运行，必须显式开闸。

## 用法（放在脚本最前面，三行）

    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from _guard import require_paid_ai_ok, require_real_db_ok
    require_paid_ai_ok(__file__, "会真实调用 DeepSeek 出题（按量计费）")
    require_real_db_ok(__file__, "会把 AI 生成的题写进真实库", writes=True)

## 开闸

    set ALLOW_PAID_AI=1         允许真实调用付费 AI
    set ALLOW_REAL_DB=1         允许读写真实库
    set ALLOW_LOCAL_SERVICE=1   允许连本机已在跑的服务（8765 那个 → 通常就是真实库）

**为什么默认关、而且要显式设成 `1`**：默认放行等于没有守卫。退出码用 2，
是为了和"脚本自己跑失败了"（1）区分开 —— 门禁拒绝不是报错，是设计。

**为什么"连本机服务"也要单独开闸**：`127.0.0.1:8765` 上跑的通常就是用户自己的
日常服务，它连的是**真实库** —— 一个只"浏览页面"的脚本照样会在里面留记录
（试卷持久化、练习 session）。2026-10-07 的安全盘点里，`verify_daily_flow.py`、
`_probe_all.py` 这类"只读浏览"脚本就是这么直连真实库的。
"""
import os
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent            # app/
ROOT = APP.parent                                # 项目根
REAL_DB = ROOT / "data" / "kaoyan.db"            # 用户自己的库（已 gitignore）
SEED_DB = ROOT / "data" / "kaoyan-seed.db"       # 随包题库（进仓库）
REAL_PORT = 8765                                 # 用户日常服务的端口

_YES = ("1", "true", "yes", "on")


def same_file(a, b) -> bool:
    """同一个文件？用 inode/文件索引比，别比字符串。

    Windows 的 8.3 短名（`C:\\Users\\JIEQIN~1\\…`）与长名指向同一个文件，
    纯字符串比较会漏判 —— `verify_resume_ui.py` 早就踩过这一条，这里沿用它的做法。
    """
    try:
        return os.path.samefile(a, b)
    except (OSError, TypeError):
        try:
            return Path(a).resolve() == Path(b).resolve()
        except (OSError, TypeError):
            return False


def _refuse(script, why, gate, extra=""):
    name = Path(script).name
    print()
    print("=" * 72)
    print(f"✗ 拒绝运行：{name}")
    print(f"  这个脚本{why}。")
    print(f"  要真跑请显式开闸：  set {gate}=1")
    if extra:
        print(f"  {extra}")
    print()
    print("  正常情况下你不需要跑它 —— 安全的回归入口是：")
    print("      cd app && python run_regression.py")
    print("=" * 72)
    sys.exit(2)


def require_real_db_ok(script, why, writes=True):
    """允许碰真实库？未开闸直接退出（退出码 2）。"""
    if os.environ.get("ALLOW_REAL_DB", "").strip().lower() in _YES:
        verb = "读**并写**" if writes else "读"
        print(f"[守卫] 已开闸 ALLOW_REAL_DB=1：本脚本会{verb}真实库 {REAL_DB.name}")
        return
    _refuse(script, why, "ALLOW_REAL_DB",
            extra=f"真实库：{REAL_DB}")


def require_paid_ai_ok(script, why):
    """允许真实调用付费 AI？未开闸直接退出（退出码 2）。"""
    if os.environ.get("ALLOW_PAID_AI", "").strip().lower() in _YES:
        print("[守卫] 已开闸 ALLOW_PAID_AI=1：本脚本会产生真实 API 费用")
        return
    _refuse(script, why, "ALLOW_PAID_AI")


def require_service_ok(script, port=REAL_PORT):
    """允许连一个**已经在跑的本机服务**吗？未开闸直接退出（退出码 2）。

    这是硬门禁，不是提醒：`127.0.0.1:8765` 上的服务连的是用户的真实库，
    "只浏览页面"的脚本也会让它写东西（试卷持久化、session 记录）。
    要对着**副本库**的服务跑（测试的常规做法），见 `tests/_safety.py::live_server`
    —— 那条路不需要本门禁，因为它自己起服务、自己证明连的是副本。
    """
    if os.environ.get("ALLOW_LOCAL_SERVICE", "").strip().lower() in _YES:
        print(f"[守卫] 已开闸 ALLOW_LOCAL_SERVICE=1：本脚本会访问 http://127.0.0.1:{port}")
        print(f"       请确认那个服务连的是哪份库："
              f"浏览器打开 http://127.0.0.1:{port}/api/health 会告诉你")
        return
    _refuse(script, f"会连本机已在跑的服务（127.0.0.1:{port}，它连的通常是真实库）",
            "ALLOW_LOCAL_SERVICE",
            extra="若那个服务其实是副本库（手动起的 8766），开闸即可。")
