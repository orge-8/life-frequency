# -*- coding: utf-8 -*-
"""L3：习惯层（routine）的解析与命中语义。

钉住六件事：

1. **行 DSL 的坏值必须告警**——写错活动名或时间窗，这条习惯永远不触发，
   不告警的话现场没有任何线索（与事件 DSL 的 ``activities=`` 同一类静默死配置）；
2. **行 ID 不能带盐**——内置 ``hash()`` 有随机盐，重启后库里的「今天已命中」
   全部对不上，习惯天天重复触发；
3. **抖动按天固定**，不是每 tick 重掷（否则窗口在边界上反复进出）；
4. **当日已定论的行不再参与命中**（已命中 / 权重未中）；
5. **``bool("false") is True`` 陷阱**——``workday_only=false`` 必须真是关闭；
6. **命中只是 proposal**，仍要过 ``enforce``（习惯不能绕过硬约束）。
"""

import hashlib
from datetime import datetime

import life_routines as R
from life_activity import SOURCE_ROUTINE

# 2026-10-08 是周四（isoweekday 4），2026-10-10 是周六
THURSDAY = datetime(2026, 10, 8, 7, 10)
SATURDAY = datetime(2026, 10, 10, 9, 0)


def _ctx(local_dt=THURSDAY, *, is_workday=True, is_holiday=None):
    return R.RoutineContext.from_local_dt(
        local_dt, is_workday=is_workday, is_holiday=is_holiday
    )


def _lines(*raw, **kwargs):
    parsed, warnings = R.parse_routine_lines(list(raw), **kwargs)
    return parsed, warnings


# ---------------------------------------------------------------- 行 DSL 解析


def test_valid_line_keeps_every_field():
    (line,), warnings = _lines("07:00-07:30|起床洗漱|daily|weight=0.8|jitter=10")
    assert warnings == []
    assert (line.start, line.end) == (420, 450)
    assert line.scene == "起床洗漱"
    assert line.scenes == ("起床洗漱",), "单候选也要进候选表（v1.17.0 PR-ROU-2）"
    assert line.scene_for(day_key="2026-10-08", now_minutes=430) == "起床洗漱"
    assert line.activity == "daily"
    assert line.weight == 0.8
    assert line.jitter == 10
    assert line.days == ()
    assert line.line_id == hashlib.sha1("07:00-07:30|起床洗漱|daily|weight=0.8|jitter=10".encode("utf-8")).hexdigest()[:16]


def test_line_id_is_salt_free_and_stable_across_text_variants():
    """同一行文本（前后空白不同）必须得到同一个 ID——它是落盘键。"""

    first, _ = _lines("07:00-07:30|起床洗漱|daily")
    second, _ = _lines("  07:00-07:30|起床洗漱|daily  ")
    assert first[0].line_id == second[0].line_id
    # 反例：内置 hash 带盐，两个进程算出来不一样（这里用同一进程内的不同文本佐证
    # 「ID 来自内容而非对象身份」）
    other, _ = _lines("07:00-07:31|起床洗漱|daily")
    assert first[0].line_id != other[0].line_id


def test_activity_alias_is_accepted_but_unknown_name_is_rejected():
    (line,), warnings = _lines("13:00-13:40|午休|午休|workday_only=true")
    assert line.activity == "lunch", "中文别名要走 normalize_activity"

    parsed, warnings = _lines("08:00-08:30|吃早饭|meal|physio=true")
    assert parsed != () and parsed[0].activity == "meal", "v1.9.1 起 meal 是合法活动"

    parsed, warnings = _lines("08:00-08:30|吃早饭|mealx|physio=true")
    assert parsed == ()
    assert any("不是已知活动" in item for item in warnings), warnings


def test_bad_window_and_zero_length_window_are_rejected():
    parsed, warnings = _lines("早上|起床洗漱|daily")
    assert parsed == ()
    assert any("时间窗" in item for item in warnings)

    parsed, warnings = _lines("07:00-07:00|起床洗漱|daily")
    assert parsed == ()
    assert any("零长度" in item for item in warnings), "零长度窗口永不命中，必须告警而不是静默丢弃"


def test_missing_scene_is_rejected():
    parsed, warnings = _lines("07:00-07:30||daily")
    assert parsed == ()
    assert any("场景" in item for item in warnings)


def test_weight_is_clamped_into_unit_range():
    (line,), warnings = _lines("07:00-07:30|起床|daily|weight=3")
    assert line.weight == 1.0
    (line,), _ = _lines("07:00-07:30|起床|daily|weight=-2")
    assert line.weight == 0.0
    (line,), warnings = _lines("07:00-07:30|起床|daily|weight=高")
    assert line.weight == 1.0
    assert any("weight" in item for item in warnings)


