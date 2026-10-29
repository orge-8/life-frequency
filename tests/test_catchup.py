# -*- coding: utf-8 -*-
"""睡醒补答（v1.23.0）：纯逻辑 + 存储 + 唤醒范围的判定矩阵。

分三层钉住，因为这三层会用**不同的方式**各自坏掉：

1. ``life_catchup``（纯逻辑）：判据、排序、配额。坏了的表现是「她补错人 / 补太多次」，
   而且**没有任何异常**——只能靠断言发现。
2. ``life_store.sleep_backlog``：v2 老库升级到 v3 必须幂等。坏了的表现是
   「新装能跑、升级就崩」，偏偏只在真机出现（本地全是新建库）。
3. ``CatchupHostMixin._wake_private_scope`` / ``_is_admin_user``：三态解析与 fail-closed。
   坏了的表现是「谁都能半夜叫醒她」——隐私边界，不能靠真机试。
"""

import sqlite3
import types

import pytest

import life_catchup as C
import life_store as S
from life_catchup_host import CatchupHostMixin


# ---------------------------------------------------------------- 判据


def test_should_record_matrix():
    """记录判据的六道闸：醒着 / 窗口内 / 命令 / 自发 / 没对她说话 / 勿扰。"""

    ok = dict(
        text="在吗", is_command=False, is_from_bot=False, asleep=True,
        in_wake_window=False, private_chat=True, mentioned=False,
    )
    assert C.should_record(**ok) == C.REASON_OK

    assert C.should_record(**{**ok, "asleep": False}) == C.REASON_AWAKE
    assert C.should_record(**{**ok, "in_wake_window": True}) == C.REASON_IN_WAKE_WINDOW
    assert C.should_record(**{**ok, "is_command": True}) == C.REASON_COMMAND
    assert C.should_record(**{**ok, "is_from_bot": True}) == C.REASON_FROM_BOT
    assert C.should_record(**{**ok, "private_chat": False}) == C.REASON_NOT_ADDRESSED
    assert C.should_record(**{**ok, "text": "  "}) == C.REASON_EMPTY


def test_group_message_records_only_when_mentioned():
    """群里只有被 @ 的那条才进积压——其余消息与她无关，补答起来就是刷屏。"""

    base = dict(text="今天吃什么", is_command=False, is_from_bot=False, asleep=True,
                in_wake_window=False, private_chat=False)
    assert C.should_record(**base, mentioned=False) == C.REASON_NOT_ADDRESSED
    assert C.should_record(**base, mentioned=True) == C.REASON_OK


@pytest.mark.parametrize(
    "text",
    ["别回了", "不用回我", "继续睡吧", "别打扰你休息", "安心睡", "别 吵 了"],
)
def test_do_not_disturb_texts_are_detected(text):
    """勿扰句式**不入队**：醒来后拿这句话去补答，等于告诉对方「我记着但没听」。"""

    assert C.is_do_not_disturb(text)
    assert C.should_record(
        text=text, is_command=False, is_from_bot=False, asleep=True,
        in_wake_window=False, private_chat=True, mentioned=False,
    ) == C.REASON_DND


def test_ordinary_text_is_not_do_not_disturb():
    for text in ["在吗", "睡了吗", "晚安", "明天见", ""]:
        assert not C.is_do_not_disturb(text)


# ---------------------------------------------------------------- 合并与排序


def _entry(row_id: int, at: float, text: str) -> dict:
    return {"id": row_id, "at": at, "text": text, "session_id": "s1"}


def test_merge_entries_keeps_latest_and_is_time_ordered():
    entries = [_entry(i, float(i), f"m{i}") for i in range(1, 8)]
    merged = C.merge_entries(entries, keep=3)
    assert [item["text"] for item in merged] == ["m5", "m6", "m7"]


def test_merge_entries_zero_keeps_everything():
    """``keep=0`` 是「全留」，不是「留一条」——``[-0:]`` 会返回整表的坑要避开。"""

    entries = [_entry(i, float(i), f"m{i}") for i in range(1, 4)]
    assert len(C.merge_entries(entries, keep=0)) == 3


