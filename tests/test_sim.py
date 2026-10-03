# -*- coding: utf-8 -*-
"""L3：状态机——睡眠记账、熬夜判定、情绪体力动力学、身体、日期、持久化。"""

import random
from datetime import datetime, timezone

import pytest

import life_activity as A
import life_events as E
import life_sim as S

TZ = 480


def ts(year, month, day, hour, minute=0):
    """构造一个 epoch，使 ``local_datetime`` 恰好得到给定的本地墙钟时间。"""

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


# ---------------------------------------------------------------- 生活日


@pytest.mark.parametrize(
    "hour,expected",
    [(0, "2026-02-07"), (3, "2026-02-07"), (11, "2026-02-07"), (12, "2026-02-08"), (23, "2026-02-08")],
)
def test_day_key_of_boundary(hour, expected):
    local = datetime(2026, 2, 8, hour, 30, tzinfo=timezone.utc)
    assert S.day_key_of(local, 12) == expected


def test_local_datetime_roundtrip():
    at = ts(2026, 2, 8, 19, 30)
    assert S.local_datetime(at, TZ).strftime("%Y-%m-%d %H:%M") == "2026-02-08 19:30"


# ---------------------------------------------------------------- 冷启动


def test_new_state_uses_rule_table_seed():
    state = S.new_state(now=ts(2026, 2, 8, 14, 0), config=cfg(), energy=8.0)
    assert state.activity == A.DAILY
    assert state.activity_source == A.SOURCE_COLD_START
    assert state.last_tick_at == ts(2026, 2, 8, 14, 0)


def test_new_state_sleeps_in_window_when_tired():
    state = S.new_state(now=ts(2026, 2, 8, 4, 0), config=cfg(), energy=2.0)
    assert state.activity == A.SLEEP
    assert state.sleep_started_at == ts(2026, 2, 8, 4, 0)


# ---------------------------------------------------------------- 记账


def test_settle_counts_sleep_minutes():
    at = ts(2026, 2, 8, 4, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at)
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(1))
    assert state.sleep_minutes_today == 10
    assert state.awake_minutes_today == 0


def test_settle_counts_awake_minutes():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, activity=A.DAILY)
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(1))
    assert state.awake_minutes_today == 10
    assert state.sleep_minutes_today == 0


def test_settle_never_double_counts_sleep_when_waking():
    """回归测试：醒来时不能再把同一段睡眠记第二遍。"""

    at = ts(2026, 2, 8, 4, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at)
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(1))
    assert state.sleep_minutes_today == 10

    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
        now=at + 600,
        config=cfg(),
    )
    assert state.sleep_minutes_today == 10  # 不是 70/20
    assert state.sleep_started_at == 0.0


# ---------------------------------------------------------------- 熬夜与体力上限


def test_short_sleep_counts_as_staying_up():
    """睡不够就记一笔熬夜：判据是「最近 24 小时累计」，且只在**睡醒时**判。"""

    at = ts(2026, 2, 8, 4, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at)
    S.settle(state, now=at + 100 * 60, config=cfg(), events=[], rng=random.Random(1))
    assert state.sleep_debt_nights == 0, "还睡着，不该判"

    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
        now=at + 100 * 60,
        config=cfg(),
    )
    assert state.sleep_debt_nights == 1, "只睡了 100 分钟 < 300"
    assert state.energy_cap == pytest.approx(10.0), "1 天还不够压低上限"


def test_enough_sleep_resets_debt():
    at = ts(2026, 2, 8, 4, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, sleep_debt_nights=2)
    S.settle(state, now=at + 8 * 3600, config=cfg(), events=[], rng=random.Random(1))
    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
        now=at + 8 * 3600,
        config=cfg(),
    )
    assert state.sleep_debt_nights == 0
    assert state.energy_cap == pytest.approx(10.0)


def test_sleep_debt_ignores_life_day_boundary():
    """回归（真机 2026-10-02）：08:00 睡到 16:00（8 小时）不该被判熬夜。

    旧判据在 12:00 边界只看得到 08:00→12:00 这 4 小时，于是判了「连熬 1 天」；
    新判据看整段睡眠（滑动 24 小时窗口），不判。
    """

    start = ts(2026, 10, 2, 8, 0)
    end = ts(2026, 10, 2, 16, 0)
    state = make_state(at=start, activity=A.SLEEP, sleep_started_at=start)
    S.settle(
        state, now=end, config=cfg(offline_gap_minutes=0), events=[], rng=random.Random(1)
    )
    assert state.sleep_minutes_today == 240, "本生活日只记到 4 小时（12:00 边界）"
    assert state.sleep_debt_nights == 0, "边界不再判分"

    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
        now=end,
        config=cfg(),
    )
    assert state.sleep_debt_nights == 0, "这一觉 8 小时，够"


def test_three_sleepless_nights_lower_energy_cap():
    """连续三次「最近 24 小时睡眠不足」⇒ 体力上限被压到 8.5。"""

    config = cfg()
    state = make_state(at=ts(2026, 2, 8, 12, 5), activity=A.DAILY)
    for index in range(3):
        start = ts(2026, 2, 8 + index, 12, 5)
        state.activity = A.SLEEP
        state.sleep_started_at = start
        state.energy = 9.5
        S.settle(state, now=start + 3600, config=config, events=[], rng=random.Random(1))
        S.apply_activity(
            state,
            A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
            now=start + 3600,
            config=config,
        )
    assert state.sleep_debt_nights == 3
    assert state.energy_cap == pytest.approx(8.5)
    assert state.energy <= 8.5


def test_sleep_debt_threshold_is_configurable():
    strict = cfg(sleep_debt_threshold_minutes=480)
    at = ts(2026, 2, 8, 4, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at)
    S.settle(state, now=at + 7 * 3600, config=strict, events=[], rng=random.Random(1))
    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
        now=at + 7 * 3600,
        config=strict,
    )
    assert state.sleep_debt_nights == 1  # 420 分钟 < 480 分钟


def test_split_sleep_within_24h_is_counted_together():
    """拆成两段睡（3 小时 + 3 小时）在滑动窗口下合起来算，最终不记熬夜。"""

    config = cfg()
    first = ts(2026, 10, 2, 2, 0)
    state = make_state(at=first, activity=A.SLEEP, sleep_started_at=first)
    S.settle(state, now=first + 3 * 3600, config=config, events=[], rng=random.Random(1))
    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
        now=first + 3 * 3600,
        config=config,
    )
    assert state.sleep_debt_nights == 1, "第一段只有 3 小时，先记一笔"

    second = first + 6 * 3600
    state.activity = A.SLEEP
    state.sleep_started_at = second
    S.settle(state, now=second + 3 * 3600, config=config, events=[], rng=random.Random(1))
    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
        now=second + 3 * 3600,
        config=config,
    )
    assert state.sleep_debt_nights == 0, "两段合计 6 小时 ≥ 5，归零"


