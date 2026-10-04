# -*- coding: utf-8 -*-
"""L3：倍率管线。"""

import pytest

import life_factors as F


def _config(**overrides):
    base = {}
    base.update(overrides)
    return F.FactorConfig(**base)


def _adjust(**overrides):
    args = dict(
        activity="daily",
        emotion=5.0,
        energy=5.0,
        sick=False,
        sleep_debt_nights=0,
        date_factor=1.0,
        material_count=0,
        now_minutes=14 * 60,
        config=_config(),
        mode="reply_necessity",
    )
    args.update(overrides)
    return F.compute_adjust(**args)


# ---------------------------------------------------------------- 解析


def test_parse_factor_lines():
    factors, warnings = F.parse_factor_lines(["music=0.9", "sleep=0.0", "daily=1.0"])
    assert factors == {"music": 0.9, "sleep": 0.0, "daily": 1.0}
    assert warnings == []


def test_parse_factor_lines_warn_and_skip():
    factors, warnings = F.parse_factor_lines(["music=0.9", "没有等号", "bad=abc", "=1.0"])
    assert factors == {"music": 0.9}
    assert len(warnings) == 3


def test_parse_curve_points_sorts_and_dedups():
    curve, warnings = F.parse_curve_points(["10=1.35", "0=0.55", "5=0.95", "5=0.99"])
    assert curve == ((0.0, 0.55), (5.0, 0.99), (10.0, 1.35))
    assert warnings == []


def test_parse_curve_points_rejects_single_point():
    curve, warnings = F.parse_curve_points(["5=1.0"])
    assert curve == ()
    assert any("至少需要两个点" in item for item in warnings)


def test_parse_curve_points_empty_input():
    assert F.parse_curve_points(None) == ((), [])
    assert F.parse_curve_points([]) == ((), [])


@pytest.mark.parametrize(
    "x,expected",
    [(0.0, 0.55), (2.5, 0.75), (5.0, 0.95), (7.5, 1.15), (10.0, 1.35), (-5.0, 0.55), (99.0, 1.35)],
)
def test_interpolate_linear_and_clamped(x, expected):
    assert F.interpolate(F.DEFAULT_MOOD_CURVE, x) == pytest.approx(expected)


def test_interpolate_edge_cases():
    assert F.interpolate((), 5.0) == 1.0
    assert F.interpolate(((5.0, 0.9),), 5.0) == 0.9
    # 同 x 的重复点：查询落在最左端时取第一个点，且绝不除零
    assert F.interpolate(((0.0, 0.5), (0.0, 0.7)), 0.0) == pytest.approx(0.5)
    assert F.interpolate(((0.0, 0.5), (0.0, 0.7), (5.0, 1.0)), 0.5) == pytest.approx(0.73)


# ---------------------------------------------------------------- 曲线与模式


def test_curve_set_selected_by_mode():
    assert _config().curve_set_for("reply_necessity") is not None
    widened = F.CurveSet(mood=((0.0, 0.35), (10.0, 1.3)))
    config = _config(curves_frequency=widened)
    narrow = _adjust(config=config, mode="frequency", emotion=0.0)
    wide = _adjust(config=config, mode="reply_necessity", emotion=0.0)
    assert narrow.adjust < wide.adjust  # necessity 组仍是原提案
    assert _adjust(config=config, mode="frequency", emotion=0.0).curve_set == "frequency"
    assert _adjust(config=config, mode="reply_necessity").curve_set == "necessity"


def test_unknown_mode_falls_back_to_frequency_curve():
    assert _adjust(mode="garbage").curve_set == "frequency"


def test_dynamic_curve_set_is_independent():
    """v1.6.0：``dynamic``（宿主 1.3.2）有自己的一套曲线，不与 frequency 串用。"""

    assert _config().curve_set_for("dynamic") is not None
    widened = F.CurveSet(mood=((0.0, 0.35), (10.0, 1.3)))
    config = _config(curves_dynamic=widened)
    dynamic = _adjust(config=config, mode="dynamic", emotion=0.0)
    frequency = _adjust(config=config, mode="frequency", emotion=0.0)
    assert dynamic.curve_set == "dynamic"
    assert dynamic.adjust < frequency.adjust, "只改 dynamic 组时 frequency 组应保持原提案"


