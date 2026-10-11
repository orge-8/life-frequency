# -*- coding: utf-8 -*-
"""L3：睡眠改进方案（v1.15.0）——PR-S1~S4 / W1~W3 / R1~R4 的行为与回归。

按方案「睡眠改进方案」的十一个 PR 分节。每个 PR 都钉住**正反两面**：
默认值下的新行为 + 关掉开关后回到旧行为（升级可回退是仓库纪律）。

另外有三条**跨 PR 的机制回归**（都是实现过程中真踩到的坑）：

* ``test_full_energy_never_goes_back_to_sleep``（PR-S1 × PR-R3）：
  体力满 → 强制唤醒 → 赖床宽限过后**不许**再被送回床。缺这条会炸出
  「新一觉的最短睡眠目标从零重算 + 体力已满永远不会因体力满而醒 ⇒
  一觉睡到 12 小时上限」的坑；
* ``test_night_waking_survives_the_next_tick_enforce``（PR-R2 × 强制层顺序）：
  tick 的顺序是 ``settle`` → ``enforce(None)``，夜醒切到 daze 后如果
  强制层不认宽限窗，同一个 tick 里就被塞回床（等于什么都没发生）；
* ``test_sleep_interval_keeps_deterministic_wakeup``（PR-S4 × PR-S1）：
  睡眠中不问模型时，醒来必须仍然由「体力满 + 最短睡眠目标」保证。
"""

import asyncio
import pathlib
import random
import sys
import time
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_activity as A  # noqa: E402
import life_factors as F  # noqa: E402
import life_interrupt as X  # noqa: E402
import life_sim as S  # noqa: E402
import life_proactive as P  # noqa: E402

TZ = 480
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() - TZ * 60


def cfg(**overrides):
    base = dict(tz_offset_minutes=TZ)
    base.update(overrides)
    return S.SimConfig(**base)


def policy(**overrides):
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


def facts(**overrides):
    base = dict(
        activity=A.DAILY,
        minutes_in_activity=120,
        now_minutes=14 * 60,
        emotion=5.0,
        energy=6.0,
        energy_cap=10.0,
        sick=False,
        sleep_minutes_today=0,
        awake_minutes_today=10 * 60,
    )
    base.update(overrides)
    return A.ActivityFacts(**base)


def request(activity, scene="", source=A.SOURCE_LLM):
    return A.ActivityDecision(activity=activity, scene=scene, source=source, note="test")


def make_state(*, at, activity=A.DAILY, **overrides):
    state = S.LifeState()
    state.last_tick_at = at
    state.activity = activity
    state.activity_since = at
    state.day_key = S.day_key_of(S.local_datetime(at, TZ), 12)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


# ================================================================ PR-S1
# 最短睡眠目标 + 熬夜恢复滞回


def test_energy_full_wake_waits_for_the_min_sleep_target():
    """体力满但没睡够目标 ⇒ 继续睡；睡够目标 ⇒ 醒。"""

    short = A.enforce(
        facts(activity=A.SLEEP, minutes_in_activity=200, minutes_in_sleep=200,
              sleep_minutes_today=200, energy=10.0, now_minutes=4 * 60),
        request(A.SLEEP),
        policy(),
    )
    assert short.activity == A.SLEEP, "200 分钟 < 6.5 小时目标：不该醒"
    assert short.source == A.SOURCE_LLM or "保持" in short.note

    enough = A.enforce(
        facts(activity=A.SLEEP, minutes_in_activity=420, minutes_in_sleep=420,
              sleep_minutes_today=420, energy=10.0, now_minutes=4 * 60),
        None,
        policy(),
    )
    assert enough.activity != A.SLEEP
    assert "体力已满" in enough.note


def test_energy_full_wake_min_hours_zero_restores_v160():
    """``energy_full_wake_min_hours=0`` = v1.6.0 旧行为：体力一回满立刻醒。"""

    out = A.enforce(
        facts(activity=A.SLEEP, minutes_in_activity=10, minutes_in_sleep=10,
              sleep_minutes_today=10, energy=10.0, now_minutes=4 * 60),
        None,
        policy(energy_full_wake_min_hours=0),
    )
    assert out.activity != A.SLEEP
    assert "体力已满" in out.note


def test_energy_full_wake_unknown_anchor_falls_back_to_old_behavior():
    """睡眠锚点不可信（minutes_in_sleep=0）时按体力满直接醒：宁可早醒，不可睡到上限。"""

    out = A.enforce(
        facts(activity=A.SLEEP, minutes_in_activity=10, minutes_in_sleep=0,
              sleep_minutes_today=10, energy=10.0, now_minutes=4 * 60),
        None,
        policy(),
    )
    assert out.activity != A.SLEEP


def test_sleep_debt_recovery_is_hysteretic():
    """滞回：一晚好觉只还 1 晚债；step=0 回到「一晚清零」。"""

    at = ts(2026, 2, 8, 4, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, sleep_debt_nights=3)
    S.settle(
        state, now=at + 8 * 3600, config=cfg(offline_gap_minutes=0), events=[],
        rng=random.Random(1),
    )
    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
        now=at + 8 * 3600,
        config=cfg(offline_gap_minutes=0),
    )
    assert state.sleep_debt_nights == 2, "3 → 2（不是清零）"
    assert state.energy_cap == pytest.approx(10.0), "降到 3 晚以下，体力上限立刻恢复"

    legacy = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, sleep_debt_nights=3)
    S.settle(
        legacy, now=at + 8 * 3600,
        config=cfg(offline_gap_minutes=0, sleep_debt_recovery_step=0), events=[],
        rng=random.Random(1),
    )
    S.apply_activity(
        legacy,
        A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
        now=at + 8 * 3600,
        config=cfg(offline_gap_minutes=0, sleep_debt_recovery_step=0),
    )
    assert legacy.sleep_debt_nights == 0


def test_energy_full_never_reenters_sleep():
    """**跨 PR 回归**：体力满的人在睡眠窗口内提议睡觉也必须被拒。

    缺这条会炸出「醒来 → 被硬约束送回床 → 新一觉的目标从零重算（体力已是满的，
    永远不满足『体力满』这条唤醒路径）⇒ 一觉睡到 12 小时上限」。
    """

    out = A.enforce(
        facts(activity=A.DAILY, now_minutes=4 * 60, energy=10.0, energy_cap=10.0,
              awake_minutes_today=10 * 60),
        request(A.SLEEP),
        policy(),
    )
    assert out.activity == A.DAILY, "满体力不需要再睡一觉"
    assert "先不睡" in out.note

    # 硬约束路径（无提议）同样不许送她回去
    retained = A.enforce(
        facts(activity=A.DAILY, now_minutes=4 * 60, energy=10.0, energy_cap=10.0,
              awake_minutes_today=10 * 60),
        None,
        policy(),
    )
    assert retained.activity == A.DAILY
    assert retained.source == A.SOURCE_RETAINED


