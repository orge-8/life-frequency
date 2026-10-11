# -*- coding: utf-8 -*-
"""打断机制（interrupt）：收到消息 → 放下手里的事回你 → 回完接着干（纯模块）。

**语义（方案六）**：真人不是按 10 分钟粒度换活动的——吃饭吃到一半手机响了，
放下筷子回消息，回完接着吃。打断就是这件事：

```text
原活动 meal ──(收到 @/私聊)──▶ chatting（interrupt_until = now + 5min）
                                    │
                       窗口结束（tick 里 expire）
                                    ▼
                              回到 meal（SOURCE_ENFORCED）
```

**触发条件（全部满足才打断）**：
1. ``[interrupt] enabled``；
2. 消息非命令、非她发的；
3. 私聊任意消息，或群聊被 @（与关系建档同判据——她只为「对她说话」放下筷子）；
4. 当前活动在可打断白名单（吃饭/发呆/看番/打游戏/听歌/日常/深夜做题/下班路上/
   通勤/睡前）。**不可打断**：sleep（有 ``at_wake_until`` 专属窗口，语义冲突）、
   sick_rest、班表相位（work/meeting/overtime/lunch——她在岗）；
5. 社交电量过低时**收窄白名单**（只回随时能放下手的轻活，方案七 §7.3 联动）；
6. 已在窗口内 → 只顺延，不反复记录。

**消费点**：``enforce`` 在窗口内保住 ``CHATTING``（不主动送她入睡/切走）、
``request_is_pointless`` 窗口内跳过、LLM 决策窗口内不问（见 ``pointless_reason``）。

⚠ ``bath`` **有意不在**白名单里：方案 §6.3 的可打断表没有它，洗澡被打断的现实感也弱
（她不会湿着手回消息）。想加就往 ``INTERRUPTIBLE_ACTIVITIES`` 里加。

⚠ **窗口内是唯一盖住入睡硬约束的地方**（``EnforcePolicy.interrupt_hold``），
所以「回消息途中被送去睡觉」这个行为是可关的，别当成 bug。
"""

from __future__ import annotations

from typing import Any

try:  # 包式加载（Runner 真机）
    from .life_activity import (
        ActivityDecision,
        ALLOWED_ACTIVITIES,
        SOURCE_ENFORCED,
        SOURCE_INTERRUPT,
    )
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_activity import (  # type: ignore[no-redef]
        ActivityDecision,
        ALLOWED_ACTIVITIES,
        SOURCE_ENFORCED,
        SOURCE_INTERRUPT,
    )

#: 打断后的活动（能量 +0.05/h——和人说话是小回血，进 ``energy_delta_per_hour``）
CHATTING = "chatting"
#: 默认窗口（分钟）。真机观察后可调；私聊可单独配更长（开放问题：先 5）
DEFAULT_WINDOW_MINUTES = 5
#: 单次打断的**总时长上限**（分钟，v1.13.1 / R4 代码审查）。活跃群里每 5 分钟
#: 有人 @ 一次就能把窗口无限顺延——她会永久停在「聊天中」，习惯表与三餐生理窗
#: 整段失效（连锁：饱腹见底、倍率长期 1.1）。超过上限后只顺延最后一窗，
#: 让窗口自然到期回退一次再被打断——语义上就是「她回完接着吃饭，你再叫她
#: 她再放下筷子」。够活跃的群 30 分钟也够回完一波了。
MAX_TOTAL_MINUTES = 30.0
#: 电量低于此值时收窄白名单（方案七 §7.3「battery < 2.0 时打断白名单收窄」）。
#: 与 ``life_mood.BATTERY_MIN_PROACTIVE`` 同值但**独立定义**：那条管「主动开口」，
#: 这条管「被打断」，语义与调参节奏都不同；绑死会让其中一个没法单独调。
NARROW_BATTERY_THRESHOLD = 2.0

#: 可打断白名单（方案六 §6.3）。⚠ ``bath`` 有意不在其中（见模块说明）。
#: v1.15.0（PR-R1）：``nap`` 加进来——小睡浅、随时能被叫醒，而且回完消息
#: 还能倒回去接着眯（这正是打断回退的语义）。``sleep`` 仍不在其中（见下）。
INTERRUPTIBLE_ACTIVITIES = frozenset({
    "meal", "daze", "anime", "game", "music", "daily", "night_study",
    "off_work", "commute", "before_sleep", "nap",
})

