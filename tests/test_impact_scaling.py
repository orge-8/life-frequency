# -*- coding: utf-8 -*-
"""L3：情绪冲击的边际效用与社交情绪的关系加权（v1.16.3 D 期 = M6 + M7）。

D 期是「体验润色」：不动结构，只把**同一个增量在不同状态下的体感**做对。两条纪律
在这里最容易破，所以用例钉得很死：

1. **回退必须逐位**：`emotion_impact_scaling = false` 时事件增量原样进出；
   `relation_emotion_scaling = false`（或根本没有关系档案）时系数恒 1.0
   —— 后者正是决议 3 的「中性锚点钉在陌生人身上」：新装插件 = 旧行为。
2. **不许双重压制**：社交通道的缩放发生在 ``life_social`` 的 grant **之前**，
   ``life_sim.append_social_event`` 里**不许**再乘一次（有用例钉住这条分工）。
"""

import asyncio
import pathlib
import sys
import time
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_activity as A  # noqa: E402
import life_events as E  # noqa: E402
import life_relations as R  # noqa: E402
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


def life_event(label, emotion, *, energy=0.0):
    return E.LifeEvent(label=label, activities=(A.DAILY,), emotion=emotion, energy=energy)


# ================================================================ M6 边际效用


@pytest.mark.parametrize(
    "emotion,delta,expected",
    [
        (1.0, 1.0, 1.3),    # 低谷 + 好消息 = 雪中送炭
        (2.0, 0.5, 1.3),
        (5.0, 1.0, 1.0),    # 基线附近不放大也不打折
        (9.0, 1.0, 0.7),    # 高涨 + 好消息 = 快乐 plateau
        (1.0, -1.0, 0.7),   # 低谷 + 坏消息 = 麻木
        (5.0, -1.0, 1.0),
        (9.0, -1.0, 1.2),   # 从高处跌落更疼
    ],
)
def test_impact_scale_quadrants(emotion, delta, expected):
    assert S.impact_scale(emotion, delta, cfg(emotion_impact_scaling=True)) == pytest.approx(
        expected
    )


def test_impact_scale_is_linear_between_the_anchor_points():
    config = cfg(emotion_impact_scaling=True)
    # 2 → 1.3、5 → 1.0：3.5 是中点
    assert S.impact_scale(3.5, 1.0, config) == pytest.approx(1.15)
    # 5 → 1.0、8 → 0.7：6.5 是中点
    assert S.impact_scale(6.5, 1.0, config) == pytest.approx(0.85)


def test_impact_scale_is_off_by_default_and_fails_open():
    assert S.impact_scale(1.0, 5.0, cfg()) == 1.0
    on = cfg(emotion_impact_scaling=True)
    assert S.impact_scale(1.0, 0.0, on) == 1.0, "零增量没有体感可言"
    assert S.impact_scale(float("nan"), 1.0, on) == 1.0
    assert S.impact_scale(1.0, float("nan"), on) == 1.0
    assert S.impact_scale(1.0, 1.0, cfg(
        emotion_impact_scaling=True, impact_positive_curve=()
    )) == 1.0


def test_event_delta_is_scaled_and_recorded_as_applied():
    at = ts(2026, 2, 8, 20, 0)
    config = cfg(emotion_impact_scaling=True, inertia_scale_enabled=True)

    # 高涨时 +1.0 只值 +0.7
    high = make_state(at=at, emotion=9.8)
    S._apply_event(high, life_event("抽卡出了", 1.0), now=at, config=config,
                   rng=__import__("random").Random(1))
    assert high.emotion == pytest.approx(10.0), "9.8 + 0.7 会被钳到上限"
    assert high.recent_events[-1]["emotion"] == pytest.approx(0.7), "记的是缩放后的增量"
    assert high.inertia_until == pytest.approx(at + S.scaled_inertia(0.7, config))

    # 低谷时 +0.5 值 +0.65
    low = make_state(at=at, emotion=1.0)
    S._apply_event(low, life_event("有人夸她", 0.5), now=at, config=config,
                   rng=__import__("random").Random(1))
    assert low.emotion == pytest.approx(1.65)

    # 高涨时挨一下更疼：-1.0 变 -1.2
    fall = make_state(at=at, emotion=9.0)
    S._apply_event(fall, life_event("笔没水了", -1.0), now=at, config=config,
                   rng=__import__("random").Random(1))
    assert fall.emotion == pytest.approx(7.8)


