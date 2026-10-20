# -*- coding: utf-8 -*-
"""DeepSeek 官方价目表与「按当前时段选费率」—— 金额估算的唯一实现。

## 为什么要有这个文件

`ai.pricing()` 原来只认 `config.json` 里一组**扁平**单价（输入 X、输出 Y）。
那个形状在只有一个模型、且价格不随时间变化时够用，但官方价目表实际是：

    模型 × 时段（高峰/空闲，2 倍差）× 缓存命中/未命中（输入侧）

核对过的官方数字（元 / 百万 tokens，2026-10-08 取自官方「模型 & 价格」页）：

| 模型 | 输入·缓存命中 | 输入·未命中 | 输出 |
| --- | --- | --- | --- |
| deepseek-flash | 0.02 / 0.04 | 1 / 2 | 4 / 8 |
| deepseek-v4-pro | 0.15 / 0.30 | 4.5 / 9.0 | 13.5 / 27.0 |

（每格「空闲时段 / 高峰时段」。高峰 = 北京时间周一至周五 9:00–12:00、14:00–18:00，
不含中国法定节假日；其余时段一律空闲，价格是高峰的一半。）

所以「切到 Pro 之后还按 Flash 的扁平价报金额」会**静默**差 4.5–9 倍，命中缓存时
更多。这与项目已经踩过的「金额写死在前端 9 处」是同一类错误，只是更难发现 ——
数字看起来仍然很精确。

## 优先级（`resolve_rates`）

1. `config.json` 的 `api.pricing`（**用户显式覆盖**，老扁平写法仍按老语义整段生效）；
2. 本文件的 `TABLE`（官方价目，按模型 id 选）；
3. 都没有 → 返回 `None`：**只显示 token，不显示金额**（沿用批次 4 的默认）。

## 时段的两个缺口（`period_of` 如实标出，不假装精确）

- 中国法定节假日以国务院办公厅通知为准，**程序算不出来**。要精确，把日期填进
  `config.json` 的 `api.offpeak_dates`（节假日）与 `api.peak_dates`（调休上班日）；
  没填时按「工作日高峰、周末空闲」推断，并在返回值的 `note` 里说明。
- 跨年时表格会过期：`TABLE_VERIFIED_AT` 记的是核对日期，返回给页面，让它能提示
  「价目是这天核对的」。
"""
from datetime import datetime, time, timedelta, timezone

# 北京没有夏令时，固定 +08:00 即可。
# **不用 zoneinfo**：Windows 上没装 tzdata 包时 `ZoneInfo("Asia/Shanghai")`
# 会抛 ModuleNotFoundError（本机实测），而项目约定「不引入新依赖」。
CST = timezone(timedelta(hours=8))

TABLE_VERIFIED_AT = "2026-10-08"
TABLE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
CURRENCY = "CNY"

# 官方价目。每格是 {时段: 单价}，键固定为 "peak"（高峰）与 "offpeak"（空闲）。
TABLE = {
    "deepseek-flash": {
        "name": "DeepSeek-V4.1-Flash",
        "input_cached": {"offpeak": 0.02, "peak": 0.04},
        "input_miss": {"offpeak": 1.0, "peak": 2.0},
        "output": {"offpeak": 4.0, "peak": 8.0},
    },
    "deepseek-v4-pro": {
        "name": "DeepSeek-V4-Pro",
        "input_cached": {"offpeak": 0.15, "peak": 0.30},
        "input_miss": {"offpeak": 4.5, "peak": 9.0},
        "output": {"offpeak": 13.5, "peak": 27.0},
    },
}

# 官方明确写着「仍可调用，但对应模型已下线，请求由 DeepSeek-V4.1-Flash 提供服务，
# 并按 Flash 价格计费」的旧模型名。留着是为了让一个老 config 也能算出**对**的金额。
LEGACY_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}

# 高峰时段（北京时间，仅工作日）
PEAK_WINDOWS = ((time(9, 0), time(12, 0)), (time(14, 0), time(18, 0)))
DEFAULT_PEAK_HOURS = [[9, 12], [14, 18]]


# ---------------------------------------------------------------- 时段判定

def _parse_date_list(values):
    """把配置里的日期列表收敛成 `{date}`：坏值直接忽略，不猜、不抛。"""
    out = set()
    for v in values or []:
        try:
            out.add(datetime.strptime(str(v).strip(), "%Y-%m-%d").date())
        except (TypeError, ValueError):
            continue
    return out


