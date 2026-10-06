# -*- coding: utf-8 -*-
"""宿主「回复触发模式」的两套公式与后果预览（纯函数，无 ctx、无 IO）。

本模块只做一件事：把 MaiBot 1.3.1 用来决定「新消息何时进入 Planner」的两套规则
原样复刻成纯函数，好让 /生活 命令能报出**真实后果**，而不是让插件自己另算一套。

核源位置（MaiBot 1.3.1，本机源码实测）：

- ``src/maisaka/mode_policy.py:13-22``        reply_trigger_mode 的判定
- ``src/config/official_configs.py:599``      Literal["frequency","reply_necessity"]，默认 frequency
- ``src/maisaka/runtime.py:1129-1136``        触发阈值 T
- ``src/maisaka/reply_necessity.py:8-15``     触发线与压力分常量
- ``src/maisaka/reply_necessity.py:136-194``  必要性评分
- ``src/maisaka/reply_necessity.py:212-236``  压力分（阈值内二次、超阈值对数）
- ``src/maisaka/turn_gates.py:118-196``       计数门 + 空窗补偿

两条通路的关键差异（README 也会写）：

- ``frequency``（宿主默认）：只看「攒够几条」与「静了多久」，**不看内容**。
  ``T = ceil(1/freq)``，另有空窗补偿（沉默可折算消息数，但封顶 ``T-1``，
  保证纯沉默不能自我触发）。
- ``reply_necessity``：``T = ceil(1/freq²)``，且要评分过关
  ``final = round((相关性 + 内容 + 压力) × (0.5 + 0.5·min(1,freq))) >= 80``。
  **注意内容分不是 0**：宿主按整批拼接长度给 +5/+10（两个 ``if`` 叠加），
  所以「纯闲聊能不能推得动」是一条**随批次增长**的曲线，不是一条固定频率的断崖。
  把内容分当 0 会得到「freq < 0.59 就永远推不动」这个**错误结论**（v1.1.0 的 bug，
  v1.1.1 修正）：按每条 8 字估算，freq=0.58 约 13 条即可触发，freq=0.45 约 21 条。
  真正的不可达区间要同时看内容分，见 ``plain_chatter_messages_needed``。
"""

from __future__ import annotations

from math import ceil, log1p

# ---------------------------------------------------------------- 模式

MODE_FREQUENCY = "frequency"
MODE_REPLY_NECESSITY = "reply_necessity"
MODE_DYNAMIC = "dynamic"
"""三条通路的标识。

- ``frequency`` / ``dynamic`` 是 MaiBot 1.3.2 的两个合法取值
  （``src/config/official_configs.py``：``Literal["frequency", "dynamic"]``）。
- ``reply_necessity`` 是 1.3.1 及更早的取值，1.3.2 用 ``dynamic`` 取代了它
  （``turn_trigger/`` 里只剩计数门与动态门，没有了必要性门）。保留识别它是为了
  兼容仍跑旧宿主的部署；新宿主不会再返回这个值。
"""

DEFAULT_MODE = MODE_FREQUENCY
"""宿主默认值（``official_configs.py``）。读不到配置时按它处理。"""

ALL_MODES = (MODE_FREQUENCY, MODE_REPLY_NECESSITY, MODE_DYNAMIC)

MODE_LABELS = {
    MODE_FREQUENCY: "计数门（frequency）",
    MODE_REPLY_NECESSITY: "评分门（reply_necessity·宿主 ≤1.3.1）",
    MODE_DYNAMIC: "动态触发（dynamic）",
}


def normalize_mode(value: object) -> str:
    """把任意配置值归一化成三个合法模式之一。

    宿主 1.3.2 只接受 ``frequency`` / ``dynamic``；其余（含 ``None``、空串、
    大小写差异、旧配置里的 ``reply_necessity``、脏值）一律按宿主默认 ``frequency``
    处理，绝不抛错——这个函数会在每个 tick 被调用，不能因为一个脏配置把后台循环打死。
    """

    text = str(value or "").strip().lower()
    return text if text in ALL_MODES else DEFAULT_MODE


# ---------------------------------------------------------------- 常量

NECESSITY_TRIGGER_SCORE = 80
"""``reply_necessity.py:8``：评分达到它才算进入 Planner。"""

