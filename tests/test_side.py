# -*- coding: utf-8 -*-
"""L3：多活动并行（side_activities，方案五 / v1.12.1）。

钉住六件事：

1. **解析**：``side`` 字段 → 白名单过滤、去重、去主、上限 2、坏项丢弃；
   老模型不输出 ``side`` ⇒ 空，行为与加它之前完全一致；
2. **enforce 只收口主活动**：背景不经 enforce（穷举对拍不受影响——side 不在
   ActivityFacts 里，裁定只看主活动）；
3. **倍率只按主活动**：``compute_adjust`` 读 ``state.activity``，side 不参与；
4. **能量加权**：主 1.0 + 每背景 0.3；
5. **scene 拼接**：「吃饭，顺便看番」；
6. **打断联动**：打断清空背景、回退不恢复。
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
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_side")
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


# ---------------------------------------------------------------- 纯模块


def test_side_whitelist_excludes_exclusive_activities():
    for banned in ("sleep", "sick_rest", "before_sleep", "chatting"):
        assert banned not in A.SIDE_ACTIVITIES, f"{banned} 不存在「顺便」形态"
    for ok in ("meal", "anime", "game", "music", "daze", "daily"):
        assert ok in A.SIDE_ACTIVITIES
    assert A.MAX_SIDE_ACTIVITIES == 2


def test_parse_response_reads_side():
    decision = A.parse_response(
        '{"activity": "meal", "scene": "在吃饭", "side": ["anime", "music", "sleep"]}'
    )
    assert decision is not None
    assert decision.side == ("anime", "music"), "非法项丢弃、上限 2"
    # 字符串形式的单项 / 背景键
    single = A.parse_response('{"activity": "meal", "side": "music"}')
    assert single is not None and single.side == ("music",)
    none_side = A.parse_response('{"activity": "meal", "scene": "在吃饭"}')
    assert none_side is not None and none_side.side == (), "老模型不输出 side ⇒ 空"


def test_normalize_side_dedups_and_drops_main():
    side = A.normalize_side(["anime", "anime", "meal", "badname", "sleep"], main="meal")
    assert side == ("anime",), "去主活动、去重、白名单过滤"
    assert A.normalize_side("not-a-list", main="meal") == ()
    assert A.normalize_side([], main="meal") == ()


def test_enforce_only_governs_main_activity():
    """背景不经 enforce：decision.side 原样穿过强制层，裁定只看主活动。"""

    decision = A.ActivityDecision("meal", "", A.SOURCE_LLM, "模型输出", side=("anime",))
    facts = A.ActivityFacts(
        activity="meal", minutes_in_activity=120, now_minutes=12 * 60 + 30, energy=6.0
    )
    out = A.enforce(facts, decision, A.EnforcePolicy())
    assert out.activity == "meal"
    assert out.side == ("anime",), "enforce 不收口背景"


def test_apply_writes_side_and_concatenates_scene():
    state = S.new_state(now=NOW, config=S.SimConfig())
    decision = A.ActivityDecision(
        "meal", "在吃饭", A.SOURCE_LLM, "模型输出", side=("anime", "music")
    )
    S.apply_activity(state, decision, now=NOW, config=S.SimConfig())
    assert state.side_activities == ["anime", "music"]
    assert "顺便看番" in state.scene, "scene 拼接第一项背景"

    # 模型下一轮不提背景 ⇒ 清空（防残留），scene 回到模型给的新值
    next_decision = A.ActivityDecision("daily", "在做事", A.SOURCE_LLM, "模型输出")
    S.apply_activity(state, next_decision, now=NOW + 60, config=S.SimConfig())
    assert state.side_activities == []

    # 非 LLM 来源（enforced）不带 side ⇒ 保留现状
    state.side_activities = ["music"]
    enforced = A.ActivityDecision("daily", "", A.SOURCE_ENFORCED, "硬约束")
    S.apply_activity(state, enforced, now=NOW + 120, config=S.SimConfig())
    assert state.side_activities == ["music"], "确定性层不替模型清背景"


def test_from_dict_sanitizes_side():
    dirty = S.LifeState.from_dict({"side_activities": ["anime", "sleep", "anime", "xx", "game"]})
    assert dirty.side_activities == ["anime", "game"], "白名单/去重/上限 2"
    # 状态文件里字符串单值不是合法持久化格式（模型侧的单值已由 normalize_side 收）：
    # from_dict 的 list 分支直接跳过 ⇒ 保持默认空
    assert S.LifeState.from_dict({"side_activities": "anime"}).side_activities == []
    assert S.LifeState.from_dict({"side_activities": 42}).side_activities == []


def test_energy_weighted_main_1_side_03():
    """能量结算：主 1.0 + 每背景 0.3。"""

    from life_sim import SimConfig

    config = SimConfig()
    state = S.new_state(now=NOW, config=config)
    state.activity = "meal"  # +0.20/h
    state.side_activities = ["anime", "music"]  # -0.50 -0.25
    # 期望合成速率 = 0.20 + 0.3 * (-0.75) = -0.025/h
    S.settle(state, now=NOW + 3600, config=config, events=None, rng=None)
    expected = 6.0 + (-0.025)
    assert state.energy == pytest.approx(expected, abs=0.05), (
        f"加权后 1 小时应 ≈ {expected:.3f}，实际 {state.energy:.3f}"
    )


# ---------------------------------------------------------------- 接线


def test_interrupt_clears_side_and_expire_does_not_restore():
    async def run():
        module, plugin, host = _make_plugin()
        state = plugin._state
        state.activity = "meal"
        state.side_activities = ["anime"]
        await plugin.note_session({
            "session_id": "p1", "user_id": "1",
            "processed_plain_text": "嗨", "time": NOW,
        })
        assert state.activity == X.CHATTING
        assert state.side_activities == [], "打断清空背景"

        # 回退：不恢复背景（穿帮）
        plugin._expire_interrupt(state.interrupt_until + 100)
        assert state.activity == "meal"
        assert state.side_activities == [], "回退不恢复背景"

    asyncio.run(run())


def test_status_card_shows_side():
    async def run():
        module, plugin, host = _make_plugin()
        plugin._state.side_activities = ["anime"]
        card = plugin._render_activity(NOW)
        assert "背景：看番" in card
        plugin._state.side_activities = []
        assert "背景：（无）" in plugin._render_activity(NOW)

    asyncio.run(run())
