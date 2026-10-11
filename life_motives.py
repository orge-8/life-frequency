# -*- coding: utf-8 -*-
"""主动开口动机扩展（motives）：问候 / 生活分享 / 关系维护（纯模块）。

**语义（方案 v2 步 9）**：人不是「有素材才说话」——早上想道个早安、做了件
好玩的事想分享、想起好久没聊的朋友想去问一句。这三类**动机素材**与节日素材、
梦境素材走同一条管道（``state.materials`` → 主动开口挑选），落点全在
**阈值与素材层**，不进倍率：

```text
settle/tick ──▶ _refresh_motives ──▶ state.materials ──▶ _maybe_proactive（既有挑选）
   │                ├─ 问候：早安 / 晚安（时段窗，每生活日至多一条）
   │                ├─ 生活分享：当前活动匹配的「想说说」念头（低概率）
   │                └─ 关系维护：familiarity ≥ 20 且超 3 天没说话的人（SQLite）
   ▼
去重表 motive_seen（(类, 键) → 时刻），重启不重发
```

**三条纪律**：
1. 素材是「想说的念头」，**发不发**仍由既有主动开口管线裁决（阈值/间隔/静默时段
   /社交电量硬闸照旧）——动机不绕过任何闸；
2. 每类每生活日至多一条（关系维护每次至多挑一人），防刷屏；
3. 全部文本第一人称、口语向、不带 user_id（关系维护用 ``relation_hint`` 或
   「一位老朋友」，不把掩码 QQ 号塞进她嘴里）。
"""

from __future__ import annotations

import random
from typing import Any

#: 问候时段窗（本地分钟，半开区间；晚安窗跨午夜用 %1440 归一）
GREETING_WINDOWS: dict[str, tuple[int, int]] = {
    "早安": (6 * 60, 11 * 60),
    "晚安": (22 * 60, 24 * 60 + 1 * 60),
}
#: 生活分享的每 tick 触发概率（tick 默认 600 秒 ⇒ 期望约 2 小时一个念头）
SHARE_PROBABILITY = 0.08
#: 关系维护的建档门槛（与 relations 的「认识」档对齐）与闲置天数
RELATION_MIN_FAMILIARITY = 20.0
RELATION_IDLE_DAYS = 3.0
#: 素材权重与 TTL（小时）
WEIGHT_GREETING = 0.5
WEIGHT_SHARE = 0.5
WEIGHT_RELATION = 0.7
TTL_HOURS_GREETING = 2.0
TTL_HOURS_SHARE = 2.0
TTL_HOURS_RELATION = 6.0

#: 生活分享模板：按活动给出「此刻想说说」的念头（第一人称、一句话）
SHARE_TEMPLATES: dict[str, tuple[str, ...]] = {
    "anime": ("刚看完一集番，有点想找人聊聊剧情", "这部番越看越上头，忍不住想安利"),
    "game": ("刚刚那把打得有点漂亮，想跟人炫耀一下", "游戏打到一半停下来歇会儿，想找人说说话"),
    "music": ("这首歌循环了好几遍，想分享给谁听听", "戴着耳机发呆，突然想找个人聊天"),
    "meal": ("今天这顿饭做得意外的好吃，想说说", "吃饭的时候突然想到，好久没跟大家唠嗑了"),
    "daze": ("发呆的时候冒出个奇怪的念头，想说给人听", "坐着发呆，突然有点想说说话"),
    "daily": ("做了点小事，说不上来为什么想分享", "今天平平淡淡的，突然想找人说两句"),
    "night_study": ("题做烦了，摸鱼摸到想聊天", "学到半夜，想找人说说话缓缓"),
    "off_work": ("下班路上的风很舒服，想说说今天的事", "总算下班了，有点想说说话"),
    "commute": ("路上看到件好玩的事，想讲给谁听", "坐车无聊，翻着聊天记录想找人说话"),
}
_DEFAULT_SHARE_TEMPLATES = ("突然有点想说说话", "今天有点想找人聊聊")


def _material(
    label: str,
    text: str,
    *,
    weight: float,
    ttl_hours: float,
) -> dict[str, Any]:
    """动机 → 主动开口素材（``expires_at``/``best_until`` 先存**时长**，
    ``stamp`` 在 plugin 侧补上绝对时刻——纯模块不碰 ``now``）。"""

    ttl = max(0.5, float(ttl_hours)) * 3600.0
    return {
        "label": label,
        "text": text,
        "weight": float(weight),
        "expires_at": ttl,
        "best_until": ttl * 0.5,
    }


