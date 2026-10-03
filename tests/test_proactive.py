# -*- coding: utf-8 -*-
"""L3：主动开口的评分、硬闸与会话记录。"""

import pytest

import life_proactive as P


def _config(**overrides):
    base = dict(enabled=True)
    base.update(overrides)
    return P.ProactiveConfig(**base)


def _material(weight=0.6, now=1000.0, ttl=3600.0, text="刚发生的一件小事", label="小事"):
    return {
        "label": label,
        "text": text,
        "weight": weight,
        "created_at": now,
        "expires_at": now + ttl,
    }


def _decide(**overrides):
    args = dict(
        config=_config(),
        now=1000.0,
        now_minutes=14 * 60,
        activity="daily",
        energy=8.0,
        emotion=6.0,
        materials=[_material()],
        session={},
        day_key="2026-02-08",
    )
    args.update(overrides)
    return P.decide(**args)


# ---------------------------------------------------------------- 评分


def test_score_landmarks():
    config = _config()
    low, _ = P.score_of(material_weight=0.5, energy=4.0, emotion=5.0, config=config)
    high, _ = P.score_of(material_weight=0.5, energy=10.0, emotion=10.0, config=config)
    assert low == pytest.approx(0.5)  # 体力不给分、情绪中性不给分
    assert high == pytest.approx(0.5 + 0.2 + 0.12)


def test_energy_bias_is_zero_below_floor():
    config = _config()
    for energy in (0.0, 2.0, 4.0):
        score, _ = P.score_of(material_weight=0.0, energy=energy, emotion=5.0, config=config)
        assert score == pytest.approx(0.0)


def test_mood_bias_is_capped_and_signed():
    config = _config()
    worst, _ = P.score_of(material_weight=0.0, energy=0.0, emotion=0.0, config=config)
    best, _ = P.score_of(material_weight=0.0, energy=0.0, emotion=10.0, config=config)
    assert worst == pytest.approx(-0.12)
    assert best == pytest.approx(0.12)
    assert worst >= -config.mood_bias_cap and best <= config.mood_bias_cap


def test_score_detail_is_human_readable():
    _, detail = P.score_of(material_weight=0.6, energy=8.0, emotion=7.0, config=_config())
    assert "素材=" in detail and "阈值=" in detail


# ---------------------------------------------------------------- 素材选择


def test_select_material_picks_highest_weight_and_skips_expired():
    now = 1000.0
    chosen = P.select_material(
        [
            _material(weight=0.3, now=now),
            _material(weight=0.9, now=now),
            _material(weight=0.99, now=now - 99999, ttl=1.0),
        ],
        now,
    )
    assert chosen is not None
    assert chosen["weight"] == pytest.approx(0.9)


def test_select_material_returns_none_when_all_expired():
    now = 1000.0
    assert P.select_material([_material(now=now - 99999, ttl=1.0)], now) is None
    assert P.select_material([], now) is None


def test_select_material_decays_past_best_until():
    """G2：过了保鲜期的素材低价值化——按有效权重挑选，附 effective_weight 供评分。"""

    now = 1000.0
    stale = _material(weight=0.9, now=now)
    stale["best_until"] = now - 800          # 已过保鲜期较久
    stale["expires_at"] = now + 200          # 衰减到接近下限：0.9×0.4 = 0.36
    fresh = _material(weight=0.6, now=now, label="新鲜")
    fresh["best_until"] = now + 500
    chosen = P.select_material([stale, fresh], now, floor=0.25)
    assert chosen is not None and chosen["label"] == "新鲜"
    assert chosen["effective_weight"] == pytest.approx(0.6)


def test_build_intent_sanitizes_and_mentions_label():
    intent = P.build_intent({"label": "<小事>", "text": "{刚发生}"})
    assert "<" not in intent and "{" not in intent
    assert "小事" in intent


# ---------------------------------------------------------------- 硬闸


