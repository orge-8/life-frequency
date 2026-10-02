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
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # 包式加载（Runner 真机）
    from .life_activity import (
        ALLOWED_ACTIVITIES,
        DAILY,
        SLEEP,
        SOURCE_COLD_START,
        SOURCE_SKIPPED,
        ActivityDecision,
        ActivityFacts,
        EnforcePolicy,
        ScheduleConfig,
        enforce,
        is_awake,
        request_is_pointless,
        rule_based_activity,
        schedule_facts,
        season_of,
    )
    from .life_events import LifeEvent, pick_event, sanitize_text
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_activity import (
        ALLOWED_ACTIVITIES,
        DAILY,
        SLEEP,
        SOURCE_COLD_START,
        SOURCE_SKIPPED,
        ActivityDecision,
        ActivityFacts,
        EnforcePolicy,
        ScheduleConfig,
        enforce,
        is_awake,
        request_is_pointless,
        rule_based_activity,
        schedule_facts,
        season_of,
    )
    from life_events import LifeEvent, pick_event, sanitize_text

STATE_VERSION = 1
DEFAULT_TZ_OFFSET_MINUTES = 480  # UTC+8
BASELINE_EMOTION = 5.0
EMOTION_MAX = 10.0
ENERGY_MAX = 10.0


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

    inertia_minutes: int = 40
    recover_per_tick: float = 0.2
    sleep_recover_multiplier: float = 2.0
    afterglow_span_hours: float = 24.0
    afterglow_cap: float = 0.6
    afterglow_gain: float = 0.15
    baseline_emotion: float = BASELINE_EMOTION
    emotion_max: float = EMOTION_MAX
    energy_max: float = ENERGY_MAX

    sleep_debt_threshold_minutes: int = 300
    sleep_debt_cap_nights: int = 3
    sleep_deprived_energy_cap: float = 8.5
    cold_check_hour: int = 2
    cold_min_days: int = 1
    cold_max_days: int = 3
    cold_base_risk: float = 0.05
    cold_sleep_debt_risk: float = 0.08

    fire_probability: float = 0.4
    material_ttl_hours: float = 6.0
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
        schedule=config.schedule,
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

    emotion: float = BASELINE_EMOTION
    energy: float = 6.0
    energy_cap: float = ENERGY_MAX
    afterglow: float = 0.0
    inertia_until: float = 0.0

    sleep_started_at: float = 0.0
    sleep_minutes_today: int = 0
    awake_minutes_today: int = 0
    sleep_debt_nights: int = 0
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
                    setattr(state, key, float(value))
                elif isinstance(current, int):
                    setattr(state, key, int(value))
                elif isinstance(current, str):
                    setattr(state, key, str(value))
                elif isinstance(current, list):
                    setattr(state, key, list(value) if isinstance(value, list) else [])
                elif isinstance(current, dict):
                    setattr(state, key, dict(value) if isinstance(value, dict) else {})
            except (TypeError, ValueError):
                continue
        if state.activity not in ALLOWED_ACTIVITIES:
            state.activity = DAILY
        state.emotion = _clamp(state.emotion, 0.0, EMOTION_MAX)
        state.energy = _clamp(state.energy, 0.0, ENERGY_MAX)
        state.energy_cap = _clamp(state.energy_cap, 1.0, ENERGY_MAX)
        state.applied = _sanitize_adjust_map(state.applied)
        state.foreign = _sanitize_adjust_map(state.foreign)
        state.observed = _sanitize_adjust_map(state.observed)
        state.unbacked = _sanitize_adjust_map(state.unbacked)
        state.unbacked_target = _sanitize_adjust_map(state.unbacked_target)
        state.materials = _sanitize_records(
            state.materials, fields=("created_at", "expires_at", "weight")
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
    if state.activity == SLEEP:
        state.sleep_started_at = float(now)
    return state


def is_cold(state: LifeState, now: float) -> bool:
    return float(now) < float(state.cold_until)


def health_label(state: LifeState, now: float, config: SimConfig) -> str:
    """给提示词与状态卡用的健康描述。"""

    if is_cold(state, now):
        remaining_hours = max(0.0, (state.cold_until - now) / 3600.0)
        return f"感冒中（约剩 {remaining_hours:.1f} 小时）"
    if state.sleep_debt_nights >= config.sleep_debt_cap_nights:
        return f"连续熬夜 {state.sleep_debt_nights} 天，体力上限被压低"
    if state.sleep_debt_nights > 0:
        return f"有点缺觉（连熬 {state.sleep_debt_nights} 天）"
    return "健康"


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
    if state.activity == SLEEP:
        # 优先用「她几点睡下的」这个专用字段；缺失时退回活动起始时间
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
        sick=is_cold(state, now),
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
    state.emotion = _clamp(state.emotion + event.emotion, 0.0, config.emotion_max)
    state.energy = _clamp(state.energy + event.energy, 0.0, state.energy_cap)
    state.inertia_until = float(now) + config.inertia_minutes * 60.0

    text = sanitize_text(event.material, max_chars=80)
    if text:
        state.materials.append(
            {
                "label": event.label,
                "text": text,
                "weight": float(event.weight),
                "created_at": float(now),
                "expires_at": float(now) + float(event.ttl_hours) * 3600.0,
            }
        )
    state.recent_events.append(
        {
            "at": float(now),
            "label": event.label,
            "activity": state.activity,
            "text": text,
            "emotion": float(event.emotion),
            "energy": float(event.energy),
        }
    )


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
    """

    record = dict(entry)
    delta = _as_float(record.get("emotion"), 0.0) or 0.0
    if delta:
        state.emotion = _clamp(state.emotion + delta, 0.0, config.emotion_max)
    state.recent_events.append(record)
    return state


def _recompute_afterglow(state: LifeState, now: float, config: SimConfig) -> None:
    """24 小时情绪余波：把窗口内的事件情绪增量折成基线偏移，钳在 ±cap。"""

    span = float(config.afterglow_span_hours) * 3600.0
    total = 0.0
    for item in state.recent_events:
        if not isinstance(item, dict):
            continue
        at = _as_float(item.get("at"), 0.0) or 0.0
        if float(now) - at <= span:
            total += _as_float(item.get("emotion"), 0.0) or 0.0
    if not math.isfinite(total):
        total = 0.0
    state.afterglow = _clamp(total * float(config.afterglow_gain), -config.afterglow_cap, config.afterglow_cap)


def _regress_emotion(state: LifeState, *, config: SimConfig, minutes: float) -> None:
    """情绪回归：惰性期内不动；之后按 tick 速率靠向基线，睡眠时加倍。

    ``minutes`` 是本次推进的步长；步长可能大于一个 tick（长期离线后的补偿步），
    所以按比例放大速率，保证「10 分钟回归 0.2」这个标定在任何步长下都成立。
    """

    steps = max(0.0, minutes) / max(1.0, float(config.tick_seconds) / 60.0)
    rate = float(config.recover_per_tick) * steps
    if state.activity == SLEEP:
        rate *= float(config.sleep_recover_multiplier)
    baseline = _clamp(
        float(config.baseline_emotion) + float(state.afterglow), 0.0, config.emotion_max
    )
    delta = baseline - state.emotion
    if abs(delta) <= rate:
        state.emotion = baseline
    else:
        state.emotion += rate if delta > 0 else -rate
    state.emotion = _clamp(state.emotion, 0.0, config.emotion_max)


def settle(
    state: LifeState,
    *,
    now: float,
    config: SimConfig,
    events: Sequence[LifeEvent] = (),
    rng: random.Random | None = None,
    on_offline_gap: Callable[[int, bool], None] | None = None,
) -> LifeState:
    """用**当前**活动把 ``last_tick_at → now`` 这段时间结算掉。

    分步推进（每步一个 tick），逐步处理：体力增减、睡眠/清醒记账、生活日边界结算、
    感冒骰子、情绪回归、事件抽取。步数按 ``max_catch_up_hours`` 封顶。

    但**「停机间隙」不结算**：间隔超过 ``offline_gap_minutes`` 时交给
    ``_skip_offline_gap``——那段时间的状态无从判断，逐 tick 补算只会凭空造出
    「清醒 N 小时」并把硬约束的睡眠资格搞坏（真机事故见该函数说明）。
    ``on_offline_gap(gap_minutes, crossed_boundary)`` 是可选的日志回调：
    本模块不依赖 ctx，要日志就得由调用方注入。
    """

    rng = rng or random.Random(0)
    now = float(now)
    if state.last_tick_at <= 0:
        state.last_tick_at = now
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

    # 若当前是睡眠且没记开始时间，补记（例如热重载或旧状态）
    if state.activity == SLEEP and not state.sleep_started_at:
        state.sleep_started_at = cursor

    for _ in range(steps):
        cursor += step_seconds
        local_cursor = local_datetime(cursor, config.tz_offset_minutes)

        # --- 体力 ---
        drain = float(delta_per_hour.get(state.activity, -0.5))
        state.energy = _clamp(
            state.energy + drain * (step_minutes / 60.0), 0.0, float(state.energy_cap)
        )

        # --- 睡眠 / 清醒记账 ---
        minutes = int(round(step_minutes))
        if state.activity == SLEEP:
            state.sleep_minutes_today += minutes
        else:
            state.awake_minutes_today += minutes

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
            if not is_cold(state, cursor):
                risk = float(config.cold_base_risk) + float(config.cold_sleep_debt_risk) * max(
                    0, int(state.sleep_debt_nights)
                )
                if float(state.energy) < float(config.sleep_energy_threshold):
                    risk += float(config.cold_sleep_debt_risk)
                if rng.random() < _clamp(risk, 0.0, 0.95):
                    days = rng.randint(int(config.cold_min_days), max(int(config.cold_min_days), int(config.cold_max_days)))
                    state.cold_days = days
                    state.cold_until = cursor + days * 86400.0

        # --- 情绪：余波 + 回归 ---
        _recompute_afterglow(state, cursor, config)
        if cursor >= float(state.inertia_until):
            _regress_emotion(state, config=config, minutes=step_minutes)

        # --- 事件抽取（睡眠中不抽）---
        if state.activity != SLEEP:
            fired = pick_event(events, state.activity, rng, probability=config.fire_probability)
            if fired is not None:
                _apply_event(state, fired, now=cursor, config=config, rng=rng)

    state.last_tick_at = now
    state.energy = _clamp(state.energy, 0.0, float(state.energy_cap))
    _recompute_afterglow(state, now, config)
    return _sweep(state, now, config)


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
    if state.activity == SLEEP and not state.sleep_started_at:
        # 睡眠锚点缺失时补「现在」，别让 minutes_in_sleep 变成一个假的大值
        state.sleep_started_at = now
    if on_offline_gap is not None:
        on_offline_gap(int(max(0.0, elapsed) // 60), crossed)
    return _sweep(state, now, config)


# ---------------------------------------------------------------- 熬夜判定

#: 「睡够没有」看最近这么久（滑动窗口）。取 24 小时：既不依赖生活日边界，
#: 又能把「拆成两段睡」自然合起来算。
SLEEP_DEBT_WINDOW_HOURS = 24.0
#: 账本最多留几段睡眠（每段至少 min_sleep_minutes，24 小时内正常只有 1~2 段）。
SLEEP_LEDGER_KEEP = 8


def record_sleep_episode(state: LifeState, *, now: float, minutes: int) -> None:
    """把一段**刚结束**的睡眠记进滚动账本，并清掉与窗口再无交集的旧条目。"""

    minutes = int(max(0, int(minutes)))
    now = float(now)
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
    """

    total = sleep_in_window(state, now=now) + max(0, int(extra_minutes))
    if total >= int(config.sleep_debt_threshold_minutes):
        state.sleep_debt_nights = 0
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
        state.emotion = _clamp(state.emotion + rule.emotion, 0.0, config.emotion_max)
        state.energy = _clamp(state.energy + rule.energy, 0.0, float(state.energy_cap))
        if rule.emotion:
            state.inertia_until = max(state.inertia_until, float(now) + config.inertia_minutes * 60.0)
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


def enforce_facts(state: LifeState, *, now: float, config: SimConfig) -> ActivityFacts:
    """状态 → ``enforce`` 需要的事实（含本地时刻与班表）。

    单独抽出来是给「这一轮问模型有没有意义」（``pointless_ask_reason``）复用：
    两处口径必须一致，否则跳过判断会与实际裁定对不上。
    """

    local_now = local_datetime(now, config.tz_offset_minutes)
    return replace(
        activity_facts(state, now),
        now_minutes=local_now.hour * 60 + local_now.minute,
        schedule=schedule_facts(local_now, config.schedule),
    )


def pointless_ask_reason(state: LifeState, *, now: float, config: SimConfig) -> str:
    """这一轮问模型是不是注定白问（空串 = 该问）。判据见 ``request_is_pointless``。"""

    return request_is_pointless(
        enforce_facts(state, now=now, config=config), build_enforce_policy(config)
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
        if decision.activity == SLEEP:
            state.sleep_started_at = now
        elif previous == SLEEP:
            # 先量这一觉再清锚点。量不到（旧状态没有 sleep_started_at）时退回当日累计：
            # 那是唯一能用的证据，而且它不能同时进账本，否则会重复计。
            minutes = _minutes_since(now, state.sleep_started_at)
            state.sleep_started_at = 0.0
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
        state.activity_since = now

    state.activity = decision.activity
    state.activity_source = decision.source
    state.activity_note = decision.note
    if decision.scene:
        state.scene = decision.scene
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
    anchor = float(state.llm_last_success_at) or float(state.activity_since)
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


def active_materials(state: LifeState, now: float) -> list[dict[str, Any]]:
    """还没过期的素材（按权重降序，供主动开口挑选）。"""

    items = [
        item
        for item in state.materials
        if isinstance(item, dict)
        and (_as_float(item.get("expires_at"), 0.0) or 0.0) > float(now)
    ]
    items.sort(key=lambda item: _as_float(item.get("weight"), 0.0) or 0.0, reverse=True)
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
    """当前是否允许模型换活动（给提示词用，决定权仍在 ``enforce``）。"""

    minutes = activity_minutes(state, now)
    if state.activity == SLEEP and minutes < int(config.min_sleep_minutes):
        return False, f"本次睡眠还没满 {config.min_sleep_minutes} 分钟"
    if state.activity != SLEEP and minutes < int(config.min_dwell_minutes):
        return False, f"当前活动才持续 {minutes} 分钟，还没到 {config.min_dwell_minutes} 分钟"
    return True, ""


def is_awake_activity(activity: str) -> bool:
    """透出 ``life_activity.is_awake``，供 plugin 层判断是否在睡觉。"""

    return is_awake(activity)


def iter_material_texts(state: LifeState, now: float) -> Iterable[str]:
    for item in active_materials(state, now):
        text = sanitize_text(item.get("text", ""), max_chars=80)
        if text:
            yield text
