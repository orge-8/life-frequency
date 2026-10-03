# -*- coding: utf-8 -*-
"""L3：与桌面插件集共存的契约。

这一组测试钉住三件真机事实，它们都是**读宿主源码**得出、且用本机 MaiBot 检出
（``src/maisaka/runtime.py`` / ``reply_necessity.py`` / ``plugin_runtime/host/hook_dispatcher.py``）
交叉核对过的：

1. 宿主每个会话只有一个倍率标量（``runtime.py:183/560``），``frequency.set_adjust``
   是**后写覆盖先写**。budget-pacer 也写它 ⇒ 必须做乘性合成，且卸载时**归还基数**，
   不能写 1.0（否则会永久抹掉对方的压制，因为对方只在自身目标变化时才重写）。
2. ``adjust == 0`` 会让宿主进入静默消费（``turn_scheduler.py:90``、
   ``reasoning_engine.py:1188``），**并且把其它插件的 ``maisaka.proactive.trigger``
   一并丢掉**（``_handle_silent_turn`` 消费 proactive 触发），所以静默是有代价的。
3. 宿主 dispatcher 的排序键是 ``(模式, 顺序槽, 来源, 插件 id, 名字)``
   （``hook_dispatcher.py:304-321``），blocking 一律先于 observe，且任一 blocking
   处理器 abort 会中断整条链 —— 所以 ``chat.receive.after_process`` 上的旁路记录
   必须是 BLOCKING + EARLY 才能保证被调用。
"""

import asyncio
import json
import logging
import math
import pathlib
import sys
import time
from typing import Any

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_interop")


class ScaleHost(FakeHost):
    """复刻宿主的单标量语义 + 「没有 heartflow chat 就静默 no-op」规则。

    ``live`` 就是 ``heartflow_manager.heartflow_chat_list`` 的键集合：
    ``data.py:809-814`` 在会话不存在时读回 **1.0**，
    ``heartflow_manager.py:103-111`` 的 ``adjust_talk_frequency`` 找不到会话时
    只记 warning 然后 no-op，而 ``data.py:838-840`` 的能力层**照样返回 success**。
    """

    def __init__(self, plugin_id: str, paths: FakePaths) -> None:
        super().__init__(plugin_id, paths=paths)
        self.adjust: dict[str, float] = {}
        self.live: set[str] = {"group-1"}
        self.noop_writes: list[str] = []
        self.read_fails = False
        self.sessions = [
            {
                "session_id": "group-1",
                "stream_id": "group-1",
                "platform": "qq",
                "group_id": "123456",
                "user_id": "10001",
                "is_group_session": True,
                "chat_type": "group",
            }
        ]
        self.config_values = {
            "chat.reply_timing.reply_trigger_mode": "reply_necessity",
            "chat.reply_timing.talk_value": 0.6,
            "personality.personality": "十九岁，话少。",
            "bot.nickname": "麦麦",
        }

    #: 扮演 budget-pacer：直接写宿主标量，不留痕迹
    def foreign_write(self, chat_id: str, value: float) -> None:
        self.adjust[chat_id] = float(value)

    async def rpc_call(self, method, plugin_id="", payload=None, **kwargs):
        kw = dict(payload or {})
        capability = kw.get("capability") or method
        args = kw.get("args") or {}
        self.calls.append((capability, args))
        if capability == "config.get":
            key = str(args.get("key") or "")
            if key in self.config_values:
                return {"success": True, "value": self.config_values[key]}
            return {"success": True, "value": args.get("default")}
        if capability == "chat.get_all_streams":
            return {"success": True, "streams": list(self.sessions)}
        if capability == "frequency.get_adjust":
            if self.read_fails:
                return {"success": False, "error": "boom"}
            session_id = str(args.get("chat_id") or "")
            if session_id not in self.live:
                return {"success": True, "value": 1.0}   # data.py:809-814
            return {"success": True, "value": self.adjust.get(session_id, 1.0)}
        if capability == "frequency.get_current_talk_value":
            # 复刻宿主 data.py:816-828：``倍率 × 基础频率``，而基础频率按会话类型取
            # （utils_config.py:601-608：私聊用 private_talk_value，其余用 talk_value）。
            session_id = str(args.get("chat_id") or "")
            base = self._base_talk_value(session_id)
            adjust = self.adjust.get(session_id, 1.0) if session_id in self.live else 1.0
            return {"success": True, "value": adjust * base}
        if capability == "frequency.set_adjust":
            session_id = str(args.get("chat_id") or "")
            value = float(args.get("value") or 0.0)
            if session_id in self.live:
                self.adjust[session_id] = value
            else:
                # heartflow_manager.adjust_talk_frequency 的 no-op 分支（只记 warning）
                self.noop_writes.append(session_id)
            return {"success": True}                      # 无论如何都是 success
        return await super().rpc_call(method, plugin_id, payload, **kwargs)

    def _base_talk_value(self, session_id: str) -> float:
        """宿主 ``ChatConfigUtils.get_talk_value`` 的复刻（私聊走另一个键）。"""

        session = next((s for s in self.sessions if s.get("session_id") == session_id), None)
        is_group = None if session is None else bool(session.get("is_group_session"))
        if is_group is False:
            return float(self.config_values.get("chat.reply_timing.private_talk_value", 1.0))
        return float(self.config_values.get("chat.reply_timing.talk_value", 1.0))


def _make_plugin(**config_overrides):
    """起一个已 bind 好配置、但没跑 on_load 的插件实例。

    ⚠ 夹具里刻意把 ``apply.only_active_sessions`` 关掉：本文件几乎不经过
    ``note_session``，而真机默认（``true``）只干预「有活动迹象」的会话，
    这些用例会因为「一个目标都没有」而变成空转。验证默认行为的用例自己把它设回
    ``True``（见 ``test_only_active_sessions_skips_history_but_covers_on_message``）。
    """

    module = _load()
    plugin = module.create_plugin()
    host = ScaleHost(module.__plugin_id__, FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["frequency"]["quiet_hours"] = []
    config["events"]["fire_probability"] = 0.0
    config["proactive"]["enabled"] = False
    config["simulation"]["dry_run"] = False
    config["apply"]["only_active_sessions"] = False
    for section, values in config_overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin._state.last_tick_at = time.time()
    return module, plugin, host


def _restart(module, paths, adjust):
    """模拟真机重启：**同一个数据目录**，宿主上留着 adjust。

    ⚠ 必须调用插件自己的 ``_restore_state_on_start``，不能手写
    ``plugin._state = plugin._load_state()``：v1.1.0 的测试就是这样绕过 on_load 的
    冷启动重建分支，于是「记忆被丢掉、倍率乘两次」的真 bug 在本地一直绿
    （``_load_state()`` 救回了记忆，on_load 又把它替换掉）。
    """

    plugin = module.create_plugin()
    host = ScaleHost(module.__plugin_id__, paths)
    host.foreign_write("group-1", adjust)
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=paths)
    config = get_default_config(type(plugin).config_model)
    config["frequency"]["quiet_hours"] = []
    config["events"]["fire_probability"] = 0.0
    config["proactive"]["enabled"] = False
    bind_context(plugin, ctx, config)
    plugin._rebuild_from_config()
    plugin._restore_state_on_start(time.time())      # ← on_load 真正做的事
    return plugin, host


# ---------------------------------------------------------------- 一、倍率合成


def test_plain_case_when_nobody_else_writes():
    """没有别人写时，下发值就是纯生活倍率。"""

    async def run():
        _, plugin, host = _make_plugin()

        target, current = await plugin._resolve_target("group-1", 0.8, time.time())
        assert target == pytest.approx(0.8)
        # 宿主上默认 1.0，会被认成基数；这是对的：1.0 就是「没有外部压制」
        assert current == pytest.approx(1.0)
        assert plugin._state.foreign["group-1"] == pytest.approx(1.0)

    asyncio.run(run())


