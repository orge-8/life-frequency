# -*- coding: utf-8 -*-
"""长期记忆（A_Memorix）只读接入（纯模块）：解析、成句、按人缓存、按会话隔离。

**为什么只做「读」**
宿主 SDK 2.8.x 的 ``capabilities/`` 里没有 memory 能力，插件侧唯一入口是
``knowledge.search``（底层 A_Memorix，长期记忆是独立 host service）。写回宿主记忆只能
走 ``database.*`` 直接操作宿主表——那等于把插件钉在宿主的内部 schema 上。本插件连
``adjust_memory.json`` 都做了双份兜底（见 ``docs/interop.md``），没有理由在这件事上赌。
所以这一层只读：读得到就用，读不到就什么都不发生（与 ``[world]`` / ``[economy]``
那几路可选接线同一条纪律）。

**四条真机契约**（来自 ``impression-card-lite`` 的踩坑，别再试一遍）
1. **必须** ``respect_filter=True`` 且带 ``person_id``。否则 A_Memorix 走全库检索、
   把别人的记忆捞回来——这是「串人」的第一根因。
2. ``mode`` 只允许 ``search`` / ``aggregate``；``hybrid`` / ``episode`` / ``time``
   在 A_Memorix 的并行或部分路径上会刷 tuple / SQLite 错。
3. 多次检索**串行**调用，并行会撞 SQLite 写锁。
4. **第 1 条的两个参数不在公开 SDK 里**（v1.22.4 起的硬约束，见下）。

**作用域必须自证，不能假设（v1.22.4）**
``person_id`` / ``mode`` / ``respect_filter`` 都不在 SDK 2.8.1 的
``KnowledgeCapability.search(query, limit=5)`` 签名里，``ctx.call_capability`` 只是把
``**kwargs`` 原样塞进 ``cap.call`` 的 ``args``——宿主**接受、忽略、报错**三种行为都
存在，公开文档与 SDK 都无从判断。所以插件不能再把「不串人」押在这两个参数被兑现上：

- **哨兵对照**（:data:`SCOPE_PROBE_PERSON_ID`）：拿到非空结果时，用一个不可能属于
  真人的 ``person_id`` 发同一条查询；回来的东西与真实查询**一致且非空**，就说明
  ``person_id`` 被忽略了 → 判 :data:`SCOPE_UNSCOPED`，**整层停用**（不是退回全库检索，
  没有作用域的检索本身就是串人）。
- **参数不受理也算证据**：宿主直接抛「unexpected keyword」一类错误，同样判
  ``unscoped``（:func:`looks_like_unsupported_argument`）。
- **没验证过就从严**：状态还是 ``unknown`` 时强制 ``require_person_mention=True``，
  只接受点名了目标人的条目——宁可这一轮少提一句，也不猜。
- **「一致」有两种成因（v1.25.2）**：宿主 A_Memorix 的人物过滤
  （``MemorySearchHitProcessingService._filter_hits``）最后一行是
  ``return filtered or hits``——**按人筛空就回退全量**。所以哨兵对照的「一致」既可能
  是参数被忽略，也可能是「这个人在候选里没有命中」。后者对「不存在的人」几乎必然
  成立，是**假阳性**。判据因此不能只看 ``facts_equal``：对照证据（条数 / 指纹 /
  是否退化查询）记进 :class:`ScopeProbe`，真机日志据此定性。判据本身尚未改动。

这三条加起来，无论宿主是「接受并生效 / 接受但忽略 / 直接报错」哪一种，都不会把
别人的记忆说出口；区别只是功能可用、自动停用、还是只在严格模式下可用。**仍未闭合的
缺口**：宿主在「某人查不到自己记忆」时会回退全量，这条路上插件拿到的就是他人在该
查询下的记忆——第二道闸（下列本地隔离 + 严格模式）是唯一还在起作用的防线。

**隔离的第二道闸在本地**
服务端过滤只能保证「这条记忆属于这个人」，不能保证「它不会出现在别人的会话里」。
所以记忆素材一律带 ``target_user_id``，进入候选前过 :func:`materials_for_session`；
并且**从不写入** ``LifeState.materials``——那是全局池，一写就跨会话、还会被
``_save_state`` 落盘，重启后继续污染（这是本模块不碰 ``state`` 的全部理由）。

**素材形态**（决议 3）：一行概括事实，禁止复述原话。A_Memorix 里存的本来就是
摘要条目，这里只做清洗与截断，不再改写语义——她该「记得这件事」，而不是「背出那句话」。
"""

