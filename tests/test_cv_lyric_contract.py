# -*- coding: utf-8 -*-
"""L3：与 cv_lyric_context 的**真实**契约（跨插件）。

加载它的真实源码，调真正的 ``api_get_recent_songs``，喂给
``life_world.parse_recent_songs`` / ``world_events``。

两条要点：
1. ``songs`` 是**命中记录**（谁在群里聊到/点了哪首歌），有界环形 30 条，
   与「用户歌单」「最近推荐（软排除）」都不是一回事；
2. 同一天反复聊到同一首歌会**各记一次**（每条记录有自己的 ``at``），
   世界事件的去重键因此带命中时刻，而不是歌名。

目录查找顺序：``LF_CV_LYRIC_DIR`` → 同级目录 → ``plugins/<名>`` → 作者本机路径。
"""

import asyncio
import importlib.util
import logging
import os
import pathlib
import sys
import tempfile
import time

import pytest

import life_world as W

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
UPSTREAM = "cv_lyric_context"
API_NAME = "get_recent_songs"


def _entry_candidates() -> tuple[pathlib.Path, ...]:
    candidates: list[pathlib.Path] = []
    env = os.environ.get("LF_CV_LYRIC_DIR", "").strip()
    if env:
        candidates.append(pathlib.Path(env))
    candidates.extend(
        [
            PLUGIN_DIR.parent / UPSTREAM,
            pathlib.Path("plugins") / UPSTREAM,
        ]
    )
    return tuple(directory / "plugin.py" for directory in candidates)


_recent_songs = None


def _load_upstream():
    """加载上游 ``plugin.py``；上游目录只**临时**进 sys.path（见 live 契约里的同款说明）。

    ``recent_songs`` 句柄趁路径有效时取到，路径立刻撤掉 —— 上游目录里的 ``plugin.py``
    与本仓库 ``import plugin as P`` 撞名，留在 sys.path 上会污染全量测试。
    """
    global _recent_songs

    for entry in _entry_candidates():
        if not entry.is_file():
            continue
        directory = str(entry.parent)
        sys.path.insert(0, directory)
        try:
            spec = importlib.util.spec_from_file_location("cv_lyric_contract_under_test", entry)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)

            import recent_songs as _songs_module  # noqa: PLC0415

            _recent_songs = _songs_module
            return module
        finally:
            try:
                sys.path.remove(directory)
            except ValueError:
                pass
    return None


@pytest.fixture(scope="module")
def lyric():
    module = _load_upstream()
    if module is None:
        pytest.skip(f"未找到 {UPSTREAM} 源码（可用 LF_CV_LYRIC_DIR 指定）")
    return module


class _Ctx:
    def __init__(self):
        self.logger = logging.getLogger("plugin.org.mai-mai.cv-lyric-context")


def _make_plugin(lyric_module, tmp_path: pathlib.Path, *, started: bool = True):
    plugin = lyric_module.create_plugin()
    plugin._set_context(_Ctx())
    plugin.set_plugin_config({})
    if started:
        plugin._recent_songs = _recent_songs.RecentSongsLog(str(tmp_path))
    return plugin


def test_the_api_is_declared_public_version_1(lyric):
    """结构性契约：public + version 1 + handler 名正确。"""

    plugin = _make_plugin(lyric, pathlib.Path(tempfile.mkdtemp()))
    apis = [item for item in plugin.get_components() if item.get("type") == "API"]
    entry = next((item for item in apis if item["name"] == API_NAME), None)
    assert entry is not None, [item["name"] for item in apis]
    metadata = entry.get("metadata") or {}
    assert metadata.get("public") is True
    assert str(metadata.get("version")) == "1"
    assert metadata.get("handler_name") == "api_get_recent_songs"


