# -*- coding: utf-8 -*-
"""L3：内心维度（mood：stress / loneliness / social battery）的演化与消费。

钉住六件事：

1. **不进倍率**——mood 演化前后 ``compute_adjust`` 的输入字段（emotion/energy/
   activity）一个都不变（方案总纪律）；
2. 工作类活动加压、睡眠解压、独处回电、被 @ 耗电；
3. 注入文本只在过载（≥7 / 电量 <3）时出现，回落不写「恢复正常」；
4. 电量闸：<2 直接不开口，2–4 阈值上浮；
5. 注入去重：同一会话同一天只有一条；
6. 注入失败三次停手（宿主可能没这个能力）。
"""

import asyncio
import pathlib
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_mood as M  # noqa: E402
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
POLICY = M.MoodPolicy()


def _make_plugin(**overrides):
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_mood")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = 0
    config["physio"]["meals"] = []
    config["mood"]["enabled"] = True
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    plugin._rebuild_from_config()
    return module, plugin, host


# ---------------------------------------------------------------- 纯模块：演化


def test_draining_activity_raises_stress_and_sleep_relieves():
    state = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 7.0})()
    M.evolve(state, activity="overtime", minutes=60, had_contact=False,
             had_mention=False, policy=POLICY, last_contact_at=0.0, now=NOW)
    assert state.stress > 3.0, "加班一小时必须更累"

    before = state.stress
    M.evolve(state, activity="sleep", minutes=60, had_contact=False,
             had_mention=False, policy=POLICY, last_contact_at=0.0, now=NOW)
    assert state.stress < before, "睡觉解压"
    assert state.social_battery > 7.0, "睡眠回电"


def test_solo_activities_recover_battery():
    state = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 5.0})()
    M.evolve(state, activity="daze", minutes=60, had_contact=False,
             had_mention=False, policy=POLICY, last_contact_at=0.0, now=NOW)
    assert state.social_battery == pytest.approx(5.4)


def test_mention_costs_battery_and_relieves_loneliness():
    state = type("S", (), {"stress": 3.0, "loneliness": 5.0, "social_battery": 7.0})()
    M.evolve(state, activity="daily", minutes=10, had_contact=True,
             had_mention=True, policy=POLICY, last_contact_at=NOW - 60, now=NOW)
    assert state.social_battery == pytest.approx(6.85)
    assert state.loneliness == pytest.approx(4.7)


def test_sleep_exempts_message_drain_tick_side():
    """v1.18.1（D9）：睡着/小睡时普通消息不扣电量——真机实测夜间群流量把

    睡眠回充（+1.0/h）全部吃光，电量整夜贴 0。tick 侧 ``had_contact`` 走
    ``last_contact_at`` 兜底判据，睡眠中同样命中，必须在这里门住。
    """

    for activity in ("sleep", "nap"):
        state = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 1.0})()
        M.evolve(state, activity=activity, minutes=10, had_contact=True,
                 had_mention=False, policy=POLICY, last_contact_at=NOW - 60, now=NOW)
        # 10 分钟睡眠回充 +0.1667，且没有被消息扣费吃掉
        assert state.social_battery == pytest.approx(1.0 + POLICY.battery_recover_per_sleep_hour / 6.0), (
            f"{activity} 中普通消息必须豁免扣费"
        )


def test_sleep_does_not_exempt_mention_drain_tick_side():
    """被 @ 不豁免：唤醒机制在，mention 是「真叫醒」。"""

    state = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 5.0})()
    M.evolve(state, activity="sleep", minutes=10, had_contact=False,
             had_mention=True, policy=POLICY, last_contact_at=NOW - 60, now=NOW)
    # 被 @ 扣费照结，睡眠回充也照给（两条独立结算）
    assert state.social_battery == pytest.approx(
        5.0 - POLICY.battery_per_mention + POLICY.battery_recover_per_sleep_hour / 6.0
    )


def test_hook_sleep_exempts_normal_message_drain():
    """接线层（v1.18.1 D9）：钩子对每条入站消息即时扣费——睡着时普通消息跳过。"""

    async def run():
        module, plugin, host = _make_plugin()
        plugin._state.activity = "sleep"
        plugin._state.social_battery = 2.0
        plugin._state.loneliness = 4.0
        plugin._mood_signal_in_hook({"mentioned": False, "at": 1_700_000_000.0})
        assert plugin._state.social_battery == pytest.approx(2.0), "睡眠中普通消息不扣电量"
        # 清醒后恢复扣费
        plugin._state.activity = "daily"
        plugin._mood_signal_in_hook({"mentioned": False, "at": 1_700_000_060.0})
        assert plugin._state.social_battery == pytest.approx(2.0 - POLICY.battery_per_message)
        # 被 @ 永远扣（睡眠中也一样）
        plugin._state.activity = "sleep"
        plugin._state.social_battery = 2.0
        plugin._mood_signal_in_hook({"mentioned": True, "at": 1_700_000_120.0})
        assert plugin._state.social_battery == pytest.approx(2.0 - POLICY.battery_per_mention)

    asyncio.run(run())


