# -*- coding: utf-8 -*-
"""L3：WebUI 自定义页面（webui.json 协议 v2 + ``life.*`` API）回归测试（v1.22.5）。

三条链路缺一条，真机上就可能出现「页面整块消失」「点了按钮 404」或「页面显示
invalidData」——本文件把三条都钉住：

1. **声明链**：``webui.json`` 必须过宿主自己的 ``load_webui_extension``
   （宿主用 ``extra="forbid"`` 的严格 pydantic 模型，多一个字段都加载失败，
   且失败会让**整个插件**加载不进去，``runner_main.py`` 对 ValueError 直接
   return False）；placement 必须是 ``workspace``（sidebar 页只在「设置」工作区
   可见，切走就消失）；``poll_interval_seconds`` 必须 ≥3（前端 ``< 3`` 不轮询）。
2. **绑定链**：页面每条 queries/actions 绑定的 api 必须在插件
   ``get_components()`` 里真有其人（静态注册；dynamic API 宿主 404）；
   API 返回值必须能过宿主的 ``validate_json``。
3. **渲染链**（本文件最重要的一条）：前端 ``resolveReference`` 对**缺失字段直接
   抛错**、``display()`` 对**对象/数组直接抛错**，整页变 invalidData。所以这里
   拿真实 API 返回值，把页面上每个 DataReference 逐条解析一遍，并按节点类型
   断言取值形态（progress 必须 0–100 数字、table 必须 dict 列表且行列字段齐全、
   text/stat 必须标量）——把前端的两种崩溃在测试里复现。

对账：``life.dashboard`` 与 ``/生活 状态``、``/生活 频率`` 必须同源同数
（两条路径分叉是这类「投影层」最常见的腐烂方式）。
"""

import asyncio
import json
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import life_webui  # noqa: E402  投影层模块本身（无状态纯函数，可脱插件单测）

_DEFAULT_HOST = (
    "C:/Users/38160/AppData/Roaming/MaiBotOneKeyDesktop/e6a2be8e69ae/modules/MaiBot"
)


def _host_schema_module():
    """加载宿主的 webui_schema（按 1.3.6+ 契约校验声明）；拿不到就跳过。"""
    import os

    host_dir = os.environ.get("MAIBOT_HOST_DIR") or _DEFAULT_HOST
    root = pathlib.Path(host_dir)
    if not (root / "src" / "plugin_runtime" / "webui_schema.py").exists():
        pytest.skip(f"未找到 MaiBot 宿主检出: {root}")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import importlib

    try:
        return importlib.import_module("src.plugin_runtime.webui_schema")
    except Exception as exc:  # pragma: no cover - 依赖缺失不算插件的锅
        pytest.skip(f"无法加载宿主 schema: {exc}")


def _declaration() -> dict:
    """webui.json 原文（渲染链测试不依赖宿主，直接读声明）。"""

    return json.loads((_PLUGIN_DIR / "webui.json").read_text(encoding="utf-8"))


def _walk_nodes(nodes: list) -> list[dict]:
    """深度优先收集声明里的全部节点（含容器子节点）。"""

    out: list[dict] = []
    pending = list(nodes)
    while pending:
        node = pending.pop(0)
        if not isinstance(node, dict):
            continue
        out.append(node)
        pending.extend(node.get("children") or [])
    return out


def _node_references(node: dict) -> list[dict]:
    """一个节点上的全部 DataReference（value / when / disabled_when）。"""

    refs: list[dict] = []
    for key in ("value", "reference"):
        item = node.get(key)
        if isinstance(item, dict) and {"scope", "source", "field"} <= item.keys():
            refs.append(item)
    for key in ("when", "disabled_when"):
        condition = node.get(key)
        if isinstance(condition, dict):
            refs.extend(_node_references(condition))
    return refs


def _resolve(payload: dict, reference: dict):
    """复刻前端 ``resolveReference``：缺 source / 缺字段都算失败（返回 _MISSING）。"""

    source = payload.get(reference.get("source") or "")
    if source is _MISSING or not isinstance(source, dict):
        return _MISSING
    value = source
    for part in (reference.get("field") or "").split("."):
        if not part:
            continue
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


class _Missing:
    """哨兵：与「取到 None」区分开（None 是合法取值，display 成 —）。"""

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return "<MISSING>"


_MISSING = _Missing()


# ------------------------------------------------------------------- 声明链


