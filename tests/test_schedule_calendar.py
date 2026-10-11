# -*- coding: utf-8 -*-
"""L3：班表吃日历（v1.17.0，PR-CAL-1）——法定节假日 / 调休上班日。

要解决的真问题（改进方案 §2 P0-1）：班表事实有**四个**计算点，而 v1.10.0 的
中国日历只接了习惯表与睡眠顺延两条——法定节假日当天，同一轮活动提示词里同时
出现「今天是：国庆节」「现在：在岗」「今天是休息日，不用上班」三句互相打架的
事实，强制层照样在上班相位禁睡禁游戏。

本文件钉住两件事：

1. 纯模块 ``schedule_facts`` 的日历覆盖语义（``workday_override`` / ``day_name``）；
2. **四处同源**：提示词行、强制层、白问判定、重新取种子拿的是同一份覆盖
   （plugin 侧 ``_schedule_calendar_override`` 是唯一入口）。
"""

import pathlib
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_activity as A  # noqa: E402
import life_sim as S  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
TZ = 480

#: 2026-10-01 是**周四**，日历里的「国庆节」（kind=holiday）——落在工作日上的
#: 法定节假日，正是 P0-1 的事故形态
HOLIDAY = datetime(2026, 10, 1, 10, 0)
#: 2026-10-10 是**周六**，用它配一条 ``workday_swap`` 的追加条目 = 调休上班
SWAP_SATURDAY = datetime(2026, 10, 10, 10, 0)
#: 2026-10-08 是**周四**，日历里没有任何条目 = 普通工作日
PLAIN_THURSDAY = datetime(2026, 10, 8, 10, 0)


def local_ts(dt: datetime) -> float:
    """本地 naive 时刻 → epoch（插件按 ``tz_offset_minutes=480`` 解释）。"""

    return dt.replace(tzinfo=timezone.utc).timestamp() - TZ * 60


def work_config(**overrides) -> A.ScheduleConfig:
    base = dict(
        enabled=True,
        workdays=(1, 2, 3, 4, 5),
        work_window=(9 * 60 + 30, 18 * 60 + 30),
        commute_minutes=45,
        lunch_window=(12 * 60, 13 * 60),
        duty="测试岗位",
    )
    base.update(overrides)
    return A.ScheduleConfig(**base)


# ================================================================ 纯模块


def test_holiday_override_turns_the_workday_into_a_rest_day():
    """法定节假日落在工作日：相位变休息日，文案带上节日名，不再提「在岗」。"""

    plain = A.schedule_facts(PLAIN_THURSDAY, work_config())
    assert plain.is_workday is True
    assert plain.phase == A.SCHEDULE_WORK
    assert "在岗" in "\n".join(plain.prompt_lines)

    holiday = A.schedule_facts(
        HOLIDAY, work_config(), workday_override=False, day_name="国庆节"
    )
    assert holiday.enabled is True
    assert holiday.is_workday is False
    assert holiday.phase == A.SCHEDULE_REST_DAY
    text = "\n".join(holiday.prompt_lines)
    assert "国庆节" in text and "法定节假日" in text and "不用上学" in text
    assert "在岗" not in text and "现在适合的活动" not in text
    assert holiday.minutes_to_off == 0 and holiday.minutes_to_work == 0


def test_holiday_override_removes_the_phase_restrictions():
    """日历说放假 ⇒ 相位矩阵不再禁游戏/看番/睡觉（enforce 同源）。"""

    policy = A.EnforcePolicy(min_dwell_minutes=60, schedule=work_config())
    working = A.ActivityFacts(
        activity=A.DAILY,
        minutes_in_activity=120,
        now_minutes=10 * 60,
        energy=7.0,
        schedule=A.schedule_facts(PLAIN_THURSDAY, work_config()),
    )
    assert A.activity_blocked_by_schedule(A.GAME, working, policy)

    holiday = A.ActivityFacts(
        activity=A.DAILY,
        minutes_in_activity=120,
        now_minutes=10 * 60,
        energy=7.0,
        schedule=A.schedule_facts(
            HOLIDAY, work_config(), workday_override=False, day_name="国庆节"
        ),
    )
    assert A.activity_blocked_by_schedule(A.GAME, holiday, policy) == ""
    assert A.activity_blocked_by_schedule(A.SLEEP, holiday, policy) == ""
    assert A.schedule_allowed_activities(holiday) == A.ALLOWED_ACTIVITIES
    # 提示词侧与强制层同源：假日不再收窄候选
    assert A.schedule_allowed_activities(A.schedule_facts(HOLIDAY, work_config())).count(
        A.GAME
    ) == 0