def test_sleep_target_tracks_rest_day():
    """PR-R4 与 PR-S1 共用同一份目标：休息日多睡。"""

    rest_short = A.enforce(
        facts(activity=A.SLEEP, minutes_in_activity=420, minutes_in_sleep=420,
              sleep_minutes_today=420, energy=10.0, rest_day=True, now_minutes=4 * 60),
        None,
        policy(),
    )
    assert rest_short.activity == A.SLEEP, "休息日目标是 6.5+1=7.5 小时，420 分钟还不够"

    rest_enough = A.enforce(
        facts(activity=A.SLEEP, minutes_in_activity=460, minutes_in_sleep=460,
              sleep_minutes_today=460, energy=10.0, rest_day=True, now_minutes=4 * 60),
        None,
        policy(),
    )
    assert rest_enough.activity != A.SLEEP

    # 顺延关掉 ⇒ 与工作日一致
    off = A.enforce(
        facts(activity=A.SLEEP, minutes_in_activity=420, minutes_in_sleep=420,
              sleep_minutes_today=420, energy=10.0, rest_day=True, now_minutes=4 * 60),
        None,
        policy(rest_day_sleep_extension_minutes=0),
    )
    assert off.activity != A.SLEEP


def test_min_sleep_target_is_clamped_to_max_sleep():
    """目标 > 睡眠上限时按上限夹取并告警（提示词与强制层不许印出矛盾的两个数）。"""

    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")
    module, plugin, _host = _make_plugin(
        simulation={"energy_full_wake_min_hours": 20.0, "max_sleep_hours": 12.0}
    )
    assert plugin._sim_config().energy_full_wake_min_hours == pytest.approx(12.0)


def test_rested_suppresses_only_window_driven_sleep():
    """**跨 PR 回归**（PR-S1 配套）：本轮窗口睡够后，不许在同一个窗口里再睡一觉。

    反例（探针实拍）：09:30 睡满目标醒来 → 09:40 因为「窗口内 + 体力 9.98 < 10」
    又被硬约束送回床 → 新一觉目标从零重算 ⇒ 一觉睡到 16:10（当天睡了两觉）。
    """

    blocked = A.enforce(
        facts(activity=A.DAZE, now_minutes=9 * 60 + 40, energy=9.98,
              rested=True, awake_minutes_today=20 * 60),
        request(A.SLEEP),
        policy(),
    )
    assert blocked.activity == A.DAZE, "窗口内已经睡过了，不再睡"

    # 但「真累了」不受压制（body 需要就是需要）
    tired = A.enforce(
        facts(activity=A.DAZE, now_minutes=9 * 60 + 40, energy=2.0,
              rested=True, awake_minutes_today=20 * 60),
        request(A.SLEEP),
        policy(),
    )
    assert tired.activity == A.SLEEP

    # 生病同理（健康优先）
    sick = A.enforce(
        facts(activity=A.DAZE, now_minutes=9 * 60 + 40, energy=9.9, sick=True,
              cold_stage=A.COLD_ONSET, rested=True, awake_minutes_today=20 * 60),
        request(A.SLEEP),
        policy(),
    )
    assert sick.activity == A.SLEEP


def test_rested_until_is_set_on_a_target_satisfying_wake():
    at = ts(2026, 3, 2, 3, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, energy=3.5)
    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.DAZE, source=A.SOURCE_ENFORCED),
        now=at + 390 * 60,          # 睡满 6.5 小时（正好是目标）
        config=cfg(),
    )
    # 窗口 03:00-11:00，09:30 醒来 ⇒ rested_until = 今天 11:00
    assert state.rested_until == pytest.approx(ts(2026, 3, 2, 11, 0))


def test_rested_not_set_when_target_is_off_or_short():
    at = ts(2026, 3, 2, 3, 0)
    # 目标 = 0（旧行为）⇒ 不记「睡够」，行为与 v1.14 一致
    legacy = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, energy=3.5)
    S.apply_activity(
        legacy,
        A.ActivityDecision(activity=A.DAZE, source=A.SOURCE_ENFORCED),
        now=at + 390 * 60,
        config=cfg(energy_full_wake_min_hours=0.0),
    )
    assert legacy.rested_until == 0.0

    # 没睡够目标 ⇒ 不记（她还会接着睡）
    short = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, energy=3.5)
    S.apply_activity(
        short,
        A.ActivityDecision(activity=A.DAZE, source=A.SOURCE_ENFORCED),
        now=at + 120 * 60,
        config=cfg(),
    )
    assert short.rested_until == 0.0


def test_multi_day_simulation_sleeps_once_per_window():
    """多日动力学回归：每个睡眠时段恰好一觉、时长 ≈ 目标、熬夜不累积。

    这条是「越熬越短」（方案 P0-1）的端到端钉子：旧行为下同一时段会睡出
    十几段 10 分钟的假觉（探针实测 380 段/12 天）。
    """

    config = cfg(offline_gap_minutes=0, energy_full_wake_min_hours=6.5)
    start = ts(2026, 3, 1, 12, 0)
    state = S.new_state(now=start, config=config, energy=5.0)
    now = start
    rng = random.Random(7)
    prev = state.activity
    prev_start = start
    episodes: list[float] = []
    for _ in range(5 * 24 * 6):
        now += 600
        state = S.settle(state, now=now, config=config, events=(), rng=rng)
        state = S.enforce_and_apply(state, now=now, config=config, decision=None)
        if state.activity != prev:
            if prev == A.SLEEP:
                episodes.append((now - prev_start) / 3600.0)
            if state.activity == A.SLEEP:
                prev_start = now
            prev = state.activity
    assert len(episodes) == 5, f"5 天应恰好 5 觉（每个睡眠时段一觉），实测 {len(episodes)}"
    assert all(6.4 <= hours <= 6.7 for hours in episodes), episodes
    assert state.sleep_debt_nights == 0, "每晚睡满目标就不该累积熬夜"


def test_multi_day_simulation_legacy_target_still_works():
    """目标关掉（旧行为）时也不会把一天睡成 12 小时上限（体力满即醒仍在）。"""

    config = cfg(offline_gap_minutes=0, energy_full_wake_min_hours=0.0)
    start = ts(2026, 3, 1, 12, 0)
    state = S.new_state(now=start, config=config, energy=5.0)
    now = start
    rng = random.Random(7)
    prev = state.activity
    prev_start = start
    episodes: list[float] = []
    for _ in range(3 * 24 * 6):
        now += 600
        state = S.settle(state, now=now, config=config, events=(), rng=rng)
        state = S.enforce_and_apply(state, now=now, config=config, decision=None)
        if state.activity != prev:
            if prev == A.SLEEP:
                episodes.append((now - prev_start) / 3600.0)
            if state.activity == A.SLEEP:
                prev_start = now
            prev = state.activity
    assert episodes, "旧行为下也该有睡眠"
    assert max(episodes) < 12.0, "任何一觉都不该睡到 12 小时上限"


