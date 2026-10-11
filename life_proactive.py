# -*- coding: utf-8 -*-
"""主动开口的评分与硬闸（纯模块，无 ctx、无 IO）。

这一半是**选装**的（``proactive.enabled`` 默认 false）：它决定她是否会主动开话，
而不是回复频率。结构照抄 ``octmicy/Mai_life`` 的成熟做法，但规模小得多：

    score = 素材权重 + 体力偏置 + 情绪偏置
    if score >= score_threshold: 开口

**评分只负责排序，数量由一组正交硬闸决定。** 这些硬闸 LLM 看不到，也不参与评分——
这样以后重调评分函数不会把消息量炸掉。硬闸顺序（每条都对应一个真实的
「她怎么不说话了」）：

1. 插件没开主动  → ``disabled``
2. 她在睡觉      → ``sleeping``
3. 体力低于下限  → ``low_energy``
4. 命中静默时段  → ``quiet``
5. 没有未过期素材 → ``no_material``
6. 今天已达上限  → ``daily_max``
7. 距上次主动太近 → ``interval``
8. 对方刚说过话  → ``silence``
9. 分数不够      → ``low_score``

每次沉默的原因都记进 ``skip_ledger``，``/生活 为什么`` 直接回答「她怎么不说话了」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

try:  # 包式加载（Runner 真机）
    from .life_activity import is_asleep, in_window
    from .life_events import sanitize_text
    from .life_sim import material_freshness
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_activity import is_asleep, in_window  # type: ignore[no-redef]
    from life_events import sanitize_text
    from life_sim import material_freshness

REASON_OK = "ok"
REASON_DISABLED = "disabled"
REASON_SLEEPING = "sleeping"
REASON_LOW_ENERGY = "low_energy"
REASON_QUIET = "quiet"
REASON_NO_MATERIAL = "no_material"
REASON_DAILY_MAX = "daily_max"
REASON_INTERVAL = "interval"
REASON_SILENCE = "silence"
REASON_LOW_SCORE = "low_score"

SKIP_REASON_LABELS: dict[str, str] = {
    REASON_DISABLED: "主动开口未启用",
    REASON_SLEEPING: "她在睡觉",
    REASON_LOW_ENERGY: "体力低于下限",
    REASON_QUIET: "命中静默时段",
    REASON_NO_MATERIAL: "没有没过期的素材",
    REASON_DAILY_MAX: "今天主动次数已用完",
    REASON_INTERVAL: "距上次主动还不够久",
    REASON_SILENCE: "对方刚说过话，先不插嘴",
    REASON_LOW_SCORE: "素材权重+状态偏置没到阈值",
}


@dataclass(frozen=True)
class ProactiveConfig:
    """主动开口的全套可调参数。"""

    enabled: bool = False
    score_threshold: float = 0.45
    min_interval_minutes: int = 180
    recent_user_silence_minutes: int = 30
    daily_max: int = 1
    minimum_energy: float = 2.5
    quiet_hours: tuple[tuple[int, int], ...] = ()
    energy_bias_divisor: float = 30.0
    energy_bias_floor: float = 4.0
    mood_bias_scale: float = 0.12
    mood_bias_cap: float = 0.15
    #: 未回应退避（G1，仅私聊）：对方连续不回应时，最小间隔按 factor^连击 拉长，
    #: 连击封顶 max_streak。默认 2^4 = 16 倍（180 分钟 → 48 小时）：封顶必须**大于**
    #: 「每天一次」的自然节奏（24h），否则在默认 ``daily_max=1`` 下退避永远不是约束
    #: 条件（实测：封顶 24h 时间隔恒为 24h，与不开退避逐位相同），而且刚好卡在
    #: ``elapsed < interval`` 的边界上、行为取决于当天开口的钟点。48h 让「隔天一次」
    #: 成为确定性结果。factor ≤ 1 视为关闭；对方一回话，连击判定立即失效。
    unanswered_backoff_factor: float = 2.0
    unanswered_backoff_max_streak: int = 4
    #: 素材保鲜衰减下限（与 ``life_sim.material_freshness`` 同一口径）
    material_decay_floor: float = 0.25
    baseline_emotion: float = 5.0

    def quiet(self, now_minutes: int) -> bool:
        return any(in_window(now_minutes, window) for window in self.quiet_hours)


@dataclass(frozen=True)
class ProactiveDecision:
    """一次主动开口裁定。``material`` 为 None 表示不该开口。"""

    should_send: bool
    reason: str
    score: float = 0.0
    material: dict[str, Any] | None = None
    intent: str = ""
    detail: str = ""
    #: 未回应退避档位（G1）：``1.0`` = 没有退避。只有 ``interval`` 闸会带上它，
    #: 供沉默台账按档位分桶——``/生活 为什么`` 因此能答出「退避到了第几级」，
    #: 而不是只显示一个笼统的「距上次主动太近」。
    backoff: float = 1.0

    @property
    def reason_label(self) -> str:
        return SKIP_REASON_LABELS.get(self.reason, self.reason)


# ---------------------------------------------------------------- 宽松取数


def _as_float(value: object, default: float = 0.0) -> float:
    """从（可能被外部写坏的）持久化状态里取数值；非数值/非有限一律退回默认值。

    ``life_state.json`` 可能被手工编辑、跨版本或外部工具写坏，而 ``float(None)`` /
    ``int("x")`` 会直接抛错——抛在 ``decide`` 里会让整轮 ``_sim_tick`` 中止
    （v1.1.0 的隐患），所以这里绝不抛。
    """

    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else default
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return default
        return number if math.isfinite(number) else default
    return default


def _as_int(value: object, default: int = 0) -> int:
    return int(_as_float(value, float(default)))


# ---------------------------------------------------------------- 评分


def score_of(
    *,
    material_weight: float,
    energy: float,
    emotion: float,
    config: ProactiveConfig,
) -> tuple[float, str]:
    """``素材权重 + 体力偏置 + 情绪偏置``，返回 ``(分数, 明细)``。

    偏置的标定参照 Mai_life（它用的是 0–100 的情绪体力刻度）：

    - 体力：``max(0, (energy - 4) / 30)`` —— 4 分以下不给分，10 分给满 ``+0.20``；
    - 情绪：把 0–10 折成 -1~1 的 valence 再 ``clamp(×0.12, ±0.15)``。

    刻意**只用加性小偏置**，不引入任何乘性权重：素材本身更重要，
    状态只做微调，这样调参不会互相干扰。
    """

    energy_bias = max(
        0.0,
        (float(energy) - float(config.energy_bias_floor)) / max(1e-6, float(config.energy_bias_divisor)),
    )
    valence = (float(emotion) - float(config.baseline_emotion)) / max(
        1e-6, float(config.baseline_emotion)
    )
    mood_bias = max(
        -float(config.mood_bias_cap),
        min(float(config.mood_bias_cap), valence * float(config.mood_bias_scale)),
    )
    total = float(material_weight) + energy_bias + mood_bias
    detail = (
        f"素材={float(material_weight):.2f} "
        f"体力偏置=+{energy_bias:.3f} "
        f"情绪偏置={mood_bias:+.3f} "
        f"合计={total:.3f} 阈值={float(config.score_threshold):.2f}"
    )
    return total, detail


def select_material(
    materials: Sequence[Mapping[str, Any]],
    now: float,
    floor: float = 0.25,
) -> dict[str, Any] | None:
    """挑一条未过期、**有效权重**最高的素材。

    有效权重 = 原始权重 × 时效系数（与 ``life_sim.material_freshness`` 同一口径）：
    过了「最佳保鲜相位」的念头低价值化、自然排到队尾，而不是占着榜首直到过期
    那一刻突然消失。返回的是**副本**，附 ``effective_weight`` 供评分使用——
    评分用衰减后的权重，「低价值化」才真正落到开口分数上。
    """

    best: dict[str, Any] | None = None
    best_weight = float("-inf")
    for item in materials or []:
        if not isinstance(item, Mapping):
            continue
        if _as_float(item.get("expires_at"), 0.0) <= float(now):
            continue
        if not sanitize_text(item.get("text", ""), max_chars=80):
            # 没有正文的素材不算素材（占位/空串一律跳过）
            continue
        raw = _as_float(item.get("weight"), 0.0)
        weight = raw * material_freshness(item, now=now, floor=floor)
        if weight > best_weight:
            best_weight = weight
            best = dict(item)
    if best is not None:
        best["effective_weight"] = best_weight
    return best


#: 主动开口的发言纪律：**主动开口不是回复谁，不许挂引用**。
#:
#: 写在**代码层**（不是 ``Field(default=...)``）：``config.toml`` 首跑落盘后就不再跟随升级更新，
#: 纪律类规则只写在配置默认值里，已部署实例永远不会生效（见 maibot-plugin-dev 铁律 11）。
#: 本常量同时被 ``build_intent``（任务描述）与 ``plugin.PROACTIVE_NO_QUOTE_HINT``
#: （Planner 请求注入）使用 —— 一处定义，避免两处措辞漂移。
#:
#: 措辞是 group-welcome v1.0.2→v1.2.3 四轮迭代后的结论：只说「不要引用」会把模型逼到无路
#: 可走（``reply`` 必须带 ``msg_id``），必须给一条可执行路径：引用对象只能是她自己。
NO_QUOTE_DISCIPLINE = (
    "这是**主动开口**，不是回复谁：请调用 reply 生成一条新的发言，并把 set_quote 设为 false，"
    "让这条发言独立出现 —— 不要引用任何消息（引用别人刚说过的话，会被误以为你在回他）；"
    "若确实需要指定回复对象，请选你自己最近发出的一条消息，切勿引用他人的消息。"
)


def build_intent(material: Mapping[str, Any]) -> str:
    """把素材拼成交给宿主的主动意图文本（含代码层的发言纪律）。"""

    text = sanitize_text(material.get("text", ""), max_chars=80)
    label = sanitize_text(material.get("label", ""), max_chars=32)
    if text:
        base = f"想自然地聊聊刚发生的一件事（{label or '生活小事'}）：{text}"
    else:
        base = f"想自然地聊聊刚发生的一件事（{label or '生活小事'}）"
    return f"{base} {NO_QUOTE_DISCIPLINE}"


# ---------------------------------------------------------------- 裁定


def decide(
    *,
    config: ProactiveConfig,
    now: float,
    now_minutes: int,
    activity: str,
    energy: float,
    emotion: float,
    materials: Sequence[Mapping[str, Any]],
    session: Mapping[str, Any] | None,
    day_key: str,
    private_chat: bool = False,
) -> ProactiveDecision:
    """是否该主动开口，以及不该开口时的原因。

    ``session`` 是每会话记录（``{"last_proactive_at","last_user_message_at","day_key","count"}``），
    由 ``life_sim`` 的 ``sessions`` 字段持久化。``private_chat`` 只影响**未回应退避**
    （G1 仅私聊启用；群聊与判不出类型的会话一律维持现有硬闸）。
    """

    if not config.enabled:
        return ProactiveDecision(False, REASON_DISABLED)

    if is_asleep(activity):
        return ProactiveDecision(False, REASON_SLEEPING)

    if float(energy) < float(config.minimum_energy):
        return ProactiveDecision(
            False, REASON_LOW_ENERGY,
            detail=f"体力 {float(energy):.1f} < 下限 {float(config.minimum_energy):.1f}",
        )

    if config.quiet(now_minutes):
        return ProactiveDecision(False, REASON_QUIET)

    material = select_material(materials, now, floor=config.material_decay_floor)
    if material is None:
        return ProactiveDecision(False, REASON_NO_MATERIAL)

    record = dict(session) if isinstance(session, Mapping) else {}
    record_day = str(record.get("day_key", ""))
    # 缺 day_key（旧版本/被外部写坏的记录）时**按今天算**而不是清零：清零会让
    # daily_max 静默失效（count=5 也照样开口）。宁可少发一条，也不多发。
    count_today = _as_int(record.get("count"), 0) if (record_day == day_key or not record_day) else 0
    daily_max = max(0, int(config.daily_max))
    if daily_max <= 0 or count_today >= daily_max:
        return ProactiveDecision(
            False, REASON_DAILY_MAX,
            material=material,
            detail=f"今天已主动 {count_today}/{daily_max} 次",
        )

    last_proactive = _as_float(record.get("last_proactive_at"), 0.0)
    last_user = _as_float(record.get("last_user_message_at"), 0.0)
    # 未回应退避（G1，仅私聊）：上次主动之后对方一直没说话 ⇒ 本次开口的间隔按
    # factor^连击 拉长（连击封顶）。对方回过话则连击视为 0——判定是即时的，
    # 对方一发言 last_user 就会超过 last_proactive，不需要落库清零。
    # 群聊 / 判不出会话类型 / 系数 ≤ 1 一律不退避（维持现有硬闸）。
    streak = _as_int(record.get("unanswered_streak"), 0)
    if (
        private_chat
        and float(config.unanswered_backoff_factor) > 1.0
        and last_proactive > 0.0
        and last_proactive > last_user
    ):
        # +1：把「上一次主动至今未回应」这一次也计进连击——这样第 2 次开口
        # 就开始退避（×2），第 3 次 ×4，依此类推；封顶后恒为 ×factor^max_streak。
        effective_streak = max(
            0, min(streak + 1, max(0, int(config.unanswered_backoff_max_streak)))
        )
    else:
        effective_streak = 0
    backoff = max(1.0, float(config.unanswered_backoff_factor)) ** effective_streak
    interval_seconds = max(0, int(config.min_interval_minutes)) * 60 * backoff
    if last_proactive > 0 and (float(now) - last_proactive) < interval_seconds:
        remain = (interval_seconds - (float(now) - last_proactive)) / 60.0
        return ProactiveDecision(
            False, REASON_INTERVAL,
            material=material,
            backoff=backoff,
            detail=(
                f"距上次主动还差 {remain:.0f} 分钟"
                + (f"（未回应退避 ×{backoff:g}）" if effective_streak else "")
            ),
        )

    silence_seconds = max(0, int(config.recent_user_silence_minutes)) * 60
    if last_user > 0 and (float(now) - last_user) < silence_seconds:
        return ProactiveDecision(False, REASON_SILENCE, material=material, detail="对方刚说过话")

    score, detail = score_of(
        material_weight=_as_float(
            material.get("effective_weight"), _as_float(material.get("weight"), 0.0)
        ),
        energy=energy,
        emotion=emotion,
        config=config,
    )
    if score < float(config.score_threshold):
        return ProactiveDecision(False, REASON_LOW_SCORE, score=score,
                                 material=material, detail=detail)

    return ProactiveDecision(
        True,
        REASON_OK,
        score=score,
        material=material,
        intent=build_intent(material),
        detail=detail,
    )


# ---------------------------------------------------------------- 会话记录


def new_session_record(*, stream_id: str, day_key: str) -> dict[str, Any]:
    return {
        "stream_id": str(stream_id),
        "last_user_message_at": 0.0,
        "last_proactive_at": 0.0,
        "day_key": str(day_key),
        "count": 0,
        "unanswered_streak": 0,
    }


def record_user_message(
    sessions: dict[str, dict[str, Any]],
    *,
    stream_id: str,
    now: float,
    day_key: str,
) -> dict[str, Any]:
    """记下对方最近一次说话的时间（供 ``silence`` 硬闸使用）。"""

    existing = sessions.get(stream_id)
    record = dict(existing) if isinstance(existing, Mapping) else new_session_record(
        stream_id=stream_id, day_key=day_key
    )
    record["stream_id"] = str(stream_id)
    record["last_user_message_at"] = float(now)
    if str(record.get("day_key", "")) != str(day_key):
        record["day_key"] = str(day_key)
        record["count"] = 0
    sessions[str(stream_id)] = record
    return record


def record_proactive(
    sessions: dict[str, dict[str, Any]],
    *,
    stream_id: str,
    now: float,
    day_key: str,
) -> dict[str, Any]:
    """记一次主动开口（若宿主 Planner 最终沉默，这也只算「已尝试」）。"""

    existing = sessions.get(stream_id)
    record = dict(existing) if isinstance(existing, Mapping) else new_session_record(
        stream_id=stream_id, day_key=day_key
    )
    record["stream_id"] = str(stream_id)
    prev_proactive = _as_float(record.get("last_proactive_at"), 0.0)
    prev_user = _as_float(record.get("last_user_message_at"), 0.0)
    record["last_proactive_at"] = float(now)
    # 未回应连击：上一次主动之后对方始终没说话 ⇒ 连击 +1；对方回过话就归零。
    # 群聊也统一记账（decide 那侧按 private_chat 决定是否启用），多记无副作用。
    record["unanswered_streak"] = (
        _as_int(record.get("unanswered_streak"), 0) + 1
        if prev_proactive > 0.0 and prev_proactive > prev_user
        else 0
    )
    if str(record.get("day_key", "")) != str(day_key):
        record["day_key"] = str(day_key)
        record["count"] = 1
    else:
        record["count"] = _as_int(record.get("count"), 0) + 1
    sessions[str(stream_id)] = record
    return record


def bump_skip_ledger(ledger: dict[str, int], reason: str, *, amount: int = 1) -> None:
    """沉默台账 +1（``skipped`` 等原因也走这里，便于 /生活 为什么 汇总）。"""

    key = str(reason or "unknown")
    ledger[key] = int(ledger.get(key, 0)) + int(amount)


def ledger_lines(ledger: Mapping[str, Any], *, limit: int = 10) -> list[str]:
    """把台账排成「原因：次数」的文本行，按次数降序。

    键允许是 ``"<原因>×<档位>"`` 的复合形式——未回应退避（G1）会按档位分桶，
    这样 ``/生活 为什么`` 能答出「退避到了第几级」而不是一个笼统的
    「距上次主动太近」。渲染时把原因换成中文标签、档位原样附在后面：
    ``interval×4`` → ``距上次主动太近 ×4：3 次``。
    """

    items: list[tuple[str, int]] = []
    for key, value in ledger.items():
        try:
            items.append((str(key), int(value)))
        except (TypeError, ValueError):
            continue
    items.sort(key=lambda pair: pair[1], reverse=True)
    lines: list[str] = []
    for name, count in items[: max(1, int(limit))]:
        reason, _, level = name.partition("×")
        label = SKIP_REASON_LABELS.get(reason, reason)
        lines.append(
            f"{label} ×{level}：{count} 次" if level else f"{label}：{count} 次"
        )
    return lines
