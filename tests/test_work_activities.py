# -*- coding: utf-8 -*-
"""L3：工作表活动（Layer 1）——让「上班」真的成为一种活动。

背景：原来 9 个活动全是学生/居家语义（深夜做题、看番、打游戏、发呆），有固定工作的
角色只能靠 `scene` 文字「假装」在上班，而**倍率按错的活动算**（在岗却按 `daily=1.0`）。
v1.3.0 补上 6 个工作语义活动，并把「相位 × 活动」的一致性交给确定性层：

    commute 通勤 0.85 / work 工作 0.75 / meeting 开会对接 0.5
    overtime 加班 0.45 / lunch 午休 1.1 / off_work 下班路上 1.0

加一个活动要同步**六处**（枚举 / 标签 / 清醒集 / 别名 / 因子表 / 时段表与相位种子），
漏一处的表现都很隐蔽（标签漏了卡片显示英文键、因子漏了静默按 1.0、时段表漏了冷启动
回到学生作息），所以这些一致性都由用例钉住。
"""

import asyncio
import logging
import pathlib
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config  # noqa: E402

import life_activity as A  # noqa: E402
import life_sim as S  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
TZ = 480
MONDAY = datetime(2026, 10, 5, 0, 0)      # 周一
SATURDAY = datetime(2026, 10, 3, 0, 0)    # 周六

WORK_ACTIVITIES = (A.COMMUTE, A.WORK, A.MEETING, A.OVERTIME, A.LUNCH, A.OFF_WORK)


def local_ts(dt: datetime) -> float:
    return dt.replace(tzinfo=timezone.utc).timestamp() - TZ * 60


def work_config(**overrides) -> A.ScheduleConfig:
    base = dict(
        enabled=True,
        workdays=(1, 2, 3, 4, 5),
        work_window=(9 * 60 + 30, 18 * 60 + 30),
        commute_minutes=45,
        lunch_window=(12 * 60, 13 * 60),
        duty="FMInfinity 共鸣电台无线电技术员",
    )
    base.update(overrides)
    return A.ScheduleConfig(**base)


def facts_at(moment: datetime, config: A.ScheduleConfig | None = None) -> A.ScheduleFacts:
    return A.schedule_facts(moment, config or work_config())


def policy(**overrides) -> A.EnforcePolicy:
    base = dict(
        sleep_window=(0, 1440),
        sleep_energy_threshold=3.0,
        min_awake_hours_per_day=8.0,
        min_dwell_minutes=60,
        schedule=work_config(),
    )
    base.update(overrides)
    return A.EnforcePolicy(**base)


def activity_facts(**overrides) -> A.ActivityFacts:
    base = dict(
        activity=A.DAILY,
        minutes_in_activity=120,
        now_minutes=10 * 60,
        energy=7.0,
        sick=False,
        awake_minutes_today=600,
        schedule=facts_at(MONDAY.replace(hour=10)),
    )
    base.update(overrides)
    return A.ActivityFacts(**base)


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_work")


class _Capture:
    def __init__(self, module):
        self.records: list[tuple[int, str]] = []
        self._handler = logging.Handler()
        self._handler.emit = lambda record: self.records.append(
            (record.levelno, record.getMessage())
        )
        self._logger = logging.getLogger(f"plugin.{module.__plugin_id__}")

    def __enter__(self):
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        return False

    def warnings(self) -> list[str]:
        return [msg for lv, msg in self.records if lv >= logging.WARNING]


# ================= A. 六处同步 =================


def test_activity_enum_has_the_work_set():
    for key in WORK_ACTIVITIES:
        assert key in A.ALLOWED_ACTIVITIES, key
    assert len(A.ALLOWED_ACTIVITIES) == len(set(A.ALLOWED_ACTIVITIES)), "枚举里有重复"


def test_every_activity_has_label_and_awake_flag():
    """漏标签 ⇒ 卡片显示英文键；漏清醒集 ⇒ 睡眠统计与「今日清醒」算错。"""

    for key in A.ALLOWED_ACTIVITIES:
        assert A.ACTIVITY_LABELS.get(key), f"{key} 没有中文标签"
    for key in WORK_ACTIVITIES:
        assert A.is_awake(key), f"{key} 应算清醒活动"
    assert A.is_awake(A.SLEEP) is False


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("通勤", A.COMMUTE), ("上班路上", A.COMMUTE), ("commute", A.COMMUTE),
        ("上班", A.WORK), ("值班", A.WORK), ("在岗", A.WORK), ("work", A.WORK),
        ("开会", A.MEETING), ("对接", A.MEETING), ("meeting", A.MEETING),
        ("加班", A.OVERTIME), ("overtime", A.OVERTIME),
        ("午休", A.LUNCH), ("午饭", A.LUNCH), ("lunch", A.LUNCH),
        ("下班", A.OFF_WORK), ("回家路上", A.OFF_WORK), ("off_work", A.OFF_WORK),
    ],
)
def test_new_activities_have_aliases(alias, expected):
    """模型爱写中文标签而不是枚举键，别名表是那层兼容。"""

    assert A._ACTIVITY_ALIASES.get(alias) == expected


