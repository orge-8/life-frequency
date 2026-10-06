# -*- coding: utf-8 -*-
"""L3：主动开口的「对方上次说话」基线补全（借鉴 idle_proactive_chat）。

``last_user_message_at`` 只由入站钩子写。**首装当天 / ``/生活 重置`` 之后**所有会话
都是 0 ⇒ ``decide`` 里「对方刚说过话」这道闸形同不存在，她可能在一个刚刚还在热聊的
会话里突然开口。参考 ``XXXxx7258/idle_proactive_chat``（MIT，1.0.5）的做法：
用 ``message.get_by_time_in_chat(filter_mai=True, filter_command=True)``
把最后一条**人类**消息的时间捞回来当基线。

本文件钉住：解析形态、只补一次、每轮有配额、连续失败就停手、以及**接线真的生效**
（近期消息 → 不开口；很久以前的消息 → 照常开口）。
"""

import asyncio
import pathlib
import sys
import time

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
CAPABILITY = "message.get_by_time_in_chat"


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_baseline")


def _make_plugin(**config_overrides):
    module = _load()
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["proactive"]["enabled"] = True
    config["proactive"]["quiet_hours"] = []
    for section, values in config_overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    return module, plugin, host


def _calls(host):
    """去重后（FakeHost 对落穿能力会双记）。"""

    unique = {}
    for kwargs in host.calls_of(CAPABILITY):
        unique[(kwargs.get("chat_id") or kwargs.get("stream_id"), kwargs.get("limit"))] = kwargs
    return list(unique.values())


def _material(now: float) -> dict:
    return {"label": "小事", "text": "刚泡了杯茶", "weight": 0.9, "created_at": now,
            "expires_at": now + 3600.0}


# ------------------------------------------------- 1. 返回形态解析


def test_history_messages_accepts_all_known_shapes():
    module, _plugin, _host = _make_plugin()
    assert module._history_messages([{"timestamp": 1}]) == [{"timestamp": 1}]
    assert module._history_messages({"messages": [{"timestamp": 2}]}) == [{"timestamp": 2}]
    assert module._history_messages({"result": [{"timestamp": 3}]}) == [{"timestamp": 3}]
    assert module._history_messages({"data": [{"timestamp": 4}]}) == [{"timestamp": 4}]
    # 失败包装 / 垃圾 / 空
    assert module._history_messages({"success": False, "error": "x"}) == []
    assert module._history_messages("字符串") == []
    assert module._history_messages(None) == []
    assert module._history_messages([1, "a", {"timestamp": 5}]) == [{"timestamp": 5}]


def test_restore_reads_latest_timestamp_from_either_key():
    module, plugin, host = _make_plugin()
    now = time.time()
    host.returns[CAPABILITY] = [
        {"timestamp": now - 600},
        {"time": now - 120},
    ]
    restored = asyncio.run(plugin._restore_last_user_from_history("group-1", now))
    assert restored == pytest.approx(now - 120)


def test_restore_returns_zero_when_nothing_found():
    module, plugin, host = _make_plugin()
    host.returns[CAPABILITY] = []
    assert asyncio.run(plugin._restore_last_user_from_history("group-1", time.time())) == 0.0


def test_restore_returns_zero_when_host_reports_failure():
    module, plugin, host = _make_plugin()
    host.returns[CAPABILITY] = {"success": False, "error": "宿主没有这个能力"}
    assert asyncio.run(plugin._restore_last_user_from_history("group-1", time.time())) == 0.0
    assert plugin._history_probe_failures == 1


def test_restore_swallows_exceptions():
    module, plugin, host = _make_plugin()

    async def boom(*_args, **_kwargs):
        raise RuntimeError("RPC 炸了")

    plugin.ctx.message.get_by_time_in_chat = boom
    assert asyncio.run(plugin._restore_last_user_from_history("group-1", time.time())) == 0.0
    assert plugin._history_probe_failures == 1


# ------------------------------------------------- 2. 只补一次、有配额、失败停手


def _ensure(plugin, session_id, record, *, now=None, probes_left=3):
    return asyncio.run(
        plugin._ensure_last_user_baseline(
            session_id, record, day_key="2026-10-06", now=now or time.time(), probes_left=probes_left
        )
    )


def test_existing_baseline_is_left_alone():
    module, plugin, host = _make_plugin()
    now = time.time()
    record = {"last_user_message_at": now - 10, "day_key": "2026-10-06", "count": 0}
    result, probed = _ensure(plugin, "group-1", record, now=now)
    assert result is record and probed is False
    assert not _calls(host), "已经有基线就不该打 RPC"