def test_composes_with_foreign_scalar():
    """别的插件写了 0.5 ⇒ 下发 0.5 × 生活倍率。"""

    async def run():
        _, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)

        target, current = await plugin._resolve_target("group-1", 0.8, time.time())
        assert target == pytest.approx(0.4)
        assert current == pytest.approx(0.5)
        assert plugin._state.foreign["group-1"] == pytest.approx(0.5)

    asyncio.run(run())


def test_composition_is_idempotent_and_never_double_multiplies():
    """核心回归：第二轮**不能**再乘一次基数（0.5×0.8×0.8）。"""

    async def run():
        _, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)

        await plugin._apply_sweep(time.time())
        first = host.adjust["group-1"]
        assert first == pytest.approx(0.5 * plugin._last_breakdown.adjust)

        writes = len(host.calls_of("frequency.set_adjust"))
        await plugin._apply_sweep(time.time())
        assert host.adjust["group-1"] == pytest.approx(first)
        assert len(host.calls_of("frequency.set_adjust")) == writes, "值没变却重复写 RPC"

    asyncio.run(run())


def test_foreign_change_is_picked_up():
    """外部基数变了 ⇒ 重新合成。"""

    async def run():
        _, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        factor = plugin._last_breakdown.adjust

        host.foreign_write("group-1", 0.2)
        await plugin._apply_sweep(time.time())
        assert host.adjust["group-1"] == pytest.approx(0.2 * factor)
        assert plugin._state.foreign["group-1"] == pytest.approx(0.2)

    asyncio.run(run())


def test_read_failure_writes_nothing_and_recovers():
    """读不到宿主现值时**什么都不做**。

    反例（曾经的实现）：拿缓存里的旧基数盲写 `旧基数 × 生活倍率`，这一笔会被记成
    「我们自己写的」，此后每轮都认不出差异 —— 如果期间 budget-pacer 改过基数，
    错误值就**永久**留存（它只在自身目标变化时才重写，不会来救）。
    """

    async def run():
        _, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        composed = host.adjust["group-1"]
        assert composed == pytest.approx(0.5 * plugin._last_breakdown.adjust)

        host.read_fails = True
        assert await plugin._resolve_target("group-1", 0.8, time.time()) is None
        await plugin._apply_sweep(time.time())
        assert host.adjust["group-1"] == pytest.approx(composed), "读失败时盲写了"
        assert plugin._state.applied["group-1"] == pytest.approx(composed)

        # 读恢复后：若外部基数在故障期间变了，必须重新识别
        host.foreign_write("group-1", 0.2)
        host.read_fails = False
        await plugin._apply_sweep(time.time())
        assert host.adjust["group-1"] == pytest.approx(
            0.2 * plugin._last_breakdown.adjust
        ), "故障期间的外部变化没有被追上"

    asyncio.run(run())


def test_compose_disabled_keeps_legacy_overwrite_behavior():
    """关掉合成＝旧行为：直接覆盖，也不去读宿主。"""

    async def run():
        _, plugin, host = _make_plugin(apply={"compose_external": False})
        host.foreign_write("group-1", 0.5)

        await plugin._apply_sweep(time.time())
        assert host.adjust["group-1"] == pytest.approx(plugin._last_breakdown.adjust)
        assert plugin._state.foreign == {}
        assert not host.calls_of("frequency.get_adjust"), "关掉合成就该连读都不读"

    asyncio.run(run())


def test_dry_run_never_reads_or_writes():
    async def run():
        _, plugin, host = _make_plugin(simulation={"dry_run": True})
        await plugin._apply_sweep(time.time())
        assert not host.calls_of("frequency.set_adjust")
        assert not host.calls_of("frequency.get_adjust")
        assert host.adjust == {}

    asyncio.run(run())


# ---------------------------------------------------------------- 二、归还基数


def test_restore_hands_back_foreign_base_not_one():
    """卸载/暂停时归还基数，而不是写 1.0。"""

    async def run():
        _, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        assert host.adjust["group-1"] != pytest.approx(0.5)

        await plugin._restore_baseline()
        assert host.adjust["group-1"] == pytest.approx(0.5)
        assert plugin._state.applied == {}
        assert plugin._state.foreign == {}

    asyncio.run(run())


def test_restore_does_not_clobber_a_later_foreign_write():
    """有人在我们之后写过 ⇒ 现值里已无我们的因子，别踩它。"""

    async def run():
        _, plugin, host = _make_plugin()
        await plugin._apply_sweep(time.time())
        # 模拟 budget-pacer 在我们之后写了自己的目标
        host.foreign_write("group-1", 0.33)
        before = len(host.calls_of("frequency.set_adjust"))

        await plugin._restore_baseline()
        assert host.adjust["group-1"] == pytest.approx(0.33), "把自己的因子归还时踩掉了别人"
        assert len(host.calls_of("frequency.set_adjust")) == before, "不该再写 RPC"

    asyncio.run(run())


def test_restore_with_compose_off_writes_one():
    """关闭合成时保持旧语义：写回 1.0。"""

    async def run():
        _, plugin, host = _make_plugin(apply={"compose_external": False})
        await plugin._apply_sweep(time.time())
        await plugin._restore_baseline()
        assert host.adjust["group-1"] == pytest.approx(1.0)

    asyncio.run(run())


def test_restore_in_dry_run_writes_nothing_but_keeps_memory():
    """演算模式下卸载：不写宿主，但**必须保留倍率记忆**（否则下次启动会乘两次）。"""

    async def run():
        _, plugin, host = _make_plugin(simulation={"dry_run": True})
        plugin._state.applied["group-1"] = 0.4
        plugin._state.foreign["group-1"] = 0.5
        await plugin._restore_baseline()
        assert host.adjust == {}, "演算模式不该写宿主"
        assert plugin._state.applied == {"group-1": 0.4}, "演算模式下不该抹掉记忆"
        assert plugin._state.foreign == {"group-1": 0.5}

    asyncio.run(run())


# ---------------------------------------------------------------- 三、重锚


def test_reanchor_discards_foreign_base_and_stays_discarded():
    """外部写入方被卸载但倍率留在宿主上时，重锚要能一键恢复，且不会被重新学回来。"""

    async def run():
        _, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        assert plugin._state.foreign["group-1"] == pytest.approx(0.5)

        written = await plugin._reanchor(time.time())
        assert written == 1
        factor = plugin._last_breakdown.adjust
        assert host.adjust["group-1"] == pytest.approx(factor)
        # 重锚后基数就是 1.0（外部那一层被丢弃），且记忆只覆盖真正写过的会话
        assert plugin._state.foreign == {"group-1": pytest.approx(1.0)}
        assert plugin._state.observed == {}

        # 下一轮：宿主值 == 自己写的值 ⇒ 不再把它当外部基数重复相乘
        await plugin._apply_sweep(time.time())
        assert host.adjust["group-1"] == pytest.approx(factor)

    asyncio.run(run())


def test_leaving_and_reentering_scope_never_double_applies():
    """回归：会话暂时离开生效范围（改 filter_mode / 会话列表临时失败）**不能**让我们
    忘掉「宿主上那一层是自己写的」。

    否则它回到范围时，现值会被误判成外部基数，`0.5×0.8` 变成 `0.5×0.8×0.8`，
    而且这个错误的基数会被永久记住（budget-pacer 不会自愈）。
    """

    async def run():
        _, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        composed = host.adjust["group-1"]
        assert composed == pytest.approx(0.5 * plugin._last_breakdown.adjust)

        # 本轮没有任何目标会话：会话列表失败 + 没有任何消息旁路记录
        host.sessions = []
        plugin._seen_sessions.clear()
        await plugin._apply_sweep(time.time())
        assert plugin._state.applied == {"group-1": pytest.approx(composed)}, (
            f"把『自己写过的值』清掉了：{plugin._state.applied}"
        )
        assert plugin._state.foreign == {"group-1": pytest.approx(0.5)}
        assert host.adjust["group-1"] == pytest.approx(composed)

        # 回到范围内：必须是同一个值，不能再乘一次生活倍率
        host.sessions = [
            {
                "session_id": "group-1",
                "stream_id": "group-1",
                "platform": "qq",
                "group_id": "123456",
                "user_id": "10001",
                "is_group_session": True,
                "chat_type": "group",
            }
        ]
        await plugin._apply_sweep(time.time())
        assert host.adjust["group-1"] == pytest.approx(composed)
        assert plugin._state.foreign["group-1"] == pytest.approx(0.5)

    asyncio.run(run())


