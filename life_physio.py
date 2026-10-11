# -*- coding: utf-8 -*-
"""生理锚点（physio）：饱腹结算 + 三餐 / 洗澡时间窗（纯模块，无 ctx、无 IO）。

**为什么需要这一层**：现有 14 个活动里没有「吃饭」——``lunch`` 是班表的午休相位
（不上班就没有），模型偶尔在 scene 里写一句「在吃东西」，但那只是文字，没有任何
机制保证她一天真的吃了三顿。真人会饿、会到点想吃饭，这是最基础的生理锚点。

设计（方案一，v2 无决策变更）：

* ``satiety``（饱腹 0–10）随**清醒时间**线性下降（睡着不饿），进餐回满；
* 三餐 / 洗澡是**时间窗**（半开区间，支持跨午夜），命中即出 proposal——
  **仍要过 enforce**，睡着的她不会被「该吃早饭了」叫醒；
* 命中状态由调用方记账（同一天同一种生理事件只出一次 proposal），
  本模块只判定「此刻在不在窗口里」与「饱腹该怎么结算」，不记任何状态。

与班表的关系：``lunch``（午休）保留为班表相位；physio 的「午餐」窗口命中时
优先出 ``meal``（吃饭是生理事件），``SCHEDULE_LUNCH`` 相位的限制矩阵本就
不禁 ``meal``，无需改矩阵。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

try:  # 包式加载（Runner 真机）
    from .life_activity import (
        BATH,
        COLD_RECOVERING,
        COLD_WORSENING,
        MEAL,
        SOURCE_PHYSIO,
        ActivityDecision,
        in_window,
        parse_bool_flag,
        parse_weekday_set,
        parse_window,
    )
    from .life_events import sanitize_text
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_activity import (  # type: ignore[no-redef]
        BATH,
        COLD_RECOVERING,
        COLD_WORSENING,
        MEAL,
        SOURCE_PHYSIO,
        ActivityDecision,
        in_window,
        parse_bool_flag,
        parse_weekday_set,
        parse_window,
    )
    from life_events import sanitize_text  # type: ignore[no-redef]

SATIETY_MAX = 10.0
#: 饱腹低于这条线的「加餐」proposal 才会出现（睡过早餐的补偿，weight 降档——开放问题已拍板）
SATIETY_SNACK_THRESHOLD = 2.5
SCENE_MAX_CHARS = 40

#: 生理窗的已知修饰符（v1.17.0，PR-PHY-3）。写错键必须告警——静默忽略一个
#: 「工作日才吃午饭」的修饰符，用户只会看到「她周末也在 12 点吃饭」而毫无线索。
_KNOWN_MEAL_KEYS = frozenset(
    {"weight", "scene", "days", "workday_only", "holiday_only"}
)


@dataclass(frozen=True)
class MealWindow:
    """一个生理时间窗（解析后的形态）。"""

    start: int
    end: int
    label: str
    kind: str
    """``meal`` = 三餐 / ``bath`` = 洗澡 / ``snack`` = 加餐。"""

    weight: float = 1.0
    scene: str = ""

    days: tuple[int, ...] = ()
    """空 = 每天；否则只在列出的星期生效（与习惯行同一套语义）。"""

    workday_only: bool = False
    holiday_only: bool = False

    def matches(self, now_minutes: int) -> bool:
        return in_window(int(now_minutes), (int(self.start), int(self.end)))

    def matches_day(self, *, weekday: int = 0, is_workday: bool = True,
                    is_holiday: bool = False) -> bool:
        """这一天这个窗有没有资格生效（星期 / 工作日 / 节假日三项，v1.17.0）。

        与习惯行的 ``line_matches_day`` **同语义**：上班日的午餐 12:00、休息日
        13:30 才吃，这样才写得出来。
        """

        if self.days and int(weekday or 0) not in self.days:
            return False
        if self.workday_only and not bool(is_workday):
            return False
        if self.holiday_only and not bool(is_holiday):
            return False
        return True


def parse_meal_lines(
    lines: object,
    *,
    default_kind: str = "meal",
) -> tuple[tuple[MealWindow, ...], list[str]]:
    """解析 ``[physio].meals`` 的行 DSL：``"07:00-08:30|早餐|meal|weight=0.9"``。

    字段：时间窗 | 名称 | 类型（可选，默认 ``meal``） | 修饰符。
    类型支持 ``meal`` / ``bath`` / ``snack``；坏行告警跳过。

    修饰符（v1.17.0，PR-PHY-3，与习惯行同一套语义）：``weight``（0–1）／
    ``scene``（场景文本）／``days``（``1-5`` / ``六日`` / ``1,3,5``，空 = 每天）／
    ``workday_only`` ／ ``holiday_only``。未知修饰符告警——「工作日才吃午饭」
    这种意图被静默吞掉，现场只会表现成「她周末也在 12 点吃饭」。
    """

    if isinstance(lines, str):
        candidates: list[object] = [lines]
    elif lines is None:
        candidates = []
    else:
        try:
            candidates = list(lines)
        except TypeError:
            candidates = [lines]

    parsed: list[MealWindow] = []
    warnings: list[str] = []
    for line in candidates:
        raw = str(line or "").strip()
        if not raw:
            continue
        fields = [chunk.strip() for chunk in raw.split("|")]
        window = parse_window(fields[0] if fields else "", default=(-1, -1))
        if window[0] < 0 or window[1] < 0:
            warnings.append(f"生理窗 {raw!r} 的时间不是 HH:MM-HH:MM，整行跳过")
            continue
        if window[0] == window[1]:
            warnings.append(f"生理窗 {raw!r} 起止相同（零长度永不命中），整行跳过")
            continue
        label = sanitize_text(fields[1] if len(fields) > 1 else "", max_chars=24)
        if not label:
            warnings.append(f"生理窗 {raw!r} 缺少名称，整行跳过")
            continue
        kind = str(fields[2] if len(fields) > 2 else default_kind).strip().lower()
        if kind not in ("meal", "bath", "snack"):
            warnings.append(
                f"生理窗 {label}: 类型 {kind!r} 不是 meal/bath/snack，按 meal 处理"
            )
            kind = "meal"
        payload: dict[str, str] = {}
        for chunk in fields[3:]:
            if "=" in chunk:
                key, value = chunk.split("=", 1)
                payload[key.strip().lower()] = value.strip()
        weight = 1.0
        if "weight" in payload:
            try:
                weight = max(0.0, min(1.0, float(payload["weight"])))
            except ValueError:
                warnings.append(f"生理窗 {label}: weight={payload['weight']!r} 不是数字，按 1.0 处理")
                weight = 1.0

        # v1.17.0（PR-PHY-3）：日期修饰符——解析核心与习惯行/班表共用一份
        days, day_warnings = parse_weekday_set(payload.get("days", ""))
        if days is None and day_warnings:
            warnings.append(
                f"生理窗 {label}: days={payload.get('days')!r} 认不出来，整行跳过"
            )
            continue
        if day_warnings:
            warnings.append(f"生理窗 {label}: {'；'.join(day_warnings)}")

        workday_only, ok = parse_bool_flag(payload.get("workday_only", ""), False)
        if not ok and "workday_only" in payload:
            warnings.append(
                f"生理窗 {label}: workday_only={payload['workday_only']!r} 认不出来，按关闭处理"
            )
        holiday_only, ok = parse_bool_flag(payload.get("holiday_only", ""), False)
        if not ok and "holiday_only" in payload:
            warnings.append(
                f"生理窗 {label}: holiday_only={payload['holiday_only']!r} 认不出来，按关闭处理"
            )
        if workday_only and holiday_only:
            warnings.append(f"生理窗 {label}: workday_only 与 holiday_only 同时为真，这一行将永不命中")

        for key in payload:
            if key not in _KNOWN_MEAL_KEYS:
                warnings.append(f"生理窗 {label}: 未知修饰符 {key}={payload[key]!r}，已忽略")

        parsed.append(
            MealWindow(
                start=int(window[0]),
                end=int(window[1]),
                label=label,
                kind=kind,
                weight=weight,
                scene=sanitize_text(payload.get("scene", ""), max_chars=SCENE_MAX_CHARS),
                days=tuple(sorted(days)) if days else (),
                workday_only=workday_only,
                holiday_only=holiday_only,
            )
        )
    return tuple(parsed), warnings


@dataclass(frozen=True)
class PhysioFacts:
    """饱腹结算需要的事实（纯数据）。"""

    satiety: float = 8.0
    awake_minutes: int = 0
    asleep: bool = False


def settle_satiety(state_satiety: float, *, minutes: float, asleep: bool,
                   decay_per_hour: float) -> float:
    """结算一段时间的饱腹：**清醒才饿**，睡觉时不消耗（半夜不会饿醒）。

    返回新值（钳在 0–10）；不修改任何外部状态。
    """

    satiety = float(state_satiety)
    if satiety <= 0.0 and not (0.0 < satiety):
        # NaN/inf 等：按「满腹」处理（宁可少一次加餐，不可永远加餐）
        return SATIETY_MAX
    if asleep:
        return satiety
    drained = satiety - max(0.0, float(decay_per_hour)) * (max(0.0, float(minutes)) / 60.0)
    return max(0.0, min(SATIETY_MAX, drained))


def eat_amount(state_satiety: float, *, floor: float = 0.65, rescue: float = 4.0) -> float:
    """吃一顿的回饱量：回饱到「至少 {floor} 成饱」与「当前 + {rescue}」的较大者。

    轻度饿（6.0）→ 直接拉到 {floor} 成；饿透（0.5）→ 至少 +4（线性只加一点的话
    她永远在挨饿，那不是人）。坏值按「比较饿」处理。
    """

    satiety = float(state_satiety)
    if satiety != satiety or satiety < 0.0 or satiety > SATIETY_MAX:
        satiety = 3.0
    target = max(satiety + max(0.0, float(rescue)), SATIETY_MAX * max(0.0, min(1.0, float(floor))))
    return max(0.0, min(SATIETY_MAX, target))


def active_windows(
    windows: Sequence[MealWindow],
    now_minutes: int,
    *,
    weekday: int = 0,
    is_workday: bool = True,
    is_holiday: bool = False,
) -> tuple[MealWindow, ...]:
    """此刻落在窗口里、且**今天有资格生效**的生理事件（按配置顺序）。

    v1.17.0（PR-PHY-3）：``weekday`` / ``is_workday`` / ``is_holiday`` 由调用方
    给（plugin 侧已有现成的 ``_routine_day_flags``，日历优先）。不传 = 旧行为
    （只有不带日期修饰符的窗会命中，因为它们的三项判据都不拦）。
    """

    minute = int(now_minutes) % 1440
    return tuple(
        item
        for item in windows
        if item.matches(minute)
        and item.matches_day(
            weekday=weekday, is_workday=is_workday, is_holiday=is_holiday
        )
    )


def proposal_for(
    window: MealWindow, *, now: float, sick_stage: str = ""
) -> ActivityDecision:
    """生理窗命中 → 一个 proposal。**仍要过 enforce**（睡着不吃饭）。

    ``sick_stage``（v1.14.0）：生病期放行三餐后，饭的场景要跟着病程走——
    加重期是「喝点粥」，好转期才有胃口。留空 = 健康，与加这个参数之前完全一致。
    """

    if window.kind == "bath":
        activity = BATH
        scene = window.scene or "洗澡"
        note = window.label
    else:
        activity = MEAL
        if window.scene:
            scene = window.scene
        elif sick_stage == COLD_WORSENING:
            scene = "喝点粥，吃点清淡的"
        elif sick_stage == COLD_RECOVERING:
            scene = "有胃口了，正经吃一顿"
        else:
            scene = f"吃{window.label}" if window.label else "吃饭"
        note = "加餐（饿过头了）" if window.kind == "snack" else window.label
    return ActivityDecision(
        activity=activity,
        scene=scene[:SCENE_MAX_CHARS],
        source=SOURCE_PHYSIO,
        note=f"生理：{note}",
    )


def need_snack(satiety: float, *, threshold: float = SATIETY_SNACK_THRESHOLD) -> bool:
    """饿到阈值以下才出「加餐」proposal（睡过早餐的补偿，weight 降档）。"""

    value = float(satiety)
    if value != value or value < 0.0 or value > SATIETY_MAX:
        return False
    return value < float(threshold)


__all__ = [
    "MEAL",
    "BATH",
    "PhysioFacts",
    "SATIETY_MAX",
    "SATIETY_SNACK_THRESHOLD",
    "MealWindow",
    "active_windows",
    "eat_amount",
    "need_snack",
    "parse_meal_lines",
    "proposal_for",
    "settle_satiety",
]
