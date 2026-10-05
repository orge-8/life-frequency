# -*- coding: utf-8 -*-
"""L3：与 bilibili-live-gateway 的**真实**契约（跨插件，不是自说自话）。

加载邻居插件的真实源码、调它真正的 ``api_get_live_status``，再把返回值喂给
``life_world.parse_live_status`` / ``world_events``。任何一侧改字段名/语义，这里立刻红。

四组断言（计划 §4.3）：**正常返回 / 空数据降级 / 坏数据降级 / 只读性**。
只读性是本插件最在意的一条：life-frequency 每推进一轮就读一次，
API 若顺手去 HTTP 探测 B 站，等于把对方的风控放大上百倍。

目录查找顺序：``LF_BILI_LIVE_DIR`` → 同级目录 → ``plugins/<名>`` → 作者本机路径；
找不到就 SKIP（不算通过、也不算失败）。
"""

import asyncio
import importlib.util
import logging
import os
import pathlib
import sys

import pytest

import life_world as W

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
UPSTREAM = "bilibili-live-gateway"
API_NAME = "get_live_status"


def _entry_candidates() -> tuple[pathlib.Path, ...]:
    candidates: list[pathlib.Path] = []
    env = os.environ.get("LF_BILI_LIVE_DIR", "").strip()
    if env:
        candidates.append(pathlib.Path(env))
    candidates.extend(
        [
            PLUGIN_DIR.parent / UPSTREAM,
            pathlib.Path("plugins") / UPSTREAM,
            pathlib.Path(r"C:\path\to") / UPSTREAM,
        ]
    )
    return tuple(directory / "plugin.py" for directory in candidates)


_blg_livegate = None


def _load_upstream():
    """加载上游 ``plugin.py``。

    ⚠ **上游目录里也有 ``plugin.py``**，而本仓库的 ``tests/test_plugin_helpers.py`` /
    ``test_interop.py`` 会 ``import plugin as P``。所以上游目录只能**临时**进 sys.path：
    加载完（连同它的兄弟模块句柄）立刻撤掉，否则全量测试会互相污染。
    """
    global _blg_livegate

    for entry in _entry_candidates():
        if not entry.is_file():
            continue
        directory = str(entry.parent)
        sys.path.insert(0, directory)
        try:
            spec = importlib.util.spec_from_file_location("blg_contract_under_test", entry)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)

            # 兄弟模块（blg_livegate）只在路径有效时可导入 —— 趁现在取到句柄
            import blg_livegate as _gate_module  # noqa: PLC0415

            _blg_livegate = _gate_module
            return module
        finally:
            try:
                sys.path.remove(directory)
            except ValueError:
                pass
    return None


@pytest.fixture(scope="module")
def gateway():
    module = _load_upstream()
    if module is None:
        pytest.skip(f"未找到 {UPSTREAM} 源码（可用 LF_BILI_LIVE_DIR 指定）")
    return module


class _ExplodingAuth:
    """一碰就炸的 auth 桩：API 若发起任何探测，测试当场失败（零网络红线）。"""

    def __init__(self):
        self.touched: list[str] = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _boom(*args, **kwargs):
            self.touched.append(name)
            raise AssertionError(f"只读 API 不允许触发 auth.{name}")

        return _boom


class _Fetcher:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    async def __call__(self, room_id):
        self.calls += 1
        if isinstance(self.value, BaseException):
            raise self.value
        return {"live_status": self.value, "title": "测试直播", "room_id": room_id}


def _make_gateway(gateway_module, *, room: dict, fetcher=None, live_statuses=(1,)):
    plugin = gateway_module.create_plugin()
    plugin.resolved_room = dict(room)
    if fetcher is None:
        plugin._gate = None
        return plugin

    gate = _blg_livegate.LiveGate(
        room_id=2233, fetch_status=fetcher, live_statuses=live_statuses, logger=None
    )
    plugin._gate = gate
    return plugin


ROOM = {"room_id": 2233, "anchor_uid": 99, "anchor_name": "测试主播"}


