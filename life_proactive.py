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
7. 上次开口待核实 → ``pending``
8. 距上次尝试太近 → ``attempt_cooldown``
9. 距上次主动太近 → ``interval``
10. 连续没回应到顶 → ``unanswered_cap``
11. 对方刚说过话  → ``silence``
12. 分数不够      → ``low_score``

第 7~10 条是 v1.18.0 借 mohobot ``proactive_chat`` 的机制补上的四道闸，语义分别是：
**受理≠发送**（``pending`` 待核实）、**失败要退避**（``attempt_cooldown``）、
**节奏要抖动**（``interval`` 里的确定性倍率）、**止损要彻底**（``unanswered_cap``）。

每次沉默的原因都记进 ``skip_ledger``，``/生活 为什么`` 直接回答「她怎么不说话了」。
"""

from __future__ import annotations

import hashlib
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
#: v1.18.0：上一次开口已被宿主受理、但还没核实她是否真的说出了口。
REASON_PENDING = "pending"
#: v1.18.0：上一次尝试没成（触发失败 / 确认没说出口），短期内不再重试。
REASON_ATTEMPT_COOLDOWN = "attempt_cooldown"
#: v1.18.0：对方连续未回应达到硬上限，彻底停口（对方回复即恢复）。
REASON_UNANSWERED_CAP = "unanswered_cap"

#: 台账专用的非「沉默原因」键：一次开口被确认说出去了。
LEDGER_SENT = "sent"
#: 台账专用：宿主受理了开口，但 Planner 最终沉默（话到嘴边没说出口）。
LEDGER_NOT_SENT = "not_sent"

SKIP_REASON_LABELS: dict[str, str] = {
    REASON_DISABLED: "主动开口未启用",
    REASON_SLEEPING: "她在睡觉",
    REASON_LOW_ENERGY: "体力低于下限",
    REASON_QUIET: "命中静默时段",
    REASON_NO_MATERIAL: "没有没过期的素材",
    REASON_DAILY_MAX: "今天主动次数已用完",
    REASON_PENDING: "上一次开口还没确认说没说出口",
    REASON_ATTEMPT_COOLDOWN: "上次尝试没成，冷却中",
    REASON_INTERVAL: "距上次主动还不够久",
    REASON_UNANSWERED_CAP: "对方连续没回应，先不打扰了",
    REASON_SILENCE: "对方刚说过话，先不插嘴",
    REASON_LOW_SCORE: "素材权重+状态偏置没到阈值",
    LEDGER_SENT: "已经开口了",
    LEDGER_NOT_SENT: "话到嘴边没说出口",
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
    #: 未回应退避（G1，v1.22.2 起默认私聊+群聊；见 ``unanswered_backoff_group``）：
    #: 对方连续不回应时，最小间隔按 factor^连击 拉长，
    #: 连击封顶 max_streak。默认 2^4 = 16 倍（180 分钟 → 48 小时）：封顶必须**大于**
    #: 「每天一次」的自然节奏（24h），否则在默认 ``daily_max=1`` 下退避永远不是约束
    #: 条件（实测：封顶 24h 时间隔恒为 24h，与不开退避逐位相同），而且刚好卡在
    #: ``elapsed < interval`` 的边界上、行为取决于当天开口的钟点。48h 让「隔天一次」
    #: 成为确定性结果。factor ≤ 1 视为关闭；对方一回话，连击判定立即失效。
    unanswered_backoff_factor: float = 2.0
    unanswered_backoff_max_streak: int = 4
    #: 未回应退避是否**对群聊也生效**（v1.22.2，默认开）。
    #:
    #: 原本只有私聊退避（群聊假设「总会有人接」）。一旦允许她去活跃冷群，那个假设就
    #: 反过来了：**没人的群恰恰是最不可能有人接的群**，不退避意味着她会在同一个死群里
    #: 每天准时抛一次话头，永远没人答——这是这个功能最典型的失败形态（把自己刷成噪音）。
    #: 置 ``False`` 精确回到 v1.22.1 的「仅私聊退避」（逐位一致，有回归用例）。
    unanswered_backoff_group: bool = True
    #: 冷群搭话总开关（v1.22.2，默认**关**）。开启后：群聊超过 ``cold_group_hours``
    #: 没人说话时，她可以抛一条「能被回答」的话头（``life_topic``）。素材按会话即时
    #: 生成、不落盘、不进全局素材池，所以不会漂到别的群。
    cold_group_enabled: bool = False
    #: 「冷」的判据（小时）：最后一条人类消息距今超过它就算冷。0 = 关闭。
    #: 刻意**不是**「多久主动一次」——节奏仍由 ``min_interval_minutes`` 与退避管。
    cold_group_hours: float = 2.0
    #: 触发失败 / 确认没说出口后的冷却（分钟，v1.18.0）。0 = 关闭。
    #: 没有它时，宿主持续故障会让每个 sim tick（≥60 秒）都全量重评一次并刷一条 warning；
    #: 有了它，一次没成的尝试把节奏推到 15 分钟后，而不是下个 tick 立刻重来。
    #: 成功后自动失效（判据是 ``last_attempt_at > last_proactive_at``），不需要清字段。
    failure_cooldown_minutes: int = 15
    #: 开口后延迟核实「她到底说没说出口」（v1.18.0，默认开）。宿主的
    #: ``maisaka.proactive.trigger`` 只回受理回执（``queued: True``），Planner 仍可沉默——
    #: 受理就计数会让 ``daily_max=1`` 的当天配额被一次沉默白吃掉。
    confirm_send: bool = True
    #: 核实延迟（分钟）：给宿主的强制轮留足跑完的时间。
    confirm_delay_minutes: int = 10
    #: 最小间隔的确定性抖动比例（v1.18.0，默认关）。0 = 精确间隔（与旧行为逐位一致）；
    #: 0.15 = 间隔随机上浮 0~15%。倍率由 ``(生活日, 会话, 当天第几次)`` 派生，
    #: 同一天同一会话恒同值 ⇒ tick 间不重掷、重启一致，也不破坏退避的确定性断言。
    interval_jitter_ratio: float = 0.0
    #: 连续未回应硬上限（v1.18.0，默认关 = 只靠退避；适用面同 ``unanswered_backoff_group``）。
    #: 退避管节奏、上限管止损：
    #: 退避封顶后她仍可隔天一条，而上限到了就彻底停口，对方一回复即恢复。
    max_unanswered_times: int = 0
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


def _strict_true(value: Any) -> bool:
    """严格真值：``bool("false")`` 是 True，这类标记位必须按字面判。

    与插件里 ``flag_value`` 的口径一致（宿主把 bool 序列化成字符串时，
    ``"false"`` 会让宽松判断把「没开」当成「开了」）。
    """

    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def select_material(
    materials: Sequence[Mapping[str, Any]],
    now: float,
    floor: float = 0.25,
) -> dict[str, Any] | None:
    """挑一条未过期素材：**独占素材优先**，否则取有效权重最高的一条。

    有效权重 = 原始权重 × 时效系数（与 ``life_sim.material_freshness`` 同一口径）：
    过了「最佳保鲜相位」的念头低价值化、自然排到队尾，而不是占着榜首直到过期
    那一刻突然消失。返回的是**副本**，附 ``effective_weight`` 供评分使用——
    评分用衰减后的权重，「低价值化」才真正落到开口分数上。

    ``exclusive``（v1.22.3，冷群话头）：会话已经冷下来时，话头**越过权重比较**直接
    胜出。理由是它跟别的素材竞争没有意义——事件素材权重 0.30–0.85、关系维护 0.70，
    而话头只有 0.55，池子里几乎总有更重的条目压着，「冷了就抛一条」会退化成「几乎
    永远不抛」。**独占只改选材，不改任何闸**：静默时段、体力下限、每日上限、最小
    间隔、未回应退避、评分阈值全部照旧生效（``decide`` 里在它之后才判）。
    多条独占同时存在时仍按有效权重取最高，保持确定性。
    """

    best: dict[str, Any] | None = None
    best_weight = float("-inf")
    exclusive: dict[str, Any] | None = None
    exclusive_weight = float("-inf")
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
        if _strict_true(item.get("exclusive")):
            if weight > exclusive_weight:
                exclusive = dict(item)
                exclusive_weight = weight
            continue
        if weight > best_weight:
            best_weight = weight
            best = dict(item)
    if exclusive is not None:
        exclusive["effective_weight"] = exclusive_weight
        return exclusive
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


#: 群聊/私聊两套引导语（v1.22.1）。**同一句素材，两种场合说成两种东西**：
#: 私聊里「想聊聊刚发生的一件事」说得通（对面就是那个人），群聊里没有对象，
#: 说成「讲故事」必然变成独白。真机实录（2026-10-18）：
#: 「诶，跟你说个事儿——刚有个八百年没联系的人突然给我发消息，我盯着那条提示看了半天，
#: 都没敢点开。」「刚下楼碰到一只猫，居然肯让我摸！好亲人啊它」——两句都是**汇报**，
#: 说完就完了，旁人无从接口。
PRIVATE_SCENE = "想自然地聊聊刚发生的一件事"
GROUP_SCENE = "在群里随口说一句"

#: 群聊开口的**合格条件**（纪律类规则，写在代码层，理由同 ``NO_QUOTE_DISCIPLINE``）。
#:
#: 三条要求各对应一个真实症状：
#: 1. **一句话、不超过 N 字** —— 治「三句叙事独白」；
#: 2. **说态度，不汇报事实** —— 治「内容无信息量」（「楼下有只猫」是事实，
#:    「这猫比我熟这条街」才是可以接的话）；
#: 3. **结尾留落子** —— 治「句号式封闭」（旁人没有插入点）；
#: 外加两条禁令：不许编具体次数/时长（真机出现过「八百年没联系」「看了半天」），
#: 不许编别人说过的话（那是把记忆当剧本写）。
GROUP_INTENT_MAX_CHARS = 30


def _catchable_hint() -> str:
    """群聊口径的约束句。**字数取自常量**，避免文案与数字两处漂移。"""

    return (
        f"按群聊的方式说：一句话、不超过 {GROUP_INTENT_MAX_CHARS} 字；说你的态度或吐槽，"
        "不要汇报事实；结尾留一个别人能接的落子（一句自嘲、一句反问，或者求个认同）。"
        "不要编造具体次数与时长，也不要编别人说过的话。"
    )


#: 私聊的约束句：不要求「留落子」（对面就在跟你说话），但同样禁止编细节——
#: 编出来的「八百年」「盯着看了半天」正是让整段话变假的来源。
_PRIVATE_BRIEF_HINT = "一句话说完；不要编造具体次数与时长，也不要编别人说过的话。"


def build_intent(material: Mapping[str, Any], *, private_chat: bool = True) -> str:
    """把素材拼成交给宿主的主动意图文本（含代码层的发言纪律）。

    ``intro``（v1.22.0，长期记忆素材）：素材自带引导语时用它替换默认引导语。
    没有这个键的素材**逐字走旧措辞**——记忆素材说的是一件旧事，说成「刚发生」是事实错误。

    ``private_chat``（v1.22.1）：**默认 True = 私聊口径**（与历史行为逐字一致，
    老调用方与老测试不受影响）。群聊与「判不出类型的会话」应传 ``False``——
    与 ``decide`` 里「判不出类型按群聊处理」同一条纪律。
    """

    text = sanitize_text(material.get("text", ""), max_chars=80)
    label = sanitize_text(material.get("label", ""), max_chars=32)
    intro = sanitize_text(material.get("intro", ""), max_chars=40)
    if private_chat:
        scene = intro or PRIVATE_SCENE
        hint = _PRIVATE_BRIEF_HINT
    else:
        scene = intro or GROUP_SCENE
        hint = _catchable_hint()
    if text:
        base = f"{scene}（{label or '生活小事'}）：{text}"
    else:
        base = f"{scene}（{label or '生活小事'}）"
    return f"{base} {hint} {NO_QUOTE_DISCIPLINE}"


# ---------------------------------------------------------------- 间隔抖动


def interval_jitter(seed_key: str, ratio: float) -> float:
    """开口间隔的确定性抖动倍率：``1.0 ~ 1.0 + ratio``（v1.18.0）。

    为什么用**确定性派生**而不是真随机：间隔闸每 tick 都要重算一次，掷骰子会让
    剩余时间在两次 tick 之间反复跳（「还差 3 分钟」→「还差 12 分钟」），既不像人
    也没法测。``sha1(种子)`` 让「同一天 + 同一会话 + 当天第几次」恒得同一个倍率，
    于是抖动只影响**节奏**，不影响任何一次判定的可复现性（与
    ``life_activity.schedule_day_shift`` 同一手法）。

    ``ratio ≤ 0`` 直接返回 ``1.0``：默认配置下与旧行为**逐位一致**。
    用 ``hashlib.sha1`` 而不是内置 ``hash()``——后者带随机盐，重启后种子全变。
    """

    try:
        value = float(ratio or 0.0)
    except (TypeError, ValueError):
        return 1.0
    spread = max(0.0, min(1.0, value))
    if spread <= 0.0:
        return 1.0
    digest = hashlib.sha1(f"proactive-jitter|{seed_key}".encode("utf-8")).hexdigest()
    roll = int(digest[:8], 16) % 10_000
    return 1.0 + (roll / 10_000.0) * spread


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

    # 待核实闸（v1.18.0）：只要上一次开口还挂着没核销，就先不开新的。
    # 刻意**不看**它到没到点：核销在每轮判定**之前**跑（``_verify_pending_proactive``），
    # 能核销的早就核销掉了；还挂着的（本轮配额不够、或者形态坏掉）恰恰正是该拦住
    # 这个会话的情况。否则「受理≠发送」的空窗里会连开好几枪，等于把 daily_max
    # 的约束力交给一个可能根本不说话的 Planner。
    if pending_verify_of(record) is not None:
        return ProactiveDecision(
            False, REASON_PENDING,
            material=material,
            detail="上一次开口还没核实说没说出口",
        )

    last_proactive = _as_float(record.get("last_proactive_at"), 0.0)
    last_user = _as_float(record.get("last_user_message_at"), 0.0)
    # 尝试冷却（v1.18.0）：上一次尝试没成（触发失败 / Planner 沉默）后短期内不重试。
    # 判据 ``last_attempt > last_proactive`` 让「成功之后冷却自动失效」，不必清字段。
    # 没有它时宿主持续故障 = 每个 sim tick 全量重评 + 一条 warning。
    last_attempt = _as_float(record.get("last_attempt_at"), 0.0)
    cooldown_seconds = max(0, int(config.failure_cooldown_minutes)) * 60
    if (
        cooldown_seconds > 0
        and last_attempt > last_proactive
        and (float(now) - last_attempt) < cooldown_seconds
    ):
        remain_attempt = (cooldown_seconds - (float(now) - last_attempt)) / 60.0
        return ProactiveDecision(
            False, REASON_ATTEMPT_COOLDOWN,
            material=material,
            detail=f"距上次没成的尝试还差 {remain_attempt:.0f} 分钟",
        )
    # 未回应退避（G1）：上次主动之后对方一直没说话 ⇒ 本次开口的间隔按
    # factor^连击 拉长（连击封顶）。对方回过话则连击视为 0——判定是即时的，
    # 对方一发言 last_user 就会超过 last_proactive，不需要落库清零。
    # 群聊 / 判不出会话类型 / 系数 ≤ 1 一律不退避（维持现有硬闸）。
    streak = _as_int(record.get("unanswered_streak"), 0)
    # 「上次主动之后对方一直没说话」——退避与硬上限**共用**这个前置判据，
    # 但两者的封顶各自独立：退避封顶（max_streak）是为了不让间隔涨到离谱，
    # 硬上限要比的是**真实连击数**，用被封顶的 effective_streak 会永远比不过 cap。
    awaiting_reply = last_proactive > 0.0 and last_proactive > last_user
    # v1.22.2：退避的适用面由「私聊」扩成「私聊 + 群聊（可关）」。上面的说明仍成立
    # ——判定是即时的：对方一发言 ``last_user`` 就会超过 ``last_proactive``，
    # 连击自动归零，不需要落库清零。
    backoff_scope = private_chat or bool(config.unanswered_backoff_group)
    if backoff_scope and float(config.unanswered_backoff_factor) > 1.0 and awaiting_reply:
        # +1：把「上一次主动至今未回应」这一次也计进连击——这样第 2 次开口
        # 就开始退避（×2），第 3 次 ×4，依此类推；封顶后恒为 ×factor^max_streak。
        effective_streak = max(
            0, min(streak + 1, max(0, int(config.unanswered_backoff_max_streak)))
        )
    else:
        effective_streak = 0
    backoff = max(1.0, float(config.unanswered_backoff_factor)) ** effective_streak
    # 确定性抖动（v1.18.0，默认关）：让「到期后第一个 tick 必发」的机械感消失。
    # 种子含当天次数 ⇒ 同一天同一会话恒同倍率（tick 间不重掷、重启一致）。
    jitter = interval_jitter(
        f"{day_key}|{record.get('stream_id', '')}|{count_today}",
        float(config.interval_jitter_ratio),
    )
    interval_seconds = max(0, int(config.min_interval_minutes)) * 60 * backoff * jitter
    if last_proactive > 0 and (float(now) - last_proactive) < interval_seconds:
        remain = (interval_seconds - (float(now) - last_proactive)) / 60.0
        return ProactiveDecision(
            False, REASON_INTERVAL,
            material=material,
            backoff=backoff,
            detail=(
                f"距上次主动还差 {remain:.0f} 分钟"
                + (f"（未回应退避 ×{backoff:g}）" if effective_streak else "")
                + (f"（抖动 ×{jitter:.2f}）" if jitter > 1.0 else "")
            ),
        )

    # 未回应硬上限（v1.18.0，仅私聊，默认关）：退避管节奏、上限管止损。
    # 比的是**真实连击** ``streak + 1``（这一次就是第 streak+1 次开口），不是被
    # 退避封顶的 effective_streak：cap=4 ⇒ 第 1~4 次放行、第 5 次停；
    # 对方一回复 ``last_proactive > last_user`` 不再成立 ⇒ 自动恢复。
    cap = max(0, int(config.max_unanswered_times))
    if backoff_scope and cap > 0 and awaiting_reply and (streak + 1) > cap:
        return ProactiveDecision(
            False, REASON_UNANSWERED_CAP,
            material=material,
            backoff=backoff,
            detail=f"连续未回应已达 {cap} 次上限（累计连击 {streak}）",
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
        intent=build_intent(material, private_chat=private_chat),
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
    """记一次**已被确认说出去了**的主动开口（v1.18.0 起语义收窄）。

    ``now`` 允许**倒签**：延迟核销要按「她开口的那一刻」记账，配额算在开口那天、
    间隔从开口时刻起算，而不是从核实时刻起算（否则多发一条后核销会把间隔闸推后）。
    """

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
    # 核销完成 ⇒ 待核实标记清掉（成功与失败两条出口都走这里）
    clear_pending_verify(record)
    sessions[str(stream_id)] = record
    return record


def record_attempt(
    sessions: dict[str, dict[str, Any]],
    *,
    stream_id: str,
    now: float,
    day_key: str,
) -> dict[str, Any]:
    """记一次**没发出去**的尝试（v1.18.0）：触发失败 / 确认 Planner 沉默了。

    只动 ``last_attempt_at`` 这个冷却锚点——**不碰** ``count``（配额没消耗）、
    ``unanswered_streak``（对方没被冷落，是她没说）、``last_proactive_at``
    （间隔闸不该被一次没成的尝试推后，否则失败会顺带惩罚下一次开口）。
    """

    existing = sessions.get(stream_id)
    record = dict(existing) if isinstance(existing, Mapping) else new_session_record(
        stream_id=stream_id, day_key=day_key
    )
    record["stream_id"] = str(stream_id)
    record["last_attempt_at"] = float(now)
    clear_pending_verify(record)
    sessions[str(stream_id)] = record
    return record


def record_pending_verify(
    sessions: dict[str, dict[str, Any]],
    *,
    stream_id: str,
    trigger_at: float,
    verify_after: float,
    day_key: str,
) -> dict[str, Any]:
    """挂起一次开口的核销（v1.18.0）：宿主已受理，待核实她是否真的说了话。

    ``day_key`` 存的是**开口那一刻**的生活日：核销可能跨过日界，倒签
    ``record_proactive`` 要用它，配额才算在开口那天。
    """

    existing = sessions.get(stream_id)
    record = dict(existing) if isinstance(existing, Mapping) else new_session_record(
        stream_id=stream_id, day_key=day_key
    )
    record["stream_id"] = str(stream_id)
    record["pending_verify"] = {
        "trigger_at": float(trigger_at),
        "verify_after": float(verify_after),
        "day_key": str(day_key),
    }
    sessions[str(stream_id)] = record
    return record


def pending_verify_of(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """取出待核实标记；形态不合法（被外部写坏）一律当没有。"""

    if not isinstance(record, Mapping):
        return None
    pending = record.get("pending_verify")
    if not isinstance(pending, Mapping):
        return None
    trigger_at = _as_float(pending.get("trigger_at"), 0.0)
    verify_after = _as_float(pending.get("verify_after"), 0.0)
    if trigger_at <= 0.0 or verify_after <= 0.0:
        return None
    return {
        "trigger_at": trigger_at,
        "verify_after": verify_after,
        "day_key": str(pending.get("day_key", "") or ""),
    }


def clear_pending_verify(record: dict[str, Any]) -> None:
    """清掉待核实标记（就地改）。"""

    record.pop("pending_verify", None)


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
