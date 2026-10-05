# -*- coding: utf-8 -*-
"""L3：与 group-welcome 的**真实**契约（跨插件）。

加载它的真实源码、调真正的 ``api_get_recent_newcomers``，喂给
``life_world.parse_recent_newcomers`` / ``world_events``。

**本文件还要钉住一条数据边界**：``seen_members.json`` 只落盘 QQ 号 + 首次出现时间戳，
昵称从不缓存 ⇒ 事件只能写「群里来了个新人」，**不能凭空造出昵称**。
这是上游 README 明文声明的边界，不是可以「优化」掉的东西。

目录查找顺序：``LF_GROUP_WELCOME_DIR`` → 同级目录 → ``plugins/<名>`` → 作者本机路径。
"""

import asyncio
import importlib.util
import logging
import os
import pathlib
import sys
import types

import pytest

import life_world as W

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
UPSTREAM = "group-welcome"
API_NAME = "get_recent_newcomers"


def _entry_candidates() -> tuple[pathlib.Path, ...]:
    candidates: list[pathlib.Path] = []
    env = os.environ.get("LF_GROUP_WELCOME_DIR", "").strip()
    if env:
        candidates.append(pathlib.Path(env))
    candidates.extend(
        [
            PLUGIN_DIR.parent / UPSTREAM,
            pathlib.Path("plugins") / UPSTREAM,
        ]
    )
    return tuple(directory / "plugin.py" for directory in candidates)


def _load_upstream():
    """加载上游 ``plugin.py``；上游目录只**临时**进 sys.path。

    ⚠ 上游目录里也有 ``plugin.py``，而本仓库的 ``tests/test_plugin_helpers.py`` /
    ``test_interop.py`` 会 ``import plugin as P`` —— 留在 sys.path 上会互相污染。
    """
    for entry in _entry_candidates():
        if not entry.is_file():
            continue
        directory = str(entry.parent)
        sys.path.insert(0, directory)
        try:
            spec = importlib.util.spec_from_file_location("group_welcome_contract_under_test", entry)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
        finally:
            try:
                sys.path.remove(directory)
            except ValueError:
                pass
    return None


@pytest.fixture(scope="module")
def welcome():
    module = _load_upstream()
    if module is None:
        pytest.skip(f"未找到 {UPSTREAM} 源码（可用 LF_GROUP_WELCOME_DIR 指定）")
    return module


class _Ctx:
    """够跑只读 handler 的最小宿主替身（不需要 fakehost：本用例不碰任何能力）。

    只读性用例需要落盘状态，所以可选地给一个 ``paths.data_dir``
    （真实的 ``_state_path()`` 走 ``self.ctx.paths.data_dir``）。
    """

    def __init__(self, data_dir=None):
        self.logger = logging.getLogger("plugin.org.orge-8.group-welcome")
        if data_dir is not None:
            self.paths = types.SimpleNamespace(data_dir=str(data_dir))


def _make_plugin(welcome_module, *, seen: dict | None = None, loaded: bool = True, data_dir=None):
    plugin = welcome_module.create_plugin()
    plugin._set_context(_Ctx(data_dir))
    plugin.set_plugin_config({})
    if loaded:
        plugin._loaded_at = 1.0
    if seen:
        plugin._seen.update(seen)
    return plugin


def test_the_api_is_declared_public_version_1(welcome):
    """结构性契约：public + version 1 + handler 名正确。"""

    plugin = _make_plugin(welcome)
    apis = [item for item in plugin.get_components() if item.get("type") == "API"]
    entry = next((item for item in apis if item["name"] == API_NAME), None)
    assert entry is not None, [item["name"] for item in apis]
    metadata = entry.get("metadata") or {}
    assert metadata.get("public") is True
    assert str(metadata.get("version")) == "1"
    assert metadata.get("handler_name") == "api_get_recent_newcomers"


def test_real_api_newcomers_map_to_world_events(welcome):
    """正常返回：两个新人 → 两条事件，且**没有昵称**（数据边界）。"""

    async def run():
        now = 1791174000.0
        plugin = _make_plugin(
            welcome,
            seen={"123456": {"10001": now - 600, "10002": now - 1200}},
        )
        payload = await plugin.api_get_recent_newcomers(since_seconds=86400)

        source = W.parse_recent_newcomers(payload, fetched_at=now)
        assert source.ok and len(source.items) == 2
        events = W.world_events(newcomers=source, now=now, tz_offset_minutes=480)
        assert [e.kind for e in events] == [W.KIND_NEWCOMER, W.KIND_NEWCOMER]
        assert {e.key for e in events} == {
            "newcomer!123456!10001", "newcomer!123456!10002"
        }
        # 数据边界：标签固定，不含任何昵称；正文里只有群号与 QQ 号
        assert all(e.label == "群里来了个新人" for e in events)
        assert all("10001" in e.text or "10002" in e.text for e in events)

    asyncio.run(run())


def test_empty_archive_degrades(welcome):
    """空数据降级：档案为空 → groups={} + reason，零事件。"""

    async def run():
        plugin = _make_plugin(welcome)
        payload = await plugin.api_get_recent_newcomers()
        assert payload["groups"] == {} and payload["reason"] == "暂无任何成员记录"

        source = W.parse_recent_newcomers(payload)
        assert source.ok and not source.items
        assert W.world_events(newcomers=source, now=1000.0) == []

    asyncio.run(run())


def test_window_without_matches_degrades(welcome):
    """窗口过滤后为空：reason 说明「时间窗内没有新成员」，不是「没有档案」。"""

    async def run():
        plugin = _make_plugin(welcome, seen={"123456": {"10001": 1.0}})
        payload = await plugin.api_get_recent_newcomers(since_seconds=60)
        assert payload["groups"] == {} and payload["reason"] == "时间窗内没有新成员"
        source = W.parse_recent_newcomers(payload)
        assert W.world_events(newcomers=source, now=1000.0) == []

    asyncio.run(run())


def test_bad_payload_degrades_without_raising():
    """坏数据降级：SDK 失败字典 / 非 dict / groups 形状坏都不许抛。"""

    for label, bad in (
        ("SDK 失败字典", {"success": False, "error": "找不到提供方"}),
        ("None", None),
        ("groups 是列表", {"schema_version": 1, "reason": "", "active": True, "groups": []}),
        ("new_members 不是列表", {"schema_version": 1, "reason": "", "active": True,
                                  "groups": {"1": {"last_welcome_at": 0.0, "new_members": "x"}}}),
    ):
        source = W.parse_recent_newcomers(bad)
        assert W.world_events(newcomers=source, now=1000.0) == [], label


def test_readonly_does_not_touch_seen(welcome, tmp_path):
    """只读性：连读多次，``_seen`` / ``_last_trigger`` 不变、文件 mtime 不变。"""

    async def run():
        now = 1791174000.0
        plugin = _make_plugin(welcome, seen={"123456": {"10001": now - 10}}, data_dir=tmp_path)
        plugin._last_trigger["123456"] = now - 100
        plugin._save_state(force=True)
        path = plugin._state_path()
        assert path is not None and path.is_file()
        mtime = path.stat().st_mtime
        seen_before = {gid: dict(members) for gid, members in plugin._seen.items()}

        for _ in range(3):
            await plugin.api_get_recent_newcomers()

        assert plugin._seen == seen_before
        assert path.stat().st_mtime == mtime, "只读 API 写了盘"

    asyncio.run(run())
