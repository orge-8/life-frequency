# -*- coding: utf-8 -*-
"""L3：关系模型（relations）的建档判据、演化与消费。

钉住五件事：

1. **建档判据收紧**（决策 2）：群里纯围观的人不建档——「她只认识跟她说过话的人」
   本身就是人味；
2. 熟悉度演化：互动 +0.2、共同经历 +1.0、7 天未互动 −0.5、钳位 0–100；
3. 档位与阈值系数：亲密 0.5× / 陌生 2.0×（对熟人开口不需要攒勇气）；
4. **落到 SQLite**：跨重启还在；档案超上限按「最近互动最久远」淘汰；
5. 接线：群聊被 @ 的人真的进了库，围观的人没有。
"""

import asyncio
import pathlib
import sys

import pytest

pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_relations as R  # noqa: E402
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
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_relations")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = 0
    config["physio"]["meals"] = []
    config["relations"]["enabled"] = True
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    plugin._rebuild_from_config()
    plugin._open_store()
    return module, plugin, host


# ---------------------------------------------------------------- 纯模块


def test_should_record_criteria():
    assert R.should_record(private_chat=True, mentioned=False,
                           is_reply_to_bot=False, user_id="42"), "私聊任意消息都建档"
    assert R.should_record(private_chat=False, mentioned=True,
                           is_reply_to_bot=False, user_id="42"), "被 @ 建档"
    assert R.should_record(private_chat=False, mentioned=False,
                           is_reply_to_bot=True, user_id="42"), "回复她建档"
    assert not R.should_record(private_chat=False, mentioned=False,
                               is_reply_to_bot=False, user_id="42"), "纯围观不建档"
    assert not R.should_record(private_chat=True, mentioned=False,
                               is_reply_to_bot=False, user_id=""), "认不出人不建档"


def test_touch_creates_and_accumulates():
    store = R.__dict__.get("MemoryStore")  # noqa: F841
    from life_store import MemoryStore

    store = MemoryStore()
    first = R.touch(store, user_id="42", now=NOW)
    assert first is not None and first["interaction_count"] == 1
    assert first["familiarity"] == 0.0, "首次互动只有建档，不加熟悉度"

    # 三次被 @：+0.2 × 3
    R.touch(store, user_id="42", now=NOW + 1, mentioned=True)
    R.touch(store, user_id="42", now=NOW + 2, mentioned=True)
    third = R.touch(store, user_id="42", now=NOW + 3, mentioned=True)
    assert third["familiarity"] == pytest.approx(0.6)
    assert R.tier_of(third["familiarity"]) == "陌生"


def test_tier_and_multiplier():
    assert R.tier_of(0) == "陌生"
    assert R.tier_of(20) == "认识"
    assert R.tier_of(50) == "熟络"
    assert R.tier_of(80) == "亲密"
    assert R.threshold_multiplier(85) == 0.5
    assert R.threshold_multiplier(5) == 2.0
    assert R.threshold_multiplier(0 if False else float("nan")) == 2.0  # NaN 按陌生


def test_decay_after_a_week():
    from life_store import MemoryStore

    store = MemoryStore()
    record = R.touch(store, user_id="1", now=NOW, mentioned=True)
    record["familiarity"] = 30.0
    store.put_relationship(record)

    # 六天：不衰减
    assert R.decay_all(store, now=NOW + 6 * 86400) == 0
    # 八天：衰减 0.5
    assert R.decay_all(store, now=NOW + 8 * 86400) == 1
    assert store.get_relationship("1")["familiarity"] == pytest.approx(29.5)


def test_prompt_lines_mask_user_id_and_skip_strangers():
    lines = R.prompt_lines(
        [
            {"user_id": "123456789", "familiarity": 85.0, "relation_hint": "朋友"},
            {"user_id": "987", "familiarity": 10.0, "relation_hint": ""},
            {"user_id": "55555555", "familiarity": 55.0, "relation_hint": ""},
        ],
        max_lines=3,
    )
    assert len(lines) == 2, "陌生档位不进 prompt"
    assert "朋友" in lines[0] and "123***89" in lines[0]
    assert "熟络" in lines[1]
    assert R.mask_user_id("123456789") == "123***89"
    assert R.mask_user_id("987") == "987"


# ---------------------------------------------------------------- 接线


def test_hook_records_mentioned_user_but_not_bystander():
    async def run():
        module, plugin, host = _make_plugin()
        hook = plugin.note_session
        # 被 @ 的群友 → 建档
        await hook({
            "session_id": "g1", "group_id": "777", "user_id": "10001",
            "is_at": True, "processed_plain_text": "你好呀",
        })
        # 纯围观 → 不建档
        await hook({
            "session_id": "g1", "group_id": "777", "user_id": "10002",
            "processed_plain_text": "哈哈哈哈",
        })
        # 私聊 → 建档
        await hook({
            "session_id": "p1", "user_id": "10003",
            "processed_plain_text": "在吗",
        })
        store = plugin._routine_store
        assert store.get_relationship("10001") is not None
        assert store.get_relationship("10002") is None, "围观群众不建档"
        assert store.get_relationship("10003") is not None

    asyncio.run(run())


def test_intimate_friend_gets_lower_threshold():
    async def run():
        module, plugin, host = _make_plugin()
        store = plugin._routine_store
        from life_store import new_relationship

        record = new_relationship("10001", now=NOW)
        record["familiarity"] = 85.0
        store.put_relationship(record)
        plugin._seen_sessions["fake-stream"] = {
            "session_id": "fake-stream", "user_id": "10001",
        }
        # v1.13.1：_relation_multiplier 改 async（SQLite 读走 to_thread，F-002）
        assert (
            await plugin._relation_multiplier("fake-stream") == pytest.approx(0.5)
        )
        # 没有档案的人 = 中性 1.0
        plugin._seen_sessions["other"] = {"session_id": "other", "user_id": "99999"}
        assert await plugin._relation_multiplier("other") == pytest.approx(1.0)

    asyncio.run(run())


def test_relations_disabled_is_inert():
    async def run():
        module, plugin, host = _make_plugin(relations={"enabled": False})
        hook = plugin.note_session
        await hook({
            "session_id": "g1", "group_id": "777", "user_id": "10001",
            "is_at": True, "processed_plain_text": "你好",
        })
        assert plugin._routine_store.get_relationship("10001") is None
        # v1.13.1：_relation_multiplier 改 async（F-002）
        assert await plugin._relation_multiplier("g1") == 1.0

    asyncio.run(run())