def test_swap_override_makes_a_saturday_a_workday():
    """调休上班的周六：相位按工作日算，文案说清「调休上班」。"""

    saturday = A.schedule_facts(SWAP_SATURDAY, work_config())
    assert saturday.is_workday is False and saturday.phase == A.SCHEDULE_REST_DAY

    swap = A.schedule_facts(
        SWAP_SATURDAY, work_config(), workday_override=True, day_name="调休"
    )
    assert swap.is_workday is True
    assert swap.phase == A.SCHEDULE_WORK
    text = "\n".join(swap.prompt_lines)
    assert "调休上班" in text and "09:30-18:30" in text and "在岗" in text
    # 调休日的在岗矩阵照常生效（这是「她今天真的要上班」的另一半）
    policy = A.EnforcePolicy(min_dwell_minutes=60, schedule=work_config())
    facts = A.ActivityFacts(now_minutes=10 * 60, energy=7.0, schedule=swap)
    assert A.activity_blocked_by_schedule(A.GAME, facts, policy)


def test_without_override_the_old_wording_is_bit_identical():
    """日历不表态（None）= v1.16.3 的旧行为：措辞与判定一字不差。"""

    for moment in (PLAIN_THURSDAY, SWAP_SATURDAY):
        old = A.schedule_facts(moment, work_config())
        new = A.schedule_facts(
            moment, work_config(), workday_override=None, day_name=""
        )
        assert old == new

    rest = A.schedule_facts(SWAP_SATURDAY, work_config())
    assert rest.prompt_lines == ("今天是休息日（周六），不上班。",)


def test_enforce_facts_passes_the_override_through():
    """``enforce_facts`` 是强制层与白问判定的共同入口，覆盖必须能穿过去。"""

    config = S.SimConfig(
        tz_offset_minutes=TZ,
        schedule=work_config(),
    )
    state = S.LifeState()
    local_now = HOLIDAY
    now = local_ts(local_now)

    blind = S.enforce_facts(state, now=now, config=config)
    assert blind.schedule.is_workday is True
    assert blind.schedule.phase == A.SCHEDULE_WORK

    aware = S.enforce_facts(
        state, now=now, config=config, workday_override=False, day_name="国庆节"
    )
    assert aware.schedule.is_workday is False
    assert aware.schedule.phase == A.SCHEDULE_REST_DAY
    assert any("国庆节" in line for line in aware.schedule.prompt_lines)
    # rest_day 也自动跟着变（原来只看班表工作日，现在吃日历）
    assert aware.rest_day is True

    reason = S.pointless_ask_reason(
        state, now=now, config=config, workday_override=False, day_name="国庆节"
    )
    assert isinstance(reason, str)  # 不抛就是同源的最低要求，语义由下面插件级用例钉


# ================================================================ 插件接线


def _make_plugin(**overrides):
    """带日历的插件脚手架（时区 480，班表启用，无习惯/生理窗干扰）。"""

    from fakehost import (  # noqa: PLC0415
        FakeHost,
        FakePaths,
        bind_context,
        build_context,
        get_default_config,
        load_plugin_module,
    )

    module = load_plugin_module(PLUGIN_DIR, "life_frequency_schedule_calendar")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = TZ
    config["frequency"]["quiet_hours"] = []
    config["physio"]["meals"] = []
    config["routines"]["lines"] = []
    config["schedule"].update(
        {
            "enabled": True,
            "workdays": "1-5",
            "work_window": "09:30-18:30",
            "commute_minutes": 45,
            "lunch_window": "12:00-13:00",
        }
    )
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    plugin._rebuild_from_config()
    return module, plugin, host


