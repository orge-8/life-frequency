# -*- coding: utf-8 -*-
"""L3：生理锚点（physio）的解析、结算与接线。

钉住六件事：

1. 行 DSL 的坏值必须告警（时间窗 / 名称 / 类型 / weight）；
2. **清醒才饿**——睡着时饱腹不下降（半夜不会饿醒）；
3. 吃饭的回饱量：饿透的人一顿拉回安全线以上（线性 +delta 不是人）；
4. 命中走 enforce——**睡着的她不会被「该吃早饭了」叫醒**；
5. 同一天同一窗口只触发一次（时间窗 90 分钟、tick 10 分钟 ⇒ 不记账命中九次）；
6. weight 没中当天不再重掷。
"""

import asyncio
import pathlib
import sys
from datetime import datetime, timezone

import pytest

import life_physio as P

pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

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
SOURCE_PHYSIO = "physio"
SLEEP = "sleep"

# 2026-10-08 = 周四
THU = 8


def _at(hour: int, minute: int, day: int = THU) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).timestamp()


def _day_key(now: float, boundary_hour: int = 12) -> str:
    return day_key_of(local_datetime(now, 0), boundary_hour)


def _make_plugin(meals=None, **overrides):
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_physio")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = 0
    config["physio"]["enabled"] = True
    if meals is not None:
        config["physio"]["meals"] = list(meals)
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    plugin._rebuild_from_config()
    return module, plugin, host


# ---------------------------------------------------------------- 纯模块


def test_parse_meal_lines_roundtrip_and_warnings():
    windows, warnings = P.parse_meal_lines(
        ["07:00-08:30|早餐|meal|weight=0.9", "22:00-23:30|洗澡|bath"]
    )
    assert warnings == []
    assert [(w.start, w.end, w.label, w.kind) for w in windows] == [
        (420, 510, "早餐", "meal"),
        (1320, 1410, "洗澡", "bath"),
    ]
    assert windows[0].weight == 0.9


def test_parse_meal_lines_rejects_garbage():
    windows, warnings = P.parse_meal_lines(
        ["早上|早餐", "07:00-07:00|零长", "|没有名字"]
    )
    assert windows == ()
    assert len(warnings) == 3
    assert any("零长度" in item for item in warnings)
    # 坏类型不拒绝整行（降级 meal 并告警）
    windows, warnings = P.parse_meal_lines(["08:00-09:00|怪类型|dinner"])
    assert len(windows) == 1 and windows[0].kind == "meal"
    assert any("不是 meal/bath/snack" in item for item in warnings)


def test_satiety_only_decays_while_awake():
    satiety = P.settle_satiety(8.0, minutes=120, asleep=False, decay_per_hour=0.8)
    assert abs(satiety - 6.4) < 1e-9
    # 睡着：不消耗（半夜不会饿醒）
    assert P.settle_satiety(8.0, minutes=120, asleep=True, decay_per_hour=0.8) == 8.0
    # 钳在 0
    assert P.settle_satiety(0.5, minutes=600, asleep=False, decay_per_hour=0.8) == 0.0


def test_eat_amount_rescues_the_starving():
    # 轻度饿：当前 +4 已经超过七成线 → 直接吃满
    assert P.eat_amount(6.0) == pytest.approx(10.0)
    # 七成线上方的小饿：也直接拉满（6.5 + 4 = 10.5 → 钳 10）
    assert P.eat_amount(6.5) == pytest.approx(10.0)
    # 饿透：底线是「七成饱」而不是「+4」（max(0.5+4, 6.5) = 6.5）
    assert P.eat_amount(0.5) == pytest.approx(6.5)
    # 满腹吃不下
    assert P.eat_amount(10.0) == pytest.approx(10.0)
    # NaN 坏值按「比较饿」处理（3 + 4 = 7）
    assert P.eat_amount(float("nan")) == pytest.approx(7.0)


def test_active_windows_and_snack_gate():
    windows, _ = P.parse_meal_lines(
        ["07:00-08:30|早餐|meal", "12:00-13:30|加餐|snack"]
    )
    at_7 = datetime(2026, 10, 8, 7, 10, tzinfo=timezone.utc)
    assert [w.label for w in P.active_windows(windows, at_7.hour * 60 + at_7.minute)] == ["早餐"]
    assert not P.need_snack(5.0)
    assert P.need_snack(2.0)
    assert not P.need_snack(float("nan"))  # 坏值不出加餐


def test_proposal_carries_physio_source():
    windows, _ = P.parse_meal_lines(["07:00-08:30|早餐|meal", "22:00-23:30|洗澡|bath"])
    meal = P.proposal_for(windows[0], now=0.0)
    assert meal.activity == "meal" and meal.source == SOURCE_PHYSIO
    assert meal.scene == "吃早餐"
    bath = P.proposal_for(windows[1], now=0.0)
    assert bath.activity == "bath" and bath.scene == "洗澡"