def test_loneliness_grows_after_six_quiet_hours_only():
    # 刚互动过：不涨
    state = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 7.0})()
    M.evolve(state, activity="daze", minutes=10, had_contact=False,
             had_mention=False, policy=POLICY, last_contact_at=NOW - 60, now=NOW,
             last_addressed_at=NOW - 60)
    assert state.loneliness == 4.0
    # 六小时没人跟她说话：涨
    # v1.20.1（#6）：锚点换成 ``last_addressed_at``（"有人跟我说话"）——
    # 用 ``last_contact_at``（"房间里有人说话"）时活跃群下永远到不了这一支。
    state2 = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 7.0})()
    M.evolve(state2, activity="daze", minutes=10, had_contact=False,
             had_mention=False, policy=POLICY, last_contact_at=NOW - 7 * 3600, now=NOW,
             last_addressed_at=NOW - 7 * 3600)
    assert state2.loneliness > 4.0


def test_mood_never_touches_the_multiplier_inputs():
    """总纪律：mood 演化**不碰** emotion / energy / activity。"""

    class S:
        stress = 3.0
        loneliness = 4.0
        social_battery = 7.0
        emotion = 6.0
        energy = 5.0

    state = S()
    M.evolve(state, activity="overtime", minutes=60, had_contact=True,
             had_mention=True, policy=POLICY, last_contact_at=NOW, now=NOW)
    assert state.emotion == 6.0 and state.energy == 5.0


# ---------------------------------------------------------------- 纯模块：消费


def test_injection_lines_gate():
    overload = type("S", (), {"stress": 7.5, "loneliness": 4.0, "social_battery": 7.0})()
    lines = M.injection_lines(overload)
    assert len(lines) == 1 and "压力" in lines[0]

    both = type("S", (), {"stress": 8.0, "loneliness": 8.0, "social_battery": 2.5})()
    assert len(M.injection_lines(both)) == 3

    calm = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 7.0})()
    assert M.injection_lines(calm) == (), "平静时闭嘴"

    recovering = type("S", (), {"stress": 4.0, "loneliness": 4.0, "social_battery": 7.0})()
    assert M.injection_lines(recovering) == (), "回落不写「恢复正常」"


def test_battery_gate():
    empty = type("S", (), {"social_battery": 1.5})()
    assert M.battery_gate(empty) == "low_battery"
    low = type("S", (), {"social_battery": 3.0})()
    assert M.battery_gate(low) == "battery_threshold"
    fine = type("S", (), {"social_battery": 6.0})()
    assert M.battery_gate(fine) == ""
    nan = type("S", (), {"social_battery": float("nan")})()
    assert M.battery_gate(nan) == "", "坏值宁可多说不可永远沉默"


def test_proactive_cost():
    state = type("S", (), {"social_battery": 5.0})()
    M.note_proactive_cost(state, policy=POLICY)
    assert state.social_battery == pytest.approx(4.7)


# ---------------------------------------------------------------- 接线


def test_hook_injects_once_per_session_per_day():
    async def run():
        module, plugin, host = _make_plugin()
        now = plugin._state.last_tick_at or 1_700_000_000.0
        plugin._state.stress = 8.0
        session_id = "fake-stream"
        # FakeHost 对 maisaka.context.append 会落穿到默认 {"success": True, "result": None}
        await plugin._maybe_inject_mood(session_id, now)
        first = [kw for kw in host.calls_of("maisaka.context.append")]
        assert first, "过载时应注入一条"
        await plugin._maybe_inject_mood(session_id, now + 60)
        second = [kw for kw in host.calls_of("maisaka.context.append")]
        assert len(second) == len(first), "同一天同一会话只注入一次"

    asyncio.run(run())


def test_hook_injection_stops_after_three_failures():
    async def run():
        module, plugin, host = _make_plugin()
        now = 1_700_000_000.0
        plugin._state.stress = 8.0

        async def broken(*args, **kwargs):
            raise RuntimeError("宿主没这能力")

        plugin.ctx.maisaka.context.append = broken
        for _ in range(3):
            await plugin._maybe_inject_mood("s1", now)
        assert plugin._mood_inject_failures == 3
        calls_before = plugin._mood_inject_failures
        await plugin._maybe_inject_mood("s2", now)  # 新会话也不该再试
        assert plugin._mood_inject_failures == calls_before

    asyncio.run(run())


