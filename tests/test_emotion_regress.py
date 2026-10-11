# -*- coding: utf-8 -*-
"""L3：情绪回归与基线（v1.16.1 B 期 = M5 比例回归/惯性缩放 + M4 日内节律/余波衰减）。

三条纪律在这里落地成用例：

1. **关掉 = 与旧行为逐位一致**：`recover_ratio_per_tick = 0` / `inertia_scale_enabled = False`
   / `afterglow_decay = False` / `baseline_diurnal_curve = ()` 四条回退路径都有用例，
   其中线性回归那条是 `==` 精确比对（不是 approx）。
2. **提示词与卡片不许比实现旧**：归因输出里的回归描述必须跟着模式变。
3. **数字来自实测**：文档里「daze 20 小时 < 3.0」「29 tick 消除 95%」两处算术与它自己
   给的参数不一致，这里按**实测**写断言，并把真实数字写进 README（见版本历史 1.16.1）。
"""

import asyncio
import math
import pathlib
import random
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_activity as A  # noqa: E402
import life_events as E  # noqa: E402
import life_sim as S  # noqa: E402

TZ = 480
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent

#: 默认曲线的解析形态（plugin.DEFAULT_DIURNAL_CURVE_LINES 的点集）
DIURNAL = ((300.0, -0.3), (780.0, 0.15), (1200.0, 0.3), (1380.0, 0.0))
RATIO = 0.08


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


def tick(state, config, *, times=1, activity=None):
    """按一个 tick 步进一次回归（不跑 settle，好把回归单独隔离出来）。"""

    if activity is not None:
        state.activity = activity
    for _ in range(times):
        S._regress_emotion(
            state,
            config=config,
            minutes=max(1.0, config.tick_seconds / 60.0),
            now=state.last_tick_at,
        )
    return state.emotion


# ================================================================ M5a 比例回归


def test_proportional_regression_is_fast_first_then_has_a_long_tail():
    """「爆发快消、余味长」——初期比线性快，尾巴比线性长。"""

    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, emotion=10.0, afterglow=0.0)
    config = cfg(recover_ratio_per_tick=RATIO, sleep_recover_multiplier=1.0)

    first = tick(state, config)
    assert first == pytest.approx(10.0 - RATIO * 5.0, abs=1e-9), "第一个 tick 消除 8% 的差距"

    steps: list[float] = []
    previous = first
    for _ in range(5):
        now_value = tick(state, config)
        steps.append(previous - now_value)
        previous = now_value
    assert all(later <= earlier + 1e-12 for earlier, later in zip(steps, steps[1:])), steps
    assert steps[0] > 0.2, f"初期必须比旧的线性 0.2/tick 快（实际 {steps[0]:.3f}）"

    # 63% 的差距：比例回归 12 个 tick，旧线性要 16 个 tick
    ratio_ticks = _ticks_to_close(RATIO, target_ratio=0.63)
    linear_ticks = math.ceil(5.0 * 0.63 / 0.2)
    assert ratio_ticks == 12 and ratio_ticks < linear_ticks

    # 尾巴：比例回归收敛到基线要比线性更久（余味长）
    assert _ticks_to_close(RATIO, target_ratio=0.95) > 5.0 * 0.95 / 0.2


def _ticks_to_close(ratio: float, *, target_ratio: float, gap: float = 5.0,
                    floor_step: float = 0.05) -> int:
    """从 ``gap`` 出发、按比例回归（含最小步长）消除 ``target_ratio`` 差距要几个 tick。"""

    remaining = gap
    for count in range(1, 1000):
        step = max(remaining * ratio, floor_step)
        remaining = max(0.0, remaining - step)
        if remaining <= gap * (1.0 - target_ratio):
            return count
    raise AssertionError("没收敛")


def test_regression_reaches_the_baseline_and_never_overshoots():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, emotion=0.0, afterglow=0.0)
    config = cfg(recover_ratio_per_tick=RATIO)
    for _ in range(200):
        tick(state, config)
    assert state.emotion == pytest.approx(5.0, abs=1e-9)

    state.emotion = 10.0
    tick(state, config, times=500)
    assert state.emotion == pytest.approx(5.0, abs=1e-9), "不许冲过基线"


