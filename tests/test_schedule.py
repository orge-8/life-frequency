# -*- coding: utf-8 -*-
"""L3：作息班表（日程锚点）——把「上班」写进作息骨架。

要解决的真问题（探针实测，2026-10-01）：活动枚举与内置时段表都是**学生作息**
（凌晨 3–11 点睡、下午游戏看番），对一个「8 点要出门上班」的角色，
08:30 会被排成「睡不着，继续做点事」、15:30 排成「打游戏」。提示词里也没有
任何「今天工作日吗、现在是否在岗」的信息——唯一提到职业的只有人设那一行。

于是加一层**可选**的班表（默认关闭）：

- **提示词**拿到事实：工作日/休息日、在岗/午休/通勤/已下班、岗位职责；
- **强制层**守住骨架：班表窗口内不许睡（生病或体力过低例外）、在岗不许打游戏/看番；
- **冷启动/规则表**在班表相位里不再用学生时段表。

这一层刻意**不改活动枚举**：场景文字（`scene`）本来就能写「在电台值班」，
真正缺的是「哪个活动 × 哪个时段」的确定性约束。
"""

import asyncio
import copy
import logging
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config  # noqa: E402

import life_activity as A  # noqa: E402
import life_sim as S  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
TZ = 480

MONDAY = datetime(2026, 10, 5, 0, 0)
SATURDAY = datetime(2026, 10, 3, 0, 0)


def local_ts(dt: datetime) -> float:
    """本地 naive 时刻 → epoch（插件按 ``tz_offset_minutes=480`` 解释）。"""

    return dt.replace(tzinfo=timezone.utc).timestamp() - TZ * 60


def duty_text() -> str:
    return "FMInfinity 共鸣电台无线电技术员：观测共鸣、搜集与推送歌曲"


def work_config(**overrides) -> A.ScheduleConfig:
    base = dict(
        enabled=True,
        workdays=(1, 2, 3, 4, 5),
        work_window=(9 * 60 + 30, 18 * 60 + 30),   # 09:30-18:30
        commute_minutes=45,
        lunch_window=(12 * 60, 13 * 60),           # 12:00-13:00
        duty=duty_text(),
    )
    base.update(overrides)
    return A.ScheduleConfig(**base)


def facts_at(moment: datetime, config: A.ScheduleConfig | None = None) -> A.ScheduleFacts:
    return A.schedule_facts(moment, config or work_config())


def policy(**overrides) -> A.EnforcePolicy:
    base = dict(
        sleep_window=(0, 1440),          # 「随时都够格睡」——把判据单独留给班表
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
        sleep_minutes_today=0,
        awake_minutes_today=600,
        schedule=facts_at(MONDAY.replace(hour=10)),
    )
    base.update(overrides)
    return A.ActivityFacts(**base)


# ================= A. 解析配置 =================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1-5", (1, 2, 3, 4, 5)),
        ("六日", (6, 7)),
        ("1,3,5", (1, 3, 5)),
        ("周三、周日", (3, 7)),
        ("6-1", (1, 6, 7)),        # 跨周区间：周六、周日、周一
        ("一二三四五", (1, 2, 3, 4, 5)),
        ("1 2 3", (1, 2, 3)),
    ],
)
def test_parse_workdays_accepts_common_forms(raw, expected):
    days, warnings = A.parse_workdays(raw)
    assert days == expected, (raw, days)
    assert warnings == [], warnings


def test_parse_workdays_empty_uses_default_silently():
    days, warnings = A.parse_workdays("")
    assert days == (1, 2, 3, 4, 5)
    assert warnings == [], "空值就是「没配」，不该告警"


@pytest.mark.parametrize("raw", ["abc", "0-9", "周八", "13"])
def test_parse_workdays_warns_and_falls_back(raw):
    days, warnings = A.parse_workdays(raw)
    assert days == (1, 2, 3, 4, 5), "坏值必须回退默认"
    assert warnings, f"{raw!r} 应当告警（否则用户改了配置毫无反应）"


def test_parse_schedule_window_ok_and_warn():
    window, warnings = A.parse_schedule_window("09:30-18:30", (0, 0))
    assert window == (570, 1110) and warnings == []

    window, warnings = A.parse_schedule_window("09:30~18:30", (0, 0))
    assert window == (570, 1110), "波浪号也应当能解析"

    window, warnings = A.parse_schedule_window("", (570, 1110))
    assert window == (570, 1110) and warnings == []

    for bad in ("abc", "09:30", "25:00-18:30", "09:00-09:00"):
        window, warnings = A.parse_schedule_window(bad, (570, 1110))
        assert window == (570, 1110), bad
        assert warnings, bad


