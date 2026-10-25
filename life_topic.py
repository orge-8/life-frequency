# -*- coding: utf-8 -*-
"""冷群搭话（topic）：判断「这个群冷下来了」并给出一条**能被回答**的话头（纯模块）。

**为什么需要单独一层**
``life_motives`` 的三类动机（问候 / 生活分享 / 关系维护）都是「有话想说」——它们
天然是**独白**（分享自己、道早安），在没人的群里说完就沉。要「活跃冷群」，缺的不是
话，是**让别人开口的钩子**：一句问句，且必须是别人愿意答的公共话题。

**三条纪律**

1. **只出问句**。陈述句在冷群里等于自言自语；问句才是把话筒递出去。测试里直接断言
   文本以问号结尾。
2. **不暴露动机**。她**不能**说「群好安静啊我来活跃一下」——那是主持人台词，不是人。
   所以动机只写在素材的 ``intro``（给模型的内心描述）里，并明确要求不要说出来。
3. **不带任何群成员信息**。冷群搭话的效果来自「谁都可能答」，一旦 @ 具体某人就变成
   社交压力，也把「活跃气氛」变成了「点名」。

**为什么不落盘、不入池**
素材由 plugin 在**每次巡检**按会话即时生成（与 ``life_memory`` 的记忆素材同一套模式）：
冷群判定是纯本地比较（``last_user_message_at`` 是入站钩子写的），生成一条字符串的成本
可以忽略；而一旦写进全局 ``state.materials``，它就会漂到别的会话去——那正是这一版要
治的病。同一天同一个会话得到**同一条**话头（种子是 ``session_id + 生活日``），
所以每 tick 重新生成也不会「换一个话题再问一遍」。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
import re
from typing import Any

try:  # 包式加载（Runner 真机）
    from .life_events import sanitize_text
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_events import sanitize_text  # type: ignore[no-redef]

# ---------------------------------------------------------------- 常量

#: 素材标签（进 ``/生活 为什么`` 的来路与状态卡）
COLD_TOPIC_LABEL = "冷群搭话"

#: 引导语（进 ``build_intent`` 的 ``intro``）。**措辞是给模型的内心描述**，
#: 所以必须自带「别说出来」——否则模型会把「我来活跃气氛」当台词讲出去，
#: 那是这个功能最典型、也最丢人的失败形态。
COLD_TOPIC_INTRO = "群里安静了半天，她有点想找个话头把它带起来（别提「活跃气氛」这四个字）"

#: 权重：问候/分享 0.5、关系维护 0.7。搭话的分量介于两者之间——它是**主动**的，
#: 但不像「去问候一个熟人」那样有明确的社交债。
COLD_TOPIC_WEIGHT = 0.55

#: 有效期（小时）。素材不落盘，只影响一次巡检内的保鲜衰减。
COLD_TOPIC_TTL_HOURS = 2.0

#: 话头池：按**时段**分桶（本地分钟，半开区间；跨午夜的桶由 ``_bucket_of`` 归一）。
#: 每条都必须是「谁都能答、答了不尴尬」的公共话题：吃、睡、追剧、天气、作息、周末。
#: ⚠ 刻意**不写**「你们在聊什么」——冷群没人聊，这句会立刻把尴尬点亮。
TOPIC_POOL: dict[str, tuple[str, ...]] = {
    "清晨": (
        "你们早饭一般吃什么呀",
        "这个点就醒着的，都是被什么叫起来的呀",
    ),
    "上午": (
        "上午这个点是不是特别容易犯困啊",
        "最近有什么在追的番吗",
    ),
    "午间": (
        "中午吃什么呀，有推荐吗",
        "你们午饭一般自己弄还是点外卖呀",
    ),
    "下午": (
        "下午这段时间你们都是怎么撑过去的呀",
        "有人最近在玩什么游戏吗",
    ),
    "傍晚": (
        "下班或者放学之后你们一般干嘛呀",
        "今天晚饭打算吃什么呀",
    ),
    "晚上": (
        "这个点大家都在忙什么呢",
        "晚上有没有什么好听的歌推荐呀",
    ),
    "深夜": (
        "这个点还醒着的都在忙啥呢",
        "熬夜的人是不是越晚越精神呀",
    ),
}

#: 兜底（时段算不出来时用；正常不会走到）
_FALLBACK_TOPICS: tuple[str, ...] = ("你们最近都在忙什么呀",)

#: 时段桶（起点分钟，名字）。按声明顺序匹配，跨午夜的「深夜」用 0–5 点表示。
_BUCKETS: tuple[tuple[int, str], ...] = (
    (5 * 60, "清晨"),
    (9 * 60, "上午"),
    (11 * 60, "午间"),
    (14 * 60, "下午"),
    (17 * 60, "傍晚"),
    (19 * 60, "晚上"),
    (23 * 60, "深夜"),
)

#: 问句判定（测试与自检用）：允许中文问号、英文问号，或「…吗 / …呀 / …呢」结尾。
_QUESTION_TAIL = re.compile(r"(吗|呢|吧|呀|啊)\s*[?？]?\s*$")


# ---------------------------------------------------------------- 冷群判定


def is_cold(last_user_at: float, now: float, *, cold_hours: float) -> bool:
    """这个会话是否「冷」到需要搭话。

    ``last_user_at <= 0``（从没见过人说话）**判为不冷**：那是「没有基线」而不是
    「确认很安静」——新加的群、刚重置过的状态都落在这里，她不该一来就主动搭话。
    这条与 ``_ensure_last_user_baseline`` 的「补基线」是两件事：那边补的是**时间**，
    这里判的是**该不该开口**，宁可不猜。

    ``cold_hours <= 0`` 视为关闭（调用方通常已经用配置开关拦过，这里是双保险）。
    """

    hours = float(cold_hours)
    if hours <= 0.0:
        return False
    last = float(last_user_at or 0.0)
    if last <= 0.0:
        return False
    return (float(now) - last) >= hours * 3600.0


def _bucket_of(now_minutes: int) -> str:
    """本地分钟 → 时段桶名。``23:00–04:59`` 归「深夜」。"""

    minutes = int(now_minutes) % (24 * 60)
    name = "深夜"
    for start, label in _BUCKETS:
        if minutes >= start:
            name = label
        else:
            break
    if minutes < _BUCKETS[0][0]:
        return "深夜"
    return name


def topic_pool(now_minutes: int) -> tuple[str, ...]:
    """该时段可用的话头池（空/坏时段回兜底，永远非空）。"""

    return TOPIC_POOL.get(_bucket_of(now_minutes)) or _FALLBACK_TOPICS


def pick_topic(
    session_id: str,
    day_key: str,
    now_minutes: int,
    *,
    pool: Sequence[str] | None = None,
) -> str:
    """确定性地挑一条话头。

    种子是 ``(会话, 生活日)``：同一天同一个会话**恒得同一条**——每 tick 重新生成
    也不会变成「换个话题再问一遍」；不同群各自不同，避免所有群同一句话。
    """

    candidates = [
        str(item).strip()
        for item in (pool or ())
        if str(item or "").strip() and is_answerable_question(item)
    ]
    pool = tuple(candidates) or topic_pool(now_minutes)
    seed = f"{session_id}|{day_key}|{_bucket_of(now_minutes)}".encode("utf-8")
    index = int(hashlib.sha1(seed).hexdigest()[:8], 16) % len(pool)
    return pool[index]


def is_answerable_question(text: object) -> bool:
    """是否「能被回答的问句」：以问号结尾，或以「吗/呢/吧/呀/啊」收尾。

    单独抽出来是给测试与自检用的：话头池以后再加句子，这条能挡住「写了句陈述句」。
    """

    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    if cleaned.endswith("?") or cleaned.endswith("？"):
        return True
    return bool(_QUESTION_TAIL.search(cleaned))


# ---------------------------------------------------------------- 素材


def cold_topic_material(
    session_id: str,
    day_key: str,
    *,
    now: float,
    now_minutes: int,
    weight: float = COLD_TOPIC_WEIGHT,
    ttl_hours: float = COLD_TOPIC_TTL_HOURS,
    pool: Sequence[str] | None = None,
) -> dict[str, Any]:
    """冷群话头 → 一条**定向**素材（带 ``target_session``，与其他会话互不可见）。

    字段与 ``life_motives._material`` 对齐（``_material`` 先存时长、由 ``stamp`` 补绝对
    时刻；这里从一开始就是绝对时刻，因为素材不落盘、没有中间态）。
    ``intro`` 让 ``build_intent`` 用「想找个话头」而不是「聊聊刚发生的一件事」——
    后者会把一次**抛话题**说成一次**汇报**。
    """

    text = sanitize_text(
        pick_topic(session_id, day_key, now_minutes, pool=pool), max_chars=60
    )
    if not text:
        return {}
    ttl = max(0.5, float(ttl_hours)) * 3600.0
    stamp = float(now)
    return {
        "label": COLD_TOPIC_LABEL,
        "text": text,
        "intro": COLD_TOPIC_INTRO,
        "weight": float(weight),
        "created_at": stamp,
        "expires_at": stamp + ttl,
        "best_until": stamp + ttl * 0.5,
        "target_session": str(session_id),
        #: 独占标记（v1.22.3）：``select_material`` 见到它就越过权重比较——
        #: 话头要和事件素材（0.30–0.85）、关系维护（0.70）竞争，0.55 的权重几乎总输，
        #: 「冷了就抛一条」会退化成「几乎永远不抛」。**只改选材，不改任何闸**。
        "exclusive": True,
    }


def materials_for_session(
    materials: Any, *, session_id: str
) -> tuple[dict[str, Any], ...]:
    """按会话过滤候选素材（与 ``life_memory.materials_for_session`` 同一职责）。

    无 ``target_session`` 的素材原样通过（常规素材本来就没有定向语义）；带定向的
    必须与本会话一致——比不了就当不匹配，宁可不开口，也不把 A 群的话头丢进 B 群。
    """

    wanted = str(session_id or "")
    out: list[dict[str, Any]] = []
    for item in materials or ():
        if not isinstance(item, dict):
            continue
        target = str(item.get("target_session") or "")
        if target and target != wanted:
            continue
        out.append(item)
    return tuple(out)


def cold_group_material(
    session_id: str,
    day_key: str,
    *,
    now: float,
    now_minutes: int,
    enabled: bool,
    is_group: bool,
    last_user_at: float,
    cold_hours: float,
    pool: Sequence[str] | None = None,
) -> dict[str, Any]:
    """冷群话头的**完整判定**（开关 + 群聊 + 冷度），命中返回一条素材，否则空 dict。

    判定放在纯模块里的理由：这是「她该不该在冷群开口」的全部条件，插件侧只负责取
    ``session_id`` 与 ``last_user_message_at``。判定留在一个地方，才能被离线穷举测试
    （见 ``tests/test_cold_group.py``），也不会被后面某个分支悄悄绕过。
    """

    if not enabled or not is_group:
        return {}
    if not is_cold(last_user_at, now, cold_hours=cold_hours):
        return {}
    return cold_topic_material(
        session_id, day_key, now=now, now_minutes=now_minutes, pool=pool
    )


__all__ = [
    "COLD_TOPIC_INTRO",
    "COLD_TOPIC_LABEL",
    "COLD_TOPIC_TTL_HOURS",
    "COLD_TOPIC_WEIGHT",
    "TOPIC_POOL",
    "cold_group_material",
    "cold_topic_material",
    "is_answerable_question",
    "is_cold",
    "materials_for_session",
    "pick_topic",
    "topic_pool",
]