# ---------------------------------------------------------------- 体力


def test_energy_drains_while_studying():
    at = ts(2026, 2, 8, 22, 0)
    state = make_state(at=at, activity=A.NIGHT_STUDY, energy=8.0)
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(1))
    assert state.energy == pytest.approx(8.0 - 1.2 / 6, rel=1e-6)


def test_energy_recovers_while_sleeping():
    at = ts(2026, 2, 8, 4, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, energy=4.0)
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(1))
    assert state.energy == pytest.approx(4.2, rel=1e-6)


def test_energy_respects_cap_after_sleep_debt():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, activity=A.DAILY, energy=9.0, sleep_debt_nights=3, energy_cap=8.5)
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(1))
    assert state.energy <= 8.5


# ---------------------------------------------------------------- 情绪


def test_inertia_blocks_regression_then_releases():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, activity=A.DAILY, emotion=1.0, inertia_until=at + 2400)
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(1))
    assert state.emotion == pytest.approx(1.0)  # 惰性期内不动

    state.last_tick_at = at + 600
    state.inertia_until = 0.0
    S.settle(state, now=at + 1200, config=cfg(), events=[], rng=random.Random(1))
    assert state.emotion == pytest.approx(1.2)  # 每 10 分钟回归 0.2


def test_sleep_doubles_emotion_recovery():
    at = ts(2026, 2, 8, 4, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at, emotion=1.0, inertia_until=0.0)
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(1))
    assert state.emotion == pytest.approx(1.4)


def test_regression_snaps_to_baseline_without_overshoot():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.DAILY, emotion=4.9, inertia_until=0.0,
        recent_events=[{"at": at, "emotion": 0.0, "label": "x", "text": "x"}],
    )
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(1))
    assert state.emotion == pytest.approx(5.0)


@pytest.mark.parametrize("delta,expected", [(100.0, 0.6), (-100.0, -0.6), (2.0, 0.3)])
def test_afterglow_is_clamped(delta, expected):
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.DAILY,
        recent_events=[{"at": at, "emotion": delta, "label": "x", "text": "x"}],
    )
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(90))
    assert state.afterglow == pytest.approx(expected)


def test_afterglow_ignores_events_older_than_span():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.DAILY,
        recent_events=[{"at": at - 30 * 3600, "emotion": 3.0, "label": "x", "text": "x"}],
    )
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(90))
    assert state.afterglow == pytest.approx(0.0)


# ---------------------------------------------------------------- 事件与素材


def test_event_roll_creates_material_and_inertia():
    at = ts(2026, 2, 8, 22, 0)
    state = make_state(at=at, activity=A.NIGHT_STUDY, emotion=5.0, energy=8.0)
    config = cfg(fire_probability=1.0)
    S.settle(
        state, now=at + 600, config=config, events=list(E.BUILTIN_EVENTS), rng=random.Random(5)
    )
    assert len(state.materials) == 1
    assert len(state.recent_events) == 1
    assert state.inertia_until > at
    assert state.recent_events[0]["activity"] == A.NIGHT_STUDY


def test_no_events_while_asleep():
    at = ts(2026, 2, 8, 4, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=at)
    S.settle(
        state, now=at + 3600 * 3, config=cfg(fire_probability=1.0),
        events=list(E.BUILTIN_EVENTS), rng=random.Random(5),
    )
    assert state.materials == []
    assert state.recent_events == []


def test_materials_expire_and_are_swept():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.DAILY,
        materials=[
            {"label": "旧", "text": "过期了", "weight": 0.5, "created_at": at - 40000,
             "expires_at": at - 1},
            {"label": "新", "text": "还有效", "weight": 0.4, "created_at": at - 60,
             "expires_at": at + 3600},
        ],
    )
    S.settle(state, now=at + 600, config=cfg(), events=[], rng=random.Random(1))
    assert [item["label"] for item in state.materials] == ["新"]


def test_active_materials_sorted_by_weight():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.DAILY,
        materials=[
            {"label": "轻", "text": "a", "weight": 0.2, "expires_at": at + 60},
            {"label": "重", "text": "b", "weight": 0.9, "expires_at": at + 60},
            {"label": "过期", "text": "c", "weight": 0.99, "expires_at": at - 60},
        ],
    )
    ordered = [item["label"] for item in S.active_materials(state, at)]
    assert ordered == ["重", "轻"]


def test_recent_event_tiers_split_near_mid_far():
    """近 / 中 / 远三层的时间窗：越近越是当前状态的成因，太久的不进提示词。"""

    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.DAILY,
        recent_events=[
            {"at": at - 30 * 24 * 3600, "label": "远古", "text": "上个月的事"},
            {"at": at - 5 * 24 * 3600, "label": "远", "text": "五天前"},
            {"at": at - 24 * 3600, "label": "中", "text": "昨天"},
            {"at": at - 3600, "label": "近", "text": "一小时前"},
        ],
    )
    tiers = S.recent_event_tiers(state, now=at, limit=9, tz_offset_minutes=TZ)
    labels = [label for label, _ in tiers]
    assert labels[0].startswith("近（12 小时内）"), labels
    assert labels[1].startswith("中（3 天内）"), labels
    assert labels[2].startswith("远（14 天内）"), labels

    by_tier = dict(tiers)
    assert [line.rsplit(" ", 1)[-1] for line in by_tier[labels[0]]] == ["近：一小时前"]
    assert [line.rsplit(" ", 1)[-1] for line in by_tier[labels[1]]] == ["中：昨天"]
    assert [line.rsplit(" ", 1)[-1] for line in by_tier[labels[2]]] == ["远：五天前"]
    # 超过「远」层窗口的旧事不进提示词（状态里仍保留给情绪余波用）
    assert all("远古" not in line for _, lines in tiers for line in lines)