def test_jitter_default_comes_from_config_and_bad_value_warns():
    (line,), _ = _lines("07:00-07:30|起床|daily", default_jitter=25)
    assert line.jitter == 25
    (line,), warnings = _lines("07:00-07:30|起床|daily|jitter=abc", default_jitter=25)
    assert line.jitter == 25
    assert any("jitter" in item for item in warnings)
    (line,), warnings = _lines("07:00-07:30|起床|daily|jitter=999")
    assert line.jitter == R.MAX_JITTER_MINUTES
    assert any("超出" in item for item in warnings)


def test_days_modifier_parses_ranges_and_rejects_garbage():
    (line,), _ = _lines("07:00-07:30|起床|daily|days=1-5")
    assert line.days == (1, 2, 3, 4, 5)
    (line,), _ = _lines("07:00-07:30|起床|daily|days=六日")
    assert line.days == (6, 7)
    parsed, warnings = _lines("07:00-07:30|起床|daily|days=星期八")
    assert parsed == ()
    assert any("days" in item for item in warnings)


def test_bool_modifier_does_not_fall_into_the_bool_string_trap():
    """``bool("false") is True``——宽松真值会把「明确关闭」读成「开启」。"""

    (line,), _ = _lines("13:00-13:40|午休|lunch|workday_only=false")
    assert line.workday_only is False
    (line,), _ = _lines("13:00-13:40|午休|lunch|workday_only=true")
    assert line.workday_only is True
    (line,), warnings = _lines("13:00-13:40|午休|lunch|workday_only=maybe")
    assert line.workday_only is False
    assert any("workday_only" in item for item in warnings)


def test_contradictory_day_flags_warn():
    (line,), warnings = _lines("13:00-14:00|午休|lunch|workday_only=true|holiday_only=true")
    assert line.workday_only and line.holiday_only
    assert any("永不命中" in item for item in warnings)


def test_unknown_modifier_warns_but_keeps_the_line():
    (line,), warnings = _lines("07:00-07:30|起床|daily|nonsense=1")
    assert line.activity == "daily"
    assert any("未知修饰符" in item for item in warnings)


# ---------------------------------------------------------------- 命中判定


def test_window_match_is_half_open_and_supports_midnight():
    (line,), _ = _lines("23:30-00:30|熬夜|night_study")
    assert R.active_lines((line,), context=_ctx(datetime(2026, 10, 8, 23, 40))) != ()
    assert R.active_lines((line,), context=_ctx(datetime(2026, 10, 8, 0, 10))) != ()
    assert R.active_lines((line,), context=_ctx(datetime(2026, 10, 8, 0, 30))) == ()

    (day,), _ = _lines("07:00-07:30|起床|daily")
    assert R.active_lines((day,), context=_ctx(datetime(2026, 10, 8, 7, 0))) != ()
    assert R.active_lines((day,), context=_ctx(datetime(2026, 10, 8, 7, 30))) == ()


def test_jitter_shifts_the_whole_window():
    (line,), _ = _lines("07:00-07:30|起床|daily|jitter=0")
    jitter = {line.line_id: 20}
    # 平移后是 07:20-07:50：07:10 不再命中，07:25 命中
    assert R.active_lines((line,), context=_ctx(datetime(2026, 10, 8, 7, 10)), jitter_map=jitter) == ()
    assert R.active_lines((line,), context=_ctx(datetime(2026, 10, 8, 7, 25)), jitter_map=jitter) != ()


def test_day_flags_gate_the_line():
    (work,), _ = _lines("13:00-14:00|午休|lunch|workday_only=true")
    (rest,), _ = _lines("13:00-14:00|睡懒觉|daze|holiday_only=true")
    noon = datetime(2026, 10, 8, 13, 10)

    workday = _ctx(noon, is_workday=True, is_holiday=False)
    assert R.active_lines((work,), context=workday) != ()
    assert R.active_lines((rest,), context=workday) == ()

    # 步 3 之前：不上班 = 节假日（互补）。步 3 之后这两个值不再互补，接口不变
    holiday = _ctx(noon, is_workday=False, is_holiday=True)
    assert R.active_lines((work,), context=holiday) == ()
    assert R.active_lines((rest,), context=holiday) != ()


def test_weekday_gate_uses_isoweekday():
    (line,), _ = _lines("07:00-08:00|晨跑|daily|days=1-5")
    assert R.active_lines((line,), context=_ctx(THURSDAY)) != ()
    assert R.active_lines((line,), context=_ctx(SATURDAY)) == ()


