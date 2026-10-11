# -*- coding: utf-8 -*-
"""L3：打断机制（interrupt）的白名单、窗口、回退与对拍。

钉住六件事：

1. **触发判据**（方案六 §6.3）：私聊任意 / 群聊被 @；命令与她自己的消息不打断；
   白名单外的活动不打断；低电量收窄白名单；
2. **窗口**：切 CHATTING、顺延不重复记录、``interrupt_until`` 净化；
3. **回退**：窗口结束回到原活动、原活动失效回 ``daily``、只回退一次；
4. **enforce 对拍**：窗口内任何提议都被收口回 CHATTING（``request_is_pointless``
   判「白问」必须与 enforce 逐字一致——穷举矩阵加「打断/非打断」一维）；
5. **回退豁免**：回到原活动不受最短停留期约束（否则永远卡在「聊天中」），
   但班表照常管着她；
6. 接线：note_session 真的会打断、窗口内 routine/LLM 都跳过、回退写 recent_events。
"""

import asyncio
import pathlib
import sys

import pytest

pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_activity as A  # noqa: E402
import life_interrupt as X  # noqa: E402
import life_sim as S  # noqa: E402
from fakehost import (  # noqa: E402
    FakeHost,
    FakePaths,
    bind_context,
    build_context,
    get_default_config,
    load_plugin_module,
)

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
NOW = 1_700_000_000.0


def _make_plugin(**overrides):
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_interrupt")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = 0
    config["physio"]["meals"] = []
    config["routines"]["lines"] = []
    config["interrupt"]["enabled"] = True
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    plugin._rebuild_from_config()
    return module, plugin, host


def _facts(**overrides):
    base = dict(
        activity=A.MEAL,
        minutes_in_activity=3,
        minutes_in_sleep=0,
        now_minutes=12 * 60 + 30,
        emotion=5.0,
        energy=6.0,
        energy_cap=10.0,
        sick=False,
        sleep_minutes_today=300,
        awake_minutes_today=300,
        in_interrupt=True,
    )
    base.update(overrides)
    return A.ActivityFacts(**base)


def _policy(**overrides):
    return A.EnforcePolicy(**overrides)


def _request(activity):
    return A.ActivityDecision(activity, "", A.SOURCE_LLM, "测试提议")


# ---------------------------------------------------------------- 纯模块


def test_should_interrupt_criteria():
    assert X.should_interrupt(
        enabled=True, activity="meal", is_command=False, is_bot_message=False,
        private_chat=True, mentioned=False,
    ), "私聊任意消息都打断"
    assert X.should_interrupt(
        enabled=True, activity="meal", is_command=False, is_bot_message=False,
        private_chat=False, mentioned=True,
    ), "群聊被 @ 打断"
    assert not X.should_interrupt(
        enabled=True, activity="meal", is_command=True, is_bot_message=False,
        private_chat=True, mentioned=False,
    ), "命令不打断"
    assert not X.should_interrupt(
        enabled=True, activity="meal", is_command=False, is_bot_message=True,
        private_chat=True, mentioned=False,
    ), "她自己的消息不打断"
    assert not X.should_interrupt(
        enabled=True, activity="meal", is_command=False, is_bot_message=False,
        private_chat=False, mentioned=False,
    ), "群聊围观消息不打断"
    assert not X.should_interrupt(
        enabled=False, activity="meal", is_command=False, is_bot_message=False,
        private_chat=True, mentioned=False,
    ), "开关关闭不打断"


def test_whitelist_is_exactly_plan_six():
    # 方案 §6.3 的可打断表：⚠ bath 有意不在其中（洗澡不回消息）
    # v1.15.0（PR-R1）：nap 加进来——小睡浅、随时能醒，回完消息还能倒回去接着眯
    assert X.INTERRUPTIBLE_ACTIVITIES == frozenset({
        "meal", "daze", "anime", "game", "music", "daily", "night_study",
        "off_work", "commute", "before_sleep", "nap",
    })
    assert "sleep" not in X.INTERRUPTIBLE_ACTIVITIES
    assert "sick_rest" not in X.INTERRUPTIBLE_ACTIVITIES
    assert "work" not in X.INTERRUPTIBLE_ACTIVITIES
    assert not X.should_interrupt(
        enabled=True, activity="sleep", is_command=False, is_bot_message=False,
        private_chat=True, mentioned=False,
    ), "睡觉不打断（有专属 wake_on_at 窗口）"
    assert not X.should_interrupt(
        enabled=True, activity="work", is_command=False, is_bot_message=False,
        private_chat=True, mentioned=False,
    ), "在岗不打断"