# ================================================================ PR-S2
# 睡眠优先：习惯表 / 生理窗不再把她叫醒

def test_routine_proposal_cannot_wake_her_by_default():
    facts_sleeping = facts(
        activity=A.SLEEP, minutes_in_activity=240, minutes_in_sleep=240,
        sleep_minutes_today=240, now_minutes=4 * 60,
    )
    out = A.enforce(facts_sleeping, request(A.DAILY, source=A.SOURCE_ROUTINE), policy())
    assert out.activity == A.SLEEP
    assert out.source == A.SOURCE_ENFORCED
    assert "睡眠优先" in out.note


def test_physio_proposal_cannot_wake_her_by_default():
    facts_sleeping = facts(
        activity=A.SLEEP, minutes_in_activity=240, minutes_in_sleep=240,
        sleep_minutes_today=240, now_minutes=4 * 60,
    )
    out = A.enforce(facts_sleeping, request(A.MEAL, source=A.SOURCE_PHYSIO), policy())
    assert out.activity == A.SLEEP
    assert "睡眠优先" in out.note


def test_wake_switches_restore_v114_behavior():
    """两个开关打开 ⇒ 恢复「睡满最短时长后习惯/生理窗可以叫醒她」。"""

    facts_sleeping = facts(
        activity=A.SLEEP, minutes_in_activity=240, minutes_in_sleep=240,
        sleep_minutes_today=240, now_minutes=4 * 60,
    )
    routine = A.enforce(
        facts_sleeping, request(A.DAILY, source=A.SOURCE_ROUTINE),
        policy(routine_can_wake=True),
    )
    assert routine.activity == A.DAILY
    physio = A.enforce(
        facts_sleeping, request(A.MEAL, source=A.SOURCE_PHYSIO),
        policy(physio_can_wake=True),
    )
    assert physio.activity == A.MEAL


def test_llm_proposal_still_wakes_her_after_min_sleep():
    """模型提议照旧能叫醒（「她自己想醒」是合法的），只有确定性来源被闸住。"""

    out = A.enforce(
        facts(activity=A.SLEEP, minutes_in_activity=240, minutes_in_sleep=240,
              sleep_minutes_today=240, now_minutes=4 * 60),
        request(A.DAILY),
        policy(),
    )
    assert out.activity == A.DAILY


def test_routine_source_still_asks_the_model_in_sleep():
    """睡眠后段问模型仍有意义（模型提议可以叫醒她）⇒ 不许被判成白问。"""

    reason = A.request_is_pointless(
        facts(activity=A.SLEEP, minutes_in_activity=240, minutes_in_sleep=240,
              sleep_minutes_today=240, now_minutes=4 * 60),
        policy(),
    )
    assert reason == ""


# ================================================================ PR-S3
# 体力归零强制入睡


def test_energy_floor_forces_sleep_despite_a_proposal():
    out = A.enforce(
        facts(activity=A.GAME, minutes_in_activity=120, now_minutes=15 * 60, energy=0.2),
        request(A.NIGHT_STUDY),
        policy(),
    )
    assert out.activity == A.SLEEP
    assert out.source == A.SOURCE_ENFORCED
    assert "体力耗尽" in out.note


def test_energy_floor_can_be_disabled():
    out = A.enforce(
        facts(activity=A.GAME, minutes_in_activity=120, now_minutes=15 * 60, energy=0.2),
        request(A.NIGHT_STUDY),
        policy(sleep_hard_floor=0.0),
    )
    assert out.activity == A.NIGHT_STUDY, "关掉兜底就回到「模型坚持不睡就随她」"


def test_energy_floor_does_not_break_the_interrupt_window():
    """打断窗口内不抢（她正在回消息）：保住 CHATTING 优先。"""

    out = A.enforce(
        facts(activity=A.CHATTING, minutes_in_activity=5, now_minutes=15 * 60,
              energy=0.2, in_interrupt=True),
        request(A.CHATTING),
        policy(),
    )
    assert out.activity == A.CHATTING


def test_energy_floor_is_pointless_and_equivalent():
    f = facts(activity=A.GAME, minutes_in_activity=120, now_minutes=15 * 60, energy=0.2)
    p = policy()
    assert A.request_is_pointless(f, p)
    baseline = A.enforce(f, None, p)
    for activity in A.ALLOWED_ACTIVITIES:
        assert A.enforce(f, request(activity), p).activity == baseline.activity


def test_energy_floor_does_not_override_sick_rest():
    """生病分支排在前面：加重期她该躺着，不该被「体力耗尽」抢成 sleep。"""

    out = A.enforce(
        facts(activity=A.DAILY, sick=True, cold_stage=A.COLD_WORSENING,
              minutes_in_activity=120, now_minutes=15 * 60, energy=0.2),
        request(A.MUSIC),
        policy(),
    )
    assert out.activity == A.SICK_REST


# ================================================================ PR-S4
# 睡眠期模型节流


def test_sleep_interval_governs_the_call_gate():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")
    _module, plugin, _host = _make_plugin(
        activity={"llm": {"min_interval_seconds": 600, "sleep_interval_seconds": 1800}}
    )
    now = 1_700_000_000.0
    plugin._state.activity = A.SLEEP
    plugin._last_llm_attempt_at = now - 700
    assert plugin._llm_ready(now) is False, "睡眠中 700 秒还不够 1800 秒"
    plugin._last_llm_attempt_at = now - 2000
    assert plugin._llm_ready(now) is True


def test_sleep_interval_zero_means_never_ask():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")
    _module, plugin, _host = _make_plugin(
        activity={"llm": {"min_interval_seconds": 600, "sleep_interval_seconds": 0}}
    )
    now = 1_700_000_000.0
    plugin._state.activity = A.SLEEP
    plugin._last_llm_attempt_at = now - 10_000
    assert plugin._llm_ready(now) is False

    # 醒着时仍按 min_interval 走（别把清醒决策一起关掉）
    plugin._state.activity = A.DAILY
    assert plugin._llm_ready(now) is True