#: 低电量时仍可打断的活动：只剩「随时能放下手」的轻活（方案七 §7.3）。
#: 吃饭/通勤/睡前/做题都被排除——那些不是「顺手回一句」就能了事的。
NARROW_INTERRUPTIBLE_ACTIVITIES = frozenset({"daze", "music", "anime"})

#: 不可打断黑名单（写出来是为了报错信息能说人话；白名单才是判定依据）
NON_INTERRUPTIBLE_REASONS = {
    "sleep": "在睡觉（被 @ 有专属的 wake_on_at 窗口）",
    "sick_rest": "在养病",
    "work": "在岗（班表相位）",
    "meeting": "在开会（班表相位）",
    "overtime": "在加班（班表相位）",
    "lunch": "在午休（班表相位）",
}


def interruptible_activities(*, battery: float | None = None) -> frozenset[str]:
    """当前生效的可打断白名单（电量低时收窄）。

    ``battery is None`` = 不知道电量（未启用 mood 维度）⇒ 用完整白名单。
    **绝不能**因为「维度没开」而退化成「永远打不断」——那是一条静默的全局失效。
    """

    if battery is None:
        return INTERRUPTIBLE_ACTIVITIES
    try:
        value = float(battery)
    except (TypeError, ValueError):
        return INTERRUPTIBLE_ACTIVITIES
    if value != value:  # NaN：按「电量未知」处理
        return INTERRUPTIBLE_ACTIVITIES
    if value < NARROW_BATTERY_THRESHOLD:
        return NARROW_INTERRUPTIBLE_ACTIVITIES
    return INTERRUPTIBLE_ACTIVITIES


def should_interrupt(
    *,
    enabled: bool,
    activity: str,
    is_command: bool,
    is_bot_message: bool,
    private_chat: bool,
    mentioned: bool,
    battery: float | None = None,
) -> str:
    """要不要打断：返回原因串（空串 = 不打断）。

    判据与 ``life_relations.should_record`` 同源（私聊 / 被 @）——她只为
    「对她说话」放下筷子。
    """

    if not enabled:
        return ""
    if is_command or is_bot_message:
        return ""
    if not (private_chat or mentioned):
        return ""
    if activity == CHATTING:
        return "顺延"  # 已在窗口内，只顺延
    if activity not in interruptible_activities(battery=battery):
        return ""
    return "放下手里的事"


