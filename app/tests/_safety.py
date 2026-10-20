# -*- coding: utf-8 -*-
"""测试安全底座：副本库 / 独立端口真服务 / AI 出口闸 / 真实库指纹。

## 用它写的测试自动获得四条保证

① **连的是副本库** —— 把 `data/kaoyan.db` 复制到临时目录，`DSH_DB` 指过去；
   全新 clone（没有 kaoyan.db）时退回随包的 `kaoyan-seed.db`。
② **服务跑在空闲端口（绝不 8765）**，而且起完先问 `/api/health` **证明**
   "我打的就是这个进程、这个副本库"—— 不是靠文档约定、也不是靠"我以为"。
③ **AI 出口被封** —— 默认装 tripwire：任何真实 AI 请求直接抛错；
   需要 AI 输出的用例改成打桩（打桩仍然走真实的解析/校验/入库代码路径）。
④ **跑完比对真实库指纹**（size + mtime_ns + sha256，含 `-wal`/`-shm`），
   不一致就报失败 —— 这是"没污染真实库"的最终证据。

## 为什么第 ② 条是新的

以前 `verify_resume_ui.py` 只断言**自己进程**里 `server.DB == 副本`，而 HTTP 请求打的
那个 8766 服务是**人手动起的** —— 没有任何东西保证那个进程连的是副本库。
`/api/health`（server.py）就是为补这一层加的：它回显库路径、是否真实库、端口、pid，
测试据此断言。pid 相同还能顺带证明"没有连到别人的服务"。

## 用法

    from _safety import Checker, copy_db, live_server, real_db_fingerprint
    db, before = copy_db("exam"), real_db_fingerprint()
    ck = Checker("判分链路")
    with live_server(db) as srv:
        srv.identity_checks(ck)
        st, paper = srv.get_json("/api/paper")
        ...
    assert real_db_fingerprint() == before
    sys.exit(ck.finish())
"""
import contextlib
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from _guard import REAL_DB, REAL_PORT, SEED_DB, same_file  # noqa: E402  （单一出处）

# 抓一份**原始的** urlopen 留住。
#
# 为什么必须留：`ai` 里的 `ai.urllib` 就是标准库 `urllib` 模块本身
# （`import urllib.request`），所以给 ai 打桩等于把**全进程**的 `urllib.request.urlopen`
# 换掉 —— 测试自己的 HTTP 客户端也会被拦下来（第一版就是这么炸的）。
# 桩拒绝未声明的联网；只有测试客户端通过此原始函数访问自己的临时服务。
_REAL_URLOPEN = urllib.request.urlopen
_COPIES = {}


class SafetyError(RuntimeError):
    """安全前置条件不成立，必须中止本次测试。"""


# ------------------------------------------------------------------ 断言收集

class Checker:
    """断言收集器：逐条打印 PASS/FAIL，结尾汇总并给出退出码。"""

    def __init__(self, title):
        self.title = title
        self.n = 0
        self.failed = []

    def check(self, name, cond, extra=""):
        self.n += 1
        print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
        if not cond:
            self.failed.append(name)
        return bool(cond)

    def finish(self):
        print()
        print("=" * 72)
        if self.failed:
            print(f"✗ {self.title}：{self.n - len(self.failed)}/{self.n} 项通过，"
                  f"失败 {len(self.failed)} 项：")
            for n in self.failed:
                print("   -", n)
            return 1
        print(f"✓ {self.title}：{self.n}/{self.n} 项全部通过")
        return 0


# ------------------------------------------------------------------ 真实库指纹

def fingerprint(path):
    p = Path(path)
    if not p.exists():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    st = p.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": h.hexdigest()}


def real_db_fingerprint():
    """真实库连同它的事务日志一起取指纹。

    `-wal` 必须一起看：SQLite 的写入可能还留在 wal 里没落盘，只看主文件会漏掉
    "库被动过"这件事。
    """
    paths = [REAL_DB]
    paths += [REAL_DB.with_name(REAL_DB.name + suffix) for suffix in ("-wal", "-shm")]
    return {p.name: fingerprint(p) for p in paths}


