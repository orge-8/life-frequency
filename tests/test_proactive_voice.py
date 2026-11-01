# -*- coding: utf-8 -*-
"""L3：主动开口的**群聊/私聊口径**（v1.22.1）。

背景是真机两条实录（2026-10-18，同一个群）：

- 「诶，跟你说个事儿——刚有个八百年没联系的人突然给我发消息，我盯着那条提示看了半天，
  都没敢点开。」
- 「刚下楼碰到一只猫，居然肯让我摸！好亲人啊它」

两句都不是「措辞不好听」，而是**在群里汇报了一件事然后结束**：旁人没有插入点（无落子），
而且信息量约等于零（陈述事实，没有态度）。拆开看是三个可确定性守住的东西：

1. 群聊与私聊用**两套引导语**——私聊里「想聊聊刚发生的一件事」成立（对面就是那个人），
   群聊里没有对象，说成「讲故事」必然变独白；
2. 群聊**一句话、限字数**——治三句叙事的独白体；
3. 群聊**结尾必须留落子**、且**先说由头、重心在态度**——治「内容无信息量」+「句号式封闭」。
   ⚠ v1.25.4 撤掉了「不要汇报事实」：它把事件本身也砍了，句子悬空反而更难接；
4. 两档都禁止**编造具体次数/时长与他人原话**——「八百年」「看了半天」都是模型加的戏，
   她的世界观里没有可依的历史。

这里只钉「意图文本长什么样」（结构性、可自动跑）。**文本好不好**没法自动判——所以
上面两条真机原句留在本文件里当基准：下次改措辞，人工拿这两句对照一眼再决定。
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_proactive as P  # noqa: E402

NOW = 1000.0


def _material(text: str = "楼下有只猫，居然肯让我摸", label: str = "遇到亲人的猫"):
    return {
        "label": label,
        "text": text,
        "weight": 0.65,
        "created_at": NOW,
        "expires_at": NOW + 6 * 3600,
    }


def _config(**overrides):
    base = dict(enabled=True)
    base.update(overrides)
    return P.ProactiveConfig(**base)


def _decide(*, private_chat: bool, material=None):
    return P.decide(
        config=_config(),
        now=NOW,
        now_minutes=14 * 60,
        activity="daily",
        energy=8.0,
        emotion=6.0,
        materials=[material or _material()],
        session={},
        day_key="2026-02-08",
        private_chat=private_chat,
    )


# ---------------------------------------------------------------- 两套口径


def test_group_intent_is_not_a_story_and_asks_for_a_hook():
    """群聊档：不许「想聊聊…的一件事」（那是讲故事），必须要求落子与态度。

    v1.25.4 改口径：**由头必须留**，被禁的是「只叙述事实」。旧措辞「不要汇报事实」在真机
    2026-11-01 把事件本身一起砍掉了（素材「今天居然有人跟我问路」→ 只发出「这业务能力，
    导航都得喊你师父了！」），所以这条断言从「要求禁止事实」翻转成「要求点明由头」。
    """

    intent = P.build_intent(_material(), private_chat=False)
    assert P.GROUP_SCENE in intent
    assert P.PRIVATE_SCENE not in intent, "群聊不该沿用私聊的叙事引导语"
    assert "留一个别人能接的落子" in intent
    assert "点明由头" in intent, "锚点丢了句子就悬空，比「只汇报事实」更难接"
    assert "不要汇报事实" not in intent, "这条会把事件一起砍掉（v1.25.4 已撤）"
    # 发言纪律照旧（不挂引用）——这一次改动不动那条
    assert P.NO_QUOTE_DISCIPLINE in intent


def test_private_intent_keeps_old_wording_but_forbids_fabricated_details():
    """私聊档：措辞逐字不变（老实例升级后行为可解释），只**加**一条禁令。"""

    intent = P.build_intent(_material(), private_chat=True)
    assert P.PRIVATE_SCENE in intent
    assert "不要编造具体次数与时长" in intent
    assert "留一个别人能接的落子" not in intent, "私聊对面就在说话，不需要钩子"
    assert P.NO_QUOTE_DISCIPLINE in intent


def test_default_argument_is_private_for_backward_compatibility():
    """老调用方（不传参）必须拿到与 v1.22.0 完全相同的引导语。"""

    material = _material()
    assert P.build_intent(material) == P.build_intent(material, private_chat=True)


def test_group_length_budget_is_a_real_number_in_the_prompt():
    """字数取自常量，且常量本身在合理区间——防「文案写 30、常量是 80」这种漂移。"""

    assert 15 <= P.GROUP_INTENT_MAX_CHARS <= 40
    assert str(P.GROUP_INTENT_MAX_CHARS) in P.build_intent(_material(), private_chat=False)


def test_length_budget_can_hold_anchor_attitude_and_hook():
    """v1.25.4：上限必须装得下**三个**要件，否则模型只能三选二。

    证据是真机那次回复器的内部推理：它写出 33 字的草稿后自己数了一遍字（`Count: … = 15
    chars`）然后否掉，最终留下的那句只有态度、没有由头。30 字是这条行为的直接成因。
    """

    sample = "今天有人问我路" + "，我指得还挺准" + "——导航该喊我师父了吧"
    assert len(sample) <= P.GROUP_INTENT_MAX_CHARS, (
        f"「由头+态度+落子」实测 {len(sample)} 字，超出 {P.GROUP_INTENT_MAX_CHARS} 字上限"
    )
    assert P.GROUP_INTENT_MAX_CHARS >= 36


def test_output_only_discipline_reaches_both_registers():
    """v1.25.4：形态纪律与场合无关——私聊也不许把思考过程写进正文。

    真机那次正文位置出现过英文 CoT（`The proactive task: …` / `Let's craft: …` /
    `Count: …`）。插件切不开 content 与 reasoning，只能把要求同时写在两档 intent 里。
    """

    assert P.OUTPUT_ONLY_HINT in P.build_intent(_material(), private_chat=True)
    assert P.OUTPUT_ONLY_HINT in P.build_intent(_material(), private_chat=False)
    assert "不要复述以上要求" in P.OUTPUT_ONLY_HINT


def test_material_text_is_passed_through_not_paraphrased_away():
    """素材原文仍要进意图：模型是「改写」而不是「凭空生成」，去掉它等于让人瞎编。"""

    intent = P.build_intent(_material(text="楼下有只猫，居然肯让我摸"), private_chat=False)
    assert "楼下有只猫，居然肯让我摸" in intent


def test_label_falls_back_and_structure_chars_are_stripped():
    intent = P.build_intent({"text": "刚发生"}, private_chat=False)
    assert "生活小事" in intent
    dirty = P.build_intent({"label": "<猫>", "text": "{猫}"}, private_chat=False)
    assert "<" not in dirty and "{" not in dirty


def test_memory_intro_overrides_scene_in_group_too():
    """v1.22.0 的 ``intro``（记忆素材）在群聊档同样优先——旧事不能被打成「刚发生」。"""

    intent = P.build_intent(
        {"label": "记忆:旧事", "text": "还记得张三提过的：考公", "intro": "想起一件旧事"},
        private_chat=False,
    )
    assert "想起一件旧事" in intent
    assert P.GROUP_SCENE not in intent
    assert "留一个别人能接的落子" in intent, "换了引导语，约束仍然要在"


# ---------------------------------------------------------------- 接线


def test_decide_uses_group_register_for_group_sessions():
    """`decide` 的 `private_chat` 必须一路传到 intent——否则改了措辞真机也不变。"""

    group = _decide(private_chat=False)
    assert group.should_send
    assert P.GROUP_SCENE in group.intent

    private = _decide(private_chat=True)
    assert private.should_send
    assert P.PRIVATE_SCENE in private.intent
