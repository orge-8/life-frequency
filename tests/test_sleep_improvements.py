# -*- coding: utf-8 -*-
"""L3 验收：v1.24.0「身体驱动作息」——睡不睡只看困意。

分节对着改造清单的九大部分，每一节钉住**机制**而不是某次实拍：
根因（为什么这样写）放在用例说明里，断言只认可观测的行为。

几条贯穿全文件的约定：

* ``decision=None`` 表示「这一轮没有模型输出」——必须仍然睡得着、醒得来；
* ``facts(...)`` 是纯数据，``enforce`` 是纯函数：凡是能用它证明的，不拉起插件；
* 插件级用例统一走 ``_make_plugin()``（tz=0、静默时段与生理窗清空），
  只测「接线」本身；
* 多日模拟一律关掉感冒/事件随机（``quiet_cfg``），否则断言的就不是作息了。
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
import life_proactive as P  # noqa: E402
import life_sim as S  # noqa: E402

TZ = 480
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() - TZ * 60


def cfg(**overrides):
    base = dict(tz_offset_minutes=TZ)
    base.update(overrides)
    return S.SimConfig(**base)


def quiet_cfg(**overrides):
    """关掉一切随机性：多日模拟里只允许「困意 → 睡 → 醒」这条链路说话。"""

    base = dict(
        offline_gap_minutes=0,
        fire_probability=0.0,
        cold_base_risk=0.0,
        cold_sleep_debt_risk=0.0,
        night_waking_enabled=False,
        insomnia_enabled=False,
    )
    base.update(overrides)
    return cfg(**base)


def policy(**overrides):
    base = dict(
        max_sleep_hours=12.0,
        min_dwell_minutes=60,
        min_sleep_minutes=180,
        sleep_need_onset=0.62,
        sleep_need_wake=0.16,
        nap_need_threshold=0.42,
        sleep_circadian_strength=0.05,
        sleep_deferral_minutes=15,
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
        sleep_need=0.25,
    )
    base.update(overrides)
    return A.ActivityFacts(**base)


def clone(src, **overrides):
    data = {name: getattr(src, name) for name in A.ActivityFacts.__dataclass_fields__}
    data.update(overrides)
    return A.ActivityFacts(**data)


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


def ledger(state):
    return S._normalize_sleep_ledger(state.sleep_ledger)


def _facts_with_need(**overrides):
    """睡着的人：默认睡满 5 小时、困意偏高（除非用例自己改）。"""

    base = dict(
        activity=A.SLEEP,
        minutes_in_activity=300,
        minutes_in_sleep=300,
        now_minutes=4 * 60,
        sleep_need=0.70,
    )
    base.update(overrides)
    return facts(**base)


# ================================================================ 一、困意与体力分开
# 清单第二部分：独立困意量；昼夜节律只做软偏置；体力不决定睡不睡。


def test_full_energy_but_sleepy_goes_to_sleep():
    """体力满 ≠ 睡够了：她可以体力 10/10 却困得睁不开眼。"""

    out = A.enforce(
        facts(energy=10.0, energy_cap=10.0, sleep_need=0.80),
        request(A.SLEEP),
        policy(),
    )
    assert out.activity == A.SLEEP
    assert out.source == A.SOURCE_LLM


def test_full_energy_is_not_a_reason_to_wake():
    """睡着、体力已满、困意还在 ⇒ 继续睡（旧模型这里会「体力满强行叫醒」）。"""

    kept = A.enforce(_facts_with_need(energy=10.0, energy_cap=10.0, sleep_need=0.50),
                     request(A.DAILY), policy())
    assert kept.activity == A.SLEEP
    assert "还没睡够" in kept.note

    retained = A.enforce(_facts_with_need(energy=10.0, energy_cap=10.0, sleep_need=0.50),
                         None, policy())
    assert retained.activity == A.SLEEP


def test_low_energy_alone_does_not_force_sleep():
    """体力 1/10 但不困 ⇒ 不睡（低体力不是入睡判据，也不再是硬底线）。"""

    out = A.enforce(facts(energy=1.0, sleep_need=0.20), request(A.SLEEP), policy())
    assert out.activity == A.DAILY
    assert "先不睡" in out.note

    retained = A.enforce(facts(energy=1.0, sleep_need=0.20), None, policy())
    assert retained.activity == A.DAILY


def test_sleep_pressure_ignores_energy_entirely():
    p = policy()
    pressures = {
        round(A.sleep_pressure(facts(energy=energy, sleep_need=0.50), p), 6)
        for energy in (0.0, 3.0, 6.0, 10.0)
    }
    assert len(pressures) == 1, pressures


def test_circadian_only_biases_it_does_not_gate():
    """生物钟加 ±0.05 的软偏置：能让「差一点」的人睡着，也能让「刚过线」的人再撑会儿。"""

    p = policy()

    early = facts(now_minutes=3 * 60, sleep_need=0.58)
    assert A._circadian_bias(early, p) == pytest.approx(0.05)
    assert A._sleep_allowed(early, p) is True, "03:00 的偏置把 0.58 推过 0.62"

    afternoon = facts(now_minutes=15 * 60, sleep_need=0.64)
    assert A._circadian_bias(afternoon, p) == pytest.approx(-0.05)
    assert A._sleep_allowed(afternoon, p) is False, "15:00 的偏置把 0.64 拉回 0.59"

    neutral = policy(sleep_circadian_strength=0.0)
    assert A._circadian_bias(early, neutral) == pytest.approx(0.0)
    assert A._sleep_allowed(facts(now_minutes=3 * 60, sleep_need=0.58), neutral) is False


def test_need_accumulates_while_awake_and_falls_while_asleep():
    at = ts(2026, 3, 2, 14, 0)

    awake = make_state(at=at, activity=A.DAILY, sleep_need=0.25)
    S.settle(awake, now=at + 3600, config=quiet_cfg(), events=[], rng=random.Random(1))
    assert awake.sleep_need == pytest.approx(0.279, rel=1e-6), "醒着 +0.029/小时"

    asleep = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, sleep_need=0.62)
    S.settle(asleep, now=at + 3600, config=quiet_cfg(), events=[], rng=random.Random(1))
    assert asleep.sleep_need == pytest.approx(0.562, rel=1e-6), "睡着 −0.058/小时"


def test_sixteen_hours_awake_is_enough_to_be_sleepy():
    """身体驱动的时间尺度：从 0.18 醒着走 16 小时会越过 0.62，8 小时不会。"""

    at = ts(2026, 3, 2, 9, 0)
    p = policy()

    short = make_state(at=at, activity=A.DAILY, sleep_need=0.18)
    S.settle(short, now=at + 8 * 3600, config=quiet_cfg(), events=[], rng=random.Random(1))
    assert short.sleep_need < p.sleep_need_onset

    long = make_state(at=at, activity=A.DAILY, sleep_need=0.18)
    S.settle(long, now=at + 16 * 3600, config=quiet_cfg(), events=[], rng=random.Random(1))
    assert long.sleep_need >= p.sleep_need_onset
    # now_minutes=0 让昼夜偏置为 0：这里只验证困意本身，不叠加生物钟
    assert A._sleep_allowed(facts(sleep_need=long.sleep_need, now_minutes=0), p)


def test_a_full_night_clears_the_need():
    at = ts(2026, 3, 2, 3, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, sleep_need=0.62)
    S.settle(state, now=at + 8 * 3600, config=quiet_cfg(), events=[], rng=random.Random(1))
    assert 0.15 <= state.sleep_need <= 0.17
    assert state.continuous_sleep_minutes == 480
    assert A._natural_wake(
        facts(activity=A.SLEEP, minutes_in_sleep=480, continuous_sleep_minutes=480,
              sleep_need=state.sleep_need, now_minutes=11 * 60),
        policy(),
    )


def test_nap_only_partially_clears_the_need():
    at = ts(2026, 3, 2, 14, 0)
    state = make_state(at=at, activity=A.NAP, sleep_started_at=at, sleep_need=0.60, energy=4.0)
    S.settle(state, now=at + 3600, config=quiet_cfg(), events=[], rng=random.Random(1))
    assert state.sleep_need == pytest.approx(0.5768, rel=1e-6), "小睡只按 40% 消退"
    assert state.energy == pytest.approx(4.6, rel=1e-6), "体力 +0.60/小时（比整觉弱）"


# ================================================================ 二、醒来
# 清单第二部分：自然醒由困意恢复决定；模型在最短时长之后也不能绕过。

def test_natural_wake_needs_both_duration_and_need_cleared():
    p = policy()

    woken = A.enforce(
        _facts_with_need(minutes_in_sleep=400, sleep_need=0.10, energy=9.0,
                         now_minutes=9 * 60),
        None, p,
    )
    assert woken.activity == A.DAZE
    assert woken.wake_kind == "natural"
    assert "自然醒" in woken.note

    too_early = A.enforce(
        _facts_with_need(minutes_in_sleep=100, sleep_need=0.10, energy=9.0,
                         now_minutes=6 * 60),
        None, p,
    )
    assert too_early.activity == A.SLEEP, "没睡满最短时长：翻身不算醒"

    still_sleepy = A.enforce(
        _facts_with_need(minutes_in_sleep=400, sleep_need=0.50, energy=9.0,
                         now_minutes=9 * 60),
        None, p,
    )
    assert still_sleepy.activity == A.SLEEP, "时长够了但困意没退：接着睡"


def test_daily_total_no_longer_wakes_anyone():
    """今日已睡 13 小时也照样睡：日累计只做展示，不再参与叫醒。"""

    out = A.enforce(
        _facts_with_need(minutes_in_sleep=240, sleep_minutes_today=13 * 60, sleep_need=0.80),
        None, policy(),
    )
    assert out.activity == A.SLEEP


def test_only_the_abnormal_cap_wakes_by_time():
    abnormal = A.enforce(
        _facts_with_need(minutes_in_sleep=800, continuous_sleep_minutes=800, sleep_need=0.90),
        None, policy(max_sleep_hours=12.0),
    )
    assert abnormal.wake_kind == "abnormal"
    assert "异常上限" in abnormal.note

    short_cap = A.enforce(
        _facts_with_need(minutes_in_sleep=100, continuous_sleep_minutes=100, sleep_need=0.90,
                         now_minutes=5 * 60),
        None, policy(max_sleep_hours=1.5),
    )
    assert short_cap.activity == A.DAILY, "睡不到 3 小时不赖床，直接回到日常"
    assert "强制唤醒" in short_cap.note

    no_cap = A.enforce(
        _facts_with_need(minutes_in_sleep=2000, continuous_sleep_minutes=2000,
                         sleep_need=0.90),
        None, policy(max_sleep_hours=0.0),
    )
    assert no_cap.activity == A.SLEEP, "0 = 不设上限"


@pytest.mark.parametrize(
    "source",
    [A.SOURCE_LLM, A.SOURCE_ROUTINE, A.SOURCE_PHYSIO, A.SOURCE_COLD_START,
     A.SOURCE_RULES, A.SOURCE_INTERRUPT],
)
def test_no_entry_point_can_wake_her_while_she_is_still_sleepy(source):
    """模型提议、习惯表、生理窗、冷启动、规则回退、打断回退——同一条身体约束。"""

    out = A.enforce(
        _facts_with_need(minutes_in_sleep=300, sleep_need=0.70),
        A.ActivityDecision(A.DAILY, source=source, note="test"),
        policy(),
    )
    assert out.activity == A.SLEEP, source
    assert out.source == A.SOURCE_ENFORCED, source


def test_routine_and_physio_switches_only_matter_once_the_body_agrees():
    """开关的真实语义：**不**是「绕过身体」，而是「她本来就该醒时，允许习惯/生理窗当触发器」。"""

    ripe = _facts_with_need(minutes_in_sleep=400, sleep_need=0.10, now_minutes=9 * 60)

    blocked = A.enforce(ripe, request(A.DAILY, source=A.SOURCE_ROUTINE), policy())
    assert blocked.activity == A.SLEEP
    assert "睡眠优先" in blocked.note

    for switch, activity, source in (
        ("routine_can_wake", A.DAILY, A.SOURCE_ROUTINE),
        ("physio_can_wake", A.MEAL, A.SOURCE_PHYSIO),
    ):
        allowed = A.enforce(
            ripe, request(activity, source=source), policy(**{switch: True})
        )
        assert allowed.activity == activity, switch
        assert allowed.wake_kind == "natural", switch

    deep = _facts_with_need(minutes_in_sleep=400, sleep_need=0.70, now_minutes=9 * 60)
    still = A.enforce(deep, request(A.DAILY, source=A.SOURCE_ROUTINE),
                      policy(routine_can_wake=True))
    assert still.activity == A.SLEEP, "困意没退，开关也救不了"


def test_no_model_output_still_sleeps_and_wakes_her():
    sent = A.enforce(facts(sleep_need=0.80), None, policy())
    assert sent.activity == A.SLEEP
    assert "无有效决策，但已经困了" in sent.note

    kept = A.enforce(facts(sleep_need=0.20), None, policy())
    assert kept.activity == A.DAILY
    assert kept.source == A.SOURCE_RETAINED

    woke = A.enforce(
        _facts_with_need(minutes_in_sleep=400, sleep_need=0.10, now_minutes=9 * 60),
        None, policy(),
    )
    assert woke.activity != A.SLEEP


def test_cold_start_seed_respects_the_body():
    """冷启动/重播种也走同一条约束：很困就种成 sleep，不困才按时刻的习惯来。"""

    for minutes in (3 * 60, 15 * 60, 20 * 60):
        sleepy = A.rule_based_activity(now_minutes=minutes, sleep_need=0.85)
        assert sleepy.activity == A.SLEEP, minutes

    awake = A.rule_based_activity(now_minutes=20 * 60, sleep_need=0.20)
    assert awake.activity != A.SLEEP


def test_before_sleep_gate_matches_the_sleep_gate():
    """「准备睡觉」和「入睡」同一口径：不够困就不许进去。"""

    refused = A.enforce(facts(sleep_need=0.20), request(A.BEFORE_SLEEP), policy())
    assert refused.activity == A.DAILY
    assert refused.source == A.SOURCE_ENFORCED
    assert "先不去准备睡" in refused.note

    allowed = A.enforce(facts(sleep_need=0.80), request(A.BEFORE_SLEEP), policy())
    assert allowed.activity == A.BEFORE_SLEEP
    assert allowed.source == A.SOURCE_LLM

    parity = A.enforce(facts(sleep_need=0.20), None, policy())
    assert parity.activity == refused.activity


def test_schedule_blocks_naps_at_work_but_health_wins():
    work = A.ScheduleFacts(enabled=True, is_workday=True, phase=A.SCHEDULE_WORK)
    healthy = facts(now_minutes=10 * 60 + 30, sleep_need=0.30, schedule=work)
    assert "不该" in A.activity_blocked_by_schedule(A.NAP, healthy, policy())

    sleepy = facts(now_minutes=10 * 60 + 30, sleep_need=0.70, schedule=work)
    assert A.activity_blocked_by_schedule(A.SLEEP, sleepy, policy()) == "", "真困了，班表不拦睡觉"
    assert A.activity_blocked_by_schedule(A.NAP, sleepy, policy()) == ""


# ================================================================ 三、切换入口统一
# 清单第六部分：区分「准备睡 / 入睡 / 短暂醒来 / 续睡 / 自然醒」。

def test_before_sleep_and_sleep_are_different_activities():
    out = A.enforce(facts(sleep_need=0.80, now_minutes=23 * 60), request(A.BEFORE_SLEEP), policy())
    assert out.activity == A.BEFORE_SLEEP

    asleep = A.enforce(facts(activity=A.BEFORE_SLEEP, minutes_in_activity=10, sleep_need=0.80,
                             now_minutes=23 * 60 + 10),
                       request(A.SLEEP), policy())
    assert asleep.activity == A.SLEEP


def test_night_break_and_wake_grace_are_distinct_states():
    """夜醒续睡（night_break）与「刚醒赖床宽限」（wake_grace）各管一段，不能互相顶替。"""

    resume = A.enforce(
        facts(activity=A.DAZE, now_minutes=5 * 60, sleep_need=0.70, night_break=True),
        request(A.SLEEP), policy(),
    )
    assert resume.activity == A.SLEEP, "同一夜里可以睡回去"

    grace = A.enforce(
        facts(activity=A.DAZE, minutes_in_activity=5, now_minutes=9 * 60,
              sleep_need=0.70, wake_grace=True),
        request(A.SLEEP), policy(),
    )
    assert grace.activity == A.DAZE, "刚醒的宽限窗内不许立刻躺回去"


def test_wake_grace_exempts_the_dwell_and_then_expires():
    switched = A.enforce(
        facts(activity=A.DAZE, minutes_in_activity=5, now_minutes=9 * 60,
              sleep_need=0.30, wake_grace=True),
        request(A.MUSIC), policy(),
    )
    assert switched.activity == A.MUSIC, "宽限窗内不卡最短停留"

    held = A.enforce(
        facts(activity=A.DAZE, minutes_in_activity=5, now_minutes=9 * 60,
              sleep_need=0.30, wake_grace=True),
        None, policy(),
    )
    assert held.activity == A.DAZE
    assert held.source == A.SOURCE_RETAINED

    after = A.enforce(
        facts(activity=A.DAZE, minutes_in_activity=30, now_minutes=9 * 60,
              sleep_need=0.80, wake_grace=False),
        None, policy(),
    )
    assert after.activity == A.SLEEP, "宽限一过就回到常规"


def test_interrupt_fallback_cannot_bypass_the_body_gate():
    """打断回退（SOURCE_INTERRUPT）同样不能叫醒困着的人。"""

    out = A.enforce(
        _facts_with_need(minutes_in_sleep=300, sleep_need=0.70, interrupt_return_to=A.GAME),
        A.ActivityDecision(A.GAME, source=A.SOURCE_INTERRUPT, note="test"),
        policy(),
    )
    assert out.activity == A.SLEEP


def test_interrupt_window_holds_chatting_until_the_body_needs_sleep():
    awake = facts(activity=A.CHATTING, in_interrupt=True, minutes_in_activity=10,
                  sleep_need=0.30, now_minutes=20 * 60)
    assert A.enforce(awake, request(A.GAME), policy()).activity == A.CHATTING

    sleepy = clone(awake, sleep_need=0.80, now_minutes=23 * 60)
    deferred = A.enforce(sleepy, request(A.GAME), policy())
    assert deferred.activity == A.CHATTING
    assert deferred.defer == "open"


# ================================================================ 四、统一记账
# 清单第一部分：账本是唯一事实源；生活日边界只改归属；停机不补记；聊天不算睡。

def test_life_day_boundary_only_changes_attribution():
    at = ts(2026, 3, 2, 11, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, sleep_need=0.70)
    now = at
    for _ in range(120):
        now += 60
        S.settle(state, now=now, config=quiet_cfg(), events=[], rng=random.Random(1))

    assert state.continuous_sleep_minutes == 120, "跨 12:00 不清连续睡眠"
    assert state.sleep_minutes_today == 60, "只清归属：边界后重新计数"
    assert state.awake_minutes_today == 0
    segments = ledger(state)
    assert len(segments) == 1 and segments[0]["minutes"] == 120, "一觉就是一段"


def test_ledger_keeps_many_segments_and_never_truncates():
    """>8 段也不许丢：旧模型只留 8 条，长觉会被挤掉。"""

    at = ts(2026, 3, 10, 12, 0)
    state = make_state(at=at, sleep_tracking_since=ts(2026, 1, 1, 0))
    for index in range(12):
        start = at - (12 - index) * 3600.0
        S._add_sleep_interval(state, start=start, end=start + 900)

    segments = ledger(state)
    assert len(segments) == 12
    assert all(item["minutes"] == 15 for item in segments)
    assert S.sleep_in_window(state, now=at, hours=24) == 180

    S._add_sleep_interval(state, start=segments[-1]["end"], end=segments[-1]["end"] + 600)
    assert ledger(state)[-1]["minutes"] == 25, "真正连续的段才会合并"

    before = len(ledger(state))
    S._add_sleep_interval(state, start=at - 11 * 3600 + 300, end=at - 11 * 3600 + 1200)
    assert len(ledger(state)) == before, "重叠的段去重，不重复计"


def test_ledger_drops_only_what_is_older_than_retention():
    at = ts(2026, 3, 10, 12, 0)
    state = make_state(at=at)
    old_end = at - S.SLEEP_LEDGER_RETENTION_HOURS * 3600.0 - 3600.0
    S._add_sleep_interval(state, start=old_end - 3600.0, end=old_end)
    S._add_sleep_interval(state, start=at - 3600.0, end=at - 2700.0)
    assert len(ledger(state)) == 1, "只保留 192 小时以内的段"


def test_chatting_is_awake_not_sleep():
    at = ts(2026, 3, 2, 20, 0)
    state = make_state(at=at, activity=A.CHATTING, sleep_need=0.70, energy=5.0,
                       interrupt_until=at + 3600)
    S.settle(state, now=at + 3600, config=quiet_cfg(), events=[], rng=random.Random(1))

    assert state.sleep_minutes_today == 0
    assert state.awake_minutes_today == 60
    assert state.continuous_awake_minutes == 60
    assert ledger(state) == [], "聊天不进睡眠账本"
    assert state.sleep_need == pytest.approx(0.729, rel=1e-6), "聊着也是醒着，困意在涨"


def test_offline_gap_books_nothing_and_resets_the_anchor():
    """停机间隙那段「她在不在睡」无从知道 ⇒ 不进账本、按未知区间留档、锚点重置。"""

    at = ts(2026, 3, 2, 20, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, sleep_need=0.70, energy=5.0)
    later = at + 8 * 3600
    S.settle(state, now=later, config=quiet_cfg(offline_gap_minutes=30, tick_seconds=600),
             events=[], rng=random.Random(1))

    assert ledger(state) == [], "不按旧锚点补记 8 小时睡眠"
    assert state.sleep_unknown and state.sleep_unknown[0]["minutes"] == 480
    assert S._normalize_sleep_ledger(state.sleep_unknown) != []
    assert state.sleep_started_at == later
    assert state.continuous_sleep_minutes == 0
    assert state.sleep_need == pytest.approx(0.70), "困意冻结，不拿停机时长去猜"
    assert state.energy == pytest.approx(5.0)
    assert state.sleep_minutes_today == 0


def test_clock_rollback_marks_unknown_and_calls_back():
    at = ts(2026, 3, 2, 10, 0)
    state = make_state(at=at, activity=A.DAILY, sleep_need=0.30)
    reported = []

    S.settle(state, now=at - 600, config=quiet_cfg(), events=[], rng=random.Random(1),
             on_clock_rollback=reported.append)

    assert state.last_tick_at == at - 600, "锚点拉回现在，否则时间永远追不上"
    assert state.sleep_unknown and state.sleep_unknown[0]["minutes"] == 10
    assert reported == [10]
    assert state.sleep_need == pytest.approx(0.30), "倒流的时间不结算"


def test_night_waking_books_the_sleep_so_far_and_keeps_the_night():
    at = ts(2026, 2, 8, 5, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at - 4 * 3600,
                       continuous_sleep_minutes=240, sleep_need=0.60, energy=6.0)
    state.last_tick_at = at - 600
    config = quiet_cfg(night_waking_enabled=True, night_waking_probability=1.0,
                       wake_daze_minutes=15)

    state = S.settle(state, now=at, config=config, events=[], rng=random.Random(1))

    assert state.activity == A.DAZE
    assert state.wake_grace_until > at
    assert state.sleep_started_at == at - 4 * 3600, "夜醒不重置入睡锚点"
    assert [item["minutes"] for item in ledger(state)] == [10], "这一段确实睡过，进账本"
    assert any(item.get("kind") == "night_waking" for item in state.recent_events)


# ================================================================ 五、缺觉与身体后果
# 清单第三部分：只按时间算；记录不足就说记录不足；体力上限压低但不补体力。

def test_deficit_is_computed_from_the_ledger_by_time():
    at = ts(2026, 3, 5, 12, 0)
    state = make_state(at=at, sleep_tracking_since=at - 73 * 3600.0)
    for day in range(3):
        start = at - (3 - day) * 86400.0 + 3 * 3600.0
        S._add_sleep_interval(state, start=start, end=start + 120 * 60.0)

    deficit, reason = S.sleep_deficit_minutes(state, now=at, config=cfg())
    assert (deficit, reason) == (1080, ""), "72h 目标 1440 分钟 − 实睡 360 分钟"
    nights, _ = S.sleep_deficit_nights(state, now=at, config=cfg())
    assert nights == pytest.approx(6.0), "每「晚」180 分钟（8h − 5h）"


def test_insufficient_records_say_so_instead_of_guessing():
    at = ts(2026, 3, 5, 12, 0)
    fresh = make_state(at=at, sleep_tracking_since=at - 3600.0)
    deficit, reason = S.sleep_deficit_minutes(fresh, now=at, config=cfg())
    assert deficit is None and "刚开始记录" in reason
    assert S.sleep_deficit_label(fresh, now=at, config=cfg()).startswith("睡眠记录不足")

    unknown = make_state(at=at, sleep_tracking_since=at - 200 * 3600.0)
    S._mark_unknown_span(unknown, start=at - 10 * 3600.0, end=at - 9 * 3600.0)
    S._mark_unknown_span(unknown, start=at - 8 * 3600.0, end=at - 5 * 3600.0)
    deficit, reason = S.sleep_deficit_minutes(unknown, now=at, config=cfg())
    assert deficit is None and "无法记账" in reason

    legacy_only = make_state(at=at)
    assert S.sleep_deficit_minutes(legacy_only, now=at, config=cfg())[0] is None


def test_energy_cap_is_lowered_by_deficit_but_never_refills_energy():
    at = ts(2026, 3, 5, 12, 0)
    state = make_state(at=at, sleep_tracking_since=at - 80 * 3600.0, energy=9.7, energy_cap=10.0)
    for day in range(3):
        start = at - (3 - day) * 86400.0 + 3 * 3600.0
        S._add_sleep_interval(state, start=start, end=start + 7200.0)

    S._refresh_energy_cap(state, now=at, config=cfg())
    assert state.energy_cap == pytest.approx(8.5)
    assert state.energy == pytest.approx(8.5), "只往下压，不凭空补"

    state.sleep_tracking_since = 0.0
    S._refresh_energy_cap(state, now=at, config=cfg())
    assert state.energy_cap == pytest.approx(10.0), "记录不足时不猜缺觉"
    assert state.energy == pytest.approx(8.5), "上限回来了也不补体力"


def test_deficit_label_reports_hours_and_cap():
    at = ts(2026, 3, 5, 12, 0)
    state = make_state(at=at, sleep_tracking_since=at - 73 * 3600.0)
    for day in range(3):
        start = at - (3 - day) * 86400.0 + 3 * 3600.0
        S._add_sleep_interval(state, start=start, end=start + 120 * 60.0)

    text = S.sleep_deficit_label(state, now=at, config=cfg())
    assert "最近 72 小时累计缺觉约 18.0 小时" in text
    assert "8.5" in text

    healthy = make_state(at=at, sleep_tracking_since=at - 73 * 3600.0)
    for day in range(3):
        start = at - (3 - day) * 86400.0 + 3 * 3600.0
        S._add_sleep_interval(healthy, start=start, end=start + 8 * 3600.0)
    assert S.sleep_deficit_label(healthy, now=at, config=cfg()) == "健康"


def test_sleep_debt_no_longer_moves_per_wake():
    """旧模型每次醒都 ±1 晚；新模型里这个字段迁移后恒为 0，只有留档字段记得旧值。"""

    config = quiet_cfg(night_waking_enabled=True, night_waking_probability=1.0,
                       wake_daze_minutes=15, tick_seconds=600)
    start = ts(2026, 3, 1, 22, 0)
    state = S.new_state(now=start, config=config, energy=5.0)
    now = start
    for _ in range(3 * 24 * 6):
        now += 600
        state = S.settle(state, now=now, config=config, events=(), rng=random.Random(4))
        state = S.enforce_and_apply(state, now=now, config=config, decision=None)

    assert state.sleep_debt_nights == 0
    assert state.legacy_sleep_debt_nights == 0


def test_grade_sleep_debt_is_a_retired_no_op():
    state = make_state(at=ts(2026, 3, 2, 14, 0), sleep_debt_nights=0)
    before = state.to_dict()
    assert S.grade_sleep_debt(state, now=ts(2026, 3, 2, 15, 0), config=cfg()) == 0
    assert state.to_dict() == before


# ================================================================ 六、睡前陪聊预算
# 清单第五部分：第一次「该睡了」开一段，最多 15 分钟，不因新消息/新会话重置。

def _chatting_facts(**overrides):
    base = dict(activity=A.CHATTING, in_interrupt=True, minutes_in_activity=10,
                now_minutes=23 * 60, sleep_need=0.80, awake_minutes_today=15 * 60)
    base.update(overrides)
    return facts(**base)


def test_budget_opens_once_and_is_never_renewed():
    first = A.enforce(_chatting_facts(), request(A.CHATTING), policy())
    assert first.activity == A.CHATTING
    assert first.defer == "open"
    assert "再聊 15 分钟" in first.note

    holding = A.enforce(clone(_chatting_facts(), sleep_defer_active=True),
                        request(A.GAME), policy())
    assert holding.activity == A.CHATTING
    assert holding.defer == "hold", "预算内继续聊，但不再开一段"
    assert "把这句话说完" in holding.note


def test_budget_expiry_forbids_further_deferral():
    expired = A.enforce(clone(_chatting_facts(), sleep_defer_expired=True),
                        request(A.GAME), policy())
    assert expired.activity == A.SLEEP
    assert expired.defer == ""
    assert "已用完" in expired.note


def test_zero_budget_means_go_to_sleep_now():
    """回归：0 分钟预算以前会让她在打断窗口里永远睡不下（每轮都「再聊 0 分钟」）。"""

    out = A.enforce(_chatting_facts(), request(A.GAME), policy(sleep_deferral_minutes=0))
    assert out.activity == A.SLEEP
    assert out.defer == ""
    assert "直接去睡" in out.note


def test_budget_window_makes_asking_pointless_but_nothing_else_does():
    """打断窗口内「答什么都留在 chatting」⇒ 一律白问；窗口外才值得问。"""

    assert A.request_is_pointless(_chatting_facts(), policy())
    assert A.request_is_pointless(clone(_chatting_facts(), sleep_defer_active=True), policy())
    assert A.request_is_pointless(clone(_chatting_facts(), sleep_defer_expired=True), policy())
    assert A.request_is_pointless(_chatting_facts(sleep_need=0.20), policy()), "不困也是白问"
    assert A.request_is_pointless(facts(sleep_need=0.80), policy()) == "", "普通清醒：值得问"


def test_budget_deadline_survives_a_restart():
    stamp = ts(2026, 3, 2, 23, 10)
    state = make_state(at=stamp, sleep_defer_until=stamp + 600)
    restored = S.LifeState.from_dict(state.to_dict())
    assert restored.sleep_defer_until == pytest.approx(stamp + 600)


def test_sleeping_and_need_cleared_clear_the_budget():
    at = ts(2026, 3, 2, 23, 0)

    asleep = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, sleep_need=0.70,
                        sleep_defer_until=at + 600)
    S.settle(asleep, now=at + 600, config=quiet_cfg(), events=[], rng=random.Random(1))
    assert asleep.sleep_defer_until == 0.0, "睡着了预算自然作废"

    cleared = make_state(at=at, activity=A.DAZE, sleep_need=0.10, sleep_defer_until=at + 600)
    S.settle(cleared, now=at + 600, config=quiet_cfg(), events=[], rng=random.Random(1))
    assert cleared.sleep_defer_until == 0.0, "困意退了，预算也没有意义了"

    still = make_state(at=at, activity=A.DAILY, sleep_need=0.80, sleep_defer_until=at + 600)
    S.settle(still, now=at + 600, config=quiet_cfg(), events=[], rng=random.Random(1))
    assert still.sleep_defer_until == at + 600, "还困着就保留"


# ================================================================ 七、小睡
# 清单第二部分：小睡按困意进入，只按比例消退困意，补不了整觉。

def test_nap_entry_is_need_based_not_energy_based():
    allowed = A.enforce(facts(sleep_need=0.60, energy=8.0), request(A.NAP), policy())
    assert allowed.activity == A.NAP, "困意到了，体力满也能眯"

    refused = A.enforce(facts(sleep_need=0.20, energy=3.0), request(A.NAP), policy())
    assert refused.activity == A.DAILY, "体力低但不困 ⇒ 不眯"
    assert "不用小睡" in refused.note


def test_very_sleepy_nap_becomes_a_real_sleep():
    out = A.enforce(facts(sleep_need=0.90), request(A.NAP), policy())
    assert out.activity == A.SLEEP
    assert "不小睡" in out.note


def test_nap_limits_and_wake_kind():
    kept = A.enforce(facts(activity=A.NAP, minutes_in_activity=5, minutes_in_sleep=5,
                           sleep_need=0.50), request(A.DAILY), policy())
    assert kept.activity == A.NAP, "未满最短时长继续眯"

    woken = A.enforce(facts(activity=A.NAP, minutes_in_activity=95, minutes_in_sleep=95,
                            sleep_need=0.50), request(A.NAP), policy())
    assert woken.activity == A.DAILY
    assert woken.wake_kind == "nap"
    assert "达上限" in woken.note


def test_nap_records_sleep_but_only_partially_recovers():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, activity=A.NAP, sleep_started_at=at, sleep_need=0.60, energy=4.0)
    S.settle(state, now=at + 3600, config=quiet_cfg(), events=[], rng=random.Random(1))
    S.apply_activity(state, A.ActivityDecision(A.DAILY, source=A.SOURCE_LLM),
                     now=at + 3600, config=quiet_cfg())

    assert state.sleep_minutes_today == 60
    assert state.awake_minutes_today == 0
    assert state.sleep_started_at == 0.0
    assert [item["minutes"] for item in ledger(state)] == [60]
    assert state.sleep_need == pytest.approx(0.5768, rel=1e-6)
    assert state.sleep_debt_nights == 0, "小睡不会被记成「熬了一夜」"


def test_nap_can_be_disabled_and_is_never_a_side_activity():
    off = A.enforce(facts(sleep_need=0.60), request(A.NAP), policy(nap_enabled=False))
    assert off.activity == A.DAILY
    assert A.NAP not in A.PromptInput(allow_nap=False).activity_choices()
    assert A.NAP in A.PromptInput().activity_choices()
    assert A.NAP not in A.SIDE_ACTIVITIES
    assert A.normalize_side([A.NAP], main=A.DAILY) == ()


def test_nap_predicates_aliases_and_interruptibility():
    assert A.is_asleep(A.NAP) is True
    assert A.is_awake(A.NAP) is False
    assert A.ACTIVITY_LABELS[A.NAP] == "小睡"
    for alias in ("小睡", "午睡", "小憩", "打盹", "眯一会儿", "nap", "doze"):
        assert A.normalize_activity(alias) == A.NAP, alias

    assert X.should_interrupt(enabled=True, activity=A.NAP, is_command=False,
                              is_bot_message=False, private_chat=True, mentioned=False)
    assert not X.should_interrupt(enabled=True, activity=A.SLEEP, is_command=False,
                                  is_bot_message=False, private_chat=True, mentioned=False)


def test_nap_is_a_silent_hard_gate_for_factors_and_proactive():
    for materials in (0.0, 99.0):
        quiet = F.compute_adjust(
            activity=A.NAP, emotion=10.0, energy=10.0, sick=False, sleep_debt_nights=0,
            date_factor=1.0, material_count=materials, now_minutes=14 * 60,
            config=F.FactorConfig(),
        )
        assert quiet.adjust == pytest.approx(0.0), "攒了素材也不抬小睡"
        assert quiet.reason == F.REASON_NAP

    out = P.decide(
        config=P.ProactiveConfig(enabled=True), now=1_000.0, now_minutes=14 * 60,
        activity=A.NAP, energy=9.0, emotion=5.0,
        materials=[{"text": "想说说", "weight": 1.0, "created_at": 0.0,
                    "expires_at": 1e12, "best_until": 1e12}],
        session={}, day_key="2026-02-08",
    )
    assert out.should_send is False
    assert out.reason == P.REASON_SLEEPING


# ================================================================ 八、夜醒与赖床
# 清单第六部分：短暂醒来 ≠ 自然醒；醒来要说清为什么。

def test_night_waking_is_once_per_life_day_and_off_by_default():
    at = ts(2026, 2, 8, 5, 0)

    off = make_state(at=at, activity=A.SLEEP, sleep_started_at=at - 4 * 3600,
                     continuous_sleep_minutes=240, sleep_need=0.60)
    off.last_tick_at = at - 600
    S.settle(off, now=at, config=quiet_cfg(), events=[], rng=random.Random(1))
    assert off.activity == A.SLEEP, "默认关"

    config = quiet_cfg(night_waking_enabled=True, night_waking_probability=1.0,
                       wake_daze_minutes=15)
    once = make_state(at=at, activity=A.SLEEP, sleep_started_at=at - 4 * 3600,
                      continuous_sleep_minutes=240, sleep_need=0.60)
    once.last_tick_at = at - 600
    S.settle(once, now=at, config=config, events=[], rng=random.Random(1))
    assert once.activity == A.DAZE

    once.activity = A.SLEEP
    once.sleep_started_at = at
    once.wake_grace_until = 0.0
    once.last_tick_at = at
    S.settle(once, now=at + 600, config=config, events=[], rng=random.Random(1))
    assert once.activity == A.SLEEP, "同一生活日不会再醒第二次"


def test_night_waking_is_skipped_when_the_need_is_gone_or_the_sleep_too_short():
    at = ts(2026, 2, 8, 5, 0)
    config = quiet_cfg(night_waking_enabled=True, night_waking_probability=1.0,
                       wake_daze_minutes=15)

    settled = make_state(at=at, activity=A.SLEEP, sleep_started_at=at - 4 * 3600,
                         continuous_sleep_minutes=240, sleep_need=0.10)
    settled.last_tick_at = at - 600
    S.settle(settled, now=at, config=config, events=[], rng=random.Random(1))
    assert settled.activity == A.SLEEP, "困意已经退够了，该走自然醒，不是「半夜醒」"

    early = make_state(at=at, activity=A.SLEEP, sleep_started_at=at - 3600,
                       continuous_sleep_minutes=60, sleep_need=0.60)
    early.last_tick_at = at - 600
    S.settle(early, now=at, config=config, events=[], rng=random.Random(1))
    assert early.activity == A.SLEEP, "没睡满最短时长不掷夜醒"


def test_night_waking_does_not_make_the_night_longer():
    """夜醒只是把同一夜切成几段（并各留 15 分钟发呆），**总睡眠时长不变**。"""

    def run(waking: bool):
        config = quiet_cfg(night_waking_enabled=waking, night_waking_probability=1.0,
                           wake_daze_minutes=15, tick_seconds=600)
        start = ts(2026, 3, 2, 3, 0)
        state = make_state(at=start, activity=A.SLEEP, sleep_started_at=start,
                           continuous_sleep_minutes=0, sleep_need=0.70, energy=3.5)
        now = start
        woke = None
        for _ in range(24 * 12):
            now += 300
            state = S.settle(state, now=now, config=config, events=(), rng=random.Random(5))
            state = S.enforce_and_apply(state, now=now, config=config, decision=None)
            if (state.sleep_need <= 0.16 and state.continuous_sleep_minutes == 0
                    and A.is_awake(state.activity)):
                woke = now
                break
        assert woke is not None
        return (woke - start) / 3600.0, S.sleep_in_window(state, now=woke, hours=12), \
            len(ledger(state))

    off_hours, off_slept, off_segments = run(False)
    on_hours, on_slept, on_segments = run(True)

    assert abs(on_slept - off_slept) <= 20, (on_slept, off_slept)
    assert on_segments > off_segments, "夜醒会把账本切出更多段（这就是它该有的样子）"
    assert on_hours - off_hours <= 1.5, "只多出发呆的那点墙钟时间"


def test_night_break_lets_her_fall_back_asleep():
    """同一夜里醒了一下：宽限窗过后、没有新提议时直接送回床，不等困意重新攒到入睡阈。"""

    out = A.enforce(
        facts(activity=A.DAZE, now_minutes=6 * 60, sleep_need=0.70, night_break=True),
        None, policy(),
    )
    assert out.activity == A.SLEEP
    assert "接着睡" in out.note

    without = A.enforce(
        facts(activity=A.DAZE, now_minutes=6 * 60, sleep_need=0.30),
        None, policy(),
    )
    assert without.activity == A.DAZE, "没有夜醒记录就按普通清醒处理"


def test_long_sleep_wakes_into_daze_and_short_one_does_not():
    long = A.enforce(_facts_with_need(minutes_in_sleep=400, sleep_need=0.10,
                                      now_minutes=9 * 60), None, policy())
    assert long.activity == A.DAZE and "赖" in long.note

    off = A.enforce(_facts_with_need(minutes_in_sleep=400, sleep_need=0.10,
                                     now_minutes=9 * 60), None, policy(wake_daze_minutes=0))
    assert off.activity == A.DAILY


def test_sick_wake_lands_in_sick_rest():
    out = A.enforce(
        _facts_with_need(minutes_in_sleep=400, sleep_need=0.10, sick=True,
                         cold_stage=A.COLD_WORSENING, now_minutes=9 * 60),
        None, policy(),
    )
    assert out.activity == A.SICK_REST
    assert out.scene, "醒来要有明确的病中场景"


# ================================================================ 九、失眠
# 清单第二部分：压力大时躺下也睡不着，但困到极点是身体说了算。

def test_insomnia_turns_sleep_into_before_sleep():
    for decision in (request(A.SLEEP), None):
        out = A.enforce(
            facts(activity=A.DAILY if decision else A.GAME, sleep_need=0.80,
                  now_minutes=23 * 60, insomnia_roll=True),
            decision, policy(),
        )
        assert out.activity == A.BEFORE_SLEEP
        assert out.scene == A.INSOMNIA_SCENE
        assert "睡不着" in out.note


def test_insomnia_can_be_disabled_and_never_overrides_exhaustion():
    disabled = A.enforce(facts(sleep_need=0.80, now_minutes=23 * 60, insomnia_roll=True),
                         request(A.SLEEP), policy(insomnia_enabled=False))
    assert disabled.activity == A.SLEEP

    exhausted = A.enforce(facts(sleep_need=0.95, now_minutes=23 * 60, insomnia_roll=True),
                          request(A.SLEEP), policy())
    assert exhausted.activity == A.SLEEP, "困到 0.90 以上，失眠也压不住"


# ================================================================ 十、病后
# 清单第四部分：病好了不许继续养病；旧状态里残留的 sick_rest 也要收口。

def test_healthy_rejects_sick_rest():
    out = A.enforce(facts(sick=False, sleep_need=0.20), request(A.SICK_REST), policy())
    assert out.activity == A.DAILY, "健康时「养病」不是有效活动"
    assert out.source != A.SOURCE_LLM

    sleepy = A.enforce(facts(activity=A.GAME, sick=False, sleep_need=0.80),
                       request(A.SICK_REST), policy())
    assert sleepy.activity == A.SLEEP, "她只是困了，不是病了"


def test_stale_sick_rest_exits_when_the_illness_ends():
    out = A.enforce(
        facts(activity=A.SICK_REST, sick=False, minutes_in_activity=200, sleep_need=0.20),
        request(A.SICK_REST), policy(),
    )
    assert out.activity == A.DAILY
    assert out.scene == "身体好了，起来活动活动", "场景不许继续挂着病中那句"
    assert "病好了" in out.note

    sleepy = A.enforce(
        facts(activity=A.SICK_REST, sick=False, minutes_in_activity=200, sleep_need=0.80),
        request(A.SICK_REST), policy(),
    )
    assert sleepy.activity == A.SLEEP
    assert sleepy.scene == "", "去睡觉就不该带养病场景"


def test_worsening_forces_rest_and_recovering_allows_only_light_activity():
    worsening = A.enforce(
        facts(activity=A.DAILY, sick=True, cold_stage=A.COLD_WORSENING, sleep_need=0.20),
        request(A.GAME), policy(),
    )
    assert worsening.activity == A.SICK_REST
    assert "强制养病" in worsening.note

    recovering = A.enforce(
        facts(activity=A.DAILY, sick=True, cold_stage=A.COLD_RECOVERING, sleep_need=0.20),
        request(A.GAME), policy(),
    )
    assert recovering.activity == A.SICK_REST
    assert "还不能干活" in recovering.note

    light = A.enforce(
        facts(activity=A.DAILY, sick=True, cold_stage=A.COLD_RECOVERING, sleep_need=0.20),
        request(A.MUSIC), policy(),
    )
    assert light.activity == A.MUSIC


# ================================================================ 十一、迁移与显示
# 清单第八部分：显式幂等迁移；旧「6 晚」留档；新模型说「记录不足」。

def test_legacy_state_migrates_once_and_keeps_evidence():
    raw = {
        "state_version": 1,
        "sleep_debt_nights": 6,
        "activity": A.DAILY,
        "continuous_awake_minutes": 600,
        "emotion": 6.2,
        "energy": 3.0,
        "sleep_need": 0.0,
        "day_key": "2026-03-05",
        "materials": [{"text": "一句想说的话"}],
    }
    state = S.LifeState.from_dict(raw)

    assert state.state_version == S.STATE_VERSION == 2
    assert state.legacy_sleep_debt_nights == 6, "旧「连熬 6 天」原样留档"
    assert state.sleep_debt_nights == 0, "不再参与新模型的任何判定"
    assert state.sleep_tracking_since == 0.0
    assert state.sleep_need == pytest.approx(0.47), "按清醒时长估一个起点（10h × 0.029）"
    assert state.emotion == pytest.approx(6.2)
    assert state.continuous_awake_minutes == 600
    assert state.materials == [{"text": "一句想说的话"}]


def test_migration_is_idempotent():
    once = S.LifeState.from_dict({"state_version": 1, "sleep_debt_nights": 6,
                                  "sleep_need": 0.0, "continuous_awake_minutes": 600})
    twice = S.LifeState.from_dict(once.to_dict())
    assert twice.legacy_sleep_debt_nights == 6
    assert twice.sleep_debt_nights == 0
    assert twice.sleep_need == pytest.approx(once.sleep_need)


def test_migration_does_not_fabricate_missing_sleep():
    state = S.LifeState.from_dict({
        "state_version": 1, "sleep_debt_nights": 6, "activity": A.SLEEP,
        "sleep_started_at": ts(2026, 3, 1, 3, 0), "day_key": "2026-03-05",
    })
    assert state.sleep_ledger == [], "不补 146 分钟，也不按旧锚点造账本"
    assert state.sleep_debt_nights == 0
    assert S.sleep_deficit_minutes(state, now=ts(2026, 3, 5, 12, 0), config=cfg())[0] is None
    assert "6" not in S.sleep_deficit_label(state, now=ts(2026, 3, 5, 12, 0), config=cfg())


def test_new_fields_roundtrip_and_are_sanitized():
    stamp = ts(2026, 3, 5, 12, 0)
    state = make_state(at=stamp, sleep_need=0.42, sleep_defer_until=stamp + 600,
                       sleep_tracking_since=stamp - 3600, legacy_sleep_debt_nights=2)
    S._mark_unknown_span(state, start=stamp - 600, end=stamp - 300)

    restored = S.LifeState.from_dict(state.to_dict())
    assert restored.sleep_need == pytest.approx(0.42)
    assert restored.sleep_defer_until == pytest.approx(stamp + 600)
    assert restored.sleep_tracking_since == pytest.approx(stamp - 3600)
    assert restored.legacy_sleep_debt_nights == 2
    assert len(restored.sleep_unknown) == 1

    for bad in (float("nan"), float("inf"), -1.0, "abc", None, True):
        dirty = S.LifeState.from_dict({
            "sleep_defer_until": bad, "sleep_tracking_since": bad,
            "wake_grace_until": bad, "last_night_waking_at": bad,
        })
        assert dirty.sleep_defer_until == 0.0, bad
        assert dirty.sleep_tracking_since == 0.0, bad
        assert dirty.wake_grace_until == 0.0, bad
        assert dirty.last_night_waking_at == 0.0, bad


def test_effect_lines_print_the_body_model_and_its_numbers():
    text = "\n".join(S.activity_effect_lines(cfg()))
    for fragment in ("0.62", "0.16", "回满也不代表睡够了", "40%", "72 小时",
                     "记录不足", "异常叫醒", "小睡"):
        assert fragment in text, fragment


def test_effect_lines_track_config_changes():
    text = "\n".join(S.activity_effect_lines(
        cfg(sleep_need_onset=0.50, sleep_need_wake=0.20, sleep_target_hours=7.0,
            sleep_deficit_window_hours=48.0)
    ))
    assert "0.50" in text and "0.20" in text
    assert "48 小时" in text and "7 小时/天" in text

    no_nap = S.activity_effect_lines(cfg(nap_enabled=False))
    assert not any(line.startswith("小睡：") for line in no_nap), "关掉小睡就别再讲它的规则"


def test_prompt_shows_need_and_budget():
    text = A.build_prompt(A.PromptInput(activity=A.DAILY, sleep_need=0.70,
                                        sleep_deferral_minutes=15))
    assert "困意：0.70" in text
    assert "睡前预算：还可以再聊 15 分钟" in text
    assert "体力满也**不代表**睡够了" in text


# ================================================================ 十二、配置与 WebUI
# 清单第七部分：退役字段不再参与判定；新参数进提示与 WebUI。

def test_config_defaults_match_the_body_model():
    defaults = S.SimConfig()
    assert defaults.sleep_need_onset == pytest.approx(0.62)
    assert defaults.sleep_need_wake == pytest.approx(0.16)
    assert defaults.sleep_need_awake_gain_per_hour == pytest.approx(0.029)
    assert defaults.sleep_need_sleep_recovery_per_hour == pytest.approx(0.058)
    assert defaults.sleep_need_nap_recovery_ratio == pytest.approx(0.40)
    assert defaults.sleep_need_nap_threshold == pytest.approx(0.42)
    assert defaults.sleep_circadian_strength == pytest.approx(0.05)
    assert defaults.sleep_deferral_minutes == 15
    assert defaults.sleep_target_hours == pytest.approx(8.0)
    assert defaults.sleep_deficit_window_hours == pytest.approx(72.0)
    assert defaults.sleep_unknown_tolerance_minutes == pytest.approx(60.0)
    assert defaults.min_sleep_minutes == 180
    assert defaults.nap_min_minutes == 20 and defaults.nap_max_minutes == 90
    assert defaults.max_sleep_hours == pytest.approx(12.0)


def test_enforce_policy_maps_every_new_knob():
    sim = S.SimConfig(sleep_need_onset=0.58, sleep_need_wake=0.20,
                      sleep_need_nap_threshold=0.45, sleep_circadian_strength=0.03,
                      sleep_deferral_minutes=7)
    p = S.build_enforce_policy(sim)
    assert p.sleep_need_onset == pytest.approx(0.58)
    assert p.sleep_need_wake == pytest.approx(0.20)
    assert p.nap_need_threshold == pytest.approx(0.45)
    assert p.sleep_circadian_strength == pytest.approx(0.03)
    assert p.sleep_deferral_minutes == 7


def test_wake_threshold_is_clamped_below_the_onset_on_load():
    """配置层就把「醒来阈 ≥ 入睡阈」修正掉：否则她会睡下即醒、来回抖动。"""

    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _m, plugin, _host = _make_plugin(
        simulation={"sleep_need_onset": 0.30, "sleep_need_wake": 0.80}
    )
    sim = plugin._sim_config()
    assert sim.sleep_need_wake < sim.sleep_need_onset
    assert sim.sleep_need_wake == pytest.approx(0.20)


def test_retired_fields_never_reach_the_enforce_policy():
    p = S.build_enforce_policy(S.SimConfig())
    for retired in ("sleep_window", "energy_full_wake", "energy_full_wake_min_hours",
                    "sleep_hard_floor", "min_awake_hours_per_day", "nap_energy_threshold",
                    "rest_day_sleep_extension_minutes", "cold_onset_awake_floor",
                    "sleep_debt_recovery_step"):
        assert not hasattr(p, retired), retired


def test_retired_switches_no_longer_change_anything():
    """旧配置怎么改都不该影响判定：白天能睡、夜里能醒，只看困意。"""

    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _m, plugin, _host = _make_plugin(
        simulation={
            "sleep_window": "23:00-07:00",
            "energy_full_wake": True,
            "energy_full_wake_min_hours": 99.0,
            "rest_day_sleep_extension_minutes": 240,
            "nap_energy_threshold": 9.9,
        },
        activity={"sleep_hard_floor": 9.9, "min_awake_hours_per_day": 24},
    )
    sim = plugin._sim_config()
    p = S.build_enforce_policy(sim)

    assert A.enforce(facts(now_minutes=15 * 60, sleep_need=0.80), request(A.SLEEP), p).activity \
        == A.SLEEP, "白日照样能睡"
    assert A.enforce(facts(now_minutes=3 * 60, sleep_need=0.20), request(A.DAILY), p).activity \
        == A.DAILY, "夜里照样能醒着"
    assert A.enforce(facts(now_minutes=15 * 60, sleep_need=0.25, energy=0.2),
                     request(A.DAILY), p).activity == A.DAILY, "体力 0.2 也不再强制入睡"


def test_nap_limits_are_clamped_on_load():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _m, plugin, _host = _make_plugin(
        simulation={"nap_min_minutes": 500, "nap_max_minutes": 30}
    )
    sim = plugin._sim_config()
    assert sim.nap_max_minutes == 30
    assert sim.nap_min_minutes == 30, "下限不得超过上限（否则小睡永远自相矛盾）"


# ================================================================ 十三、插件接线
# 唤醒链路 / 节流 / 预算是状态机，纯函数证明不了，只能拉到插件上测。

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

    module = load_plugin_module(PLUGIN_DIR, "life_frequency_sleep_acceptance")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = 0
    config["frequency"]["quiet_hours"] = []
    config["physio"]["meals"] = []
    config["routines"]["lines"] = []
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin._rebuild_from_config()
    return module, plugin, host


def _sleepy(plugin, now: float) -> None:
    plugin._state.activity = A.SLEEP
    plugin._state.activity_since = now - 3600
    plugin._state.sleep_started_at = now - 3600
    plugin._state.continuous_sleep_minutes = 60
    plugin._state.sleep_minutes_today = 60
    plugin._state.sleep_need = 0.60
    plugin._state.energy = 5.0
    plugin._state.at_wake_until = 0.0
    plugin._state.sleep_defer_until = 0.0


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
        assert plugin._state.at_wake_until > now
        assert len(host.calls_of("frequency.set_adjust")) == 1

    asyncio.run(run())


def test_group_at_wakes_only_her_body_allows_it():
    """醒的窗口是「回消息的权限」，不是「换活动」：活动仍是 sleep，也不产生梦/补答。"""

    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, host = _make_plugin()
        now = time.time()
        _sleepy(plugin, now)
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        assert plugin._state.at_wake_until > now
        assert plugin._state.activity == A.SLEEP
        assert plugin._dream_wake_pending is None
        assert plugin._catchup_pending_wake is False
        assert plugin._state.sleep_defer_until == 0.0, "临时唤醒不占用睡前预算"
        assert len(host.calls_of("frequency.set_adjust")) == 1

    asyncio.run(run())


def test_wake_window_extends_without_a_second_host_write():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, host = _make_plugin()
        now = time.time()
        _sleepy(plugin, now)
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        host.calls.clear()
        plugin._state.at_wake_until = time.time() + 60.0
        await plugin.note_session(
            message={"session_id": "group-1", "processed_plain_text": "那你继续睡吧"}
        )
        assert plugin._state.at_wake_until > time.time() + 60.0
        assert not host.calls_of("frequency.set_adjust"), "顺延不重写宿主"

    asyncio.run(run())


def test_wake_extension_is_capped_and_can_be_disabled():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, _host = _make_plugin(
            simulation={"wake_minutes": 10, "wake_max_extensions_minutes": 20}
        )
        now = time.time()
        _sleepy(plugin, now)
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        cap = time.time() + 20 * 60.0
        for _ in range(5):
            plugin._state.at_wake_until = time.time() + 10 * 60.0
            await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        assert plugin._state.at_wake_until <= cap + 2.0

        _m2, frozen, _host2 = _make_plugin(simulation={"wake_extend_on_message": False})
        now = time.time()
        _sleepy(frozen, now)
        await frozen.note_session(message={"session_id": "group-1", "is_at": True})
        frozen._state.at_wake_until = time.time() + 60.0
        value = frozen._state.at_wake_until
        await frozen.note_session(
            message={"session_id": "group-1", "processed_plain_text": "继续说"}
        )
        assert frozen._state.at_wake_until == pytest.approx(value)

    asyncio.run(run())


def test_grumpy_note_only_when_woken_shortly_after_falling_asleep():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, _host = _make_plugin()
        now = time.time()
        _sleepy(plugin, now)
        plugin._state.sleep_started_at = now - 30 * 60.0
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        assert "刚睡下没多久" in plugin._life_digest()

        _sleepy(plugin, now)
        plugin._state.sleep_started_at = now - 6 * 3600.0
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        digest = plugin._life_digest()
        assert "刚刚被叫醒" in digest and "刚睡下没多久" not in digest

    asyncio.run(run())


def test_nap_is_not_woken_by_at():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, host = _make_plugin(simulation={"wake_on_private": True})
        now = time.time()
        plugin._state.activity = A.NAP
        plugin._state.activity_since = now - 600
        plugin._state.sleep_started_at = now - 600
        plugin._state.continuous_sleep_minutes = 10
        plugin._state.sleep_need = 0.60
        plugin._state.at_wake_until = 0.0
        await plugin.note_session(message={"session_id": "group-1", "is_at": True})
        assert plugin._state.at_wake_until == 0.0
        assert not host.calls_of("frequency.set_adjust")

    asyncio.run(run())


def test_plugin_bedtime_budget_opens_once_then_expires():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, _host = _make_plugin()
        now = time.time()
        state = plugin._state
        state.activity = A.CHATTING
        state.interrupted_from = A.GAME
        state.interrupt_started_at = now - 60
        state.interrupt_until = now + 3600
        state.sleep_need = 0.80
        state.energy = 5.0
        state.awake_minutes_today = 15 * 3600
        state.continuous_awake_minutes = 900
        state.sleep_defer_until = 0.0

        plugin._enforce_and_apply(now, None)
        assert state.activity == A.CHATTING
        deadline = state.sleep_defer_until
        assert deadline == pytest.approx(now + 15 * 60, abs=1.0)

        plugin._enforce_and_apply(now + 60, None)
        assert state.sleep_defer_until == pytest.approx(deadline), "新消息不重置预算"

        state.sleep_defer_until = now + 1.0
        plugin._check_bedtime_budget(now + 120)
        assert state.activity == A.SLEEP, "消息入口同样要兑现截止时刻"
        assert state.sleep_defer_until == 0.0

    asyncio.run(run())


def test_plugin_zero_budget_goes_to_sleep_immediately():
    """回归：预算 0 分钟时，插件必须当场送她入睡（以前会永远「再聊 0 分钟」）。"""

    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, _host = _make_plugin(simulation={"sleep_deferral_minutes": 0})
        now = time.time()
        state = plugin._state
        state.activity = A.CHATTING
        state.interrupted_from = A.GAME
        state.interrupt_started_at = now - 60
        state.interrupt_until = now + 3600
        state.sleep_need = 0.80
        state.awake_minutes_today = 15 * 3600
        state.continuous_awake_minutes = 900
        state.sleep_defer_until = 0.0

        plugin._enforce_and_apply(now, None)
        assert state.activity == A.SLEEP
        assert state.sleep_defer_until == 0.0

    asyncio.run(run())


def test_plugin_natural_wake_arms_dream_and_catchup():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, _host = _make_plugin()
        now = time.time()
        state = plugin._state
        state.activity = A.SLEEP
        state.activity_since = now - 480 * 60
        state.sleep_started_at = now - 480 * 60
        state.continuous_sleep_minutes = 480
        state.sleep_need = 0.10
        state.energy = 9.0
        state.energy_cap = 10.0
        state.sleep_defer_until = now + 600

        plugin._enforce_and_apply(now, None)
        assert state.activity == A.DAZE
        assert state.wake_grace_until > now
        assert plugin._dream_wake_pending is not None
        assert plugin._catchup_pending_wake is True
        assert state.sleep_defer_until == 0.0

        plugin._enforce_and_apply(now + 60, None)
        assert state.activity == A.DAZE, "宽限窗内不许被塞回床"

        state.wake_grace_until = now - 1.0
        plugin._enforce_and_apply(now + 120, None)
        assert state.wake_grace_until == 0.0

    asyncio.run(run())


def test_plugin_abnormal_wake_arms_catchup_but_not_dream():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, _host = _make_plugin()
        now = time.time()
        state = plugin._state
        state.activity = A.SLEEP
        state.activity_since = now - 800 * 60
        state.sleep_started_at = now - 800 * 60
        state.continuous_sleep_minutes = 800
        state.sleep_need = 0.90
        state.energy = 1.0

        plugin._enforce_and_apply(now, None)
        assert state.activity == A.DAZE
        assert plugin._dream_wake_pending is None, "异常叫醒不是自然醒"
        assert plugin._catchup_pending_wake is True

    asyncio.run(run())


def test_plugin_nap_wake_does_not_dream():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _m, plugin, _host = _make_plugin()
        now = time.time()
        plugin._state.activity = A.NAP
        plugin._state.activity_since = now - 95 * 60
        plugin._state.sleep_started_at = now - 95 * 60
        plugin._state.continuous_sleep_minutes = 95
        plugin._state.sleep_need = 0.50
        plugin._enforce_and_apply(now, None)
        assert plugin._state.activity != A.NAP, "到上限该醒"
        assert plugin._dream_wake_pending is None, "小睡不做梦"

    asyncio.run(run())


def test_sleep_interval_governs_the_call_gate():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")
    _m, plugin, _host = _make_plugin()
    now = 1_700_000_000.0

    plugin.config.activity.llm.sleep_interval_seconds = 1800
    plugin._state.activity = A.SLEEP
    plugin._last_llm_attempt_at = now - 700
    assert plugin._llm_ready(now) is False
    plugin._last_llm_attempt_at = now - 2000
    assert plugin._llm_ready(now) is True

    plugin.config.activity.llm.sleep_interval_seconds = 0
    plugin._last_llm_attempt_at = now - 10_000
    assert plugin._llm_ready(now) is False, "0 = 睡着时完全不问模型"

    plugin._state.activity = A.DAILY
    assert plugin._llm_ready(now) is True, "醒着仍按 min_interval 走"


def test_deterministic_wake_without_any_model():
    """睡眠期完全不问模型：醒来仍由困意恢复保证。"""

    config = quiet_cfg(tick_seconds=600, min_sleep_minutes=180)
    start = ts(2026, 3, 2, 3, 0)
    state = make_state(at=start, activity=A.SLEEP, sleep_started_at=start,
                       continuous_sleep_minutes=0, sleep_need=0.70, energy=3.5)
    now = start
    woke = None
    for _ in range(24 * 12):
        now += 300
        state = S.settle(state, now=now, config=config, events=(), rng=random.Random(0))
        state = S.enforce_and_apply(state, now=now, config=config, decision=None)
        if A.is_awake(state.activity) and state.continuous_sleep_minutes == 0:
            woke = now
            break

    assert woke is not None, "没有任何模型参与时她也必须自然醒"
    slept = (woke - start) / 3600.0
    assert 8.5 <= slept <= 10.0, f"从 0.70 睡到 0.16 约需 9.3 小时，实测 {slept:.2f}"


def test_persistent_model_failure_still_settles_into_a_rhythm():
    """模型连续三天不给输出：作息仍然成立，也不出现高频开关。"""

    config = quiet_cfg(tick_seconds=600)
    start = ts(2026, 3, 1, 12, 0)
    state = S.new_state(now=start, config=config, energy=5.0)
    now = start
    prev = state.activity
    switches = 0
    sleeps = []
    prev_start = start
    for _ in range(3 * 24 * 6):
        now += 600
        state = S.settle(state, now=now, config=config, events=(), rng=random.Random(9))
        state = S.enforce_and_apply(state, now=now, config=config, decision=None)
        if state.activity != prev:
            switches += 1
            if prev == A.SLEEP:
                sleeps.append((now - prev_start) / 3600.0)
            if state.activity == A.SLEEP:
                prev_start = now
            prev = state.activity

    assert len(sleeps) == 3, sleeps
    assert all(6.0 <= hours <= 9.5 for hours in sleeps), sleeps
    assert switches <= 10


# ================================================================ 十四、多日多 seed
# 清单第九部分：多日、多 seed 的动力学验收。

@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_five_day_simulation_with_one_sleep_per_day(seed):
    config = quiet_cfg(tick_seconds=600)
    start = ts(2026, 3, 1, 12, 0)
    state = S.new_state(now=start, config=config, energy=5.0)
    now = start
    rng = random.Random(seed)
    prev = state.activity
    prev_start = start
    sleeps = []
    for _ in range(5 * 24 * 6):
        now += 600
        state = S.settle(state, now=now, config=config, events=(), rng=rng)
        state = S.enforce_and_apply(state, now=now, config=config, decision=None)
        if state.activity != prev:
            if prev == A.SLEEP:
                sleeps.append((now - prev_start) / 3600.0)
            if state.activity == A.SLEEP:
                prev_start = now
            prev = state.activity

    assert len(sleeps) == 5, f"5 天应恰好 5 觉，实测 {len(sleeps)}"
    assert all(6.0 <= hours <= 9.5 for hours in sleeps), sleeps
    assert state.sleep_debt_nights == 0
    assert S.sleep_deficit_minutes(state, now=now, config=config) == (0, "")
    assert state.energy_cap == pytest.approx(10.0)


def test_no_ten_minute_sleep_wake_loop_in_a_full_day():
    """旧模型最刺眼的症状：一整天里几十段 10 分钟的假觉。"""

    config = quiet_cfg(tick_seconds=600)
    start = ts(2026, 3, 1, 12, 0)
    state = S.new_state(now=start, config=config, energy=5.0)
    now = start
    prev = state.activity
    prev_start = start
    spans = []
    for _ in range(24 * 6):
        now += 600
        state = S.settle(state, now=now, config=config, events=(), rng=random.Random(11))
        state = S.enforce_and_apply(state, now=now, config=config, decision=None)
        if state.activity != prev:
            if prev in (A.SLEEP, A.NAP):
                spans.append((now - prev_start) / 60.0)
            if state.activity in (A.SLEEP, A.NAP):
                prev_start = now
            prev = state.activity

    assert len(spans) == 1, spans
    assert all(minutes >= config.min_sleep_minutes for minutes in spans), spans


def test_request_is_pointless_is_honest_across_all_activities():
    p = policy()
    cases = (
        _facts_with_need(minutes_in_sleep=400, sleep_need=0.70),
        _chatting_facts(),
        _chatting_facts(sleep_defer_active=True),
        facts(sleep_need=0.80),
        facts(),
    )
    for case in cases:
        reason = A.request_is_pointless(case, p)
        baseline = A.enforce(case, None, p).activity
        collapsed = all(
            A.enforce(case, A.ActivityDecision(activity, note="t"), p).activity == baseline
            for activity in A.ALLOWED_ACTIVITIES
        )
        assert collapsed == bool(reason), (case.activity, reason)
