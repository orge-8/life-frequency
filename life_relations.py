# -*- coding: utf-8 -*-
"""关系模型（relations）：按人建档的熟悉度演化（纯模块 + SQLite 持久化）。

**核心决策（方案二 v2）**：关系按人（user_id）建，不按会话建——同一个人可能
出现在多个群/私聊，「熟人」是人属性。群聊只对**明确 @ 过/回复过她**的人建档
（决策 2 收紧）：群里其余人当背景板，不进表。「她只认识跟她说过话的人」本身
就是人味。

熟悉度（familiarity 0–100）是唯一演化的核心量：

* 有效互动（私聊消息 / 被 @ / 回复她）：+0.2，刷新 last_interaction_at；
* 共同经历（social intake 命中 who）：+1.0；
* 7 天未互动：−0.5（被遗忘本身也是人味）；
* 档位：<20 陌生 / 20–49 认识 / 50–79 熟络 / ≥80 亲密。

持久化走 ``life_store``（SQLite 第二租户，routine_daily 是第一个）。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping

try:  # 包式加载（Runner 真机）
    from .life_factors import interpolate
    from .life_store import (
        DEFAULT_PLATFORM,
        LifeStore,
        MemoryStore,
        new_relationship,
        person_id_for,
    )
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_factors import interpolate  # type: ignore[no-redef]
    from life_store import (  # type: ignore[no-redef]
        DEFAULT_PLATFORM,
        LifeStore,
        MemoryStore,
        new_relationship,
        person_id_for,
    )

#: 有效互动的单次增量
FAMILIARITY_PER_INTERACTION = 0.2
#: 共同经历的单次增量
FAMILIARITY_PER_SHARED_EVENT = 1.0
#: 每 7 天未互动的衰减
FAMILIARITY_DECAY_PER_WEEK = 0.5
_WEEK_SECONDS = 7 * 86400.0
#: 档位边界（familiarity 0–100）
TIERS = ((80, "亲密"), (50, "熟络"), (20, "认识"), (0, "陌生"))
#: 关系档案上限（超过按「最近互动最久远」淘汰）
DEFAULT_KEEP = 200

#: 档位 → 行为基调（v1.20.0，G1「关系基调」）。
#:
#: **为什么必须有**：关系层过去只把熟悉度当**门槛系数**（``threshold_multiplier``）
#: 与**情绪增益**（``emotion_factor``）用，从没告诉过模型「对不同熟悉度的人该用
#: 什么语气」——模型只拿到过一个数字，不知道这份熟悉意味着什么。
#: 参照 ``social-state`` 的实测结论（其 ``plugin.py:2235`` 原话）：
#: **「光有数字 LLM 不一定演，按分档给出明确的语气锚点」**。
#:
#: 每条都写成**可执行的语气方向**（做什么 / 不做什么），不是形容词——
#: 「语气放松」是形容词，「可以开玩笑、接梗，不必拘谨」才是可执行的。
#:
#: ⚠️ **刻意没有「陌生」这一档**（v1.20.0 真机数据修）：与 ``prompt_lines`` 的
#: ``familiarity < 20`` 跳过是**同一口径**——陌生档不进提示词。为什么这条必须守住：
#: 2026-10-16 真机监控（v1.19.2、283 样本、23.5 小时）显示关系档案 24–25 人里
#: **最高熟悉度只有 9.8，全部落在「陌生」档**。若陌生档也注入，她会对**每一个人**
#: ——包括聊过几百次的老朋友——输出「客气、有距离感，不主动搭话」，那是**反向效果**，
#: 比不注入差得多。熟悉度全是陌生时，正确行为是**这一轮不注入**。
TIER_TONES: dict[str, str] = {
    "亲密": "说话随意，可以直接吐槽、撒娇或打断对方，不用客套",
    "熟络": "语气放松，可以开玩笑、接梗，不必拘谨",
    "认识": "友好而简短，不主动套近乎、不问私人问题",
}

#: 熟悉度 → 社交情绪系数曲线（v1.16.3 M7）。**中性锚点钉在陌生人身上**（决议 3）：
#: 新装插件时 relations 没有任何数据、所有人都是陌生 ⇒ 系数 1.0 = 旧行为逐位一致；
#: 偏离只随关系加深**单向**发生，用户那时也已积累了观察数据。
#: （原设计的 0.7 / 1.0 / 1.4 已作废：锚 0.7 等于开箱第一天社交收益就被悄悄打七折。）
RELATION_EMOTION_CURVE: tuple[tuple[float, float], ...] = (
    (20.0, 1.0),
    (50.0, 1.2),
    (80.0, 1.5),
)
#: 系数上限（决议 3：单向只放大，且封顶 1.5）
RELATION_EMOTION_CAP = 1.5


def tier_of(familiarity: float) -> str:
    """熟悉度 → 档位名。"""

    value = float(familiarity) if familiarity == familiarity else 0.0
    for threshold, label in TIERS:
        if value >= threshold:
            return label
    return "陌生"


def tone_of(familiarity: float) -> str:
    """熟悉度 → 行为基调文案（空串 = 不注入：陌生档与坏值都走这条）。

    **陌生档返回空串是刻意的**（与 ``prompt_lines`` 同口径）：真机上熟悉度普遍
    到不了「认识」档，此时不注入才是对的（详见 ``TIER_TONES`` 的说明）。
    """

    return TIER_TONES.get(tier_of(familiarity), "")


def tone_line(familiarity: float) -> str:
    """关系基调的**注入行**（v1.20.0，G1；空串 = 不注入）。

    与 :func:`prompt_lines` 分成两条通道，因为受众不同：

    * ``prompt_lines`` 是给**人看**的状态卡口径（含脱敏 QQ 号，用在 ``/生活 状态``
      与关系分布）；
    * ``tone_line`` 是给**模型看**的语气口径——**刻意不含任何账号**，只说「这个人
      跟你是什么关系、因此该用什么语气」。

    注入点在**回复侧**（``maisaka.replyer.before_request``）：语气是回复的属性，
    塞进活动决策提示词里没有意义（那一步决定的是「她在做什么」）。

    ⚠ **坏值（NaN / 非有限 / 非数值）一律返回空串，不按「陌生」处理**——与
    :func:`threshold_multiplier` 的 NaN⇒陌生 不同，因为代价不对称：
    门槛算成陌生只是让她更保守（安全），而语气指令给错会让她对熟人突然冷淡。
    宁可这一轮不注入。
    """

    try:
        value = float(familiarity)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value):
        return ""
    tone = tone_of(value)
    if not tone:
        return ""
    return f"关系基调（{tier_of(value)}）：{tone}"


def threshold_multiplier(familiarity: float) -> float:
    """档位 → 主动开口阈值系数（方案二 §2.4：0.5 / 1.0 / 1.5 / 2.0）。

    对亲密的人开口门槛更低（0.5——熟人之间不需要攒「想说的话」）；
    对陌生人的门槛翻倍（2.0——她不会贸然找陌生人搭话）。
    """

    tier = tier_of(familiarity)
    return {"亲密": 0.5, "熟络": 0.8, "认识": 1.2, "陌生": 2.0}[tier]


def should_record(
    *,
    private_chat: bool,
    mentioned: bool,
    is_reply_to_bot: bool,
    user_id: str,
) -> bool:
    """建档判据（v2 收紧版）：满足任一才建档/更新。

    1. 私聊任意消息（user_id 必然可识别）；
    2. 群聊且被 @ / 被提及（她感知到「这人在跟我说话」）；
    3. 群聊且是对她消息的回复（载荷含 reply 且指向她）。

    纯围观、刷屏、@别人 → 不建档。
    """

    user_id = str(user_id or "").strip()
    if not user_id:
        return False  # 识别不出人，无从谈起
    if private_chat:
        return True
    return bool(mentioned or is_reply_to_bot)


def touch(
    store: Any,
    *,
    person_id: str,
    now: float,
    platform: str = DEFAULT_PLATFORM,
    user_id: str = "",
    mentioned: bool = False,
    shared_event: bool = False,
    keep: int = DEFAULT_KEEP,
) -> dict[str, Any] | None:
    """一次互动：建档或更新熟悉度。返回更新后的档案（认不出人返回 None）。

    v1.20.0（Y4）：**主键由 ``user_id`` 改为 ``person_id``**（``md5(platform_user_id)``），
    ``user_id`` / ``platform`` 降级为普通字段。``user_id`` 仍要传——它是给人看的
    脱敏显示口径（``prompt_lines`` 用 ``mask_user_id``），也是排障时唯一能对上号的字段。

    ⚠ 演化**不在**本函数里做衰减判定——衰减由调用方在 tick 里按天批量跑
    （:func:`decay_all`），避免每条消息都 O(n) 扫全表。
    """

    pid = str(person_id or "").strip()
    if not pid:
        return None
    record = store.get_relationship(pid)
    if record is None:
        record = new_relationship(
            pid, platform=str(platform or DEFAULT_PLATFORM), user_id=user_id, now=now
        )
    else:
        record["last_interaction_at"] = float(now)
        # 昵称/平台可能变（换平台登录、宿主改了平台标识）——每次互动顺手刷新，
        # 否则排障时看到的永远是最初那一次的值。
        if user_id:
            record["user_id"] = str(user_id).strip()[:64]
        if platform:
            record["platform"] = str(platform).strip()[:32]
    record["interaction_count"] = int(record.get("interaction_count") or 0) + 1
    gain = 0.0
    if mentioned:
        gain += FAMILIARITY_PER_INTERACTION
    if shared_event:
        gain += FAMILIARITY_PER_SHARED_EVENT
    record["familiarity"] = min(100.0, max(0.0, float(record.get("familiarity") or 0.0) + gain))
    store.put_relationship(record)
    store.prune_relationships(keep=max(0, int(keep)))
    return record


def decay_all(store: Any, *, now: float, keep: int = DEFAULT_KEEP) -> int:
    """按「7 天未互动 −0.5」批量衰减；返回被更新的条数（供日志）。

    每 tick 调用也安全（无变化时是纯 SELECT），但建议按天限频。
    """

    try:
        people = store.top_relationships(limit=max(0, int(keep)))
    except Exception:  # noqa: BLE001 —— 读失败按「没有档案」处理
        return 0
    changed = 0
    for record in people:
        last = float(record.get("last_interaction_at") or 0.0)
        if last <= 0.0:
            continue
        weeks = (float(now) - last) / _WEEK_SECONDS
        if weeks < 1.0:
            continue
        current = float(record.get("familiarity") or 0.0)
        decayed = max(0.0, current - FAMILIARITY_DECAY_PER_WEEK * int(weeks))
        if decayed != current:
            record["familiarity"] = decayed
            store.put_relationship(record)
            changed += 1
    return changed


def prompt_lines(
    top: list[dict[str, Any]],
    *,
    max_lines: int = 3,
    memory_facts: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """近层注入的「最熟的人」行（只对 ≥认识 档位的人提）。

    昵称不明时显示脱敏 QQ 号（前 3 后 2，中间 *）——方案二 §2.4 的状态卡口径。

    ``memory_facts``（v1.22.0）：``person_id → 事实序列``，给了就在该人那一行末尾
    追加「；记得 X」。**只服务关系档案的展示**（`/生活 关系`、状态卡），不进回复
    摘要——`_life_digest` 走的是 `_relation_tone_line` 那条路径，刻意不接记忆，
    理由见 `life_config.MemoryConfig` 的类说明。

    事实由 `life_memory` 清洗过（去结构字符、截断），这里只做展示层的封顶：
    一行里塞三段旧事会糊成一团，所以最多 ``_MEMORY_FACTS_PER_LINE`` 条。
    """

    lines: list[str] = []
    for record in top[: max(0, int(max_lines))]:
        familiarity = float(record.get("familiarity") or 0.0)
        if familiarity < 20.0:
            continue
        uid = str(record.get("user_id") or "")
        # v1.13.1（F-006，安全审计）：统一走 ``mask_user_id``——这里曾是内联重复实现，
        # 与公共函数口径漂移的风险就是 F-001（world 层明文 QQ）漏网的根因
        masked = mask_user_id(uid)
        hint = str(record.get("relation_hint") or "").strip()
        label = f"{hint}（{masked}）" if hint else masked
        line = f"认识的人：{label}（{tier_of(familiarity)}，熟悉度 {familiarity:.0f}/100）"
        facts = _memory_facts_of(record, memory_facts)
        if facts:
            line = f"{line}；记得 {'；'.join(facts)}"
        lines.append(line)
    return tuple(lines)


#: 关系档案里每人最多展示几条旧事（与 ``[memory] in_relations`` 的展示口径）
_MEMORY_FACTS_PER_LINE = 2


def _memory_facts_of(
    record: Mapping[str, Any],
    memory_facts: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """从 ``memory_facts`` 里取这个人名下的旧事（认不出人就返回空）。

    先按 ``person_id`` 找（与宿主 A_Memorix 同一套 id），找不到再退回 ``user_id``
    ——调用方可能只有旧口径的键。两条都不中就是没有记忆，静默返回空。
    """

    if not memory_facts:
        return ()
    for key in (record.get("person_id"), record.get("user_id")):
        text = str(key or "").strip()
        if not text:
            continue
        raw = memory_facts.get(text)
        if not raw:
            continue
        if isinstance(raw, str):
            raw = (raw,)
        out: list[str] = []
        for item in list(raw)[: max(0, _MEMORY_FACTS_PER_LINE)]:
            fact = str(item or "").strip()
            if fact:
                out.append(fact)
        if out:
            return tuple(out)
    return ()


def emotion_curve() -> tuple[tuple[float, float], ...]:
    """默认的关系系数曲线（供 plugin 在配置为空/坏时回退）。

    单独出一个函数而不是直接暴露常量：调用方拿到的一定是**元组的拷贝**，
    改不动模块级常量（这类往返被改坏过一次，就不再有第二次）。
    """

    return tuple((float(x), float(y)) for x, y in RELATION_EMOTION_CURVE)


def emotion_factor(
    familiarity: float | None, *, curve: object = None, cap: float = RELATION_EMOTION_CAP
) -> float:
    """熟悉度 → 社交情绪增益系数（v1.16.3 M7）。

    陌生（<20）→ 1.0、熟（50）→ 1.2、亲密（≥80）→ 1.5，分段线性、单向只放大、封顶。

    **缺数据 = 1.0**（= 陌生人 = 旧行为）：新装插件时所有关系都是陌生，或者档案读
    不到（库不可用 / 关系层关掉）——这两种情况都必须是「行为与升级前一致」，否则
    开箱第一天的社交收益就被悄悄改了（决议 3 的完整推理见 README 版本历史 1.16.3）。

    认不出人（``None`` / 空串 / 坏值）按 1.0 处理：宁可不放大，也不猜错人。
    """

    if familiarity is None:
        return 1.0
    try:
        value = float(familiarity)
    except (TypeError, ValueError):
        return 1.0
    if value != value:  # NaN
        return 1.0
    if math.isfinite(value) is False:
        return 1.0
    points = tuple(curve) if curve else RELATION_EMOTION_CURVE
    if not points:
        return 1.0
    factor = interpolate(points, value)
    if factor != factor or not math.isfinite(factor):
        return 1.0
    ceiling = max(1.0, float(cap))
    return max(1.0, min(ceiling, float(factor)))


def mask_user_id(user_id: str) -> str:
    """QQ 号脱敏：前 3 后 2，中间 *（≤5 位原样显示）。"""

    uid = str(user_id or "").strip()
    if len(uid) <= 5:
        return uid
    return f"{uid[:3]}***{uid[-2:]}"


__all__ = [
    "DEFAULT_KEEP",
    "DEFAULT_PLATFORM",
    "FAMILIARITY_DECAY_PER_WEEK",
    "FAMILIARITY_PER_INTERACTION",
    "FAMILIARITY_PER_SHARED_EVENT",
    "RELATION_EMOTION_CAP",
    "RELATION_EMOTION_CURVE",
    "TIERS",
    "TIER_TONES",
    "decay_all",
    "emotion_curve",
    "emotion_factor",
    "mask_user_id",
    "person_id_for",
    "prompt_lines",
    "should_record",
    "threshold_multiplier",
    "tier_of",
    "tone_line",
    "tone_of",
    "touch",
]
