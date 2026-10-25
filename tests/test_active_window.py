# -*- coding: utf-8 -*-
"""活跃会话判定改成时间窗口（v1.23.2，[apply].active_window_hours 默认 72）。

真机背景：旧的 `_is_active_session` 只问「state.sessions 里有没有这条记录」，而那个
字典的 14 天清理有早退（``len(sessions) <= 500`` 直接 return），真机只有十几个会话
**永远触发不到** ⇒ 「历史上收过一条消息的会话」永久算活跃。实测一轮 18 个会话，
其中多数早就没人说话，连宿主名单外的僵尸会话（`HeartflowPolicyDeniedError` 那个）
也混在里面每轮撞一次策略。

改成看 `last_user_message_at` / `last_proactive_at` 距现在多久。三条不能破的底线：

1. ``applied`` / ``unbacked`` 里的会话**不看时间**——那两张表是「宿主上那一层是我们
   写的」的记忆载体，按时间踢出去会让会话回到范围时把自己的写入误判成外部基数，
   **生活倍率乘两次**（`_apply_sweep` 那段注释专门警告过）。
2. 缺时间戳的老记录**按活跃处理**——一次升级把所有会话突然踢掉，比多干预几天危险。
3. ``active_window_hours=0`` 必须精确退回旧行为（只要见过就算活跃）。
"""

import asyncio
import pathlib
import sys
import time

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakePaths, bind_context, build_context, get_default_config  # noqa: E402
from test_interop import ScaleHost, _load  # noqa: E402

HOUR = 3600.0


def _make_plugin(**config_overrides):
    """与 ``test_interop._make_plugin`` 同口径。"""

    module = _load()
    plugin = module.create_plugin()
    host = ScaleHost(module.__plugin_id__, FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["frequency"]["quiet_hours"] = []
    config["events"]["fire_probability"] = 0.0
    config["proactive"]["enabled"] = False
    config["simulation"]["dry_run"] = False
    config["apply"]["only_active_sessions"] = False
    for section, values in config_overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin._state.last_tick_at = time.time()
    return module, plugin, host


def _record(plugin, session_id: str, hours_ago: float, *, now: float) -> None:
    """给会话写一条「最后一次活动在 hours_ago 小时前」的记录。"""

    plugin._state.sessions[session_id] = {
        "stream_id": session_id,
        "last_user_message_at": now - hours_ago * HOUR,
        "last_proactive_at": 0.0,
        "day_key": "2026-10-24",
        "count": 0,
    }


def _is_active(plugin, session_id: str, now: float) -> bool:
    return plugin._is_active_session(session_id, now)


def test_window_keeps_recent_and_drops_stale():
    """72 小时是分界：窗口内算活跃，窗口外不算。"""

    async def run():
        _, plugin, host = _make_plugin()
        now = time.time()
        _record(plugin, "fresh", hours_ago=10.0, now=now)
        _record(plugin, "stale", hours_ago=100.0, now=now)
        assert _is_active(plugin, "fresh", now)
        assert not _is_active(plugin, "stale", now)

    asyncio.run(run())


def test_window_boundary_is_inclusive_within_tolerance():
    """边界两侧都要有明确答案（71h59m 活跃 / 72h01m 不活跃）。"""

    async def run():
        _, plugin, host = _make_plugin()
        now = time.time()
        _record(plugin, "inside", hours_ago=72.0 - 1.0 / 60.0, now=now)
        _record(plugin, "outside", hours_ago=72.0 + 1.0 / 60.0, now=now)
        assert _is_active(plugin, "inside", now)
        assert not _is_active(plugin, "outside", now)

    asyncio.run(run())


def test_applied_and_unbacked_ignore_the_window():
    """在管的会话不看时间——踢掉它们会让倍率被乘两次。"""

    async def run():
        _, plugin, host = _make_plugin()
        now = time.time()
        _record(plugin, "managed", hours_ago=500.0, now=now)
        assert not _is_active(plugin, "managed", now), "光有记录且过期 ⇒ 不活跃"

        plugin._state.applied["managed"] = 0.8
        assert _is_active(plugin, "managed", now), "在 applied 里必须无视时间"

        del plugin._state.applied["managed"]
        plugin._state.unbacked["managed"] = now + 60.0
        assert _is_active(plugin, "managed", now), "在 unbacked 里必须无视时间"

    asyncio.run(run())


def test_record_without_timestamp_is_treated_as_active():
    """老状态文件缺时间戳 ⇒ 保守算活跃（一次升级别把会话全踢掉）。"""

    async def run():
        _, plugin, host = _make_plugin()
        now = time.time()
        plugin._state.sessions["legacy"] = {"stream_id": "legacy", "day_key": "2026-10-24"}
        assert _is_active(plugin, "legacy", now)
        plugin._state.sessions["broken"] = {"stream_id": "broken", "last_user_message_at": None}
        assert _is_active(plugin, "broken", now)

    asyncio.run(run())


def test_zero_window_restores_the_old_behaviour():
    """``active_window_hours=0`` 必须精确退回「见过就算活跃」。"""

    async def run():
        _, plugin, host = _make_plugin(apply={"active_window_hours": 0})
        now = time.time()
        _record(plugin, "ancient", hours_ago=5000.0, now=now)
        assert _is_active(plugin, "ancient", now)

    asyncio.run(run())


def test_sweep_skips_stale_sessions_and_counts_them():
    """端到端：过期会话不进 targets，且跳过数要能在状态卡上答出来。"""

    async def run():
        _, plugin, host = _make_plugin(apply={"only_active_sessions": True})
        now = time.time()
        host.sessions = [
            {"session_id": name, "stream_id": name, "platform": "qq",
             "group_id": str(i), "user_id": "10001", "is_group_session": True,
             "chat_type": "group"}
            for i, name in enumerate(("fresh", "stale", "ancient"))
        ]
        host.live = set()
        _record(plugin, "fresh", hours_ago=1.0, now=now)
        _record(plugin, "stale", hours_ago=100.0, now=now)
        _record(plugin, "ancient", hours_ago=5000.0, now=now)

        await plugin._apply_sweep(now)
        assert plugin._last_target_count == 1, "只剩 1 个在窗口内的会话"
        assert plugin._last_skipped_idle == 2, "跳过的两个要记账，否则卡上答不出来"

    asyncio.run(run())


def test_proactive_at_also_counts_as_activity():
    """她主动开口过也算活动（只按对方发言判会漏掉「只有她在说」的会话）。"""

    async def run():
        _, plugin, host = _make_plugin()
        now = time.time()
        plugin._state.sessions["she-spoke"] = {
            "stream_id": "she-spoke",
            "last_user_message_at": 0.0,
            "last_proactive_at": now - 5.0 * HOUR,
            "day_key": "2026-10-24",
        }
        assert _is_active(plugin, "she-spoke", now)

    asyncio.run(run())