PRESSURE_STANDARD_SCORE = 50
"""``reply_necessity.py:9``：压力分基准（阈值处取到）。"""

PRESSURE_MAX_SCORE = 100
"""``reply_necessity.py:10``：压力分上限。"""

PRESSURE_FULL_RATIO = 5.0
"""``reply_necessity.py:11``：积压达到阈值的 5 倍时压力分打满。"""

IDLE_PRESSURE_BONUS = 15
"""``reply_necessity.py:12``：闲置达到平均消息间隔时的额外压力分。"""

# 相关性分：reply_necessity.py:136-153
RELEVANCE_AT = 100
RELEVANCE_MENTION = 80
RELEVANCE_INDIRECT = 40  # 私聊 或 focus
RELEVANCE_PLAIN_GROUP = 0

# 内容分：reply_necessity.py:239-277
CONTENT_QUESTION = 15
CONTENT_REQUEST = 20
CONTENT_OPINION = 20
CONTENT_LONG_TEXT = 5
CONTENT_VERY_LONG_TEXT = 10
CONTENT_SHORT_REACTION = -25

CONTENT_MAX_SCORE = (
    CONTENT_QUESTION + CONTENT_REQUEST + CONTENT_OPINION + CONTENT_LONG_TEXT + CONTENT_VERY_LONG_TEXT
)
"""内容分上限 70：问题 15 + 请求 20 + 征询 20 + 长文本 5 + 较长文本 10。

⚠ 两个长度加成**会叠加**（``reply_necessity.py:267-273`` 是两个独立 if，不是 elif）——
这里一开始漏算成 65，被 ``test_host_model`` 里直接调用宿主 ``_score_content`` 的真值校验抓出来，
所以那条断言现在写的是**相等**而不是上界。

配合 ``RELEVANCE_AT = 100``：``(100 + 70) × 0.5 = 85 ≥ 80``。
这就是「倍率只要不是**精确的 0**，@ 在评分门下就能穿透」的原因——
静默下限 ``silence_floor > 0`` 会以这种方式漏话，见 README「与其它插件共存」。
"""

PRESENCE_PENALTY_MAX = 25
"""``reply_necessity.py:15``：最近 5 分钟窗口内自己刷屏的扣分上限。"""

DEFAULT_CHATTER_MESSAGE_LENGTH = 8
"""「纯闲聊需要几条」这个结论背后的建模假设：一条群聊闲聊约 8 个字。

宿主的**内容分**来自整批拼接长度（``reply_necessity.py:158/267-273``），所以
「同一批里有多少条」和「这批有多长」是耦合的；不写明这个假设，
`/生活 频率` 印出的数字就无从解释。用户消息更长时会更早触发（更少条数）。

⚠ 这个假设只对计数门 / 评分门（宿主 ≤1.3.1）有意义：1.3.2 的动态门不数条数。
"""


# ---------------------------------------------------------------- 动态门（MaiBot 1.3.2）

DYNAMIC_WINDOW_SECONDS = 3600.0
"""动态门的统计窗口：1 小时（``turn_trigger/dynamic_gate.py``）。"""

DYNAMIC_FORCED_EXPECTED_REPLIES = 0.91
"""一次 @ 强制触发按 0.91 次「预期回复」计入窗口（``dynamic_gate.py`` 的
``FORCED_TURN_EXPECTED_REPLIES``，拟合数据中的实测均值）。"""

DYNAMIC_VIRTUAL_ROUND_SECONDS = 40.0
"""预期回复数按「虚拟轮次」累计：同一时长内到达的消息归为一轮、只计一次
（``VIRTUAL_ROUND_SECONDS``）。不设门控时 Planner 本来就不是每条消息判一次，
思考耗时/空闲退避/wait 会把消息攒成一批（拟合数据里平均每批 2.9 条）。"""

DYNAMIC_MIN_WINDOW_SCORE_COUNT = 20
"""窗口内样本少于它时用静态阈值表；够了才按「概率从高到低累积到预算」定门槛。"""

