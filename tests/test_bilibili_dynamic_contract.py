# -*- coding: utf-8 -*-
"""L3：与 bilibili-dynamic-push 的**真实**契约（跨插件）。

加载它的真实源码，调真正的 ``api_get_subscriptions`` / ``api_get_recent_pushes``，
喂给 ``life_world`` 的对应解析函数。

两条上游语义要点（写进断言，防日后被「顺手优化」掉）：

1. ``get_subscriptions`` 的 ``name`` 为空就返回空串——**不猜、不用 uid 顶替**；
2. ``get_recent_pushes`` 只记**推送成功**那一刻（手动 ``/dyn test`` 走 ``record=False``，
   因为它推的是旧动态，记了会被当成「UP 主刚发了新动态」）。

目录查找顺序：``LF_BILI_DYNAMIC_DIR`` → 同级目录 → ``plugins/<名>`` → 作者本机路径。
"""

import asyncio
import importlib.util
import logging
import os
import pathlib
import sys
import tempfile

import pytest

import life_world as W

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
UPSTREAM = "bilibili-dynamic-push"
SUBS_API = "get_subscriptions"
PUSHES_API = "get_recent_pushes"


def _entry_candidates() -> tuple[pathlib.Path, ...]:
    candidates: list[pathlib.Path] = []
    env = os.environ.get("LF_BILI_DYNAMIC_DIR", "").strip()
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


_subscription_store = None


def _load_upstream():
    """加载上游 ``plugin.py``；上游目录只**临时**进 sys.path（见 live 契约里的同款说明）。

    上游存在模块名冲突风险：``plugin.py`` 与本仓库的 ``import plugin as P`` 撞名，
    所以兄弟模块句柄（``subscription_store``）要趁路径有效时取到，路径立刻撤掉。
    """
    global _subscription_store

    for entry in _entry_candidates():
        if not entry.is_file():
            continue
        directory = str(entry.parent)
        sys.path.insert(0, directory)
        try:
            spec = importlib.util.spec_from_file_location("bili_dynamic_contract_under_test", entry)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)

            import subscription_store as _store_module  # noqa: PLC0415

            _subscription_store = _store_module
            return module
        finally:
            try:
                sys.path.remove(directory)
            except ValueError:
                pass
    return None


@pytest.fixture(scope="module")
def dynamic_push():
    module = _load_upstream()
    if module is None:
        pytest.skip(f"未找到 {UPSTREAM} 源码（可用 LF_BILI_DYNAMIC_DIR 指定）")
    return module


class _Ctx:
    def __init__(self):
        self.logger = logging.getLogger("plugin.org.mai-mai.bilibili-dynamic-push")


def _make_plugin(dynamic_push_module, tmp_path: pathlib.Path, *, started: bool = True):
    plugin = dynamic_push_module.create_plugin()
    plugin._set_context(_Ctx())
    plugin.set_plugin_config({})
    if started:
        plugin._subs = _subscription_store.SubscriptionStore(str(tmp_path))
        plugin._push_log = _subscription_store.PushLog(str(tmp_path))
    return plugin


def test_both_apis_are_declared_public_version_1(dynamic_push):
    """结构性契约：两个 API 都必须 public + version 1。"""

    plugin = _make_plugin(dynamic_push, pathlib.Path(tempfile.mkdtemp()))
    apis = {
        item["name"]: item
        for item in plugin.get_components()
        if item.get("type") == "API"
    }
    assert {SUBS_API, PUSHES_API} <= set(apis), sorted(apis)
    for name, handler in (
        (SUBS_API, "api_get_subscriptions"),
        (PUSHES_API, "api_get_recent_pushes"),
    ):
        metadata = apis[name].get("metadata") or {}
        assert metadata.get("public") is True, name
        assert str(metadata.get("version")) == "1", name
        assert metadata.get("handler_name") == handler, name


def test_real_subscriptions_contract(dynamic_push, tmp_path):
    """正常返回 + 空 name 不猜 + fixed 保留；空订阅走 reason 降级。"""

    async def run():
        plugin = _make_plugin(dynamic_push, tmp_path)
        empty = await plugin.api_get_subscriptions()
        assert empty["up"] == [] and empty["reason"] == "暂无订阅"
        assert W.parse_subscriptions(empty).count == 0

        # 命令订阅（无 name）与配置行订阅（fixed=True，带 name）
        await plugin._subs.add("114514", 111)
        plugin._subs.sync_from_config(["2233 => 222, 333"])
        await plugin._subs.set_name("2233", "罗翔说刑法")

        payload = await plugin.api_get_subscriptions()
        assert payload["count"] == 2
        by_uid = {item["uid"]: item for item in payload["up"]}
        assert by_uid["114514"]["name"] == "", "name 为空必须原样返回，不得拿 uid 顶替"
        assert by_uid["114514"]["fixed"] is False
        assert by_uid["2233"]["fixed"] is True

        source = W.parse_subscriptions(payload)
        assert source.ok and source.count == 2
        # 无名的订阅在状态卡里退回 uid（展示层兜底，不改上游契约）
        assert "114514" in source.names

    asyncio.run(run())