def test_narrow_whitelist_on_low_battery():
    # 方案七 §7.3：battery < 2.0 → 白名单收窄，只剩随时能放下手的轻活
    full = X.interruptible_activities(battery=5.0)
    assert full == X.INTERRUPTIBLE_ACTIVITIES
    narrow = X.interruptible_activities(battery=1.5)
    assert narrow == X.NARROW_INTERRUPTIBLE_ACTIVITIES
    assert "meal" not in narrow and "commute" not in narrow
    # battery 未知（mood 未启用）→ 完整白名单，绝不静默失效
    assert X.interruptible_activities(battery=None) == X.INTERRUPTIBLE_ACTIVITIES
    assert X.interruptible_activities(battery=float("nan")) == X.INTERRUPTIBLE_ACTIVITIES
    assert not X.should_interrupt(
        enabled=True, activity="meal", is_command=False, is_bot_message=False,
        private_chat=False, mentioned=True, battery=1.0,
    ), "低电量时群聊吃饭窗不打断"
    assert X.should_interrupt(
        enabled=True, activity="daze", is_command=False, is_bot_message=False,
        private_chat=False, mentioned=True, battery=1.0,
    ), "低电量时发呆仍可打断"


def test_apply_interrupt_switches_and_extends():
    state = S.new_state(now=NOW, config=S.SimConfig())
    state.activity = "meal"
    X.apply_interrupt(state, now=NOW, window_minutes=5)
    assert state.activity == X.CHATTING
    assert state.activity_source == A.SOURCE_INTERRUPT
    assert state.interrupted_from == "meal"
    assert state.interrupt_until == pytest.approx(NOW + 300.0)

    # 窗口内再收到消息 → 只顺延，不重复记录
    X.apply_interrupt(state, now=NOW + 120, window_minutes=5)
    assert state.interrupted_from == "meal", "连续聊天不换『被打断前在干什么』"
    assert state.interrupt_until == pytest.approx(NOW + 120 + 300.0), "只顺延"


def test_in_window_and_sanitization():
    state = S.new_state(now=NOW, config=S.SimConfig())
    assert not X.in_interrupt_window(state, now=NOW), "没开过窗"
    X.apply_interrupt(state, now=NOW, window_minutes=5)
    assert X.in_interrupt_window(state, now=NOW + 60)
    assert not X.in_interrupt_window(state, now=NOW + 301)

    # from_dict 净化：NaN / 负数时间戳 → 没有窗口；非法活动名 → 清空
    dirty = S.LifeState.from_dict({
        "interrupt_until": float("nan"),
        "interrupted_from": "mealx",
    })
    assert dirty.interrupt_until == 0.0
    assert dirty.interrupted_from == ""


def test_expire_returns_to_previous_activity_once():
    state = S.new_state(now=NOW, config=S.SimConfig())
    state.activity = "meal"
    X.apply_interrupt(state, now=NOW, window_minutes=5)

    # 窗口内：不回退
    assert X.expire_interrupt(state, now=NOW + 60) is None
    # 刚过窗：回退一次
    decision = X.expire_interrupt(state, now=NOW + 301)
    assert decision is not None and decision.activity == "meal"
    assert decision.source == A.SOURCE_ENFORCED
    assert state.interrupt_until == 0.0 and state.interrupted_from == ""
    # 第二次：不再回退（没有窗口了）
    assert X.expire_interrupt(state, now=NOW + 400) is None

    # 原活动已失效（状态损坏）→ 回 daily 而不是卡死
    state2 = S.new_state(now=NOW, config=S.SimConfig())
    state2.activity = "meal"
    X.apply_interrupt(state2, now=NOW, window_minutes=5)
    state2.interrupted_from = "mealx"  # 模拟写入未净化的坏值
    decision2 = X.expire_interrupt(state2, now=NOW + 301)
    assert decision2 is not None and decision2.activity == A.DAILY


def test_expire_note_and_context_fact():
    note = X.expire_note("meal", A.ACTIVITY_LABELS.get)
    assert "吃饭" in note and "回" in note
    fact = X.context_fact("meal", A.ACTIVITY_LABELS.get)
    assert "吃饭" in fact and "放下手里的事" in fact


