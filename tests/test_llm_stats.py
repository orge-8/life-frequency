# -*- coding: utf-8 -*-
"""L3：模型调用分解统计（v1.17.0，PR-OBS-1）。

改进方案 §2 P2-5：``_skipped_llm_calls`` 只有一个总数 ＋ 每小时一条汇总日志，
用户分不清「习惯窗口省下的」「生理窗口省下的」与「注定白问省下的」——也就无法
判断模型是不是被习惯表饿死了。

现在按**生活日 + 桶**记在 ``LifeState.llm_ask_stats``（键 ``"生活日|桶"``，
与 ``care_today`` 同一套按生活日惰性清理的模式），``/生活 活动`` 印一行分解，
并且硬闸纪律照旧：**只记账，不碰决策**（统计是观测，坏了也不许影响裁定）。
"""

import asyncio
import pathlib
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import (  # noqa: E402
    FakeHost,
    FakePaths,
    bind_context,
    build_context,
    get_default_config,
    load_plugin_module,
)
from life_sim import LifeState, day_key_of, local_datetime  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
THU = 8


def _at(hour: int, minute: int = 0, day: int = THU) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).timestamp()


def _make_plugin(lines=None, **overrides):
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_llm_stats")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = 0
    config["routines"]["enabled"] = True
    config["routines"]["lines"] = list(lines or [])
    config["physio"]["meals"] = []
    config["schedule"]["enabled"] = False
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    plugin._rebuild_from_config()
    plugin._open_store()
    return module, plugin, host


# ================================================================ 计数与清理


def test_buckets_are_counted_and_aggregated_for_today():
    _m, plugin, _host = _make_plugin()
    now = _at(12)

    for _ in range(3):
        plugin._note_llm_stat("asked", now)
    plugin._note_llm_stat("failed", now)
    plugin._note_llm_stat("skip_routine", now)
    plugin._note_llm_stat("skip_pointless", now)
    plugin._note_llm_stat("skip_pointless", now)

    stats = plugin._llm_stats_today(now)
    assert stats["asked"] == 3
    assert stats["failed"] == 1
    assert stats["skip_routine"] == 1
    assert stats["skip_pointless"] == 2

    line = plugin._llm_stats_line(now)
    assert "问 3 次" in line and "失败 1" in line and "跳过 3 次" in line
    assert "习惯窗口 1" in line and "注定白问 2" in line


def test_stats_are_scoped_to_the_life_day():
    _m, plugin, _host = _make_plugin()
    today = _at(12, 0, THU)
    yesterday = _at(12, 0, THU - 1)

    plugin._note_llm_stat("asked", yesterday)
    plugin._note_llm_stat("asked", today)

    assert plugin._llm_stats_today(today)["asked"] == 1
    assert plugin._llm_stats_today(yesterday)["asked"] == 1
    # 生活日边界在 12:00：11:00 与 13:00 分属两个生活日
    assert plugin._llm_stats_today(_at(11))["asked"] == 1
    assert plugin._llm_stats_today(_at(13))["asked"] == 1


def test_empty_and_bad_values_are_harmless():
    _m, plugin, _host = _make_plugin()
    now = _at(12)
    assert plugin._llm_stats_today(now) == {}
    assert plugin._llm_stats_line(now) == ""

    plugin._note_llm_stat("", now)
    assert plugin._llm_stats_today(now) == {}, "空桶名不该建键"

    plugin._state.llm_ask_stats = {"2026-10-08|asked": "坏值"}
    assert plugin._llm_stats_today(now) == {}, "坏值不炸统计"


def test_state_sanitization_and_daily_prune_keep_the_table_small():
    state = LifeState.from_dict(
        {"llm_ask_stats": {"2026-10-08|asked": 2, "bad": None, "2026-10-08|failed": -1}}
    )
    assert state.llm_ask_stats.get("2026-10-08|asked") == 2.0
    assert float(state.llm_ask_stats.get("bad", 0.0) or 0.0) == 0.0


# ================================================================ 整轮 tick


class _FrozenTime:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def time(self) -> float:
        return self.value


