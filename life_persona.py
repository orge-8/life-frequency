# -*- coding: utf-8 -*-
"""人设化的冷场话头（v1.23.0）：让「她抛什么话题」与这个人设有关（纯模块）。

**为什么需要它**：冷群话头原先是 `life_topic.TOPIC_POOL` 里一张静态表，按
``sha1(会话|生活日|时段)`` 抽签——「最近有什么在追的番吗」这种句子对任何 bot 都成立，
也就等于**不成立**。人设只在「活动推演」那条链路上用过（``[activity.llm].persona_max_chars``），
主动开口完全没吃它。

**做法**：按「人设 + 生活日 + 时段段」生成一小把**能被回答的问句**并缓存。

- 缓存键含 ``persona_hash``：改了人设立刻失效，不会继续用旧人设的问句。
- 生成失败/关掉/人设为空 ⇒ 返回空列表，调用方退回静态池（**永不阻塞**冷场搭话）。
- 只收问句（以 `?／？` 或「吗/呢/吧/呀/啊」收尾）：陈述句在冷群里没人接得住。

成本：每个「人设 × 生活日 × 时段段」最多一次模型调用（默认一天 8 段，实测通常只生成
1~2 段——冷群搭话一天最多开一次口），比每 tick 生成省一个量级。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Sequence

#: 缓存最多保留几个键（人设×日×时段）。超了按时间淘汰——只是缓存，丢了下次重生成。
MAX_CACHE_KEYS = 8

#: 一次生成几条候选（多给几条，`pick_topic` 才有得挑）。
DEFAULT_TOPIC_COUNT = 4

#: 时段段长度（分钟）：3 小时一段（凌晨/清晨/上午/午间/下午/傍晚/夜里/深夜）。
BUCKET_MINUTES = 180

_SLOT_LABELS = (
    (0, "凌晨"),
    (360, "清晨"),
    (540, "上午"),
    (720, "午间"),
    (900, "下午"),
    (1080, "傍晚"),
    (1260, "夜里"),
    (1380, "深夜"),
)

#: 「能被回答的问句」判据（与 ``life_topic.is_answerable_question`` 同口径；
#: 这里独立一份是为了不让本模块反向依赖 ``life_topic``，两处由测试对拍）。
_QUESTION_TAIL = re.compile(r"[吗呢吧呀啊]$")

_NUMBERING = re.compile(r"^\s*(?:[-*·]|\d+[.、)]|[（(]\d+[）)])\s*")


def bucket_index(now_minutes: int) -> int:
    """当前属于第几个时段段（0 起）。坏值按 0 处理，绝不抛。"""

    try:
        minutes = int(now_minutes)
    except (TypeError, ValueError):
        return 0
    if minutes < 0:
        return 0
    return min(len(_SLOT_LABELS) - 1, minutes // BUCKET_MINUTES)


def slot_label(now_minutes: int) -> str:
    """时段名（给提示词用的人话）。"""

    index = bucket_index(now_minutes)
    return _SLOT_LABELS[index][1]


def persona_hash(persona: object) -> str:
    clean = re.sub(r"\s+", " ", str(persona or "")).strip()
    if not clean:
        return ""
    return hashlib.sha1(clean.encode("utf-8")).hexdigest()[:10]


def cache_key(persona: object, day_key: str, now_minutes: int) -> str:
    """缓存键：``人设哈希|生活日|时段段``。人设为空 ⇒ 空串（不生成）。"""

    digest = persona_hash(persona)
    if not digest:
        return ""
    return f"{digest}|{str(day_key or '').strip()}|{bucket_index(now_minutes)}"


def build_prompt(persona: object, *, now_minutes: int, count: int = DEFAULT_TOPIC_COUNT) -> str:
    """生成话头的提示词。**只出问句**、不暴露动机、不 @ 任何人。

    三条约束都来自 `life_topic` 那套纪律的实测结论：陈述句在冷群里接不住；
    一说「为了活跃气氛」就变成任务播报；@ 人会把「随口一问」变成点名叫人回答。
    """

    body = re.sub(r"\s+", " ", str(persona or "")).strip()
    limit = max(1, int(count))
    return (
        f"下面是一个聊天机器人的人设：\n{body[:600]}\n\n"
        f"现在是{slot_label(now_minutes)}。请以**这个人**的口吻，写出 {limit} 句"
        "能在安静群里随口抛出、**别人容易接话**的短问句。\n"
        "硬性要求：\n"
        "1. 每行一句，不要编号、不要解释、不要引号；\n"
        "2. 必须是问句（以问号或「吗/呢/吧/呀/啊」收尾）；\n"
        "3. 不点任何人的名字、不 @、不提「活跃气氛」「找话题」这类字眼；\n"
        "4. 每句不超过 30 字，具体到此刻（这个时段的人在做什么），不要空泛寒暄。\n"
    )


def is_answerable(text: object) -> bool:
    """能不能被接话：以问号结尾，或以「吗/呢/吧/呀/啊」收尾。"""

    clean = str(text or "").strip().strip("\"'“”‘’")
    if not clean:
        return False
    if clean.endswith("?") or clean.endswith("？"):
        return True
    return bool(_QUESTION_TAIL.search(clean))


def parse_topics(raw: object, *, count: int = DEFAULT_TOPIC_COUNT) -> list[str]:
    """模型输出 → 话头列表。只留问句，去重，按 ``count`` 截断。

    模型经常带编号、引号、解释行；这里一律剥掉而不是「原地信任」——一条陈述句混进
    池子会让 ``life_topic.pick_topic`` 抽到它，冷群里就变成没人接得住的独白。
    """

    if isinstance(raw, (list, tuple)):
        lines: Iterable[str] = [str(item) for item in raw]
    else:
        lines = str(raw or "").splitlines()
    limit = max(1, int(count))
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        text = _NUMBERING.sub("", str(line or "")).strip().strip("\"'“”‘’")
        if not text or len(text) > 40:
            continue
        if not is_answerable(text):
            continue
        key = text.rstrip("?？吗呢吧呀啊")
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def prune_cache(cache: object, *, keep: int = MAX_CACHE_KEYS) -> dict[str, list[str]]:
    """把缓存洗成 ``{key: [话头]}``，并按「插入顺序保留最近 keep 个」。"""

    if not isinstance(cache, dict):
        return {}
    cleaned: dict[str, list[str]] = {}
    for key, value in cache.items():
        name = str(key or "").strip()
        if not name:
            continue
        if isinstance(value, (list, tuple)):
            topics = [str(item).strip() for item in value if str(item or "").strip()]
        else:
            topics = []
        if topics:
            cleaned[name] = topics[:8]
    limit = max(1, int(keep))
    if len(cleaned) > limit:
        cleaned = dict(list(cleaned.items())[-limit:])
    return cleaned


def cached_topics(cache: object, key: str) -> list[str]:
    """读缓存（空键/坏值一律返回空列表 = 用静态池）。"""

    if not isinstance(cache, dict) or not key:
        return []
    value = cache.get(key)
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def topics_for(cache: object, persona: object, day_key: str, now_minutes: int) -> list[str]:
    """按「人设 + 生活日 + 时段段」取话头；没有就返回空列表。"""

    return cached_topics(cache, cache_key(persona, day_key, now_minutes))


__all__ = [
    "BUCKET_MINUTES",
    "DEFAULT_TOPIC_COUNT",
    "MAX_CACHE_KEYS",
    "bucket_index",
    "build_prompt",
    "cache_key",
    "cached_topics",
    "is_answerable",
    "parse_topics",
    "persona_hash",
    "prune_cache",
    "slot_label",
    "topics_for",
]