def test_memory_maps_are_capped_not_silently_unbounded():
    """会话记录不能无限增长，但清理只能发生在远超真实会话数的情况下。"""

    async def run():
        import plugin as P

        _, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        # 灌到刚好超过上限：只有「不在范围内」的才会被清掉
        plugin._state.applied.update({f"ghost-{i}": 0.5 for i in range(P._ADJUST_MEMORY_LIMIT + 5)})
        plugin._state.foreign.update({f"ghost-{i}": 0.5 for i in range(P._ADJUST_MEMORY_LIMIT + 5)})

        await plugin._apply_sweep(time.time())
        assert "group-1" in plugin._state.applied, "把在范围内的会话记录清掉了"
        assert len(plugin._state.applied) <= P._ADJUST_MEMORY_LIMIT + 1
        assert not [k for k in plugin._state.foreign if k.startswith("ghost-")]

    asyncio.run(run())


def test_randomized_interleaving_never_diverges():
    """随机交错 200 轮（外部写入 / 读失败 / 会话列表失败 / 倍率漂移）后的不变量。

    不变量：每次**成功**的巡检之后，宿主上的倍率都等于 ``外部基数 × 生活倍率``
    （容差 = 去抖阈值），且功能故障停止后一轮内收敛。
    """

    async def run():
        import random

        _, plugin, host = _make_plugin()
        rng = random.Random(20261001)
        base = 1.0
        host.foreign_write("group-1", base)
        saved = host.sessions

        for _ in range(200):
            if rng.random() < 0.25:
                base = round(rng.uniform(0.05, 1.0), 3)
                host.foreign_write("group-1", base)
            plugin._state.activity = "daily"
            plugin._state.emotion = rng.uniform(1.0, 10.0)
            plugin._state.energy = rng.uniform(1.0, 10.0)
            plugin._state.cold_until = 0.0
            plugin._state.sleep_debt_nights = 0
            plugin._last_breakdown = None

            host.read_fails = rng.random() < 0.1
            blank = rng.random() < 0.1
            if blank:
                host.sessions = []
                plugin._seen_sessions.clear()
            writes_before = len(host.calls_of("frequency.set_adjust"))
            await plugin._apply_sweep(time.time())
            writes_after = len(host.calls_of("frequency.set_adjust"))
            host.sessions = saved
            # 每次巡检、每个会话最多一次 set_adjust —— 它是会唤醒 Planner 的昂贵操作
            assert writes_after - writes_before <= max(1, len(plugin._state.applied) + 1)

            if not host.read_fails and not blank:
                factor = plugin._last_breakdown.adjust
                want = base * factor
                got = float(host.adjust["group-1"])
                assert abs(got - want) <= 1e-6, f"diverged: host={got} want={want}"
                assert plugin._state.foreign["group-1"] == pytest.approx(base)

        # 故障停止后必须收敛
        host.read_fails = False
        await plugin._apply_sweep(time.time())
        assert float(host.adjust["group-1"]) == pytest.approx(
            base * plugin._last_breakdown.adjust, abs=1e-6
        )

    asyncio.run(run())


def test_write_that_does_not_stick_is_backed_off_not_retried_forever():
    """回归（真机语义）：会话在 ``chat.get_all_streams`` 里但**还没有 heartflow chat** 时，
    宿主的 ``adjust_talk_frequency`` 会静默 no-op（只记 warning），而能力层照样返回
    ``success``（``heartflow_manager.py:103-111`` / ``data.py:838-840``）。

    曾经的实现每个巡检都白写一遍：`chat_manager.sessions` 是**全部已知聊天流**，
    几百群的实例每分钟就能刷出几百条无用写入 + 几百条宿主 warning。
    """

    async def run():
        _, plugin, host = _make_plugin()
        host.live = set()                       # 会话存在，但没有 heartflow chat 对象

        await plugin._apply_sweep(time.time())          # 第 1 轮：尝试写入
        assert len(host.calls_of("frequency.set_adjust")) == 1, "第一次尝试写入是应该的"
        await plugin._apply_sweep(time.time())          # 第 2 轮：发现没生效 → 退避
        assert plugin._state.unbacked.get("group-1"), "没识别出这一笔没生效"
        assert "group-1" not in plugin._state.applied, "没生效的写入不该记成自己写的"

        for _ in range(5):
            await plugin._apply_sweep(time.time())
        assert len(host.calls_of("frequency.set_adjust")) == 1, "退避期间还在反复写"
        assert host.adjust.get("group-1") is None

        # 有了消息 ⇒ 取消退避并立刻重试；这时 heartflow chat 已存在
        host.live = {"group-1"}
        await plugin.note_session(message={"session_id": "group-1", "group_id": "123456"})
        assert "group-1" not in plugin._state.unbacked, "有消息进来却没取消退避"
        await plugin._apply_sweep(time.time())
        assert len(host.calls_of("frequency.set_adjust")) == 2, "没重试"
        await plugin._apply_sweep(time.time())          # 第二轮确认写入生效
        assert float(host.adjust["group-1"]) == pytest.approx(
            plugin._last_breakdown.adjust
        ), "会话变活之后没有重新下发"

    asyncio.run(run())


def test_state_memory_survives_schema_version_mismatch():
    """回归：STATE_VERSION 不匹配时不能把倍率记忆一起丢掉。

    记忆描述的是**宿主上的值**，与生活状态的结构无关。丢掉它，下一次巡检就会把
    「外部基数 × 我们的因子」整个当成外部基数，再乘一次生活倍率（且错误永久留存）。
    """

    async def run():
        _, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        composed = float(host.adjust["group-1"])
        plugin._save_state()

        payload = json.loads(plugin._state_path().read_text(encoding="utf-8"))
        payload["state_version"] = int(payload.get("state_version", 1)) + 1
        plugin._state_path().write_text(json.dumps(payload), encoding="utf-8")

        reloaded = plugin._load_state()
        assert reloaded.applied, "版本不匹配时丢了 applied"
        assert reloaded.foreign == {"group-1": pytest.approx(0.5)}
        assert reloaded.activity, "生活状态本身仍应重置"

        plugin._state = reloaded
        await plugin._apply_sweep(time.time())
        assert float(host.adjust["group-1"]) == pytest.approx(composed), "被乘了两次"

    asyncio.run(run())


def test_dry_run_flip_at_unload_keeps_memory():
    """回归：dry_run 打开后卸载，不能把倍率记忆抹掉。

    演算模式下一笔都没写，宿主上的值没被我们动过 ⇒ 记忆仍然有效。
    抹掉它，重启后就会把「别人的基数 × 我们的因子」当成基数再乘一次。
    """

    async def run():
        _, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        composed = float(host.adjust["group-1"])

        plugin.config.simulation.dry_run = True     # 用户中途打开演算模式
        await plugin.on_unload()
        assert plugin._state.foreign == {"group-1": pytest.approx(0.5)}, "记忆被抹掉了"
        assert plugin._state.applied, "记忆被抹掉了"

        # 关掉演算模式继续跑：宿主上留着 0.5×生活倍率，重新接管时不能再乘一次
        plugin.config.simulation.dry_run = False
        plugin._stopping = False
        await plugin._apply_sweep(time.time())
        assert float(host.adjust["group-1"]) == pytest.approx(composed), "被乘了两次"

    asyncio.run(run())


