# -*- coding: utf-8 -*-
"""L3：与 budget-pacer 的**真实**契约（跨插件，不是自说自话）。

本文件的独特价值：它不吃我自己写的假 payload，而是**加载邻居插件的真实源码**，
调它真正的 ``api_budget_status``，再把返回值喂给 ``life_economy.parse_budget_status``。
任何一侧改了字段名/语义，这里立刻红——单侧的假数据测不出这种漂移。

目录查找顺序：``LF_BUDGET_PACER_DIR`` → 同级目录 → 作者本机路径。
找不到就 SKIP（不算通过、也不算失败），与 ``test_host_model`` 的做法一致。

真机链路（读宿主源码得出）：

    插件 → ctx.api.call("插件ID.API名", version, **args)
         → cap.call(api.call) → 目标插件的 @API 处理器
         → 返回 {"success": True, "result": <我们的 dict>} → SDK 解包成 dict
    失败时返回 {"success": False, "error": ...} 且**不抛异常**。
"""

import asyncio
import importlib.util
import logging
import os
import pathlib
import sys
import types
from datetime import datetime

import pytest

import life_economy as E

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
API_NAME = "get_budget_status"


def _budget_pacer_entry_candidates() -> tuple[pathlib.Path, ...]:
    candidates: list[pathlib.Path] = []
    env = os.environ.get("LF_BUDGET_PACER_DIR", "").strip()
    if env:
        candidates.append(pathlib.Path(env))
    candidates.extend(
        [
            PLUGIN_DIR.parent / "budget-pacer",            # 同级目录（开发机）
            pathlib.Path("plugins") / "budget-pacer",      # 装进 MaiBot 的常见位置
        ]
    )
    return tuple(directory / "plugin.py" for directory in candidates)


def _load_budget_pacer():
    for entry in _budget_pacer_entry_candidates():
        if entry.is_file():
            spec = importlib.util.spec_from_file_location("budget_pacer_contract", entry)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    return None


@pytest.fixture(scope="module")
def budget_pacer():
    module = _load_budget_pacer()
    if module is None:
        pytest.skip("未找到 budget-pacer 源码（可用 LF_BUDGET_PACER_DIR 指定）")
    return module


class _FrequencySpy:
    def __init__(self):
        self.calls = []

    async def set_adjust(self, chat_id, value):
        self.calls.append((chat_id, value))
        return True


class _Statistics:
    def __init__(self, payload):
        self.payload = payload
        self.queries = 0

    async def model_trend(self, **kwargs):
        self.queries += 1
        return self.payload


class _Ctx:
    """够跑 ``api_budget_status`` 的最小宿主替身（只读路径不碰 send/db/chat）。"""

    def __init__(self, payload):
        self.logger = logging.getLogger("plugin.org.orge-8.budget-pacer")
        self.frequency = _FrequencySpy()
        self.statistics = types.SimpleNamespace(local=_Statistics(payload))


def _series_today(cost: float) -> dict:
    label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {"timestamps": [label], "values_by_key": {"m": [cost]}, "labels_by_key": {"m": "m"}}


def _make_budget_pacer(module, spend: float, *, budget_limit: float = 30.0):
    plugin = module.BudgetPacerPlugin()
    ctx = _Ctx(_series_today(spend))
    plugin._set_context(ctx)
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "1", "enabled": True},
            "budget": {"monthly_limit": budget_limit},
        }
    )
    return plugin, ctx


def test_the_api_is_declared_public_version_1(budget_pacer):
    """结构性契约：``@API`` 必须是 public + version 1，否则真机跨插件调用直接被拒。"""

    plugin, _ctx = _make_budget_pacer(budget_pacer, spend=1.0)
    apis = [item for item in plugin.get_components() if item.get("type") == "API"]
    assert apis, "budget-pacer 没有暴露任何 API 组件"
    entry = next((item for item in apis if item["name"] == API_NAME), None)
    assert entry is not None, [item["name"] for item in apis]
    metadata = entry.get("metadata") or {}
    assert metadata.get("public") is True, "非 public 的 API 跨插件不可见（components.py:244）"
    assert str(metadata.get("version")) == "1"
    assert metadata.get("handler_name") == "api_budget_status"


def test_real_api_output_is_accepted_by_life_economy(budget_pacer):
    """端到端：真 API 的返回值 → 解析 → 分档 → 提示词，全链路对得上。"""

    async def run():
        plugin, ctx = _make_budget_pacer(budget_pacer, spend=12.5)
        payload = await plugin.api_budget_status(refresh=True, stale_after_seconds=0)
        assert isinstance(payload, dict) and payload.get("ok") is True

        snap = E.parse_budget_status(payload, fetched_at=__import__("time").time())
        assert snap.ok is True, f"life-frequency 认不出 budget-pacer 的返回：{snap.reason} {snap.error}"
        assert snap.budget == pytest.approx(30.0)
        assert snap.spend == pytest.approx(12.5)
        assert snap.today_spend == pytest.approx(12.5)
        assert snap.remaining == pytest.approx(17.5)
        assert snap.active is True
        assert E.economy_tier(snap) == E.TIER_NORMAL, E.economy_tier(snap)
        assert ctx.frequency.calls == [], "只读 API 竟然下发了倍率"

    asyncio.run(run())


def test_real_api_over_budget_maps_to_broke_and_a_frugal_hint(budget_pacer):
    """超支时 life-frequency 必须判成「见底」并给出省钱约束。"""

    async def run():
        plugin, ctx = _make_budget_pacer(budget_pacer, spend=31.0)
        payload = await plugin.api_budget_status(refresh=True)
        snap = E.parse_budget_status(payload, fetched_at=__import__("time").time())
        assert snap.remaining < 0, payload
        assert E.economy_tier(snap) == E.TIER_BROKE
        hint = E.frugal_hint(snap, E.TIER_BROKE)
        assert "泡面" in hint and "不会主动" in hint
        assert ctx.frequency.calls == []

    asyncio.run(run())


def test_real_api_reports_inactive_when_budget_is_zero(budget_pacer):
    """预算为 0（插件不调节）时，life-frequency 不能把它当成「余额充足的穷日子」。"""

    async def run():
        plugin, _ctx = _make_budget_pacer(budget_pacer, spend=5.0, budget_limit=0.0)
        payload = await plugin.api_budget_status(refresh=True)
        snap = E.parse_budget_status(payload, fetched_at=__import__("time").time())
        assert snap.ok is True
        assert snap.active is False
        assert E.economy_tier(snap) == E.TIER_UNKNOWN
        assert E.frugal_hint(snap, E.economy_tier(snap)) == ""

    asyncio.run(run())