def test_recent_event_tiers_are_ordered_and_local_time():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.DAILY,
        recent_events=[
            {"at": at - 600, "label": "后", "text": "新的"},
            {"at": at - 3600, "label": "先", "text": "<b>旧</b>"},
        ],
    )
    tiers = S.recent_event_tiers(state, now=at, limit=8, tz_offset_minutes=TZ)
    near = dict(tiers)[tiers[0][0]]
    assert len(near) == 2
    assert "先" in near[0] and "后" in near[1], near        # 层内按时间升序
    assert "<b>" not in near[0]                             # 仍然净化
    assert near[1].startswith("02-08 13:50"), near           # 本地时区（UTC+8），不是 UTC
    assert all(lines == () for _, lines in tiers[1:]), tiers  # 空层保持空


@pytest.mark.parametrize(
    "limit,expected",
    [
        (0, (0, 0, 0)), (1, (1, 0, 0)), (2, (2, 0, 0)),
        (3, (1, 1, 1)), (4, (2, 1, 1)), (8, (4, 2, 2)), (9, (4, 3, 2)),
    ],
)
def test_tier_quotas(limit, expected):
    assert S._tier_quotas(limit) == expected


def test_recent_event_tiers_respect_the_total_limit():
    """总条数上限；以及空层配额回收（默认 smart）与旧行为（``pick="recent"``）的差别。"""

    at = ts(2026, 2, 8, 14, 0)
    events = [{"at": at - index * 600, "label": f"e{index}", "text": "x"} for index in range(20)]
    state = make_state(at=at, activity=A.DAILY, recent_events=events)

    smart = S.recent_event_tiers(state, now=at, limit=8, tz_offset_minutes=TZ)
    total = sum(len(lines) for _, lines in smart)
    assert total <= 8, total
    # 20 条全在「近」层 ⇒ 中/远两层没货，它们的配额按「近 → 中 → 远」回收
    assert len(dict(smart)[smart[0][0]]) == 8, "空层的配额应当回收给有货的层"

    old = S.recent_event_tiers(state, now=at, limit=8, tz_offset_minutes=TZ, pick="recent")
    assert len(dict(old)[old[0][0]]) == 4, "旧行为是固定 4/2/2，近层只拿 4 条"
    assert sum(len(lines) for _, lines in old) == 4, "旧行为把空层的 4 个位置白扔了"


def test_smart_pick_prefers_the_strongest_causes_over_the_newest():
    """层内按情绪/体力的**变化量**排序：她此刻为什么是这个状态，比「刚刚发生」更重要。"""

    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.DAILY,
        recent_events=[
            # 四件琐事（刚发生、情绪几乎为零）会把配额吃光
            {"at": at - 600, "label": "琐事1", "text": "x", "emotion": 0.1},
            {"at": at - 1200, "label": "琐事2", "text": "x", "emotion": 0.1},
            {"at": at - 1800, "label": "琐事3", "text": "x", "emotion": 0.1},
            {"at": at - 2400, "label": "琐事4", "text": "x", "emotion": 0.1},
            # 真正解释她现在情绪的成因（11 小时前，但情绪冲击最大）
            {"at": at - 11 * 3600, "label": "删了存档", "text": "手滑", "emotion": -1.2},
        ],
    )
    smart = S.recent_event_tiers(state, now=at, limit=4, tz_offset_minutes=TZ)
    lines = [line for _, group in smart for line in group]
    assert any("删了存档" in line for line in lines), lines
    # 旧行为只看时间 ⇒ 它进不来（这就是真机上「最强成因只有 15% 概率出现」的那条）
    old = S.recent_event_tiers(state, now=at, limit=4, tz_offset_minutes=TZ, pick="recent")
    old_lines = [line for _, group in old for line in group]
    assert all("删了存档" not in line for line in old_lines), old_lines


def test_smart_pick_dedupes_labels_within_a_tier_but_not_across_tiers():
    """同标签在**每层内**只出现一次；跨层允许重复（那是「连着几天都这样」的信号）。"""

    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.DAILY,
        recent_events=[
            {"at": at - 600, "label": "支线通关", "text": "三次", "emotion": 0.8},
            {"at": at - 1200, "label": "支线通关", "text": "三次", "emotion": 0.8},
            {"at": at - 1800, "label": "支线通关", "text": "三次", "emotion": 0.8},
            {"at": at - 2400, "label": "抽卡出货", "text": "出货", "emotion": 1.3},
            # 昨天也通关了一次 ⇒ 允许出现在「中」层
            {"at": at - 30 * 3600, "label": "支线通关", "text": "三次", "emotion": 0.8},
            {"at": at - 31 * 3600, "label": "队友离谱", "text": "离谱", "emotion": -0.7},
        ],
    )
    tiers = S.recent_event_tiers(state, now=at, limit=8, tz_offset_minutes=TZ)
    labels = [(tier_label, [line.rsplit(" ", 1)[-1].split("：")[0] for line in group])
              for tier_label, group in tiers]
    near = dict(labels)[tiers[0][0]]
    mid = dict(labels)[tiers[1][0]]
    assert near.count("支线通关") == 1, near
    assert len(near) == len(set(near)), f"同层内不许重复标签：{near}"
    assert "支线通关" in mid, f"跨层重复是有意的（连着几天都在做同一件事）：{mid}"

    # max_per_label=0 关掉去重 ⇒ 同层内重复回来
    off = S.recent_event_tiers(
        state, now=at, limit=8, tz_offset_minutes=TZ, max_per_label=0
    )
    off_near = [line.rsplit(" ", 1)[-1].split("：")[0] for line in dict(off)[off[0][0]]]
    assert off_near.count("支线通关") > 1, off_near


def test_tier_pick_falls_back_to_smart_on_unknown_value():
    """配置写错（拼错/留空）按 smart 处理，不静默退回旧行为。"""

    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.DAILY,
        recent_events=[{"at": at - 600 * i, "label": f"e{i}", "text": "x"} for i in range(20)],
    )
    for value in ("", "  ", "smart", "SMART", "写错了"):
        tiers = S.recent_event_tiers(
            state, now=at, limit=8, tz_offset_minutes=TZ, pick=value
        )
        assert len(dict(tiers)[tiers[0][0]]) == 8, value
    # 只有显式 recent（大小写无关）才回退旧行为
    for value in ("recent", "RECENT", " recent "):
        tiers = S.recent_event_tiers(
            state, now=at, limit=8, tz_offset_minutes=TZ, pick=value
        )
        assert len(dict(tiers)[tiers[0][0]]) == 4, value


