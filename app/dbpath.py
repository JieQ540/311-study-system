# -*- coding: utf-8 -*-
"""决定「这次到底读写哪个数据库」—— server.py 与 ai.py 共用这一处。

选择的顺序：

  1. 环境变量 ``DSH_DB``   —— 测试专用：把库指向临时副本，不复制、不改动别的库
  2. ``data/kaoyan.db`` 存在**且真的有表** —— 用用户自己的库（含全部练习记录）
  3. 其余情况 —— 把随包的 ``data/kaoyan-seed.db`` 复制出一份 ``data/kaoyan.db``，再指向它

为什么必须有第 3 步（而不是直接拿 seed 库来写）
------------------------------------------------
``kaoyan-seed.db`` 是 **git 跟踪**的随包文件。直接把练习记录写进去有三个后果：

- 用户按 README 去 ``data/kaoyan.db`` 备份，那里什么都没有 —— 记录等于丢了；
- ``git checkout .`` / 重新 clone / ``git stash`` 会**静默抹掉**全部学习历史；
- 以后 ``git pull`` 必然与远端的 seed 冲突。

README 承诺「第一次写库会自动生成 data/kaoyan.db」，这里就是那句承诺的实现。

为什么第 2 步判断的是「有没有表」，而不是 ``.exists()``
------------------------------------------------------
只看文件是否存在的话，一个 0 字节、或被中断写坏的 ``data/kaoyan.db`` 会被选中，
启动时直接崩在 ``no such table: attempts``。库不可用时应当重建，而不是拒绝启动。
原有文件（非空时）先备份成 ``*.bak-<时间戳>``，**绝不静默删除** —— 万一是真实
数据，至少还找得回来。

注意：本模块在**导入时**就完成定位（``DB = resolve()``），这样 server 与 ai
拿到的一定是同一个库。也正因为如此，数据管线脚本（ingest_* / tag_questions）
在全新 clone 上第一次运行时，也会先得到一份从 seed 复制出来的 ``kaoyan.db`` ——
这比旧行为（直接写进被 git 跟踪的 seed 库）安全。
"""
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # 项目根（本文件在 app/ 下）
LIVE_DB = ROOT / "data" / "kaoyan.db"           # 用户自己的库（已 gitignore）
SEED_DB = ROOT / "data" / "kaoyan-seed.db"      # 随包题库（进仓库）


def _has_schema(path: Path) -> bool:
    """这个文件是不是一个能用的库（有 questions 表）？

    不能只看 ``.exists()``：0 字节和损坏文件都会「存在」但不可用。
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        con = sqlite3.connect(path, timeout=5)
    except sqlite3.Error:
        return False
    try:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='questions'"
        ).fetchone() is not None
    except sqlite3.Error:        # 不是 SQLite 文件 / 损坏 / 不是数据库
        return False
    finally:
        con.close()


def resolve(verbose: bool = True) -> Path:
    """返回本次要用的数据库路径，必要时顺手把 seed 复制成用户自己的库。"""
    env = os.environ.get("DSH_DB")
    if env:
        return Path(env)

    if _has_schema(LIVE_DB):
        return LIVE_DB

    if not SEED_DB.exists():
        # 既没有自己的库、也没有随包种子库（比如被裁掉的发行版）：
        # 交回 LIVE_DB，让调用方在空库上建表。
        return LIVE_DB

    LIVE_DB.parent.mkdir(parents=True, exist_ok=True)
    note = ""
    if LIVE_DB.exists() and LIVE_DB.stat().st_size > 0:
        # 存在但不是可用的库：先备份再重建，不静默删用户的东西
        backup = LIVE_DB.with_name(
            f"{LIVE_DB.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        LIVE_DB.replace(backup)
        note = f"；原文件不是可用的库，已备份为 {backup.name}"
    shutil.copy2(SEED_DB, LIVE_DB)
    if verbose:
        print(f"[db] 已从随包题库生成 {LIVE_DB.relative_to(ROOT)}，"
              f"之后的练习记录都存在这个文件里{note}")
    return LIVE_DB


DB = resolve()