# ---------------------------------------------------------------- enforce 对拍


def test_enforce_holds_chatting_for_any_request_in_window():
    """窗口内 enforce 必须把**任何**提议都收口回 CHATTING。

    这是 ``request_is_pointless`` 判「打断窗口中 = 白问」的前提：只有
    「无论答什么都留在 CHATTING」才配叫白问。
    """

    facts = _facts(activity=X.CHATTING, in_interrupt=True)
    policy = _policy()
    baseline = A.enforce(facts, None, policy)
    assert baseline.activity == X.CHATTING
    for activity in A.ALLOWED_ACTIVITIES:
        out = A.enforce(facts, _request(activity), policy)
        assert out.activity == X.CHATTING, f"提议 {activity} 竟然切走了"


def test_pointless_matrix_with_interrupt_dimension():
    """穷举对拍矩阵加一维（打断/非打断）。

    凡是被 ``request_is_pointless`` 判成白问的事实，提议任何活动得到的裁定
    必须与「没提议」一致——现在带着 ``in_interrupt`` 再验一遍。
    """

    skip_cases = [
        ("打断窗口内（正在回消息）", _facts(activity=X.CHATTING, in_interrupt=True)),
        (
            "打断窗口内且未满停留期",
            _facts(activity=X.CHATTING, in_interrupt=True, minutes_in_activity=1),
        ),
    ]
    for label, facts in skip_cases:
        reason = A.request_is_pointless(facts, _policy())
        assert reason, f"{label}：应当被判为白问"
        baseline = A.enforce(facts, None, _policy())
        for activity in A.ALLOWED_ACTIVITIES:
            out = A.enforce(facts, _request(activity), _policy())
            assert out.activity == baseline.activity, (
                f"{label}：提议 {activity} 得到 {out.activity}，没提议是 {baseline.activity}"
            )

    # 反例：窗口外同样的事实不该被判白问
    assert A.request_is_pointless(
        _facts(activity=X.CHATTING, in_interrupt=False, minutes_in_activity=120),
        _policy(),
    ) == "", "窗口外且已过停留期，问一次有意义"


def test_enforce_does_not_hold_chatting_outside_window():
    facts = _facts(activity=X.CHATTING, in_interrupt=False, minutes_in_activity=120)
    out = A.enforce(facts, _request(A.GAME), _policy())
    assert out.activity == A.GAME, "窗口外提议别的活动应该被放行"


def test_expire_return_is_exempt_from_dwell_but_not_schedule():
    """回退豁免最短停留期，但不豁免班表。"""

    # 豁免：CHATTING 只停留 1 分钟，回到 meal 也应放行
    facts = _facts(
        activity=X.CHATTING, in_interrupt=False, minutes_in_activity=1,
        interrupt_return_to="meal",
    )
    out = A.enforce(facts, _request("meal"), _policy())
    assert out.activity == "meal", "回退必须豁免最短停留期"

    # 同样的事实但 requested 不是回退目标 → 照常被停留期挡住
    out2 = A.enforce(facts, _request(A.GAME), _policy())
    assert out2.activity == X.CHATTING, "非回退目标仍受停留期约束"

    # 班表不豁免：工作相位的回退（原活动是 game，白名单成员）被班表拦下——
    # 「work 相位限制表」含 GAME（在岗不能打游戏），回退到 game 必须被拦住
    from life_activity import ScheduleFacts, ScheduleConfig

    facts3 = _facts(
        activity=X.CHATTING, in_interrupt=False, minutes_in_activity=1,
        interrupt_return_to="game",
        schedule=ScheduleFacts(
            enabled=True, is_workday=True, phase="work",
        ),
    )
    policy3 = _policy(schedule=ScheduleConfig(enabled=True))
    out3 = A.enforce(facts3, _request("game"), policy3)
    assert out3.activity != "game", "在岗时间回退不能回到打游戏"


def test_energy_delta_and_factor_registered():
    assert S._default_energy_delta().get("chatting") == pytest.approx(0.05)
    assert "chatting" not in A.PromptInput().activity_choices(), "模型不该被提供 CHATTING 候选"


# ---------------------------------------------------------------- 接线


