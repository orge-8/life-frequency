# -*- coding: utf-8 -*-
"""社交经历：把「这几天聊了什么」接成她的经历（纯模块，无 ctx、无 IO、无全局随机）。

两条来源，各管一段。为什么必须两条：日记是**睡前批量生成**的，从生成到次日睡前
最长有 24 小时空档。

==============  ==========================================  ==============
来源            给出什么                                    带原文吗
==============  ==========================================  ==============
better-diary    「当天最值得写的几件事」：谁、做了什么、原话     有（已是模型摘要）
入站消息 hook    「有人找过她」这类事实                        没有
==============  ==========================================  ==============

两条都会带**情绪增量**，所以必须守住三件事（不守就会被反噬）：

1. **按会话 / 按条数收敛，不按消息条数**：群里刷屏不能让她的情绪基线被群活跃度绑架；
2. **每个生活日有总额度**（``SocialPolicy.daily_emotion_cap``）：用完就只记事、不加情绪；
3. **睡眠中照常记录，但不产生情绪增量**（``record_while_asleep`` / ``emotion_while_asleep``）
   —— 醒来时能在「近层」看到「有人叫过我」，情绪不吃这笔账。

本模块只**产出** ``recent_events`` 形状的 dict；真正落到状态上是
``life_sim.append_social_event``（状态归它管，而且它不像普通事件那样冻结情绪回归）。

**去重**：``seen`` 是调用方持有的 ``dict[str, float]``（键 → 首次入库时间），由本模块
读和写。键的命名空间：

- ``digest!<日期>!<event_id>``：某天某个选材事件已经入过库；
- ``live!<生活日>!<会话>!<kind>``：某会话在同一生活日已经算过一次「有人找我」；
- ``live!count!<生活日>``：该生活日**已产出的实时事件条数**（值是浮点数）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

try:
    from .life_events import sanitize_text
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_events import sanitize_text

#: 事件正文的字符上限，与 ``life_sim._apply_event`` 保持一致（渲染时还会再截一次）
MAX_TEXT_CHARS = 80
#: 标签的字符上限（提示词里按 24 字渲染）
MAX_LABEL_CHARS = 24
#: 「昨天的事还影响今天的心情，三天前的不该再记一笔」——它早通过情绪余波结算过了。
#: 与 ``SimConfig.afterglow_span_hours``（24 小时）同一口径。
FRESH_EMOTION_HOURS = 24.0
#: 实时信号最多攒多久（更早的信号直接丢：它们已经不在「近层」里了）
LIVE_SIGNAL_TTL_HOURS = 12.0
#: ``seen`` / 每日额度表各自最多留多少条
SEEN_KEEP = 200
DAILY_KEEP = 8

DIGEST_SOURCE = "better-diary 日记"

#: 实时信号的标签固定成一个，这样「每层同标签上限」天然把它压到每层最多 1 行
LIVE_LABEL = "有人找我"


# ---------------------------------------------------------------- 策略


@dataclass(frozen=True)
class SocialPolicy:
    """``[social]`` → 纯策略（由 plugin 组装，便于脱机单测）。"""

    digest_emotion: float = 0.4
    """一条选材事件（她今天和人聊到的事）值多少情绪。"""

    mention_emotion: float = 0.3
    """有人点名找她（群里 @ / 私聊）值多少情绪。"""

    group_emotion: float = 0.1
    """只是群里有人在聊（没点她）值多少情绪。"""

    daily_emotion_cap: float = 1.5
    """每个生活日的社交情绪总额度；用完就只记事、不加情绪。"""

    record_while_asleep: bool = True
    """睡眠中也照常记录（醒来时能在「近层」看到有人找过她）。"""

    emotion_while_asleep: bool = False
    """睡眠中**不产生情绪增量**：被找这件事记下，但情绪不吃这笔账。"""

    include_quote: bool = False
    """是否把「原话」写进经历正文。关掉更省 token、也更少外部文本。"""

    max_digest_events_per_day: int = 3
    """一天最多把几条选材事件接进经历（与 better-diary 的 max_events 同量级）。"""

    max_live_events_per_day: int = 2
    """一天最多产出几条实时事件。**必须有上界**：否则攒下的信号会挤掉她自己的生活事件。"""


# ---------------------------------------------------------------- 时间


def local_stamp_to_epoch(text: object, *, tz_offset_minutes: int) -> float:
    """把 better-diary 的 ``generated_at``（本地墙钟字符串）换成 epoch 秒。

    与 ``life_sim.local_datetime`` 互为逆运算：本地墙钟 ``L`` ↔ epoch ``L - tz``。
    解析不出来返回 ``0.0``（调用方据此丢条目，而不是猜一个时间）。
    """

    raw = str(text or "").strip()
    if not raw:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc).timestamp() - int(tz_offset_minutes) * 60
    return 0.0


# ---------------------------------------------------------------- 选材摘要


@dataclass(frozen=True)
class DigestItem:
    """better-diary 给出的「今天最值得写的一件事」。"""

    at: float
    date: str
    event_id: str
    who: str
    what: str
    quote: str

    @property
    def key(self) -> str:
        return f"digest!{self.date}!{self.event_id or self.what}"


def _text(value: object, limit: int) -> str:
    """外部文本 → 净化后的短文本（走项目统一的 ``sanitize_text``）。

    v1.8.2 修：以前这里只做空白折叠 + 截断，**不剥控制字符与结构字符**——而
    日记摘要的 what/who/quote 源自群聊原话的模型摘要（外部间接通道），带着
    ``【】「」{}`` 原样入库。当时所有消费点都有二次净化所以不可利用，但任何
    新增消费点会直接吃到伪造的分节/引用；与 ``life_world._text`` 对齐后，
    入库前就剥干净。顺带这保证 quote 内不会再出现「」⇒ ``（原话：「…」）``
    的引用边界不会被子串破坏。
    """

    return sanitize_text(value, max_chars=limit).rstrip()


@dataclass(frozen=True)
class DigestResult:
    """``get_day_digest`` 的解析结果。

    ``usable`` 区分**两种「没有内容」**（这是最容易做错的地方）：

    - ``usable=True``：对方**按契约回答了**，只是这几天没有可用的选材事件
      （还没写过日记、选材降级）。这是正常状态，不该退避、不该告警；
    - ``usable=False``：对方的回答**根本不是这个契约**（旧版本没有这个 API、
      提供方插件没装、接口报错、结构变了）。这是故障，必须退避并如实上报。
    """

    items: list[DigestItem] = field(default_factory=list)
    reason: str = ""
    usable: bool = True


def parse_digest(
    payload: object,
    *,
    tz_offset_minutes: int,
    max_per_day: int = 3,
) -> DigestResult:
    """解析 ``get_day_digest`` 的返回。

    只认契约里写明的字段，**认不出就少给条目、绝不臆造**（跨插件契约见
    better-diary README 的「给其它插件的只读接口」）。
    """

    if not isinstance(payload, Mapping):
        return DigestResult(reason=f"返回值不是对象（{type(payload).__name__}）", usable=False)
    if payload.get("success") is False:
        detail = _text(payload.get("error"), 60)
        return DigestResult(reason=f"接口报错：{detail or '未说明'}", usable=False)
    days = payload.get("days")
    if not isinstance(days, list):
        return DigestResult(reason="返回值缺少 days 列表（插件版本太旧？）", usable=False)

    limit = max(1, int(max_per_day))
    items: list[DigestItem] = []
    for day in days:
        if not isinstance(day, Mapping):
            continue
        date = _text(day.get("date"), 16)
        at = local_stamp_to_epoch(day.get("generated_at"), tz_offset_minutes=tz_offset_minutes)
        raw_items = day.get("items")
        if not isinstance(raw_items, list) or not date:
            continue
        taken = 0
        for entry in raw_items:
            if taken >= limit:
                break
            if not isinstance(entry, Mapping):
                continue
            what = _text(entry.get("what"), MAX_TEXT_CHARS)
            if not what:
                continue
            items.append(
                DigestItem(
                    at=at,
                    date=date,
                    event_id=_text(entry.get("event_id"), 40),
                    who=_text(entry.get("who"), 16),
                    what=what,
                    quote=_text(entry.get("quote"), 60),
                )
            )
            taken += 1
    if items:
        return DigestResult(items=items)
    return DigestResult(
        reason=_text(payload.get("reason"), 60) or "对方没有可用的选材事件", usable=True
    )


# ---------------------------------------------------------------- 入站信号


def flag_value(source: object, key: str) -> bool:
    """严格真值：只认**真布尔** ``True`` 与少数明确写法（``"true"`` / ``"1"`` / ``"yes"`` / ``"on"``）。

    ``bool("false")`` 是 ``True`` —— 宿主把字段序列化成字符串时，这条纪律能防止
    「没被 @ 」被误判成「被 @ 了」（better-diary 在权限判定上踩过同一个坑）。
    公开出来是因为 ``plugin.note_session`` 也要判 ``is_at``（睡眠唤醒），
    两处必须同一套口径，否则一条消息在两个模块里会得到不一样的结论。
    """

    if not isinstance(source, Mapping):
        return False
    value = source.get(key)
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on"):
        return True
    return False


def _flag(source: Mapping[str, Any], key: str) -> bool:
    return flag_value(source, key)


def _deep(source: Mapping[str, Any], *keys: str) -> Any:
    node: Any = source
    for key in keys:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def session_ids(message: object) -> tuple[str, str, str]:
    """从 Hook 载荷里取 ``(session_id, group_id, user_id)``；认不出就给空串。

    ⚠ 真机载荷是**嵌套**的：``session_id`` 在顶层，群号/QQ 号在
    ``message_info.group_info.group_id`` / ``message_info.user_info.user_id`` 下
    （宿主 ``plugin_runtime/host/message_utils.py:412-450`` 的
    ``_session_message_to_dict``）。平铺的 ``group_id`` / ``user_id`` 只是测试脚手架
    的写法——只认顶层会让真机上的群号读成空串，于是会话表的范围匹配悄悄失效。
    两个形状都认，并且**顶层优先**（插件自己写的载荷/未来版本若加了平铺键，行为不变）。
    """

    if not isinstance(message, Mapping):
        return "", "", ""
    session_id = str(message.get("session_id") or message.get("stream_id") or "").strip()
    group_id = str(
        message.get("group_id") or _deep(message, "message_info", "group_info", "group_id") or ""
    ).strip()
    user_id = str(
        message.get("user_id") or _deep(message, "message_info", "user_info", "user_id") or ""
    ).strip()
    return session_id, group_id, user_id


def live_signal(message: object, *, now: float) -> dict[str, Any] | None:
    """把一条入站消息压成**无原文**的信号；不值得记就返回 ``None``。

    这个函数会被 ``chat.receive.after_process`` 钩子调到（消息主链上），所以它
    **只做几次取值判断**：不发 RPC、不落盘、不做任何 O(n) 扫描。
    """

    if not isinstance(message, Mapping):
        return None
    session_id, group_id, _user_id = session_ids(message)
    if not session_id:
        return None
    if _flag(message, "is_command"):
        return None  # 命令不是她的人生经历（`/生活` 自己也在其中）
    body = str(message.get("processed_plain_text") or "").strip()
    mentioned = _flag(message, "is_mentioned") or _flag(message, "is_at")
    if not body and not mentioned:
        return None  # 纯图片/语音，且没人叫她 —— 不算「有人找她」
    return {
        "at": float(now),
        "session_id": session_id,
        "is_group": bool(group_id),
        "mentioned": bool(mentioned),
        "text_len": len(body),
    }


def prune_signals(
    signals: Iterable[Mapping[str, Any]], *, now: float, ttl_hours: float = LIVE_SIGNAL_TTL_HOURS
) -> list[dict[str, Any]]:
    """丢掉太旧的信号（它们已经不在「近层」里了），并保持时间升序。

    时间戳不可信的一律丢掉：``at`` 决定它落在哪一层，猜一个时间等于凭空造经历。
    （信号是本插件自己写的内存对象，出现坏时间只可能是手改/未来版本改过结构。）
    """

    span = max(0.0, float(ttl_hours)) * 3600.0
    kept: list[dict[str, Any]] = []
    for signal in signals:
        if not isinstance(signal, Mapping):
            continue
        at = _as_float(signal.get("at"), 0.0)
        if at <= 0.0 or float(now) - at > span:
            continue
        record = dict(signal)
        record["at"] = at
        kept.append(record)
    kept.sort(key=lambda signal: float(signal.get("at") or 0.0))
    return kept


# ---------------------------------------------------------------- 入库


@dataclass(frozen=True)
class IntakeContext:
    """一次 tick 的入库上下文（都由 plugin 从状态与策略里组装）。"""

    now: float
    day_key: str
    activity: str
    asleep: bool
    day_used: float
    """本生活日已经花掉的社交情绪额度。"""

    policy: SocialPolicy


@dataclass
class SocialIntake:
    """一次入库的产出：要追加的经历 + 花掉的额度（纯数据，便于断言）。"""

    events: list[dict[str, Any]] = field(default_factory=list)
    emotion_used: float = 0.0
    skipped_asleep: int = 0
    skipped_budget: int = 0
    skipped_seen: int = 0

    def __bool__(self) -> bool:
        return bool(self.events)


def _grant(
    want: float, *, left: float, asleep: bool, policy: SocialPolicy
) -> tuple[float, bool]:
    """算出这条经历实际能加多少情绪；返回 ``(增量, 是否因为额度用完被削)``。"""

    if asleep and not policy.emotion_while_asleep:
        return 0.0, False
    allowed = min(max(0.0, float(want)), max(0.0, float(left)))
    return (allowed, allowed < max(0.0, float(want)))


def _event(
    *,
    at: float,
    label: str,
    text: str,
    emotion: float,
    activity: str,
) -> dict[str, Any]:
    """``recent_events`` 的形状（与 ``life_sim._apply_event`` 的字段保持一致）。

    ``source`` 是本模块额外加的一个标记位：它让状态卡与测试能区分「她自己碰上的事」
    与「人际往来」，而 ``life_sim`` 侧的读取一律走 ``.get``，多了这个键不影响任何
    既有逻辑。
    """

    return {
        "at": float(at),
        "label": _text(label, MAX_LABEL_CHARS),
        "activity": str(activity or ""),
        "text": _text(text, MAX_TEXT_CHARS),
        "emotion": round(float(emotion), 3),
        "energy": 0.0,
        "source": "social",
    }


def intake_digest(
    items: Sequence[DigestItem],
    ctx: IntakeContext,
    seen: dict[str, float],
) -> SocialIntake:
    """把选材事件接进经历。同一条（同日期同 event_id）只入一次。"""

    result = SocialIntake()
    if not items:
        return result
    left = max(0.0, float(ctx.policy.daily_emotion_cap) - max(0.0, float(ctx.day_used)))
    fresh_span = FRESH_EMOTION_HOURS * 3600.0
    # 由远到近入库：这样「近层」在配额里排在后面时不会被远的挤掉观感
    for item in sorted(items, key=lambda entry: entry.at):
        if item.key in seen:
            result.skipped_seen += 1
            continue
        if item.at <= 0:
            continue  # 没有可信时间戳 → 不猜（它会落到哪一层完全无从判断）
        if not ctx.policy.record_while_asleep and ctx.asleep:
            result.skipped_asleep += 1
            continue
        # 只给「还新鲜」的事记情绪：更早的事早就通过情绪余波结算过了
        want = ctx.policy.digest_emotion if (ctx.now - item.at) <= fresh_span else 0.0
        emotion, _trimmed = _grant(want, left=left, asleep=ctx.asleep, policy=ctx.policy)
        if want > 0 and emotion <= 0:
            result.skipped_budget += 1
        left = max(0.0, left - emotion)
        result.emotion_used += emotion
        seen[item.key] = float(ctx.now)
        label = f"和{item.who}聊天" if item.who else "听人聊到的事"
        body = item.what
        if ctx.policy.include_quote and item.quote:
            body = f"{body}（原话：「{item.quote}」）"
        result.events.append(
            _event(
                at=item.at,
                label=label,
                text=body,
                emotion=emotion,
                activity=ctx.activity,
            )
        )
    return result


def intake_live(
    signals: Sequence[Mapping[str, Any]],
    ctx: IntakeContext,
    seen: dict[str, float],
) -> SocialIntake:
    """把入站信号聚合成**至多一条**「有人找我」。

    聚合而不是逐条记：她的一天不需要 41 条「有人说话」，而 ``recent_events`` 只有
    40 个位置 —— 逐条记会把她自己的生活事件挤出去。
    """

    result = SocialIntake()
    if not ctx.policy.record_while_asleep and ctx.asleep:
        result.skipped_asleep = len(signals)
        return result

    count_key = f"live!count!{ctx.day_key}"
    produced = int(seen.get(count_key, 0.0))
    if produced >= max(0, int(ctx.policy.max_live_events_per_day)):
        result.skipped_seen = len(signals)
        return result

    fresh: list[Mapping[str, Any]] = []
    for signal in signals:
        session_id = str(signal.get("session_id") or "")
        if not session_id:
            continue
        kind = "mention" if signal.get("mentioned") else "talk"
        key = f"live!{ctx.day_key}!{session_id}!{kind}"
        if key in seen:
            result.skipped_seen += 1
            continue
        fresh.append(signal)
        seen[key] = float(ctx.now)
    if not fresh:
        return result

    mentions = sum(1 for signal in fresh if signal.get("mentioned"))
    sessions = len({str(signal.get("session_id")) for signal in fresh})
    privates = len(
        {
            str(signal.get("session_id"))
            for signal in fresh
            if not signal.get("is_group")
        }
    )
    if mentions:
        where = "私聊里" if privates and privates == sessions else "群里"
        text = f"{where}有人叫我"
        if sessions > 1:
            text += f"（{sessions} 个会话）"
        want = ctx.policy.mention_emotion + ctx.policy.group_emotion * max(0, sessions - 1)
    else:
        text = f"{sessions} 个会话有人在聊"
        want = ctx.policy.group_emotion * max(1, sessions)

    left = max(0.0, float(ctx.policy.daily_emotion_cap) - max(0.0, float(ctx.day_used)))
    emotion, _ = _grant(want, left=left, asleep=ctx.asleep, policy=ctx.policy)
    if want > 0 and emotion <= 0:
        result.skipped_budget += 1
    seen[count_key] = float(produced + 1)
    result.emotion_used += emotion
    result.events.append(
        _event(
            at=max(float(signal.get("at") or 0.0) for signal in fresh),
            label=LIVE_LABEL,
            text=text,
            emotion=emotion,
            activity=ctx.activity,
        )
    )
    return result


# ---------------------------------------------------------------- 维护


def prune_seen(seen: dict[str, float], *, keep: int = SEEN_KEEP) -> None:
    """把去重表收到最近 ``keep`` 条（按时间戳），防止状态文件无限增长。"""

    limit = max(1, int(keep))
    if len(seen) <= limit:
        return
    ordered = sorted(seen.items(), key=lambda pair: _as_float(pair[1]), reverse=True)
    for key, _ in ordered[limit:]:
        seen.pop(key, None)


def prune_daily(daily: dict[str, float], *, keep: int = DAILY_KEEP) -> None:
    """每日额度表只留最近 ``keep`` 个生活日（键是 ISO 日期，直接按字符串排序）。"""

    limit = max(1, int(keep))
    if len(daily) <= limit:
        return
    for key in sorted(daily, reverse=True)[limit:]:
        daily.pop(key, None)


def sanitize_seen(seen: object) -> dict[str, float]:
    """状态文件里的去重表可能被手改成任何东西 —— 坏条目一律丢掉。

    只收**真正的数字**：``"abc"`` 用 ``float()`` 会抛、``"1.5"`` 说明文件被改过、
    ``bool`` 是 ``int`` 的子类要单独排除。一个字都不猜（与 ``life_sim`` 的
    ``_sanitize_adjust_map`` 同一条原则）。
    """

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


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


# ---------------------------------------------------------------- 展示


@dataclass(frozen=True)
class SocialStatus:
    """取数结果。``ok=False`` 时 ``reason`` / ``detail`` 说明为什么没有。"""

    ok: bool = False
    reason: str = ""
    detail: str = ""
    fetched_at: float = 0.0
    latest_day: str = ""
    latest_generated_at: str = ""
    item_count: int = 0

    def label(self) -> str:
        return self.latest_day or "—"


def unavailable(reason: str, detail: str = "", *, fetched_at: float = 0.0) -> SocialStatus:
    return SocialStatus(ok=False, reason=reason, detail=detail, fetched_at=fetched_at)


def social_lines(
    status: SocialStatus | None,
    *,
    enabled: bool,
    today_events: int = 0,
    emotion_used: float = 0.0,
    daily_cap: float = 0.0,
    now: float = 0.0,
) -> list[str]:
    """``/生活`` 卡片上的社交几行。**「没接上」绝不能看起来像「没问题」。**"""

    if not enabled:
        return ["社交经历：未接入（[social] enabled = false）"]
    head = "社交经历"
    if status is None or not status.ok:
        reason = (status.reason if status is not None else "") or "还没取过"
        detail = f"（{status.detail}）" if status is not None and status.detail else ""
        return [f"{head}：未取到 —— {reason}{detail}"]
    tally = f"　今日已记 {today_events} 条（情绪额度 {emotion_used:.1f}/{daily_cap:.1f}）"
    if not status.item_count:
        # 「接通了，但这几天没有内容」也要说清楚为什么（还没写日记 / 选材降级），
        # 不能显示成「最新  —」那种看不懂的样子。
        why = status.reason or "对方还没有可用的日记"
        return [f"{head}：{DIGEST_SOURCE} 已接入，但暂时没有内容（{why}）", tally]
    age = ""
    if status.fetched_at and now:
        minutes = max(0.0, (float(now) - float(status.fetched_at)) / 60.0)
        age = f" · {minutes:.0f} 分钟前取到"
    return [
        f"{head}：{DIGEST_SOURCE} 已接入（最新 {status.latest_day} "
        f"{status.latest_generated_at[-8:]}）{age}",
        tally,
    ]
