# -*- coding: utf-8 -*-
"""睡醒补答的宿主接线层（v1.23.0）：记录 / 醒来触发 / 被动回填（mixin）。

为什么是 mixin 而不是继续写在 ``plugin.py`` 里：这块要接的东西太多——消息钩子、
tick、replyer 注入、频率写入——它不是「胶水」而是第三个通道，混在入口文件里会让
「哪些是宿主接线、哪些是业务」这条界线重新糊掉（v1.23.3 起入口文件不再有行数预算，
但界线本身仍然由 ``check_plugin.py`` 的 ``self.ctx`` 反推守着）。放进独立模块后
``plugin.py`` 只留三行调用点。

与 ``life_catchup`` 的分工：

```text
life_catchup.py        纯逻辑：要不要记 / 补谁 / 什么时候 / 说什么（可离线穷举测试）
life_catchup_host.py   宿主侧：取会话与人物、写 SQLite、排错开队列、调 proactive
```

三条宿主侧纪律：

1. **SQLite 一律 ``asyncio.to_thread``**：记录点在消息主链上，同步磁盘 I/O 会卡住事件循环。
2. **回复侧注入是同步路径**：那里读库会拖慢每条回复，所以记录时顺手留一份内存镜像。
3. **配额按宿主受理计**：不复用 ``[proactive].confirm_send``——那条链失败时会把账记到
   主动开口的每日额度上，一次补答就能吃掉她整天的日常开口。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Mapping

try:  # 包式加载（Runner 真机）
    from .life_activity import is_asleep
    from .life_catchup import (
        SKIP_LABELS as CATCHUP_SKIP_LABELS,
        Candidate as CatchupCandidate,
        CatchupRules,
        build_intent as catchup_intent,
        fill_fact as catchup_fill_fact,
        merge_entries as catchup_merge_entries,
        plan as catchup_plan,
        should_record as catchup_should_record,
    )
    from .life_relations import person_id_for as relation_person_id
    from .life_sim import day_key_of, local_datetime
    from .life_social import flag_value
except ImportError:  # 平铺兜底（脚本直跑 / 测试）
    from life_activity import is_asleep  # type: ignore[no-redef]
    from life_catchup import (  # type: ignore[no-redef]
        SKIP_LABELS as CATCHUP_SKIP_LABELS,
        Candidate as CatchupCandidate,
        CatchupRules,
        build_intent as catchup_intent,
        fill_fact as catchup_fill_fact,
        merge_entries as catchup_merge_entries,
        plan as catchup_plan,
        should_record as catchup_should_record,
    )
    from life_relations import person_id_for as relation_person_id  # type: ignore[no-redef]
    from life_sim import day_key_of, local_datetime  # type: ignore[no-redef]
    from life_social import flag_value  # type: ignore[no-redef]


def _as_str_list(raw: Any) -> list[str]:
    """把配置里的「每行一个」列表宽松摊平（兼容字符串、换行、逗号）。"""

    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        return [item for item in raw.replace("\n", ",").split(",")]
    return []


class CatchupHostMixin:
    """补答的宿主侧方法集。**只由 ``LifeFrequencyPlugin`` 继承**，不单独实例化。

    ⚠ 这里所有 ``self._xxx`` 都假定宿主类已经准备好：``_state`` / ``config`` /
    ``_routine_store`` / ``_seen_sessions`` / ``_relation_familiarity`` /
    ``_persona`` / ``_trigger_proactive``。mixin 不自己造这些（造了就是第二份真相）。
    """

    # ------------------------------------------------------------ 初始化

    def _init_catchup(self) -> None:
        """在插件 ``__init__`` 里调一次（替代 22 行字段声明挤在入口文件里）。"""

        #: 已排入错开队列的补答：``[(到期时刻, session_id)]``。纯内存——重启即丢，
        #: 但条目本身在 SQLite 里，下一次启动扫描会把它们重新排上。
        self._catchup_queue: list[tuple[float, str]] = []
        #: 错开执行的后台任务。同一时刻只保留一个，避免两次睡醒叠出两条并发链。
        self._catchup_task: Any = None
        self._catchup_generation: int = 0
        #: person_id → 已注入过的行 id 上界（防止每轮回复都重复提同一句）
        self._catchup_injected: dict[str, int] = {}
        #: person_id → 最近几条原文的内存镜像（回复侧注入是同步路径，不能读库）
        self._catchup_texts: dict[str, list[dict[str, Any]]] = {}
        #: 本次睡醒的序号（错开种子用；每次「睡 → 非睡」+1）
        self._catchup_wake_seq: int = 0
        #: 刚发生过一次「睡 → 非睡」，等 ``_maybe_catchup`` 消费
        self._catchup_pending_wake: bool = False
        #: 启动扫描是否已做过（只做一次：覆盖「睡醒发生在停机期间」那一类）
        self._catchup_scanned: bool = False
        #: 上次清理库的时刻（每小时一次）
        self._catchup_pruned_at: float = 0.0

    # ------------------------------------------------------------ 配置与身份

    def _catchup_rules(self) -> CatchupRules:
        """配置 → ``CatchupRules``。"""

        catchup = self.config.catchup
        return CatchupRules(
            enabled=bool(catchup.enabled),
            active_enabled=bool(catchup.active_enabled),
            per_wake_max=max(0, int(catchup.per_wake_max)),
            per_person_daily=max(1, int(catchup.per_person_daily)),
            global_daily_max=max(0, int(catchup.global_daily_max)),
            late_degrade_hours=max(0.0, float(catchup.late_degrade_hours)),
            keep_per_person=max(0, int(catchup.keep_per_person)),
            max_chars=max(0, int(catchup.max_chars)),
            retention_hours=max(0.0, float(catchup.retention_hours)),
            requote_limit=max(0, int(catchup.requote_limit)),
            stagger_max_seconds=max(0, int(catchup.stagger_max_seconds)),
            persona_chars=max(0, int(catchup.persona_chars)),
        )

    def _wake_private_scope(self) -> str:
        """私聊能不能叫醒她：``off`` / ``admin`` / ``anyone``。

        留空 = 跟随旧字段 ``[simulation].wake_on_private``（为真即 ``anyone``，
        否则 ``admin``）。这样升级后**不会**凭空出现「谁都能叫醒她」的行为，
        而 `admin` 档在 ``[security].admin_ids`` 留空时等于 `off`（fail-closed）。
        """

        raw = str(self.config.simulation.wake_private_scope or "").strip().lower()
        if raw in {"off", "admin", "anyone"}:
            return raw
        return "anyone" if bool(self.config.simulation.wake_on_private) else "admin"

    def _admin_ids(self) -> set[str]:
        admins = {str(item).strip() for item in _as_str_list(self.config.security.admin_ids)}
        admins.discard("")
        return admins

    def _is_admin_user(self, user_id: str, *, platform: str = "") -> bool:
        """这个 QQ 号在不在管理员名单里（``'123'`` / ``'qq:123'`` 两种写法都认）。

        ⚠ 与 ``_is_admin``（命令鉴权）**共用同一份名单但语义不同**：那条管「谁能改
        状态」，这条管「谁能在她睡觉时叫醒她」。共用名单是刻意的取舍——加人做管理员
        就等于允许他半夜叫醒她（README 的「权限与亲密关系」一节写明了这一点）。
        """

        uid = str(user_id or "").strip()
        if not uid:
            return False
        admins = self._admin_ids()
        if not admins:
            return False
        plat = str(platform or self.config.apply.platform or "").strip().lower()
        candidates = {uid, f"qq:{uid}"}
        if plat:
            candidates.add(f"{plat}:{uid}")
        return bool(admins & candidates)

    def _clock_label(self, stamp: float) -> str:
        """时间戳 → 本地 ``HH:MM``（补答文本里的「03:12」）。"""

        try:
            return local_datetime(
                float(stamp), self._sim_config().tz_offset_minutes
            ).strftime("%H:%M")
        except Exception:  # noqa: BLE001 —— 时间格式化失败不该影响补答
            return ""

    def _catchup_person(self, session_id: str, user_id: str = "") -> tuple[str, str]:
        """``(person_id, platform)``：与关系档案同一个键算法（md5(platform:user_id)）。"""

        info = self._seen_sessions.get(session_id) or {}
        uid = str(user_id or info.get("user_id") or "").strip()
        platform = str(info.get("platform") or self.config.apply.platform or "qq")
        if not uid:
            return "", platform
        return str(relation_person_id(platform, uid) or ""), platform

    # ------------------------------------------------------------ 记录

    async def _record_sleep_backlog(
        self,
        message: Any,
        *,
        session_id: str,
        group_id: str,
        user_id: str,
        now: float,
    ) -> str:
        """她睡着时进来的消息 → 积压。返回判定原因（非空 = 没记录）。

        判据全部在 ``life_catchup.should_record``；这里只补三件宿主侧的事：
        取文本、取 ``person_id``、把写库挪进线程池。
        """

        rules = self._catchup_rules()
        if not rules.enabled or self._routine_store is None:
            return "disabled"
        private_chat = not bool(group_id)
        mentioned = bool(flag_value(message, "is_at") or flag_value(message, "is_mentioned"))
        text = (
            str((message or {}).get("processed_plain_text") or "").strip()
            if isinstance(message, Mapping)
            else ""
        )
        is_command = str(text or "").lstrip().startswith("/") or bool(
            flag_value(message, "is_command")
        )
        reason = catchup_should_record(
            text=text,
            is_command=is_command,
            is_from_bot=bool(user_id) and str(user_id) == str(self._bot_user_id or ""),
            asleep=is_asleep(self._state.activity),
            in_wake_window=self._wake_active(now),
            private_chat=private_chat,
            mentioned=mentioned,
        )
        if reason:
            return reason
        person_id, platform = self._catchup_person(session_id, user_id)
        if not person_id:
            return "无法确定人物"
        store = self._routine_store
        row_id = await asyncio.to_thread(
            store.add_sleep_backlog,
            person_id,
            session_id,
            at=float(now),
            text=str(text or ""),
            user_id=str(user_id or ""),
            platform=platform,
            is_group=not private_chat,
            max_chars=max(1, int(rules.max_chars)),
        )
        if row_id:
            cached = self._catchup_texts.setdefault(person_id, [])
            cached.append(
                {
                    "id": int(row_id),
                    "at": float(now),
                    "text": str(text or "")[: max(1, int(rules.max_chars))],
                    "session_id": session_id,
                }
            )
            keep = max(1, int(rules.keep_per_person))
            if len(cached) > keep:
                del cached[:-keep]
            self._module_log("debug", 
                "%s 睡眠积压 +1（%s，session=%s，共 %d 条缓存）",
                _PLUGIN_TAG, "群聊 @ 我" if not private_chat else "私聊", session_id, len(cached),
            )
        return ""

    # ------------------------------------------------------------ 醒来触发

    async def _maybe_catchup(self, now: float) -> None:
        """tick 里的补答入口：**只在自然醒**（``sleep → 非 sleep``）排一次。

        ⚠ 为什么不能用「唤醒窗口结束」：主人在 03:00 叫醒她，窗口结束时 ``activity``
        仍是 ``sleep``——此时补答等于在凌晨替她给 5 个人发消息，与「其他人睡觉时别打扰
        她」的初衷正相反。判据因此挂在活动转换上，而不是窗口。

        启动扫描只做一次：覆盖「睡醒发生在停机期间」这一类，受 ``retention_hours``
        兜底，不会把上个星期的留言翻出来。
        """

        rules = self._catchup_rules()
        if not rules.enabled or self._routine_store is None:
            self._catchup_pending_wake = False
            return
        if (self._stopping or not self.config.plugin.enabled or self._is_paused()
                or is_asleep(self._state.activity)):
            return
        if self._catchup_task is not None and not self._catchup_task.done():
            return

        armed = bool(self._catchup_pending_wake)
        self._catchup_pending_wake = False
        first_scan = not self._catchup_scanned
        self._catchup_scanned = True
        if not armed and not first_scan:
            return

        generation = self._catchup_generation
        store = self._routine_store
        retention = max(0.0, float(rules.retention_hours)) * 3600.0
        since = (float(now) - retention) if retention > 0.0 else 0.0
        rows = await asyncio.to_thread(store.pending_sleep_sessions, since=since)
        if generation != self._catchup_generation:
            self._module_log("debug", "%s 补答扫描期间配置或生命周期变化，放弃旧结果", _PLUGIN_TAG)
            return
        if not rows:
            return

        sim_config = self._sim_config()
        day_key = day_key_of(
            local_datetime(now, sim_config.tz_offset_minutes), sim_config.day_boundary_hour
        )
        self._roll_catchup_day(day_key)

        candidates: list[CatchupCandidate] = []
        for row in rows:
            person_id = str(row.get("person_id") or "")
            entries = await asyncio.to_thread(store.peek_sleep_backlog, person_id, limit=50)
            if generation != self._catchup_generation:
                self._module_log("debug", "%s 补答扫描期间配置或生命周期变化，放弃旧结果", _PLUGIN_TAG)
                return
            if not entries:
                continue
            merged = catchup_merge_entries(entries, keep=max(1, int(rules.keep_per_person)))
            candidates.append(
                CatchupCandidate(
                    session_id=str(row.get("session_id") or ""),
                    person_id=person_id,
                    user_id=str(row.get("user_id") or ""),
                    is_group=bool(row.get("is_group")),
                    last_at=float(row.get("last_at") or 0.0),
                    count=int(row.get("count") or len(entries)),
                    entries=tuple(merged),
                    familiarity=float(self._relation_familiarity.get(person_id, 0.0) or 0.0),
                )
            )
        if not candidates:
            return

        self._catchup_wake_seq += 1
        planned, skipped = catchup_plan(
            candidates=candidates,
            rules=rules,
            now=float(now),
            day_key=day_key,
            wake_seq=self._catchup_wake_seq,
            person_used_today=self._state.catchup_people,
            global_used_today=int(self._state.catchup_sent),
            stagger_seed=f"{day_key}|{'scan' if first_scan and not armed else 'wake'}",
        )
        for session_id, reason in skipped:
            self._module_log("debug", 
                "%s 补答跳过 session=%s：%s",
                _PLUGIN_TAG, session_id, CATCHUP_SKIP_LABELS.get(reason, reason),
            )
        if not planned:
            return
        self._module_log("info", 
            "%s 睡醒补答排队 %d 个会话（错开上限 %ds）：%s",
            _PLUGIN_TAG,
            len(planned),
            int(rules.stagger_max_seconds),
            "、".join(f"{candidate.session_id}(+{offset:.0f}s)" for offset, candidate in planned),
        )
        self._spawn_catchup_flush(
            planned, now=float(now), sleep_minutes=self._last_sleep_minutes(now)
        )

    def _roll_catchup_day(self, day_key: str) -> None:
        """跨生活日重置补答配额（与其它「生活日」计数器同口径）。"""

        if str(self._state.catchup_day or "") == str(day_key or ""):
            return
        self._state.catchup_day = str(day_key or "")
        self._state.catchup_sent = 0
        self._state.catchup_people = {}
        self._state_dirty = True

    def _last_sleep_minutes(self, now: float) -> float:
        """上一觉睡了多久（分钟）；量不出来返回 0（intent 里就不提时长）。"""

        started = float(getattr(self._state, "sleep_started_at", 0.0) or 0.0)
        if started > 0.0 and float(now) > started:
            return (float(now) - started) / 60.0
        # ``apply_activity`` 醒来时会清 ``sleep_started_at``，而本方法在醒来后的同一个
        # tick 里调用 ⇒ 退到睡眠账本的最后一个区间。两处都量不出来就返回 0。
        ledger = getattr(self._state, "sleep_ledger", ()) or ()
        if ledger:
            last = ledger[-1] if isinstance(ledger[-1], dict) else {}
            begin = float(last.get("start") or 0.0)
            end = float(last.get("end") or 0.0)
            if begin > 0.0 and end > begin:
                return (end - begin) / 60.0
        return 0.0

    def _spawn_catchup_flush(
        self, planned: list[tuple[float, CatchupCandidate]], *, now: float, sleep_minutes: float
    ) -> None:
        """把已排队的补答丢进一个后台任务，按错开秒数逐个触发。

        为什么用后台任务而不是「下一个 tick 再发」：tick 默认 600 秒，而错开窗口只有
        0~120 秒——放进 tick 等于错开完全失效，所有会话挤在同一刻。代价是重启会丢掉
        未执行的偏移（条目仍在库里，下次启动扫描会重排），这是刻意的取舍。
        """

        if self._stopping:
            return
        if self._catchup_task is not None and not self._catchup_task.done():
            self._catchup_pending_wake = True
            self._module_log("debug", "%s 上批补答仍在执行，本次留待下轮重排", _PLUGIN_TAG)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            self._module_log("warning", "%s 补答后台任务无法启动：%s", _PLUGIN_TAG, exc)
            return
        started = time.monotonic()

        async def runner() -> None:
            for offset, candidate in planned:
                delay = started + float(offset) - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                rules = self._catchup_rules()
                if (self._stopping or not self.config.plugin.enabled or self._is_paused()
                        or is_asleep(self._state.activity)
                        or not rules.enabled or not rules.active_enabled):
                    self._module_log("debug", "%s 补答条件已变化，未处理条目留在积压库", _PLUGIN_TAG)
                    return
                try:
                    await self._catchup_trigger(
                        candidate, now=time.time(), sleep_minutes=sleep_minutes
                    )
                except Exception as exc:  # noqa: BLE001 —— 一条失败不该拖垮整批
                    self._module_log("warning", 
                        "%s 补答触发失败 session=%s：%s", _PLUGIN_TAG, candidate.session_id, exc
                    )

        self._catchup_task = loop.create_task(runner())

    async def _cancel_catchup_flush(self) -> None:
        self._catchup_generation += 1
        task = self._catchup_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        except Exception as exc:
            self._module_log("warning", "%s 补答后台任务退出异常：%s", _PLUGIN_TAG, exc)
        finally:
            if self._catchup_task is task:
                self._catchup_task = None

    async def _catchup_trigger(
        self, candidate: CatchupCandidate, *, now: float, sleep_minutes: float = 0.0
    ) -> str:
        """触发一次 Planner（**插件不写她的措辞**，只交代来由与原文）。

        配额口径：**按宿主受理计**（``"sent"`` 即计数）。不复用 ``[proactive]`` 的
        ``confirm_send`` 核销链——那条链在「没说出话」时会把账记到主动开口的每日额度上
        （默认 1 条），一次补答就能吃掉她整天的日常主动开口。
        """

        rules = self._catchup_rules()
        intent = catchup_intent(
            candidate,
            rules=rules,
            persona=self._persona,
            label_of_time=self._clock_label,
            sleep_minutes=sleep_minutes,
        )
        decision = SimpleNamespace(
            material={
                "label": "睡醒补答",
                "text": intent[:200],
                "target_session": candidate.session_id,
            },
            score=0.0,
            intent=intent,
            detail=f"catchup:{candidate.count}",
        )
        sim_config = self._sim_config()
        day_key = day_key_of(
            local_datetime(now, sim_config.tz_offset_minutes), sim_config.day_boundary_hour
        )
        outcome = await self._trigger_proactive(
            candidate.session_id, decision, day_key, now, expected_last_user=0.0
        )
        if outcome != "sent":
            self._module_log("info", 
                "%s 补答未受理 session=%s（%s），条目保留，下次醒来或被动回填再处理",
                _PLUGIN_TAG, candidate.session_id, outcome,
            )
            return outcome

        ids = [int(item.get("id")) for item in candidate.entries if item.get("id")]
        store = self._routine_store
        if store is not None and ids:
            try:
                await asyncio.to_thread(store.mark_sleep_backlog_caught_up, ids, at=float(now))
            except Exception as exc:  # noqa: BLE001
                self._module_log("debug", "%s 补答核销失败：%s", _PLUGIN_TAG, exc)
        self._state.catchup_sent = int(self._state.catchup_sent) + 1
        people = {str(k): int(v) for k, v in (self._state.catchup_people or {}).items()}
        people[candidate.person_id] = int(people.get(candidate.person_id, 0)) + 1
        self._state.catchup_people = people
        self._state_dirty = True
        # 已在主动补答里交代过的条目，不要再被被动回填重复提一次
        if candidate.person_id and ids:
            self._catchup_injected[candidate.person_id] = max(ids)
        self._module_log("info", 
            "%s 睡醒补答已受理 session=%s（%d 条，今日第 %d 次）",
            _PLUGIN_TAG, candidate.session_id, candidate.count, self._state.catchup_sent,
        )
        return outcome

    # ------------------------------------------------------------ 被动回填

    def _catchup_fill(self, session_id: str, now: float) -> str:
        """被动回填文本（注入 replyer 的 ``extra_prompt``）。空串 = 这轮没什么可提的。

        只提一次：用「已注入到的行 id 上界」去重，避免她连续三轮都先说「你睡着时找过我」。
        """

        rules = self._catchup_rules()
        if not rules.enabled or not session_id:
            return ""
        person_id, _platform = self._catchup_person(session_id)
        if not person_id:
            return ""
        entries = self._catchup_texts.get(person_id) or []
        if not entries:
            return ""
        newest = max(int(item.get("id") or 0) for item in entries)
        if int(self._catchup_injected.get(person_id, 0) or 0) >= newest:
            return ""  # 这一批已经提过（或已主动补过）
        late_limit = max(0.0, float(rules.late_degrade_hours)) * 3600.0
        if late_limit > 0.0 and (float(now) - float(entries[-1].get("at") or 0.0)) > late_limit * 4:
            # 迟到大超过「降级阈值」的 4 倍：连回填都不提，避免「你上周三找过我」
            return ""
        text = catchup_fill_fact(entries, rules=rules, label_of_time=self._clock_label)
        if not text:
            return ""
        self._catchup_injected[person_id] = newest
        return text

    async def _catchup_prune(self, now: float, rules: CatchupRules) -> None:
        """每小时清一次库（过期 + 每人超量）。"""

        if self._routine_store is None:
            return
        if float(now) - float(self._catchup_pruned_at or 0.0) < 3600.0:
            return
        self._catchup_pruned_at = float(now)
        retention = max(0.0, float(rules.retention_hours)) * 3600.0
        before = (float(now) - retention) if retention > 0.0 else 0.0
        try:
            await asyncio.to_thread(
                self._routine_store.prune_sleep_backlog,
                before=before,
                keep_per_person=max(1, int(rules.keep_per_person)),
            )
        except Exception as exc:  # noqa: BLE001
            self._module_log("debug", "%s 补答清理失败：%s", _PLUGIN_TAG, exc)


#: 日志前缀。mixin 不能 ``from .plugin import __plugin_id__``（循环导入），
#: 所以这里独立定义一份；值与 ``plugin.__plugin_id__`` 必须一致（有测试钉住）。
_PLUGIN_TAG = "org.orge-8.life-frequency"

__all__ = ["CatchupHostMixin"]
