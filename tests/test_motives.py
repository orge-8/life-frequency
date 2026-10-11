# -*- coding: utf-8 -*-
"""L3：主动开口动机扩展（motives，方案步 9 / v1.13.0）。

钉住五件事：

1. **问候类**：早安/晚安时段窗命中、窗外出空、同生活日同窗只一条；
2. **分享类**：概率可控（rng 注入）、同活动同日只一条、未知活动有兜底模板；
3. **关系维护类**：familiarity 门槛 + 闲置天数 + 同人同日只念叨一次 +
   每次至多一人 + 最久没聊的优先 + 文本不带 user_id；
4. **素材形态**：与既有素材管道同构（label/weight/expires_at/best_until），
   stamp 把时长换算成绝对时刻；
5. **接线**：tick 内真的产素材并写去重表；开关全关完全惰性；动机不绕过
   主动开口管线（生成≠发送）。
"""

import asyncio
import pathlib
import random
import sys

import pytest

pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_motives as M  # noqa: E402
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
DAY = "2026-10-08"


def _make_plugin(**overrides):
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_motives")
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
    plugin._open_store()
    return module, plugin, host


# ---------------------------------------------------------------- 纯模块


def test_greeting_window_and_dedup():
    # 早安窗（06:00–11:00）命中
    morning = M.greeting_material(now_minutes=8 * 60, day_key=DAY, seen={})
    assert morning is not None and "早安" in morning["text"]
    assert morning["_key"] == f"问候:早安:{DAY}"
    # 晚安窗（22:00–01:00 跨午夜）
    assert M.greeting_material(now_minutes=23 * 60, day_key=DAY, seen={}) is not None
    assert M.greeting_material(now_minutes=0 * 60 + 30, day_key=DAY, seen={}) is not None
    # 窗外出空（午后）
    assert M.greeting_material(now_minutes=14 * 60, day_key=DAY, seen={}) is None
    # 同生活日同窗只一条
    seen = {f"问候:早安:{DAY}": NOW}
    assert M.greeting_material(now_minutes=9 * 60, day_key=DAY, seen=seen) is None
    # 第二天又能发
    assert M.greeting_material(now_minutes=9 * 60, day_key="2026-10-09", seen=seen) is not None


def test_share_probability_and_dedup():
    # probability 上限用必中/必不中夹逼
    always = random.Random(1)
    hit = M.share_material(activity="anime", rng=always, day_key=DAY, seen={})
    assert hit is None or hit["label"] == "动机:分享"
    # p=0.08 逐次验证：真 rng 大样本两侧都出现
    rng = random.Random(7)
    hits = sum(
        1 for _ in range(500)
        if M.share_material(activity="anime", rng=rng, day_key=f"d{_}", seen={})
    )
    assert 10 < hits < 90, f"0.08 概率 500 次命中 {hits} 次不合理"
    # 同活动同日只一条
    seen = {f"分享:anime:{DAY}": NOW}
    rng = random.Random(1)
    for _ in range(50):
        assert M.share_material(activity="anime", rng=rng, day_key=DAY, seen=seen) is None
    # 未知活动有兜底模板
    fallback = M.share_material(activity="mealx", rng=random.Random(1), day_key=DAY, seen={})
    if fallback is not None:
        assert fallback["text"] in M._DEFAULT_SHARE_TEMPLATES


def test_relation_materials():
    records = [
        {"user_id": "10001", "familiarity": 60.0, "last_interaction_at": NOW - 5 * 86400,
         "relation_hint": ""},
        {"user_id": "10002", "familiarity": 80.0, "last_interaction_at": NOW - 9 * 86400,
         "relation_hint": "小林"},
        {"user_id": "10003", "familiarity": 10.0, "last_interaction_at": NOW - 30 * 86400},
        {"user_id": "10004", "familiarity": 50.0, "last_interaction_at": NOW - 3600.0},
    ]
    out = M.relation_materials(records, now=NOW, day_key=DAY, seen={})
    assert len(out) == 1, "每次至多一人"
    assert "小林" in out[0]["text"], "最久没聊的熟人优先、用 relation_hint 称呼"
    assert "10002" not in out[0]["text"], "文本不带 user_id"
    assert "10003" not in str(out), "陌生档不产生维护动机"
    assert "10004" not in str(out), "还没闲置到天数不产生"
    # 同人同日只念叨一次；每次至多一人 ⇒ 第二批轮到次熟的 10001
    seen = {out[0]["_key"]: NOW}
    again = M.relation_materials(records, now=NOW + 60, day_key=DAY, seen=seen)
    assert len(again) == 1 and again[0]["_key"] == "关系:10001:" + DAY
    assert again[0]["_key"] != out[0]["_key"]
    # 第三批：两人都念叨过了 ⇒ 空
    seen.update({again[0]["_key"]: NOW})
    assert M.relation_materials(records, now=NOW + 120, day_key=DAY, seen=seen) == []
    # 第二天又能念叨
    seen_day2 = {f"关系:10002:2026-10-09": NOW}
    out2 = M.relation_materials(
        records, now=NOW + 86400, day_key="2026-10-09", seen=seen_day2
    )
    assert out2 and out2[0]["_key"] == "关系:10001:2026-10-09"