def test_recent_event_tiers_handle_boundaries_and_bad_stamps():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.DAILY,
        recent_events=[
            {"at": at - 12 * 3600, "label": "边界", "text": "正好 12 小时"},
            {"at": at + 600, "label": "未来", "text": "时钟回拨"},
            {"at": "abc", "label": "坏时间", "text": "x"},
            {"at": None, "label": "没时间", "text": "x"},
            {"label": "缺字段", "text": "x"},
            "不是字典",
        ],
    )
    tiers = S.recent_event_tiers(state, now=at, limit=8, tz_offset_minutes=TZ)
    near = dict(tiers)[tiers[0][0]]
    assert len(near) == 2, near            # 边界那条算「近」；未来时间戳按刚发生算
    assert all("坏时间" not in line and "没时间" not in line for line in near), near
    assert S.recent_event_tiers(state, now=at, limit=0) == ()


def test_recent_event_tiers_survive_hostile_state():
    """状态可被手改坏：非字典条目、缺 at、at 是字符串、at 是天文数字都不许抛。

    ``at = 1e12``（公元 33658 年）在 Windows 上会让 ``datetime.fromtimestamp`` 抛
    ``OSError``；取值链上没有异常出口，一旦抛出就是整个 tick 中断（活动决策与倍率
    同步一起停）。
    """

    state = S.LifeState()
    state.recent_events = [{"at": "abc"}, None, 42, {"at": 1e12, "text": "x"}]  # type: ignore[list-item]
    for limit in (0, 1, 8):
        tiers = S.recent_event_tiers(state, now=1_800_000_000.0, limit=limit, tz_offset_minutes=TZ)
        assert isinstance(tiers, tuple)
    lines = [line for _, items in S.recent_event_tiers(
        state, now=1_800_000_000.0, limit=8, tz_offset_minutes=TZ) for line in items]
    assert any("时间未知" in line for line in lines), lines


# ---------------------------------------------------------------- 身体：感冒


def test_cold_roll_can_trigger_and_is_bounded():
    at = ts(2026, 2, 8, 2, 5)
    state = make_state(at=at, activity=A.DAILY)
    config = cfg(cold_base_risk=1.0, cold_min_days=1, cold_max_days=3)
    S.settle(state, now=at + 600, config=config, events=[], rng=random.Random(4))
    assert S.is_cold(state, at + 600) is True
    assert 1 <= state.cold_days <= 3
    assert state.cold_until > at + 600


def test_cold_roll_is_skipped_when_risk_is_zero():
    at = ts(2026, 2, 8, 2, 5)
    state = make_state(at=at, activity=A.DAILY)
    config = cfg(cold_base_risk=0.0, cold_sleep_debt_risk=0.0)
    S.settle(state, now=at + 600, config=config, events=[], rng=random.Random(4))
    assert S.is_cold(state, at + 600) is False
    assert state.cold_checked_day == "2026-02-07"


def test_cold_roll_happens_once_per_day():
    at = ts(2026, 2, 8, 2, 5)
    state = make_state(at=at, activity=A.DAILY)
    config = cfg(cold_base_risk=1.0)
    S.settle(state, now=at + 600, config=config, events=[], rng=random.Random(4))
    first_day = state.cold_checked_day
    S.settle(state, now=at + 1200, config=config, events=[], rng=random.Random(4))
    assert state.cold_checked_day == first_day


def test_health_label_reflects_state():
    at = ts(2026, 2, 8, 14, 0)
    healthy = make_state(at=at, activity=A.DAILY)
    assert S.health_label(healthy, at, cfg()) == "健康"

    cold = make_state(at=at, activity=A.DAILY, cold_until=at + 3600, cold_days=2)
    assert "感冒中" in S.health_label(cold, at, cfg())

    deprived = make_state(at=at, activity=A.DAILY, sleep_debt_nights=3)
    assert "熬夜" in S.health_label(deprived, at, cfg())


# ---------------------------------------------------------------- 日期规则


def test_parse_festival_lines_full_and_bad():
    rules, warnings = S.parse_festival_lines(
        ["02-07|生日|emotion=1.5|factor=1.3|weight=0.8|material=今天是我生日", "垃圾行"]
    )
    assert len(rules) == 1
    assert rules[0].name == "生日"
    assert rules[0].month == 2 and rules[0].day == 7
    assert rules[0].factor == pytest.approx(1.3)
    assert any("整行跳过" in item for item in warnings)


def test_parse_festival_lines_requires_name():
    rules, warnings = S.parse_festival_lines(["03-01"])
    assert rules == []
    assert any("缺少名称" in item for item in warnings)


def test_birthday_is_configurable_and_fires_once():
    config = cfg(birthday="02-07", birthday_factor=1.3, birthday_emotion=1.5)
    at = ts(2026, 2, 7, 10, 0)
    state = make_state(at=at, activity=A.DAILY, emotion=5.0)
    S.settle(state, now=at + 600, config=config, events=[], rng=random.Random(1))
    assert state.emotion == pytest.approx(6.5)
    assert len(state.materials) == 1
    assert state.materials[0]["label"] == "日期:生日"

    S.settle(state, now=at + 1200, config=config, events=[], rng=random.Random(1))
    assert len(state.materials) == 1  # 同一天不重复触发


def test_no_birthday_when_unset():
    config = cfg(birthday="")
    at = ts(2026, 2, 7, 10, 0)
    state = make_state(at=at, activity=A.DAILY, emotion=5.0)
    S.settle(state, now=at + 600, config=config, events=[], rng=random.Random(1))
    assert S.date_factor(state, at, config) == pytest.approx(1.0)
    assert state.materials == []


def test_date_factor_multiplies_and_is_clamped():
    config = cfg(festivals=S.parse_festival_lines(["06-01|甲|factor=2.0", "06-01|乙|factor=3.0"])[0])
    at = ts(2026, 6, 1, 10, 0)
    state = make_state(at=at, activity=A.DAILY)
    assert S.date_factor(state, at, config) == pytest.approx(5.0)  # 钳到上限


def test_date_context_reports_festival_and_season():
    config = cfg(birthday="02-07")
    at = ts(2026, 2, 7, 10, 0)
    state = make_state(at=at, activity=A.DAILY)
    context = S.date_context(state, at, config)
    assert context["festival"] == "生日"
    assert context["season"] == "冬"
    assert context["date_label"] == "02月07日"


# ---------------------------------------------------------------- 确定性


def test_settle_is_deterministic_for_same_seed():
    at = ts(2026, 2, 8, 13, 0)
    first = make_state(at=at, activity=A.DAILY)
    second = make_state(at=at, activity=A.DAILY)
    config = cfg(fire_probability=0.6)
    S.settle(first, now=at + 3600, config=config, events=list(E.BUILTIN_EVENTS), rng=random.Random(11))
    S.settle(second, now=at + 3600, config=config, events=list(E.BUILTIN_EVENTS), rng=random.Random(11))
    assert first.to_dict() == second.to_dict()


