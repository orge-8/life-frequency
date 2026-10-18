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


def test_hanging_model_call_is_cut_by_the_outer_hard_fuse():
    """v1.20.0（G2）：宿主侧超时不生效（调用永不返回）时，外层硬熔断必须掐断这一轮。

    没有它的话 ``_sim_loop`` 会**永久挂起**——整个生活模型停摆（不推进 tick、
    不下发倍率、不核销 pending_verify）。这类故障诊断查得出来，但插件无法自愈。

    内层预算压到 1 秒、余量收紧到 0.2 秒 ⇒ 外层总闸约 1.2 秒，测试跑得动。
    """

    async def run():
        module, plugin, host = _make_plugin()
        # ⚠ 这一处打桩打的是**标准库 time 模块本身**（``module.time`` 就是 ``time``），
        # 不是插件模块的属性。所以还原也必须还原 ``time.time``——写成
        # ``module.time = real_time`` 只是把插件模块的 ``time`` 属性换成一个函数，
        # 全局时钟**仍然冻结**：此后所有依赖真实时钟的用例都会挂死
        # （实测 ``test_watch::test_watch_loop_collects_several_samples_then_stops``
        # 在采样间隔的 ``time.sleep`` 里永不退出；见本文件末尾的还原守卫）。
        real_clock = module.time.time
        now = _at(14, 0)
        real_margin = module._LLM_HARD_FUSE_MARGIN_SECONDS
        try:
            module.time.time = lambda: now
            module._LLM_HARD_FUSE_MARGIN_SECONDS = 0.2
            plugin._state.last_tick_at = now - 600
            plugin._llm_ready = lambda _now: True
            plugin._fetch_identity = lambda: asyncio.sleep(0)
            plugin.config.activity.llm.skip_when_forced = False
            plugin.config.activity.llm.timeout_ms = 1000

            async def never_returns(**_kwargs):
                await asyncio.sleep(3600)

            plugin.ctx.llm.generate = never_returns
            # 关键断言：这一轮必须**返回**。真挂住的话这里先超时，测试红。
            await asyncio.wait_for(plugin._sim_tick(), timeout=10)
        finally:
            module.time.time = real_clock
            module._LLM_HARD_FUSE_MARGIN_SECONDS = real_margin

        stats = plugin._llm_stats_today(now)
        assert stats.get("asked") == 1, stats
        assert stats.get("failed") == 1, "熔断要记进既有失败链，而不是静默返回"
        # 熔断被 ``_is_timeout_failure`` 认成「等超时」⇒ 复用了既有告警，不必另开分支
        assert plugin._state.activity == "daily", "熔断后保持当前活动"
        assert plugin._state.llm_fail_streak == 1, "熔断计入失败计数，供冷却使用"

    asyncio.run(run())


def test_outer_fuse_margin_is_positive():
    """外层总闸必须**严格高于**内层 RPC 预算（铁律：外层先断会盖住内层错误）。

    总闸是 ``timeout_ms / 1000 + _LLM_HARD_FUSE_MARGIN_SECONDS``，所以余量为正
    就等于「外层在内层之后」。这条守的是那个常数不被改成 0 或负数——
    改成 0 之后行为看起来照常（正常调用内层先超时），只有真挂起时才暴露。
    """

    module, plugin, host = _make_plugin()
    assert module._LLM_HARD_FUSE_MARGIN_SECONDS > 0, "余量必须为正，否则外层会先于内层断"


def test_frozen_clock_cases_restore_the_real_clock():
    """时钟还原守卫：打桩打的是**标准库 time**，漏还原会把后面所有用例挂死。

    实测代价（2026-10-18）：``test_hanging_model_call_is_cut_by_the_outer_hard_fuse``
    的 ``finally`` 写成 ``module.time = real_time``——只换掉了插件模块的属性，全局
    ``time.time`` 仍是冻结的 lambda。于是 ``test_watch.py`` 的 watch 循环在
    ``while time.time() < next_wall`` 里永不退出（堆栈停在采样间隔的 ``time.sleep``），
    **单跑 test_watch 绿、全量跑挂死**，而门禁只看到「卡住」看不到原因。

    这条守的是「跑完这一族用例之后，时钟必须还是活的」。
    """

    import time as _time

    first = _time.time()
    _time.sleep(0.01)
    assert _time.time() > first, (
        "全局 time.time 被冻结且没有还原（曾让 test_watch 的 watch 循环永久挂起）"
    )


def test_frozen_clock_cases_restore_the_real_clock():
    """时钟还原守卫：打桩打的是**标准库 time**，漏还原会把后面所有用例挂死。

    实测代价（2026-10-18）：``test_hanging_model_call_is_cut_by_the_outer_hard_fuse``
    的 ``finally`` 写成 ``module.time = real_time``——只换掉了插件模块的属性，全局
    ``time.time`` 仍是冻结的 lambda。于是 ``test_watch.py`` 的 watch 循环在
    ``while time.time() < next_wall`` 里永不退出（堆栈停在采样间隔的 ``time.sleep``），
    **单跑 test_watch 绿、全量跑挂死**，而门禁只看到「卡住」看不到原因。

    这条守的是「跑完这一族用例之后，时钟必须还是活的」。
    """

    import time as _time

    first = _time.time()
    _time.sleep(0.01)
    assert _time.time() > first, (
        "全局 time.time 被冻结且没有还原（曾让 test_watch 的 watch 循环永久挂起）"
    )
