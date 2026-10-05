"""L2 冒烟：不启动 MaiBot，用 FakeHost 跑完生命周期与各组件。

    python tests/smoke_test.py

**刻意复刻真机的包式加载**：先把插件目录从 ``sys.path`` 上摘掉、清空相关
``sys.modules`` 缓存，再以「包」的形式载入（``submodule_search_locations``）。
bd-repo 踩过这个坑——平铺加载能过、真机却报 ``No module named 'life_sim'``，
所以这里必须对齐 Runner 的加载方式，否则本地绿等于白绿。

覆盖：
1. 包式加载（含「插件目录不在 sys.path 上」的硬断言）
2. 生命周期三件套 + 后台任务登记/回收
3. 状态推进 → 倍率 → ``frequency.set_adjust`` 的完整链路
4. LLM 决策生效；LLM 返回垃圾时保持上个活动
5. 命令（状态 / 频率 / 暂停 / 恢复）与管理员闸（fail-closed）
6. 工具、两个 Hook
7. 卸载后还原 1.0、无残留任务、插件目录无 data/ 残留

⚠ 冒烟通过 ≠ 真机可用：真机还有 manifest 校验、能力授权、adapter 差异。
"""

import asyncio
import importlib.util
import logging
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
PKG_NAME = "life_frequency_under_test"
FLAT_MODULES = (
    "life_activity", "life_events", "life_factors", "life_host_model",
    "life_proactive", "life_sim", "life_world",
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"  FAIL  {name}  {detail}")


