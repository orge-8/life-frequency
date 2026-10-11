# -*- coding: utf-8 -*-
"""习惯层（routine）：每天固定时间做固定的事（纯模块，无 ctx、无 IO）。

**为什么需要这一层**：真人不是每 10 分钟重新想一次「我接下来干嘛」——她 07:00 会
起床洗漱、08:00 会吃早饭、23:00 会洗澡。这些事不需要模型发挥，模型发挥反而会
让她的作息**每天都长得不一样**（同一个周二上午，昨天在听歌、今天在打游戏）。

所以习惯表是一个 **proposal 源**，位置在模型**之下**、强制层**之上**：

```text
习惯表（确定性） → 模型（空隙里发挥） → enforce 强制层（唯一收口）
```

三个纪律：

1. **习惯只锁活动，不锁场景细节**：场景文本由行里的第二个字段给定（「起床洗漱」），
   模型在空隙轮里可以换活动，但习惯窗口内由习惯说了算——细节留给模型，骨架交给用户。
2. **每条习惯一天只命中一次**：命中状态存 SQLite（``life_store.routine_daily``），
   键 ``(day_key, line_id)``。没有这个记忆，40 分钟的习惯窗口在 10 分钟一个 tick
   下会被命中四次。
3. **抖动必须按天固定**：``07:00-07:30|起床洗漱`` 天天分秒不差地触发，是最刺眼的
   机器感。抖动量在当天第一次见到该行时掷一次并存库，之后整天的窗口位置不变
   ——否则每个 tick 重掷，窗口会在边界上反复进出（同一顿早饭被命中好几次）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

try:  # 包式加载（Runner 真机）
    from .life_activity import (
        SOURCE_ROUTINE,
        ActivityDecision,
        in_window,
        minutes_to_hhmm,
        normalize_activity,
        parse_bool_flag,
        parse_weekday_set,
        parse_window,
    )
    from .life_events import sanitize_text
    from .life_store import ROUTINE_FIRED, ROUTINE_PENDING, ROUTINE_SKIPPED
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_activity import (  # type: ignore[no-redef]
        SOURCE_ROUTINE,
        ActivityDecision,
        in_window,
        minutes_to_hhmm,
        normalize_activity,
        parse_bool_flag,
        parse_weekday_set,
        parse_window,
    )
    from life_events import sanitize_text  # type: ignore[no-redef]
    from life_store import (  # type: ignore[no-redef]
        ROUTINE_FIRED,
        ROUTINE_PENDING,
        ROUTINE_SKIPPED,
    )

#: 场景候选的分隔符（v1.17.0，PR-ROU-2）：全角/半角分号。
#: 刻意不用换行或逗号——场景文本里逗号很常见（「靠着窗边，听着歌」）。
_SCENE_SEP = re.compile(r"[;\uFF1B]+")
#: 场景文本上限，与活动决策的 ``max_scene_chars``（40）对齐
MAX_SCENE_CHARS = 40
#: 抖动量的合法区间（分钟）。超过这个范围不是「自由」，是写错了
MAX_JITTER_MINUTES = 180
DEFAULT_JITTER_MINUTES = 15


def _as_bool(value: object, default: bool = False) -> tuple[bool, bool]:
    """``"true"`` → ``(True, True)``；认不出来 → ``(default, False)``。

    v1.17.0（PR-PHY-3）：实现搬去 ``life_activity.parse_bool_flag``，三处行 DSL
    （习惯 / 生理窗 / 班表）共用一份——``bool("false")`` 恒为 True 这个坑只该
    修一次。这里保留包装是为了不动既有调用点与用例。
    """

    return parse_bool_flag(value, default)


def _as_weekdays(value: object) -> tuple[tuple[int, ...], str]:
    """``"1-5"`` / ``"六日"`` / ``"1,3,5"`` → 星期元组；空串表示「每天」。

    v1.17.0（PR-PHY-3）：解析核心搬去 ``life_activity.parse_weekday_set``
    （与班表、生理窗同源）；这里只保留「空 = 每天、坏值 = 整行跳过」的包装语义。
    """

    days, warnings = parse_weekday_set(value)
    if days is None:
        if warnings:
            return (), f"days={str(value or '').strip()!r} 认不出来（用 1-7 或 一/二/…/日）"
        return (), ""
    return tuple(sorted(days)), ""


def line_id_of(raw: str) -> str:
    """行 ID = 原始行文本的 SHA1 前 16 位。

    ⚠ **不能用内置 ``hash()``**：它带随机盐（``PYTHONHASHSEED``），同一行在下次
    启动会算出不同的 ID ⇒ 库里的「今天已命中」全部对不上，习惯天天重复触发。
    """

    return hashlib.sha1(str(raw or "").strip().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RoutineLine:
    """一条习惯（解析后的形态）。"""

    line_id: str
    raw: str
    start: int
    end: int
    scene: str
    activity: str
    weight: float = 1.0
    jitter: int = DEFAULT_JITTER_MINUTES
    days: tuple[int, ...] = ()
    """空 = 每天；否则只在列出的星期生效。"""

    workday_only: bool = False
    holiday_only: bool = False
    physio: bool = False
    """标记「这一行是三餐/洗澡」；v1.9.x 的 physio（方案一）消费，本版只解析不消费。"""

    scenes: tuple[str, ...] = ()
    """场景候选（v1.17.0，PR-ROU-2）。单候选时 ``scene`` 与它内容相同。

    多候选用 ``；`` 分隔——长窗口（三小时晚自习）里场景文字一直不变是 README
    自认的机器感来源，而习惯窗口内**本来就不问模型**，所以只能靠行内素材轮换。
    """

    def window(self, jitter_minutes: int = 0) -> tuple[int, int]:
        """抖動后的窗口（半开区间，支持跨午夜）。"""

        return (int(self.start) + int(jitter_minutes), int(self.end) + int(jitter_minutes))

    def scene_for(self, *, day_key: str = "", now_minutes: int = 0) -> str:
        """命中时用的场景文本（v1.17.0，PR-ROU-2）。

        单候选 = 行里写的那句（与加这一层之前**逐位一致**）；多候选按
        ``sha1(生活日|行ID|小时桶)`` 确定性挑一条：同一天同一小时桶里稳定
        （不会每 tick 换一句），跨桶轮换，且零额外模型调用。
        **不传 day_key = 不轮换**（取第一个候选）——老调用点因此逐位不变。
        """

        if len(self.scenes) <= 1 or not str(day_key or "").strip():
            return self.scene
        bucket = int(now_minutes) // 60
        seed = f"{day_key}|{self.line_id}|{bucket}".encode("utf-8")
        index = int(hashlib.sha1(seed).hexdigest()[:8], 16) % len(self.scenes)
        return self.scenes[index]

    def label(self) -> str:
        """日志/状态展示用的一段人话。"""

        days = ""
        if self.days:
            days = " 周" + "".join(
                {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}[day]
                for day in self.days
            )
        return f"{minutes_to_hhmm(self.start)}-{minutes_to_hhmm(self.end)} {self.scene}{days}"


def parse_routine_lines(
    lines: object,
    *,
    default_jitter: int = DEFAULT_JITTER_MINUTES,
) -> tuple[tuple[RoutineLine, ...], list[str]]:
    """解析 ``[routines].lines`` 的行 DSL。

    格式：``HH:MM-HH:MM|场景|活动|key=value...``

    ``07:00-07:30|起床洗漱|daily|weight=1.0``

    场景字段（v1.17.0，PR-ROU-2）可以用 ``；`` 分隔多个候选：
    ``07:00-07:30|起床洗漱；冲了个澡；做了个拉伸|daily``——单候选 = 旧行为。

    修饰符：``weight``（0–1，当日命中概率）／``jitter``（分钟，窗口整体平移幅度）／
    ``days``（星期，空 = 每天）／``workday_only``／``holiday_only``／``physio``。
    坏行告警跳过——写错了必须有线索，不能静默少一条习惯。
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

    parsed: list[RoutineLine] = []
    warnings: list[str] = []
    base_jitter = max(0, min(MAX_JITTER_MINUTES, int(default_jitter or 0)))

    for line in candidates:
        raw = str(line or "").strip()
        if not raw:
            continue
        fields = [chunk.strip() for chunk in raw.split("|")]

        window = parse_window(fields[0] if fields else "", default=(-1, -1))
        if window[0] < 0 or window[1] < 0:
            warnings.append(f"习惯行 {raw!r} 的时间窗不是 HH:MM-HH:MM，整行跳过")
            continue
        if window[0] == window[1]:
            warnings.append(f"习惯行 {raw!r} 的起止时间相同（零长度窗口永不命中），整行跳过")
            continue

        # v1.17.0（PR-ROU-2）：场景候选（`；` 分隔）。逐条净化，空候选丢弃；
        # 一条都不剩 = 缺场景文本（与旧口径一致：整行跳过并告警）。
        scenes = tuple(
            text
            for text in (
                sanitize_text(chunk, max_chars=MAX_SCENE_CHARS)
                for chunk in _SCENE_SEP.split(fields[1] if len(fields) > 1 else "")
            )
            if text
        )
        if not scenes:
            warnings.append(f"习惯行 {raw!r} 缺少场景文本（第二个字段），整行跳过")
            continue
        scene = scenes[0]

        activity = normalize_activity(fields[2] if len(fields) > 2 else "")
        if not activity:
            warnings.append(
                f"习惯行 {raw!r} 的活动（第三个字段）不是已知活动，整行跳过"
            )
            continue

        payload: dict[str, str] = {}
        for chunk in fields[3:]:
            if "=" in chunk:
                key, value = chunk.split("=", 1)
                payload[key.strip().lower()] = value.strip()

        weight = 1.0
        if "weight" in payload:
            try:
                weight = float(payload["weight"])
            except ValueError:
                warnings.append(f"习惯 {scene}: weight={payload['weight']!r} 不是数字，按 1.0 处理")
                weight = 1.0
            weight = max(0.0, min(1.0, weight))

        jitter = base_jitter
        if "jitter" in payload:
            try:
                jitter = int(float(payload["jitter"]))
            except ValueError:
                warnings.append(f"习惯 {scene}: jitter={payload['jitter']!r} 不是整数，按 {base_jitter} 处理")
                jitter = base_jitter
            if jitter < 0 or jitter > MAX_JITTER_MINUTES:
                warnings.append(
                    f"习惯 {scene}: jitter={jitter} 超出 0-{MAX_JITTER_MINUTES}，已钳到边界"
                )
                jitter = max(0, min(MAX_JITTER_MINUTES, jitter))

        days, days_warning = _as_weekdays(payload.get("days", ""))
        if days_warning:
            warnings.append(f"习惯 {scene}: {days_warning}，整行跳过")
            continue

        workday_only, ok = _as_bool(payload.get("workday_only", ""), False)
        if not ok and "workday_only" in payload:
            warnings.append(f"习惯 {scene}: workday_only={payload['workday_only']!r} 认不出来，按关闭处理")
        holiday_only, ok = _as_bool(payload.get("holiday_only", ""), False)
        if not ok and "holiday_only" in payload:
            warnings.append(f"习惯 {scene}: holiday_only={payload['holiday_only']!r} 认不出来，按关闭处理")
        physio, ok = _as_bool(payload.get("physio", ""), False)
        if not ok and "physio" in payload:
            warnings.append(f"习惯 {scene}: physio={payload['physio']!r} 认不出来，按关闭处理")

        if workday_only and holiday_only:
            warnings.append(f"习惯 {scene}: workday_only 与 holiday_only 同时为真，这一行将永不命中")

        known_keys = {"weight", "jitter", "days", "workday_only", "holiday_only", "physio"}
        for key in payload:
            if key not in known_keys:
                warnings.append(f"习惯 {scene}: 未知修饰符 {key}={payload[key]!r}，已忽略")

        parsed.append(
            RoutineLine(
                line_id=line_id_of(raw),
                raw=raw,
                start=int(window[0]),
                end=int(window[1]),
                scene=scene,
                activity=activity,
                weight=weight,
                jitter=jitter,
                days=days,
                workday_only=workday_only,
                holiday_only=holiday_only,
                physio=physio,
                scenes=scenes,
            )
        )

    return tuple(parsed), warnings