def test_sleep_interval_keeps_deterministic_wakeup():
    """**跨 PR 回归**：睡眠中不问模型时，醒来仍由确定性条件保证。"""

    at = ts(2026, 1, 1, 3, 0)
    config = cfg(energy_full_wake_min_hours=6.5)
    state = S.LifeState()
    state.activity = A.SLEEP
    state.sleep_started_at = at
    state.activity_since = at
    state.last_tick_at = at
    state.day_key = S.day_key_of(S.local_datetime(at, TZ), 12)

    woke = None
    now = at
    for _ in range(24 * 6 + 2):
        now += 600
        state = S.settle(state, now=now, config=config, events=(), rng=random.Random(0))
        # 模拟「睡眠中完全不问模型」：只跑硬约束，decision 恒为 None
        state = S.enforce_and_apply(state, now=now, config=config, decision=None)
        if state.activity != A.SLEEP:
            woke = now
            break
    assert woke is not None, "没有任何模型参与时她也必须按时醒"
    slept = (woke - at) / 3600.0
    assert slept == pytest.approx(6.5, abs=0.25), f"应睡满目标 6.5 小时，实际 {slept:.2f}"


# ================================================================ PR-W1/W2/W3
# 唤醒链路（插件级）


def _make_plugin(**overrides):
    """与 test_dream.py 同款脚手架（时区置 0，避免跨日干扰）。"""

    from fakehost import (  # noqa: PLC0415
        FakeHost,
        FakePaths,
        bind_context,
        build_context,
        get_default_config,
        load_plugin_module,
    )

    module = load_plugin_module(PLUGIN_DIR, "life_frequency_sleep_improvements")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = 0
    # 唤醒用例必须**与墙钟无关**：默认 quiet_hours = 23:30-08:00，而这里时区置 0
    # 直用 time.time()，在 UTC 夜里跑就整条唤醒链路被静默时段挡掉（4 条用例
    # 白天全绿、凌晨全红）。清空静默时段，让「被叫醒」只由唤醒配置决定。
    config["frequency"]["quiet_hours"] = []
    config["physio"]["meals"] = []
    config["routines"]["lines"] = []
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    plugin._rebuild_from_config()
    return module, plugin, host


def _sleepy(plugin, now: float) -> None:
    plugin._state.activity = A.SLEEP
    plugin._state.activity_since = now - 3600
    plugin._state.sleep_started_at = now - 3600
    plugin._state.sleep_minutes_today = 60
    plugin._state.energy = 5.0
    plugin._state.at_wake_until = 0.0


def test_private_message_does_not_wake_her_by_default():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, host = _make_plugin()
        now = time.time()
        _sleepy(plugin, now)
        await plugin.note_session(message={"session_id": "private-1"})
        assert plugin._state.at_wake_until == 0.0
        assert not host.calls_of("frequency.set_adjust")

    asyncio.run(run())


def test_private_message_wakes_her_when_enabled():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, host = _make_plugin(simulation={"wake_on_private": True})
        now = time.time()
        _sleepy(plugin, now)
        await plugin.note_session(message={"session_id": "private-1"})
        assert plugin._state.at_wake_until > now, "私聊也该开清醒窗口"
        assert len(host.calls_of("frequency.set_adjust")) == 1

    asyncio.run(run())


def test_group_at_still_wakes_her():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, host = _make_plugin()
        now = time.time()
        _sleepy(plugin, now)
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        assert plugin._state.at_wake_until > now
        assert len(host.calls_of("frequency.set_adjust")) == 1

    asyncio.run(run())


def test_wake_window_extends_on_a_plain_reply_without_second_write():
    """窗口内对方继续说话 ⇒ 顺延，但**不再**写一次宿主（每次消息一次 RPC 是成本）。"""

    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, host = _make_plugin()
        now = time.time()
        _sleepy(plugin, now)
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        host.calls.clear()
        plugin._state.at_wake_until = time.time() + 60.0  # 窗口只剩 1 分钟
        await plugin.note_session(
            message={"session_id": "group-1", "processed_plain_text": "那你继续睡吧"}
        )
        assert plugin._state.at_wake_until > time.time() + 60.0, "对方还在说话就该顺延"
        assert not host.calls_of("frequency.set_adjust"), "顺延不重写宿主"

    asyncio.run(run())


def test_wake_extension_is_capped():
    """总时长上限：活跃群里每 5 分钟一句话不能把窗口无限顺延。"""

    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, _host = _make_plugin(
            simulation={"wake_minutes": 10, "wake_max_extensions_minutes": 20}
        )
        now = time.time()
        _sleepy(plugin, now)
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        cap = time.time() + 20 * 60.0
        assert plugin._state.at_wake_until <= cap + 2.0
        # 连续顺延只能顶到上限
        for _ in range(5):
            plugin._state.at_wake_until = time.time() + 10 * 60.0
            await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        assert plugin._state.at_wake_until <= cap + 2.0, "顺延必须封顶"

    asyncio.run(run())


def test_wake_extension_can_be_disabled():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, _host = _make_plugin(
            simulation={"wake_extend_on_message": False}
        )
        now = time.time()
        _sleepy(plugin, now)
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        plugin._state.at_wake_until = time.time() + 60.0
        frozen = plugin._state.at_wake_until
        await plugin.note_session(
            message={"session_id": "group-1", "processed_plain_text": "继续说"}
        )
        assert plugin._state.at_wake_until == pytest.approx(frozen)

    asyncio.run(run())


def test_grumpy_note_only_when_woken_shortly_after_falling_asleep():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, _host = _make_plugin()
        now = time.time()
        # 刚睡 30 分钟就被叫醒 → 带「刚睡下没多久」
        _sleepy(plugin, now)
        plugin._state.sleep_started_at = now - 30 * 60.0
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        assert "刚睡下没多久" in plugin._life_digest()

        # 睡饱了被叫醒 → 不带那句
        _sleepy(plugin, now)
        plugin._state.sleep_started_at = now - 6 * 3600.0
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        digest = plugin._life_digest()
        assert "刚刚被叫醒" in digest and "刚睡下没多久" not in digest

    asyncio.run(run())


def test_nap_is_not_woken_by_at():
    """小睡不享 wake_on_at：90 分钟自己会醒（被 @ 也不会被抬倍率）。"""

    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, host = _make_plugin(simulation={"wake_on_private": True})
        now = time.time()
        plugin._state.activity = A.NAP
        plugin._state.activity_since = now - 600
        plugin._state.sleep_started_at = now - 600
        plugin._state.at_wake_until = 0.0
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        assert plugin._state.at_wake_until == 0.0
        assert not host.calls_of("frequency.set_adjust")

    asyncio.run(run())


# ================================================================ PR-R1
# 小睡（nap）


