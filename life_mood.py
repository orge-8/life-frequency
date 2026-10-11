# -*- coding: utf-8 -*-
"""情绪多维化（mood）+ 社交电量（social battery）：内心维度的演化与消费（纯模块）。

**为什么需要**：现有 ``emotion`` 是倍率管线的唯一情绪输入——它被设计成「外显的
喜怒」，驱动的是「她说话多不多」。但人的内里还有两个慢变维度：

* ``stress``（压力）：加班、开会、熬夜会上调；休息、睡觉、娱乐下调。
* ``loneliness``（孤独）：长时间无人互动累积上升；被 @、被找、共同经历回落。

它们**只进 prompt 与素材，不进倍率**——这是仓库早已立下的纪律（经济维度先例 +
「不要双重抑制」注释）：倍率已经被活动、情绪、体力三层调制，再叠内心维度就是
乘法堆叠的变体。

社交电量（``social_battery``，0–10）是孤独的镜像：独处太久孤独上升、社交太累
电量下降。消费点在主动开口的硬闸（电量 < 2 直接不开口）与消息风格注入。

注入通道：``ctx.maisaka.context.append``（世界内措辞），按 ``(session_id, day_key)``
去重——**每天每个会话至多一条**，不写「她恢复正常了」（那才有系统感）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

try:  # 包式加载（Runner 真机）
    from .life_activity import is_asleep
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_activity import is_asleep  # type: ignore[no-redef]

STRESS_MAX = 10.0
LONELINESS_MAX = 10.0
BATTERY_MAX = 10.0
#: 注入阈值：达到才说「她最近压力很大」——低于它闭嘴，情绪不需要随时直播
INJECT_THRESHOLD = 7.0
#: 电量阈值：低于它直接不开口（REASON_LOW_BATTERY）
BATTERY_MIN_PROACTIVE = 2.0
#: 电量 2.0–4.0：开口分数阈值上浮（她还在挑话题，但明显没平时积极）
BATTERY_THRESHOLD_PENALTY = 0.5
#: 无互动满 6 小时：孤独 +0.5（battery 充好了，孤独来了——互为镜像）
LONELY_AFTER_HOURS = 6.0
LONELY_STEP = 0.5


@dataclass(frozen=True)
class MoodPolicy:
    """内心维度的全部可调参数（由配置映射而来）。"""

    enabled: bool = True
    #: 每次 tick（默认 10 分钟）stress 的回归步长（向 baseline 收敛）
    stress_regress_per_tick: float = 0.02
    #: 每次事件命中「累」类活动（work/meeting/overtime）的 stress 上调
    stress_per_work_tick: float = 0.05
    #: 睡眠每 tick 的 stress 下调（睡觉是最有效的解压）
    stress_relief_per_sleep_tick: float = 0.08
    #: 每 tick 无互动时 loneliness 上升（10 分钟一 tick ⇒ 一小时 +0.3）
    loneliness_per_quiet_tick: float = 0.05
    #: 被找（live_signal / 提及）时 loneliness 的回落
    loneliness_relief_per_contact: float = 0.3
    #: 社交电量：入站普通消息 -0.05 / 被 @ -0.15 / 主动开口 -0.3
    battery_per_message: float = 0.05
    battery_per_mention: float = 0.15
    battery_per_proactive: float = 0.3
    #: 打断窗口（chatting）每小时 -0.5；独处活动每小时 +0.4；睡眠每小时 +1.0
    battery_drain_per_chat_hour: float = 0.5
    battery_recover_per_solo_hour: float = 0.4
    battery_recover_per_sleep_hour: float = 1.0
    stress_baseline: float = 3.0
    loneliness_baseline: float = 4.0
    battery_baseline: float = 7.0
    #: 高压崩溃的判据阈值（v1.16.2 M3a）：``stress`` 达到它**并持续** ``hours`` 才算
    #: 「绷不住」。阈值与开关由 ``life_sim.stress_breakdown_*`` 消费，这里只负责计时。
    stress_breakdown_threshold: float = 7.0
    #: 压力回落到它以下就清掉高压起点（下一次高压重新计时）
    stress_breakdown_reset: float = 5.0

    #: 「累」类活动：这些活动在做就是在消耗心力
    DRAINING_ACTIVITIES = ("work", "meeting", "overtime", "night_study")
    #: 「独处回血」活动（方案七 §7.2）
    SOLO_ACTIVITIES = ("daze", "bath", "music", "anime", "meal")


def clamp_mood(state: Any) -> None:
    """把三个内心维度钳进合法区间（from_dict 之后调用；NaN 由 float 分支拦截）。"""

    state.stress = max(0.0, min(STRESS_MAX, float(state.stress)))
    state.loneliness = max(0.0, min(LONELINESS_MAX, float(state.loneliness)))
    state.social_battery = max(0.0, min(BATTERY_MAX, float(state.social_battery)))


def evolve(
    state: Any,
    *,
    activity: str,
    minutes: float,
    had_contact: bool,
    had_mention: bool,
    policy: MoodPolicy,
    last_contact_at: float,
    now: float,
) -> None:
    """结算一段时间的内心维度（每 tick 调一次；``minutes`` 通常 ≈ tick_seconds/60）。

    ⚠ 只在 ``state`` 上原地修改；睡眠/小睡中 stress 下调、电量回血，但**孤独不涨**
    （她睡着，不会感到孤单）。v1.15.0（PR-R1）起「在睡」统一走
    ``life_activity.is_asleep``（sleep + nap），不再写字面 ``== "sleep"``。
    """

    if not policy.enabled:
        return
    hours = max(0.0, float(minutes)) / 60.0
    if hours <= 0.0:
        return

    # ---- stress ----
    if is_asleep(activity):
        state.stress = max(0.0, state.stress - policy.stress_relief_per_sleep_tick * (minutes / 10.0))
    else:
        if activity in policy.DRAINING_ACTIVITIES:
            state.stress = min(STRESS_MAX, state.stress + policy.stress_per_work_tick * (minutes / 10.0))
        else:
            # 无事发生：缓慢向基线回归
            delta = policy.stress_regress_per_tick * (minutes / 10.0)
            if state.stress > policy.stress_baseline:
                state.stress = max(policy.stress_baseline, state.stress - delta)
            else:
                state.stress = min(policy.stress_baseline, state.stress + delta)

    # ---- 高压持续时长（v1.16.2 M3a，只计时、不产出）----
    # 崩溃事件的触发条件是「stress ≥ 阈值**持续** 2 小时」，瞬时高压不算——真人也不会
    # 因为一次加班就崩。这里维护 ``state.stress_high_since``：进入高压记起点，回落到
    # reset 阈值以下清零（下一次高压重新计时）。真正的产出在
    # ``life_sim.settle_stress_breakdown``（那里才有素材/经历/情绪与惰性期）。
    if state.stress >= policy.stress_breakdown_threshold:
        if float(getattr(state, "stress_high_since", 0.0) or 0.0) <= 0.0:
            state.stress_high_since = float(now)
    elif state.stress < policy.stress_breakdown_reset:
        state.stress_high_since = 0.0

    # ---- loneliness（睡着不涨）----
    if not is_asleep(activity):
        if had_mention or had_contact:
            state.loneliness = max(0.0, state.loneliness - policy.loneliness_relief_per_contact)
        else:
            quiet_hours = max(0.0, (float(now) - float(last_contact_at)) / 3600.0) if last_contact_at > 0 else hours
            if quiet_hours >= LONELY_AFTER_HOURS:
                state.loneliness = min(
                    LONELINESS_MAX,
                    state.loneliness + policy.loneliness_per_quiet_tick * (minutes / 10.0),
                )

    # ---- social battery ----
    if had_mention:
        state.social_battery = max(0.0, state.social_battery - policy.battery_per_mention)
    elif had_contact:
        state.social_battery = max(0.0, state.social_battery - policy.battery_per_message)
    if is_asleep(activity):
        state.social_battery = min(BATTERY_MAX, state.social_battery + policy.battery_recover_per_sleep_hour * hours)
    elif activity in policy.SOLO_ACTIVITIES:
        state.social_battery = min(BATTERY_MAX, state.social_battery + policy.battery_recover_per_solo_hour * hours)


def note_proactive_cost(state: Any, *, policy: MoodPolicy) -> None:
    """主动开口一次：电量 -0.3（找人说话也是社交，也耗电）。"""

    if policy.enabled:
        state.social_battery = max(0.0, float(state.social_battery) - policy.battery_per_proactive)


# ---------------------------------------------------------------- 消费点（阈值/prompt，不进倍率）


def battery_gate(state: Any) -> str:
    """主动开口的电量闸：返回拒绝原因；空串 = 放行。

    * ``battery < 2.0`` → 直接不开口（``low_battery``）；
    * ``2.0–4.0`` → 阈值上浮 0.5（调用方把 ``score_threshold`` 抬高）。
    """

    value = float(state.social_battery)
    if value != value:
        return ""  # NaN 按「满电」处理（宁可多说，不可永远沉默）
    if value < BATTERY_MIN_PROACTIVE:
        return "low_battery"
    if value < 4.0:
        return "battery_threshold"
    return ""


def injection_lines(state: Any) -> tuple[str, ...]:
    """当前值得注入的消息风格提示（世界内措辞；空 = 什么都不说）。

    * stress >= 7：「她最近压力很大，说话会比平时短、没什么耐心。」
    * loneliness >= 7：「她有点孤单，很希望有人陪她聊聊。」
    * battery < 3：「她今天社交得有点累，回复会简短一些。」

    ⚠ 状态回落到 4 以下**不再注入**——不写「她恢复正常了」，避免系统感。
    """

    lines: list[str] = []
    stress = float(state.stress) if state.stress == state.stress else 0.0
    lonely = float(state.loneliness) if state.loneliness == state.loneliness else 0.0
    battery = float(state.social_battery) if state.social_battery == state.social_battery else BATTERY_MAX
    if stress >= INJECT_THRESHOLD:
        lines.append("（生活状态）她最近压力很大，说话会比平时短、没什么耐心。")
    if lonely >= INJECT_THRESHOLD:
        lines.append("（生活状态）她有点孤单，很希望有人陪她聊聊。")
    if battery < 3.0:
        lines.append("（生活状态）她今天社交得有点累，回复会简短一些。")
    return tuple(lines)


def prompt_lines(state: Any) -> tuple[str, ...]:
    """活动决策 prompt 的「内心状态」节（事实风格，数字现算）。"""

    stress = float(state.stress) if state.stress == state.stress else 0.0
    lonely = float(state.loneliness) if state.loneliness == state.loneliness else 0.0
    battery = float(state.social_battery) if state.social_battery == state.social_battery else BATTERY_MAX
    return (
        f"内心压力：{stress:.1f}/10（高=容易累、容易烦）",
        f"孤独感：{lonely:.1f}/10（高=很想找人说话）",
        f"社交电量：{battery:.1f}/10（低=今天不想多聊）",
    )


__all__ = [
    "BATTERY_MAX",
    "BATTERY_MIN_PROACTIVE",
    "BATTERY_THRESHOLD_PENALTY",
    "INJECT_THRESHOLD",
    "MoodPolicy",
    "battery_gate",
    "clamp_mood",
    "evolve",
    "injection_lines",
    "note_proactive_cost",
    "prompt_lines",
]