def test_note_session_interrupts_private_message():
    async def run():
        module, plugin, host = _make_plugin()
        plugin._state.activity = "meal"
        plugin._state.activity_since = NOW - 60
        await plugin.note_session({
            "session_id": "p1", "user_id": "10003",
            "processed_plain_text": "在吗", "time": NOW,
        })
        assert plugin._state.activity == X.CHATTING
        assert plugin._state.interrupted_from == "meal"
        assert plugin._state.interrupt_until > NOW

    asyncio.run(run())


def test_note_session_group_requires_at():
    async def run():
        module, plugin, host = _make_plugin()
        plugin._state.activity = "meal"
        # 群聊围观消息：不打断
        await plugin.note_session({
            "session_id": "g1", "group_id": "777", "user_id": "10002",
            "processed_plain_text": "哈哈哈", "time": NOW,
        })
        assert plugin._state.activity == "meal"
        # 被 @：打断
        await plugin.note_session({
            "session_id": "g1", "group_id": "777", "user_id": "10002",
            "is_at": True, "processed_plain_text": "@她 来玩", "time": NOW,
        })
        assert plugin._state.activity == X.CHATTING

    asyncio.run(run())


def test_interrupt_disabled_is_inert():
    async def run():
        module, plugin, host = _make_plugin(interrupt={"enabled": False})
        plugin._state.activity = "meal"
        await plugin.note_session({
            "session_id": "p1", "user_id": "1",
            "processed_plain_text": "在吗", "time": NOW,
        })
        assert plugin._state.activity == "meal"
        assert plugin._interrupt_skip_reason(NOW) == ""

    asyncio.run(run())


def test_context_fact_injected_once_per_interrupt_batch():
    async def run():
        module, plugin, host = _make_plugin()
        calls: list[tuple] = []

        async def fake_append(session_id, items, **kwargs):
            calls.append((session_id, items))

        plugin.ctx.maisaka.context.append = fake_append
        plugin._state.activity = "meal"

        # 第一次私聊：打断 + 注入
        await plugin.note_session({
            "session_id": "p1", "user_id": "1",
            "processed_plain_text": "嗨", "time": NOW,
        })
        assert len(calls) == 1, "第一次打断注入一条"
        assert "吃饭" in calls[0][1][0]["text"]
        # 窗口内连续消息（原活动仍是 meal 记录在案）：不再注入
        plugin._state.activity = X.CHATTING
        await plugin.note_session({
            "session_id": "p1", "user_id": "1",
            "processed_plain_text": "在听吗", "time": NOW + 10,
        })
        assert len(calls) == 1, "同一次打断只注入一次"

    asyncio.run(run())


def test_tick_returns_to_previous_activity_after_window(monkeypatch=None):
    async def run():
        module, plugin, host = _make_plugin()
        plugin._state.activity = "meal"
        plugin._state.activity_since = NOW - 3600  # 吃饭早就过了停留期
        await plugin.note_session({
            "session_id": "p1", "user_id": "1",
            "processed_plain_text": "嗨", "time": NOW,
        })
        assert plugin._state.activity == X.CHATTING

        # 把时钟推过窗口（note_session 用的是真实墙钟，从状态里取实际截止时间），
        # 手动走一次回退
        assert plugin._state.interrupt_until > 0
        plugin._expire_interrupt(plugin._state.interrupt_until + 100)
        assert plugin._state.activity == "meal", "回退到原活动（停留期豁免）"
        notes = [e.get("text", "") for e in plugin._state.recent_events]
        assert any("回" in n for n in notes), "回退事件落 recent_events"
        # 再跑一次：不重复回退
        assert plugin._expire_interrupt(plugin._state.interrupt_until + 200) is False

    asyncio.run(run())


def test_tick_skips_llm_and_routine_in_window():
    async def run():
        module, plugin, host = _make_plugin(
            routines={"lines": ["00:00-23:59|测试场景|game|weight=1.0"]},
        )
        plugin._rebuild_from_config()
        plugin._state.activity = "meal"
        X.apply_interrupt(plugin._state, now=NOW, window_minutes=5)
        reason = plugin._interrupt_skip_reason(NOW + 60)
        assert reason, "窗口内不问模型"
        assert "打断" in reason
        # routine 跑一次也不应命中（窗口内不跑）——直接看 tick 分支条件
        from fakehost import get_default_config as _g  # noqa: F401
        assert X.in_interrupt_window(plugin._state, now=NOW + 60)

    asyncio.run(run())