def test_disabled_blocks():
    out = _decide(config=_config(enabled=False))
    assert out.should_send is False
    assert out.reason == P.REASON_DISABLED
    assert out.reason_label == "主动开口未启用"


def test_sleeping_blocks():
    out = _decide(activity="sleep")
    assert out.should_send is False
    assert out.reason == P.REASON_SLEEPING


def test_low_energy_blocks():
    out = _decide(energy=2.0)
    assert out.should_send is False
    assert out.reason == P.REASON_LOW_ENERGY
    assert "下限" in out.detail


def test_quiet_hours_blocks():
    out = _decide(config=_config(quiet_hours=((23 * 60, 8 * 60),)), now_minutes=2 * 60)
    assert out.should_send is False
    assert out.reason == P.REASON_QUIET


def test_no_material_blocks():
    out = _decide(materials=[])
    assert out.should_send is False
    assert out.reason == P.REASON_NO_MATERIAL


def test_daily_max_blocks():
    out = _decide(session={"day_key": "2026-02-08", "count": 1}, config=_config(daily_max=1))
    assert out.should_send is False
    assert out.reason == P.REASON_DAILY_MAX
    assert out.material is not None


def test_daily_max_zero_blocks():
    out = _decide(session={}, config=_config(daily_max=0))
    assert out.should_send is False
    assert out.reason == P.REASON_DAILY_MAX


def test_count_resets_on_new_day():
    out = _decide(
        session={"day_key": "2026-02-07", "count": 5},
        day_key="2026-02-08",
        config=_config(daily_max=1),
    )
    assert out.should_send is True


def test_interval_blocks():
    out = _decide(session={"last_proactive_at": 1000.0 - 60.0}, config=_config(min_interval_minutes=180))
    assert out.should_send is False
    assert out.reason == P.REASON_INTERVAL
    assert "还差" in out.detail


def test_interval_allows_after_enough_time():
    out = _decide(
        session={"last_proactive_at": 1000.0 - 3600.0 * 4},
        config=_config(min_interval_minutes=180),
    )
    assert out.should_send is True


def test_silence_blocks_right_after_user_message():
    out = _decide(session={"last_user_message_at": 1000.0 - 60.0})
    assert out.should_send is False
    assert out.reason == P.REASON_SILENCE


def test_low_score_blocks():
    out = _decide(materials=[_material(weight=0.1)], energy=4.0, emotion=5.0)
    assert out.should_send is False
    assert out.reason == P.REASON_LOW_SCORE
    assert out.score < 0.45


def test_happy_path_returns_intent():
    out = _decide(materials=[_material(weight=0.8)], energy=9.0, emotion=8.0)
    assert out.should_send is True
    assert out.reason == P.REASON_OK
    assert out.intent
    assert out.score >= 0.45


def test_every_block_reason_has_a_label():
    for reason in (
        P.REASON_DISABLED, P.REASON_SLEEPING, P.REASON_LOW_ENERGY, P.REASON_QUIET,
        P.REASON_NO_MATERIAL, P.REASON_DAILY_MAX, P.REASON_INTERVAL, P.REASON_SILENCE,
        P.REASON_LOW_SCORE,
    ):
        assert reason in P.SKIP_REASON_LABELS


# ---------------------------------------------------------------- 会话记录


def test_record_user_message_creates_and_updates():
    sessions: dict[str, dict] = {}
    record = P.record_user_message(sessions, stream_id="s1", now=500.0, day_key="2026-02-08")
    assert record["last_user_message_at"] == 500.0
    assert sessions["s1"]["stream_id"] == "s1"

    P.record_user_message(sessions, stream_id="s1", now=900.0, day_key="2026-02-08")
    assert sessions["s1"]["last_user_message_at"] == 900.0


def test_record_user_message_resets_count_on_new_day():
    sessions = {"s1": {"stream_id": "s1", "day_key": "2026-02-07", "count": 3,
                       "last_user_message_at": 0.0, "last_proactive_at": 0.0}}
    P.record_user_message(sessions, stream_id="s1", now=500.0, day_key="2026-02-08")
    assert sessions["s1"]["count"] == 0
    assert sessions["s1"]["day_key"] == "2026-02-08"