@dataclass(frozen=True)
class RoutineContext:
    """某一时刻判定习惯需要的全部事实（纯数据，便于单测）。"""

    now_minutes: int = 0
    weekday: int = 0
    """1 = 周一 … 7 = 周日（``isoweekday()`` 口径）。"""

    is_workday: bool = True
    is_holiday: bool = False

    @classmethod
    def from_local_dt(
        cls,
        local_dt: datetime,
        *,
        is_workday: bool,
        is_holiday: bool | None = None,
    ) -> "RoutineContext":
        """由本地时间与「今天是什么日子」构造。

        ``is_holiday`` 省略时取 ``not is_workday``——那是 **v1.9.0 的无日历降级口径**；
        v1.10.0 起 plugin 侧由 ``_routine_day_flags`` 传入真正的日历判定
        （法定节假日 / 调休上班），两者**不再互补**：调休上班的周六既是工作日
        又不是节日。
        """

        try:
            weekday = int(local_dt.isoweekday())
            now_minutes = int(local_dt.hour) * 60 + int(local_dt.minute)
        except Exception:  # noqa: BLE001 —— 传进来的不是 datetime 时按「不匹配」处理
            return cls()
        return cls(
            now_minutes=now_minutes,
            weekday=weekday if 1 <= weekday <= 7 else 0,
            is_workday=bool(is_workday),
            is_holiday=bool(not is_workday) if is_holiday is None else bool(is_holiday),
        )