# ================= B. 相位判定 =================


@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (6, 0, A.SCHEDULE_AFTER),       # 深夜：还是她自己的时间
        (7, 0, A.SCHEDULE_AFTER),       # 07:15 之前都算自由时间
        (7, 30, A.SCHEDULE_BEFORE),     # 出门前 90 分钟（08:45 出门）→ 准备时间
        (8, 50, A.SCHEDULE_COMMUTE),    # 08:45 起算通勤（09:30 往前 45 分钟）
        (9, 45, A.SCHEDULE_WORK),
        (12, 30, A.SCHEDULE_LUNCH),
        (15, 0, A.SCHEDULE_WORK),
        (18, 45, A.SCHEDULE_OFF_WORK),  # 下班回程（18:30 + 45 分钟）
        (21, 0, A.SCHEDULE_AFTER),
    ],
)
def test_phases_across_a_workday(hour, minute, expected):
    result = facts_at(MONDAY.replace(hour=hour, minute=minute))
    assert result.phase == expected, (hour, minute, result.phase)
    assert result.is_workday is True
    assert result.enabled is True


def test_prep_window_starts_before_going_out():
    """「出门前 90 分钟」是准备时间，也是禁睡窗口的起点（详见 SCHEDULE_PREP_MINUTES）。"""

    assert facts_at(MONDAY.replace(hour=8, minute=0)).phase == A.SCHEDULE_BEFORE
    assert facts_at(MONDAY.replace(hour=7, minute=10)).phase == A.SCHEDULE_AFTER
    assert facts_at(MONDAY.replace(hour=7, minute=20)).phase == A.SCHEDULE_BEFORE
    assert A.SCHEDULE_PREP_MINUTES == 90


def test_weekend_has_no_work_phase():
    result = facts_at(SATURDAY.replace(hour=10))
    assert result.is_workday is False
    assert result.phase == A.SCHEDULE_REST_DAY
    assert result.in_duty_window() is False


def test_disabled_schedule_reports_off_duty():
    result = facts_at(MONDAY.replace(hour=10), A.ScheduleConfig())
    assert result.enabled is False
    assert result.phase == A.SCHEDULE_OFF_DUTY
    assert result.prompt_lines == (), "关闭班表时不该往提示词里塞任何作息"


def test_prompt_lines_carry_weekday_phase_and_duty():
    work = facts_at(MONDAY.replace(hour=10))
    text = "\n".join(work.prompt_lines)
    assert "工作日" in text and "周一" in text
    assert "09:30-18:30" in text
    assert "在岗" in text
    assert duty_text() in text
    assert "不能" in text or "不合适" in text, "约束要明说，别指望模型自己推"

    weekend = facts_at(SATURDAY.replace(hour=10))
    assert "休息日" in "\n".join(weekend.prompt_lines)

    lunch = facts_at(MONDAY.replace(hour=12, minute=30))
    assert "午休" in "\n".join(lunch.prompt_lines)


def test_minutes_to_work_and_off_are_reported():
    before = facts_at(MONDAY.replace(hour=8, minute=0))
    assert before.minutes_to_work == 45, "08:00 距出门（08:45）还有 45 分钟"

    work = facts_at(MONDAY.replace(hour=15, minute=0))
    assert work.minutes_to_off == 3 * 60 + 30, "15:00 距下班（18:30）还有 3.5 小时"


def test_schedule_facts_tolerates_junk_input():
    """传进来的不是 datetime 也不许抛——它跑在每 10 分钟的生活循环里。"""

    for junk in (None, "", object(), 42):
        result = A.schedule_facts(junk, work_config())
        assert isinstance(result, A.ScheduleFacts)
        assert result.enabled is False


# ================= C. 相容性判定 =================


def test_sleep_is_blocked_during_work_but_excused_by_health():
    work = activity_facts()
    assert A.activity_blocked_by_schedule(A.SLEEP, work, policy()), "上班时段不能睡"

    tired = activity_facts(energy=2.0)
    assert A.activity_blocked_by_schedule(A.SLEEP, tired, policy()) == "", "体力撑不住可以睡"

    sick = activity_facts(sick=True)
    assert A.activity_blocked_by_schedule(A.SLEEP, sick, policy()) == "", "生病可以睡"

    before_sleep = activity_facts()
    assert A.activity_blocked_by_schedule(A.BEFORE_SLEEP, before_sleep, policy())