def copy_db(tag="safe"):
    """真实库 → 临时副本。没有真实库（全新 clone）就用随包种子库。"""
    src = REAL_DB if REAL_DB.exists() else SEED_DB
    if not src.exists():
        raise SystemExit(f"✗ 既没有 {REAL_DB.name} 也没有 {SEED_DB.name}，建不出副本库")
    owner = tempfile.TemporaryDirectory(prefix=f"kaoyan_{tag}_")
    db = Path(owner.name) / "kaoyan.db"
    # 不能以 SQLite mode=ro 打开真实 WAL 库：它仍可能创建/修改 -wal、-shm。
    # 先只用文件读取复制主库和 WAL，确认复制期间源未变，再在临时目录重建 SHM。
    try:
        stage = Path(owner.name) / "snapshot.db"
        src_wal = src.with_name(src.name + "-wal")
        stage_wal = stage.with_name(stage.name + "-wal")
        for _ in range(3):
            before = (fingerprint(src), fingerprint(src_wal))
            shutil.copy2(src, stage)
            if src_wal.exists():
                shutil.copy2(src_wal, stage_wal)
            elif stage_wal.exists():
                stage_wal.unlink()
            after = (fingerprint(src), fingerprint(src_wal))
            copied = (fingerprint(stage), fingerprint(stage_wal))
            # copy2 保留源 mtime；连大小/哈希一起核对，避免复制出跨事务的两个文件。
            if before == after == copied:
                break
        else:
            raise SafetyError("源数据库正在变化，无法取得稳定副本，请停止练习写入后重试")
        with contextlib.closing(sqlite3.connect(stage)) as source:
            with contextlib.closing(sqlite3.connect(db)) as target:
                source.backup(target)
    except BaseException:
        owner.cleanup()
        raise
    _COPIES[db] = owner
    return db


def release_db(db):
    """只清理本模块亲自创建并登记的临时目录，绝不根据外部路径递归删除。"""
    owner = _COPIES.get(Path(db))
    if owner:
        owner.cleanup()
        _COPIES.pop(Path(db), None)


def require_test_db(db):
    """在导入业务模块之前拒绝真实库、种子库及其硬链接/路径别名。"""
    db = Path(db).resolve()
    if any(same_file(db, protected) for protected in (REAL_DB, SEED_DB)):
        raise SafetyError("拒绝用真实库或种子库启动测试服务")
    if not db.is_file():
        raise SafetyError("测试副本不存在，不能由服务自动创建")
    try:
        with contextlib.closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
            valid = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='questions'").fetchone()
        if not valid:
            raise SafetyError("测试副本缺少 questions 表")
    except sqlite3.Error as e:
        raise SafetyError("测试副本不是有效 SQLite 数据库") from e
    return db


# ------------------------------------------------------------------ 端口

def free_port():
    """要一个真正空闲的端口。**绝不返回 8765**（那是用户的日常服务）。"""
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port != REAL_PORT:
            return port
    raise SystemExit("✗ 连续 50 次都只拿到 8765，放弃")