def line_matches_day(line: RoutineLine, context: RoutineContext) -> bool:
    """这一行今天**有没有资格**生效（星期 / 工作日 / 节假日三项）。"""

    if line.days and context.weekday not in line.days:
        return False
    if line.workday_only and not context.is_workday:
        return False
    if line.holiday_only and not context.is_holiday:
        return False
    return True


def active_lines(
    lines: Sequence[RoutineLine],
    *,
    context: RoutineContext,
    jitter_map: Mapping[str, int] | None = None,
) -> tuple[RoutineLine, ...]:
    """此刻落在窗口里的习惯行（**不看命中状态**，按配置顺序）。"""

    jitter_map = jitter_map or {}
    matched: list[RoutineLine] = []
    for line in lines:
        if not line_matches_day(line, context):
            continue
        if in_window(context.now_minutes, line.window(int(jitter_map.get(line.line_id, 0)))):
            matched.append(line)
    return tuple(matched)


def pick_pending(
    active: Sequence[RoutineLine],
    status_map: Mapping[str, int] | None = None,
) -> RoutineLine | None:
    """在窗口内的行里挑第一条「当日还没定论」的。

    状态取自 ``life_store`` 的 ``routine_daily``（``fired`` 列）：
    ``ROUTINE_PENDING`` 才参与命中。
    """

    status_map = status_map or {}
    for line in active:
        if int(status_map.get(line.line_id, ROUTINE_PENDING)) == ROUTINE_PENDING:
            return line
    return None