def test_sleep_blocked_from_prep_through_commute_and_lunch():
    """出门前 90 分钟起到下班回家之前都不能睡——否则会睡掉整个上班日。"""

    for moment in (
        MONDAY.replace(hour=8, minute=0),    # 出门前
        MONDAY.replace(hour=9, minute=0),    # 通勤
        MONDAY.replace(hour=12, minute=30),  # 午休
        MONDAY.replace(hour=10, minute=0),   # 在岗
    ):
        facts = activity_facts(schedule=facts_at(moment))
        assert A.activity_blocked_by_schedule(A.SLEEP, facts, policy()), moment

    # 深夜（自由时间）与下班后照旧可以睡
    for moment in (MONDAY.replace(hour=6, minute=0), MONDAY.replace(hour=21, minute=0)):
        facts = activity_facts(schedule=facts_at(moment))
        assert A.activity_blocked_by_schedule(A.SLEEP, facts, policy()) == "", moment


def test_fun_is_blocked_on_duty_but_allowed_on_break_and_after_work():
    assert A.activity_blocked_by_schedule(A.GAME, activity_facts(), policy())
    assert A.activity_blocked_by_schedule(A.ANIME, activity_facts(), policy())

    lunch = activity_facts(schedule=facts_at(MONDAY.replace(hour=12, minute=30)))
    assert A.activity_blocked_by_schedule(A.ANIME, lunch, policy()) == "", "午休看一集合情合理"

    commute = activity_facts(schedule=facts_at(MONDAY.replace(hour=9, minute=0)))
    assert A.activity_blocked_by_schedule(A.GAME, commute, policy()) == ""

    after = activity_facts(schedule=facts_at(MONDAY.replace(hour=21, minute=0)))
    assert A.activity_blocked_by_schedule(A.GAME, after, policy()) == ""


def test_nothing_is_blocked_on_weekend_or_when_disabled():
    weekend = activity_facts(schedule=facts_at(SATURDAY.replace(hour=10)))
    assert A.activity_blocked_by_schedule(A.SLEEP, weekend, policy()) == ""
    assert A.activity_blocked_by_schedule(A.GAME, weekend, policy()) == ""

    off = activity_facts(schedule=A.ScheduleFacts())
    assert A.activity_blocked_by_schedule(A.SLEEP, off, policy()) == ""
    assert A.activity_blocked_by_schedule(A.GAME, off, policy()) == ""


# ================= D. enforce 收口 =================


def test_enforce_refuses_to_sleep_through_work():
    result = A.enforce(
        activity_facts(), A.ActivityDecision(A.SLEEP, "困了"), policy()
    )
    assert result.activity == A.DAILY, result
    assert result.source == A.SOURCE_ENFORCED
    assert "不该睡觉" in result.note, result.note


def test_enforce_rejects_desk_fun_and_pulls_her_out_of_it():
    """当前活动也不合规时必须真的拉出来，不能「保持」在游戏里。"""

    gaming = activity_facts(activity=A.GAME, minutes_in_activity=300)
    result = A.enforce(gaming, A.ActivityDecision(A.ANIME, "看两集"), policy())
    assert result.activity == A.DAILY, "在岗摸鱼要被收口成中性活动"
    assert "不该看番" in result.note, result.note

    # 当前活动合规时保持当前活动，别把她无故重置
    working = activity_facts(activity=A.DAILY, minutes_in_activity=300)
    result2 = A.enforce(working, A.ActivityDecision(A.GAME, "开一把"), policy())
    assert result2.activity == A.DAILY
    assert "不该打游戏" in result2.note, result2.note


def test_enforce_allows_health_exception_to_win():
    tired = activity_facts(energy=1.5)
    assert A.enforce(tired, A.ActivityDecision(A.SLEEP, "撑不住了"), policy()).activity == A.SLEEP

    sick = activity_facts(sick=True)
    assert A.enforce(sick, A.ActivityDecision(A.SLEEP, "病了"), policy()).activity == A.SLEEP


def test_enforce_lets_her_relax_on_lunch_break():
    lunch = activity_facts(
        schedule=facts_at(MONDAY.replace(hour=12, minute=30)), minutes_in_activity=180
    )
    result = A.enforce(lunch, A.ActivityDecision(A.ANIME, "午休看一集"), policy())
    assert result.activity == A.ANIME, result