def test_record_proactive_increments_and_rolls_over():
    sessions: dict[str, dict] = {}
    P.record_proactive(sessions, stream_id="s1", now=500.0, day_key="2026-02-08")
    assert sessions["s1"]["count"] == 1
    P.record_proactive(sessions, stream_id="s1", now=600.0, day_key="2026-02-08")
    assert sessions["s1"]["count"] == 2
    assert sessions["s1"]["last_proactive_at"] == 600.0

    P.record_proactive(sessions, stream_id="s1", now=700.0, day_key="2026-02-09")
    assert sessions["s1"]["count"] == 1  # 新的一天重新计数


def test_new_session_record_defaults():
    record = P.new_session_record(stream_id="s9", day_key="2026-02-08")
    assert record["count"] == 0
    assert record["last_proactive_at"] == 0.0


# ---------------------------------------------------------------- 台账


def test_bump_and_render_ledger():
    ledger: dict[str, int] = {}
    P.bump_skip_ledger(ledger, P.REASON_LOW_ENERGY)
    P.bump_skip_ledger(ledger, P.REASON_LOW_ENERGY)
    P.bump_skip_ledger(ledger, P.REASON_SILENCE, amount=3)
    P.bump_skip_ledger(ledger, "skipped")
    lines = P.ledger_lines(ledger)
    assert lines[0] == "对方刚说过话，先不插嘴：3 次"
    assert any("体力低于下限：2 次" == line for line in lines)
    assert any("skipped" in line for line in lines)


def test_ledger_lines_tolerates_bad_values():
    assert P.ledger_lines({"good": 2, "bad": "abc"}) == ["good：2 次"]
    assert P.ledger_lines({}) == []


# ---------------------------------------------------------------- v1.1.1 回归


def test_material_without_text_never_opens():
    """回归：没有正文的素材不该让她主动开话（intent 会退化成纯模板句）。"""

    for bad in ({"label": "小事", "weight": 1.0, "expires_at": 9999.0},
                _material(text="   "),
                _material(text="")):
        assert P.select_material([bad], 1000.0) is None, bad
        decision = _decide(materials=[bad])
        assert decision.should_send is False, bad
        assert decision.reason == P.REASON_NO_MATERIAL


def test_malformed_persisted_state_never_raises():
    """回归：被外部写坏的 ``life_state.json`` 不能把整轮 ``_sim_tick`` 打断。"""

    assert P.select_material([{"expires_at": None, "weight": None, "text": "x"}], 1000.0) is None
    assert P.select_material([{"expires_at": "abc", "weight": "0.9", "text": "x"}], 1000.0) is None
    decision = _decide(materials=[_material()], session=5)          # 非 mapping
    assert isinstance(decision, P.ProactiveDecision)
    decision = _decide(materials=[_material()], session={"count": None, "day_key": "2026-02-08"})
    assert isinstance(decision, P.ProactiveDecision)
    decision = _decide(
        materials=[_material()],
        session={"count": 5, "day_key": "2026-02-08", "last_proactive_at": "abc"},
    )
    assert isinstance(decision, P.ProactiveDecision)


def test_missing_day_key_does_not_reset_daily_max():
    """回归：会话记录缺 ``day_key`` 时按今天算，``daily_max`` 不许静默失效。"""

    decision = _decide(
        config=_config(daily_max=1),
        materials=[_material(weight=1.0)],
        session={"count": 5, "day_key": ""},
    )
    assert decision.should_send is False
    assert decision.reason == P.REASON_DAILY_MAX


def test_records_are_rebuilt_from_garbage():
    sessions = {"s1": "not-a-mapping"}
    P.record_user_message(sessions, stream_id="s1", now=1000.0, day_key="2026-02-08")
    assert sessions["s1"]["last_user_message_at"] == pytest.approx(1000.0)
    P.record_proactive(sessions, stream_id="s1", now=1000.0, day_key="2026-02-08")
    assert sessions["s1"]["count"] == 1