def test_tick_records_a_routine_skip_and_never_asks():
    """习惯命中那一轮：记 ``skip_routine``，且真的没问模型。"""

    async def run():
        module, plugin, _host = _make_plugin(["07:00-08:00|起床洗漱|daily|jitter=0"])
        now = _at(7, 30)
        real_time = module.time
        module.time = _FrozenTime(now)
        try:
            plugin._state.activity = "daze"
            plugin._state.activity_since = now - 3600
            plugin._state.last_tick_at = now - 600
            plugin._llm_ready = lambda _now: True
            plugin._fetch_identity = lambda: asyncio.sleep(0)

            async def spy(_now):
                raise AssertionError("习惯命中时不该问模型")

            plugin._ask_activity = spy
            await plugin._sim_tick()
        finally:
            module.time = real_time

        stats = plugin._llm_stats_today(now)
        assert stats.get("skip_routine") == 1, stats
        assert not stats.get("asked")

    asyncio.run(run())


def test_tick_records_an_ask_when_the_coast_is_clear():
    """没有习惯/生理窗命中时：记 ``asked``（模型真的被问了）。"""

    async def run():
        module, plugin, _host = _make_plugin()
        now = _at(15)
        real_time = module.time
        module.time = _FrozenTime(now)
        try:
            plugin._state.activity = "daze"
            plugin._state.activity_since = now - 7200
            plugin._state.last_tick_at = now - 600
            plugin._llm_ready = lambda _now: True
            plugin._fetch_identity = lambda: asyncio.sleep(0)
            plugin.config.activity.llm.skip_when_forced = False

            async def fake_generate(**_kwargs):
                return {
                    "success": True,
                    "response": '{"activity": "music", "scene": "戴着耳机"}',
                }

            plugin.ctx.llm.generate = fake_generate
            await plugin._sim_tick()
        finally:
            module.time = real_time

        stats = plugin._llm_stats_today(now)
        assert stats.get("asked") == 1, stats
        assert plugin._state.activity == "music"

    asyncio.run(run())


def test_tick_records_a_failure_when_the_model_returns_junk():
    """垃圾输出记 ``failed``（与 ``asked`` 分开：状态卡才能区分「没问」与「问了挂了」）。"""

    async def run():
        module, plugin, _host = _make_plugin()
        now = _at(15)
        real_time = module.time
        module.time = _FrozenTime(now)
        try:
            plugin._state.activity = "daze"
            plugin._state.activity_since = now - 7200
            plugin._state.last_tick_at = now - 600
            plugin._llm_ready = lambda _now: True
            plugin._fetch_identity = lambda: asyncio.sleep(0)
            plugin.config.activity.llm.skip_when_forced = False

            async def fake_generate(**_kwargs):
                return {"success": True, "response": "我今天不太想说话"}

            plugin.ctx.llm.generate = fake_generate
            await plugin._sim_tick()
        finally:
            module.time = real_time

        stats = plugin._llm_stats_today(now)
        assert stats.get("asked") == 1, stats
        assert stats.get("failed") == 1, stats
        assert plugin._state.activity == "daze", "解析失败就保持当前活动"

    asyncio.run(run())


def test_stats_do_not_change_the_decision():
    """统计是观测：开了统计之后裁定与来源都不变（回归纪律）。"""

    async def run():
        module, plugin, _host = _make_plugin()
        now = _at(15)
        real_time = module.time
        module.time = _FrozenTime(now)
        try:
            plugin._state.activity = "daze"
            plugin._state.activity_since = now - 7200
            plugin._state.last_tick_at = now - 600
            plugin._llm_ready = lambda _now: True
            plugin._fetch_identity = lambda: asyncio.sleep(0)
            plugin.config.activity.llm.skip_when_forced = False

            async def fake_generate(**_kwargs):
                return {
                    "success": True,
                    "response": '{"activity": "music", "scene": "戴着耳机"}',
                }

            plugin.ctx.llm.generate = fake_generate
            await plugin._sim_tick()
        finally:
            module.time = real_time

        assert plugin._state.activity == "music"
        assert plugin._state.scene == "戴着耳机"
        assert plugin._state.activity_source == "llm"

    asyncio.run(run())
