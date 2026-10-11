# -*- coding: utf-8 -*-
"""生活状态机：作息活动的时长结算、情绪体力动力学、身体与日期（纯模块）。

设计要点：

- **纯函数 + 注入时钟与随机源**。本模块不读系统时间、不用全局 ``random``，
  所有时间从 ``now`` / ``tz_offset_minutes`` 推出来，所有随机来自调用方传入的
  ``random.Random``。因此同种子能复现同一条时间线，测试可以逐点断言。
- **补齐是分步的**。``settle`` 把离线区间切成 tick 大小逐步推进，而不是用一次
  大积分——睡眠时长、日历边界、感冒骰子都依赖「跨过某个时刻」，一次大积分会漏掉它们。
  步数按 ``max_catch_up_hours`` 封顶，防止长期离线后一次跑上千步。
- **只有 mechanics 在这里**。活动由谁选（LLM 还是规则表）不归本模块管：
  ``settle`` 先用**当前**活动结算，``plugin.py`` 再拿新状态去问模型，
  最后调 ``apply_activity`` 落定。这个顺序保证模型永远基于新鲜状态决策。
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # 包式加载（Runner 真机）
    from .life_activity import (
        ACTIVITY_LABELS,
        ALLOWED_ACTIVITIES,
        COLD_ONSET,
        COLD_RECOVERING,
        COLD_STAGE_LABELS,
        COLD_STAGES,
        COLD_WORSENING,
        DAILY,
        DAZE,
        MAX_SIDE_ACTIVITIES,
        MEAL,
        NAP,
        SICK_REST,
        SIDE_ACTIVITIES,
        SOURCE_LLM,
        SLEEP,
        SOURCE_COLD_START,
        SOURCE_ENFORCED,
        SOURCE_SKIPPED,
        ActivityDecision,
        ActivityFacts,
        EnforcePolicy,
        ScheduleConfig,
        enforce,
        in_window,
        is_asleep,
        is_awake,
        minutes_until_window_exit,
        NIGHT_BREAK_RESUME_SECONDS,
        normalize_cold_stage,
        normalize_side,
        on_sick_leave,
        request_is_pointless,
        rule_based_activity,
        schedule_facts,
        season_of,
    )
    from .life_events import LifeEvent, pick_event, sanitize_text
    from .life_factors import interpolate
    from .life_physio import settle_satiety
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_activity import (
        ACTIVITY_LABELS,
        ALLOWED_ACTIVITIES,
        COLD_ONSET,
        COLD_RECOVERING,
        COLD_STAGE_LABELS,
        COLD_STAGES,
        COLD_WORSENING,
        DAILY,
        DAZE,
        MAX_SIDE_ACTIVITIES,
        MEAL,
        NAP,
        SICK_REST,
        SIDE_ACTIVITIES,
        SOURCE_LLM,
        SLEEP,
        SOURCE_COLD_START,
        SOURCE_ENFORCED,
        SOURCE_SKIPPED,
        ActivityDecision,
        ActivityFacts,
        EnforcePolicy,
        ScheduleConfig,
        enforce,
        in_window,
        is_asleep,
        is_awake,
        minutes_until_window_exit,
        NIGHT_BREAK_RESUME_SECONDS,
        normalize_cold_stage,
        normalize_side,
        on_sick_leave,
        request_is_pointless,
        rule_based_activity,
        schedule_facts,
        season_of,
    )
    from life_events import LifeEvent, pick_event, sanitize_text
    from life_factors import interpolate  # type: ignore[no-redef]
    from life_physio import settle_satiety  # type: ignore[no-redef]

STATE_VERSION = 1
DEFAULT_TZ_OFFSET_MINUTES = 480  # UTC+8
BASELINE_EMOTION = 5.0
EMOTION_MAX = 10.0
ENERGY_MAX = 10.0
#: 连续清醒分钟数的上限（v1.16.0 M1 的字段防御）。365 天足够长，更大的值只可能是
#: 坏状态文件——一个 ``1e18`` 会让疲劳曲线取到端点值，她从此再也睡不着。
MAX_CONTINUOUS_AWAKE_MINUTES = 366 * 24 * 60
#: 高压崩溃事件的键（v1.16.2 M3a）——与病程各阶段键同层级的确定性事件标识。
STRESS_BREAKDOWN = "stress_breakdown"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------- 日期规则


@dataclass(frozen=True)
class FestivalRule:
    """一条可配置的日期规则（生日也是一个 FestivalRule）。

    ``factor`` 参与发言频率倍率；``emotion`` / ``energy`` 在当天触发一次；
    ``material`` 是当天产出的「想跟你说的素材」。
    """

    name: str
    month: int
    day: int
    factor: float = 1.0
    emotion: float = 0.0
    energy: float = 0.0
    weight: float = 0.5
    material: str = ""

    @property
    def mmdd(self) -> str:
        return f"{self.month:02d}-{self.day:02d}"

    def matches(self, local_dt: datetime) -> bool:
        return local_dt.month == self.month and local_dt.day == self.day

    def key(self, local_dt: datetime) -> str:
        """年内唯一键，用来保证同一天只触发一次。"""

        return f"{local_dt.year}:{self.mmdd}:{self.name}"


_DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _parse_mmdd(text: object) -> tuple[int, int] | None:
    raw = sanitize_text(text, max_chars=16).replace("/", "-").replace(".", "-")
    parts = [chunk for chunk in raw.split("-") if chunk]
    if len(parts) != 2:
        return None
    try:
        month, day = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    if day > _DAYS_IN_MONTH[month - 1]:
        # 02-30 这类「看着合法但永远不会命中」的日期必须在配置报错，
        # 否则用户以为登记了生日，实际全链路静默无事发生。
        return None
    return month, day


def parse_mmdd(text: object) -> tuple[int, int] | None:
    """公开入口：``MM-DD`` → ``(month, day)``；非法或该月没有这一天返回 ``None``。

    plugin 层用它来在配置阶段告警（而不是等到某天静默不触发）。
    """

    return _parse_mmdd(text)


def parse_festival_lines(lines: object) -> tuple[list[FestivalRule], list[str]]:
    """解析节日行 DSL：``"MM-DD|名称|emotion=1.2|energy=0|factor=1.3|weight=0.8|material=..."``。

    只有 ``MM-DD`` 与名称是必需的；其余字段可省。坏行告警跳过。
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

    rules: list[FestivalRule] = []
    warnings: list[str] = []
    for line in candidates:
        raw = str(line or "").strip()
        if not raw:
            continue
        fields = [chunk.strip() for chunk in raw.split("|")]
        date_part = _parse_mmdd(fields[0] if fields else "")
        if date_part is None:
            warnings.append(f"节日行 {raw!r} 的日期不是 MM-DD，整行跳过")
            continue
        name = sanitize_text(fields[1] if len(fields) > 1 else "", max_chars=24)
        if not name:
            warnings.append(f"节日行 {raw!r} 缺少名称，整行跳过")
            continue

        payload: dict[str, str] = {}
        for chunk in fields[2:]:
            if "=" in chunk:
                key, value = chunk.split("=", 1)
                payload[key.strip().lower()] = value.strip()

        def _num(key: str, default: float) -> float:
            if key not in payload:
                return default
            try:
                return float(payload[key])
            except ValueError:
                warnings.append(f"节日 {name}: {key}={payload[key]!r} 不是数字，按 {default} 处理")
                return default

        month, day = date_part
        rules.append(
            FestivalRule(
                name=name,
                month=month,
                day=day,
                factor=_clamp(_num("factor", 1.0), 0.2, 5.0),
                emotion=_clamp(_num("emotion", 0.0), -5.0, 5.0),
                energy=_clamp(_num("energy", 0.0), -5.0, 5.0),
                weight=_clamp(_num("weight", 0.5), 0.0, 1.0),
                material=sanitize_text(payload.get("material", ""), max_chars=80),
            )
        )
    return rules, warnings


# ---------------------------------------------------------------- 配置


def _default_energy_delta() -> dict[str, float]:
    """每小时体力变化：负=消耗，正=恢复（标定参考 Mai_life 的 load 表）。"""

    return {
        SLEEP: 1.20,
        # 小睡（v1.15.0，PR-R1）：眯一会儿恢复得比整觉弱——这是它与 sleep 的
        # 实质差别之一（另两条是时限与不做梦）。
        NAP: 0.60,
        "sick_rest": 0.60,
        "before_sleep": -0.30,
        # 工作表（v1.3.0）：通勤与在岗都在耗神，加班最累，午休回一点血
        "commute": -0.35,
        "work": -0.55,
        "meeting": -0.65,
        "overtime": -0.85,
        "lunch": 0.15,
        "off_work": -0.45,
        "night_study": -1.20,
        "game": -0.70,
        "anime": -0.50,
        "music": -0.25,
        "daze": -0.10,
        DAILY: -0.50,
        # v1.9.1（physio）：吃饭是小回血，洗澡是轻度消耗
        "meal": 0.20,
        "bath": -0.10,
        # v1.11.1（interrupt）：和人说话是小回血——比睡觉弱得多，但确实是正的
        "chatting": 0.05,
    }


@dataclass(frozen=True)
class SimConfig:
    """状态机的全部可调参数（由配置映射而来）。"""

    tick_seconds: int = 600
    max_catch_up_hours: float = 72.0
    #: 超过「这么久没推进」就判定为**停机间隙**：这段时间不记账（不计清醒/睡眠、
    #: 不扣体力、不抽事件），只把生活日推进到当前。0 = 关闭判定（退回逐 tick 补算）。
    #: 为什么需要它见 ``_skip_offline_gap``：逐 tick 补算会把停机时间整段算成
    #: 当前活动的时长，真机上让 ``awake_minutes_today`` 从 216 跳到 859。
    offline_gap_minutes: int = 30
    tz_offset_minutes: int = DEFAULT_TZ_OFFSET_MINUTES
    day_boundary_hour: int = 12
    sleep_window: tuple[int, int] = (3 * 60, 11 * 60)
    sleep_window_text: str = "03:00-11:00"
    sleep_energy_threshold: float = 3.0
    max_sleep_hours: float = 12.0
    min_awake_hours_per_day: float = 8.0
    min_dwell_minutes: int = 60
    min_sleep_minutes: int = 180
    #: 睡眠中体力恢复到上限就强制唤醒（v1.6.0：**不再要求睡满最短时长**——
    #: 体力满了继续躺只是空转，真机实测她带着 10.0/10 又睡了 73 分钟）。
    #: 默认开；关掉则回到「模型提议醒 / 睡满每日上限」两条路。
    energy_full_wake: bool = True
    #: 「体力满唤醒」的最短睡眠目标（小时，v1.15.0 PR-S1）。0 = v1.6.0 旧行为。
    #: 默认 6.5：断开「熬夜帽压低上限 → 睡得更短 → 债还不清」的正反馈。
    energy_full_wake_min_hours: float = 6.5
    #: 休息日在最短睡眠目标上额外顺延的分钟数（v1.15.0 PR-R4）；0 = 不区分。
    rest_day_sleep_extension_minutes: float = 60.0
    #: 体力 ≤ 它就无视模型提议强制入睡（v1.15.0 PR-S3）；0 = 关闭。
    sleep_hard_floor: float = 0.5
    #: 习惯表 / 生理窗能不能把她从睡眠里叫起来（v1.15.0 PR-S2）。
    #: 默认 False = 睡眠优先。
    routine_can_wake: bool = False
    physio_can_wake: bool = False
    #: 长睡眠醒来后先赖床这么多分钟（v1.15.0 PR-R3）；0 = 关闭。
    wake_daze_minutes: int = 15
    #: 小睡（v1.15.0 PR-R1）。
    nap_enabled: bool = True
    nap_min_minutes: int = 20
    nap_max_minutes: int = 90
    nap_energy_threshold: float = 4.0
    #: 入睡困难（v1.15.0 PR-R2）：压力 ≥ 阈值时按概率把入睡收口成「睡不着」。
    insomnia_enabled: bool = True
    insomnia_stress_threshold: float = 7.0
    insomnia_probability: float = 0.5
    #: 夜间易醒（v1.15.0 PR-R2）：长睡眠中段低概率醒一下再睡回去；默认关。
    night_waking_enabled: bool = False
    night_waking_probability: float = 0.02

    # ---- 清醒疲劳代谢（v1.16.0 M1，``[emotion_energy]``）----
    #: 连续清醒小时 → **额外**每小时体力消耗（负值，分段线性）。默认空 = 关闭 =
    #: 与加这一层之前逐位一致；插件侧默认给一条保守曲线（12 小时起才起作用）。
    #: 它回答的是「最闲的一天她为什么也会困」：原来体力只由活动表驱动，
    #: 清闲路径 ``daze``（-0.10/h）连熬 16 小时只掉 1.6 分，永远到不了入睡阈值。
    fatigue_ramp_curve: tuple[tuple[float, float], ...] = ()

    inertia_minutes: int = 40
    recover_per_tick: float = 0.2
    sleep_recover_multiplier: float = 2.0
    #: 比例回归（v1.16.1 M5a，``[emotion_energy] recover_ratio_per_tick``）：每个 tick 消除
    #: 「当前情绪与基线的差距」的固定比例。``0`` = 关闭 = 沿用 ``recover_per_tick`` 的
    #: 线性步长（与 v1.15.0 逐位一致）。真人的情绪是「爆发快消、余味长」：极端情绪初期
    #: 回落快、接近基线变慢，线性步长做不到这一点。
    recover_ratio_per_tick: float = 0.0
    #: 比例回归时的**每 tick 最小步长**（0 = 不设下限）。没有它，「距基线 0.01」时
    #: 比例步长会小到浮点精度以下、情绪永远擦不干净；有了它尾部仍会收敛。
    recover_min_step: float = 0.05
    #: 惯性期按冲击大小缩放（v1.16.1 M5b）：``inertia × |delta|``，钳进
    #: ``[inertia_scale_min_minutes, max(inertia_scale_max_minutes, inertia_minutes)]``。
    #: ``False`` = 关闭 = 任何事件都冻结 ``inertia_minutes``（v1.15.0 旧行为）。
    inertia_scale_enabled: bool = False
    inertia_scale_min_minutes: float = 5.0
    inertia_scale_max_minutes: float = 90.0
    afterglow_span_hours: float = 24.0
    afterglow_cap: float = 0.6
    afterglow_gain: float = 0.15
    #: 余波按年龄线性衰减（v1.16.1 M4b）：``w = 1 − age/span``，出窗自然归零而不是
    #: 跳变。``False`` = 旧等权（窗口内一律 1.0）。等权 → 衰减后有效总量约减半，
    #: 所以打开它时 ``afterglow_gain`` 要翻倍（插件侧自动选 0.30）才能维持同等稳态。
    afterglow_decay: bool = False
    #: 日内节律曲线（v1.16.1 M4a）：**本地时刻分钟** → 基线偏移（分段线性）。
    #: 默认空 = 关闭 = 基线不会随时刻浮动（v1.15.0 行为）。
    baseline_diurnal_curve: tuple[tuple[float, float], ...] = ()

    # ---- 体力 → 情绪 / 消耗的耦合（v1.16.2 M2，``[emotion_energy]``）----
    #: 疲劳压低情绪基线（v1.16.2 M2a）：``energy < threshold`` 时基线下移
    #: ``penalty × (threshold − energy)``（``energy = 0`` 时正好是 ``−penalty × threshold``）。
    #: ``0`` = 关闭 = 旧行为。⚠ **这是「身体状态间接影响倍率」纪律的唯一开口**
    #: （方案决议 1）：体力因子代表「没力气说话」，情绪基线下移代表「累到不想说话」，
    #: 是两种不同机制的状态成因，不是同一原因的第二次乘法压制。后续任何新耦合需求
    #: 都要回到设计层重新论证，**不得援引本条放行**。
    emotion_fatigue_penalty: float = 0.0
    emotion_fatigue_threshold: float = 3.0
    #: 低体力消耗放大（v1.16.2 M2b）：``energy < threshold`` 时负 delta × multiplier。
    #: **只放大消耗项、不碰恢复项**——越累越容易更累的软恶性循环，把她推向休息。
    #: ``1.0`` = 关闭（旧行为）。
    low_energy_drain_multiplier: float = 1.0
    low_energy_threshold: float = 3.0

    # ---- 内心维度 → 情绪（v1.16.2 M3，确定性产出）----
    #: 高压崩溃（v1.16.2 M3a）：``stress`` 持续偏高到点触发一次「绷不住」。
    #: 硬编码事件（与病程 ``_COLD_EVENTS`` 同一套确定性管线），只暴露开关与阈值。
    stress_breakdown_enabled: bool = False
    stress_breakdown_threshold: float = 7.0
    #: 需要**持续**高多少小时才算「绷不住」（瞬时高压不算）
    stress_breakdown_hours: float = 2.0
    #: 压力回落到它以下就清掉高压起点（下一次高压重新计时）
    stress_breakdown_reset: float = 5.0

    # ---- 情绪冲击的边际效用（v1.16.3 M6）----
    #: 同一个增量在不同情绪水平上的**体感**不同（方案 P7a）：低谷时雪中送炭、
    #: 高涨时快乐麻木、从高处跌落更疼。``False`` = 关闭（增量原样进出）。
    emotion_impact_scaling: bool = False
    #: 正向增量曲线（情绪 → 系数）：≤2 放大到 1.3、5 → 1.0、≥8 压到 0.7（分段线性）
    impact_positive_curve: tuple[tuple[float, float], ...] = ((2.0, 1.3), (5.0, 1.0), (8.0, 0.7))
    #: 负向增量曲线：≤2 麻木（0.7）、5 → 1.0、≥8 落差（1.2）
    impact_negative_curve: tuple[tuple[float, float], ...] = ((2.0, 0.7), (5.0, 1.0), (8.0, 1.2))

    baseline_emotion: float = BASELINE_EMOTION
    emotion_max: float = EMOTION_MAX
    energy_max: float = ENERGY_MAX

    sleep_debt_threshold_minutes: int = 300
    sleep_debt_cap_nights: int = 3
    sleep_deprived_energy_cap: float = 8.5
    #: 「一晚好觉」清几晚熬夜债（v1.15.0 PR-S1）。默认 1 = **滞回**：连熬三晚要
    #: 三个好觉才还清（0 = 旧行为：一晚直接归零）。
    sleep_debt_recovery_step: int = 1
    cold_check_hour: int = 2
    cold_min_days: int = 1
    cold_max_days: int = 3
    cold_base_risk: float = 0.05
    cold_sleep_debt_risk: float = 0.08

    # ---- 病程系统（v1.14.0）----
    #: 痊愈后几天内不再掷中招骰子（修「病好第二天就能无缝再病」）。
    cold_immunity_days: int = 5
    #: 阶段 → 健康因子（初起/加重/好转）。空 = 回退 ``[health] health_factors.cold``。
    cold_stage_factors: dict[str, float] = field(
        default_factory=lambda: {"onset": 0.7, "worsening": 0.15, "recovering": 0.5}
    )
    #: 每日**计入阶段流转**的关心次数上限（多出来的关心仍然有情绪收益，但不加速康复）。
    cold_care_daily_cap: int = 2
    #: 病后余韵时长：这段时间不算生病，只是「刚好利索，体力还没回来」。
    cold_convalescent_hours: int = 24
    #: 病假口（v1.14.0 §3.4）：加重/好转期算「请了病假」，班表的在岗约束对她挂起。
    cold_sick_leave: bool = True
    #: 季节风险系数：月份 → 倍率（如 ``{"1": 1.5, "7": 0.7}``）。**空 = 不启用**（默认），
    #: 这样「生病不是均匀白噪声」是可以选的氛围，而不是升级后偷偷改掉的数值。
    cold_season_factors: dict[str, float] = field(default_factory=dict)

    fire_probability: float = 0.4
    material_ttl_hours: float = 6.0
    #: 素材「最佳保鲜相位」（G2）：TTL 的前 `material_best_ratio` 段是全额权重，
    #: 之后线性衰减到 `material_decay_floor`、到过期触底——衰减而非清零，让放旧的
    #: 念头自然排到候选队尾。ratio=1 或 floor=1 都退回「全额到过期」的旧行为。
    material_best_ratio: float = 0.5
    material_decay_floor: float = 0.25
    #: 经历留存条数。与 ``[activity.llm] recent_events_keep`` 同源（plugin.py 接线），
    #: 300 条 ≈ 15.9 天的事件量（约 18.9 条/日），足够喂饱「远（14 天内）」层；
    #: v1.4.0 及以前固定 40 条 ≈ 2.1 天，远层永远拿不到内容。
    recent_events_keep: int = 300
    materials_keep: int = 20

    birthday: str = ""
    birthday_factor: float = 1.3
    birthday_emotion: float = 1.5
    birthday_material: str = "今天是我生日"
    festivals: tuple[FestivalRule, ...] = ()
    #: 按**名称**覆盖节日倍率（``[date].date_factors``，如 ``{"生日": 1.4}``）。
    #: v1.1.0 里这个配置项只声明没被读过，等于设了完全没用（死配置）。
    date_factor_overrides: dict[str, float] = field(default_factory=dict)

    energy_delta_per_hour: dict[str, float] = field(default_factory=_default_energy_delta)

    #: 作息班表（``[activity.schedule]``）。默认关闭 = 与加这一层之前完全一致。
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)

    # ---- 生理锚点（v1.9.1，physio）----
    #: ``[physio] enabled``。关掉 = 纯模块不参与结算，行为与 v1.8.x 完全一致。
    physio_enabled: bool = True
    physio_meals: tuple = ()
    #: 清醒时每小时饱腹下降。
    satiety_decay_per_hour: float = 0.8
    #: 一餐的最短停留（分钟）。命中三餐窗后她至少「吃饭」这么久。
    meal_duration_minutes: int = 40
    #: 洗澡窗（可选；空 = 关闭洗澡锚点）。
    bath_window: str = "22:00-23:30"


