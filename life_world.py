# -*- coding: utf-8 -*-
"""外部世界维度：把四个上游插件的只读状态变成她的「外面的世界」经历。

> **本文件是步骤 5 的暂存件**：life-frequency 仓库正在等插件中心 approve，
> 在批准前不往仓库里写任何东西。批准后把本文件落到 life-frequency/life_world.py
> 即可（它与仓库里的 life_events/life_social 同级，导入方式完全一致）。

数据源（全部 ``version="1"``、只读、零网络零写盘，契约见各上游 README）：

    bilibili-live-gateway  get_live_status       → UP 主开播了
    bilibili-dynamic-push  get_recent_pushes     → UP 主发了新视频 / 新动态
    group-welcome          get_recent_newcomers  → 群里来了个新人
    cv_lyric_context       get_recent_songs      → 有人聊到了《X》
    bilibili-dynamic-push  get_subscriptions     → 只用于状态卡（订阅了谁），不产事件

流程（**每个源独立降级**：任一源坏掉只少一类事件，不影响其他源，更不影响生活推进）：

    payload(dict) ──parse_*──▶ XxxSource(ok/active/reason/items)
    sources + WorldPolicy ──world_events──▶ list[WorldEvent]（kind/label/emotion/energy/at/key）
    events + seen ──intake_world──▶ WorldIntake（recent_events 形状的 dict + 跳过计数）

**去重键**（``seen`` 是调用方持有的 ``dict[str, float]``，键 → 首次入库时间）：

- ``live!<房间号>!<生活日>``：某房间同一生活日只记一次开播。
  上游只给「当前是否开播」与「最近一次成功探测时刻」，**没有开播开始时刻**，
  所以按生活日去重（连续多天开播则每天各记一次）——这是数据边界，不是漏做。
- ``push!<url>``：一条动态只记一次（url 内含动态 ID，跨轮询稳定）。
- ``newcomer!<群号>!<QQ号>``：一个新人只记一次。
- ``song!<歌名>!<命中时刻>``：**一次命中**只记一次（同一天反复聊到同一首歌会各记一次，
  这正是想要的：它们是不同的发生）。用命中时刻而不是歌名当身份。

两个纪律（与 life_social 同源）：

1. **总量有上界**：每个生活日最多接进 ``max_events_per_day`` 条，情绪另有 ``daily_emotion_cap``；
   外部世界不能挤掉她自己的生活事件；
2. **外部文本一律走 ``sanitize_text`` + 长度上限**：歌名/UP 名/标题都来自外部，
   进经历前先去控制字符与结构字符（防提示词注入），标签本身用空格/《》而非「」，
   避免被渲染期 sanitize 剥掉后读起来像断句。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

try:
    from .life_events import sanitize_text
    from .life_relations import mask_user_id
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_events import sanitize_text
    from life_relations import mask_user_id

# ---------------------------------------------------------------- 常量

#: 事件正文的字符上限（与 life_social / life_sim 同一口径）
MAX_TEXT_CHARS = 80
#: 标签的字符上限（提示词里按 24 字渲染）
MAX_LABEL_CHARS = 24
#: 外部字段（UP 名 / 歌名 / 群号）的单字段上限
MAX_NAME_CHARS = 24
#: 「昨天的事还影响今天的心情」——超过这个跨度的事不再给情绪（会由情绪余波结算）
FRESH_EMOTION_HOURS = 24.0
#: 去重表最多留多少条
WORLD_SEEN_KEEP = 400
#: 每个源单轮最多取几条（对方 API 自己也有上限，这里再兜一层）
DEFAULT_MAX_ITEMS_PER_SOURCE = 10

KIND_LIVE = "up_live"
KIND_VIDEO = "up_video"
KIND_NEWCOMER = "newcomer"
KIND_SONG = "song"

#: 上游 dynamic-push 的动态类型里，哪些算「视频」
VIDEO_DYN_TYPES = ("DYNAMIC_TYPE_AV", "DYNAMIC_TYPE_VIDEO")

LIVE_LABEL_TAIL = "开播了"
NEWCOMER_LABEL = "群里来了个新人"


# ---------------------------------------------------------------- 安全取值


def _finite(value: Any, fallback: float = 0.0) -> float:
    """任意输入 → 有限浮点；坏值（nan/inf/字符串/怪对象）一律用兜底值。"""

    try:
        number = float(value)
    except Exception:  # noqa: BLE001 —— 负载来自另一个插件的 msgpack 返回值
        return fallback
    return number if math.isfinite(number) else fallback


def _text(value: Any, limit: int = MAX_NAME_CHARS) -> str:
    """外部文本 → 净化后的短文本（走项目统一的 sanitize_text）。"""

    try:
        return sanitize_text(value, max_chars=limit)
    except Exception:  # noqa: BLE001 —— 负载来自另一个插件的 msgpack 返回值
        return ""


_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})


def _strict_bool(value: Any) -> bool:
    """严格真值：只认**真布尔**与少数明确写法（与 ``life_social.flag_value`` 同一口径）。

    v1.8.2 修：``active`` / ``is_live`` 以前用 ``bool(payload.get(...))``——
    ``bool("false")`` 是 ``True``，上游把 bool 序列化成字符串时「未开播」会被
    判成「开播了」，凭空产出假事件。本插件自己在 ``flag_value`` 文档化过这个坑，
    这里对齐同一套口径。
    """

    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() in _TRUE_STRINGS


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except Exception:  # noqa: BLE001
        return None
    return number


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


# ---------------------------------------------------------------- 策略


@dataclass(frozen=True)
class WorldPolicy:
    """``[world]`` → 纯策略（由 plugin 组装，便于脱机单测）。"""

    live_enabled: bool = True
    video_enabled: bool = True
    newcomer_enabled: bool = True
    song_enabled: bool = True

    live_emotion: float = 0.20
    video_emotion: float = 0.15
    newcomer_emotion: float = 0.10
    song_emotion: float = 0.12

    daily_emotion_cap: float = 1.0
    """每个生活日「外面的世界」能给她多少情绪；用完只记事、不加情绪。"""

    max_events_per_day: int = 6
    """每个生活日最多接进几条世界事件（必须有上界，防挤掉她自己的生活事件）。"""

    record_while_asleep: bool = True
    """睡眠中也照常记录（醒来时能在近层看到「外面发生过什么」）。"""

    emotion_while_asleep: bool = False
    """睡眠中不产生情绪增量。"""

    newcomer_window_seconds: int = 86400
    """向 group-welcome 要多久以内的新人（默认 24 小时）。"""

    max_items_per_source: int = DEFAULT_MAX_ITEMS_PER_SOURCE

    def normalized(self) -> "WorldPolicy":
        """收口非法数值：情绪非负、上限至少 1、窗口至少 1 小时。"""

        return WorldPolicy(
            live_enabled=bool(self.live_enabled),
            video_enabled=bool(self.video_enabled),
            newcomer_enabled=bool(self.newcomer_enabled),
            song_enabled=bool(self.song_enabled),
            live_emotion=max(0.0, _finite(self.live_emotion, 0.20)),
            video_emotion=max(0.0, _finite(self.video_emotion, 0.15)),
            newcomer_emotion=max(0.0, _finite(self.newcomer_emotion, 0.10)),
            song_emotion=max(0.0, _finite(self.song_emotion, 0.12)),
            daily_emotion_cap=max(0.0, _finite(self.daily_emotion_cap, 1.0)),
            max_events_per_day=max(1, int(_finite(self.max_events_per_day, 6))),
            record_while_asleep=bool(self.record_while_asleep),
            emotion_while_asleep=bool(self.emotion_while_asleep),
            newcomer_window_seconds=max(3600, int(_finite(self.newcomer_window_seconds, 86400))),
            max_items_per_source=min(50, max(1, int(_finite(self.max_items_per_source, 10)))),
        )


# ---------------------------------------------------------------- 解析：通用


def _base_ok(payload: Any) -> tuple[bool, str, str]:
    """校验所有上游 API 的共同形状，返回 ``(是否可继续解析, reason, error)``。

    ⚠ 跨插件调用失败时 SDK **不抛异常**：Host 返回 ``{"success": False, "error": ...}``，
    SDK 只对带 ``result`` 键的成功包装解包（``maibot_sdk/context.py``），
    失败字典会原样落进来。必须先判它，否则会把 error 文本当数据。
    """

    if not isinstance(payload, Mapping):
        return False, "bad_payload", f"返回类型 {type(payload).__name__} 不是字典"
    if payload.get("success") is False:
        return False, "api_error", _text(payload.get("error") or "API 调用失败", 200)
    if "schema_version" not in payload:
        return False, "bad_payload", "缺 schema_version（不是本插件认识的契约）"
    return True, "", ""


def _reason_of(payload: Mapping[str, Any]) -> str:
    """上游的 ``reason``：空串 = 数据正常，非空 = 降级原因（照样解析已有条目）。"""

    return _text(payload.get("reason") or "", 120)


def _active_of(payload: Mapping[str, Any]) -> bool:
    # v1.8.2 修：改用严格真值（bool("false") is True 的坑，见 _strict_bool）
    return _strict_bool(payload.get("active"))


# ---------------------------------------------------------------- 解析：开播


@dataclass(frozen=True)
class LiveStatus:
    """``get_live_status`` 的解析结果（``ok=False`` 时只有 reason/error 有意义）。"""

    ok: bool = False
    active: bool = False
    reason: str = ""
    error: str = ""
    room_id: int | None = None
    anchor_name: str = ""
    is_live: bool = False
    live_status: int | None = None
    live_status_name: str = ""
    last_ok_at: float = 0.0
    updated_at: float = 0.0
    fetched_at: float = 0.0


def parse_live_status(payload: Any, *, fetched_at: float = 0.0) -> LiveStatus:
    """把 ``get_live_status`` 的返回值解析成快照；任何形状异常都 ``ok=False``。"""

    fetched = _finite(fetched_at, 0.0)
    ok, reason, error = _base_ok(payload)
    if not ok:
        return LiveStatus(reason=reason, error=error, fetched_at=fetched)

    return LiveStatus(
        ok=True,
        active=_active_of(payload),
        reason=_reason_of(payload),
        error="",
        room_id=_int_or_none(payload.get("room_id")),
        anchor_name=_text(payload.get("anchor_name")),
        is_live=_strict_bool(payload.get("is_live")),
        live_status=_int_or_none(payload.get("live_status")),
        live_status_name=_text(payload.get("live_status_name"), 12),
        last_ok_at=_finite(payload.get("last_ok_at"), 0.0),
        updated_at=_finite(payload.get("updated_at"), 0.0),
        fetched_at=fetched,
    )


# ---------------------------------------------------------------- 解析：动态推送


@dataclass(frozen=True)
class PushItem:
    uid: str = ""
    name: str = ""
    dyn_type: str = ""
    title: str = ""
    url: str = ""
    at: float = 0.0

    def is_video(self) -> bool:
        return self.dyn_type in VIDEO_DYN_TYPES


@dataclass(frozen=True)
class PushSource:
    ok: bool = False
    active: bool = False
    reason: str = ""
    error: str = ""
    items: tuple[PushItem, ...] = ()
    fetched_at: float = 0.0


def parse_recent_pushes(
    payload: Any,
    *,
    fetched_at: float = 0.0,
    limit: int = DEFAULT_MAX_ITEMS_PER_SOURCE,
) -> PushSource:
    """把 ``get_recent_pushes`` 的返回值解析成条目列表（坏条目跳过，不整体失败）。"""

    fetched = _finite(fetched_at, 0.0)
    ok, reason, error = _base_ok(payload)
    if not ok:
        return PushSource(reason=reason, error=error, fetched_at=fetched)

    raw_items = payload.get("pushes")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        return PushSource(
            ok=True, active=_active_of(payload), reason="bad_payload" if raw_items is not None else _reason_of(payload),
            error="pushes 不是列表", fetched_at=fetched,
        )

    cap = min(50, max(1, int(_finite(limit, DEFAULT_MAX_ITEMS_PER_SOURCE))))
    items: list[PushItem] = []
    for entry in raw_items[:cap]:
        if not isinstance(entry, Mapping):
            continue
        at = _finite(entry.get("at"), 0.0)
        items.append(
            PushItem(
                uid=_text(entry.get("uid"), 24),
                name=_text(entry.get("name")),
                dyn_type=_text(entry.get("dyn_type"), 32),
                title=_text(entry.get("title"), MAX_TEXT_CHARS),
                url=_text(entry.get("url"), 160),
                at=at,
            )
        )
    return PushSource(
        ok=True, active=_active_of(payload), reason=_reason_of(payload),
        items=tuple(items), fetched_at=fetched,
    )


# ---------------------------------------------------------------- 解析：新人


@dataclass(frozen=True)
class Newcomer:
    group_id: str = ""
    user_id: str = ""
    first_seen: float = 0.0
    last_welcome_at: float = 0.0


@dataclass(frozen=True)
class NewcomerSource:
    ok: bool = False
    active: bool = False
    reason: str = ""
    error: str = ""
    items: tuple[Newcomer, ...] = ()
    fetched_at: float = 0.0


def parse_recent_newcomers(
    payload: Any,
    *,
    fetched_at: float = 0.0,
    limit: int = DEFAULT_MAX_ITEMS_PER_SOURCE,
) -> NewcomerSource:
    """把 ``get_recent_newcomers`` 的返回值解析成 (群, 成员) 列表。"""

    fetched = _finite(fetched_at, 0.0)
    ok, reason, error = _base_ok(payload)
    if not ok:
        return NewcomerSource(reason=reason, error=error, fetched_at=fetched)

    groups = payload.get("groups")
    if groups is None:
        groups = {}
    if not isinstance(groups, Mapping):
        return NewcomerSource(
            ok=True, active=_active_of(payload), reason="bad_payload",
            error="groups 不是字典", fetched_at=fetched,
        )

    cap = min(50, max(1, int(_finite(limit, DEFAULT_MAX_ITEMS_PER_SOURCE))))
    items: list[Newcomer] = []
    for group_id, entry in groups.items():
        if not isinstance(entry, Mapping):
            continue
        welcome_at = _finite(entry.get("last_welcome_at"), 0.0)
        members = entry.get("new_members")
        if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
            continue
        for member in members[:cap]:
            if not isinstance(member, Mapping):
                continue
            user_id = _text(member.get("user_id"), 24)
            if not user_id:
                continue
            items.append(
                Newcomer(
                    group_id=_text(group_id, 24),
                    user_id=user_id,
                    first_seen=_finite(member.get("first_seen"), 0.0),
                    last_welcome_at=welcome_at,
                )
            )
    return NewcomerSource(
        ok=True, active=_active_of(payload), reason=_reason_of(payload),
        items=tuple(items), fetched_at=fetched,
    )


# ---------------------------------------------------------------- 解析：歌曲


@dataclass(frozen=True)
class SongItem:
    name: str = ""
    artist: str = ""
    at: float = 0.0


@dataclass(frozen=True)
class SongSource:
    ok: bool = False
    active: bool = False
    reason: str = ""
    error: str = ""
    items: tuple[SongItem, ...] = ()
    fetched_at: float = 0.0


def parse_recent_songs(
    payload: Any,
    *,
    fetched_at: float = 0.0,
    limit: int = DEFAULT_MAX_ITEMS_PER_SOURCE,
) -> SongSource:
    """把 ``get_recent_songs`` 的返回值解析成条目列表。歌名空的条目直接丢（无从渲染）。"""

    fetched = _finite(fetched_at, 0.0)
    ok, reason, error = _base_ok(payload)
    if not ok:
        return SongSource(reason=reason, error=error, fetched_at=fetched)

    raw_items = payload.get("songs")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        return SongSource(
            ok=True, active=_active_of(payload), reason="bad_payload",
            error="songs 不是列表", fetched_at=fetched,
        )

    cap = min(50, max(1, int(_finite(limit, DEFAULT_MAX_ITEMS_PER_SOURCE))))
    items: list[SongItem] = []
    for entry in raw_items[:cap]:
        if not isinstance(entry, Mapping):
            continue
        name = _text(entry.get("name"))
        if not name:
            continue
        items.append(
            SongItem(name=name, artist=_text(entry.get("artist"), 40), at=_finite(entry.get("at"), 0.0))
        )
    return SongSource(
        ok=True, active=_active_of(payload), reason=_reason_of(payload),
        items=tuple(items), fetched_at=fetched,
    )


# ---------------------------------------------------------------- 解析：订阅（仅展示）


@dataclass(frozen=True)
class SubscriptionSource:
    ok: bool = False
    active: bool = False
    reason: str = ""
    error: str = ""
    count: int = 0
    names: tuple[str, ...] = ()
    fetched_at: float = 0.0


def parse_subscriptions(payload: Any, *, fetched_at: float = 0.0) -> SubscriptionSource:
    """把 ``get_subscriptions`` 的返回值解析成「订阅了谁」（只给状态卡用，不产事件）。"""

    fetched = _finite(fetched_at, 0.0)
    ok, reason, error = _base_ok(payload)
    if not ok:
        return SubscriptionSource(reason=reason, error=error, fetched_at=fetched)

    raw_items = payload.get("up")
    items = raw_items if isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)) else ()
    names: list[str] = []
    for entry in items:
        if isinstance(entry, Mapping):
            label = _text(entry.get("name"), MAX_LABEL_CHARS) or _text(entry.get("uid"), 24)
            if label:
                names.append(label)
    return SubscriptionSource(
        ok=True, active=_active_of(payload), reason=_reason_of(payload),
        count=max(0, int(_finite(payload.get("count"), len(names)))),
        names=tuple(names), fetched_at=fetched,
    )


# ---------------------------------------------------------------- 世界事件


@dataclass(frozen=True)
class WorldEvent:
    """规范化后的「外面的世界」事件（纯数据，便于断言）。"""

    kind: str
    label: str
    text: str = ""
    emotion: float = 0.0
    energy: float = 0.0
    at: float = 0.0
    key: str = ""


def _day_key(now: float, *, tz_offset_minutes: int = 0) -> str:
    """生活日（本地日历日）。与 life_sim 的 day_key 同口径：epoch + 时区偏移后取日期。"""

    import datetime as _dt

    stamp = _finite(now, 0.0) + float(tz_offset_minutes) * 60.0
    return _dt.datetime.fromtimestamp(stamp, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


def world_events(
    *,
    live: LiveStatus | None = None,
    pushes: PushSource | None = None,
    newcomers: NewcomerSource | None = None,
    songs: SongSource | None = None,
    policy: WorldPolicy | None = None,
    now: float = 0.0,
    tz_offset_minutes: int = 0,
    day_key: str = "",
) -> list[WorldEvent]:
    """把各源解析结果规范化成世界事件（**不去重、不看额度**，那是 intake_world 的事）。

    ``day_key`` 可由调用方传 ``life_sim.day_key_of(...)`` 的结果，保证「生活日」
    与每日额度、作息班表用的是同一个定义；留空则按 ``tz_offset_minutes`` 现算。
    """

    cfg = (policy or WorldPolicy()).normalized()
    day = day_key or _day_key(now, tz_offset_minutes=tz_offset_minutes)
    out: list[WorldEvent] = []

    if cfg.live_enabled and live is not None and live.ok and live.active and live.is_live:
        name = live.anchor_name or (f"房间{live.room_id}" if live.room_id is not None else "关注的 UP 主")
        out.append(
            WorldEvent(
                kind=KIND_LIVE,
                label=_text(f"{name} {LIVE_LABEL_TAIL}", MAX_LABEL_CHARS),
                text=_text(live.live_status_name or "直播中", MAX_TEXT_CHARS),
                emotion=cfg.live_emotion,
                energy=0.0,
                at=live.last_ok_at or live.updated_at,
                key=f"live!{live.room_id}!{day}",
            )
        )

    if cfg.video_enabled and pushes is not None and pushes.ok:
        for item in pushes.items:
            if item.at <= 0:
                continue
            name = item.name or item.uid or "关注的 UP 主"
            tail = "发了新视频" if item.is_video() else "发了新动态"
            key = f"push!{item.url}" if item.url else f"push!{item.uid}!{item.at:.0f}"
            out.append(
                WorldEvent(
                    kind=KIND_VIDEO,
                    label=_text(f"{name} {tail}", MAX_LABEL_CHARS),
                    text=item.title,
                    emotion=cfg.video_emotion,
                    energy=0.0,
                    at=item.at,
                    key=key,
                )
            )

    if cfg.newcomer_enabled and newcomers is not None and newcomers.ok:
        for item in newcomers.items:
            if item.first_seen <= 0:
                continue
            out.append(
                WorldEvent(
                    kind=KIND_NEWCOMER,
                    label=NEWCOMER_LABEL,
                    text=_text(
                        # v1.13.1（F-001，安全审计）：文案**不携带 QQ 号与群号明文**——
                        # 这条文本会进 recent_events → 活动决策 prompt（可能发往第三方
                        # 模型服务）→ 明文落盘 life_state.json。第三方群成员的标识
                        # 不该离开本机；标识只留在去重 key（本进程内）里。
                        f"群里来了新成员 {mask_user_id(item.user_id)}",
                        MAX_TEXT_CHARS,
                    ),
                    emotion=cfg.newcomer_emotion,
                    energy=0.0,
                    at=item.first_seen,
                    key=f"newcomer!{item.group_id}!{item.user_id}",
                )
            )

    if cfg.song_enabled and songs is not None and songs.ok:
        for item in songs.items:
            if item.at <= 0:
                continue
            body = f"{item.name} — {item.artist}" if item.artist else item.name
            out.append(
                WorldEvent(
                    kind=KIND_SONG,
                    label=_text(f"有人聊到了《{item.name}》", MAX_LABEL_CHARS),
                    text=_text(body, MAX_TEXT_CHARS),
                    emotion=cfg.song_emotion,
                    energy=0.0,
                    at=item.at,
                    key=f"song!{item.name}!{item.at:.0f}",
                )
            )

    out.sort(key=lambda event: event.at)
    return out


# ---------------------------------------------------------------- 入库


@dataclass(frozen=True)
class WorldContext:
    """一次 tick 的入库上下文（由 plugin 从状态与策略组装）。"""

    now: float
    activity: str
    asleep: bool
    day_used: float
    """本生活日已经花掉的世界情绪额度。"""

    day_events: int = 0
    """本生活日已经接进几条世界事件（用于 max_events_per_day）。"""

    policy: WorldPolicy = field(default_factory=WorldPolicy)


@dataclass
class WorldIntake:
    """一次入库的产出：要追加的经历 + 花掉的额度（纯数据，便于断言）。"""

    events: list[dict[str, Any]] = field(default_factory=list)
    emotion_used: float = 0.0
    skipped_seen: int = 0
    skipped_asleep: int = 0
    skipped_budget: int = 0
    skipped_cap: int = 0
    skipped_stale: int = 0

    def __bool__(self) -> bool:
        return bool(self.events)


def _event(
    *,
    at: float,
    label: str,
    text: str,
    emotion: float,
    activity: str,
) -> dict[str, Any]:
    """``recent_events`` 的形状（与 life_sim._apply_event / life_social._event 一致）。

    ``source="world"`` 让状态卡与测试能区分「她自己碰上的事 / 人际往来 / 外面的世界」，
    life_sim 侧一律走 ``.get``，多这个键不影响既有逻辑。
    """

    return {
        "at": float(at),
        "label": _text(label, MAX_LABEL_CHARS),
        "activity": str(activity or ""),
        "text": _text(text, MAX_TEXT_CHARS),
        "emotion": round(float(emotion), 3),
        "energy": 0.0,
        "source": "world",
    }


def intake_world(
    events: Sequence[WorldEvent],
    ctx: WorldContext,
    seen: dict[str, float],
) -> WorldIntake:
    """把世界事件接进经历：去重、睡眠策略、每日条数与情绪额度，都在这里收口。"""

    policy = (ctx.policy or WorldPolicy()).normalized()
    result = WorldIntake()
    if not events:
        return result

    left_emotion = max(0.0, policy.daily_emotion_cap - max(0.0, _finite(ctx.day_used, 0.0)))
    room = max(0, policy.max_events_per_day - max(0, int(_finite(ctx.day_events, 0))))
    fresh_span = FRESH_EMOTION_HOURS * 3600.0

    for event in sorted(events, key=lambda item: item.at):
        if event.key and event.key in seen:
            result.skipped_seen += 1
            continue
        if event.at <= 0:
            result.skipped_stale += 1
            continue
        if room <= 0:
            result.skipped_cap += 1
            continue
        if not policy.record_while_asleep and ctx.asleep:
            result.skipped_asleep += 1
            continue

        want = event.emotion if (_finite(ctx.now, 0.0) - event.at) <= fresh_span else 0.0
        if want > 0 and ctx.asleep and not policy.emotion_while_asleep:
            want = 0.0
        emotion = min(max(0.0, _finite(want, 0.0)), left_emotion)
        if want > 0 and emotion <= 0:
            result.skipped_budget += 1

        left_emotion = max(0.0, left_emotion - emotion)
        result.emotion_used += emotion
        room -= 1
        if event.key:
            seen[event.key] = float(_finite(ctx.now, 0.0))
        result.events.append(
            _event(
                at=event.at,
                label=event.label,
                text=event.text,
                emotion=emotion,
                activity=ctx.activity,
            )
        )
    return result


# ---------------------------------------------------------------- 维护


def prune_world_seen(seen: dict[str, float], *, keep: int = WORLD_SEEN_KEEP) -> None:
    """把去重表收到最近 ``keep`` 条（只记首次入库时间，防状态文件无限增长）。"""

    limit = max(1, int(keep))
    if len(seen) <= limit:
        return
    ordered = sorted(seen.items(), key=lambda pair: _as_float(pair[1]), reverse=True)
    for key, _ in ordered[limit:]:
        seen.pop(key, None)


def sanitize_world_seen(seen: object) -> dict[str, float]:
    """状态文件里的去重表被手改成任何东西都不怕：只收真正的有限数字。"""

    if not isinstance(seen, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, value in seen.items():
        if not isinstance(key, str) or not key:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if math.isfinite(number):
            out[key] = number
    return out


# ---------------------------------------------------------------- 展示


@dataclass(frozen=True)
class WorldStatus:
    """状态卡用的一行摘要（每个源一行，坏源显示原因而不是静默消失）。"""

    live: LiveStatus | None = None
    pushes: PushSource | None = None
    newcomers: NewcomerSource | None = None
    songs: SongSource | None = None
    subscriptions: SubscriptionSource | None = None


#: 上游在「健康、但确实暂无数据」时会照契约回一个说明性 ``reason``（例如 ``暂无推送记录``）。
#: 这**不是劣化**，不该在状态卡上写成「降级」——真机上就出现过「动态：降级（暂无推送记录）」
#: 这种把"还没数据"说成"坏了"的误读。只认四个上游在契约里承诺的空窗措辞；
#: 出现未知措辞时一律按「降级」显示（宁可误报，不可漏报）。
BENIGN_EMPTY_REASONS: frozenset[str] = frozenset(
    {
        "暂无推送记录",  # bilibili-dynamic-push.get_recent_pushes
        "时间窗内没有推送记录",  # bilibili-dynamic-push.get_recent_pushes
        "暂无任何成员记录",  # group-welcome.get_recent_newcomers
        "时间窗内没有新成员",  # group-welcome.get_recent_newcomers
        "暂无歌曲命中记录",  # cv_lyric_context.get_recent_songs
    }
)


def _source_state(
    ok: bool | None,
    active: bool,
    reason: str,
    error: str,
    empty: str,
    *,
    benign_empty: bool = False,
) -> str:
    """一行数据源状态：**「没接上」绝不能看起来像「没问题」**，反之亦然。"""

    if ok is None:
        return "未取过"
    if not ok:
        return f"未取到（{error or '不可用'}）"
    if not active:
        return f"上游未启用（{reason or '未就绪'}）"
    if reason:
        return f"已接入（{reason}）" if benign_empty else f"降级（{reason}）"
    return empty


def world_lines(
    status: WorldStatus | None,
    *,
    enabled: bool,
    today_events: int = 0,
    emotion_used: float = 0.0,
    daily_cap: float = 0.0,
) -> list[str]:
    """``/生活`` 卡片上的「外面的世界」几行。**「没接上」绝不能看起来像「没问题」。**"""

    if not enabled:
        return ["外面的世界：未接入（[world] enabled = false）"]
    head = "外面的世界"
    if status is None:
        return [f"{head}：未取过"]
    tally = f"　今日已记 {today_events} 条（情绪额度 {emotion_used:.1f}/{daily_cap:.1f}）"
    lines: list[str] = []
    subs = status.subscriptions
    if subs is not None and subs.ok and subs.active:
        lines.append(f"{head}：订阅 {subs.count} 位 UP 主" + (f"（{'、'.join(subs.names[:3])}…）" if subs.names else ""))
    live = status.live
    if live is None or not live.ok:
        lines.append(f"　直播：{_source_state(None if live is None else live.ok, False, '', '' if live is None else live.error, '')}")
    elif not live.active:
        lines.append(f"　直播：上游未就绪（{live.reason or '未配置房间'}）")
    elif live.reason:
        lines.append(f"　直播：降级（{live.reason}）")
    else:
        state = "直播中" if live.is_live else "未开播"
        lines.append(f"　直播：{live.anchor_name or live.room_id} {state}（{live.live_status_name}）")
    for label, src, empty in (
        ("动态", status.pushes, "已接入（暂无推送记录）"),
        ("新人", status.newcomers, "已接入（暂无新人记录）"),
        ("歌曲", status.songs, "已接入（暂无命中记录）"),
    ):
        ok = None if src is None else src.ok
        active = bool(src is not None and src.active)
        reason = "" if src is None else src.reason
        error = "" if src is None else src.error
        count = len(src.items) if src is not None else 0
        # 只有「没条目 + 上游明确说是空窗」才算健康；带条目的 reason 与未知措辞都仍按降级显示
        benign = count == 0 and reason in BENIGN_EMPTY_REASONS
        lines.append(
            f"　{label}：{_source_state(ok, active, reason, error, f'已接入（{count} 条）' if count else empty, benign_empty=benign)}"
        )
    lines.append(tally)
    return lines