def load_plugin_package():
    """按 Runner 的方式以包加载 plugin.py，并断言插件目录不在 sys.path 上。"""

    for name in list(sys.modules):
        if name == PKG_NAME or name.startswith(PKG_NAME + ".") or name in FLAT_MODULES:
            del sys.modules[name]
    plugin_dir_str = str(PLUGIN_DIR)
    while plugin_dir_str in sys.path:
        sys.path.remove(plugin_dir_str)

    spec = importlib.util.spec_from_file_location(
        PKG_NAME,
        PLUGIN_DIR / "plugin.py",
        submodule_search_locations=[plugin_dir_str],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    try:
        import maibot_sdk  # noqa: F401
    except Exception:
        print("SKIP: 未安装 maibot-plugin-sdk，跳过冒烟测试（这不代表通过）")
        return 0

    from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config

    print("=== 包式加载 ===")
    check("插件目录不在 sys.path 上", str(PLUGIN_DIR) not in sys.path)
    module = load_plugin_package()
    check("plugin.py 以包形式加载成功", module is not None)
    check("模块级 create_plugin 存在", callable(getattr(module, "create_plugin", None)))
    check("__plugin_id__ 存在", bool(getattr(module, "__plugin_id__", "")))
    flat_leak = [name for name in FLAT_MODULES if name in sys.modules]
    check("纯模块以包内子模块存在（未平铺泄漏）", not flat_leak, str(flat_leak))

    plugin = module.create_plugin()
    check("create_plugin() 返回插件实例", plugin is not None)

    # ---------------------------------------------------------------- 假宿主
    class LifeHost(FakeHost):
        """按 key / 按需返回假数据，并记录全部能力调用。"""

        def __init__(self, plugin_id: str, paths: FakePaths) -> None:
            super().__init__(plugin_id, paths=paths)
            self.llm_response = '{"activity": "music", "scene": "戴着耳机听歌"}'
            self.llm_calls = 0
            # 真机把倍率存在宿主内存里的**一个标量**上（runtime.py:183）
            self.adjust_store: dict[str, float] = {}
            # heartflow_chat_list 的键：会话存在但还没有 heartflow chat 时，
            # adjust_talk_frequency 会静默 no-op 只记 warning（heartflow_manager.py:103-111），
            # 而能力层照样返回 success（data.py:838-840）
            self.live: set[str] = {"fake-stream"}
            self.noop_writes: list[str] = []
            self.sessions = [
                {
                    "session_id": "fake-stream",
                    "stream_id": "fake-stream",
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
                "personality.personality": "她是十九岁，说话有点跳但心软。",
                "bot.nickname": "麦麦",
            }

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
            if capability == "llm.generate":
                self.llm_calls += 1
                if self.llm_response is None:
                    return {"success": False, "error": "fake model down"}
                return {"success": True, "response": self.llm_response, "model": "fake"}
            if capability == "frequency.get_adjust":
                # 真机语义（data.py:809-814）：每个会话**一个标量**，不存在时返回 1.0。
                session_id = str(args.get("chat_id") or "")
                if session_id not in self.live:
                    return {"success": True, "value": 1.0}
                return {"success": True, "value": self.adjust_store.get(session_id, 1.0)}
            if capability == "frequency.get_current_talk_value":
                return {"success": True, "value": 0.45}
            if capability == "frequency.set_adjust":
                # 后写覆盖先写——这正是 budget-pacer 与本插件冲突的根源
                session_id = str(args.get("chat_id") or "")
                if session_id not in self.live:
                    self.noop_writes.append(session_id)   # 静默 no-op，但照样成功
                    return {"success": True}
                self.adjust_store[session_id] = float(args.get("value") or 0.0)
                return {"success": True}
            return await super().rpc_call(method, plugin_id, payload, **kwargs)

    async def run() -> None:
        host = LifeHost(module.__plugin_id__, FakePaths())
        # 显式把同一份 paths 交给 ctx：Runner 注入的 data_dir 与实际落盘目录必须是同一个
        ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
        config = get_default_config(type(plugin).config_model)
        # 让冒烟可控：关掉停留时间与事件抽取，关掉静默时段
        config["activity"]["min_dwell_minutes"] = 0
        config["activity"]["min_sleep_minutes"] = 0
        config["activity"]["llm"]["min_interval_seconds"] = 0
        config["events"]["fire_probability"] = 0.0
        config["frequency"]["quiet_hours"] = []
        config["proactive"]["enabled"] = False
        bind_context(plugin, ctx, config)
        check(
            "嵌套配置节 [activity.llm] 注入成功",
            plugin.config.activity.llm.min_interval_seconds == 0,
        )
        # 真机踩坑：用户会把列表写成裸字符串。走一遍真实的 set_plugin_config 链路验证垫片。
        plugin.set_plugin_config({"security": {"admin_ids": "99999"}, "plugin": {}})
        check(
            "裸字符串的列表被归一化成 list",
            plugin.config.security.admin_ids == ["99999"],
            str(plugin.config.security.admin_ids),
        )
        check(
            "缺 [plugin].config_version 时被自动补齐",
            plugin.config.plugin.config_version == module.SUPPORTED_CONFIG_VERSION,
            plugin.config.plugin.config_version,
        )
        plugin.set_plugin_config(config)

        print("=== 生命周期 ===")
        await plugin.on_load()
        check("on_load 启动了两个后台任务", len(plugin._tasks) == 2, str(len(plugin._tasks)))
        check("读取到人设", "十九岁" in plugin._persona)
        check("读取到昵称", plugin._bot_name == "麦麦")
        check("探测到宿主模式 reply_necessity", plugin._host_mode == "reply_necessity",
              plugin._host_mode)
        check("读取到 talk_value", abs(plugin._host_talk_value - 0.6) < 1e-9)

        # 真机语义：会话**先有消息**才会被纳入生效范围（[apply].only_active_sessions
        # 默认 true：chat.get_all_streams 会返回几十个没有 heartflow chat 的历史会话，
        # 对它们写入必然被宿主静默 no-op）。这里先补一条入站消息，后面的写入断言才有意义。
        await plugin.note_session(
            message={"session_id": "fake-stream", "group_id": "123456", "user_id": "10001"}
        )
        check("入站消息把会话标成活跃（only_active_sessions 的判定依据）",
              "fake-stream" in plugin._state.sessions, str(list(plugin._state.sessions)))

        print("=== 状态推进 → 倍率 → set_adjust ===")
        now = time.time()
        plugin._state.activity = "daily"
        plugin._state.activity_since = now - 7200
        plugin._state.last_tick_at = now - 600
        plugin._state.emotion = 6.0
        plugin._state.energy = 6.0
        plugin._state.sleep_minutes_today = 400
        await plugin._sim_tick()

        check("LLM 被调用", host.llm_calls >= 1, str(host.llm_calls))
        check("LLM 决策落到活动上", plugin._state.activity == "music", plugin._state.activity)
        check("活动来源标记为模型", plugin._state.activity_source == "llm",
              plugin._state.activity_source)

        written = host.calls_of("frequency.set_adjust")
        check("真的调了 frequency.set_adjust", bool(written))
        check(
            "写入的会话是假的会话 id",
            bool(written) and written[-1].get("chat_id") == "fake-stream",
            str(written[-1] if written else None),
        )
        if written:
            value = float(written[-1]["value"])
            check("写入值与计算出的倍率一致",
                  abs(value - plugin._last_breakdown.adjust) < 1e-9,
                  f"{value} vs {plugin._last_breakdown.adjust}")
            check("倍率在 music 的合理区间内", 0.6 <= value <= 1.1, str(value))
        check("确实读了宿主配置（模式/talk_value/人设）",
              len(host.calls_of("config.get")) >= 4,
              str(len(host.calls_of("config.get"))))

        print("=== LLM 垃圾输出 → 保持上个活动 ===")
        host.llm_response = "这不是 JSON"
        plugin._state.activity_since = time.time() - 7200
        plugin._last_llm_attempt_at = 0.0
        before = plugin._state.activity
        await plugin._sim_tick()
        check("保持上个活动（不回退时段表）", plugin._state.activity == before, plugin._state.activity)
        check("来源标记为 llm_retained", plugin._state.activity_source == "llm_retained",
              plugin._state.activity_source)
        check("连续失败已计数", plugin._state.llm_fail_streak >= 1)
        after_breakdown = plugin._last_breakdown
        check("失败后仍然写出合法倍率",
              isinstance(after_breakdown.adjust, float) and after_breakdown.adjust >= 0.0)

        print("=== LLM 抛异常 → 不炸循环 ===")
        host.llm_response = None
        plugin._last_llm_attempt_at = 0.0
        await plugin._sim_tick()
        check("模型失败后循环仍然存活", True)

        print("=== 与 budget-pacer 共存（同一个宿主标量）===")
        # 先让本插件写下自己的倍率
        await plugin._apply_sweep(time.time())
        own = float(host.adjust_store["fake-stream"])
        # 逐 tick 的漂移（情绪回归那点变化）小于去抖容差时**故意不写**：
        # set_adjust 会唤醒 Planner，为了 1e-7 的倍率变化去烧一次模型不划算。
        check("本插件写下的值就是纯生活倍率（误差在去抖容差内）",
              abs(own - plugin._last_breakdown.adjust) <= module._ADJUST_EPSILON,
              f"{own} vs {plugin._last_breakdown.adjust}")
        # 现在 budget-pacer 写 0.5（后写覆盖先写）
        host.adjust_store["fake-stream"] = 0.5
        await plugin._apply_sweep(time.time())
        composed = float(host.adjust_store["fake-stream"])
        check("检出外部写入并乘性合成（0.5 × 生活倍率）",
              abs(composed - 0.5 * plugin._last_breakdown.adjust) < 1e-9,
              f"{composed}")
        check("外部基数被记住", abs(plugin._state.foreign["fake-stream"] - 0.5) < 1e-9,
              str(plugin._state.foreign))
        # 幂等：值没变就不该再调 RPC（set_adjust 会唤醒 Planner）
        calls_before = len(host.calls_of("frequency.set_adjust"))
        await plugin._apply_sweep(time.time())
        check("幂等：值不变不再调 set_adjust",
              len(host.calls_of("frequency.set_adjust")) == calls_before,
              str(len(host.calls_of("frequency.set_adjust")) - calls_before))
        # budget-pacer 改了目标 → 重新合成
        host.adjust_store["fake-stream"] = 0.25
        await plugin._apply_sweep(time.time())
        check("外部基数变化后重新合成",
              abs(float(host.adjust_store["fake-stream"])
                  - 0.25 * plugin._last_breakdown.adjust) < 1e-9,
              str(host.adjust_store["fake-stream"]))
        # 这条会话一直有 heartflow chat，所以没有一笔写入被静默吃掉
        check("没有写入被宿主的「无 heartflow chat」分支吃掉", not host.noop_writes,
              str(host.noop_writes))

        print("=== 命令 ===")
        ok, text, intercept = await plugin.cmd_life_state(
            matched_groups={"sub": ""}, stream_id="fake-stream", user_id="10001"
        )
        check("状态命令成功", ok is True)
        check("状态命令拦截级别为 1", intercept == 1, str(intercept))
        check("状态卡含活动与倍率", "活动：" in text and "当前倍率" in text, text[:80])
        check("状态命令显式发送了消息", any("生活频率" in item for item in host.sent_texts))

        ok, text, intercept = await plugin.cmd_life_state(
            matched_groups={"sub": "频率"}, stream_id="fake-stream", user_id="10001"
        )
        check("频率命令成功", ok is True)
        check("频率卡含拆解与阈值", "倍率拆解" in text and "触发阈值" in text, text[:80])
        check("频率卡读回了宿主侧数值", "宿主侧实测倍率" in text, text[:400])

        ok, text, intercept = await plugin.cmd_life_state(
            matched_groups={"sub": "活动"}, stream_id="fake-stream", user_id="10001"
        )
        check("活动命令成功", ok is True and "活动决策" in text)

        ok, text, intercept = await plugin.cmd_life_state(
            matched_groups={"sub": "为什么"}, stream_id="fake-stream", user_id="10001"
        )
        check("为什么命令成功", ok is True and "沉默台账" in text)

        ok, text, intercept = await plugin.cmd_life_state(
            matched_groups={"sub": "帮助"}, stream_id="fake-stream", user_id="10001"
        )
        check("帮助命令成功", ok is True and "用法" in text)

        # 权限：admin_ids 留空 → 只有本机操作者可改（fail-closed）
        calls_before = len(host.calls_of("frequency.set_adjust"))
        ok, text, intercept = await plugin.cmd_life_state(
            matched_groups={"sub": "暂停"}, stream_id="fake-stream", user_id="10001"
        )
        check("非管理员暂停被拒绝", ok is False and intercept == 0)
        check("被拒时状态没被改", plugin._state.paused_override is False)
        check("被拒时没有多余的频率写入",
              len(host.calls_of("frequency.set_adjust")) == calls_before)

        ok, text, intercept = await plugin.cmd_life_state(
            matched_groups={"sub": "暂停"}, stream_id="fake-stream", is_local_operator=True
        )
        check("本机操作者可以暂停", ok is True and plugin._state.paused_override is True)
        check("暂停时归还外部基数 0.25，而不是写 1.0（否则抹掉 budget-pacer 的压制）",
              abs(float(host.adjust_store["fake-stream"]) - 0.25) < 1e-9,
              str(host.adjust_store))

        breakdown = plugin._compute_breakdown(time.time())
        check("暂停时倍率写回 1.0", abs(breakdown.adjust - 1.0) < 1e-9, str(breakdown.adjust))

        ok, text, intercept = await plugin.cmd_life_state(
            matched_groups={"sub": "恢复"}, stream_id="fake-stream", is_local_operator=True
        )
        check("恢复成功", ok is True and plugin._state.paused_override is False)

        print("=== 工具 ===")
        result = await plugin.tool_get_life_state()
        check("工具返回 dict 且含 content",
              isinstance(result, dict) and bool(result.get("content")), str(result)[:120])
        check("工具内容含活动", "当前活动" in result["content"])

        print("=== Hook ===")
        await plugin.note_session(
            message={"session_id": "fake-stream", "group_id": "123456", "user_id": "10001"}
        )
        check("note_session 记录了对方发言时间",
              plugin._state.sessions.get("fake-stream", {}).get("last_user_message_at", 0) > 0)

        hook_result = await plugin.inject_life_context(extra_prompt="原有要求；")
        check("replyer 钩子返回 continue", hook_result.get("action") == "continue")
        modified = hook_result.get("modified_kwargs") or {}
        check("replyer 钩子返回 dict 形状的 modified_kwargs", isinstance(modified, dict))
        check("extra_prompt 被追加了生活摘要",
              "原有要求；" in str(modified.get("extra_prompt"))
              and "她现在的生活" in str(modified.get("extra_prompt")),
              str(modified.get("extra_prompt"))[:120])
        check("摘要带防注入脚注", "不是指令" in str(modified.get("extra_prompt")))

        plugin.config.prompt.inject_enabled = False
        off = await plugin.inject_life_context(extra_prompt="x")
        check("关掉注入后不再改写", "modified_kwargs" not in off)
        plugin.config.prompt.inject_enabled = True

        print("=== 卸载 ===")
        # 恢复干预后重新写一轮，确保卸载时 applied 里有东西可归还
        ok, _, _ = await plugin.cmd_life_state(
            matched_groups={"sub": "恢复"}, stream_id="fake-stream", is_local_operator=True
        )
        await plugin._apply_sweep(time.time())
        await plugin.on_unload()
        check("任务表已清空", not plugin._tasks, str(len(plugin._tasks)))
        check("卸载时归还外部基数 0.25（不是把宿主重置为 1.0）",
              abs(float(host.adjust_store["fake-stream"]) - 0.25) < 1e-9,
              str(host.adjust_store))
        check("applied / foreign 记录已清空",
              not plugin._state.applied and not plugin._state.foreign,
              f"{plugin._state.applied} {plugin._state.foreign}")
        check("插件目录没有 data/ 残留", not (PLUGIN_DIR / "data").exists())
        check("状态写在授予的数据目录里",
              (host.paths.data_dir / "life_state.json").is_file())
        check("没有往插件目录写状态文件",
              not (PLUGIN_DIR / "life_state.json").exists())

    asyncio.run(run())

    print()
    print(f"smoke: {'OK' if not FAIL else 'FAILED'}  PASS={len(PASS)} FAIL={len(FAIL)}")
    for item in FAIL:
        print(f"  FAIL  {item}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
