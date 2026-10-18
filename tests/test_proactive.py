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


def test_unanswered_backoff_lengthens_interval_private_and_group():
    """G1：对方连续不回应 → 间隔 ×2^连击 拉长（封顶）；回话立即归零。

    v1.22.2 起**群聊也退避**（``unanswered_backoff_group`` 默认开）：允许她去活跃冷群
    之后，「群里总会有人接」这个前提反过来了——没人的群最不可能有人接。置
    ``unanswered_backoff_group=False`` 精确回到 v1.22.1 的「仅私聊」（下面有断言）。
    """

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

    out = _decide(session=record)           # 群聊（private_chat 默认 False）：v1.22.2 起也退避
    assert out.reason == P.REASON_INTERVAL
    assert "未回应退避 ×2" in out.detail

    legacy = _decide(session=record, config=_config(unanswered_backoff_group=False))
    assert "退避" not in legacy.detail        # 关掉开关 = 回到仅私聊

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

    group = _decide(session=record)                 # 群聊也退避（v1.22.2 默认）
    assert group.backoff == pytest.approx(4.0)

    legacy = _decide(session=record, config=_config(unanswered_backoff_group=False))
    assert legacy.backoff == pytest.approx(1.0), "关掉开关 = 回到仅私聊"

    label = P.SKIP_REASON_LABELS[P.REASON_INTERVAL]
    lines = P.ledger_lines({f"{P.REASON_INTERVAL}×4": 3, P.REASON_INTERVAL: 5})
    assert f"{label} ×4：3 次" in lines
    assert f"{label}：5 次" in lines


# ---------------------------------------------------------------- v1.18.0 四道新闸
#
# 来自 mohobot ``plugins/proactive_chat``（AGPL-3.0，只借机制）的四条：
# 失败要冷却、受理≠发送要核实、间隔要抖动、止损要有硬上限。


def test_attempt_cooldown_blocks_then_expires():
    now = 1000.0
    blocked = _decide(now=now, session={"last_attempt_at": now - 300.0})
    assert blocked.reason == P.REASON_ATTEMPT_COOLDOWN
    assert "分钟" in blocked.detail
    # 冷却 15 分钟：1000 秒前失败 ⇒ 已经过了
    assert _decide(now=now, session={"last_attempt_at": now - 1000.0}).should_send


def test_attempt_cooldown_can_be_disabled():
    now = 1000.0
    decision = _decide(config=_config(failure_cooldown_minutes=0), now=now,
                       session={"last_attempt_at": now - 1.0})
    assert decision.should_send


def test_a_later_success_clears_the_cooldown():
    """``last_attempt_at`` 比 ``last_proactive_at`` 旧 ⇒ 上次失败之后已经成功过。"""

    now = 1000.0
    outcome = _decide(now=now, session={"last_attempt_at": now - 60.0,
                                        "last_proactive_at": now - 30.0})
    assert outcome.reason == P.REASON_INTERVAL


def test_record_attempt_only_moves_the_cooldown_anchor():
    """没有发出去的尝试不该消耗配额、不该记连击、不该推后间隔闸。"""

    sessions: dict = {}
    P.record_proactive(sessions, stream_id="s1", now=100.0, day_key="d")
    before = dict(sessions["s1"])
    P.record_attempt(sessions, stream_id="s1", now=200.0, day_key="d")
    after = sessions["s1"]
    assert after["last_attempt_at"] == 200.0
    for key in ("count", "unanswered_streak", "last_proactive_at"):
        assert after[key] == before[key], f"{key} 不该被一次没发出去的尝试改动"


def test_pending_verify_blocks_until_it_is_resolved():
    """只要上一次开口还挂着（没核销），pending 就拦住——**不看**它到没到点。

    核销在每轮判定**之前**跑（``_verify_pending_proactive``）：能核销的早就核销掉了，
    还挂着的（本轮配额不够 / 形状坏掉）恰恰是该拦住这个会话的情况。
    """

    now = 1000.0
    not_due = {"pending_verify": {"trigger_at": now - 60.0, "verify_after": now + 60.0}}
    assert _decide(now=now, session=not_due).reason == P.REASON_PENDING
    # 已到点但（因为配额等原因）还没核销：同样拦住，否则空窗里会连开两枪
    due = {"pending_verify": {"trigger_at": now - 600.0, "verify_after": now - 1.0}}
    assert _decide(now=now, session=due).reason == P.REASON_PENDING
    # 核销掉（或形状坏掉被丢弃）之后立刻放行
    assert _decide(now=now, session={"pending_verify": None}).should_send


