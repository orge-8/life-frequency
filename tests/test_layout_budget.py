# -*- coding: utf-8 -*-
"""L3：交付形态的**结构纪律**（v1.21.0 建立；v1.23.3 删除两条预算门禁）。

保留什么、为什么保留
--------------------

这个文件 v1.21.0 建立时钉的是五条预算/纪律。v1.23.3 按用户指令**删掉两条预算**
（``plugin.py`` 行数、运行时部署单元体积），只留三条与「代码长什么样」直接相关的
结构纪律：

1. 配置模型不许回流 ``plugin.py``（它回流一次就等于把配置层拆了又拼回去）；
2. README 是上手文档，不是参考手册（细节章节要外移到 ``docs/``）；
3. 插件目录里不许出现 ``life_state.json`` / ``config.toml``（会遮住真机数据）。

为什么删掉那两条预算（钉在这里，免得下次又加回来）
------------------------------------------------

**理由不是「它们碍事」，是「它们量错东西」**：

* 2026-10-18 那次「插件是不是太臃肿」的评估，结论第一条就是**运行时不是痛点**：
  「9 千行的模块照样秒加载、常驻内存也没变」。既然体积/行数与启动耗时、常驻内存
  都不相关，拿它们当门禁就是在量一个无关的量。
* 真正的痛点（维护面：改一处要翻两百个方法；交付面：28 张配置卡片）**没有被这两条
  预算解决过**——它们只惩罚了「往 plugin.py 里堆新代码」这一种做法，而那次评估真正
  生效的是配置层出仓 + 默认视图治理 + 文档出仓，跟预算数字无关。
* 两条预算在 v1.22.4 / v1.22.5 / v1.23.2 连续三次贴顶后被迫抬升（7,300→7,400→7,500；
  1.1→1.2→1.3 MB）。**一条每年要抬三次的线，已经不是纪律而是仪式**：每次抬升都要写
  理由、都要重新实测，而这些理由（``@API`` 必须挂在插件类上、睡醒补答进真机）事后看
  都成立——也就是说这三条线从头到尾没有拦下任何一件坏事。

删掉之后仍然由活的东西约束形态：

* 配置模型回流 —— 上面第 1 条（本文件）；
* 「胶水不许长肉」——``check_plugin.py`` 的 ``self.ctx`` 反推与
  ``life_config.py`` 不许出现 ``self.ctx`` 的 AST 检查；
* 可维护性——``tests/`` 的 1,500+ 条单测与冒烟。

历史记录（保留，便于回溯当时的判断）
----------------------------------

v1.23.2 修订：三条一起抬（体积 1.2→1.3 MB、plugin.py 7,400→7,500、
life_config.py 2,700→2,750），因为三条同时贴顶。

v1.22.5 修订：``plugin.py`` 7,300→7,400（``@API`` 必须挂在插件类上，是搬不走的
宿主胶水）；体积计量加入 ``webui.json`` 并同步 1.1→1.2 MB。
"""

import ast
import pathlib

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _lines(name: str) -> int:
    return len((PLUGIN_DIR / name).read_text(encoding="utf-8").splitlines())


def test_config_models_live_in_the_config_module():
    """配置模型不许回流 `plugin.py`——回流了就等于把配置层拆了又拼回去。"""

    source = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            text = getattr(base, "id", None) or getattr(base, "attr", None) or ""
            if text == "PluginConfigBase":
                offenders.append(node.name)
    assert offenders == [], (
        f"plugin.py 里又定义了配置模型：{offenders}。请搬到 life_config.py，"
        "并在 plugin.py 顶部 import"
    )


def test_readme_stays_an_onboarding_document():
    """README 是上手文档，不是参考手册：细节章节外移到 `docs/`。"""

    count = _lines("README.md")
    assert count <= 1100, (
        f"README 已 {count} 行（预算 1100）。参考性内容请放 docs/"
        "（config.md / interop.md / gates.md 就是先例）"
    )
    text = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    for heading in ("## 安装", "## 配置", "## 命令", "## 故障排查"):
        assert heading in text, f"README 缺少必要章节：{heading}"


def test_no_stray_state_files_are_committed():
    """状态与配置是运行时的，不许出现在插件目录里（会遮住真机数据）。"""

    for name in ("life_state.json", "config.toml"):
        assert not (PLUGIN_DIR / name).exists(), (
            f"插件目录里出现了 {name}：运行时应写在 Runner 授予的数据目录"
        )
