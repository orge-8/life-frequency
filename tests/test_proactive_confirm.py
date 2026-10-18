# -*- coding: utf-8 -*-
"""L3：主动开口的延迟核销（v1.18.0，借 mohobot ``proactive_chat`` 的「发送成功才计数」）。

真机事实（MaiBot 1.3.2，``src/maisaka/runtime.py::enqueue_proactive_task``）：
宿主对 ``maisaka.proactive.trigger`` 只回 ``{"stream_id", "task_id", "queued": True}``
——**受理回执，不含「她是否真的说了话」**，Planner 仍有权沉默。而插件的
``record_proactive`` 原先是「受理即计数」：``daily_max=1`` 时，Planner 沉默一次就白吃掉
当天配额，``unanswered_streak`` 也虚涨，接着触发退避。

修法：受理后挂 ``pending_verify``，到点用 ``message.get_by_time_in_chat`` 查窗口内她有没有
说话——说了才倒签计数，没说就只记一次失败尝试（进冷却）。**查询失败按「说了」计**：
绝不能让「查不了」变成「配额失效、随便发」。

本文件钉住：核销成功 / 失败 / 查不了三条出口、倒签语义、每轮配额、以及净化后
``pending_verify`` 不会带着半个坏 dict 进消费点。
"""

import asyncio
import pathlib
import sys
import time

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
TRIGGER = "maisaka.proactive.trigger"
HISTORY = "message.get_by_time_in_chat"
SESSION = "group-1"
DAY = "2026-10-11"
BOT_QQ = "999"


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_proactive_confirm")


def _make_plugin(**config_overrides):
    module = _load()
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["proactive"]["enabled"] = True
    config["proactive"]["quiet_hours"] = []
    for section, values in config_overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    # 直接给上身份缓存：省掉 config.get 的间接层，让「谁是她」在用例里是显式的
    plugin._bot_qq_ids = {BOT_QQ}
    return module, plugin, host


def _patch_sessions(plugin, *session_ids):
    async def fake_list():
        return [(sid, {"session_id": sid, "group_id": "123456"}) for sid in session_ids]

    plugin._list_sessions = fake_list


def _material(now: float) -> dict:
    return {"label": "小事", "text": "刚泡了杯茶", "weight": 0.9, "created_at": now,
            "expires_at": now + 3600.0}


def _park(plugin, *, trigger_at: float, verify_after: float, count: int = 0) -> dict:
    """在会话上挂一个待核实的开口。"""

    record = {
        "stream_id": SESSION,
        "last_user_message_at": trigger_at - 600.0,
        "last_proactive_at": 0.0,
        "day_key": DAY,
        "count": count,
        "unanswered_streak": 0,
        "pending_verify": {"trigger_at": trigger_at, "verify_after": verify_after, "day_key": DAY},
    }
    plugin._state.sessions[SESSION] = record
    return record


def _her_message(stamp: float) -> dict:
    """她发的消息：真机序列化把时间戳转成**字符串**，这里就用字符串。"""

    return {
        "timestamp": str(stamp),
        "message_info": {"user_info": {"user_id": BOT_QQ, "user_nickname": "她"}},
    }


def _user_message(stamp: float) -> dict:
    return {
        "timestamp": stamp,
        "message_info": {"user_info": {"user_id": "111", "user_nickname": "用户"}},
    }


# ------------------------------------------------- 1. 三条核销出口


def test_confirmed_send_counts_and_backdates_the_anchor():
    _module, plugin, host = _make_plugin()
    now = time.time()
    trigger_at = now - 20 * 60.0
    _park(plugin, trigger_at=trigger_at, verify_after=now - 1.0)
    host.returns[HISTORY] = [_her_message(trigger_at + 30.0)]

    asyncio.run(plugin._verify_pending_proactive(now))

    record = plugin._state.sessions[SESSION]
    assert record["count"] == 1, "核实说了话才计数"
    assert record["last_proactive_at"] == pytest.approx(trigger_at), "必须倒签到开口那一刻"
    assert "pending_verify" not in record
    assert plugin._state.skip_ledger.get("sent", 0) >= 1
    # 查询参数：核实必须包含她自己（filter_mai=False），并被限定在开口之后的窗口
    call = host.calls_of(HISTORY)[0]
    assert call.get("filter_mai") is False
    assert call.get("start_time") == str(trigger_at)
    assert call.get("limit_mode") == "latest"


def test_planner_silence_does_not_consume_the_quota():
    _module, plugin, host = _make_plugin()
    now = time.time()
    trigger_at = now - 20 * 60.0
    _park(plugin, trigger_at=trigger_at, verify_after=now - 1.0)
    # 窗口内只有用户说话，她一个字都没说
    host.returns[HISTORY] = [_user_message(trigger_at + 30.0)]

    asyncio.run(plugin._verify_pending_proactive(now))

    record = plugin._state.sessions[SESSION]
    assert record["count"] == 0, "没说出来就不该消耗当天配额"
    assert record["last_proactive_at"] == 0.0, "没说出来就不该重开间隔闸"
    assert record["unanswered_streak"] == 0, "没说出来就不该记「对方不理她」"
    assert record["last_attempt_at"] == pytest.approx(now), "要进失败冷却"
    assert "pending_verify" not in record
    assert plugin._state.skip_ledger.get("not_sent", 0) >= 1