def port_busy(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ------------------------------------------------------------------ AI 出口闸

class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ai_reply(content_obj, usage=None):
    """把「模型该回的那段 JSON」包成 OpenAI 风格的响应。"""
    if not isinstance(content_obj, str):
        content_obj = json.dumps(content_obj, ensure_ascii=False)
    return {"choices": [{"message": {"content": content_obj}, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 1, "completion_tokens": 1},
            "model": "stub-model"}


@contextlib.contextmanager
def ai_transport(handler=None):
    """接管 `ai` 的 HTTP 出口。

    - `handler=None` → **tripwire**：任何真实 AI 请求直接抛 AssertionError
      （"这个用例不该出网"这件事必须响，而不是悄悄花钱）；
    - `handler=req_payload -> 响应 dict` → 打桩，不联网、不花钱。

    指向 `chat/completions` 或桩 base_url 的请求由桩处理，其它 urllib 请求一律拒绝。
    测试客户端保留原始 urlopen，只访问已经验明身份的临时服务。

    yield 一个 list，记录每次出网请求（可断言"调了几次、payload 长什么样"）。
    """
    import ai

    calls = []
    real_urlopen = ai.urllib.request.urlopen
    real_cfg = ai._CFG
    # 全新 clone 没有 config.json，cfg() 会抛；注入桩配置，让"请求长什么样"照样能验。
    #
    # 两个刻意的选择：
    #   ① `model` 用**真实存在的** `deepseek-flash` 而不是编造的 `stub-model` ——
    #      设置接口要按模型查官方价目（"切了模型却仍按旧价算"是必须能被测出来的风险），
    #      编造的模型名会让这条链路永远查不到、也就永远测不到。
    #   ② 带一组**扁平价目**：金额与"当前是高峰还是空闲"无关，用例在任何时刻跑都稳定。
    stub_cfg = {"api": {"model": "deepseek-flash", "base_url": "http://127.0.0.1:1",
                        "api_key": "sk-stub", "temperature": 0.3, "timeout_seconds": 30,
                        "pricing": {"currency": "CNY", "input_per_million": 1.0,
                                    "output_per_million": 4.0,
                                    "effective_date": "2026-09-01"}}}
    ai_base = stub_cfg["api"]["base_url"]

    def is_ai_url(url):
        return urlsplit(url).path.endswith("/chat/completions") or urlsplit(url).netloc == urlsplit(ai_base).netloc

    def fake_urlopen(req, timeout=None):
        url = getattr(req, "full_url", str(req))
        if not is_ai_url(url):
            raise SafetyError("测试期间拒绝未声明的联网请求")
        payload = {}
        if getattr(req, "data", None):
            payload = json.loads(req.data.decode("utf-8"))
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        if handler is None:
            raise AssertionError(
                "AI 出口闸：本用例声明了『不调 AI』，却真的发出了请求 → " + url)
        return _FakeResp(handler(payload))

    ai._CFG = stub_cfg
    ai.urllib.request.urlopen = fake_urlopen
    try:
        yield calls
    finally:
        ai.urllib.request.urlopen = real_urlopen
        ai._CFG = real_cfg


# ------------------------------------------------------------------ 真服务

class TestServer:
    """跑在独立端口上的真 uvicorn（连副本库）的客户端。"""

    def __init__(self, base, port, db, ai_calls):
        self.base = base
        self.port = port
        self.db = db
        self.ai_calls = ai_calls

    # ---- HTTP

    def get(self, path, timeout=120):
        return self._raw(urllib.request.Request(self.base + path), timeout)

    def post(self, path, payload, timeout=600):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        return self._raw(req, timeout)

    @staticmethod
    def _raw(req, timeout):
        try:
            with _REAL_URLOPEN(req, timeout=timeout) as r:      # 绕开 AI 桩（见文件头说明）
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get_json(self, path, timeout=120):
        st, body = self.get(path, timeout)
        try:
            return st, json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return st, None

    def post_json(self, path, payload, timeout=600):
        st, body = self.post(path, payload, timeout)
        try:
            return st, json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return st, None

    def request_json(self, method, path, payload=None, timeout=600):
        """任意方法（目前给设置接口的 PUT 用）。"""
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        return self._raw_json(req, timeout)

    def _raw_json(self, req, timeout):
        st, body = self._raw(req, timeout)
        try:
            return st, json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return st, None

    # ---- 身份证明

    def identity_checks(self, ck=None):
        """证明「我打的这个服务 = 本进程临时起的那个 + 连的是本次副本库」。

        这几条是本文件存在的理由：**不能靠端口号或文档约定**，要问服务自己。
        """
        st, h = self.get_json("/api/health")
        h = h if isinstance(h, dict) else {}
        checks = [
            ("/api/health 可达且成功", st == 200 and h.get("ok") is True),
            ("服务的库 = 本次副本库", bool(h.get("db")) and same_file(h["db"], self.db)),
            ("服务不是真实库", h.get("db_is_real") is False),
            ("服务端口正确", str(h.get("port")) == str(self.port)),
            ("服务 pid 与本测试进程一致", h.get("pid") == os.getpid()),
            ("端口不是用户的 8765", self.port != REAL_PORT),
        ]
        for name, ok in checks:
            if ck:
                ck.check(name, ok)
            if not ok:
                raise SafetyError("服务身份检查失败：" + name)


def _wait_ready(srv, uvicorn_server, thread, timeout=25.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not thread.is_alive():
            raise SystemExit("✗ 临时服务线程退出了（端口被占？看上面的 uvicorn 输出）")
        try:
            with _REAL_URLOPEN(srv.base + "/api/health", timeout=1.5) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.15)
    raise SystemExit(f"✗ 临时服务 {timeout:.0f} 秒内没起来：{srv.base}")


@contextlib.contextmanager
def live_server(db, port=None, handler=None):
    """起一个真 uvicorn（独立端口）+ 副本库，装好 AI 出口闸，yield `TestServer`。

    handler 见 `ai_transport`：默认 None = 出网即报错。
    之所以在**同进程内**起服务（而不是 subprocess）：只有这样才能给 `ai` 装桩，
    让 HTTP 链路（真 socket、真 uvicorn）与"不花钱"同时成立。
    """
    db = require_test_db(db)
    if port == REAL_PORT:
        raise SafetyError(f"拒绝在用户的 {REAL_PORT} 端口上起临时服务")
    # dbpath / ai 可能早于 server 导入；不能只检查 server 的缓存。
    for name in ("dbpath", "ai", "server"):
        mod = sys.modules.get(name)
        if mod is not None and not same_file(getattr(mod, "DB", None), db):
            raise SafetyError(f"{name} 已连接其他数据库，请在独立进程运行套件")
    previous_db = os.environ.get("DSH_DB")
    os.environ["DSH_DB"] = str(db)
    try:
        with _serve(db, port, handler) as srv:
            yield srv
    finally:
        if previous_db is None:
            os.environ.pop("DSH_DB", None)
        else:
            os.environ["DSH_DB"] = previous_db


@contextlib.contextmanager
def _serve(db, port, handler):
    if "server" in sys.modules:
        srv_mod = sys.modules["server"]
        if not same_file(srv_mod.DB, db):
            raise SystemExit(f"✗ server 已经连着别的库（{srv_mod.DB}），本进程不能再换副本库")
    else:
        import server as srv_mod  # noqa: E402
    if not same_file(srv_mod.DB, db):
        raise SystemExit(f"✗ server 没连到副本库：{srv_mod.DB} vs {db}")

    import uvicorn
    # 保留绑定的 socket，避免“找到空闲端口”和真正启动之间被其他进程抢占。
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port or 0))
        port = sock.getsockname()[1]
        if port == REAL_PORT:
            raise SafetyError("系统分配了受保护端口，拒绝启动")
        config = uvicorn.Config(srv_mod.app, host="127.0.0.1", port=port,
                                log_level="warning", access_log=False)
        usrv = uvicorn.Server(config)
        thread = threading.Thread(target=usrv.run, kwargs={"sockets": [sock]}, daemon=True)
        with ai_transport(handler) as calls:
            thread.start()
            srv = TestServer(f"http://127.0.0.1:{port}", port, db, calls)
            try:
                _wait_ready(srv, usrv, thread)
                srv.identity_checks()   # 身份成立之后，调用者才拿得到服务
                yield srv
            finally:
                usrv.should_exit = True
                thread.join(timeout=10)
                if thread.is_alive():
                    usrv.force_exit = True
                    thread.join(timeout=5)
                if thread.is_alive():
                    raise SafetyError("测试服务未能停止，拒绝报告成功")


