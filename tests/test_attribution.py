# -*- coding: utf-8 -*-
"""L3：情绪体力归因（v1.16.0 M8）与清醒疲劳接线（v1.16.0 M1）。

M8 是「观测先行」的那一项——**零行为变更**，只把「为什么是这个数」摊开。所以它的
用例分两层：

* 纯模块层逐项对账：基线构成必须与 ``_regress_emotion`` 真正用的基线**同一个数**，
  否则卡片又会和实现分叉（那正是 M8 要消灭的问题）；
* 插件层：``/生活 归因`` / ``/生活 关系`` 能起来、坏值不炸、疲劳曲线清空 = 关闭。

M1（清醒疲劳）的曲线标定归 ``test_sim.py``，这里只测**接线**（配置 → SimConfig）
与归因输出里体力流水的口径。
"""

import asyncio
import math
import pathlib
import random
import sys
import time
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_activity as A  # noqa: E402
import life_sim as S  # noqa: E402

TZ = 480
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent

#: 默认清醒疲劳曲线的**落盘形态**（plugin.DEFAULT_FATIGUE_RAMP_LINES）；
#: 这里刻意手写一份而不是 import 插件：纯模块用例不该为了一个字符串依赖 SDK。
RAMP_LINES = ("12=0", "16=-0.15", "20=-0.4", "24=-0.7")
RAMP = ((12.0, 0.0), (16.0, -0.15), (20.0, -0.4), (24.0, -0.7))


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


def event(at, label="一件小事", emotion=0.0, energy=0.0, **extra):
    payload = {"at": float(at), "label": label, "text": "x", "emotion": emotion,
               "energy": energy}
    payload.update(extra)
    return payload


# ================================================================ 基线构成


def test_baseline_line_lists_every_part_and_omits_empty_ones():
    at = ts(2026, 2, 8, 20, 0)
    # 活动进行中（否则没有任何可归因的时段，只有基线两行）
    extra = dict(activity_since=at - 600)

    state = make_state(at=at, afterglow=0.31, **extra)
    lines = S.attribution_lines(state, at, cfg())
    assert lines[0] == "情绪基线：5.00（基础） +0.31（余波） = 5.31"

    # 余波为 0 ⇒ 不印这一项（不印「+0.00（余波）」这种噪音）
    state.afterglow = 0.0
    assert S.attribution_lines(state, at, cfg())[0] == "情绪基线：5.00（基础） = 5.00"


def test_baseline_composition_is_the_very_value_regression_uses():
    """对账：卡片上的合计 == ``_regress_emotion`` 实际回归到的那个数。

    这是 M8 存在的意义——「能看见」与「真在跑」必须是同一个数。
    """

    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, emotion=9.0)
    state.recent_events = [event(at, "抽卡出货", emotion=1.0)]

    # 按 tick 步进（一次跳 4 小时会被判成「停机间隙」而不记账），跑到情绪收敛
    for step in range(24):
        S.settle(state, now=at + 600 * (step + 1), config=cfg(), events=(),
                 rng=random.Random(1))

    now = at + 600 * 24
    _, expected = S.emotion_baseline_parts(state, now, cfg())
    assert state.emotion == pytest.approx(expected)
    line = S.attribution_lines(state, now, cfg())[0]
    assert line.endswith(f"= {expected:.2f}")


def test_afterglow_weight_is_one_inside_the_window_and_zero_outside():
    config = cfg(afterglow_span_hours=24.0)
    assert S.afterglow_weight(0.0, config) == 1.0
    assert S.afterglow_weight(24 * 3600.0, config) == 1.0
    assert S.afterglow_weight(24 * 3600.0 + 1.0, config) == 0.0
    # 窗口为 0 时余波整体失效（不除零、不外推）
    assert S.afterglow_weight(0.0, cfg(afterglow_span_hours=0.0)) == 0.0


# ================================================================ 来源与冲击榜