def test_the_api_is_declared_public_version_1(gateway):
    """结构性契约：``@API`` 必须 public + version 1，否则真机跨插件调用直接被拒。"""

    plugin = _make_gateway(gateway, room=ROOM)
    apis = [item for item in plugin.get_components() if item.get("type") == "API"]
    entry = next((item for item in apis if item["name"] == API_NAME), None)
    assert entry is not None, [item["name"] for item in apis]
    metadata = entry.get("metadata") or {}
    assert metadata.get("public") is True
    assert str(metadata.get("version")) == "1"
    assert metadata.get("handler_name") == "api_get_live_status"


def test_real_api_live_maps_to_world_event(gateway):
    """正常返回：直播中 → 开播事件（标签/键/情绪），且**没碰 auth**。"""

    async def run():
        plugin = _make_gateway(gateway, room=ROOM, fetcher=_Fetcher(1))
        auth = _ExplodingAuth()
        plugin._auth = auth
        await plugin._gate.check_now()

        payload = await plugin.api_get_live_status()
        assert payload["is_live"] is True and payload["live_status"] == 1

        status = W.parse_live_status(payload, fetched_at=payload["updated_at"])
        assert status.ok and status.active and status.anchor_name == "测试主播"
        events = W.world_events(live=status, now=payload["updated_at"], tz_offset_minutes=480)
        assert [e.kind for e in events] == [W.KIND_LIVE]
        assert events[0].label == "测试主播 开播了"
        assert events[0].key.startswith("live!2233!")
        assert events[0].emotion == pytest.approx(0.20)
        assert auth.touched == [], "只读 API 触发了 auth"

    asyncio.run(run())


def test_real_api_unconfigured_room_degrades(gateway):
    """空数据降级：未配置房间 → active=False + reason，且零网络、零事件。"""

    async def run():
        plugin = _make_gateway(gateway, room={})
        auth = _ExplodingAuth()
        plugin._auth = auth
        payload = await plugin.api_get_live_status()

        assert payload["active"] is False and payload["reason"] == "live.room_id 未配置"
        status = W.parse_live_status(payload)
        assert status.ok and not status.active and status.reason == "live.room_id 未配置"
        assert W.world_events(live=status, now=1000.0) == []
        assert auth.touched == []

    asyncio.run(run())


def test_probe_failure_is_not_reported_as_live(gateway):
    """探测失败：errors/error_hint 如实上报，不误报开播（对方闸门最容易写错的一条）。"""

    async def run():
        plugin = _make_gateway(gateway, room=ROOM, fetcher=_Fetcher(RuntimeError("风控")))
        await plugin._gate.check_now()
        payload = await plugin.api_get_live_status()

        assert payload["errors"] == 1 and payload["error_hint"] != ""
        assert payload["is_live"] is False and payload["live_status"] is None
        status = W.parse_live_status(payload)
        assert W.world_events(live=status, now=payload["updated_at"]) == []

    asyncio.run(run())


def test_bad_payload_degrades_without_raising():
    """坏数据降级：SDK 失败字典 / 非 dict / 缺字段都不许抛，事件为空。"""

    for label, bad in (
        ("SDK 失败字典", {"success": False, "error": "找不到 API get_live_status"}),
        ("None", None),
        ("列表", [1, 2]),
        ("缺 schema_version", {"active": True, "is_live": True}),
    ):
        status = W.parse_live_status(bad)
        assert status.ok is False, label
        assert status.reason in ("bad_payload", "api_error"), (label, status.reason)
        assert W.world_events(live=status, now=1000.0) == [], label


def test_readonly_repeated_calls_do_not_change_upstream_state(gateway):
    """只读性：连读多次，闸门计数与回调记录不变。"""

    async def run():
        plugin = _make_gateway(gateway, room=ROOM, fetcher=_Fetcher(1))
        auth = _ExplodingAuth()
        plugin._auth = auth
        await plugin._gate.check_now()
        checks, errors = plugin._gate.checks, plugin._gate.errors

        for _ in range(3):
            payload = await plugin.api_get_live_status()
            assert payload["live_status"] == 1

        assert (plugin._gate.checks, plugin._gate.errors) == (checks, errors)
        assert auth.touched == []

    asyncio.run(run())