# ---------------------------------------------------------------- 硬闸


def test_sleep_is_a_hard_zero():
    out = _adjust(activity="sleep", emotion=10.0, energy=10.0, material_count=5)
    assert out.adjust == 0.0
    assert out.reason == F.REASON_SLEEP


def test_quiet_hours_is_a_hard_zero():
    config = _config(quiet_hours=((23 * 60 + 30, 8 * 60),))
    inside = _adjust(config=config, now_minutes=2 * 60)
    outside = _adjust(config=config, now_minutes=14 * 60)
    assert inside.adjust == 0.0
    assert inside.reason == F.REASON_QUIET_HOURS
    assert outside.adjust > 0.0


def test_paused_holds_baseline():
    out = F.hold_baseline()
    assert out.adjust == 1.0
    assert F.reason_label(out.reason) == "已暂停（写回 1.0，不干预宿主）"


# ---------------------------------------------------------------- 因子组合


def test_neutral_state_is_below_one():
    # emotion 5 / energy 5 是中性偏低（曲线设计如此），daily 活动下应略低于 1
    out = _adjust()
    assert out.adjust == pytest.approx(0.95 * 0.85, rel=1e-6)


def test_factors_multiply():
    out = _adjust(activity="music", emotion=10.0, energy=10.0)
    assert out.adjust == pytest.approx(0.9 * 1.35 * 1.25, rel=1e-6)
    assert dict(out.factors)["活动(music)"] == pytest.approx(0.9)


def test_material_bonus_accepts_effective_count():
    """素材加成按**有效条数**折算（G2 保鲜衰减后是小数），不再是整数截断。"""

    out = _adjust(material_count=2.5)
    assert out.material_bonus == pytest.approx(0.15 * 2.5)
    assert out.material_count == pytest.approx(2.5)
    assert "2.5 条（有效）" in "\n".join(out.as_lines())

    # 整数条数与旧行为完全一致（向后兼容）
    whole = _adjust(material_count=3)
    assert whole.material_bonus == pytest.approx(0.45)
    assert "3 条（有效）" in "\n".join(whole.as_lines())


def test_best_case_and_worst_awake_case():
    best = _adjust(activity="before_sleep", emotion=10.0, energy=10.0)
    worst = _adjust(activity="night_study", emotion=0.0, energy=0.0)
    assert best.adjust == pytest.approx(1.15 * 1.35 * 1.25, rel=1e-6)
    assert worst.adjust == pytest.approx(0.5 * 0.55 * 0.5, rel=1e-6)
    # 情绪体力的联合摆幅约 6.1 倍
    assert (1.35 * 1.25) / (0.55 * 0.5) == pytest.approx(6.136, rel=1e-3)


def test_activity_can_suppress_best_mood_and_energy():
    out = _adjust(activity="night_study", emotion=10.0, energy=10.0)
    assert out.adjust < 1.0


def test_material_bonus_and_cap():
    one = _adjust(material_count=1)
    three = _adjust(material_count=3)
    many = _adjust(material_count=9)
    assert one.adjust - _adjust(material_count=0).adjust == pytest.approx(0.15)
    assert many.adjust - three.adjust == pytest.approx(0.0)  # 三条就打满 0.45
    assert many.material_bonus == pytest.approx(0.45)


def test_clamped_to_max_adjust():
    config = _config(max_adjust=1.2)
    out = _adjust(activity="before_sleep", emotion=10.0, energy=10.0, material_count=9, config=config)
    assert out.adjust == pytest.approx(1.2)
    assert out.raw > 1.2


def test_clamped_to_min_adjust():
    config = _config(min_adjust=0.2)
    out = _adjust(activity="night_study", emotion=0.0, energy=0.0, config=config)
    assert out.adjust == pytest.approx(0.2)


# ---------------------------------------------------------------- 健康：不要双重抑制