def test_order_candidates_prefers_familiar_then_longest_wait():
    cands = [
        C.Candidate("s-stranger", "p1", "u1", False, last_at=100.0, count=1, familiarity=0.0),
        C.Candidate("s-friend-new", "p2", "u2", False, last_at=900.0, count=1, familiarity=0.8),
        C.Candidate("s-friend-old", "p3", "u3", False, last_at=200.0, count=1, familiarity=0.8),
    ]
    ordered = C.order_candidates(cands)
    assert [item.session_id for item in ordered] == ["s-friend-old", "s-friend-new", "s-stranger"]


def test_stagger_offsets_are_deterministic_and_within_cap():
    first = C.stagger_offsets(5, max_seconds=120, seed_key="2026-10-23|wake1")
    again = C.stagger_offsets(5, max_seconds=120, seed_key="2026-10-23|wake1")
    other = C.stagger_offsets(5, max_seconds=120, seed_key="2026-10-23|wake2")
    assert first == again, "同一次睡醒重算必须得到同一组偏移"
    assert first != other, "不同睡醒之间不该完全一致（否则等于没有错开）"
    assert all(0.0 <= value <= 120.0 for value in first)
    assert C.stagger_offsets(3, max_seconds=0, seed_key="x") == [0.0, 0.0, 0.0]
    assert C.stagger_offsets(0, max_seconds=120, seed_key="x") == []


# ---------------------------------------------------------------- 配额


def _rules(**kwargs) -> C.CatchupRules:
    base = dict(
        enabled=True, active_enabled=True, per_wake_max=5, per_person_daily=1,
        global_daily_max=6, late_degrade_hours=6.0, keep_per_person=5, max_chars=200,
        retention_hours=24.0, requote_limit=3, stagger_max_seconds=0, persona_chars=0,
    )
    base.update(kwargs)
    return C.CatchupRules(**base)


def _candidate(session: str, person: str, *, at: float = 1000.0, familiarity: float = 0.0):
    return C.Candidate(
        session_id=session, person_id=person, user_id=person, is_group=False,
        last_at=at, count=1, entries=(_entry(1, at, "在吗"),), familiarity=familiarity,
    )


def test_plan_skips_when_active_disabled():
    planned, skipped = C.plan(
        candidates=[_candidate("s1", "p1")], rules=_rules(active_enabled=False),
        now=1000.0, day_key="2026-10-23", wake_seq=1,
        person_used_today={}, global_used_today=0,
    )
    assert planned == []
    assert skipped == [("s1", "not_active")]


def test_plan_respects_per_wake_cap():
    cands = [_candidate(f"s{i}", f"p{i}") for i in range(8)]
    planned, skipped = C.plan(
        candidates=cands, rules=_rules(per_wake_max=3), now=1000.0,
        day_key="2026-10-23", wake_seq=1, person_used_today={}, global_used_today=0,
    )
    assert len(planned) == 3
    assert {reason for _, reason in skipped} == {"per_wake"}


def test_plan_respects_per_person_and_global_caps():
    cands = [_candidate(f"s{i}", f"p{i}") for i in range(4)]
    planned, skipped = C.plan(
        candidates=cands, rules=_rules(per_person_daily=1, global_daily_max=2),
        now=1000.0, day_key="2026-10-23", wake_seq=1,
        person_used_today={"p0": 1}, global_used_today=0,
    )
    assert len(planned) == 2
    reasons = {reason for _, reason in skipped}
    assert "per_person" in reasons and "global_daily" in reasons


def test_plan_degrades_late_messages_to_passive():
    """迟到超过阈值就不主动补——隔一整天再提「你昨晚找过我」很奇怪。"""

    cands = [_candidate("s-old", "p1", at=0.0), _candidate("s-new", "p2", at=30000.0)]
    planned, skipped = C.plan(
        candidates=cands, rules=_rules(late_degrade_hours=6.0), now=40000.0,
        day_key="2026-10-23", wake_seq=1, person_used_today={}, global_used_today=0,
    )
    assert [candidate.session_id for _, candidate in planned] == ["s-new"]
    assert ("s-old", "too_late") in skipped


