# -*- coding: utf-8 -*-
"""L3：班表按日微扰 + 加班日（v1.17.0，PR-SCH-2）。

改进方案 §2 P2-1：习惯表有按日抖动（``[routines] jitter_minutes`` 的整个存在
理由就是「天天分秒不差地触发是最刺眼的机器感」），班表却一直是她 09:30 整到点、
18:30 整下班，天天如此；``overtime`` 活动也从来不会因为「今天要加班」而出现。

两条派生都走 ``sha1(day_key | 窗口 | 用途)`` 的**确定性**路线：同一天恒同答案、
跨天不同、重启一致、零新表，并且必须在**提示词、强制层、白问判定、重新取种子**
四处拿同一份（否则又是 P0-1 式自相矛盾）。
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

import life_activity as A  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
TZ = 0  # 测试脚手架统一用 UTC，本地时刻 = 这里构造的 UTC 时刻

# 2026-10-08 = 周四（工作日，日历无条目）
THU = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)


def _at(hour: int, minute: int = 0, day: int = 8) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).timestamp()


def work_config(**overrides) -> A.ScheduleConfig:
    base = dict(
        enabled=True,
        workdays=(1, 2, 3, 4, 5),
        work_window=(9 * 60 + 30, 18 * 60 + 30),
        commute_minutes=45,
        lunch_window=(12 * 60, 13 * 60),
    )
    base.update(overrides)
    return A.ScheduleConfig(**base)


def _make_plugin(**overrides):
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_schedule_jitter")
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


# ================================================================ 确定性派生


def test_shift_is_off_by_default_and_bounded():
    assert A.schedule_day_shift("2026-10-08", jitter_minutes=0) == 0
    assert A.schedule_day_shift("2026-10-08", jitter_minutes=-5) == 0
    for day in range(1, 29):
        shift = A.schedule_day_shift(f"2026-10-{day:02d}", jitter_minutes=15)
        assert -15 <= shift <= 15, day
    # 上限钳制：配成 900 分钟不是「自由」，是写错了
    for day in range(1, 29):
        shift = A.schedule_day_shift(f"2026-10-{day:02d}", jitter_minutes=900)
        assert -A.MAX_SCHEDULE_JITTER_MINUTES <= shift <= A.MAX_SCHEDULE_JITTER_MINUTES


def test_shift_is_stable_within_a_day_and_varies_across_days():
    same = [
        A.schedule_day_shift("2026-10-08", jitter_minutes=20, work_window_text="09:30-18:30")
        for _ in range(5)
    ]
    assert len(set(same)) == 1, "同一天必须恒同答案（否则窗口会来回跳）"

    values = {
        A.schedule_day_shift(f"2026-10-{day:02d}", jitter_minutes=20)
        for day in range(1, 32)
    }
    assert len(values) > 3, f"跨天应该真的在变：{sorted(values)}"


def test_overtime_flags_are_deterministic_and_calibrated():
    assert A.schedule_day_overtime("2026-10-08", probability=0.0) is False
    assert A.schedule_day_overtime("2026-10-08", probability=1.0) is True
    assert A.schedule_day_overtime("2026-10-08", probability="坏值") is False

    hits = [
        A.schedule_day_overtime(f"2026-{month:02d}-{day:02d}", probability=0.3)
        for month in (1, 2, 3)
        for day in range(1, 29)
    ]
    ratio = sum(1 for item in hits if item) / len(hits)
    assert 0.2 < ratio < 0.4, f"p=0.3 的长期频率应落在 0.2~0.4，实测 {ratio:.3f}"
    # 同一天重复问，答案不变（不消耗随机源）
    assert A.schedule_day_overtime("2026-10-08", probability=0.5) == A.schedule_day_overtime(
        "2026-10-08", probability=0.5
    )


# ================================================================ 纯模块：事实


def test_without_a_day_key_nothing_changes():
    """不给 ``day_key`` = 不微扰（老调用点与既有用例逐位不变）。"""

    plain = A.schedule_facts(THU.replace(hour=10), work_config(daily_jitter_minutes=30))
    keyed = A.schedule_facts(
        THU.replace(hour=10), work_config(daily_jitter_minutes=0), day_key="2026-10-08"
    )
    assert plain.prompt_lines == keyed.prompt_lines


def test_shift_moves_the_whole_work_window_consistently():
    day_key = "2026-10-08"
    cfg = work_config(daily_jitter_minutes=30)
    shift = A.schedule_day_shift(day_key, jitter_minutes=30, work_window_text="09:30-18:30")
    text = "\n".join(A.schedule_facts(THU.replace(hour=10), cfg, day_key=day_key).prompt_lines)
    expected = (
        f"{A.minutes_to_hhmm(9 * 60 + 30 + shift)}-{A.minutes_to_hhmm(18 * 60 + 30 + shift)}"
    )
    assert expected in text, text

    # 同一生活日的另一个时刻：窗口必须一样（微扰不随 tick 变）
    later = "\n".join(A.schedule_facts(THU.replace(hour=15), cfg, day_key=day_key).prompt_lines)
    assert expected in later, later


def test_overtime_extends_the_end_and_says_so():
    cfg = work_config(overtime_probability=1.0, overtime_extra_minutes=120)
    facts = A.schedule_facts(THU.replace(hour=19), cfg, day_key="2026-10-08")
    text = "\n".join(facts.prompt_lines)
    assert "加班" in text and "20:30" in text
    assert facts.phase == A.SCHEDULE_WORK, "19:00 在加班顺延后的在岗窗口里"
    assert facts.minutes_to_off == 90

    off = A.schedule_facts(THU.replace(hour=19), work_config(), day_key="2026-10-08")
    assert off.phase == A.SCHEDULE_OFF_WORK, "没加班时 19:00 是刚下班"


def test_overtime_phase_blocks_the_same_activities_in_enforce():
    """加班日的在岗延长必须同时作用在强制层（同源）。"""

    policy = A.EnforcePolicy(min_dwell_minutes=60, schedule=work_config(overtime_probability=1.0))
    facts = A.ActivityFacts(
        activity=A.DAILY,
        minutes_in_activity=120,
        now_minutes=19 * 60,
        energy=7.0,
        schedule=A.schedule_facts(
            THU.replace(hour=19),
            work_config(overtime_probability=1.0),
            day_key="2026-10-08",
        ),
    )
    assert A.activity_blocked_by_schedule(A.GAME, facts, policy)

    no_overtime = A.ActivityFacts(
        activity=A.DAILY,
        minutes_in_activity=120,
        now_minutes=19 * 60,
        energy=7.0,
        schedule=A.schedule_facts(THU.replace(hour=19), work_config(), day_key="2026-10-08"),
    )
    assert A.activity_blocked_by_schedule(A.GAME, no_overtime, policy) == ""


# ================================================================ 插件接线


def test_plugin_prompt_and_enforce_share_the_overtime_day():
    _m, plugin, _host = _make_plugin(
        schedule={"overtime_probability": 1.0, "overtime_extra_minutes": 120}
    )
    now = _at(19)
    facts = plugin._schedule_facts_now(now)
    assert facts.phase == A.SCHEDULE_WORK
    assert "加班" in "\n".join(plugin._schedule_lines_now(now))

    plugin._state.activity = A.DAILY
    plugin._state.activity_since = now - 7200
    plugin._state.energy = 7.0
    plugin._state.awake_minutes_today = 600
    plugin._enforce_and_apply(now, A.ActivityDecision(A.GAME, "开了几把"))
    assert plugin._state.activity == A.DAILY, "加班日在岗时不该放行游戏"

    _m2, plugin2, _host2 = _make_plugin()
    plugin2._state.activity = A.DAILY
    plugin2._state.activity_since = now - 7200
    plugin2._state.energy = 7.0
    plugin2._state.awake_minutes_today = 600
    plugin2._enforce_and_apply(now, A.ActivityDecision(A.GAME, "开了几把"))
    assert plugin2._state.activity == A.GAME, "默认不加班：19:00 已是自由时间"


def test_plugin_applies_the_daily_shift_from_the_life_day_key():
    _m, plugin, _host = _make_plugin(schedule={"daily_jitter_minutes": 30})
    now = _at(10)
    day_key = plugin._schedule_day_key(local_datetime(now, TZ))
    assert day_key == day_key_of(local_datetime(now, TZ), 12)

    shift = A.schedule_day_shift(day_key, jitter_minutes=30, work_window_text="09:30-18:30")
    text = "\n".join(plugin._schedule_lines_now(now))
    expected = (
        f"{A.minutes_to_hhmm(9 * 60 + 30 + shift)}-{A.minutes_to_hhmm(18 * 60 + 30 + shift)}"
    )
    assert expected in text, text
    # 同源：插件算出来的事实 == 用同一个 day_key 直接调纯函数的结果
    direct = A.schedule_facts(
        local_datetime(now, TZ), plugin._schedule_config(), day_key=day_key
    )
    assert plugin._schedule_facts_now(now).prompt_lines == direct.prompt_lines
    assert plugin._schedule_facts_now(now).phase == direct.phase