def test_low_battery_blocks_proactive():
    async def run():
        module, plugin, host = _make_plugin(
            proactive={"enabled": True, "quiet_hours": [], "score_threshold": 0.0,
                       "min_interval_minutes": 0, "recent_user_silence_minutes": 0,
                       "daily_max": 5},
        )
        plugin._state.social_battery = 1.0
        plugin._state.materials.append(
            {"label": "test", "text": "今天好累", "weight": 2.0,
             "created_at": 0.0, "expires_at": 9_999_999_999.0}
        )
        await plugin._maybe_proactive(1_700_000_000.0)
        # 没有任何触发；台账里记 low_battery
        assert plugin._state.skip_ledger.get("low_battery"), plugin._state.skip_ledger

    asyncio.run(run())


# ================================================================ v1.19.2
# 孤独回落有界化（D8-2）+ 钩子层不再回落（D8-1）
#
# 真机 2026-10-15：钩子每条消息 -0.3 而上涨唯一通道 quiet>=6h 才 +0.05/tick，
# 活跃群（消息间隔中位 0.17h）下结构性只降不涨，18h 恒 0.0。

def test_loneliness_relief_is_bounded_per_tick():
    """一个 tick 内来 30 条消息也只回一次：-relief 而非 30×relief。"""

    state = type("S", (), {"stress": 3.0, "loneliness": 5.0, "social_battery": 9.0})()
    M.evolve(state, activity="daily", minutes=10, had_contact=True,
             had_mention=False, policy=POLICY, last_contact_at=NOW - 60, now=NOW,
             had_addressed=True)
    assert state.loneliness == pytest.approx(5.0 - POLICY.loneliness_relief_per_tick), (
        "回落必须每 tick 至多一次（有界），与消息条数无关"
    )


def test_loneliness_can_actually_grow_between_sparse_quiets():
    """回归锚点：涨与落同量级时，安静段孤独能真实累积（旧设计下永远不能）。"""

    state = type("S", (), {"stress": 3.0, "loneliness": 0.0, "social_battery": 9.0})()
    # 模拟「每小时一批消息 + 消息间隔 < 6h 但有安静 tick」的活跃群白天：
    # 消息 tick 落 -0.3，安静 tick（未满 6h）不动 ⇒ 白天缓降、最多到 0，不再钉死在任意值
    now = NOW
    for _ in range(30):
        M.evolve(state, activity="daily", minutes=10, had_contact=True,
                 had_mention=False, policy=POLICY, last_contact_at=now - 60, now=now,
                 had_addressed=True, last_addressed_at=now - 60)
        now += 600
    assert state.loneliness == 0.0
    # 深夜安静段（>6h）必须能涨回去——旧设计下白天已被钉 0，这段也能涨，但
    # 关键差别是：**白天不再以 -0.3/条 的速度瞬间清零后保持 0**，而是有界缓降
    state2 = type("S", (), {"stress": 3.0, "loneliness": 2.0, "social_battery": 9.0})()
    for _ in range(12):
        M.evolve(state2, activity="daze", minutes=10, had_contact=False,
                 had_mention=False, policy=POLICY, last_contact_at=now - 7 * 3600, now=now,
                 last_addressed_at=now - 7 * 3600)
        now += 600
    assert state2.loneliness > 2.0, "安静 6h 后孤独必须累积（每小时 +0.3）"


def test_hook_no_longer_relieves_loneliness():
    """接线层（D8-1）：钩子只刷 last_contact_at 锚点，孤独回落统一下沉到 tick 层。"""

    async def run():
        module, plugin, host = _make_plugin()
        plugin._state.activity = "daily"
        plugin._state.loneliness = 5.0
        before = plugin._state.loneliness
        plugin._mood_signal_in_hook({"mentioned": False, "at": 1_700_000_000.0})
        assert plugin._state.loneliness == before, "钩子层不许再动孤独（有界化后统一在 tick 层）"
        assert plugin._state.last_contact_at == 1_700_000_000.0, "锚点照常刷新"

    asyncio.run(run())


def test_hook_sleep_still_updates_contact_anchor():
    """睡眠中收到消息：锚点照刷（醒来后 quiet 从消息时刻起算），孤独电量都不动。"""

    async def run():
        module, plugin, host = _make_plugin()
        plugin._state.activity = "sleep"
        plugin._state.loneliness = 5.0
        plugin._state.social_battery = 3.0
        plugin._mood_signal_in_hook({"mentioned": False, "at": 1_700_000_500.0})
        assert plugin._state.last_contact_at == 1_700_000_500.0
        assert plugin._state.loneliness == 5.0, "睡眠中孤独不被消息压（模块注释语义）"
        assert plugin._state.social_battery == 3.0, "睡眠中普通消息不扣电量（v1.18.1 语义保持）"

    asyncio.run(run())


