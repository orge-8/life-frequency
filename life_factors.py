# -*- coding: utf-8 -*-
"""状态 → 发言频率倍率（纯函数）。

管线是**有序的乘法步骤 + 两个硬闸**，刻意保持「加一维 = 加一个因子」的形状，
以后要补天气 / 热点 / 音乐时不需要动核心逻辑：

    adjust = 1.0
      × 活动因子        （sleep 由硬闸直接归零）
      × 情绪因子        （按宿主模式选曲线组）
      × 体力因子
      × 健康因子        （感冒 / 熬夜上限被压低，可同时命中）
      × 日期因子        （生日 / 自定义节日）
      + 素材加成        （6 小时内的「想跟你说的素材」条数，封顶）
      → 钳到 [min_adjust, max_adjust]

**硬闸优先**：睡眠中或命中静默时段 → 直接 0.0，不看其它因子。

两个关于「不要双重抑制」的约定（都有真实故障背景）：

- 养病的活动因子是中性 **1.0**，压制只由健康因子出一次。否则 ``0.3 × 0.3 = 0.09``，
  等于把她彻底静音。
- 熬夜与感冒可以叠加（那是两种不同的不适），但各自只在对应健康因子里出一次。

**曲线按宿主模式分两套**：``frequency`` 计数门下整段只映射到 1→4 条消息，
``reply_necessity`` 评分门下则覆盖 4→13 条以及 0.6 的断崖两侧。两套都可独立调，
插件读 ``chat.reply_timing.reply_trigger_mode`` 自动选，读不到按宿主默认走计数门。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

try:  # 包式加载（Runner 真机）
    from .life_activity import DAILY, SLEEP, in_window
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_activity import DAILY, SLEEP, in_window

# ---------------------------------------------------------------- 默认值

DEFAULT_ACTIVITY_FACTORS: dict[str, float] = {
    SLEEP: 0.0,
    "sick_rest": 1.0,
    "before_sleep": 1.15,
    "night_study": 0.5,
    "music": 0.9,
    "game": 0.75,
    "anime": 0.75,
    "daze": 0.6,
    DAILY: 1.0,
}
"""活动因子。``sick_rest`` 故意中性——见模块开头的「不要双重抑制」。"""

DEFAULT_HEALTH_FACTORS: dict[str, float] = {
    "healthy": 1.0,
    "cold": 0.3,
    "sleep_deprived": 0.9,
}

DEFAULT_MOOD_CURVE: tuple[tuple[float, float], ...] = ((0.0, 0.55), (5.0, 0.95), (10.0, 1.35))
DEFAULT_ENERGY_CURVE: tuple[tuple[float, float], ...] = ((0.0, 0.5), (5.0, 0.85), (10.0, 1.25))
"""默认曲线（原提案）：情绪/体力各 0–10 分。