def test_pending_verify_helpers_round_trip():
    sessions: dict = {}
    P.record_pending_verify(sessions, stream_id="s1", trigger_at=10.0,
                            verify_after=20.0, day_key="2026-10-11")
    assert P.pending_verify_of(sessions["s1"]) == {
        "trigger_at": 10.0, "verify_after": 20.0, "day_key": "2026-10-11",
    }
    P.clear_pending_verify(sessions["s1"])
    assert P.pending_verify_of(sessions["s1"]) is None
    # 形状坏掉一律当没有（被外部写坏的状态文件不能让闸门抛错）
    assert P.pending_verify_of(None) is None
    assert P.pending_verify_of({}) is None
    assert P.pending_verify_of({"pending_verify": "不是字典"}) is None
    assert P.pending_verify_of({"pending_verify": {"trigger_at": "坏", "verify_after": 1.0}}) is None
    assert P.pending_verify_of({"pending_verify": {"trigger_at": -1.0, "verify_after": 2.0}}) is None
    assert P.pending_verify_of({"pending_verify": {"trigger_at": 1.0, "verify_after": 0.0}}) is None


def test_record_proactive_clears_the_pending_marker():
    """核销落地（不管成功还是失败）都必须把待核实标记清掉，否则闸门永久卡住。"""

    sessions: dict = {}
    P.record_pending_verify(sessions, stream_id="s1", trigger_at=10.0,
                            verify_after=20.0, day_key="d")
    P.record_proactive(sessions, stream_id="s1", now=10.0, day_key="d")
    assert "pending_verify" not in sessions["s1"]
    P.record_pending_verify(sessions, stream_id="s1", trigger_at=30.0,
                            verify_after=40.0, day_key="d")
    P.record_attempt(sessions, stream_id="s1", now=35.0, day_key="d")
    assert "pending_verify" not in sessions["s1"]


def test_interval_jitter_is_off_by_default_and_deterministic():
    assert P.interval_jitter("2026-10-11|s1|0", 0.0) == 1.0
    assert P.interval_jitter("2026-10-11|s1|0", -1.0) == 1.0
    one = P.interval_jitter("2026-10-11|s1|0", 0.15)
    assert 1.0 <= one < 1.15
    # 同一种子恒同倍率（tick 间不重掷、重启一致）
    assert P.interval_jitter("2026-10-11|s1|0", 0.15) == one
    # 比例超范围被夹到 1.0
    assert P.interval_jitter("2026-10-11|s1|0", 999.0) < 2.0
    assert P.interval_jitter("2026-10-11|s1|0", "坏值") == 1.0


def test_interval_jitter_spreads_across_sessions_and_days():
    same_day = {P.interval_jitter(f"2026-10-11|s{i}|0", 0.15) for i in range(8)}
    assert len(same_day) > 1, "抖动应该在会话之间散开"
    days = {P.interval_jitter(f"2026-10-{day:02d}|s1|0", 0.15) for day in range(1, 8)}
    assert len(days) > 1, "抖动应该按天变化"


def test_interval_gate_honours_the_jitter_ceiling():
    """抖动只把间隔**拉长**：基础间隔到头仍被拦，抖动上界之后才放行。"""

    config = _config(min_interval_minutes=180, interval_jitter_ratio=0.15)
    # ``last_user_message_at`` 与 ``last_proactive_at`` 同为 10000：**对方后来接过话**，
    # 未回应退避不成立（v1.22.2 起群聊也退避，不补这一笔会把 ×2 混进抖动断言里）
    session = {"stream_id": "s1", "day_key": "2026-10-11", "count": 0,
               "last_user_message_at": 10000.0}
    seed_ratio = P.interval_jitter("2026-10-11|s1|0", 0.15)
    base = 180 * 60.0
    at_base = 10000.0 + base
    assert _decide(config=config, now=at_base, day_key="2026-10-11",
                   materials=[_material(now=at_base)],
                   session={**session, "last_proactive_at": 10000.0}).reason == P.REASON_INTERVAL
    at_ceiling = 10000.0 + base * seed_ratio
    assert _decide(config=config, now=at_ceiling + 1.0, day_key="2026-10-11",
                   materials=[_material(now=at_ceiling + 1.0)],
                   session={**session, "last_proactive_at": 10000.0}).should_send