def test_nap_is_a_hard_gate():
    """小睡 = 完全静默：走硬闸归零，且**不吃素材加成**。"""

    quiet = F.compute_adjust(
        activity=A.NAP, emotion=10.0, energy=10.0, sick=False, sleep_debt_nights=0,
        date_factor=1.0, material_count=0.0, now_minutes=14 * 60, config=F.FactorConfig(),
    )
    assert quiet.adjust == pytest.approx(0.0)
    assert quiet.reason == F.REASON_NAP
    assert F.reason_label(F.REASON_NAP) == "小睡中"

    loaded = F.compute_adjust(
        activity=A.NAP, emotion=10.0, energy=10.0, sick=False, sleep_debt_nights=0,
        date_factor=1.0, material_count=99.0, now_minutes=14 * 60, config=F.FactorConfig(),
    )
    assert loaded.adjust == pytest.approx(0.0), "攒了素材也不该把小睡抬起来"


def test_nap_entry_needs_daytime_and_tiredness():
    ok = A.enforce(
        facts(activity=A.DAILY, minutes_in_activity=120, now_minutes=14 * 60, energy=3.0),
        request(A.NAP),
        policy(),
    )
    assert ok.activity == A.NAP

    not_tired = A.enforce(
        facts(activity=A.DAILY, minutes_in_activity=120, now_minutes=14 * 60, energy=8.0),
        request(A.NAP),
        policy(),
    )
    assert not_tired.activity == A.DAILY

    in_sleep_window = A.enforce(
        facts(activity=A.DAILY, minutes_in_activity=120, now_minutes=4 * 60, energy=3.0),
        request(A.NAP),
        policy(),
    )
    assert in_sleep_window.activity == A.DAILY, "睡眠时段内该睡整觉，不是「眯一会儿」"


def test_nap_respects_its_own_limits():
    kept = A.enforce(
        facts(activity=A.NAP, minutes_in_activity=5, minutes_in_sleep=5,
              now_minutes=14 * 60, energy=6.0),
        request(A.DAILY),
        policy(),
    )
    assert kept.activity == A.NAP, "未满最短时长：继续眯着"

    woken = A.enforce(
        facts(activity=A.NAP, minutes_in_activity=95, minutes_in_sleep=95,
              now_minutes=14 * 60, energy=6.0),
        request(A.NAP),
        policy(),
    )
    assert woken.activity != A.NAP, "到上限必须叫醒"
    assert "达上限" in woken.note


def test_nap_can_be_disabled():
    out = A.enforce(
        facts(activity=A.DAILY, minutes_in_activity=120, now_minutes=14 * 60, energy=3.0),
        request(A.NAP),
        policy(nap_enabled=False),
    )
    assert out.activity == A.DAILY, "关掉后 nap 不是有效提议"
    assert A.NAP not in A.PromptInput(allow_nap=False).activity_choices()
    assert A.NAP in A.PromptInput().activity_choices()


def test_nap_counts_as_sleep_for_accounting_and_debt():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, activity=A.NAP, sleep_started_at=at, energy=5.0)
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(1))
    assert state.sleep_minutes_today == 10, "小睡计入「已睡」"
    assert state.awake_minutes_today == 0

    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
        now=at + 600,
        config=cfg(),
    )
    assert state.sleep_started_at == 0.0
    assert state.sleep_ledger, "小睡也要进滚动账本（24 小时窗口本就该含小睡）"


def test_nap_flag_and_awake_predicates():
    assert A.is_asleep(A.NAP) is True
    assert A.is_awake(A.NAP) is False
    assert A.is_asleep(A.SLEEP) is True
    assert A.is_awake(A.DAILY) is True
    assert A.NAP not in A.SIDE_ACTIVITIES, "小睡是独占型，不能当背景活动"
    assert A.NAP in A.ALLOWED_ACTIVITIES and A.ACTIVITY_LABELS[A.NAP] == "小睡"


def test_nap_aliases_and_normalization():
    for alias in ("小睡", "午睡", "小憩", "打盹", "眯一会儿", "nap", "doze"):
        assert A.normalize_activity(alias) == A.NAP, alias


def test_nap_is_interruptible_but_sleep_is_not():
    assert X.should_interrupt(
        enabled=True, activity=A.NAP, is_command=False, is_bot_message=False,
        private_chat=True, mentioned=False,
    ), "小睡浅、回完消息还能倒回去接着眯"
    assert not X.should_interrupt(
        enabled=True, activity=A.SLEEP, is_command=False, is_bot_message=False,
        private_chat=True, mentioned=False,
    )


def test_nap_is_blocked_in_the_work_phase_but_allowed_when_exhausted():
    """在岗趴着睡 = 摸鱼，被相位矩阵挡住；但累到阈值以下/生病时放行（健康优先）。"""

    work = A.ScheduleFacts(enabled=True, is_workday=True, phase=A.SCHEDULE_WORK)
    healthy = facts(activity=A.DAILY, now_minutes=10 * 60 + 30, energy=6.0, schedule=work)
    assert "不该" in A.activity_blocked_by_schedule(A.NAP, healthy, policy())

    exhausted = facts(activity=A.DAILY, now_minutes=10 * 60 + 30, energy=2.0, schedule=work)
    assert A.activity_blocked_by_schedule(A.NAP, exhausted, policy()) == ""

    lunch = A.ScheduleFacts(enabled=True, is_workday=True, phase=A.SCHEDULE_LUNCH)
    during_lunch = facts(activity=A.DAILY, now_minutes=12 * 60 + 30, energy=3.0, schedule=lunch)
    assert A.activity_blocked_by_schedule(A.NAP, during_lunch, policy()) == "", "午休可以眯一会儿"


def test_nap_is_a_light_activity_while_recovering():
    out = A.enforce(
        facts(activity=A.DAILY, sick=True, cold_stage=A.COLD_RECOVERING,
              minutes_in_activity=120, now_minutes=14 * 60, energy=3.0),
        request(A.NAP),
        policy(),
    )
    assert out.activity == A.NAP


def test_nap_proposes_in_prompt_but_never_as_side():
    prompt = A.build_prompt(A.PromptInput(activity=A.DAILY, allow_nap=True))
    assert "小睡" in prompt
    assert A.normalize_side([A.NAP], main=A.DAILY) == ()


def test_nap_does_not_dream():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, _host = _make_plugin()
        now = time.time()
        plugin._state.activity = A.NAP
        plugin._state.activity_since = now - 95 * 60
        plugin._state.sleep_started_at = now - 95 * 60
        plugin._enforce_and_apply(now, None)
        assert plugin._state.activity != A.NAP, "到上限该醒"
        assert plugin._dream_wake_pending is None, "小睡（<3h）不做梦"

    asyncio.run(run())


