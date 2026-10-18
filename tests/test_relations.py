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


def P(uid: str, platform: str = "qq") -> str:
    """测试侧算 person_id（v1.20.0/Y4 起关系表主键）——与实现同源，不另写一份。"""

    return R.person_id_for(platform, uid)


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
    from life_store import MemoryStore

    store = MemoryStore()
    first = R.touch(store, person_id=P("42"), user_id="42", now=NOW)
    assert first is not None and first["interaction_count"] == 1
    assert first["familiarity"] == 0.0, "首次互动只有建档，不加熟悉度"

    # 三次被 @：+0.2 × 3
    R.touch(store, person_id=P("42"), user_id="42", now=NOW + 1, mentioned=True)
    R.touch(store, person_id=P("42"), user_id="42", now=NOW + 2, mentioned=True)
    third = R.touch(store, person_id=P("42"), user_id="42", now=NOW + 3, mentioned=True)
    assert third["familiarity"] == pytest.approx(0.6)
    assert R.tier_of(third["familiarity"]) == "陌生"


def test_touch_without_person_id_is_rejected():
    """认不出人（空 person_id）→ 不建档。过去判据是空 user_id，现在是空 person_id。"""

    from life_store import MemoryStore

    store = MemoryStore()
    assert R.touch(store, person_id="", user_id="42", now=NOW) is None
    assert store.top_relationships(10) == []


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
    record = R.touch(store, person_id=P("1"), user_id="1", now=NOW, mentioned=True)
    record["familiarity"] = 30.0
    store.put_relationship(record)

    # 六天：不衰减
    assert R.decay_all(store, now=NOW + 6 * 86400) == 0
    # 八天：衰减 0.5
    assert R.decay_all(store, now=NOW + 8 * 86400) == 1
    assert store.get_relationship(P("1"))["familiarity"] == pytest.approx(29.5)


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
        assert store.get_relationship(P("10001")) is not None
        assert store.get_relationship(P("10002")) is None, "围观群众不建档"
        assert store.get_relationship(P("10003")) is not None

    asyncio.run(run())


def test_intimate_friend_gets_lower_threshold():
    async def run():
        module, plugin, host = _make_plugin()
        store = plugin._routine_store
        from life_store import new_relationship

        record = new_relationship(P("10001"), platform="qq", user_id="10001", now=NOW)
        record["familiarity"] = 85.0
        store.put_relationship(record)
        plugin._seen_sessions["fake-stream"] = {
            "session_id": "fake-stream", "user_id": "10001", "platform": "qq",
        }
        # v1.13.1：_relation_multiplier 改 async（SQLite 读走 to_thread，F-002）
        assert (
            await plugin._relation_multiplier("fake-stream") == pytest.approx(0.5)
        )
        # 没有档案的人 = 中性 1.0
        plugin._seen_sessions["other"] = {
            "session_id": "other", "user_id": "99999", "platform": "qq",
        }
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
        assert plugin._routine_store.get_relationship(P("10001")) is None
        # v1.13.1：_relation_multiplier 改 async（F-002）
        assert await plugin._relation_multiplier("g1") == 1.0

    asyncio.run(run())


# ---------------------------------------------------------------- 关系基调（v1.20.0 G1）


def test_tone_table_covers_every_injectable_tier():
    """可注入的档位（认识及以上）都要有基调文案——漏一个是「那一档静默不注入」。

    **「陌生」必须不在表里**：2026-10-16 真机 283 样本显示熟悉度最高只有 9.8、
    全部落在陌生档；若陌生档也注入，她会对每个人（含老朋友）说「客气、有距离感」。
    """

    assert set(R.TIER_TONES) == {"认识", "熟络", "亲密"}
    assert "陌生" not in R.TIER_TONES
    assert R.tier_of(5) == "陌生"


def test_tone_of_returns_executable_direction_not_adjective():
    assert "不用客套" in R.tone_of(90)
    assert "可以开玩笑" in R.tone_of(60)
    assert "不主动套近乎" in R.tone_of(30)
    # 陌生档不注入（真机上绝大多数人都在这一档，注入即反向效果）
    assert R.tone_of(5) == ""
    assert R.tone_of(19.99) == ""
    assert R.tone_line(5) == ""
    assert R.tone_line(0) == ""