def test_parse_response_accepts_a_chinese_work_alias():
    decision = A.parse_response('{"activity": "上班", "scene": "在调设备"}')
    assert decision is not None and decision.activity == A.WORK


def test_default_factor_table_covers_every_activity():
    module = _load()
    factors = module._default_activity_factors()
    missing = [key for key in A.ALLOWED_ACTIVITIES if key not in factors]
    assert missing == [], f"这些活动没有因子，会静默按 1.0：{missing}"
    assert [line for line in module.DEFAULT_ACTIVITY_FACTOR_LINES
            if line.startswith("work=")] == ["work=0.75"]
    for key, value in factors.items():
        assert 0.0 <= value <= 2.0, (key, value)


def test_energy_delta_covers_every_activity():
    deltas = S._default_energy_delta()
    missing = [key for key in A.ALLOWED_ACTIVITIES if key not in deltas]
    assert missing == [], f"这些活动没有体力消耗标定，会退到兜底 -0.5/小时：{missing}"


# ================= B. 相位 → 种子活动 =================


@pytest.mark.parametrize(
    "hour,minute,expected_activity",
    [
        (8, 0, A.DAILY),        # 出门前：洗漱收拾
        (9, 0, A.COMMUTE),      # 通勤中
        (10, 30, A.WORK),       # 在岗
        (12, 30, A.LUNCH),      # 午休
        (18, 45, A.OFF_WORK),   # 下班回程
    ],
)
def test_rule_seed_matches_the_schedule_phase(hour, minute, expected_activity):
    moment = MONDAY.replace(hour=hour, minute=minute)
    seed = A.rule_based_activity(
        now_minutes=hour * 60 + minute, energy=6.5,
        schedule=facts_at(moment), work_scene="在电台值守",
    )
    assert seed.activity == expected_activity, seed


def test_rule_seed_uses_the_configured_work_scene_only_for_work():
    schedule = facts_at(MONDAY.replace(hour=10, minute=30))
    assert A.rule_based_activity(
        now_minutes=10 * 60 + 30, energy=6.5, schedule=schedule, work_scene="在电台值守"
    ).scene == "在电台值守"
    # 其它相位的场景是内置文案，不该被 work_scene 顶掉
    commute = facts_at(MONDAY.replace(hour=9))
    assert A.rule_based_activity(
        now_minutes=9 * 60, energy=6.5, schedule=commute, work_scene="在电台值守"
    ).scene != "在电台值守"


# ================= C. 相位 × 活动一致性矩阵 =================


@pytest.mark.parametrize(
    "hour,minute,allowed,blocked",
    [
        (8, 0, (A.DAILY, A.MUSIC), (A.WORK, A.COMMUTE, A.LUNCH, A.OFF_WORK, A.SLEEP)),
        (9, 0, (A.COMMUTE, A.MUSIC), (A.WORK, A.MEETING, A.LUNCH, A.OFF_WORK)),
        (10, 30, (A.WORK, A.MEETING, A.OVERTIME, A.DAILY), (A.COMMUTE, A.LUNCH, A.GAME, A.ANIME)),
        (12, 30, (A.LUNCH, A.WORK, A.GAME), (A.COMMUTE, A.OFF_WORK, A.SLEEP)),
        (18, 45, (A.OFF_WORK, A.COMMUTE, A.OVERTIME), (A.WORK, A.MEETING, A.LUNCH)),
        (21, 0, (A.WORK, A.GAME, A.COMMUTE), ()),           # 自由时间不限制
    ],
)
def test_phase_activity_matrix(hour, minute, allowed, blocked):
    facts = activity_facts(schedule=facts_at(MONDAY.replace(hour=hour, minute=minute)))
    for activity in allowed:
        assert A.activity_blocked_by_schedule(activity, facts, policy()) == "", (
            f"{A.schedule_phase_label(facts.schedule.phase)} 不该拦 {activity}"
        )
    for activity in blocked:
        assert A.activity_blocked_by_schedule(activity, facts, policy()), (
            f"{A.schedule_phase_label(facts.schedule.phase)} 应该拦住 {activity}"
        )


