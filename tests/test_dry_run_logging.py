# -*- coding: utf-8 -*-
"""演算模式（dry_run）日志按轮聚合（v1.23.2）。

真机：18 个会话 × 每分钟一轮 ≈ **2.6 万条/天** `[演算] session=... 倍率 X`，
把宿主日志冲得只剩这一类行 —— 而同一轮 18 条的取值**完全相同**，重复 18 次零信息量。
真正的异常（锁库、事件循环卡顿）被埋在里面看不见。

⚠ 这组断言的是**日志条数**，不是日志内容。只断言内容会放逐会话刷屏过关
（第一条内容是对的）。每条用例都先定条数，再看内容。
"""

import asyncio
import logging
import pathlib
import sys
import time

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakePaths, bind_context, build_context, get_default_config  # noqa: E402
from test_interop import ScaleHost, _load  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
LOGGER_NAME = "plugin.org.orge-8.life-frequency"


def _make_plugin(**config_overrides):
    """与 ``test_interop._make_plugin`` 同口径（关掉 only_active_sessions）。"""

    module = _load()
    plugin = module.create_plugin()
    host = ScaleHost(module.__plugin_id__, FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["frequency"]["quiet_hours"] = []
    config["events"]["fire_probability"] = 0.0
    config["proactive"]["enabled"] = False
    config["simulation"]["dry_run"] = True
    config["apply"]["only_active_sessions"] = False
    for section, values in config_overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    plugin._state.last_tick_at = time.time()
    return module, plugin, host


def _seed_sessions(host, count: int) -> None:
    host.sessions = [
        {
            "session_id": f"group-{i}", "stream_id": f"group-{i}", "platform": "qq",
            "group_id": str(10000 + i), "user_id": "10001",
            "is_group_session": True, "chat_type": "group",
        }
        for i in range(count)
    ]
    host.live = {f"group-{i}" for i in range(count)}


def _dry_lines(caplog) -> list[str]:
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == LOGGER_NAME and "[演算]" in rec.getMessage()
    ]


def test_sweep_logs_one_line_not_one_per_session(caplog):
    """18 个会话一轮 ⇒ 只能有一条（真机就是这个规模）。"""

    async def run():
        _, plugin, host = _make_plugin()
        _seed_sessions(host, 18)
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            await plugin._apply_sweep(time.time())
        lines = _dry_lines(caplog)
        assert len(lines) == 1, f"逐会话刷屏回来了：{lines}"
        assert "18 个会话" in lines[0], lines[0]
        assert "未写宿主" in lines[0], lines[0]
        assert plugin._dry_run_log.targets == {}, "记账必须清空，否则下轮条数会算多"

    asyncio.run(run())


def test_repeat_round_is_throttled_but_still_accounted(caplog):
    """倍率没变 ⇒ 第二轮不打；但轮数要接着记（否则「安静」与「停了」分不清）。"""

    async def run():
        _, plugin, host = _make_plugin()
        _seed_sessions(host, 3)
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            await plugin._apply_sweep(time.time())
            first = len(_dry_lines(caplog))
            caplog.clear()
            await plugin._apply_sweep(time.time())
            second = len(_dry_lines(caplog))
        assert (first, second) == (1, 0), f"节流没生效：{first} / {second}"
        assert plugin._dry_run_log.rounds == 1, "节流期内的轮数得记下来"

        plugin._dry_run_log.log_at = time.time() - 3600.0     # 越过节流窗口
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            caplog.clear()
            await plugin._apply_sweep(time.time())
        lines = _dry_lines(caplog)
        assert len(lines) == 1, lines
        assert "已连续 2 轮保持该值" in lines[0], lines[0]

    asyncio.run(run())


def test_value_change_is_logged_immediately(caplog):
    """倍率变了必须当场可见——这是演算模式唯一想看的东西。"""

    async def run():
        _, plugin, host = _make_plugin()
        _seed_sessions(host, 2)
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            await plugin._apply_sweep(time.time())
            caplog.clear()
            plugin._state.emotion = 0.05
            plugin._state.energy = 5.0
            await plugin._apply_sweep(time.time())
        lines = _dry_lines(caplog)
        assert len(lines) == 1, f"值变了却不打（或又刷屏了）：{lines}"
        assert "已连续" not in lines[0], f"值变了不该走节流分支：{lines[0]}"

    asyncio.run(run())


def test_single_session_wake_path_still_logs(caplog):
    """``@`` 唤醒是单会话路径：没有可聚合的对象，必须照旧逐条打。

    反过来钉住一件事：聚合模式**不能**把这条唯一的线索也吞掉。
    """

    async def run():
        _, plugin, host = _make_plugin()
        _seed_sessions(host, 1)
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            await plugin._apply_one_session("group-0", 1.0, time.time(), reason="被叫醒")
        lines = _dry_lines(caplog)
        assert len(lines) == 1, f"单会话路径被聚合吞了：{lines}"
        assert "session=group-0" in lines[0], lines[0]

    asyncio.run(run())


def test_aggregation_does_not_change_behaviour(caplog):
    """降噪不能顺手改行为：演算模式依旧一笔都不写宿主。"""

    async def run():
        _, plugin, host = _make_plugin()
        _seed_sessions(host, 5)
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            await plugin._apply_sweep(time.time())
        assert not host.calls_of("frequency.set_adjust")
        assert host.adjust == {}, "演算模式不许写宿主"

    asyncio.run(run())
