# -*- coding: utf-8 -*-
"""主动开口撞宿主**策略**拒绝后的会话级冷却（v1.23.2）。

真机日志（2026-10-23 22:56:09/22:56:10）：

```
[心流管理] 适配器名单禁止创建聊天运行实例: e82ef538...
[IPC插件系统] [cap.maisaka.context.append] 执行失败: ...（满栈 traceback）
```

宿主那份适配器名单插件读不到，但被拒一次就能记住。冷却期内不再把这个会话当候选，
免得每 tick 撞一次墙、每 tick 一条宿主侧满栈 traceback。

⚠ 本文件最关键的一条是 ``test_transient_failure_does_not_enter_the_denied_cooldown``：
``success=False`` **也**用于「宿主故障」这类瞬时失败，那种失败归
``[proactive].failure_cooldown_minutes`` 管（可配 0 = 每周期重试）。不加区分地一律进
30 分钟冷却，就是用隐藏闸盖掉用户配置 —— 第一版实现正是这么打挂
``test_proactive_race`` 两条的。
"""

import asyncio
import json
import pathlib
import sys
import time

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
TRIGGER = "maisaka.proactive.trigger"
SESSION = "group-1"
#: 真机原文（``HeartflowPolicyDeniedError`` 的message）
POLICY_ERROR = "适配器名单禁止创建聊天运行实例: e82ef538d09149a1202e401581622d6b"


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_proactive_denied")


def _make_plugin(**config_overrides):
    module = _load()
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["proactive"]["enabled"] = True
    config["proactive"]["quiet_hours"] = []
    for section, values in config_overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin.config
    return module, plugin, host


def _patch_sessions(plugin, *session_ids):
    async def fake_list():
        return [(sid, {"session_id": sid, "group_id": "123456"}) for sid in session_ids]

    plugin._list_sessions = fake_list


def _material(now: float) -> dict:
    return {"label": "小事", "text": "刚泡了杯茶", "weight": 0.9, "created_at": now,
            "expires_at": now + 3600.0}


def _prepare(plugin, host, now: float) -> None:
    plugin._state.materials = [_material(now)]
    plugin._state.sessions[SESSION] = {
        "stream_id": SESSION,
        "last_user_message_at": now - 10 * 3600.0,
        "last_proactive_at": 0.0,
        "day_key": "2026-10-11",
        "count": 0,
        "unanswered_streak": 0,
    }
    _patch_sessions(plugin, SESSION)


# ------------------------------------------------------- 一、策略拒绝才进冷却


def test_policy_denial_enters_the_cooldown():
    """真机原文那种错误 ⇒ 记住这个会话。"""

    async def run():
        _, plugin, host = _make_plugin()
        host.returns[TRIGGER] = {"success": False, "error": POLICY_ERROR}
        now = time.time()
        _prepare(plugin, host, now)

        await plugin._maybe_proactive(now)
        assert plugin._proactive_denied.is_denied(SESSION, now + 60.0), "被策略拒绝后应进冷却"

        # 冷却期内：不再打 RPC（否则每 tick 一条宿主侧满栈 traceback）
        calls_before = len(host.calls_of(TRIGGER))
        plugin._state.materials = [_material(now)]
        await plugin._maybe_proactive(now + 60.0)
        assert len(host.calls_of(TRIGGER)) == calls_before, "冷却期内不该再撞一次墙"

    asyncio.run(run())


def test_transient_failure_does_not_enter_the_denied_cooldown():
    """「宿主故障」是抖动，不能进这个冷却——否则盖掉 failure_cooldown_minutes=0。"""

    async def run():
        _, plugin, host = _make_plugin(proactive={"failure_cooldown_minutes": 0})
        host.returns[TRIGGER] = {"success": False, "error": "宿主故障"}
        now = time.time()
        _prepare(plugin, host, now)

        await plugin._maybe_proactive(now)
        assert not plugin._proactive_denied.is_denied(SESSION, now + 1.0), (
            "瞬时失败不该被当成策略拒绝"
        )
        # 配置说关掉冷却 ⇒ 下个周期照旧重试
        plugin._state.materials = [_material(now)]
        calls_before = len(host.calls_of(TRIGGER))
        await plugin._maybe_proactive(now + 1.0)
        assert len(host.calls_of(TRIGGER)) == calls_before + 1, (
            "failure_cooldown_minutes=0 时瞬时失败必须照旧重试"
        )

    asyncio.run(run())


