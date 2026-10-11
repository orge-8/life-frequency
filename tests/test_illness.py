# -*- coding: utf-8 -*-
"""L3：生病机制的互动层（v1.14.0 方案 §5 / §7）。

覆盖三件事，都是「方案落地了但行为可能静默失效」的类型：

1. **关心句式**（§5.1）：严格句式（第二人称 / 叮嘱询问）命中、反向用例不命中、
   坏正则只告警不抛错、每会话每日只记一次；
2. **``/生活 送药``**（§5.2）：健康时拒绝、冷却、每场病上限、累计缩短封顶；
3. **双口径**（§7）：进模型与注入回复的描述**不含**剩余小时，状态卡**含**；
   README 命令清单里有这一行（否则用户根本不知道有这个命令）。
"""

import asyncio
import logging
import pathlib
import sys
import time

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config  # noqa: E402

import life_activity as A  # noqa: E402
import life_physio as P  # noqa: E402
import life_sim as S  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
README = PLUGIN_DIR / "README.md"


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_illness")


def _make_plugin(**config_overrides):
    """已 bind 好配置的插件实例（不跑 on_load；与 test_interop 的口径一致）。"""

    module = _load()
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["frequency"]["quiet_hours"] = []
    config["events"]["fire_probability"] = 0.0
    config["proactive"]["enabled"] = False
    config["apply"]["only_active_sessions"] = False
    for section, values in config_overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin._rebuild_from_config()
    plugin._state.last_tick_at = time.time()
    return module, plugin, host


def _make_sick(
    plugin, *, now=None, stage=A.COLD_WORSENING, hours_left=30.0, sick_for_hours=1.0
):
    now = float(now or time.time())
    plugin._state.cold_until = now + hours_left * 3600.0
    plugin._state.cold_started_at = now - sick_for_hours * 3600.0
    plugin._state.cold_stage = stage
    plugin._state.cold_days = 2
    plugin._state.activity = A.SICK_REST
    # 活动锚点放在过去：否则「最短停留期」会挡掉本轮所有切换（真实运行中她已经在养病）
    plugin._state.activity_since = now - 6 * 3600.0
    plugin._state.activity_source = A.SOURCE_ENFORCED
    return now


# ---------------------------------------------------------------- 关心句式（纯模块）


def test_care_patterns_hit_and_miss():
    patterns, warnings = S.parse_care_patterns(S.DEFAULT_CARE_PATTERNS)
    assert not warnings and patterns
    for hit in (
        "你吃药了吗",
        "宝宝好点了吗",
        "多喝热水，好点了吗",
        "还难受吗",
        "记得吃药",
        "你要多休息",
    ):
        assert S.care_hit(hit, patterns) is True, hit
    # 反向：自述（无第二人称、无叮嘱词）与第三人称 —— 不该被算成「关心」
    for miss in (
        "我昨天也感冒了，吃了药才好",
        "他感冒一个礼拜了",
        "今天天气不错",
        "我有点难受，先去躺一会儿",
        "",
    ):
        assert S.care_hit(miss, patterns) is False, miss


def test_care_patterns_bad_regex_warns_instead_of_raising():
    patterns, warnings = S.parse_care_patterns(["(未闭合", "你吃药了吗"])
    assert warnings and "不是合法正则" in warnings[0]
    assert len(patterns) == 1, "坏行必须被跳过，好行照常编译"
    assert S.care_hit("你吃药了吗", patterns) is True


def test_register_care_is_once_per_session_per_day():
    config = S.SimConfig()
    state = S.LifeState()
    now = time.time()
    assert S.register_care(state, now=now, session_id="s1", config=config) is True
    assert S.register_care(state, now=now + 60, session_id="s1", config=config) is False
    assert S.register_care(state, now=now, session_id="s2", config=config) is True
    assert sum(state.care_today.values()) == 2.0


# ---------------------------------------------------------------- 关心旁路（插件层）