def test_nap_settle_uses_sleep_rate():
    """体力恢复比整觉弱：+0.60/小时（而不是 +1.20）。"""

    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, activity=A.NAP, sleep_started_at=at, energy=4.0)
    S.settle(
        state, now=at + 3600, config=cfg(offline_gap_minutes=0), events=[],
        rng=random.Random(1),
    )
    assert state.energy == pytest.approx(4.6, rel=1e-6)


def test_nap_is_never_proactive():
    """小睡时也不主动开口（与睡觉同一条硬闸）。"""

    from life_proactive import ProactiveConfig, REASON_SLEEPING, decide  # noqa: PLC0415

    out = decide(
        config=ProactiveConfig(enabled=True),
        now=1_000.0,
        now_minutes=14 * 60,
        activity=A.NAP,
        energy=9.0,
        emotion=5.0,
        materials=[{"text": "想说说", "weight": 1.0, "created_at": 0.0,
                    "expires_at": 1e12, "best_until": 1e12}],
        session={},
        day_key="2026-02-08",
    )
    assert out.should_send is False
    assert out.reason == REASON_SLEEPING


# ================================================================ PR-R2
# 失眠与夜间易醒


def test_insomnia_turns_sleep_into_before_sleep():
    out = A.enforce(
        facts(activity=A.DAILY, now_minutes=4 * 60, energy=5.0,
              awake_minutes_today=10 * 60, insomnia_roll=True),
        request(A.SLEEP),
        policy(),
    )
    assert out.activity == A.BEFORE_SLEEP
    assert out.source == A.SOURCE_ENFORCED
    assert "睡不着" in out.note


def test_insomnia_also_applies_to_the_deterministic_sleep_path():
    out = A.enforce(
        facts(activity=A.GAME, now_minutes=4 * 60, energy=5.0,
              awake_minutes_today=10 * 60, insomnia_roll=True),
        None,
        policy(),
    )
    assert out.activity == A.BEFORE_SLEEP


def test_insomnia_can_be_disabled_and_never_hits_exhausted():
    disabled = A.enforce(
        facts(activity=A.DAILY, now_minutes=4 * 60, energy=5.0,
              awake_minutes_today=10 * 60, insomnia_roll=True),
        request(A.SLEEP),
        policy(insomnia_enabled=False),
    )
    assert disabled.activity == A.SLEEP

    exhausted = A.enforce(
        facts(activity=A.DAILY, now_minutes=4 * 60, energy=0.2,
              awake_minutes_today=10 * 60, insomnia_roll=True),
        request(A.SLEEP),
        policy(),
    )
    assert exhausted.activity == A.SLEEP, "累到硬底线以下就沾床就着"


def test_insomnia_roll_is_once_per_day_and_rng_backed():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")
    _m, plugin, _host = _make_plugin(
        mood={"insomnia_enabled": True, "insomnia_stress_threshold": 7.0,
              "insomnia_probability": 1.0}
    )
    now = 1_700_000_000.0
    plugin._state.stress = 8.5
    plugin._state.day_key = "2026-02-08"
    assert plugin._insomnia_roll(now) is True
    assert plugin._insomnia_roll(now) is False, "每生活日至多一次"


def test_insomnia_roll_respects_the_threshold():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")
    _m, plugin, _host = _make_plugin(
        mood={"insomnia_enabled": True, "insomnia_stress_threshold": 7.0,
              "insomnia_probability": 1.0}
    )
    plugin._state.stress = 3.0
    plugin._state.day_key = "2026-02-08"
    assert plugin._insomnia_roll(1_700_000_000.0) is False


def test_night_waking_switches_to_daze_and_records_the_event():
    at = ts(2026, 2, 8, 5, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at - 4 * 3600,
                       energy=6.0)
    state.last_tick_at = at - 600
    config = cfg(night_waking_enabled=True, night_waking_probability=1.0,
                 wake_daze_minutes=15)
    S.settle(state, now=at, config=config, events=[], rng=random.Random(1))
    assert state.activity == A.DAZE
    assert state.wake_grace_until > at
    assert state.last_night_waking_at == pytest.approx(at)
    assert any(item.get("kind") == "night_waking" for item in state.recent_events)
    # 夜醒**不重置**入睡锚点、也**不**记半段睡眠：这一夜还没结束，等真正醒来再记
    # （否则目标从零重算 + 账本里两段重叠）。
    assert state.sleep_started_at == pytest.approx(at - 4 * 3600)
    assert state.sleep_ledger == []


def test_night_waking_is_once_per_night_and_off_by_default():
    at = ts(2026, 2, 8, 5, 0)
    off = make_state(at=at, activity=A.SLEEP, sleep_started_at=at - 4 * 3600, energy=6.0)
    off.last_tick_at = at - 600
    S.settle(off, now=at, config=cfg(), events=[], rng=random.Random(1))
    assert off.activity == A.SLEEP, "默认关：整觉就是整觉"

    config = cfg(night_waking_enabled=True, night_waking_probability=1.0,
                 wake_daze_minutes=15)
    first = make_state(at=at, activity=A.SLEEP, sleep_started_at=at - 4 * 3600, energy=6.0)
    first.last_tick_at = at - 600
    S.settle(first, now=at, config=config, events=[], rng=random.Random(1))
    assert first.activity == A.DAZE
    # 再睡回去，同一生活日不会再醒第二次
    first.activity = A.SLEEP
    first.sleep_started_at = at
    first.wake_grace_until = 0.0
    first.last_tick_at = at
    S.settle(first, now=at + 600, config=config, events=[], rng=random.Random(1))
    assert first.activity == A.SLEEP


def test_night_waking_skipped_when_energy_full_or_too_early():
    at = ts(2026, 2, 8, 5, 0)
    config = cfg(night_waking_enabled=True, night_waking_probability=1.0,
                 wake_daze_minutes=15)

    full = make_state(at=at, activity=A.SLEEP, sleep_started_at=at - 4 * 3600,
                      energy=10.0, energy_cap=10.0)
    full.last_tick_at = at - 600
    S.settle(full, now=at, config=config, events=[], rng=random.Random(1))
    assert full.activity == A.SLEEP, "体力已满：本来就该醒了，不需要「半夜醒」"

    early = make_state(at=at, activity=A.SLEEP, sleep_started_at=at - 60 * 60,
                       energy=6.0)
    early.last_tick_at = at - 600
    S.settle(early, now=at, config=config, events=[], rng=random.Random(1))
    assert early.activity == A.SLEEP, "没睡满最短时长不掷夜醒"


