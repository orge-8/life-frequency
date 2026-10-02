# -*- coding: utf-8 -*-
"""L3：plugin.py 里的纯辅助函数（能力返回值归一化、config 垫片）。

这些是「本地绿、真机挂」最容易出问题的两处边界，所以单独钉住：

- 能力返回值成功时是裸值（list/float/str），**失败时是 ``{"success": false, ...}``**；
  归一化助手必须同时接住两种形状。
- 用户在 config.toml 里常把列表写成裸字符串（``admin_ids = "123"``）；
- SDK 是**先版本检查再 pydantic**，config.toml 少写 ``[plugin]`` 节会直接抛
  ``PluginConfigVersionError``，所以要有补齐的垫片。
"""

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

import plugin as P  # noqa: E402


# ---------------------------------------------------------------- 列表归一化


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, []),
        ([], []),
        (["a", "b"], ["a", "b"]),
        ("123456", ["123456"]),
        ("123, 456", ["123", "456"]),
        ("123，456", ["123", "456"]),  # 中文逗号
        ("123;456", ["123", "456"]),   # 分号
        ("123\n456", ["123", "456"]),  # 换行
        ("  ", []),
    ],
)
def test_as_str_list(raw, expected):
    assert P._as_str_list(raw) == expected


# ---------------------------------------------------------------- 返回值归一化


@pytest.mark.parametrize(
    "raw,expected",
    [
        ([{"a": 1}], [{"a": 1}]),
        ({"success": False, "error": "no session"}, []),
        (None, []),
        ("nope", []),
    ],
)
def test_as_sequence(raw, expected):
    assert P._as_sequence(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (1.5, 1.5),
        (2, 2.0),
        ({"success": False, "error": "no session"}, None),
        ({"success": True, "value": 0.4}, None),  # 未拆包说明调用方搞错了，这里也不猜
        (None, None),
        ("0.4", None),
        (True, None),  # bool 是 int 的子类，必须排除
    ],
)
def test_as_number(raw, expected):
    assert P._as_number(raw) == expected
    assert P._as_number(raw, default=-1.0) == (expected if expected is not None else -1.0)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("麦麦", "麦麦"),
        ("  麦麦  ", "麦麦"),
        ({"success": False}, ""),
        (None, ""),
        ("", ""),
        (["x"], ""),
    ],
)
def test_as_text(raw, expected):
    assert P._as_text(raw) == expected


def test_as_text_default_used_when_empty():
    assert P._as_text(None, "麦麦") == "麦麦"
    assert P._as_text("", "麦麦") == "麦麦"
    assert P._as_text("x", "麦麦") == "x"


# ---------------------------------------------------------------- config 垫片


def test_sanitize_config_fills_missing_plugin_section():
    result = P.LifeFrequencyPlugin._sanitize_config({})
    assert result["plugin"]["config_version"] == P.SUPPORTED_CONFIG_VERSION


def test_sanitize_config_keeps_existing_version():
    result = P.LifeFrequencyPlugin._sanitize_config(
        {"plugin": {"config_version": "9.9.9", "enabled": False}}
    )
    assert result["plugin"]["config_version"] == "9.9.9"
    assert result["plugin"]["enabled"] is False


def test_sanitize_config_replaces_blank_version():
    result = P.LifeFrequencyPlugin._sanitize_config({"plugin": {"config_version": "  "}})
    assert result["plugin"]["config_version"] == P.SUPPORTED_CONFIG_VERSION


def test_sanitize_config_passes_through_non_dict():
    assert P.LifeFrequencyPlugin._sanitize_config(None) is None
    assert P.LifeFrequencyPlugin._sanitize_config("nope") == "nope"


def test_sanitize_config_does_not_mutate_input():
    original = {"plugin": {"enabled": True}}
    P.LifeFrequencyPlugin._sanitize_config(original)
    assert original == {"plugin": {"enabled": True}}


# ---------------------------------------------------------------- 配置模型


