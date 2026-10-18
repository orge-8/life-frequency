# -*- coding: utf-8 -*-
"""L3：WebUI 配置页的「默认视图」预算（v1.21.0）。

要解决的真问题（2026-10-18 评估）：v1.20.1 的配置页有 **28 张卡片 / 251 个字段**
默认全开，用户的第一反应是「不知道从哪下手」。痛点不是字段总数（那些字段大多
是标定过的调参项，删不掉也挪不走），而是**默认视图的密度**。

所以这里的门禁量的是「默认视图」而不是「字段总数」：

1. 默认展开的字段数有上限（``DEFAULT_VIEW_FIELD_BUDGET``）——防止以后加功能时
   顺手往常用节里塞字段，把页面重新撑回去；
2. 默认展开的节必须恰好是 ``_WEBUI_PRIMARY_SECTIONS`` 里那几个，且都排在最前；
3. 每个折叠节必须写清「什么时候需要动它」——折叠不是藏起来，是让用户不点开
   也知道这节跟自己有没有关系。

这些断言只读 Schema 的展示元数据（``collapsed`` / ``order`` / ``description``），
不涉及任何配置键，因此天然不会影响 ``config.toml`` 的向后兼容。
"""

import pathlib
import sys

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent

#: 默认视图可见字段的上限。当前 35（7 节），留一倍余量给后续功能。
#: 超过它就意味着常用节又被塞满了，得重新想「这个新字段属于谁」。
DEFAULT_VIEW_FIELD_BUDGET = 60


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_webui_view")


def _schema() -> dict:
    """插件注册给 WebUI 的配置 Schema（与真机同一条生成链路）。

    ⚠ 必须走**插件实例**的 ``get_webui_config_schema``：真机 Runner 调的就是它。
    """

    module = _load()
    plugin = module.create_plugin()
    return plugin.get_webui_config_schema(
        plugin_id=module.__plugin_id__, plugin_name="生活频率", plugin_version="1.21.0",
        plugin_description="", plugin_author="orge-8",
    )["sections"]


def _visible(sections: dict) -> dict:
    return {name: s for name, s in sections.items() if not s.get("collapsed")}


def test_default_view_stays_within_field_budget():
    """默认展开的字段数不能超过预算。"""

    sections = _schema()
    visible = _visible(sections)
    count = sum(len(s.get("fields") or {}) for s in visible.values())
    assert count <= DEFAULT_VIEW_FIELD_BUDGET, (
        f"默认视图有 {count} 个字段（上限 {DEFAULT_VIEW_FIELD_BUDGET}），"
        f"展开的节：{sorted(visible)}。新加的字段应当放进已有的高级节，"
        "而不是继续堆进常用节"
    )


def test_visible_sections_are_exactly_the_primary_set():
    """展开的节必须与 ``_WEBUI_PRIMARY_SECTIONS`` 完全一致。"""

    module = _load()
    expected = set(module._WEBUI_PRIMARY_SECTIONS)
    visible = set(_visible(_schema()))
    assert visible == expected, (
        f"默认展开的节与白名单不一致：多出 {sorted(visible - expected)}，"
        f"缺少 {sorted(expected - visible)}"
    )


def test_primary_sections_sort_before_the_folded_ones():
    """常用节必须排在最前：折叠节整体后移，用户打开页面看到的就是常用项。"""

    sections = _schema()
    visible_orders = [
        float(s.get("order") or 0) for s in _visible(sections).values()
    ]
    folded_orders = [
        float(s.get("order") or 0)
        for s in sections.values()
        if s.get("collapsed")
    ]
    assert visible_orders, "没有默认展开的节"
    assert max(visible_orders) < min(folded_orders), (
        f"折叠节里出现了比常用节更靠前的（常用最大 {max(visible_orders)}，"
        f"折叠最小 {min(folded_orders)}）"
    )


def test_every_folded_section_explains_when_to_open_it():
    """折叠节的说明里必须有「什么时候需要动」——折叠后标题与说明仍然可见。"""

    sections = _schema()
    missing = [
        name
        for name, section in sections.items()
        if section.get("collapsed")
        and "什么时候需要动" not in str(section.get("description") or "")
    ]
    assert missing == [], (
        f"这些折叠节没写「什么时候需要动」，用户点不点开全凭猜：{missing}"
    )


def test_every_field_carries_a_hint():
    """可视化模式只渲染 label/hint/placeholder，不渲染 description。

    字段填法写在 description 里而不搬进 hint，用户在配置页上就一个字都看不到。
    """

    sections = _schema()
    missing = [
        (name, field_name)
        for name, section in sections.items()
        for field_name, field in (section.get("fields") or {}).items()
        if not str(field.get("hint") or "").strip()
    ]
    assert missing == [], f"这些字段在可视化模式下没有任何说明：{missing}"