DYNAMIC_STATIC_KEEP_RATIO_THRESHOLDS: tuple[tuple[float, float], ...] = (
    (0.0, 1.0),
    (0.1, 0.605),
    (0.2, 0.482),
    (0.3, 0.430),
    (0.4, 0.376),
    (0.5, 0.336),
    (0.6, 0.313),
    (0.7, 0.290),
    (0.8, 0.250),
    (0.9, 0.206),
    (1.0, 0.0),
)
"""宿主 ``_STATIC_KEEP_RATIO_THRESHOLDS``：小样本时 keep_ratio → 概率门槛（分段线性）。"""


def dynamic_static_threshold(keep_ratio: float) -> float:
    """小样本下的静态概率门槛（复刻 ``dynamic_gate._static_threshold``）。"""

    ratio = min(1.0, max(0.0, float(keep_ratio)))
    for index in range(1, len(DYNAMIC_STATIC_KEEP_RATIO_THRESHOLDS)):
        low_ratio, low_threshold = DYNAMIC_STATIC_KEEP_RATIO_THRESHOLDS[index - 1]
        high_ratio, high_threshold = DYNAMIC_STATIC_KEEP_RATIO_THRESHOLDS[index]
        if ratio <= high_ratio:
            span = high_ratio - low_ratio
            if span <= 0:
                return float(high_threshold)
            return low_threshold + (high_threshold - low_threshold) * (
                (ratio - low_ratio) / span
            )
    return 0.0


def dynamic_keep_ratio(effective_frequency: float) -> float:
    """动态门里的「目标回复比例」= ``min(1, max(0, 生效频率))``。

    ⚠ 与计数门最大的区别：倍率在这里不是「攒几条」的刻度，而是**窗口内保留多少
    比例的预期回复机会**。生效频率 ≥ 1 时 keep_ratio=1、静态门槛 0（几乎全放行），
    所以倍率 >1 在动态门下没有额外上行空间——想让她整体更活跃要调宿主的
    ``talk_value``，本插件的倍率只负责往下压（睡眠=0、感冒、低情绪体力…）。
    """

    return min(1.0, max(0.0, float(effective_frequency)))


# ---------------------------------------------------------------- 门控


def clamp_frequency(effective_frequency: float) -> float:
    """宿主的统一处理：``min(1.0, freq)``，负数归零（``runtime.py:1131``）。"""

    return min(1.0, max(0.0, float(effective_frequency)))


def is_silent(effective_frequency: float) -> bool:
    """生效频率是否触发静默模式（``runtime.py:1032-1034``）。

    静默为真时宿主走 ``_handle_silent_turn``：消息被静默消费，不进
    Planner/Replyer，零模型开销（``reasoning_engine.py:1083-1089``）。
    注意该分支位于「@ 强制触发」检查**之前**（``turn_scheduler.py:90`` 早于 ``:98``），
    所以静默时 @ 也穿透不了。
    """

    return float(effective_frequency) <= 0.0


def trigger_threshold(mode: str, effective_frequency: float) -> int:
    """折算「攒够几条消息」的门槛。

    - ``frequency`` / ``dynamic``（宿主 1.3.2）：``max(1, ceil(1/freq))``。宿主
      ``runtime.py::_get_message_trigger_threshold`` **不区分模式**都是这个公式。
      ⚠ 在 ``dynamic`` 模式下它只是**日志与等待节奏**用的条数（宿主把它印在
      「[频率: x][pending/T 消息]」里），真正放行由概率门决定——见
      ``dynamic_static_threshold`` 与 ``preview``。
    - ``reply_necessity``（宿主 ≤1.3.1）：``max(1, ceil(1/freq²))``。

    返回 ``0`` 表示静默（宿主在该分支直接返回 0）。
    """

    frequency = clamp_frequency(effective_frequency)
    if frequency <= 0.0:
        return 0
    # v1.8.2 修：钳一个下限。病态小值（如 1e-160，来自配坏的 min_adjust/silence_floor）
    # 会让 1/f² 下溢为 0（ZeroDivisionError）或 1/f 溢出为 inf（ceil(inf) 抛
    # OverflowError）——normalize_mode 的纪律是「绝不抛错」，这里对齐。
    # 1e-6 以下在宿主眼里与 0 无异，门槛给个有限的大数即可（仅用于预览展示）。
    frequency = max(frequency, 1e-6)
    if normalize_mode(mode) == MODE_REPLY_NECESSITY:
        return max(1, int(ceil(1.0 / (frequency * frequency))))
    return max(1, int(ceil(1.0 / frequency)))