def test_messages_older_than_the_trigger_do_not_count():
    """窗口外的旧消息不能拿来充数（``start_time`` 是开口时刻）。"""

    _module, plugin, host = _make_plugin()
    now = time.time()
    trigger_at = now - 20 * 60.0
    _park(plugin, trigger_at=trigger_at, verify_after=now - 1.0)
    host.returns[HISTORY] = [_her_message(trigger_at - 3600.0)]

    asyncio.run(plugin._verify_pending_proactive(now))

    assert plugin._state.sessions[SESSION]["count"] == 0
    assert plugin._state.skip_ledger.get("not_sent", 0) >= 1


def test_query_failure_counts_as_sent_and_opens_the_breaker():
    """查不了 ⇒ 退回旧语义（按说了算）。绝不能让能力缺失变成配额失效。"""

    _module, plugin, host = _make_plugin()
    now = time.time()
    trigger_at = now - 20 * 60.0
    _park(plugin, trigger_at=trigger_at, verify_after=now - 1.0)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("RPC 炸了")

    plugin.ctx.message.get_by_time_in_chat = boom

    asyncio.run(plugin._verify_pending_proactive(now))

    record = plugin._state.sessions[SESSION]
    assert record["count"] == 1, "查不了就按「说了」计，配额照常消耗"
    assert record["last_proactive_at"] == pytest.approx(trigger_at)
    assert plugin._history_probe_failures == 1
    assert "pending_verify" not in record


def test_host_reported_failure_counts_as_sent():
    _module, plugin, host = _make_plugin()
    now = time.time()
    trigger_at = now - 20 * 60.0
    _park(plugin, trigger_at=trigger_at, verify_after=now - 1.0)
    host.returns[HISTORY] = {"success": False, "error": "宿主没有这个能力"}

    asyncio.run(plugin._verify_pending_proactive(now))

    assert plugin._state.sessions[SESSION]["count"] == 1
    assert plugin._history_probe_failures == 1


def test_unknown_identity_counts_as_sent_and_warns():
    """连她是谁都判不出来时，不能拿「没找到她的消息」当「她没说话」。"""

    _module, plugin, host = _make_plugin()
    now = time.time()
    trigger_at = now - 20 * 60.0
    _park(plugin, trigger_at=trigger_at, verify_after=now - 1.0)
    plugin._bot_qq_ids = set()
    plugin._bot_name = ""
    host.returns[HISTORY] = []

    asyncio.run(plugin._verify_pending_proactive(now))

    assert plugin._state.sessions[SESSION]["count"] == 1
    assert not host.calls_of(HISTORY), "身份都判不出来就不该白打一次 RPC"


# ------------------------------------------------- 2. 配额与待核实闸


def test_not_yet_due_is_left_alone():
    _module, plugin, host = _make_plugin()
    now = time.time()
    _park(plugin, trigger_at=now - 60.0, verify_after=now + 600.0)

    asyncio.run(plugin._verify_pending_proactive(now))

    assert plugin._state.sessions[SESSION]["pending_verify"]["verify_after"] > now
    assert not host.calls_of(HISTORY)


def test_a_deferred_verification_still_blocks_a_second_opening():
    """本轮核销配额不够而**被推迟**的待核实，也必须继续拦住这个会话。

    否则「受理 ≠ 发送」的空窗会连开两枪：第一枪还没核实，第二枪又出去了。
    """

    _module, plugin, host = _make_plugin()
    now = time.time()
    plugin._state.materials = [_material(now)]
    # 三个同时到期的待核实，每轮只核销 2 个 ⇒ 第三个被推迟
    for index in range(3):
        session_id = f"group-{index}"
        plugin._state.sessions[session_id] = {
            "stream_id": session_id,
            "last_user_message_at": now - 10 * 3600.0,
            "last_proactive_at": 0.0,
            "day_key": DAY,
            "count": 0,
            "unanswered_streak": 0,
            "pending_verify": {"trigger_at": now - 600.0, "verify_after": now - 1.0, "day_key": DAY},
        }
    host.returns[HISTORY] = [_her_message(now - 300.0)]
    _patch_sessions(plugin, *[f"group-{index}" for index in range(3)])

    asyncio.run(plugin._maybe_proactive(now))

    assert len(host.calls_of(HISTORY)) == _module._PENDING_VERIFY_PER_TICK
    assert not host.calls_of(TRIGGER), "还有没核销的开口挂着，不该再开新的一枪"


