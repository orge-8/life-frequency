# -*- coding: utf-8 -*-
"""L3：宿主两套触发模式公式的数值断言。

必要时会去本机的 MaiBot 源码里把 ``reply_necessity`` 模块拉进来逐点对比——
那是**真值校验**，不是自说自话。找不到源码就跳过（不算通过，也不算失败）。
"""

import importlib.util
import math
import os
import pathlib

import pytest

import life_host_model as lm


def _host_source_candidates() -> tuple[pathlib.Path, ...]:
    """宿主真值源的查找顺序：环境变量 → 常见检出位置。

    v1.1.0 只硬编码了作者本机的绝对路径（``C:\\Users\\<作者>\\WorkBuddy\\...\\MaiBot``），
    换一台机器就静默 SKIP（``pytest -q`` 还不显示 skip），"真值校验"其实没有跑。
    这里支持 ``LF_MAIBOT_SRC`` 指向任意检出目录，并额外尝试几个常见位置。
    """

    candidates: list[pathlib.Path] = []
    env = os.environ.get("LF_MAIBOT_SRC", "").strip()
    if env:
        root = pathlib.Path(env)
        candidates.append(root / "src" / "maisaka" / "reply_necessity.py")
        candidates.append(root / "reply_necessity.py")
    candidates.extend(
        [
            pathlib.Path("repos/MaiBot/src/maisaka/reply_necessity.py"),
            pathlib.Path("../MaiBot/src/maisaka/reply_necessity.py"),
            pathlib.Path.home() / "repos" / "MaiBot" / "src" / "maisaka" / "reply_necessity.py",
            # 作者本机的检出：仅作兜底（跨机器请用 LF_MAIBOT_SRC 或上面的相对路径）
            pathlib.Path(r"C:\path\to\WorkBuddy\2026-09-29-10-10-17\repos\MaiBot"
                         r"\src\maisaka\reply_necessity.py"),
        ]
    )
    return tuple(candidates)


HOST_SOURCE_CANDIDATES = _host_source_candidates()