from __future__ import annotations

import hashlib
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

#: 作用域尚未验证：``person_id`` / ``respect_filter`` 到底生不生效还不知道。
#: 此状态下**强制**按人点名过滤（见 :func:`filter_facts` 的 ``require_person_mention``）。
SCOPE_UNKNOWN = "unknown"
#: 已验证：服务端确实按 ``person_id`` 圈定了检索范围。
SCOPE_SCOPED = "scoped"
#: **存疑**（v1.25.3）：对照拿不到可用证据（没有第二个真人可对照，或对照人查不到任何
#: 东西）。此状态**不停用**，退到「条目必须点名本人」的严格模式——把「验不出」当成
#: 「无效」，等于让一次环境缺失永久关掉功能。
SCOPE_UNCERTAIN = "uncertain"
#: 已验证：**没有**按人圈定（参数被忽略或不被受理）。此状态下整层停用。
SCOPE_UNSCOPED = "unscoped"

#: 哨兵 ``person_id``：与 :func:`relation_person_id` 同形却不可能对应真人
#: （真人的键是 ``md5(platform_user_id)`` 的 32 位十六进制）。
#:
#: ⚠ **v1.25.3 起不再作为判据**：宿主 ``MemorySearchHitProcessingService._filter_hits``
#: 以 ``return filtered or hits`` 收尾，「不可能的人」必然筛空、必然回退全量，于是它
#: 与真人结果的一致性只证明「宿主对空命中 fail-open」，不证明参数被忽略。留作常量是
#: 为了让日志里出现这个值时仍能一眼认出「这是哨兵、不是真人」。
SCOPE_PROBE_PERSON_ID = "0" * 32

#: 查询词退化成 ``person_id`` 时的替代词（v1.25.3）。向量检索拿 md5 当查询等于乱查，
#: 候选集会退化成「此人自己的记忆」，对照判据随之失效。用一个语义可用、跨人通用的
#: 短句取代它——它不是「更好的检索」，只是「不再是垃圾查询」。
MEMORY_FALLBACK_QUERY = "最近聊过的事"

#: 一条人能不能当「对照真人」的判据失败上限：连续这么多轮都判不出，就停止探测
#: （每轮探测都是一次额外 RPC，10 分钟一个 tick 会白烧）。
MAX_SCOPE_PROBES = 3

#: 诊断指纹长度（v1.25.2）：日志里只写 ``sha1(事实)[:8]``，**永不**写原文。
#: 记忆条目本身就可能是隐私，把对照两侧的原文打进日志等于换个地方泄露一次。
FACT_DIGEST_CHARS = 8

#: 证据里保留几条指纹：两条足够判断「同一个结果集」还是「撞巧同数量」。
FACT_DIGEST_SAMPLES = 2

#: 宿主「不接受这些参数」的错误特征（中英文宿主都考虑）。命中即判
#: :data:`SCOPE_UNSCOPED`——参数不被受理时，继续发等于发一条无作用域的全库检索。
_UNSUPPORTED_ARG_MARKERS: tuple[str, ...] = (
    "unexpected keyword",
    "unexpected parameter",
    "unexpected argument",
    "unknown parameter",
    "unknown argument",
    "unsupported parameter",
    "unsupported argument",
)

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


# ---------------------------------------------------------------- 作用域自证


def facts_equal(left: Sequence[str], right: Sequence[str]) -> bool:
    """两组事实是否**逐条相同**（哨兵对照的判据，忽略顺序）。

    只在两边都非空时才有意义：空对空说明「两边都查不到」，那也可能是过滤生效了
    （哨兵本来就没有记忆），不能拿来判 ``unscoped``。
    """

    a = tuple(str(item or "").strip().casefold() for item in left if str(item or "").strip())
    b = tuple(str(item or "").strip().casefold() for item in right if str(item or "").strip())
    if not a or not b or len(a) != len(b):
        return False
    return sorted(a) == sorted(b)


def looks_like_unsupported_argument(error: Any) -> bool:
    """这个异常是不是「宿主不接受 ``person_id`` / ``respect_filter``」？

    **只认明确特征**，认不出就返回 ``False``——判定错成「不支持」会让功能白停，
    判定错成「支持」会让全库检索继续跑，后者才是事故。所以宁可漏判（维持
    ``unknown`` 的严格模式），也不许拿模糊信号下结论。
    """

    text = f"{type(error).__name__}: {error}".casefold()
    if not text.strip(":"):
        return False
    return any(marker in text for marker in _UNSUPPORTED_ARG_MARKERS)