def test_verification_is_capped_per_tick():
    _module, plugin, host = _make_plugin()
    now = time.time()
    for index in range(5):
        session_id = f"group-{index}"
        plugin._state.sessions[session_id] = {
            "stream_id": session_id,
            "last_user_message_at": now - 600.0,
            "last_proactive_at": 0.0,
            "day_key": DAY,
            "count": 0,
            "pending_verify": {"trigger_at": now - 600.0, "verify_after": now - 1.0, "day_key": DAY},
        }
    host.returns[HISTORY] = [_her_message(now - 300.0)]

    asyncio.run(plugin._verify_pending_proactive(now))

    assert len(host.calls_of(HISTORY)) == _module._PENDING_VERIFY_PER_TICK


def test_confirmation_off_means_no_verification_rpc():
    _module, plugin, host = _make_plugin(proactive={"confirm_send": False})
    now = time.time()
    _park(plugin, trigger_at=now - 600.0, verify_after=now - 1.0)

    asyncio.run(plugin._verify_pending_proactive(now))

    assert not host.calls_of(HISTORY), "关掉核实就不该再打 RPC"


def test_pending_window_blocks_a_second_opening_in_the_same_session():
    _module, plugin, host = _make_plugin()
    now = time.time()
    plugin._state.materials = [_material(now)]
    _park(plugin, trigger_at=now - 60.0, verify_after=now + 600.0)
    _patch_sessions(plugin, SESSION)

    asyncio.run(plugin._maybe_proactive(now))

    assert not host.calls_of(TRIGGER), "待核实期间不该叠加第二次开口"
    assert plugin._state.skip_ledger.get("pending", 0) >= 1


def test_a_pending_session_does_not_block_other_sessions():
    """待核实是**每会话**的闸，不该让整个插件陪等。"""

    _module, plugin, host = _make_plugin()
    now = time.time()
    plugin._state.materials = [_material(now)]
    _park(plugin, trigger_at=now - 60.0, verify_after=now + 600.0)
    plugin._state.sessions["group-2"] = {
        "stream_id": "group-2",
        "last_user_message_at": now - 10 * 3600.0,
        "last_proactive_at": 0.0,
        "day_key": DAY,
        "count": 0,
        "unanswered_streak": 0,
    }
    _patch_sessions(plugin, SESSION, "group-2")

    asyncio.run(plugin._maybe_proactive(now))

    assert host.calls_of(TRIGGER), "另一个会话该照常开口"


# ------------------------------------------------- 3. 语义细节


def test_verified_send_spends_social_battery_but_silence_does_not():
    """耗电记在「真的开口」那一刻：没说出来就不耗电。"""

    _module, plugin, host = _make_plugin()
    now = time.time()
    trigger_at = now - 20 * 60.0
    _park(plugin, trigger_at=trigger_at, verify_after=now - 1.0)
    before = float(plugin._state.social_battery)
    host.returns[HISTORY] = [_her_message(trigger_at + 30.0)]
    asyncio.run(plugin._verify_pending_proactive(now))
    after_sent = float(plugin._state.social_battery)
    assert after_sent < before, "说出去了就该耗电"

    plugin._state.sessions[SESSION]["pending_verify"] = {
        "trigger_at": now, "verify_after": now - 1.0, "day_key": DAY,
    }
    host.returns[HISTORY] = [_user_message(now)]
    asyncio.run(plugin._verify_pending_proactive(now))
    assert float(plugin._state.social_battery) == pytest.approx(after_sent), "没说出口不该耗电"


def test_confirmed_send_towards_midnight_counts_on_the_opening_day():
    """核销可能跨日：配额必须算在**开口那天**，而不是核实那天。"""

    _module, plugin, host = _make_plugin()
    now = time.time()
    _park(plugin, trigger_at=now - 3600.0, verify_after=now - 1.0)
    plugin._state.sessions[SESSION]["day_key"] = "2026-10-10"
    plugin._state.sessions[SESSION]["pending_verify"]["day_key"] = "2026-10-10"
    host.returns[HISTORY] = [_her_message(now - 1800.0)]

    asyncio.run(plugin._verify_pending_proactive(now))

    record = plugin._state.sessions[SESSION]
    assert record["day_key"] == "2026-10-10"
    assert record["count"] == 1


def test_session_sanitize_keeps_the_new_fields_and_drops_a_broken_pending():
    import life_sim as S

    cleaned = S._sanitize_session_map({
        "a": {"last_attempt_at": float("nan"),
              "pending_verify": {"trigger_at": 1.0, "verify_after": 2.0, "day_key": "d"}},
        "b": {"pending_verify": {"trigger_at": "坏", "verify_after": 2.0}},
        "c": {"pending_verify": "不是字典"},
        "d": {"last_attempt_at": 5.0},
    })
    assert cleaned["a"]["last_attempt_at"] == 0.0
    assert cleaned["a"]["pending_verify"]["verify_after"] == 2.0
    assert "pending_verify" not in cleaned["b"], "非法 trigger_at 要整键丢弃"
    assert "pending_verify" not in cleaned["c"], "非 dict 要整键丢弃"
    assert cleaned["d"]["last_attempt_at"] == 5.0