def test_plugin_reads_the_calendar_for_the_prompt_lines():
    _m, plugin, _host = _make_plugin()
    now = local_ts(HOLIDAY)

    assert plugin._schedule_calendar_override(
        datetime(2026, 10, 1, 10, 0)
    ) == (False, "国庆节")
    facts = plugin._schedule_facts_now(now)
    assert facts.is_workday is False and facts.phase == A.SCHEDULE_REST_DAY

    lines = plugin._schedule_lines_now(now)
    text = "\n".join(lines)
    assert "国庆节" in text and "法定节假日" in text
    assert "在岗" not in text and "上班时段" not in text
    # 休息日多睡那一行不再重复说「今天是休息日」（同段里说两遍会读成两件事）
    assert "休息日，不用上班" not in text

    # 普通工作日仍然照旧（日历不表态）
    plain = plugin._schedule_facts_now(local_ts(PLAIN_THURSDAY))
    assert plain.is_workday is True and plain.phase == A.SCHEDULE_WORK


def test_swap_entry_from_calendar_extra_makes_her_work_on_saturday():
    _m, plugin, _host = _make_plugin(
        calendar={"extra": ["2026-10-10|调休上班|workday_swap"]}
    )
    assert plugin._schedule_calendar_override(
        datetime(2026, 10, 10, 10, 0)
    ) == (True, "调休上班")
    facts = plugin._schedule_facts_now(local_ts(SWAP_SATURDAY))
    assert facts.is_workday is True and facts.phase == A.SCHEDULE_WORK
    assert any("调休上班" in line for line in plugin._schedule_lines_now(local_ts(SWAP_SATURDAY)))


def test_honor_calendar_off_restores_the_v163_behaviour():
    """``honor_calendar=false`` = 升级可回退：退回「只按星期判定」。"""

    _m, plugin, _host = _make_plugin(schedule={"honor_calendar": False})
    now = local_ts(HOLIDAY)
    assert plugin._schedule_calendar_override(datetime(2026, 10, 1, 10, 0)) == (None, "")
    facts = plugin._schedule_facts_now(now)
    assert facts.is_workday is True and facts.phase == A.SCHEDULE_WORK
    text = "\n".join(plugin._schedule_lines_now(now))
    assert "在岗" in text and "国庆节（周四），法定节假日" not in text


def _playing(plugin, now: float) -> None:
    """她已经在打游戏打了两小时（停留期早已满足）。"""

    plugin._state.activity = A.GAME
    plugin._state.activity_since = now - 7200
    plugin._state.energy = 7.0
    plugin._state.awake_minutes_today = 600


def test_enforce_path_sees_the_same_calendar_override():
    """强制层同源：假日不拦游戏；关掉 honor_calendar 后又拦回去。"""

    _m, plugin, _host = _make_plugin()
    now = local_ts(HOLIDAY)
    _playing(plugin, now)
    plugin._state.activity = A.DAILY
    plugin._enforce_and_apply(now, A.ActivityDecision(A.GAME, "开了几把"))
    assert plugin._state.activity == A.GAME, plugin._state.activity_note

    _m2, plugin2, _host2 = _make_plugin(schedule={"honor_calendar": False})
    _playing(plugin2, now)
    plugin2._state.activity = A.DAILY
    plugin2._enforce_and_apply(now, A.ActivityDecision(A.GAME, "开了几把"))
    assert plugin2._state.activity == A.DAILY, "关掉日历后应回到「在岗不许摸鱼」"


def test_reseed_seed_also_follows_the_calendar():
    """重新取种子也吃日历：国庆节取到的不是「在岗位上做事」。"""

    _m, plugin, _host = _make_plugin()
    now = local_ts(HOLIDAY)
    plugin._state.activity = A.DAILY
    plugin._state.llm_last_success_at = now - 48 * 3600
    plugin._reseed_activity_if_stale(now, plugin._sim_config())
    assert plugin._state.activity != A.WORK, plugin._state.activity_note
    assert "在岗" not in plugin._state.activity_note