def test_enforce_is_unchanged_when_schedule_is_off():
    """回归：默认（班表关闭）行为必须与加这一层之前逐字一致。"""

    facts = activity_facts(schedule=A.ScheduleFacts())
    # sleep_window 放开 + 清醒够 + 体力正常 ⇒ 想睡就睡（旧行为）
    assert A.enforce(facts, A.ActivityDecision(A.SLEEP, "困"), policy()).activity == A.SLEEP
    assert A.enforce(facts, A.ActivityDecision(A.GAME, "玩"), policy()).activity == A.GAME


def test_enforce_on_weekend_ignores_the_schedule():
    weekend = activity_facts(schedule=facts_at(SATURDAY.replace(hour=10)))
    assert A.enforce(weekend, A.ActivityDecision(A.SLEEP, "补觉"), policy()).activity == A.SLEEP
    assert A.enforce(weekend, A.ActivityDecision(A.GAME, "开黑"), policy()).activity == A.GAME


# ================= E. 规则表 / 冷启动 =================


def test_rules_seed_puts_her_at_work_instead_of_the_student_window():
    schedule = facts_at(MONDAY.replace(hour=10, minute=30))
    seed = A.rule_based_activity(
        now_minutes=10 * 60 + 30, energy=6.5, schedule=schedule, work_scene="在电台值守"
    )
    assert seed.activity == A.WORK, seed
    assert seed.scene == "在电台值守"
    assert "在岗" in seed.note, seed.note

    # 内置时段表在同一时刻给的是「深夜做题（睡不着）」——正是要修掉的行为
    table_only = A.rule_based_activity(now_minutes=10 * 60 + 30, energy=6.5)
    assert table_only.activity == A.NIGHT_STUDY


def test_rules_seed_handles_commute_and_lunch_scenes():
    commute = A.rule_based_activity(
        now_minutes=9 * 60, energy=6.5, schedule=facts_at(MONDAY.replace(hour=9))
    )
    assert commute.activity == A.COMMUTE and "路上" in commute.scene, commute

    lunch = A.rule_based_activity(
        now_minutes=12 * 60 + 30, energy=6.5, schedule=facts_at(MONDAY.replace(hour=12, minute=30))
    )
    assert lunch.activity == A.LUNCH and "午休" in lunch.scene, lunch

    off_work = A.rule_based_activity(
        now_minutes=18 * 60 + 45, energy=6.5, schedule=facts_at(MONDAY.replace(hour=18, minute=45))
    )
    assert off_work.activity == A.OFF_WORK and "回家" in off_work.scene, off_work


def test_rules_seed_does_not_send_her_to_bed_before_work():
    """出门前的准备时间不该再由学生时段表安排（它会给「深夜做题」甚至睡）。"""

    morning = A.rule_based_activity(
        now_minutes=8 * 60 + 30, energy=6.5, schedule=facts_at(MONDAY.replace(hour=8, minute=30))
    )
    assert morning.activity == A.DAILY, morning
    assert "出门" in morning.scene

    # 同一时刻、低体力：健康优先，仍然允许睡（班表窗口内的例外）
    tired_facts = activity_facts(
        schedule=facts_at(MONDAY.replace(hour=8, minute=30)), energy=1.0
    )
    assert A.activity_blocked_by_schedule(A.SLEEP, tired_facts, policy()) == ""

    # 深夜（自由时间）交回时段表
    night = A.rule_based_activity(
        now_minutes=6 * 60, energy=6.5, schedule=facts_at(MONDAY.replace(hour=6))
    )
    table_only = A.rule_based_activity(now_minutes=6 * 60, energy=6.5)
    assert night.activity == table_only.activity


def test_rules_seed_keeps_the_old_table_on_weekend():
    schedule = facts_at(SATURDAY.replace(hour=10, minute=30))
    weekend = A.rule_based_activity(now_minutes=10 * 60 + 30, energy=6.5, schedule=schedule)
    table_only = A.rule_based_activity(now_minutes=10 * 60 + 30, energy=6.5)
    assert weekend.activity == table_only.activity, "休息日就该回到原来的时段表"


def test_cold_start_through_life_sim_respects_the_schedule():
    config = S.SimConfig(schedule=work_config())
    monday_10 = local_ts(MONDAY.replace(hour=10))
    state = S.new_state(now=monday_10, config=config)
    assert state.activity == A.WORK, state.activity
    assert state.activity != A.NIGHT_STUDY, "冷启动不该再给学生作息"