def test_declaration_loads_under_host_schema():
    ext = _host_schema_module().load_webui_extension(str(_PLUGIN_DIR))
    assert ext.schema_version == 2
    assert [page.id for page in ext.pages] == ["dashboard"]
    page = ext.pages[0]
    # 必须 workspace：sidebar 页只在「设置」工作区进侧边栏，用户切到别的插件
    # 工作区就整个消失（Layout.tsx:127-135 的过滤）——真机踩过这个坑。
    assert page.placement == "workspace"
    assert (ext.workspace_title or "").strip(), "workspace 页必须配 workspace_title，否则宿主拒绝加载"
    # 前端 seconds < 3 直接 return（plugin-webui.tsx:176-183）：轮询间隔必须 ≥3
    assert page.poll_interval_seconds >= 3, "定时轮询间隔必须 ≥3 秒，否则前端不轮询"
    assert page.auto_refresh is True


def test_manifest_declares_required_capability():
    manifest = json.loads((_PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    assert "declarative_basic" in (manifest.get("capabilities") or []), (
        "webui.json 声明了 declarative_basic，manifest capabilities 必须同步加上，"
        "否则宿主按「未声明能力使用特性」处理"
    )


def test_declared_capabilities_match_extension():
    ext = _host_schema_module().load_webui_extension(str(_PLUGIN_DIR))
    assert set(ext.required_capabilities) == {"declarative_basic"}


def test_page_stays_within_protocol_limits():
    """节点数 / 深度 / 列数 / 页数上限（宿主 webui_schema 的同款约束，提前挡。"""

    declaration = _declaration()
    assert declaration["schema_version"] == 2
    pages = declaration["pages"]
    assert 1 <= len(pages) <= 20
    for page in pages:
        assert page["placement"] == "workspace", "自定义页面一律用 workspace（skill 铁律）"
        nodes = _walk_nodes(page["content"])
        assert len(nodes) <= 200, f"节点数 {len(nodes)} 超 200"
        assert len(page["content"]) <= 50
        depth = 1
        pending = [(node, 1) for node in page["content"]]
        while pending:
            node, level = pending.pop()
            depth = max(depth, level)
            pending.extend((child, level + 1) for child in (node.get("children") or []))
        assert depth <= 8, f"树深 {depth} 超 8"
        for node in nodes:
            if node.get("type") == "grid":
                assert 1 <= node.get("columns", 1) <= 4
            if node.get("type") == "table":
                assert 1 <= len(node.get("columns") or []) <= 20
        assert 0 <= page.get("poll_interval_seconds", 0) <= 60
        assert page.get("poll_interval_seconds") not in (1, 2)


# ------------------------------------------------------------------- 绑定链


def test_bindings_are_zero_parameter_and_declaration_free():
    """查询/动作全部零参数：页面没有输入框（数字参数走 string 的坑从源头避免）。

    queries 声明 arguments/confirmation 会被宿主直接拒绝（webui_schema.py:379-381）。
    """

    for page in _declaration()["pages"]:
        for name, binding in (page.get("queries") or {}).items():
            assert binding.get("parameters", {}) == {}, f"query {name} 不该有参数"
            assert not binding.get("arguments"), f"query {name} 不得声明 arguments"
            assert not binding.get("confirmation"), f"query {name} 不得声明 confirmation"
        for name, binding in (page.get("actions") or {}).items():
            assert binding.get("parameters", {}) == {}, f"action {name} 不该有参数"
            assert not binding.get("arguments"), f"action {name} 不该依赖 selection"


def test_declared_actions_exist_and_danger_has_confirmation():
    """页面上每个按钮都必须指向已声明动作；danger 必须配 confirmation。"""

    for page in _declaration()["pages"]:
        actions = page.get("actions") or {}
        for node in _walk_nodes(page["content"]):
            if node.get("type") != "button":
                continue
            assert node.get("action") in actions, f"按钮指向未声明动作: {node.get('action')}"
            if node.get("variant") == "danger":
                assert actions[node["action"]].get("confirmation"), "danger 操作必须配 confirmation"


def test_bound_apis_are_registered():
    """每个被页面绑定的 api 都必须在插件组件表里，且是静态注册。"""

    from fakehost import load_plugin_module

    module = load_plugin_module(_PLUGIN_DIR, "life_frequency_webui_decl")
    components = {
        (item.get("name"), str(item.get("metadata", {}).get("version", "1")))
        for item in module.create_plugin().get_components()
        if item.get("type") == "API"
        and item.get("metadata", {}).get("enabled") is True
        and item.get("metadata", {}).get("dynamic") is not True
    }
    bound: list[tuple[str, str]] = []
    for page in _declaration()["pages"]:
        for binding in (page.get("queries") or {}).values():
            bound.append((binding["api"], binding.get("version", "1")))
        for binding in (page.get("actions") or {}).values():
            bound.append((binding["api"], binding.get("version", "1")))
    missing = [pair for pair in bound if pair not in components]
    assert not missing, f"页面绑定了插件没注册的 API: {missing}"
    assert bound, "页面没有绑定任何 API"


# ------------------------------------------------------------------- 插件脚手架


def _controllable_config(config: dict) -> dict:
    """与 smoke_test 同款可控化：静默时段 / 三餐 / 习惯全关，避免撞墙钟。"""

    config["activity"]["min_dwell_minutes"] = 0
    config["activity"]["min_sleep_minutes"] = 0
    config["activity"]["llm"]["min_interval_seconds"] = 0
    config["events"]["fire_probability"] = 0.0
    config["frequency"]["quiet_hours"] = []
    config["proactive"]["enabled"] = False
    config["physio"]["meals"] = []
    config["routines"]["lines"] = []
    return config


async def _build_plugin(module_name: str):
    """完整生命周期脚手架：bind_context → on_load（与真机 Runner 同链路）。

    调用方负责在同一个事件循环里 ``await plugin.on_unload()``——on_load 起的两个
    后台循环与 on_unload 必须在同一个 loop（smoke_test 的同款纪律）。
    """

    from fakehost import (
        FakeHost,
        FakePaths,
        bind_context,
        build_context,
        get_default_config,
        load_plugin_module,
    )

    module = load_plugin_module(_PLUGIN_DIR, module_name)
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    bind_context(plugin, ctx, _controllable_config(get_default_config(type(plugin).config_model)))
    await plugin.on_load()
    return module, plugin, host


async def _seed_relations(plugin, now: float) -> None:
    """塞两条关系档案（走线程池，与命令侧读盘的纪律一致）。"""

    rows = [
        {"person_id": "qq:123456789", "platform": "qq", "user_id": "123456789",
         "familiarity": 82.0, "relation_hint": "群主", "interaction_count": 12,
         "last_interaction_at": now - 3600.0},
        {"person_id": "qq:987654321", "platform": "qq", "user_id": "987654321",
         "familiarity": 35.0, "relation_hint": "", "interaction_count": 3,
         "last_interaction_at": now - 86400.0},
    ]
    await asyncio.to_thread(plugin._routine_store.put_relationship, rows[0])
    await asyncio.to_thread(plugin._routine_store.put_relationship, rows[1])


# ------------------------------------------------------------------- 渲染链


def _assert_page_contract(payload: dict) -> None:
    """把页面上每个 DataReference 逐条解析，并按节点类型断言取值形态。

    这就是前端两种崩溃的测试复刻：``resolveReference`` 缺字段抛错、
    ``display()`` 遇对象/数组抛错（plugin-webui.tsx / plugin-webui-renderer.tsx）。

    ⚠ 前端 ``data`` 以**查询名**为键（``results[name] = api 返回值``），所以这里
    要按声明里的 queries 名把 payload 包一层——少包一层会误报「字段缺失」。
    """

    for page in _declaration()["pages"]:
        data = {name: payload for name in (page.get("queries") or {})}
        for node in _walk_nodes(page["content"]):
            kind = node.get("type")
            for reference in _node_references(node):
                value = _resolve(data, reference)
                where = f"{kind} 节点引用 {reference['source']}.{reference['field']}"
                assert value is not _MISSING, f"{where}：字段缺失（前端会整页 invalidData）"
                if kind == "progress":
                    assert isinstance(value, (int, float)) and not isinstance(value, bool), (
                        f"{where}：progress 必须绑定数字，实际 {type(value).__name__}"
                    )
                    assert 0 <= value <= 100, f"{where}：progress 值 {value} 越界 0–100"
                elif kind == "table":
                    assert isinstance(value, list), f"{where}：table 必须绑定数组"
                    for row in value:
                        assert isinstance(row, dict), f"{where}：table 行必须是 dict"
                        for column in node.get("columns") or []:
                            cell = row.get(column["field"], _MISSING)
                            assert cell is not _MISSING, (
                                f"{where}：行缺少列字段 {column['field']}（前端渲染空单元格）"
                            )
                            assert not isinstance(cell, (dict, list)), (
                                f"{where}：列 {column['field']} 取值是对象/数组（display 抛错）"
                            )
                elif kind in ("text", "stat"):
                    assert not isinstance(value, (dict, list)), (
                        f"{where}：{kind} 绑定到对象/数组（display 抛错），"
                        "列表信息必须在插件侧拼成字符串"
                    )


def test_dashboard_payload_satisfies_page_contract():
    """真实 API 返回值必须满足页面上每个引用的形态契约。"""

    async def flow():
        module, plugin, host = await _build_plugin("life_frequency_webui_payload")
        try:
            now = time.time()
            await _seed_relations(plugin, now)
            payload = await plugin.api_dashboard()
            assert payload["status"]["ready"] is True
            assert payload["frequency"]["ready"] is True
            assert payload["attribution"]["ready"] is True
            assert payload["relations"]["ready"] is True
            assert payload["relations"]["rows"], "种子关系没有进快照"
            _assert_page_contract(payload)
            # 暂停态下倍率口径切到 hold_baseline：字段仍须恒存在
            await plugin.api_pause()
            paused_payload = await plugin.api_dashboard()
            _assert_page_contract(paused_payload)
            assert paused_payload["status"]["paused"] is True
        finally:
            await plugin.on_unload()

    asyncio.run(flow())


def test_dashboard_degrades_instead_of_raising():
    """一段炸了只降级该段：绑定字段仍然恒存在，页面照常渲染。"""

    async def flow():
        module, plugin, host = await _build_plugin("life_frequency_webui_degraded")
        try:
            # 关系库不可用 + 两个维度关掉 + 拆解函数抛错：三段同时恶化
            plugin._routine_store = None
            plugin.config.relations.enabled = False
            plugin.config.mood.enabled = False

            def _boom(_now: float):
                raise RuntimeError("模拟拆解失败")

            plugin._compute_breakdown = _boom  # type: ignore[method-assign]
            payload = await plugin.api_dashboard()
            assert payload["status"]["ready"] is False
            assert payload["frequency"]["ready"] is False
            assert payload["relations"]["ready"] is False
            assert payload["relations"]["rows"] == []
            # 降级占位的字段集必须与正常返回一致——否则页面照样 invalidData
            _assert_page_contract(payload)
        finally:
            await plugin.on_unload()

    asyncio.run(flow())


def test_returns_survive_host_json_guard():
    """宿主对返回值执行 validate_json；混入非 JSON 类型等于整页报错。"""

    async def flow():
        module, plugin, host = await _build_plugin("life_frequency_webui_json")
        try:
            now = time.time()
            await _seed_relations(plugin, now)
            schema = _host_schema_module()
            for payload in (
                await plugin.api_dashboard(),
                await plugin.api_pause(),
                await plugin.api_resume(),
                await plugin.api_pause(),
            ):
                schema.validate_json(payload, max_bytes=524288)  # ValueError = 前端报错
        finally:
            await plugin.on_unload()

    asyncio.run(flow())


# ------------------------------------------------------------------- 对账与写入口


def test_dashboard_matches_command_cards():
    """``life.dashboard`` 与 ``/生活 状态``、``/生活 频率`` 必须同源同数。

    两条路径分叉是「投影层」最常见的腐烂方式：命令侧改了取数，页面侧不知道。
    这里用**同一批纯函数**同时算两份，断言关键数字在两边一致。
    """

    async def flow():
        module, plugin, host = await _build_plugin("life_frequency_webui_reconcile")
        try:
            now = time.time()
            await _seed_relations(plugin, now)

            card = plugin._render_status(now, "")
            freq_card = await plugin._render_frequency(now, "")
            payload = await plugin.api_dashboard()
            status, frequency = payload["status"], payload["frequency"]

            breakdown = plugin._compute_breakdown(now)
            # 状态卡与快照的「当前倍率」必须是同一个数
            assert f"当前倍率：{breakdown.adjust:.3f}" in card
            assert status["adjust_text"] == f"{breakdown.adjust:.3f}"
            # 频率卡与快照的曲线组/硬闸理由一致
            if breakdown.reason == "ok":
                assert f"曲线组：{breakdown.curve_set}" in freq_card
                assert f"曲线组：{breakdown.curve_set}" in frequency["gate_text"]
            else:
                assert module.reason_label(breakdown.reason) in freq_card
                assert module.reason_label(breakdown.reason) in frequency["gate_text"]
            # 活动名与因子表同源
            label = plugin._activity_label(plugin._state.activity)
            assert label in card and label in status["activity_line"]
            if breakdown.reason == "ok":
                names = [row["factor"] for row in frequency["factors"]]
                assert names[0] == "原始倍率" and names[-1] == "最终倍率"
                # 因子名与卡片拆解行同源（as_lines 的 "× {name} = {value:.3f}"）
                for name, _value in breakdown.factors:
                    assert name in names, f"因子 {name} 在快照表里缺失"
                for row in frequency["factors"]:
                    assert len(row["value"].split(".")[-1]) == 3, "取值必须三位小数"
            # 睡眠小时数同源（一个是卡片句子、一个是 stat 短句）
            slept = module.sleep_hours_today(plugin._state)
            awake = module.awake_hours_today(plugin._state)
            assert f"今日已睡 {slept:.1f} 小时" in card
            assert status["sleep_text"] == f"{slept:.1f}h / {awake:.1f}h"
        finally:
            await plugin.on_unload()

    asyncio.run(flow())


def test_pause_resume_roundtrip_and_persist():
    """页面写操作与命令同路径：状态翻转 → 幂等 → 落盘 → 恢复。"""

    async def flow():
        module, plugin, host = await _build_plugin("life_frequency_webui_write")
        try:
            now = time.time()

            first = await plugin.api_pause()
            assert first["ok"] is True and first["paused"] is True and first["changed"] is True
            assert plugin._state.paused_override is True
            assert "已停止干预" in plugin._render_status(now, "")

            # 幂等：重复暂停不改状态、不重复落盘语义
            again = await plugin.api_pause()
            assert again["ok"] is True and again["changed"] is False
            assert again["reason"] == "状态未变（已是目标状态）"

            # 持久化：重载磁盘副本必须看得到（否则重启即丢）
            assert plugin._load_state().paused_override is True

            resumed = await plugin.api_resume()
            assert resumed["ok"] is True and resumed["paused"] is False
            assert plugin._state.paused_override is False
            assert "已停止干预" not in plugin._render_status(now, "")
            assert plugin._load_state().paused_override is False

            # 插件停用时拒绝写（与命令侧 early-return 同口径）
            plugin.config.plugin.enabled = False
            blocked = await plugin.api_pause()
            assert blocked["ok"] is False and blocked["changed"] is False
            assert "已停用" in blocked["reason"]
        finally:
            await plugin.on_unload()

    asyncio.run(flow())


def test_relations_snapshot_shapes_and_masking():
    """关系段的纯函数口径：脱敏、档位、时间戳、关闭/无库/空档案三条降级分支。"""

    pytest.importorskip("maibot_sdk", reason="接线用例需要 maibot-plugin-sdk")

    async def flow():
        module, plugin, host = await _build_plugin("life_frequency_webui_relations")
        try:
            now = time.time()
            await _seed_relations(plugin, now)
            records = await asyncio.to_thread(plugin._routine_store.top_relationships, 50)
            snapshot = life_webui.relations_snapshot(plugin, records)
            assert snapshot["ready"] is True
            assert snapshot["count"] == 2
            assert snapshot["max_text"] == "82.0"
            rows = {row["person"]: row for row in snapshot["rows"]}
            # QQ 号必须脱敏（前 3 后 2；≤5 位原样）；备注拼进 person 列
            assert "群主（123***89）" in rows
            assert rows["群主（123***89）"]["tier"] == "亲密"
            assert rows["群主（123***89）"]["interactions"] == 12
            assert rows["群主（123***89）"]["last_seen"] != "—"
            assert "987***21" in rows
            assert rows["987***21"]["tier"] == "认识"

            # 三条降级分支
            store = plugin._routine_store
            plugin.config.relations.enabled = False
            off = life_webui.relations_snapshot(plugin, records)
            assert off["ready"] is False and off["rows"] == []
            assert "关系层已关闭" in off["note"]
            plugin.config.relations.enabled = True
            plugin._routine_store = None
            broken = life_webui.relations_snapshot(plugin, records)
            assert broken["ready"] is False and "私有库不可用" in broken["note"]
            plugin._routine_store = store
            empty = life_webui.relations_snapshot(plugin, [])
            assert empty["ready"] is True and "还没有档案" in empty["note"]
        finally:
            await plugin.on_unload()

    asyncio.run(flow())