def test_event_delta_is_untouched_when_scaling_is_off():
    at = ts(2026, 2, 8, 20, 0)
    for emotion, delta in ((9.8, 1.0), (1.0, 0.5), (9.0, -1.0)):
        state = make_state(at=at, emotion=emotion)
        S._apply_event(state, life_event("一件事", delta), now=at, config=cfg(),
                       rng=__import__("random").Random(1))
        assert state.emotion == pytest.approx(S._clamp(emotion + delta, 0.0, 10.0))
        assert state.recent_events[-1]["emotion"] == pytest.approx(delta)


def test_date_rules_use_the_same_scaling():
    at = ts(2026, 2, 8, 20, 0)
    festival = S.FestivalRule(name="生日", month=2, day=8, emotion=1.0)
    config = cfg(emotion_impact_scaling=True, festivals=(festival,))

    happy = make_state(at=at, emotion=8.5)
    S._fire_date_rules(happy, local_dt=S.local_datetime(at, TZ), now=at, config=config)
    assert happy.emotion == pytest.approx(9.2), "8.5 + 1.0×0.7"

    sad = make_state(at=at, emotion=1.0)
    S._fire_date_rules(sad, local_dt=S.local_datetime(at, TZ), now=at, config=config)
    assert sad.emotion == pytest.approx(2.3), "1.0 + 1.0×1.3（低谷的节日更亮）"


# ================================================================ M7 关系加权


@pytest.mark.parametrize(
    "familiarity,expected",
    [(None, 1.0), (0.0, 1.0), (19.0, 1.0), (20.0, 1.0), (35.0, 1.1), (50.0, 1.2),
     (80.0, 1.5), (100.0, 1.5), (float("nan"), 1.0), ("很多", 1.0)],
)
def test_relation_emotion_factor(familiarity, expected):
    assert R.emotion_factor(familiarity) == pytest.approx(expected)


def test_relation_factor_is_one_way_and_capped():
    """单向只放大：曲线里写小于 1 或大于 1.5 的值都会被钳回来。"""

    assert R.emotion_factor(0.0, curve=((0.0, 0.5), (100.0, 0.6))) == pytest.approx(1.0)
    assert R.emotion_factor(100.0, curve=((0.0, 1.0), (100.0, 3.0))) == pytest.approx(1.5)
    assert R.emotion_factor(50.0) == pytest.approx(1.2), "缺曲线时用内置表"
    # 决议 3 的锚点：陌生人（新装插件）系数必须是 1.0，否则第一天就被悄悄打折
    assert R.emotion_factor(0.0) == 1.0


def test_relation_curve_helper_returns_a_copy():
    first = R.emotion_curve()
    assert first == R.emotion_curve()
    assert isinstance(first, tuple)
    assert first is not R.RELATION_EMOTION_CURVE


def _ctx(*, relation_factor=1.0, impact_factor=1.0, loneliness=0.0, policy_kwargs=None,
         day_used=0.0):
    kwargs = {"loneliness_scaling": False}
    kwargs.update(policy_kwargs or {})
    return SOS.IntakeContext(
        now=1_800_000_000.0, day_key="2026-10-01", activity=A.DAILY, asleep=False,
        day_used=day_used, policy=SOS.SocialPolicy(**kwargs), loneliness=loneliness,
        relation_factor=relation_factor, impact_factor=impact_factor,
    )


def test_close_friend_mention_is_worth_more():
    signal = {"at": 1_800_000_000.0, "session_id": "p1", "is_group": False,
              "mentioned": True, "user_id": "10001"}

    stranger = SOS.intake_live([signal], _ctx(relation_factor=1.0), {})
    close = SOS.intake_live([signal], _ctx(relation_factor=1.5), {})
    assert stranger.events[0]["emotion"] == pytest.approx(0.3)
    assert close.events[0]["emotion"] == pytest.approx(0.45)


def test_relation_weight_still_respects_the_daily_cap():
    signal = {"at": 1_800_000_000.0, "session_id": "p1", "is_group": False,
              "mentioned": True, "user_id": "10001"}
    result = SOS.intake_live([signal], _ctx(relation_factor=1.5, day_used=1.4), {})
    assert result.events[0]["emotion"] == pytest.approx(0.1), "额度只剩 0.1"


def test_relation_weight_is_not_applied_to_the_diary_digest():
    """日记摘要只有昵称、认不出人 ⇒ 不加权（宁可不放大，也不猜错人）。"""

    item = SOS.DigestItem(at=1_800_000_000.0, date="2026-10-01", event_id="e1",
                          who="阿岚", what="一起打了会儿游戏", quote="")
    result = SOS.intake_digest([item], _ctx(relation_factor=1.5), {})
    assert result.events[0]["emotion"] == pytest.approx(0.4), "关系系数不该作用于摘要"