def test_state_machine_blocks_sleep_during_work():
    """走完整链路：配置 → SimConfig → EnforcePolicy → facts → enforce。"""

    config = S.SimConfig(schedule=work_config())
    monday_10 = local_ts(MONDAY.replace(hour=10))
    state = S.new_state(now=monday_10, config=config)
    state.activity = A.DAILY
    state.activity_since = monday_10 - 3600
    state.awake_minutes_today = 600
    state.energy = 7.0

    state = S.enforce_and_apply(
        state, now=monday_10, config=config, decision=A.ActivityDecision(A.SLEEP, "困")
    )
    assert state.activity != A.SLEEP, "上班时间被睡过去了"
    assert "不该睡觉" in state.activity_note


# ================= F. 提示词 =================


def test_prompt_carries_the_schedule_lines():
    facts = facts_at(MONDAY.replace(hour=10))
    prompt = A.build_prompt(
        A.PromptInput(bot_name="测试bot", persona="电台技术员", schedule_lines=facts.prompt_lines)
    )
    assert "今天是工作日" in prompt
    assert "在岗" in prompt
    assert duty_text() in prompt


def test_prompt_has_no_schedule_section_when_disabled():
    prompt = A.build_prompt(A.PromptInput(bot_name="麦麦", persona="x"))
    assert "工作日" not in prompt and "上班" not in prompt


# ================= G. 插件接线 =================


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_schedule")


def _make_plugin(**schedule_overrides):
    module = _load()
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["frequency"]["quiet_hours"] = []
    config["events"]["fire_probability"] = 0.0
    config["proactive"]["enabled"] = False
    config["simulation"]["dry_run"] = False
    config["apply"]["only_active_sessions"] = False
    #: ⚠ 班表在**顶层** `[schedule]`：嵌在 `[activity]` 里时 SDK 不展开成 section，
    #: WebUI 只会显示成 `[object Object]` 的文本框（见 test_schedule_is_a_top_level_section）
    schedule = dict(config.get("schedule") or {})
    schedule.update({"enabled": True, "workdays": "1-5", "work_window": "09:30-18:30",
                     "commute_minutes": 45, "lunch_window": "12:00-13:00",
                     "duty": duty_text()})
    schedule.update(schedule_overrides)
    config["schedule"] = schedule
    bind_context(plugin, ctx, config)
    return module, plugin, host


def _webui_schema(module) -> dict:
    """插件注册给 WebUI 的配置 Schema（与真机同一条生成链路）。

    ⚠ 必须走**插件实例**的 ``get_webui_config_schema``：真机 Runner 调的就是它
    （``runner_main.py:1468-1475``），而插件覆写了它来把嵌套配置提升成 section。
    """

    plugin = module.create_plugin()
    return plugin.get_webui_config_schema(
        plugin_id=module.__plugin_id__, plugin_name="生活频率", plugin_version="1.3.0",
        plugin_description="", plugin_author="orge-8",
    )["sections"]


def test_schedule_is_a_top_level_section_in_the_webui_schema():
    """班表必须是**顶层**节，否则 WebUI 会把整份配置显示成 ``[object Object]``。

    SDK 只在顶层把「是配置模型类」的字段展开成 section（``maibot_sdk/config.py:209-227``）；
    ``_build_section_schema``（同文件 :274-280）对节内字段不做这个判断，一律走
    ``_build_field_schema`` → ``_map_field_type`` 映成 ``type=object`` 且**不带
    ``properties``**，而 WebUI 的插件配置页按 ``ui_type`` 选控件
    （``dashboard/src/routes/plugin-config.tsx:163-346``）且**没有 json 分支**
    ⇒ 落到 default 当文本框 ⇒ 值被字符串化成 ``[object Object]``。
    """

    module = _load()
    sections = _webui_schema(module)
    assert "schedule" in sections, f"班表没成为顶层节：{sorted(sections)}"
    fields = sections["schedule"]["fields"]
    assert set(fields) == {
        "enabled", "workdays", "work_window", "commute_minutes",
        "lunch_window", "duty", "work_scene",
    }, sorted(fields)
    for name, field in fields.items():
        assert field["type"] in ("boolean", "string", "integer"), (name, field["type"])
        assert field["label"] != name, f"{name} 缺中文 label，WebUI 会显示成英文键名"
    assert "schedule" not in (sections["activity"]["fields"] or {}), "班表又被塞回 [activity] 里了"


