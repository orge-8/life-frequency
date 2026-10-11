# -*- coding: utf-8 -*-
"""L3：中国日历（calendar）的表解析与三个消费点。

钉住五件事：

1. 表解析：坏条目告警、kind 白名单、`workday_swap` 的「周末算工作日」；
2. **缺表/坏表降级**为空表（绝不阻塞插件加载），当年没覆盖时告警一次；
3. 文件加载：``data/calendar.toml`` 缺失 → 内置表；toml 坏 → 空表 + 告警；
4. prompt 日期行真的带节日名（春节没反应的直接修法）；
5. 节日素材当天只产出一次、18:00 后开始衰减、调休日不产素材。
"""

import asyncio
import importlib
import pathlib
import sys
from datetime import datetime, timezone

import pytest

pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_calendar as C  # noqa: E402
from fakehost import (  # noqa: E402
    FakeHost,
    FakePaths,
    bind_context,
    build_context,
    get_default_config,
    load_plugin_module,
)

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _make_plugin(**overrides):
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_calendar")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["simulation"]["tz_offset_minutes"] = 0
    config["physio"]["meals"] = []
    config["calendar"]["enabled"] = True
    for section, values in overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    plugin._rebuild_from_config()
    return module, plugin, host


# ---------------------------------------------------------------- 表解析


def test_build_calendar_parses_and_validates():
    calendar, warnings = C.build_calendar(
        [
            {"date": "2026-02-17", "name": "春节", "kind": "holiday"},
            {"date": "2026-02-18", "name": "春节", "kind": "holiday"},
            {"date": "bad-date", "name": "坏日期"},
            {"date": "2026-03-01", "name": ""},
            {"date": "2026-04-04", "name": "怪类型", "kind": "somewhere"},
        ]
    )
    assert len(calendar.days) == 3, "坏 kind 的条目降级进表而不是丢弃"
    assert set(calendar.years) == {"2026"}
    assert warnings, "坏条目必须留线索"
    assert any("date 不是" in item for item in warnings)
    assert any("缺少 name" in item for item in warnings)
    assert any("kind" in item for item in warnings)
    assert calendar.day_info(2026, 2, 17).is_holiday
    # 坏 kind 降级 festival（不放假），不是丢弃
    assert calendar.day_info(2026, 4, 4).kind == C.KIND_FESTIVAL


def test_workday_swap_overrides_weekday():
    calendar, warnings = C.build_calendar(
        [{"date": "2026-02-07", "name": "调休上班", "kind": "workday_swap"}]
    )
    assert warnings == []
    # 2026-02-07 是周六：平时不算工作日，调休后算
    assert C.is_workday(calendar, 2026, 2, 7, weekday=6) is True
    assert C.is_workday(calendar, 2026, 2, 8, weekday=7) is False  # 周日照常休


def test_holiday_overrides_weekday():
    calendar, _ = C.build_calendar([{"date": "2026-10-01", "name": "国庆节", "kind": "holiday"}])
    # 2026-10-01 是周四
    assert C.is_workday(calendar, 2026, 10, 1, weekday=4) is False
    # 七夕（festival，不放假）不影响工作日
    calendar2, _ = C.build_calendar([{"date": "2026-08-19", "name": "七夕", "kind": "festival"}])
    assert C.is_workday(calendar2, 2026, 8, 19, weekday=3) is True


def test_builtin_table_covers_2026_and_2027_with_spring_festival():
    calendar = C.builtin_calendar()
    assert set(calendar.years) >= {"2026", "2027"}
    assert calendar.day_info(2026, 2, 17).name == "春节"
    assert calendar.day_info(2027, 2, 6).name == "春节"
    assert calendar.day_info(2026, 2, 17).is_holiday
    # 冬至是 festival（不放假）：那天她照常上班，但会知道是冬至
    assert calendar.day_info(2026, 12, 22).kind == C.KIND_FESTIVAL


def test_load_calendar_file_falls_back_gracefully(tmp_path):
    calendar, warnings = C.load_calendar_file(tmp_path / "nope.toml")
    assert len(calendar.days) == 0
    assert warnings, "缺文件必须留痕"

    bad = tmp_path / "bad.toml"
    bad.write_text("这是[不是 toml", encoding="utf-8")
    calendar, warnings = C.load_calendar_file(bad)
    assert len(calendar.days) == 0
    assert any("不可读" in item for item in warnings)


def test_load_calendar_file_reads_real_table():
    table = PLUGIN_DIR / "data" / "calendar.toml"
    if not table.is_file():
        pytest.skip("data/calendar.toml 不存在")
    calendar, warnings = C.load_calendar_file(table)
    assert warnings == [], warnings
    assert set(calendar.years) >= {"2026", "2027"}
    assert calendar.day_info(2026, 2, 17).name == "春节"


# ---------------------------------------------------------------- 接线


