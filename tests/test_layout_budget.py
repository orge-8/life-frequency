# -*- coding: utf-8 -*-
"""L3：交付形态预算（v1.21.0；v1.22.5 修订）。

背景：2026-10-18 的「插件是不是太臃肿」评估。实测痛点不是运行时（9 千行的模块照样
秒加载、常驻内存也没变），而是**维护面**与**交付面**：

* 维护面——`plugin.py` 曾是一个 6,130 行的类 + 2,100 行配置声明，改一处要翻两百个方法；
* 交付面——装的人面对 28 张配置卡片 / 251 个字段，不知道从哪下手。

所以这里把三条预算钉成门禁，防止功能继续往 `plugin.py` 里堆：

1. ``plugin.py`` ≤ 7,400 行：它只放**宿主胶水**（`self.ctx.*` 必须在它里面，因为
   `check_plugin.py` 只扫它反推 capability）。纯逻辑去 `life_*.py`，配置模型去
   `life_config.py`。
2. ``life_config.py`` ≤ 2,700 行：配置模型只该描述「有哪些字段、怎么展示」，
   长业务逻辑说明放错地方了。
3. 运行时部署单元（`plugin.py` + `life_*.py` + `_manifest.json` + `webui.json`）
   ≤ 1.2 MB：`tests/`、`collect_diagnostics.py`、`docs/`、`CHANGELOG.md` 都不进真机，
   不算。

超预算时的正确做法**不是**改这个文件，而是问「这个新东西属于哪个模块」。

v1.22.5 修订记录（两处，都是本次自定义 WebUI 页面直接逼出来的）：

* **条目上限 7,300 → 7,400**：``webui.json`` 的 ``@API`` 必须挂在插件类上
  （SDK 装饰器只认类方法，``life_webui`` 装不下），三个端点 + 两行 import 是
  **搬不走的宿主胶水**（净增 18 行，7,276 → 7,294）。原上限只留 6 行余量，
  下一次任何命令改动都会误触——那会训练出「随便抬预算」的坏习惯，所以一次抬到
  留 ~100 行工作余量，并把理由钉在这里。逻辑本体仍在 ``life_webui.py``。
* **payload 计量加入 ``webui.json``**：它随插件进真机（``runner_main`` 读的就是
  它），却不在原计量范围里——custom 页面 HTML 上限 2 MiB，漏计等于门禁开叉。
  计入后总量 1,124,044 → 1,161,514 B（超 1.1 MB 上限 8 KB），故上限同步抬到
  1.2 MB：净增 37.5 KB 全部是自定义页面的出货代码（``life_webui.py`` 28 KB +
  ``webui.json`` 9 KB），与「页面声明 + 投影层」这个功能相称，留 ~97 KB 工作余量。
"""

import ast
import pathlib

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent

ENTRY_BUDGET_LINES = 7400
CONFIG_BUDGET_LINES = 2700
RUNTIME_BUDGET_BYTES = int(1.2 * 1024 * 1024)


def _lines(name: str) -> int:
    return len((PLUGIN_DIR / name).read_text(encoding="utf-8").splitlines())


def test_entry_module_stays_a_glue_layer():
    """`plugin.py` 是宿主胶水层，不许长回 9 千行。"""

    count = _lines("plugin.py")
    assert count <= ENTRY_BUDGET_LINES, (
        f"plugin.py 已 {count} 行（预算 {ENTRY_BUDGET_LINES}）。"
        "新逻辑请放进 life_*.py；配置字段放进 life_config.py"
    )


def test_config_module_stays_declarative():
    """`life_config.py` 只描述配置，别在里面写业务流程。"""

    count = _lines("life_config.py")
    assert count <= CONFIG_BUDGET_LINES, (
        f"life_config.py 已 {count} 行（预算 {CONFIG_BUDGET_LINES}）"
    )


def test_runtime_payload_stays_under_budget():
    """真机要拷的那部分 ≤ 1.1 MB（文档、测试、诊断工具不算）。"""

    runtime = [PLUGIN_DIR / "plugin.py", PLUGIN_DIR / "_manifest.json"]
    runtime += sorted(PLUGIN_DIR.glob("life_*.py"))
    # v1.22.5：webui.json 随插件进真机（Runner 注册时读它），必须计入
    declaration = PLUGIN_DIR / "webui.json"
    if declaration.is_file():
        runtime.append(declaration)
    total = sum(p.stat().st_size for p in runtime if p.is_file())
    assert total <= RUNTIME_BUDGET_BYTES, (
        f"运行时部署单元 {total / 1024:.0f} KB（预算 {RUNTIME_BUDGET_BYTES / 1024:.0f} KB），"
        f"文件数 {len(runtime)}"
    )


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