def test_real_pushes_contract_maps_to_world_events(dynamic_push, tmp_path):
    """正常返回：一条视频动态 → 视频事件；标题换行被压平。"""

    async def run():
        plugin = _make_plugin(dynamic_push, tmp_path)
        empty = await plugin.api_get_recent_pushes()
        assert empty["pushes"] == [] and empty["reason"] == "暂无推送记录"

        await plugin._push_log.append(
            uid="114514",
            name="测试UP",
            dyn_type="DYNAMIC_TYPE_AV",
            title="【原创】新曲 PV\n第二行",
            url="https://www.bilibili.com/video/BV1xx",
            at=1791172000.0,
        )
        payload = await plugin.api_get_recent_pushes()
        source = W.parse_recent_pushes(payload, fetched_at=1791174000.0)
        assert source.ok and len(source.items) == 1
        assert "\n" not in source.items[0].title

        events = W.world_events(pushes=source, now=1791174000.0)
        assert [e.kind for e in events] == [W.KIND_VIDEO]
        assert events[0].label == "测试UP 发了新视频"
        assert events[0].key == "push!https://www.bilibili.com/video/BV1xx"

    asyncio.run(run())


def test_manual_test_push_is_not_recorded(dynamic_push, tmp_path):
    """手动 ``/dyn test`` 走 record=False：不产生事件（否则会造假的世界事件）。"""

    async def run():
        plugin = _make_plugin(dynamic_push, tmp_path)
        calls = {"sent": 0}

        async def _stream_id(group_id):
            return f"stream-{group_id}"

        async def _send(stream_id, text, image_b64s, *, where, sender_name):
            calls["sent"] += 1

        plugin._get_stream_id = _stream_id
        plugin._send_dynamic_content = _send
        item = {
            "id_str": "1234567890",
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {
                "module_author": {"name": "测试UP", "pub_ts": 1791172000},
                "module_dynamic": {
                    "desc": {"text": ""},
                    "major": {
                        "type": "MAJOR_TYPE_OPUS",
                        "opus": {"title": "旧动态标题", "summary": {"text": ""}, "pics": []},
                    },
                },
            },
        }
        await plugin._push_dynamic("114514", item, [12345], record=False)
        assert calls["sent"] == 1, "手动推送本身仍要真的发出去"
        payload = await plugin.api_get_recent_pushes()
        assert payload["pushes"] == []
        assert W.world_events(pushes=W.parse_recent_pushes(payload), now=1791174000.0) == []

    asyncio.run(run())


def test_bad_payload_degrades_without_raising():
    """坏数据降级：三个源各自的坏形状都不许抛，且零事件。"""

    for label, bad in (
        ("SDK 失败字典", {"success": False, "error": "找不到提供方"}),
        ("None", None),
        ("pushes 不是列表", {"schema_version": 1, "reason": "", "active": True, "pushes": "x"}),
        ("songs 不是列表", {"schema_version": 1, "reason": "", "active": True, "songs": "x"}),
        ("缺 schema_version", {"active": True, "up": []}),
    ):
        assert W.world_events(
            pushes=W.parse_recent_pushes(bad), songs=W.parse_recent_songs(bad), now=1000.0
        ) == [], label


def test_readonly_does_not_touch_files(dynamic_push, tmp_path):
    """只读性：连读两个 API，订阅表与推送记录文件都不被改写。"""

    async def run():
        plugin = _make_plugin(dynamic_push, tmp_path)
        await plugin._subs.add("114514", 111)
        await plugin._subs.save()
        await plugin._push_log.append(
            uid="114514", name="测试UP", dyn_type="DYNAMIC_TYPE_DRAW",
            title="标题", url="https://t.bilibili.com/1", at=1791172000.0,
        )
        subs_path = tmp_path / "subscriptions.json"
        log_path = tmp_path / "push_log.json"
        before = (subs_path.read_bytes(), log_path.read_bytes(),
                  subs_path.stat().st_mtime, log_path.stat().st_mtime)

        for _ in range(3):
            await plugin.api_get_subscriptions()
            await plugin.api_get_recent_pushes()

        after = (subs_path.read_bytes(), log_path.read_bytes(),
                 subs_path.stat().st_mtime, log_path.stat().st_mtime)
        assert after == before, "只读 API 改写了上游文件"

    asyncio.run(run())
