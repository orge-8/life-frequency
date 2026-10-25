# -*- coding: utf-8 -*-
"""人设化冷场话头（v1.23.0）：缓存键、解析、池覆盖、状态净化。

这一层的失败形态都很安静：模型回一句陈述句 → 冷群里她自言自语；缓存键漏了人设
→ 改了人设还用旧话头；解析不做编号剥离 → 她发「1. 最近在追什么番」。
所以每条都要有断言，而不是靠真机看。
"""

import life_persona as P
import life_sim
import life_topic as T


def test_cache_key_depends_on_persona_day_and_slot():
    base = P.cache_key("一个嘴硬心软的少女", "2026-10-23", 600)
    assert base
    assert base != P.cache_key("另一个人设", "2026-10-23", 600), "改人设必须换键"
    assert base != P.cache_key("一个嘴硬心软的少女", "2026-10-24", 600)
    assert base == P.cache_key("一个嘴硬心软的少女", "2026-10-23", 700), "同时段段内应复用"
    assert P.cache_key("", "2026-10-23", 600) == "", "没人设就不生成"


def test_slot_label_covers_a_day():
    labels = {P.slot_label(minutes) for minutes in range(0, 24 * 60, 180)}
    assert "凌晨" in labels and "上午" in labels and "夜里" in labels
    assert P.slot_label(-5) == P.slot_label(0), "坏值按 0 处理，不抛"


def test_build_prompt_states_constraints():
    prompt = P.build_prompt("嘴硬心软的少女", now_minutes=1500, count=3)
    assert "嘴硬心软的少女" in prompt
    assert "深夜" in prompt, "时段要进提示词，否则话题会跟此刻脱节"
    assert "必须" in prompt and "问句" in prompt
    assert "不要编号" in prompt or "不要" in prompt


def test_parse_topics_filters_out_statements_and_numbering():
    raw = """1. 最近在追什么番吗
- 今天降温了
2) 你那边下雨了吗？
“晚饭吃了吗”
这是一句解释：以上是候选。
最近在追什么番吗
"""
    topics = P.parse_topics(raw, count=4)
    assert "最近在追什么番吗" in topics
    assert "你那边下雨了吗？" in topics
    assert "晚饭吃了吗" in topics, "引号要剥掉"
    assert "今天降温了" not in topics, "陈述句在冷群接不住"
    assert "这是一句解释：以上是候选。" not in topics
    assert topics.count("最近在追什么番吗") == 1, "去重"


def test_parse_topics_respects_count_and_rejects_garbage():
    assert len(P.parse_topics("\n".join(["在吗？"] * 10), count=2)) == 1
    assert P.parse_topics("", count=3) == []
    assert P.parse_topics(None, count=3) == []
    long_line = "这" * 60 + "吗"
    assert P.parse_topics(long_line, count=3) == [], "超长句不要（会像播报）"


def test_prune_cache_caps_entries_and_drops_empty():
    cache = {f"k{i}": ["在吗？"] for i in range(12)}
    cache["bad"] = []
    pruned = P.prune_cache(cache, keep=8)
    assert len(pruned) == 8 and "bad" not in pruned
    assert P.prune_cache("nope") == {}


def test_pick_topic_uses_persona_pool_and_falls_back():
    pool = ["你昨晚干啥了？", "周末打算去哪玩吗"]
    picked = T.pick_topic("g1", "2026-10-23", 600, pool=pool)
    assert picked in pool
    # 池子里只有陈述句 ⇒ 退回静态池（否则她会发一句没人接的话）
    fallback = T.pick_topic("g1", "2026-10-23", 600, pool=["今天好冷"])
    assert fallback in T.topic_pool(600)


def test_state_sanitizes_persona_topic_cache():
    raw = {
        "persona_topics": {
            "good": ["在吗？"],
            "empty": [],
            "": ["在吗？"],
            "bad": "不是列表",
            "n1": ["1"], "n2": ["2"], "n3": ["3"], "n4": ["4"], "n5": ["5"], "n6": ["6"],
        }
    }
    state = life_sim.LifeState.from_dict(raw)
    assert "good" in state.persona_topics
    assert set(state.persona_topics) >= {"good"}
    assert "" not in state.persona_topics
    assert "bad" not in state.persona_topics and "empty" not in state.persona_topics
    assert len(state.persona_topics) <= 8, "缓存要有上限，否则状态文件随天增长"