def test_real_api_hits_map_to_world_events(lyric, tmp_path):
    """正常返回：两次命中（同名不同时刻）→ 两条事件、两个去重键。"""

    async def run():
        plugin = _make_plugin(lyric, tmp_path)
        plugin._recent_songs.add("夏天再见，逃离人间", "某P主、某歌手", at=1791171000.0)
        plugin._recent_songs.add("夏天再见，逃离人间", "某P主、某歌手", at=1791173000.0)

        payload = await plugin.api_get_recent_songs()
        assert len(payload["songs"]) == 2

        source = W.parse_recent_songs(payload, fetched_at=1791174000.0)
        assert source.ok and len(source.items) == 2
        events = W.world_events(songs=source, now=1791174000.0)
        assert [e.kind for e in events] == [W.KIND_SONG, W.KIND_SONG]
        assert events[0].label == "有人聊到了《夏天再见，逃离人间》"
        assert events[0].emotion == pytest.approx(0.12)
        assert len({e.key for e in events}) == 2, "两次命中必须各有去重键"

    asyncio.run(run())


def test_hit_without_artist_keeps_empty_string(lyric, tmp_path):
    """歌手取不到就留空串（上游不猜）：正文退化为纯歌名，不许编造歌手。"""

    async def run():
        plugin = _make_plugin(lyric, tmp_path)
        plugin._recent_songs.add("没有歌手信息的歌", "", at=1791171000.0)
        payload = await plugin.api_get_recent_songs()
        source = W.parse_recent_songs(payload, fetched_at=1791174000.0)
        events = W.world_events(songs=source, now=1791174000.0)
        assert events[0].text == "没有歌手信息的歌"
        assert "—" not in events[0].text

    asyncio.run(run())


def test_empty_and_broken_archive_degrade(lyric, tmp_path):
    """空数据/坏数据降级：空档案与坏文件都给 reason，零事件。"""

    async def run():
        plugin = _make_plugin(lyric, tmp_path)
        empty = await plugin.api_get_recent_songs()
        assert empty["songs"] == [] and empty["reason"] == "暂无歌曲命中记录"
        assert W.world_events(songs=W.parse_recent_songs(empty), now=1000.0) == []

        # 坏文件：上游备份成 .broken 并从空开始（不许抛）
        (tmp_path / "recent_songs.json").write_text("{ 坏 json", encoding="utf-8")
        plugin2 = _make_plugin(lyric, tmp_path)
        broken = await plugin2.api_get_recent_songs()
        assert broken["songs"] == []
        assert (tmp_path / "recent_songs.json.broken").exists()

    asyncio.run(run())


def test_not_started_degrades(lyric, tmp_path):
    """上游未启用/未加载（on_load 未跑）→ active=False + reason，事件为空。"""

    async def run():
        plugin = _make_plugin(lyric, tmp_path, started=False)
        payload = await plugin.api_get_recent_songs()
        assert payload["active"] is False and payload["songs"] == []
        assert "未启动" in payload["reason"] or "停用" in payload["reason"]
        assert W.world_events(songs=W.parse_recent_songs(payload), now=1000.0) == []

    asyncio.run(run())


def test_bad_payload_degrades_without_raising():
    """坏数据降级：SDK 失败字典 / 非 dict / songs 形状坏都不许抛。"""

    for label, bad in (
        ("SDK 失败字典", {"success": False, "error": "找不到提供方"}),
        ("None", None),
        ("songs 不是列表", {"schema_version": 1, "reason": "", "active": True, "songs": {}}),
        ("缺 schema_version", {"active": True, "songs": []}),
    ):
        assert W.world_events(songs=W.parse_recent_songs(bad), now=1000.0) == [], label


def test_readonly_does_not_touch_upstream_state(lyric, tmp_path):
    """只读性：连读多次，记录文件与内存条目都不变（不 flush、不 dirty）。"""

    async def run():
        now = time.time()
        plugin = _make_plugin(lyric, tmp_path)
        plugin._recent_songs.add("测试曲", "某P主", at=now - 10)
        plugin._recent_songs.flush(force=True)
        path = pathlib.Path(plugin._recent_songs.path)
        before = (path.read_bytes(), path.stat().st_mtime)
        entries_before = [dict(item) for item in plugin._recent_songs._entries]

        for _ in range(3):
            payload = await plugin.api_get_recent_songs()
            assert len(payload["songs"]) == 1

        assert (path.read_bytes(), path.stat().st_mtime) == before, "只读 API 写了盘"
        assert plugin._recent_songs._entries == entries_before
        assert plugin._recent_songs.dirty is False, "只读 API 制造了待写状态"

    asyncio.run(run())