def test_rest_day_and_disabled_schedule_block_nothing():
    weekend = activity_facts(schedule=facts_at(SATURDAY.replace(hour=10)))
    off = activity_facts(schedule=A.ScheduleFacts())
    for activity in A.ALLOWED_ACTIVITIES:
        assert A.activity_blocked_by_schedule(activity, weekend, policy()) == "", activity
        assert A.activity_blocked_by_schedule(activity, off, policy()) == "", activity


def test_health_exception_still_wins_for_sleep_in_every_duty_phase():
    for moment in (MONDAY.replace(hour=8), MONDAY.replace(hour=9),
                   MONDAY.replace(hour=10, minute=30), MONDAY.replace(hour=12, minute=30)):
        tired = activity_facts(schedule=facts_at(moment), energy=1.0)
        sick = activity_facts(schedule=facts_at(moment), sick=True)
        assert A.activity_blocked_by_schedule(A.SLEEP, tired, policy()) == "", moment
        assert A.activity_blocked_by_schedule(A.SLEEP, sick, policy()) == "", moment


def test_enforce_accepts_work_and_rejects_commute_at_the_desk():
    desk = activity_facts()          # 周一 10:00，在岗
    assert A.enforce(desk, A.ActivityDecision(A.WORK, "在调设备"), policy()).activity == A.WORK
    rejected = A.enforce(desk, A.ActivityDecision(A.COMMUTE, "在路上"), policy())
    assert rejected.activity == A.DAILY
    assert "不该通勤" in rejected.note, rejected.note


def test_enforce_accepts_commute_while_commuting():
    commuting = activity_facts(schedule=facts_at(MONDAY.replace(hour=9)))
    result = A.enforce(commuting, A.ActivityDecision(A.COMMUTE, "在挤地铁"), policy())
    assert result.activity == A.COMMUTE, result


def test_enforce_allows_meeting_and_overtime_as_alternatives():
    """在岗除了 work 还允许 meeting / overtime —— 否则模型被逼成一个固定答案。"""

    desk = activity_facts()
    for activity in (A.MEETING, A.OVERTIME):
        result = A.enforce(desk, A.ActivityDecision(activity, "忙"), policy())
        assert result.activity == activity, (activity, result)


# ================= D. 提示词与强制层同源 =================


def test_prompt_lists_exactly_the_allowed_activities():
    facts = facts_at(MONDAY.replace(hour=10, minute=30))
    prompt = "\n".join(facts.prompt_lines)
    allowed = A.schedule_allowed_activities(facts)
    assert "现在适合的活动" in prompt, prompt
    for activity in allowed:
        assert A.ACTIVITY_LABELS[activity] in prompt, activity
    for activity in set(A.ALLOWED_ACTIVITIES) - set(allowed):
        # 被禁的活动只出现在「都不合适」那句里，不该出现在「适合」清单里
        assert f"{activity}=" not in prompt, f"{activity} 不该出现在适合清单里"


def test_allowed_activities_match_the_enforce_matrix():
    """提示词说「适合」的，强制层就必须真的放行（同源，不能各写一套）。"""

    for hour, minute in ((8, 0), (9, 0), (10, 30), (12, 30), (18, 45), (21, 0)):
        facts = activity_facts(schedule=facts_at(MONDAY.replace(hour=hour, minute=minute)))
        for activity in A.schedule_allowed_activities(facts):
            assert A.activity_blocked_by_schedule(activity, facts, policy()) == "", (
                hour, minute, activity
            )


# ================= E. 升级迁移：老配置也要拿到新活动 =================


OLD_NINE = [
    "sleep=0.0", "sick_rest=1.0", "before_sleep=1.15", "night_study=0.5",
    "music=0.9", "game=0.75", "anime=0.75", "daze=0.6", "daily=1.0",
]


def _make_plugin(factor_lines=None, schedule=None, factors_mode=None):
    module = _load()
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["frequency"]["quiet_hours"] = []
    if factor_lines is not None:
        config["activity"]["activity_factors"] = list(factor_lines)
    if factors_mode is not None:
        config["activity"]["factors_mode"] = factors_mode
    if schedule is not None:
        entry = dict(config.get("schedule") or {})
        entry.update(schedule)
        config["schedule"] = entry
    bind_context(plugin, ctx, config)
    return module, plugin