def test_settle_differs_for_different_seed():
    at = ts(2026, 2, 8, 13, 0)
    first = make_state(at=at, activity=A.DAILY)
    second = make_state(at=at, activity=A.DAILY)
    # 事件抽取要真的跑起来，所以关掉停机间隙判定（1 小时 > 默认 30 分钟的宽限）
    config = cfg(fire_probability=0.9, offline_gap_minutes=0)
    S.settle(first, now=at + 3600, config=config, events=list(E.BUILTIN_EVENTS), rng=random.Random(1))
    S.settle(second, now=at + 3600, config=config, events=list(E.BUILTIN_EVENTS), rng=random.Random(2))
    assert first.to_dict() != second.to_dict()


def test_catch_up_bounds_step_count_not_elapsed_time():
    """离线补算封顶的是**步数**（也就是离散掷骰次数），不是流逝的时间。

    48 小时按 tick=600s 本该跑 288 步；``max_catch_up_hours=1`` 把它压到 6 步，
    所以事件最多抽 6 次。体力/睡眠这类连续量仍然按真实流逝时间积分。

    ⚠ 真实的「停机间隙」现在不走补算（``offline_gap_minutes``，见
    ``test_settle_skips_offline_gap_*``）——补算只在宽限之内、或显式关掉该判定时
    才发生。这里为了考步数封顶，显式关掉它。
    """

    at = ts(2026, 2, 8, 13, 0)
    state = make_state(at=at, activity=A.DAILY)
    config = cfg(max_catch_up_hours=1.0, fire_probability=1.0, offline_gap_minutes=0)
    S.settle(
        state, now=at + 48 * 3600, config=config, events=list(E.BUILTIN_EVENTS),
        rng=random.Random(1),
    )
    assert state.last_tick_at == at + 48 * 3600
    assert 1 <= len(state.recent_events) <= 6


# ---------------------------------------------------------------- 持久化


def test_state_roundtrip():
    at = ts(2026, 2, 8, 13, 0)
    state = make_state(at=at, activity=A.DAILY, emotion=6.7, energy=3.3)
    restored = S.LifeState.from_dict(state.to_dict())
    assert restored.to_dict() == state.to_dict()


def test_from_dict_tolerates_garbage():
    assert S.LifeState.from_dict(None).activity == A.DAILY
    assert S.LifeState.from_dict("not a dict").emotion == pytest.approx(5.0)
    assert S.LifeState.from_dict({"unknown_field": 1}).to_dict() == S.LifeState().to_dict()
    assert S.LifeState.from_dict({"emotion": "abc"}).emotion == pytest.approx(5.0)
    assert S.LifeState.from_dict({"emotion": 999}).emotion == pytest.approx(10.0)
    assert S.LifeState.from_dict({"energy": -5}).energy == pytest.approx(0.0)
    assert S.LifeState.from_dict({"activity": "泡面"}).activity == A.DAILY


# ---------------------------------------------------------------- 活动落定


def test_apply_activity_only_resets_timer_on_change():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, activity=A.DAILY)
    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
        now=at + 600,
        config=cfg(),
    )
    assert state.activity_since == at  # 没变就不重置

    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.MUSIC, source=A.SOURCE_LLM),
        now=at + 900,
        config=cfg(),
    )
    assert state.activity_since == at + 900
    assert state.activity == A.MUSIC


def test_enforce_and_apply_wakes_her_when_sleep_is_too_long():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.SLEEP, sleep_started_at=at - 30 * 3600,
        sleep_minutes_today=13 * 60,
    )
    state.activity_since = at - 30 * 3600  # 单次睡眠已 30 小时
    S.enforce_and_apply(state, now=at, config=cfg(), decision=None)
    assert state.activity == A.DAILY
    assert state.activity_source == A.SOURCE_ENFORCED


def test_enforce_and_apply_keeps_activity_when_llm_returns_nothing():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, activity=A.MUSIC)
    S.enforce_and_apply(state, now=at, config=cfg(), decision=None)
    assert state.activity == A.MUSIC
    assert state.activity_source == A.SOURCE_RETAINED


def test_activity_minutes_and_can_switch():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, activity=A.DAILY)
    assert S.activity_minutes(state, at + 1800) == 30
    allowed, reason = S.can_switch(state, at + 1800, cfg())
    assert allowed is False and "60" in reason
    allowed, _ = S.can_switch(state, at + 3600, cfg())
    assert allowed is True


# ---------------------------------------------------------------- LLM 生命周期标记


def test_mark_llm_success_and_failure():
    state = S.LifeState()
    S.mark_llm_success(state, now=1000.0, raw="ok")
    assert state.llm_last_success_at == 1000.0
    assert state.llm_fail_streak == 0

    S.mark_llm_failure(state, now=2000.0, raw="<bad>", config_limit=3)
    S.mark_llm_failure(state, now=2100.0, raw="<bad>", config_limit=3)
    assert state.llm_fail_streak == 2
    assert "<" not in state.llm_last_raw


def test_should_reseed_only_after_limit():
    state = S.LifeState()
    S.mark_llm_success(state, now=1000.0, raw="ok")
    assert S.should_reseed(state, now=1000.0 + 23 * 3600, hours=24) is False
    assert S.should_reseed(state, now=1000.0 + 25 * 3600, hours=24) is True
    assert S.should_reseed(state, now=1000.0 + 999 * 3600, hours=0) is False


def test_should_reseed_uses_activity_since_when_never_succeeded():
    """回归：``llm_last_success_at`` 为 0（旧状态文件）时兜底也必须能触发。

    旧写法退到 ``last_tick_at``，而它在每 tick 末尾都会被刷成 ``now`` ⇒ 差分恒为 0，
    兜底等于失效。
    """

    state = S.LifeState()
    state.llm_last_success_at = 0.0
    state.activity_since = 1000.0
    assert S.should_reseed(state, now=1000.0 + 7 * 3600, hours=8) is False
    assert S.should_reseed(state, now=1000.0 + 9 * 3600, hours=8) is True