def test_care_hook_records_only_when_sick_and_addressed():
    async def run():
        _module, plugin, _host = _make_plugin()
        now = _make_sick(plugin)
        before = plugin._state.emotion

        message = {"session_id": "private-1", "processed_plain_text": "你吃药了吗"}
        assert plugin._maybe_care(
            message, session_id="private-1", group_id="", now=now
        ) is True
        assert sum(plugin._state.care_today.values()) == 1.0
        assert plugin._state.emotion == pytest.approx(before + S.CARE_EMOTION_GAIN)

        # 同会话同日只记一次（去重表命中）
        assert plugin._maybe_care(
            message, session_id="private-1", group_id="", now=now + 60
        ) is False
        assert sum(plugin._state.care_today.values()) == 1.0

        # 群聊里没被 @：她不是在对她说，不算
        assert plugin._maybe_care(
            {"processed_plain_text": "你吃药了吗"},
            session_id="group-1",
            group_id="123",
            now=now,
        ) is False
        # 反向句式：别人在讲自己的感冒
        assert plugin._maybe_care(
            {"processed_plain_text": "我昨天也感冒了，吃了药才好"},
            session_id="private-2",
            group_id="",
            now=now,
        ) is False
        # 不生病时完全不记（连计数都不涨）
        plugin._state.cold_until = 0.0
        plugin._state.cold_stage = ""
        assert plugin._maybe_care(
            message, session_id="private-3", group_id="", now=now
        ) is False
        assert sum(plugin._state.care_today.values()) == 1.0

    asyncio.run(run())


def test_care_disabled_by_zero_cap():
    async def run():
        _module, plugin, _host = _make_plugin(health={"cold_care_daily_cap": 0})
        now = _make_sick(plugin)
        assert plugin._maybe_care(
            {"processed_plain_text": "你吃药了吗"},
            session_id="private-1",
            group_id="",
            now=now,
        ) is False
        assert not plugin._state.care_today

    asyncio.run(run())


# ---------------------------------------------------------------- /生活 送药


def test_medicine_command_refuses_when_healthy():
    async def run():
        _module, plugin, host = _make_plugin()
        plugin._state.cold_until = 0.0
        plugin._state.cold_stage = ""
        ok, text, level = await plugin.cmd_life_send_medicine(stream_id="group-1")
        assert ok is False and level == 1
        assert "身体挺好的" in text
        assert host.calls_of("send.text"), "拒绝也要回话（否则用户以为命令没生效）"

    asyncio.run(run())


def test_medicine_command_shortens_then_caps_and_cools_down():
    async def run():
        _module, plugin, _host = _make_plugin()
        now = _make_sick(plugin, hours_left=48.0)
        before = plugin._state.cold_until

        ok, text, _level = await plugin.cmd_life_send_medicine(
            stream_id="private-1", user_id="10001"
        )
        assert ok is True and "谢谢" in text or "收下" in text
        assert plugin._state.cold_until == pytest.approx(before - 4 * 3600.0)
        assert plugin._state.cold_medicine_count == 1
        assert plugin._state.care_today, "送药要把当日关心计满（加速康复的那一项）"

        # 同会话冷却：第 2 次立刻再送会被挡
        now2 = plugin._state.cold_until
        ok2, text2, _ = await plugin.cmd_life_send_medicine(
            stream_id="private-1", user_id="10001"
        )
        assert ok2 is False and "分钟" in text2
        assert plugin._state.cold_until == pytest.approx(now2)

        # 换个会话可以再送一次（每场病上限 2 次）
        ok3, _text3, _ = await plugin.cmd_life_send_medicine(
            stream_id="private-2", user_id="10002"
        )
        assert ok3 is True and plugin._state.cold_medicine_count == 2

        # 第 3 次（第三个会话）被「每场病 2 次」挡住
        ok4, text4, _ = await plugin.cmd_life_send_medicine(
            stream_id="private-3", user_id="10003"
        )
        assert ok4 is False and "休息" in text4
        assert plugin._state.cold_medicine_count == 2

        # 累计缩短不超过 cold_max_days/2 天（默认 3 天 → 1.5 天 = 36 小时）
        saved = plugin._state.cold_medicine_seconds
        assert saved <= max(1, int(plugin._sim_config().cold_max_days)) / 2 * 86400 + 1e-6

    asyncio.run(run())