# ---------------------------------------------------------------- 接线


def test_meal_hit_sets_activity_and_satiety():
    async def run():
        module, plugin, _ = _make_plugin(["12:00-13:30|午餐|meal|jitter=0"] if False else
                                         ["12:00-13:30|午餐|meal"])
        config = plugin._sim_config()
        now = _at(12, 30)
        plugin._state.activity = "daze"
        plugin._state.activity_since = now - 7200
        plugin._state.satiety = 5.0
        plugin._state.meal_count_today = 0

        fired, reason = await plugin._run_physio(now, config)
        assert fired is True, reason
        assert "午餐" in reason
        assert plugin._state.activity == "meal"
        assert plugin._state.activity_source == SOURCE_PHYSIO
        assert plugin._state.satiety == pytest.approx(9.5)  # 5 + 4.5
        assert plugin._state.meal_count_today == 1
        assert plugin._state.last_meal_at == now

    asyncio.run(run())


def test_same_window_fires_once_per_day():
    async def run():
        module, plugin, _ = _make_plugin(["12:00-13:30|午餐|meal"])
        config = plugin._sim_config()
        plugin._state.activity = "daze"
        plugin._state.activity_since = _at(12, 0) - 7200

        assert (await plugin._run_physio(_at(12, 10), config))[0] is True
        again, reason = await plugin._run_physio(_at(12, 50), config)
        assert again is False
        assert "生理窗内" in reason, reason
        # 次日同一个窗口重新可用
        plugin._state.activity_since = _at(12, 0, 9) - 7200
        assert (await plugin._run_physio(_at(12, 10, 9), config))[0] is True

    asyncio.run(run())


def test_sleeping_bot_is_not_woken_by_breakfast():
    """睡着的她不被「该吃早饭了」叫醒——**睡满最短时长之前**。

    （enforce 的既有语义是「睡满 min_sleep_minutes 后任何有效提议都能叫醒她」，
    那是正确的人味：睡到自然醒然后去吃饭。这条用例钉的是「刚睡下就被生理窗拽起来」。）
    """

    async def run():
        module, plugin, _ = _make_plugin(["07:00-08:30|早餐|meal"])
        config = plugin._sim_config()
        now = _at(7, 30)
        plugin._state.activity = SLEEP
        plugin._state.activity_since = now - 3600  # 只睡了 60 分钟 < 180
        plugin._state.sleep_started_at = now - 3600
        plugin._state.energy = 5.0

        fired, _ = await plugin._run_physio(now, config)
        assert fired is True, "proposal 发生了"
        assert plugin._state.activity == SLEEP, "但强制层让它继续睡"
        assert plugin._state.meal_count_today == 0, "没吃成的饭不能进账"
        assert plugin._state.satiety == 8.0, "没吃成的饭不能回饱"

    asyncio.run(run())


def test_weight_miss_is_remembered_for_the_whole_window():
    async def run():
        module, plugin, _ = _make_plugin(["12:00-13:30|午餐|meal|weight=0"])
        config = plugin._sim_config()
        plugin._state.activity = "daze"
        plugin._state.activity_since = _at(12, 0) - 7200

        fired, reason = await plugin._run_physio(_at(12, 10), config)
        assert fired is False
        assert "今天没吃" in reason, reason
        again, _ = await plugin._run_physio(_at(13, 0), config)
        assert again is False, "同一窗口内不许重掷"

    asyncio.run(run())


def test_disabled_physio_is_inert():
    async def run():
        module, plugin, _ = _make_plugin(["12:00-13:30|午餐|meal"],
                                         physio={"enabled": False})
        assert await plugin._run_physio(_at(12, 30), plugin._sim_config()) == (False, "")
        # settle 也不结算饱腹
        before = plugin._state.satiety
        from life_sim import settle
        settle(plugin._state, now=_at(14, 0), config=plugin._sim_config(),
               rng=plugin._rng)
        assert plugin._state.satiety == before

    asyncio.run(run())


# ------------------------------------------------ v1.17.0（PR-PHY-3）日期修饰符


def test_physio_window_day_modifiers_filter_by_weekday():
    """``days=6-7``：周末才吃的午餐；工作日那一刻窗根本没命中。"""

    windows, warnings = P.parse_meal_lines(["12:00-13:00|午餐|meal|days=6-7"])
    assert warnings == []
    assert windows[0].days == (6, 7)
    saturday = P.active_windows(windows, 12 * 60 + 30, weekday=6, is_workday=False)
    thursday = P.active_windows(windows, 12 * 60 + 30, weekday=4, is_workday=True)
    assert [w.label for w in saturday] == ["午餐"]
    assert thursday == ()