def fact_digest(fact: Any) -> str:
    """一条事实的**指纹**（``sha1[:FACT_DIGEST_CHARS]``），日志里只用它代替原文。"""

    text = str(fact or "").strip()
    if not text:
        return ""
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:FACT_DIGEST_CHARS]


@dataclass(frozen=True)
class ScopeProbe:
    """一次哨兵对照的**证据快照**（v1.25.2，只记录，不参与判定）。

    为什么要有这个东西：v1.22.4 的判据是「两侧一致且非空 ⇒ ``person_id`` 被忽略 ⇒
    停用」。真机日志只留一句「哨兵返回了与真人一致的结果」，看不出**为什么一致**。
    而宿主 A_Memorix 的人物过滤是：

    .. code-block:: python

        # src/A_memorix/core/runtime/services/search_hit_processing_service.py
        return filtered or hits     # 筛空 = 回退全量

    也就是说「一致」有两种成因，从返回内容上看**完全同形**：

    1. ``person_id`` 被忽略 → 任何人的查询都拿到同一批候选；
    2. ``person_id`` 生效，但这位「人」在候选里一条都没有 → ``filtered`` 为空 →
       回退全量 → 与真人结果撞成一致（宿主 v1.3.19 仍是这一行为，r-dev 分支同样）。

    第 2 种是**假阳性**，而且它对「不存在的人」几乎必然成立。所以这条证据必须带上
    :attr:`query_degraded`——当查询词退化成 ``person_id``（md5）时，候选集本来就
    几乎只剩这个人自己的记忆，回退全量与真人结果的差异被抹平，误判概率最高。
    只加证据、不改判定：先让真机日志能回答「是哪一种」，再谈换判据。
    """

    #: 真人查询解析出的事实条数 / 哨兵查询解析出的事实条数
    real_count: int
    probe_count: int
    #: 两侧是否**逐条一致**（:func:`facts_equal` 的结论，判定的唯一依据）
    identical: bool
    #: 查询词是否就是 ``person_id`` 本身（退化查询，见模块文档契约 1 的注释）
    query_degraded: bool
    #: 两侧事实的指纹（不带原文），最多 :data:`FACT_DIGEST_SAMPLES` 条
    real_digests: tuple[str, ...]
    probe_digests: tuple[str, ...]
    #: 本次是否真的有一个「另一个真人」当对照（没有就只能判存疑）
    decoy_available: bool = True

    def summary(self) -> str:
        """一行诊断串：够定性，不含任何记忆原文。"""

        def _digests(items: Sequence[str]) -> str:
            return ",".join(items) or "-"

        return (
            f"证据: 真人{self.real_count}条/对照{self.probe_count}条"
            f"/逐条一致={'是' if self.identical else '否'}"
            f"/对照人可用={'是' if self.decoy_available else '否'}"
            f"/查询词退化为person_id={'是' if self.query_degraded else '否'}"
            f"/指纹 真人={_digests(self.real_digests)} 对照={_digests(self.probe_digests)}"
            + (
                "（一致有两种成因：参数被忽略，或该人在候选里无命中导致宿主"
                "「筛空回退全量」——后者是假阳性，退化查询下最易发生）"
                if self.identical and self.query_degraded
                else ""
            )
        )


