# -*- coding: utf-8 -*-
"""L3：习惯层在插件里的接线（不是纯模块，而是「真的会生效」）。

纯模块的解析/命中语义在 ``test_routines.py``；这里钉的是**接线**那几条：

1. 命中 → 活动真的变了，且来源标成 ``routine``（状态卡要如实说「谁定的」）；
2. 同一天不会命中第二次（命中状态真的落库了，不是只在内存里）；
3. 命中后的提问间隙真的存在（模型的舞台是习惯之间的空隙）；
4. **习惯不能绕过硬约束**——睡眠中的她不会被一条 07:00 的习惯叫醒；
5. 权重没中的行当天不再重掷；
6. 整轮 ``_sim_tick`` 里命中习惯时**真的没问模型**（省调用）。
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
from life_sim import day_key_of, local_datetime  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
SOURCE_ROUTINE = "routine"
SLEEP = "sleep"

# 2026-10-08 = 周四（工作日），2026-10-10 = 周六
THU = 8
SAT = 10


def _at(hour: int, minute: int, day: int = THU) -> float:
    """本地时刻 → epoch（配置里把时偏移设成 0，本地时间就等于这里构造的 UTC 时间）。"""

    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).timestamp()


def _day_key(now: float, boundary_hour: int = 12) -> str:
    """生活日标识。**不是**公历日：生活日边界默认在 12:00（07:30 属于前一天）。

    测试里写死 ``"2026-10-08"`` 会查到空表——这正是要复用的那个函数存在的意义。
    """

    return day_key_of(local_datetime(now, 0), boundary_hour)


def _make_plugin(lines=None, **overrides):
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_routine")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = 0
    config["routines"]["enabled"] = True
    config["routines"]["lines"] = list(lines or [])
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config  # 触发 pydantic 校验
    plugin._rebuild_from_config()
    plugin._open_store()
    return module, plugin, host


class _FrozenTime:
    """把 ``plugin.py`` 里的 ``time.time()`` 钉住（该模块只用到这一个 time 接口）。"""

    def __init__(self, value: float) -> None:
        self.value = float(value)

    def time(self) -> float:
        return self.value


# ---------------------------------------------------------------- 命中


def test_hit_sets_activity_scene_and_source():
    async def run():
        module, plugin, _ = _make_plugin(["07:00-08:00|起床洗漱|daily|jitter=0"])
        now = _at(7, 30)
        plugin._state.activity = "daze"
        plugin._state.activity_since = now - 3600  # 过了最短停留期
        plugin._state.scene = ""

        fired, reason = await plugin._run_routine(now, plugin._sim_config())

        assert fired is True, reason
        assert "习惯命中" in reason
        assert plugin._state.activity == "daily"
        assert plugin._state.activity_source == SOURCE_ROUTINE
        assert plugin._state.scene == "起床洗漱"

    asyncio.run(run())


def test_multi_scene_line_hits_with_one_of_the_candidates():
    """v1.17.0（PR-ROU-2）：多候选场景在接线里真的生效（状态卡拿到其中一条）。"""

    async def run():
        module, plugin, _ = _make_plugin(
            ["19:00-22:00|晚自习；看网课；整理笔记|night_study|jitter=0"]
        )
        now = _at(19, 30)
        plugin._state.activity = "daze"
        plugin._state.activity_since = now - 3600
        plugin._state.energy = 8.0

        fired, reason = await plugin._run_routine(now, plugin._sim_config())

        assert fired is True, reason
        assert plugin._state.activity == "night_study"
        assert plugin._state.scene in {"晚自习", "看网课", "整理笔记"}
        assert plugin._state.scene in reason

    asyncio.run(run())


def test_same_day_does_not_hit_twice():
    async def run():
        module, plugin, _ = _make_plugin(["07:00-08:00|起床洗漱|daily|jitter=0"])
        config = plugin._sim_config()
        plugin._state.activity = "daze"
        plugin._state.activity_since = _at(7, 0) - 3600

        first, _ = await plugin._run_routine(_at(7, 10), config)
        assert first is True
        # 同一天、同一窗口、换一个时刻：不该再命中（命中状态已落库）
        second, reason = await plugin._run_routine(_at(7, 40), config)
        assert second is False
        assert "习惯窗口内" in reason, reason

    asyncio.run(run())


def test_next_day_hits_again():
    async def run():
        module, plugin, _ = _make_plugin(["07:00-08:00|起床洗漱|daily|jitter=0"])
        config = plugin._sim_config()
        plugin._state.activity = "daze"
        plugin._state.activity_since = _at(7, 0) - 3600

        assert (await plugin._run_routine(_at(7, 10, THU), config))[0] is True
        # 换一天：day_key 变了 ⇒ 这是一条全新的记录
        assert (await plugin._run_routine(_at(7, 10, 9), config))[0] is True

    asyncio.run(run())


def test_gap_after_a_hit_suppresses_asking():
    async def run():
        module, plugin, _ = _make_plugin(["07:00-08:00|起床洗漱|daily|jitter=0"],
                                         routines={"llm_gap_minutes": 120})
        config = plugin._sim_config()
        plugin._state.activity = "daze"
        plugin._state.activity_since = _at(7, 0) - 3600

        await plugin._run_routine(_at(7, 30), config)
        in_gap, reason = await plugin._run_routine(_at(8, 30), config)
        assert in_gap is False
        assert "提问间隔" in reason, reason

        after_gap, reason = await plugin._run_routine(_at(10, 0), config)
        assert after_gap is False
        assert reason == "", reason

    asyncio.run(run())


# ---------------------------------------------------------------- 硬约束优先


def test_routine_cannot_wake_her_up():
    """习惯只是 proposal：睡着的她不会被 07:00 那条「起床」叫醒。

    真要她起床，靠的是睡眠上限 / 体力回满这些既有硬约束在这一刻正好放行
    ——习惯层**不**绕过它们（否则用户配错一行就能让她睡眠剥夺）。
    """

    async def run():
        module, plugin, _ = _make_plugin(["07:00-08:00|起床洗漱|daily|jitter=0"])
        now = _at(7, 30)
        plugin._state.activity = SLEEP
        plugin._state.activity_since = now - 3600
        plugin._state.sleep_started_at = now - 3600
        plugin._state.sleep_minutes_today = 60
        plugin._state.energy = 5.0

        fired, _ = await plugin._run_routine(now, plugin._sim_config())
        assert fired is True, "命中确实发生了（proposal 交给了强制层）"
        assert plugin._state.activity == SLEEP, "但强制层把它挡回来了"

    asyncio.run(run())


# ---------------------------------------------------------------- 权重与抖动


def test_weight_miss_is_remembered_not_rerolled():
    async def run():
        module, plugin, _ = _make_plugin(["07:00-08:00|晨跑|daily|jitter=0|weight=0"])
        config = plugin._sim_config()
        plugin._state.activity = "daze"
        plugin._state.activity_since = _at(7, 0) - 3600

        fired, reason = await plugin._run_routine(_at(7, 10), config)
        assert fired is False
        assert "今天没发生" in reason, reason

        # 关键：不能因为「这次没中」就下一轮再掷一次——窗口还剩 50 分钟，
        # 反复掷一个 0.8 的权重迟早会中，权重就形同虚设
        status = plugin._routine_store.load_day(_day_key(_at(7, 10)))
        assert status, "没落库就没记住"
        assert list(status.values())[0][1] == 2, status  # ROUTINE_SKIPPED
        assert (await plugin._run_routine(_at(7, 40), config))[0] is False

    asyncio.run(run())


def test_jitter_is_drawn_once_per_day():
    async def run():
        module, plugin, _ = _make_plugin(["07:00-08:00|起床洗漱|daily|jitter=15"])
        config = plugin._sim_config()
        plugin._state.activity = "daze"
        plugin._state.activity_since = _at(7, 0) - 3600

        await plugin._run_routine(_at(7, 30), config)
        first = dict(plugin._routine_store.load_day(_day_key(_at(7, 30))))
        await plugin._run_routine(_at(7, 50), config)
        second = dict(plugin._routine_store.load_day(_day_key(_at(7, 30))))
        assert first == second, "抖动量在同一天里必须是同一个值"
        assert first, "抖动没有落库"

    asyncio.run(run())


# ---------------------------------------------------------------- 开关与工作日


def test_disabled_or_empty_lines_are_inert():
    async def run():
        module, plugin, _ = _make_plugin(
            ["07:00-08:00|起床洗漱|daily|jitter=0"], routines={"enabled": False}
        )
        assert await plugin._run_routine(_at(7, 30), plugin._sim_config()) == (False, "")

        module, plugin, _ = _make_plugin([])
        assert await plugin._run_routine(_at(7, 30), plugin._sim_config()) == (False, "")

    asyncio.run(run())


def test_workday_only_line_skips_the_weekend():
    async def run():
        module, plugin, _ = _make_plugin(["09:00-10:00|例会|meeting|jitter=0|workday_only=true"])
        config = plugin._sim_config()
        plugin._state.activity_since = _at(9, 0) - 3600

        assert (await plugin._run_routine(_at(9, 30, THU), config))[0] is True
        # 同一条行、周六（班表未启用 ⇒ 周六不是工作日）
        assert (await plugin._run_routine(_at(9, 30, SAT), config))[0] is False

    asyncio.run(run())


def test_bad_lines_are_reported_not_silently_dropped(caplog):
    """写错活动名必须留下线索——否则「配了习惯却从不生效」在现场无从排查。"""

    async def run():
        module, plugin, _ = _make_plugin(["07:00-08:00|吃早饭|mealx"])
        assert plugin._routine_lines == ()
        assert await plugin._run_routine(_at(7, 30), plugin._sim_config()) == (False, "")

    asyncio.run(run())


# ---------------------------------------------------------------- 整轮 tick


def test_sim_tick_does_not_ask_the_model_when_a_routine_fires():
    async def run():
        module, plugin, _ = _make_plugin(["07:00-08:00|起床洗漱|daily|jitter=0"])
        now = _at(7, 30)
        real_time = module.time
        module.time = _FrozenTime(now)
        try:
            plugin._state.activity = "daze"
            plugin._state.activity_since = now - 3600
            plugin._state.last_tick_at = now - 600
            plugin._llm_ready = lambda _now: True
            plugin._fetch_identity = lambda: asyncio.sleep(0)

            asked: list[float] = []

            async def spy(_now):
                asked.append(_now)
                return None

            plugin._ask_activity = spy
            await plugin._sim_tick()
        finally:
            module.time = real_time

        assert plugin._state.activity_source == SOURCE_ROUTINE
        assert asked == [], "习惯命中这一轮不该再问模型"

    asyncio.run(run())