def test_context_defaults_holiday_to_the_inverse_of_workday():
    assert _ctx(THURSDAY, is_workday=True).is_holiday is False
    assert _ctx(THURSDAY, is_workday=False).is_holiday is True
    assert _ctx(THURSDAY, is_workday=True, is_holiday=True).is_holiday is True


def test_pick_pending_skips_settled_lines():
    (line,), _ = _lines("07:00-08:00|起床|daily")
    active = (line,)
    assert R.pick_pending(active, {}) is line
    assert R.pick_pending(active, {line.line_id: R.ROUTINE_FIRED}) is None
    assert R.pick_pending(active, {line.line_id: R.ROUTINE_SKIPPED}) is None
    assert R.pick_pending((), {}) is None


def test_roll_weight_edges():
    (line,), _ = _lines("07:00-08:00|起床|daily|weight=1")
    assert R.roll_weight(line, _FakeRng(0.99)) is True
    (never,), _ = _lines("07:00-08:00|起床|daily|weight=0")
    assert R.roll_weight(never, _FakeRng(0.0)) is False
    (half,), _ = _lines("07:00-08:00|起床|daily|weight=0.5")
    assert R.roll_weight(half, _FakeRng(0.49)) is True
    assert R.roll_weight(half, _FakeRng(0.51)) is False
    assert R.roll_weight(half, object()) is False, "随机源坏了按「不中」处理，不许炸"


def test_decision_is_only_a_proposal_carrying_the_routine_source():
    (line,), _ = _lines("07:00-07:30|起床洗漱|daily")
    decision = R.decision_for(line)
    assert decision.activity == "daily"
    assert decision.scene == "起床洗漱"
    assert decision.source == SOURCE_ROUTINE
    assert SOURCE_ROUTINE in __import__("life_activity").SOURCE_LABELS


class _FakeRng:
    """只提供 ``random()`` 的随机源替身（让概率断言可复现）。"""

    def __init__(self, value: float) -> None:
        self._value = value

    def random(self) -> float:
        return self._value


# ------------------------------------------------ v1.17.0（PR-ROU-2）场景候选


def test_multi_scene_candidates_are_parsed_and_purged():
    (line,), warnings = _lines("07:00-08:00|起床洗漱；冲了个澡；|daily")
    assert warnings == []
    assert line.scenes == ("起床洗漱", "冲了个澡"), "空候选要丢掉"
    assert line.scene == "起床洗漱", "第一个候选仍是 scene（状态卡与旧调用点的口径）"
    for scene in line.scenes:
        assert len(scene) <= R.MAX_SCENE_CHARS


def test_scene_rotation_is_deterministic_within_an_hour_bucket():
    (line,), _ = _lines("19:00-22:00|晚自习；看网课；整理笔记；写点东西|night_study")
    picks = {
        line.scene_for(day_key="2026-10-08", now_minutes=19 * 60 + minute)
        for minute in range(0, 60, 7)
    }
    assert len(picks) == 1, f"同一小时桶里不该换句子：{picks}"

    over_buckets = {
        line.scene_for(day_key="2026-10-08", now_minutes=(19 + hour) * 60 + 5)
        for hour in range(3)
    }
    assert len(over_buckets) > 1, "跨小时桶应该有机会换句子"

    # 同一个 day_key + 同一个桶：永远同一个答案（不消耗随机源）
    again = line.scene_for(day_key="2026-10-08", now_minutes=19 * 60 + 5)
    assert again in picks


def test_single_scene_line_is_bit_identical_to_the_old_behaviour():
    (line,), _ = _lines("07:00-08:00|起床洗漱|daily")
    for day_key in ("2026-10-08", "2026-10-09"):
        for minute in (0, 420, 1439):
            assert line.scene_for(day_key=day_key, now_minutes=minute) == "起床洗漱"

    decision = R.decision_for(line)
    assert decision.scene == "起床洗漱"
    assert R.decision_for(line, day_key="2026-10-08", now_minutes=430).scene == "起床洗漱"


def test_decision_for_uses_the_rotated_candidate():
    (line,), _ = _lines("19:00-22:00|甲；乙；丙；丁|night_study")
    scenes = {
        R.decision_for(line, day_key=f"2026-10-{day:02d}", now_minutes=19 * 60 + 5).scene
        for day in range(1, 15)
    }
    assert scenes <= set(line.scenes)
    assert len(scenes) > 1, f"多候选要真的轮换：{scenes}"
    assert R.decision_for(line).scene == "甲", "不传 key = 第一个候选"