def test_sources_rank_by_contribution_and_stop_at_three():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, recent_events=[
        event(at - 600, "小事A", emotion=0.2),
        event(at - 1200, "大事B", emotion=-1.5, source="social"),
        event(at - 1800, "中事C", emotion=0.8),
        event(at - 2400, "小事D", emotion=-0.3, source="world"),
    ])
    lines = S.attribution_lines(state, at, cfg())
    head = lines.index("余波来源（近 24 小时，对基线偏移贡献 top 3）：")
    ranked = lines[head + 1:head + 4]
    assert len(ranked) == 3, "top 3 就该只有 3 行"
    assert "大事B" in ranked[0] and "（社交）" in ranked[0]
    assert "中事C" in ranked[1]
    # 按 |对基线的贡献| 排：-0.3 排在 +0.2 前面，第 4 名的 +0.2 出局
    assert "小事D" in ranked[2]
    assert all("小事A" not in item for item in ranked), "第 4 名不该出现"


def test_impacts_rank_by_absolute_delta_and_stop_at_five():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, recent_events=[
        event(at - 600 * index, f"事{index}", emotion=delta)
        for index, delta in enumerate([0.1, -2.0, 1.5, -0.5, 0.9, 0.3, -1.2])
    ])
    lines = S.attribution_lines(state, at, cfg())
    head = lines.index("情绪冲击（近 24 小时，按 |增量| top 5）：")
    ranked = lines[head + 1:head + 6]
    assert len(ranked) == 5
    assert "事1" in ranked[0] and "-2.00" in ranked[0], "（|−2.0| 最大）"
    assert "事2" in ranked[1]
    assert "事6" in "".join(ranked), "|−1.2| 也进前五"
    assert all("事0" not in item and "事5" not in item for item in ranked), "最小的两条出局"


def test_events_outside_the_window_are_not_counted():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, afterglow=0.0, recent_events=[
        event(at - 25 * 3600, "太旧了", emotion=3.0),
        event(at - 23 * 3600, "还在窗口里", emotion=1.0),
    ])
    text = "\n".join(S.attribution_lines(state, at, cfg()))
    assert "太旧了" not in text
    assert "还在窗口里" in text
    # 25 小时前的那条也不能进余波（否则基线会虚高）
    S._recompute_afterglow(state, at, cfg())
    assert state.afterglow == pytest.approx(1.0 * 0.15)


# ================================================================ 回归与惯性


def test_regression_line_reports_inertia_distance_and_eta():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, emotion=7.0, afterglow=0.0, inertia_until=at + 1200)
    line = [item for item in S.attribution_lines(state, at, cfg()) if item.startswith("情绪回归：")][0]
    assert "当前 7.00" in line and "距基线 +2.00" in line
    assert "惯性还剩 20 分钟" in line
    assert "约需 120 分钟回到基线" in line, line  # 20 惯性 + 2.0/0.2×10 分钟
    assert "不回归" in line, "惯性期里必须说清这段时间是冻结的"


def test_regression_line_says_when_the_rate_is_zero():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, emotion=8.0)
    line = [item for item in S.attribution_lines(state, at, cfg(recover_per_tick=0.0))
            if item.startswith("情绪回归：")][0]
    assert "回归速率为 0" in line and "约需" not in line


# ================================================================ 体力流水


def test_energy_flow_counts_main_background_and_ramp():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(
        at=at,
        activity=A.DAZE,
        activity_since=at - 3600,
        continuous_awake_minutes=14 * 60,
        side_activities=["anime"],
    )
    text = "\n".join(S.attribution_lines(state, at, cfg(fatigue_ramp_curve=RAMP)))
    assert "体力流水（近 1 小时）：" in text
    assert "发呆 -0.10/h × 60 分钟 = -0.10" in text
    # 背景活动按 0.3 权重折算：看番 -0.50/h × 0.3 = -0.15/h
    assert "背景·看番 -0.15/h × 60 分钟 = -0.15" in text
    assert "按 0.3 权重折算" in text
    assert "清醒疲劳 -0.07/h × 60 分钟" in text
    assert "连续清醒 14.0 小时" in text
    assert "合计 ≈ -0.33" in text  # -0.10 - 0.15 - 0.075