def test_command_patterns_are_mutually_exclusive():
    """两条命令正则**不许同时命中**同一句话。

    宿主的命令匹配是 ``pattern.search()`` 且「第一个命中的组件赢」——同一句话命中
    两个组件时，行为取决于注册顺序（最难排查的静默失效）。所以
    ``/生活 送药`` 由 ``cmd_life_state`` 的 sub 分支处理、``/送药`` 由独立命令处理，
    两条正则刻意不重叠。
    """

    import re

    module = _load()
    state = re.compile(module.LIFE_STATE_PATTERN)
    medicine = re.compile(module.MEDICINE_COMMAND_PATTERN)
    samples = [
        "/生活", "/生活 状态", "/生活 频率", "/生活 送药", "/送药", "/送点药",
        "/送吃的", "生活 送药", "我给你送药", "/点歌 生活", "生活得好累",
    ]
    for text in samples:
        assert not (state.match(text) and medicine.match(text)), text

    assert state.match("/生活 送药").group("sub") == "送药"
    assert medicine.match("/送药") and medicine.match("/生活 送药") is None
    assert medicine.match("生活 送药") is None, "裸词不该触发状态变更命令"


def test_life_state_subcommand_routes_to_medicine():
    """``/生活 送药`` 必须真的送药（而不是回一张用法卡）。"""

    async def run():
        _module, plugin, _host = _make_plugin()
        _make_sick(plugin, hours_left=40.0)
        before = plugin._state.cold_until
        ok, text, level = await plugin.cmd_life_state(
            matched_groups={"sub": "送药"}, stream_id="private-9", text="/生活 送药"
        )
        assert ok is True and level == 1, text
        assert plugin._state.cold_until == pytest.approx(before - 4 * 3600.0)

        # 裸写「生活 送药」是普通聊天：不改变她的身体状态，也不拦截
        ok2, text2, level2 = await plugin.cmd_life_state(
            matched_groups={"sub": "送药"}, stream_id="private-10", text="生活 送药"
        )
        assert (ok2, text2, level2) == (False, "", 0)

    asyncio.run(run())


def test_medicine_reply_varies_by_stage():
    now = time.time()
    texts = {}
    for stage in (A.COLD_ONSET, A.COLD_WORSENING, A.COLD_RECOVERING):
        state = S.LifeState(
            cold_until=now + 3600, cold_stage=stage, cold_started_at=now - 60
        )
        texts[stage] = S.medicine_reply(state, now)
    assert len(set(texts.values())) == 3, texts


# ---------------------------------------------------------------- 病中三餐（§3）


def test_meal_is_adopted_while_sick_but_bath_is_not():
    """病中放行三餐（修「一场感冒三天不吃」），洗澡仍然不放行。"""

    async def run():
        _module, plugin, _host = _make_plugin(
            physio={
                "enabled": True,
                "meals": [
                    "00:00-23:59|正餐|meal|weight=1.0",
                    "00:00-23:59|洗澡|bath|weight=1.0",
                ],
            }
        )
        now = _make_sick(plugin, stage=A.COLD_WORSENING)
        plugin._state.satiety = 2.0
        plugin._state.meal_count_today = 0
        plugin._parsed_physio_windows, warnings = P.parse_meal_lines(
            plugin.config.physio.meals
        )
        assert not warnings, warnings

        fired, reason = await plugin._run_physio(now, plugin._sim_config())
        assert fired is True, reason
        assert plugin._state.activity == A.MEAL, "病重期三餐必须被放行（否则她三天不吃）"
        assert plugin._state.satiety > 2.0 and plugin._state.meal_count_today == 1
        # 场景文案跟着病程走（加重期「喝点粥」）
        assert "粥" in plugin._state.scene, plugin._state.scene

        # 同一 tick 的下一个窗口（洗澡）不会被采纳：病中不放行 bath
        plugin._state.activity = A.SICK_REST
        plugin._state.activity_since = now - 3600
        fired2, _reason2 = await plugin._run_physio(now + 60, plugin._sim_config())
        assert plugin._state.activity != A.BATH

    asyncio.run(run())


# ---------------------------------------------------------------- 边界输入（审计项 4 / 14）


