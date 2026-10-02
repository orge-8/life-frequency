# -*- coding: utf-8 -*-
"""经济维度：把「真实月度预算」当她的生活费（纯函数，不碰 ctx、不碰频率）。

数据源是 budget-pacer 的只读 API ``get_budget_status``（version 1）。本模块只做
**解析 → 净化 → 分档 → 生成提示词约束与展示行**，任何输入都不许抛异常：

    payload(dict)          ──parse_budget_status──▶ EconomySnapshot
    EconomySnapshot + 阈值 ──economy_tier────────▶ 宽裕 / 正常 / 紧张 / 见底
    EconomySnapshot + 档位 ──frugal_hint─────────▶ 喂给活动决策提示词的一段约束
    EconomySnapshot + 档位 ──economy_lines───────▶ ``/生活`` 状态卡上的几行

**为什么经济维度不参与倍率**：宿主每个会话只有一个频率标量
（``frequency.set_adjust`` 后写覆盖先写），而 budget-pacer 已经因为「花费超前于
时间进度」在压它。如果这里再因为「余额低」乘一次，就是**同一原因双重压制**——
这跟本插件既有的「不要双重抑制」纪律是同一回事。所以经济只改变她的**行为**
（活动规划里省钱、犹豫、吃泡面）与展示，压制始终由预算插件独家负责。

口径（与 budget-pacer 的 API 对齐）：

- 生活费 = 月度预算，**每月 1 日到账**（自然月窗口，budget-pacer 跨月自动重置）；
- 每天开销 = 当日真实模型花费，从余额里扣；
- 余额 = 生活费 − 本月已花（可为负 = 已超支）；
- 档位还会参考**节奏比**：余额还剩不少但花得比时间进度快，同样算「手头紧」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------- 档位

TIER_UNKNOWN = "unknown"
TIER_ROOMY = "宽裕"
TIER_NORMAL = "正常"
TIER_TIGHT = "紧张"
TIER_BROKE = "见底"

#: 单次展示的金额保留位数（元）
MONEY_DIGITS = 2


def _finite(value: Any, fallback: float = 0.0) -> float:
    """把任意输入转成有限浮点；坏值（含 nan/inf/字符串）一律用兜底值。

    这里刻意捕获 ``Exception`` 而不是只捕 ``TypeError``/``ValueError``：负载来自
    另一个插件的 msgpack 返回值，一个字段坏掉不该让整个生活循环抛出去
    （``__float__`` 抛异常的怪对象在真机上不可达，但防护的成本是零）。
    """

    try:
        number = float(value)
    except Exception:  # noqa: BLE001
        return fallback
    return number if math.isfinite(number) else fallback


def _text(value: Any, limit: int = 60) -> str:
    """把任意输入压成短文本，去掉换行（日志/展示里单行）。"""

    try:
        raw = str(value or "")
    except Exception:  # noqa: BLE001 —— __str__ 抛异常的怪对象也不许炸
        return ""
    cleaned = " ".join(raw.split())
    return cleaned[:limit]


# ---------------------------------------------------------------- 快照


@dataclass(frozen=True)
class EconomySnapshot:
    """一次取数的结果。``ok=False`` 时除 ``reason`` / ``error`` 外都不具有意义。"""

    ok: bool = False
    active: bool = False
    reason: str = "unavailable"
    error: str = ""
    month: str = ""
    budget: float = 0.0
    spend: float = 0.0
    today_spend: float = 0.0
    remaining: float = 0.0
    remaining_ratio: float = 0.0
    spend_ratio: float = 0.0
    progress: float = 0.0
    pacing: float = 0.0
    adjust: float = 1.0
    updated_at: float = 0.0
    stale_seconds: float = -1.0
    source: str = ""
    fetched_at: float = 0.0

    def age_seconds(self, now: float) -> float:
        """本快照在本地存了多久（秒）。``fetched_at`` 为 0 时返回 ``-1``。"""

        if self.fetched_at <= 0:
            return -1.0
        return max(0.0, _finite(now, 0.0) - self.fetched_at)

    def is_stale(self, now: float, stale_after_seconds: float) -> bool:
        """本地数据是否已过期（过期后不拿它约束她的行为，只在卡片上带 ⚠ 展示）。

        ``stale_after_seconds <= 0`` 表示**关闭过期检查**（永不过期）；
        没有取数时间戳（``fetched_at <= 0``）则视为不可信。
        """

        age = self.age_seconds(now)
        if age < 0:
            return True
        limit = _finite(stale_after_seconds, 0.0)
        if limit <= 0:
            return False
        return age > limit


def unavailable(reason: str, error: str = "", *, fetched_at: float = 0.0) -> EconomySnapshot:
    """构造一个「没拿到数据」的快照。"""

    return EconomySnapshot(
        ok=False, active=False, reason=_text(reason, 40) or "unavailable",
        error=_text(error, 200), fetched_at=_finite(fetched_at, 0.0),
    )


#: 缺了这些键就不算有效负载（budget-pacer 契约：改名等于破坏兼容）
REQUIRED_KEYS = ("active", "budget", "spend", "remaining")


def parse_budget_status(
    payload: Any,
    *,
    fetched_at: float = 0.0,
) -> EconomySnapshot:
    """把 ``api.call`` 的返回值解析成快照；任何形状异常都返回 ``ok=False`` 的快照。

    ⚠ 跨插件调用失败时 SDK **不抛异常**：Host 返回
    ``{"success": False, "error": "..."}``，而 SDK 只对带 ``result`` 键的成功包装
    做解包（``maibot_sdk/context.py:282-285``），所以失败字典会原样落到这里。
    必须先判这个形状，否则会把 ``error`` 文本当成预算数据。
    """

    fetched = _finite(fetched_at, 0.0)
    if not isinstance(payload, dict):
        return unavailable("bad_payload", f"返回类型 {type(payload).__name__} 不是字典", fetched_at=fetched)

    if payload.get("success") is False:
        return unavailable("api_error", _text(payload.get("error") or "API 调用失败", 200), fetched_at=fetched)

    if payload.get("ok") is False:
        return unavailable(
            _text(payload.get("reason") or "not_ok", 40),
            _text(payload.get("error") or "", 200),
            fetched_at=fetched,
        )

    missing = [key for key in REQUIRED_KEYS if key not in payload]
    if missing:
        return unavailable("bad_payload", "缺字段：" + "、".join(sorted(missing)), fetched_at=fetched)

    month = _text(payload.get("month"), 16)
    if month and not _looks_like_month(month):
        return unavailable("bad_payload", f"月份格式异常：{month}", fetched_at=fetched)

    budget = max(0.0, _finite(payload.get("budget"), 0.0))
    spend = max(0.0, _finite(payload.get("spend"), 0.0))
    today_spend = max(0.0, _finite(payload.get("today_spend"), 0.0))
    remaining = _finite(payload.get("remaining"), budget - spend)
    remaining_ratio = _finite(
        payload.get("remaining_ratio"), (remaining / budget) if budget > 0 else 0.0
    )
    spend_ratio = _finite(payload.get("spend_ratio"), (spend / budget) if budget > 0 else 0.0)
    pacing = _finite(payload.get("pacing"), 0.0)

    return EconomySnapshot(
        ok=True,
        active=bool(payload.get("active")),
        reason=_text(payload.get("reason") or "unknown", 40),
        error="",
        month=month,
        budget=budget,
        spend=spend,
        today_spend=today_spend,
        remaining=remaining,
        # 比例只用于分档，夹到 [-10, 10]：配置被写坏时也别让它翻天
        remaining_ratio=_clamp(remaining_ratio, -10.0, 10.0),
        spend_ratio=_clamp(spend_ratio, 0.0, 10.0),
        progress=_clamp(_finite(payload.get("progress"), 0.0), 0.0, 1.0),
        pacing=_clamp(pacing, 0.0, 100.0),
        adjust=_clamp(_finite(payload.get("adjust"), 1.0), 0.0, 100.0),
        updated_at=_finite(payload.get("updated_at"), 0.0),
        stale_seconds=_finite(payload.get("stale_seconds"), -1.0),
        source=_text(payload.get("source"), 20),
        fetched_at=fetched,
    )


def _clamp(value: float, low: float, high: float) -> float:
    if low > high:
        low, high = high, low
    return max(low, min(high, value))


def _looks_like_month(text: str) -> bool:
    """``YYYY-MM`` 且月份在 1~12。"""

    parts = text.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        return False
    return parts[0].isdigit() and parts[1].isdigit() and 1 <= int(parts[1]) <= 12


# ---------------------------------------------------------------- 分档


@dataclass(frozen=True)
class EconomyThresholds:
    """分档阈值（都可在 ``[economy]`` 配置里改）。"""

    broke_ratio: float = 0.10
    tight_ratio: float = 0.30
    roomy_ratio: float = 0.60
    tight_pacing: float = 1.20
    pacing_from_spend_ratio: float = 0.50
    """花费进度达到它之后，才允许**节奏比**把档位升级为「紧张」。

    月初的时间进度被预算插件的 ``p_floor_days`` 兜在 1 天上，于是「花了预算的
    2%」就能算出节奏比 2.0——钱几乎没动却被判成手头紧。真机上这是系统性误报，
    所以节奏比只在「已经花掉一半以上」时才作为升级信号。
    """

    def normalized(self) -> "EconomyThresholds":
        """收口非法组合：``0 <= 见底 <= 紧张 <= 宽裕 <= 1``，节奏比阈值下限 1.0。

        配置是用户手写的，出现 ``broke > tight`` 之类颠倒不能让档位变成随机的：
        排序一次，谁大谁就是更宽松的那一档。
        """

        broke = _clamp(_finite(self.broke_ratio, 0.10), 0.0, 1.0)
        tight = _clamp(_finite(self.tight_ratio, 0.30), 0.0, 1.0)
        roomy = _clamp(_finite(self.roomy_ratio, 0.60), 0.0, 1.0)
        low, mid, high = sorted((broke, tight, roomy))
        return EconomyThresholds(
            broke_ratio=low,
            tight_ratio=mid,
            roomy_ratio=high,
            tight_pacing=max(1.0, _finite(self.tight_pacing, 1.20)),
            pacing_from_spend_ratio=_clamp(
                _finite(self.pacing_from_spend_ratio, 0.50), 0.0, 1.0
            ),
        )


def economy_tier(
    snapshot: EconomySnapshot,
    thresholds: EconomyThresholds | None = None,
    *,
    now: float = 0.0,
    stale_after_seconds: float = 0.0,
) -> str:
    """给快照分档：``宽裕 / 正常 / 紧张 / 见底``；不可用或已过期时返回 ``unknown``。

    档位只由**余额**与（有门槛的）**节奏比**决定，不看倍率——倍率是预算插件的事。
    """

    if not snapshot.ok or not snapshot.active:
        return TIER_UNKNOWN
    if snapshot.is_stale(now, stale_after_seconds):
        return TIER_UNKNOWN

    limits = (thresholds or EconomyThresholds()).normalized()
    if snapshot.remaining_ratio <= limits.broke_ratio or snapshot.remaining <= 0:
        return TIER_BROKE
    if snapshot.remaining_ratio <= limits.tight_ratio:
        return TIER_TIGHT
    if (
        snapshot.pacing >= limits.tight_pacing
        and snapshot.spend_ratio >= limits.pacing_from_spend_ratio
    ):
        return TIER_TIGHT
    if snapshot.remaining_ratio >= limits.roomy_ratio:
        return TIER_ROOMY
    return TIER_NORMAL


# ---------------------------------------------------------------- 行为约束


def frugal_hint(snapshot: EconomySnapshot, tier: str) -> str:
    """手头紧时给活动决策提示词的一段约束；不需要约束时返回空串。

    刻意写「别主动说缺钱」：照设定图，她**心里有数但不会跟人哭穷**。
    这段文本会被拼进提示词，属于不可信输入的对照组——里面不含任何来自聊天内容的数据。
    """

    if tier == TIER_BROKE:
        return (
            f"【手头】这月生活费只剩 {_money(snapshot.remaining)} 元（已花 {_pct(snapshot.spend_ratio)}），"
            "基本见底了：别安排任何要花钱的活动，在家吃泡面/剩饭、宅着看书看番、"
            "出门也只是走走；买东西先反复犹豫。她心里有数，但**不会主动跟人说缺钱**，"
            "别把「没钱」挂在嘴边。"
        )
    if tier == TIER_TIGHT:
        return (
            f"【手头】这月生活费剩 {_money(snapshot.remaining)} 元（余额 {_pct(snapshot.remaining_ratio)}，"
            f"花费进度 {_pct(snapshot.spend_ratio)}、时间进度 {_pct(snapshot.progress)}）："
            "花钱的事先往后放——想吃外卖会犹豫一下、能在家解决就在家解决、买东西先看价格。"
            "她不会主动跟人哭穷。"
        )
    return ""


# ---------------------------------------------------------------- 展示


def reason_label(reason: str) -> str:
    """budget-pacer 的判定原因 → 中文短语（纯展示）。"""

    return {
        "on_track": "节奏正常",
        "brake": "花费超前，正在压低",
        "coast": "花费滞后（未启用加频）",
        "boost": "花费滞后，正在放松",
        "over_budget": "已达预算，进入闸门",
        "budget_disabled": "未设置预算",
        "disabled": "预算插件未启用",
        "paused": "调节已暂停",
    }.get(str(reason or ""), str(reason or "未知"))


def economy_lines(
    snapshot: EconomySnapshot | None,
    tier: str,
    *,
    now: float = 0.0,
    stale_after_seconds: float = 0.0,
) -> list[str]:
    """``/生活`` 状态卡里的经济几行。任何情况下都返回至少一行，保证可诊断。"""

    if snapshot is None or not snapshot.ok:
        detail = snapshot.error if snapshot is not None else ""
        suffix = f"：{detail}" if detail else ""
        return [f"经济：未接入预算插件（{_reason_or_unavailable(snapshot)}）{suffix}"]

    label = tier if tier != TIER_UNKNOWN else "未知"
    lines = [
        f"经济：余额 {_money(snapshot.remaining)} 元 / 生活费 {_money(snapshot.budget)} 元"
        f"（{label}）",
        f"　本月已花 {_money(snapshot.spend)} 元（{_pct(snapshot.spend_ratio)}）"
        f"　今日 {_money(snapshot.today_spend)} 元",
    ]
    extras: list[str] = []
    if not snapshot.active:
        extras.append(reason_label(snapshot.reason))
    else:
        extras.append(f"节奏比 {snapshot.pacing:.2f}（{reason_label(snapshot.reason)}）")
    if snapshot.is_stale(now, stale_after_seconds):
        age = snapshot.age_seconds(now)
        extras.append(f"⚠ 数据已过期（{_age_text(age)}）")
    lines.append("　" + "　".join(extras) + "　| 生活费每月 1 日到账")
    return lines


def _reason_or_unavailable(snapshot: EconomySnapshot | None) -> str:
    if snapshot is None:
        return "尚未取数"
    return reason_label(snapshot.reason) if snapshot.reason else "不可用"


def _money(value: float) -> str:
    return f"{_finite(value, 0.0):.{MONEY_DIGITS}f}"


def _pct(ratio: float) -> str:
    return f"{_finite(ratio, 0.0) * 100:.1f}%"


def _age_text(seconds: float) -> str:
    if seconds < 0:
        return "从未取到"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    return f"{seconds / 3600:.1f} 小时前"
