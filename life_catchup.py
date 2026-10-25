# -*- coding: utf-8 -*-
"""睡醒补答（catchup）：她睡着时进来的消息，醒来之后怎么处理（纯模块）。

**要解决的问题**（v1.23.0）：她睡觉时频率被硬闸压成 0，宿主的静默轮会把消息吃掉
——连 `@` 也叫不动（`wake_on_at` 是唯一例外）。于是「有人半夜找过她」这件事在她
的记忆里是不存在的：醒了之后既不提、也不知道。补答就是把这件事补回去。

```text
她睡着 ──私聊 / 群 @──▶ sleep_backlog（SQLite，含原文）
                              │
   自然醒（sleep → 非 sleep）──┴──▶ ① 主动：每个有积压的会话各触发一次 Planner
                                  ② 被动：没被触发的，注入她下一次回复的 extra_prompt
```

**三条纪律**（每一条都对应一个真实的失败形态）：

1. **勿扰语句不入队**：对方说「别回了」「继续睡」，醒来后**不能**拿这句去补答
   ——那等于告诉他「你的话我记着，但我没听」。判据见 ``is_do_not_disturb``。
2. **群聊只在被 @ 时入队**：群里其余消息与她无关，攒起来补答就是刷屏。
3. **补答是「触发 Planner」，不是「插件发消息」**：插件只提供「谁在什么时候说过什么」，
   措辞由宿主 Planner 决定；它有权沉默，所以配额在**核实说出去了**之后才计
   （与 ``[proactive].confirm_send`` 同源）。

**为什么不复用 ``life_proactive`` 的评分**：那套分数衡量的是「她此刻想不想说话」，
而补答要回答的是「这段对话断在半截，该由她接上」。硬塞进同一个评分会让「刚睡醒」
这个状态反复压过「有人真的在等她」。所以补答有独立的闸，只共用 ``_trigger_proactive``
这条发送通道。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: 「明确勿扰」句式。命中即**不入队**——醒来后提起这句是二次伤害。
#: 与 ``life_proactive`` 的入站筛选口径独立：那条管「要不要回」，这条管「要不要记住」。
_DND_PATTERN = re.compile(r"(?:别回|不用回|不要回|继续睡|别醒|别打扰|安心睡|不用理我|别烦我|别吵)")

#: 记录路径的判定结果（非空 = 不记录）。写出来是为了日志能说人话。
REASON_AWAKE = "她醒着"
REASON_IN_WAKE_WINDOW = "在唤醒窗口内"
REASON_COMMAND = "命令"
REASON_FROM_BOT = "她自己发的"
REASON_NOT_ADDRESSED = "不是对她说的"
REASON_DND = "对方明确说了别打扰"
REASON_EMPTY = "空消息"
REASON_OWNER_WOKE = "能叫醒她的人发的（走唤醒链）"
REASON_OK = ""

#: 触发的三种去向。
TARGET_ACTIVE = "active"   # 主动：结束睡眠时触发一次 Planner
TARGET_PASSIVE = "passive"  # 被动：注入她下一次回复的提示词
TARGET_SKIP = "skip"

SKIP_LABELS: dict[str, str] = {
    "disabled": "补答未启用",
    "no_backlog": "没有待补的留言",
    "too_late": "迟到太久，只做被动回填",
    "per_wake": "本次睡醒的补答名额已用完",
    "per_person": "这个人今天已经被补过",
    "global_daily": "补答今天的总名额已用完",
    "not_active": "主动补答未启用（只做被动回填）",
}


# ---------------------------------------------------------------- 取数


def _as_float(value: object, default: float = 0.0) -> float:
    """宽松取数：``life_state.json`` / SQLite 都可能被外部写坏，这里绝不抛。"""

    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number and abs(number) != float("inf") else default
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return default
        return number if number == number and abs(number) != float("inf") else default
    return default


def _as_int(value: object, default: int = 0) -> int:
    return int(_as_float(value, float(default)))


def compact(text: object) -> str:
    """压掉所有空白：匹配勿扰句式时不能因为「别 回 了」漏判。"""

    return re.sub(r"\s+", "", str(text or "").lower())


def is_do_not_disturb(text: object) -> bool:
    """这句话是不是在要求「别打扰」。空串不算。"""

    raw = compact(text)
    if not raw:
        return False
    return bool(_DND_PATTERN.search(raw))


def excerpt(text: object, *, max_chars: int) -> str:
    """截断一条原文。``max_chars<=0`` = 不截断（与配置里其它「0 即关」一致）。"""

    clean = str(text or "").strip()
    limit = int(max_chars)
    if limit <= 0:
        return clean
    return clean if len(clean) <= limit else clean[:limit] + "…"


# ---------------------------------------------------------------- 配置


@dataclass(frozen=True)
class CatchupRules:
    """补答的全部可调参数（由 ``[catchup]`` 节映射而来）。

    默认值刻意保守：``active_enabled`` 关着，先让被动回填在真机上跑一段时间，
    确认「她确实会提到睡前的留言」再打开主动触发。
    """

    enabled: bool = True
    #: 主动补答（结束时触发 Planner）。关 = 只做被动回填。
    active_enabled: bool = False
    #: 一次睡醒最多主动补几个会话（用户口径：每个有积压的会话各触发一次，但要有上限）。
    per_wake_max: int = 5
    #: 同一个人每天最多被主动补一次。
    per_person_daily: int = 1
    #: 补答自己的每日总上限（与 ``[proactive].daily_max`` **分开计数**）。0 = 不设上限。
    global_daily_max: int = 6
    #: 迟到超过它（小时）就不再主动补，只留被动回填。0 = 不设上限。
    late_degrade_hours: float = 6.0
    #: 每人最多保留的原文条数（合并时只留最近的几条）。
    keep_per_person: int = 5
    #: 单条原文的截断长度。
    max_chars: int = 200
    #: 条目总保留时长（小时）：超过即删，避免「重启后补上个星期的留言」。
    retention_hours: float = 24.0
    #: 塞给 Planner / 回填的原文条数上限。**默认 0 = 完全不复述**（v1.23.1 口径 A）：
    #: 消息本来就在宿主 ``_chat_history`` 里，再说一遍会让同一句话出现两次。
    #: 调大它就回到「兜底模式」——只在担心历史被裁掉时才需要。
    requote_limit: int = 0
    #: 错开窗口（秒）：多个会话同一次睡醒命中时，按它随机散开，避免同刻多个主动任务。
    stagger_max_seconds: int = 120
    #: 人设摘要塞进 intent 的字数上限。0 = 不塞。
    persona_chars: int = 200


# ---------------------------------------------------------------- 记录


def should_record(
    *,
    text: object,
    is_command: bool,
    is_from_bot: bool,
    asleep: bool,
    in_wake_window: bool,
    private_chat: bool,
    mentioned: bool,
) -> str:
    """这条消息要不要进积压。返回非空原因串 = **不记录**。

    ⚠ 判据顺序有意义：``asleep`` 在最前（她醒着的时候一切照常），``in_wake_window``
    紧随其后（窗口内她真的看得见，记录会让她醒来后重复回应同一句话）。
    """

    if not asleep:
        return REASON_AWAKE
    if in_wake_window:
        return REASON_IN_WAKE_WINDOW
    if is_command:
        return REASON_COMMAND
    if is_from_bot:
        return REASON_FROM_BOT
    if not (private_chat or mentioned):
        return REASON_NOT_ADDRESSED
    if is_do_not_disturb(text):
        return REASON_DND
    if not str(text or "").strip():
        return REASON_EMPTY
    return REASON_OK


def merge_entries(entries: Sequence[Mapping[str, Any]], *, keep: int) -> list[dict[str, Any]]:
    """把同一个人的多条记录收成「最近的 keep 条」，时间正序（供人话阅读）。

    ``keep<=0`` = 全留（配置里 0 即关的约定；这里不给「至少留 1 条」的暗规则，
    因为 ``[-0:]`` 会变成「全留」，与字面相反——见 ``life_sim._keep_tail`` 的教训）。
    """

    items = [dict(item) for item in entries if isinstance(item, Mapping)]
    items.sort(key=lambda item: _as_float(item.get("at"), 0.0))
    limit = int(keep)
    if limit <= 0:
        return items
    return items[-limit:]


def describe_entries(
    entries: Sequence[Mapping[str, Any]],
    *,
    max_chars: int,
    requote_limit: int,
    label_of_time: Any = None,
) -> str:
    """把若干条记录排成一句话用的片段：``「03:12 在吗」「03:15 睡了没」``。

    ``label_of_time`` 是「时间戳 → HH:MM」的可调用对象（时区由插件侧决定，
    纯模块不碰时区）。
    """

    limit = int(requote_limit)
    picked = list(entries)
    if limit > 0:
        picked = picked[-limit:]
    parts: list[str] = []
    for item in picked:
        body = excerpt(item.get("text"), max_chars=max_chars)
        if not body:
            continue
        stamp = ""
        if callable(label_of_time):
            try:
                stamp = str(label_of_time(_as_float(item.get("at"), 0.0)) or "").strip()
            except Exception:  # noqa: BLE001 —— 时间格式化失败不该影响补答
                stamp = ""
        parts.append(f"{stamp}「{body}」" if stamp else f"「{body}」")
    return "".join(parts)


def time_span(
    entries: Sequence[Mapping[str, Any]], *, label_of_time: Any = None
) -> str:
    """这批记录的时间段：``23:40–01:12``（单条则只给一个时刻）。

    为什么默认不带原文（v1.23.1）：消息本来就在宿主的 ``_chat_history`` 里
    （静默轮同样"完成历史维护"）。复述一遍会让模型把同一句话讲两次。补答要说清的
    只有「什么时段、几条」——这正是历史里读不出来的那一层（历史只有时间戳，
    没有「你当时在睡」这个事实）。
    """

    stamps: list[float] = []
    for item in entries:
        value = _as_float(item.get("at"), 0.0)
        if value > 0.0:
            stamps.append(value)
    if not stamps:
        return ""
    if not callable(label_of_time):
        return ""
    try:
        first = str(label_of_time(min(stamps)) or "").strip()
        last = str(label_of_time(max(stamps)) or "").strip()
    except Exception:  # noqa: BLE001 —— 时间格式化失败不该影响补答
        return ""
    if not first:
        return ""
    if not last or last == first:
        return first
    return f"{first}–{last}"


# ---------------------------------------------------------------- 选人


@dataclass(frozen=True)
class Candidate:
    """一个待补的会话。"""

    session_id: str
    person_id: str
    user_id: str
    is_group: bool
    last_at: float
    count: int
    entries: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    familiarity: float = 0.0


def order_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    """排序：**熟悉的先补**，同样熟悉则**等得久的先补**。

    为什么不是「等得久的先补」：一次睡醒最多补 5 个，若只按时间排，最熟的两个人
    可能永远排不上（对方每次都刚好晚一分钟）。熟悉度是稳定优先级，等待时长是次级。
    """

    items = list(candidates)
    items.sort(key=lambda item: (-float(item.familiarity), float(item.last_at), item.session_id))
    return items


def stagger_offsets(count: int, *, max_seconds: int, seed_key: str) -> list[float]:
    """给 ``count`` 个会话各算一个错开秒数（0..max_seconds），**确定性**。

    确定性由 ``sha1(seed_key|序号)`` 派生：同一次睡醒的重算得到同一组偏移（重启、
    重复 tick 都不会让「谁先谁后」跳变），但不同会话之间彼此独立、不会全撞在 0。
    """

    total = max(0, int(count))
    cap = max(0, int(max_seconds))
    if total <= 0:
        return []
    if cap <= 0:
        return [0.0] * total
    offsets: list[float] = []
    for index in range(total):
        digest = hashlib.sha1(f"{seed_key}|{index}".encode("utf-8")).hexdigest()
        offsets.append(float(int(digest[:8], 16) % (cap + 1)))
    return offsets


def plan(
    *,
    candidates: Sequence[Candidate],
    rules: CatchupRules,
    now: float,
    day_key: str,
    wake_seq: int,
    person_used_today: Mapping[str, int],
    global_used_today: int,
    stagger_seed: str = "",
) -> tuple[list[tuple[float, Candidate]], list[tuple[str, str]]]:
    """决定这一次睡醒补谁、什么时候补。

    返回 ``(待发列表, 跳过列表)``：待发是 ``[(延迟秒数, 候选)]``，跳过是
    ``[(session_id, 原因键)]``（进沉默台账，供 ``/生活 为什么`` 解释）。
    """

    due_candidates: list[Candidate] = []
    skipped: list[tuple[str, str]] = []
    if not rules.enabled:
        return [], [(item.session_id, "disabled") for item in candidates]
    if not rules.active_enabled:
        return [], [(item.session_id, "not_active") for item in candidates]
    if not candidates:
        return [], []

    ordered = order_candidates(candidates)
    slots = max(0, int(rules.per_wake_max))
    budget = max(0, int(rules.global_daily_max))
    late_limit = max(0.0, float(rules.late_degrade_hours)) * 3600.0
    remaining = max(0, int(global_used_today))

    for candidate in ordered:
        if slots <= 0:
            skipped.append((candidate.session_id, "per_wake"))
            continue
        if _as_int(person_used_today.get(candidate.person_id), 0) >= max(
            1, int(rules.per_person_daily)
        ):
            skipped.append((candidate.session_id, "per_person"))
            continue
        if budget > 0 and remaining >= budget:
            skipped.append((candidate.session_id, "global_daily"))
            continue
        if late_limit > 0 and (float(now) - float(candidate.last_at)) > late_limit:
            skipped.append((candidate.session_id, "too_late"))
            continue
        due_candidates.append(candidate)
        slots -= 1
        remaining += 1

    offsets = stagger_offsets(
        len(due_candidates),
        max_seconds=int(rules.stagger_max_seconds),
        seed_key=f"{stagger_seed or day_key}|wake{wake_seq}",
    )
    return list(zip(offsets, due_candidates)), skipped


# ---------------------------------------------------------------- 措辞


def persona_hint(persona: object, *, max_chars: int) -> str:
    """人设摘要（塞进 intent 用）。空串 = 不塞。"""

    limit = int(max_chars)
    if limit <= 0:
        return ""
    text = re.sub(r"\s+", " ", str(persona or "")).strip()
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


def build_intent(
    candidate: Candidate,
    *,
    rules: CatchupRules,
    bot_name: str = "她",
    persona: str = "",
    label_of_time: Any = None,
    sleep_minutes: float = 0.0,
) -> str:
    """拼出交给宿主 Planner 的任务描述（**插件不写她要说的话**）。

    为什么把原文塞进 intent 而不是塞进素材池：素材池是「她自己的念头」，
    而这条是「别人对她说过的话」。混进去会让冷场话题和「有人在等你回复」
    抢同一个评分；分开之后，Planner 能看出这一轮的来由是「睡着期间有人找过她」。
    """

    where = "群里有人" if candidate.is_group else "有人"
    hours = max(0.0, float(sleep_minutes)) / 60.0
    slept = f"你刚睡了约 {hours:.1f} 小时，" if sleep_minutes > 0 else "你刚睡醒，"
    span = time_span(candidate.entries, label_of_time=label_of_time)
    window = f"（共 {candidate.count} 条{('，' + span + ' 之间') if span else ''}）"
    # ⚠ 默认 **不复述原文**（``requote_limit<=0``）：内容已经在对话历史里。
    # 只有用户显式把 ``requote_limit`` 调大时才引，用于「历史可能被裁掉」的兜底。
    quote = ""
    if int(rules.requote_limit) > 0:
        body = describe_entries(
            candidate.entries,
            max_chars=int(rules.max_chars),
            requote_limit=int(rules.requote_limit),
            label_of_time=label_of_time,
        )
        quote = f"（原文摘录，仅供历史被裁剪时对照：{body}）" if body else ""
    lines = [
        f"{slept}睡着的时候{where}找过你{window}。{quote}",
        "这些消息已经在对话历史里了：不要逐条复述，也不要解释自己的作息细节，自然地接一句就好。",
        "如果你觉得此刻不适合说话（还没缓过来、心情不对、没什么可说的），保持沉默也完全可以。",
    ]
    if candidate.is_group:
        lines.append("这是群聊：不要 @ 具体的人，也不要引用具体消息。")
    hint = persona_hint(persona, max_chars=int(rules.persona_chars))
    if hint:
        lines.append(f"（人设参考，别照抄：{hint}）")
    return "\n".join(lines)


def fill_fact(
    entries: Sequence[Mapping[str, Any]],
    *,
    rules: CatchupRules,
    label_of_time: Any = None,
) -> str:
    """被动回填用的一句世界内事实（注入 replyer 的 ``extra_prompt``）。

    与主动补的区别：这条只陈述事实，**不要求她回应**——她这一轮本来就在回别的话，
    看到「睡着时有人找过你」之后要不要接，由模型自己决定。
    """

    span = time_span(entries, label_of_time=label_of_time)
    if not span and not entries:
        return ""
    window = f"（{span} 之间）" if span else ""
    quote = ""
    if int(rules.requote_limit) > 0:
        body = describe_entries(
            entries,
            max_chars=int(rules.max_chars),
            requote_limit=int(rules.requote_limit),
            label_of_time=label_of_time,
        )
        quote = f"原文摘录：{body}。" if body else ""
    # 与 ``build_intent`` 同一口径：原文不重复，只说「什么时候、几条」，内容让历史去讲。
    return (
        f"（生活状态）你睡着的时候对方留过言{window}，消息就在上面的对话历史里。"
        f"{quote}你当时没看到，现在才看到——不必逐条复述。"
    )


__all__ = [
    "Candidate",
    "CatchupRules",
    "REASON_AWAKE",
    "REASON_COMMAND",
    "REASON_DND",
    "REASON_EMPTY",
    "REASON_FROM_BOT",
    "REASON_IN_WAKE_WINDOW",
    "REASON_NOT_ADDRESSED",
    "REASON_OK",
    "REASON_OWNER_WOKE",
    "SKIP_LABELS",
    "TARGET_ACTIVE",
    "TARGET_PASSIVE",
    "TARGET_SKIP",
    "build_intent",
    "compact",
    "describe_entries",
    "excerpt",
    "fill_fact",
    "is_do_not_disturb",
    "merge_entries",
    "order_candidates",
    "persona_hint",
    "plan",
    "should_record",
    "stagger_offsets",
    "time_span",
]
