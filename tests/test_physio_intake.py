# -*- coding: utf-8 -*-
"""L3：进餐入账的唯一口径（v1.17.0，PR-PHY-1 / PR-ROU-1）。

两条缺陷（改进方案 §2 P1-2 / P1-4）：

* ``life_physio.eat_amount`` 的 floor/rescue 语义是**死代码**——plugin 里实际是
  「饱腹 + 4.5」平加，饿透（0.5/10）的人一顿饭只回半饱；
* 习惯行的 ``physio=true`` 是**死标记**——配置描述写着「v1.9.x 的 physio 消费」，
  全库零消费点：用户按描述写「吃早饭|meal|physio=true」，活动变成 meal 了，
  但不回饱、不计「今日已吃」，饱腹一路见底。

修法是把「一次真的吃成/洗成」收敛成**一个函数**
（``plugin._settle_physio_intake``），习惯表与生理窗共用；本文件同时钉住
「同一顿不重复回饱」（两条路常常重叠）与「被强制层收口就不入账」。
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

import life_activity as A  # noqa: E402
import life_physio as P  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent

# 2026-10-08 = 周四（工作日）
THU = 8


def _at(hour: int, minute: int, day: int = THU) -> float:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).timestamp()


def _make_plugin(lines=None, **overrides):
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_physio_intake")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = 0
    config["routines"]["enabled"] = True
    config["routines"]["lines"] = list(lines or [])
    config["schedule"]["enabled"] = False
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    plugin._rebuild_from_config()
    plugin._open_store()
    return module, plugin, host


# ================================================================ 纯函数：回饱口径


def test_eat_amount_matches_the_old_flat_gain_above_two():
    """饱腹 ≥ 2.0 时 ``eat_amount(rescue=4.5)`` 与 v1.16.3 的 `+4.5` 逐位一致。"""

    for satiety in (2.0, 3.4, 5.0, 6.2, 8.0, 9.0):
        new = P.eat_amount(satiety, floor=0.65, rescue=4.5)
        old = min(10.0, satiety + 4.5)
        assert new == pytest.approx(old), satiety


def test_eat_amount_lifts_a_starving_person():
    """饿透（< 2.0）时 floor 生效：一顿饭至少回到 6.5 成——这是模块本来的承诺。"""

    assert P.eat_amount(0.5, floor=0.65, rescue=4.5) == pytest.approx(6.5)
    assert P.eat_amount(0.0, floor=0.65, rescue=4.5) == pytest.approx(6.5)
    assert P.eat_amount(1.9, floor=0.65, rescue=4.5) == pytest.approx(6.5)
    # 旧行为（平加）在同样输入下只有 5.0/4.5/6.4 —— 饿着的人吃半饱
    assert min(10.0, 0.5 + 4.5) < P.eat_amount(0.5, floor=0.65, rescue=4.5)


def test_eat_amount_never_exceeds_the_cap():
    for satiety in (6.5, 9.9, 10.0):
        assert P.eat_amount(satiety, floor=0.65, rescue=4.5) <= P.SATIETY_MAX


# ================================================================ 共用入账函数


def test_meal_intake_uses_eat_amount_and_counts():
    _m, plugin, _host = _make_plugin()
    now = _at(8, 10)
    plugin._state.satiety = 5.0
    plugin._state.meal_count_today = 0

    settled = plugin._settle_physio_intake(A.ActivityDecision(A.MEAL, "吃早饭"), now)

    assert settled is True
    assert plugin._state.satiety == pytest.approx(9.5)
    assert plugin._state.meal_count_today == 1
    assert plugin._state.last_meal_at == pytest.approx(now)


def test_starving_meal_gets_the_floor():
    _m, plugin, _host = _make_plugin()
    now = _at(18, 30)
    plugin._state.satiety = 0.5

    assert plugin._settle_physio_intake(A.ActivityDecision(A.MEAL, "吃晚饭"), now)
    assert plugin._state.satiety == pytest.approx(6.5)
    assert plugin._state.meal_count_today == 1


def test_same_meal_within_the_gap_does_not_double_count():
    _m, plugin, _host = _make_plugin()
    first = _at(7, 5)
    plugin._state.satiety = 4.0

    assert plugin._settle_physio_intake(A.ActivityDecision(A.MEAL, "早饭"), first)
    satiety_after_first = plugin._state.satiety
    assert plugin._state.meal_count_today == 1

    # 15 分钟后习惯行又命中同一顿：活动照旧，但不能回第二次饱
    second = first + 15 * 60
    assert plugin._settle_physio_intake(A.ActivityDecision(A.MEAL, "早饭"), second) is False
    assert plugin._state.satiety == pytest.approx(satiety_after_first)
    assert plugin._state.meal_count_today == 1
    assert plugin._state.last_meal_at == pytest.approx(first), "锚点不该被重复进餐推后"


def test_a_genuinely_separate_meal_counts_again():
    _m, plugin, _host = _make_plugin()
    breakfast = _at(8, 0)
    dinner = _at(18, 30)
    plugin._state.satiety = 2.5

    assert plugin._settle_physio_intake(A.ActivityDecision(A.MEAL, "早饭"), breakfast)
    assert plugin._settle_physio_intake(A.ActivityDecision(A.MEAL, "晚饭"), dinner)
    assert plugin._state.meal_count_today == 2
    assert plugin._state.last_meal_at == pytest.approx(dinner)


def test_bath_only_records_the_time():
    _m, plugin, _host = _make_plugin()
    now = _at(22, 30)
    plugin._state.satiety = 6.0

    assert plugin._settle_physio_intake(A.ActivityDecision(A.BATH, "洗澡"), now)
    assert plugin._state.last_bath_at == pytest.approx(now)
    assert plugin._state.satiety == pytest.approx(6.0), "洗澡不回饱"
    assert plugin._state.meal_count_today == 0


# ================================================================ 习惯行 physio=true


def test_routine_physio_line_actually_feeds_her():
    """配置描述承诺的消费，现在真的发生了。"""

    async def run():
        _m, plugin, _host = _make_plugin(
            ["08:00-08:30|吃早饭|meal|physio=true|jitter=0"]
        )
        now = _at(8, 10)
        plugin._state.activity = A.DAZE
        plugin._state.activity_since = now - 3600
        plugin._state.satiety = 5.0
        plugin._state.meal_count_today = 0

        fired, reason = await plugin._run_routine(now, plugin._sim_config())

        assert fired is True, reason
        assert plugin._state.activity == A.MEAL
        assert plugin._state.satiety == pytest.approx(9.5)
        assert plugin._state.meal_count_today == 1
        assert plugin._state.last_meal_at == pytest.approx(now)

    asyncio.run(run())


def test_routine_without_the_physio_flag_does_not_feed():
    """没写 physio=true 的行只是「活动」，不碰饱腹——旧行为逐位不变。"""

    async def run():
        _m, plugin, _host = _make_plugin(["08:00-08:30|坐着发呆|daze|jitter=0"])
        now = _at(8, 10)
        plugin._state.activity = A.BATH
        plugin._state.activity_since = now - 3600
        plugin._state.satiety = 5.0

        fired, _reason = await plugin._run_routine(now, plugin._sim_config())

        assert fired is True
        assert plugin._state.activity == A.DAZE
        assert plugin._state.satiety == pytest.approx(5.0)
        assert plugin._state.meal_count_today == 0

    asyncio.run(run())


def test_routine_physio_line_rejected_by_enforce_does_not_feed():
    """她睡着时习惯不能把她叫起来吃饭（睡眠优先）——没吃成就不该回饱。"""

    async def run():
        _m, plugin, _host = _make_plugin(
            ["08:00-08:30|吃早饭|meal|physio=true|jitter=0"]
        )
        now = _at(8, 10)
        plugin._state.activity = A.SLEEP
        plugin._state.activity_since = now - 3600
        plugin._state.sleep_started_at = now - 3600
        plugin._state.sleep_minutes_today = 60
        plugin._state.energy = 5.0
        plugin._state.satiety = 4.0

        fired, _reason = await plugin._run_routine(now, plugin._sim_config())

        assert fired is True, "习惯本身命中了（进了强制层）"
        assert plugin._state.activity == A.SLEEP, plugin._state.activity_note
        assert plugin._state.satiety == pytest.approx(4.0)
        assert plugin._state.meal_count_today == 0
        assert plugin._state.last_meal_at == 0.0

    asyncio.run(run())


# ================================================================ 生理窗走同一条路


def test_physio_window_uses_the_shared_intake():
    async def run():
        _m, plugin, _host = _make_plugin(
            physio={"meals": ["07:00-08:30|早餐|meal|weight=1.0"]}
        )
        now = _at(7, 30)
        plugin._state.activity = A.DAZE
        plugin._state.activity_since = now - 3600
        plugin._state.satiety = 0.5
        plugin._state.meal_count_today = 0

        fired, reason = await plugin._run_physio(now, plugin._sim_config())

        assert fired is True, reason
        assert plugin._state.activity == A.MEAL
        assert plugin._state.satiety == pytest.approx(6.5), "生理窗也走 eat_amount 的 floor"
        assert plugin._state.meal_count_today == 1

    asyncio.run(run())


def test_physio_window_and_routine_share_one_meal():
    """两条路都命中同一顿：只回一次饱、只计一顿（180 分钟去重）。"""

    async def run():
        _m, plugin, _host = _make_plugin(
            ["07:00-08:30|吃早饭|meal|physio=true|jitter=0"],
            physio={"meals": ["07:00-08:30|早餐|meal|weight=1.0"]},
        )
        now = _at(7, 5)
        plugin._state.activity = A.DAZE
        plugin._state.activity_since = now - 3600
        plugin._state.satiety = 5.0
        plugin._state.meal_count_today = 0

        routine_fired, _reason = await plugin._run_routine(now, plugin._sim_config())
        assert routine_fired is True
        assert plugin._state.meal_count_today == 1

        plugin._state.activity_since = now - 3600
        physio_fired, _reason2 = await plugin._run_physio(now + 600, plugin._sim_config())
        assert physio_fired is True
        assert plugin._state.meal_count_today == 1, "同一顿不该记两次"
        assert plugin._state.satiety < 10.0

    asyncio.run(run())


# ================================================================ 最短停留期覆盖


def _dwell_policy(**overrides):
    base = dict(
        sleep_window=(3 * 60, 11 * 60),
        sleep_energy_threshold=3.0,
        max_sleep_hours=12.0,
        min_awake_hours_per_day=8.0,
        min_dwell_minutes=60,
        min_sleep_minutes=180,
    )
    base.update(overrides)
    return A.EnforcePolicy(**base)


def _meal_facts(**overrides):
    base = dict(
        activity=A.MEAL,
        minutes_in_activity=45,
        now_minutes=12 * 60 + 30,
        energy=6.0,
        awake_minutes_today=600,
    )
    base.update(overrides)
    return A.ActivityFacts(**base)


def test_default_policy_has_no_overrides_and_keeps_the_global_dwell():
    """纯模块默认空表 = v1.16.3 行为（每个活动都用 ``min_dwell_minutes``）。"""

    policy = _dwell_policy()
    assert policy.dwell_overrides == {}
    assert A.dwell_minutes_for(A.MEAL, policy) == 60
    result = A.enforce(_meal_facts(minutes_in_activity=45), A.ActivityDecision(A.GAME, "玩"), policy)
    assert result.activity == A.MEAL and "未满 60 分钟" in result.note


def test_dwell_override_makes_the_meal_duration_config_effective():
    """``meal_duration_minutes=40``：39 分钟被拒、41 分钟放行。"""

    policy = _dwell_policy(dwell_overrides={A.MEAL: 40})
    assert A.dwell_minutes_for(A.MEAL, policy) == 40
    assert A.dwell_minutes_for(A.GAME, policy) == 60, "没配的活动仍用全局值"

    blocked = A.enforce(_meal_facts(minutes_in_activity=39), A.ActivityDecision(A.GAME, "玩"), policy)
    assert blocked.activity == A.MEAL and "未满 40 分钟" in blocked.note

    allowed = A.enforce(_meal_facts(minutes_in_activity=41), A.ActivityDecision(A.GAME, "玩"), policy)
    assert allowed.activity == A.GAME


def test_dwell_override_bad_values_fall_back_to_the_global():
    policy = _dwell_policy(dwell_overrides={A.MEAL: "四十分钟"})
    assert A.dwell_minutes_for(A.MEAL, policy) == 60


def test_pointless_ask_matches_enforce_under_dwell_overrides():
    """对拍：跳过判定与实际裁定必须给出同一个答案（含覆盖表）。"""

    policy = _dwell_policy(dwell_overrides={A.MEAL: 40})
    for minutes in (10, 39, 40, 41, 59, 61):
        facts = _meal_facts(minutes_in_activity=minutes)
        reason = A.request_is_pointless(facts, policy)
        resolved = A.enforce(facts, A.ActivityDecision(A.GAME, "玩"), policy)
        if reason:
            assert resolved.activity == facts.activity, (minutes, reason, resolved)
        if resolved.activity == facts.activity:
            assert reason, f"{minutes} 分钟时「换活动」会被否，跳过判定却说要问"


def test_plugin_config_feeds_the_meal_dwell_override():
    """接线：``[physio] meal_duration_minutes`` 真的进了强制层的策略。"""

    _m, plugin, _host = _make_plugin(physio={"meal_duration_minutes": 25})
    policy = plugin._interrupt_policy()
    assert policy.dwell_overrides.get(A.MEAL) == 25

    now = _at(12, 30)
    plugin._state.activity = A.MEAL
    plugin._state.activity_since = now - 30 * 60
    plugin._state.energy = 6.0
    plugin._state.awake_minutes_today = 600
    plugin._enforce_and_apply(now, A.ActivityDecision(A.GAME, "开了几把"))
    assert plugin._state.activity == A.GAME, plugin._state.activity_note

    _m2, plugin2, _host2 = _make_plugin(physio={"meal_duration_minutes": 60})
    plugin2._state.activity = A.MEAL
    plugin2._state.activity_since = now - 30 * 60
    plugin2._state.energy = 6.0
    plugin2._state.awake_minutes_today = 600
    plugin2._enforce_and_apply(now, A.ActivityDecision(A.GAME, "开了几把"))
    assert plugin2._state.activity == A.MEAL, "配成 60 = 回到旧行为"


# ================================================================ 饱腹进提示词（PR-PRM-2）


def test_prompt_carries_satiety_and_the_last_meal():
    prompt = A.build_prompt(
        A.PromptInput(satiety=6.2, meal_count_today=2, last_meal_hours_ago=1.5)
    )
    line = next(text for text in prompt.splitlines() if text.startswith("饱腹："))
    assert "6.2/10" in line and "今日已吃 2 顿" in line and "上一餐约 1.5 小时前" in line

    hungry = A.build_prompt(A.PromptInput(satiety=3.0, meal_count_today=0))
    assert "今日还没吃过东西" in hungry
    assert "上一餐" not in hungry


def test_prompt_hides_satiety_when_physio_is_off():
    """没开生理锚点时不说饱腹——没有机制支撑的话不说（与休息日多睡同一条纪律）。"""

    plain = A.build_prompt(A.PromptInput())
    assert "饱腹" not in plain
    # NaN 也不能漏出「饱腹 nan/10」
    assert "饱腹" not in A.build_prompt(A.PromptInput(satiety=float("nan")))


def test_plugin_feeds_the_real_satiety_into_the_prompt():
    async def run():
        _m, plugin, _host = _make_plugin()
        now = _at(15, 0)
        plugin._state.last_tick_at = now
        plugin._state.satiety = 6.2
        plugin._state.meal_count_today = 2
        plugin._state.last_meal_at = now - 90 * 60
        captured: dict[str, str] = {}

        async def fake_generate(**kwargs):
            captured["prompt"] = str(kwargs.get("prompt") or "")
            return {"success": True, "response": '{"activity": "daily", "scene": "在发呆"}'}

        plugin.ctx.llm.generate = fake_generate
        decision = await plugin._ask_activity(now)

        assert decision is not None
        prompt = captured.get("prompt", "")
        assert "饱腹：6.2/10" in prompt
        assert "今日已吃 2 顿" in prompt
        assert "上一餐约 1.5 小时前" in prompt

    asyncio.run(run())


def test_plugin_hides_satiety_when_physio_is_disabled():
    async def run():
        _m, plugin, _host = _make_plugin(physio={"enabled": False})
        now = _at(15, 0)
        plugin._state.last_tick_at = now
        plugin._state.satiety = 2.0
        captured: dict[str, str] = {}

        async def fake_generate(**kwargs):
            captured["prompt"] = str(kwargs.get("prompt") or "")
            return {"success": True, "response": '{"activity": "daily", "scene": "在发呆"}'}

        plugin.ctx.llm.generate = fake_generate
        assert await plugin._ask_activity(now) is not None
        assert "饱腹" not in captured.get("prompt", "")

    asyncio.run(run())
