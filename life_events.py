# -*- coding: utf-8 -*-
"""生活事件库与行 DSL 解析（纯模块，无 ctx、无 IO、无全局随机）。

一个「事件」是她在某个活动下可能碰到的一件小事。它做三件事：

1. 改情绪 / 改体力（进入 ``life_sim`` 的状态推进）；
2. 产出一条「想跟你说的素材」（带 TTL，默认 6 小时过期，进 ``materials``）；
3. 带一个 ``weight``（0–1），表达「这件事让她有多想开口」，供主动开口评分使用。

**为什么事件要按活动打标签**：每个 tick 只从「当前活动允许的候选」里抽，所以
「深夜做题」抽不到「抽卡出了」，而「下午游戏」抽得到。活动 → 事件 → 素材 → 主动
话题这条链全由作息活动开头（见 README 的「为什么作息活动权重最大」）。

内建库 52 条（对应参考设定里「23 条自写 + 29 条补充」的规模），可用配置追加或覆盖：

    [events]
    extra = [
      "标签|activities=music,daze|emotion=-0.5|energy=0|weight=0.6|material=想说的那句话|ttl=6",
    ]
    disabled = ["笔没水了"]

行 DSL 说明（``|`` 分隔，首个字段是标签，其余 ``key=value``）：

- ``activities``：逗号分隔的活动名，留空 = 任意清醒活动
- ``emotion`` / ``energy``：``-3`` ~ ``3`` 的小数增量
- ``weight``：``0`` ~ ``1``
- ``material``：素材文本（入库前会被 ``sanitize_text`` 清洗并截断）
- ``ttl``：素材有效小时数，``0.5`` ~ ``72``

坏行只告警、不抛错——配置写错绝不能导致插件加载失败或后台循环退出。
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, replace

# ---------------------------------------------------------------- 常量

DEFAULT_TTL_HOURS = 6.0
"""素材默认有效期（小时），对应参考设定里的「6 小时过期」。"""

MAX_MATERIAL_CHARS = 80
MAX_LABEL_CHARS = 24

_EVENT_KEYS = ("activities", "emotion", "energy", "weight", "material", "ttl")

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: 结构字符：`<>{}` 是 JSON 输出模板的占位符，``【】「」`` 是提示词自己的分节/引用符。
#: 不中和它们的话，一段来路可疑的文本就能在提示词里伪造出一个「【输出要求】」小节
#: （分节符是 `【】`，v1.1.0 漏了），或者用「」冒充系统引用。
_STRUCT_CHARS = re.compile(r"[<>{}`【】「」]")
_WHITESPACE = re.compile(r"\s+")


def sanitize_text(text: object, *, max_chars: int = MAX_MATERIAL_CHARS) -> str:
    """清洗外部/配置来源的文本，供入库与拼提示词使用。

    做四件事：去掉控制字符、去掉 ``<>{}``` ` `` 这类结构字符（防提示词注入的
    基本卫生）、把连续空白压成单空格、按长度截断。这不是完备的注入防御，
    真正的防御在 ``life_activity.build_prompt`` 的固定脚注里。
    """

    cleaned = _CONTROL_CHARS.sub("", str(text or ""))
    cleaned = _STRUCT_CHARS.sub("", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    return cleaned


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class LifeEvent:
    """一条生活事件。``activities`` 为空元组表示「任意清醒活动都可以」。"""

    label: str
    activities: tuple[str, ...] = ()
    emotion: float = 0.0
    energy: float = 0.0
    weight: float = 0.3
    material: str = ""
    ttl_hours: float = DEFAULT_TTL_HOURS

    def matches(self, activity: str) -> bool:
        """这条事件是否允许在给定活动下发生。"""

        return not self.activities or activity in self.activities


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_float(raw: str) -> float | None:
    """解析数值；非有限（``nan``/``inf``/``1e400``）一律当作解析失败。

    ``float("nan")`` 不会抛错，但会一路走到 ``_clamp``：``max(0.0, min(1.0, nan))``
    在 CPython 下取到边界值，于是 ``weight=nan`` 变成「最想开口」、``emotion=nan`` 变成 +3，
    而且**一条告警都没有**——与「不是数字就告警按默认处理」的文档承诺相反。
    """

    try:
        number = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ---------------------------------------------------------------- 行 DSL


def parse_event_line(line: object) -> tuple[LifeEvent | None, list[str]]:
    """解析一行事件 DSL，返回 ``(事件或 None, 警告列表)``。

    返回 ``None`` 表示这行不可用（空行、缺标签、没有任何可识别字段、
    或数值字段无法解析）；警告列表里是给人看的说明。
    """

    warnings: list[str] = []
    raw_line = str(line or "").strip()
    if not raw_line:
        return None, warnings

    fields = [chunk.strip() for chunk in raw_line.split("|")]
    label = sanitize_text(fields[0], max_chars=MAX_LABEL_CHARS)
    if not label:
        return None, ["缺少事件标签，整行跳过"]

    payload: dict[str, str] = {}
    for chunk in fields[1:]:
        if not chunk:
            continue
        if "=" not in chunk:
            warnings.append(f"{label}: 字段 {chunk!r} 不是 key=value，已忽略")
            continue
        key, value = chunk.split("=", 1)
        key = key.strip().lower()
        if key not in _EVENT_KEYS:
            warnings.append(f"{label}: 未知字段 {key!r}，已忽略")
            continue
        payload[key] = value.strip()

    if not payload:
        return None, [f"{label}: 没有任何可识别字段，整行跳过"]

    activities: tuple[str, ...] = ()
    if "activities" in payload:
        activities = tuple(
            sanitize_text(part, max_chars=32)
            for part in re.split(r"[,，]", payload["activities"])
            if sanitize_text(part, max_chars=32)
        )

    emotion = 0.0
    if "emotion" in payload:
        parsed = _parse_float(payload["emotion"])
        if parsed is None:
            warnings.append(f"{label}: emotion={payload['emotion']!r} 不是数字，按 0 处理")
        else:
            emotion = _clamp(parsed, -3.0, 3.0)

    energy = 0.0
    if "energy" in payload:
        parsed = _parse_float(payload["energy"])
        if parsed is None:
            warnings.append(f"{label}: energy={payload['energy']!r} 不是数字，按 0 处理")
        else:
            energy = _clamp(parsed, -3.0, 3.0)

    weight = 0.3
    if "weight" in payload:
        parsed = _parse_float(payload["weight"])
        if parsed is None:
            warnings.append(f"{label}: weight={payload['weight']!r} 不是数字，按 0.3 处理")
        else:
            weight = _clamp(parsed, 0.0, 1.0)

    ttl_hours = DEFAULT_TTL_HOURS
    if "ttl" in payload:
        parsed = _parse_float(payload["ttl"])
        if parsed is None:
            warnings.append(f"{label}: ttl={payload['ttl']!r} 不是数字，按 {DEFAULT_TTL_HOURS} 处理")
        else:
            ttl_hours = _clamp(parsed, 0.5, 72.0)

    material = sanitize_text(payload.get("material", ""), max_chars=MAX_MATERIAL_CHARS)

    return (
        LifeEvent(
            label=label,
            activities=activities,
            emotion=emotion,
            energy=energy,
            weight=weight,
            material=material,
            ttl_hours=ttl_hours,
        ),
        warnings,
    )


def parse_event_lines(lines: object) -> tuple[list[LifeEvent], list[str]]:
    """批量解析行 DSL（``lines`` 可以是任何可迭代对象或单个字符串）。"""

    if isinstance(lines, str):
        candidates: list[object] = [lines]
    elif lines is None:
        candidates = []
    else:
        try:
            candidates = list(lines)
        except TypeError:
            candidates = [lines]

    events: list[LifeEvent] = []
    warnings: list[str] = []
    for line in candidates:
        event, line_warnings = parse_event_line(line)
        warnings.extend(line_warnings)
        if event is not None:
            events.append(event)
    return events, warnings


def merge_events(
    extra_lines: object = None,
    disabled: object = None,
    *,
    builtin: tuple[LifeEvent, ...] | None = None,
) -> tuple[list[LifeEvent], list[str]]:
    """把内建库与配置叠加成最终事件表。

    规则：同标签的 ``extra`` 覆盖内建；``disabled`` 里的标签被移除。返回
    ``(事件列表, 警告列表)``，顺序稳定（内建在前、新增在后），便于测试与日志。
    """

    base = list(BUILTIN_EVENTS if builtin is None else builtin)
    warnings: list[str] = []

    extra_events, extra_warnings = parse_event_lines(extra_lines)
    warnings.extend(extra_warnings)

    if isinstance(disabled, str):
        disabled_labels = {sanitize_text(disabled, max_chars=MAX_LABEL_CHARS)}
    elif disabled is None:
        disabled_labels = set()
    else:
        try:
            disabled_labels = {
                sanitize_text(item, max_chars=MAX_LABEL_CHARS) for item in disabled
            }
        except TypeError:
            disabled_labels = {sanitize_text(disabled, max_chars=MAX_LABEL_CHARS)}
    disabled_labels.discard("")

    merged: dict[str, LifeEvent] = {}
    for event in base:
        merged[event.label] = event
    for event in extra_events:
        if event.label in merged:
            warnings.append(f"{event.label}: 覆盖同名的内建事件")
        merged[event.label] = event

    for label in sorted(disabled_labels):
        if label in merged:
            del merged[label]
        else:
            warnings.append(f"disabled 里的 {label!r} 不存在，已忽略")

    return list(merged.values()), warnings


# ---------------------------------------------------------------- 抽取


def eligible_events(events: object, activity: str) -> list[LifeEvent]:
    """当前活动下允许发生的事件（保持输入顺序，便于确定性测试）。"""

    try:
        candidates = list(events)
    except TypeError:
        return []
    return [event for event in candidates if event.matches(activity)]


def pick_event(
    events: object,
    activity: str,
    rng: random.Random,
    *,
    probability: float = 0.4,
    activity_weights: dict[str, float] | None = None,
) -> LifeEvent | None:
    """按概率抽一条事件；不触发或没有候选时返回 ``None``。

    ``probability`` 是参考设定里的「每 10 分钟 40% 概率」。``rng`` 必须由调用方
    注入（``life_sim`` 传自己那份种子随机源），这样同种子能复现同一段时间线。
    """

    if rng.random() >= _clamp(float(probability), 0.0, 1.0):
        return None

    candidates = eligible_events(events, activity)
    if not candidates:
        return None

    if activity_weights:
        weights = [max(0.0, float(activity_weights.get(e.label, e.weight))) for e in candidates]
        if sum(weights) > 0:
            return rng.choices(candidates, weights=weights, k=1)[0]

    return rng.choice(candidates)


def with_material_prefix(event: LifeEvent, prefix: str) -> LifeEvent:
    """调试/测试用的便捷包装：给素材加个前缀。"""

    return replace(event, material=sanitize_text(prefix + event.material, max_chars=MAX_MATERIAL_CHARS))


# ---------------------------------------------------------------- 内建事件库

BUILTIN_EVENTS: tuple[LifeEvent, ...] = (
    # ---- 日常（daily）----
    LifeEvent("早饭合口味", ("daily",), 0.4, 0.2, 0.45, "今天早饭居然挺合口味，难得"),
    LifeEvent("拆快递", ("daily",), 0.3, -0.1, 0.40, "快递到了，拆的时候还有点小期待"),
    LifeEvent("洗衣忘掏口袋", ("daily",), -0.5, -0.2, 0.35, "洗衣服忘了掏口袋，纸巾糊了一桶"),
    LifeEvent("被问路", ("daily",), 0.4, 0.0, 0.50, "今天居然有人跟我问路，我指得还挺准"),
    LifeEvent("晒被子", ("daily",), 0.5, -0.2, 0.40, "把被子晒了，晚上应该是太阳的味道"),
    LifeEvent("手机快没电", ("daily",), -0.3, -0.1, 0.30, "手机差点没电关机，吓我一跳"),
    LifeEvent("排队很久", ("daily",), -0.6, -0.4, 0.35, "排队排到腿酸，前面那位还在慢慢找零钱"),
    LifeEvent("收拾桌子", ("daily",), 0.5, -0.3, 0.40, "桌子收拾干净了，心情莫名好了一点"),
    LifeEvent("买到最后一个", ("daily",), 0.6, 0.0, 0.55, "我到的时候只剩最后一个了，运气不错"),
    LifeEvent("淋了一点雨", ("daily",), -0.4, -0.3, 0.40, "出门没带伞，淋了一小段"),
    LifeEvent("遇到亲人的猫", ("daily",), 0.7, 0.0, 0.65, "楼下有只猫，居然肯让我摸"),
    LifeEvent("忘了要买什么", ("daily",), -0.2, 0.0, 0.30, "进超市转了两圈，忘了本来要买什么"),
    LifeEvent("煮糊了", ("daily",), -0.7, -0.3, 0.50, "煮着煮着忘了，锅底糊了一层"),
    LifeEvent("久未联系的人来信", ("daily",), 0.6, 0.0, 0.70, "一个很久没联系的人突然发消息给我"),
    # ---- 深夜做题（night_study）----
    LifeEvent("一道题卡住", ("night_study",), -0.8, -0.7, 0.60, "有一步怎么都想不通，卡了快一个小时"),
    LifeEvent("突然想通", ("night_study",), 1.2, 0.2, 0.75, "刚才卡住的地方突然通了，很想喊一声"),
    LifeEvent("笔没水了", ("night_study",), -0.4, -0.2, 0.30, "写到一半笔没水了，翻遍抽屉没找到替换的"),
    LifeEvent("翻到旧本子", ("night_study",), -0.3, 0.0, 0.60, "翻到以前的本子，字比我印象里丑多了"),
    LifeEvent("水放凉了", ("night_study",), -0.2, 0.0, 0.35, "桌角那杯水放凉了，一口没喝"),
    LifeEvent("越写越精神", ("night_study",), 0.5, 0.3, 0.50, "本来困了，写着写着反而清醒了"),
    LifeEvent("手机响个不停", ("night_study",), -0.5, -0.2, 0.40, "手机一直在响，我看了好几眼"),
    LifeEvent("答案全对", ("night_study",), 0.9, -0.2, 0.70, "最后对答案，居然全对"),
    # ---- 听歌（music）----
    LifeEvent("随机到老歌", ("music",), 0.7, 0.0, 0.65, "随机播到一首很久没听的老歌，前奏一出来就愣住了"),
    LifeEvent("单曲循环", ("music",), 0.4, 0.0, 0.50, "这首歌我循环了一晚上，还没腻"),
    LifeEvent("耳机坏了一边", ("music",), -0.6, 0.0, 0.35, "耳机左耳好像没声了，晃了两下也没好"),
    LifeEvent("歌词很戳", ("music",), -0.5, 0.0, 0.70, "有句歌词听着听着就不太对了"),
    LifeEvent("跟唱跑调", ("music",), 0.4, 0.0, 0.40, "刚才跟着唱，跑调跑得挺离谱"),
    LifeEvent("挖到新歌手", ("music",), 0.6, 0.0, 0.60, "挖到一个没听过的歌手，第一首就很对味"),
    LifeEvent("推荐算法跑偏", ("music",), -0.1, 0.0, 0.30, "今天推荐给我的歌都怪怪的"),
    # ---- 游戏（game）----
    LifeEvent("卡在同一个boss", ("game",), -0.9, -0.5, 0.60, "同一个 boss 我死了不知道多少次"),
    LifeEvent("抽卡出货", ("game",), 1.3, -0.2, 0.80, "随手一发居然出了"),
    LifeEvent("队友离谱", ("game",), -0.7, -0.3, 0.55, "今天匹配到的队友有点离谱"),
    LifeEvent("支线通关", ("game",), 0.8, -0.4, 0.65, "磨了很久的支线终于通了"),
    LifeEvent("删了存档", ("game",), -1.2, -0.2, 0.70, "我手滑把一个存档删了"),
    # ---- 看番（anime）----
    LifeEvent("追的番更新", ("anime",), 0.8, -0.2, 0.70, "追的那部更新了，这集信息量好大"),
    LifeEvent("喜欢的角色退场", ("anime",), -1.1, 0.0, 0.75, "我喜欢的那个角色居然就这么退场了"),
    LifeEvent("一口气看六集", ("anime",), 0.5, -0.6, 0.45, "说好只看一集，结果一口气看了六集"),
    LifeEvent("看哭了", ("anime",), -0.6, -0.3, 0.70, "看到一半眼泪就下来了，有点丢人"),
    LifeEvent("弹幕同款心声", ("anime",), 0.4, 0.0, 0.35, "那句台词一出来，弹幕全是我心里的想法"),
    # ---- 发呆（daze）----
    LifeEvent("窗边发呆", ("daze",), -0.2, 0.2, 0.40, "在窗边坐了一会儿，什么也没干"),
    LifeEvent("忘了今天星期几", ("daze",), 0.0, 0.0, 0.30, "发呆发到想不起来今天星期几"),
    LifeEvent("想起很久以前", ("daze",), -0.5, 0.0, 0.65, "坐着坐着就开始想以前的事"),
    LifeEvent("楼下有人放歌", ("daze",), 0.3, 0.0, 0.35, "楼下有人在放歌，隔着窗户听得模模糊糊"),
    LifeEvent("天暗了才发现", ("daze",), -0.1, 0.0, 0.40, "回过神的时候天已经暗了"),
    LifeEvent("想找人说说话", ("daze",), -0.3, 0.0, 0.85, "不知道为什么，突然有点想找人说说话"),
    # ---- 睡前（before_sleep）----
    LifeEvent("躺下反而清醒", ("before_sleep",), -0.2, -0.1, 0.45, "一躺下反而清醒了"),
    LifeEvent("刷手机停不下来", ("before_sleep",), 0.0, -0.4, 0.40, "说好再刷五分钟，抬头已经过去半小时"),
    LifeEvent("想起白天一句话", ("before_sleep",), -0.4, 0.0, 0.60, "躺下突然想起白天有人说的一句话"),
    LifeEvent("今天过得还行", ("before_sleep",), 0.6, 0.0, 0.55, "今天好像也没什么大事，但过得还行"),
    LifeEvent("明天要早起", ("before_sleep",), -0.3, -0.2, 0.40, "明天要早起，但一点都不想睡"),
    # ---- 养病（sick_rest）----
    LifeEvent("嗓子疼", ("sick_rest",), -0.8, -0.5, 0.50, "嗓子疼得厉害，咽口水都费劲"),
    LifeEvent("出了身汗", ("sick_rest",), -0.4, 0.3, 0.30, "出了一身汗，好像退下去一点了"),
)

assert len(BUILTIN_EVENTS) == 52, f"内建事件库应为 52 条，实际 {len(BUILTIN_EVENTS)}"
