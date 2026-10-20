# -*- coding: utf-8 -*-
"""provider / model 注册表 —— 「可用模型有哪些、各自什么能力、密钥从哪来」。

## 设计来源与本地化取舍

按 https://github.com/earendil-works/pi/blob/main/packages/ai/README.md 的
**provider 集合**语义做本地化（照搬设计、不引入依赖）：

| pi-ai 概念 | 这里的对应物 |
| --- | --- |
| `Models.getModels()` / `getModel(provider, id)` | `get_catalog()` / `find_model()` |
| `Model` 元数据（`contextWindow`/`input`/`reasoning`） | 同名小写字段，**尽量从服务端 `/models` 动态取** |
| `models.getAuth(model)` | `describe_auth()`：配没配、密钥从哪来、尾 4 位（**绝不给密钥本体**） |
| `models.refresh()`（Dynamic Providers） | `refresh()`：重新拉 `/models` 并落缓存 |

pi-ai 的能力标签是**随包编目**（`src/providers/data/<id>.json`）的；这里改成
**优先问服务端**（DeepSeek 的 `GET /models` 会回 `context_window`、`max_output_tokens`、
`input_modalities`、`effort.supported_levels`），因为编目会过期，而服务端不会骗自己。

## 三级来源与「新鲜度」必须如实标注

1. `live`    —— 刚问了服务端；
2. `cache`   —— 用本地缓存（`source/catalog_cache.json`，默认 5 分钟有效）；
3. `bundled` —— 用随包的 `models_catalog.json`（离线/未配密钥/网络不通时）。

**为什么不静默退回**：如果"离线兜底"不说明来源，用户会以为页面上的能力数据是刚查的；
一旦官方改了上下文窗口或下架模型，人会照着一个过期的数字做判断。所以 `get_catalog()`
永远带回 `source` 与 `reason`，页面照它显示。
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent
ROOT = APP.parent
BUNDLED = APP / "models_catalog.json"                  # 随包编目（进 git）
CACHE = ROOT / "source" / "catalog_cache.json"          # 运行期缓存（不进 git）

CACHE_TTL = 300          # 秒；5 分钟内不重复请求 /models
FETCH_TIMEOUT = 10       # 秒；这是"看一眼目录"，不该把页面卡住

PROVIDERS = {
    "deepseek": {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "api": "openai-completions",
        "pricing_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        "env_keys": ["DEEPSEEK_API_KEY"],
    },
}


# ---------------------------------------------------------------- 小工具

def redact(text, api_key=None):
    """把可能混进报错里的密钥抠掉。

    为什么必须有：HTTP 报错、URL、异常字符串都可能把 Authorization 带出来，
    而这些文本会进页面、进日志、进对话。任何**对外**的字符串都先过这里。
    """
    s = str(text or "")
    for secret in filter(None, [api_key]):
        if len(str(secret)) >= 8:
            s = s.replace(str(secret), "***")
    return s


def _fetch_models(base_url, api_key, timeout=FETCH_TIMEOUT):
    """问服务端要模型清单。返回 `(models, error)`：**不抛异常**，让调用方决定兜底。"""
    url = str(base_url or "").rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + str(api_key or "")})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        return None, f"HTTP {e.code}：{redact(detail, api_key)}"
    except Exception as e:
        return None, f"{type(e).__name__}: {redact(e, api_key)}"
    data = raw.get("data") if isinstance(raw, dict) else raw
    if not isinstance(data, list):
        return None, "返回结构不是模型列表"
    return [_normalize(m) for m in data if isinstance(m, dict) and m.get("id")], None


def _normalize(m):
    """服务端条目 → 本项目的模型元数据（字段名收成小写驼峰，与 pi-ai 对齐）。"""
    effort = m.get("effort") or {}
    levels = effort.get("supported_levels") or []
    modalities = m.get("input_modalities") or ["text"]
    return {
        "id": m.get("id"),
        "name": m.get("name") or m.get("id"),
        "type": "chat",
        "provider": m.get("owned_by") or "deepseek",
        "context_window": m.get("context_window"),
        "max_output_tokens": m.get("max_output_tokens"),
        "input": modalities,
        "vision": "image" in modalities,
        "reasoning": bool(levels),          # 有 effort 档位 = 支持思考模式
        "reasoning_levels": levels,
        "reasoning_default": effort.get("default_level"),
    }


# ---------------------------------------------------------------- 读缓存 / 编目

def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _bundled():
    d = _read_json(BUNDLED) or {}
    return [m for m in (d.get("models") or []) if m.get("id")], d


def _cached(ttl=CACHE_TTL, now=None):
    d = _read_json(CACHE)
    if not d:
        return None, "没有本地缓存"
    models = [m for m in (d.get("models") or []) if m.get("id")]
    if not models:
        return None, "本地缓存里没有模型"
    age = (now if now is not None else time.time()) - float(d.get("fetched_at") or 0)
    if ttl is not None and age > ttl:
        return None, f"本地缓存已过期（{int(age)} 秒前取的）"
    return models, ""


def write_cache(models, provider=None):
    """把刚取到的模型清单落盘（原子替换，别留下半个文件）。"""
    payload = {"provider": provider or "deepseek",
               "fetched_at": time.time(),
               "fetched_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
               "models": models}
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CACHE)
    except OSError:
        pass          # 缓存写不进去不是错误：下次再问服务端就是了


def get_catalog(api_cfg=None, allow_network=False, force=False, ttl=CACHE_TTL):
    """当前可用的模型清单。

    返回 `{"models": [...], "source": "live|cache|bundled|config-only", "reason": str,
            "fetched_at": str, "provider": {...}}`。

    `allow_network=False`（默认）时**只读缓存与编目** —— GET 接口不应因为网络慢而卡住，
    也不该偷偷产生一次外部请求（测试环境里"未声明的联网"是要被拒绝的）。
    """
    api_cfg = api_cfg or {}
    provider_id = str(api_cfg.get("provider") or "deepseek")
    provider = dict(PROVIDERS.get(provider_id) or {})
    provider.setdefault("id", provider_id)
    provider.setdefault("name", provider_id)
    base_url = api_cfg.get("base_url") or provider.get("base_url")
    api_key = api_cfg.get("api_key")

    def wrap(models, source, reason="", fetched_at=""):
        return {"models": models, "source": source, "reason": reason,
                "fetched_at": fetched_at, "provider": provider,
                "allow_network": bool(allow_network)}

    models, why = _cached(ttl=None if force else ttl)
    # 不联网时：只要有缓存就直接用（`why` 只在缓存过期/缺失时非空，一并交回说明）。
    if models and not allow_network:
        return wrap(models, "cache", why)

    if allow_network and api_key:
        live, err = _fetch_models(base_url, api_key)
        if live:
            write_cache(live, provider_id)
            return wrap(live, "live", "", time.strftime("%Y-%m-%d %H:%M:%S"))
        if models:
            return wrap(models, "cache", f"问服务端失败（{err}），用本地缓存")
        bundled, _ = _bundled()
        if bundled:
            return wrap(bundled, "bundled", f"问服务端失败（{err}），用随包编目")
        return wrap([], "config-only", f"问服务端失败（{err}），且没有编目")

    if models:
        return wrap(models, "cache", why)

    bundled, meta = _bundled()
    if bundled:
        reason = "未配密钥，用随包编目" if not api_key else "未联网查询，用随包编目"
        return wrap(bundled, "bundled", reason, str(meta.get("verified_at") or ""))

    # 最后一档：连编目都没有时，至少把当前配置的模型当成"已知的一个"报出来，
    # 让页面还能显示"正在用什么"，而不是空白 + 一个假报错。
    current = api_cfg.get("model")
    if current:
        return wrap([{"id": current, "name": current, "type": "chat",
                      "reasoning": None, "vision": None, "context_window": None,
                      "input": [], "unknown": True}],
                    "config-only", "没有编目也没有缓存，只报当前配置的模型")
    return wrap([], "config-only", "没有任何模型信息")


def refresh(api_cfg, force=True):
    """重新问一次服务端，并把结果写进缓存。`(catalog, diff)`。"""
    before, _ = _cached(ttl=None)
    after = get_catalog(api_cfg, allow_network=True, force=force)
    old_ids = {m.get("id") for m in (before or [])}
    new_ids = {m.get("id") for m in after.get("models") or []}
    diff = {"added": sorted(new_ids - old_ids), "removed": sorted(old_ids - new_ids),
            "changed": sorted(i for i in (old_ids & new_ids)
                              if _signature(next(m for m in before if m.get("id") == i))
                              != _signature(next(m for m in after["models"] if m.get("id") == i)))}
    return after, diff


def _signature(m):
    """能力指纹：用于判断"同一个模型的能力变了没有"。"""
    m = m or {}
    return (m.get("context_window"), m.get("max_output_tokens"),
            tuple(m.get("input") or []), tuple(m.get("reasoning_levels") or []))


# ---------------------------------------------------------------- 查询 / 鉴权

def find_model(model_id, catalog=None):
    if not catalog:
        return None
    for m in catalog.get("models") or []:
        if m.get("id") == model_id:
            return m
    return None


def describe_auth(api_cfg=None, sources=None, local_has_key=False):
    """密钥配没配、从哪来、尾 4 位 —— 对应 pi-ai 的 `models.getAuth()`。

    **绝不返回密钥本体**（红线：明文 key 不外发）。尾 4 位只用于让用户确认
    "换的是不是我以为的那把钥匙"，长度不足时整段打码。
    """
    api_cfg = api_cfg or {}
    key = str(api_cfg.get("api_key") or "")
    env_names = (PROVIDERS.get(str(api_cfg.get("provider") or "deepseek")) or {}).get("env_keys") or []
    env_hit = next((n for n in env_names if os.environ.get(n)), None)

    if key:
        source = "config.local.json" if local_has_key else "config.json"
        how = ("本地覆盖层里的密钥（优先级最高，可从这里清除）" if local_has_key
               else "config.json 里的密钥")
    elif env_hit:
        source, how = env_hit, f"环境变量 {env_hit}"
    else:
        source, how = "none", "没有可用密钥：出题/批改会失败，只有题库相关功能可用"

    return {
        "configured": bool(key or env_hit),
        "source": source,
        "source_text": how,
        "key_tail": ("…" + key[-4:]) if len(key) > 8 else ("***" if key else ""),
        "key_length_ok": len(key) >= 8 if key else False,
        "from_local": bool(local_has_key),
        "env_candidates": env_names,
        "env_hit": env_hit,
        "base_url": api_cfg.get("base_url") or "",
        "provider": api_cfg.get("provider") or "",
        "chat_endpoint": str(api_cfg.get("base_url") or "").rstrip("/") + "/chat/completions",
    }