def _activity_label(activity: str) -> str:
    return ACTIVITY_LABELS.get(str(activity), str(activity))


def activity_effect_lines(
    config: SimConfig,
    *,
    activity_factors: Mapping[str, float] | None = None,
    at_wake: bool = False,
    mention_economy: bool = False,
) -> tuple[str, ...]:
    """「选这个活动会影响什么」的事实行，供活动决策提示词使用。

    ⚠ **全部数字都从 ``config`` / ``activity_factors`` 现算**，一个都不写死。理由：
    提示词里印一个和实现不同的数值，比不印更糟 —— 模型会按错的因果做选择，而
    ``enforce`` 只按真值收口，两边永远对不上；用户改了配置（比如
    ``[events] fire_probability`` 或 ``[health] sleep_debt_threshold_minutes``）
    提示词也必须跟着变。回归用例见 ``tests/test_sim.py::test_effect_lines_track_config``。

    ``at_wake`` / ``mention_economy`` 是**要如实说明**的两件事：
    ``at_wake`` 关掉时「睡觉连 `@` 都不回」才成立；经济维度与活动无关，
    不说清楚模型可能会「为了省钱挑便宜的活动」——那是我在 prompt 里凭空创造出的因果。
    """

    lines: list[str] = []

    # ---- 体力：每小时增减（真值就是 settle 里用的那张表）----
    deltas: dict[str, float] = {}
    for key, value in dict(config.energy_delta_per_hour).items():
        number = _as_float(value)
        if number is not None and math.isfinite(number):
            deltas[str(key)] = float(number)
    gains = sorted(((k, v) for k, v in deltas.items() if v > 0), key=lambda kv: -kv[1])
    costs = sorted(((k, v) for k, v in deltas.items() if v < 0), key=lambda kv: kv[1])
    if gains:
        lines.append(
            "体力每小时（恢复）："
            + "、".join(f"{_activity_label(k)} +{v:.2f}" for k, v in gains)
        )
    if costs:
        lines.append(
            "体力每小时（消耗）："
            + "、".join(f"{_activity_label(k)} {v:.2f}" for k, v in costs)
        )

    # ---- 清醒疲劳（v1.16.0 M1）----
    # 只在真的启用时提：这是「困意由身体驱动」的那一条，模型不知道它会以为
    # 「一直躺着就不困」。数值同样从曲线现算，不写死。
    ramp_curve = tuple(getattr(config, "fatigue_ramp_curve", ()) or ())
    if ramp_curve:
        points = "、".join(f"清醒 {x:g} 小时 {y:+g}/h" for x, y in ramp_curve)
        lines.append(
            f"清醒疲劳：连续清醒越久，**额外**消耗越大（{points}，线性插值）——"
            "睡一觉（含小睡）就清零；这是她熬夜之后自己会困的原因之一。"
        )

    # ---- 情绪 ----
    tick_minutes = max(1.0, float(config.tick_seconds) / 60.0)
    diurnal = tuple(getattr(config, "baseline_diurnal_curve", ()) or ())
    diurnal_note = ""
    if diurnal:
        # 幅度从曲线现算（不写死）：她自己都「说不清为什么早上低落」，
        # 但模型得知道基线会随时刻浮动，否则会把它当成随机波动。
        amplitude = max(abs(float(y)) for _, y in diurnal)
        diurnal_note = f"；另外基线本身有 ±{amplitude:.1f} 的日内节律（早晨低、傍晚高）"
    ratio = max(0.0, float(getattr(config, "recover_ratio_per_tick", 0.0) or 0.0))
    if ratio > 0.0:
        recovery = f"每 {tick_minutes:.0f} 分钟消除与基线差距的 {min(1.0, ratio):.0%}"
    else:
        recovery = f"约 {config.recover_per_tick:.2f}/{tick_minutes:.0f} 分钟"
    lines.append(
        f"情绪：默认会往基线（{config.baseline_emotion:.1f} ± 最近 24 小时事件余波 "
        f"{config.afterglow_cap:.1f}）回归，{recovery}，"
        f"睡觉时快 {config.sleep_recover_multiplier:.1f} 倍{diurnal_note}；"
        "真正拉高或拉低情绪的是发生的事（清醒时每个推进间隔有 "
        f"{_clamp(config.fire_probability, 0.0, 1.0):.0%} 概率发生一件，"
        "不同活动会抽到不同的事）。"
    )

    # ---- 今日已睡 / 清醒 + 睡眠怎么结束 ----
    lines.append(
        f"今日已睡/清醒：睡觉与小睡都计入「已睡」，其余活动都计入「清醒」；"
        f"已睡累计（或这一觉连续）到 {config.max_sleep_hours:g} 小时会被强制叫醒；"
        f"清醒不足 {config.min_awake_hours_per_day:g} 小时则不许入睡"
        f"（生病或体力低于 {config.sleep_energy_threshold:g} 时不受此限）。"
    )
    if config.energy_full_wake:
        min_hours = max(0.0, float(getattr(config, "energy_full_wake_min_hours", 0.0) or 0.0))
        if min_hours > 0:
            extra = ""
            rest_extra = max(
                0.0, float(getattr(config, "rest_day_sleep_extension_minutes", 0.0) or 0.0)
            )
            if rest_extra > 0:
                extra = f"（休息日多睡 {rest_extra / 60.0:g} 小时）"
            lines.append(
                f"睡觉怎么结束：体力回到上限**且**睡够 {min_hours:g} 小时才醒{extra}"
                f"（上限随熬夜天数变，见下条）；或睡满 {config.max_sleep_hours:g} 小时被强制唤醒。"
            )
        else:
            lines.append(
                "睡觉怎么结束：体力回到上限就立刻醒（上限随熬夜天数变，见下条），"
                f"不会睡到自然醒；或睡满 {config.max_sleep_hours:g} 小时被强制唤醒。"
            )

    # ---- 小睡（v1.15.0 PR-R1）----
    if bool(getattr(config, "nap_enabled", False)):
        nap_delta = _as_float(dict(config.energy_delta_per_hour).get(NAP), 0.0) or 0.0
        lines.append(
            f"小睡：白天（睡眠时段之外）真的困了才眯一会儿，最多 "
            f"{max(1, int(config.nap_max_minutes))} 分钟；体力每小时 "
            f"{nap_delta:+.2f}（比睡觉弱）、不做梦，睡着时同样完全静默。"
        )

    # ---- 熬夜与体力上限 ----
    recovery = max(0, int(getattr(config, "sleep_debt_recovery_step", 0) or 0))
    recovery_text = (
        f"睡够的一晚会清掉 {recovery} 晚熬夜债（连熬要多睡几晚才还清）"
        if recovery > 0
        else "睡够的一晚会把熬夜天数直接清零"
    )
    lines.append(
        f"熬夜与上限：每次睡醒按最近 24 小时实际睡够多少判，低于 "
        f"{config.sleep_debt_threshold_minutes / 60.0:g} 小时算一夜没睡够；"
        f"{recovery_text}；连 {config.sleep_debt_cap_nights} 夜会把体力上限从 "
        f"{config.energy_max:g} 压到 {config.sleep_deprived_energy_cap:g}"
        f"（上限变低也更容易感冒）。"
    )

    # ---- 感冒（v1.14.0：分阶段的病程，不再是「一段连续的只能养病」）----
    lines.append(
        f"感冒：每天 {config.cold_check_hour}:00 之后判一次，基础 "
        f"{_clamp(config.cold_base_risk, 0.0, 1.0):.0%}，每有一晚没睡够 +"
        f"{_clamp(config.cold_sleep_debt_risk, 0.0, 1.0):.0%}，"
        f"体力低于 {config.sleep_energy_threshold:g} 时再 +"
        f"{_clamp(config.cold_sleep_debt_risk, 0.0, 1.0):.0%}；中招大约 "
        f"{config.cold_min_days}~{max(config.cold_min_days, config.cold_max_days)} 天，"
        "病程分三段：初起（嗓子不舒服但还能做事）、加重（只能躺着养病）、"
        "好转（能起来做点轻的）；每天按她睡够没有、有没有在休息"
        + (
            "、有没有人关心"
            if int(getattr(config, "cold_care_daily_cap", 0) or 0) > 0
            else ""
        )
        + "决定是好转还是加重。"
    )
    if int(getattr(config, "cold_immunity_days", 0) or 0) > 0:
        lines.append(
            f"病好之后 {int(config.cold_immunity_days)} 天内不会再中招；"
            "她说起病情的口吻随阶段变化，不会报出还有几小时痊愈。"
        )
    # 病假只在**真启用了班表**时才说：没有班表的角色本来就不上班，提「上班时段」
    # 是凭空造出来的因果（`tests/test_schedule.py::test_activity_prompt_has_no_schedule_when_disabled`
    # 钉着这条）。
    if bool(getattr(config, "cold_sick_leave", False)) and bool(
        getattr(getattr(config, "schedule", None), "enabled", False)
    ):
        lines.append(
            "病假：加重与好转期她会请病假——上班时段也不用去，"
            "不要给她安排上班、通勤、开会、加班这类活动。"
        )

    # ---- 班表（仅启用时）----
    if config.schedule.enabled:
        lines.append(
            "作息班表：上班/通勤/午休这些相位里的活动受限，违反会被强制改掉"
            "（上面的班表行已经写明现在适合哪些）。"
        )

    # ---- 钱（经济提示存在时才说，避免无谓噪音）----
    if mention_economy:
        lines.append(
            "钱：活动不改变手头宽裕程度——经济维度只读预算插件的数据，"
            "手头紧只会让她的场景与语气省着花。不要为了省钱挑活动。"
        )

    # ---- 发言频率（只说结论，不给数值清单）----
    if activity_factors:
        values: list[float] = []
        for key, value in activity_factors.items():
            number = _as_float(value)
            if str(key) == SLEEP or number is None or not math.isfinite(number):
                continue
            if number > 0:
                values.append(float(number))
        ratio_text = ""
        if values and min(values) > 0:
            ratio = max(values) / min(values)
            ratio_text = f"其余活动之间最多相差约 {ratio:.1f} 倍。"
        if float(_as_float(activity_factors.get(SLEEP), 0.0) or 0.0) <= 0:
            silent_part = (
                "睡觉等于完全静默——消息照收、进历史，但不进她的思考"
                + ("（被 @ 会临时醒来一次）" if at_wake else "（连 @ 也不回）")
            )
        else:
            silent_part = "睡觉时说话极少"
        lines.append(
            f"说话多少：{silent_part}；{ratio_text}"
            "别为了让她多说话或少说话而挑活动。"
        )

    return tuple(lines)


class SimConfigError(ValueError):
    """配置非法（例如 tick 太小）。"""


def build_enforce_policy(config: SimConfig) -> EnforcePolicy:
    """把 SimConfig 里与 ``enforce`` 有关的部分抽出来。"""

    return EnforcePolicy(
        sleep_window=config.sleep_window,
        sleep_energy_threshold=config.sleep_energy_threshold,
        max_sleep_hours=config.max_sleep_hours,
        min_awake_hours_per_day=config.min_awake_hours_per_day,
        min_dwell_minutes=config.min_dwell_minutes,
        min_sleep_minutes=config.min_sleep_minutes,
        energy_full_wake=config.energy_full_wake,
        # v1.15.0（睡眠改进方案）：时长目标 / 硬底线 / 赖床 / 小睡 / 失眠 + 两个
        # 「睡眠优先」开关。默认值即方案推荐值，全部可配、可关。
        energy_full_wake_min_hours=config.energy_full_wake_min_hours,
        rest_day_sleep_extension_minutes=config.rest_day_sleep_extension_minutes,
        sleep_hard_floor=config.sleep_hard_floor,
        routine_can_wake=config.routine_can_wake,
        physio_can_wake=config.physio_can_wake,
        wake_daze_minutes=config.wake_daze_minutes,
        nap_enabled=config.nap_enabled,
        nap_min_minutes=config.nap_min_minutes,
        nap_max_minutes=config.nap_max_minutes,
        nap_energy_threshold=config.nap_energy_threshold,
        insomnia_enabled=config.insomnia_enabled,
        schedule=config.schedule,
        # v1.14.0（病程 §3.4）：加重/好转期算「请了病假」，班表的在岗约束对她挂起。
        cold_sick_leave=bool(config.cold_sick_leave),
        # v1.11.1（interrupt）：SimConfig 里没有这一项——它属于**插件配置**
        # （[interrupt].hold_over_sleep），由 plugin 侧在收口后覆盖。默认 True
        # 表示「窗口内保住聊天态」是这一版的既定行为。
        interrupt_hold=True,
        # v1.17.0（PR-PHY-2）：`[physio] meal_duration_minutes`（默认 40）以前
        # 定义、装配齐全却**零消费**——用户调它没有任何效果。现在把它接成
        # 「meal 这个活动的最短停留期」：吃完饭至少待满这么久才能切走。
        # 配成与 min_dwell_minutes 相同的值即逐位回到旧行为。
        dwell_overrides={MEAL: max(0, int(config.meal_duration_minutes))},
    )


def all_festival_rules(config: SimConfig) -> tuple[FestivalRule, ...]:
    """生日 + 自定义节日的合并表，并应用 ``date_factors`` 的名称覆盖。"""

    rules: list[FestivalRule] = []
    overrides = dict(config.date_factor_overrides or {})
    birthday = _parse_mmdd(config.birthday)
    if birthday is not None:
        rules.append(
            FestivalRule(
                name="生日",
                month=birthday[0],
                day=birthday[1],
                factor=overrides.get("生日", config.birthday_factor),
                emotion=config.birthday_emotion,
                weight=0.8,
                material=config.birthday_material,
            )
        )
    for rule in config.festivals:
        if rule.name in overrides:
            rule = replace(rule, factor=float(overrides[rule.name]))
        rules.append(rule)
    return tuple(rules)


def active_festivals(config: SimConfig, local_dt: datetime) -> tuple[FestivalRule, ...]:
    """今天命中的日期规则。"""

    return tuple(rule for rule in all_festival_rules(config) if rule.matches(local_dt))


# ---------------------------------------------------------------- 状态


def _as_float(value: object, default: float | None = None) -> float | None:
    """宽松取数：非数值/非有限一律返回 ``default``，**绝不抛**。

    状态文件是可以被外部写坏的（手工编辑、跨版本、外部工具），而 ``float("abc")`` 会抛
    ``ValueError``、``float(None)`` 会抛 ``TypeError``。v1.1.0 里 ``_sweep`` 的列表推导
    在抛错时**不会执行赋值**，于是坏条目永远留在状态里、每 tick 都炸一次，
    倍率也就再也不下发（只能手删状态文件）。所有读持久化状态的地方都用这个助手。
    """

    if isinstance(value, bool) or value is None:
        # bool 是 int 的子类：状态里的 true/false 不该被当成 1.0/0.0 的数值字段
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


def _sanitize_records(items: object, *, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """条目级净化列表字段（``materials`` / ``recent_events``）。

    ``fields`` 里的键必须是有限数，否则整条丢弃；非 dict 条目直接丢。
    只保留 dict 且补齐字段类型，保证下游 ``float(...)`` 不会再抛。
    """

    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        record = dict(item)
        bad = False
        for field in fields:
            if field not in record:
                continue
            number = _as_float(record.get(field))
            if number is None:
                bad = True
                break
            record[field] = number
        if not bad:
            cleaned.append(record)
    return cleaned


def _sanitize_adjust_map(raw: object) -> dict[str, float]:
    """把 ``{会话: 倍率}`` 洗成有限非负浮点；坏项丢弃而不是让恢复整体失败。"""

    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, float] = {}
    for key, value in raw.items():
        session_id = str(key or "").strip()
        if not session_id:
            continue
        # 只收真正的数字：字符串 "0.5" 说明状态文件被改坏了，不猜（与 plugin 层
        # ``_as_number`` 同一条原则）。bool 是 int 的子类，必须排除。
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if not math.isfinite(number):
            continue
        cleaned[session_id] = max(0.0, number)
    return cleaned


def _sanitize_float_map(raw: object) -> dict[str, float]:
    """把 ``{键: 数值}`` 洗成有限浮点；坏项丢弃（``social_seen`` / ``social_daily``）。

    与 ``_sanitize_adjust_map`` 的区别：这里**允许负值**（情绪额度表迟早要能记负），
    但同样不认字符串数字 —— 状态文件被改坏时不猜。
    """

    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, float] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if not name or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if math.isfinite(number):
            cleaned[name] = number
    return cleaned


def _sanitize_int_map(raw: object) -> dict[str, int]:
    """把 ``{键: 计数}`` 洗成真正的 int 映射；坏项丢弃（``skip_ledger``）。

    v1.8.2 修：``from_dict`` 以前对这张表走通用 ``dict(value)``，坏值（``"abc"`` /
    ``null``）会一路活到消费点 ``bump_skip_ledger`` 的 ``int(...)`` 才炸——异常发生在
    ``_sim_tick`` 内部，被兜住但**整 tick 中止**，每个 tick 都重复一次。
    """

    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, int] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if not name or isinstance(value, bool) or not isinstance(value, int):
            continue
        cleaned[name] = value
    return cleaned


def _sanitize_session_map(raw: object) -> dict[str, dict[str, Any]]:
    """把 ``{会话: 会话记录}`` 洗成 ``dict`` 记录并校准时间/计数字段。

    值不是 dict 的条目直接丢（消费点虽有 isinstance 守卫，坏条目不该进状态）；
    记录里的时间戳/计数走数值净化：一个 ``NaN`` 的 ``last_user_message_at`` 会让
    「对方刚说过话」这道闸的比较恒为假 ⇒ 静默失效。
    """

    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        session_id = str(key or "").strip()
        if not session_id or not isinstance(value, dict):
            continue
        record = dict(value)
        for stamp in ("last_user_message_at", "last_proactive_at"):
            if stamp in record:
                number = _as_float(record.get(stamp))
                record[stamp] = number if number is not None else 0.0
        for count in ("count", "unanswered_streak"):
            if count in record:
                number = _as_float(record.get(count))
                record[count] = int(number) if number is not None else 0
        cleaned[session_id] = record
    return cleaned