def test_impact_factor_applies_to_both_social_paths():
    signal = {"at": 1_800_000_000.0, "session_id": "p1", "is_group": False,
              "mentioned": True, "user_id": "10001"}
    item = SOS.DigestItem(at=1_800_000_000.0, date="2026-10-01", event_id="e1",
                          who="阿岚", what="聊了会儿", quote="")

    assert SOS.intake_live([signal], _ctx(impact_factor=1.3), {}).events[0]["emotion"] \
        == pytest.approx(0.39)
    assert SOS.intake_digest([item], _ctx(impact_factor=1.3), {}).events[0]["emotion"] \
        == pytest.approx(0.52)


def test_social_event_does_not_scale_again():
    """分工纪律：缩放只在 life_social 里发生一次（append_social_event 只写数值）。"""

    at = ts(2026, 2, 8, 20, 0)
    state = make_state(at=at, emotion=9.8)
    entry = SOS._event(at=at, label="有人找我", text="有人在叫我", emotion=0.45,
                       activity=A.DAILY)
    S.append_social_event(state, entry, config=cfg(emotion_impact_scaling=True))
    assert state.emotion == pytest.approx(10.0), "9.8 + 0.45 → 钳到 10.0"
    assert state.recent_events[-1]["emotion"] == pytest.approx(0.45), "不许被再乘一次"


# ================================================================ 插件层接线


class _FakeStore:
    """够用的关系库替身：只实现纯模块真正调用的那几个方法。"""

    def __init__(self, records=None):
        self.records = {str(item["user_id"]): dict(item) for item in (records or [])}
        self.puts = 0

    def get_relationship(self, user_id):  # noqa: ANN001
        record = self.records.get(str(user_id))
        return dict(record) if record else None

    def put_relationship(self, record):  # noqa: ANN001
        self.puts += 1
        self.records[str(record["user_id"])] = dict(record)
        return True

    def prune_relationships(self, keep=200):  # noqa: ANN001
        return 0

    def top_relationships(self, limit=3):  # noqa: ANN001
        ordered = sorted(self.records.values(),
                         key=lambda item: -float(item.get("familiarity") or 0.0))
        return ordered[: max(0, int(limit))]


def _make_plugin(**overrides):
    from fakehost import (  # noqa: PLC0415
        FakeHost,
        FakePaths,
        bind_context,
        build_context,
        get_default_config,
        load_plugin_module,
    )

    module = load_plugin_module(PLUGIN_DIR, "life_frequency_impact_scaling")
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


def test_d_phase_defaults_are_wired_on():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    module, plugin, _host = _make_plugin()
    assert tuple(module.DEFAULT_IMPACT_POSITIVE_LINES) == ("2=1.3", "5=1.0", "8=0.7")
    config = plugin._sim_config()
    assert config.emotion_impact_scaling is True
    assert config.impact_positive_curve == module.DEFAULT_IMPACT_POSITIVE_CURVE
    assert config.impact_negative_curve == module.DEFAULT_IMPACT_NEGATIVE_CURVE
    assert plugin.config.social.relation_emotion_scaling is True
    assert plugin._relation_emotion_curve() == R.RELATION_EMOTION_CURVE


def test_d_phase_switches_come_back_to_old_behaviour():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin(
        emotion_energy={"emotion_impact_scaling": False},
        social={"relation_emotion_scaling": False},
    )
    assert plugin._sim_config().emotion_impact_scaling is False
    # 关掉关系加权后，即使索引里有熟人也是 1.0
    plugin._relation_familiarity = {"10001": 88.0}
    assert plugin._relation_factor_for_signals([{"user_id": "10001"}]) == 1.0


def test_fresh_install_has_no_relation_data_so_the_factor_is_one():
    """决议 3 的核心：新装插件 relations 全空 ⇒ 社交收益与升级前逐位一致。"""

    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin()
    plugin._relation_familiarity = {}
    assert plugin._relation_factor_for_signals([{"user_id": "10001"}]) == 1.0
    assert plugin._relation_factor_for_signals([]) == 1.0


def test_relation_factor_takes_the_most_familiar_speaker():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin()
    plugin._relation_familiarity = {"10001": 5.0, "10002": 85.0, "10003": 45.0}
    assert plugin._relation_factor_for_signals(
        [{"user_id": "10001"}, {"user_id": "10002"}]
    ) == pytest.approx(1.5)