def test_night_waking_lets_her_fall_back_asleep_past_the_awake_floor():
    """**回归**：夜醒后不许被「每日清醒下限」扣成人质。

    探针实拍（未修时）：06:00 夜醒 → `awake_minutes_today` 只有十几分钟 →
    「要清醒满 8 小时才许入睡」一直拒绝送她回去 → 她 daze 到次日 03:00。
    """

    out = A.enforce(
        facts(activity=A.DAZE, now_minutes=6 * 60, energy=7.1,
              awake_minutes_today=10, night_break=True),
        request(A.SLEEP),
        policy(),
    )
    assert out.activity == A.SLEEP, "同一夜里可以睡回去"

    blocked = A.enforce(
        facts(activity=A.DAZE, now_minutes=6 * 60, energy=7.1, awake_minutes_today=10),
        request(A.SLEEP),
        policy(),
    )
    assert blocked.activity == A.DAZE, "普通情况照旧守清醒下限"


def test_night_waking_does_not_restart_the_sleep_target():
    """**跨 PR 回归**（PR-R2 × PR-S1）：夜醒是同一夜的中断，不重置入睡锚点。

    反例（未修时）：03:00 入睡、04:00 夜醒、再睡回去 ⇒ 锚点被重置 ⇒ 新一觉
    再睡满 6.5 小时，直到 10:45（这一夜变成 7.75 小时）。
    """

    config = cfg(
        offline_gap_minutes=0, night_waking_enabled=True,
        night_waking_probability=1.0, wake_daze_minutes=15,
    )
    start = ts(2026, 3, 2, 3, 0)
    state = make_state(at=start, activity=A.SLEEP, sleep_started_at=start, energy=3.5)
    state.last_tick_at = start
    now = start
    woke = None
    for _ in range(24 * 6):
        now += 600
        state = S.settle(state, now=now, config=config, events=(), rng=random.Random(3))
        state = S.enforce_and_apply(state, now=now, config=config, decision=None)
        if float(state.rested_until or 0.0) > now:
            woke = now
            break
    assert woke is not None, "这一夜总得结束"
    slept = (woke - start) / 3600.0
    assert 6.4 <= slept <= 6.8, f"夜醒不该让这一夜变长（实测 {slept:.2f} 小时）"


def test_night_waking_survives_the_next_tick_enforce():
    """**跨 PR 回归**：夜醒切到 daze 后，同一个 tick 的 enforce(None) 不许塞回床。"""

    at = ts(2026, 2, 8, 5, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at - 4 * 3600,
                       energy=6.0, awake_minutes_today=20 * 60)
    state.last_tick_at = at - 600
    config = cfg(night_waking_enabled=True, night_waking_probability=1.0,
                 wake_daze_minutes=15)
    state = S.settle(state, now=at, config=config, events=[], rng=random.Random(1))
    assert state.activity == A.DAZE
    state = S.enforce_and_apply(state, now=at, config=config, decision=None)
    assert state.activity == A.DAZE, "宽限窗内不该被硬约束送回床"


# ================================================================ PR-R3
# 醒后赖床


def test_long_sleep_wakes_into_daze():
    out = A.enforce(
        facts(activity=A.SLEEP, minutes_in_activity=400, minutes_in_sleep=400,
              sleep_minutes_today=400, energy=10.0, now_minutes=4 * 60),
        None,
        policy(),
    )
    assert out.activity == A.DAZE
    assert "赖" in out.note


def test_short_sleep_wakes_straight_to_daily():
    """这一觉很短但被**日累计上限**强行唤醒（强制唤醒路径）⇒ 不赖床。"""

    out = A.enforce(
        facts(activity=A.SLEEP, minutes_in_activity=100, minutes_in_sleep=100,
              sleep_minutes_today=13 * 60, energy=6.0, now_minutes=4 * 60),
        None,
        policy(),
    )
    assert out.activity == A.DAILY
    assert "强制唤醒" in out.note


def test_daze_wake_can_be_disabled():
    out = A.enforce(
        facts(activity=A.SLEEP, minutes_in_activity=400, minutes_in_sleep=400,
              sleep_minutes_today=400, energy=10.0, now_minutes=4 * 60),
        None,
        policy(wake_daze_minutes=0),
    )
    assert out.activity == A.DAILY


def test_sick_wake_still_lands_in_sick_rest():
    out = A.enforce(
        facts(activity=A.SLEEP, minutes_in_activity=400, minutes_in_sleep=400,
              sleep_minutes_today=400, energy=10.0, sick=True,
              cold_stage=A.COLD_WORSENING, now_minutes=4 * 60),
        None,
        policy(),
    )
    assert out.activity == A.SICK_REST


def test_wake_grace_exempts_dwell_and_blocks_going_back_to_sleep():
    # 豁免最短停留期：刚醒那会儿本来就在过渡
    switched = A.enforce(
        facts(activity=A.DAZE, minutes_in_activity=5, now_minutes=9 * 60,
              energy=10.0, wake_grace=True),
        request(A.MUSIC),
        policy(),
    )
    assert switched.activity == A.MUSIC

    # 宽限窗内不许立刻又睡
    back = A.enforce(
        facts(activity=A.DAZE, minutes_in_activity=5, now_minutes=9 * 60,
              energy=8.0, wake_grace=True, awake_minutes_today=10 * 60),
        request(A.SLEEP),
        policy(),
    )
    assert back.activity == A.DAZE

    # 宽限窗内没有提议 → 保持赖床（不触发硬约束送睡）
    held = A.enforce(
        facts(activity=A.DAZE, minutes_in_activity=5, now_minutes=9 * 60,
              energy=8.0, wake_grace=True, awake_minutes_today=10 * 60),
        None,
        policy(),
    )
    assert held.activity == A.DAZE
    assert held.source == A.SOURCE_RETAINED


def test_wake_grace_expires_into_normal_rules():
    """宽限窗一过就回到常规：睡眠窗口内 + 体力不满 ⇒ 该睡就睡。"""

    out = A.enforce(
        facts(activity=A.DAZE, minutes_in_activity=30, now_minutes=9 * 60,
              energy=8.0, wake_grace=False, awake_minutes_today=10 * 60),
        None,
        policy(),
    )
    assert out.activity == A.SLEEP


def test_plugin_sets_and_clears_the_wake_grace():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, _host = _make_plugin(simulation={"wake_daze_minutes": 15})
        now = 1_700_000_000.0
        plugin._state.activity = A.SLEEP
        plugin._state.activity_since = now - 400 * 60
        plugin._state.sleep_started_at = now - 400 * 60
        plugin._state.sleep_minutes_today = 400
        plugin._state.awake_minutes_today = 10 * 3600
        plugin._state.energy = 10.0
        plugin._state.energy_cap = 10.0
        plugin._enforce_and_apply(now, None)
        assert plugin._state.activity == A.DAZE
        assert plugin._state.wake_grace_until > now, "醒来要开宽限窗"

        # 宽限窗内再来一次收口：不许被塞回床
        plugin._enforce_and_apply(now + 60, None)
        assert plugin._state.activity == A.DAZE

        # 窗口过期后清零
        plugin._state.wake_grace_until = now - 1.0
        plugin._enforce_and_apply(now + 120, None)
        assert plugin._state.wake_grace_until == 0.0

    asyncio.run(run())