def test_config_model_defaults_are_present():
    config = P.LifeFrequencyConfig()
    assert config.plugin.config_version == P.SUPPORTED_CONFIG_VERSION
    assert config.plugin.enabled is True
    assert config.activity.mode == "llm"
    assert config.activity.llm.min_interval_seconds == 600
    assert config.activity.min_dwell_minutes == 60
    assert config.activity.min_sleep_minutes == 180
    assert config.simulation.tick_seconds == 600
    assert config.frequency.max_adjust == 2.0
    assert config.security.admin_ids == []
    assert config.proactive.enabled is False
    assert config.prompt.inject_enabled is True


def test_config_model_nested_section_is_addressable():
    """[activity.llm] 必须是嵌套节（而不是根级 activity_llm）。"""

    dumped = P.LifeFrequencyConfig().model_dump()
    assert "activity_llm" not in dumped
    assert isinstance(dumped["activity"]["llm"], dict)


def test_config_model_string_list_fields_normalize():
    config = P.LifeFrequencyConfig.model_validate(
        {"security": {"admin_ids": "123,456"}, "apply": {"target_chats": "group:1"}}
    )
    assert config.security.admin_ids == ["123", "456"]
    assert config.apply.target_chats == ["group:1"]


def test_plugin_id_matches_manifest():
    import json
    import pathlib

    manifest_path = pathlib.Path(__file__).resolve().parent.parent / "_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert P.__plugin_id__ == manifest["id"]


def test_default_factor_tables_parse():
    activity = P._default_activity_factors()
    health = P._default_health_factors()
    assert activity["sleep"] == 0.0
    assert activity["sick_rest"] == 1.0  # 中性，避免与 health 双重抑制
    assert activity["night_study"] == 0.5
    assert health["cold"] == 0.3


# ---------------------------------------------------------------- 架构不变量


def _ctx_self_attribute_lines(path):
    """用 AST 找出所有 ``self.ctx`` 属性访问的行号（不是文本匹配）。"""

    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        inner = node.value
        if (
            isinstance(inner, ast.Attribute)
            and inner.attr == "ctx"
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "self"
        ):
            hits.append(node.lineno)
    return hits


def test_pure_modules_never_touch_ctx():
    """纯模块里不允许出现 ``self.ctx``。

    ``check_plugin.py`` 的能力反推**只扫 plugin.py**，所以一旦业务逻辑里冒出
    ``ctx`` 调用，manifest 就不会有对应声明，真机直接抛
    ``RuntimeError: 插件 X 未获授权能力`` —— 本地门禁还可能看不出问题。
    这条断言把这个架构约定钉死。
    """

    import pathlib

    plugin_dir = pathlib.Path(__file__).resolve().parent.parent
    offenders = {}
    for path in sorted(plugin_dir.glob("life_*.py")):
        hits = _ctx_self_attribute_lines(path)
        if hits:
            offenders[path.name] = hits
    assert not offenders, f"纯模块里出现了 self.ctx：{offenders}"


def test_all_manifest_capabilities_are_used_in_plugin_py():
    """manifest 声明的能力必须在 plugin.py 里真的被调用（避免 WARN 与空挂权限）。"""

    import json
    import pathlib
    import re

    plugin_dir = pathlib.Path(__file__).resolve().parent.parent
    manifest = json.loads((plugin_dir / "_manifest.json").read_text(encoding="utf-8"))
    source = (plugin_dir / "plugin.py").read_text(encoding="utf-8")

    used = set()
    # 能力名就是 self.ctx 之后的整条点分链（可能是 send.text，也可能是
    # maisaka.proactive.trigger 这种三段），所以按链抓而不是按两段抓。
    for chain in re.findall(r"self\.ctx\.((?:[a-z_]+\.)+[a-z_]+)\(", source):
        if chain.startswith(("logger.", "paths.")):
            continue  # ctx.logger / ctx.paths 是辅助对象，不需要声明能力
        used.add(chain)

    declared = set(manifest["capabilities"])
    missing = used - declared
    unused = declared - used
    assert not missing, f"代码用了但没声明：{sorted(missing)}"
    assert not unused, f"声明了但没用到：{sorted(unused)}"