def test_command_pattern_does_not_shadow_other_plugins_commands():
    """回归：``/生活`` 的匹配必须锚在消息开头。

    宿主的命令匹配是 ``pattern.search()`` 且**第一个命中的插件赢、立刻返回**
    （``component_query.py:665-679``），所以宽松的 ``(?<!\\S)生活`` 会把
    ``/点歌 生活``、``/卡片 生活``、``/身份 生活`` 这类「别的命令 + 生活做参数」
    的消息整条截胡，对方那条命令永远不会执行。这一条把两个方向都钉住。
    """

    import ast
    import re

    source = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
    pattern_text = None
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "cmd_life_state":
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call):
                    for kw in decorator.keywords:
                        if kw.arg == "pattern":
                            pattern_text = ast.literal_eval(kw.value)
    assert pattern_text, "没找到 /生活 的 pattern"
    rx = re.compile(pattern_text)

    # 必须命中（群里 @麦麦 /生活 会带前缀，方括号段也要能穿过去）
    for text in ("生活", "/生活", "／生活", "生活 状态", "/生活 频率", "  /生活",
                 "@麦麦 生活", "@麦麦 /生活 活动", "[CQ:at,qq=12345] 生活"):
        assert rx.search(text), f"该命中的没命中：{text!r}"

    # 绝不能命中：别人的命令把「生活」当参数，以及普通聊天里出现「生活」
    for text in (
        "我的生活", "今天 生活 好累", "生活得好累",
        "/点歌 生活", "/本地歌 生活", "/卡片 生活", "/欢迎 生活", "/身份 生活",
        "/动态 生活", "/预算 生活", "/websearch 生活", "/歌词 搜索 生活",
        "/导入歌词 生活", "/日记 生活", "/撤回 生活", "/帮助 生活",
    ):
        assert not rx.search(text), f"会截胡别的命令/误触：{text!r}"

    # 反向：其它插件的命令正则不能命中本插件的用法（桌面插件存在时才跑）
    desktop = pathlib.Path.home() / "Desktop"
    if not desktop.is_dir():
        return
    for other in sorted(desktop.glob("*/plugin.py")):
        if other.parent.name == "life-frequency":
            continue
        text = other.read_text(encoding="utf-8", errors="replace")
        for literal in re.findall(r'pattern=r?"([^"]*)"', text):
            try:
                other_rx = re.compile(literal)
            except re.error:
                continue
            for mine in ("/生活", "生活 状态"):
                assert not other_rx.search(mine), (
                    f"{other.parent.name} 的命令正则也会命中 {mine!r}：{literal!r}"
                )


def test_real_restart_with_same_state_file_does_not_double_apply():
    """真机重启路径：同一个数据目录 + ``_load_state()``，宿主上留着我们上次的合成值。"""

    async def run():
        module, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        composed = float(host.adjust["group-1"])
        plugin._save_state()

        plugin2, host2 = _restart(module, host.paths, composed)
        assert plugin2._state.foreign == {"group-1": pytest.approx(0.5)}, "重启后没读到基数"
        await plugin2._apply_sweep(time.time())
        assert float(host2.adjust["group-1"]) == pytest.approx(composed), "重启后被乘了两次"

    asyncio.run(run())


def test_reanchor_covers_out_of_scope_sessions_it_remembers():
    """回归：重锚必须连「记得写过、但现在不在生效范围内」的会话一起处理。

    只清表不写那些会话，就会留下「宿主上有我们的因子、而我们忘了」的空档；
    等它回到范围，现值会被当成外部基数 ⇒ 生活倍率被乘两次，且错误基数永久留存。
    """

    async def run():
        _, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        assert plugin._state.foreign["group-1"] == pytest.approx(0.5)

        # 把 group-1 移出生效范围（记忆必须保留）
        plugin.config.apply.filter_mode = "whitelist"
        plugin.config.apply.target_chats = ["group:999"]
        await plugin._apply_sweep(time.time())
        assert "group-1" in plugin._state.applied

        written = await plugin._reanchor(time.time())
        assert written == 1, "离范围的已记忆会话没有被重锚"
        factor = plugin._last_breakdown.adjust
        assert float(host.adjust["group-1"]) == pytest.approx(factor)
        assert plugin._state.foreign == {"group-1": pytest.approx(1.0)}

        # 回到范围内：不能再乘一次
        plugin.config.apply.filter_mode = "all"
        await plugin._apply_sweep(time.time())
        assert float(host.adjust["group-1"]) == pytest.approx(factor), "重锚后被乘了两次"

    asyncio.run(run())


def test_reanchor_in_dry_run_changes_nothing():
    async def run():
        _, plugin, host = _make_plugin(simulation={"dry_run": True})
        plugin._state.applied = {"group-1": 0.4}
        plugin._state.foreign = {"group-1": 0.5}
        assert await plugin._reanchor(time.time()) == 0
        assert host.adjust == {}
        assert plugin._state.applied == {"group-1": 0.4}, "演算模式不该改记忆"
        assert plugin._state.foreign == {"group-1": 0.5}

    asyncio.run(run())


def test_corrupt_state_file_falls_back_to_memory_sidecar():
    """回归：状态文件损坏/缺失时，对账记忆要从 adjust_memory.json 救回来。

    救不回来的话，下次巡检会把「外部基数 × 我们的因子」当成外部基数再乘一次，
    而且这个错误基数会被永久记住（budget-pacer 只在自身目标变化时才重写）。
    """

    async def run():
        module, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        composed = float(host.adjust["group-1"])
        plugin._save_state()

        memory_path = plugin._memory_path()
        assert memory_path.is_file(), "没有写对账记忆的副文件"
        assert json.loads(memory_path.read_text(encoding="utf-8"))["foreign"]

        # (a) 主文件损坏
        plugin._state_path().write_text('{"state_version": 1, "applied": ', encoding="utf-8")
        reloaded = plugin._load_state()
        assert reloaded.foreign == {"group-1": pytest.approx(0.5)}, "损坏时没救回记忆"

        # (b) 主文件整个丢失
        plugin._state_path().unlink()
        reloaded2 = plugin._load_state()
        assert reloaded2.foreign == {"group-1": pytest.approx(0.5)}, "丢失时没救回记忆"

        # 真机路径：拿救回来的记忆继续跑，不能再乘一次
        plugin._state = reloaded2
        await plugin._apply_sweep(time.time())
        assert float(host.adjust["group-1"]) == pytest.approx(composed), "被乘了两次"

    asyncio.run(run())


def test_backoff_does_not_block_a_changed_target():
    """回归：退避只针对**同一个目标值**。

    否则进入睡眠要归零时会被退避挡住，她会带着全速倍率睡觉最长一个退避窗口。
    """

    async def run():
        _, plugin, host = _make_plugin()
        host.live = set()                        # 写不进去 → 进入退避
        await plugin._apply_sweep(time.time())
        await plugin._apply_sweep(time.time())
        assert plugin._state.unbacked.get("group-1")
        writes = len(host.calls_of("frequency.set_adjust"))

        # 目标大幅变化（硬闸归零）⇒ 必须无视退避按新目标重试
        plugin._state.activity = "sleep"
        plugin._state.activity_since = time.time() - 3600
        host.live = {"group-1"}                  # 现在能写进去了
        await plugin._apply_sweep(time.time())
        assert len(host.calls_of("frequency.set_adjust")) == writes + 1, "退避挡住了新目标"
        assert float(host.adjust["group-1"]) == pytest.approx(0.0)

    asyncio.run(run())