def _peak_windows(api_cfg=None):
    """高峰时段窗口，默认 9:00–12:00 与 14:00–18:00，可用配置覆盖（测试也靠它）。"""
    raw = (api_cfg or {}).get("peak_hours") or DEFAULT_PEAK_HOURS
    windows = []
    for pair in raw:
        try:
            start, end = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 <= start < end <= 24:
            windows.append((time(start % 24, 0), time(end % 24, 0)))
    return tuple(windows) or tuple(
        (time(a, 0), time(b, 0)) for a, b in DEFAULT_PEAK_HOURS)


def period_info(when=None, api_cfg=None):
    """时段判定的**完整结果**：`{"period", "note", "source"}`。

    `period_of()` 只回一个字符串（调用点大多只关心这个）；但"这次是推断的、
    节假日可能算高"这条提示必须能传到页面上 —— 否则用户看到金额却不知道它可能偏。
    """
    now = when or datetime.now(CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    else:
        now = now.astimezone(CST)

    api_cfg = api_cfg or {}
    offpeak_dates = _parse_date_list(api_cfg.get("offpeak_dates"))
    peak_dates = _parse_date_list(api_cfg.get("peak_dates"))
    day = now.date()
    note, source = "", "weekday"

    if day in peak_dates:                      # 调休上班日：即使是周末也按高峰
        is_peak, source = True, "peak_dates"
        note = "该日在 api.peak_dates（调休上班日）名单里，按高峰时段计。"
    elif day in offpeak_dates:                 # 法定节假日：即使是工作日也按空闲
        is_peak, source = False, "offpeak_dates"
        note = "该日在 api.offpeak_dates（节假日）名单里，按空闲时段计。"
    elif now.weekday() >= 5:                   # 5=周六 6=周日
        is_peak, source = False, "weekend"
    else:
        is_peak = any(a <= now.time() < b for a, b in _peak_windows(api_cfg))
        if not (offpeak_dates or peak_dates):
            note = ("未配置 api.offpeak_dates / api.peak_dates（法定节假日与调休），"
                    "本次按「工作日高峰、周末空闲」推断；节假日期间可能算高。")
    return {"period": "peak" if is_peak else "offpeak", "note": note, "source": source,
            "beijing_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "peak_hours": [list(w) for w in _peak_windows(api_cfg)]}


def period_of(when=None, api_cfg=None):
    """这一刻按哪个时段的费率计费？返回 `peak` 或 `offpeak`（只回时段）。

    需要"为什么是这个时段、有没有可能不准"就用 `period_info()`。
    """
    return period_info(when, api_cfg)["period"]


def _pick(mapping, period):
    """从 {时段: 价} 取该时段的值；缺该时段就退回另一个时段（老扁平写法会走到这）。"""
    if not isinstance(mapping, dict):
        return None
    v = mapping.get(period)
    if v is None:
        v = mapping.get("peak" if period == "offpeak" else "offpeak")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _entry_rates(entry, period):
    def num(key):
        v = entry.get(key)
        if isinstance(v, dict):
            v = _pick(v, period)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    cached, miss, out = num("input_cached"), num("input_miss"), num("output")
    if miss is None and cached is not None:
        # 只给了缓存价：未命中价不能猜（宁可当作没配），否则会低估。
        miss = None
    if miss is None or out is None:
        return None
    if min(miss, out, cached if cached is not None else miss) < 0:
        return None
    return {"input_cached_per_million": cached,
            "input_miss_per_million": miss,
            "output_per_million": out}


# ---------------------------------------------------------------- 费率解析

def resolve_rates(provider=None, model=None, when=None, api_cfg=None):
    """解析「这一刻、这个模型」该用哪组费率。

    返回 `None` = **没有价目**（调用方必须只显示 token，不显示金额）。
    返回的 dict 带 `source`（rate 从哪来）与 `period`（高峰/空闲），页面据此说明。
    """
    api_cfg = api_cfg or {}
    period = period_of(when, api_cfg)

    def frame(rates, source, name=None, note="", effective=""):
        # `effective_date` 的形状：显式覆盖时在 `api.pricing.effective_date` 里；
        # 官方表则用"核对日期"。注意**不是** `api.pricing_effective_date` —— 早先写错过，
        # 症状是金额的生效日期静默变成空串（页面照它标「按 X 生效的价目估算」）。
        eff = str(effective or "").strip()
        if not eff:
            eff = TABLE_VERIFIED_AT if source == "table" else ""
        return {**rates, "period": period, "source": source, "model": model,
                "model_name": name or model, "currency": CURRENCY,
                "input_per_million": rates["input_miss_per_million"],
                "effective_date": eff,
                "verified_at": TABLE_VERIFIED_AT, "source_url": TABLE_SOURCE,
                "note": note}

    # ① 用户显式覆盖（老扁平写法或新写法都收在这一支，语义一致）
    #
    # **写了价目就算数**：写坏了（缺字段、非数字、负数）也**不回落到官方表**，
    # 而是返回 None = "没有可用价目"。理由：回落会让一个写错的配置静默变成"看起来
    # 正常的金额"，而按官方表算的数与用户以为的价目不是一回事 —— 这是本文件存在的
    # 整个理由（金额错了比不显示更糟）。老 `ai.pricing()` 就是这个契约。
    if "pricing" in api_cfg:
        explicit = api_cfg.get("pricing") or {}
        try:
            flat = {"input_cached_per_million": None,
                    "input_miss_per_million": float(explicit["input_per_million"]),
                    "output_per_million": float(explicit["output_per_million"])}
        except (KeyError, TypeError, ValueError):
            return None
        if min(flat["input_miss_per_million"], flat["output_per_million"]) < 0:
            return None
        hit = explicit.get("input_cached_per_million")
        try:
            flat["input_cached_per_million"] = None if hit is None else float(hit)
        except (TypeError, ValueError):
            return None
        if flat["input_cached_per_million"] is not None and flat["input_cached_per_million"] < 0:
            return None
        return frame(flat, "config", effective=explicit.get("effective_date"),
                     note="金额按 config 里显式配置的价目估算（覆盖官方表）。")

    # ② 官方表（含旧模型名 → 现役模型的别名）
    key = str(model or "").strip()
    alias = LEGACY_ALIASES.get(key)
    entry = TABLE.get(alias or key)
    if entry:
        rates = _entry_rates(entry, period)
        if rates:
            note = ""
            if alias:
                note = (f"模型名 `{key}` 已下线，官方说明由 {alias} 提供服务并按它的价格计费，"
                        f"故按 {alias} 的价目估算。")
            return frame(rates, "table", name=entry.get("name") or key, note=note)

    return None


def rate_rows(model):
    """这个模型在**两个时段**的完整费率（给设置页展示/对比用）。"""
    key = str(model or "").strip()
    entry = TABLE.get(LEGACY_ALIASES.get(key) or key)
    if not entry:
        return None
    return {
        "name": entry.get("name") or key,
        "currency": CURRENCY,
        "peak": _entry_rates(entry, "peak"),
        "offpeak": _entry_rates(entry, "offpeak"),
        "verified_at": TABLE_VERIFIED_AT,
        "source_url": TABLE_SOURCE,
        "aliases": [k for k, v in LEGACY_ALIASES.items() if v == (LEGACY_ALIASES.get(key) or key)],
    }


def estimate(usage, rates):
    """按一组费率算金额。

    **三段分开算**：输入里命中缓存的那部分按 `input_cached` 计（命中价约为未命中的
    1/50，全按未命中算会明显偏高）；没给缓存拆分时**一律按未命中**计 —— 这是上界，
    宁可报高不报低（报低会让人以为还能继续跑）。
    """
    if not rates:
        return None
    u = usage or {}
    prompt = u.get("prompt_tokens", 0) or 0
    cached = u.get("cached_tokens")
    if cached is None:
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached is None:
        cached = u.get("prompt_cache_hit_tokens")
    try:
        cached = max(0, min(int(cached or 0), int(prompt)))
    except (TypeError, ValueError):
        cached = 0
    miss = max(0, int(prompt) - cached)
    completion = u.get("completion_tokens", 0) or 0

    cached_rate = rates.get("input_cached_per_million")
    if cached_rate is None:                    # 没配缓存价：命中部分也按未命中价算
        cached_rate = rates["input_miss_per_million"]
    cost = (miss / 1e6 * rates["input_miss_per_million"]
            + cached / 1e6 * cached_rate
            + completion / 1e6 * rates["output_per_million"])
    return round(cost, 6)