def _load_host_module():
    for candidate in HOST_SOURCE_CANDIDATES:
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("host_reply_necessity", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


# ---------------------------------------------------------------- 模式归一化


def test_normalize_mode_falls_back_to_host_default():
    assert lm.normalize_mode("frequency") == "frequency"
    assert lm.normalize_mode("reply_necessity") == "reply_necessity"
    assert lm.normalize_mode(" Reply_Necessity ") == "reply_necessity"
    assert lm.normalize_mode("") == lm.DEFAULT_MODE == "frequency"
    assert lm.normalize_mode(None) == "frequency"
    assert lm.normalize_mode("bogus") == "frequency"


# ---------------------------------------------------------------- 触发阈值


@pytest.mark.parametrize(
    "frequency,expected",
    [(1.0, 1), (1.7, 1), (0.96, 2), (0.81, 2), (0.68, 3), (0.60, 3), (0.45, 5), (0.28, 13)],
)
def test_necessity_threshold(frequency, expected):
    assert lm.trigger_threshold(lm.MODE_REPLY_NECESSITY, frequency) == expected


@pytest.mark.parametrize(
    "frequency,expected",
    [(1.0, 1), (1.7, 1), (0.96, 2), (0.81, 2), (0.68, 2), (0.60, 2), (0.45, 3), (0.28, 4)],
)
def test_frequency_threshold(frequency, expected):
    assert lm.trigger_threshold(lm.MODE_FREQUENCY, frequency) == expected


def test_threshold_is_zero_when_silent():
    for mode in lm.ALL_MODES:
        assert lm.trigger_threshold(mode, 0.0) == 0
        assert lm.trigger_threshold(mode, -1.0) == 0
        assert lm.is_silent(0.0) is True


def test_frequency_is_clamped_to_one_for_threshold():
    # runtime.py:1131 用 min(1.0, freq)，所以 adjust > 1 不会把阈值压到 0
    assert lm.trigger_threshold(lm.MODE_FREQUENCY, 9.0) == 1
    assert lm.trigger_threshold(lm.MODE_REPLY_NECESSITY, 9.0) == 1


# ---------------------------------------------------------------- 评分


def test_necessity_factor_range():
    assert lm.necessity_factor(0.0) == pytest.approx(0.5)
    assert lm.necessity_factor(0.5) == pytest.approx(0.75)
    assert lm.necessity_factor(1.0) == pytest.approx(1.0)
    assert lm.necessity_factor(5.0) == pytest.approx(1.0)  # min(1, freq) 封顶


def test_pressure_score_landmarks():
    assert lm.pressure_score(pending_count=0, threshold=1) == 0
    assert lm.pressure_score(pending_count=1, threshold=1) == 50
    assert lm.pressure_score(pending_count=3, threshold=1) == 84
    assert lm.pressure_score(pending_count=5, threshold=1) == 100
    assert lm.pressure_score(pending_count=500, threshold=1) == 100  # 上限
    # 阈值内是平方增长，且闲置加成只在阈值内出现
    # ratio=0.5 → round(50×0.25)=round(12.5)=12（银行家舍入），再加 15 → 27，仍被 50 封顶
    assert lm.pressure_score(pending_count=1, threshold=2, idle_reached_average=False) == 12
    assert lm.pressure_score(pending_count=1, threshold=2, idle_reached_average=True) == 27


def test_necessity_score_uses_half_factor_at_zero_frequency():
    # 频率为 0 时因子仍是 0.5（不是 0）——评分不会被砍到零。
    # 用 pending=0 把压力分彻底摘掉，只验证「raw × 0.5」这一步。
    score = lm.necessity_score(
        effective_frequency=0.0, relevance=100, content=0, pending_count=0, threshold=1
    )
    assert score == 50

    # 加上压力分后是 (100 + 50) × 0.5 = 75，说明因子作用在总分而不是某一项上
    with_pressure = lm.necessity_score(
        effective_frequency=0.0, relevance=100, content=0, pending_count=1, threshold=1
    )
    assert with_pressure == 75


@pytest.mark.parametrize(
    "keyword,expected_relevance",
    [
        ({"has_at": True}, lm.RELEVANCE_AT),
        ({"has_mention": True}, lm.RELEVANCE_MENTION),
        ({"is_private": True}, lm.RELEVANCE_INDIRECT),
        ({"focus_active": True}, lm.RELEVANCE_INDIRECT),
        ({}, lm.RELEVANCE_PLAIN_GROUP),
    ],
)
def test_relevance_priority(keyword, expected_relevance):
    assert lm.relevance_for(**keyword) == expected_relevance


# ---------------------------------------------------------------- 纯闲聊需要几条


@pytest.mark.parametrize(
    "frequency,expected",
    [(1.0, 3), (0.96, 5), (0.81, 6), (0.68, 11), (0.60, 13)],
)
def test_plain_chatter_needed_in_necessity_mode(frequency, expected):
    """v1.1.1 修正：内容分随批次长度增长，所以条数比 v1.1.0 少（0.60：15→13）。"""

    assert lm.plain_chatter_messages_needed(lm.MODE_REPLY_NECESSITY, frequency) == expected


@pytest.mark.parametrize("frequency,expected", [(0.45, 21), (0.40, 34)])
def test_plain_chatter_needs_many_messages_near_the_floor(frequency, expected):
    """低于 0.6 并不等于「推不动」——这正是 v1.1.0 印错的结论。"""

    assert lm.plain_chatter_messages_needed(lm.MODE_REPLY_NECESSITY, frequency) == expected


@pytest.mark.parametrize("frequency", [0.35, 0.28, 0.15])
def test_plain_chatter_really_unreachable_below_the_real_floor(frequency):
    """真正的不可达下界（按每条 8 字估算）在 0.35~0.40 之间，不是 0.59。"""

    assert lm.plain_chatter_messages_needed(lm.MODE_REPLY_NECESSITY, frequency) is None


def test_needed_count_depends_on_message_length():
    """同一频率下消息越长、越早触发：内容分来自整批拼接长度（宿主 reply_necessity.py:267-273）。"""

    short = lm.plain_chatter_messages_needed(lm.MODE_REPLY_NECESSITY, 0.60)
    long_ = lm.plain_chatter_messages_needed(
        lm.MODE_REPLY_NECESSITY, 0.60, message_length=20
    )
    assert short == 13
    assert long_ == 10
    assert long_ < short


@pytest.mark.parametrize(
    "frequency,expected",
    [(0.60, 13), (0.58, 13), (0.45, 21)],
)
def test_no_059_cliff_anymore(frequency, expected):
    """回归：v1.1.0 声称「freq < 0.59 纯闲聊永远推不动」，那是把内容分当 0 的产物。

    宿主对整批文本给 +5/+10，攒到十几条时必然命中，所以 0.58 只有 13 条。
    """

    assert lm.plain_chatter_messages_needed(lm.MODE_REPLY_NECESSITY, frequency) == expected


def test_plain_chatter_in_frequency_mode_equals_threshold():
    for frequency in (1.0, 0.96, 0.81, 0.68, 0.60, 0.45, 0.28):
        assert lm.plain_chatter_messages_needed(
            lm.MODE_FREQUENCY, frequency
        ) == lm.trigger_threshold(lm.MODE_FREQUENCY, frequency)


# ---------------------------------------------------------------- 计数门与空窗补偿


def test_idle_equivalent_caps_at_threshold_minus_one():
    assert lm.idle_equivalent_count(
        idle_seconds=10_000.0, average_message_interval=60.0, threshold=4
    ) == pytest.approx(3.0)
    # 无平均间隔时不做补偿
    assert lm.idle_equivalent_count(
        idle_seconds=10_000.0, average_message_interval=0.0, threshold=4
    ) == 0.0


def test_count_gate_requires_at_least_one_message():
    # 纯沉默（pending=0）永远不触发，哪怕空窗无限长
    assert lm.triggers_by_count(
        effective_frequency=1.0, pending_count=0, idle_seconds=99999.0,
        average_message_interval=1.0,
    ) is False


def test_count_gate_idle_compensation():
    # threshold=4，pending=1，静默 3 个平均间隔 → 1+3 = 4 → 触发
    assert lm.triggers_by_count(
        effective_frequency=0.25, pending_count=1, idle_seconds=180.0,
        average_message_interval=60.0,
    ) is True
    # 静默不足 → 不触发
    assert lm.triggers_by_count(
        effective_frequency=0.25, pending_count=1, idle_seconds=60.0,
        average_message_interval=60.0,
    ) is False


def test_count_gate_is_silent_when_frequency_zero():
    assert lm.triggers_by_count(
        effective_frequency=0.0, pending_count=99, idle_seconds=99999.0,
        average_message_interval=1.0,
    ) is False


# ---------------------------------------------------------------- 预览


def test_preview_reports_silence():
    out = lm.preview(mode="frequency", talk_value=1.0, adjust=0.0)
    assert out["silent"] is True
    assert out["threshold"] == 0
    assert out["plain_chatter_messages_needed"] is None
    assert "静默" in out["verdict"]


def test_preview_reports_both_modes_differently():
    necessity = lm.preview(mode="reply_necessity", talk_value=1.0, adjust=0.55)
    frequency = lm.preview(mode="frequency", talk_value=1.0, adjust=0.55)
    assert necessity["plain_chatter_messages_needed"] == 14
    assert "每条约 8 字" in necessity["verdict"], "结论必须写明建模假设，否则数字无从解释"
    assert frequency["plain_chatter_messages_needed"] == 2
    assert necessity["threshold"] != frequency["threshold"]


def test_preview_multiplies_talk_value_and_adjust():
    out = lm.preview(mode="frequency", talk_value=0.4, adjust=0.5)
    assert out["effective_frequency"] == pytest.approx(0.2)
    assert out["threshold"] == lm.trigger_threshold("frequency", 0.2)


# ---------------------------------------------------------------- 真值校验


def test_matches_real_host_source_when_available():
    """把公式与本机 MaiBot 源码逐点对比（找不到源码则跳过）。"""

    host = _load_host_module()
    if host is None:
        pytest.skip("未找到本机 MaiBot 源码，跳过真值校验（不等于通过）")

    for threshold in (1, 2, 3, 5, 13):
        for pending in range(0, 200):
            for idle in (False, True):
                assert lm.pressure_score(
                    pending_count=pending, threshold=threshold, idle_reached_average=idle
                ) == host._calculate_pressure_score(
                    pending_count=pending,
                    normalized_threshold=threshold,
                    idle_reached_average=idle,
                )

    for frequency in (0.28, 0.45, 0.6, 0.81, 0.96, 1.0, 1.4):
        threshold = lm.trigger_threshold(lm.MODE_REPLY_NECESSITY, frequency)
        for relevance in (
            lm.RELEVANCE_PLAIN_GROUP,
            lm.RELEVANCE_INDIRECT,
            lm.RELEVANCE_AT,
        ):
            for content in (0, 15, 35, 65, -25):
                for pending in (1, 2, 5, 15, 40):
                    # 压力分取自**宿主**实现，因子用明文公式重算，避免自证
                    pressure = host._calculate_pressure_score(
                        pending_count=pending,
                        normalized_threshold=max(1, threshold),
                        idle_reached_average=False,
                    )
                    factor = 0.5 + 0.5 * min(1.0, frequency)
                    want = max(0, int(round((relevance + content + pressure) * factor)))
                    got = lm.necessity_score(
                        effective_frequency=frequency,
                        relevance=relevance,
                        content=content,
                        pending_count=pending,
                        threshold=threshold,
                    )
                    assert got == want


@pytest.mark.parametrize(
    "count,length",
    [(1, 8), (4, 8), (5, 8), (10, 8), (14, 8), (15, 8), (20, 4), (30, 4), (3, 20), (6, 20)],
)
def test_plain_chatter_content_matches_real_host_source_when_available(count, length):
    """真值校验：``plain_chatter_content`` 必须等于宿主 ``_score_content`` 的长度分。

    刻意选不含问题/请求/征询标记的文本（``"字"*n``），这样宿主的内容分**只剩**
    「长文本 +5 / 较长文本 +10」两项，可以与插件模型逐点对比。
    """

    host = _load_host_module()
    if host is None:
        pytest.skip("未找到本机 MaiBot 源码，跳过真值校验（不等于通过）")

    texts = ["字" * length] * count
    combined = "\n".join(texts)
    host_content, _ = host._score_content(texts, combined, is_direct_context=False)
    assert lm.plain_chatter_content(
        message_count=count, message_length=length
    ) == host_content, f"count={count} length={length} total={len(combined)}"


def test_content_ceiling_and_at_leak_match_real_host_source_when_available():
    """用宿主**自己的** ``score_reply_necessity`` 验证两条共存结论。

    共存结论（README / COMPAT.md 里对用户承诺的两条）都建立在「内容分上限 65」上，
    所以这里不自己算，直接把候选文本喂给宿主实现，取它给出的最高分：

    - ``CONTENT_MAX_SCORE`` 是内容分上限；
    - ``adjust`` 只要不是精确的 0，``@`` 在评分门下就能穿透（≥80），
      而提及 / 私聊 / 普通闲聊不能 —— 这就是静默下限会漏话的原因。
    """

    host = _load_host_module()
    if host is None:
        pytest.skip("未找到本机 MaiBot 源码，跳过真值校验（不等于通过）")

    def host_score(
        *,
        texts,
        has_at=False,
        has_mention=False,
        is_group_chat=True,
        frequency=1e-9,
        threshold=10**12,
    ):
        return host.score_reply_necessity(
            host.ReplyNecessityInput(
                texts=list(texts),
                pending_count=len(texts),
                trigger_threshold=threshold,
                has_at=has_at,
                has_mention=has_mention,
                is_group_chat=is_group_chat,
                focus_active=False,
                recent_self_replies=0,
                recent_window_messages=0,
                effective_frequency=frequency,
                idle_seconds=0.0,
                idle_reached_average=False,
            )
        ).score

    # 撑满内容分：问题 + 请求 + 征询 + 长文本 + 较长文本（同一段文本可以同时命中）
    stuffed = "@麦麦 你能帮我看看这段代码为什么报错吗，你怎么看？" + "很长的补充说明。" * 20
    content_score, reasons = host._score_content([stuffed], stuffed, is_direct_context=True)
    assert content_score == lm.CONTENT_MAX_SCORE, (
        f"宿主实际内容分上限是 {content_score}（{reasons}），模型里写的是 {lm.CONTENT_MAX_SCORE}"
    )

    # 穿透：任何 >0 的倍率都让 @ 过关
    assert host_score(texts=[stuffed], has_at=True) >= lm.NECESSITY_TRIGGER_SCORE
    # 不穿透：提及 / 私聊 / 普通闲聊
    assert host_score(texts=[stuffed], has_mention=True) < lm.NECESSITY_TRIGGER_SCORE
    assert host_score(texts=[stuffed], is_group_chat=False) < lm.NECESSITY_TRIGGER_SCORE
    assert host_score(texts=[stuffed]) < lm.NECESSITY_TRIGGER_SCORE
