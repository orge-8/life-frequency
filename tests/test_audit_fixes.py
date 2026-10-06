# -*- coding: utf-8 -*-
"""v1.8.2 审计修复回归。

每条用例都对应审计中发现并**复现过**的缺陷（复现脚本在修复前全红、修复后全绿）：

- M1/L1  状态文件 NaN/Infinity：float 字段不收口 ⇒ 生活状态静默冻结；
          int 字段 OverflowError 穿透 from_dict 的 except ⇒ 插件加载失败
- L3     运行中时钟回拨：settle 静默冻结、无日志
- L4     skip_ledger / sessions 值类型不清洗：消费点炸 / 硬闸静默失效
- L2     病态小频率：trigger_threshold 抛 ZeroDivision/OverflowError
- L6     单行代码围栏：一次有效决策被丢弃
- L7     事件 DSL activities 不校验白名单：零告警死配置
- L8     bool("false")==True：上游 bool 序列化成字符串时产出假事件
- 补丁   inject_life_context 无分隔拼接；敌意状态文件整体不炸
"""

from __future__ import annotations

import logging
import math
import pathlib
import sys

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import (  # noqa: E402
    FakeHost,
    FakePaths,
    bind_context,
    build_context,
    get_default_config,
    load_plugin_module,
)

import life_activity  # noqa: E402  （conftest 已把插件目录放进 sys.path）
import life_host_model  # noqa: E402
import life_sim  # noqa: E402
import life_world  # noqa: E402
from life_sim import LifeState, SimConfig, new_state, settle  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _config() -> SimConfig:
    return SimConfig()


def _make_plugin(**config_overrides):
    module = load_plugin_module(PLUGIN_DIR, "life_frequency_audit_fixes")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    for section, values in config_overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    return module, plugin, host


class _Capture:
    """抓插件日志（root 的 WARNING 级别会挡掉 INFO，必须自己设级别）。"""

    def __init__(self):
        self.records: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: self.records.append(record.getMessage())
        handler.setLevel(logging.DEBUG)
        self._handler = handler
        self._logger = logging.getLogger("plugin.org.orge-8.life-frequency")
        self._logger.addHandler(handler)
        self._logger.setLevel(logging.DEBUG)

    def stop(self):
        self._logger.removeHandler(self._handler)


# ---------------------------------------------------------------- M1/L1: from_dict 数值收口


def test_from_dict_rejects_nonfinite_float_fields():
    state = LifeState.from_dict(
        {
            "last_tick_at": float("nan"),
            "cold_until": float("inf"),
            "llm_last_success_at": float("nan"),
            "llm_cooldown_until": float("inf"),
            "inertia_until": float("nan"),
            "activity_since": float("inf"),
            "sleep_started_at": float("nan"),
        }
    )
    for name in (
        "last_tick_at",
        "cold_until",
        "llm_last_success_at",
        "llm_cooldown_until",
        "inertia_until",
        "activity_since",
        "sleep_started_at",
    ):
        value = float(getattr(state, name))
        assert math.isfinite(value), f"{name} 应被收口为有限值，实际 {value!r}"
    assert state.last_tick_at == 0.0  # 回到默认值，而不是保留 NaN


def test_from_dict_rejects_bool_in_numeric_fields():
    state = LifeState.from_dict({"emotion": True, "sleep_debt_nights": True})
    assert state.emotion != 1.0  # bool 不是 1.0
    assert state.sleep_debt_nights == 0


def test_from_dict_survives_infinity_and_huge_int():
    # int(inf) 抛 OverflowError（不在旧 except 名单里）→ 旧版插件直接加载失败
    state = LifeState.from_dict(
        {"sleep_debt_nights": float("inf"), "state_version": 10**400}
    )
    assert state.sleep_debt_nights == 0
    assert state.state_version == int(LifeState().state_version)


