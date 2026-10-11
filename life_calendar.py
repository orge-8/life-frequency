# -*- coding: utf-8 -*-
"""中国日历（calendar）：法定节假日 / 调休 / 主要农历节日的离线表（纯模块，无 ctx）。

**为什么是离线表**：「春节没反应」是最刺眼的非人味缺口。农历算法库（如
``zhdate`` / ``lunarcalendar``）是新增依赖且年临界复杂；国务院的放假安排每年
年底才公布、且包含**调休**这种「某个周六上班、某个周一不上班」的反直觉规则——
任何算法都算不出调休，只有公布了的表才可信。

数据文件 ``data/calendar.toml`` 随插件分发（**不是用户配置**）：

```toml
[[days]]
date = "2026-02-17"
name = "春节"
kind = "holiday"        # holiday=法定节假日 / festival=传统节日(不放假) / workday_swap=调休上班
lunar = true
```

覆盖范围：2026–2027 国务院公布的法定节假日与调休 + 主要农历节日（春节/除夕/
元宵/清明/端午/七夕/中秋/重阳/冬至）。农历节日的公历日期**直接预计算硬编码**
进表——表体积换准确，每年维护一次。表缺当年的数据 → 降级为「全部按普通日」，
启动时告警一次（不阻塞加载）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:  # 包式加载（Runner 真机）
    from .life_events import sanitize_text
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_events import sanitize_text  # type: ignore[no-redef]

KIND_HOLIDAY = "holiday"
"""法定节假日：她不上班（班表相位整体失效）。"""

KIND_FESTIVAL = "festival"
"""传统节日（不放假）：照常上班，但提示词与素材会提到节日。"""

KIND_WORKDAY_SWAP = "workday_swap"
"""调休上班日：某个周末被改成工作日（班表照常，别的插件眼里的周六也该上班）。"""

_VALID_KINDS = (KIND_HOLIDAY, KIND_FESTIVAL, KIND_WORKDAY_SWAP)

DATE_FMT_ERROR = "date 不是 YYYY-MM-DD"


@dataclass(frozen=True)
class DayInfo:
    """某一天在日历上的事实。"""

    name: str = ""
    kind: str = ""

    def __bool__(self) -> bool:
        return bool(self.name)

    @property
    def is_holiday(self) -> bool:
        """法定节假日（她不上班）。"""

        return self.kind == KIND_HOLIDAY

    @property
    def is_workday_swap(self) -> bool:
        """调休上班的周末。"""

        return self.kind == KIND_WORKDAY_SWAP


@dataclass(frozen=True)
class Calendar:
    """加载后的日历表（内存态，进程共享一份）。"""

    days: Mapping[str, DayInfo]
    """``YYYY-MM-DD`` → ``DayInfo``。"""

    years: frozenset[str] = frozenset()
    """表里出现过的年份（判断「今年有没有被覆盖」用）。"""

    source_name: str = "data/calendar.toml"
    """日志里显示的数据来源（真实路径或内置默认）。"""

    def day_info(self, year: int, month: int, day: int) -> DayInfo:
        key = f"{year:04d}-{month:02d}-{day:02d}"
        return self.days.get(key) or DayInfo()

    def covers_year(self, year: int) -> bool:
        return f"{year:04d}" in self.years


def _parse_date(text: object) -> tuple[int, int, int] | None:
    raw = str(text or "").strip()
    parts = raw.split("-")
    if len(parts) != 3:
        return None
    try:
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1970 <= year <= 2999):
        return None
    return year, month, day


def build_calendar(entries: object, *, source_name: str = "data/calendar.toml") -> tuple[Calendar, list[str]]:
    """把 ``[[days]]`` 条目数组洗成 :class:`Calendar`；坏条目告警跳过。

    ``entries`` 可以是任意可迭代的 mapping（解析 toml 的调用方负责取
    ``payload.get("days")``）。重名日期后到先得覆盖——不告警，维护表时多写一条
    修正条目是合法操作。
    """

    if isinstance(entries, Mapping):
        candidates: list[object] = [entries]
    else:
        try:
            candidates = [item for item in entries]  # type: ignore[union-attr]
        except TypeError:
            return Calendar({}, frozenset(), source_name), [f"{source_name} 的 days 不是列表"]

    days: dict[str, DayInfo] = {}
    years: set[str] = set()
    warnings: list[str] = []
    for entry in candidates:
        if not isinstance(entry, Mapping):
            continue
        date = _parse_date(entry.get("date"))
        if date is None:
            warnings.append(f"日历条目 {entry!r} 的 {DATE_FMT_ERROR}，整条跳过")
            continue
        name = sanitize_text(entry.get("name"), max_chars=24)
        if not name:
            warnings.append(f"日历条目 {entry.get('date')!r} 缺少 name，整条跳过")
            continue
        kind = str(entry.get("kind") or KIND_FESTIVAL).strip().lower()
        if kind not in _VALID_KINDS:
            warnings.append(
                f"日历条目 {name}: kind={kind!r} 不是 {'/'.join(_VALID_KINDS)}，按 festival 处理"
            )
            kind = KIND_FESTIVAL
        key = f"{date[0]:04d}-{date[1]:02d}-{date[2]:02d}"
        days[key] = DayInfo(name=name, kind=kind)
        years.add(key[:4])
    return Calendar(days, frozenset(years), source_name), warnings


def load_calendar_file(path: object) -> tuple[Calendar, list[str]]:
    """从 toml 文件加载日历。文件不存在/解析失败 → **空表 + 告警**（绝不抛）。

    Python 3.11+ 用内置 ``tomllib``；更老的解释器上「日历不可用」比装一个 toml
    依赖更好——降级路径与「表里没有这一年」是同一条。
    """

    path = Path(str(path or ""))
    source = str(path)
    if not path.is_file():
        calendar, warnings = build_calendar([], source_name=source)
        return calendar, [*warnings, f"日历表不存在：{source}"]
    try:
        import tomllib
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except ModuleNotFoundError:
        calendar, warnings = build_calendar([], source_name=source)
        return calendar, [*warnings, "当前 Python 无 tomllib（<3.11），日历降级为空表"]
    except (OSError, ValueError) as exc:
        calendar, warnings = build_calendar([], source_name=source)
        return calendar, [*warnings, f"日历表不可读（{source}）：{exc}"]
    if not isinstance(payload, Mapping):
        calendar, warnings = build_calendar([], source_name=source)
        return calendar, [*warnings, f"日历表格式不对（{source}）：顶层不是表"]
    entries = payload.get("days")
    if entries is None:
        calendar, warnings = build_calendar([], source_name=source)
        return calendar, [*warnings, f"日历表缺 days 节（{source}），降级为空表"]
    return build_calendar(entries, source_name=source)


#: 内置默认表（当 ``data/calendar.toml`` 缺失时使用；与表文件同一批日期）。
#: 维护口径：每年更新一次，2026-10 覆盖 2026 与 2027。
BUILTIN_CALENDAR_ENTRIES = [
    # ---- 2026 ----
    {"date": "2026-01-01", "name": "元旦", "kind": "holiday"},
    {"date": "2026-02-16", "name": "除夕", "kind": "holiday", "lunar": True},
    {"date": "2026-02-17", "name": "春节", "kind": "holiday", "lunar": True},
    {"date": "2026-02-18", "name": "春节", "kind": "holiday", "lunar": True},
    {"date": "2026-02-19", "name": "春节", "kind": "holiday", "lunar": True},
    {"date": "2026-02-20", "name": "春节", "kind": "holiday", "lunar": True},
    {"date": "2026-03-05", "name": "元宵节", "kind": "festival", "lunar": True},
    {"date": "2026-04-05", "name": "清明节", "kind": "holiday", "lunar": True},
    {"date": "2026-04-06", "name": "清明节", "kind": "holiday", "lunar": True},
    {"date": "2026-05-01", "name": "劳动节", "kind": "holiday"},
    {"date": "2026-05-02", "name": "劳动节", "kind": "holiday"},
    {"date": "2026-05-03", "name": "劳动节", "kind": "holiday"},
    {"date": "2026-06-19", "name": "端午节", "kind": "holiday", "lunar": True},
    {"date": "2026-06-20", "name": "端午节", "kind": "holiday", "lunar": True},
    {"date": "2026-06-21", "name": "端午节", "kind": "holiday", "lunar": True},
    {"date": "2026-08-19", "name": "七夕", "kind": "festival", "lunar": True},
    {"date": "2026-09-25", "name": "中秋节", "kind": "holiday", "lunar": True},
    {"date": "2026-09-26", "name": "中秋节", "kind": "holiday", "lunar": True},
    {"date": "2026-09-27", "name": "中秋节", "kind": "holiday", "lunar": True},
    {"date": "2026-10-01", "name": "国庆节", "kind": "holiday"},
    {"date": "2026-10-02", "name": "国庆节", "kind": "holiday"},
    {"date": "2026-10-03", "name": "国庆节", "kind": "holiday"},
    {"date": "2026-10-04", "name": "国庆节", "kind": "holiday"},
    {"date": "2026-10-05", "name": "国庆节", "kind": "holiday"},
    {"date": "2026-10-06", "name": "国庆节", "kind": "holiday"},
    {"date": "2026-10-07", "name": "国庆节", "kind": "holiday"},
    {"date": "2026-10-18", "name": "重阳节", "kind": "festival", "lunar": True},
    {"date": "2026-12-22", "name": "冬至", "kind": "festival", "lunar": True},
    # ---- 2027（按国务院惯例推算；年末公布后随版本更新）----
    {"date": "2027-01-01", "name": "元旦", "kind": "holiday"},
    {"date": "2027-02-05", "name": "除夕", "kind": "holiday", "lunar": True},
    {"date": "2027-02-06", "name": "春节", "kind": "holiday", "lunar": True},
    {"date": "2027-02-07", "name": "春节", "kind": "holiday", "lunar": True},
    {"date": "2027-02-08", "name": "春节", "kind": "holiday", "lunar": True},
    {"date": "2027-02-09", "name": "春节", "kind": "holiday", "lunar": True},
    {"date": "2027-02-25", "name": "元宵节", "kind": "festival", "lunar": True},
    {"date": "2027-04-05", "name": "清明节", "kind": "holiday", "lunar": True},
    {"date": "2027-05-01", "name": "劳动节", "kind": "holiday"},
    {"date": "2027-05-02", "name": "劳动节", "kind": "holiday"},
    {"date": "2027-05-03", "name": "劳动节", "kind": "holiday"},
    {"date": "2027-06-09", "name": "端午节", "kind": "holiday", "lunar": True},
    {"date": "2027-06-10", "name": "端午节", "kind": "holiday", "lunar": True},
    {"date": "2027-06-11", "name": "端午节", "kind": "holiday", "lunar": True},
    {"date": "2027-08-08", "name": "七夕", "kind": "festival", "lunar": True},
    {"date": "2027-09-15", "name": "中秋节", "kind": "holiday", "lunar": True},
    {"date": "2027-09-16", "name": "中秋节", "kind": "holiday", "lunar": True},
    {"date": "2027-09-17", "name": "中秋节", "kind": "holiday", "lunar": True},
    {"date": "2027-10-01", "name": "国庆节", "kind": "holiday"},
    {"date": "2027-10-02", "name": "国庆节", "kind": "holiday"},
    {"date": "2027-10-03", "name": "国庆节", "kind": "holiday"},
    {"date": "2027-10-04", "name": "国庆节", "kind": "holiday"},
    {"date": "2027-10-05", "name": "国庆节", "kind": "holiday"},
    {"date": "2027-10-06", "name": "国庆节", "kind": "holiday"},
    {"date": "2027-10-07", "name": "国庆节", "kind": "holiday"},
    {"date": "2027-11-07", "name": "重阳节", "kind": "festival", "lunar": True},
    {"date": "2027-12-22", "name": "冬至", "kind": "festival", "lunar": True},
]


def builtin_calendar() -> Calendar:
    """内置默认表（不读文件）。测试与「文件缺失兜底」共用。"""

    calendar, _ = build_calendar(BUILTIN_CALENDAR_ENTRIES, source_name="内置表")
    return calendar


def is_workday(calendar: Calendar, year: int, month: int, day: int, *, weekday: int) -> bool:
    """「日历上的工作日」：法定节假日 = 不上班，调休日 = 上班，其余按星期。"""

    info = calendar.day_info(year, month, day)
    if info.is_holiday:
        return False
    if info.is_workday_swap:
        return True
    return 1 <= int(weekday) <= 5


__all__ = [
    "BUILTIN_CALENDAR_ENTRIES",
    "Calendar",
    "DayInfo",
    "KIND_FESTIVAL",
    "KIND_HOLIDAY",
    "KIND_WORKDAY_SWAP",
    "builtin_calendar",
    "build_calendar",
    "is_workday",
    "load_calendar_file",
]