def test_backoff_is_exponential_for_dead_sessions():
    """历史会话可能成百上千，固定间隔重试会攒出 10^4/天 级无用 RPC 与宿主 warning。

    ⚠ 断言比较的是**窗口长度**，不是绝对截止时刻：v1.1.0 写的是
    ``unbacked > first + retry``，两边差值恒为 ``retry``，只有两次 ``time.time()``
    恰好跨过时钟 tick 时才严格成立——而 Windows 上 ``time.time()`` 粒度约 15.6ms
    （本机实测 20 次连续调用只返回 1 个不同值），于是这条用例时红时绿，
    把门禁变成了掷骰子（产品实现本身是对的：窗口 900→1800，比值 2.0）。
    """

    async def run():
        _, plugin, host = _make_plugin()
        host.live = set()                        # 会话在，但永远没有 heartflow chat

        t1 = time.time()
        await plugin._apply_sweep(t1)
        await plugin._apply_sweep(t1)
        first = float(plugin._state.unbacked["group-1"])
        window1 = first - t1
        assert plugin._state.unbacked_strikes["group-1"] == 1
        assert window1 > 0
        assert window1 <= plugin._unbacked_retry_seconds() + 1

        # 退避到期后重试：一轮真的写、下一轮才发现又没生效（所以是两轮）
        plugin._state.unbacked["group-1"] = t1 - 1
        await plugin._apply_sweep(t1)              # 重试写入
        assert plugin._state.applied.get("group-1"), "到期后没有重试"
        await plugin._apply_sweep(t1)              # 发现再次失败
        assert plugin._state.unbacked_strikes["group-1"] == 2
        window2 = float(plugin._state.unbacked["group-1"]) - t1
        assert window2 == pytest.approx(2.0 * window1), f"不是指数退避：{window1} -> {window2}"

        # 永不超上限
        plugin._state.unbacked_strikes["group-1"] = 50.0
        plugin._state.unbacked["group-1"] = t1 - 1
        await plugin._apply_sweep(t1)
        await plugin._apply_sweep(t1)
        assert float(plugin._state.unbacked["group-1"]) <= t1 + 24 * 3600 + 2

    asyncio.run(run())


def test_memory_survives_real_start_path_when_main_state_file_is_lost():
    """回归（v1.1.0 真 bug）：主状态文件丢失后，**走 on_load 那条路**也要留住对账记忆。

    README 承诺「两份分开存，主文件丢了也能从副文件救回来」。v1.1.0 里
    ``_load_state()`` 确实救回了记忆，但 on_load 紧接着用 ``new_state()`` 把它整体
    替换掉，于是下一次巡检把「外部基数 × 生活倍率」当成外部基数再乘一次，
    而且错误基数会永久留存。
    """

    async def run():
        module, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        composed = float(host.adjust["group-1"])
        plugin._save_state()

        plugin._state_path().unlink()                 # 只删主文件
        assert plugin._memory_path().is_file()

        plugin2, host2 = _restart(module, host.paths, composed)
        assert plugin2._state.foreign == {"group-1": pytest.approx(0.5)}, "真机启动路径丢了记忆"
        await plugin2._apply_sweep(time.time())
        # 生活状态被重置 ⇒ 这一轮的生活因子与重启前不同，所以比较的是
        # 「外部基数 × 新因子」（一次乘法），而不是重启前那个绝对值。
        assert float(host2.adjust["group-1"]) == pytest.approx(
            0.5 * plugin2._last_breakdown.adjust
        ), "被乘了两次"

    asyncio.run(run())


def test_memory_survives_real_start_path_on_state_version_bump():
    """同上，但触发条件是 ``STATE_VERSION`` 升版（README 明确承诺的另一条）。"""

    async def run():
        module, plugin, host = _make_plugin()
        host.foreign_write("group-1", 0.5)
        await plugin._apply_sweep(time.time())
        composed = float(host.adjust["group-1"])
        plugin._save_state()

        payload = json.loads(plugin._state_path().read_text(encoding="utf-8"))
        payload["state_version"] = int(payload.get("state_version", 1)) + 1
        plugin._state_path().write_text(json.dumps(payload), encoding="utf-8")

        plugin2, host2 = _restart(module, host.paths, composed)
        assert plugin2._state.foreign == {"group-1": pytest.approx(0.5)}, "升版路径丢了记忆"
        assert plugin2._state.activity, "生活状态本身仍应重置为冷启动"
        await plugin2._apply_sweep(time.time())
        assert float(host2.adjust["group-1"]) == pytest.approx(
            0.5 * plugin2._last_breakdown.adjust
        ), "被乘了两次"

    asyncio.run(run())


# ---------------------------------------------------------------- 四、静默下限


def _factor_config(**overrides):
    import life_factors as F

    kwargs = dict(activity_factors={"sleep": 0.0, "daily": 1.0}, quiet_hours=())
    kwargs.update(overrides)
    return F.FactorConfig(**kwargs)


def test_hard_gates_stay_zero_by_default():
    """默认必须保持「睡就是睡」：硬闸倍率 0。"""

    import life_factors as F

    config = _factor_config()
    assert config.silence_floor == 0.0
    sleep = F.compute_adjust(
        activity="sleep", emotion=5.0, energy=5.0, sick=False, sleep_debt_nights=0,
        date_factor=1.0, material_count=0, now_minutes=600, config=config,
    )
    assert sleep.adjust == 0.0 and sleep.reason == F.REASON_SLEEP


def test_silence_floor_raises_hard_gate():
    import life_factors as F

    config = _factor_config(silence_floor=0.02, quiet_hours=((23 * 60 + 30, 8 * 60),))
    sleep = F.compute_adjust(
        activity="sleep", emotion=5.0, energy=5.0, sick=False, sleep_debt_nights=0,
        date_factor=1.0, material_count=0, now_minutes=600, config=config,
    )
    assert sleep.adjust == pytest.approx(0.02)
    assert sleep.reason == F.REASON_SLEEP, "原因仍要标成睡眠，好让 /生活 说清楚"

    quiet = F.compute_adjust(
        activity="daily", emotion=5.0, energy=5.0, sick=False, sleep_debt_nights=0,
        date_factor=1.0, material_count=0, now_minutes=2 * 60, config=config,
    )
    assert quiet.adjust == pytest.approx(0.02)
    assert quiet.reason == F.REASON_QUIET_HOURS


def test_silence_floor_leaks_at_mentions_in_necessity_mode():
    """⚠ 把静默下限设成 >0 就不再安静：评分门里**@ 会穿透**。

    这是宿主常量决定的，不是本插件的 bug：``necessity_factor`` 的值域是 ``[0.5, 1.0]``，
    ``@`` 给 100 分相关性、内容分上限 70 ⇒ ``(100+70) × 0.5 = 85 ≥ 80``。
    所以「睡眠期间连 @ 都不回」与「保留其它插件的主动开口」二者不可兼得，
    默认必须是 0.0，README 把这个取舍写清楚。
    """

    import life_host_model as M

    line = M.NECESSITY_TRIGGER_SCORE
    tiny = 1e-9  # 任何 > 0 的倍率都不再是宿主眼里的「静默」

    def score(relevance: int, content: int) -> int:
        return M.necessity_score(
            effective_frequency=tiny, relevance=relevance, content=content,
            pending_count=1, threshold=M.trigger_threshold("reply_necessity", tiny),
        )

    assert M.CONTENT_MAX_SCORE == 70
    assert score(M.RELEVANCE_AT, M.CONTENT_MAX_SCORE) == 85, "@ + 长请求 穿透（85 ≥ 80）"
    assert score(M.RELEVANCE_MENTION, M.CONTENT_MAX_SCORE) == 75, "仅提及不该穿透"
    assert score(M.RELEVANCE_INDIRECT, M.CONTENT_MAX_SCORE) == 55, "私聊不该穿透"
    assert score(M.RELEVANCE_PLAIN_GROUP, M.CONTENT_MAX_SCORE) == 35, "普通闲聊不该穿透"

    # 精确的 0 才是真静默：宿主在那条分支上根本不看评分
    assert M.is_silent(0.0) is True
    assert M.is_silent(tiny) is False
    assert M.necessity_factor(0.0) == pytest.approx(0.5)
    assert M.necessity_factor(tiny) == pytest.approx(0.5)


def test_note_session_is_blocking_early_and_never_aborts():
    """宿主里 blocking 先于 observe 跑，且 abort 会断链 ⇒ 旁路记录必须 BLOCKING+EARLY。"""

    import ast

    source = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "note_session":
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            flat = ast.unparse(decorator)
            if "HookHandler" in flat:
                found = flat
    assert found, "没找到 note_session 的 HookHandler 装饰器"
    assert "chat.receive.after_process" in found
    assert "BLOCKING" in found, f"必须是 blocking 才能先于 aborted 的 blocking 链被执行：{found}"
    assert "EARLY" in found, f"必须是 EARLY：{found}"
    assert "OBSERVE" not in found, f"observe 排在所有 blocking 之后：{found}"