class MemoryScopeGuard:
    """``knowledge.search`` 作用域自证的状态机（纯逻辑，不碰 IO）。

    宿主对 ``person_id`` / ``respect_filter`` 有「接受并生效 / 接受但忽略 / 直接报错」
    三种可能，公开 SDK 无从判断——所以不串人这件事由**这个状态机**负责，不由
    SDK 签名假设负责。四态见 :data:`SCOPE_UNKNOWN` / :data:`SCOPE_SCOPED` /
    :data:`SCOPE_UNCERTAIN` / :data:`SCOPE_UNSCOPED`。

    **粘性**：判成 ``unscoped`` 之后不再回头。宿主不支持是契约事实，重试也不会变，
    而每次重试都是一次无作用域的全库检索。``uncertain`` 允许再验（最多
    :data:`MAX_SCOPE_PROBES` 次）：它是「这次没有证据」，不是「宿主不行」。

    **v1.25.3 改四态**：v1.22.4 是二元的——哨兵与真人结果一致就停用整层。核实宿主
    源码后发现 ``_filter_hits`` 以 ``return filtered or hits`` 收尾，「按人筛空」
    会回退全量，而哨兵（不存在的人）恰恰必然筛空，于是「一致」并不能证明参数被忽略。
    现在改成与**另一个真实的真人**对照：另一个人拿到的东西与你不同 ⇒ 真的按人筛过；
    完全一样 ⇒ 没有作用域；对照人自己一无所获 ⇒ 存疑（**不停用**，退严格模式）。
    """

    def __init__(self, *, require_person_mention: bool = False) -> None:
        self._state = SCOPE_UNKNOWN
        self._require_person_mention = bool(require_person_mention)
        self._report: ScopeProbe | None = None
        self._attempts = 0

    @property
    def state(self) -> str:
        return self._state

    @property
    def report(self) -> ScopeProbe | None:
        """最近一次对照的证据快照；没对照过是 ``None``。"""

        return self._report

    @property
    def disabled(self) -> bool:
        """已证实宿主不按人圈定：这一层必须停用。

        **只有 ``unscoped``**——``uncertain`` 照样继续读，只是要求条目点名本人。
        """

        return self._state == SCOPE_UNSCOPED

    @property
    def attempts(self) -> int:
        """已经做过几次对照（封顶 :data:`MAX_SCOPE_PROBES`）。"""

        return self._attempts

    @property
    def strict_person_mention(self) -> bool:
        """是否强制「条目必须点名本人」：只有确认过作用域生效才敢关掉它。"""

        return self._require_person_mention or self._state != SCOPE_SCOPED

    def note_rejected(self, error: Any) -> bool:
        """检索抛异常时调用；返回 ``True`` 表示「宿主不认这些参数」→ 已停用。"""

        if looks_like_unsupported_argument(error):
            self._state = SCOPE_UNSCOPED
            return True
        return False

    def probe_needed(self, facts: Sequence[str]) -> bool:
        """拿到 ``facts`` 后要不要再做一次对照。

        未验证 / 存疑都值得再试（存疑可能只是「这次没有对照人」），但总次数封顶，
        否则每个 tick 都白烧一次额外 RPC。
        """

        if not facts or self._attempts >= MAX_SCOPE_PROBES:
            return False
        return self._state in (SCOPE_UNKNOWN, SCOPE_UNCERTAIN)

    def note_probe(
        self,
        real_facts: Sequence[str],
        probe_facts: Sequence[str],
        *,
        query_degraded: bool = False,
        decoy_available: bool = True,
    ) -> ScopeProbe:
        """喂进对照结果并定性。

        - 没有对照人 / 对照人什么都没查到 → ``uncertain``（无证据，**不停用**）
        - 两边逐条一致且非空 → ``unscoped``（另一个人拿到了同样的东西）
        - 否则 → ``scoped``（差异只能来自按人过滤）
        """

        self._attempts += 1
        identical = facts_equal(real_facts, probe_facts)
        self._report = ScopeProbe(
            real_count=len([item for item in real_facts if str(item or "").strip()]),
            probe_count=len([item for item in probe_facts if str(item or "").strip()]),
            identical=identical,
            query_degraded=bool(query_degraded),
            real_digests=tuple(fact_digest(item) for item in real_facts[:FACT_DIGEST_SAMPLES]),
            probe_digests=tuple(fact_digest(item) for item in probe_facts[:FACT_DIGEST_SAMPLES]),
            decoy_available=bool(decoy_available),
        )
        if not decoy_available or not self._report.probe_count:
            self._state = SCOPE_UNCERTAIN
        elif identical:
            self._state = SCOPE_UNSCOPED
        else:
            self._state = SCOPE_SCOPED
        return self._report

    def reset(self) -> None:
        """重新自证（配置关掉又打开时用：宿主可能已经换过）。"""

        self._state = SCOPE_UNKNOWN
        self._report = None
        self._attempts = 0