def test_care_hit_bounds_pathological_text():
    """灾难性回溯的句式 + 超长正文不许挂住消息钩子（正文被截断到 500 字）。"""

    import time as _time

    # 灾难性回溯的句式 + 20 万字符正文：必须在截断后才匹配（否则钩子会被挂住）
    patterns, _warnings = S.parse_care_patterns([r"(a+)+$"])
    started = _time.monotonic()
    assert S.care_hit("a" * 20000 + "!", patterns) is True  # 截断后只剩 a，能匹配
    assert _time.monotonic() - started < 0.5, "超长正文必须在截断后才交给用户正则"

    # 关心词落在截断窗口内仍然生效（不是「一刀切失效」）
    hit, _ = S.parse_care_patterns(S.DEFAULT_CARE_PATTERNS)
    assert S.care_hit("你吃药了吗" + "啊" * 5000, hit) is True
    # 超出窗口的关心词不生效——这是截断的**已知代价**，写进用例免得日后被当成 bug
    assert S.care_hit("啊" * 600 + "你吃药了吗", hit) is False


def test_dirty_persisted_cold_fields_are_normalized():
    """坏状态文件不许让病程逻辑抛错（审计项 4：边界输入必须单独成组）。"""

    raw = {
        "cold_until": 5000.0,
        "cold_stage": "nonsense",
        "cold_started_at": -1.0,
        "cold_immunity_until": float("inf"),
        "convalescent_until": "abc",
        "cold_medicine_count": -3,
        "cold_medicine_seconds": float("nan"),
        "care_today": {"2026-02-08": "abc", "bad": 2.0},
        "medicine_cooldown": {"s1": 123.0, "s2": "abc"},
    }
    state = S.LifeState.from_dict(raw)
    assert state.cold_stage == ""  # 未知阶段被归一化成「没有阶段」
    assert state.cold_started_at == 0.0  # 负数时间戳按「不知道」处理
    assert state.cold_immunity_until == 0.0  # inf 被拦掉（否则免疫期永不结束）
    assert state.cold_medicine_count == 0
    assert state.cold_medicine_seconds == 0.0
    # 两张映射表都是「坏值整条丢弃」（不是转成 0）——与既有净化纪律一致
    assert state.care_today == {"bad": 2.0}
    assert state.medicine_cooldown == {"s1": 123.0}
    # 归一化后的状态照常跑：未知阶段 + 生病 ⇒ 按加重期处理
    assert S.cold_stage(state, 1000.0) == A.COLD_WORSENING
    assert S.health_label_admin(state, 1000.0, S.SimConfig())


def test_stage_helpers_survive_dirty_values():
    config = S.SimConfig()
    # NaN cold_until：不算生病（比较恒为假），但取数不许抛
    state = S.LifeState(cold_until=float("nan"), cold_stage=A.COLD_WORSENING)
    assert S.is_cold(state, 1000.0) is False
    assert S.cold_stage(state, 1000.0) == ""
    assert S.health_label_prompt(state, 1000.0, config)  # 不许抛
    assert S.health_label_admin(state, 1000.0, config)

    # 起点缺失时按 cold_until - cold_days 反推天数，不抛异常
    legacy = S.LifeState(cold_until=1000.0 + 2 * 86400.0, cold_days=2)
    assert S.cold_day_index(legacy, 1000.0, config) == 1


def test_permanent_cold_is_not_shrunk_by_medicine():
    """``cold_until=inf``（整活/夹具）不被送药改成有限值。"""

    now = time.time()
    state = S.LifeState(
        cold_until=float("inf"), cold_stage=A.COLD_WORSENING, cold_started_at=now - 60
    )
    ok, _reason, _text = S.take_medicine(
        state, now=now, session_id="s1", config=S.SimConfig()
    )
    assert ok is True and state.cold_until == float("inf")
    assert state.cold_medicine_seconds == 4 * 3600.0


def test_medicine_budget_is_respected_with_small_max_days():
    """累计缩短**永远不超过** ``cold_max_days/2`` 天的预算（小预算也要收口）。

    ⚠ 实测口径：默认「每场最多 2 次 × 每次 4 小时 = 8 小时」本来就小于
    ``cold_max_days=1`` 给出的 12 小时预算，所以**先撞到的是次数上限**；
    这条用例钉的是「两条闸都在、且都不会被越过」，以及预算确实参与了收口。
    """

    now = time.time()
    state = S.LifeState(
        cold_until=now + 3 * 86400.0, cold_stage=A.COLD_WORSENING, cold_started_at=now
    )
    config = S.SimConfig(cold_max_days=1, cold_min_days=1)
    budget = 0.5 * 86400.0
    for index in range(2):
        ok, _reason, _text = S.take_medicine(
            state, now=now + index * 60, session_id=f"s{index}", config=config
        )
        assert ok is True
        assert float(state.cold_medicine_seconds) <= budget + 1e-6
    spent = float(state.cold_medicine_seconds)
    assert spent == pytest.approx(8 * 3600.0)  # 2 次 × 4 小时
    assert state.cold_until == pytest.approx(now + 3 * 86400.0 - spent)