def test_energy_flow_says_the_switched_part_is_not_attributed():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, activity=A.GAME, activity_since=at - 900)
    text = "\n".join(S.attribution_lines(state, at, cfg()))
    assert "当前活动是 15 分钟前开始的" in text
    assert "切换前的时段不归因" in text
    assert "打游戏 -0.70/h × 15 分钟 = -0.17" in text


def test_energy_flow_lists_event_energy_bumps():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, activity=A.GAME, activity_since=at - 3600, recent_events=[
        event(at - 1200, "去楼下跑了一圈", energy=0.4),
    ])
    text = "\n".join(S.attribution_lines(state, at, cfg()))
    assert "事件（体力项合计） = +0.40" in text
    assert "合计 ≈ -0.30" in text


def test_energy_flow_refuses_to_guess_without_an_activity_anchor():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, activity=A.GAME, activity_since=0.0)
    lines = S.attribution_lines(state, at, cfg())
    assert not any(item.startswith("体力流水") for item in lines)


# ================================================================ 空与坏值


def test_empty_history_still_shows_the_baseline_and_says_so():
    """空历史：不许报错，也不许给一张看不懂的空卡。"""

    at = ts(2026, 2, 8, 20, 0)
    lines = S.attribution_lines(S.LifeState(), at, cfg())
    assert lines[0] == "情绪基线：5.00（基础） = 5.00"
    assert any("暂无归因数据" in item for item in lines)
    assert any(item.startswith("情绪回归：") for item in lines)


@pytest.mark.parametrize(
    "bad",
    [
        [{"at": float("nan"), "emotion": 1.0}],
        [{"at": float("inf"), "emotion": -1.0}],
        [{"at": "soon", "emotion": "很多", "energy": None}],
        ["not-a-dict", 42, None],
        [{"at": ts(2026, 2, 8, 20, 0) - 10, "emotion": 1e300, "label": "x" * 500}],
    ],
)
def test_bad_state_values_never_raise(bad):
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, recent_events=list(bad))
    lines = S.attribution_lines(state, at, cfg())
    assert lines and isinstance(lines[0], str)


def test_bad_activity_anchor_and_nan_energy_do_not_raise():
    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, activity=A.DAILY, activity_since=float("nan"))
    state.side_activities = ["anime"]
    lines = S.attribution_lines(state, at, cfg(fatigue_ramp_curve=RAMP))
    assert any(item.startswith("情绪基线") for item in lines)
    assert not any("nan" in item.lower() for item in lines)
    # 起点不可信 ⇒ 干脆不猜（不拿 NaN 去乘一小时）
    assert not any(item.startswith("体力流水") for item in lines)


# ================================================================ 疲劳曲线零件


def test_fatigue_ramp_extra_endpoints_and_disabled_curve():
    config = cfg(fatigue_ramp_curve=RAMP)
    assert S.fatigue_ramp_extra(0, config) == 0.0
    assert S.fatigue_ramp_extra(12 * 60, config) == 0.0
    assert S.fatigue_ramp_extra(14 * 60, config) == pytest.approx(-0.075)
    assert S.fatigue_ramp_extra(20 * 60, config) == pytest.approx(-0.4)
    # 端点外不外推：24 小时以上保持端点值
    assert S.fatigue_ramp_extra(72 * 60, config) == pytest.approx(-0.7)
    # 空曲线 = 关闭
    assert S.fatigue_ramp_extra(48 * 60, cfg()) == 0.0
    # 坏值（NaN）按关闭处理，绝不让 drain 变成 NaN
    assert S.fatigue_ramp_extra(float("nan"), config) == 0.0


def test_fatigue_ramp_refuses_to_give_energy_back():
    """曲线配成正数（熬夜回血）按 0 处理——那与机制意图相反。"""

    config = cfg(fatigue_ramp_curve=((12.0, 0.0), (20.0, 0.5)))
    assert S.fatigue_ramp_extra(20 * 60, config) == 0.0
    assert math.isfinite(S.fatigue_ramp_extra(20 * 60, config))


# ================================================================ 插件层接线