class MemoryLog:
    """记忆层的日志出口：一次性告警 + debug，``logger`` 为 ``None`` 时全静默。

    告警只报一次是刻意的：10 分钟一个 tick，每 tick 刷同一条警告没人看，真出事时
    还会把它顶掉。**坏了又好了要把标记撤掉**（:meth:`forget`），否则下次再坏不吭声。
    """

    def __init__(self, logger: Any = None, plugin_id: str = "") -> None:
        self._logger = logger
        self._plugin_id = str(plugin_id or "")
        self._warned: set[str] = set()

    def bind(self, logger: Any) -> "MemoryLog":
        """绑定 logger（插件的 ``ctx.logger`` 是 bind 之后才有的，每次调用顺手刷新）。"""

        self._logger = logger
        return self

    def warned(self, key: str) -> bool:
        return key in self._warned

    def forget(self, key: str) -> None:
        self._warned.discard(key)

    def debug(self, message: str, *args: Any) -> None:
        if self._logger is None:
            return
        self._logger.debug(f"{self._plugin_id} {message}", *args)

    def warn_once(self, key: str, message: str, *args: Any) -> None:
        if key in self._warned:
            self.debug(message, *args)
            return
        self._warned.add(key)
        if self._logger is None:
            return
        self._logger.warning(f"{self._plugin_id} {message}", *args)


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


# ---------------------------------------------------------------- 读取一个人的记忆


async def fetch_person_facts(
    call_capability: Any,
    *,
    person_id: str,
    query: str = "",
    guard: MemoryScopeGuard,
    cache: FactCache,
    log: MemoryLog,
    limit: int = 5,
    max_chars: int = DEFAULT_FACT_MAX_CHARS,
    bot_tokens: Iterable[object] = (),
    require_person_mention: bool = False,
    decoy_person_id: str = "",
) -> tuple[str, ...] | None:
    """这个人的记忆事实；``None`` = 这次不能用（停用 / 失败），**不写缓存**。

    把「作用域自证 + 检索 + 解析 + 身份过滤」收在一处，``plugin.py`` 只留「取配置 +
    委托」——入口模块的结构预算是门禁化的，一层可选接线不该把它继续撑大。

    **串行**：一次只发一条 RPC（对照那次也严格前后相继），调用方不许 gather——并行
    会撞 A_Memorix 的 SQLite 写锁。

    ``decoy_person_id`` 是「另一个**真实的**人」的 ``person_id``（v1.25.3）。为什么
    不能再用「不存在的人」：宿主按人筛空会回退全量，不存在的人必然筛空，于是它的
    结果永远与真人一致，判据恒假。
    """

    pid = str(person_id or "").strip()
    if not pid or guard.disabled:
        return None
    limit_i = max(1, int(limit))
    raw_query = str(query or "").strip()
    #: 调用方不给查询词时会传 ``pid``（md5）。拿它当向量检索词等于乱查：候选集会退化成
    #: 「此人自己的记忆」，对照判据随之失效。所以这里换成一句可用的短句，**并且照样
    #: 记下「原本退化了」这件事**，供日志定性。
    query_degraded = not raw_query or raw_query == pid
    search_query = MEMORY_FALLBACK_QUERY if query_degraded else raw_query
    try:
        content = await call_capability(
            "knowledge.search",
            query=search_query,
            person_id=pid,
            limit=limit_i,
            mode=MEMORY_SEARCH_MODES[0],
            respect_filter=True,
        )
    except Exception as exc:  # noqa: BLE001 —— 记忆是可选接线，失败不影响主流程
        if guard.note_rejected(exc):
            _disable(cache, log, f"宿主不接受检索参数（{exc}）")
            return None
        _note_failure(log, exc)
        return None
    if isinstance(content, Mapping) and content.get("success") is False:
        _note_failure(log, content.get("error"))
        return None

    raw = facts_from_content(content, max_items=limit_i, max_chars=int(max_chars))
    decoy = str(decoy_person_id or "").strip()
    if guard.probe_needed(raw):
        await _verify_scope(
            call_capability, query=search_query, limit=limit_i, max_chars=max_chars,
            real_facts=raw, guard=guard, log=log, query_degraded=query_degraded,
            decoy_person_id=decoy if decoy != pid else "",
        )
        if guard.disabled:
            report = guard.report
            reason = "与「另一个真人」的对照结果逐条一致（宿主未按人筛）"
            if report is not None:
                reason = f"{reason}；{report.summary()}"
            _disable(cache, log, reason)
            return None
    facts = filter_facts(
        raw,
        # 只有调用方真给了查询词、且它不是 person_id 时才当身份词用
        person_tokens=(raw_query,) if not query_degraded else (),
        bot_tokens=bot_tokens,
        require_person_mention=require_person_mention or guard.strict_person_mention,
    )
    log.debug(
        "长期记忆 person_id=%s 查询=%r(退化=%s) 对照人=%s 原始=%d 过滤后=%d 作用域=%s",
        pid, search_query, query_degraded, "有" if decoy and decoy != pid else "无",
        len(raw), len(facts), guard.state,
    )
    return facts