# ---------------------------------------------------------------- G1 未回应退避


def test_unanswered_backoff_lengthens_interval_private_only():
    """G1：对方连续不回应 → 间隔 ×2^连击 拉长（封顶）；仅私聊；回话立即归零。"""

    record = {
        "last_proactive_at": 900.0,        # 距 now=1000 只有 100 秒
        "last_user_message_at": 0.0,       # 主动之后对方一直没说话
        "unanswered_streak": 0,
        "day_key": "2026-02-08",
        "count": 0,
    }
    out = _decide(session=record, private_chat=True)
    assert out.reason == P.REASON_INTERVAL
    assert "未回应退避 ×2" in out.detail    # 第 1 次未回应：0+1 → 间隔 ×2

    once = dict(record, unanswered_streak=1)
    out = _decide(session=once, private_chat=True)
    assert "×4" in out.detail               # 连击 1+1 → ×4

    capped = dict(record, unanswered_streak=9)
    out = _decide(session=capped, private_chat=True)
    assert "×16" in out.detail              # 连击封顶 2^4 = 48 小时

    out = _decide(session=record)           # 群聊 / 未传 private_chat：不退避
    assert out.reason == P.REASON_INTERVAL
    assert "退避" not in out.detail

    replied = dict(record, last_user_message_at=950.0)   # 主动之后对方说过话
    out = _decide(session=replied, private_chat=True)
    assert out.reason == P.REASON_INTERVAL
    assert "退避" not in out.detail

    off = _decide(session=record, private_chat=True,
                  config=_config(unanswered_backoff_factor=1.0))
    assert "退避" not in off.detail          # 系数 1 = 关闭


def test_record_proactive_tracks_unanswered_streak():
    sessions: dict = {}
    P.record_user_message(sessions, stream_id="s1", now=1000.0, day_key="2026-02-08")
    P.record_proactive(sessions, stream_id="s1", now=2000.0, day_key="2026-02-08")
    assert sessions["s1"]["unanswered_streak"] == 0     # 第 1 次主动：此前没有未回应
    P.record_proactive(sessions, stream_id="s1", now=3000.0, day_key="2026-02-08")
    assert sessions["s1"]["unanswered_streak"] == 1     # 第 1 次主动无人回应 → 连击 1
    P.record_user_message(sessions, stream_id="s1", now=4000.0, day_key="2026-02-08")
    P.record_proactive(sessions, stream_id="s1", now=5000.0, day_key="2026-02-08")
    assert sessions["s1"]["unanswered_streak"] == 0     # 对方回过话 → 归零


def test_backoff_level_is_observable_in_the_ledger():
    """退避档位必须能被用户看见：``decision.backoff`` + 台账复合键渲染。

    没有这一条，退避就是**静默生效**的——``/生活 为什么`` 只会显示一个笼统的
    「距上次主动太近」，用户无从判断她是在正常冷却还是已经被降温到隔天一次。
    """

    record = {
        "last_proactive_at": 900.0,
        "last_user_message_at": 0.0,
        "unanswered_streak": 1,
        "day_key": "2026-02-08",
        "count": 0,
    }
    out = _decide(session=record, private_chat=True)
    assert out.reason == P.REASON_INTERVAL
    assert out.backoff == pytest.approx(4.0)        # 连击 1+1 → ×4

    group = _decide(session=record)                 # 群聊不退避
    assert group.backoff == pytest.approx(1.0)

    label = P.SKIP_REASON_LABELS[P.REASON_INTERVAL]
    lines = P.ledger_lines({f"{P.REASON_INTERVAL}×4": 3, P.REASON_INTERVAL: 5})
    assert f"{label} ×4：3 次" in lines
    assert f"{label}：5 次" in lines