def test_linear_regression_is_bit_for_bit_the_old_behaviour():
    """``recover_ratio_per_tick = 0`` 必须与 v1.15.0 的线性实现**逐位一致**。"""

    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, emotion=9.7, afterglow=0.13)
    config = cfg(recover_ratio_per_tick=0.0)
    mirror = make_state(at=at, emotion=9.7, afterglow=0.13)

    for _ in range(20):
        tick(state, config)
        # 旧实现的字面复刻（顺序也要一样：先乘 steps 再乘睡眠倍数）
        rate = float(config.recover_per_tick) * 1.0
        baseline = S._clamp(
            float(config.baseline_emotion) + float(mirror.afterglow), 0.0, config.emotion_max
        )
        delta = baseline - mirror.emotion
        if abs(delta) <= rate:
            mirror.emotion = baseline
        else:
            mirror.emotion += rate if delta > 0 else -rate
        assert state.emotion == mirror.emotion, "线性回退路径必须逐位一致"


def test_sleep_multiplier_applies_to_the_ratio_too():
    at = ts(2026, 2, 8, 20, 0)
    awake = make_state(at=at, emotion=10.0, afterglow=0.0)
    asleep = make_state(at=at, emotion=10.0, afterglow=0.0, activity=A.SLEEP)
    config = cfg(recover_ratio_per_tick=RATIO, sleep_recover_multiplier=2.0)

    awake_step = 10.0 - tick(awake, config)
    asleep_step = 10.0 - tick(asleep, config)
    assert asleep_step == pytest.approx(awake_step * 2.0, rel=1e-9)


def test_min_step_keeps_the_tail_converging():
    at = ts(2026, 2, 8, 20, 0)
    # 距基线 0.2：比例步长只有 0.016，靠最小步长 0.05 才走得干净
    floored = make_state(at=at, emotion=5.2, afterglow=0.0)
    tick(floored, cfg(recover_ratio_per_tick=RATIO, recover_min_step=0.05))
    assert floored.emotion == pytest.approx(5.15)

    tiny = make_state(at=at, emotion=5.2, afterglow=0.0)
    tick(tiny, cfg(recover_ratio_per_tick=RATIO, recover_min_step=0.0))
    assert tiny.emotion == pytest.approx(5.2 - 0.016)

    # 0.016 的步长在 float64 上还是会走，但慢得多——这就是「尾巴」的由来
    assert floored.emotion < tiny.emotion


# ================================================================ M5b 惯性缩放


@pytest.mark.parametrize(
    "delta,minutes",
    [(-0.3, 12.0), (0.5, 20.0), (1.5, 60.0), (2.0, 80.0), (2.5, 90.0), (9.0, 90.0),
     (0.05, 5.0), (0.0, 5.0)],
)
def test_inertia_scales_with_the_impact_and_is_clamped(delta, minutes):
    config = cfg(inertia_scale_enabled=True)
    assert S.scaled_inertia(delta, config) == pytest.approx(minutes * 60.0)


def test_inertia_scaling_can_be_switched_off():
    config = cfg(inertia_scale_enabled=False)
    for delta in (-0.3, 1.5, 9.0):
        assert S.scaled_inertia(delta, config) == pytest.approx(40 * 60.0)

    zero_base = cfg(inertia_scale_enabled=True, inertia_minutes=0)
    assert S.scaled_inertia(2.0, zero_base) == 0.0


def test_inertia_cap_never_shortens_a_longer_configured_inertia():
    """把基础惯性期配得比上限还长时，缩放不该反而把惯性缩短。"""

    config = cfg(inertia_scale_enabled=True, inertia_minutes=240)
    assert S.scaled_inertia(0.5, config) == pytest.approx(120.0 * 60.0)
    assert S.scaled_inertia(9.0, config) == pytest.approx(240.0 * 60.0)


def test_events_and_date_rules_share_the_scaled_inertia():
    at = ts(2026, 2, 8, 20, 0)
    config = cfg(inertia_scale_enabled=True)

    state = make_state(at=at)
    S._apply_event(
        state,
        E.LifeEvent(label="笔没水了", activities=(A.DAILY,), emotion=-0.3, energy=0.0),
        now=at, config=config, rng=random.Random(1),
    )
    assert state.inertia_until == pytest.approx(at + 12 * 60.0)

    # 日期规则（生日类大冲击）走同一个 helper：1.5 × 40 = 60 分钟
    festival = S.FestivalRule(name="生日", month=2, day=8, emotion=1.5)
    dated = make_state(at=at)
    S._fire_date_rules(dated, local_dt=S.local_datetime(at, TZ), now=at,
                       config=cfg(inertia_scale_enabled=True, festivals=(festival,)))
    assert dated.inertia_until == pytest.approx(at + 60 * 60.0)