def test_nan_last_tick_at_self_heals_in_settle():
    """NaN 锚点的第二道闸：即使绕过 from_dict 混进来，settle 也必须自愈。"""
    state = new_state(now=1000.0, config=_config())
    state.activity = "daily"
    state.last_tick_at = float("nan")
    out = settle(state, now=2000.0, config=_config())
    assert out.last_tick_at == 2000.0
    out = settle(state, now=3000.0, config=_config())
    assert out.awake_minutes_today > 0  # 记账真的在推进


def test_nan_llm_success_anchor_does_not_kill_reseed_valve():
    """M1c 第二道闸：NaN 是真值，``nan or activity_since`` 会选中 NaN ⇒ 安全阀失效。

    from_dict 在入口拦 NaN；这里模拟绕过入口的脏值，安全阀必须退到 activity_since。
    """
    state = new_state(now=1000.0, config=_config())
    state.activity_source = "llm"
    state.llm_last_success_at = float("nan")
    assert life_sim.should_reseed(state, now=1e9, hours=0.1) is True


# ---------------------------------------------------------------- L3: 时钟回拨


def test_settle_resets_anchor_on_clock_rollback():
    state = new_state(now=3000.0, config=_config())
    state.activity = "daily"
    state.last_tick_at = 3000.0
    # 墙钟回到 2000（回拨 1000 秒）
    rolled: list[int] = []
    out = settle(state, now=2000.0, config=_config(),
                 on_clock_rollback=rolled.append)
    assert out.last_tick_at == 2000.0  # 锚点被重置（旧版保持 3000 → 永久冻结）
    assert rolled and rolled[0] == 16  # 留痕回调（约 16 分钟）
    # 之后恢复正常推进
    out2 = settle(state, now=2600.0, config=_config())
    assert out2.awake_minutes_today > 0


def test_settle_small_rollback_resets_without_log():
    """毫秒级 NTP 抖动：锚点照样重置，但不打日志（阈值 60 秒）。"""
    state = new_state(now=2000.0, config=_config())
    state.last_tick_at = 2030.0  # 回拨 30 秒
    rolled: list[int] = []
    out = settle(state, now=2000.0, config=_config(),
                 on_clock_rollback=rolled.append)
    assert out.last_tick_at == 2000.0
    assert not rolled


# ---------------------------------------------------------------- L4: skip_ledger / sessions 净化


def test_from_dict_sanitizes_skip_ledger():
    state = LifeState.from_dict({"skip_ledger": {"sent": "abc", "ok": 2, "bad": True}})
    assert state.skip_ledger == {"ok": 2}


def test_from_dict_sanitizes_sessions():
    state = LifeState.from_dict(
        {
            "sessions": {
                "not-a-dict": "garbage",
                "ok": {"last_user_message_at": float("nan"), "count": "x", "day_key": "d"},
            }
        }
    )
    assert "not-a-dict" not in state.sessions
    record = state.sessions["ok"]
    assert record["last_user_message_at"] == 0.0
    assert record["count"] == 0
    assert record["day_key"] == "d"


# ---------------------------------------------------------------- L2: 病态小频率


@pytest.mark.parametrize(
    ("mode", "frequency"),
    [("reply_necessity", 1e-170), ("reply_necessity", 1e-160), ("frequency", 5e-324)],
)
def test_trigger_threshold_pathological_frequency_is_finite(mode, frequency):
    value = life_host_model.trigger_threshold(mode, frequency)
    assert isinstance(value, int) and value >= 1


# ---------------------------------------------------------------- L6: 单行代码围栏


def test_single_line_code_fence_parses():
    decision = life_activity.parse_response('```{"activity": "daily", "scene": "x"}```')
    assert decision is not None and decision.activity == "daily"


def test_multiline_code_fence_still_parses():
    decision = life_activity.parse_response(
        '```json\n{"activity": "music", "scene": "ok"}\n```'
    )
    assert decision is not None and decision.activity == "music"


def test_bare_fence_language_tag_only_fails_closed():
    assert life_activity.parse_response("```json") is None


# ---------------------------------------------------------------- L7: activities 白名单告警


