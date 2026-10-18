# -*- coding: utf-8 -*-
"""L3：主动开口的触发前复检与失败冷却（v1.18.0，借 mohobot ``proactive_chat``）。

两个真机问题：

1. **触发前有 await**：``decide`` 到 ``maisaka.proactive.trigger`` 之间隔着
   ``_append_proactive_fact`` 的一次 RPC。对方的入站钩子完全可能在这期间把
   ``last_user_message_at`` 刷新——她于是开口打断一场正在进行的对话。
   参考做法：mohobot 在生成窗口内发现用户来了新消息就**丢弃**本次结果。
2. **失败不该热重试**：``_trigger_proactive`` 返回 False 时什么都不记，
   下个推进周期（≥60 秒）就全量重评一次并再刷一条告警。
   参考做法：mohobot 给失败排一个新的随机窗口、离线顺延 300 秒。

本文件钉住：三态返回值、触发前复检真的拦得住、失败真的进冷却、以及
「受理即挂待核实」（受理≠发送，见 ``test_proactive_confirm.py``）。
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
TRIGGER = "maisaka.proactive.trigger"
SESSION = "group-1"


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_proactive_race")


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


def _patch_sessions(plugin, *session_ids):
    async def fake_list():
        return [(sid, {"session_id": sid, "group_id": "123456"}) for sid in session_ids]

    plugin._list_sessions = fake_list


def _material(now: float) -> dict:
    return {"label": "小事", "text": "刚泡了杯茶", "weight": 0.9, "created_at": now,
            "expires_at": now + 3600.0}


def _session_record(now: float) -> dict:
    """一个「对方十小时前说过话、她还没主动过」的干净会话记录。"""

    return {
        "stream_id": SESSION,
        "last_user_message_at": now - 10 * 3600.0,
        "last_proactive_at": 0.0,
        "day_key": "2026-10-11",
        "count": 0,
        "unanswered_streak": 0,
    }


def _prepare(plugin, host, now: float) -> None:
    plugin._state.materials = [_material(now)]
    plugin._state.sessions[SESSION] = _session_record(now)
    _patch_sessions(plugin, SESSION)


# ------------------------------------------------- 1. 触发前三态返回值


def test_trigger_returns_sent_aborted_failed():
    """三态字符串是契约：三种善后完全不同，不能再挤进一个 bool。"""

    _module, plugin, host = _make_plugin()
    now = time.time()
    decision = _decision(now)

    assert asyncio.run(
        plugin._trigger_proactive(SESSION, decision, "2026-10-11", now, expected_last_user=0.0)
    ) == "sent"

    # 触发前对方说话了 ⇒ aborted（不是失败）
    plugin._state.sessions[SESSION] = dict(_session_record(now), last_user_message_at=now)
    assert asyncio.run(
        plugin._trigger_proactive(SESSION, decision, "2026-10-11", now, expected_last_user=0.0)
    ) == "aborted"

    # 宿主拒绝 ⇒ failed（快照与记录一致，复检放行，失败来自宿主）
    host.returns[TRIGGER] = {"success": False, "error": "宿主故障"}
    baseline = now - 10 * 3600.0
    plugin._state.sessions[SESSION] = _session_record(now)
    assert asyncio.run(
        plugin._trigger_proactive(SESSION, decision, "2026-10-11", now,
                                  expected_last_user=baseline)
    ) == "failed"


def _decision(now: float):
    import life_proactive as P

    material = _material(now)
    return P.ProactiveDecision(
        True, "ok", score=0.8, material=material, intent=P.build_intent(material), detail="测试"
    )


# ------------------------------------------------- 2. 触发前复检（接线）


def test_aborts_when_the_user_speaks_during_the_fact_append():
    """写「开口来由」是一次 RPC，对方可能就在这期间说话 ⇒ 放弃这次开口。"""

    _module, plugin, host = _make_plugin()
    now = time.time()
    _prepare(plugin, host, now)

    async def fact_then_user_speaks(session_id, label, motive):
        # 模拟「就在这个 await 里，对方发了一条消息」：入站钩子改的就是这个字段
        plugin._state.sessions[session_id]["last_user_message_at"] = now + 1.0

    plugin._append_proactive_fact = fact_then_user_speaks

    asyncio.run(plugin._maybe_proactive(now))

    assert not host.calls_of(TRIGGER), "触发前发现对方说话就不该再触发"
    assert plugin._state.skip_ledger.get("silence", 0) >= 1, "应当记一次「对方刚说过话」"
    assert "pending_verify" not in plugin._state.sessions[SESSION]
    assert plugin._state.sessions[SESSION]["count"] == 0


def test_aborted_opening_does_not_start_a_cooldown():
    """放弃不是失败：它不该让下一次开口被冷却闸挡住。"""

    _module, plugin, host = _make_plugin()
    now = time.time()
    _prepare(plugin, host, now)

    async def fact_then_user_speaks(session_id, label, motive):
        plugin._state.sessions[session_id]["last_user_message_at"] = now + 1.0

    plugin._append_proactive_fact = fact_then_user_speaks
    asyncio.run(plugin._maybe_proactive(now))

    assert plugin._state.sessions[SESSION].get("last_attempt_at") is None


def test_still_triggers_and_parks_a_pending_verification_when_nobody_spoke():
    _module, plugin, host = _make_plugin()
    now = time.time()
    _prepare(plugin, host, now)

    asyncio.run(plugin._maybe_proactive(now))

    assert host.calls_of(TRIGGER), "没人说话就该照常开口"
    record = plugin._state.sessions[SESSION]
    pending = record.get("pending_verify")
    assert pending, "受理之后必须挂待核实（受理 ≠ 发送）"
    assert pending["trigger_at"] == pytest.approx(now)
    assert pending["verify_after"] > now
    assert record["count"] == 0, "还没核实就不该消耗当天配额"
    assert record["last_proactive_at"] == 0.0, "还没核实就不该重开间隔闸"


def test_pending_verification_is_skipped_when_confirmation_is_off():
    _module, plugin, host = _make_plugin(proactive={"confirm_send": False})
    now = time.time()
    _prepare(plugin, host, now)

    asyncio.run(plugin._maybe_proactive(now))

    record = plugin._state.sessions[SESSION]
    assert "pending_verify" not in record, "关掉核实就退回旧的受理即计语义"
    assert record["count"] == 1
    assert plugin._state.skip_ledger.get("sent", 0) >= 1


# ------------------------------------------------- 3. 失败冷却（接线）


def test_trigger_failure_starts_the_cooldown_instead_of_hot_retrying():
    _module, plugin, host = _make_plugin()
    now = time.time()
    host.returns[TRIGGER] = {"success": False, "error": "宿主故障"}
    _prepare(plugin, host, now)

    asyncio.run(plugin._maybe_proactive(now))

    record = plugin._state.sessions[SESSION]
    assert record["last_attempt_at"] == pytest.approx(now)
    assert record["count"] == 0, "没发出去的尝试不该消耗配额"
    assert "pending_verify" not in record

    # 下一个周期（一秒后）：冷却闸拦住，不再打 RPC、不再刷告警
    calls_before = len(host.calls_of(TRIGGER))
    asyncio.run(plugin._maybe_proactive(now + 1.0))
    assert len(host.calls_of(TRIGGER)) == calls_before, "失败后不该每个周期都重试"
    assert plugin._state.skip_ledger.get("attempt_cooldown", 0) >= 1


def test_cooldown_expires_and_the_next_opening_is_allowed():
    _module, plugin, host = _make_plugin()
    now = time.time()
    host.returns[TRIGGER] = {"success": False, "error": "宿主故障"}
    _prepare(plugin, host, now)
    asyncio.run(plugin._maybe_proactive(now))

    # 冷却（默认 15 分钟）过后，素材仍在保鲜期内 ⇒ 重新开口
    host.returns[TRIGGER] = {"success": True}
    later = now + 16 * 60.0
    plugin._state.materials = [_material(later)]
    asyncio.run(plugin._maybe_proactive(later))
    assert host.calls_of(TRIGGER), "冷却过后应当允许再试"
    assert plugin._state.sessions[SESSION]["last_proactive_at"] == 0.0


def test_cooldown_can_be_disabled_by_config():
    _module, plugin, host = _make_plugin(proactive={"failure_cooldown_minutes": 0})
    now = time.time()
    host.returns[TRIGGER] = {"success": False, "error": "宿主故障"}
    _prepare(plugin, host, now)
    asyncio.run(plugin._maybe_proactive(now))

    calls_before = len(host.calls_of(TRIGGER))
    asyncio.run(plugin._maybe_proactive(now + 1.0))
    assert len(host.calls_of(TRIGGER)) == calls_before + 1, "关掉冷却就该照旧每个周期重试"