def test_offline_gap_does_not_count_as_model_failure_time():
    """停机 10.6 小时不该算进 `reseed_after_hours`：否则重启后会立刻重新取种子。

    真机 2026-10-02：模型最后一次成功在 10-01 傍晚，重启已是次日 08:00——
    若把停机算进去，`hours=8` 的兜底会在重启第一轮就把她的活动改写掉。
    """

    off = ts(2026, 10, 1, 21, 21)
    on = ts(2026, 10, 2, 8, 0)
    state = make_state(at=off, activity=A.GAME)
    state.llm_last_success_at = off - 2 * 3600  # 停机前已有 2 小时没成功

    S.settle(state, now=on, config=cfg(), events=(), rng=random.Random(1))

    assert state.llm_last_success_at == pytest.approx(off - 2 * 3600 + (on - off))
    # 「插件在跑但没成功」仍只算 2 小时，离 8 小时阈值还差得远
    assert S.should_reseed(state, now=on, hours=8) is False


def test_offline_gap_leaves_zero_success_anchor_alone():
    """``llm_last_success_at`` 为 0 时不能被推成「过去的时间」。"""

    off = ts(2026, 10, 1, 21, 21)
    on = ts(2026, 10, 2, 8, 0)
    state = make_state(at=off, activity=A.GAME)
    state.llm_last_success_at = 0.0

    S.settle(state, now=on, config=cfg(), events=(), rng=random.Random(1))

    assert state.llm_last_success_at == 0.0


def test_iter_material_texts():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(
        at=at, activity=A.DAILY,
        materials=[{"label": "x", "text": "<有>效", "weight": 0.5, "expires_at": at + 60}],
    )
    assert list(S.iter_material_texts(state, at)) == ["有效"]


# ---------------------------------------------------------------- G2 保鲜相位


def test_material_freshness_decay_phases():
    """G2：保鲜期全额 → 线性衰减到 floor → 过期归零；旧条目视为全额新鲜。"""

    at = ts(2026, 2, 8, 14, 0)
    item = {"weight": 0.8, "created_at": at, "best_until": at + 1800, "expires_at": at + 3600}
    assert S.material_freshness(item, now=at + 900, floor=0.25) == pytest.approx(1.0)
    assert S.material_freshness(item, now=at + 2700, floor=0.25) == pytest.approx(0.625)
    assert S.material_freshness(item, now=at + 3599, floor=0.25) == pytest.approx(0.25, abs=1e-3)
    assert S.material_freshness(item, now=at + 3600, floor=0.25) == 0.0
    # 旧条目（无 best_until）＝ 全额新鲜到过期，行为与加入该功能前一致
    legacy = {"weight": 0.8, "expires_at": at + 3600}
    assert S.material_freshness(legacy, now=at + 3599, floor=0.25) == 1.0


def test_active_materials_ranks_by_effective_weight():
    """过了保鲜期的素材低价值化：按**有效权重**排序，而非原始权重。"""

    at = ts(2026, 2, 8, 14, 0)
    fresh = {"label": "新鲜", "text": "a", "weight": 0.5,
             "created_at": at, "best_until": at + 3600, "expires_at": at + 7200}
    stale = {"label": "放旧", "text": "b", "weight": 0.6,
             "created_at": at, "best_until": at + 60, "expires_at": at + 7200}
    state = make_state(at=at, activity=A.DAILY, materials=[stale, fresh])
    mats = S.active_materials(state, at + 3000, floor=0.25)
    assert [m["label"] for m in mats] == ["新鲜", "放旧"]


def test_material_effective_count_and_legacy_items():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, activity=A.DAILY, materials=[
        {"weight": 0.5, "created_at": at, "best_until": at + 3600, "expires_at": at + 7200},
        {"weight": 0.5, "created_at": at, "expires_at": at + 7200},
    ])
    assert S.material_effective_count(state, at, floor=0.25) == pytest.approx(2.0)
    # 衰减中点：0.625 + 旧条目 1.0
    assert S.material_effective_count(state, at + 5400, floor=0.25) == pytest.approx(1.625)


def test_apply_event_stamps_best_until_from_config_ratio():
    at = ts(2026, 2, 8, 14, 0)
    state = make_state(at=at, activity=A.DAILY)
    event = S.LifeEvent(label="煮糊了", material="锅底糊了一层", emotion=-0.3,
                        energy=0.0, weight=0.5, ttl_hours=6.0)
    S._apply_event(state, event, now=at, config=cfg(), rng=random.Random(0))
    assert state.materials[0]["best_until"] == pytest.approx(at + 3 * 3600)


def test_corrupt_best_until_entry_is_dropped():
    at = ts(2026, 2, 8, 14, 0)
    state = S.LifeState.from_dict({
        "activity": "daily",
        "materials": [
            {"label": "坏", "text": "t", "weight": 0.5, "created_at": at,
             "expires_at": at + 3600, "best_until": "abc"},
            {"label": "好", "text": "t", "weight": 0.5, "created_at": at,
             "expires_at": at + 3600, "best_until": at + 1800},
        ],
    })
    assert [m.get("label") for m in state.materials] == ["好"]


# ---------------------------------------------------------------- v1.1.1 回归


def test_sleep_cap_holds_across_the_day_boundary():
    """回归（v1.1.0 真 bug）：连续睡眠不得超过 ``max_sleep_hours``。

    ``sleep_minutes_today`` 会在 12:00 的生活日边界清零，而 ``_must_wake`` 只用它
    判上限 ⇒ 上限被重置，03:00 入睡能睡到次日 00:00（21 小时），
    与 README「模型挂掉也会被按时唤醒」矛盾。
    """

    for label, set_activity_since in (("regular", True), ("stale activity_since=0", False)):
        # energy_full_wake 在本用例里关掉：这里钉的是「12 小时上限跨生活日边界仍然
        # 生效」这一条回归；体力满提前醒是另一条行为（见 test_energy_full_wake_*）。
        config = cfg(day_boundary_hour=12, max_sleep_hours=12.0, energy_full_wake=False)
        start = ts(2026, 1, 1, 3, 0)
        state = S.LifeState()
        state.activity = A.SLEEP
        state.sleep_started_at = start
        state.last_tick_at = start
        if set_activity_since:
            state.activity_since = start
        state.day_key = S.day_key_of(S.local_datetime(start, TZ), 12)

        woke = None
        now = start
        for _ in range(24 * 6 + 2):                 # 10 分钟一步，最多 24 小时
            now += 600
            state = S.settle(state, now=now, config=config, events=(), rng=random.Random(0))
            state = S.enforce_and_apply(state, now=now, config=config, decision=None)
            if state.activity != A.SLEEP:
                woke = now
                break
        assert woke is not None, f"{label}: 12 小时后仍没被唤醒"
        slept = (woke - start) / 3600.0
        # 断言**正好**是 12 小时（不是「小于 12 小时就算过」）：
        # 只写 <= cap 会被「刚躺下就被叫醒」这种反向 bug 骗过（开发中真的踩到过）。
        assert slept == pytest.approx(12.0, abs=0.2), f"{label}: 连续睡了 {slept:.1f} 小时"