async def _verify_scope(
    call_capability: Any,
    *,
    query: str,
    limit: int,
    max_chars: int,
    real_facts: Sequence[str],
    guard: MemoryScopeGuard,
    log: MemoryLog,
    query_degraded: bool = False,
    decoy_person_id: str = "",
) -> None:
    """对照一个**真实的另一个人**：同一条查询，只换 ``person_id``。

    判据（v1.25.3）：另一个人拿到的东西与你**不同** ⇒ 服务端确实按人筛过；完全
    一样 ⇒ 没有作用域。对照人自己查不到任何东西 ⇒ **存疑**，不是「无效」——宿主
    ``_filter_hits`` 的 ``return filtered or hits`` 会让「按人筛空」回退全量，这时
    两边的「一致」只说明对方没命中，不能反推参数被忽略。

    **对照不可用 / 探测自己失败都不算证据**：不发 RPC，维持当前状态（未验证或存疑
    ⇒ 严格模式），下一次再验。
    """

    decoy = str(decoy_person_id or "").strip()
    if not decoy:
        log.debug("长期记忆作用域未验证：本次没有可用的「另一个真人」当对照")
        return
    try:
        probe = await call_capability(
            "knowledge.search",
            query=query,
            person_id=decoy,
            limit=limit,
            mode=MEMORY_SEARCH_MODES[0],
            respect_filter=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("长期记忆作用域对照失败（维持当前状态，下次再验）：%s", exc)
        return
    if isinstance(probe, Mapping) and probe.get("success") is False:
        log.debug("长期记忆作用域对照被宿主拒绝：%s", probe.get("error"))
        return
    guard.note_probe(
        real_facts,
        facts_from_content(probe, max_items=limit, max_chars=int(max_chars)),
        query_degraded=query_degraded,
        decoy_available=True,
    )
    report = guard.report
    if report is not None:
        log.debug("长期记忆作用域对照 → %s", report.summary())


def _note_failure(log: MemoryLog, error: Any) -> None:
    log.warn_once(
        "memory_fetch",
        "长期记忆检索失败（本进程只报一次，之后降为 debug）：%s；"
        "请确认宿主已配置 A_Memorix，或关掉 [memory] enabled", error,
    )


def _disable(cache: FactCache, log: MemoryLog, reason: str) -> None:
    """停用整层：已缓存的事实一并作废（它们是在无作用域的前提下取回来的）。

    只有两种情况会走到这里：**与另一个真人的对照完全一致**，或**宿主直接不认参
    数**。「没有对照人」「对照人查不到东西」都判存疑，不停用——把「验不出」当成
    「无效」，等于让一次环境缺失永久关掉功能。
    """

    cache.clear()
    log.warn_once(
        "memory_scope",
        "长期记忆已停用：宿主 knowledge.search 未兑现 person_id / respect_filter 的"
        "作用域（%s）。无作用域的检索会把别人的记忆捞回来，因此本进程不再读取。"
        "如果是「与另一个真人对照逐条一致」判出来的，说明服务端确实没按人筛；"
        "确认宿主支持这两个参数后可重新打开 [memory] enabled", reason,
    )


__all__ = [
    "DEFAULT_FACT_MAX_CHARS",
    "FACT_DIGEST_CHARS",
    "FACT_DIGEST_SAMPLES",
    "FactCache",
    "MAX_SCOPE_PROBES",
    "MEMORY_FALLBACK_QUERY",
    "MemoryLog",
    "MemoryScopeGuard",
    "ScopeProbe",
    "fetch_person_facts",
    "fact_digest",
    "MAX_MATERIAL_CHARS",
    "MEMORY_INTRO",
    "MEMORY_LABEL",
    "MEMORY_MATERIAL_TTL_HOURS",
    "MEMORY_MATERIAL_WEIGHT",
    "MEMORY_SEARCH_MODES",
    "SCOPE_PROBE_PERSON_ID",
    "SCOPE_SCOPED",
    "SCOPE_UNCERTAIN",
    "SCOPE_UNKNOWN",
    "SCOPE_UNSCOPED",
    "fact_to_material",
    "facts_equal",
    "facts_from_content",
    "filter_facts",
    "looks_like_unsupported_argument",
    "materials_for_session",
    "memory_materials",
    "scrub_fact",
]