# ================================================================ M4a 日内节律


@pytest.mark.parametrize(
    "hour,minute,expected",
    [(5, 0, -0.3), (13, 0, 0.15), (20, 0, 0.3), (23, 0, 0.0),
     (2, 0, -0.3),  # 第一个点之前取端点值（凌晨是低谷的延续）
     (9, 0, -0.075)],  # 05:00 → 13:00 的中点
)
def test_diurnal_curve_takes_values_by_local_clock(hour, minute, expected):
    at = ts(2026, 2, 8, hour, minute)
    assert S.diurnal_offset(at, cfg(baseline_diurnal_curve=DIURNAL)) == pytest.approx(
        expected, abs=1e-9
    )


def test_diurnal_curve_is_continuous_across_midnight():
    """曲线按 24 小时闭环：午夜不许出现台阶（23:59 与 00:01 只差几分钟的分量）。"""

    late = S.diurnal_offset(ts(2026, 2, 8, 23, 59), cfg(baseline_diurnal_curve=DIURNAL))
    early = S.diurnal_offset(ts(2026, 2, 9, 0, 1), cfg(baseline_diurnal_curve=DIURNAL))
    assert abs(late - early) < 0.01, (late, early)
    assert early == pytest.approx(-0.3, abs=0.01)


def test_empty_diurnal_curve_is_off():
    at = ts(2026, 2, 8, 5, 0)
    assert S.diurnal_offset(at, cfg()) == 0.0
    assert S.emotion_baseline_parts(make_state(at=at), at, cfg())[1] == 5.0


def test_diurnal_shows_up_in_the_baseline_composition():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, afterglow=0.2)
    lines = S.attribution_lines(state, at, cfg(baseline_diurnal_curve=DIURNAL))
    assert lines[0] == "情绪基线：5.00（基础） +0.20（余波） +0.30（节律） = 5.50"


def test_baseline_composition_matches_what_regression_converges_to():
    """对账（含节律）：卡片上的合计就是回归真正收敛到的那个数。"""

    at = ts(2026, 2, 8, 6, 0)
    config = cfg(baseline_diurnal_curve=DIURNAL, recover_ratio_per_tick=RATIO,
                 inertia_scale_enabled=True)
    state = make_state(at=at, emotion=2.0, afterglow=0.0)
    for step in range(1, 37):
        S.settle(state, now=at + 600 * step, config=config, events=(), rng=random.Random(1))

    now = at + 600 * 36
    _, expected = S.emotion_baseline_parts(state, now, config)
    assert abs(state.emotion - expected) < 0.1, (state.emotion, expected)


# ================================================================ M4b 余波衰减


def test_afterglow_decay_weights_by_age_and_never_jumps():
    config = cfg(afterglow_decay=True, afterglow_span_hours=24.0)
    assert S.afterglow_weight(0.0, config) == pytest.approx(1.0)
    assert S.afterglow_weight(3600.0, config) == pytest.approx(1.0 - 1 / 24)
    # 23 小时前的贡献不到 1 小时前的 10%（方案 §3 M4b 的验收线）
    assert S.afterglow_weight(23 * 3600.0, config) < 0.1 * S.afterglow_weight(3600.0, config)
    assert S.afterglow_weight(24 * 3600.0, config) == pytest.approx(0.0)
    assert S.afterglow_weight(24 * 3600.0 + 1.0, config) == 0.0


def test_equal_weight_is_still_available_and_doubles_the_total():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, recent_events=[
        {"at": at - 3600.0, "label": "近事", "emotion": 1.0},
        {"at": at - 23 * 3600.0, "label": "旧事", "emotion": 1.0},
    ])

    S._recompute_afterglow(state, at, cfg(afterglow_decay=False, afterglow_gain=0.15))
    assert state.afterglow == pytest.approx(2.0 * 0.15)

    # 衰减 + 翻倍系数 ⇒ 维持同等稳态（这正是 M4b 的重标定）
    S._recompute_afterglow(state, at, cfg(afterglow_decay=True, afterglow_gain=0.30))
    assert state.afterglow == pytest.approx(2.0 * 0.15, rel=0.05)

    # 衰减但沿旧系数 ⇒ 余波约减半（说明「翻倍」不是拍脑袋）
    S._recompute_afterglow(state, at, cfg(afterglow_decay=True, afterglow_gain=0.15))
    assert state.afterglow == pytest.approx(2.0 * 0.15 * 0.5, rel=0.05)