def test_no_object_typed_field_remains_in_the_webui_schema():
    """全页不许再有任何 ``type=object`` 字段（它们就是 ``[object Object]`` 的来源）。

    嵌套配置被插件提升成「点号路径 section」（见下一条用例），所以这里应当是空集。
    """

    module = _load()
    sections = _webui_schema(module)
    objects = [
        (section_name, field_name)
        for section_name, section in sections.items()
        for field_name, field in (section.get("fields") or {}).items()
        if field.get("type") == "object"
    ]
    assert objects == [], f"仍有 object 型字段，WebUI 会显示 [object Object]：{objects}"
    empty = [name for name, section in sections.items() if not (section.get("fields") or {})]
    assert empty == [], f"留下了空 section（页面上是空卡片）：{empty}"


def test_nested_config_objects_become_dotted_path_sections():
    """``[activity.llm]`` / ``[emotion_energy.curves.*]`` 被提升成点号路径 section。

    WebUI 读写按 **section 名当点号路径**（``plugin-config/utils.ts:25-76``），
    因此 ``config.toml`` 的键路径一个都不用改，旧配置继续生效。
    """

    module = _load()
    sections = _webui_schema(module)

    llm = sections.get("activity.llm")
    assert llm, sorted(sections)
    assert llm["title"] == "活动模型调用参数"
    assert set(llm["fields"]) == {
        "task_name", "temperature", "max_tokens", "timeout_ms",
        "min_interval_seconds", "fail_streak_limit", "cooldown_minutes",
        "recent_events_in_prompt", "recent_near_hours", "recent_mid_hours",
        "recent_far_days", "persona_max_chars", "skip_when_forced",
        "recent_pick_mode", "recent_max_per_label",
    }

    for path in ("emotion_energy.curves.frequency", "emotion_energy.curves.necessity"):
        section = sections.get(path)
        assert section, f"{path} 没被提升：{sorted(sections)}"
        assert set(section["fields"]) == {"mood", "energy"}, path
        for field in section["fields"].values():
            assert field["type"] == "array" and field["ui_type"] == "list"
    # 被掏空的中间层不该留下空卡片
    assert "emotion_energy.curves" not in sections


def test_webui_schema_paths_all_write_back_to_the_real_config():
    """**最强的一条**：按 WebUI 的写法逐字段写回，必须落在真实配置键上。

    页面的写入是 ``setNestedField(config, sectionName, fieldName, value)``
    （section 名按 ``.`` 拆路径、字段名原样），校验用的是
    ``ConfigDict(extra="ignore")`` —— 所以路径写错**不会报错，只会静默丢掉**。
    这里就用 Python 复刻同一套写法，再拿 pydantic 校验，确认每个字段都真的落到了
    对应位置（默认值之外发生了变化），从而把「改错了默默无效」这类缺陷挡在门外。
    """

    module = _load()
    plugin = module.create_plugin()
    sections = plugin.get_webui_config_schema(
        plugin_id=module.__plugin_id__, plugin_name="", plugin_version="",
        plugin_description="", plugin_author="",
    )["sections"]
    default = module.LifeFrequencyConfig().model_dump()

    def set_nested_field(config: dict, path: str, field_name: str, value) -> None:
        """复刻 plugin-config/utils.ts:setNestedField（section 名拆点、字段名原样）。"""

        current_target = config
        current_source = config
        for part in [p for p in path.split(".") if p]:
            source_value = current_source.get(part) if isinstance(current_source, dict) else None
            if isinstance(source_value, dict):
                next_value = dict(source_value)
                current_source = source_value
            else:
                next_value = {}
                current_source = None
            current_target[part] = next_value
            current_target = next_value
        current_target[field_name] = value

    def get_nested(config: dict, path: str):
        current = config
        for part in [p for p in path.split(".") if p]:
            current = current.get(part) if isinstance(current, dict) else None
        return current

    def probe_value(field):
        kind = field.get("type")
        if kind == "boolean":
            return not bool(field.get("default"))
        if kind == "integer":
            return int(field.get("default") or 0) + 7
        if kind == "number":
            return float(field.get("default") or 0.0) + 7.5
        if kind == "array":
            return list(field.get("default") or []) + ["5=0.99"]
        return "__probe__"

    checked = 0
    for section_name, section in sections.items():
        for field_name, field in (section.get("fields") or {}).items():
            payload = copy.deepcopy(default)
            value = probe_value(field)
            set_nested_field(payload, section_name, field_name, value)
            model = module.LifeFrequencyConfig.model_validate(payload)
            landed = get_nested(model.model_dump(), section_name)
            assert isinstance(landed, dict), f"{section_name} 整节没落到配置里"
            assert landed.get(field_name) == value, (
                f"{section_name}.{field_name} 写回后没有落到真实配置键上"
                f"（拿到 {landed.get(field_name)!r}）—— WebUI 里改这个字段会静默失效"
            )
            checked += 1
    assert checked >= 40, f"只检查到 {checked} 个字段，schema 可能没生成全"