@dataclass
class LifeState:
    """全部持久化状态。字段都有默认值，缺字段/损坏文件也能起来。"""

    state_version: int = STATE_VERSION
    last_tick_at: float = 0.0
    day_key: str = ""

    activity: str = DAILY
    activity_source: str = SOURCE_COLD_START
    activity_note: str = ""
    activity_since: float = 0.0
    scene: str = ""
    #: 睡眠期间被 ``@`` 唤醒到的时刻（epoch 秒；``0`` = 没有唤醒窗口）。
    #: 它是一条**临时清醒窗口**：``now < at_wake_until`` 时倍率按清醒算，而不是睡眠的 0。
    #: 原因是宿主的判定顺序——先判「频率是否静默」再判「`@` 强制触发」
    #: （``src/maisaka/turn_trigger/scheduler.py:57`` / ``:65``），倍率为精确 0 时这条 `@`
    #: 会被静默轮吃掉（``reasoning_engine.py:1197`` 还会清掉强制轮标记），所以她必须先在
    #: 宿主眼里「不是静默」才有机会回话。窗口一过，只要 ``activity`` 仍是 ``sleep``
    #: 就自动回到静默（``plugin._effective_activity``），不需要额外清理。
    at_wake_until: float = 0.0

    emotion: float = BASELINE_EMOTION
    energy: float = 6.0
    energy_cap: float = ENERGY_MAX
    afterglow: float = 0.0
    inertia_until: float = 0.0

    sleep_started_at: float = 0.0
    sleep_minutes_today: int = 0
    awake_minutes_today: int = 0
    #: 连续清醒分钟数（v1.16.0 M1）：醒着就累加、**过程中不清零**，一觉结束才归零
    #: （``record_sleep_episode`` / ``apply_activity``）。驱动清醒疲劳（``fatigue_ramp_extra``）。
    #: 为什么不复用 ``awake_minutes_today``：那个计数器在生活日边界（默认 12:00）清零，
    #: 会把「早上 8 点熬到次日凌晨 2 点」的连续清醒切成两段，疲劳永远攒不出来。
    #: 停机间隙不结算 ⇒ 也不计入（与「停机不记账」同一条纪律）。
    continuous_awake_minutes: int = 0
    sleep_debt_nights: int = 0
    #: 刚醒的赖床宽限窗截止（epoch 秒；0 = 没有宽限）。
    #: v1.15.0（PR-R3）：长睡眠醒来先进 ``daze``，这段时间里强制层不把她按回床、
    #: 也豁免最短停留期；另外它同时被**夜间易醒**（PR-R2）复用，
    #: 免掉「醒来 → 同一个 tick 又被硬约束送回床」的往复。
    wake_grace_until: float = 0.0
    #: 最近一次「半夜醒了一下」的时刻（epoch 秒；叙事与状态卡用，不参与倍率）。
    last_night_waking_at: float = 0.0
    #: 「本轮睡眠时段已经睡够」的截止时刻（epoch 秒；0 = 没有这条抑制）。
    #: v1.15.0：睡满最短睡眠目标醒来时置成本轮窗口的结束时刻；在此之前窗口驱动的
    #: 入睡一律不许（否则会出现「窗口内睡出第二个整觉」，见 ``ActivityFacts.rested``）。
    rested_until: float = 0.0
    #: 最近若干段**已结束**的睡眠：``[{"start": 入睡时刻, "end": 醒来时刻,
    #: "minutes": 分钟数}, ...]``（``start`` / ``end`` 为权威字段，按与窗口的
    #: 交集求和；``minutes`` 只是给人看）。「这一觉睡够没有」按最近 24 小时的
    #: **滑动窗口**判（``grade_sleep_debt``），所以需要它：生活日的当日计数器会在
    #: 12:00 清零，跨边界的一段睡眠会被两个生活日各记一半，于是边界恰好落在一段
    #: 睡眠中间时，会把**没睡完的觉**当成一整夜来判（真机 2026-10-02：她 08:00
    #: 睡下，12:00 边界只记到 4 小时）。
    sleep_ledger: list[dict[str, Any]] = field(default_factory=list)

    cold_until: float = 0.0
    cold_days: int = 0
    cold_checked_day: str = ""

    # ---- 病程系统（v1.14.0）----
    #: 病程阶段：``onset`` / ``worsening`` / ``recovering``；空串 = 非生病（含病后余韵）。
    #: **与 ``cold_until`` 独立**：阶段流转会改阶段而未必改预期痊愈时刻（反之亦然），
    #: 派生任何一个都会让两者互相绑死、没法单独调。
    cold_stage: str = ""
    #: 本次中招时刻（epoch 秒；0 = 不知道）。「感冒第几天」由它算。
    cold_started_at: float = 0.0
    #: 阶段流转的当日去重（与 ``cold_checked_day`` 同模式，各管各的：
    #: 一个是「今天掷过骰子没有」，一个是「今天结算过阶段没有」）。
    cold_stage_checked_day: str = ""
    #: 痊愈后免疫期截止（epoch 秒；这段时间内骰子直接跳过）。
    cold_immunity_until: float = 0.0
    #: 病后余韵截止（epoch 秒）。余韵**不算生病**（``is_cold`` 为假），只进提示词与事件。
    convalescent_until: float = 0.0
    #: 生活日 → 当日收到的关心次数（跨日清理，同 ``social_daily`` 模式）。
    care_today: dict[str, float] = field(default_factory=dict)
    #: 会话 id → 送药冷却截止（epoch 秒）。防单会话刷命令。
    medicine_cooldown: dict[str, float] = field(default_factory=dict)
    #: 本场感冒已生效的送药次数与累计缩短秒数（封顶用）；``cold_medicine_key``
    #: 是计数的归属场次（``cold_started_at`` 的字符串形式），换了场次自动归零。
    cold_medicine_count: int = 0
    cold_medicine_seconds: float = 0.0
    cold_medicine_key: str = ""

    # ---- 生理锚点（v1.9.1，physio）----
    #: 饱腹 0–10：随清醒时间下降（睡着不饿），进餐回满。见 life_physio。
    satiety: float = 8.0
    #: 上次进餐时刻（epoch 秒；0 = 从未吃过）。
    last_meal_at: float = 0.0
    #: 本生活日已进餐次数（跨日清零，见 _settle_day）。
    meal_count_today: int = 0
    #: 上次洗澡时刻（epoch 秒；0 = 从未洗过）。
    last_bath_at: float = 0.0

    # ---- 内心维度（v1.10.1，mood）----
    #: 压力 0–10：慢变；只进 prompt 与素材，**不进倍率**（纪律见 life_mood 模块说明）。
    stress: float = 3.0
    #: 孤独 0–10：无互动累积、被找回落；同上不进倍率。
    loneliness: float = 4.0
    #: 社交电量 0–10：孤独的镜像——社交耗电、独处与睡眠回血。
    social_battery: float = 7.0
    #: 最近一次「有人互动」的时刻（epoch 秒；供 loneliness 的 6 小时判据）。
    last_contact_at: float = 0.0
    #: 压力**持续**处于高位的起点（epoch 秒；0 = 当前不高）。
    #: v1.16.2（M3a）：高压崩溃事件要求「stress ≥ 阈值持续 ≥ 2 小时」，所以要记起点；
    #: 由 ``life_mood.evolve`` 每 tick 维护，回落到 ``stress_breakdown_reset`` 以下时清零。
    stress_high_since: float = 0.0
    #: 消息风格注入的去重表：``(session_id, day_key)`` → 已注入的时刻。
    #: 每天每会话至多一条（「她最近压力很大」天天说就成了 system 广播）。
    mood_injected: dict[str, float] = field(default_factory=dict)

    # ---- 模型调用分解统计（v1.17.0，PR-OBS-1）----
    #: ``"生活日|桶"`` → 次数。桶：asked / failed / skip_routine / skip_physio /
    #: skip_pointless / skip_interrupt / skip_window。
    #: 为什么需要：只有一个总数（``_skipped_llm_calls``）时，用户分不清「习惯窗口
    #: 省下的」与「注定白问省下的」，也就无法判断模型是不是被习惯表饿死了。
    #: 与 ``care_today`` 同一套「按生活日惰性清理」的字符串键模式。
    llm_ask_stats: dict[str, float] = field(default_factory=dict)

    # ---- 打断机制（v1.11.1，interrupt）----
    #: 被打断前的主活动；空 = 不在打断窗口。
    interrupted_from: str = ""
    #: 打断窗口截止（epoch 秒；0 = 没有窗口）。
    interrupt_until: float = 0.0
    #: 本次打断的起点（epoch 秒；0 = 没有窗口）——顺延上限用它算「这次打断总共
    #: 回了多久」（R4：活跃群每 5 分钟一条 @ 能把窗口无限顺延，她会永久停在
    #: 聊天中、习惯表与三餐窗整段失效）。
    interrupt_started_at: float = 0.0
    #: 打断事实注入的去重表：``(session_id, 原活动)`` → 已注入时刻。
    #: 独立于 ``mood_injected``：两张表的批次键语义不同（mood 是按**生活日**、
    #: interrupt 是按**这一次打断**），混用会让「同一会话今天第二次被打断时
    #: 恰好原活动相同」而漏注入。
    interrupt_injected: dict[str, float] = field(default_factory=dict)

    # ---- 多活动并行（v1.12.1，side_activities）----
    #: 背景活动（「吃饭时顺便看番」）：**只由模型提议**，enforce 不收口（方案五）。
    #: 上限 2、白名单 SIDE_ACTIVITIES；打断时清空（回消息不会「顺便看番」），
    #: 回退不恢复（背景是上一轮的决策产物，恢复会穿帮）。
    side_activities: list[str] = field(default_factory=list)

    # ---- 主动开口动机（v1.13.0，motives）----
    #: 动机素材去重表：``(类:键:生活日)`` → 已生成时刻。独立成表：
    #: 与节日 ``fired_date_keys``（列表语义）和 mood 注入（会话维度）都不同构。
    motive_seen: dict[str, float] = field(default_factory=dict)

    fired_date_keys: list[str] = field(default_factory=list)
    materials: list[dict[str, Any]] = field(default_factory=list)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    #: 社交经历的去重表（键 → 首次入库时间）。命名空间见 ``life_social`` 的模块说明。
    #: 它与 ``recent_events`` 分开存：经历会被 ``_keep_tail`` 截断，去重表不能跟着丢，
    #: 否则每次截断后同一件事都会被重新记一遍。
    social_seen: dict[str, float] = field(default_factory=dict)
    #: 生活日 → 该日已经花掉的社交情绪额度。防止「群里刷屏 = 无限情绪」：
    #: 额度的松紧必须由配置决定，而不是由今天群里多热闹决定。
    social_daily: dict[str, float] = field(default_factory=dict)
    #: 「外面的世界」经历的去重表（键 → 首次入库时间）。命名空间见 ``life_world``：
    #: ``live!<房间>!<生活日>`` / ``push!<url>`` / ``newcomer!<群>!<QQ>`` / ``song!<歌名>!<命中时刻>``。
    #: 与 ``social_seen`` 分开：两边的键命名空间独立，混用会让 prune 互相挤掉。
    world_seen: dict[str, float] = field(default_factory=dict)
    #: 生活日 → 该日已经花掉的「世界」情绪额度。外部世界不能挤掉她自己的情绪基线。
    world_daily: dict[str, float] = field(default_factory=dict)

    skip_ledger: dict[str, int] = field(default_factory=dict)
    # 会话 id → 我们最后写到宿主上的倍率。用来区分「宿主上的值是本人写的」还是
    # 「别人写的（例如 budget-pacer）」——宿主只有一个标量，后写覆盖先写。
    applied: dict[str, float] = field(default_factory=dict)
    # 会话 id → 宿主上被识别为「别人写的」倍率基数。下发时是 基数 × 生活倍率，
    # 卸载时归还基数而不是写 1.0，否则会把别的插件正在生效的压制抹掉。
    foreign: dict[str, float] = field(default_factory=dict)
    # 会话 id → 我们**写入之前**读到的宿主值。用来判断那一笔到底有没有生效：
    # 宿主值还停在这个老值上 ⇒ 写入被静默吃掉了（该会话还没有 heartflow chat 对象，
    # ``heartflow_manager.adjust_talk_frequency`` 会 no-op 且能力层照样返回 success）。
    observed: dict[str, float] = field(default_factory=dict)
    # 会话 id → 允许再次尝试写入的时间戳。对「写不进去」的会话退避，
    # 否则每个巡检都白写一遍（宿主还会逐条打 warning）。
    unbacked: dict[str, float] = field(default_factory=dict)
    # 会话 id → 退避时**尝试写入的那个目标值**。目标变了（例如进入睡眠要归零）
    # 就必须无视退避立刻重试，否则她会带着全速倍率睡觉最长一个退避窗口。
    unbacked_target: dict[str, float] = field(default_factory=dict)
    # 会话 id → 连续失败次数，用来做指数退避（历史会话可能成百上千，
    # 固定间隔重试会攒出 10^4/天 量级的无用 RPC 与宿主 warning）。
    unbacked_strikes: dict[str, float] = field(default_factory=dict)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    paused_override: bool = False
    """命令里的「暂停」写在这里而不是配置里：状态文件会持久化，重启后仍然有效。"""

    llm_fail_streak: int = 0
    llm_cooldown_until: float = 0.0
    llm_last_success_at: float = 0.0
    llm_last_raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: object) -> "LifeState":
        """从（可能损坏的）字典恢复状态；未知字段忽略，缺失字段用默认值。

        列表字段（``materials`` / ``recent_events``）做**条目级**净化：非 dict 条目、
        数值字段非有限/非数值的条目直接丢弃。否则一个 ``expires_at: "abc"`` 就能让
        ``settle`` 每 tick 抛错、``active_materials`` 每轮抛错、倍率再也不下发，
        而且因为异常发生在列表推导内部（赋值不执行）永远无法自愈——只能手删状态文件。
        """

        state = cls()
        if not isinstance(raw, dict):
            return state
        for key, value in raw.items():
            if not hasattr(state, key):
                continue
            current = getattr(state, key)
            try:
                if isinstance(current, bool):
                    # 只接受真正的布尔：``bool("false") is True``，用强转会把
                    # 「paused_override: "false"」变成「已暂停」这种静默反转。
                    setattr(state, key, value if isinstance(value, bool) else current)
                elif isinstance(current, float):
                    # v1.8.2 修：只认真的数字，且**必须有限**。以前 ``float(value)``
                    # 会把 JSON 的 ``NaN``/``Infinity`` 字面量原样收进来——而
                    # ``last_tick_at=NaN`` 让 settle 的比较恒为假 ⇒ 生活状态每 tick
                    # 空转、记账零推进且**无任何日志**；``cold_until=inf`` 是永久感冒；
                    # ``llm_last_success_at=NaN`` 让「模型失败重取种子」安全阀失效。
                    # 字符串数字同样不猜（与 _sanitize_float_map 同一条原则）。
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        continue
                    number = float(value)
                    if not math.isfinite(number):
                        continue
                    setattr(state, key, number)
                elif isinstance(current, int):
                    # v1.8.2 修：先过 float 再取整。以前 ``int(value)`` 对
                    # ``Infinity`` 抛 OverflowError——不在下面的 except 名单里，
                    # 会穿透出去让**插件加载失败**（违背本方法的容错契约）。
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        continue
                    number = float(value)
                    if not math.isfinite(number):
                        continue
                    setattr(state, key, int(number))
                elif isinstance(current, str):
                    # 上限防御：坏文件里的超长字符串不该原样进状态文件（渲染层另有截断）
                    setattr(state, key, str(value)[:2000])
                elif isinstance(current, list):
                    setattr(state, key, list(value) if isinstance(value, list) else [])
                elif isinstance(current, dict):
                    setattr(state, key, dict(value) if isinstance(value, dict) else {})
            except (TypeError, ValueError, OverflowError):
                continue
        if state.activity not in ALLOWED_ACTIVITIES:
            state.activity = DAILY
        state.emotion = _clamp(state.emotion, 0.0, EMOTION_MAX)
        state.energy = _clamp(state.energy, 0.0, ENERGY_MAX)
        state.energy_cap = _clamp(state.energy_cap, 1.0, ENERGY_MAX)
        # v1.16.0（M1）：连续清醒分钟数收口到 [0, 上限]。负数/坏值按 0（= 刚醒），
        # 超大值按上限——``int`` 分支已拦掉 NaN/inf，这里只管范围。
        state.continuous_awake_minutes = max(
            0, min(MAX_CONTINUOUS_AWAKE_MINUTES, int(state.continuous_awake_minutes))
        )
        if not math.isfinite(state.at_wake_until) or state.at_wake_until < 0.0:
            # 坏时间戳按「没有唤醒窗口」处理：``inf`` 会让她从此不睡，``nan`` 的比较恒为假
            # （看着无害，但状态卡会印出 nan，排查时无从判断）
            state.at_wake_until = 0.0
        # v1.15.0（PR-R2/R3）：两个新时间戳同款收口（非有限/负数按「没有」处理）
        # v1.16.2（M3a）：``stress_high_since`` 同待遇——坏值按「当前不高」，否则
        # 一个 NaN 会让高压计时恒不成立（她永远不会「绷不住」，且没有任何日志）。
        for stamp in ("wake_grace_until", "last_night_waking_at", "rested_until",
                      "stress_high_since"):
            value = float(getattr(state, stamp) or 0.0)
            setattr(state, stamp, value if math.isfinite(value) and value > 0.0 else 0.0)
        state.applied = _sanitize_adjust_map(state.applied)
        state.foreign = _sanitize_adjust_map(state.foreign)
        state.observed = _sanitize_adjust_map(state.observed)
        state.unbacked = _sanitize_adjust_map(state.unbacked)
        state.unbacked_target = _sanitize_adjust_map(state.unbacked_target)
        state.materials = _sanitize_records(
            state.materials, fields=("created_at", "expires_at", "best_until", "weight")
        )
        state.recent_events = _sanitize_records(
            state.recent_events, fields=("at", "emotion", "energy")
        )
        # 睡眠账本同样做条目级净化：一个坏条目会让 sleep_in_window / grade_sleep_debt
        # 在每次醒来时抛错，而异常发生在判分内部 ⇒ 熬夜统计永久失真且无法自愈。
        # ``start`` / ``end`` 是权威字段（按区间交集求和），所以只校验这两个。
        state.sleep_ledger = _sanitize_records(
            state.sleep_ledger, fields=("start", "end")
        )
        state.social_seen = _sanitize_float_map(state.social_seen)
        state.social_daily = _sanitize_float_map(state.social_daily)
        state.world_seen = _sanitize_float_map(state.world_seen)
        state.world_daily = _sanitize_float_map(state.world_daily)
        # v1.8.2 修：这两张表以前走通用 dict(value) 不洗值——坏值会活到消费点才炸
        # （skip_ledger 在 bump_skip_ledger 的 int() 处、sessions 的时间戳 NaN 会让
        # 「对方刚说过话」的闸静默失效），与上面六张映射表对齐。
        state.skip_ledger = _sanitize_int_map(state.skip_ledger)
        state.sessions = _sanitize_session_map(state.sessions)
        # v1.9.1（physio）：饱腹钳进 0–10（float 分支已拦 NaN/inf，这里只管范围）
        state.satiety = _clamp(state.satiety, 0.0, 10.0)
        # v1.10.1（mood）：内心维度钳进 0–10；注入去重表洗值
        state.stress = _clamp(state.stress, 0.0, 10.0)
        state.loneliness = _clamp(state.loneliness, 0.0, 10.0)
        state.social_battery = _clamp(state.social_battery, 0.0, 10.0)
        state.mood_injected = {
            str(key): float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else 0.0
            for key, value in state.mood_injected.items()
            if str(key or "").strip()
        }
        # v1.13.1（interrupt）：坏时间戳按「没有窗口」处理；被中断的活动名必须合法
        if not math.isfinite(state.interrupt_until) or state.interrupt_until < 0.0:
            state.interrupt_until = 0.0
        if not math.isfinite(state.interrupt_started_at) or state.interrupt_started_at < 0.0:
            state.interrupt_started_at = 0.0
        if state.interrupted_from and state.interrupted_from not in ALLOWED_ACTIVITIES:
            state.interrupted_from = ""
        state.interrupt_injected = _sanitize_float_map(state.interrupt_injected)
        state.motive_seen = _sanitize_float_map(state.motive_seen)
        # v1.14.0（病程系统）：阶段名过白名单（坏值按旧口径 = 生病但无阶段 ⇒ 加重），
        # 时间戳与计数做有限性收口，两张映射表洗值。
        state.cold_stage = normalize_cold_stage(state.cold_stage)
        for stamp in (
            "cold_started_at",
            "cold_immunity_until",
            "convalescent_until",
            "cold_medicine_seconds",
        ):
            value = float(getattr(state, stamp) or 0.0)
            setattr(state, stamp, value if math.isfinite(value) and value > 0.0 else 0.0)
        state.cold_medicine_count = max(0, int(state.cold_medicine_count))
        state.care_today = _sanitize_float_map(state.care_today)
        state.medicine_cooldown = _sanitize_float_map(state.medicine_cooldown)
        # v1.17.0（PR-OBS-1）：模型调用分解统计（"生活日|桶" → 次数），坏值当 0
        state.llm_ask_stats = _sanitize_float_map(state.llm_ask_stats)
        # v1.12.1（side）：背景活动逐项过白名单、去重、上限 2（坏值静默丢弃）
        cleaned_side: list[str] = []
        raw_side = state.side_activities
        if isinstance(raw_side, str):
            raw_side = (raw_side,)
        if isinstance(raw_side, (list, tuple)):
            for item in raw_side:
                activity = str(item or "").strip()
                if (
                    activity in ALLOWED_ACTIVITIES
                    and activity in SIDE_ACTIVITIES
                    and activity not in cleaned_side
                ):
                    cleaned_side.append(activity)
                if len(cleaned_side) >= MAX_SIDE_ACTIVITIES:
                    break
        state.side_activities = cleaned_side
        return state


def day_key_of(local_dt: datetime, boundary_hour: int) -> str:
    """把本地时刻映射到「生活日」标识：``[boundary, boundary+24h)`` 为一天。

    这样凌晨 03:00–11:00 的睡眠会落在**前一天**的账上，正好在当天 12:00 结算，
    与「每天 12 点结算昨晚睡眠」的直觉一致。
    """

    shifted = local_dt - timedelta(hours=int(boundary_hour))
    return shifted.date().isoformat()


def local_datetime(now: float, tz_offset_minutes: int) -> datetime:
    """把 epoch 秒 + 时区偏移换成「本地」datetime（用 UTC 承载，避免依赖宿主时区）。"""

    return datetime.fromtimestamp(float(now) + int(tz_offset_minutes) * 60, tz=timezone.utc)


# ---------------------------------------------------------------- 构造


def new_state(
    *,
    now: float,
    config: SimConfig,
    energy: float = 6.0,
    emotion: float = BASELINE_EMOTION,
) -> LifeState:
    """冷启动状态：用时段表取一次种子，之后不再用时段表。"""

    local_now = local_datetime(now, config.tz_offset_minutes)
    seed = rule_based_activity(
        now_minutes=local_now.hour * 60 + local_now.minute,
        energy=energy,
        sick=False,
        sleep_energy_threshold=config.sleep_energy_threshold,
        schedule=schedule_facts(local_now, config.schedule),
        work_scene=config.schedule.work_scene,
    )
    state = LifeState(
        state_version=STATE_VERSION,
        last_tick_at=float(now),
        day_key=day_key_of(local_now, config.day_boundary_hour),
        activity=seed.activity,
        activity_source=SOURCE_COLD_START,
        activity_note=seed.note,
        activity_since=float(now),
        scene=seed.scene,
        emotion=_clamp(emotion, 0.0, config.emotion_max),
        energy=_clamp(energy, 0.0, config.energy_max),
        energy_cap=config.energy_max,
        # 冷启动就把「最近一次成功」置为当前时间：这样 reseed_after_hours 是从冷启动
        # 起算的，而不是被每 tick 前进的 last_tick_at 拖着走（那样子安全阀永远触发不了）。
        llm_last_success_at=float(now),
    )
    if is_asleep(state.activity):
        state.sleep_started_at = float(now)
    return state


def is_cold(state: LifeState, now: float) -> bool:
    return float(now) < float(state.cold_until)


def cold_stage(state: LifeState, now: float) -> str:
    """当前病程阶段；不生病返回空串（**病后余韵也返回空串**——余韵不是病）。

    旧状态只有 ``cold_until`` 没有阶段时按 ``worsening`` 处理（与
    ``life_activity.effective_cold_stage`` 同一口径，保证存量行为不变）。
    """

    if not is_cold(state, now):
        return ""
    return normalize_cold_stage(getattr(state, "cold_stage", "")) or COLD_WORSENING


def in_convalescence(state: LifeState, now: float) -> bool:
    """是否处在「病刚好、还虚着」的余韵窗口里（不是生病）。"""

    if is_cold(state, now):
        return False
    until = float(getattr(state, "convalescent_until", 0.0) or 0.0)
    return math.isfinite(until) and until > 0.0 and float(now) < until