def test_attribution_shows_the_decayed_contribution():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, recent_events=[
        {"at": at - 12 * 3600.0, "label": "半天前的事", "emotion": 1.0},
    ])
    lines = S.attribution_lines(state, at, cfg(afterglow_decay=True, afterglow_gain=0.30))
    # 12 小时 = 窗口的一半 ⇒ 贡献 1.0 × 0.5 × 0.30 = +0.15
    assert any("+0.15" in item and "半天前的事" in item for item in lines), lines


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

    module = load_plugin_module(PLUGIN_DIR, "life_frequency_emotion_regress")
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


def test_defaults_wire_the_whole_b_phase_on():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    module, plugin, _host = _make_plugin()
    config = plugin._sim_config()
    assert tuple(module.DEFAULT_DIURNAL_CURVE_LINES) == (
        "300=-0.3", "780=0.15", "1200=0.3", "1380=0",
    )
    assert config.recover_ratio_per_tick == pytest.approx(0.08)
    assert config.recover_min_step == pytest.approx(0.05)
    assert config.inertia_scale_enabled is True
    assert config.afterglow_decay is True
    assert config.afterglow_gain == pytest.approx(0.30), "衰减开 ⇒ 系数自动翻倍"
    assert config.baseline_diurnal_curve == DIURNAL


def test_every_switch_back_to_old_behaviour_is_available():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin(emotion_energy={
        "recover_ratio_per_tick": 0.0,
        "inertia_scale_enabled": False,
        "afterglow_decay": False,
        "baseline_diurnal_curve": [],
    })
    config = plugin._sim_config()
    assert config.recover_ratio_per_tick == 0.0, "0 = 回到线性步长"
    assert config.inertia_scale_enabled is False, "关 = 一律固定惯性期"
    assert config.afterglow_decay is False and config.afterglow_gain == pytest.approx(0.15)
    assert config.baseline_diurnal_curve == ()


def test_explicit_afterglow_gain_wins_over_the_automatic_one():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin(emotion_energy={"afterglow_gain": 0.22})
    assert plugin._sim_config().afterglow_gain == pytest.approx(0.22)


def test_bad_diurnal_lines_warn_but_do_not_crash():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin(
        emotion_energy={"baseline_diurnal_curve": ["早上=-0.3", "780=0.15"]}
    )
    assert plugin._sim_config().baseline_diurnal_curve == ()


def test_attribution_card_switches_to_the_proportional_wording():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        import time

        _module, plugin, _host = _make_plugin()
        now = time.time()
        plugin._state.activity = A.DAILY
        plugin._state.activity_since = now - 600
        plugin._state.emotion = 8.0

        ok, text, _level = await plugin.cmd_life_state(
            matched_groups={"sub": "归因"}, stream_id="group-1", text="/生活 归因"
        )
        assert ok is True
        assert "按比例回归" in text, text
        assert "节律" in text and "情绪基线" in text

    asyncio.run(run())


def test_activity_prompt_discloses_the_diurnal_rhythm_from_config():
    """提示词不许把「基线是条直线」当成事实——节律开着就得说出来，幅度取自曲线。"""

    deltas = {A.SLEEP: 0.0, A.DAILY: -0.5}
    on = "\n".join(S.activity_effect_lines(
        cfg(baseline_diurnal_curve=DIURNAL, recover_ratio_per_tick=RATIO),
        activity_factors=deltas,
    ))
    assert "日内节律" in on and "±0.3" in on
    assert "每 10 分钟消除与基线差距的 8%" in on

    off = "\n".join(S.activity_effect_lines(cfg(), activity_factors=deltas))
    assert "日内节律" not in off
    assert "0.20/10 分钟" in off, "线性模式下仍然只说线性那条"

    weak = "\n".join(S.activity_effect_lines(
        cfg(baseline_diurnal_curve=((300.0, -0.6), (1200.0, 0.6))), activity_factors=deltas
    ))
    assert "±0.6" in weak, "幅度必须来自配置"