def test_webui_schema_override_never_breaks_the_page():
    """修正逻辑自身出错时必须回退 SDK 原样输出——否则 Runner 会把整页变成空 Schema。"""

    module = _load()
    plugin = module.create_plugin()
    original = module._promote_nested_config_sections

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟修正逻辑出错")

    module._promote_nested_config_sections = boom
    try:
        schema = plugin.get_webui_config_schema(
            plugin_id=module.__plugin_id__, plugin_name="", plugin_version="",
            plugin_description="", plugin_author="",
        )
    finally:
        module._promote_nested_config_sections = original

    assert schema.get("sections"), "出错了却没回退，配置页会变空白"
    assert schema["sections"]["activity"]["fields"], "回退后连普通字段都没了"


def test_webui_visual_mode_shows_field_guidance_as_hints():
    """可视化模式只渲染 label/hint/placeholder，**从不渲染 description**。

    ``FieldRenderer``（``dashboard/src/routes/plugin-config.tsx:163-361``）按
    ``ui_type`` 分支里只输出这三样；本插件所有字段的填法说明都写在 description
    里，不抄进 hint 用户在配置页上一个字都看不到。有 description 的字段必须带
    hint（内容与 description 一致）。
    """

    module = _load()
    sections = _webui_schema(module)
    missing = []
    for section_name, section in sections.items():
        for field_name, field in (section.get("fields") or {}).items():
            if field.get("description") and field.get("hint") != field["description"]:
                missing.append(f"[{section_name}] {field_name}")
    assert missing == [], f"这些字段的说明在可视化模式下看不见：{missing}"


def test_webui_optional_sections_collapsed_by_default():
    """默认关闭的可选功能节默认收起：标题与说明仍可见，点开即用。

    可视化模式所有节默认展开，整页十几张卡片会淹没常用配置；只收起
    ``proactive`` / ``schedule`` / ``social`` 这三个默认关闭的集成。
    """

    module = _load()
    sections = _webui_schema(module)
    for name in ("proactive", "schedule", "social"):
        assert sections[name]["collapsed"] is True, f"[{name}] 应默认收起"
    expanded = [n for n, s in sections.items() if n not in ("proactive", "schedule", "social")
                and s.get("collapsed")]
    assert expanded == [], f"不该收起的节被收起了：{expanded}"


def test_webui_free_text_fields_use_textarea():
    """成句的自由文本字段用多行输入框（textarea 占满整行宽，比单行输入好编辑）。

    ``x-widget: "textarea"`` 经 SDK 映射成 ``ui_type=textarea``；
    ``FieldRenderer`` 的 textarea 分支用 ``rows`` 决定高度。
    """

    module = _load()
    sections = _webui_schema(module)
    for section_name, field_name in (
        ("schedule", "duty"),
        ("schedule", "work_scene"),
        ("date", "birthday_material"),
    ):
        field = sections[section_name]["fields"][field_name]
        assert field["ui_type"] == "textarea", (section_name, field_name, field["ui_type"])
        assert field["rows"] >= 2, (section_name, field_name)


def test_webui_enum_like_text_fields_keep_labels_short_and_hinted():
    """枚举型 text 字段不再把可选值塞进 label（会被截断），可选值挪进 hint/placeholder。

    ``filter_mode`` 的旧 label 「过滤模式（all/whitelist/blacklist）」在卡片里显示不全；
    label 收短后，可选值由 hint（= description）与 placeholder 表达。
    """

    module = _load()
    sections = _webui_schema(module)
    assert sections["frequency"]["fields"]["mode_source"]["label"] == "模式来源"
    assert sections["apply"]["fields"]["filter_mode"]["label"] == "过滤模式"
    assert sections["apply"]["fields"]["filter_mode"]["placeholder"] == "all"
    assert sections["activity.llm"]["fields"]["recent_pick_mode"]["placeholder"] == "smart"


