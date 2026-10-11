# -*- coding: utf-8 -*-
"""L3：梦境与睡眠余波（dream）的概率、兜底、衰减与接线。

钉住五件事（方案八 §8.4 的测试要求）：

1. **概率种子可复现**：``roll_dream`` 的 rng 注入——同种子同结果、
   非 ≥3h 长睡眠绝不触发、开关关闭绝不触发；
2. **LLM 失败兜底**：模型超时/失败/输出壳话 → 模板库接管，梦永不缺席也永不阻塞；
3. **素材衰减**：权重 0.6、``expires_at`` = 本地今天 12:00，上午全额、之后
   线性衰减到 0（下午还讲梦就奇怪了）；下午醒来不产素材；
4. **模板桶**：压力高 → 焦虑桶、孤独高 → 「有人陪」桶、NaN → 平静桶；
5. **接线**：只有 ≥3h 的「睡→醒」会标记 pending；梦落 recent_events（kind=dream）
   与素材；叙事层纪律——情绪/体力增量恒 0、不碰倍率。
"""

import asyncio
import pathlib
import random
import sys

import pytest

pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_activity as A  # noqa: E402
import life_dream as D  # noqa: E402
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
NOW = 1_700_000_000.0  # 2023-11-14 22:13 UTC（测试只用相对关系，不看墙钟意义）


def _make_plugin(**overrides):
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_dream")
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


def test_roll_dream_seed_reproducible_and_thresholds():
    # 同种子同结果（概率判定可复现）
    r1 = D.roll_dream(enabled=True, probability=1.0, sleep_minutes=300,
                      rng=random.Random(42))
    r2 = D.roll_dream(enabled=True, probability=0.0, sleep_minutes=300,
                      rng=random.Random(42))
    assert r1 is True and r2 is False, "probability=1.0 必中、0.0 必不中"

    rng = random.Random(7)
    results = {
        D.roll_dream(enabled=True, probability=0.5, sleep_minutes=300, rng=rng)
        for _ in range(50)
    }
    assert results == {True, False}, "0.5 概率在 50 次里应两者都出现"

    # 非 ≥3h 长睡眠绝不触发（小憩不做梦）
    for minutes in (0, 60, 179.9):
        assert not D.roll_dream(enabled=True, probability=1.0,
                                sleep_minutes=minutes, rng=random.Random(1))
    assert D.roll_dream(enabled=True, probability=1.0, sleep_minutes=180.0,
                        rng=random.Random(1)), "恰好 180 分钟算长睡眠"
    # 开关关闭 / 坏输入
    assert not D.roll_dream(enabled=False, probability=1.0, sleep_minutes=600,
                            rng=random.Random(1))
    assert not D.roll_dream(enabled=True, probability=1.0, sleep_minutes=float("nan"),
                            rng=random.Random(1))


def test_mood_buckets():
    assert D.mood_bucket(7.0, 2.0) == "anxious", "压力优先于孤独"
    assert D.mood_bucket(2.0, 7.0) == "lonely"
    assert D.mood_bucket(3.0, 3.0) == "calm"
    assert D.mood_bucket(float("nan"), float("nan")) == "calm"
    # 模板库按方案规模：≥18 条、三桶各有货
    assert sum(len(v) for v in D.TEMPLATES.values()) >= 18
    for bucket in ("anxious", "lonely", "calm"):
        assert D.TEMPLATES[bucket], bucket
    rng = random.Random(3)
    text = D.template_text("anxious", rng)
    assert text in D.TEMPLATES["anxious"]
    assert D.template_text("no-such-bucket", rng) in D.TEMPLATES["calm"]


def test_sanitize_dream_strips_llm_shells():
    assert D.sanitize_dream("梦见家里阳台上的花全开了。") == "梦见家里阳台上的花全开了"
    assert D.sanitize_dream("「梦见下雪了」") == "梦见下雪了"
    assert D.sanitize_dream("好的，梦境是：梦见在便利店买了个饭团") == "梦见在便利店买了个饭团"
    assert D.sanitize_dream("梦境：「梦见迷路了，怎么走都回到原地」") == (
        "梦见迷路了，怎么走都回到原地"
    )
    assert D.sanitize_dream("") == ""
    assert D.sanitize_dream(None) == ""
    # 输出了一整段 = 没听懂要求，按失败处理（空串 → 调用方落模板）
    assert D.sanitize_dream("梦一：" + "很长" * 200) == ""


def test_dream_records_are_narrative_only():
    event = D.recent_event("梦见下雪了", now=NOW, activity="daily")
    assert event["kind"] == "dream" and event["label"] == "梦境"
    assert event["emotion"] == 0.0 and event["energy"] == 0.0, "叙事层：零增量"

    mat = D.material("梦见下雪了", now=NOW, expires_at=NOW + 3600)
    assert mat["weight"] == pytest.approx(0.6)
    assert mat["best_until"] == pytest.approx(NOW + 3600.0)
    # 素材随 expires_at 走 material_freshness：过点触底
    assert S.material_freshness(mat, now=NOW) == pytest.approx(1.0)
    assert S.material_freshness(mat, now=NOW + 3601) == pytest.approx(0.0)