def roll_weight(line: RoutineLine, rng: Any) -> bool:
    """掷一次「今天这条习惯来不来」（每个生活日只掷一次，结果存库）。

    ``weight=1.0`` 恒中、``weight=0`` 恒不中；中间值按概率——同一条习惯不是每天都
    发生，才是活的（天天分秒不差地吃早饭，本身就是机器感）。
    """

    if line.weight >= 1.0:
        return True
    if line.weight <= 0.0:
        return False
    try:
        return float(rng.random()) < float(line.weight)
    except Exception:  # noqa: BLE001 —— 随机源异常时按「不中」处理（宁可少一次，不可炸）
        return False


def decision_for(
    line: RoutineLine, *, day_key: str = "", now_minutes: int = 0
) -> ActivityDecision:
    """习惯命中 → 一个 proposal。**仍要过 ``enforce``**（习惯不能让她在睡眠时段爬起来）。

    ``day_key`` / ``now_minutes``（v1.17.0，PR-ROU-2）：多候选场景用它们确定性选一条；
    不传 = 用第一个候选（单候选行与老调用点**逐位一致**）。
    """

    return ActivityDecision(
        activity=line.activity,
        scene=line.scene_for(day_key=day_key, now_minutes=now_minutes),
        source=SOURCE_ROUTINE,
        note=f"习惯：{line.scene}",
    )


__all__ = [
    "DEFAULT_JITTER_MINUTES",
    "MAX_JITTER_MINUTES",
    "MAX_SCENE_CHARS",
    "ROUTINE_FIRED",
    "ROUTINE_PENDING",
    "ROUTINE_SKIPPED",
    "RoutineContext",
    "RoutineLine",
    "active_lines",
    "decision_for",
    "line_matches_day",
    "line_id_of",
    "parse_routine_lines",
    "pick_pending",
    "roll_weight",
]