def test_hook_mention_still_costs_battery_and_does_not_relieve_loneliness():
    """被 @：电量照扣（真叫醒），孤独回落也统一下沉到 tick 层（不在钩子）。"""

    async def run():
        module, plugin, host = _make_plugin()
        plugin._state.activity = "daily"
        plugin._state.loneliness = 5.0
        plugin._state.social_battery = 5.0
        plugin._mood_signal_in_hook({"mentioned": True, "at": 1_700_000_900.0})
        assert plugin._state.social_battery == pytest.approx(
            5.0 - POLICY.battery_per_mention), "被 @ 扣电量保持"
        assert plugin._state.loneliness == 5.0, "孤独回落不在钩子层"

    asyncio.run(run())


# ---------------------------------------------------------------- 「被找」口径（v1.20.1 #6）


def test_loneliness_growth_ignores_group_chatter_among_others():
    """**核心回归**：别人互相刷屏不算"有人陪她"，孤独必须能涨。

    真机 283 样本（23.5 小时、v1.19.2）里孤独 **283/283 恒 0.0000**，根因是
    ``had_contact`` 由**任何**入站群消息刷新 ⇒ 活跃群清醒时几乎每个 tick 都走回落
    分支、增长分支连进都进不去。这一条把「房间里有人说话」与「有人跟我说话」分开：
    前者成立（``had_contact=True``）而后者不成立时，孤独**不再回落**，安静够久就涨。
    """

    state = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 9.0})()
    # 群里一直有人在聊（had_contact 恒真），但没人跟她说话、且已 7 小时没人找她
    for _ in range(12):
        M.evolve(
            state, activity="daily", minutes=10,
            had_contact=True, had_mention=False, policy=POLICY,
            last_contact_at=NOW - 60,          # 房间里刚有人说话
            now=NOW,
            had_addressed=False,
            last_addressed_at=NOW - 7 * 3600,  # 但她已经 7 小时没被找过
        )
        assert state.loneliness >= 4.0, "别人刷屏不该消除她的孤独"
    assert state.loneliness > 4.0, "安静够久就该涨（每小时 +0.3）"


def test_loneliness_still_relieves_when_someone_addresses_her():
    """反向：真的有人跟她说话时照常回落（有界，每 tick 至多一次）。"""

    state = type("S", (), {"stress": 3.0, "loneliness": 6.0, "social_battery": 9.0})()
    M.evolve(
        state, activity="daily", minutes=10,
        had_contact=True, had_mention=False, policy=POLICY,
        last_contact_at=NOW, now=NOW,
        had_addressed=True, last_addressed_at=NOW,
    )
    assert state.loneliness == pytest.approx(6.0 - POLICY.loneliness_relief_per_tick)


def test_loneliness_rollback_switch_restores_v1192_behaviour():
    """回退开关：``loneliness_requires_addressed=False`` 精确回到"房间里有人=有人陪"。"""

    policy = M.MoodPolicy(loneliness_requires_addressed=False)
    state = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 9.0})()
    for _ in range(12):
        M.evolve(
            state, activity="daily", minutes=10,
            had_contact=True, had_mention=False, policy=policy,
            last_contact_at=NOW - 60, now=NOW,
            had_addressed=False, last_addressed_at=NOW - 7 * 3600,
        )
    # 旧口径下 had_contact=True ⇒ 每 tick 走回落、增长分支永远进不去
    assert state.loneliness == pytest.approx(
        4.0 - 12 * POLICY.loneliness_relief_per_tick
    ), "旧口径下只降不涨"


def test_battery_branch_still_counts_group_chatter():
    """电量**不受**这项影响：群体闲聊仍算「在场社交」、照常耗电。

    两个维度问的不是同一个问题——孤独看"有没有人跟我说话"，电量看"我在不在社交场合"。
    """

    state = type("S", (), {"stress": 3.0, "loneliness": 4.0, "social_battery": 5.0})()
    before = state.social_battery
    M.evolve(
        state, activity="daily", minutes=10,
        had_contact=True, had_mention=False, policy=POLICY,
        last_contact_at=NOW, now=NOW,
        had_addressed=False, last_addressed_at=NOW - 7 * 3600,
    )
    assert state.social_battery < before, "群里有人说话仍然耗电（在场社交）"