def test_relation_index_is_filled_by_touch_and_by_the_daily_refresh():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _module, plugin, _host = _make_plugin()
        store = _FakeStore([{"user_id": "20002", "familiarity": 60.0}])
        plugin._routine_store = store

        # ① 群里被 @ → 建档并顺手写进索引（被 @ 的互动 +0.2）
        await plugin._record_relation(
            {"is_mentioned": True, "processed_plain_text": "在吗"},
            session_id="g1", group_id="123456", user_id="20001", now=time.time(),
        )
        assert plugin._relation_familiarity["20001"] == pytest.approx(0.2)
        assert plugin._relation_factor_for_signals([{"user_id": "20001"}]) == pytest.approx(1.0)

        # ② 全量刷新：库里的 60 分熟人进索引
        await plugin._refresh_relation_index()
        assert plugin._relation_familiarity["20002"] == pytest.approx(60.0)
        assert plugin._relation_factor_for_signals([{"user_id": "20002"}]) == pytest.approx(
            R.emotion_factor(60.0)
        )

    asyncio.run(run())


def test_relation_index_is_empty_when_the_relation_layer_is_off():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _module, plugin, _host = _make_plugin(relations={"enabled": False})
        plugin._relation_familiarity = {"10001": 90.0}
        await plugin._refresh_relation_index()
        assert plugin._relation_familiarity == {}
        assert plugin._relation_factor_for_signals([{"user_id": "10001"}]) == 1.0

    asyncio.run(run())


def test_intake_wires_relation_and_impact_factors_into_the_event():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _module, plugin, _host = _make_plugin(
            social={"enabled": True}, mood={"loneliness_social_scaling": False}
        )
        plugin._relation_familiarity = {"10001": 85.0}
        plugin._state.day_key = "2026-10-01"
        plugin._state.activity = A.DAILY
        plugin._state.emotion = 1.0  # 低谷 ⇒ 正向增量 ×1.3
        plugin._social_inbox.append(
            {"at": time.time(), "session_id": "p1", "is_group": False,
             "mentioned": True, "text_len": 2, "user_id": "10001"}
        )
        plugin._intake_social(time.time(), plugin._sim_config())
        event = plugin._state.recent_events[-1]
        # 0.3（被叫到）× 1.5（亲密）× 1.3（雪中送炭）
        assert event["emotion"] == pytest.approx(0.585, abs=1e-3), event

    asyncio.run(run())


def test_bad_relation_curve_warns_and_falls_back_to_the_builtin_one():
    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    _module, plugin, _host = _make_plugin(social={"relation_emotion_curve": ["熟悉=1.2"]})
    assert plugin._relation_emotion_curve() == R.RELATION_EMOTION_CURVE


def test_attribution_card_reflects_the_scaled_event_delta():
    """缩放后的增量必须与卡片对得上（否则「为什么是这个数」又说不清）。"""

    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def run():
        _module, plugin, _host = _make_plugin()
        now = time.time()
        plugin._state.activity = A.DAILY
        plugin._state.activity_since = now - 600
        plugin._state.emotion = 9.6
        plugin._state.recent_events = [
            {"at": now - 300, "label": "抽卡出了", "text": "x", "emotion": 0.7, "energy": 0.0}
        ]
        ok, text, _level = await plugin.cmd_life_state(
            matched_groups={"sub": "归因"}, stream_id="g1", text="/生活 归因"
        )
        assert ok is True and "+0.70" in text, text

    asyncio.run(run())


# ================================================================ 曲线标定脚本


def _load_calibrator():
    import importlib.util

    path = PLUGIN_DIR / "calibrate_curves.py"
    spec = importlib.util.spec_from_file_location("life_frequency_calibrator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_calibration_script_is_deterministic_and_reports_the_verdict():
    """报告里承诺「同种子输出逐位一致」——这条承诺要有回归守着。

    顺便钉住结论段的**判定规则**：只有「新机制相对旧机制的位移 > 0.4」才算需要
    重标定点值；锚点漂移在旧机制里同样存在时不该改曲线（那是模拟路径的属性）。
    """

    cal = _load_calibrator()
    config = cal.production_config()

    first = cal.simulate(config, days=1, seed=7)
    second = cal.simulate(config, days=1, seed=7)
    assert first["emotion"] == second["emotion"], "同种子必须逐位一致"
    assert first["energy"] == second["energy"]
    # 换一个种子应当得到不同的时间线（否则说明随机源没接上）
    assert cal.simulate(config, days=1, seed=8)["emotion"] != first["emotion"]

    old = cal.simulate(cal.a_phase_off(config), days=1, seed=7)
    report = cal.render_report(days=1, new=first, old=old, config=config)
    for needle in ("## 情绪（0–10）", "## 体力（0–10）", "## 曲线点值是否还立在原位",
                   "**结论**", "calibrate_curves.py --days 7"):
        assert needle in report, needle
    # 一天的模拟里机制之间的位移不该被误判成「需要重标定」
    assert ("三套曲线的点值本轮不动" in report) or ("需要重新标定点值" in report)
    assert cal.summary(first["emotion"])["p50"] == pytest.approx(
        cal.summary(second["emotion"])["p50"]
    )