def greeting_material(
    *,
    now_minutes: int,
    day_key: str,
    seen: dict[str, float],
) -> dict[str, Any] | None:
    """问候类：早安 / 晚安时段窗内、本生活日还没发过 → 一条素材。

    ``seen`` 是插件的去重表（键 ``f"问候:{day_key}"``）；生成与去重都在
    纯模块里判，plugin 只负责把键写回去（与 mood 注入同款「成功后标记」纪律）。
    """

    minute = int(now_minutes) % 1440
    label = ""
    for name, (start, end) in GREETING_WINDOWS.items():
        if start <= minute < min(end, 24 * 60):
            label = name
            break
        if end > 24 * 60 and minute < end - 24 * 60:
            # 跨午夜窗的尾段（00:00–01:00）：还属于「今晚该说晚安」的时段
            label = name
            break
    if not label:
        return None
    key = f"问候:{label}:{day_key}"
    if key in seen:
        return None
    text = "早上刚起来，跟大家说声早安呀" if label == "早安" else "快到睡觉的点啦，道个晚安"
    return _material("动机:问候", text, weight=WEIGHT_GREETING,
                     ttl_hours=TTL_HOURS_GREETING) | {"_key": key}


def share_material(
    *,
    activity: str,
    rng: random.Random,
    day_key: str,
    seen: dict[str, float],
) -> dict[str, Any] | None:
    """生活分享类：低概率冒出一个「想说说此刻在做的事」的念头。"""

    key = f"分享:{activity}:{day_key}"
    if key in seen:
        return None
    if rng.random() >= SHARE_PROBABILITY:
        return None
    pool = SHARE_TEMPLATES.get(activity) or _DEFAULT_SHARE_TEMPLATES
    text = pool[rng.randrange(len(pool))]
    return _material("动机:分享", text, weight=WEIGHT_SHARE,
                     ttl_hours=TTL_HOURS_SHARE) | {"_key": key}


def relation_materials(
    records: list[dict[str, Any]] | None,
    *,
    now: float,
    day_key: str,
    seen: dict[str, float],
    min_familiarity: float = RELATION_MIN_FAMILIARITY,
    idle_days: float = RELATION_IDLE_DAYS,
    max_one: int = 1,
) -> list[dict[str, Any]]:
    """关系维护类：熟悉且太久没说话的人 → 「好久没和 TA 说话了」素材。

    ``records`` 来自 relations 表（plugin 侧 ``asyncio.to_thread`` 读）。
    每次至多 ``max_one`` 条（挑「最久没聊的熟人」）；同一个人每个生活日只念叨
    一次。文本不带 user_id：有 ``relation_hint`` 用它，否则「一位老朋友」。
    """

    if not records:
        return []
    idle_seconds = max(0.0, float(idle_days)) * 86400.0
    candidates: list[tuple[float, dict[str, Any]]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            familiarity = float(record.get("familiarity") or 0.0)
            last_at = float(record.get("last_interaction_at") or 0.0)
        except (TypeError, ValueError):
            continue
        if familiarity < min_familiarity or last_at <= 0.0:
            continue
        if now - last_at < idle_seconds:
            continue
        user_id = str(record.get("user_id") or "")
        key = f"关系:{user_id}:{day_key}"
        if key in seen:
            continue
        candidates.append((now - last_at, record))
    candidates.sort(key=lambda pair: -pair[0])  # 最久没聊的排前

    out: list[dict[str, Any]] = []
    for _gap, record in candidates[: max(0, int(max_one))]:
        user_id = str(record.get("user_id") or "")
        hint = str(record.get("relation_hint") or "").strip()
        key = f"关系:{user_id}:{day_key}"
        text = (
            f"好久没和{hint}说话了，有点想知道近况，去打个招呼吧"
            if hint
            else "想起一位很久没聊的朋友，有点想知道 TA 近况，去打个招呼吧"
        )
        out.append(
            _material(
                "动机:关系维护",
                text,
                weight=WEIGHT_RELATION,
                ttl_hours=TTL_HOURS_RELATION,
            )
            | {"_key": key}
        )
    return out


def stamp(material: dict[str, Any], *, now: float) -> dict[str, Any]:
    """去掉内部键、把时长换算成绝对时刻（plugin 侧在拿到 ``now`` 后调用）。"""

    clean = {k: v for k, v in material.items() if not k.startswith("_")}
    now = float(now)
    clean["created_at"] = now
    clean["expires_at"] = now + float(clean.get("expires_at") or 0.0)
    clean["best_until"] = now + float(clean.get("best_until") or 0.0)
    return clean


def key_of(material: dict[str, Any]) -> str:
    """素材的去重键（plugin 写回 ``motive_seen`` 用）；非动机素材返回空串。"""

    return str(material.get("_key") or "")


__all__ = [
    "GREETING_WINDOWS",
    "RELATION_IDLE_DAYS",
    "RELATION_MIN_FAMILIARITY",
    "SHARE_PROBABILITY",
    "SHARE_TEMPLATES",
    "TTL_HOURS_GREETING",
    "TTL_HOURS_RELATION",
    "TTL_HOURS_SHARE",
    "WEIGHT_GREETING",
    "WEIGHT_RELATION",
    "WEIGHT_SHARE",
    "greeting_material",
    "key_of",
    "relation_materials",
    "share_material",
    "stamp",
]