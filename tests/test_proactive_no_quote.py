# -*- coding: utf-8 -*-
"""L3：主动开口「不要引用任何消息」。

真机要求（2026-10-05）：她主动找话说时**不许挂引用** —— 引用别人刚说过的话，
看起来就像在回复那个人，语义完全错位。

与 group-welcome v1.0.2→v1.2.3 同一个坑，那边四轮迭代的结论直接复用：

* ``set_quote`` 是 **Planner 调 reply 工具时的参数**，插件改不了（无法强制）；
* 只说「不要引用任何消息」会把模型逼到无路可走（``reply`` 必须带 ``msg_id``），
  所以必须给一条可执行路径：**引用对象只能是她自己**；
* 光写进 ``intent`` 不够（intent 属任务描述，模型未必当硬约束）⇒
  再往 Planner 请求的 ``items`` 注入一条系统级规则。

本文件钉住四件事：

1. 纪律写在**代码层**（``life_proactive.NO_QUOTE_DISCIPLINE``），intent 与注入共用同一份文本；
2. 注入**只在该会话的窗口内**发生，窗口过期 / 别的会话 / 形态不匹配都不注入；
3. ``modified_kwargs`` 必须回传**完整** kwargs（非增量）——否则 planner 会连 reply 工具都看不见；
4. 触发成功才开窗（宿主未受理就不该开）。
"""

import asyncio
import logging
import pathlib
import sys
import time

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config  # noqa: E402

import life_proactive as P  # noqa: E402  （conftest 已把插件目录放进 sys.path）

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_no_quote")


def _make_plugin(**config_overrides):
    module = _load()
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    for section, values in config_overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    return module, plugin, host


class _Capture:
    """抓插件日志（root 的 WARNING 级别会挡掉 INFO，必须自己设级别）。"""

    def __init__(self):
        self.records: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: self.records.append(record.getMessage())
        handler.setLevel(logging.DEBUG)
        self._handler = handler
        self._logger = logging.getLogger("plugin.org.orge-8.life-frequency")
        self._logger.addHandler(handler)
        self._logger.setLevel(logging.DEBUG)

    def stop(self):
        self._logger.removeHandler(self._handler)


# ------------------------------------------------- 1. 纪律在代码层，且 intent 真的带上


def test_discipline_lives_in_code_and_forbids_quoting():
    """"这条规则失效会造成公开事故" ⇒ 必须写在代码层，不能只写在 Field(default=...)。"""

    text = P.NO_QUOTE_DISCIPLINE
    assert text.strip(), "纪律不能是空串"
    assert "主动开口" in text and "不是回复谁" in text
    assert "set_quote" in text and "false" in text
    assert "不要引用" in text


def test_discipline_gives_an_executable_path():
    """只说「不要引用」会把模型逼到无路可走：reply 必须带 msg_id，得指一条可行的路。"""

    text = P.NO_QUOTE_DISCIPLINE
    assert "你自己最近发出的一条消息" in text, "必须给出「引用只能引用自己」这条可执行路径"
    assert "切勿引用他人的消息" in text


def test_intent_carries_the_discipline():
    intent = P.build_intent({"label": "刚写完代码", "text": "折腾到刚才才把那段调通"})
    assert "刚写完代码" in intent and "折腾到刚才才把那段调通" in intent
    assert P.NO_QUOTE_DISCIPLINE in intent, "intent 必须带上这条纪律"
    # 没有正文的素材也要带纪律
    assert P.NO_QUOTE_DISCIPLINE in P.build_intent({"label": "小事"})


def test_plugin_hint_reuses_the_same_discipline():
    """一处定义：Planner 注入用的文本必须内嵌同一份纪律，避免两处措辞漂移。"""

    module, _plugin, _host = _make_plugin()
    assert P.NO_QUOTE_DISCIPLINE in module.PROACTIVE_NO_QUOTE_HINT
    assert module.PROACTIVE_NO_QUOTE_HINT.startswith(module.PROACTIVE_NO_QUOTE_MARKER)
    assert module.PROACTIVE_NO_QUOTE_WINDOW_SECONDS > 0


# ------------------------------------------------- 2. 窗口内才注入


def test_hook_does_not_inject_without_a_window():
    _module, plugin, _host = _make_plugin()

    async def run():
        return await plugin.inject_no_quote_hint(session_id="group-1", items=[], prompt="x")

    result = asyncio.run(run())
    assert result == {"action": "continue"}, "没有窗口就一个字段都不许改"


def test_hook_injects_inside_the_window():
    module, plugin, _host = _make_plugin()
    plugin._open_no_quote_window("group-1", time.time())

    async def run():
        return await plugin.inject_no_quote_hint(
            session_id="group-1", items=[{"item_type": "UserMessageItem"}], prompt="原有要求", attempt=3
        )

    result = asyncio.run(run())
    assert result["action"] == "continue"
    modified = result["modified_kwargs"]
    # 必须回传**完整** kwargs：只回传改动键会把 prompt / attempt / tool_definitions 全丢掉
    assert modified["prompt"] == "原有要求"
    assert modified["attempt"] == 3
    assert len(modified["items"]) == 2
    injected = modified["items"][-1]
    assert injected["item_type"] == "SystemMessageItem"
    text = injected["parts"][0]["text"]
    assert module.PROACTIVE_NO_QUOTE_MARKER in text
    assert module.NO_QUOTE_DISCIPLINE in text
    # 原 kwargs 本身不被就地改动（hook 契约：改副本）
    assert len(modified["items"]) == 2