def test_unrecognised_error_is_treated_as_transient():
    """认不出的错误一律按抖动处理：宁可多试一次，也不误伤。"""

    async def run():
        _, plugin, host = _make_plugin()
        now = time.time()
        for error in ("", None, "timeout", "连接被重置"):
            plugin._proactive_denied.until.clear()
            plugin._proactive_denied.apply_if_policy(SESSION, now, error)
            assert not plugin._proactive_denied.until, f"{error!r} 不该被认成策略拒绝"

        for error in (POLICY_ERROR, "适配器名单禁止创建聊天运行实例: x",
                      "HeartflowPolicyDenied", "policy denied", "不允许接入"):
            plugin._proactive_denied.until.clear()
            plugin._proactive_denied.apply_if_policy(SESSION, now, error)
            assert plugin._proactive_denied.until, f"{error!r} 应被认成策略拒绝"

    asyncio.run(run())


# ------------------------------------------------------------ 二、冷却表本身


def test_cooldown_expires_and_cleans_itself():
    """到期后恢复候选资格，并从表里删掉（不能让字典无限增长）。"""

    async def run():
        _, plugin, host = _make_plugin()
        now = time.time()
        plugin._proactive_denied.note("group-9", now)
        assert plugin._proactive_denied.is_denied("group-9", now + 1799.0)
        assert not plugin._proactive_denied.is_denied("group-9", now + 1801.0)
        assert "group-9" not in plugin._proactive_denied.until, "过期项要顺手清掉"

    asyncio.run(run())


def test_denied_table_has_an_upper_bound():
    """冷却表不能随会话数无限增长。"""

    async def run():
        _, plugin, host = _make_plugin()
        now = time.time()
        for i in range(600):
            plugin._proactive_denied.note(f"group-{i}", now)
        assert len(plugin._proactive_denied.until) <= 200

    asyncio.run(run())


def test_skip_is_recorded_with_a_chinese_label():
    """跳过要进沉默台账，且 /生活 为什么 要能答出中文原因。"""

    from life_proactive import SKIP_REASON_LABELS

    assert "宿主策略" in SKIP_REASON_LABELS["host_denied"], SKIP_REASON_LABELS["host_denied"]

    async def run():
        _, plugin, host = _make_plugin()
        host.returns[TRIGGER] = {"success": False, "error": POLICY_ERROR}
        now = time.time()
        _prepare(plugin, host, now)
        await plugin._maybe_proactive(now)
        plugin._state.materials = [_material(now)]
        await plugin._maybe_proactive(now + 60.0)
        assert plugin._state.skip_ledger.get("host_denied", 0) >= 1, (
            "冷却期内的跳过必须留痕，否则台账答不出「为什么一直不开口」"
        )

    asyncio.run(run())


# ------------------------------------------- 三、冷却表跨重启（v1.23.4）


def test_denial_is_persisted_and_restored_after_restart():
    """被拒的结论必须落盘：只存内存时**每次重启**都会对名单外会话再撞一次墙。

    真机日志（10-24 19:38:57）里「失败的 ``context.append`` + 宿主未受理」这对行
    正是这么重复出现的——重启一次、撞一次、留一条满栈 traceback。
    """

    async def run():
        _, plugin, host = _make_plugin()
        host.returns[TRIGGER] = {"success": False, "error": POLICY_ERROR}
        now = time.time()
        _prepare(plugin, host, now)
        await plugin._maybe_proactive(now)

        payload = json.loads(plugin._state_path().read_text(encoding="utf-8"))
        assert payload.get("proactive_denied", {}).get(SESSION, 0.0) > now, (
            "被策略拒绝后必须立刻落盘，否则重启后这张表就没了"
        )

        # 重启：同一个 data_dir，全新实例，只走「恢复状态」这一步
        from fakehost import load_plugin_module

        module = load_plugin_module(PLUGIN_DIR, "life_frequency_denied_restart")
        plugin2 = module.create_plugin()
        host2 = FakeHost(module.__plugin_id__, paths=FakePaths(root=host.paths.root))
        ctx2 = build_context(module.__plugin_id__, rpc_call=host2.rpc_call, paths=host2.paths)
        bind_context(plugin2, ctx2, get_default_config(type(plugin2).config_model))
        plugin2.config
        plugin2._restore_state_on_start(now + 30.0)

        assert plugin2._proactive_denied.is_denied(SESSION, now + 30.0), (
            "重启后必须接回「这个会话不受理」的结论，否则又白撞一次墙"
        )

    asyncio.run(run())


def test_restore_drops_expired_and_garbage_entries():
    """恢复时过期项/坏值直接丢掉，只留还有效的；坏数据绝不能让加载失败。"""

    _, plugin, _host = _make_plugin()
    now = 5000.0
    kept = plugin._proactive_denied.restore(
        {
            "gone": now - 1,
            "live": now + 600,
            "junk": "x",
            "": now + 600,
            "nan": float("nan"),
        },
        now=now,
    )
    assert kept == 1
    assert plugin._proactive_denied.is_denied("live", now)
    assert "gone" not in plugin._proactive_denied.until, "过期项不该被接回来"

