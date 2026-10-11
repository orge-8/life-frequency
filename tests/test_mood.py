# -*- coding: utf-8 -*-
"""L3：内心维度（mood：stress / loneliness / social battery）的演化与消费。

钉住六件事：

1. **不进倍率**——mood 演化前后 ``compute_adjust`` 的输入字段（emotion/energy/
   activity）一个都不变（方案总纪律）；
2. 工作类活动加压、睡眠解压、独处回电、被 @ 耗电；
3. 注入文本只在过载（≥7 / 电量 <3）时出现，回落不写「恢复正常」；
4. 电量闸：<2 直接不开口，2–4 阈值上浮；
5. 注入去重：同一会话同一天只有一条；
6. 注入失败三次停手（宿主可能没这个能力）。
"""

import asyncio
import pathlib
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_mood as M  # noqa: E402
from fakehost import (  # noqa: E402
    FakeHost,
    FakePaths,
    bind_context,
    build_context,
    get_default_config,
    load_plugin_module,
)

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
NOW = 1_700_000_000.0
POLICY = M.MoodPolicy()


def _make_plugin(**overrides):
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_mood")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = 0
    config["physio"]["meals"] = []
    config["mood"]["enabled"] = True
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    plugin._rebuild_from_config()
    return module, plugin, host


# ---------------------------------------------------------------- 纯模块：演化


def test_draining_activity_raises_stress_and_sleep_relieves():
    state = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 7.0})()
    M.evolve(state, activity="overtime", minutes=60, had_contact=False,
             had_mention=False, policy=POLICY, last_contact_at=0.0, now=NOW)
    assert state.stress > 3.0, "加班一小时必须更累"

    before = state.stress
    M.evolve(state, activity="sleep", minutes=60, had_contact=False,
             had_mention=False, policy=POLICY, last_contact_at=0.0, now=NOW)
    assert state.stress < before, "睡觉解压"
    assert state.social_battery > 7.0, "睡眠回电"


def test_solo_activities_recover_battery():
    state = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 5.0})()
    M.evolve(state, activity="daze", minutes=60, had_contact=False,
             had_mention=False, policy=POLICY, last_contact_at=0.0, now=NOW)
    assert state.social_battery == pytest.approx(5.4)


def test_mention_costs_battery_and_relieves_loneliness():
    state = type("S", (), {"stress": 3.0, "loneliness": 5.0, "social_battery": 7.0})()
    M.evolve(state, activity="daily", minutes=10, had_contact=True,
             had_mention=True, policy=POLICY, last_contact_at=NOW - 60, now=NOW)
    assert state.social_battery == pytest.approx(6.85)
    assert state.loneliness == pytest.approx(4.7)


def test_loneliness_grows_after_six_quiet_hours_only():
    # 刚互动过：不涨
    state = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 7.0})()
    M.evolve(state, activity="daze", minutes=10, had_contact=False,
             had_mention=False, policy=POLICY, last_contact_at=NOW - 60, now=NOW)
    assert state.loneliness == 4.0
    # 六小时没人理：涨
    state2 = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 7.0})()
    M.evolve(state2, activity="daze", minutes=10, had_contact=False,
             had_mention=False, policy=POLICY, last_contact_at=NOW - 7 * 3600, now=NOW)
    assert state2.loneliness > 4.0


def test_mood_never_touches_the_multiplier_inputs():
    """总纪律：mood 演化**不碰** emotion / energy / activity。"""

    class S:
        stress = 3.0
        loneliness = 4.0
        social_battery = 7.0
        emotion = 6.0
        energy = 5.0

    state = S()
    M.evolve(state, activity="overtime", minutes=60, had_contact=True,
             had_mention=True, policy=POLICY, last_contact_at=NOW, now=NOW)
    assert state.emotion == 6.0 and state.energy == 5.0


# ---------------------------------------------------------------- 纯模块：消费


def test_injection_lines_gate():
    overload = type("S", (), {"stress": 7.5, "loneliness": 4.0, "social_battery": 7.0})()
    lines = M.injection_lines(overload)
    assert len(lines) == 1 and "压力" in lines[0]

    both = type("S", (), {"stress": 8.0, "loneliness": 8.0, "social_battery": 2.5})()
    assert len(M.injection_lines(both)) == 3

    calm = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 7.0})()
    assert M.injection_lines(calm) == (), "平静时闭嘴"

    recovering = type("S", (), {"stress": 4.0, "loneliness": 4.0, "social_battery": 7.0})()
    assert M.injection_lines(recovering) == (), "回落不写「恢复正常」"


def test_battery_gate():
    empty = type("S", (), {"social_battery": 1.5})()
    assert M.battery_gate(empty) == "low_battery"
    low = type("S", (), {"social_battery": 3.0})()
    assert M.battery_gate(low) == "battery_threshold"
    fine = type("S", (), {"social_battery": 6.0})()
    assert M.battery_gate(fine) == ""
    nan = type("S", (), {"social_battery": float("nan")})()
    assert M.battery_gate(nan) == "", "坏值宁可多说不可永远沉默"


def test_proactive_cost():
    state = type("S", (), {"social_battery": 5.0})()
    M.note_proactive_cost(state, policy=POLICY)
    assert state.social_battery == pytest.approx(4.7)


# ---------------------------------------------------------------- 接线


def test_hook_injects_once_per_session_per_day():
    async def run():
        module, plugin, host = _make_plugin()
        now = plugin._state.last_tick_at or 1_700_000_000.0
        plugin._state.stress = 8.0
        session_id = "fake-stream"
        # FakeHost 对 maisaka.context.append 会落穿到默认 {"success": True, "result": None}
        await plugin._maybe_inject_mood(session_id, now)
        first = [kw for kw in host.calls_of("maisaka.context.append")]
        assert first, "过载时应注入一条"
        await plugin._maybe_inject_mood(session_id, now + 60)
        second = [kw for kw in host.calls_of("maisaka.context.append")]
        assert len(second) == len(first), "同一天同一会话只注入一次"

    asyncio.run(run())


def test_hook_injection_stops_after_three_failures():
    async def run():
        module, plugin, host = _make_plugin()
        now = 1_700_000_000.0
        plugin._state.stress = 8.0

        async def broken(*args, **kwargs):
            raise RuntimeError("宿主没这能力")

        plugin.ctx.maisaka.context.append = broken
        for _ in range(3):
            await plugin._maybe_inject_mood("s1", now)
        assert plugin._mood_inject_failures == 3
        calls_before = plugin._mood_inject_failures
        await plugin._maybe_inject_mood("s2", now)  # 新会话也不该再试
        assert plugin._mood_inject_failures == calls_before

    asyncio.run(run())


def test_low_battery_blocks_proactive():
    async def run():
        module, plugin, host = _make_plugin(
            proactive={"enabled": True, "quiet_hours": [], "score_threshold": 0.0,
                       "min_interval_minutes": 0, "recent_user_silence_minutes": 0,
                       "daily_max": 5},
        )
        plugin._state.social_battery = 1.0
        plugin._state.materials.append(
            {"label": "test", "text": "今天好累", "weight": 2.0,
             "created_at": 0.0, "expires_at": 9_999_999_999.0}
        )
        await plugin._maybe_proactive(1_700_000_000.0)
        # 没有任何触发；台账里记 low_battery
        assert plugin._state.skip_ledger.get("low_battery"), plugin._state.skip_ledger

    asyncio.run(run())
