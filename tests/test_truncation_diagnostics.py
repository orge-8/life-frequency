# -*- coding: utf-8 -*-
"""活动决策「输出被 max_tokens 截断」的可归因性（v1.23.4）。

真机日志（2026-10-24 19:38:57）：

```
[模型] 模型MiniMax-M3.1-Flash因为达到最大输出 token 限制（max_tokens=200，任务=utils…）
[plugin.org.orge-8.life-frequency] 活动决策连续失败 18 次，进入 30 分钟冷却
```

这两行之间当时**没有任何因果线索**：宿主那条 warning 打在宿主侧，插件只知道
「我没解析出 JSON」，于是「被截断」在插件日志里长得和「模型拒答」一模一样。
18 次连续失败（每次冷却放行后立刻再失败，≈7.5 小时）因此无从归因。

本文件钉死三件事：

1. 默认 ``max_tokens`` 不许退回 200 —— 截断是**确定性**失败，重试/换温度都没用；
2. 解析失败的 raw 必须**出现在日志里**（以前只进 ``llm_last_raw``，而它只在状态
   文件里，不打日志）；
3. 「疑似截断」与「模型没给 JSON」措辞必须不同 —— 只有前者该提示调 ``max_tokens``，
   后者提示调上限等于让人白改配置。
"""

import logging
import pathlib
import sys

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config  # noqa: E402

import life_config as C  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
LOGGER_NAME = "plugin.org.orge-8.life-frequency"


def _make_plugin():
    from fakehost import load_plugin_module

    module = load_plugin_module(PLUGIN_DIR, "life_frequency_truncation")
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["events"]["fire_probability"] = 0.0
    bind_context(plugin, ctx, config)
    plugin.config
    return module, plugin


# ------------------------------------------------------------ 1. 默认上限


def test_default_max_tokens_leaves_room_for_a_reasoning_preamble():
    """默认值必须明显大于「一个 JSON」的裸长度。

    真机那次是 200：模型先写一段推理再给 JSON，200 用完时 JSON 还在键名中间。
    给 800 不等于鼓励啰嗦，而是给「先生成后输出」的模型留余地。
    """

    assert C.ActivityLLMConfig().max_tokens >= 800, (
        "max_tokens 退回 200 会让「先推理再给 JSON」的模型 100% 截断（确定性失败）"
    )


# ------------------------------------------------------- 2. 截断形态的识别


def test_cut_json_is_recognised():
    module, _plugin = _make_plugin()
    looks = module._looks_like_cut_json

    # 截断：有 { 没有配平的 }，或停在字符串/键名中间
    assert looks('{"activity": "sleep", "scene": "在床上翻了个') is True
    assert looks('```json\n{"activity": "sleep"') is True
    # 完整 JSON（含围栏/前后散文）：不是截断
    assert looks('{"activity": "sleep", "scene": "听歌"}') is False
    assert looks('按照要求：```{"activity": "music"}```') is False
    # 根本不是 JSON：不能栽赃给 max_tokens
    assert looks("抱歉，作为一个人工智能我无法完成这个请求。") is False
    assert looks("") is False


# --------------------------------------------------- 3. 日志里必须看得到 raw


def test_truncated_output_is_logged_with_snippet_and_actionable_hint(caplog):
    module, plugin = _make_plugin()
    raw = '{"activity": "sleep", "scene": "把被子往上拉了拉，翻身'
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        module.LifeFrequencyPlugin._note_llm_failure(plugin, 1000.0, raw)

    text = caplog.text
    assert "无法解析" in text, "解析失败必须在日志里留痕"
    assert "截断" in text, "截断形态要说成截断，而不是含糊的「解析失败」"
    assert "把被子往上拉了拉" in text, "raw 片段要进日志，否则无从判断是截断还是拒答"
    assert "max_tokens" in text, "截断是确定性失败，必须直接给出该调哪个键"
    assert int(plugin._state.llm_fail_streak) == 1


def test_non_json_prose_is_not_blamed_on_max_tokens(caplog):
    """模型的散文/拒答不能说成「截断」——否则用户会去改一个没用的配置。"""

    module, plugin = _make_plugin()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        module.LifeFrequencyPlugin._note_llm_failure(
            plugin, 1000.0, "抱歉，作为一个人工智能我无法完成这个请求。"
        )

    text = caplog.text
    assert "无法解析" in text
    assert "截断" not in text, "纯散文不是截断"
    assert "max_tokens" not in text, "不该让用户去调一个与本次失败无关的配置"


def test_structured_failure_markers_are_not_treated_as_model_output(caplog):
    """``<failed>`` / ``<exception>`` 这类内部分类标记不是模型输出，不该刷片段日志。"""

    module, plugin = _make_plugin()
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        module.LifeFrequencyPlugin._note_llm_failure(plugin, 1000.0, "<failed>宿主返回 500")
        module.LifeFrequencyPlugin._note_llm_failure(plugin, 1001.0, "<exception>连接被重置")

    assert "无法解析" not in caplog.text
    assert int(plugin._state.llm_fail_streak) == 2