def test_upgrade_fills_new_work_factors_and_warns_once():
    """老 config.toml（只有 9 个学生因子）必须自动拿到工作表因子，并留下线索。

    否则 `compute_adjust` 的 ``.get(activity, 1.0)`` 会让「在岗话少」静默失效——
    配了班表却没有任何效果，日志里一个字都没有。
    """

    module, plugin = _make_plugin(OLD_NINE)
    with _Capture(module) as cap:
        factors = plugin._factor_config().activity_factors
        first = cap.warnings()
        plugin._factor_config()
        second = cap.warnings()

    assert factors["work"] == pytest.approx(0.75), factors
    assert factors["commute"] == pytest.approx(0.85)
    assert factors["lunch"] == pytest.approx(1.1)
    assert set(module.ALLOWED_ACTIVITIES) <= set(factors), "仍有活动没有因子"
    assert any("已按内置默认补齐" in msg for msg in first), first
    assert "work" in first[0] and "commute" in first[0], first[0]
    assert len(second) == len(first), f"补齐告警不该每轮刷：{second}"


def test_explicit_factor_wins_over_the_builtin_default():
    module, plugin = _make_plugin(OLD_NINE + ["work=0.9", "lunch=1.0"])
    factors = plugin._factor_config().activity_factors
    assert factors["work"] == pytest.approx(0.9), "显式配置被默认值覆盖了"
    assert factors["lunch"] == pytest.approx(1.0)
    # 仍然缺的那些才补齐
    assert factors["overtime"] == pytest.approx(0.45)


def test_neutral_factor_can_be_written_explicitly():
    """想让她某活动保持中性（1.0）就显式写出来——不必再靠"删行"。"""

    module, plugin = _make_plugin(
        [line for line in OLD_NINE if not line.startswith("daily=")] + ["daily=1.0"]
    )
    with _Capture(module) as cap:
        factors = plugin._factor_config().activity_factors
    assert factors["daily"] == pytest.approx(1.0)
    assert not any("daily" in msg for msg in cap.warnings()), cap.warnings()


def test_empty_factor_table_still_falls_back_to_builtin():
    module, plugin = _make_plugin([])
    factors = plugin._factor_config().activity_factors
    assert factors == module._default_activity_factors(), "整表被清空时应整体回退内置表"


# ================= E2. replace 模式：删除内置因子后不再补齐 =================


def test_replace_mode_keeps_deleted_builtin_deleted():
    """replace 模式：删掉的内置因子保持删除（该活动按 1.0），缺键只告警不补齐。

    merge 模式（默认）会把删掉的行按内置默认填回来——防的是升级丢因子与手滑；
    用户显式切到 replace 就是声明「这张表是整张表」，删除必须生效。
    """

    module, plugin = _make_plugin(OLD_NINE, factors_mode="replace")
    with _Capture(module) as cap:
        factors = plugin._factor_config().activity_factors
        first = cap.warnings()
        plugin._factor_config()
        second = cap.warnings()

    assert "work" not in factors, "replace 模式下被删的内置因子不该被补回来"
    assert "commute" not in factors and "overtime" not in factors
    assert factors["music"] == pytest.approx(0.9), "留下的行要原样生效"
    assert any("work" in msg and "commute" in msg for msg in first), first
    assert "不再补齐" in first[0] or "按 1.0" in first[0], first[0]
    assert len(second) == len(first), f"缺键提醒不该每轮刷：{second}"


def test_replace_mode_flags_missing_sleep_hard_gate():
    """replace 模式删掉 sleep=0：睡觉不再静音，这是硬闸，必须单独点名。"""

    module, plugin = _make_plugin(
        [line for line in OLD_NINE if not line.startswith("sleep=")],
        factors_mode="replace",
    )
    with _Capture(module) as cap:
        factors = plugin._factor_config().activity_factors
    assert "sleep" not in factors
    joined = " | ".join(cap.warnings())
    assert "睡觉" in joined and "静音" in joined, cap.warnings()


def test_replace_mode_empty_table_is_all_neutral_not_defaults():
    """replace + 空表 = 用户明确的「全部按 1.0」，不许落回内置表（merge 才回退）。"""

    module, plugin = _make_plugin([], factors_mode="replace")
    with _Capture(module) as cap:
        factors = plugin._factor_config().activity_factors
    assert factors == {}
    assert any("列表为空" in msg for msg in cap.warnings()), cap.warnings()


def test_unknown_factors_mode_falls_back_to_merge():
    module, plugin = _make_plugin(OLD_NINE, factors_mode="bogus")
    with _Capture(module) as cap:
        factors = plugin._factor_config().activity_factors
    assert factors["work"] == pytest.approx(0.75), "未知模式应按 merge 补齐"
    assert any("merge/replace" in msg for msg in cap.warnings()), cap.warnings()


