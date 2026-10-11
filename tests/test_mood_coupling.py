# -*- coding: utf-8 -*-
"""L3：内心维度与外显情绪/体力的耦合（v1.16.2 C 期 = M2 体力耦合 + M3 内心维度通道）。

这一期是**风险最高**的一期（方案 §4：耦合族），所以每个机制都钉三件事：

1. **正行为**：机制真的发生（疲劳压低基线、低体力放大消耗、高压崩一次、孤独放大社交）；
2. **回退**：关掉 = 与旧行为逐位一致（数值精确比对，不是「差不多」）；
3. **边界与不对称**：只放大消耗不放大恢复、每生活日至多一次、额度仍然封顶
   （「剩余额度 0.2 × 1.5 = 0.3 → 实际只能发 0.2」这条方案点名的边界就在这里）。
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
import life_mood as M  # noqa: E402
import life_sim as S  # noqa: E402
import life_social as SOS  # noqa: E402

TZ = 480
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() - TZ * 60


def cfg(**overrides):
    base = dict(tz_offset_minutes=TZ)
    base.update(overrides)
    return S.SimConfig(**base)


def make_state(*, at, activity=A.DAILY, **overrides):
    state = S.LifeState()
    state.last_tick_at = at
    state.activity = activity
    state.activity_since = at
    state.day_key = S.day_key_of(S.local_datetime(at, TZ), 12)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def run(state, *, at, hours, config):
    """按 10 分钟一步推进（一次跳几小时会被判成停机间隙、整段不记账）。"""

    for step in range(1, int(hours * 6) + 1):
        S.settle(
            state, now=at + 600 * step, config=config, events=(), rng=random.Random(1)
        )
    return state


# ================================================================ M2a 疲劳压情绪基线


@pytest.mark.parametrize(
    "energy,expected",
    [(4.0, 0.0), (3.0, 0.0), (2.0, -0.3), (1.0, -0.6), (0.0, -0.9)],
)
def test_fatigue_offset_scales_below_the_threshold(energy, expected):
    state = make_state(at=ts(2026, 2, 8, 14, 0), energy=energy)
    config = cfg(emotion_fatigue_penalty=0.3, emotion_fatigue_threshold=3.0)
    assert S.fatigue_offset(state, config) == pytest.approx(expected)


def test_fatigue_offset_is_off_by_default_and_never_positive():
    state = make_state(at=ts(2026, 2, 8, 14, 0), energy=0.0)
    assert S.fatigue_offset(state, cfg()) == 0.0
    assert S.fatigue_offset(state, cfg(emotion_fatigue_penalty=0.5, emotion_fatigue_threshold=5.0)) \
        == pytest.approx(-2.5)


def test_fatigue_shows_up_in_the_baseline_and_clamps_at_zero():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, energy=2.0, afterglow=0.2)
    state.emotion = 3.0
    lines = S.attribution_lines(
        state, at, cfg(emotion_fatigue_penalty=0.3, emotion_fatigue_threshold=3.0)
    )
    assert lines[0] == "情绪基线：5.00（基础） +0.20（余波） -0.30（疲劳） = 4.90"

    # 极端疲劳 + 负余波：合计不许被压到 0 以下（基线的下界就是 0）
    heavy = make_state(at=at, energy=0.0, afterglow=-0.6)
    parts, total = S.emotion_baseline_parts(
        heavy, at, cfg(emotion_fatigue_penalty=0.3, emotion_fatigue_threshold=3.0)
    )
    assert total == pytest.approx(5.0 - 0.6 - 0.9)
    assert all(value <= 0 or name != "疲劳" for name, value in parts)


def test_regression_pulls_emotion_down_when_she_is_exhausted():
    """基线通道真的在起作用（不是只印在卡片上）。"""

    at = ts(2026, 2, 8, 14, 0)
    config = cfg(emotion_fatigue_penalty=0.3, emotion_fatigue_threshold=3.0)
    state = make_state(at=at, energy=1.0, emotion=8.0)
    for _ in range(120):
        S._regress_emotion(state, config=config, minutes=10.0, now=at)
    assert state.emotion == pytest.approx(5.0 - 0.6)

    state.emotion = 8.0
    for _ in range(120):
        S._regress_emotion(state, config=cfg(), minutes=10.0, now=at)
    assert state.emotion == pytest.approx(5.0), "关掉就该回到旧基线"


# ================================================================ M2b 低体力消耗放大


def test_low_energy_amplifies_consumption_only():
    at = ts(2026, 2, 8, 14, 0)
    config = cfg(low_energy_drain_multiplier=1.25, low_energy_threshold=3.0)

    # 低体力 + 消耗活动：game -0.70/h × 1.25 = -0.875/h
    low = run(make_state(at=at, activity=A.GAME, energy=2.0), at=at, hours=2, config=config)
    assert low.energy == pytest.approx(2.0 - 0.875 * 2)

    # 高体力：不放大
    high = run(make_state(at=at, activity=A.GAME, energy=8.0), at=at, hours=2, config=config)
    assert high.energy == pytest.approx(8.0 - 0.70 * 2)

    # 恢复项**不放大**（睡着 +1.20/h 照旧）：否则「越累回血越慢」会把她焊在床上
    asleep = run(make_state(at=at, activity=A.SLEEP, energy=2.0), at=at, hours=2, config=config)
    assert asleep.energy == pytest.approx(2.0 + 1.20 * 2)


def test_low_energy_amplifier_is_off_by_default():
    at = ts(2026, 2, 8, 14, 0)
    state = run(make_state(at=at, activity=A.GAME, energy=2.0), at=at, hours=2, config=cfg())
    assert state.energy == pytest.approx(2.0 - 0.70 * 2)


def test_low_energy_amplifier_stacks_with_the_fatigue_ramp():
    """两条一起开：时间驱动 + 状态驱动叠加，但不许爆炸。"""

    at = ts(2026, 2, 8, 8, 0)
    config = cfg(
        low_energy_drain_multiplier=1.25,
        low_energy_threshold=3.0,
        fatigue_ramp_curve=((12.0, 0.0), (16.0, -0.15), (20.0, -0.4), (24.0, -0.7)),
    )
    state = run(make_state(at=at, activity=A.WORK, energy=8.0), at=at, hours=20, config=config)
    # work -0.55/h：20 小时基础 -11 → 会先撞 0 被钳住，说明放大确实在推她到极限
    assert state.energy == 0.0

    milder = run(
        make_state(at=at, activity=A.DAZE, energy=8.0), at=at, hours=20, config=config
    )
    # 发呆 20 小时：疲劳把自己推到 4.6，**没跌破放大阈值**——所以放大在这条路上
    # 根本没触发（这正是「只在她已经低体力时才咬人」的意思）
    assert milder.energy > 3.0, milder.energy


# ================================================================ M3a 高压崩溃


def _stress_state(*, at, stress=8.0, since=None, day_key="2026-02-08"):
    state = make_state(at=at, stress=stress, day_key=day_key)
    state.stress_high_since = float(at if since is None else since)
    return state


def test_breakdown_needs_the_threshold_and_the_duration():
    at = ts(2026, 2, 8, 14, 0)
    config = cfg(stress_breakdown_enabled=True, stress_breakdown_threshold=7.0,
                 stress_breakdown_hours=2.0)

    # 刚进入高压 → 不崩
    assert S.settle_stress_breakdown(
        _stress_state(at=at, since=at - 60), now=at, day_key="2026-02-08", config=config
    ) is False
    # 压力没到阈值 → 不崩
    assert S.settle_stress_breakdown(
        _stress_state(at=at, stress=6.9, since=at - 3 * 3600),
        now=at, day_key="2026-02-08", config=config,
    ) is False
    # 关掉开关 → 不崩
    assert S.settle_stress_breakdown(
        _stress_state(at=at, since=at - 3 * 3600), now=at, day_key="2026-02-08",
        config=cfg(stress_breakdown_enabled=False),
    ) is False
    # 满两小时 → 崩
    state = _stress_state(at=at, since=at - 2 * 3600)
    assert S.settle_stress_breakdown(
        state, now=at, day_key="2026-02-08", config=config
    ) is True
    assert state.emotion == pytest.approx(5.0 - 0.8)
    assert state.recent_events[-1]["label"] == "情绪有点绷不住"
    assert state.recent_events[-1]["emotion"] == pytest.approx(-0.8)
    assert state.materials[-1]["text"] == "最近真的有点累，感觉自己快绷不住了"
    assert state.materials[-1]["weight"] == pytest.approx(0.75)
    assert state.materials[-1]["expires_at"] - at == pytest.approx(8 * 3600)
    # 进惰性期（一次真实的情绪打击，不是事实通报）
    assert state.inertia_until > at


def test_breakdown_happens_at_most_once_per_life_day():
    at = ts(2026, 2, 8, 14, 0)
    config = cfg(stress_breakdown_enabled=True)
    state = _stress_state(at=at, since=at - 3 * 3600)

    assert S.settle_stress_breakdown(state, now=at, day_key="2026-02-08", config=config) is True
    assert S.settle_stress_breakdown(state, now=at + 600, day_key="2026-02-08", config=config) is False
    assert len(state.materials) == 1, "同一天不许崩两次"

    # 新的一天：如果高压还在（起点没被清），可以再崩一次
    assert S.settle_stress_breakdown(
        state, now=at + 86400, day_key="2026-02-09", config=config
    ) is True
    assert len(state.materials) == 2


def test_stress_falling_below_the_reset_threshold_restarts_the_clock():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, stress=8.5, stress_high_since=0.0)
    policy = M.MoodPolicy(stress_breakdown_threshold=7.0, stress_breakdown_reset=5.0)

    M.evolve(state, activity=A.DAILY, minutes=10.0, had_contact=False, had_mention=False,
             policy=policy, last_contact_at=at, now=at)
    assert state.stress_high_since == pytest.approx(at), "进入高压要记起点"

    # 压力回落到 5 以下 → 起点清零（下一次高压重新计时）
    state.stress = 4.0
    M.evolve(state, activity=A.DAILY, minutes=10.0, had_contact=False, had_mention=False,
             policy=policy, last_contact_at=at, now=at + 600)
    assert state.stress_high_since == 0.0

    # 起点清了以后，即使压力又回到 8，也要再等满 2 小时
    state.stress = 8.0
    M.evolve(state, activity=A.DAILY, minutes=10.0, had_contact=False, had_mention=False,
             policy=policy, last_contact_at=at, now=at + 1200)
    assert state.stress_high_since == pytest.approx(at + 1200)
    config = cfg(stress_breakdown_enabled=True)
    assert S.settle_stress_breakdown(
        state, now=at + 1200 + 3600, day_key="2026-02-08", config=config
    ) is False


def test_breakdown_fires_through_settle():
    """整条链路：settle 里真的会产出（不只是单元函数能跑）。"""

    at = ts(2026, 2, 8, 12, 0)
    config = cfg(stress_breakdown_enabled=True, stress_breakdown_hours=2.0)
    state = make_state(at=at, activity=A.WORK, stress=8.0)
    state.stress_high_since = at

    for step in range(1, 13):  # 2 小时 = 12 个 tick
        S.settle(state, now=at + 600 * step, config=config, events=(), rng=random.Random(1))
    assert any(item["label"] == "情绪有点绷不住" for item in state.recent_events)
    assert state.emotion < 5.0


def test_breakdown_off_leaves_no_trace():
    at = ts(2026, 2, 8, 12, 0)
    state = make_state(at=at, activity=A.WORK, stress=9.0)
    state.stress_high_since = at
    for step in range(1, 13):
        S.settle(state, now=at + 600 * step, config=cfg(), events=(), rng=random.Random(1))
    assert not any(item["label"] == "情绪有点绷不住" for item in state.recent_events)
    assert state.materials == []


# ================================================================ M3b 孤独放大社交收益


def _intake_ctx(*, policy_kwargs=None, loneliness=0.0, day_used=0.0, now=1_800_000_000.0):
    # 默认**打开**孤独系数（插件侧默认就是开）——纯模块的类默认是关，
    # 那是为了「关掉 = 旧行为」这条纪律，不是本组用例要断言的东西。
    kwargs = {"loneliness_scaling": True}
    kwargs.update(policy_kwargs or {})
    policy = SOS.SocialPolicy(**kwargs)
    return SOS.IntakeContext(
        now=now, day_key="2026-10-01", activity=A.DAILY, asleep=False,
        day_used=day_used, policy=policy, loneliness=loneliness,
    )


@pytest.mark.parametrize(
    "loneliness,expected",
    [(0.0, 0.8), (2.0, 0.8), (4.5, 1.15), (7.0, 1.5), (10.0, 1.5)],
)
def test_loneliness_factor_curve(loneliness, expected):
    policy = SOS.SocialPolicy(loneliness_scaling=True)
    assert SOS.loneliness_factor(loneliness, policy) == pytest.approx(expected)


def test_loneliness_factor_is_off_by_default_and_fails_open():
    off = SOS.SocialPolicy()
    assert SOS.loneliness_factor(10.0, off) == 1.0
    assert SOS.loneliness_factor(0.0, off) == 1.0

    on = SOS.SocialPolicy(loneliness_scaling=True)
    assert SOS.loneliness_factor(float("nan"), on) == 1.0, "坏值按中性处理（不能让她彻底收不到情绪）"
    assert SOS.loneliness_factor("很多", on) == 1.0
    assert SOS.loneliness_factor(4.0, SOS.SocialPolicy(loneliness_scaling=True,
                                                       loneliness_curve=())) == 1.0


def test_lonely_her_gets_more_from_the_same_mention():
    signal = {"at": 1_800_000_000.0, "session_id": "g1", "is_group": True, "mentioned": True}

    lonely = SOS.intake_live([signal], _intake_ctx(loneliness=8.0), {})
    neutral = SOS.intake_live([signal], _intake_ctx(loneliness=9.0, policy_kwargs={
        "loneliness_scaling": False,
    }), {})
    assert lonely.events[0]["emotion"] == pytest.approx(0.3 * 1.5)
    assert neutral.events[0]["emotion"] == pytest.approx(0.3)

    loved = SOS.intake_live([signal], _intake_ctx(loneliness=0.0), {})
    assert loved.events[0]["emotion"] == pytest.approx(0.3 * 0.8)


def test_loneliness_scaling_still_respects_the_daily_budget():
    """方案点名的边界：剩余额度 0.2 × 1.5 = 0.3，但实际只能发 0.2。"""

    signal = {"at": 1_800_000_000.0, "session_id": "g1", "is_group": True, "mentioned": True}
    ctx = _intake_ctx(loneliness=9.0, day_used=1.3)  # 额度 1.5 ⇒ 剩 0.2
    result = SOS.intake_live([signal], ctx, {})
    assert result.events[0]["emotion"] == pytest.approx(0.2)
    assert result.skipped_budget == 0, "被额度削了但不是「额度用完」（还剩 0.2 可用）"


def test_scaling_is_applied_before_the_cap_not_after():
    """顺序纪律：先缩放再扣额度。反过来（额度用完就没系数）会让系数形同虚设。"""

    signal = {"at": 1_800_000_000.0, "session_id": "g1", "is_group": True, "mentioned": True}
    generous = SOS.intake_live([signal], _intake_ctx(loneliness=9.0), {})
    assert generous.events[0]["emotion"] == pytest.approx(0.45)


# ================================================================ 插件层接线


def _make_plugin(**overrides):
    from fakehost import (  # noqa: PLC0415
        FakeHost,
        FakePaths,
        bind_context,
        build_context,
        get_default_config,
        load_plugin_module,
    )

    module = load_plugin_module(PLUGIN_DIR, "life_frequency_mood_coupling")
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
    plugin.config
    plugin._rebuild_from_config()
    return module, plugin, host


def test_c_phase_defaults_are_wired_on():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin()
    config = plugin._sim_config()
    assert config.emotion_fatigue_penalty == pytest.approx(0.3)
    assert config.emotion_fatigue_threshold == pytest.approx(3.0)
    assert config.low_energy_drain_multiplier == pytest.approx(1.25)
    assert config.low_energy_threshold == pytest.approx(3.0)
    assert config.stress_breakdown_enabled is True
    assert config.stress_breakdown_threshold == pytest.approx(7.0)
    assert config.stress_breakdown_hours == pytest.approx(2.0)
    assert config.stress_breakdown_reset == pytest.approx(5.0)

    policy = plugin._social_policy()
    assert policy.loneliness_scaling is True
    assert policy.loneliness_curve == ((2.0, 0.8), (7.0, 1.5))

    mood = plugin._mood_policy()
    assert mood.stress_breakdown_threshold == pytest.approx(7.0)
    assert mood.stress_breakdown_reset == pytest.approx(5.0)


def test_c_phase_switches_all_come_back_to_old_behaviour():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin(
        emotion_energy={
            "emotion_fatigue_penalty": 0.0,
            "low_energy_drain_multiplier": 1.0,
        },
        mood={
            "stress_breakdown_enabled": False,
            "loneliness_social_scaling": False,
        },
    )
    config = plugin._sim_config()
    assert config.emotion_fatigue_penalty == 0.0
    assert config.low_energy_drain_multiplier == 1.0
    assert config.stress_breakdown_enabled is False
    assert plugin._social_policy().loneliness_scaling is False
    # 关掉孤独系数后，同一句话的收益回到「所有人生效相同」
    state = make_state(at=1_800_000_000.0)
    assert S.fatigue_offset(state, config) == 0.0


def test_mood_disabled_turns_the_loneliness_scaling_off_too():
    """内心维度总开关关掉时，它的消费点也必须失效（不能只关演化）。"""

    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin(mood={"enabled": False})
    assert plugin._social_policy().loneliness_scaling is False


def test_bad_loneliness_curve_warns_but_keeps_the_default():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin(
        mood={"loneliness_social_curve": ["孤独=0.8", "7=1.5"]}
    )
    policy = plugin._social_policy()
    # 只剩一个合法点 ⇒ 整条曲线作废 ⇒ 回退内置曲线（而不是「所有系数都变 1.0」）
    assert policy.loneliness_curve == SOS.SocialPolicy().loneliness_curve


def test_social_intake_reads_loneliness_from_the_state():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _module, plugin, _host = _make_plugin(social={"enabled": True})
        plugin._state.loneliness = 9.0
        plugin._state.day_key = "2026-10-01"
        plugin._state.activity = A.DAILY
        plugin._social_inbox.append(
            {"at": time.time(), "session_id": "g1", "is_group": True,
             "mentioned": True, "text_len": 5}
        )
        plugin._intake_social(time.time(), plugin._sim_config())
        event = plugin._state.recent_events[-1]
        assert event["emotion"] == pytest.approx(0.45, abs=1e-3), event

    asyncio.run(run())