def test_tone_skips_strangers_in_step_with_prompt_lines():
    """基调与状态卡**同一口径**：两处都按「陌生档不出现」处理（familiarity < 20）。

    口径漂移的话会出现「状态卡不显示这个人是熟人、回复却按熟人调语气」这种
    自相矛盾的状态，所以这里对拍两处的边界。
    """

    low = [{"user_id": "123456789", "familiarity": 19.9, "relation_hint": ""}]
    assert R.prompt_lines(low, max_lines=3) == (), "陌生档不进 prompt_lines"
    assert R.tone_line(19.9) == "", "陌生档不注入基调"

    just_over = [{"user_id": "123456789", "familiarity": 20.0, "relation_hint": ""}]
    assert len(R.prompt_lines(just_over, max_lines=3)) == 1
    assert R.tone_line(20.0) != "", "「认识」档两处都出现"


def test_tone_line_carries_no_account_id():
    """基调行是给**模型**看的，绝不能塞账号（脱敏纪律，与 prompt_lines 相反）。"""

    line = R.tone_line(85.0)
    assert "关系基调" in line and "亲密" in line
    assert not any(ch.isdigit() for ch in line), line


def test_tone_line_is_silent_on_bad_values():
    """坏值不注入——语气指令给错的代价不对称（门槛算错只是更保守）。"""

    assert R.tone_line(float("nan")) == ""
    assert R.tone_line(float("inf")) == ""
    assert R.tone_line(None) == ""
    assert R.tone_line("abc") == ""


def test_bad_values_are_still_a_stranger_for_the_threshold():
    """对照：**门槛**侧继承既有约定（NaN 按陌生 2.0），与基调侧刻意不同。"""

    assert R.threshold_multiplier(float("nan")) == 2.0
    assert R.tone_line(float("nan")) == ""


def test_life_digest_carries_the_relation_tone():
    """接线：回复摘要里要真的出现关系基调行，且认不出会话时不出现。"""

    async def run():
        module, plugin, host = _make_plugin()
        store = plugin._routine_store
        from life_store import new_relationship

        record = new_relationship(P("10001"), platform="qq", user_id="10001", now=NOW)
        record["familiarity"] = 88.0
        store.put_relationship(record)
        plugin._relation_familiarity[P("10001")] = 88.0
        plugin._seen_sessions["p1"] = {
            "session_id": "p1", "user_id": "10001", "platform": "qq",
        }

        assert "关系基调" in plugin._life_digest("p1")
        assert "亲密" in plugin._life_digest("p1")
        # 认不出会话 ⇒ 不注入（宁缺勿错），但摘要本身照常产出
        assert "关系基调" not in plugin._life_digest("unknown-session")

    asyncio.run(run())


def test_relation_tone_missing_record_is_silent():
    """有会话映射但关系索引里没有这个人 ⇒ 不注入，也不抛。"""

    async def run():
        module, plugin, host = _make_plugin()
        plugin._seen_sessions["p2"] = {
            "session_id": "p2", "user_id": "99999", "platform": "qq",
        }
        plugin._relation_familiarity.clear()
        assert plugin._relation_tone_line("p2") == ""

    asyncio.run(run())


# ---------------------------------------------------------------- person_id（v1.20.0 Y4）


def test_person_id_matches_the_host_algorithm():
    """`person_id` 必须与宿主 `person_info::get_person_id` 逐位一致。

    对齐是「本表能直接 join 宿主人物画像（A_Memorix）」这个承诺的**唯一**依据，
    所以这里照抄宿主的三条规则独立复算，而不是拿实现去比实现。
    """

    import hashlib

    assert R.person_id_for("qq", "123456") == hashlib.md5(b"qq_123456").hexdigest()
    # 宿主：platform 形如 "webchat-abc" 时取 "-" 后半段
    assert R.person_id_for("webchat-abc", "u1") == hashlib.md5(b"abc_u1").hexdigest()
    # 宿主：platform or "qq"
    assert R.person_id_for("", "1") == R.person_id_for("qq", "1")
    assert R.person_id_for(None, "1") == R.person_id_for("qq", "1")
    # 认不出人 → 空串（调用方据此跳过建档，宁可不记也不记错人）
    assert R.person_id_for("qq", "") == ""
    assert R.person_id_for("qq", "   ") == ""


def test_person_id_separates_the_same_uid_across_platforms():
    """同一 QQ 号在两个平台上必须是两份档案——v1 的裸 user_id 主键做不到这件事。"""

    assert R.person_id_for("qq", "10001") != R.person_id_for("discord", "10001")
    assert len(R.person_id_for("qq", "10001")) == 32