def test_missing_baseline_is_restored_and_persisted():
    module, plugin, host = _make_plugin()
    now = time.time()
    host.returns[CAPABILITY] = [{"timestamp": now - 300}]

    result, probed = _ensure(plugin, "group-1", None, now=now)
    assert probed is True
    assert result is not None and result["last_user_message_at"] == pytest.approx(now - 300)
    assert plugin._state.sessions["group-1"]["last_user_message_at"] == pytest.approx(now - 300)
    assert plugin._state_dirty is True
    assert _calls(host)[0].get("limit") == 1
    assert _calls(host)[0].get("filter_mai") is True
    assert _calls(host)[0].get("filter_command") is True


def test_missing_baseline_with_empty_history_is_not_retried():
    module, plugin, host = _make_plugin()
    now = time.time()
    host.returns[CAPABILITY] = []

    result, probed = _ensure(plugin, "group-1", None, now=now)
    assert result is None and probed is True
    assert "group-1" in plugin._history_probed
    assert "group-1" not in plugin._state.sessions, "查不到就不该凭空建记录"

    again, probed_again = _ensure(plugin, "group-1", None, now=now)
    assert again is None and probed_again is False
    assert len(_calls(host)) == 1, "同一个会话只补一次，不该每轮重试"


def test_probe_respects_per_tick_budget():
    module, plugin, host = _make_plugin()
    now = time.time()
    host.returns[CAPABILITY] = [{"timestamp": now - 300}]
    _result, probed = _ensure(plugin, "group-1", None, now=now, probes_left=0)
    assert probed is False
    assert not _calls(host), "配额用完就不该打 RPC"


def test_probe_stops_after_repeated_failures():
    module, plugin, host = _make_plugin()
    now = time.time()
    host.returns[CAPABILITY] = {"success": False, "error": "nope"}
    for index in range(module._HISTORY_PROBE_MAX_FAILURES):
        _ensure(plugin, f"group-{index}", None, now=now)
        assert plugin._history_probe_failures == index + 1
    calls_before = len(_calls(host))
    _result, probed = _ensure(plugin, "group-last", None, now=now)
    assert probed is False
    assert len(_calls(host)) == calls_before, "连续失败到上限后必须停手"


# ------------------------------------------------- 3. 接线：真的影响判定


def _patch_sessions(plugin, *session_ids):
    async def fake_list():
        return [(sid, {"session_id": sid, "group_id": "123456"}) for sid in session_ids]

    plugin._list_sessions = fake_list


def test_recent_human_message_blocks_opening_on_a_fresh_install():
    """首装当天最危险的情形：会话刚刚还在聊，插件却以为「没人说过话」。"""

    module, plugin, host = _make_plugin()
    now = time.time()
    plugin._state.materials = [_material(now)]
    host.returns[CAPABILITY] = [{"timestamp": now - 60}]
    _patch_sessions(plugin, "group-1")

    asyncio.run(plugin._maybe_proactive(now))

    assert plugin._state.sessions["group-1"]["last_user_message_at"] == pytest.approx(now - 60)
    assert not host.calls_of("maisaka.proactive.trigger"), "对方刚说过话就不该开口"


def test_stale_human_message_still_allows_opening():
    module, plugin, host = _make_plugin()
    now = time.time()
    plugin._state.materials = [_material(now)]
    host.returns[CAPABILITY] = [{"timestamp": now - 10 * 3600}]
    _patch_sessions(plugin, "group-1")

    asyncio.run(plugin._maybe_proactive(now))

    assert plugin._state.sessions["group-1"]["last_user_message_at"] == pytest.approx(now - 10 * 3600)
    assert host.calls_of("maisaka.proactive.trigger"), "基线很旧就不该拦住开口"


def test_live_hook_still_wins_over_restored_baseline():
    """实时钩子写的值永远比历史基线新（基线只在缺失时补一次）。"""

    module, plugin, host = _make_plugin()
    now = time.time()
    plugin._state.sessions["group-1"] = {"last_user_message_at": now - 5, "day_key": "2026-10-06", "count": 0}
    host.returns[CAPABILITY] = [{"timestamp": now - 99999}]
    _patch_sessions(plugin, "group-1")

    asyncio.run(plugin._maybe_proactive(now))

    assert plugin._state.sessions["group-1"]["last_user_message_at"] == pytest.approx(now - 5)
    assert not _calls(host), "已有实时值就不该再查历史"