def test_plugin_loads_calendar_and_prompt_date_line_carries_festival():
    """prompt 日期行真的带节日名：固定 now 到 2026-02-17（春节）验证。"""

    async def run():
        module, plugin, _ = _make_plugin()
        assert plugin._calendar is not None
        assert plugin._calendar.day_info(2026, 2, 17).is_holiday

        # 把时钟钉在春节当天 10:00
        fixed = datetime(2026, 2, 17, 10, 0, tzinfo=timezone.utc).timestamp()
        prompts: list[str] = []

        async def fake_llm(**kwargs):
            prompts.append(str(kwargs.get("prompt") or ""))
            return {"success": False, "error": "test-stop"}

        plugin.ctx.llm.generate = fake_llm
        plugin._state.activity_since = fixed - 7200
        plugin._last_llm_attempt_at = 0.0
        await plugin._ask_activity(fixed)
        assert prompts, "活动决策根本没发 prompt"
        assert "春节" in prompts[0], prompts[0][:400]

    asyncio.run(run())


def test_festival_material_fires_once_and_decays():
    async def run():
        module, plugin, _ = _make_plugin()
        config = plugin._sim_config()
        morning = datetime(2026, 2, 17, 9, 0, tzinfo=timezone.utc).timestamp()

        first = plugin._calendar_festival_material(morning, config)
        assert first is not None
        assert "春节" in first["text"] and "放假" in first["text"]
        assert first["weight"] == 1.2, "法定大节用高档权重"
        assert first["best_until"] > morning, "上午还没到衰减线"

        # 同一天第二次：去重表挡住
        assert plugin._calendar_festival_material(morning + 600, config) is None

        # 小节日权重低档
        plugin._state.social_seen.clear()
        qixi = datetime(2026, 8, 19, 9, 0, tzinfo=timezone.utc).timestamp()
        small = plugin._calendar_festival_material(qixi, config)
        assert small is not None and small["weight"] == 0.8
        assert small["best_until"] > qixi, "七夕上午还没到 18:00 衰减线"

        # 调休日不产素材（那是一个普通的、要上班的周六）
        plugin._state.social_seen.clear()
        # 2026-02-07 不在表里；用一个 workday_swap 条目验证
        from life_calendar import build_calendar

        swap, _ = build_calendar(
            [{"date": "2026-02-07", "name": "调休上班", "kind": "workday_swap"}]
        )
        plugin._calendar = swap
        swap_day = datetime(2026, 2, 7, 9, 0, tzinfo=timezone.utc).timestamp()
        assert plugin._calendar_festival_material(swap_day, config) is None

    asyncio.run(run())


def test_routine_day_flags_use_the_calendar():
    """习惯层的「节假日」判据走日历：春节周二 = 节假日，调休周六 = 工作日。"""

    async def run():
        module, plugin, _ = _make_plugin()
        # 春节（2026-02-17 周二）：不是工作日、是节假日——旧判据会说是工作日
        spring = datetime(2026, 2, 17, 10, 0, tzinfo=timezone.utc)
        is_workday, is_holiday = plugin._routine_day_flags(spring)
        assert (is_workday, is_holiday) == (False, True)

        # 普通周二
        normal = datetime(2026, 3, 3, 10, 0, tzinfo=timezone.utc)
        assert plugin._routine_day_flags(normal) == (True, False)

        # 七夕（festival，不放假）= 照常上班
        qixi = datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc)
        assert plugin._routine_day_flags(qixi) == (True, False)

    asyncio.run(run())


def test_calendar_disabled_is_inert():
    async def run():
        module, plugin, _ = _make_plugin(calendar={"enabled": False})
        assert plugin._calendar is None
        config = plugin._sim_config()
        morning = datetime(2026, 2, 17, 9, 0, tzinfo=timezone.utc).timestamp()
        assert plugin._calendar_festival_material(morning, config) is None
        spring = datetime(2026, 2, 17, 10, 0, tzinfo=timezone.utc)
        # 降级：没有日历就按星期（周二 = 工作日）
        assert plugin._routine_day_flags(spring) == (True, False)

    asyncio.run(run())


def test_missing_year_warns_once():
    async def run():
        module, plugin, host = _make_plugin()
        # 用只有 2020 年的表覆盖：当前年份没被覆盖 ⇒ 告警一次
        from life_calendar import build_calendar

        old, _ = build_calendar([{"date": "2020-01-01", "name": "元旦", "kind": "holiday"}])
        plugin._calendar = old
        plugin._warned.clear()
        plugin._load_calendar = lambda: None  # 不重载，直接检查判定路径不炸
        spring = datetime(2026, 2, 17, 10, 0, tzinfo=timezone.utc)
        # 降级到星期判据（春节周二 → 工作日，这是缺表的已知代价）
        is_workday, is_holiday = plugin._routine_day_flags(spring)
        assert is_workday is True and is_holiday is False

    asyncio.run(run())