def _make_plugin(**overrides):
    """与 test_sleep_improvements 同款脚手架（时区置 0、清掉静默时段）。"""

    from fakehost import (  # noqa: PLC0415
        FakeHost,
        FakePaths,
        bind_context,
        build_context,
        get_default_config,
        load_plugin_module,
    )

    module = load_plugin_module(PLUGIN_DIR, "life_frequency_attribution")
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


def test_default_fatigue_curve_is_wired_into_sim_config():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    module, plugin, _host = _make_plugin()
    assert tuple(module.DEFAULT_FATIGUE_RAMP_LINES) == RAMP_LINES
    assert plugin._sim_config().fatigue_ramp_curve == RAMP


def test_emptying_the_curve_is_the_off_switch():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin(emotion_energy={"fatigue_ramp_curve": []})
    assert plugin._sim_config().fatigue_ramp_curve == ()
    # 空曲线下这行提示词也不该出现（不印一个已经关掉的机制）
    lines = S.activity_effect_lines(plugin._sim_config(), activity_factors={A.SLEEP: 0.0})
    assert not any("清醒疲劳" in item for item in lines)


def test_bad_curve_lines_are_dropped_with_a_warning_not_a_crash():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin(
        emotion_energy={"fatigue_ramp_curve": ["白天=0", "16=-0.2"]}
    )
    # 只剩一个合法点 ⇒ 整条曲线作废（曲线至少两个点），但不许抛错
    assert plugin._sim_config().fatigue_ramp_curve == ()


def test_attribution_command_renders_on_a_live_plugin():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _module, plugin, host = _make_plugin()
        now = time.time()
        plugin._state.activity = A.DAILY
        plugin._state.activity_since = now - 900
        plugin._state.emotion = 6.4
        plugin._state.continuous_awake_minutes = 15 * 60
        plugin._state.recent_events = [event(now - 600, "抽卡出货", emotion=1.3)]

        ok, text, level = await plugin.cmd_life_state(
            matched_groups={"sub": "归因"}, stream_id="group-1", text="/生活 归因"
        )
        assert ok is True and level == 1
        assert "情绪体力归因" in text
        assert "情绪基线：5.00（基础）" in text
        assert "抽卡出货" in text
        assert "体力流水（近 1 小时）：" in text
        assert "不回溯" in text
        assert host.calls_of("send.text"), "命令必须自己发出去（返回值不会自动发）"

    asyncio.run(run())


def test_relations_command_shows_the_distribution():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    class FakeStore:
        def __init__(self, records):
            self.records = records

        def top_relationships(self, limit=3):  # noqa: ANN001
            return list(self.records)[: max(0, int(limit))]

    async def run():
        _module, plugin, _host = _make_plugin(relations={"enabled": True})
        plugin._routine_store = FakeStore([
            {"user_id": "123456789", "familiarity": 88.0, "relation_hint": "阿岚"},
            {"user_id": "223456789", "familiarity": 60.0},
            {"user_id": "323456789", "familiarity": 35.0},
            {"user_id": "423456789", "familiarity": 5.0},
        ])
        text = plugin._render_relations(plugin._routine_store.records)
        assert "共 4 人" in text
        assert "亲密（≥80）：1 人" in text
        assert "熟络（50–79）：1 人" in text
        assert "认识（20–49）：1 人" in text
        assert "陌生（0–19）：1 人" in text
        assert "熟悉度：平均 47.0" in text
        assert "阿岚" in text and "阿岚" not in "".join(
            item for item in text.splitlines() if "123456789" in item
        ), "QQ 号必须脱敏"

        ok, command_text, _level = await plugin.cmd_life_state(
            matched_groups={"sub": "关系"}, stream_id="group-1", text="/生活 关系"
        )
        assert ok is True and "关系档案" in command_text

    asyncio.run(run())


def test_relations_command_degrades_when_everything_is_off():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin(relations={"enabled": False})
    assert "已关闭" in plugin._render_relations([])
    plugin.config.relations.enabled = True
    plugin._routine_store = None
    assert "不可用" in plugin._render_relations([])
    plugin._routine_store = object()
    assert "还没有档案" in plugin._render_relations([])