def test_note_session_returns_continue_never_abort():
    async def run():
        _, plugin, _host = _make_plugin()
        result = await plugin.note_session(
            message={"session_id": "group-1", "group_id": "123456", "user_id": "10001"}
        )
        assert result == {"action": "continue"}
        # 没有会话 id 时也必须给出合法动作，不能返回 None（blocking 处理器）
        assert await plugin.note_session(message={}) == {"action": "continue"}
        assert await plugin.note_session(message=None) == {"action": "continue"}

    asyncio.run(run())


def test_inject_uses_extra_prompt_which_host_actually_consumes():
    """别的桌面插件都在 ``before_model_request`` 上改 ``items``；本插件用
    ``before_request`` 的 ``extra_prompt``（宿主 ``maisaka_generator_base.py:1128``
    会把它折进「额外回复要求」）——两条链路、两个字段，互不争用。"""

    import ast

    source = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "inject_life_context":
            flat = " ".join(ast.unparse(d) for d in node.decorator_list)
            assert "maisaka.replyer.before_request" in flat, flat
            assert "before_model_request" not in flat, "别去和别人的 items 注入抢同一条链"
            body = ast.unparse(node)
            assert "extra_prompt" in body
            assert "items" not in body, "这条链路只有 extra_prompt，没有 items"
            return
    raise AssertionError("没找到 inject_life_context")


# ---------------------------------------------------------------- 五、状态持久化


def test_foreign_map_survives_state_round_trip():
    import life_sim as S

    state = S.LifeState()
    state.applied = {"a": 0.4}
    state.foreign = {"a": 0.5, "b": 1.0}
    restored = S.LifeState.from_dict(state.to_dict())
    assert restored.foreign == {"a": 0.5, "b": 1.0}
    assert restored.applied == {"a": 0.4}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"a": 0.5}, {"a": 0.5}),
        ({"a": -3.0}, {"a": 0.0}),          # 负倍率会把宿主钉死在 0，钳掉
        ({"a": "0.5"}, {}),                  # 字符串不是数字，丢弃
        ({"a": float("inf")}, {}),           # 非有限值丢弃
        ({"": 0.5}, {}),                     # 空会话 id 丢弃
        ({"a": None}, {}),
        ("nope", {}),
        (None, {}),
    ],
)
def test_foreign_map_sanitizes_bad_entries(raw, expected):
    import life_sim as S

    assert S._sanitize_adjust_map(raw) == expected


def test_tolerates_state_file_without_foreign_key():
    """老状态文件没有 foreign 字段，必须能平滑升级。"""

    import life_sim as S

    legacy = {"state_version": S.STATE_VERSION, "activity": "daily", "applied": {"a": 0.4}}
    state = S.LifeState.from_dict(legacy)
    assert state.foreign == {}
    assert state.applied == {"a": 0.4}


# ---------------------------------------------------------------- 五、v1.1.1 回归


def test_invalid_filter_mode_warns_and_shows_up_in_status():
    """回归：filter_mode 写错值会让插件**静默**失效（零写入、零告警、状态卡正常）。"""

    async def run():
        module, plugin, host = _make_plugin()
        plugin.config.apply.filter_mode = "白名单"          # 中文值：用户很容易这么写

        captured: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: captured.append(record.getMessage())
        logging.getLogger(f"plugin.{module.__plugin_id__}").addHandler(handler)
        try:
            await plugin._apply_sweep(time.time())
        finally:
            logging.getLogger(f"plugin.{module.__plugin_id__}").removeHandler(handler)

        assert not host.calls_of("frequency.set_adjust"), "非法 filter_mode 不该写任何会话"
        assert any("filter_mode" in item for item in captured), captured
        assert plugin._last_target_count == 0

        text = plugin._render_status(time.time())
        assert "本轮命中 0 个会话" in text
        assert "一个都没命中" in text

    asyncio.run(run())


def test_bare_life_chat_is_not_intercepted():
    """回归：命令正则允许省略斜杠，但「生活 好累」这类普通聊天不该被换成用法卡。"""

    async def run():
        _, plugin, host = _make_plugin()
        ok, text, level = await plugin.cmd_life_state(
            matched_groups={"sub": "好累"}, stream_id="group-1", text="生活 好累"
        )
        assert (ok, text, level) == (False, "", 0), "普通聊天被拦截了"
        assert not host.calls_of("send.text"), "不该往群里发用法卡"

        # 显式写法仍然要回用法卡
        ok2, text2, level2 = await plugin.cmd_life_state(
            matched_groups={"sub": "好累"}, stream_id="group-1", text="/生活 好累"
        )
        assert ok2 is True and level2 == 1 and "用法" in text2

    asyncio.run(run())


def test_admin_allow_path_and_qq_prefix():
    """README 承诺的两种管理员写法 + 白名单**放行**路径（v1.1.0 无测试覆盖）。"""

    async def run():
        _module, plugin, host = _make_plugin(security={"admin_ids": ["qq:99999"]})
        assert plugin._is_admin({"user_id": "99999"}) is True
        assert plugin._is_admin({"user_id": "qq:99999"}) is True
        assert plugin._is_admin({"user_id": "10001"}) is False
        assert plugin._is_admin({"user_id": "10001", "is_local_operator": True}) is True

        ok, text, level = await plugin.cmd_life_state(
            matched_groups={"sub": "暂停"}, stream_id="group-1", user_id="99999"
        )
        assert ok is True and plugin._state.paused_override is True, text

    asyncio.run(run())


def test_llm_cooldown_after_fail_streak():
    """README 承诺「连续失败 3 次进 30 分钟冷却」（v1.1.0 无测试覆盖）。"""

    async def run():
        _module, plugin, host = _make_plugin()
        host.returns["llm.generate"] = {"success": True, "response": "这不是 JSON"}
        now = time.time()
        for _ in range(3):
            plugin._last_llm_attempt_at = 0.0
            assert await plugin._ask_activity(now) is None
        assert plugin._state.llm_fail_streak >= 3
        assert plugin._state.llm_cooldown_until > now
        assert plugin._llm_ready(now) is False, "冷却期内不该再问模型"

    asyncio.run(run())


def test_backoff_registries_are_bounded_for_dead_sessions():
    """回归：裁剪守卫只看 ``applied`` 时，几百个死会话永远不会被清理。

    死会话正是从 ``applied`` 里 pop 掉、转进 ``unbacked`` 的那批，所以守卫必须看**所有**
    记忆表（v1.1.0：``unbacked`` 700 条、``applied`` 0 条 → 守卫一次都不触发）。
    """

    async def run():
        _module, plugin, host = _make_plugin()
        host.sessions = [
            {"session_id": f"group-{i}", "stream_id": f"group-{i}", "platform": "qq",
             "group_id": str(i), "user_id": str(10000 + i), "is_group_session": True,
             "chat_type": "group"}
            for i in range(700)
        ]
        host.live = set()                       # 全部是死会话
        now = 1_800_000_000.0
        await plugin._apply_sweep(now)
        await plugin._apply_sweep(now)
        assert plugin._state.applied == {}, "死会话应当已转进退避"
        assert len(plugin._state.unbacked) == 700

        # 让它们离开生效范围（此时才允许清理），守卫必须按 unbacked 的规模触发
        host.sessions = []
        plugin._seen_sessions.clear()
        await plugin._apply_sweep(now)
        assert len(plugin._state.unbacked) <= _module._ADJUST_MEMORY_LIMIT + 1
        assert len(plugin._state.unbacked_strikes) <= _module._ADJUST_MEMORY_LIMIT + 1

    asyncio.run(run())


def test_stale_session_records_are_pruned():
    """回归：``state.sessions`` 从不清理会让状态文件无限增长。"""

    async def run():
        _module, plugin, host = _make_plugin()
        now = 1_800_000_000.0
        for i in range(700):
            plugin._state.sessions[f"old-{i}"] = {
                "stream_id": f"old-{i}", "last_user_message_at": now - 40 * 86400,
                "last_proactive_at": 0.0, "day_key": "2020-01-01", "count": 0,
            }
        plugin._prune_stale_sessions(now)
        assert len(plugin._state.sessions) == 0, "14 天没动静的记录应当被清掉"

        plugin._state.sessions["keep-1"] = {
            "stream_id": "keep-1", "last_user_message_at": now - 60,
            "last_proactive_at": 0.0, "day_key": "2026-01-01", "count": 0,
        }
        plugin._prune_stale_sessions(now)
        assert "keep-1" in plugin._state.sessions

    asyncio.run(run())