def test_is_known_activity_covers_aliases():
    assert life_activity.is_known_activity("work")
    assert life_activity.is_known_activity("sleep")
    # 别名口径与 normalize_activity 一致：能被 normalize 认出的写法都算已知
    for alias in ("睡觉", "工作", "上班"):
        normalized = life_activity.normalize_activity(alias)
        if normalized is not None:
            assert life_activity.is_known_activity(alias), alias
    assert not life_activity.is_known_activity("workk")
    assert not life_activity.is_known_activity("")


def test_plugin_warns_on_unknown_activity():
    module, plugin, _host = _make_plugin(events={"extra": ["期中考试|activities=workk|emotion=-1"]})
    capture = _Capture()
    try:
        plugin._rebuild_from_config()
    finally:
        capture.stop()
    assert any("workk" in text for text in capture.records), capture.records
    assert any("期中考试" in text for text in capture.records)


def test_plugin_stays_quiet_on_known_activities():
    module, plugin, _host = _make_plugin(events={"extra": ["散步|activities=daily|emotion=0.5"]})
    capture = _Capture()
    try:
        plugin._rebuild_from_config()
    finally:
        capture.stop()
    assert not any("已知活动" in text for text in capture.records)


# ---------------------------------------------------------------- L8: 世界源严格布尔


def test_world_sources_use_strict_bool():
    payload = {
        "schema_version": "1",
        "active": "false",
        "is_live": "true",
        "anchor_name": "UP",
        "live_status_name": "直播中",
    }
    status = life_world.parse_live_status(payload, fetched_at=1000.0)
    assert status.active is False  # bool("false") 陷阱：旧版判 True
    assert status.is_live is True  # 明确写法仍然认
    assert life_world.parse_live_status({"schema_version": "1", "is_live": "false"}).is_live is False
    assert life_world.parse_live_status({"schema_version": "1", "is_live": True}).is_live is True


def test_world_events_skip_unlive_upstream():
    status = life_world.parse_live_status(
        {"schema_version": "1", "active": "true", "is_live": "false"}, fetched_at=1.0
    )
    assert life_world.world_events(live=status, now=1000.0) == []


# ---------------------------------------------------------------- 敌意状态文件整体不炸


def test_from_dict_survives_full_hostile_payload():
    hostile = {
        "state_version": 999,
        "emotion": float("nan"),
        "energy": "not-a-number",
        "activity": {"malicious": True},
        "scene": "x" * 100000,
        "events": ["not-a-dict", {"at": "soon", "emotion": 1e300}],
        "materials": [{"text": None, "at": -1}],
        "sessions": {"k": "not-a-dict"},
        "applied": {"sid": "huge-value"},
        "recent_events": list(range(100000)),
        "skip_ledger": {"sent": "abc"},
    }
    state = LifeState.from_dict(hostile)
    assert math.isfinite(state.emotion) and math.isfinite(state.energy)
    assert state.activity in life_sim.ALLOWED_ACTIVITIES
    assert len(state.scene) <= 2000
    assert state.sessions == {}
    assert state.skip_ledger == {}


# ---------------------------------------------------------------- 注入分隔（补丁）


def test_inject_life_context_inserts_separator():
    import asyncio

    module, plugin, _host = _make_plugin()

    async def run():
        result = await plugin.inject_life_context(extra_prompt="已有提示词末尾")
        merged = (result.get("modified_kwargs") or {}).get("extra_prompt", "")
        assert merged.startswith("已有提示词末尾\n【")
        # 空原文时不引入前导空行
        result2 = await plugin.inject_life_context(extra_prompt="")
        merged2 = (result2.get("modified_kwargs") or {}).get("extra_prompt", "")
        assert merged2.startswith("【她现在的生活")

    asyncio.run(run())


def test_inject_life_context_respects_limit_with_separator():
    import asyncio

    module, plugin, _host = _make_plugin(prompt={"max_chars": 600})

    async def run():
        result = await plugin.inject_life_context(extra_prompt="x" * 500)
        merged = (result.get("modified_kwargs") or {}).get("extra_prompt", "")
        assert len(merged) <= 600
        assert merged.startswith("x" * 500 + "\n【")

    asyncio.run(run())