# ------------------------------------------------------------------ 假数据

def canned_generate(difficulty=3, outline_id=307, n_single=2, n_subj=1):
    """造一份**能过 `audit_ai_set`** 的"模型回复"（出题链路用）。

    规则是照着 `ai.audit_ai_set` 的硬校验反推的：题干 ≥15 字、四个选项都非空、
    答案在 ABCD 且轮换、解析里不许出现选项字母、难度贴住目标档、
    主观题 ≥3 个采分点、辨析题首个采分点必须含「判断正误」类词。
    分值形态照着 `server._ai_marks_trusted` 反推：逐点之和 == 满分，
    且每个小问内要点之和 == 该小问分值。
    """
    def single(i):
        letter = "ABCD"[(i - 1) % 4]
        opts = {k: f"选项内容{k}，描述一种教育主张或做法" for k in "ABCD"}
        opts[letter] = "活动课程"
        return {"outline_id": outline_id, "difficulty": difficulty,
                "stem": f"某中学的课堂上，教师组织学生讨论第{i}个问题并完成小组作业，"
                        f"这一做法最符合下列哪一种课程主张？",
                "options": opts, "answer": letter,
                "explain": "以活动为中心的组织方式强调通过实际活动获得直接经验，"
                           "与把课程等同于科目划分的主张不同。",
                "cognitive": "应用"}

    def subj(qtype, full, sections, points):
        return {"outline_id": outline_id, "qtype": qtype, "full_score": full,
                "stem": f"（{qtype}）请结合材料分析该观点，并说明理由。",
                "sections": sections, "points": points,
                "note": "答出要点即可，言之有理酌情给分。", "difficulty": difficulty}

    singles = [single(i) for i in range(1, n_single + 1)]
    subs = []
    if n_subj:
        subs.append(subj(
            "analysis", 15,
            [{"key": "1", "label": "（1）判断", "score": 6, "desc": "判断正误"},
             {"key": "2", "label": "（2）理由", "score": 9, "desc": "阐明理由"}],
            [{"seq": 1, "section": "1", "score": 6,
              "claim": "该说法错误：尊重兴趣不等于放弃引导", "evidence": "兴趣与努力相互促进"},
             {"seq": 2, "section": "2", "score": 4,
              "claim": "教师应发挥主导作用", "evidence": "教育是有目的的活动"},
             {"seq": 3, "section": "2", "score": 5,
              "claim": "兴趣需要在活动中培养", "evidence": "直接经验与间接经验相结合"}]))
    return {"single": singles, "subjective": subs}


def canned_generate_handler(**kw):
    """`ai_transport` 用的 handler：任何出题请求都回同一份假卷子。"""
    paper = canned_generate(**kw)

    def handler(_payload):
        return ai_reply(paper)

    return handler