def necessity_factor(effective_frequency: float) -> float:
    """复刻 ``reply_necessity.py:175-176``：评分乘数 ``0.5 + 0.5·min(1,freq)``。

    值域 ``[0.5, 1.0]``——即使频率很低，评分也只被砍一半，不会直接归零。
    """

    return 0.5 + 0.5 * clamp_frequency(effective_frequency)


def pressure_score(
    *,
    pending_count: int,
    threshold: int,
    idle_reached_average: bool = False,
) -> int:
    """复刻 ``reply_necessity.py:212-236``：积压压力分。

    阈值内按平方增长；超过阈值后按 ``log1p`` 增长（不是线性），
    在 ``pending = 5×threshold`` 处打满 ``PRESSURE_MAX_SCORE``。
    """

    normalized_threshold = max(1, int(threshold))
    pending_ratio = max(0.0, float(pending_count) / normalized_threshold)

    if pending_ratio <= 1.0:
        score = int(round(PRESSURE_STANDARD_SCORE * pending_ratio * pending_ratio))
        if idle_reached_average:
            score += IDLE_PRESSURE_BONUS
        return min(PRESSURE_STANDARD_SCORE, score)

    overflow_ratio = pending_ratio - 1.0
    full_overflow_ratio = PRESSURE_FULL_RATIO - 1.0
    overflow_factor = min(1.0, log1p(overflow_ratio) / log1p(full_overflow_ratio))
    score = PRESSURE_STANDARD_SCORE + int(
        round((PRESSURE_MAX_SCORE - PRESSURE_STANDARD_SCORE) * overflow_factor)
    )
    return min(PRESSURE_MAX_SCORE, score)


def necessity_score(
    *,
    effective_frequency: float,
    relevance: int,
    content: int,
    pending_count: int,
    threshold: int,
    presence_penalty: int = 0,
    idle_reached_average: bool = False,
) -> int:
    """复刻 ``reply_necessity.py:174-177``：最终必要性评分。

    ``raw = 相关性 + 内容 + 压力 - 存在感惩罚``，再乘 ``necessity_factor`` 取整。
    """

    pressure = pressure_score(
        pending_count=pending_count,
        threshold=threshold,
        idle_reached_average=idle_reached_average,
    )
    raw = int(relevance) + int(content) + pressure - max(0, int(presence_penalty))
    return max(0, int(round(raw * necessity_factor(effective_frequency))))


def triggers_by_necessity(
    *,
    effective_frequency: float,
    relevance: int,
    content: int,
    pending_count: int,
    presence_penalty: int = 0,
    idle_reached_average: bool = False,
) -> tuple[bool, int]:
    """评分门是否放行，返回 ``(是否触发, 评分)``（``turn_gates.py:97``）。"""

    threshold = trigger_threshold(MODE_REPLY_NECESSITY, effective_frequency)
    score = necessity_score(
        effective_frequency=effective_frequency,
        relevance=relevance,
        content=content,
        pending_count=pending_count,
        threshold=threshold,
        presence_penalty=presence_penalty,
        idle_reached_average=idle_reached_average,
    )
    return score >= NECESSITY_TRIGGER_SCORE, score


def idle_equivalent_count(
    *,
    idle_seconds: float,
    average_message_interval: float,
    threshold: int,
) -> float:
    """复刻 ``turn_gates.py:181-184``：空窗折算成等效消息数。

    封顶 ``threshold - 1``，这是宿主防止「纯靠沉默反复自我唤醒」的双保险之一。
    """

    if average_message_interval is None or average_message_interval <= 0:
        return 0.0
    normalized_threshold = max(0, int(threshold))
    return min(
        max(0.0, float(idle_seconds)) / float(average_message_interval),
        float(max(0, normalized_threshold - 1)),
    )


