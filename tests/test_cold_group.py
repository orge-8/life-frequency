# -*- coding: utf-8 -*-
"""L3：冷群搭话（v1.22.2）——判定、话头形态、按会话隔离、群聊退避。

这个功能最典型的失败形态不是「不开口」，而是**她自己把死群刷成噪音**：冷群里没人
接话，她还每天准时抛一条。所以这里钉四件事：

1. **判定可穷举**：开关 / 群聊 / 冷度三段条件全部落在 ``life_topic.cold_group_material``，
   任何一段不满足都必须返回空；
2. **话头必须能被回答**：以问号或「吗/呢/吧/呀/啊」收尾——陈述句在冷群里等于自言自语；
3. **按会话隔离**：素材带 ``target_session``，不进 ``state.materials``，不会漂到别的群；
4. **群聊也退避**：连续没人接时开口间隔按 2^连击 拉长，有人回话立即归零
   （``unanswered_backoff_group=False`` 回到 v1.22.1 的仅私聊行为）。
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_proactive as P  # noqa: E402
import life_topic as T  # noqa: E402

NOW = 1_700_000_000.0
HOUR = 3600.0


# ---------------------------------------------------------------- 冷度判定


def test_is_cold_boundary_and_no_baseline():
    """刚好 ``cold_hours`` 算冷；**没有基线（0）不算冷**——那是「不知道」不是「很安静」。"""

    assert T.is_cold(NOW - 2 * HOUR, NOW, cold_hours=2.0) is True
    assert T.is_cold(NOW - 2 * HOUR + 1, NOW, cold_hours=2.0) is False
    assert T.is_cold(0.0, NOW, cold_hours=2.0) is False, "从没见过人说话时不该主动搭话"
    assert T.is_cold(NOW - 99 * HOUR, NOW, cold_hours=0.0) is False, "0 = 关闭"


def test_cold_group_material_requires_all_three_conditions():
    """开关 / 群聊 / 冷度三段条件，缺一条都必须是空（否则某个分支就能绕过判定）。"""

    hot = NOW - 5 * 60
    cold = NOW - 5 * HOUR
    assert T.cold_group_material(
        "g1", "2026-02-08", now=NOW, now_minutes=600,
        enabled=True, is_group=True, last_user_at=cold, cold_hours=2.0,
    )
    assert not T.cold_group_material(
        "g1", "2026-02-08", now=NOW, now_minutes=600,
        enabled=False, is_group=True, last_user_at=cold, cold_hours=2.0,
    )
    assert not T.cold_group_material(
        "g1", "2026-02-08", now=NOW, now_minutes=600,
        enabled=True, is_group=False, last_user_at=cold, cold_hours=2.0,
    )
    assert not T.cold_group_material(
        "g1", "2026-02-08", now=NOW, now_minutes=600,
        enabled=True, is_group=True, last_user_at=hot, cold_hours=2.0,
    )


# ---------------------------------------------------------------- 话头形态


def test_every_topic_is_answerable_question():
    """话头池每一条都必须能被回答——以后加句子时这条会挡住「写了句陈述句」。"""

    for bucket, pool in T.TOPIC_POOL.items():
        assert pool, f"{bucket} 桶不能为空"
        for text in pool:
            assert T.is_answerable_question(text), f"{bucket}: {text!r} 不是问句"


def test_topic_pick_is_deterministic_and_session_scoped():
    """同一天同一个会话恒得同一条（每 tick 重算也不会换话题再问一遍），不同群尽量不同。"""

    a1 = T.pick_topic("group-1", "2026-02-08", 600)
    a2 = T.pick_topic("group-1", "2026-02-08", 601)
    assert a1 == a2
    picks = {T.pick_topic(f"group-{i}", "2026-02-08", 600) for i in range(12)}
    assert len(picks) > 1, "十几个群不该全都同一句话"


def test_topic_material_carries_intro_and_session_target():
    """素材必须带 ``intro``（换成「想找个话头」）与 ``target_session``（隔离依据）。"""

    material = T.cold_topic_material("group-7", "2026-02-08", now=NOW, now_minutes=600)
    assert material["label"] == T.COLD_TOPIC_LABEL
    assert material["intro"] == T.COLD_TOPIC_INTRO
    assert "活跃气氛" in material["intro"], "必须写明『别说自己在活跃气氛』"
    assert material["target_session"] == "group-7"
    assert material["weight"] == pytest.approx(T.COLD_TOPIC_WEIGHT)
    assert material["expires_at"] > NOW


def test_materials_for_session_never_leaks_across_groups():
    materials = [
        {"text": "常规素材（无定向）"},
        {"text": "A 群的话头", "target_session": "group-a"},
        {"text": "B 群的话头", "target_session": "group-b"},
    ]
    kept = T.materials_for_session(materials, session_id="group-b")
    assert [item["text"] for item in kept] == ["常规素材（无定向）", "B 群的话头"]


def test_intent_uses_topic_intro_not_the_recent_event_phrase():
    """冷群话头走群聊口径：引导语是「找个话头」，不是「聊聊刚发生的一件事」。"""

    material = T.cold_topic_material("group-1", "2026-02-08", now=NOW, now_minutes=600)
    intent = P.build_intent(material, private_chat=False)
    # ⚠ 逐字比对做不到：``build_intent`` 会把 ``intro`` 过一遍 ``sanitize_text``，
    # 「」这类结构字符被中和（防提示词注入）。所以断言的是**语义片段**。
    assert "找个话头" in intent
    assert "活跃气氛" in intent, "必须保留『别说自己在活跃气氛』这条约束"
    assert P.PRIVATE_SCENE not in intent
    assert P.GROUP_SCENE not in intent, "有 intro 时不该再叠一层默认引导语"
    assert "留一个别人能接的落子" in intent


# ---------------------------------------------------------------- 群聊退避


def _config(**overrides):
    base = dict(enabled=True)
    base.update(overrides)
    return P.ProactiveConfig(**base)


def _material():
    return {
        "label": "小事",
        "text": "刚发生的一件小事",
        "weight": 0.9,
        "created_at": NOW,
        "expires_at": NOW + 6 * HOUR,
    }


def _decide(*, private_chat: bool, session: dict, config=None):
    return P.decide(
        config=config or _config(),
        now=NOW,
        now_minutes=14 * 60,
        activity="daily",
        energy=8.0,
        emotion=6.0,
        materials=[_material()],
        session=session,
        day_key="2026-02-08",
        private_chat=private_chat,
    )


def test_group_backoff_kicks_in_when_nobody_answered():
    """连续未回应的群：间隔按 2^连击 拉长（默认 180 分钟 × 倍数）。"""

    session = {
        "last_proactive_at": NOW - 240 * 60,   # 4 小时前她说过一次
        "last_user_message_at": NOW - 300 * 60,  # 之后一直没人接
        "unanswered_streak": 1,
        "day_key": "2026-02-08",
        "count": 0,
    }
    blocked = _decide(private_chat=False, session=session)
    assert not blocked.should_send
    assert blocked.reason == P.REASON_INTERVAL
    assert blocked.backoff == pytest.approx(4.0), "连击 1 → +1 → ×2^2 = 4"
    assert "未回应退避" in blocked.detail


def test_group_backoff_can_be_disabled_back_to_private_only():
    """``unanswered_backoff_group=False`` 精确回到 v1.22.1：群聊不退避。"""

    session = {
        "last_proactive_at": NOW - 240 * 60,
        "last_user_message_at": NOW - 300 * 60,
        "unanswered_streak": 1,
        "day_key": "2026-02-08",
        "count": 0,
    }
    allowed = _decide(
        private_chat=False, session=session,
        config=_config(unanswered_backoff_group=False),
    )
    assert allowed.should_send, "关掉群聊退避后，4 小时 > 最小间隔 3 小时，应当能开口"


def test_reply_resets_the_backoff_immediately():
    """有人回话 ⇒ ``last_user`` 超过 ``last_proactive`` ⇒ 连击立即归零。"""

    session = {
        "last_proactive_at": NOW - 240 * 60,
        "last_user_message_at": NOW - 60,   # 一分钟前有人说话
        "unanswered_streak": 3,
        "day_key": "2026-02-08",
        "count": 0,
    }
    decision = _decide(private_chat=False, session=session)
    # 「对方刚说过话」这道既有闸会先拦下它——这里断言的是**没有**落到退避分支
    assert decision.reason != P.REASON_INTERVAL or "未回应退避" not in decision.detail


def test_unanswered_cap_also_covers_groups_when_enabled():
    """硬上限与退避同一适用面：cap=2、真实连击已到 2 ⇒ 第三次彻底停口。"""

    # 间隔必须**先**满足（连击 2 ⇒ 退避 ×8 = 1440 分钟），否则会先被 interval 分支拦下，
    # 那样这条用例就永远测不到 cap——第一版就是这么写错的（last_proactive 只有 999 分钟前）。
    session = {
        "last_proactive_at": NOW - 4000 * 60,
        "last_user_message_at": NOW - 4100 * 60,
        "unanswered_streak": 2,
        "day_key": "2026-02-08",
        "count": 0,
    }
    decision = _decide(private_chat=False, session=session, config=_config(max_unanswered_times=2))
    assert not decision.should_send
    assert decision.reason == P.REASON_UNANSWERED_CAP


# ---------------------------------------------------------------- 独占选材（v1.22.3）


def _plain(weight: float, text: str = "她自己的生活琐事") -> dict:
    return {
        "label": "事件",
        "text": text,
        "weight": weight,
        "created_at": NOW,
        "expires_at": NOW + 6 * HOUR,
    }


def test_exclusive_material_wins_over_a_heavier_ordinary_one():
    """独占越过权重比较：0.55 的话头必须压过 0.85 的事件素材。

    不这样做的话「冷了就抛一条」会退化成「几乎永远不抛」——事件素材 TTL 6 小时、
    每 tick 40% 抽取，池子里几乎总有更重的条目压着。
    """

    topic = T.cold_topic_material("group-1", "2026-02-08", now=NOW, now_minutes=600)
    picked = P.select_material([_plain(0.85), topic, _plain(0.7)], NOW)
    assert picked is not None
    assert picked["label"] == T.COLD_TOPIC_LABEL
    assert picked["effective_weight"] == pytest.approx(T.COLD_TOPIC_WEIGHT)


def test_without_exclusive_the_max_weight_rule_is_unchanged():
    """没有独占素材时，行为与 v1.22.2 逐位一致（普通素材仍取最高）。"""

    picked = P.select_material([_plain(0.5, "轻的"), _plain(0.8, "重的")], NOW)
    assert picked is not None and picked["text"] == "重的"


def test_exclusive_still_respects_the_other_gates():
    """独占只改选材：静默时段、每日上限、最小间隔、评分阈值一律照旧。"""

    topic = T.cold_topic_material("group-1", "2026-02-08", now=NOW, now_minutes=600)
    # 基准会话：她 4 小时前说过一次，之后**对方接过话**（last_user 晚于 last_proactive）
    # ⇒ 未回应退避不成立。不这么设的话，第 ④ 条会被 ×2 退避拦在 interval 上，
    # 用例就永远测不到「所有闸都过时能开口」。
    session = {"last_proactive_at": NOW - 240 * 60, "last_user_message_at": NOW - 200 * 60,
               "unanswered_streak": 0, "day_key": "2026-02-08", "count": 0}

    # ① 每日上限：今天已经开口过一次 ⇒ 冷群话头也发不出去
    capped = P.decide(
        config=_config(daily_max=1), now=NOW, now_minutes=14 * 60,
        activity="daily", energy=8.0, emotion=6.0,
        materials=[topic], session={**session, "count": 1},
        day_key="2026-02-08", private_chat=False,
    )
    assert capped.reason == P.REASON_DAILY_MAX

    # ② 静默时段（14:00 落在 13:00–15:00 里）
    quiet = P.decide(
        config=_config(quiet_hours=((13 * 60, 15 * 60),)), now=NOW, now_minutes=14 * 60,
        activity="daily", energy=8.0, emotion=6.0,
        materials=[topic], session=session, day_key="2026-02-08", private_chat=False,
    )
    assert quiet.reason == P.REASON_QUIET

    # ③ 最小间隔：3 小时前刚说过，现在不能再抛
    too_soon = P.decide(
        config=_config(), now=NOW, now_minutes=14 * 60,
        activity="daily", energy=8.0, emotion=6.0,
        materials=[topic],
        session={**session, "last_proactive_at": NOW - 60 * 60},
        day_key="2026-02-08", private_chat=False,
    )
    assert too_soon.reason == P.REASON_INTERVAL

    # ④ 全部闸都过时，它才真的能开口
    ok = P.decide(
        config=_config(), now=NOW, now_minutes=14 * 60,
        activity="daily", energy=8.0, emotion=6.0,
        materials=[topic], session=session, day_key="2026-02-08", private_chat=False,
    )
    assert ok.should_send
    assert "留一个别人能接的落子" in ok.intent