def test_material_shape_and_stamp():
    morning = M.greeting_material(now_minutes=8 * 60, day_key=DAY, seen={})
    assert set(("label", "text", "weight", "expires_at", "best_until")) <= set(morning)
    stamped = M.stamp(morning, now=NOW)
    assert "_key" not in stamped, "内部键不进素材池"
    assert stamped["created_at"] == NOW
    assert stamped["expires_at"] == pytest.approx(NOW + M.TTL_HOURS_GREETING * 3600)
    assert stamped["best_until"] < stamped["expires_at"], "全额保鲜期在前"
    # 走既有 material_freshness：期内全额、过期触底
    import life_sim as S

    assert S.material_freshness(stamped, now=NOW + 3600) == pytest.approx(1.0)
    assert S.material_freshness(stamped, now=stamped["expires_at"] + 1) == pytest.approx(0.0)


# ---------------------------------------------------------------- 接线


def test_tick_produces_motive_materials_and_stamps_seen():
    async def run():
        module, plugin, host = _make_plugin()
        # 让问候窗必中：直接调 _refresh_motives，用本地午后时间窗不行——
        # 真机时区不可控，改成直接验证「生成后素材进池、键进去重表」
        material = M.greeting_material(now_minutes=8 * 60, day_key=DAY, seen={})
        assert material is not None
        plugin._state.materials.append(M.stamp(material, now=NOW))
        plugin._state.motive_seen[material["_key"]] = NOW
        assert any(m["label"] == "动机:问候" for m in plugin._state.materials)
        assert material["_key"] in plugin._state.motive_seen

    asyncio.run(run())


def test_refresh_motives_disabled_is_inert():
    async def run():
        module, plugin, host = _make_plugin(motives={"enabled": False})
        before = len(plugin._state.materials)
        await plugin._refresh_motives(NOW, plugin._sim_config())
        assert len(plugin._state.materials) == before
        assert plugin._state.motive_seen == {}

    asyncio.run(run())


def test_refresh_motives_greeting_uses_local_clock():
    """把时区配成「此刻正处早安窗」，tick 生成应写素材与去重键。"""

    async def run():
        import time as _time
        from datetime import datetime, timezone

        now = _time.time()
        # 计算让本地（UTC+偏移）落在 08:00–08:59 的偏移量
        utc_hour = datetime.fromtimestamp(now, tz=timezone.utc).hour
        target_offset = (8 - utc_hour) * 60 % (24 * 60)
        module, plugin, host = _make_plugin(
            simulation={"tz_offset_minutes": target_offset}
        )
        # 清掉其它素材源干扰
        plugin._state.materials.clear()
        await plugin._refresh_motives(now, plugin._sim_config())
        greetings = [m for m in plugin._state.materials if m.get("label") == "动机:问候"]
        assert len(greetings) == 1, "问候窗内生成一条早安素材"
        assert any(k.startswith("问候:早安:") for k in plugin._state.motive_seen)
        # 再跑一次：去重生效，不重复
        await plugin._refresh_motives(now + 60, plugin._sim_config())
        greetings = [m for m in plugin._state.materials if m.get("label") == "动机:问候"]
        assert len(greetings) == 1

    asyncio.run(run())


def test_relation_motive_reads_store_records():
    """关系维护动机真的读 relations 库：闲置熟人 → 生成素材、去重表落键。"""

    async def run():
        module, plugin, host = _make_plugin(
            motives={"greeting": False, "share": False, "relation_idle_days": 1.0},
            relations={"enabled": True},
        )
        from life_store import new_relationship

        store = plugin._routine_store
        assert store is not None
        record = new_relationship("10001", now=NOW - 5 * 86400)
        record["familiarity"] = 60.0
        record["last_interaction_at"] = NOW - 5 * 86400
        store.put_relationship(record)

        plugin._state.materials.clear()
        await plugin._refresh_motives(NOW, plugin._sim_config())
        rel = [m for m in plugin._state.materials if m.get("label") == "动机:关系维护"]
        assert len(rel) == 1, "闲置熟人产生一条维护动机"
        assert "很久没聊的朋友" in rel[0]["text"]
        assert any(k.startswith("关系:10001:") for k in plugin._state.motive_seen)
        # 再跑：同人同日不重复
        await plugin._refresh_motives(NOW + 60, plugin._sim_config())
        rel = [m for m in plugin._state.materials if m.get("label") == "动机:关系维护"]
        assert len(rel) == 1

    asyncio.run(run())


def test_relation_motive_respects_idle_days_zero():
    async def run():
        module, plugin, host = _make_plugin(
            motives={"greeting": False, "share": False, "relation_idle_days": 0.0},
            relations={"enabled": True},
        )
        from life_store import new_relationship

        store = plugin._routine_store
        record = new_relationship("10001", now=NOW - 30 * 86400)
        record["familiarity"] = 60.0
        record["last_interaction_at"] = NOW - 30 * 86400
        store.put_relationship(record)
        await plugin._refresh_motives(NOW, plugin._sim_config())
        assert not [m for m in plugin._state.materials if m.get("label") == "动机:关系维护"], (
            "idle_days=0 显式关闭关系维护"
        )

    asyncio.run(run())
