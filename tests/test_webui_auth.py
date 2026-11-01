# -*- coding: utf-8 -*-
"""WebUI 写接口的鉴权档位 + 睡眠积压是否留存原文（v1.25.1）。

插件中心 1.25.0 审核提出两条待确认口径，这里把它们钉成可回归的行为：

1. ``life.pause`` / ``life.resume`` 是零参数 ``@API``，而同一执行体的
   ``/生活 暂停``、``/生活 恢复`` 走 fail-closed 的 ``_is_admin``。
   宿主 API 层**不携带调用方身份**（``supervisor.invoke_api`` 只传 args），
   所以插件侧无法区分「dashboard 按钮」和「别的插件 ``api.call``」。
   处置：新增 ``[security].webui_write_mode``，``login``（默认）沿用「登录即可用」，
   ``strict`` 要求调用方自证 ``admin_id``。两条档位都要钉子。

2. 睡醒补答原本把消息原文写进 ``sleep_backlog``。``requote_limit`` 默认 0 时
   原文**不参与任何输出**，留着只是让聊天记录在插件侧多存一份。
   处置：``[catchup].store_text`` 默认关。
"""

import pathlib
import sys

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config  # noqa: E402

import life_catchup as C  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_webui_auth")


def _make_plugin(**security_overrides):
    module = _load()
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["frequency"]["quiet_hours"] = []
    config["events"]["fire_probability"] = 0.0
    config["proactive"]["enabled"] = False
    config["simulation"]["dry_run"] = False
    security = dict(config.get("security") or {})
    security.update(security_overrides)
    config["security"] = security
    bind_context(plugin, ctx, config)
    return module, plugin, host


# ---------------------------------------------------------------- 默认档位


def test_default_write_mode_is_login():
    """默认 ``login``：保持 1.25.0 的行为，页面按钮不会突然失效。"""

    _module, plugin, _host = _make_plugin()
    assert plugin.config.security.webui_write_mode == "login"


@pytest.mark.parametrize("raw", ["", "  ", "LOGIN", "strict ", "loose", "admin", None])
def test_write_mode_normalizes_to_known_values(raw):
    """坏值一律落到 ``login``（不抛错，不让配置笔误把按钮锁死）。"""

    _module, plugin, _host = _make_plugin(webui_write_mode=raw)
    assert plugin.config.security.webui_write_mode in {"login", "strict"}
    if str(raw or "").strip().lower() != "strict":
        assert plugin.config.security.webui_write_mode == "login"


# ---------------------------------------------------------------- login 档


def test_login_mode_pause_works_without_actor():
    """``login`` 档：与 1.25.0 一致，无身份也能暂停（依赖宿主登录态）。"""

    _module, plugin, _host = _make_plugin()
    result = plugin.api_pause()
    out = _run(result)
    assert out["ok"] is True
    assert out["paused"] is True
    assert out["auth"] == "login"
    assert plugin._state.paused_override is True


def test_login_mode_is_idempotent():
    _module, plugin, _host = _make_plugin()
    _run(plugin.api_pause())
    again = _run(plugin.api_pause())
    assert again["ok"] is True
    assert again["changed"] is False, "重复暂停必须是无害的"
    assert again["paused"] is True


# ---------------------------------------------------------------- strict 档


def test_strict_mode_rejects_without_actor():
    """``strict`` 档：不传身份即拒绝，且状态**不变**（fail-closed）。"""

    _module, plugin, _host = _make_plugin(
        webui_write_mode="strict", admin_ids=["123456"]
    )
    out = _run(plugin.api_pause())
    assert out["ok"] is False
    assert out["changed"] is False
    assert out["auth"] == "strict"
    assert plugin._state.paused_override is False, "被拒时不许改状态"


def test_strict_mode_rejects_empty_admin_list():
    """名单为空 = 没人有权（与 ``_is_admin`` 同一口径），传了身份也不放行。"""

    _module, plugin, _host = _make_plugin(webui_write_mode="strict", admin_ids=[])
    out = _run(plugin.api_pause(admin_id="123456"))
    assert out["ok"] is False
    assert plugin._state.paused_override is False


def test_strict_mode_rejects_unknown_actor():
    _module, plugin, _host = _make_plugin(
        webui_write_mode="strict", admin_ids=["123456"]
    )
    out = _run(plugin.api_pause(admin_id="999999"))
    assert out["ok"] is False
    assert plugin._state.paused_override is False


@pytest.mark.parametrize("actor", ["123456", "qq:123456"])
def test_strict_mode_allows_known_admin(actor):
    """``'123'`` 与 ``'qq:123'`` 两种写法都认（与 ``_is_admin`` 一致）。"""

    _module, plugin, _host = _make_plugin(
        webui_write_mode="strict", admin_ids=["123456"]
    )
    out = _run(plugin.api_pause(admin_id=actor))
    assert out["ok"] is True
    assert out["auth"] == "strict"
    assert plugin._state.paused_override is True
    assert out["actor"] and actor.split(":")[-1] not in out["actor"], "回显必须脱敏"


def test_strict_mode_resume_also_guarded():
    """恢复口子同档位——只卡暂停等于没卡。"""

    _module, plugin, _host = _make_plugin(
        webui_write_mode="strict", admin_ids=["123456"]
    )
    plugin._state.paused_override = True
    out = _run(plugin.api_resume())
    assert out["ok"] is False
    assert plugin._state.paused_override is True, "被拒时不许悄悄恢复"


# ---------------------------------------------------------------- 原文留存


def test_catchup_store_text_defaults_off():
    """默认不留原文：补答只用「谁、几点、私聊还是群里被 @」。"""

    assert C.CatchupRules().store_text is False


def test_catchup_rules_map_store_text():
    _module, plugin, _host = _make_plugin()
    assert plugin._catchup_rules().store_text is False

    plugin.config.catchup.store_text = True
    assert plugin._catchup_rules().store_text is True


def test_requote_without_store_text_degrades_to_no_quote():
    """开了 ``requote_limit`` 却没开 ``store_text``：退化成不复述，而不是报错。

    fail-safe 的方向很重要——宁可少一句引文，也不要为了引文把原文默认写盘。
    """

    rules = C.CatchupRules(requote_limit=3, store_text=False)
    candidate = C.Candidate(
        session_id="s", person_id="p", user_id="u",
        is_group=False, last_at=1000.0, count=1,
        entries=({"at": 1000.0, "text": ""},),
    )
    intent = C.build_intent(
        candidate, rules=rules, label_of_time=lambda _t: "03:12"
    )
    assert "原文摘录" not in intent
    assert "03:12" in intent, "时间窗仍然要保留（元数据足以说清发生了什么）"


# ---------------------------------------------------------------- 小工具


def _run(value):
    """``@API`` 方法是 ``async def``，测试里同步跑一遍。"""

    import asyncio

    return asyncio.run(value)