def test_energy_full_wake_fires_before_the_sleep_cap():
    """体力回满（且睡满最短时长）就先醒，不等 12 小时上限（energy_full_wake 默认开）。"""

    config = cfg(day_boundary_hour=12, max_sleep_hours=12.0)
    start = ts(2026, 1, 1, 3, 0)
    state = S.LifeState()
    state.activity = A.SLEEP
    state.sleep_started_at = start
    state.activity_since = start
    state.last_tick_at = start
    state.day_key = S.day_key_of(S.local_datetime(start, TZ), 12)

    woke = None
    now = start
    for _ in range(24 * 6 + 2):                 # 10 分钟一步，最多 24 小时
        now += 600
        state = S.settle(state, now=now, config=config, events=(), rng=random.Random(0))
        state = S.enforce_and_apply(state, now=now, config=config, decision=None)
        if state.activity != A.SLEEP:
            woke = now
            break
    assert woke is not None, "体力回满后应被唤醒"
    slept = (woke - start) / 3600.0
    assert config.min_sleep_minutes / 60.0 <= slept < 12.0, f"睡了 {slept:.1f} 小时才醒"


def test_impossible_mmdd_is_rejected():
    """02-30 这类「看着合法但永不命中」的日期必须判非法（否则生日静默失效）。"""

    assert S.parse_mmdd("02-30") is None
    assert S.parse_mmdd("04-31") is None
    assert S.parse_mmdd("2月7日") is None
    assert S.parse_mmdd("2026-02-07") is None
    assert S.parse_mmdd("02-29") == (2, 29)          # 闰日按合法处理
    assert S.parse_mmdd("02-07") == (2, 7)


def test_date_factor_overrides_apply_by_name():
    """回归（v1.1.0 真 bug）：``[date].date_factors`` 是死配置，设了完全没用。"""

    base = cfg(birthday="02-07", birthday_factor=1.3)
    overridden = cfg(
        birthday="02-07", birthday_factor=1.3, date_factor_overrides={"生日": 1.4}
    )
    local = S.local_datetime(ts(2026, 2, 7, 12, 0), TZ)
    assert S.all_festival_rules(base)[0].factor == pytest.approx(1.3)
    assert S.all_festival_rules(overridden)[0].factor == pytest.approx(1.4)
    assert S.date_factor(S.LifeState(), ts(2026, 2, 7, 12, 0), overridden) == pytest.approx(1.4)


def test_corrupt_material_entry_is_dropped_not_fatal():
    """回归（v1.1.0 真 bug）：一个坏条目会让 ``settle`` 每 tick 抛错且永不自愈。"""

    state = S.LifeState.from_dict(
        {
            "state_version": S.STATE_VERSION,
            "last_tick_at": 1000.0,
            "materials": [
                {"label": "x", "text": "t", "expires_at": "abc"},
                None,
                3,
                {"label": "good", "text": "好", "weight": 0.5, "expires_at": 9e9},
            ],
        }
    )
    assert [item["label"] for item in state.materials] == ["good"]
    assert S.settle(state, now=1060.0, config=cfg(), events=(), rng=random.Random(0))
    assert S.active_materials(state, 1060.0)


def test_corrupt_recent_event_never_raises():
    state = S.LifeState.from_dict(
        {
            "state_version": S.STATE_VERSION,
            "last_tick_at": 1000.0,
            "recent_events": [
                {"at": "abc", "label": "L", "text": "T"},
                {"at": None, "label": "L2"},
                {"at": 900.0, "label": "ok", "text": "T", "emotion": 1.0},
            ],
        }
    )
    assert [item.get("label") for item in state.recent_events] == ["ok"]
    tiers = S.recent_event_tiers(state, now=1000.0, limit=8, tz_offset_minutes=TZ)
    near = dict(tiers)[tiers[0][0]]
    assert near == ("01-01 08:15 ok：T",), near      # 本地时间（UTC+8），不是 UTC 00:15
    S._recompute_afterglow(state, 1000.0, cfg())


def test_non_boolean_paused_override_is_ignored():
    """``bool("false") is True``：字符串 "false" 不能把插件静默变成「已暂停」。"""

    assert S.LifeState.from_dict({"paused_override": "false"}).paused_override is False
    assert S.LifeState.from_dict({"paused_override": 0}).paused_override is False
    assert S.LifeState.from_dict({"paused_override": True}).paused_override is True


def test_keep_zero_means_keep_nothing():
    """回归：``*_keep = 0`` 的语义是「不保留」，不是「保留 1 条」。"""

    at = 1_700_000_000.0
    state = S.LifeState()
    state.materials = [
        {"label": "a", "text": "a", "weight": 0.5, "expires_at": at + 100},
        {"label": "b", "text": "b", "weight": 0.5, "expires_at": at + 100},
    ]
    state.recent_events = [{"at": at, "label": "L", "text": "T"}]
    S._sweep(state, at, cfg(materials_keep=0, recent_events_keep=0))
    assert state.materials == []
    assert state.recent_events == []

    state.recent_events = [{"at": at, "label": "L", "text": "T"}]
    assert S.recent_event_tiers(state, now=at, limit=0) == ()
    tiers = S.recent_event_tiers(state, now=at, limit=1, tz_offset_minutes=TZ)
    assert len(tiers[0][1]) == 1 and tiers[0][1][0].endswith("L：T")
    assert all(lines == () for _, lines in tiers[1:]), "limit<3 时全部给最近层"


# ---------------------------------------------------------------- 停机间隙


def test_settle_skips_offline_gap_without_crediting_awake_time():
    """回归（真机 2026-10-02）：停机 10.6 小时不得被记成「清醒 10.6 小时」。

    现场：她停摆前在 game，插件停机到次日早上重启。逐 tick 补算把
    `awake_minutes_today` 从 216 顶到 859，凭空打开了「每日清醒下限」
    ——约束的松紧变成了「插件停过多久」的函数。
    """

    off = ts(2026, 10, 1, 21, 21)
    on = ts(2026, 10, 2, 8, 0)
    state = make_state(at=off, activity=A.GAME, awake_minutes_today=216, energy=0.6)

    S.settle(state, now=on, config=cfg(), events=(), rng=random.Random(1))

    assert state.awake_minutes_today == 216, "停机时间不记账"
    assert state.sleep_minutes_today == 0
    assert state.energy == 0.6, "也不该按 game 扣体力"
    assert state.activity == A.GAME, "活动保持，等下一次决策"
    assert state.last_tick_at == on, "时钟必须推进，否则每个 tick 都会重判一次"


