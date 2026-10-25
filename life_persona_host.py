# -*- coding: utf-8 -*-
"""人设化话头的宿主接线（v1.23.0）：生成、缓存、给冷群素材用（mixin）。

为什么独立成一个 mixin 而不是塞进 ``life_catchup_host.py``：两者不是一件事
（补答管「睡着时的消息」，这里管「冷场时抛什么话题」），只是**同一次版本**进来的。
混在一个类里以后想单独关掉其中一个就得读两份无关代码。

``plugin.py`` 侧只留三行：继承、``_init_persona_topics()``、tick 里一次 ``await``。
"""

from __future__ import annotations

try:  # 包式加载（Runner 真机）
    from .life_persona import (
        DEFAULT_TOPIC_COUNT,
        build_prompt,
        cache_key,
        parse_topics,
        prune_cache,
        topics_for,
    )
    from .life_sim import day_key_of, local_datetime
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_persona import (  # type: ignore[no-redef]
        DEFAULT_TOPIC_COUNT,
        build_prompt,
        cache_key,
        parse_topics,
        prune_cache,
        topics_for,
    )
    from life_sim import day_key_of, local_datetime  # type: ignore[no-redef]


class PersonaTopicMixin:
    """冷场话头的人设化（**只在冷群搭话开启且读得到人设时**才动）。"""

    def _init_persona_topics(self) -> None:
        self._persona_topics_at: float = 0.0
        self._persona_topics_fail_streak: int = 0

    def _persona_topics_ready(self) -> bool:
        """值不值得生成：主动开口 + 冷群搭话都开着，且人设够长。"""

        proactive = self.config.proactive
        if not (bool(proactive.enabled) and bool(proactive.cold_group_enabled)):
            return False
        return len(str(self._persona or "").strip()) >= 8

    def _persona_topic_pool(self, day_key: str, now_minutes: int) -> tuple[str, ...]:
        """给 ``life_topic`` 用的话头池；空元组 = 用静态池。

        **同步**读缓存：调用点在 ``_maybe_proactive`` 的选材路径上，不能在那里等模型。
        """

        if not self._persona_topics_ready():
            return ()
        topics = topics_for(self._state.persona_topics, self._persona, day_key, now_minutes)
        return tuple(topics)

    async def _refresh_persona_topics(self, now: float) -> None:
        """tick 里调用：缺这一段的话头就生成一次（同段只生成一次）。

        四条纪律：

        1. **失败不重试到底**：连续失败 3 次后本次加载不再尝试（否则模型不可用时会
           每个 tick 打一次调用，日志与费用都难看）。
        2. **人设为空就不生成**：没读到时 ``persona`` 是空串，生成出来的一定跑偏。
        3. **结果只信问句**：``parse_topics`` 会把陈述句/编号/解释行全部滤掉。
        4. **缓存有上限**：``prune_cache`` 只留最近 8 组，避免状态文件随天增长。
        """

        if not self._persona_topics_ready():
            return
        if self._persona_topics_fail_streak >= 3:
            return
        sim_config = self._sim_config()
        local_now = local_datetime(now, sim_config.tz_offset_minutes)
        now_minutes = local_now.hour * 60 + local_now.minute
        day_key = day_key_of(local_now, sim_config.day_boundary_hour)
        key = cache_key(self._persona, day_key, now_minutes)
        if not key or topics_for(self._state.persona_topics, self._persona, day_key, now_minutes):
            return
        # 同一个 tick 内不重复尝试（``_persona_topics_at`` 是「上次尝试」而不是「上次成功」）
        if float(now) - float(self._persona_topics_at or 0.0) < 600.0:
            return
        self._persona_topics_at = float(now)

        # ⚠ 不直接摸 ``self.ctx``：``life_*.py`` 被约定为纯模块（同 ``life_catchup_host``），
        # 真正的模型调用由宿主类的 ``_llm_text`` 代劳。
        try:
            raw = await self._llm_text(
                build_prompt(self._persona, now_minutes=now_minutes),
                temperature=0.9,
                max_tokens=120,
                timeout_ms=12_000,
            )
        except Exception as exc:  # noqa: BLE001 —— 冷场话头生成失败只该退回静态池
            self._persona_topics_fail_streak += 1
            self._module_log("debug", "人设话头生成失败（第 %s 次）：%s",
                             self._persona_topics_fail_streak, exc)
            return
        topics = parse_topics(raw, count=DEFAULT_TOPIC_COUNT)
        if not topics:
            self._persona_topics_fail_streak += 1
            return
        self._persona_topics_fail_streak = 0
        cache = prune_cache({**(self._state.persona_topics or {}), key: topics})
        self._state.persona_topics = cache
        self._state_dirty = True
        self._module_log("info", "人设话头已生成（%d 条，时段 %02d:%02d）：%s",
                         len(topics), now_minutes // 60, now_minutes % 60, "／".join(topics))


__all__ = ["PersonaTopicMixin"]
