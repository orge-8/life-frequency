# -*- coding: utf-8 -*-
"""L3：v1.13.1 上线前全检修复的回归钉子。

每条对应 GStack 全检（2026-10-09）发现的一个 P0/P1，修复前全部红、修复后全绿：

- F-001：新人事件文本不得含 QQ 号/群号明文（隐私，端到端到 prompt）；
- R1：``_noon_epoch`` 必须扣回时区偏移（默认 UTC+8 下梦素材触底平移 8h）；
- R6：窗口外 ``enforce`` 必须拒绝 chatting 提议（模型不可凭空造系统态）；
- R4：打断顺延有总时长上限（活跃群每 5 分钟一条 @ 不得永久 chatting）；
- R5：强制层切到独占型活动时清空背景活动；
- F-008/F-009：user_id / relation_hint 入库截断 64；
- F-004：打断注入去重键含批次（同活动第二次打断仍会注入）。
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
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_prelaunch_fixes")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = 0
    config["physio"]["meals"] = []
    config["routines"]["lines"] = []
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    plugin._rebuild_from_config()
    return module, plugin, host


def test_f001_newcomer_text_masks_ids():
    """F-001：新人事件文本不携带 QQ 号/群号明文（全检安全审计 P0）。"""

    import life_world as W

    # 真实契约：{"schema_version":1, "groups": {群号: {"new_members": [...]}}}
    source = W.parse_recent_newcomers(
        {
            "schema_version": 1,
            "groups": {
                "876543210": {
                    "new_members": [
                        {"user_id": "123456789", "first_seen": NOW},
                    ],
                },
            },
        },
        fetched_at=NOW,
    )
    assert source.ok and source.items, "契约解析正常"
    events = W.world_events(newcomers=source, now=NOW)
    assert events, "世界事件正常生成"
    texts = " ".join(item.text for item in events)
    assert "123456789" not in texts, "QQ 号明文不得出现在事件文本里"
    assert "876543210" not in texts, "群号明文不得出现在事件文本里"
    assert "123***89" in texts, "脱敏标识保留（前3后2）"


def test_r1_noon_epoch_subtracts_tz_offset():
    """R1：_noon_epoch 扣回时区偏移（全检代码审查 P0，真机 UTC+8 偏 8h）。"""

    async def run():
        from datetime import datetime, timedelta, timezone

        module, plugin, host = _make_plugin(
            simulation={"tz_offset_minutes": 480}
        )
        TZ = timezone(timedelta(hours=8))
        # 本地 09:00 → 素材应活到本地 12:00
        now = datetime(2026, 10, 9, 9, 0, tzinfo=TZ).timestamp()
        expires = plugin._noon_epoch(now)
        local_expires = datetime.fromtimestamp(expires, tz=TZ)
        assert (local_expires.hour, local_expires.minute) == (12, 0), (
            f"触底时刻应为本地 12:00，实际 {local_expires:%H:%M}"
        )
        # 本地 15:00 / 19:00 醒来 → 已过触底点，不产素材
        for hour in (15, 19):
            t = datetime(2026, 10, 9, hour, 0, tzinfo=TZ).timestamp()
            assert plugin._noon_epoch(t) <= t, f"本地 {hour}:00 醒来不应产梦素材"
        # 本地 08:00 醒来 → 仍产素材
        t8 = datetime(2026, 10, 9, 8, 0, tzinfo=TZ).timestamp()
        assert plugin._noon_epoch(t8) > t8

    asyncio.run(run())


def test_r6_enforce_rejects_chatting_outside_window():
    """R6：窗口外 enforce 拒绝 chatting 提议（模型不可凭空造系统态）。"""

    decision = A.ActivityDecision("chatting", "", A.SOURCE_LLM, "模型提议聊天")
    facts = A.ActivityFacts(
        activity="meal", minutes_in_activity=120, now_minutes=12 * 60 + 30,
        energy=6.0, in_interrupt=False,
    )
    out = A.enforce(facts, decision, A.EnforcePolicy())
    assert out.activity == "meal", "窗口外 chatting 提议必须退回保持当前活动"

    # 窗口内仍是合法（打断机制本身），且附带 in_interrupt 的对拍不回归
    facts2 = A.ActivityFacts(
        activity="meal", minutes_in_activity=120, now_minutes=12 * 60 + 30,
        energy=6.0, in_interrupt=True,
    )
    out2 = A.enforce(facts2, decision, A.EnforcePolicy())
    assert out2.activity == A.CHATTING


def test_r4_interrupt_extension_capped():
    """R4：打断顺延有总时长上限（活跃群不会永久 chatting）。"""

    state = S.new_state(now=NOW, config=S.SimConfig())
    state.activity = "meal"
    # 每 4 分钟一条消息 × 20 次（覆盖 80 分钟）——远超 30 分钟上限
    for i in range(20):
        X.apply_interrupt(state, now=NOW + i * 240, window_minutes=5)
    total = (state.interrupt_until - NOW) / 60.0
    assert total <= X.MAX_TOTAL_MINUTES + 5.0, (
        f"顺延上限失效：10 次顺延后窗口覆盖 {total:.0f} 分钟（上限 {X.MAX_TOTAL_MINUTES}）"
    )
    # 上限内正常顺延不受影响
    state2 = S.new_state(now=NOW, config=S.SimConfig())
    state2.activity = "meal"
    X.apply_interrupt(state2, now=NOW, window_minutes=5)
    X.apply_interrupt(state2, now=NOW + 120, window_minutes=5)  # 2 分钟后顺延
    assert state2.interrupt_until == pytest.approx(NOW + 120 + 300.0)
    # 过期回退后起点被清，下一次打断重新开始计上限
    X.expire_interrupt(state2, now=NOW + 500)
    assert state2.interrupt_started_at == 0.0
    X.apply_interrupt(state2, now=NOW + 600, window_minutes=5)
    assert state2.interrupt_started_at == pytest.approx(NOW + 600)


def test_r5_exclusive_activity_clears_side():
    """R5：强制层切到独占型活动时清空背景活动。"""

    state = S.new_state(now=NOW, config=S.SimConfig())
    state.side_activities = ["anime"]
    enforced_sleep = A.ActivityDecision(A.SLEEP, "", A.SOURCE_ENFORCED, "硬约束送她入睡")
    S.apply_activity(state, enforced_sleep, now=NOW, config=S.SimConfig())
    assert state.activity == A.SLEEP
    assert state.side_activities == [], "睡觉不存在「顺便看番」"

    # 养病同理
    state.side_activities = ["music"]
    S.apply_activity(
        state, A.ActivityDecision(A.SICK_REST, "", A.SOURCE_ENFORCED, "感冒"),
        now=NOW + 60, config=S.SimConfig(),
    )
    assert state.side_activities == []


def test_f008_store_truncates_user_id_and_hint():
    """F-008/F-009：user_id 与 relation_hint 入库截断 64。"""

    from life_store import LifeStore, new_relationship
    import tempfile, os

    store = LifeStore(os.path.join(tempfile.mkdtemp(), "t.db"))
    record = new_relationship("U" * 100000, now=NOW)
    record["relation_hint"] = "h" * 500
    assert store.put_relationship(record)
    # 全长查不到（已截断），64 长查得到
    assert store.get_relationship("U" * 100000) is None
    got = store.get_relationship("U" * 64)
    assert got is not None
    assert got["user_id"] == "U" * 64
    assert len(got["relation_hint"]) == 64


def test_f004_interrupt_dedup_key_includes_batch():
    """F-004：同活动第二次打断仍会注入（去重键含批次）。"""

    async def run():
        module, plugin, host = _make_plugin()
        plugin._state.activity = "meal"
        calls: list[str] = []

        async def fake_append(session_id, items, **kwargs):
            calls.append(session_id)

        plugin.ctx.maisaka.context.append = fake_append
        # 第一次打断：注入
        await plugin.note_session({
            "session_id": "p1", "user_id": "1",
            "processed_plain_text": "嗨", "time": NOW,
        })
        assert len(calls) == 1
        # 窗口结束回退
        plugin._expire_interrupt(plugin._state.interrupt_until + 100)
        assert plugin._state.activity == "meal"
        # 第二次从 meal 打断：仍要注入（批次变了）
        await plugin.note_session({
            "session_id": "p1", "user_id": "1",
            "processed_plain_text": "在吗", "time": NOW + 1000,
        })
        assert plugin._state.activity == X.CHATTING
        assert len(calls) == 2, "同活动第二次打断必须重新注入（F-004）"

    asyncio.run(run())