def cold_day_index(state: LifeState, now: float, config: SimConfig) -> int:
    """「感冒第几天」（1 起）。锚点缺失时用 ``cold_until − cold_days`` 反推。

    真人说的是「病第二天」，而不是「约剩三十七点五小时」——给模型的口径用这个。
    """

    del config  # 目前不需要配置；留着是为了与其它取数函数同形（未来按天边界算可用）
    started = float(getattr(state, "cold_started_at", 0.0) or 0.0)
    if not (math.isfinite(started) and started > 0.0):
        days = max(1, int(state.cold_days or 1))
        until = float(state.cold_until or 0.0)
        if math.isfinite(until) and until > 0.0:
            started = until - days * 86400.0
    if not (math.isfinite(started) and started > 0.0):
        return 1
    return max(1, int((float(now) - started) // 86400.0) + 1)


def _sleep_debt_label(state: LifeState, config: SimConfig) -> str:
    if int(state.sleep_debt_nights) >= int(config.sleep_debt_cap_nights):
        return f"连续熬夜 {state.sleep_debt_nights} 天，体力上限被压低"
    if int(state.sleep_debt_nights) > 0:
        return f"有点缺觉（连熬 {state.sleep_debt_nights} 天）"
    return "健康"


def health_label_prompt(state: LifeState, now: float, config: SimConfig) -> str:
    """**进 LLM** 的健康描述（v1.14.0 §7）：模糊病程口径，不给精确剩余时间。

    她不该知道自己「约剩 37.5 小时痊愈」——那是上帝视角，真人只知道「病第二天，
    嗓子还疼」。精确口径见 ``health_label_admin``（状态卡/排查用）。
    """

    stage = cold_stage(state, now)
    if stage:
        day = cold_day_index(state, now, config)
        text = {
            COLD_ONSET: "有点小感冒，嗓子不太舒服",
            COLD_WORSENING: "感冒正厉害，烧得迷迷糊糊",
            COLD_RECOVERING: "感冒在好转，人还有点虚",
        }.get(stage, "感冒中，人不太舒服")
        return f"{text}（第 {day} 天）"
    if in_convalescence(state, now):
        return "感冒刚好利索，体力还没完全回来"
    return _sleep_debt_label(state, config)


def _day_key_now(now: float, config: SimConfig) -> str:
    return day_key_of(
        local_datetime(now, config.tz_offset_minutes), config.day_boundary_hour
    )


def health_label_admin(state: LifeState, now: float, config: SimConfig) -> str:
    """**状态卡/排查**用的精确口径：阶段 + 第几天 + 剩余小时 + 病假 + 关心次数。"""

    stage = cold_stage(state, now)
    if stage:
        remaining = max(0.0, (float(state.cold_until) - float(now)) / 3600.0)
        text = (
            f"感冒{COLD_STAGE_LABELS.get(stage, stage)}"
            f"（第 {cold_day_index(state, now, config)} 天，约剩 {remaining:.1f} 小时）"
        )
        extras: list[str] = []
        # 「已请病假」只在真有班表、且今天是工作日时才说——没有班表的角色本来就不上班，
        # 状态卡印一句「已请病假」是凭空造出来的事实。
        if on_sick_leave(stage, enabled=bool(config.cold_sick_leave)) and _is_workday_for(
            state, now=now, config=config
        ):
            extras.append("已请病假")
        care = float(state.care_today.get(_day_key_now(now, config), 0.0) or 0.0)
        if care > 0:
            extras.append(f"今日收到关心 {int(care)} 次")
        return text + ("；" + "；".join(extras) if extras else "")
    if in_convalescence(state, now):
        left = max(0.0, (float(state.convalescent_until) - float(now)) / 3600.0)
        return f"病后余韵（约剩 {left:.1f} 小时）"
    base = _sleep_debt_label(state, config)
    immunity = float(getattr(state, "cold_immunity_until", 0.0) or 0.0)
    if math.isfinite(immunity) and immunity > float(now):
        days = (immunity - float(now)) / 86400.0
        return f"{base}；感冒免疫期还剩 {days:.1f} 天"
    return base


def health_label(state: LifeState, now: float, config: SimConfig) -> str:
    """兼容别名：**精确口径**（``health_label_admin``）。

    存量调用点（状态卡、工具、测试）都取这个名字；LLM 侧请显式用
    ``health_label_prompt``——两者口径不同是刻意的（v1.14.0 §7）。
    """

    return health_label_admin(state, now, config)


def _minutes_since(now: float, anchor: object) -> int:
    """``anchor`` 到 ``now`` 的分钟数；锚点不可信（<=0 或在未来）时返回 0。

    宁可返回 0（=「无从判断」）也不要返回一个巨大的假值：``activity_since`` 在旧
    状态里可能是 0，按 ``now - 0`` 算会得到几十万分钟，于是「睡够上限了」这种判断
    会在她刚躺下时就成立（v1.1.1 开发中踩到过：立刻被叫醒，等于永远不睡）。
    """

    value = _as_float(anchor, 0.0) or 0.0
    if value <= 0 or value > float(now):
        return 0
    return int(max(0.0, (float(now) - value) // 60))


def activity_facts(state: LifeState, now: float) -> ActivityFacts:
    """当前状态 → ``enforce`` 需要的事实（本地时间由 tz 偏移还原）。"""

    single_sleep = 0
    if is_asleep(state.activity):
        # 优先用「她几点睡下的」这个专用字段；缺失时退回活动起始时间
        # v1.15.0（PR-R1）：小睡同样走这条（``is_asleep``），否则小睡时
        # ``minutes_in_sleep`` 恒为 0（= 锚点不可信），时长上限判定会失效。
        single_sleep = _minutes_since(now, state.sleep_started_at) or _minutes_since(
            now, state.activity_since
        )
    return ActivityFacts(
        activity=state.activity,
        minutes_in_activity=_minutes_since(now, state.activity_since),
        minutes_in_sleep=single_sleep,
        now_minutes=0,  # 由 settle / plugin 用本地时间覆盖
        emotion=state.emotion,
        energy=state.energy,
        energy_cap=state.energy_cap,
        sick=is_cold(state, now),
        # v1.14.0（病程系统）：强制层按阶段分层（初起不强制养病 / 好转放宽轻活动）。
        # 阶段为空 + sick=True 时那边按 worsening 兜底（= 旧行为）。
        cold_stage=cold_stage(state, now),
        sleep_minutes_today=state.sleep_minutes_today,
        awake_minutes_today=state.awake_minutes_today,
    )


# ---------------------------------------------------------------- 结算


def _keep_tail(items: list[Any], keep: int) -> list[Any]:
    """保留末尾 ``keep`` 条；``keep <= 0`` 表示**不保留**（不是保留 1 条）。

    v1.1.0 用 ``[-max(1, n):]``，于是 ``materials_keep = 0`` / ``recent_events_keep = 0``
    与 ``recent_events_in_prompt = 0`` 的语义与字面相反（想清空却永远留 1 条）。
    """

    count = max(0, int(keep))
    return list(items[-count:]) if count else []


def _sweep(state: LifeState, now: float, config: SimConfig) -> LifeState:
    """过期清理：素材、近期事件、日期触发记录。

    取数一律走 ``_as_float``：坏条目在这里**丢弃**而不是抛错。抛错的代价见
    ``_as_float`` 的说明（每 tick 炸一次且永不自愈）。
    """

    state.materials = _keep_tail(
        [
            item
            for item in state.materials
            if isinstance(item, dict)
            and (_as_float(item.get("expires_at"), 0.0) or 0.0) > float(now)
        ],
        config.materials_keep,
    )
    state.recent_events = _keep_tail(
        [item for item in state.recent_events if isinstance(item, dict)],
        config.recent_events_keep,
    )
    state.fired_date_keys = [str(key) for key in state.fired_date_keys][-366:]
    # v1.14.0（病程）：关心计数按生活日留两天（今天够用，昨天只给排查看），
    # 送药冷却只留还没到期的条目——这两张表都是「历史上百条无意义」的类型。
    #
    # ⚠ 日期一律用 ``local_datetime(now) - timedelta`` 推，**不要**拿 ``now - 86400``
    # 再转换：测试夹具会用 1000 / 2000 这种小纪元，减一天会变成负时间戳，
    # ``datetime.fromtimestamp`` 直接抛 OSError（v1.14.0 实测踩到）。
    try:
        local_now = local_datetime(float(now), config.tz_offset_minutes)
        day_today = day_key_of(local_now, config.day_boundary_hour)
        day_yesterday = day_key_of(
            local_now - timedelta(days=1), config.day_boundary_hour
        )
    except (OSError, OverflowError, ValueError):
        # 时钟不可信（极端小/大 epoch）时**跳过清理**而不是抛错：清理是优化，不是正确性
        day_today = ""
        day_yesterday = ""
    if state.care_today and day_today:
        state.care_today = {
            str(key): value
            for key, value in state.care_today.items()
            if str(key) in (day_today, day_yesterday)
        }
    if state.llm_ask_stats and day_today:
        # v1.17.0（PR-OBS-1）：键是 ``"生活日|桶"``——按前缀清理（与 care_today
        # 的整键匹配不同，这里必须拆开比；昨天以前的统计不再有任何读点）
        state.llm_ask_stats = {
            str(key): value
            for key, value in state.llm_ask_stats.items()
            if str(key).split("|", 1)[0] in (day_today, day_yesterday)
        }
    if state.medicine_cooldown:
        state.medicine_cooldown = {
            str(key): value
            for key, value in state.medicine_cooldown.items()
            if float(value or 0.0) > float(now)
        }
    return state


def _apply_event(
    state: LifeState,
    event: LifeEvent,
    *,
    now: float,
    config: SimConfig,
    rng: random.Random,
) -> None:
    """把一条事件落到状态上：改情绪体力、进惰性期、产素材、记近期经历。"""

    del rng  # 事件本身不消耗随机性（抽取时已用掉）
    # v1.16.3（M6）：增量按当前情绪水平做边际缩放。**记录的是缩放后的增量**——
    # 余波与归因都按它算（记原始值会让卡片与她真正经历的情绪对不上）。
    applied = scaled_emotion_delta(float(state.emotion), float(event.emotion), config)
    state.emotion = _clamp(state.emotion + applied, 0.0, config.emotion_max)
    state.energy = _clamp(state.energy + event.energy, 0.0, state.energy_cap)
    # v1.16.1（M5b）：惯性期按冲击大小缩放（关掉 = 固定 inertia_minutes）
    state.inertia_until = float(now) + scaled_inertia(applied if applied else event.emotion, config)

    text = sanitize_text(event.material, max_chars=80)
    if text:
        ttl_seconds = float(event.ttl_hours) * 3600.0
        ratio = max(0.0, min(1.0, float(config.material_best_ratio)))
        state.materials.append(
            {
                "label": event.label,
                "text": text,
                "weight": float(event.weight),
                "created_at": float(now),
                "expires_at": float(now) + ttl_seconds,
                # 「最佳保鲜相位」：TTL 的前 ratio 段全额权重，之后线性衰减
                # （见 material_freshness）。ratio=1 时与 expires_at 重合 = 旧行为。
                "best_until": float(now) + ttl_seconds * ratio,
            }
        )
    state.recent_events.append(
        {
            "at": float(now),
            "label": event.label,
            "activity": state.activity,
            "text": text,
            "emotion": float(applied),
            "energy": float(event.energy),
        }
    )


# ---------------------------------------------------------------- 病程系统（v1.14.0）

#: 病程的**确定性产出**（§4）：五个时点各一条，不走 40% 随机抽取，到点必发。
#: 键 → ``(素材, 经历标签, weight, TTL 小时)``；素材为空串 = 只记经历（请假是行政事实）。
_COLD_EVENTS: dict[str, tuple[str, str, float, float]] = {
    COLD_ONSET: ("嗓子有点痒，好像要感冒了", "好像有点感冒的苗头", 0.85, 12.0),
    COLD_WORSENING: ("烧上来了，今天大概只能躺着", "感冒加重了", 0.70, 8.0),
    COLD_RECOVERING: ("烧退了，感觉活过来了", "开始退烧了", 0.75, 8.0),
    "healed": ("终于好利索了，病一场真耽误事", "感冒好了", 0.80, 12.0),
    "sick_leave": ("", "打电话请了病假", 0.60, 8.0),
}

#: 单次阶段流转对 ``cold_until`` 的最大调整步长（天）。§2.3 的纪律：
#: 「关心刷满 = 秒愈」与「连熬 = 永久感冒」都靠这个封顶挡住。
_COLD_STEP_MIN_DAYS = 0.5
_COLD_STEP_MAX_DAYS = 1.0
#: ``cold_until`` 相对 ``cold_started_at`` 的总上限放宽（天）：``cold_max_days + 2``。
_COLD_CAP_SLACK_DAYS = 2.0
#: 向好流转后 ``cold_until`` 至少留下的余量（小时）——别把她当场治到「还剩 0 分钟」。
_COLD_MIN_REMAINING_HOURS = 6.0


def _cold_episode(state: LifeState) -> str:
    """本次感冒的场次键（用于病程事件的去重，换一场病自动换键）。"""

    started = float(getattr(state, "cold_started_at", 0.0) or 0.0)
    if math.isfinite(started) and started > 0.0:
        return str(int(started))
    until = float(getattr(state, "cold_until", 0.0) or 0.0)
    return f"until:{int(until)}" if math.isfinite(until) and until > 0.0 else "unknown"


def _record_deterministic_event(
    state: LifeState,
    *,
    now: float,
    config: SimConfig,
    label: str,
    material: str,
    weight: float,
    ttl_hours: float,
    emotion: float = 0.0,
) -> None:
    """确定性事件的公共装配（v1.16.2 抽出）：素材（含最佳保鲜相位）+ 经历。

    病程事件（``_illness_event``）与高压崩溃（``settle_stress_breakdown``）共用这一份：
    两处各写一份迟早会漂（一边补了 ``best_until``、另一边忘了）。
    ``emotion`` 同时写进经历——它既进归因对账，也被 ``_recompute_afterglow`` 计入余波。
    """

    text = sanitize_text(material, max_chars=80)
    if text:
        ttl_seconds = float(ttl_hours) * 3600.0
        ratio = max(0.0, min(1.0, float(config.material_best_ratio)))
        state.materials.append(
            {
                "label": label,
                "text": text,
                "weight": float(weight),
                "created_at": float(now),
                "expires_at": float(now) + ttl_seconds,
                "best_until": float(now) + ttl_seconds * ratio,
            }
        )
    state.recent_events.append(
        {
            "at": float(now),
            "label": label,
            "activity": state.activity,
            "text": text,
            "emotion": float(emotion),
            "energy": 0.0,
        }
    )


def _illness_event(
    state: LifeState, *, now: float, config: SimConfig, episode: str, key: str
) -> bool:
    """落一条病程事件（素材 + 经历）；同一场同一种只发一次。

    与 ``_apply_event`` 的三点差异都是有意的：

    1. **不改情绪/体力、不动 ``inertia_until``**：起病是事实通报，不该冻结情绪回归
       （口径同 ``append_social_event``）；
    2. **确定性**：不掷骰子，到点必发——「她病了你却不知道」正是本版要修的失真；
    3. 去重走 ``state.motive_seen``（键全程带场次），停机/重启后不会重发。
    """

    spec = _COLD_EVENTS.get(str(key))
    if spec is None:
        return False
    dedup = f"cold!{episode}!{key}"
    if dedup in state.motive_seen:
        return False
    state.motive_seen[dedup] = float(now)

    material, label, weight, ttl_hours = spec
    _record_deterministic_event(
        state, now=now, config=config, label=label, material=material,
        weight=weight, ttl_hours=ttl_hours,
    )
    return True


#: 内心维度的**确定性产出**（v1.16.2 M3a）。与病程 ``_COLD_EVENTS`` 同一套管线：
#: 硬编码、到点必发、**不进** ``[events] extra`` 的随机池（决议 2：它是内心维度的
#: 确定性产出，不是随机事件池的一员；用户只调开关与阈值，措辞随版本演进）。
#: 键 → ``(素材, 经历标签, weight, TTL 小时, 情绪增量)``
_MOOD_EVENTS: dict[str, tuple[str, str, float, float, float]] = {
    STRESS_BREAKDOWN: ("最近真的有点累，感觉自己快绷不住了", "情绪有点绷不住", 0.75, 8.0, -0.8),
}


def settle_stress_breakdown(
    state: LifeState, *, now: float, day_key: str, config: SimConfig
) -> bool:
    """高压崩溃（v1.16.2 M3a）：压力**持续**偏高时，确定性地产出一条外显的情绪下滑。

    为什么需要它（方案 P2）：注入的风格提示写着「她最近压力很大，说话会比平时短、
    没什么耐心」，而那时 ``emotion`` 完全可能还在 8.0——**措辞说没耐心，倍率却比
    平时还高**。崩一次之后，提示词与数值终于指同一件事。

    判据（全满足）：开关开、``stress ≥ threshold``、持续 ≥ ``hours``、本生活日还没崩过。
    高压起点由 ``life_mood.evolve`` 维护（``state.stress_high_since``），回落到
    ``stress_breakdown_reset`` 以下时清零 ⇒ 下次高压要重新计时。

    ⚠ 与 ``_illness_event`` 的差异**有意**：它**会**改 ``emotion`` 并进惰性期
    （一次真实的情绪打击，不是事实通报）。副作用是它也进余波，这正是「压力在外显
    情绪上显形」的意思。
    """

    if not bool(getattr(config, "stress_breakdown_enabled", False)):
        return False
    spec = _MOOD_EVENTS.get(STRESS_BREAKDOWN)
    if spec is None:
        return False
    since = float(state.stress_high_since)
    if since <= 0.0:
        return False
    if float(state.stress) < float(config.stress_breakdown_threshold):
        return False
    if float(now) - since < max(0.0, float(config.stress_breakdown_hours)) * 3600.0:
        return False
    dedup = f"mood!{STRESS_BREAKDOWN}!{day_key}"
    if dedup in state.motive_seen:
        return False
    state.motive_seen[dedup] = float(now)

    material, label, weight, ttl_hours, emotion = spec
    _record_deterministic_event(
        state, now=now, config=config, label=label, material=material,
        weight=weight, ttl_hours=ttl_hours, emotion=emotion,
    )
    state.emotion = _clamp(float(state.emotion) + float(emotion), 0.0, config.emotion_max)
    state.inertia_until = max(
        float(state.inertia_until), float(now) + scaled_inertia(emotion, config)
    )
    return True


def _immunity_active(state: LifeState, now: float) -> bool:
    """是否在痊愈后的免疫期里（修「病好第二天无缝再病」）。"""

    until = float(getattr(state, "cold_immunity_until", 0.0) or 0.0)
    return math.isfinite(until) and until > float(now)


def start_cold(
    state: LifeState, *, now: float, config: SimConfig, days: int
) -> LifeState:
    """中招：进入 ``onset``（初起）并落一条起病素材。

    ``cold_until`` 仍是**预期痊愈时刻**（语义不变，旧状态与状态卡都靠它）；
    ``cold_stage`` 与它独立——流转会改阶段而未必改预期时刻。
    """

    days = max(1, int(days))
    state.cold_days = days
    state.cold_until = float(now) + days * 86400.0
    state.cold_started_at = float(now)
    state.cold_stage = COLD_ONSET
    state.cold_stage_checked_day = _day_key_now(now, config)
    state.cold_medicine_count = 0
    state.cold_medicine_seconds = 0.0
    state.cold_medicine_key = ""
    state.convalescent_until = 0.0
    _illness_event(
        state, now=now, config=config, episode=_cold_episode(state), key=COLD_ONSET
    )
    return state


def finish_cold(state: LifeState, *, now: float, config: SimConfig) -> LifeState:
    """痊愈：清掉病程、开免疫期与病后余韵，并落一条痊愈素材。

    幂等（不在生病态时直接返回）。``cold_until=inf``（永久感冒夹具）**永远走不到
    这里**——``is_cold`` 恒为真，这正是「整活状态不该被自动治好」的口径。
    """

    if not str(getattr(state, "cold_stage", "") or "").strip():
        return state
    _illness_event(
        state, now=now, config=config, episode=_cold_episode(state), key="healed"
    )
    state.cold_stage = ""
    state.cold_until = 0.0
    state.cold_days = 0
    state.cold_started_at = 0.0
    state.cold_stage_checked_day = ""
    state.cold_medicine_count = 0
    state.cold_medicine_seconds = 0.0
    state.cold_medicine_key = ""
    state.cold_immunity_until = float(now) + max(0, int(config.cold_immunity_days)) * 86400.0
    state.convalescent_until = (
        float(now) + max(0, int(config.cold_convalescent_hours)) * 3600.0
    )
    return state


def _finish_cold_if_expired(state: LifeState, *, now: float, config: SimConfig) -> None:
    """病程到期（``cold_until`` 已过）却没走痊愈流程时补一次。"""

    if str(getattr(state, "cold_stage", "") or "").strip() and not is_cold(state, now):
        finish_cold(state, now=now, config=config)


def _shorten_cold(state: LifeState, *, now: float, config: SimConfig, rng: random.Random) -> None:
    """向好流转：把预期痊愈时刻小幅提前（封顶 + 不低于最小余量）。"""

    until = float(state.cold_until or 0.0)
    if not math.isfinite(until):
        return
    step = rng.uniform(_COLD_STEP_MIN_DAYS, _COLD_STEP_MAX_DAYS) * 86400.0
    floor = float(now) + _COLD_MIN_REMAINING_HOURS * 3600.0
    state.cold_until = max(floor, until - step)


def _extend_cold(state: LifeState, *, now: float, config: SimConfig, rng: random.Random) -> None:
    """向坏流转：把预期痊愈时刻小幅推后（总上限 ``cold_max_days + 2`` 天）。"""

    until = float(state.cold_until or 0.0)
    if not math.isfinite(until):
        return
    base = float(state.cold_started_at or 0.0)
    if not (math.isfinite(base) and base > 0.0):
        base = float(now)
    cap = base + (float(max(1, int(config.cold_max_days))) + _COLD_CAP_SLACK_DAYS) * 86400.0
    step = rng.uniform(_COLD_STEP_MIN_DAYS, _COLD_STEP_MAX_DAYS) * 86400.0
    state.cold_until = min(cap, until + step)


def _is_workday_for(state: LifeState, *, now: float, config: SimConfig) -> bool:
    """今天是不是（班表意义上的）工作日——病假事件的触发条件之一。"""

    schedule = config.schedule
    if not schedule.enabled:
        return False
    local_dt = local_datetime(now, config.tz_offset_minutes)
    return int(local_dt.isoweekday()) in {int(day) for day in schedule.workdays}


def _note_sick_leave(state: LifeState, *, now: float, config: SimConfig) -> None:
    """进入加重期且当天是工作日 → 记一条「请了病假」（§3.4，只记经历、不进素材池）。"""

    if not bool(config.cold_sick_leave):
        return
    if not _is_workday_for(state, now=now, config=config):
        return
    _illness_event(
        state, now=now, config=config, episode=_cold_episode(state), key="sick_leave"
    )


def _cold_rest_score(state: LifeState, config: SimConfig, day_key: str) -> int:
    """今天的「休息分」（§2.3）：睡够了 +1、此刻在养病/睡觉 +1、被关心 +0~cap。"""

    score = 0
    if int(state.sleep_debt_nights) == 0:
        score += 1
    if is_asleep(state.activity) or state.activity == SICK_REST:
        score += 1
    care = int(float(state.care_today.get(day_key, 0.0) or 0.0))
    score += min(max(0, care), max(0, int(config.cold_care_daily_cap)))
    return score


def settle_cold_stage(
    state: LifeState, *, now: float, config: SimConfig, rng: random.Random
) -> None:
    """每天结算一次病程流转（§2.3）；与感冒骰子同一处、同一个 ``rng``。

    判据（都取**确定性的**状态量，不看模型措辞）：

    * **向好**（休息分 ≥ 2）：``onset``→``recovering``（跳过重病）、
      ``worsening``→``recovering``、``recovering``→提前痊愈；
    * **向坏**（没睡够，或体力低于入睡阈值）：``onset``→``worsening``、
      ``recovering``→``worsening``、``worsening`` 原地延长 0.5~1 天（总上限封顶）；
    * 其余：阶段与预期痊愈时刻都不动（病程只是「按天」推进，不是每个 tick 掷骰子）。
    """

    stage = cold_stage(state, now)
    if not stage:
        return
    day_key = _day_key_now(now, config)
    if str(getattr(state, "cold_stage_checked_day", "") or "") == day_key:
        return
    state.cold_stage_checked_day = day_key

    score = _cold_rest_score(state, config, day_key)
    worsening_pressure = int(state.sleep_debt_nights) > 0 or float(state.energy) < float(
        config.sleep_energy_threshold
    )

    if score >= 2:
        if stage == COLD_RECOVERING:
            # 好转期又休息好了 ⇒ 提前痊愈（这一步走完整痊愈流程：免疫期 + 余韵 + 素材）。
            # ⚠ ``cold_until=inf``（永久感冒夹具/整活）**不在这里被治好**：任何流转都不许
            # 把 inf 改成有限值——否则「整活出来的永久感冒」会被一次普通休息悄悄结束。
            if math.isfinite(float(state.cold_until or 0.0)):
                state.cold_until = float(now)
                finish_cold(state, now=now, config=config)
            return
        state.cold_stage = COLD_RECOVERING
        _illness_event(
            state, now=now, config=config,
            episode=_cold_episode(state), key=COLD_RECOVERING,
        )
        _shorten_cold(state, now=now, config=config, rng=rng)
        return

    if not worsening_pressure:
        return

    if stage == COLD_ONSET:
        state.cold_stage = COLD_WORSENING
        _illness_event(
            state, now=now, config=config,
            episode=_cold_episode(state), key=COLD_WORSENING,
        )
        _note_sick_leave(state, now=now, config=config)
    elif stage == COLD_RECOVERING:
        state.cold_stage = COLD_WORSENING
        _illness_event(
            state, now=now, config=config,
            episode=_cold_episode(state), key=COLD_WORSENING,
        )
    _extend_cold(state, now=now, config=config, rng=rng)


# ---------------------------------------------------------------- 关心互动（v1.14.0 §5）

#: 一条关心的情绪收益（克制：真正的收益是「她好得快一点」）。
CARE_EMOTION_GAIN = 0.3
#: 送药命令：情绪收益 / 每次缩短的小时数 / 每场病次数上限 / 会话冷却（小时）。
MEDICINE_EMOTION_GAIN = 0.8
MEDICINE_SHORTEN_HOURS = 4.0
MEDICINE_MAX_PER_COLD = 2
MEDICINE_COOLDOWN_HOURS = 12.0

#: 默认关心句式（§5.1 已拍板：**必须带第二人称或叮嘱/询问结构**，防误命中）。
#:
#: * 第一组＝主语前置：第二人称（你/妳/您/宝/宝子）后 6 字内出现关心词 ——
#:   「你吃药了吗」「宝宝好点了吗」「你多喝热水」；
#: * 第二组＝**叮嘱 / 询问句式**：这些词本身就是对对话对象说的（「记得吃药」
#:   「多喝热水」「好点了吗」「还难受吗」），因此不要求再带主语。
#:
#: 反向用例（必须不命中，`:tests/test_illness.py` 钉着）：
#: 「我昨天也感冒了，吃了药才好」（自述，无第二人称、也无叮嘱词——注意
#: 「吃了药」里没有「吃药」这个子串）、「他感冒一个礼拜了」（第三人称）。
#: ⚠ **已知取舍**：这一组偏严——「多喝热水」这类无主语句子能命中，但
#: 「你要好好的」这种没有关心词的宽泛问候不命中；想更宽松请往
#: ``[health] cold_care_patterns`` 加行（坏正则只告警，不影响消息主链）。
DEFAULT_CARE_PATTERNS: tuple[str, ...] = (
    r"(你|妳|您|宝|宝子).{0,6}(吃药|喝药|喝水|好点|好些|休息|退烧|感冒|嗓子|难受)",
    r"(吃药|喝药|多喝热水|多休息|好好休息|好点了吗|好点了没|好些了吗|退烧了吗|还难受吗)",
)


def parse_care_patterns(lines: object) -> tuple[tuple[Any, ...], list[str]]:
    """解析关心句式正则表；坏行**告警跳过**（照 ``parse_event_line`` 的纪律）。

    配置写崩的最坏结果是「关心功能静默关闭 + 一条 warning」——它挂在消息主链的
    旁路上，绝不能因为一条正则让整条消息链出错。
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

    compiled: list[Any] = []
    warnings: list[str] = []
    for line in candidates:
        raw = str(line or "").strip()
        if not raw:
            continue
        try:
            compiled.append(re.compile(raw))
        except re.error as exc:
            warnings.append(f"关心句式 {raw!r} 不是合法正则（{exc}），已忽略")
    return tuple(compiled), warnings


#: 关心句式匹配的正文上限（字符）。**必须封顶**：这条正则在
#: ``chat.receive.after_process``（BLOCKING 消息钩子）里跑，而句式表是**用户可编辑**的
#: 正则——一条写坏的灾难性回溯正则遇上超长消息会挂住整条消息链（全 bot 收不到消息、
#: 日志还没有异常）。截断到一屏以内的正文，代价是「关心词出现在 500 字之后不生效」。
CARE_TEXT_MAX_CHARS = 500


def care_hit(text: object, patterns: object) -> bool:
    """消息正文是否命中关心句式（任一命中即算）。坏 pattern 静默跳过。"""

    raw = str(text or "")[:CARE_TEXT_MAX_CHARS]
    if not raw:
        return False
    try:
        candidates = list(patterns or ())
    except TypeError:
        return False
    for pattern in candidates:
        try:
            if pattern.search(raw):
                return True
        except Exception:  # noqa: BLE001 —— 坏 pattern 不该影响判定
            continue
    return False


def register_care(
    state: LifeState, *, now: float, session_id: str, config: SimConfig
) -> bool:
    """记一次「被关心」（§5.1）：每会话每日一次，返回是否新记。

    只维护**计数与去重**，不碰情绪——情绪收敛由调用方经 ``append_social_event``
    落库（「情绪怎么写」全插件只有两处实现：``_apply_event`` 与 ``append_social_event``）。
    去重表复用 ``mood_injected``（同为「键 → 时刻」的字符串键表）。
    """

    day_key = _day_key_now(now, config)
    dedup = f"care!{session_id}!{day_key}"
    if dedup in state.mood_injected:
        return False
    state.mood_injected[dedup] = float(now)
    state.care_today[day_key] = float(state.care_today.get(day_key, 0.0) or 0.0) + 1.0
    return True


def medicine_reply(state: LifeState, now: float) -> str:
    """按病程阶段给送药回复文案（纯函数，便于断言阶段措辞）。"""

    stage = cold_stage(state, now)
    return {
        COLD_ONSET: "她收下了药，说「就是有点嗓子痒，你比我妈还紧张」，语气倒是轻快的。",
        COLD_WORSENING: "她迷迷糊糊地收了药，含混地说了声谢谢，又翻过身去睡了。",
        COLD_RECOVERING: "她把药收好，说「已经在好了，不过还是谢啦」，声音还有点哑。",
    }.get(stage, "她把药收下了。")


def take_medicine(
    state: LifeState, *, now: float, session_id: str, config: SimConfig
) -> tuple[bool, str, str]:
    """``/生活 送药`` 的全部规则（§5.2）：返回 ``(是否生效, 结果标签, 给用户的文案)``。

    防刷三件套都在这里：会话冷却、每场病次数上限、累计缩短量封顶
    （``cold_max_days / 2`` 天）。**所有状态改动都在本函数里**，命令侧只负责发送——
    否则「她身体状态的修改规则」会散到插件层，与 ``settle`` 的口径迟早对不上。
    """

    stage = cold_stage(state, now)
    if not stage:
        return False, "not_sick", "她最近身体挺好的，药留着自己吃吧。"

    until = float(state.medicine_cooldown.get(str(session_id), 0.0) or 0.0)
    if math.isfinite(until) and until > float(now):
        minutes = int((until - float(now)) // 60.0) + 1
        return False, "cooldown", f"刚送过啦，{minutes} 分钟后再说——她一次也吃不了那么多。"

    episode = _cold_episode(state)
    if str(state.cold_medicine_key or "") != episode:
        # 换了一场病，计数与累计缩短量归零（旧场次的记录不该压着新场次）
        state.cold_medicine_key = episode
        state.cold_medicine_count = 0
        state.cold_medicine_seconds = 0.0

    if int(state.cold_medicine_count) >= MEDICINE_MAX_PER_COLD:
        return False, "capped", "这场病她已经吃过药了，剩下的得靠好好休息。"

    remaining = max(0.0, float(state.cold_until) - float(now))
    budget = max(
        0.0,
        float(max(1, int(config.cold_max_days))) / 2.0 * 86400.0
        - float(state.cold_medicine_seconds or 0.0),
    )
    shorten = min(MEDICINE_SHORTEN_HOURS * 3600.0, budget, remaining)
    if shorten <= 0.0:
        return False, "capped", "她这场病已经快好了，药就不必了。"

    if math.isfinite(float(state.cold_until)):
        state.cold_until = float(now) + (remaining - shorten)
    state.cold_medicine_seconds = float(state.cold_medicine_seconds or 0.0) + shorten
    state.cold_medicine_count = int(state.cold_medicine_count) + 1
    state.medicine_cooldown[str(session_id)] = float(now) + MEDICINE_COOLDOWN_HOURS * 3600.0
    # 「当日关心计满」：送药 = 一次实打实的照料，直接顶到流转加分上限
    day_key = _day_key_now(now, config)
    state.care_today[day_key] = float(
        max(
            float(state.care_today.get(day_key, 0.0) or 0.0),
            max(0, int(config.cold_care_daily_cap)),
        )
    )
    return True, "ok", medicine_reply(state, now)


def append_social_event(
    state: LifeState, entry: Mapping[str, Any], *, config: SimConfig
) -> LifeState:
    """把一条**社交**经历落到状态上：只改情绪 + 追加经历。

    与 ``_apply_event``（事件库抽中的「她自己碰上的事」）的三点区别都是有意的：

    1. **不动 ``inertia_until``**：别人说话不该冻结她的情绪回归，否则一次群聊就能让
       她的情绪僵住 ``inertia_minutes`` 分钟；
    2. **不产素材**：素材是主动开口那条链路的输入，那条链路有自己的开关；
    3. **不掷骰子、不推进时间**：它由 tick 注入，不是「结算里发生了一件事」。

    情绪增量由 ``life_social`` 按每日额度算好（含「睡眠中不产生情绪增量」），
    这里只做数值收敛并负责写入。

    ⚠ v1.16.3（M6/M7）：**边际效用与关系/孤独系数都在 ``life_social`` 里、额度扣除之前
    就已经乘进 ``entry["emotion"]`` 了**，这里不许再乘一次——同一原因在管线上出现两次
    正是仓库纪律点名要避免的事（「不要双重抑制」）。顺序见 ``life_social.intake_digest``
    的注释：基础值 × 关系系数 × 孤独系数 × 边际效用 → 日额度封顶。
    """

    record = dict(entry)
    delta = _as_float(record.get("emotion"), 0.0) or 0.0
    if delta:
        state.emotion = _clamp(state.emotion + delta, 0.0, config.emotion_max)
    state.recent_events.append(record)
    return state


def fatigue_ramp_extra(minutes: float, config: SimConfig) -> float:
    """连续清醒 ``minutes`` 分钟时的**额外**体力消耗（每小时，≤ 0）。

    v1.16.0（M1）。曲线是「清醒小时 → 每小时消耗」，复用 ``life_factors`` 的
    ``parse_curve_points`` / ``interpolate``（与情绪/体力因子曲线同一套口径：
    端点外不外推）。空曲线 = 关闭 = 与加这一层之前逐位一致。

    只允许**消耗**：曲线若配成正数（熬夜回血）按 0 处理——那与机制意图相反，
    放行等于给「多熬一会儿更精神」开了条后门。
    """

    curve = tuple(getattr(config, "fatigue_ramp_curve", ()) or ())
    if not curve:
        return 0.0
    extra = interpolate(curve, max(0.0, float(minutes)) / 60.0)
    if not math.isfinite(extra) or extra >= 0.0:
        return 0.0
    return float(extra)


def afterglow_weight(age_seconds: float, config: SimConfig) -> float:
    """一条事件对情绪余波的权重（v1.16.0 M8 抽出，v1.16.1 M4b 接上衰减）。

    ``afterglow_decay`` 关着是等权（窗口内 1.0、出窗 0.0，v1.15.0 行为）；开着则按
    年龄线性衰减 ``1 − age/span``（与 ``material_freshness`` 同思路），出窗自然归零、
    不再跳变。``_recompute_afterglow`` 与归因输出**共用本函数**——各写一份必然分叉，
    而「卡片说的构成」与「实际回归用的基线」分叉正是 M8 要消灭的那类问题。
    """

    span = float(config.afterglow_span_hours) * 3600.0
    if span <= 0.0:
        return 0.0
    age = max(0.0, float(age_seconds))
    if age > span:
        return 0.0
    if not bool(getattr(config, "afterglow_decay", False)):
        return 1.0
    return max(0.0, 1.0 - age / span)


def scaled_inertia(delta: float, config: SimConfig) -> float:
    """一次情绪冲击的惯性期（**秒**）：``inertia_minutes × |delta|``，带上下限。

    v1.16.1（M5b）。旧行为是「任何事件一律冻结 ``inertia_minutes``」：「笔没水了」
    （−0.3）和「抽卡出了」（+2.0）冻结同样久，等于把所有事按同一分量对待。
    缩放后小事一晃而过、大事余味更长。

    上限取 ``max(inertia_scale_max_minutes, inertia_minutes)``：用户把基础惯性期配得比
    上限还长时，缩放**不该反而把惯性缩短**（那会让「调大惯性期」这个旋钮失效）。
    """

    base = max(0.0, float(config.inertia_minutes)) * 60.0
    if base <= 0.0 or not bool(getattr(config, "inertia_scale_enabled", False)):
        return base
    percentage = abs(float(delta))
    if not math.isfinite(percentage):
        return base
    lower = max(0.0, float(config.inertia_scale_min_minutes)) * 60.0
    upper = max(base, max(0.0, float(config.inertia_scale_max_minutes)) * 60.0)
    return _clamp(base * percentage, min(lower, upper), upper)


def _recompute_afterglow(state: LifeState, now: float, config: SimConfig) -> None:
    """24 小时情绪余波：把窗口内的事件情绪增量折成基线偏移，钳在 ±cap。"""

    total = 0.0
    for item in state.recent_events:
        if not isinstance(item, dict):
            continue
        at = _as_float(item.get("at"), 0.0) or 0.0
        weight = afterglow_weight(float(now) - at, config)
        if weight > 0.0:
            total += (_as_float(item.get("emotion"), 0.0) or 0.0) * weight
    if not math.isfinite(total):
        total = 0.0
    state.afterglow = _clamp(total * float(config.afterglow_gain), -config.afterglow_cap, config.afterglow_cap)


def diurnal_offset(now: float, config: SimConfig) -> float:
    """日内节律的基线偏移（v1.16.1 M4a）：按**本地时刻**取分段线性曲线。

    幅度刻意压在 ±0.3（对情绪因子的影响 ≤ ±0.06，约 4%——氛围层，不是数值层）。
    曲线为空 = 关闭 = 基线不随时刻浮动（与 v1.15.0 逐位一致）。

    ⚠ 曲线按 **24 小时闭环**处理：最后一个点之后线性回到第一个点的值。没有这一步，
    「23:00 → 0」与「05:00 → −0.3」之间会在午夜出现**台阶**（23:59 是 0、00:01 是
    −0.3），情绪基线会跟着跳一下——那不是节律，是缺陷。
    """

    curve = tuple(getattr(config, "baseline_diurnal_curve", ()) or ())
    if not curve:
        return 0.0
    if curve[-1][0] < 1440.0:
        curve = curve + ((1440.0, float(curve[0][1])),)
    local_now = local_datetime(float(now), config.tz_offset_minutes)
    value = interpolate(curve, float(local_now.hour * 60 + local_now.minute))
    if not math.isfinite(value):
        return 0.0
    return float(value)


def impact_scale(emotion: float, delta: float, config: SimConfig) -> float:
    """一次情绪增量在当前情绪水平上的放大/折扣系数（v1.16.3 M6）。

    方案 P7a：原来的 ``_apply_event`` 直接加减——情绪 9 时 +1 被 clamp 白白浪费、
    情绪 1 时 +0.5 几乎无感。真人的体感不是线性的：

    | 当前情绪 | 正向 delta | 负向 delta |
    |---|---|---|
    | ≤ 2（低谷） | **×1.3**（雪中送炭） | ×0.7（麻木） |
    | 5（基线） | ×1.0 | ×1.0 |
    | ≥ 8（高涨） | ×0.7（快乐 plateau） | **×1.2**（落差） |

    ``emotion_impact_scaling = False``（模块默认）时恒返回 1.0 = 旧行为。
    坏值一律按 1.0（系数坏了不该改变情绪走向）。
    """

    if not bool(getattr(config, "emotion_impact_scaling", False)):
        return 1.0
    value = float(delta)
    if not math.isfinite(value) or value == 0.0:
        return 1.0
    level = float(emotion)
    if not math.isfinite(level):
        return 1.0
    curve = tuple(
        (config.impact_positive_curve if value > 0.0 else config.impact_negative_curve) or ()
    )
    if not curve:
        return 1.0
    factor = interpolate(curve, level)
    if not math.isfinite(factor):
        return 1.0
    return max(0.0, float(factor))


def scaled_emotion_delta(emotion: float, delta: float, config: SimConfig) -> float:
    """``delta`` 经 M6 缩放后的实际增量（事件 / 日期 / 社交三条通道共用）。"""

    return float(delta) * impact_scale(emotion, delta, config)


def fatigue_offset(state: LifeState, config: SimConfig) -> float:
    """体力低下对情绪基线的压低量（v1.16.2 M2a，≤ 0）。

    ``energy`` 低于 ``emotion_fatigue_threshold``（默认 3.0）时基线下移
    ``penalty × (threshold − energy)``：体力 3 → 0，体力 1 → −0.6，体力 0 → −0.9。
    ``emotion_fatigue_penalty = 0`` = 关闭 = 旧行为。

    **为什么可以让身体状态进基线**（方案决议 1，这是这条纪律的唯一开口）：
    体力因子回答「她现在有多少力气说话」，情绪基线偏移回答「她累到什么程度会低落」——
    后者是前者的**成因**，不是把同一个原因在乘法管线上再压一次；而且它走的是
    ``_regress_emotion`` 的基线通道，与余波、节律并列，不进因子表（纪律要求
    「内心维度不进 compute_adjust 的因子表」仍然成立）。
    """

    penalty = max(0.0, float(getattr(config, "emotion_fatigue_penalty", 0.0) or 0.0))
    if penalty <= 0.0:
        return 0.0
    threshold = max(0.0, float(getattr(config, "emotion_fatigue_threshold", 0.0) or 0.0))
    energy = float(state.energy)
    if not math.isfinite(energy) or energy >= threshold:
        return 0.0
    return -penalty * (threshold - max(0.0, energy))


def emotion_baseline_parts(
    state: LifeState, now: float, config: SimConfig
) -> tuple[tuple[tuple[str, float], ...], float]:
    """情绪基线的**分项与合计**（v1.16.0 M8；v1.16.1 M4a 加节律；v1.16.2 M2a 加疲劳）。

    归因输出与 ``_regress_emotion`` 共用同一份口径：各写一份迟早会出现
    「卡片说基线 4.8、回归按 5.1 走」的分叉，而那正是 M8 要消灭的问题。
    项的**顺序**就是卡片上的顺序：基础 → 余波 → 疲劳 → 节律。
    """

    parts: list[tuple[str, float]] = []
    afterglow = float(state.afterglow)
    if afterglow:
        parts.append(("余波", afterglow))
    fatigue = fatigue_offset(state, config)
    if fatigue:
        parts.append(("疲劳", fatigue))
    diurnal = diurnal_offset(now, config)
    if diurnal:
        parts.append(("节律", diurnal))
    total = float(config.baseline_emotion) + sum(value for _, value in parts)
    return tuple(parts), _clamp(total, 0.0, config.emotion_max)


def _regress_emotion(
    state: LifeState, *, config: SimConfig, minutes: float, now: float
) -> None:
    """情绪回归：惰性期内不动；之后靠向基线，睡眠时加倍。

    两种走法（v1.16.1 M5a）：

    * ``recover_ratio_per_tick > 0``：**比例回归**——每 tick 消除与基线差距的固定比例
      （``steps`` 个 tick 后剩下的差距是 ``(1−k)^steps``，绝不过冲），另有每 tick 下限
      ``recover_min_step``。极端情绪初期回落快、逼近基线时变慢，即「爆发快消、余味长」；
    * ``= 0``：**旧线性步长** ``recover_per_tick``（v1.15.0 行为，逐位一致）。

    ``minutes`` 是本次推进的步长；步长可能大于一个 tick（长期离线后的补偿步），
    所以两种走法都按 ``steps`` 折算。``now`` 供基线分项按时刻取值（M4a 日内节律）。
    """

    steps = max(0.0, minutes) / max(1.0, float(config.tick_seconds) / 60.0)
    if steps <= 0.0:
        return
    _, baseline = emotion_baseline_parts(state, now, config)
    delta = baseline - state.emotion
    distance = abs(delta)
    if distance <= 0.0:
        state.emotion = baseline
        return

    ratio = max(0.0, float(getattr(config, "recover_ratio_per_tick", 0.0) or 0.0))
    if ratio > 0.0:
        floor_step = max(0.0, float(getattr(config, "recover_min_step", 0.0) or 0.0))
        if state.activity == SLEEP:
            ratio *= float(config.sleep_recover_multiplier)
            floor_step *= float(config.sleep_recover_multiplier)
        effective = min(1.0, ratio)
        step = distance * (1.0 - (1.0 - effective) ** steps)
        step = max(step, floor_step * steps)
    else:
        step = float(config.recover_per_tick) * steps
        if state.activity == SLEEP:
            step *= float(config.sleep_recover_multiplier)

    if step >= distance:
        state.emotion = baseline
    else:
        state.emotion += step if delta > 0 else -step
    state.emotion = _clamp(state.emotion, 0.0, config.emotion_max)


#: 归因输出（M8）里的来源标签：``recent_events`` 的 ``source`` 字段 → 人话。
_ATTRIBUTION_SOURCES = {"social": "社交", "world": "外面"}
#: 余波/冲击的统计窗口（小时）——与 ``afterglow_span_hours`` 独立：归因要能显示
#: 「窗口里有什么」，就算用户把余波窗口改成 6 小时，冲击榜仍然看 24 小时。
_ATTRIBUTION_WINDOW_HOURS = 24.0
#: 体力流水的回看窗口（分钟）
_ATTRIBUTION_ENERGY_MINUTES = 60.0


def _attribution_source(item: Mapping[str, Any]) -> str:
    """一条经历来自哪里（事件 / 社交 / 外面 / 日期）。"""

    source = str(item.get("source") or "").strip()
    if source in _ATTRIBUTION_SOURCES:
        return _ATTRIBUTION_SOURCES[source]
    return "日期" if str(item.get("label") or "").startswith("日期") else "事件"


def _age_text(seconds: float) -> str:
    """距今多久 → 人话。"""

    minutes = int(max(0.0, float(seconds)) // 60)
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes / 60.0
    if hours < 24.0:
        return f"{hours:.1f} 小时前"
    return f"{hours / 24.0:.1f} 天前"


def _attribution_energy_lines(
    state: LifeState, now: float, config: SimConfig, events_energy: float
) -> list[str]:
    """近 1 小时的体力收支（M8 第 5 项）。

    ⚠ **只按当前活动折算**：状态里没有活动切换历史，所以「一小时前她在做什么」无从
    得知。活动是在这一小时内开始的就明说「切换前的时段没有归因」，不猜——
    归因输出的价值全在「不编」，编出来的数字会让后续标定照着错的依据走。
    """

    window = float(_ATTRIBUTION_ENERGY_MINUTES) * 60.0
    since = _as_float(state.activity_since, 0.0)
    if since is None:
        since = 0.0
    if float(since) <= 0.0:
        # 活动起点未知（旧状态 / 冷启动前）：宁可不归因，也不拿一个假起点去乘
        return []
    start = max(float(now) - window, float(since))
    minutes = max(0.0, (float(now) - start) / 60.0)
    per_hour = dict(config.energy_delta_per_hour)

    lines: list[str] = []
    total = 0.0
    if minutes > 0.0:
        rate = float(per_hour.get(state.activity, -0.5))
        value = rate * minutes / 60.0
        total += value
        lines.append(
            f"　{_activity_label(state.activity)} {rate:+.2f}/h × {minutes:.0f} 分钟"
            f" = {value:+.2f}"
        )
        for side in state.side_activities:
            side_rate = float(per_hour.get(side, -0.5)) * 0.3
            side_value = side_rate * minutes / 60.0
            total += side_value
            lines.append(
                f"　背景·{_activity_label(side)} {side_rate:+.2f}/h × {minutes:.0f} 分钟"
                f" = {side_value:+.2f}（按 0.3 权重折算）"
            )
        ramp = fatigue_ramp_extra(state.continuous_awake_minutes, config)
        if ramp:
            ramp_value = ramp * minutes / 60.0
            total += ramp_value
            lines.append(
                f"　清醒疲劳 {ramp:+.2f}/h × {minutes:.0f} 分钟 = {ramp_value:+.2f}"
                f"（连续清醒 {state.continuous_awake_minutes / 60.0:.1f} 小时）"
            )
        if float(since) > float(now) - window:
            lines.append(
                f"　（当前活动是 {max(0.0, (float(now) - float(since)) / 60.0):.0f} 分钟前"
                "开始的，切换前的时段不归因）"
            )
    if events_energy:
        total += events_energy
        lines.append(f"　事件（体力项合计） = {events_energy:+.2f}")
    if lines:
        lines.append(f"　合计 ≈ {total:+.2f}")
    return lines


def attribution_lines(state: LifeState, now: float, config: SimConfig) -> list[str]:
    """情绪 / 体力的**归因**输出（v1.16.0 M8）：回答「为什么是这个数」。

    纯展示、零行为变更：只读 ``recent_events`` / 活动锚点 / 现有状态字段，**不新增
    持久化字段**，也不碰任何结算路径。方案把 M8 排在第一期的理由就是这个——
    后面每一项数值标定（M1 的疲劳曲线、M4 的节律幅度）都依赖「先能看见」。

    五段：① 情绪基线构成；② 近 24 小时余波来源 top 3；③ 近 24 小时情绪冲击
    top 5（按 |增量|）；④ 惯性剩余 / 距基线距离 / 预计回归；⑤ 近 1 小时体力流水。
    ①④ 永远可算、永远印；②③⑤ 缺数据就省略，全空时补一句「暂无归因数据」——
    不报错、也不给一张空卡（空卡看起来像命令坏了）。

    事件只打标签与来源、不打原话之外的内容——``recent_events`` 里的 ``text`` 本身
    已经过 ``sanitize_text`` 落盘，这里再截一次，不引入新的隐私面。
    """

    now = float(now)
    window = float(_ATTRIBUTION_WINDOW_HOURS) * 3600.0
    gain = float(config.afterglow_gain)

    records: list[dict[str, Any]] = []
    for item in state.recent_events or []:
        if not isinstance(item, dict):
            continue
        at = _as_float(item.get("at"), 0.0) or 0.0
        age = float(now) - at
        if not (0.0 <= age <= window):
            continue
        emotion = _as_float(item.get("emotion"), 0.0) or 0.0
        records.append(
            {
                "label": sanitize_text(item.get("label", ""), max_chars=32) or "（无标签）",
                "source": _attribution_source(item),
                "age": age,
                "emotion": emotion,
                "energy": _as_float(item.get("energy"), 0.0) or 0.0,
                "afterglow": emotion * afterglow_weight(age, config) * gain,
            }
        )
    events_energy = sum(
        record["energy"]
        for record in records
        if record["age"] <= float(_ATTRIBUTION_ENERGY_MINUTES) * 60.0
    )

    parts, baseline = emotion_baseline_parts(state, now, config)
    composition = f"情绪基线：{float(config.baseline_emotion):.2f}（基础）"
    for name, value in parts:
        composition += f" {value:+.2f}（{name}）"
    composition += f" = {baseline:.2f}"

    energy_lines = _attribution_energy_lines(state, now, config, events_energy)

    lines: list[str] = [composition]
    has_data = bool(records) or bool(energy_lines)
    if not has_data:
        # 基线构成与回归状态**永远可算**，所以照印；没有可归因的事件/时段时明说一句，
        # 而不是给一张空卡（空卡看起来像命令坏了）。
        lines.append("暂无归因数据（近 24 小时没有事件，也没有可归因的活动时段）")
    else:
        top_sources = sorted(
            (record for record in records if record["emotion"]),
            key=lambda record: -abs(record["afterglow"]),
        )[:3]
        if top_sources:
            lines.append("余波来源（近 24 小时，对基线偏移贡献 top 3）：")
            for record in top_sources:
                lines.append(
                    f"　{record['afterglow']:+.2f}　{_age_text(record['age'])}"
                    f"　{record['label']}（{record['source']}）"
                )
        else:
            lines.append("余波来源：近 24 小时没有任何带情绪增量的事")

        impacts = sorted(
            (record for record in records if record["emotion"]),
            key=lambda record: -abs(record["emotion"]),
        )[:5]
        if impacts:
            lines.append("情绪冲击（近 24 小时，按 |增量| top 5）：")
            for record in impacts:
                lines.append(
                    f"　{record['emotion']:+.2f}　{_age_text(record['age'])}"
                    f"　{record['label']}（{record['source']}）"
                )

    inertia_left = max(0.0, float(state.inertia_until) - now) / 60.0
    distance = float(state.emotion) - baseline
    gap = abs(distance)
    ratio = max(0.0, float(getattr(config, "recover_ratio_per_tick", 0.0) or 0.0))
    floor_step = max(0.0, float(getattr(config, "recover_min_step", 0.0) or 0.0))
    rate = max(0.0, float(config.recover_per_tick))
    sleep_multiplier = max(0.0, float(config.sleep_recover_multiplier))
    if state.activity == SLEEP:
        ratio *= sleep_multiplier
        floor_step *= sleep_multiplier
        rate *= sleep_multiplier
    tick_minutes = max(1.0, float(config.tick_seconds) / 60.0)
    regression = f"情绪回归：当前 {float(state.emotion):.2f}，距基线 {distance:+.2f}"
    if inertia_left > 0.0:
        # 惯性期里回归是冻结的，所以「还要多久」必须从惯性结束之后起算
        regression += f"；惯性还剩 {inertia_left:.0f} 分钟（这段时间不回归）"

    # 还需多少个 tick 回到基线（``None`` = 不会自己回去）
    ticks: float | None = None
    if ratio > 0.0:
        effective = min(1.0, ratio)
        threshold = max(floor_step, 1e-6)
        if gap <= threshold:
            ticks = 0.0
        elif effective >= 1.0:
            ticks = 1.0
        else:
            ticks = max(0.0, math.log(threshold / gap) / math.log(1.0 - effective))
        regression += f"；按比例回归（每 {tick_minutes:.0f} 分钟消除 {effective:.0%} 的距离）"
    elif rate > 0.0:
        ticks = gap / rate
        regression += f"；按 {rate:.2f}/{tick_minutes:.0f} 分钟回归"
    else:
        regression += "；回归速率为 0（不会自己回到基线）"

    if ticks is not None:
        if ticks <= 0.0:
            regression += "；就在基线上"
        else:
            regression += f"，约需 {inertia_left + ticks * tick_minutes:.0f} 分钟回到基线"
    lines.append(regression)

    if energy_lines:
        lines.append("体力流水（近 1 小时）：")
        lines.extend(energy_lines)

    return lines


def settle(
    state: LifeState,
    *,
    now: float,
    config: SimConfig,
    events: Sequence[LifeEvent] = (),
    rng: random.Random | None = None,
    on_offline_gap: Callable[[int, bool], None] | None = None,
    on_clock_rollback: Callable[[int], None] | None = None,
) -> LifeState:
    """用**当前**活动把 ``last_tick_at → now`` 这段时间结算掉。

    分步推进（每步一个 tick），逐步处理：体力增减、睡眠/清醒记账、生活日边界结算、
    感冒骰子、情绪回归、事件抽取。步数按 ``max_catch_up_hours`` 封顶。

    但**「停机间隙」不结算**：间隔超过 ``offline_gap_minutes`` 时交给
    ``_skip_offline_gap``——那段时间的状态无从判断，逐 tick 补算只会凭空造出
    「清醒 N 小时」并把硬约束的睡眠资格搞坏（真机事故见该函数说明）。
    ``on_offline_gap(gap_minutes, crossed_boundary)`` 是可选的日志回调：
    本模块不依赖 ctx，要日志就得由调用方注入。
    ``on_clock_rollback(gap_minutes)`` 同理：时钟**回拨**超过 1 分钟时回调一次
    （锚点已在本函数内重置，回调只负责留痕）。
    """

    rng = rng or random.Random(0)
    now = float(now)
    last = float(state.last_tick_at)
    if not math.isfinite(last) or last <= 0:
        # v1.8.2 修：NaN/负数锚点在这里自愈。``last_tick_at <= 0`` 对 NaN 恒为假，
        # 以前 NaN 会穿过这道闸让 elapsed 恒为 0 ⇒ 状态每 tick 空转且无日志
        # （settle 是纯模块，来自状态文件的 NaN 已由 from_dict 拦截，这里是第二道闸）
        state.last_tick_at = now
        last = now

    # v1.8.2 修：时钟回拨（NTP 校正 / 虚拟机快照恢复）。回拨后 ``now < last_tick_at``，
    # elapsed 恒为 0 而锚点永远追不回来 ⇒ 生活状态静默冻结（睡眠/体力/事件/日结算
    # 全部停摆）且日志干净。这段「倒流」的时间本就无从结算，把锚点拉回现在即可；
    # 回拨超过 1 分钟时回调一次留痕（阈值之下多为毫秒级 NTP 抖动，不必刷日志）。
    rollback_seconds = last - now
    if rollback_seconds > 0.0:
        state.last_tick_at = now
        if rollback_seconds >= 60.0 and on_clock_rollback is not None:
            on_clock_rollback(int(rollback_seconds // 60.0))
        return _sweep(state, now, config)

    elapsed = max(0.0, now - float(state.last_tick_at))

    # 宽限取「配置值」与「3 个 tick」的较大者：单次 tick 比 tick_seconds 慢一些
    # （模型调用、事件循环卡顿）是正常的，不能误判成停机。
    offline_gap_minutes = max(0.0, float(config.offline_gap_minutes))
    grace_seconds = max(
        offline_gap_minutes * 60.0,
        3.0 * max(60, int(config.tick_seconds)),
    )
    if offline_gap_minutes > 0 and elapsed > grace_seconds:
        return _skip_offline_gap(
            state,
            now=now,
            elapsed=elapsed,
            config=config,
            on_offline_gap=on_offline_gap,
        )

    if elapsed <= 0.0:
        return _sweep(state, now, config)

    tick_seconds = max(60, int(config.tick_seconds))
    max_steps = int(max(1.0, float(config.max_catch_up_hours) * 3600.0 / tick_seconds))
    steps = min(max_steps, max(1, int(round(elapsed / tick_seconds))))
    step_seconds = elapsed / steps
    step_minutes = step_seconds / 60.0
    delta_per_hour = config.energy_delta_per_hour

    cursor = now - elapsed
    local_cursor = local_datetime(cursor, config.tz_offset_minutes)

    # 若当前是睡眠/小睡且没记开始时间，补记（例如热重载或旧状态）
    if is_asleep(state.activity) and not state.sleep_started_at:
        state.sleep_started_at = cursor

    for _ in range(steps):
        cursor += step_seconds
        local_cursor = local_datetime(cursor, config.tz_offset_minutes)

        # --- 体力 ---
        # v1.12.1（side）：主活动 1.0 + 每个背景活动 0.3 加权——「顺便」的事
        # 只占小头；背景项不经 enforce，白名单已在入口滤掉睡觉等独占活动
        asleep = is_asleep(state.activity)
        drain = float(delta_per_hour.get(state.activity, -0.5))
        if state.side_activities:
            drain += 0.3 * sum(
                float(delta_per_hour.get(item, -0.5)) for item in state.side_activities
            )
        # v1.16.0（M1）：清醒疲劳——连续清醒越久，**额外**基础消耗越大。用本步
        # 开始前的连续清醒时长（这一步还没被计入），且**睡着时不适用**：她是躺着
        # 回血，不是躺着受累（否则熬到 22 小时才睡下的人会被扣掉大半恢复量）。
        if not asleep:
            drain += fatigue_ramp_extra(state.continuous_awake_minutes, config)
        # v1.16.2（M2b）：低体力时**消耗**被放大（越累越容易更累的软恶性循环，把她
        # 推向休息）。只放大负值——恢复项（睡觉 +1.20、吃饭 +0.20）照旧，不然
        # 「体力越低回血越慢」会变成把她焊在床上的正反馈（决议 4 明确不做那个方向）。
        if drain < 0.0 and float(state.energy) < float(config.low_energy_threshold):
            drain *= max(1.0, float(config.low_energy_drain_multiplier))
        state.energy = _clamp(
            state.energy + drain * (step_minutes / 60.0), 0.0, float(state.energy_cap)
        )

        # --- 睡眠 / 清醒记账（v1.15.0：睡眠与小睡走同一个谓词）---
        minutes = int(round(step_minutes))
        if asleep:
            state.sleep_minutes_today += minutes
        else:
            state.awake_minutes_today += minutes
            # v1.16.0（M1）：连续清醒只加不清零（清零在一觉结束时）。生活日边界
            # 不清它——「从早上熬到次日凌晨」本来就是一整段连续清醒。
            state.continuous_awake_minutes = min(
                MAX_CONTINUOUS_AWAKE_MINUTES,
                int(state.continuous_awake_minutes) + minutes,
            )

        # --- 生活日边界结算 ---
        current_day = day_key_of(local_cursor, config.day_boundary_hour)
        if state.day_key and current_day != state.day_key:
            _settle_day(state, config)
            state.day_key = current_day
        elif not state.day_key:
            state.day_key = current_day

        # --- 日期规则（生日 / 自定义节日），同一天只触发一次 ---
        _fire_date_rules(state, local_dt=local_cursor, now=cursor, config=config)

        # --- 感冒骰子（每天一次）---
        if local_cursor.hour >= int(config.cold_check_hour) and state.cold_checked_day != current_day:
            state.cold_checked_day = current_day
            # v1.14.0：免疫期内不掷（修「病好第二天无缝再病」）。
            if not is_cold(state, cursor) and not _immunity_active(state, cursor):
                risk = float(config.cold_base_risk) + float(config.cold_sleep_debt_risk) * max(
                    0, int(state.sleep_debt_nights)
                )
                if float(state.energy) < float(config.sleep_energy_threshold):
                    risk += float(config.cold_sleep_debt_risk)
                # 季节系数（可选，默认空表 = 不启用）：生病不再是均匀白噪声。
                season_table = dict(getattr(config, "cold_season_factors", {}) or {})
                if season_table:
                    month_factor = _as_float(
                        season_table.get(str(int(local_cursor.month))), 1.0
                    )
                    risk *= float(month_factor if month_factor is not None else 1.0)
                if rng.random() < _clamp(risk, 0.0, 0.95):
                    days = rng.randint(int(config.cold_min_days), max(int(config.cold_min_days), int(config.cold_max_days)))
                    start_cold(state, now=cursor, config=config, days=days)

        # --- 病程流转（每天一次，与骰子同一处）---
        settle_cold_stage(state, now=cursor, config=config, rng=rng)
        # 到期痊愈（自然走完 cold_until）：补一次完整流程（免疫期 + 余韵 + 素材）
        _finish_cold_if_expired(state, now=cursor, config=config)

        # --- 内心维度的确定性产出（v1.16.2 M3a）：压力持续偏高就崩一次 ---
        settle_stress_breakdown(state, now=cursor, day_key=current_day, config=config)

        # --- 情绪：余波 + 回归 ---
        _recompute_afterglow(state, cursor, config)
        if cursor >= float(state.inertia_until):
            _regress_emotion(state, config=config, minutes=step_minutes, now=cursor)

        # --- 饱腹结算（v1.9.1）：清醒才饿，睡觉（含小睡）不消耗 ---
        if config.physio_enabled:
            state.satiety = settle_satiety(
                state.satiety,
                minutes=step_minutes,
                asleep=is_asleep(state.activity),
                decay_per_hour=config.satiety_decay_per_hour,
            )

        # --- 事件抽取（睡眠中不抽；v1.14.0 带病程阶段过滤）---
        if not is_asleep(state.activity):
            fired = pick_event(
                events,
                state.activity,
                rng,
                probability=config.fire_probability,
                stage=cold_stage(state, cursor),
            )
            if fired is not None:
                _apply_event(state, fired, now=cursor, config=config, rng=rng)

        # --- 夜间易醒（v1.15.0 PR-R2）：排在事件抽取**之后**——
        # 她刚醒的这一步不该再抽一件白天的事。 ---
        _roll_night_waking(state, now=cursor, config=config, rng=rng)

    state.last_tick_at = now
    state.energy = _clamp(state.energy, 0.0, float(state.energy_cap))
    _recompute_afterglow(state, now, config)
    # 收尾补一次到期痊愈：步长被 max_catch_up_hours 截断时，最后一步可能没走到
    # cold_until，但墙钟已经过了——不补的话她会永远挂着「生病中」的阶段字段。
    _finish_cold_if_expired(state, now=now, config=config)
    return _sweep(state, now, config)


def night_break_active(state: LifeState, *, now: float) -> bool:
    """是否处在「夜醒后的同一夜」窗口里（v1.15.0 PR-R2）。

    判据只有「距上次夜醒不超过 ``NIGHT_BREAK_RESUME_SECONDS``」一条：
    宽限窗不能当判据（它可能只有一个 tick 那么短），而硬约束把她送回床发生在
    宽限窗过期后的下一个 tick。
    """

    last = float(getattr(state, "last_night_waking_at", 0.0) or 0.0)
    return last > 0.0 and (float(now) - last) <= float(NIGHT_BREAK_RESUME_SECONDS)


def _mark_rested_if_satisfied(
    state: LifeState, *, now: float, minutes: int, config: SimConfig
) -> None:
    """睡满「最短睡眠目标」醒来 ⇒ 记「本轮睡眠时段到此为止」（v1.15.0 PR-S1 配套）。

    只在**仍在睡眠窗口内**时记：窗口外醒来（白天累趴了补觉）没有「再睡一觉」的问题，
    记了反而会把当晚的窗口驱动入睡一起压掉（``rested_until`` 会指向下一个窗口末端）。

    目标为 0（旧行为）时**不记**：那种配置下没有「睡够」的概念，行为与 v1.14 一致。
    """

    target = max(0.0, float(getattr(config, "energy_full_wake_min_hours", 0.0) or 0.0)) * 60.0
    if target <= 0.0 or int(minutes) < target:
        return
    local = local_datetime(now, config.tz_offset_minutes)
    now_minutes = local.hour * 60 + local.minute
    if not in_window(now_minutes, config.sleep_window):
        return
    exit_minutes = minutes_until_window_exit(now_minutes, config.sleep_window)
    if exit_minutes > 0:
        state.rested_until = float(now) + exit_minutes * 60.0


def _skip_offline_gap(
    state: LifeState,
    *,
    now: float,
    elapsed: float,
    config: SimConfig,
    on_offline_gap: Callable[[int, bool], None] | None = None,
) -> LifeState:
    """停机间隙：只推进生活日，**不记账**。

    为什么不逐 tick 补算（v1.3.0 的真机事故，2026-10-02）：

    * 补算会把停机时间整段算成**当前活动的时长**。真机上她停摆前在 ``game``，
      一次 10.6 小时的补算把 ``awake_minutes_today`` 从 216 顶到 859，于是
      「每日清醒下限」被凭空打开——约束的松紧由「插件停过多久」决定，这是错的；
    * 补算还会在跨越生活日边界时触发 ``_settle_day``，把两个计数器**在非边界
      时刻清零**（真机是 10-01 17:4x），反过来把该睡的人锁死最多
      ``min_awake_hours_per_day`` 小时。

    正确做法：这段时间的状态无从判断，所以

    1. 只把 ``day_key`` 推进到当前生活日；
    2. 若期间跨过边界，按新的一天重置两个计数器（旧的一天确实过去了），
       但**不判熬夜**（不知道她睡没睡，不能凭空记一笔）；
    3. 不记清醒/睡眠分钟、不扣体力、不抽事件、不掷感冒骰子；
    4. 把「模型多久没交作业」的计时器（``llm_last_success_at``）一起往前推——
       停机不是模型的错，不该算进 ``reseed_after_hours``；
    5. 通过 ``on_offline_gap`` 回调让调用方留日志（本模块不依赖 ctx）。
    """

    local_now = local_datetime(now, config.tz_offset_minutes)
    current_day = day_key_of(local_now, config.day_boundary_hour)
    crossed = bool(state.day_key) and state.day_key != current_day
    if crossed:
        state.sleep_minutes_today = 0
        state.awake_minutes_today = 0
        # 不判新的一夜，但按**已知的**熬夜天数把体力上限重新夹一次，
        # 免得停机期间一直挂着旧的（可能更严的）上限。
        _refresh_energy_cap(state, config)
    state.day_key = current_day
    state.last_tick_at = now
    # 停机不是模型的错：把「多久没交作业」的计时器一起往前推，差分保持不变。
    # 否则长停机后重启的第一轮就会撞上 reseed_after_hours，把她的活动改写成
    # 「当前时刻的时段表种子」（见 should_reseed 的说明）。
    if float(state.llm_last_success_at) > 0:
        state.llm_last_success_at = min(
            now, float(state.llm_last_success_at) + max(0.0, float(elapsed))
        )
    if is_asleep(state.activity) and not state.sleep_started_at:
        # 睡眠锚点缺失时补「现在」，别让 minutes_in_sleep 变成一个假的大值
        state.sleep_started_at = now
    # 停机间隙不掷骰子、不结算流转（那段时间的状态无从判断），但**到期该痊愈的
    # 必须痊愈**：否则她会顶着「生病中」的阶段字段一直躺着，而 cold_until 早过了。
    _finish_cold_if_expired(state, now=now, config=config)
    if on_offline_gap is not None:
        on_offline_gap(int(max(0.0, elapsed) // 60), crossed)
    return _sweep(state, now, config)


# ---------------------------------------------------------------- 熬夜判定

#: 「睡够没有」看最近这么久（滑动窗口）。取 24 小时：既不依赖生活日边界，
#: 又能把「拆成两段睡」自然合起来算。
SLEEP_DEBT_WINDOW_HOURS = 24.0
#: 账本最多留几段睡眠（每段至少 min_sleep_minutes，24 小时内正常只有 1~2 段）。
SLEEP_LEDGER_KEEP = 8


#: 夜间易醒的去重键前缀（键 = ``night_waking:<生活日>``，每夜至多一次）
NIGHT_WAKING_DEDUP = "night_waking"


def _roll_night_waking(
    state: LifeState, *, now: float, config: SimConfig, rng: random.Random
) -> None:
    """夜间易醒（v1.15.0 PR-R2）：睡到中段偶尔睁一下眼，过会儿自己又睡回去。

    **为什么需要它**：原来一觉就是一条直线——睡下、结算、醒来，中间什么都不会发生。
    真人会在半夜醒一次（翻身、喝水、看一眼手机），这也是「睡醒不一定清爽」的来源。

    判据（全部满足）：开关开、正在**睡整觉**（小睡不掷）、本生活日还没醒过、
    已睡满 ``min_sleep_minutes``、体力没回满、掷骰命中。

    **落地方式（关键）**：切到 ``daze`` 并开一个 ``wake_grace_until`` 宽限窗。
    不能只切活动——tick 的顺序是 ``settle`` → ``enforce(None)``，而 enforce 见她在
    睡眠窗口内又够格入睡，会**在同一个 tick 里**把她送回去，等于什么都没发生；
    宽限窗让 enforce 放行这一小段。宽限时长复用 ``wake_daze_minutes``
    （0 = 只记事件、不切活动，因为她没有「醒来待一会儿」的窗口）。

    **睡账不丢**：夜醒**不重置入睡锚点**（``sleep_started_at`` 保留）——这一夜是一个
    整体，中间的十几分钟只是「醒了一下又睡回去」；重睡时 ``apply_activity`` 用
    「距上次夜醒不超过 ``NIGHT_BREAK_RESUME_SECONDS``」认出「这是在续上一觉」，
    因而不再重置锚点。否则新一觉的「最短睡眠目标」从零重算
    （03:00 睡、06:00 夜醒、再睡满 6.5 小时 = 白多睡一个多小时）。
    """

    if not bool(getattr(config, "night_waking_enabled", False)):
        return
    if state.activity != SLEEP:
        # 只对**整觉**掷骰：小睡本来就短，再去掉中间一段就没有意义了
        return
    if float(state.wake_grace_until or 0.0) > float(now):
        return  # 已经在醒着的那一段里，别重复触发
    slept = _minutes_since(now, state.sleep_started_at)
    if slept < max(1, int(config.min_sleep_minutes)):
        return  # 睡太短：还在「刚躺下」的保护里
    if float(state.energy) >= float(state.energy_cap):
        return  # 体力已满：她本来就该醒了，不需要「半夜醒」
    day_key = _day_key_now(now, config)
    dedup = f"{NIGHT_WAKING_DEDUP}:{day_key}"
    if dedup in state.motive_seen:
        return  # 每生活日至多一次
    probability = _as_float(getattr(config, "night_waking_probability", 0.0), 0.0) or 0.0
    if probability <= 0.0 or rng.random() >= _clamp(probability, 0.0, 1.0):
        return
    state.motive_seen[dedup] = float(now)
    state.last_night_waking_at = float(now)
    state.recent_events.append(
        {
            "at": float(now),
            "label": "夜醒",
            "kind": "night_waking",
            "activity": SLEEP,
            "text": "半夜醒了一下",
            "emotion": 0.0,
            "energy": 0.0,
        }
    )
    grace_seconds = max(0, int(getattr(config, "wake_daze_minutes", 0) or 0)) * 60.0
    if grace_seconds <= 0.0:
        # 没有宽限窗 ⇒ 不切活动（切了会被同一个 tick 的硬约束立刻送回床，
        # 状态卡上只会闪一下）。事件已经记下，叙事层照样有收获。
        return
    # 注意：**不**在这里记睡眠段、**不**清入睡锚点——这一夜还没结束
    # （清锚点会让「最短睡眠目标」从零重算，见函数说明；整段会在**真正醒来**时
    # 由 ``apply_activity`` 一次记进滚动账本并判熬夜）
    state.activity = DAZE
    state.activity_since = float(now)
    state.activity_source = SOURCE_ENFORCED
    state.activity_note = "半夜醒了一次"
    state.scene = "半夜醒了一下，翻个身"
    state.wake_grace_until = float(now) + grace_seconds


def record_sleep_episode(state: LifeState, *, now: float, minutes: int) -> None:
    """把一段**刚结束**的睡眠记进滚动账本，并清掉与窗口再无交集的旧条目。

    v1.16.0（M1）：一觉结束 = **连续清醒重新起算**（``continuous_awake_minutes``
    归零）。放在这里是因为「睡了一觉」正是这个计数器的语义边界；调用方
    （``apply_activity``）另有一处兜底，用于量不到这段睡眠时长的旧状态。
    """

    minutes = int(max(0, int(minutes)))
    now = float(now)
    state.continuous_awake_minutes = 0
    if minutes > 0:
        state.sleep_ledger.append(
            {"start": now - minutes * 60.0, "end": now, "minutes": minutes}
        )
    cutoff = now - SLEEP_DEBT_WINDOW_HOURS * 3600.0
    # 判据是「还有没有交集」，不是「结束时间在不在窗口内」：见 sleep_in_window
    state.sleep_ledger = [
        item
        for item in state.sleep_ledger
        if float(item.get("end", 0.0) or 0.0) > cutoff
    ][-SLEEP_LEDGER_KEEP:]


def sleep_in_window(
    state: LifeState, *, now: float, hours: float = SLEEP_DEBT_WINDOW_HOURS
) -> int:
    """窗口内**实际发生**的睡眠分钟数（每段按与窗口的交集计）。

    ⚠ 不能写成「结束时间落在窗口内的段相加」：相邻两夜只隔约 24 小时，那样会把
    「上一夜」整段吃进来，于是每天只睡 4 小时的人也会显示 8 小时、熬夜永远攒不出来
    （探针实拍：错误写法给出 1/0/0，正确应为 1/2/3）。所以这里按区间交集求和，
    ``start`` / ``end`` 是权威字段，``minutes`` 只是给人看。
    """

    now = float(now)
    lower = now - float(hours) * 3600.0
    total_seconds = 0.0
    for item in state.sleep_ledger:
        end = float(item.get("end", 0.0) or 0.0)
        start = float(item.get("start", end) or end)
        overlap = min(end, now) - max(start, lower)
        if overlap > 0:
            total_seconds += overlap
    return int(round(total_seconds / 60.0))


def _refresh_energy_cap(state: LifeState, config: SimConfig) -> None:
    """按**当前**熬夜天数刷新体力上限，并把体力夹进上限。"""

    if int(state.sleep_debt_nights) >= int(config.sleep_debt_cap_nights):
        state.energy_cap = float(config.sleep_deprived_energy_cap)
    else:
        state.energy_cap = float(config.energy_max)
    state.energy = min(float(state.energy), float(state.energy_cap))


def grade_sleep_debt(
    state: LifeState,
    *,
    now: float,
    config: SimConfig,
    extra_minutes: int = 0,
) -> int:
    """在**睡醒时**判这一觉够不够，返回变化后的熬夜天数。

    判据：最近 24 小时累计睡眠 ≥ ``sleep_debt_threshold_minutes``。为什么不再用
    「本生活日累计」（v1.3.2 修正）：

    * **边界会劈开一段睡眠**。生活日边界（默认 12:00）落在一段睡眠中间时，两个
      生活日各拿到一半，边界那次结算就会把「没睡完的觉」当成一整夜——真机
      2026-10-02：她 08:00 睡下、睡到 16:00（8 小时，明明睡够了），但 12:00 那次
      结算只看到 08:00→12:00 这 4 小时，于是判了「连熬 1 天」；
    * **拆成两段睡要看边界脸色**。3 小时 + 3 小时是否合起来算，在「本日合计」下
      取决于两段有没有跨过 12:00；滑动窗口下必然合起来算。

    ``extra_minutes`` 供「睡眠锚点不可信」的兜底路径使用（见 ``apply_activity``）：
    那时当日累计是唯一能用的证据，而它不能同时进账本，否则会重复计。

    v1.15.0（PR-S1）**滞回**：睡够的这次只清 ``sleep_debt_recovery_step`` 晚债
    （默认 1），不再一晚直接归零。旧行为（一晚清零）在默认参数下会与
    「体力满即醒」的正反馈叠加——连熬三晚把上限压到 8.5 后，睡够一晚就洗白、
    第二天又睡不到阈值。``sleep_debt_recovery_step = 0`` 可回到旧行为。
    """

    total = sleep_in_window(state, now=now) + max(0, int(extra_minutes))
    if total >= int(config.sleep_debt_threshold_minutes):
        step = max(0, int(getattr(config, "sleep_debt_recovery_step", 0) or 0))
        if step <= 0:
            state.sleep_debt_nights = 0  # 旧行为：一晚清零
        else:
            state.sleep_debt_nights = max(0, int(state.sleep_debt_nights) - step)
    else:
        state.sleep_debt_nights = min(99, int(state.sleep_debt_nights) + 1)
    _refresh_energy_cap(state, config)
    return state.sleep_debt_nights


def _settle_day(state: LifeState, config: SimConfig) -> None:
    """生活日边界：清零当日计数器，并按已知的熬夜天数刷新体力上限。

    ⚠ 熬夜判定**不在这里**了：它已改成「睡醒时按最近 24 小时滑动窗口判」
    （``grade_sleep_debt``），所以边界既不需要、也不应该给一段没睡完的觉打分。
    """

    state.sleep_minutes_today = 0
    state.awake_minutes_today = 0
    # v1.9.1（physio）：进餐次数按生活日清零
    state.meal_count_today = 0
    _refresh_energy_cap(state, config)


def _fire_date_rules(
    state: LifeState,
    *,
    local_dt: datetime,
    now: float,
    config: SimConfig,
) -> None:
    """生日 / 自定义节日：当天触发一次情绪体力变化与素材。"""

    for rule in active_festivals(config, local_dt):
        key = rule.key(local_dt)
        if key in state.fired_date_keys:
            continue
        state.fired_date_keys.append(key)
        # v1.16.3（M6）：日期情绪也走同一个边际缩放（方案 §3 M6：事件/社交/日期三条通道）
        applied = scaled_emotion_delta(float(state.emotion), float(rule.emotion), config)
        state.emotion = _clamp(state.emotion + applied, 0.0, config.emotion_max)
        state.energy = _clamp(state.energy + rule.energy, 0.0, float(state.energy_cap))
        if applied:
            # v1.16.1（M5b）：与 ``_apply_event`` 走**同一个** helper——两处各写一份
            # 迟早会漂成「事件按冲击缩放、日期仍固定 40 分钟」
            state.inertia_until = max(
                state.inertia_until, float(now) + scaled_inertia(applied, config)
            )
        text = sanitize_text(rule.material, max_chars=80)
        if text:
            state.materials.append(
                {
                    "label": f"日期:{rule.name}",
                    "text": text,
                    "weight": float(rule.weight),
                    "created_at": float(now),
                    "expires_at": float(now) + max(1.0, float(config.material_ttl_hours)) * 3600.0,
                }
            )


# ---------------------------------------------------------------- 活动落定


def enforce_facts(
    state: LifeState,
    *,
    now: float,
    config: SimConfig,
    rest_day: bool | None = None,
    workday_override: bool | None = None,
    day_name: str = "",
    day_key: str = "",
) -> ActivityFacts:
    """状态 → ``enforce`` 需要的事实（含本地时刻与班表）。

    单独抽出来是给「这一轮问模型有没有意义」（``pointless_ask_reason``）复用：
    两处口径必须一致，否则跳过判断会与实际裁定对不上。

    ``rest_day``（v1.15.0 PR-R4）：带日历的判定归 plugin 层（``life_calendar``
    不属于本模块），那里可以显式覆盖；不给时按班表推（班表休息日 ⇒ 休息日）。

    ``workday_override`` / ``day_name``（v1.17.0，PR-CAL-1）：日历对「今天算不算
    工作日」的覆盖，同样由 plugin 层算好透传（本模块不认识日历）。**必须与
    提示词侧用同一份**——否则会出现「提示词说放假、强制层还在岗」的 P0 缺陷
    （见改进方案 §2 P0-1）。

    ``day_key``（v1.17.0，PR-SCH-2）：生活日标识——给了它才会应用班表的按日微扰
    与加班日；同样必须与提示词侧同源。
    """

    local_now = local_datetime(now, config.tz_offset_minutes)
    # v1.11.1（interrupt）：打断窗口内把「正在回消息」这条事实喂给强制层，它据此
    # 保住 CHATTING（见 life_activity.enforce 的 2a-0 分支）。
    #
    # ⚠ 这里**刻意不 import life_interrupt.in_interrupt_window**：那个模块 import
    # life_activity，会与本模块构成加载顺序上的环。判据只有「interrupt_until 未过」
    # 一行，在两处各写一遍而用同一批字段；真要对拍由 tests/test_interrupt.py 里
    # 「两处判据必须给出相同答案」的用例守着。
    until = float(state.interrupt_until or 0.0)
    in_interrupt = until == until and until > 0.0 and float(now) < until
    schedule = schedule_facts(
        local_now,
        config.schedule,
        workday_override=workday_override,
        day_name=day_name,
        day_key=day_key,
    )
    if rest_day is None:
        computed_rest = bool(config.schedule.enabled and not schedule.is_workday)
    else:
        computed_rest = bool(rest_day)
    return replace(
        activity_facts(state, now),
        now_minutes=local_now.hour * 60 + local_now.minute,
        schedule=schedule,
        in_interrupt=in_interrupt,
        # v1.15.0（PR-R3/PR-R2）：赖床/夜醒宽限窗从状态直接算（纯模块能拿到）
        wake_grace=float(state.wake_grace_until or 0.0) > float(now),
        rest_day=computed_rest,
        # v1.15.0（PR-S1 配套）：本轮窗口已睡够 ⇒ 不许在同一个窗口里再睡一觉
        rested=float(state.rested_until or 0.0) > float(now),
        # v1.15.0（PR-R2）：夜醒后的同一夜里可以无阻睡回去（不受清醒下限拦阻）
        night_break=night_break_active(state, now=now),
    )


def pointless_ask_reason(
    state: LifeState,
    *,
    now: float,
    config: SimConfig,
    rest_day: bool | None = None,
    workday_override: bool | None = None,
    day_name: str = "",
    day_key: str = "",
) -> str:
    """这一轮问模型是不是注定白问（空串 = 该问）。判据见 ``request_is_pointless``。

    ``workday_override`` / ``day_name``（v1.17.0，PR-CAL-1）与 ``day_key``
    （PR-SCH-2）：与 ``enforce_facts`` 同一份日历覆盖与同一个生活日——
    **这里漏传会让「跳过判定」与「实际裁定」在节假日/加班日分叉**，
    正是本函数要防的那类不一致。
    """

    return request_is_pointless(
        enforce_facts(
            state,
            now=now,
            config=config,
            rest_day=rest_day,
            workday_override=workday_override,
            day_name=day_name,
            day_key=day_key,
        ),
        build_enforce_policy(config),
    )


def mark_ask_skipped(state: LifeState, *, reason: str) -> None:
    """记下「这一轮故意没问模型」。

    单独一类来源，是为了**别让状态卡把它显示成「模型坏了」**——排查时这两种情况的
    含义正好相反（一个是模型故障，一个是插件主动省钱）。
    """

    state.activity_source = SOURCE_SKIPPED
    state.activity_note = f"本轮未问模型：{reason}"


def enforce_and_apply(
    state: LifeState,
    *,
    now: float,
    config: SimConfig,
    decision: ActivityDecision | None,
) -> LifeState:
    """跑强制层并把结果落进状态（活动切换时重置停留计时）。"""

    facts = enforce_facts(state, now=now, config=config)
    resolved = enforce(facts, decision, build_enforce_policy(config))
    return apply_activity(state, resolved, now=now, config=config)


def apply_activity(
    state: LifeState,
    decision: ActivityDecision,
    *,
    now: float,
    config: SimConfig,
) -> LifeState:
    """把裁定结果写进状态；活动真的变了才重置停留计时与睡眠起点。

    ⚠ 这里有**两本不能混的账**：

    * ``sleep_minutes_today`` **只由 ``settle`` 逐步累加**，这里绝不重复计入——
      否则每次醒来都会把同一段睡眠记两遍，当日统计失真；
    * 而醒来时要把这一觉记进 ``sleep_ledger`` 并结算熬夜（``grade_sleep_debt``），
      用的是 ``sleep_started_at → now`` 的**整段时长**。必须用整段而不是当日计数器：
      计数器会在 12:00 边界被清零、把跨边界的一觉劈成两半（真机 2026-10-02 的事故）；
      整段时长也包含插件停机的那段时间——那段时间她确实在睡，算进去是对的。
    """

    now = float(now)
    previous = state.activity
    changed = decision.activity != previous

    if changed:
        if is_asleep(decision.activity):
            # 夜醒中断后接着睡（v1.15.0，PR-R2）：**夜醒后一小时内**再睡下 ⇒ 这是在续
            # 上一觉，保留原来的入睡锚点，这一夜算一整体（否则目标从零重算，
            # 一夜被拆成两段各自睡满目标 = 白多睡一个多小时）。
            # 判据用「距上次夜醒不超过 1 小时」而不是宽限窗：宽限窗可能只有几分钟，
            # 而硬约束把她送回床是在宽限窗过期后的下一个 tick，可能落在窗外。
            resuming = night_break_active(state, now=now) and _minutes_since(
                now, state.sleep_started_at
            ) > 0
            if not resuming:
                state.sleep_started_at = now
        elif is_asleep(previous):
            # 先量这一觉再清锚点。量不到（旧状态没有 sleep_started_at）时退回当日累计：
            # 那是唯一能用的证据，而且它不能同时进账本，否则会重复计。
            minutes = _minutes_since(now, state.sleep_started_at)
            state.sleep_started_at = 0.0
            # v1.16.0（M1）：醒了 ⇒ 连续清醒从零起算。这里必须也做一次——
            # 下面 ``minutes <= 0`` 的那条路不会调用 ``record_sleep_episode``。
            state.continuous_awake_minutes = 0
            if minutes > 0:
                record_sleep_episode(state, now=now, minutes=minutes)
                grade_sleep_debt(state, now=now, config=config)
            elif int(state.sleep_minutes_today) > 0:
                grade_sleep_debt(
                    state,
                    now=now,
                    config=config,
                    extra_minutes=int(state.sleep_minutes_today),
                )
            # v1.15.0（PR-S1 配套）：这一觉睡够了「最短睡眠目标」⇒ 本轮睡眠时段到此
            # 为止（``rested_until`` = 本轮窗口结束）。没有这一步，醒来十分钟后
            # 「窗口内 + 体力差一点点满」就会把她送回床，再睡出第二个整觉。
            _mark_rested_if_satisfied(state, now=now, minutes=minutes, config=config)
        state.activity_since = now

    state.activity = decision.activity
    state.activity_source = decision.source
    state.activity_note = decision.note
    if decision.scene:
        state.scene = decision.scene

    # ---- 背景活动（v1.12.1，方案五）----
    # 独占型活动（睡觉/养病/睡前等）不存在「顺便」的形态：强制层把她切过去时
    # 必须清空背景（v1.13.1 / R5——否则会出现「当前：睡觉 / 背景：看番」，
    # 体力按睡觉 + 0.3×看番结算，与 SIDE_ACTIVITIES 白名单自述矛盾）。
    if decision.activity not in SIDE_ACTIVITIES:
        state.side_activities = []
    elif decision.source == SOURCE_LLM:
        # 只由**模型提议**驱动：LLM decision 总是覆盖（模型没提 = 清空，防止背景
        # 活动永久残留）；routine/physio/enforced 等确定性来源不带 side ⇒ 保留现状
        # （背景是上一轮的决策产物，确定性层不替模型清）。打断清空在 life_interrupt。
        state.side_activities = list(normalize_side(decision.side, main=decision.activity))
        # scene 拼接「吃饭，顺便看番」——只展示第一项，避免句子变成清单
        if state.side_activities:
            side_label = ACTIVITY_LABELS.get(state.side_activities[0], state.side_activities[0])
            base_scene = sanitize_text(state.scene, max_chars=40)
            state.scene = f"{base_scene or '手头的事'}，顺便{side_label}"[:60]
    elif not set(state.side_activities) <= SIDE_ACTIVITIES:
        # 主活动不是 LLM 决定、但旧背景里有非法项（如升级后白名单收紧）→ 只过滤不清空
        state.side_activities = [
            item for item in state.side_activities if item in SIDE_ACTIVITIES
        ]
    return state


def mark_llm_success(state: LifeState, *, now: float, raw: str) -> LifeState:
    """记录一次成功的模型决策（供 ``reseed_after_hours`` 判定）。"""

    state.llm_last_success_at = float(now)
    state.llm_fail_streak = 0
    state.llm_last_raw = sanitize_text(raw, max_chars=400)
    return state


def mark_llm_failure(state: LifeState, *, now: float, raw: str, config_limit: int) -> LifeState:
    """记录一次失败；达到连续失败上限时由调用方决定是否进冷却。"""

    state.llm_fail_streak = int(state.llm_fail_streak) + 1
    del config_limit
    state.llm_last_raw = sanitize_text(raw, max_chars=400)
    return state


def should_reseed(state: LifeState, *, now: float, hours: float) -> bool:
    """连续多久没有一次成功的模型决策 → 用时段表重新取种子。

    这是「失败即保持上个活动」的配套安全阀：没有它，模型长期不可用时她会被
    永久冻结在某个活动里（若正好是 ``sleep``，就是永久静默）。

    ⚠ 它度量的是「**插件在跑**但模型没交作业」的时长，所以停机时间必须由调用方
    先排除掉（``_skip_offline_gap`` 会把 ``llm_last_success_at`` 一起往前推）。
    否则任何一次长停机都会在重启后立刻触发重新取种子，把她的活动改写成
    「当前时刻的时段表种子」——哪怕模型下一秒就能正常决策。
    """

    limit = float(hours)
    if limit <= 0:
        return False
    # 锚点：最后一次成功；从未成功过时退到「当前活动是什么时候开始的」。
    # 不能用 ``last_tick_at``：它在每 tick 末尾都会被刷新成 ``now``，差分恒为 0，
    # 于是那种状态下兜底永远不会触发（旧写法就是这样）。
    # v1.8.2 硬化：过 ``_as_float``（只认有限数）。``float("nan")`` 是**真值**，
    # 旧写法 ``nan or activity_since`` 会选中 NaN、随后所有比较恒为假 ⇒ 安全阀
    # 静默失效（审计 M1c 的机理；from_dict 已在入口拦 NaN，这里是第二道闸）。
    anchor = _as_float(state.llm_last_success_at) or _as_float(state.activity_since) or 0.0
    if anchor <= 0:
        return False
    return (float(now) - anchor) >= limit * 3600.0


# ---------------------------------------------------------------- 读取辅助


def _tier_quotas(limit: int) -> tuple[int, int, int]:
    """把总条数分配成「近 / 中 / 远」三层（近约一半、中约三成、远拿剩下的）。

    ``limit < 3`` 时三层各 1 条做不到，全部给最近层——宁可只有近层，也不要凭空
    把「近」压成 0 条（那会让最近发生的事反而进不了提示词）。
    """

    total = max(0, int(limit))
    if total <= 0:
        return (0, 0, 0)
    if total < 3:
        return (total, 0, 0)
    near = max(1, int(round(total * 0.5)))
    mid = max(1, int(round(total * 0.3)))
    far = total - near - mid
    while far < 1:
        if near > mid and near > 1:
            near -= 1
        elif mid > 1:
            mid -= 1
        else:
            break
        far = total - near - mid
    return (near, mid, far)


def _span_text(hours: float) -> str:
    """时间窗 → 人话：``12`` → ``12 小时内``；``72`` → ``3 天内``。"""

    span = max(0.0, float(hours))
    if span < 48:
        return f"{span:g} 小时内"
    days = span / 24.0
    return f"{days:g} 天内"


def _event_line(item: dict[str, Any], tz_offset_minutes: int) -> str:
    """一条经历 → 提示词行（时间按**插件本地时区**渲染，与提示词里的「现在」同一口径）。

    ``at`` 是状态文件里的数字，可能被手改成天文数字（``1e12`` 在 Windows 上会让
    ``datetime.fromtimestamp`` 抛 ``OSError``）。取值链上没有异常出口——一个坏时间戳
    不该让整个 tick 中断（活动决策、倍率同步都会跟着停）。所以这里兜底成「时间未知」。
    """

    at = _as_float(item.get("at"), 0.0) or 0.0
    label = sanitize_text(item.get("label", ""), max_chars=32)
    text = sanitize_text(item.get("text", ""), max_chars=80)
    try:
        stamp = local_datetime(at, tz_offset_minutes).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        stamp = "时间未知"
    if text:
        return f"{stamp} {label}：{text}" if label else f"{stamp} {text}"
    return f"{stamp} {label}" if label else ""


#: 分层取法：``smart`` = 自适应配额 + 显著性排序 + 标签去重；``recent`` = v1.3.x 旧行为
#: （固定配额 + 层内纯按时间取最新 N + 不去重），一字不差可回退。
PICK_SMART = "smart"
PICK_RECENT = "recent"


def _event_salience(item: dict[str, Any]) -> float:
    """一条经历的「分量」：情绪与体力的**绝对变化量**之和（体力按半权折算）。

    为什么按变化量而不是时间：提示词要回答的是「她此刻为什么是这个状态」，所以
    「11 小时前手滑删了存档（-1.2）」比「10 分钟前晒了被子（+0.5）」更该出现。
    旧取法只看时间，实测「最强情绪成因」只有 15% 的概率进得了提示词。
    """

    return abs(_as_float(item.get("emotion"), 0.0) or 0.0) + 0.5 * abs(
        _as_float(item.get("energy"), 0.0) or 0.0
    )


def _tier_pool(bucket: Sequence[tuple[float, dict[str, Any]]]) -> list[tuple[float, dict[str, Any]]]:
    """层内候选的**挑选顺序**（不是最终展示顺序）：按分量降序，同分量按时间新→旧。"""

    return sorted(bucket, key=lambda pair: (-_event_salience(pair[1]), -pair[0]))


def _select_tier_items(
    buckets: Sequence[Sequence[tuple[float, dict[str, Any]]]],
    *,
    limit: int,
    smart: bool,
    max_per_label: int,
) -> list[list[tuple[float, dict[str, Any]]]]:
    """按配额挑条目，返回三层各自选中的 ``(at, item)``（层内按时间升序）。

    旧行为（``smart=False``）：固定配额 (近/中/远) + 层内取最新 N，不去重、不让配额。
    **依赖调用方把每层按时间升序排好**（``recent_event_tiers`` 负责）。

    新行为（``smart=True``）修三件事：

    1. **显著性排序**：层内先挑分量大的（见 ``_event_salience``），同分量才看时间；
    2. **标签去重**：``max_per_label`` 是**每一层内**同标签的出现上限（1 = 每层只出现
       一次）。去重键是标签而不是整行：真机上出现过「支线通关：磨了很久的支线终于
       通了」在 6 行里占 4 行（只有时间戳不同），位置被同一件事烧掉。
       **跨层允许重复**是有意的——「近层和远层都有支线通关」正是「她这几天一直在
       打游戏」这个信号本身，把它一起去掉反而丢信息；
    3. **空层配额回收**：某层存货不足时，它的余量按「近 → 中 → 远」补给还有存货的层。
       旧写法下远层恒空（留存 40 条 × 18.9 条/日 ≈ 2.1 天，永远够不到 3 天），
       配的 8 个位置平均只填 5.8 个、从未填满过。
    """

    quotas = _tier_quotas(limit)
    if not smart:
        return [
            list(buckets[index][-quotas[index]:]) if quotas[index] > 0 else []
            for index in range(len(quotas))
        ]

    per_label = max(0, int(max_per_label))
    used: list[dict[str, int]] = [{}, {}, {}]
    pools = [_tier_pool(list(bucket)) for bucket in buckets]
    cursors = [0, 0, 0]
    chosen: list[list[tuple[float, dict[str, Any]]]] = [[], [], []]

    def take(index: int, want: int) -> int:
        """从第 index 层的候选池继续往后取，最多 want 条；返回实际取到几条。

        游标只前进不回头：同一条候选不会被两层重复考虑（否则第二遍会原样再取一遍）。
        """

        pool = pools[index]
        got = 0
        while cursors[index] < len(pool) and got < want:
            pair = pool[cursors[index]]
            cursors[index] += 1
            label = str(pair[1].get("label") or "")
            if label and per_label > 0 and used[index].get(label, 0) >= per_label:
                continue
            if label:
                used[index][label] = used[index].get(label, 0) + 1
            chosen[index].append(pair)
            got += 1
        return got

    taken = [take(index, quotas[index]) for index in range(len(quotas))]
    spare = max(0, int(limit)) - sum(taken)
    for index in range(len(quotas)):
        if spare <= 0:
            break
        spare -= take(index, spare)

    # 展示一律「层内时间升序」：层名已经表达了时间跨度，条目再按时间读最省心
    for tier in chosen:
        tier.sort(key=lambda pair: pair[0])
    return chosen


def recent_event_tiers(
    state: LifeState,
    *,
    now: float,
    limit: int = 8,
    near_hours: float = 12.0,
    mid_hours: float = 72.0,
    far_days: float = 14.0,
    tz_offset_minutes: int = DEFAULT_TZ_OFFSET_MINUTES,
    pick: str = PICK_SMART,
    max_per_label: int = 1,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """近期经历 → **近 / 中 / 远三层**，供提示词分组展示。

    为什么不再用「最近 N 条」：那等于把三天前和十分钟前的事并列，模型分不清
    「刚发生」与「还记得」。分层后时间感是显式的：

    - **近**：``near_hours`` 小时内（默认 12 小时）——她当前状态的直接成因；
    - **中**：``mid_hours`` 小时内（默认 3 天）——最近几天的基调；
    - **远**：更早，但不超过 ``far_days``（默认 14 天）——只剩「记得」的份量。

    条数按 ``_tier_quotas(limit)`` 分配；**层内时间升序、层间由近到远**。
    没有可信 ``at`` 的条目、以及超过 ``far_days`` 的旧条目都不进提示词
    （旧条目保留在状态里给情绪余波等机制用）。

    ``pick`` 决定**怎么挑**（见 ``_select_tier_items``）：默认 ``smart`` =
    显著性排序 + 标签去重 + 空层配额回收；``recent`` = v1.3.x 的旧行为，可一键回退。
    留存条数 ``recent_events_keep`` 可配（``[activity.llm]``，默认 300 ≈ 15.9 天的
    事件量）：v1.4.0 及以前固定 40 条（≈ 2.1 天）时「远」层永远拿不到内容，
    调大留存即可喂饱它。
    """

    if limit <= 0:
        return ()
    now = float(now)
    near_span = max(0.0, float(near_hours)) * 3600.0
    mid_span = max(near_span, float(mid_hours) * 3600.0)
    far_span = max(mid_span, max(0.0, float(far_days)) * 86400.0)

    buckets: list[list[tuple[float, dict[str, Any]]]] = [[], [], []]
    for item in state.recent_events:
        if not isinstance(item, dict):
            continue
        at = _as_float(item.get("at"), 0.0) or 0.0
        if at <= 0:
            continue                      # 时间戳不可信 → 不进任何一层
        age = max(0.0, now - at)          # 时间戳在未来（时钟回拨）按「刚发生」处理
        if age <= near_span:
            buckets[0].append((at, item))
        elif age <= mid_span:
            buckets[1].append((at, item))
        elif age <= far_span:
            buckets[2].append((at, item))

    # 每层按时间升序排好：旧行为直接取尾部 N 条，smart 行为用它做同分时的次序依据
    for bucket in buckets:
        bucket.sort(key=lambda pair: pair[0])

    labels = (
        f"近（{_span_text(near_hours)}）",
        f"中（{_span_text(mid_hours)}）",
        f"远（{_span_text(far_days * 24.0)}）",
    )
    picked_tiers = _select_tier_items(
        buckets,
        limit=limit,
        smart=str(pick or "").strip().lower() != PICK_RECENT,
        max_per_label=max_per_label,
    )
    tiers: list[tuple[str, tuple[str, ...]]] = []
    for label, picked in zip(labels, picked_tiers):
        lines = tuple(line for _, item in picked if (line := _event_line(item, tz_offset_minutes)))
        tiers.append((label, lines))
    return tuple(tiers)


def material_freshness(item: Mapping[str, Any], *, now: float, floor: float = 0.25) -> float:
    """素材时效系数：``best_until`` 前恒为 1.0，之后线性衰减到 ``floor``（过期触底）。

    「最佳保鲜相位」（G2）：一个念头刚冒出来时最想说，放久了就该低价值化——
    但**衰减而非清零**，让它自然排到候选队尾，而不是占着榜首直到过期那一刻
    突然消失。没有 ``best_until`` 的旧条目视为「全额新鲜到过期」（向后兼容：
    老状态文件里的素材行为与此功能加入前完全一致）。坏时间戳 / 区间倒挂一律
    按 1.0 处理：宁可高估新鲜度，也不静默吞掉素材。
    """

    expires = _as_float(item.get("expires_at"), 0.0) or 0.0
    if expires <= 0.0 or float(now) >= expires:
        return 0.0
    best = _as_float(item.get("best_until"), 0.0) or 0.0
    if best <= 0.0 or float(now) <= best:
        return 1.0
    span = expires - best
    if span <= 0.0:
        return 1.0
    progress = (float(now) - best) / span
    floored = min(1.0, max(0.0, float(floor)))
    return 1.0 - (1.0 - floored) * min(1.0, max(0.0, progress))


def material_effective_count(state: LifeState, now: float, *, floor: float = 0.25) -> float:
    """素材的「有效条数」：各条时效系数之和（供素材加成使用）。

    素材加成原来是按条数计（每条 +0.15、封顶 +0.45）；有了保鲜相位后，过了
    ``best_until`` 的素材按衰减比例折算——全新鲜时与旧行为完全一致，放旧的
    素材让加成**先于过期平滑下降**。
    """

    return float(
        sum(
            material_freshness(item, now=now, floor=floor)
            for item in state.materials
            if isinstance(item, dict)
            and (_as_float(item.get("expires_at"), 0.0) or 0.0) > float(now)
        )
    )


def active_materials(
    state: LifeState, now: float, *, floor: float = 0.25
) -> list[dict[str, Any]]:
    """还没过期的素材（按**有效权重**降序，供主动开口挑选与状态卡展示）。

    有效权重 = 原始权重 × 时效系数：放旧的念头自然排到队尾（「重新挑」），
    而不是按原始权重一直排在最前。
    """

    items = [
        item
        for item in state.materials
        if isinstance(item, dict)
        and (_as_float(item.get("expires_at"), 0.0) or 0.0) > float(now)
    ]
    items.sort(
        key=lambda item: (
            max(0.0, _as_float(item.get("weight"), 0.0) or 0.0)
            * material_freshness(item, now=now, floor=floor)
        ),
        reverse=True,
    )
    return items


def date_context(state: LifeState, now: float, config: SimConfig) -> dict[str, str]:
    """给提示词与状态卡用的日期上下文。"""

    del state
    local_now = local_datetime(now, config.tz_offset_minutes)
    rules = active_festivals(config, local_now)
    return {
        "now_label": local_now.strftime("%Y-%m-%d %H:%M"),
        "date_label": local_now.strftime("%m月%d日"),
        "season": season_of(local_now.month),
        "festival": "、".join(rule.name for rule in rules),
        "weekday": "一二三四五六日"[local_now.weekday()],
    }


def date_factor(state: LifeState, now: float, config: SimConfig) -> float:
    """当天日期规则的倍率乘积（生日 / 自定义节日），钳在 [0.2, 5.0]。"""

    del state
    local_now = local_datetime(now, config.tz_offset_minutes)
    factor = 1.0
    for rule in active_festivals(config, local_now):
        factor *= float(rule.factor)
    return _clamp(factor, 0.2, 5.0)


def sleep_hours_today(state: LifeState) -> float:
    return float(state.sleep_minutes_today) / 60.0


def awake_hours_today(state: LifeState) -> float:
    return float(state.awake_minutes_today) / 60.0


def activity_minutes(state: LifeState, now: float) -> int:
    return int(max(0.0, (float(now) - float(state.activity_since)) // 60))


def can_switch(state: LifeState, now: float, config: SimConfig) -> tuple[bool, str]:
    """当前是否允许模型换活动（给提示词用，决定权仍在 ``enforce``）。

    v1.15.0（PR-R1）：小睡有自己的时限——没满 ``nap_min_minutes`` 不许换，
    满了或到上限就交给 ``enforce``（它会叫醒她）。
    """

    minutes = activity_minutes(state, now)
    if state.activity == NAP:
        if minutes < int(config.nap_min_minutes):
            return False, f"小睡还没满 {config.nap_min_minutes} 分钟"
        if minutes >= int(config.nap_max_minutes):
            return False, f"小睡已经 {minutes} 分钟（上限 {config.nap_max_minutes}），该醒了"
        return True, ""
    if is_asleep(state.activity) and minutes < int(config.min_sleep_minutes):
        return False, f"本次睡眠还没满 {config.min_sleep_minutes} 分钟"
    if not is_asleep(state.activity) and minutes < int(config.min_dwell_minutes):
        return False, f"当前活动才持续 {minutes} 分钟，还没到 {config.min_dwell_minutes} 分钟"
    return True, ""


def is_awake_activity(activity: str) -> bool:
    """透出 ``life_activity.is_awake``，供 plugin 层判断是否在睡觉。"""

    return is_awake(activity)


def iter_material_texts(
    state: LifeState, now: float, *, floor: float = 0.25
) -> Iterable[str]:
    for item in active_materials(state, now, floor=floor):
        text = sanitize_text(item.get("text", ""), max_chars=80)
        if text:
            yield text