def test_interval_jitter_is_visible_in_the_detail():
    config = _config(min_interval_minutes=180, interval_jitter_ratio=0.15)
    now = 10000.0 + 180 * 60.0
    outcome = _decide(config=config, now=now, day_key="2026-10-11",
                      materials=[_material(now=now)],
                      session={"stream_id": "s1", "last_proactive_at": 10000.0})
    assert outcome.reason == P.REASON_INTERVAL
    assert "抖动" in outcome.detail


#: 未回应上限的时间锚点：必须是一个**正数**的真实 epoch。``decide`` 的退避段要求
#: ``last_proactive_at > 0``（0 = 从没主动过），拿 10000.0 去减 10 天会变成负数，
#: 闸门会被整条跳过——测试就变成假绿。
_CAP_NOW = 1_700_000_000.0


def _unanswered_session(now: float, streak: int) -> dict:
    return {
        "last_proactive_at": now - 10 * 86400.0,
        "last_user_message_at": now - 11 * 86400.0,
        "unanswered_streak": streak,
        "day_key": "2026-10-11",
        "count": 0,
    }


def test_unanswered_cap_stops_at_the_configured_streak():
    now = _CAP_NOW
    config = _config(max_unanswered_times=4)
    # streak=3 ⇒ 这一次是第 4 次 ⇒ 仍放行；streak=4 ⇒ 第 5 次 ⇒ 停
    assert _decide(config=config, now=now, materials=[_material(now=now)],
                   session=_unanswered_session(now, 3), private_chat=True).should_send
    capped = _decide(config=config, now=now, materials=[_material(now=now)],
                     session=_unanswered_session(now, 4), private_chat=True)
    assert capped.reason == P.REASON_UNANSWERED_CAP
    assert "4 次" in capped.detail


def test_unanswered_cap_is_off_by_default_and_covers_groups_too():
    now = _CAP_NOW
    session = _unanswered_session(now, 9)
    material = [_material(now=now)]
    # 默认 0 = 关：退避封顶后她仍能隔天说一句（私聊与群聊都一样）
    assert _decide(now=now, materials=material, session=session, private_chat=True).should_send
    assert _decide(now=now, materials=material, session=session, private_chat=False).should_send
    # v1.22.2：上限与退避同一适用面，群聊也生效
    capped = _decide(config=_config(max_unanswered_times=4), now=now, materials=material,
                     session=session, private_chat=False)
    assert capped.reason == P.REASON_UNANSWERED_CAP
    # 关掉群聊退避 = 上限也不在群聊生效（回到 v1.22.1）
    legacy = _decide(now=now, materials=material, session=session, private_chat=False,
                     config=_config(max_unanswered_times=4, unanswered_backoff_group=False))
    assert legacy.should_send


def test_unanswered_cap_recovers_when_the_user_answers():
    now = _CAP_NOW
    session = dict(_unanswered_session(now, 9), last_user_message_at=now - 5.0)
    outcome = _decide(config=_config(max_unanswered_times=4), now=now,
                      materials=[_material(now=now)], session=session, private_chat=True)
    assert outcome.reason == P.REASON_SILENCE, "对方刚回话 ⇒ 上限不成立，转由沉默闸解释"


def test_unanswered_cap_is_not_confused_by_the_backoff_ceiling():
    """硬上限比的是**真实连击**，不能被退避封顶（max_streak）掩盖。"""

    now = _CAP_NOW
    config = _config(max_unanswered_times=6, unanswered_backoff_max_streak=4)
    session = _unanswered_session(now, 6)
    assert _decide(config=config, now=now, materials=[_material(now=now)],
                   session=session, private_chat=True).reason == P.REASON_UNANSWERED_CAP


def test_new_reason_labels_are_present():
    for key in (P.REASON_PENDING, P.REASON_ATTEMPT_COOLDOWN,
                P.REASON_UNANSWERED_CAP, P.LEDGER_SENT, P.LEDGER_NOT_SENT):
        assert P.SKIP_REASON_LABELS.get(key), f"{key} 缺少中文标签"