def test_sick_state_never_rolls_the_skip_meal_dice():
    """病中不掷「今天这顿不吃」：健康时照常偶尔不吃，病中三餐必出 proposal。

    真机 2026-10-09：状态卡「今日已吃 0 顿」而她在养病——`weight` 骰子掷到
    「不吃」后当天不再重掷，病中一顿不吃就是一天不吃。
    """

    async def run():
        meals = ["12:00-13:30|午餐|meal|weight=0.0"]  # 骰子必落空
        _module, plugin, _host = _make_plugin(physio={"enabled": True, "meals": meals})
        plugin._parsed_physio_windows, warnings = P.parse_meal_lines(meals)
        assert not warnings
        noon = time.time()  # 用「现在」当窗口内时刻：窗口是全天候的替身
        plugin._parsed_physio_windows = P.parse_meal_lines(
            ["00:00-23:59|午餐|meal|weight=0.0"]
        )[0]
        plugin._state.meal_count_today = 0
        plugin._state.satiety = 3.0

        # 健康：掷骰未过 ⇒ 这顿不吃
        plugin._state.cold_until = 0.0
        plugin._state.cold_stage = ""
        plugin._state.activity = A.DAILY
        plugin._state.activity_since = noon - 3600
        fired, reason = await plugin._run_physio(noon, plugin._sim_config())
        assert fired is False and "今天没吃" in reason, reason
        assert plugin._state.meal_count_today == 0

        # 生病：骰子被跳过 ⇒ 这顿照吃（且进账）
        plugin._physio_fired.clear()
        _make_sick(plugin, now=noon)
        plugin._state.activity_since = noon - 3600
        fired2, reason2 = await plugin._run_physio(noon, plugin._sim_config())
        assert fired2 is True, reason2
        assert plugin._state.activity == A.MEAL
        assert plugin._state.meal_count_today == 1

    asyncio.run(run())


def test_meal_window_is_not_consumed_by_a_blocked_attempt():
    """窗口内第一次提案被硬约束收口 ⇒ **不消耗当天的机会**，醒来/停留期满后能补吃。

    真机 2026-10-09 的机制性缺陷：以前提案前就 ``fired_today.add(key)``，
    于是「午餐第一次尝试时她正好睡着/刚换过活动」= 这一天再也吃不上午饭
    （状态卡实拍「今日已吃 0 顿、饱腹 3.4」）。
    """

    async def run():
        meals = ["00:00-23:59|午餐|meal|weight=1.0"]
        _module, plugin, _host = _make_plugin(physio={"enabled": True, "meals": meals})
        plugin._parsed_physio_windows = P.parse_meal_lines(meals)[0]
        now = time.time()
        plugin._state.satiety = 5.0
        plugin._state.meal_count_today = 0

        # 第一次：她刚睡下（未满最短睡眠）⇒ 睡眠分支收口，饭没吃成
        plugin._state.cold_until = 0.0
        plugin._state.cold_stage = ""
        plugin._state.activity = A.SLEEP
        plugin._state.activity_since = now - 600
        plugin._state.sleep_started_at = now - 600
        plugin._state.energy = 5.0
        fired, _reason = await plugin._run_physio(now, plugin._sim_config())
        assert fired is True
        assert plugin._state.activity == A.SLEEP
        assert plugin._state.meal_count_today == 0

        # 第二次（她醒了、窗口还没过）：必须还能吃上
        plugin._state.activity = A.SICK_REST
        plugin._state.activity_since = now - 3600
        _make_sick(plugin, now=now)
        fired2, _reason2 = await plugin._run_physio(now + 600, plugin._sim_config())
        assert fired2 is True
        assert plugin._state.activity == A.MEAL, "被收口一次之后当天就再也吃不上饭"
        assert plugin._state.meal_count_today == 1
        assert plugin._state.satiety > 5.0

        # 吃成之后本生活日不再重复提案（记账语义不变）
        plugin._state.activity = A.SICK_REST
        plugin._state.activity_since = now + 600 - 3600
        fired3, _reason3 = await plugin._run_physio(now + 1200, plugin._sim_config())
        assert fired3 is False, "同一窗口吃成之后不该再出 proposal"

    asyncio.run(run())