def triggers_by_count(
    *,
    effective_frequency: float,
    pending_count: int,
    idle_seconds: float = 0.0,
    average_message_interval: float = 0.0,
) -> bool:
    """复刻 ``turn_gates.py:118-155``：计数门是否放行。

    先看条数是否达到阈值；不足时用空窗折算补齐；``pending_count < 1`` 一律不触发
    （``turn_gates.py:170-171``——纯沉默不能触发）。
    """

    threshold = trigger_threshold(MODE_FREQUENCY, effective_frequency)
    if threshold <= 0 or int(pending_count) < 1:
        return False
    if int(pending_count) >= threshold:
        return True
    equivalent = idle_equivalent_count(
        idle_seconds=idle_seconds,
        average_message_interval=average_message_interval,
        threshold=threshold,
    )
    return int(pending_count) + equivalent >= threshold


def plain_chatter_content(
    *,
    message_count: int,
    message_length: int = 8,
) -> int:
    """纯闲聊批次的**内容分**，复刻宿主 ``reply_necessity.py:267-273``。

    宿主给分依据是**整批拼接后的文本**（``combined_clean_text = "\\n".join(texts)``，
    ``reply_necessity.py:158``），不是单条消息：

    - 总长度 ``>= 40``  → +5（长文本）
    - 总长度 ``>= 120`` → +10（较长文本）

    两个 ``if`` **互相独立、会叠加**（不是 ``elif``），所以内容分取值是 ``0 / 5 / 15``。
    这里刻意**不含** ``is_short_reaction_batch`` 的 −25 分支：本函数描述的是
    「纯闲聊」（正常说话，不是「哈哈/666」这类纯反应批），那个分支由调用方另行判定。

    早期版本把内容分写死成 ``0``，于是在批次够长时低估了可达性（把「其实攒 13 条就能
    触发」报成「不可达」）——见 ``plain_chatter_messages_needed`` 的说明。
    """

    count = max(0, int(message_count))
    length = max(0, int(message_length))
    if count <= 0:
        return 0
    # 与宿主一致：joining 会插入 count-1 个换行符
    total = count * length + max(0, count - 1)
    score = 0
    if total >= 40:
        score += CONTENT_LONG_TEXT
    if total >= 120:
        score += CONTENT_VERY_LONG_TEXT
    return score


def plain_chatter_messages_needed(
    mode: str,
    effective_frequency: float,
    *,
    max_pending: int = 500,
    message_length: int = DEFAULT_CHATTER_MESSAGE_LENGTH,
) -> int | None:
    """纯群聊闲聊（无 @、无提及、无问题/请求、无存在感惩罚）要攒几条才进 Planner。

    ``message_length`` 是「一条闲聊大概多少字」的建模假设（默认 8）：因为宿主的
    内容分来自**整批拼接长度**，批次越大内容分越高，所以可达性必须与条数**同时**求解
    ——这正是早期把 ``content`` 写死为 0 时算错的地方（会把可达的批次报成「不可达」）。

    返回 ``None`` 表示在 ``max_pending`` 条以内**不可达**：压力分上限 100 加满格内容分
    也够不到触发线，必须靠 @/提及/私聊或带问题的消息才能叫动她。

    计数门下返回触发阈值 ``T``（忽略空窗补偿——补偿只会让它更少）。
    动态门（1.3.2）下返回 ``None``：它按「每批消息的回复可能性」与频率比对，不数条数。
    """

    if is_silent(effective_frequency):
        return None
    if normalize_mode(mode) == MODE_DYNAMIC:
        return None
    if normalize_mode(mode) != MODE_REPLY_NECESSITY:
        threshold = trigger_threshold(MODE_FREQUENCY, effective_frequency)
        return threshold if threshold > 0 else None

    for pending in range(1, max(1, int(max_pending)) + 1):
        reached, _ = triggers_by_necessity(
            effective_frequency=effective_frequency,
            relevance=RELEVANCE_PLAIN_GROUP,
            content=plain_chatter_content(
                message_count=pending, message_length=message_length
            ),
            pending_count=pending,
        )
        if reached:
            return pending
    return None



def relevance_for(*, has_at: bool = False, has_mention: bool = False,
                  is_private: bool = False, focus_active: bool = False) -> int:
    """复刻 ``reply_necessity.py:139-152`` 的相关性分（顺序即优先级）。"""

    if has_at:
        return RELEVANCE_AT
    if has_mention:
        return RELEVANCE_MENTION
    if is_private or focus_active:
        return RELEVANCE_INDIRECT
    return RELEVANCE_PLAIN_GROUP