def apply_interrupt(
    state: Any,
    *,
    now: float,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> Any:
    """执行打断：记 ``interrupted_from`` → 切 CHATTING → 开窗。

    纯内存操作（零 RPC 零落盘——落盘随下一次 tick 的 ``_save_state``）。
    已在窗口内时只顺延（连续聊天不反复记录「放下手里的事」，也不刷新停留计时）。
    """

    now = float(now)
    if state.activity != CHATTING:
        state.interrupted_from = str(state.activity)
        state.activity = CHATTING
        state.activity_source = SOURCE_INTERRUPT
        state.activity_note = "被消息打断，先回人"
        state.activity_since = now
        state.scene = "正在回消息"
        state.interrupt_started_at = now
        # 方案五联动：打断期间背景活动清空（回消息不会「顺便看番」）；
        # 回退**不恢复**——背景是上一轮的决策产物，恢复会穿帮
        if getattr(state, "side_activities", ()):
            state.side_activities = []
    else:
        # v1.13.1（R4）：起点丢了（旧状态 / 字段被清）就当作「这次顺延是新的一轮」
        if not float(getattr(state, "interrupt_started_at", 0.0) or 0.0):
            state.interrupt_started_at = now

    window = max(1, int(window_minutes)) * 60.0
    started = float(getattr(state, "interrupt_started_at", 0.0) or 0.0)
    total_cap = MAX_TOTAL_MINUTES * 60.0
    if started and now - started >= total_cap:
        # v1.13.1（R4）：这次打断已回满总上限 ⇒ **不再顺延**，让窗口自然到期、
        # tick 里回退到原活动。之后每条新消息会开一次**新的**打断（上限重新计）
        # ——「她回完接着吃饭，你再叫她她再放下筷子」，而不是永久挂在聊天中。
        return state
    if started:
        state.interrupt_until = min(now + window, started + total_cap)
    else:
        state.interrupt_until = now + window
    return state


def in_interrupt_window(state: Any, *, now: float) -> bool:
    """此刻是否在打断窗口内（``interrupt_until`` 未过且字段有效）。"""

    until = float(getattr(state, "interrupt_until", 0.0) or 0.0)
    if until != until or until <= 0.0:  # NaN / 未开窗
        return False
    return float(now) < until


def expire_interrupt(state: Any, *, now: float) -> ActivityDecision | None:
    """tick 里调用：窗口结束 → 回到原活动。

    返回的 decision **仍走 enforce**（回退后她不一定接着干原活动——她可能在
    回完消息后决定出门；enforce 的停留期判定与班表照常管着她）。
    原活动失效（如状态损坏）时回 ``daily``。

    ⚠ **只在「刚过窗」的那一次 tick** 返回决策：判据是 ``interrupted_from`` 还有值。
    任何一次 tick 漏调、或宿主时钟回拨让它再过一次窗，第二次都会返回 ``None``，
    不会出现「反复弹回原活动」把她卡死的情况。
    """

    until = float(getattr(state, "interrupt_until", 0.0) or 0.0)
    if until != until:
        # NaN 窗口按「没有窗口」处理：让它继续留着等于打断态永不过期
        state.interrupt_until = 0.0
        state.interrupt_started_at = 0.0
        state.interrupted_from = ""
        return None
    if until > 0.0 and float(now) < until:
        return None  # 还在窗口内

    previous = str(getattr(state, "interrupted_from", "") or "")
    if not previous and until <= 0.0:
        return None  # 从来没有窗口（绝大多数 tick 走这条，成本最低）

    state.interrupt_until = 0.0
    state.interrupt_started_at = 0.0
    state.interrupted_from = ""
    if previous and previous in ALLOWED_ACTIVITIES and previous != CHATTING:
        return ActivityDecision(
            previous, "", SOURCE_ENFORCED, "回完消息，接着干原来的事",
        )
    return ActivityDecision(
        "daily", "", SOURCE_ENFORCED, "回完消息（原活动已失效，回到日常）",
    )


def expire_note(interrupted_from: str, label_of: Any) -> str:
    """写进 ``recent_events`` 的一句叙述（方案 §6.3「回退事件」）。

    独立出来是为了让 plugin 侧只管落库，措辞（要含活动 label）留在纯模块里，
    测试不必起插件就能断言这句话。
    """

    label = label_of(interrupted_from) if callable(label_of) else str(interrupted_from)
    return f"{label}到一半去回了会儿消息，回完接着干"


def pointless_reason(state: Any, *, now: float) -> str:
    """``request_is_pointless`` 窗口内理由的同源措辞（纯模块口径）。

    ⚠ **不是**另一条判定：真正的判据在 ``life_activity.request_is_pointless``
    （它必须与 ``enforce`` 逐字对拍，所以住在同一个模块里）。这里只是给状态卡
    与日志一个统一措辞，避免「活动决策」和「沉默台账」对同一件事说两种话。
    """

    if in_interrupt_window(state, now=now):
        return "打断窗口中（正在回消息）"
    return ""


def context_fact(interrupted_from: str, label_of: Any) -> str:
    """注入会话的世界内事实（方案六 §6.3；``inject_notice`` 开着才发）。"""

    label = label_of(interrupted_from) if callable(label_of) else str(interrupted_from)
    return f"（生活状态）她刚才在{label}，看到你说话就放下手里的事来回你了。"


__all__ = [
    "CHATTING",
    "DEFAULT_WINDOW_MINUTES",
    "INTERRUPTIBLE_ACTIVITIES",
    "MAX_TOTAL_MINUTES",
    "NARROW_BATTERY_THRESHOLD",
    "NARROW_INTERRUPTIBLE_ACTIVITIES",
    "NON_INTERRUPTIBLE_REASONS",
    "apply_interrupt",
    "context_fact",
    "expire_interrupt",
    "expire_note",
    "in_interrupt_window",
    "interruptible_activities",
    "pointless_reason",
    "should_interrupt",
]