def test_empty_physio_windows_warn_instead_of_silent():
    """``[physio] meals`` 为空 ⇒ 必须告警 + 状态卡明说「未配置」。

    这条以前是完全静默的：饱腹照常下降、一餐都不触发，日志里一个字都没有
    （真机 2026-10-09 的排查盲区）。
    """

    async def run():
        _module, plugin, _host = _make_plugin(physio={"enabled": True, "meals": []})
        plugin._parsed_physio_windows = P.parse_meal_lines([])[0]
        logged: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: logged.append(record.getMessage() % record.args)
        logger = logging.getLogger(f"plugin.{_module.__plugin_id__}")
        logger.addHandler(handler)
        try:
            fired, reason = await plugin._run_physio(time.time(), plugin._sim_config())
        finally:
            logger.removeHandler(handler)
        assert (fired, reason) == (False, "")
        assert any("生理锚点已启用但没有任何可用时间窗" in item for item in logged), logged

        status = plugin._render_status(time.time())
        assert "生理窗：（未配置" in status, status

    asyncio.run(run())


def test_status_card_lists_configured_physio_windows():
    async def run():
        meals = ["07:00-08:30|早餐|meal|weight=0.9", "12:00-13:30|午餐|meal"]
        _module, plugin, _host = _make_plugin(physio={"enabled": True, "meals": meals})
        plugin._parsed_physio_windows = P.parse_meal_lines(meals)[0]
        status = plugin._render_status(time.time())
        assert "生理窗：2 个" in status and "早餐" in status and "午餐" in status, status

    asyncio.run(run())


# ---------------------------------------------------------------- 双口径（§7）


def test_prompt_hides_remaining_hours_but_status_shows_them():
    async def run():
        _module, plugin, _host = _make_plugin()
        now = _make_sick(plugin, hours_left=37.5, sick_for_hours=30.0)  # 病第二天

        digest = plugin._life_digest()
        assert "第 2 天" in digest
        assert "约剩" not in digest and "37.5" not in digest, digest

        status = plugin._render_status(now)
        assert "约剩" in status and "37.5" in status, status

        # `/生活 活动` 给管理员看病程与最近几条病程经历（§2.3：流转必须留痕，
        # 否则「她怎么突然好转了」在卡片上查不到原因）
        activity = plugin._render_activity(now)
        assert "病程：" in activity and "第 2 天" in activity, activity

    asyncio.run(run())


def test_replace_factor_mode_missing_keys_are_visible_in_frequency_card():
    """replace 模式漏键必须**看得见**：1.0 的因子在拆解里被过滤，卡片会静默漏报。

    真机 2026-10-09 配置实拍：`factors_mode="replace"` 的列表漏了
    `night_study` / `meal` / `bath` / `chatting`（v1.9.1 与 v1.11.1 新增的键），
    它们全部按 1.0 处理，`/生活 频率` 里一个字的痕迹都没有。
    """

    async def run():
        _module, plugin, _host = _make_plugin(
            activity={
                "factors_mode": "replace",
                "activity_factors": ["sleep=0.0", "daily=1.0"],  # 漏掉其余全部内置键
            }
        )
        card = await plugin._render_frequency(time.time(), "group-1")
        assert "replace 模式缺内置键" in card, card
        assert "night_study" in card and "meal" in card, card
        # 缺键清单也被记在实例上（`/生活 频率` 之外的地方也能查）
        assert "night_study" in plugin._activity_factor_absent

        # merge 模式下不缺键（会自动补齐），不该出现这行
        _m2, plugin2, _h2 = _make_plugin(activity={"factors_mode": "merge"})
        card2 = await plugin2._render_frequency(time.time(), "group-1")
        assert "replace 模式缺内置键" not in card2

    asyncio.run(run())


def test_readme_lists_the_medicine_command():
    text = README.read_text(encoding="utf-8")
    assert "/生活 送药" in text, "送药命令必须进 README 命令清单（它是唯一改变她身体状态的命令）"
    assert "唯一一个改变她身体状态" in text or "首个" in text or "改变她身体状态" in text
