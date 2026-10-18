# -*- coding: utf-8 -*-
"""长期记忆（A_Memorix）只读接入（纯模块）：解析、成句、按人缓存、按会话隔离。

**为什么只做「读」**
宿主 SDK 2.8.x 的 ``capabilities/`` 里没有 memory 能力，插件侧唯一入口是
``knowledge.search``（底层 A_Memorix，长期记忆是独立 host service）。写回宿主记忆只能
走 ``database.*`` 直接操作宿主表——那等于把插件钉在宿主的内部 schema 上。本插件连
``adjust_memory.json`` 都做了双份兜底（见 ``docs/interop.md``），没有理由在这件事上赌。
所以这一层只读：读得到就用，读不到就什么都不发生（与 ``[world]`` / ``[economy]``
那几路可选接线同一条纪律）。

**三条真机契约**（来自 ``impression-card-lite`` 的踩坑，别再试一遍）
1. **必须** ``respect_filter=True`` 且带 ``person_id``。否则 A_Memorix 走全库检索、
   把别人的记忆捞回来——这是「串人」的第一根因。
2. ``mode`` 只允许 ``search`` / ``aggregate``；``hybrid`` / ``episode`` / ``time``
   在 A_Memorix 的并行或部分路径上会刷 tuple / SQLite 错。
3. 多次检索**串行**调用，并行会撞 SQLite 写锁。

**隔离的第二道闸在本地**
服务端过滤只能保证「这条记忆属于这个人」，不能保证「它不会出现在别人的会话里」。
所以记忆素材一律带 ``target_user_id``，进入候选前过 :func:`materials_for_session`；
并且**从不写入** ``LifeState.materials``——那是全局池，一写就跨会话、还会被
``_save_state`` 落盘，重启后继续污染（这是本模块不碰 ``state`` 的全部理由）。

**素材形态**（决议 3）：一行概括事实，禁止复述原话。A_Memorix 里存的本来就是
摘要条目，这里只做清洗与截断，不再改写语义——她该「记得这件事」，而不是「背出那句话」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

try:  # 包式加载（Runner 真机）
    from .life_events import sanitize_text
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_events import sanitize_text  # type: ignore[no-redef]

# ---------------------------------------------------------------- 常量

#: 允许的检索模式（见模块文档契约 2）。``hybrid`` / ``episode`` / ``time`` 一律不试。
MEMORY_SEARCH_MODES: tuple[str, ...] = ("search", "aggregate")

#: 素材标签（进 ``build_intent`` 的括号里，也进 ``/生活 为什么`` 的来由）
MEMORY_LABEL = "记忆:旧事"

#: 素材权重：问候/分享 0.5、关系维护 0.7。记忆比「分享个念头」重一点、比
#: 「主动去问候一个熟人」轻一点——它是**附带**的话题，不是必须说的事。
MEMORY_MATERIAL_WEIGHT = 0.6

#: 素材有效期（小时）。因为素材**不落盘**，它只在一次巡检内存在，TTL 只影响
#: ``best_until`` 之后的衰减，不会跨 tick 累积。
MEMORY_MATERIAL_TTL_HOURS = 3.0

#: 单条事实渲染上限（字符）。60 字够说清「提过要考公，最近在刷题」，再长就是原文复述。
DEFAULT_FACT_MAX_CHARS = 60

#: 素材正文总长上限（与 ``life_events.MAX_MATERIAL_CHARS`` 同口径，避免两处漂移）
MAX_MATERIAL_CHARS = 80

#: 事实正文的引导语（``build_intent`` 的 "intro"）。**不出现"记得你说过"**：
#: 那是复述原话的入口，也是出戏的入口（她该像想起一件事，不像查聊天记录）。
MEMORY_INTRO = "想起一件旧事，想自然地提一句"

#: 「没有记忆」的桩回答。宿主查不到时会回这些，当成空结果而不是一条事实。
#: ⚠ 措辞必须**够具体**：早期写成裸的「未找到」会把「上个月未找到合适的房子」
#: 这种真实记忆一起丢掉——判桩回答不能靠一个词。
_NO_MEMORY_MARKERS: tuple[str, ...] = (
    "你不太了解",
    "没有找到相关",
    "未找到相关",
    "没有相关记忆",
    "暂无相关记忆",
)

#: 检索结果自带的标题前缀（A_Memorix 的返回是文本，不是 JSON）。
_PREFIXES: tuple[str, ...] = (
    "你知道这些知识:",
    "你知道这些知识：",
    "你知道这些记忆:",
    "你知道这些记忆：",
    "相关记忆:",
    "相关记忆：",
)

#: 行首列表符号与序号（模型/工具回显常见形态："1. xxx"、"- xxx"、"• xxx"、"1）xxx"）
_BULLET = re.compile(r"^[\-\*•·]\s*")
_ORDERED = re.compile(r"^\d{1,2}[.、)）]\s*")

#: 结尾省略号（来源被截断的痕迹，留着会让句子读起来像半句话）
_TRAILING_ELLIPSIS = re.compile(r"(\.{3,}|…+)\s*$")


# ---------------------------------------------------------------- 解析


def _iter_memory_items(value: Any) -> Iterable[str]:
    """把 ``knowledge.search`` 的各种返回形态摊平成文本行。

    宿主的返回**没有稳定契约**：真机上是带序号的整段文本，SDK 类型标注却是 ``Any``
    （``maibot_sdk/capabilities/knowledge.py:20``）。所以这里按「能认的都认、
    认不出的跳过」写：字符串按行拆，列表/字典递归，标量转字符串。
    """

    if value is None:
        return
    if isinstance(value, str):
        yield from value.splitlines()
        return
    if isinstance(value, Mapping):
        for key in ("result", "data", "memories", "items", "content", "text", "summary"):
            if key in value:
                yield from _iter_memory_items(value[key])
                return
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            if isinstance(item, str):
                yield from item.splitlines()
            elif isinstance(item, Mapping):
                for key in ("content", "text", "memory", "summary", "fact"):
                    inner = item.get(key)
                    if isinstance(inner, str):
                        yield inner
                        break
            else:
                yield str(item)
        return
    yield str(value)


def scrub_fact(text: object, *, max_chars: int = DEFAULT_FACT_MAX_CHARS) -> str:
    """把一条记忆条目清洗成一行事实。

    顺序有讲究：先 ``sanitize_text``（控制字符与 ``<>{}`【】「」`` 这些结构字符必须
    去掉，否则一条来路可疑的记忆就能在提示词里伪造出 ``【输出要求】`` 分节），
    再剥列表符号与序号，最后截断。截断加省略号是给人看的；模型看到的是一句完整话。
    """

    cleaned = sanitize_text(text, max_chars=0)
    if not cleaned:
        return ""
    for prefix in _PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break
    cleaned = _BULLET.sub("", cleaned)
    cleaned = _ORDERED.sub("", cleaned)
    cleaned = _TRAILING_ELLIPSIS.sub("", cleaned).strip()
    if not cleaned:
        return ""
    limit = int(max_chars)
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "…"
    return cleaned


def facts_from_content(
    content: Any,
    *,
    max_items: int = 5,
    max_chars: int = DEFAULT_FACT_MAX_CHARS,
) -> tuple[str, ...]:
    """``knowledge.search`` 返回值 → 去重后的干净事实元组。

    ``max_items <= 0`` 直接返回空：调用方关掉上限时不该退化成「全部都要」，
    那与 ``recent_events_keep = 0`` 的老坑是同一类语义反转。
    """

    limit = int(max_items)
    if limit <= 0 or content is None:
        return ()

    out: list[str] = []
    seen: set[str] = set()
    for raw in _iter_memory_items(content):
        lowered = str(raw or "").casefold()
        if any(marker in lowered for marker in _NO_MEMORY_MARKERS):
            continue
        fact = scrub_fact(raw, max_chars=max_chars)
        if not fact:
            continue
        key = fact.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(fact)
        if len(out) >= limit:
            break
    return tuple(out)


# ---------------------------------------------------------------- 过滤


def _tokens(values: Iterable[object]) -> tuple[str, ...]:
    """身份词 → 小写 token 元组。单字 token 丢弃（「A」「1」这种会命中一切）。"""

    out: list[str] = []
    for value in values:
        text = str(value or "").strip().casefold()
        if len(text) >= 2 and text not in out:
            out.append(text)
    return tuple(out)


def filter_facts(
    facts: Sequence[str],
    *,
    person_tokens: Iterable[object] = (),
    bot_tokens: Iterable[object] = (),
    require_person_mention: bool = False,
) -> tuple[str, ...]:
    """本地身份过滤（第二道防线，确定性、不依赖模型自觉）。

    两条规则：

    1. **剔除 bot 自述条目**：提到她自己的昵称/QQ、却没提到目标本人的记忆，不进候选。
       A_Memorix 会把 bot 自己说过的话一并存下，这类条目拿来做「关于 TA 的旧事」
       会让对话绕回她身上（``impression-card-lite`` 为此专门写了过滤器）。
    2. 可选 ``require_person_mention``：要求条目里出现目标本人的身份词。**默认关**——
       服务端已经用 ``person_id`` + ``respect_filter=True`` 圈定过，再要求点名会把
       「提过要考公」这类正确条目一起丢掉。开着它只在怀疑服务端过滤失效时有意义。
    """

    person = _tokens(person_tokens)
    bot = _tokens(bot_tokens)
    if not person and not bot and not require_person_mention:
        return tuple(facts)

    kept: list[str] = []
    for fact in facts:
        lowered = str(fact or "").casefold()
        if not lowered:
            continue
        mentions_person = any(token in lowered for token in person)
        mentions_bot = any(token in lowered for token in bot)
        if mentions_bot and not mentions_person:
            continue
        if require_person_mention and not mentions_person:
            continue
        kept.append(fact)
    return tuple(kept)


# ---------------------------------------------------------------- 素材


def fact_to_material(
    fact: str,
    *,
    now: float,
    target_user_id: str,
    hint: str = "",
    weight: float = MEMORY_MATERIAL_WEIGHT,
    ttl_hours: float = MEMORY_MATERIAL_TTL_HOURS,
    intro: str = MEMORY_INTRO,
) -> dict[str, Any] | None:
    """一条事实 → 一条**定向**主动开口素材。

    与 ``life_motives._material`` 的差别只有三处，都是刻意的：
    ``expires_at`` / ``best_until`` 直接是**绝对时刻**（那边先存时长、由 ``stamp``
    换算，因为动机在纯模块里生成后才落盘；这里从不落盘，没有中间态）；
    多一个 ``target_user_id``（会话隔离的唯一依据）；
    多一个 ``intro``（``build_intent`` 用它替换「刚发生的一件事」，否则记忆会被
    说成「刚发生」——那是事实错误）。
    """

    text = scrub_fact(fact, max_chars=MAX_MATERIAL_CHARS)
    if not text:
        return None
    uid = str(target_user_id or "").strip()
    if not uid:
        # 认不出人就不生成素材：宁可少一条，也不让一条无主的记忆漂到任何会话里
        return None

    who = sanitize_text(hint, max_chars=24) or "一个人"
    body = sanitize_text(f"还记得{who}提过的：{text}", max_chars=MAX_MATERIAL_CHARS)
    ttl = max(0.5, float(ttl_hours)) * 3600.0
    stamp = float(now)
    return {
        "label": MEMORY_LABEL,
        "text": body,
        "intro": intro,
        "weight": float(weight),
        "created_at": stamp,
        "expires_at": stamp + ttl,
        "best_until": stamp + ttl * 0.5,
        "target_user_id": uid,
    }


def memory_materials(
    facts: Sequence[str],
    *,
    now: float,
    target_user_id: str,
    hint: str = "",
    weight: float = MEMORY_MATERIAL_WEIGHT,
    ttl_hours: float = MEMORY_MATERIAL_TTL_HOURS,
    max_items: int = 2,
) -> tuple[dict[str, Any], ...]:
    """批量把事实转成素材（``max_items <= 0`` 时为空，语义同 :func:`facts_from_content`）。"""

    limit = int(max_items)
    if limit <= 0:
        return ()
    out: list[dict[str, Any]] = []
    for fact in facts:
        material = fact_to_material(
            fact, now=now, target_user_id=target_user_id,
            hint=hint, weight=weight, ttl_hours=ttl_hours,
        )
        if material is not None:
            out.append(material)
        if len(out) >= limit:
            break
    return tuple(out)


def materials_for_session(
    materials: Iterable[Mapping[str, Any]],
    *,
    user_id: str,
) -> tuple[dict[str, Any], ...]:
    """按当前会话的发言人过滤候选素材（隔离的**唯一**收口）。

    无 ``target_user_id`` 的素材原样通过（``state.materials`` 里的动机/节日/梦境
    素材本来就没有定向语义，一刀切会把它们也挡掉）。带定向的必须与 ``user_id``
    完全一致——比不了就当不匹配，宁可不开口，不猜人。
    """

    uid = str(user_id or "").strip()
    out: list[dict[str, Any]] = []
    for item in materials:
        if not isinstance(item, Mapping):
            continue
        target = str(item.get("target_user_id") or "").strip()
        if target and target != uid:
            continue
        out.append(dict(item))
    return tuple(out)


# ---------------------------------------------------------------- 缓存


@dataclass
class FactCache:
    """按 ``person_id`` 的惰性缓存（TTL 默认 6 小时）。

    三个刻意的决定：

    1. **空结果也缓存**。查不到（新用户 / 记忆库还没攒到东西）如果不记时间戳，
       每次巡检都会重查一遍——10 分钟一个 tick、N 个会话，这是最容易被忽略的
       成本放大器。
    2. **进程内不落盘**。落盘就要给 ``relationships`` 加列、升 schema 版本，而这份
       缓存的全部价值只是「省几次 RPC」。重启后冷启动多查一轮，接受。
    3. **TTL 是「刷新上限」不是「刷新节拍」**：到期不代表立刻去查，而是下一次
       真要用它时才查（惰性）。所以没有后台任务，也不会有空转的定时器。
    """

    ttl_seconds: float = 6.0 * 3600.0
    _entries: dict[str, tuple[float, tuple[str, ...]]] = field(default_factory=dict)

    def get(self, key: str, now: float) -> tuple[str, ...] | None:
        """命中返回事实元组（可能为空元组），未命中/过期返回 ``None``。"""

        entry = self._entries.get(str(key))
        if entry is None:
            return None
        fetched_at, facts = entry
        if float(now) - float(fetched_at) >= max(0.0, float(self.ttl_seconds)):
            return None
        return facts

    def put(self, key: str, facts: Sequence[str], now: float) -> None:
        self._entries[str(key)] = (float(now), tuple(str(f) for f in facts if str(f)))

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


__all__ = [
    "DEFAULT_FACT_MAX_CHARS",
    "FactCache",
    "MAX_MATERIAL_CHARS",
    "MEMORY_INTRO",
    "MEMORY_LABEL",
    "MEMORY_MATERIAL_TTL_HOURS",
    "MEMORY_MATERIAL_WEIGHT",
    "MEMORY_SEARCH_MODES",
    "fact_to_material",
    "facts_from_content",
    "filter_facts",
    "materials_for_session",
    "memory_materials",
    "scrub_fact",
]
