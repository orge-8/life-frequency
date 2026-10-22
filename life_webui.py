# -*- coding: utf-8 -*-
"""WebUI 自定义页面（``webui.json``）的投影层（v1.22.5）。

把插件内部状态整成 dashboard **声明式页面**能吃的 JSON。与配置页管线
（``get_webui_config_schema`` / SDK config schema）完全无关：那是另一条管线，
``webui.json`` 协议 v2 走的是 ``@API`` 绑定，宿主 v1.3.8 起可用。

三条硬契约（宿主 v1.3.8 源码逐条核对，``dashboard/src/lib/plugin-webui.ts``
的 ``resolveReference`` / ``display``）：

1. **绑定字段恒存在**：前端对缺失的 DataReference 字段**直接抛错**，整页变
   invalidData（``Missing data field``）。所以每个被页面绑定的字段都有兜底值，
   绝不「有时有、有时没有」。
2. **text/stat 只吃标量**：``display()`` 遇对象/数组抛错（``Expected a scalar
   display value``）。班表、经济、社交、世界、归因这些列表信息全部在**插件侧**
   拼成字符串；只有 ``table`` 节点吃 dict 列表。
3. **不抛异常**：任何一段失败只降级该段（``ready=False`` + 一句说明）。宿主把
   异常转成「插件 API 执行失败」，用户在页面上看不到原因——排障成本全在真机日志。

与命令卡的口径关系：本模块**复用命令同款的取数函数**（``_compute_breakdown`` /
``_economy_card_lines`` / ``relations_prompt_lines`` …），不另造一套算法；
两处口径若分叉，由 ``tests/test_webui_pages.py`` 的对账用例钉住。

本模块刻意不 import maibot_sdk、不碰 ``ctx``：它读插件实例已经算好的东西
（state / config / breakdown / 卡片行函数），写操作只有暂停/恢复两个入口，
且与 ``/生活 暂停``、``/生活 恢复`` 走**同一份**执行体（见 ``set_paused``）。
关系档案的 SQLite 读走 ``asyncio.to_thread``——与 v1.13.1（F-002）的命令侧
纪律同一条：盘上读不许占事件循环。
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Callable

try:
    from .life_events import sanitize_text
    from .life_factors import reason_label
    from .life_host_model import MODE_LABELS, preview
    from .life_relations import mask_user_id, prompt_lines as relations_prompt_lines, tier_of as relation_tier
    from .life_sim import (
        activity_minutes,
        attribution_lines,
        awake_hours_today,
        health_label_admin,
        local_datetime,
        sleep_hours_today,
    )
except ImportError:  # 平铺加载（本地测试 / 旧宿主）：两条路径都必须成立
    from life_events import sanitize_text  # type: ignore[no-redef]
    from life_factors import reason_label  # type: ignore[no-redef]
    from life_host_model import MODE_LABELS, preview  # type: ignore[no-redef]
    from life_relations import (  # type: ignore[no-redef]
        mask_user_id,
        prompt_lines as relations_prompt_lines,
        tier_of as relation_tier,
    )
    from life_sim import (  # type: ignore[no-redef]
        activity_minutes,
        attribution_lines,
        awake_hours_today,
        health_label_admin,
        local_datetime,
        sleep_hours_today,
    )

#: 关系表行数上限：夹到与命令侧 ``/生活 关系`` 同一个量级，防返回值撞宿主
#: 20000 元素上限（页面是 5 秒一刷的，行数本来就只做概览）。
RELATION_ROW_LIMIT = 50

#: 单个文本字段的封顶（宿主 Text 节点声明上限 4000；留出余量防截断在半个字上）。
TEXT_CAP = 2000

#: 降级占位（字段恒存在契约的兜底）：任何一段炸了都用它顶上，页面照常渲染。
_DEGRADED_STATUS: dict[str, Any] = {
    "ready": False,
    "paused": False,
    "adjust": 0.0,
    "adjust_text": "—",
    "effective": 0.0,
    "effective_text": "—",
    "emotion": 0.0,
    "energy": 0.0,
    "energy_cap": 10.0,
    "satiety": 0.0,
    "emotion_pct": 0.0,
    "energy_pct": 0.0,
    "satiety_pct": 0.0,
    "sleep_text": "状态读取失败",
    "activity_line": "状态读取失败（详见真机日志）",
    "date_line": "—",
    "schedule_text": "—",
    "health_line": "状态读取失败",
    "inner_line": "—",
    "top_relation": "—",
    "model_status": "—",
    "wiring_text": "—",
    "material_text": "—",
    "host_line": "—",
    "scope_line": "—",
    "proactive_text": "—",
    "backoff_text": "—",
    "pause_line": "状态读取失败",
}


# ------------------------------------------------------------------ 标量兜底


def _num(value: Any, default: float = 0.0) -> float:
    """有限浮点（NaN/Inf/非数一律回落默认值——前端 progress 对非有限值抛错）。"""

    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _pct(value: Any, scale: float = 10.0) -> float:
    """0–100 的进度条值：宿主 ``progress`` 节点值域硬约束，越界整页抛错。"""

    if scale <= 0:
        return 0.0
    return max(0.0, min(100.0, _num(value) / scale * 100.0))


def _text(value: Any, default: str = "") -> str:
    """标量文本（None → 默认；对象递归拒绝——只接受能直接 display 的东西）。"""

    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    return default


def _clip(text: str, cap: int = TEXT_CAP) -> str:
    """超长截断并显式标注（静默截断会让用户以为数据就只有这么多）。"""

    out = _text(text)
    if len(out) <= cap:
        return out
    return out[:cap] + "……（已截断）"


def _join(lines: Any, default: str = "—") -> str:
    """列表 → 换行拼接（空列表给默认值，绝不返回空串——空串在页面上等于没有这行）。"""

    items = [_text(item) for item in (lines or []) if _text(item)]
    return "\n".join(items) if items else default


def _stamp(at: Any, tz_offset_minutes: int) -> str:
    """epoch → 「MM-DD HH:MM」（插件本地时区）；0/坏值 → 「—」。"""

    value = _num(at)
    if value <= 0:
        return "—"
    try:
        return local_datetime(value, tz_offset_minutes).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


# ------------------------------------------------------------------ 四段快照


def status_snapshot(plugin: Any, now: float) -> dict[str, Any]:
    """「生活状态」段：与 ``/生活 状态`` 同源（同一批取数函数），字段恒存在。

    倍率取 ``_last_breakdown or _compute_breakdown``——与状态卡逐字同款
    （卡面优先用上一轮扫过的缓存，避免每次看卡片都重算）。
    """

    state = plugin._state
    config = plugin.config
    sim_config = plugin._sim_config()
    breakdown = plugin._last_breakdown or plugin._compute_breakdown(now)
    mode_label = MODE_LABELS.get(plugin._host_mode, plugin._host_mode)
    talk_value, talk_source = plugin._talk_value_for("")
    adjust = _num(breakdown.adjust, 1.0)
    paused = bool(plugin._is_paused())

    lines_out: list[str] = []
    activity = _text(state.activity)
    scene = sanitize_text(getattr(state, "scene", ""), max_chars=40)
    head = f"活动：{plugin._activity_label(activity)}"
    if scene:
        head += f"（{scene}）"
    lines_out.append(head)
    lines_out.append(
        f"　来源：{plugin._source_label(getattr(state, 'activity_source', ''))}"
        f" · 已持续 {activity_minutes(state, now)} 分钟"
    )

    date_line = "—"
    try:
        local_now = local_datetime(now, sim_config.tz_offset_minutes)
        day = plugin._calendar_day(local_now)
        date_line = f"{local_now:%Y-%m-%d} 周{'一二三四五六日'[local_now.weekday()]}"
        if day is not None and getattr(day, "name", ""):
            date_line += f" {day.name}"
    except Exception:  # noqa: BLE001 —— 日期行渲染失败不挡整段（与状态卡同款容忍）
        pass

    wake_note = _text(plugin._wake_note(now))
    if wake_note:
        lines_out.append(f"　被 @ 唤醒：清醒到 {wake_note}（之后自动回睡；睡眠记账不受影响）")

    schedule = list(plugin._schedule_lines(now))
    physio_note = _text(plugin._physio_window_note(sim_config))
    health = _text(health_label_admin(state, now, sim_config))
    sleep_line = (
        f"今日已睡 {sleep_hours_today(state):.1f} 小时 / 清醒 {awake_hours_today(state):.1f} 小时"
        "（本生活日已记账）"
    )
    satiety_line = (
        f"饱腹 {_num(state.satiety):.1f}/10　今日已吃 {int(_num(state.meal_count_today))} 顿"
        + (f"　{physio_note}" if physio_note else "")
        if sim_config.physio_enabled
        else ""
    )

    inner_line = "—"
    if config.mood.enabled:
        inner_line = (
            f"内心：压力 {_num(state.stress):.1f}/10　孤独 {_num(state.loneliness):.1f}/10"
            f"　社交电量 {_num(state.social_battery):.1f}/10"
        )

    # 最熟的人：页面**刻意不做宿主长期记忆 RPC**（5 秒一刷的页面不值得每轮一次
    # 主机 RPC）；带「；记得 X」的完整口径留在 /生活 状态 与 /生活 关系。
    top_relation = "—"
    relation_count = 0
    if config.relations.enabled and getattr(plugin, "_routine_store", None) is not None:
        try:
            records = plugin._routine_store.top_relationships(1)
            relation_count = len(records)
            prompt = relations_prompt_lines(records, max_lines=1)
            if prompt:
                top_relation = prompt[0]
            elif records:
                top_relation = "最熟的人：还没攒够熟悉度"
        except Exception:  # noqa: BLE001 —— 展示失败不挡整段
            top_relation = "最熟的人：读取失败"

    model_status = "—"
    fail_streak = int(_num(state.llm_fail_streak))
    cooldown_left = max(0.0, _num(state.llm_cooldown_until) - now) / 60.0
    if fail_streak or cooldown_left > 0:
        model_status = f"模型状态：连续失败 {fail_streak} 次"
        if cooldown_left > 0:
            model_status += f"　冷却剩余 {cooldown_left:.0f} 分钟"
        raw = _text(getattr(state, "llm_last_raw", ""))
        if raw:
            model_status += f"　最近一次：{sanitize_text(raw, max_chars=60)}"

    wiring = [
        *list(plugin._economy_card_lines(now)),
        *list(plugin._social_card_lines(now)),
        *list(plugin._world_card_lines(now)),
    ]

    target_count = int(_num(getattr(plugin, "_last_target_count", 0)))
    scope_line = f"生效范围：{config.apply.filter_mode}（本轮命中 {target_count} 个会话）"
    skipped = int(_num(getattr(plugin, "_last_skipped_idle", 0)))
    if skipped:
        scope_line += f"　已跳过 {skipped} 个无活动迹象的历史会话"
    if target_count == 0:
        scope_line += "　⚠ 一个都没命中：filter_mode 写错或白名单为空时本插件不会干预任何会话"

    proactive_scope = "全部会话"
    if str(config.proactive.filter_mode or "all").strip().lower() != "all":
        proactive_scope = (
            f"{'白名单' if str(config.proactive.filter_mode).strip().lower() == 'whitelist' else '黑名单'}"
            f" {len(config.proactive.target_chats)} 个"
        )
    proactive_text = "主动开口：" + ("已启用" if config.proactive.enabled else "未启用")
    if config.proactive.enabled:
        proactive_text += f"（范围：{proactive_scope}）"
    if config.simulation.dry_run:
        proactive_text += "　|　演算模式（未写宿主）"

    if paused:
        pause_line = "当前倍率：已停止干预，归还外部基数（没有外部写入者时就是 1.0）"
    elif breakdown.reason != "ok":
        if adjust > 0:
            pause_line = (
                f"当前倍率：{adjust:.3f}（{reason_label(breakdown.reason)}，"
                "已按静默下限保留主动开口；普通闲聊仍几乎不触发）"
            )
        else:
            pause_line = (
                f"当前倍率：写回 0.0（{reason_label(breakdown.reason)}，"
                "她完全静默、零模型开销）"
            )
    else:
        pause_line = f"当前倍率：{adjust:.3f}　→ 生效频率 {talk_value * adjust:.3f}（{talk_source} {talk_value:.3f}）"

    return {
        "ready": True,
        "paused": paused,
        "enabled": bool(config.plugin.enabled),
        "adjust": adjust,
        "adjust_text": f"{adjust:.3f}",
        "effective": talk_value * adjust,
        "effective_text": f"{talk_value * adjust:.3f}",
        "emotion": _num(state.emotion, 5.0),
        "energy": _num(state.energy, 5.0),
        "energy_cap": _num(state.energy_cap, 10.0),
        "satiety": _num(state.satiety, 5.0),
        "emotion_pct": _pct(state.emotion),
        "energy_pct": _pct(state.energy),
        "satiety_pct": _pct(state.satiety),
        "sleep_text": _clip(f"{sleep_hours_today(state):.1f}h / {awake_hours_today(state):.1f}h"),
        "activity_line": _clip("\n".join(lines_out)),
        "date_line": _clip(date_line),
        "schedule_text": _clip(_join(schedule, "（未配置作息班表）")),
        "health_line": _clip("\n".join(item for item in (health, sleep_line, satiety_line) if item)),
        "inner_line": _clip(inner_line),
        "top_relation": _clip(top_relation),
        "relation_count": relation_count,
        "model_status": _clip(model_status),
        "wiring_text": _clip(_join(wiring, "（经济 / 社交 / 世界三层均未接线或暂无内容）")),
        "material_text": _clip(_text(plugin._material_line(now, sim_config), "—")),
        "host_line": _clip(f"宿主模式：{mode_label}　{talk_source}：{talk_value:.3f}"),
        "scope_line": _clip(scope_line),
        "proactive_text": _clip(proactive_text),
        "backoff_text": _clip(_text(plugin._backoff_line(now), "—")),
        "pause_line": _clip(pause_line),
    }


def frequency_snapshot(plugin: Any, now: float) -> dict[str, Any]:
    """「倍率拆解」段：与 ``/生活 频率`` 同源，但**只取全局口径**。

    刻意不做会话侧实测回读（``frequency.get_adjust``）：页面没有会话上下文，
    硬塞一个会话号反而会让用户以为那是全局值。实测口径留在命令里。
    本函数**不写** ``plugin._last_breakdown``——查询是只读的，不该顺手改卡面缓存。
    """

    config = plugin.config
    sim_config = plugin._sim_config()
    breakdown = plugin._compute_breakdown(now)
    mode_label = MODE_LABELS.get(plugin._host_mode, plugin._host_mode)
    talk_value, talk_source = plugin._talk_value_for("")
    adjust = _num(breakdown.adjust, 1.0)
    composed = adjust  # 无会话上下文：不认外部基数，下发倍率就是生活倍率本身

    head = f"宿主模式：{mode_label}（来源：{plugin._host_mode_source}）　{talk_source}：{talk_value:.3f}"
    gate_parts: list[str] = [head]

    if breakdown.reason != "ok":
        gate_parts.append(f"硬闸命中：{reason_label(breakdown.reason)} → 倍率 {adjust:.3f}")
    else:
        gate_parts.append(f"曲线组：{breakdown.curve_set}")
        absent = tuple(getattr(plugin, "_activity_factor_absent", ()) or ())
        if absent:
            gate_parts.append(
                "⚠ 因子表 replace 模式缺内置键："
                + "、".join(_text(item) for item in absent)
                + "（这些活动按 1.0 处理，拆解里不显示）"
            )
        wake_note = _text(plugin._wake_note(now))
        if wake_note:
            gate_parts.append(f"⚠ 此刻正被 @ 唤醒（清醒到 {wake_note}）：倍率按清醒活动算")
        fallback = max(0.0, _num(config.frequency.min_adjust))
        if fallback > 0 and _num(breakdown.raw) + _num(breakdown.material_bonus) < fallback:
            gate_parts.append(f"（被下限 {fallback:g} 抬高）")

    factors: list[dict[str, str]] = []
    if breakdown.reason == "ok":
        factors.append({"factor": "原始倍率", "value": f"{_num(breakdown.raw):.3f}"})
        for name, value in breakdown.factors or ():
            factors.append({"factor": _text(name), "value": f"{_num(value):.3f}"})
        if _num(breakdown.material_bonus):
            factors.append({
                "factor": f"素材加成（有效 {_num(breakdown.material_count):g} 条）",
                "value": f"+{_num(breakdown.material_bonus):.3f}",
            })
        factors.append({"factor": "最终倍率", "value": f"{adjust:.3f}"})

    compose_text = (
        "合成：下发 = 外部基数 × 生活倍率（与 budget-pacer 共存）"
        if config.apply.compose_external
        else "合成：已关闭，本插件直接覆盖宿主倍率"
    )
    if config.simulation.dry_run:
        compose_text += "（演算模式：以上都是计算结果，没有真的写到宿主）"

    out = preview(mode=plugin._host_mode, talk_value=talk_value, adjust=composed, pending_count=1)
    verdict_parts: list[str] = [_text(out.get("verdict"), "—")]
    if out.get("probability_gate"):
        verdict_parts.append(
            f"动态门：目标回复比例 {_num(out.get('dynamic_keep_ratio')):.3f}"
            f"（统计窗口 {_num(out.get('dynamic_window_seconds')) / 3600:.0f} 小时）"
            f"　·　小样本静态概率门槛 {_num(out.get('dynamic_static_threshold')):.3f}"
        )
    else:
        needed = out.get("plain_chatter_messages_needed")
        verdict_parts.append(
            "纯闲聊需要："
            + (str(int(needed)) if needed is not None else "不可达（只回 @/提及/私聊或带问题的消息）")
        )
        threshold = out.get("threshold")
        if threshold is not None:
            verdict_parts.append(f"触发阈值：{int(threshold)} 条消息")
        if "necessity_factor" in out:
            verdict_parts.append(
                f"必要性系数：{_num(out.get('necessity_factor')):.3f}（评分线 {out.get('score_line')}）"
            )

    return {
        "ready": True,
        "reason": _text(breakdown.reason),
        "reason_label": _clip(reason_label(breakdown.reason)),
        "adjust": adjust,
        "effective": talk_value * composed,
        "gate_text": _clip("\n".join(gate_parts)),
        "factors": factors,
        "compose_text": _clip(compose_text),
        "verdict_text": _clip("\n".join(verdict_parts)),
    }


def attribution_snapshot(plugin: Any, now: float) -> dict[str, Any]:
    """「情绪体力归因」段：``life_sim.attribution_lines`` 原样拼接（format=log）。

    纯展示、零行为变更；渲染失败降级成一句说明——这是排查工具，恰恰在状态怪的
    时候最需要它可读，绝不能反过来抛错（与 ``/生活 归因`` 同款容忍）。
    """

    try:
        lines = list(attribution_lines(plugin._state, now, plugin._sim_config()))
        text = "\n".join(_text(item) for item in lines if _text(item))
        if not text:
            return {"ready": True, "text": "暂无可归因的时段（活动切换不回溯，只读最近经历与活动锚点）。"}
        return {
            "ready": True,
            "text": _clip(text + "\n口径：只读最近经历与活动锚点，不改任何结算；活动切换不回溯。"),
        }
    except Exception as exc:  # noqa: BLE001 —— 展示失败不该让页面报错
        _warn(plugin, "归因输出失败：%s", exc)
        return {"ready": False, "text": "情绪体力归因：渲染失败（详见真机日志）"}


def relation_rows(records: list[dict[str, Any]], tz_offset_minutes: int) -> list[dict[str, Any]]:
    """关系档案记录 → 表格行（dict 列表；QQ 号走统一脱敏口径）。

    ``table.columns[].field`` 按字段取值，返回标量列表渲染不出任何一列——
    必须是 dict 列表（宿主前端 ``plugin-webui.tsx`` 的 table 分支）。
    """

    rows: list[dict[str, Any]] = []
    for record in records or []:
        try:
            familiarity = _num(record.get("familiarity"))
        except AttributeError:  # pragma: no cover - 记录不是 Mapping 时跳过
            continue
        uid = _text(record.get("user_id"))
        masked = mask_user_id(uid) if uid else "未知"
        hint = sanitize_text(record.get("relation_hint"), max_chars=40)
        rows.append({
            "person": _clip(f"{hint}（{masked}）" if hint else masked, 80),
            "familiarity": f"{familiarity:.1f}",
            "tier": _text(relation_tier(familiarity)),
            "interactions": int(_num(record.get("interaction_count"))),
            "last_seen": _stamp(record.get("last_interaction_at"), tz_offset_minutes),
            "hint": _clip(hint, 80) if hint else "—",
        })
    return rows


def relations_snapshot(
    plugin: Any,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """「关系档案」段的统计部分（记录由调用方读好传进来，SQLite 读在 ``dashboard``）。"""

    config = plugin.config
    if not config.relations.enabled:
        return {"ready": False, "note": "关系层已关闭（[relations] enabled = false）：所有人按陌生人处理",
                "count": 0, "avg_text": "—", "median_text": "—", "max_text": "—", "rows": []}
    if getattr(plugin, "_routine_store", None) is None:
        return {"ready": False, "note": "私有库不可用：关系档案读不到（生活状态照常推进）",
                "count": 0, "avg_text": "—", "median_text": "—", "max_text": "—", "rows": []}
    if not records:
        return {"ready": True, "note": "还没有档案：私聊、被 @、回复她才会建档（群里其余人当背景板）",
                "count": 0, "avg_text": "—", "median_text": "—", "max_text": "—", "rows": []}

    values = sorted(_num(item.get("familiarity")) for item in records)
    return {
        "ready": True,
        "note": "页面只读概览（不做宿主长期记忆 RPC）；完整口径见 /生活 关系。",
        "count": len(values),
        "avg_text": f"{sum(values) / len(values):.1f}",
        "median_text": f"{values[len(values) // 2]:.1f}",
        "max_text": f"{values[-1]:.1f}",
        "rows": relation_rows(records, plugin._sim_config().tz_offset_minutes),
    }


# ------------------------------------------------------------------ 组装与写入口


async def _read_relation_records(plugin: Any, limit: int) -> list[dict[str, Any]]:
    """关系记录读盘（走线程池，与命令侧 F-002 纪律同一条）；失败返回空列表。"""

    store = getattr(plugin, "_routine_store", None)
    if store is None:
        return []
    try:
        records = await asyncio.to_thread(store.top_relationships, limit)
    except Exception as exc:  # noqa: BLE001 —— 读盘失败只降级关系段
        _warn(plugin, "关系档案读取失败：%s", exc)
        return []
    return [item for item in (records or []) if isinstance(item, dict)]


async def dashboard(plugin: Any, now: float) -> dict[str, Any]:
    """页面唯一的查询：状态 / 倍率 / 归因 / 关系四段打包。

    单查询而不是四个：前端 ``loadQueries`` 是**顺序执行**的，四段就是一个快照的
    四块；合成一个查询还顺带保证四段用的是同一个 ``now``（四段各算各的会出现在
    两次查询之间倍率变了的撕裂观感）。任一段炸了只降级该段。
    """

    sections: dict[str, dict[str, Any]] = {}

    for name, builder in (
        ("status", lambda: status_snapshot(plugin, now)),
        ("frequency", lambda: frequency_snapshot(plugin, now)),
        ("attribution", lambda: attribution_snapshot(plugin, now)),
    ):
        try:
            sections[name] = builder()
        except Exception as exc:  # noqa: BLE001 —— 一段失败不拖垮整页
            _warn(plugin, "WebUI %s 段失败：%s", name, exc)
            sections[name] = _degraded(name)

    try:
        records = await _read_relation_records(plugin, RELATION_ROW_LIMIT)
        sections["relations"] = relations_snapshot(plugin, records)
    except Exception as exc:  # noqa: BLE001 —— 关系段失败只降级关系段
        _warn(plugin, "WebUI relations 段失败：%s", exc)
        sections["relations"] = _degraded("relations")

    snapshot = dict(sections)
    snapshot["now"] = _text(now)
    return snapshot


def _degraded(name: str) -> dict[str, Any]:
    """降级占位：字段集与正常返回**完全一致**（绑定字段恒存在契约）。"""

    if name == "status":
        return dict(_DEGRADED_STATUS)
    if name == "frequency":
        return {
            "ready": False, "reason": "error", "reason_label": "读取失败",
            "adjust": 0.0, "effective": 0.0, "gate_text": "倍率拆解读取失败（详见真机日志）",
            "factors": [], "compose_text": "—", "verdict_text": "—",
        }
    if name == "attribution":
        return {"ready": False, "text": "情绪体力归因：读取失败（详见真机日志）"}
    return {
        "ready": False, "note": "关系档案读取失败（详见真机日志）",
        "count": 0, "avg_text": "—", "median_text": "—", "max_text": "—", "rows": [],
    }


async def set_paused(plugin: Any, paused: bool) -> dict[str, Any]:
    """暂停 / 恢复：与 ``/生活 暂停``、``/生活 恢复`` **同一份执行体**。

    命令侧的两个分支逐字照搬（``paused_override`` + ``_restore_baseline`` +
    ``_save_state``），页面只是另一个入口——不新造第二套状态机。

    鉴权说明：WebUI 调用不带 QQ 侧身份（``plugin.invoke_api`` 只传声明过的
    参数），命令侧的 ``_is_admin`` 在这里必然 fail-closed；dashboard 本身在
    MaiBot 登录态之后，与 identity-guard 的写操作同款口径（登录即可用）。
    """

    state = plugin._state
    target = bool(paused)
    current = bool(getattr(state, "paused_override", False))
    if not plugin.config.plugin.enabled:
        return {"ok": False, "paused": current, "changed": False,
                "reason": "生活频率插件已停用（可在插件配置里重新启用）"}
    if current == target:
        return {"ok": True, "paused": current, "changed": False,
                "reason": "状态未变（已是目标状态）"}

    if target:
        state.paused_override = True
        try:
            await plugin._restore_baseline()
        except Exception as exc:  # noqa: BLE001 —— 归还失败不挡暂停本身（下轮扫会重试）
            _warn(plugin, "暂停时归还基数失败（稍后重试）：%s", exc)
        plugin._save_state()
        _info(plugin, "WebUI 暂停频率干预（与 /生活 暂停 同路径）")
        reason = "已暂停频率干预（生活状态继续推进，倍率归还给外部基数；重启后仍然有效）"
    else:
        state.paused_override = False
        plugin._state_dirty = True
        plugin._save_state()
        _info(plugin, "WebUI 恢复频率干预（与 /生活 恢复 同路径）")
        reason = "已恢复频率干预"

    return {"ok": True, "paused": target, "changed": True, "reason": reason}


# ------------------------------------------------------------------ 日志小灶


def _warn(plugin: Any, message: str, *args: Any) -> None:
    try:
        plugin.ctx.logger.warning(message, *args)
    except Exception:  # noqa: BLE001 —— 日志本身不许炸掉调用方
        pass


def _info(plugin: Any, message: str, *args: Any) -> None:
    try:
        plugin.ctx.logger.info(message, *args)
    except Exception:  # noqa: BLE001 —— 同上
        pass


__all__ = [
    "RELATION_ROW_LIMIT",
    "attribution_snapshot",
    "dashboard",
    "frequency_snapshot",
    "relation_rows",
    "relations_snapshot",
    "set_paused",
    "status_snapshot",
]