def test_plugin_config_maps_into_sim_config():
    _module, plugin, _host = _make_plugin()
    schedule = plugin._schedule_config()
    assert schedule.enabled is True
    assert schedule.workdays == (1, 2, 3, 4, 5)
    assert schedule.work_window == (570, 1110)
    assert schedule.lunch_window == (720, 780)
    assert schedule.commute_minutes == 45
    assert plugin._sim_config().schedule == schedule


def test_plugin_warns_once_on_bad_workdays():
    """同一个坏值只告警一次（不同类型的问题各告警一次），且都留痕。"""

    module, plugin, _host = _make_plugin(workdays="abc")
    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    log = logging.getLogger(f"plugin.{module.__plugin_id__}")
    log.setLevel(logging.WARNING)
    log.addHandler(handler)
    try:
        plugin._schedule_config()
        first = [m for m in records if "作息班表告警" in m]
        plugin._schedule_config()          # 第二次不该再刷
        second = [m for m in records if "作息班表告警" in m]
    finally:
        log.removeHandler(handler)
    assert first, "坏值必须告警，否则用户改了配置毫无反应"
    assert len(first) == len(set(first)), f"同一条告警重复了：{first}"
    assert second == first, f"第二次又刷了一遍：{second}"
    assert plugin._schedule_config().workdays == (1, 2, 3, 4, 5)


def test_status_cards_show_the_schedule_line():
    _module, plugin, _host = _make_plugin()
    now = local_ts(MONDAY.replace(hour=10))
    status = plugin._render_status(now)
    assert "作息：" in status and "在岗" in status, status
    activity_card = plugin._render_activity(now)
    assert "作息：" in activity_card and "在岗" in activity_card


def test_status_card_hides_schedule_when_disabled():
    _module, plugin, _host = _make_plugin(enabled=False)
    assert "作息：" not in plugin._render_status(local_ts(MONDAY.replace(hour=10)))


def test_activity_prompt_really_carries_the_schedule():
    """端到端：真调一次 ``_ask_activity``，看它发给模型的提示词。"""

    async def run():
        _module, plugin, host = _make_plugin()
        captured: dict[str, str] = {}

        async def fake_generate(**kwargs):
            captured["prompt"] = str(kwargs.get("prompt") or "")
            return {"success": True, "response": '{"activity": "daily", "scene": "在值守"}'}

        plugin.ctx.llm.generate = fake_generate
        now = local_ts(MONDAY.replace(hour=10))
        plugin._state.last_tick_at = now
        decision = await plugin._ask_activity(now)
        assert decision is not None and decision.activity == A.DAILY
        prompt = captured.get("prompt", "")
        assert "今天是工作日" in prompt, prompt
        assert "在岗" in prompt
        assert duty_text() in prompt

    asyncio.run(run())


def test_activity_prompt_has_no_schedule_when_disabled():
    async def run():
        _module, plugin, _host = _make_plugin(enabled=False)
        captured: dict[str, str] = {}

        async def fake_generate(**kwargs):
            captured["prompt"] = str(kwargs.get("prompt") or "")
            return {"success": True, "response": '{"activity": "daily", "scene": "发呆"}'}

        plugin.ctx.llm.generate = fake_generate
        now = local_ts(MONDAY.replace(hour=10))
        plugin._state.last_tick_at = now
        await plugin._ask_activity(now)
        prompt = captured.get("prompt", "")
        assert "今天是工作日" not in prompt, "关闭班表后不该出现作息约束"
        assert "上班时段" not in prompt

    asyncio.run(run())


def test_reseed_uses_the_schedule_instead_of_the_student_table():
    async def run():
        _module, plugin, _host = _make_plugin()
        now = local_ts(MONDAY.replace(hour=10, minute=30))
        plugin._state.llm_last_success_at = now - 48 * 3600   # 模型长时间没成功
        plugin._state.activity = A.SLEEP
        plugin._state.activity_since = now - 3600
        plugin._state.sleep_started_at = now - 3600
        config = plugin._sim_config()
        plugin._reseed_activity_if_stale(now, config)
        assert plugin._state.activity != A.NIGHT_STUDY, "重新取种子又回到学生作息"
        assert plugin._state.activity == A.WORK

    asyncio.run(run())