def test_physio_window_workday_and_holiday_flags():
    """``workday_only`` / ``holiday_only`` 与日历判据（plugin 传进来的日标志）同源。"""

    workday_windows, _ = P.parse_meal_lines(["12:00-13:00|工作日午餐|meal|workday_only=true"])
    holiday_windows, _ = P.parse_meal_lines(["10:00-11:00|假期早午餐|meal|holiday_only=true"])

    assert P.active_windows(workday_windows, 12 * 60 + 30, weekday=4, is_workday=True)
    assert not P.active_windows(workday_windows, 12 * 60 + 30, weekday=4, is_workday=False)
    assert P.active_windows(holiday_windows, 10 * 60 + 30, weekday=4, is_workday=False, is_holiday=True)
    assert not P.active_windows(holiday_windows, 10 * 60 + 30, weekday=4, is_workday=True)


def test_physio_window_without_modifiers_still_matches_every_day():
    windows, _ = P.parse_meal_lines(["07:00-08:30|早餐|meal"])
    for weekday in (1, 4, 6, 7):
        for is_workday in (True, False):
            assert P.active_windows(windows, 7 * 60 + 30, weekday=weekday, is_workday=is_workday)
    # 不传日标志（老调用点）= 旧行为
    assert P.active_windows(windows, 7 * 60 + 30)


def test_physio_line_warns_on_unknown_modifier_and_bad_days():
    _windows, warnings = P.parse_meal_lines(["07:00-08:00|早餐|meal|wokday_only=true"])
    assert any("未知修饰符" in item and "wokday_only" in item for item in warnings), warnings

    _windows2, warnings2 = P.parse_meal_lines(["07:00-08:00|早餐|meal|days=8"])
    assert any("days" in item and "整行跳过" in item for item in warnings2), warnings2

    _windows3, warnings3 = P.parse_meal_lines(
        ["07:00-08:00|早餐|meal|workday_only=false"]
    )
    assert warnings3 == [], "=false 是真的关闭，不该告警"
    assert _windows3[0].workday_only is False


def test_weekday_and_bool_parsers_are_one_implementation():
    """三处行 DSL 的星期/布尔解析现在是同一份核心（PR-PHY-3）。"""

    import life_activity as A
    import life_routines as R

    for text in ("1-5", "六日", "1,3,5", "6-1", "周三", "一二三四五", "", "8", "abc"):
        core, core_warnings = A.parse_weekday_set(text)
        routine_days, routine_error = R._as_weekdays(text)
        workdays, workday_warnings = A.parse_workdays(text, default=(1, 2, 3, 4, 5))
        if core is None:
            assert routine_days == ()
            assert workdays == (1, 2, 3, 4, 5)
        else:
            assert routine_days == tuple(sorted(core))
            assert workdays == tuple(sorted(core))
            assert routine_error == ""
        assert bool(core_warnings) == bool(routine_error) or core is None

    for text in ("true", "false", "1", "0", "yes", "off", "是", "禁用", "", "maybe"):
        core_bool, core_ok = A.parse_bool_flag(text, False)
        routine_bool, routine_ok = R._as_bool(text, False)
        assert (core_bool, core_ok) == (routine_bool, routine_ok)


def test_plugin_physio_window_follows_the_weekday():
    """接线：周末专属的午餐窗在工作日不提案、周六提案。"""

    async def run():
        _module, plugin, _ = _make_plugin(["12:00-13:00|周末午餐|meal|days=6-7|weight=1.0"])
        config = plugin._sim_config()

        plugin._state.activity = "daze"
        plugin._state.activity_since = _at(12, 0) - 7200
        thursday, _reason = await plugin._run_physio(_at(12, 30, THU), config)
        assert thursday is False, "周四不该命中周末窗"
        assert plugin._state.meal_count_today == 0

        # 2026-10-10 = 周六
        plugin._state.activity_since = _at(12, 0, 10) - 7200
        saturday, reason = await plugin._run_physio(_at(12, 30, 10), config)
        assert saturday is True, reason
        assert plugin._state.meal_count_today == 1

    asyncio.run(run())


def test_plugin_physio_window_follows_the_calendar():
    """``workday_only`` 吃日历：把周四追加成法定节假日后，工作日午餐窗不再命中。"""

    async def run():
        _module, plugin, _ = _make_plugin(
            ["12:00-13:00|工作日午餐|meal|workday_only=true|weight=1.0"],
            calendar={"extra": ["2026-10-08|调休放假|holiday"]},
        )
        config = plugin._sim_config()
        plugin._state.activity = "daze"
        plugin._state.activity_since = _at(12, 0) - 7200

        fired, _reason = await plugin._run_physio(_at(12, 30, THU), config)
        assert fired is False, "日历说今天放假 ⇒ 工作日午餐窗不该命中"
        assert plugin._state.meal_count_today == 0

    asyncio.run(run())