def test_dream_prompt_carries_mood_and_seeds():
    prompt = D.dream_prompt(
        bot_name="小铃", stress=8.0, loneliness=2.0,
        seed_lines=("昨天和小林聊了猫", "交了一份作业"),
    )
    assert "小铃" in prompt and "一句话" in prompt
    assert "焦虑" in prompt, "压力高时基调偏焦虑"
    assert "小林" in prompt
    calm = D.dream_prompt(bot_name="她", stress=1.0, loneliness=1.0, seed_lines=())
    assert "平静" in calm and "（最近很平淡）" in calm


# ---------------------------------------------------------------- 接线


def test_short_wake_does_not_mark_pending():
    """短睡眠（<3h）醒来不标记 pending——小憩不做梦。

    把最短睡眠时长配小（60 分钟）让 enforce 允许 100 分钟的小憩被唤醒；
    100 < DREAM_MIN_SLEEP_MINUTES(180) ⇒ 不标记。
    """

    async def run():
        module, plugin, host = _make_plugin(activity={"min_sleep_minutes": 60})
        state = plugin._state
        state.activity = A.SLEEP
        state.sleep_started_at = NOW - 100 * 60  # 100 分钟小憩
        state.activity_since = state.sleep_started_at  # 锚点有效（0 会被当成损坏）
        plugin._enforce_and_apply(
            NOW, A.ActivityDecision("daily", "", A.SOURCE_LLM, "测试唤醒")
        )
        assert state.activity != A.SLEEP
        assert plugin._dream_wake_pending is None, "小憩不做梦"

    asyncio.run(run())


def test_long_wake_marks_pending_and_dream_lands():
    async def run():
        module, plugin, host = _make_plugin(dream={"probability": 1.0})
        state = plugin._state
        state.activity = A.SLEEP
        state.sleep_started_at = NOW - 400 * 60  # 6.7 小时
        state.activity_since = state.sleep_started_at
        # 模型提议醒来（活动任意非 sleep）
        plugin._enforce_and_apply(
            NOW, A.ActivityDecision("daily", "", A.SOURCE_LLM, "测试唤醒")
        )
        assert state.activity != A.SLEEP
        assert plugin._dream_wake_pending is not None, "≥3h 醒来标记 pending"
        minutes = plugin._dream_wake_pending[1]
        assert minutes == pytest.approx(400.0, rel=0.01)

        # 生成（概率 1.0）；把 use_llm 关掉 → 必走模板，不发 RPC
        config = plugin.config
        config.dream.use_llm = False
        await plugin._maybe_dream(NOW)
        dreams = [e for e in state.recent_events if e.get("kind") == "dream"]
        assert len(dreams) == 1, "梦落 recent_events"
        assert dreams[0]["emotion"] == 0.0 and dreams[0]["energy"] == 0.0
        assert plugin._dream_wake_pending is None, "pending 只消费一次"
        # 再跑一次：没有 pending，不会重复做梦
        await plugin._maybe_dream(NOW + 1)
        dreams = [e for e in state.recent_events if e.get("kind") == "dream"]
        assert len(dreams) == 1
        # 素材：NOW 是 22:13 UTC（tz_offset=0），已过 12:00 → 不产素材
        dream_materials = [m for m in state.materials if m.get("label") == "梦境"]
        assert dream_materials == [], "下午醒来不产梦素材"

    asyncio.run(run())


def test_llm_failure_falls_back_to_template():
    async def run():
        module, plugin, host = _make_plugin(dream={"probability": 1.0})

        async def broken_generate(**kwargs):
            raise TimeoutError("模型超时")

        plugin.ctx.llm.generate = broken_generate
        plugin._dream_wake_pending = (NOW, 400.0)
        await plugin._maybe_dream(NOW)
        dreams = [e for e in plugin._state.recent_events if e.get("kind") == "dream"]
        assert len(dreams) == 1, "LLM 失败也要有梦（模板兜底）"
        text = dreams[0]["text"]
        assert any(text in pool for pool in D.TEMPLATES.values()), "内容来自模板库"

    asyncio.run(run())


def test_probability_zero_never_dreams():
    async def run():
        module, plugin, host = _make_plugin(dream={"probability": 0.0})
        plugin._dream_wake_pending = (NOW, 400.0)
        await plugin._maybe_dream(NOW)
        assert not [e for e in plugin._state.recent_events if e.get("kind") == "dream"]

    asyncio.run(run())


def test_morning_wake_produces_decaying_material():
    async def run():
        module, plugin, host = _make_plugin(dream={"probability": 1.0})
        plugin.config.dream.use_llm = False
        # 本地（tz_offset=0）UTC 上午 03:26 醒来 → 素材应活到当天 12:00
        wake = 1_699_940_000.0
        expires = plugin._noon_epoch(wake)
        local = expires - wake
        assert 0 < local <= 12 * 3600, "上午醒来 → 素材活到今天 12:00"
        plugin._dream_wake_pending = (wake, 400.0)
        await plugin._maybe_dream(wake)
        mats = [m for m in plugin._state.materials if m.get("label") == "梦境"]
        assert len(mats) == 1, "上午醒来的梦产素材"
        assert mats[0]["expires_at"] == pytest.approx(expires)

    asyncio.run(run())


def test_dream_disabled_is_inert():
    async def run():
        module, plugin, host = _make_plugin(dream={"enabled": False})
        plugin._dream_wake_pending = (NOW, 400.0)
        await plugin._maybe_dream(NOW)
        assert not [e for e in plugin._state.recent_events if e.get("kind") == "dream"]
        assert plugin._dream_wake_pending is None, "pending 照样清掉"

    asyncio.run(run())