def test_settle_offline_gap_callback_reports_no_boundary_crossing():
    off = ts(2026, 10, 1, 21, 21)
    on = ts(2026, 10, 2, 8, 0)
    state = make_state(at=off, activity=A.GAME, awake_minutes_today=216)
    seen: list = []

    S.settle(
        state,
        now=on,
        config=cfg(),
        events=(),
        rng=random.Random(1),
        on_offline_gap=lambda gap, crossed: seen.append((gap, crossed)),
    )

    assert seen and seen[0][0] == int((on - off) // 60)
    assert seen[0][1] is False, "10-01 21:21 → 10-02 08:00 不跨 12:00 边界"


def test_settle_offline_gap_resets_counters_across_boundary_but_no_debt():
    """跨过生活日边界：只重置计数器，**不**凭空判一笔熬夜。"""

    off = ts(2026, 10, 1, 11, 0)          # 12:00 边界之前
    on = ts(2026, 10, 2, 8, 0)            # 边界之后
    state = make_state(
        at=off,
        activity=A.GAME,
        awake_minutes_today=500,
        sleep_minutes_today=100,
        sleep_debt_nights=0,
        energy=4.0,
    )
    seen: list = []

    S.settle(
        state,
        now=on,
        config=cfg(),
        events=(),
        rng=random.Random(1),
        on_offline_gap=lambda gap, crossed: seen.append((gap, crossed)),
    )

    assert state.day_key == "2026-10-01"
    assert state.awake_minutes_today == 0
    assert state.sleep_minutes_today == 0
    assert state.sleep_debt_nights == 0, "不知道她睡没睡，不能记熬夜"
    assert seen[0][1] is True


def test_settle_normal_tick_is_not_treated_as_offline_gap():
    """宽限之内的一步必须照旧记账，别把正常推进也吞掉。"""

    at = ts(2026, 10, 2, 8, 0)
    state = make_state(at=at, activity=A.GAME, awake_minutes_today=0)

    S.settle(state, now=at + 600, config=cfg(), events=(), rng=random.Random(1))

    assert state.awake_minutes_today == 10


def test_settle_offline_gap_can_be_disabled():
    """``offline_gap_minutes=0`` 时退回旧的逐 tick 补算行为。"""

    off = ts(2026, 10, 1, 21, 21)
    on = ts(2026, 10, 2, 8, 0)
    state = make_state(at=off, activity=A.GAME, awake_minutes_today=216, energy=0.6)

    S.settle(
        state, now=on, config=cfg(offline_gap_minutes=0), events=(), rng=random.Random(1)
    )

    assert state.awake_minutes_today > 216, "关掉判定就该恢复补算"


# ---------------------------------------------------------------- 睡眠账本（v1.3.2）


def test_sleep_ledger_prunes_and_roundtrips():
    """账本要能落盘恢复，并且只留与 24 小时窗口还有交集的条目。"""

    at = ts(2026, 10, 2, 8, 0)
    state = make_state(at=at, activity=A.DAILY)
    S.record_sleep_episode(state, now=at, minutes=300)
    S.record_sleep_episode(state, now=at + 26 * 3600, minutes=420)

    assert len(state.sleep_ledger) == 1, "旧条目该被清掉"
    assert S.sleep_in_window(state, now=at + 26 * 3600) == 420
    restored = S.LifeState.from_dict(state.to_dict())
    assert restored.sleep_ledger == state.sleep_ledger


def test_sleep_in_window_counts_overlap_only():
    at = ts(2026, 10, 2, 8, 0)
    state = make_state(at=at, activity=A.DAILY)
    S.record_sleep_episode(state, now=at - 2 * 3600, minutes=120)   # 04:00→06:00
    S.record_sleep_episode(state, now=at, minutes=300)              # 03:00→08:00

    assert S.sleep_in_window(state, now=at) == 420
    assert S.sleep_in_window(state, now=at, hours=1.0) == 60, "只算 07:00→08:00 的交集"


def test_two_nights_a_day_apart_do_not_stack():
    """回归（探针实拍）：相邻两夜只隔约 24 小时，窗口不能把上一夜整段算进来。

    错误写法会把「上一夜 + 这一夜」相加，于是每天只睡 4 小时的人显示 8 小时、
    熬夜永远攒不出来（错误结果 1/0/0，正确应为 1/2/3）。
    """

    config = cfg()
    state = make_state(at=ts(2026, 10, 2, 4, 0), activity=A.DAILY)
    debts = []
    for index in range(3):
        start = ts(2026, 10, 2 + index, 4, 0)
        state.activity = A.SLEEP
        state.sleep_started_at = start
        S.settle(state, now=start + 4 * 3600, config=config, events=[], rng=random.Random(1))
        S.apply_activity(
            state,
            A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
            now=start + 4 * 3600,
            config=config,
        )
        debts.append(state.sleep_debt_nights)
    assert debts == [1, 2, 3], debts
    assert state.energy_cap == pytest.approx(8.5)


def test_corrupt_sleep_ledger_entries_are_dropped():
    """坏条目必须被丢掉：否则判分会每次醒来抛错且永不自愈。"""

    # 账本里存的是 **epoch 秒**（start/end），sleep_in_window 返回分钟
    end = 1000.0 + 300 * 60
    state = S.LifeState.from_dict(
        {
            "state_version": S.STATE_VERSION,
            "sleep_ledger": [
                {"start": "abc", "end": end},
                {"start": 200.0, "end": "x"},
                {"start": 1000.0, "end": end},
                "junk",
            ],
        }
    )
    assert len(state.sleep_ledger) == 1
    assert S.sleep_in_window(state, now=end) == 300


def test_wake_without_anchor_falls_back_to_day_total():
    """旧状态没有 sleep_started_at 时，用当日累计判一次，并且不进账本（避免重复计）。"""

    at = ts(2026, 2, 8, 4, 0)
    state = make_state(at=at, activity=A.SLEEP, sleep_started_at=0.0, sleep_minutes_today=100)
    S.apply_activity(
        state,
        A.ActivityDecision(activity=A.DAILY, source=A.SOURCE_LLM),
        now=at + 600,
        config=cfg(),
    )
    assert state.sleep_debt_nights == 1
    assert state.sleep_ledger == [], "兜底路径不计账本"