def preview(
    *,
    mode: str,
    talk_value: float,
    adjust: float,
    pending_count: int = 1,
    idle_reached_average: bool = False,
    message_length: int = DEFAULT_CHATTER_MESSAGE_LENGTH,
) -> dict:
    """给 /生活 命令用的后果预览：这个 adjust 在当前模式下意味着什么。

    只读、无副作用；所有字段都是可直接打印的原始数值，措辞交给调用方。
    ``message_length`` 见 ``plain_chatter_messages_needed``（默认按每条 8 字估算）。
    """

    normalized_mode = normalize_mode(mode)
    effective = max(0.0, float(talk_value) * float(adjust))
    threshold = trigger_threshold(normalized_mode, effective)

    result: dict = {
        "mode": normalized_mode,
        "mode_label": MODE_LABELS[normalized_mode],
        "talk_value": float(talk_value),
        "adjust": float(adjust),
        "effective_frequency": effective,
        "threshold": threshold,
        "silent": is_silent(effective),
        "chatter_message_length": max(0, int(message_length)),
    }

    if result["silent"]:
        result["plain_chatter_messages_needed"] = None
        result["verdict"] = "静默：消息被静默消费，不进 Planner，零模型开销（@ 也穿透不了）"
        return result

    if normalized_mode == MODE_DYNAMIC:
        keep_ratio = dynamic_keep_ratio(effective)
        static_gate = dynamic_static_threshold(keep_ratio)
        result["plain_chatter_messages_needed"] = None
        result["probability_gate"] = True
        result["dynamic_keep_ratio"] = keep_ratio
        result["dynamic_static_threshold"] = static_gate
        result["dynamic_window_seconds"] = DYNAMIC_WINDOW_SECONDS
        if keep_ratio >= 1.0:
            result["verdict"] = (
                "动态触发：目标回复比例 100%（生效频率 ≥ 1）——静态门槛 0，"
                "每批新消息基本都会进 Planner；倍率再高也没有上行空间"
            )
        else:
            result["verdict"] = (
                f"动态触发：生效频率 {effective:.3f} 就是 1 小时窗口内的目标回复比例"
                f"（keep_ratio {keep_ratio:.3f}）。门槛由窗口里各批消息的「回复可能性」"
                f"从高到低累积到预算定出；窗口样本不足 {DYNAMIC_MIN_WINDOW_SCORE_COUNT} 批时"
                f"用静态表，当前约 {static_gate:.3f}。群聊按此概率放行，"
                "私聊不适用动态门（有新消息直接进 Planner）"
            )
        return result

    needed = plain_chatter_messages_needed(
        normalized_mode, effective, message_length=message_length
    )
    result["plain_chatter_messages_needed"] = needed

    if normalized_mode == MODE_REPLY_NECESSITY:
        result["necessity_factor"] = necessity_factor(effective)
        _, score = triggers_by_necessity(
            effective_frequency=effective,
            relevance=RELEVANCE_PLAIN_GROUP,
            content=plain_chatter_content(
                message_count=pending_count, message_length=message_length
            ),
            pending_count=pending_count,
            idle_reached_average=idle_reached_average,
        )
        result["plain_chatter_score"] = score
        result["score_line"] = NECESSITY_TRIGGER_SCORE
        if needed is None:
            result["verdict"] = (
                f"评分门：按每条约 {max(0, int(message_length))} 字估算，"
                "纯闲聊在 500 条内推不动（压力分上限加内容分也够不到 80），"
                "只有 @/提及/私聊，或带问题/请求的消息才叫得动她"
            )
        else:
            result["verdict"] = (
                f"评分门：按每条约 {max(0, int(message_length))} 字估算，"
                f"纯闲聊约攒到 {needed} 条能进 Planner"
            )
    else:
        result["verdict"] = (
            f"计数门：攒到 {threshold} 条进 Planner；"
            f"不足时沉默约 {max(0, threshold - 1)} 个平均消息间隔也能补上"
        )
    return result