联合摆幅约 6.1 倍（0.275 ↔ 1.6875）。注意：在宿主的 ``reply_necessity`` 模式下，
``adjust < 0.6`` 会让纯闲聊永远推不动（那条断崖来自宿主常量，与曲线无关）；
在默认的 ``frequency`` 计数门模式下整段只映射到 1→4 条消息——想让它更明显，
把 ``curves.frequency`` 下探（README 给了参考值）。
"""


# ---------------------------------------------------------------- 行 DSL 解析


def _split_pairs(lines: object) -> tuple[list[tuple[str, str]], list[str]]:
    if isinstance(lines, str):
        candidates: list[object] = [lines]
    elif lines is None:
        candidates = []
    else:
        try:
            candidates = list(lines)
        except TypeError:
            candidates = [lines]

    pairs: list[tuple[str, str]] = []
    warnings: list[str] = []
    for line in candidates:
        raw = str(line or "").strip()
        if not raw:
            continue
        if "=" not in raw:
            warnings.append(f"{raw!r} 不是 key=value，已忽略")
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            warnings.append(f"{raw!r} 缺少键名，已忽略")
            continue
        pairs.append((key, value.strip()))
    return pairs, warnings


def parse_factor_lines(
    lines: object,
    *,
    known_keys: object = None,
    label: str = "",
) -> tuple[dict[str, float], list[str]]:
    """解析 ``["music=0.9", "sleep=0.0"]`` 形式的因子表；坏行告警跳过。

    ``known_keys`` 给定时，未知键也会**告警**（照原样保留，消费侧取不到就会静默失效，
    这是「设了没用」类问题唯一的线索）。非有限值（``nan``/``inf``/``1e400``）按坏行处理：
    ``float("nan")`` 能通过 ``float()``，但会一路污染倍率（``min(max_adjust, nan)`` 在 CPython
    下等于 ``max_adjust``，于是活动被推到最大发言频率），所以必须在这里拦住。
    """

    pairs, warnings = _split_pairs(lines)
    factors: dict[str, float] = {}
    known = {str(item) for item in known_keys} if known_keys is not None else None
    prefix = f"{label}因子" if label else "因子"
    for key, value in pairs:
        try:
            number = float(value)
        except ValueError:
            warnings.append(f"{key}={value!r} 不是数字，已忽略")
            continue
        if not math.isfinite(number):
            warnings.append(f"{key}={value!r} 不是有限数（nan/inf），已忽略")
            continue
        if known is not None and key not in known:
            warnings.append(f"{prefix}键 {key!r} 不在已知列表里，可能永远不会被用到（已保留）")
        factors[key] = number
    return factors, warnings


def parse_curve_points(lines: object) -> tuple[tuple[tuple[float, float], ...], list[str]]:
    """解析 ``["0=0.55", "5=0.95", "10=1.35"]`` 形式的分段线性曲线。

    返回按 x 升序去重的点集；少于两个点视为无效（回退默认曲线由调用方决定）。
    非有限点按坏行丢弃（``nan`` 会让整条曲线恒返回上界，见 ``parse_factor_lines``）。
    """

    pairs, warnings = _split_pairs(lines)
    points: dict[float, float] = {}
    for key, value in pairs:
        try:
            x = float(key)
            y = float(value)
        except ValueError:
            warnings.append(f"{key}={value!r} 不是数字点，已忽略")
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            warnings.append(f"{key}={value!r} 不是有限数（nan/inf），已忽略")
            continue
        points[x] = y
    ordered = tuple(sorted(points.items()))
    if ordered and len(ordered) < 2:
        warnings.append("曲线至少需要两个点，已忽略整条曲线")
        return (), warnings
    return ordered, warnings


def interpolate(curve: Sequence[tuple[float, float]], x: float) -> float:
    """分段线性插值；曲线外取端点值（不平外推）。"""

    if not curve:
        return 1.0
    if len(curve) == 1:
        return float(curve[0][1])
    value = float(x)
    if value <= float(curve[0][0]):
        return float(curve[0][1])
    if value >= float(curve[-1][0]):
        return float(curve[-1][1])
    for index in range(1, len(curve)):
        left_x, left_y = float(curve[index - 1][0]), float(curve[index - 1][1])
        right_x, right_y = float(curve[index][0]), float(curve[index][1])
        if value <= right_x:
            span = right_x - left_x
            if span <= 0:
                return right_y
            ratio = (value - left_x) / span
            return left_y + (right_y - left_y) * ratio
    return float(curve[-1][1])


# ---------------------------------------------------------------- 配置


@dataclass(frozen=True)
class CurveSet:
    """一套情绪/体力曲线（按宿主模式二选一）。"""

    mood: tuple[tuple[float, float], ...] = DEFAULT_MOOD_CURVE
    energy: tuple[tuple[float, float], ...] = DEFAULT_ENERGY_CURVE


@dataclass(frozen=True)
class FactorConfig:
    """倍率管线的全部可调参数。"""

    activity_factors: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_ACTIVITY_FACTORS)
    )
    health_factors: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_HEALTH_FACTORS)
    )
    curves_necessity: CurveSet = field(default_factory=CurveSet)
    curves_frequency: CurveSet = field(default_factory=CurveSet)
    quiet_hours: tuple[tuple[int, int], ...] = ()
    max_adjust: float = 2.0
    min_adjust: float = 0.0
    material_bonus: float = 0.15
    material_bonus_cap: float = 0.45
    sleep_debt_cap_nights: int = 3
    # 硬闸（睡眠 / 静默时段）落地时的倍率下限。0 = 真静默：宿主 _is_reply_frequency_silent()
    # 为真，不跑 Planner/Replyer，但**其它插件**的 maisaka.proactive.trigger 会被静默消费掉
    # （group-welcome 的欢迎语就是这么丢的）。>0 则不再静默，代价见 README「与其它插件共存」。
    silence_floor: float = 0.0

    def curve_set_for(self, mode: str) -> CurveSet:
        """按宿主模式取曲线组；未知模式按计数门（宿主默认）处理。"""

        return self.curves_necessity if str(mode) == "reply_necessity" else self.curves_frequency


@dataclass(frozen=True)
class AdjustBreakdown:
    """倍率的逐项拆解，给 ``/生活 频率`` 直接展示。"""

    adjust: float
    raw: float
    reason: str
    factors: tuple[tuple[str, float], ...] = ()
    material_count: int = 0
    material_bonus: float = 0.0
    curve_set: str = "frequency"

    def as_lines(self) -> list[str]:
        lines = [f"原始倍率：{self.raw:.3f}"]
        for name, value in self.factors:
            lines.append(f"  × {name} = {value:.3f}")
        if self.material_bonus:
            lines.append(f"  + 素材 {self.material_count} 条 = +{self.material_bonus:.3f}")
        lines.append(f"最终倍率：{self.adjust:.3f}")
        return lines


# ---------------------------------------------------------------- 主管线

REASON_OK = "ok"
REASON_SLEEP = "sleep"
REASON_QUIET_HOURS = "quiet_hours"


def compute_adjust(
    *,
    activity: str,
    emotion: float,
    energy: float,
    sick: bool,
    sleep_debt_nights: int,
    date_factor: float,
    material_count: int,
    now_minutes: int,
    config: FactorConfig,
    mode: str = "frequency",
) -> AdjustBreakdown:
    """算出倍率并给出逐项拆解。

    所有入参都是标量，调用方（plugin 层）负责从状态里取出来——这样这个函数
    在测试里就是一个纯粹的数值函数，不需要构造整个 state。
    """

    hard_gate_floor = max(0.0, float(config.silence_floor))
    # 上限必须同时压住两个下限：钳制写成 max(min_adjust, min(max_adjust, x)) 时，
    # min_adjust / silence_floor 会**击穿** max_adjust（v1.1.0 的 bug：
    # 「倍率上限」设 1.0 却写出 5.0）。plugin 层会在配置里发现这种组合并告警，
    # 这里做一次无条件的收口，保证任何调用路径都不会越过用户设的上限。
    ceiling = max(float(config.max_adjust), float(config.min_adjust), hard_gate_floor)

    # ---- 硬闸 1：睡眠 ----
    if activity == SLEEP:
        return AdjustBreakdown(
            adjust=min(hard_gate_floor, ceiling), raw=0.0, reason=REASON_SLEEP
        )

    # ---- 硬闸 2：静默时段 ----
    for window in config.quiet_hours:
        if in_window(now_minutes, window):
            return AdjustBreakdown(
                adjust=min(hard_gate_floor, ceiling), raw=0.0, reason=REASON_QUIET_HOURS
            )

    curve = config.curve_set_for(mode)
    curve_name = "necessity" if str(mode) == "reply_necessity" else "frequency"

    factors: list[tuple[str, float]] = []
    raw = 1.0

    activity_factor = float(config.activity_factors.get(activity, 1.0))
    factors.append((f"活动({activity})", activity_factor))
    raw *= activity_factor

    mood_factor = interpolate(curve.mood, emotion)
    factors.append((f"情绪({emotion:.1f})", mood_factor))
    raw *= mood_factor

    energy_factor = interpolate(curve.energy, energy)
    factors.append((f"体力({energy:.1f})", energy_factor))
    raw *= energy_factor

    health_factor = float(config.health_factors.get("healthy", 1.0))
    if sick:
        cold = float(config.health_factors.get("cold", 1.0))
        factors.append(("感冒", cold))
        health_factor *= cold
    if int(sleep_debt_nights) >= int(config.sleep_debt_cap_nights):
        deprived = float(config.health_factors.get("sleep_deprived", 1.0))
        factors.append(("熬夜上限被压低", deprived))
        health_factor *= deprived
    if not sick and int(sleep_debt_nights) < int(config.sleep_debt_cap_nights):
        factors.append(("健康", health_factor))
    raw *= health_factor

    if abs(float(date_factor) - 1.0) > 1e-9:
        factors.append(("日期", float(date_factor)))
        raw *= float(date_factor)

    bonus = min(
        float(config.material_bonus_cap),
        max(0.0, float(config.material_bonus)) * max(0, int(material_count)),
    )

    # 兜底：任何一条因子非有限（只可能来自被外部写坏的配置对象）都不许污染倍率
    if not math.isfinite(raw):
        raw = 1.0
        bonus = 0.0

    adjusted = max(float(config.min_adjust), min(ceiling, raw + bonus))
    if not math.isfinite(adjusted):
        adjusted = max(0.0, float(config.min_adjust))
    return AdjustBreakdown(
        adjust=adjusted,
        raw=raw,
        reason=REASON_OK,
        factors=tuple((name, value) for name, value in factors if abs(value - 1.0) > 1e-9),
        material_count=max(0, int(material_count)),
        material_bonus=bonus,
        curve_set=curve_name,
    )


def hold_baseline() -> AdjustBreakdown:
    """暂停状态下写回宿主的值（1.0 = 不干预），同时说明原因。"""

    return AdjustBreakdown(adjust=1.0, raw=1.0, reason="paused")


def reason_label(reason: str) -> str:
    return {
        REASON_OK: "正常",
        REASON_SLEEP: "睡眠中",
        REASON_QUIET_HOURS: "静默时段",
        "paused": "已暂停（写回 1.0，不干预宿主）",
    }.get(reason, reason)