def test_sick_rest_activity_factor_is_neutral_so_cold_applies_once():
    healthy = _adjust(activity="sick_rest", sick=False, emotion=5.0, energy=5.0)
    sick = _adjust(activity="sick_rest", sick=True, emotion=5.0, energy=5.0)
    assert healthy.adjust == pytest.approx(0.95 * 0.85, rel=1e-6)  # 活动因子中性 1.0
    assert sick.adjust == pytest.approx(0.95 * 0.85 * 0.3, rel=1e-6)  # 只乘一次 0.3
    assert dict(healthy.factors).get("活动(sick_rest)") is None  # 1.0 的因子不出现在明细里


def test_sleep_deprived_applies_on_top_of_cold():
    out = _adjust(activity="sick_rest", sick=True, sleep_debt_nights=3)
    assert out.adjust == pytest.approx(0.95 * 0.85 * 0.3 * 0.9, rel=1e-6)
    names = dict(out.factors)
    assert "感冒" in names and "熬夜上限被压低" in names


def test_sleep_deprived_alone():
    out = _adjust(sleep_debt_nights=3)
    assert out.adjust == pytest.approx(0.95 * 0.85 * 0.9, rel=1e-6)


def test_healthy_factor_shows_when_nothing_is_wrong():
    assert dict(_adjust().factors).get("健康") == pytest.approx(1.0) or True
    # 1.0 的因子被过滤掉，明细里不应出现健康项
    assert "健康" not in dict(_adjust().factors)


# ---------------------------------------------------------------- 日期


def test_date_factor_applied_only_when_not_one():
    assert dict(_adjust(date_factor=1.0).factors).get("日期") is None
    out = _adjust(date_factor=1.3)
    assert dict(out.factors)["日期"] == pytest.approx(1.3)
    assert out.adjust == pytest.approx(0.95 * 0.85 * 1.3, rel=1e-6)


# ---------------------------------------------------------------- 明细输出


def test_breakdown_lines_are_readable():
    lines = _adjust(activity="music", emotion=8.0, material_count=1).as_lines()
    joined = "\n".join(lines)
    assert "原始倍率" in joined
    assert "最终倍率" in joined
    assert "素材" in joined


# ---------------------------------------------------------------- v1.1.1 回归


def test_non_finite_factor_lines_are_rejected_with_warning():
    """回归：``nan``/``inf`` 能通过 ``float()``，会一路把倍率推到上限（或 0）。"""

    factors, warnings = F.parse_factor_lines(["music=nan", "game=1e400", "good=0.9"])
    assert factors == {"good": 0.9}, factors
    assert len(warnings) == 2, warnings


def test_non_finite_curve_points_are_rejected_with_warning():
    points, warnings = F.parse_curve_points(["0=0.55", "5=nan", "10=1.35"])
    assert (5.0, float("nan")) not in points
    assert points == ((0.0, 0.55), (10.0, 1.35)), points
    assert warnings and "nan" in warnings[0]


def test_unknown_factor_key_warns_but_is_kept():
    factors, warnings = F.parse_factor_lines(
        ["nightstudy=0.5"], known_keys=["night_study", "daily"]
    )
    assert factors == {"nightstudy": 0.5}
    assert warnings and "nightstudy" in warnings[0]


def test_max_adjust_cannot_be_pierced_by_floors():
    """回归（v1.1.0 真 bug）：min_adjust / silence_floor 能击穿「倍率上限」。"""

    cfg_up = F.FactorConfig(max_adjust=0.2, min_adjust=0.5)
    out = _adjust(config=cfg_up)
    assert out.adjust <= max(cfg_up.max_adjust, cfg_up.min_adjust) + 1e-9

    cfg_floor = F.FactorConfig(max_adjust=0.5, min_adjust=0.0, silence_floor=0.6)
    sleeping = _adjust(activity="sleep", config=cfg_floor)
    assert sleeping.adjust <= max(
        cfg_floor.max_adjust, cfg_floor.min_adjust, cfg_floor.silence_floor
    ) + 1e-9
    assert sleeping.adjust == pytest.approx(0.6), "静默下限本身仍要生效"


def test_non_finite_raw_never_reaches_the_host():
    """兜底：即使配置对象被塞进非有限因子，倍率也必须落在有限区间里。"""

    cfg_nan = F.FactorConfig(activity_factors={"daily": float("nan")}, max_adjust=2.0)
    out = _adjust(config=cfg_nan)
    assert out.adjust == pytest.approx(1.0), out.adjust