def test_only_active_sessions_skips_history_but_covers_on_message():
    """回归（真机反馈）：``chat.get_all_streams`` 返回的全是历史会话时不要空转。

    真机实测：冷启动 60 秒后的第一轮巡检对 41 个历史会话各写一次，宿主逐个报
    「无法调整频率，未找到 session_id=… 的聊天流」（静默 no-op），插件随后才进退避。
    默认 ``only_active_sessions=True`` 后，冷启动不再对纯历史会话写入；
    会话一有消息进来（``note_session``）就在下一轮被纳入。
    """

    async def run():
        _module, plugin, host = _make_plugin()
        host.sessions = [
            {"session_id": f"group-{i}", "stream_id": f"group-{i}", "platform": "qq",
             "group_id": str(i), "user_id": str(10000 + i), "is_group_session": True,
             "chat_type": "group"}
            for i in range(41)
        ]
        plugin.config.apply.only_active_sessions = True
        await plugin._apply_sweep(time.time())
        assert plugin._last_target_count == 0, "冷启动不该干预纯历史会话"
        assert plugin._last_skipped_idle == 41
        assert not host.calls_of("frequency.set_adjust"), "不该对历史会话写宿主"

        # 其中一个会话来了消息 ⇒ 下一轮被纳入
        await plugin.note_session(message={"session_id": "group-7", "group_id": "7"})
        await plugin._apply_sweep(time.time())
        assert plugin._last_target_count == 1
        assert [kw.get("chat_id") for kw in host.calls_of("frequency.set_adjust")] == ["group-7"]

        # 关掉开关 ⇒ 恢复「所有会话都试一遍」的旧行为（group-7 上一步已写过、值没变，
        # 所以不会再重复写一次 RPC——这是刻意的省调用行为）
        plugin.config.apply.only_active_sessions = False
        host.calls.clear()
        await plugin._apply_sweep(time.time())
        assert plugin._last_target_count == 41
        written = {kw.get("chat_id") for kw in host.calls_of("frequency.set_adjust")}
        assert "group-0" in written and len(written) == 40

    asyncio.run(run())


def test_backoff_state_is_visible_in_cards_and_logs_are_split():
    """真机反馈的可观测性要求：卡上要看得到「写不进去」；日志要分清原因、并降噪。"""

    async def run():
        module, plugin, host = _make_plugin()
        host.live = set()                       # heartflow chat 不存在：写入被静默 no-op
        now = 1_800_000_000.0

        records: list[tuple[int, str]] = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append((record.levelno, record.getMessage()))
        log = logging.getLogger(f"plugin.{module.__plugin_id__}")
        log.setLevel(logging.DEBUG)             # INFO 默认会被 root 的 WARNING 挡掉
        log.addHandler(handler)
        try:
            await plugin._apply_sweep(now)      # 第一笔写入「成功」
            await plugin._apply_sweep(now)      # 这一轮才发现没生效 → 进入退避
        finally:
            logging.getLogger(f"plugin.{module.__plugin_id__}").removeHandler(handler)

        assert plugin._state.unbacked, "没有进入退避"
        infos = [m for lv, m in records if lv == logging.INFO]
        warnings = [m for lv, m in records if lv >= logging.WARNING]
        assert any("写不进去" in m for m in infos), records
        assert not any("不改写倍率" in m for m in warnings), "死会话退避不该刷 warning"

        # 目标值一变就无视退避立刻重试（这里改成睡眠归零）
        plugin._state.activity = "sleep"
        plugin._state.activity_since = now - 3600
        await plugin._apply_sweep(now)
        assert host.calls_of("frequency.set_adjust")

        # 卡面：状态卡与频率卡都要能看到退避情况（频率卡此刻走「硬闸命中」分支，
        # 也必须报告写入情况；不一致提示在非硬闸的那张卡上验证）
        freq_before = await plugin._render_frequency(now, "group-1")
        status = plugin._render_status(now)
        assert "写不进去（退避中）" in status, status
        freq = await plugin._render_frequency(now, "group-1")
        assert "写不进去（退避中）" in freq

        plugin._state.activity = "daily"
        plugin._state.activity_since = now - 3600
        freq_ok = await plugin._render_frequency(now, "group-1")
        assert "与上面算出的下发倍率不一致" in freq_ok, freq_ok

    asyncio.run(run())


def test_continuous_backoff_eventually_warns_once():
    """长期故障要看得见：连续多轮写不进去时升级成一次 warning。"""

    async def run():
        module, plugin, host = _make_plugin()
        host.live = set()
        now = 1_800_000_000.0
        records: list[tuple[int, str]] = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append((record.levelno, record.getMessage()))
        log = logging.getLogger(f"plugin.{module.__plugin_id__}")
        log.setLevel(logging.DEBUG)
        log.addHandler(handler)
        try:
            # 真实节奏：写入（宿主静默 no-op）→ 下一轮才发现没生效 → 退避到期后重试 …
            for _ in range(4):
                await plugin._apply_sweep(now)
                await plugin._apply_sweep(now)
                now += 24 * 3600            # 跨过退避封顶（24h）后再来一轮
        finally:
            log.removeHandler(handler)
        warnings = [(lv, m) for lv, m in records if lv >= logging.WARNING]
        assert warnings, [(lv, m[:30]) for lv, m in records]
        assert any("连续" in m for _, m in warnings), [m[:60] for _, m in warnings]
        assert any("写不进去" in m for _, m in warnings), [m[:60] for _, m in warnings]

    asyncio.run(run())


def test_private_chat_uses_private_talk_value():
    """真机反馈：私聊的基础频率是 ``private_talk_value``，不是 ``talk_value``。

    宿主 ``ChatConfigUtils.get_talk_value()``（``utils_config.py:601-608``）在
    ``is_group_chat is False`` 时取 ``private_talk_value``，而
    ``runtime._get_effective_reply_frequency`` 正是按会话类型去查。v1.1.2 之前插件
    只读 ``talk_value``，于是真机上私聊卡面写「基础频率 0.200 → 生效频率 0.165 / 攒 7 条」，
    而宿主实际是 ``1.000 × 0.826 = 0.826`` → **攒 2 条**（用户实测 1 条消息就进了 Planner）。
    """

    async def run():
        _module, plugin, host = _make_plugin()
        host.config_values["chat.reply_timing.talk_value"] = 0.6
        host.config_values["chat.reply_timing.private_talk_value"] = 1.0
        # 真机那台是计数门（frequency）——阈值 = ceil(1/生效频率)
        host.config_values["chat.reply_timing.reply_trigger_mode"] = "frequency"
        host.sessions = [
            {"session_id": "group-1", "stream_id": "group-1", "platform": "qq",
             "group_id": "123456", "user_id": "10001", "is_group_session": True,
             "chat_type": "group"},
            {"session_id": "private-1", "stream_id": "private-1", "platform": "qq",
             "user_id": "10001", "is_group_session": False, "chat_type": "private"},
        ]
        host.live = {"group-1", "private-1"}
        await plugin.note_session(message={"session_id": "private-1", "user_id": "10001"})
        await plugin._list_sessions()          # 真机里是巡检先列会话，再按会话类型取基础频率
        await plugin._refresh_host_context()
        plugin.config.apply.only_active_sessions = False

        assert plugin._session_is_group("private-1") is False
        assert plugin._session_is_group("group-1") is True
        assert plugin._talk_value_for("private-1") == (pytest.approx(1.0), "私聊基础频率")
        assert plugin._talk_value_for("group-1")[0] == pytest.approx(0.6)

        status = plugin._render_status(time.time(), "private-1")
        assert "私聊基础频率：1.000" in status, status
        group_status = plugin._render_status(time.time(), "group-1")
        assert "群聊基础频率：0.600" in group_status, group_status

        # 频率卡：写入生效后，基础频率应当由宿主实测反推得到（私聊 1.0 / 群聊 0.6），
        # 阈值随之为实际值——真机群里是 0.165 → 7 条，私聊是 0.826 → 2 条。
        await plugin._apply_sweep(time.time())
        for stream, base in (("private-1", 1.0), ("group-1", 0.6)):
            freq = await plugin._render_frequency(time.time(), stream)
            assert f"基础频率（由宿主实测反推）：{base:.3f}" in freq, freq
            effective = base * plugin._last_breakdown.adjust
            expected_threshold = max(1, math.ceil(1.0 / effective))
            assert f"生效频率 = 基础频率 × 下发倍率 = {effective:.3f}" in freq, freq
            assert f"触发阈值：{expected_threshold} 条消息" in freq, freq
            assert "宿主侧实际触发阈值" in freq
        assert "宿主侧实际触发阈值" in freq

    asyncio.run(run())