# ---------------------------------------------------------------- 措辞


def test_build_intent_does_not_repeat_history_content_by_default():
    """默认口径 A：**不复述原文**——那些消息本来就在宿主的对话历史里。

    这条是 v1.23.1 的核心断言：一旦有人把原文又塞回 intent，同一句话会在
    Planner 眼里出现两次，她会讲两遍。
    """

    cand = C.Candidate(
        session_id="s1", person_id="p1", user_id="u1", is_group=False, last_at=300.0,
        count=3,
        entries=(
            _entry(1, 100.0, "在吗"),
            _entry(2, 200.0, "睡了吗"),
            _entry(3, 300.0, "明天有空吗"),
        ),
    )
    intent = C.build_intent(
        cand, rules=_rules(requote_limit=0), persona="", sleep_minutes=420.0,
        label_of_time=lambda stamp: f"{int(stamp) // 60:02d}:{int(stamp) % 60:02d}",
    )
    assert "刚睡了约 7.0 小时" in intent
    assert "共 3 条" in intent
    assert "01:40–05:00" in intent, "要给时间段（历史里读不出「你当时在睡」）"
    assert "在吗" not in intent and "明天有空吗" not in intent, "不该复述原文"
    assert "已经在对话历史里" in intent
    assert "不要逐条复述" in intent
    assert "保持沉默" in intent, "必须给 Planner 沉默的许可，否则她会硬找话说"


def test_build_intent_can_opt_into_quoting_for_trimmed_history():
    """把 ``requote_limit`` 调大 = 回到兜底模式（担心历史被裁掉时才需要）。"""

    cand = C.Candidate(
        session_id="s1", person_id="p1", user_id="u1", is_group=False, last_at=300.0,
        count=2,
        entries=(_entry(1, 100.0, "在吗"), _entry(2, 200.0, "睡了吗")),
    )
    intent = C.build_intent(
        cand, rules=_rules(requote_limit=1), persona="", sleep_minutes=0.0,
        label_of_time=None,
    )
    assert "睡了吗" in intent and "在吗" not in intent, "只引 requote_limit 条"


def test_build_intent_group_note_and_persona_hint():
    cand = C.Candidate(
        session_id="g1", person_id="p1", user_id="u1", is_group=True, last_at=100.0,
        count=1, entries=(_entry(1, 100.0, "有人吗"),),
    )
    intent = C.build_intent(
        cand, rules=_rules(persona_chars=10), persona="一个嘴硬心软的少女",
        label_of_time=None, sleep_minutes=0.0,
    )
    assert "这是群聊" in intent and "不要 @" in intent
    assert "人设参考" in intent


def test_fill_fact_says_she_missed_it_without_repeating_text():
    fact = C.fill_fact(
        [_entry(1, 100.0, "在吗")], rules=_rules(requote_limit=0),
        label_of_time=lambda stamp: "03:12",
    )
    assert "03:12" in fact, "时间段必须给（历史里没有「你当时在睡」）"
    assert "在吗" not in fact, "默认不复述原文"
    assert "没看到" in fact and "对话历史" in fact


def test_time_span_handles_single_and_missing_timestamps():
    one = [_entry(1, 100.0, "a")]
    assert C.time_span(one, label_of_time=lambda stamp: "03:12") == "03:12"
    two = [_entry(1, 100.0, "a"), _entry(2, 400.0, "b")]
    assert C.time_span(two, label_of_time=lambda stamp: f"{int(stamp) // 60:02d}:00") == "01:00–06:00"
    assert C.time_span([], label_of_time=lambda stamp: "x") == ""
    assert C.time_span(one, label_of_time=None) == ""


# ---------------------------------------------------------------- 存储