# ================================================================ PR-R4
# 休息日睡懒觉（配置 / 提示词 / 事实接线）


def test_rest_day_facts_reach_the_prompt_lines():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")
    # 2026-02-07 是周六；tz=0 的脚手架下周六 → 不上班 → 休息日
    _m, plugin, _host = _make_plugin(
        simulation={"tz_offset_minutes": 0, "rest_day_sleep_extension_minutes": 60}
    )
    saturday = datetime(2026, 2, 7, 12, 0, tzinfo=timezone.utc).timestamp()
    tuesday = datetime(2026, 2, 10, 12, 0, tzinfo=timezone.utc).timestamp()
    assert plugin._rest_day(saturday) is True
    assert plugin._rest_day(tuesday) is False
    lines = plugin._schedule_lines_now(saturday)
    assert any("休息日" in line for line in lines)
    assert not any("休息日" in line for line in plugin._schedule_lines_now(tuesday))


def test_rest_day_line_hidden_when_extension_or_target_is_off():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")
    saturday = datetime(2026, 2, 7, 12, 0, tzinfo=timezone.utc).timestamp()

    _m, plugin, _host = _make_plugin(
        simulation={"tz_offset_minutes": 0, "rest_day_sleep_extension_minutes": 0}
    )
    assert not any("休息日" in line for line in plugin._schedule_lines_now(saturday))

    _m2, plugin2, _host2 = _make_plugin(
        simulation={"tz_offset_minutes": 0, "energy_full_wake_min_hours": 0.0}
    )
    assert not any(
        "休息日" in line for line in plugin2._schedule_lines_now(saturday)
    ), "没有最短睡眠目标时「多睡会儿」是一句没有机制支撑的空话"


def test_effect_lines_track_the_new_knobs():
    lines = S.activity_effect_lines(
        cfg(
            energy_full_wake_min_hours=6.5,
            rest_day_sleep_extension_minutes=60,
        ),
        activity_factors={"sleep": 0.0, "nap": 0.0, "daily": 1.0, "music": 0.9},
    )
    text = "\n".join(lines)
    assert "6.5 小时" in text and "休息日多睡 1 小时" in text
    assert "小睡" in text
    assert "还清" in text or "清掉" in text, "熬夜滞回也要如实说明"


def test_effect_lines_without_target_keep_the_old_wording():
    lines = S.activity_effect_lines(cfg(energy_full_wake_min_hours=0.0))
    text = "\n".join(lines)
    assert "立刻醒" in text
    assert "休息日" not in text


# ================================================================ 配置与状态接线


def test_new_state_timestamps_are_sanitized():
    for bad in (float("nan"), float("inf"), -1.0, "abc", None, True):
        state = S.LifeState.from_dict({"wake_grace_until": bad, "last_night_waking_at": bad})
        assert state.wake_grace_until == 0.0, bad
        assert state.last_night_waking_at == 0.0, bad
    good = S.LifeState.from_dict({"wake_grace_until": 123.5, "last_night_waking_at": 456.0})
    assert good.wake_grace_until == pytest.approx(123.5)
    assert good.last_night_waking_at == pytest.approx(456.0)
    assert S.LifeState.from_dict({}).wake_grace_until == 0.0, "旧状态文件零迁移"


def test_config_defaults_and_mapping():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")
    _m, plugin, _host = _make_plugin()
    config = plugin.config
    assert config.simulation.energy_full_wake_min_hours == pytest.approx(6.5)
    assert config.simulation.rest_day_sleep_extension_minutes == 60
    assert config.simulation.routine_can_wake is False
    assert config.simulation.physio_can_wake is False
    assert config.simulation.wake_daze_minutes == 15
    assert config.simulation.nap_enabled is True
    assert config.simulation.wake_on_private is False
    assert config.simulation.wake_extend_on_message is True
    assert config.simulation.wake_max_extensions_minutes == 30
    assert config.activity.sleep_hard_floor == pytest.approx(0.5)
    assert config.activity.llm.sleep_interval_seconds == 1800
    assert config.health.sleep_debt_recovery_step == 1
    assert config.mood.insomnia_enabled is True
    assert config.mood.night_waking_enabled is False

    sim = plugin._sim_config()
    policy_now = S.build_enforce_policy(sim)
    assert sim.energy_full_wake_min_hours == pytest.approx(6.5)
    assert sim.sleep_debt_recovery_step == 1
    assert sim.nap_max_minutes == 90
    assert sim.insomnia_enabled is True
    assert sim.night_waking_enabled is False
    assert policy_now.sleep_hard_floor == pytest.approx(0.5)
    assert policy_now.routine_can_wake is False
    assert policy_now.physio_can_wake is False
    assert policy_now.wake_daze_minutes == 15
    assert policy_now.energy_full_wake_min_hours == pytest.approx(6.5)


def test_nap_limits_are_clamped():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")
    _m, plugin, _host = _make_plugin(
        simulation={"nap_min_minutes": 500, "nap_max_minutes": 30}
    )
    sim = plugin._sim_config()
    assert sim.nap_max_minutes == 30
    assert sim.nap_min_minutes == 30, "下限不得超过上限（否则小睡永远自相矛盾）"


def test_sleep_config_is_visible_in_webui_schema():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")
    _m, plugin, _host = _make_plugin()
    schema = plugin.get_webui_config_schema()
    sections = schema.get("sections") if isinstance(schema, dict) else None
    assert sections, f"schema 形状不对：{type(schema)}"
    sim_fields = sections["simulation"]["fields"]
    for name in (
        "energy_full_wake_min_hours", "rest_day_sleep_extension_minutes",
        "routine_can_wake", "physio_can_wake", "wake_daze_minutes",
        "nap_enabled", "nap_max_minutes", "nap_min_minutes", "nap_energy_threshold",
        "wake_on_private", "wake_extend_on_message", "wake_max_extensions_minutes",
        "wake_grumpy_note",
    ):
        assert name in sim_fields, f"simulation.{name} 没进 WebUI schema"
    assert "sleep_hard_floor" in sections["activity"]["fields"]
    assert "sleep_interval_seconds" in sections["activity.llm"]["fields"]
    assert "sleep_debt_recovery_step" in sections["health"]["fields"]
    for name in (
        "insomnia_enabled", "insomnia_stress_threshold", "insomnia_probability",
        "night_waking_enabled", "night_waking_probability",
    ):
        assert name in sections["mood"]["fields"], f"mood.{name} 没进 WebUI schema"