def test_persona_injection_is_capped_by_config():
    """人设注入上限可配（v1.3.2）：读取时不截断，截断发生在拼活动提示词时。"""

    async def run():
        _, plugin, host = _make_plugin(activity={"llm": {"persona_max_chars": 12}})
        long_persona = "她" + "很长的设定" * 20
        host.config_values["personality.personality"] = long_persona

        await plugin._fetch_identity()
        assert plugin._persona == long_persona, "读取阶段不该截断（上限在提示词侧生效）"

        host.returns["llm.generate"] = {
            "success": True,
            "response": '{"activity": "daily", "scene": "在做点日常的事"}',
        }
        await plugin._ask_activity(now=1_800_000_000.0)

        prompts = [kw.get("prompt", "") for kw in host.calls_of("llm.generate")]
        assert prompts, "应该调过一次模型"
        assert "很长的设定" * 2 in prompts[-1], "上限内的部分要在提示词里"
        assert "很长的设定" * 3 not in prompts[-1], "超出上限的部分不该进提示词"

        # 0 = 不带人设（省 token）：连上限内的那段也不该出现
        _, zero_plugin, zero_host = _make_plugin(activity={"llm": {"persona_max_chars": 0}})
        zero_host.config_values["personality.personality"] = "她是十九岁，话少。"
        await zero_plugin._fetch_identity()
        zero_host.returns["llm.generate"] = {
            "success": True,
            "response": '{"activity": "daily", "scene": "在做点日常的事"}',
        }
        await zero_plugin._ask_activity(now=1_800_000_000.0)
        zero_prompts = [kw.get("prompt", "") for kw in zero_host.calls_of("llm.generate")]
        assert zero_prompts, "应该调过一次模型"
        assert "十九岁" not in zero_prompts[-1], "persona_max_chars=0 时不该带人设"

    asyncio.run(run())


def _proactive_material(now: float) -> dict[str, Any]:
    return {
        "label": "小事",
        "text": "刚发生的一件小事",
        "weight": 0.9,
        "created_at": now,
        "expires_at": now + 3600.0,
    }


def _proactive_triggered(host) -> list[str]:
    """取主动开口触发过的会话（按顺序去重）。

    ⚠ 测试脚手架的已知习惯：``ScaleHost.rpc_call`` 先记一次，未接住的能力落到
    ``FakeHost.rpc_call`` 会**再记一次**（api.call 等落穿能力皆双记）。
    ``maisaka.proactive.trigger`` 正是落穿能力，所以这里必须去重后再断言。
    """

    return list(dict.fromkeys(
        kw.get("stream_id") for kw in host.calls_of("maisaka.proactive.trigger")
    ))


def test_proactive_scope_whitelist_and_intersection_with_apply():
    """v1.5.2：主动开口自己的白名单；有效范围 = [apply] ∩ [proactive]。

    ``[apply]`` 是总闸（频率 + 主动开口），``[proactive]`` 只能在总闸内**再收窄**。
    """

    async def run():
        _module, plugin, host = _make_plugin(
            proactive={
                "enabled": True,
                "quiet_hours": [],
                "filter_mode": "whitelist",
                "target_chats": ["group:123456"],       # host.sessions 里 group-1 的群号
            },
        )
        now = time.time()
        plugin._state.materials = [_proactive_material(now)]
        await plugin._maybe_proactive(now)
        assert _proactive_triggered(host) == ["group-1"], "白名单内的群应当能主动开口"

        # 白名单换成一个不存在的群 ⇒ 不命中 ⇒ 不触发（排除 daily_max 等其它闸的干扰：
        # 每次断言前清空会话记录，让它回到「今天一次都没开口」的初始态）
        host.calls.clear()
        plugin._state.sessions.clear()
        plugin.config.proactive.target_chats = ["group:999"]
        await plugin._maybe_proactive(now)
        assert not host.calls_of("maisaka.proactive.trigger"), "白名单外的会话不该主动开口"

        # proactive 放开为 all，但 [apply] 总闸收窄 ⇒ 交集为空 ⇒ 仍不触发
        plugin.config.proactive.filter_mode = "all"
        plugin.config.proactive.target_chats = []
        plugin.config.apply.filter_mode = "whitelist"
        plugin.config.apply.target_chats = ["group:999"]
        await plugin._maybe_proactive(now)
        assert not host.calls_of("maisaka.proactive.trigger"), "[apply] 总闸必须继续生效"

    asyncio.run(run())


def test_proactive_scope_blacklist_and_illegal_mode_fallback():
    """v1.5.2：黑名单与非法值回退——写错 filter_mode 必须告警一次并保守处理。"""

    async def run():
        module, plugin, host = _make_plugin(
            proactive={"enabled": True, "quiet_hours": [], "filter_mode": "白名单"},
        )
        now = time.time()
        plugin._state.materials = [_proactive_material(now)]

        records: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        log = logging.getLogger(f"plugin.{module.__plugin_id__}")
        log.setLevel(logging.DEBUG)
        log.addHandler(handler)
        try:
            # 非法值「白名单」→ 告警一次 + 按 whitelist 回退；target_chats 为空 ⇒ 无一命中
            await plugin._maybe_proactive(now)
        finally:
            log.removeHandler(handler)
        assert not host.calls_of("maisaka.proactive.trigger"), "回退后的空 whitelist 不该触发"
        assert any("主动开口范围" in item and "filter_mode" in item for item in records), records

        # 黑名单命中 group-1 ⇒ 它不能开口
        plugin._state.sessions.clear()
        plugin.config.proactive.filter_mode = "blacklist"
        plugin.config.proactive.target_chats = ["group:123456"]
        await plugin._maybe_proactive(now)
        assert not host.calls_of("maisaka.proactive.trigger")

        # 黑名单换成别的群 ⇒ group-1 解禁，可以开口
        plugin.config.proactive.target_chats = ["group:999"]
        await plugin._maybe_proactive(now)
        assert _proactive_triggered(host) == ["group-1"], "黑名单外的会话应当能主动开口"

    asyncio.run(run())


def test_proactive_scope_defaults_to_all_and_shows_in_status():
    """v1.5.2：默认 filter_mode=all 行为与旧版完全一致；状态卡显示当前范围。"""

    async def run():
        _module, plugin, host = _make_plugin(proactive={"enabled": True, "quiet_hours": []})
        now = time.time()
        plugin._state.materials = [_proactive_material(now)]
        await plugin._maybe_proactive(now)
        assert _proactive_triggered(host) == ["group-1"], "默认 all 下不该改变既有行为"

        card_all = plugin._render_status(now, "group-1")
        assert "主动开口：已启用（范围：全部会话）" in card_all, card_all

        plugin.config.proactive.filter_mode = "whitelist"
        plugin.config.proactive.target_chats = ["group:123456", "private:42"]
        card_whitelist = plugin._render_status(now, "group-1")
        assert "主动开口：已启用（范围：白名单 2 个）" in card_whitelist, card_whitelist

    asyncio.run(run())