def test_backlog_roundtrip_and_caught_up(tmp_path):
    store = S.LifeStore(str(tmp_path / "t.db"))
    row_id = store.add_sleep_backlog("p1", "s1", at=100.0, text="在吗", user_id="u1")
    assert row_id > 0
    assert [item["text"] for item in store.peek_sleep_backlog("p1")] == ["在吗"]

    pending = store.pending_sleep_sessions(since=0.0)
    assert len(pending) == 1 and pending[0]["count"] == 1

    assert store.mark_sleep_backlog_delivered([row_id], at=200.0) == 1
    assert store.peek_sleep_backlog("p1")[0]["delivered_at"] == 200.0
    assert store.pending_sleep_sessions(since=0.0)[0]["delivered_at"] == 200.0

    assert store.mark_sleep_backlog_caught_up([row_id], at=300.0) == 1
    assert store.peek_sleep_backlog("p1") == []
    assert store.pending_sleep_sessions(since=0.0) == []


def test_backlog_upgrade_from_v2_is_idempotent(tmp_path):
    """老库（``user_version=2``）打开后必须自动建出新表且版本升到 3。

    只测新库会漏掉这一类：本地跑测试永远是新建库，真机是**升级**。
    """

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE relationships (person_id TEXT PRIMARY KEY, platform TEXT NOT NULL "
        "DEFAULT 'qq', user_id TEXT NOT NULL DEFAULT '', first_seen_at REAL NOT NULL, "
        "last_interaction_at REAL NOT NULL, interaction_count INTEGER NOT NULL DEFAULT 0, "
        "familiarity REAL NOT NULL DEFAULT 0, relation_hint TEXT NOT NULL DEFAULT '', "
        "shared_events INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    for _ in range(2):  # 连开两次：第二次不该因为表已存在而失败
        store = S.LifeStore(path)
        assert store.backend == "sqlite"
        assert store.add_sleep_backlog("p1", "s1", at=100.0, text="在吗") > 0
        store.close()

    conn = sqlite3.connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == S.SCHEMA_VERSION == 3


def test_memory_store_backlog_has_same_semantics():
    store = S.MemoryStore()
    row_id = store.add_sleep_backlog("p1", "s1", at=100.0, text="在吗")
    assert store.pending_sleep_sessions()[0]["ids"] == [row_id]
    assert store.mark_sleep_backlog_caught_up([row_id], at=200.0) == 1
    assert store.pending_sleep_sessions() == []


# ---------------------------------------------------------------- 唤醒范围


class _Stub(CatchupHostMixin):
    """只带配置的最小宿主：``_wake_private_scope`` / ``_is_admin_user`` 不碰 ctx。"""

    def __init__(self, *, scope: str, legacy: bool, admins: list[str]):
        self.config = types.SimpleNamespace(
            simulation=types.SimpleNamespace(
                wake_private_scope=scope, wake_on_private=legacy
            ),
            security=types.SimpleNamespace(admin_ids=admins),
            apply=types.SimpleNamespace(platform="qq"),
        )


@pytest.mark.parametrize(
    "scope,legacy,admins,uid,expected",
    [
        ("off", False, ["10001"], "10001", False),
        ("admin", False, ["10001"], "10001", True),
        ("admin", False, ["qq:10001"], "10001", True),
        ("admin", False, ["10001"], "20002", False),
        ("admin", False, [], "10001", False),   # fail-closed：没配名单谁都不许
        ("anyone", False, [], "20002", True),
        ("", True, [], "20002", True),          # 旧字段为真 ⇒ anyone
        ("", False, ["10001"], "10001", True),  # 旧字段为假 ⇒ admin
    ],
)
def test_wake_private_scope_resolution(scope, legacy, admins, uid, expected):
    stub = _Stub(scope=scope, legacy=legacy, admins=admins)
    resolved = stub._wake_private_scope()
    allowed = resolved == "anyone" or (
        resolved == "admin" and stub._is_admin_user(uid, platform="qq")
    )
    assert allowed is expected


class _FlushStub(CatchupHostMixin):
    def __init__(self):
        from life_sim import LifeState

        self._init_catchup()
        self._stopping = False
        self._state = LifeState()
        self.config = types.SimpleNamespace(plugin=types.SimpleNamespace(enabled=True))
        self.logs = []

    def _module_log(self, level, text, *args):
        self.logs.append((level, text % args))

    def _is_paused(self):
        return False

    def _catchup_rules(self):
        return _rules()


def test_flush_offsets_use_batch_clock_and_isolate_failure(monkeypatch):
    import asyncio
    import life_catchup_host as H

    async def run():
        stub = _FlushStub()
        clock = [100.0]
        seen = []
        waits = []

        async def sleep(delay):
            waits.append(delay)
            clock[0] += delay

        async def trigger(candidate, **kwargs):
            seen.append((candidate.session_id, clock[0] - 100.0))
            clock[0] += 2.0
            if candidate.session_id == "s0":
                raise RuntimeError("injected failure")

        stub._catchup_trigger = trigger
        with monkeypatch.context() as patch:
            patch.setattr(H.time, "monotonic", lambda: clock[0])
            patch.setattr(H.asyncio, "sleep", sleep)
            stub._spawn_catchup_flush(
                [(delay, _candidate(f"s{i}", f"p{i}")) for i, delay in enumerate([10, 5, 20])],
                now=1000.0, sleep_minutes=120,
            )
            await stub._catchup_task
        assert seen == [("s0", 10.0), ("s1", 12.0), ("s2", 20.0)]
        assert waits == [10.0, 6.0]
        assert any("injected failure" in message for _, message in stub.logs)

    asyncio.run(run())


def test_flush_does_not_replace_active_task():
    import asyncio

    async def run():
        stub = _FlushStub()
        entered = asyncio.Event()
        release = asyncio.Event()
        seen = []

        async def trigger(candidate, **kwargs):
            seen.append(candidate.session_id)
            entered.set()
            await release.wait()

        stub._catchup_trigger = trigger
        stub._spawn_catchup_flush([(0, _candidate("s1", "p1"))], now=1000, sleep_minutes=120)
        first = stub._catchup_task
        await asyncio.wait_for(entered.wait(), 1)
        try:
            stub._spawn_catchup_flush([(0, _candidate("s2", "p2"))], now=1000, sleep_minutes=120)
            assert stub._catchup_task is first
            assert stub._catchup_pending_wake is True
            assert seen == ["s1"]
        finally:
            release.set()
            await asyncio.gather(first, stub._catchup_task, return_exceptions=True)

    asyncio.run(run())


def test_config_cancellation_invalidates_scan_in_flight(monkeypatch):
    import asyncio
    import life_catchup_host as H
    from life_sim import SimConfig

    async def run():
        stub = _FlushStub()
        stub._routine_store = S.MemoryStore()
        stub._routine_store.add_sleep_backlog("p1", "s1", at=1000.0, text="在吗")
        stub._relation_familiarity = {}
        stub._sim_config = lambda: SimConfig()
        scheduled = []
        stub._spawn_catchup_flush = lambda *args, **kwargs: scheduled.append(args)

        async def query(fn, *args, **kwargs):
            result = fn(*args, **kwargs)
            await stub._cancel_catchup_flush()
            stub._catchup_scanned = False
            return result

        with monkeypatch.context() as patch:
            patch.setattr(H.asyncio, "to_thread", query)
            await stub._maybe_catchup(1000.0)
        assert scheduled == []

    asyncio.run(run())


@pytest.mark.parametrize("stop", ["stopping", "sleep", "disabled", "paused", "catchup_disabled"])
def test_flush_rechecks_state_after_wait(monkeypatch, stop):
    import asyncio
    import life_catchup_host as H

    async def run():
        stub = _FlushStub()
        seen = []

        async def trigger(candidate, **kwargs):
            seen.append(candidate.session_id)

        async def sleep(delay):
            if stop == "stopping":
                stub._stopping = True
            elif stop == "sleep":
                stub._state.activity = "sleep"
            elif stop == "disabled":
                stub.config.plugin.enabled = False
            elif stop == "paused":
                stub._is_paused = lambda: True
            else:
                stub._catchup_rules = lambda: _rules(active_enabled=False)

        stub._catchup_trigger = trigger
        with monkeypatch.context() as patch:
            patch.setattr(H.asyncio, "sleep", sleep)
            stub._spawn_catchup_flush([(10, _candidate("s1", "p1"))], now=1000, sleep_minutes=120)
            await stub._catchup_task
        assert seen == []

    asyncio.run(run())
