# -*- coding: utf-8 -*-
"""L3：交付形态的结构纪律。

行数与体积预算已全部删除（用户指令 2026-10-29）。剩下的是与“代码长什么样”直接
相关、且无法用测试替代的结构检查：

1. 配置模型不许回流 ``plugin.py``（回流一次就等于把配置层拆了又拼回去）；
2. README 必须保留安装、配置、命令、故障排查四个上手章节；
3. 插件目录里不许出现 ``life_state.json`` / ``config.toml``（会遮住真机数据）。

不再检查 README 行数或任何文件大小：它们与启动耗时、内存和可维护性没有稳定关系，
而且会迫使正常文档和功能改动为了“贴线”而删信息。真正的约束由配置层 AST 检查、
``check_plugin.py`` 的能力反推、全量单测与冒烟承担。
"""

import ast
import pathlib

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent


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


def test_readme_keeps_onboarding_sections():
    """README 可以任意长，但四个上手章节不能丢。"""

    text = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    for heading in ("## 安装", "## 配置", "## 命令", "## 故障排查"):
        assert heading in text, f"README 缺少必要章节：{heading}"


def test_no_stray_state_files_are_committed():
    """状态与配置是运行时的，不许出现在插件目录里（会遮住真机数据）。"""

    for name in ("life_state.json", "config.toml"):
        assert not (PLUGIN_DIR / name).exists(), (
            f"插件目录里出现了 {name}：运行时应写在 Runner 授予的数据目录"
        )