def test_hook_is_idempotent_on_the_same_request():
    module, plugin, _host = _make_plugin()
    plugin._open_no_quote_window("group-1", time.time())

    async def run():
        first = await plugin.inject_no_quote_hint(session_id="group-1", items=[])
        # 第二次拿「第一次的完整 kwargs」再调一次：模拟同一请求被多个处理器依次经过
        second = await plugin.inject_no_quote_hint(**first["modified_kwargs"])
        return first, second

    first, second = asyncio.run(run())
    assert len(first["modified_kwargs"]["items"]) == 1
    assert len(second["modified_kwargs"]["items"]) == 1, "同一请求不许重复注入"


def test_hook_skips_expired_window():
    _module, plugin, _host = _make_plugin()
    plugin._proactive_no_quote_until["group-1"] = time.time() - 1.0

    async def run():
        return await plugin.inject_no_quote_hint(session_id="group-1", items=[])

    assert asyncio.run(run()) == {"action": "continue"}


def test_hook_only_touches_its_own_session():
    _module, plugin, _host = _make_plugin()
    plugin._open_no_quote_window("group-1", time.time())

    async def run():
        return await plugin.inject_no_quote_hint(session_id="group-2", items=[])

    assert asyncio.run(run()) == {"action": "continue"}, "别的会话的正常引用行为不该被影响"


def test_hook_without_session_id_is_a_noop():
    _module, plugin, _host = _make_plugin()
    plugin._open_no_quote_window("group-1", time.time())

    async def run():
        return await plugin.inject_no_quote_hint(items=[])

    assert asyncio.run(run()) == {"action": "continue"}


def test_missing_session_id_is_reported_once_when_a_window_is_open():
    """真机上「规则永远不生效」必须能查：窗口开着但没有会话键 ⇒ 告警一次。"""

    _module, plugin, _host = _make_plugin()
    plugin._open_no_quote_window("group-1", time.time())
    capture = _Capture()
    try:

        async def run():
            await plugin.inject_no_quote_hint(items=[])
            await plugin.inject_no_quote_hint(items=[])  # 第二次不该再刷

        asyncio.run(run())
    finally:
        capture.stop()
    warnings = [line for line in capture.records if "没有 session_id" in line]
    assert len(warnings) == 1, capture.records


def test_hook_warns_when_items_shape_is_unexpected():
    """「注入了但没生效」必须留痕，否则真机上永远查不出来。"""

    _module, plugin, _host = _make_plugin()
    plugin._open_no_quote_window("group-1", time.time())
    capture = _Capture()
    try:

        async def run():
            return await plugin.inject_no_quote_hint(session_id="group-1", items="不是列表")

        result = asyncio.run(run())
    finally:
        capture.stop()
    assert result == {"action": "continue"}
    assert any("注入失败" in line for line in capture.records), capture.records


# ------------------------------------------------- 3. 窗口由「触发成功」打开


def _decision(material: dict) -> P.ProactiveDecision:
    return P.ProactiveDecision(
        True, "ok", score=0.8, material=material, intent=P.build_intent(material), detail="测试"
    )


def test_trigger_opens_the_window_on_success():
    _module, plugin, host = _make_plugin()
    material = {"label": "小事", "text": "刚泡了杯茶"}
    now = time.time()

    accepted = asyncio.run(plugin._trigger_proactive("group-1", _decision(material), "2026-10-05", now))
    assert accepted, "FakeHost 默认受理主动任务"
    assert "group-1" in plugin._proactive_no_quote_until
    assert plugin._proactive_no_quote_until["group-1"] > now

    async def run():
        return await plugin.inject_no_quote_hint(session_id="group-1", items=[])

    assert asyncio.run(run())["modified_kwargs"]["items"], "触发后当轮必须能注入规则"


def test_trigger_failure_does_not_open_the_window():
    _module, plugin, host = _make_plugin()
    host.returns["maisaka.proactive.trigger"] = {"success": False, "error": "宿主拒绝"}
    material = {"label": "小事", "text": "刚泡了杯茶"}

    accepted = asyncio.run(
        plugin._trigger_proactive("group-1", _decision(material), "2026-10-05", time.time())
    )
    assert not accepted
    assert "group-1" not in plugin._proactive_no_quote_until, "宿主没受理就不该开窗"


def test_window_table_is_pruned_when_it_grows():
    _module, plugin, _host = _make_plugin()
    now = time.time()
    # 塞满过期窗口 + 一个有效窗口，超过上限后过期项应被清掉
    for index in range(12):
        plugin._proactive_no_quote_until[f"old-{index}"] = now - 10
    plugin._open_no_quote_window("group-1", now)
    assert "group-1" in plugin._proactive_no_quote_until
    assert len(plugin._proactive_no_quote_until) <= 9, plugin._proactive_no_quote_until