def test_replace_mode_deleted_activity_is_neutral_at_compute():
    """端到端：replace 模式删掉 work=0.75 后，work 与 daily 的倍率一致（因子 1.0）。"""

    from life_factors import FactorConfig, compute_adjust

    module, plugin = _make_plugin(OLD_NINE, factors_mode="replace")
    factors = plugin._factor_config().activity_factors

    def adjust_with(activity: str) -> float:
        return compute_adjust(
            activity=activity, emotion=5.0, energy=5.0, sick=False, sleep_debt_nights=0,
            date_factor=1.0, material_count=0, now_minutes=12 * 60,
            config=FactorConfig(activity_factors=factors),
        ).adjust

    assert adjust_with(A.WORK) == pytest.approx(adjust_with(A.DAILY))


# ================= F. 行为：在岗真的比日常更安静 =================


def test_work_factor_actually_lowers_the_multiplier():
    from life_factors import FactorConfig, compute_adjust

    module = _load()
    factors = module._default_activity_factors()

    def adjust_with(activity: str) -> float:
        return compute_adjust(
            activity=activity, emotion=5.0, energy=5.0, sick=False, sleep_debt_nights=0,
            date_factor=1.0, material_count=0, now_minutes=12 * 60,
            config=FactorConfig(activity_factors=factors),
        ).adjust

    daily = adjust_with(A.DAILY)
    work = adjust_with(A.WORK)
    meeting = adjust_with(A.MEETING)
    lunch = adjust_with(A.LUNCH)
    assert work < daily, (work, daily)
    assert meeting < work < lunch, (meeting, work, lunch)
    assert lunch > daily, lunch


def test_activity_prompt_shows_three_recent_tiers():
    """端到端：真调一次 ``_ask_activity``，提示词里应出现近/中/远三层。

    （放本文件是因为夹具在这里；被测行为属于活动提示词，与工作表无关。）
    """

    async def run():
        module, plugin = _make_plugin()
        now = local_ts(MONDAY.replace(hour=10))
        plugin._state.recent_events = [
            {"at": now - 3600, "label": "近事", "text": "锅底糊了", "emotion": 0.0},
            {"at": now - 36 * 3600, "label": "中事", "text": "电台来新投稿", "emotion": 0.0},
            {"at": now - 9 * 24 * 3600, "label": "远事", "text": "上上周的事", "emotion": 0.0},
            {"at": now - 40 * 24 * 3600, "label": "太旧", "text": "不该出现", "emotion": 0.0},
        ]
        captured: dict[str, str] = {}

        async def fake_generate(**kwargs):
            captured["prompt"] = str(kwargs.get("prompt") or "")
            return {"success": True, "response": '{"activity": "daily", "scene": "在发呆"}'}

        plugin.ctx.llm.generate = fake_generate
        await plugin._ask_activity(now)
        prompt = captured.get("prompt", "")
        assert "近（12 小时内）" in prompt, prompt
        assert "中（3 天内）" in prompt, prompt
        assert "远（14 天内）" in prompt, prompt
        assert "近事" in prompt and "中事" in prompt and "远事" in prompt
        assert "太旧" not in prompt, "超过远层窗口的经历不该进提示词"

    asyncio.run(run())


def test_work_scene_reaches_the_activity_prompt():
    """端到端：在岗相位的提示词必须同时带上「适合的活动」与岗位职责。"""

    async def run():
        module, plugin = _make_plugin(schedule={
            "enabled": True, "workdays": "1-5", "work_window": "09:30-18:30",
            "commute_minutes": 45, "lunch_window": "12:00-13:00",
            "duty": "FMInfinity 共鸣电台无线电技术员",
        })
        plugin._state.last_tick_at = local_ts(MONDAY.replace(hour=10))
        captured: dict[str, str] = {}

        async def fake_generate(**kwargs):
            captured["prompt"] = str(kwargs.get("prompt") or "")
            return {"success": True, "response": '{"activity": "work", "scene": "在调设备"}'}

        plugin.ctx.llm.generate = fake_generate
        decision = await plugin._ask_activity(local_ts(MONDAY.replace(hour=10)))
        assert decision is not None and decision.activity == A.WORK
        prompt = captured.get("prompt", "")
        assert "work=工作" in prompt, prompt
        assert "现在适合的活动" in prompt
        assert "打游戏" in prompt          # 出现在「都不合适」那句里

    asyncio.run(run())
