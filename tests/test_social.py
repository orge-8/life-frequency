# -*- coding: utf-8 -*-
"""L3：社交经历 —— 日记选材摘要 + 入站信号 → 她的「近期经历」。

分两层：

1. **纯模块**（``life_social``）：跨插件契约的解析、入站信号的提取、聚合、
   每日情绪额度、去重、睡眠规则，以及各种坏输入（都不许抛）。
2. **桥接**（``plugin._refresh_social`` / ``_intake_social`` / ``note_session`` / 卡片）：
   只读 RPC 的节奏与降级、经历是否真的进了状态、以及一条硬纪律——
   **社交经历绝不写 frequency.set_adjust**。

真机语义对齐（都读过源码）：

- 跨插件 API 用 ``ctx.api.call("<插件ID>.<API名>", version=..., **kwargs)``；
  ``life-frequency`` 已在 manifest 声明 ``api.call``（与 budget-pacer 那条同一条通道）。
- 入站钩子 ``chat.receive.after_process`` 的载荷里有 ``processed_plain_text``、
  ``is_mentioned`` / ``is_at`` / ``is_command``、``message_info.group_info.group_id``。
- 睡眠中的社交**照常记录但不产生情绪增量**（``emotion_while_asleep = false``）。
"""

import asyncio
import json
import logging
import pathlib
import sys
import time

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config  # noqa: E402

import life_activity as A  # noqa: E402  （conftest 已把插件目录放进 sys.path）
import life_sim as S  # noqa: E402
import life_social as SOS  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
TARGET = "org.orge-8.better-diary.get_day_digest"
TZ = 480


# ---------------------------------------------------------------- 夹具


def digest_payload(*, date="2026-10-01", generated_at="2026-10-01 23:40:00",
                   items=None, reason="", days=None) -> dict:
    """造一份与 better-diary 的 ``get_day_digest`` 契约一致的返回。"""

    if days is not None:
        return {"schema_version": 1, "count": len(days), "days": days,
                "continuity": {}, "reason": reason}
    return {
        "schema_version": 1,
        "count": 1,
        "days": [{
            "date": date,
            "generated_at": generated_at,
            "material_mode": "events",
            "items": items if items is not None else [
                {"event_id": "ev_1", "who": "阿岚", "what": "说起她换了个新键盘",
                 "quote": "这个轴体声音太大了"},
                {"event_id": "ev_2", "who": "", "what": "群里在约周末爬山", "quote": ""},
            ],
            "meta": {"topics": [], "people": [], "projects": [], "unresolved": []},
        }],
        "continuity": {},
        "reason": reason,
    }


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_social")


def _make_plugin(**config_overrides):
    module = _load()
    plugin = module.create_plugin()
    host = FakeHost(module.__plugin_id__, paths=FakePaths())
    ctx = build_context(module.__plugin_id__, rpc_call=host.rpc_call, paths=host.paths)
    config = get_default_config(type(plugin).config_model)
    config["frequency"]["quiet_hours"] = []
    config["events"]["fire_probability"] = 0.0
    config["proactive"]["enabled"] = False
    config["simulation"]["dry_run"] = False
    for section, values in config_overrides.items():
        config[section].update(values)
    bind_context(plugin, ctx, config)
    return module, plugin, host


class _Capture:
    def __init__(self, module):
        self.records: list[tuple[int, str]] = []
        self._handler = logging.Handler()
        self._handler.emit = lambda record: self.records.append(
            (record.levelno, record.getMessage())
        )
        self._logger = logging.getLogger(f"plugin.{module.__plugin_id__}")

    def __enter__(self):
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        return False

    def text(self, level=logging.DEBUG) -> str:
        return "\n".join(msg for lvl, msg in self.records if lvl >= level)


def ctx_for(now: float, *, day_key="2026-10-01", activity=A.DAILY, asleep=False,
            day_used=0.0, **policy_kwargs) -> SOS.IntakeContext:
    return SOS.IntakeContext(
        now=now,
        day_key=day_key,
        activity=activity,
        asleep=asleep,
        day_used=day_used,
        policy=SOS.SocialPolicy(**policy_kwargs),
    )


def digest_items(payload, *, max_per_day: int = 3):
    """只要条目（可用性由 ``test_parse_digest_distinguishes_unusable_from_empty`` 覆盖）。"""

    return SOS.parse_digest(
        payload, tz_offset_minutes=TZ, max_per_day=max_per_day
    ).items


# ================= A. 时间与契约解析 =================


def test_local_stamp_round_trips_with_local_datetime():
    """``generated_at`` 是**本地墙钟**，必须与 ``life_sim.local_datetime`` 互相反算。"""

    stamp = "2026-10-01 23:40:00"
    epoch = SOS.local_stamp_to_epoch(stamp, tz_offset_minutes=TZ)
    assert epoch > 0
    assert S.local_datetime(epoch, TZ).strftime("%Y-%m-%d %H:%M:%S") == stamp


def test_local_stamp_returns_zero_for_garbage():
    for bad in ("", "   ", "昨天", "2026-13-45 99:99:99", None):
        assert SOS.local_stamp_to_epoch(bad, tz_offset_minutes=TZ) == 0.0


def test_parse_digest_reads_the_contract():
    result = SOS.parse_digest(digest_payload(), tz_offset_minutes=TZ)
    assert result.usable is True and result.reason == ""
    items = result.items
    assert [item.what for item in items] == ["说起她换了个新键盘", "群里在约周末爬山"]
    assert items[0].who == "阿岚" and items[0].quote == "这个轴体声音太大了"
    assert items[0].event_id == "ev_1" and items[0].date == "2026-10-01"
    assert S.local_datetime(items[0].at, TZ).strftime("%H:%M") == "23:40"
    assert items[0].key == "digest!2026-10-01!ev_1"


def test_parse_digest_survives_bad_shapes():
    """对方升级/降级/返回垃圾都不许抛 —— 跨进程边界上异常会变成 RPCError。"""

    for bad in (None, [], "x", 42, {}, {"days": "不是列表"}, {"days": [1, 2]},
                {"days": [{"date": "", "items": [{"what": "x"}]}]},
                {"days": [{"date": "2026-10-01", "items": "不是列表"}]}):
        result = SOS.parse_digest(bad, tz_offset_minutes=TZ)
        assert result.items == []
        assert isinstance(result.reason, str) and result.reason


def test_parse_digest_drops_entries_without_what_and_caps_per_day():
    payload = digest_payload(items=[
        {"event_id": "a", "who": "甲", "what": "第一件"},
        {"event_id": "b", "who": "乙", "what": "   "},          # 空 what → 丢
        {"event_id": "c", "who": "丙", "what": "第二件"},
        {"event_id": "d", "who": "丁", "what": "第三件"},
    ])
    items = digest_items(payload, max_per_day=2)
    assert [item.what for item in items] == ["第一件", "第二件"]


def test_parse_digest_passes_the_reason_through_when_empty():
    result = SOS.parse_digest(
        digest_payload(items=[], reason="还没有任何日记存档"), tz_offset_minutes=TZ
    )
    assert result.items == [] and result.usable is True
    assert result.reason == "还没有任何日记存档"


def test_parse_digest_keeps_items_with_no_generated_at():
    """时间戳认不出来时**不猜时间**：``at=0``，入库时会被丢掉（而不是落到错误的层）。"""

    payload = digest_payload(generated_at="不是时间")
    items = digest_items(payload)
    assert items and all(item.at == 0.0 for item in items)


# ================= B. 入站信号 =================


def test_live_signal_reads_group_identity_and_mention():
    signal = SOS.live_signal(
        {"session_id": "g1", "processed_plain_text": "麦麦在吗", "is_mentioned": True,
         "message_info": {"group_info": {"group_id": "123456"}}},
        now=1000.0,
    )
    assert signal == {"at": 1000.0, "session_id": "g1", "is_group": True,
                      "mentioned": True, "text_len": 4, "user_id": ""}
    # v1.16.3（M7）：说话的人要在信号里——社交情绪按关系加权要靠它认人。
    # ⚠ 它只在内存里流转（不落盘、不进提示词），所以带 user_id 不违反脱敏纪律。
    identified = SOS.live_signal(
        {"session_id": "p1", "processed_plain_text": "在吗",
         "message_info": {"user_info": {"user_id": "10001"}}},
        now=2.0,
    )
    assert identified and identified["user_id"] == "10001"
    top_level = SOS.live_signal({"stream_id": "p1", "group_id": "", "processed_plain_text": "hi"},
                                now=1.0)
    assert top_level and top_level["is_group"] is False


def test_live_signal_ignores_commands_and_quiet_messages():
    assert SOS.live_signal({"session_id": "s", "processed_plain_text": "/生活",
                            "is_command": True}, now=1.0) is None
    assert SOS.live_signal({"session_id": "s", "processed_plain_text": ""}, now=1.0) is None
    assert SOS.live_signal({"processed_plain_text": "没有会话 id"}, now=1.0) is None
    assert SOS.live_signal(None, now=1.0) is None
    # 非文本消息：没文本但有人叫她 → 仍算「有人找她」
    sticker = SOS.live_signal({"session_id": "s", "processed_plain_text": "",
                               "is_at": True}, now=1.0)
    assert sticker and sticker["mentioned"] is True and sticker["text_len"] == 0


def test_live_signal_is_strict_about_string_flags():
    """``bool("false") is True`` —— 宿主把字段序列化成字符串时不许误判成「被 @ 了」。"""

    for value in ("false", "0", "no", "off", "", 0, None, []):
        assert SOS.live_signal({"session_id": "s", "processed_plain_text": "闲聊",
                                "is_mentioned": value}, now=1.0)["mentioned"] is False
    for value in (True, "true", "1", "yes", "on"):
        assert SOS.live_signal({"session_id": "s", "processed_plain_text": "闲聊",
                                "is_mentioned": value}, now=1.0)["mentioned"] is True


def test_prune_signals_drops_expired_and_junk():
    """坏时间戳一律丢：``at`` 决定它落在哪一层，猜时间等于凭空造经历。"""

    now = 100_000.0
    signals = [
        {"at": now - 60, "session_id": "新"},
        {"at": now - 13 * 3600, "session_id": "太旧"},
        {"at": "abc", "session_id": "坏时间"},
        {"session_id": "没时间"},
        "不是字典",
        None,
    ]
    kept = SOS.prune_signals(signals, now=now)
    assert [item["session_id"] for item in kept] == ["新"]


def test_parse_digest_distinguishes_unusable_from_empty():
    """「对方答的不是这个契约」是故障，「按契约答了但这几天没内容」不是。"""

    ok_empty = SOS.parse_digest(digest_payload(items=[], reason="还没有任何日记存档"),
                                tz_offset_minutes=TZ)
    assert ok_empty.usable is True and ok_empty.items == []
    assert ok_empty.reason == "还没有任何日记存档"

    for bad in ({"success": False, "error": "未找到 API 提供方插件"}, "不是字典",
                {"days": "不是列表"}, 42):
        result = SOS.parse_digest(bad, tz_offset_minutes=TZ)
        assert result.usable is False and result.reason
    assert "未找到 API 提供方插件" in SOS.parse_digest(
        {"success": False, "error": "未找到 API 提供方插件"}, tz_offset_minutes=TZ
    ).reason


# ================= C. 入库：额度、去重、睡眠 =================


def test_intake_digest_adds_events_without_touching_energy():
    now = 1_000_000.0
    items = digest_items(digest_payload())
    seen: dict[str, float] = {}
    intake = SOS.intake_digest(items, ctx_for(now), seen)
    assert len(intake.events) == 2
    assert intake.emotion_used == pytest.approx(0.8)
    first = intake.events[0]
    assert first["label"] == "和阿岚聊天" and first["source"] == "social"
    assert first["energy"] == 0.0 and first["activity"] == A.DAILY
    assert first["emotion"] == pytest.approx(0.4)
    assert first["text"] == "说起她换了个新键盘"
    assert len(seen) == 2


def test_intake_digest_is_idempotent():
    now = 1_000_000.0
    items = digest_items(digest_payload())
    seen: dict[str, float] = {}
    assert len(SOS.intake_digest(items, ctx_for(now), seen).events) == 2
    second = SOS.intake_digest(items, ctx_for(now + 60), seen)
    assert second.events == [] and second.skipped_seen == 2


def test_intake_digest_keeps_recording_but_stops_paying_for_stale_items():
    """超过 24 小时的事早通过情绪余波结算过了：仍可记进经历，但不再记一笔情绪。"""

    now = 1_000_000.0
    payload = digest_payload(generated_at=S.local_datetime(now - 30 * 3600, TZ)
                             .strftime("%Y-%m-%d %H:%M:%S"))
    items = digest_items(payload)
    intake = SOS.intake_digest(items, ctx_for(now), {})
    assert len(intake.events) == 2
    assert intake.emotion_used == 0.0
    assert all(event["emotion"] == 0.0 for event in intake.events)


def test_intake_digest_respects_the_daily_cap():
    now = 1_000_000.0
    items = digest_items(digest_payload())
    intake = SOS.intake_digest(items, ctx_for(now, daily_emotion_cap=0.5), {})
    assert intake.emotion_used == pytest.approx(0.5)
    assert intake.events[0]["emotion"] == pytest.approx(0.4)
    assert intake.events[1]["emotion"] == pytest.approx(0.1), "额度只剩 0.1"

    # 额度已经用掉 0.4：这一轮只补得上 0.1，第二条一分钱都拿不到
    spent = SOS.intake_digest(items, ctx_for(now, day_used=0.4, daily_emotion_cap=0.5), {})
    assert spent.emotion_used == pytest.approx(0.1)
    assert spent.skipped_budget == 1, "完全没拿到情绪的那条要计数"
    # 额度用光：仍记事，但不再产生情绪
    broke = SOS.intake_digest(items, ctx_for(now, day_used=0.5, daily_emotion_cap=0.5), {})
    assert len(broke.events) == 2 and broke.emotion_used == 0.0
    assert broke.skipped_budget == 2


def test_intake_digest_include_quote_toggle():
    now = 1_000_000.0
    items = digest_items(digest_payload())
    with_quote = SOS.intake_digest(items, ctx_for(now, include_quote=True), {})
    assert "原话" in with_quote.events[0]["text"]
    without = SOS.intake_digest(items, ctx_for(now), {})
    assert "原话" not in without.events[0]["text"]


def test_intake_live_aggregates_a_crowd_into_one_event():
    now = 1_000_000.0
    signals = [
        {"at": now - 60, "session_id": "g1", "is_group": True, "mentioned": True, "text_len": 5},
        {"at": now - 30, "session_id": "g2", "is_group": True, "mentioned": True, "text_len": 5},
        {"at": now - 10, "session_id": "p1", "is_group": False, "mentioned": False, "text_len": 2},
    ]
    intake = SOS.intake_live(signals, ctx_for(now), {})
    assert len(intake.events) == 1, "41 个会话不该变成 41 条经历"
    event = intake.events[0]
    assert event["label"] == SOS.LIVE_LABEL
    assert "有人叫我" in event["text"] and "3 个会话" in event["text"]
    assert event["at"] == now - 10, "时间取最新那条信号"
    assert event["emotion"] == pytest.approx(0.3 + 0.1 * 2)
    assert event["energy"] == 0.0


def test_intake_live_dedupes_sessions_and_respects_the_daily_limit():
    now = 1_000_000.0
    seen: dict[str, float] = {}
    first = SOS.intake_live(
        [{"at": now, "session_id": "g1", "is_group": True, "mentioned": False, "text_len": 3}],
        ctx_for(now), seen,
    )
    assert len(first.events) == 1
    again = SOS.intake_live(
        [{"at": now + 60, "session_id": "g1", "is_group": True, "mentioned": False, "text_len": 3}],
        ctx_for(now + 60), seen,
    )
    assert again.events == [], "同一会话同一生活日只算一次"

    second = SOS.intake_live(
        [{"at": now + 120, "session_id": "g2", "is_group": True, "mentioned": False, "text_len": 3}],
        ctx_for(now + 120), seen,
    )
    assert len(second.events) == 1, "上限是 2 条/生活日"
    third = SOS.intake_live(
        [{"at": now + 180, "session_id": "g3", "is_group": True, "mentioned": False, "text_len": 3}],
        ctx_for(now + 180), seen,
    )
    assert third.events == [] and third.skipped_seen == 1


def test_asleep_records_without_emotion():
    """真机决定：睡觉时照常记录（醒来能看到有人找过她），但情绪不吃这笔账。"""

    now = 1_000_000.0
    items = digest_items(digest_payload())
    ctx = ctx_for(now, activity=A.SLEEP, asleep=True)
    intake = SOS.intake_digest(items, ctx, {})
    assert len(intake.events) == 2, "记录照旧"
    assert intake.emotion_used == 0.0
    assert all(event["emotion"] == 0.0 for event in intake.events)

    live = SOS.intake_live(
        [{"at": now, "session_id": "g1", "is_group": True, "mentioned": True, "text_len": 3}],
        ctx, {},
    )
    assert len(live.events) == 1 and live.events[0]["emotion"] == 0.0


def test_asleep_can_skip_recording_entirely():
    now = 1_000_000.0
    items = digest_items(digest_payload())
    ctx = ctx_for(now, activity=A.SLEEP, asleep=True, record_while_asleep=False)
    assert SOS.intake_digest(items, ctx, {}).events == []
    assert SOS.intake_live(
        [{"at": now, "session_id": "g1", "is_group": True, "mentioned": True, "text_len": 3}],
        ctx, {},
    ).events == []
    # 反过来：允许睡眠影响情绪时，情绪照给
    awake_policy = ctx_for(now, activity=A.SLEEP, asleep=True, emotion_while_asleep=True)
    assert SOS.intake_digest(items, awake_policy, {}).emotion_used > 0


# ================= D. 维护与展示 =================


def test_prune_seen_keeps_the_newest_entries():
    seen = {f"k{i}": float(i) for i in range(10)}
    SOS.prune_seen(seen, keep=3)
    assert sorted(seen) == ["k7", "k8", "k9"]


def test_prune_daily_keeps_the_newest_life_days():
    daily = {f"2026-09-{day:02d}": 0.0 for day in range(1, 11)}
    SOS.prune_daily(daily, keep=2)
    assert sorted(daily) == ["2026-09-09", "2026-09-10"]


def test_sanitize_seen_drops_hostile_entries():
    assert SOS.sanitize_seen({"a": 1.0, "b": "abc", "c": float("nan"), "": 2.0}) == {"a": 1.0}
    assert SOS.sanitize_seen("不是字典") == {}
    assert SOS.sanitize_seen({"a": True}) == {}, "bool 不是时间戳"


def test_social_lines_never_make_not_connected_look_fine():
    assert "未接入" in SOS.social_lines(None, enabled=False)[0]
    lines = SOS.social_lines(SOS.unavailable("调用失败", "RPCError: 找不到提供方"),
                             enabled=True)
    assert "未取到" in lines[0] and "调用失败" in lines[0] and "找不到提供方" in lines[0]
    ok = SOS.SocialStatus(ok=True, fetched_at=1000.0, latest_day="2026-10-01",
                          latest_generated_at="2026-10-01 23:40:00", item_count=2)
    lines = SOS.social_lines(ok, enabled=True, today_events=2, emotion_used=0.6,
                             daily_cap=1.5, now=1600.0)
    assert "已接入" in lines[0] and "2026-10-01" in lines[0] and "10 分钟前" in lines[0]
    assert "0.6/1.5" in lines[1]


def test_social_lines_explain_a_connected_but_empty_digest():
    """「接通了但没内容」要说清原因，不能显示成看不懂的「最新  —」。"""

    empty = SOS.SocialStatus(ok=True, reason="还没有任何日记存档", fetched_at=1000.0,
                             item_count=0)
    lines = SOS.social_lines(empty, enabled=True, now=1000.0)
    assert "已接入" in lines[0] and "暂时没有内容" in lines[0]
    assert "还没有任何日记存档" in lines[0]


# ================= E. life_sim 侧的状态写入 =================


def _sim_config() -> S.SimConfig:
    return S.SimConfig()


def test_append_social_event_clamps_emotion_and_keeps_energy_untouched():
    state = S.LifeState()
    state.emotion = 9.9
    state.energy = 5.0
    state.inertia_until = 123.0
    S.append_social_event(state, {"at": 1.0, "label": "和阿岚聊天", "emotion": 1.0,
                                  "energy": 0.0, "text": "x"}, config=_sim_config())
    assert state.emotion == pytest.approx(S.SimConfig().emotion_max), "情绪要夹到上限"
    assert state.energy == pytest.approx(5.0)
    assert state.inertia_until == pytest.approx(123.0), "别人说话不该冻结她的情绪回归"
    assert state.materials == [], "社交经历不产素材（那是主动开口那条链路的事）"
    assert len(state.recent_events) == 1


def test_from_dict_sanitizes_social_maps():
    state = S.LifeState.from_dict({
        "social_seen": {"good": 1.0, "bad": "abc", "nan": float("nan")},
        "social_daily": {"2026-10-01": 0.5, "坏": None},
        "recent_events": [{"at": 1.0, "label": "有人找我", "emotion": 0.2, "source": "social"}],
    })
    assert state.social_seen == {"good": 1.0}
    assert state.social_daily == {"2026-10-01": 0.5}
    assert state.recent_events[0]["source"] == "social"


# ================= F. 桥接：取数与入库 =================


def test_refresh_social_calls_the_read_only_api_and_parses():
    async def run():
        module, plugin, host = _make_plugin(social={"enabled": True})
        host.api_returns[TARGET] = digest_payload()
        now = time.time()
        await plugin._refresh_social(now)
        calls = host.calls_of("api.call")
        assert len(calls) == 1 and calls[0]["api_name"] == TARGET
        assert calls[0]["version"] == "1" and calls[0]["args"] == {"days": 2}
        assert plugin._social is not None and plugin._social.ok
        assert plugin._social.item_count == 2
        assert plugin._social.latest_day == "2026-10-01"
        assert [item.what for item in plugin._social_items] == [
            "说起她换了个新键盘", "群里在约周末爬山",
        ]

    asyncio.run(run())


def test_refresh_social_is_rate_limited_and_disabled_means_no_calls():
    async def run():
        module, plugin, host = _make_plugin(social={"enabled": True})
        host.api_returns[TARGET] = digest_payload()
        now = time.time()
        await plugin._refresh_social(now)
        await plugin._refresh_social(now + 60)
        assert len(host.calls_of("api.call")) == 1
        await plugin._refresh_social(now + 31 * 60)
        assert len(host.calls_of("api.call")) == 2

        _, off_plugin, off_host = _make_plugin(social={"enabled": False})
        await off_plugin._refresh_social(now)
        assert off_host.calls_of("api.call") == []
        assert off_plugin._social is None

    asyncio.run(run())


def test_refresh_social_degrades_and_backs_off_when_provider_missing():
    async def run():
        module, plugin, host = _make_plugin(social={"enabled": True})  # 没登记返回
        now = time.time()
        with _Capture(module) as cap:
            await plugin._refresh_social(now)
            assert plugin._social is not None and plugin._social.ok is False
            assert plugin._social.reason == "接口不可用"
            assert "未找到 API 提供方插件" in plugin._social.detail
            await plugin._refresh_social(now + 60)          # 退避窗口内不重试
            assert len(host.calls_of("api.call")) == 1
            assert "读取日记摘要失败" in cap.text(logging.WARNING)
            assert cap.text(logging.WARNING).count("读取日记摘要失败") == 1
        assert "未取到" in plugin._render_status(now)
        assert "未找到 API 提供方插件" in plugin._render_status(now)

    asyncio.run(run())


def test_refresh_social_timeout_degrades_instead_of_hanging():
    async def run():
        module, plugin, host = _make_plugin(social={"enabled": True,
                                                    "api_timeout_seconds": 1})

        async def never_returns(*args, **kwargs):
            await asyncio.sleep(30)

        plugin.ctx.api.call = never_returns
        started = time.time()
        await plugin._refresh_social(started)
        assert time.time() - started < 5, "超时没生效会把生活循环拖住"
        assert plugin._social.ok is False and plugin._social.reason == "超时"

    asyncio.run(run())


def test_refresh_social_cancellation_propagates():
    async def run():
        module, plugin, host = _make_plugin(social={"enabled": True})

        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        plugin.ctx.api.call = cancelled
        with pytest.raises(asyncio.CancelledError):
            await plugin._refresh_social(time.time())

    asyncio.run(run())


def test_intake_social_appends_events_and_spends_the_daily_budget():
    async def run():
        # 本用例钉的是**额度记账**本身，所以显式关掉 v1.16.2 的孤独系数
        # （插件默认开；不关的话 0.4+0.4 会被孤独基线 4.0 放大成 0.86，
        #  测出来的就不再是基础额度了）。系数自己有专项用例。
        module, plugin, host = _make_plugin(
            social={"enabled": True}, mood={"loneliness_social_scaling": False}
        )
        host.api_returns[TARGET] = digest_payload()
        # 夹具的 generated_at 固定在 2026-10-01 23:40：now 必须与它对齐在 24h
        # 新鲜度窗口内，否则真实时钟漂出窗口后情绪额度恒为 0（2026-10-03 踩到的
        # 时间炸弹）。用与插件相同的换算函数构造固定 now，用例从此与时钟无关。
        now = SOS.local_stamp_to_epoch("2026-10-02 12:00:00", tz_offset_minutes=TZ)
        await plugin._refresh_social(now)
        plugin._state.day_key = "2026-10-01"
        plugin._state.activity = A.DAILY
        plugin._intake_social(now, plugin._sim_config())

        assert len(plugin._state.recent_events) == 2
        assert plugin._state.social_daily["2026-10-01"] == pytest.approx(0.8)
        assert plugin._social_today_count == 2
        # 再跑一轮（同一批摘要）不会重复入库
        plugin._intake_social(now + 600, plugin._sim_config())
        assert len(plugin._state.recent_events) == 2
        assert plugin._state.social_daily["2026-10-01"] == pytest.approx(0.8)
        # 卡片如实显示额度
        card = plugin._render_status(now)
        assert "社交经历" in card and "0.8/1.5" in card

    asyncio.run(run())


def test_intake_social_resets_the_counter_on_a_new_life_day():
    async def run():
        module, plugin, host = _make_plugin(social={"enabled": True})
        host.api_returns[TARGET] = digest_payload()
        now = time.time()
        await plugin._refresh_social(now)
        plugin._state.day_key = "2026-10-01"
        plugin._state.activity = A.DAILY
        plugin._intake_social(now, plugin._sim_config())
        assert plugin._social_today_count == 2

        plugin._state.day_key = "2026-10-02"
        plugin._intake_social(now + 86400, plugin._sim_config())
        assert plugin._social_today_count == 0, "新生活日的计数要归零"
        assert plugin._social_today_day == "2026-10-02"

    asyncio.run(run())


def test_intake_social_records_live_signals_while_asleep_without_emotion():
    async def run():
        module, plugin, host = _make_plugin(social={"enabled": True})
        plugin._state.day_key = "2026-10-01"
        plugin._state.activity = A.SLEEP
        plugin._state.emotion = 5.0
        plugin._social_inbox.append(
            {"at": time.time(), "session_id": "g1", "is_group": True,
             "mentioned": True, "text_len": 5}
        )
        plugin._intake_social(time.time(), plugin._sim_config())
        assert len(plugin._state.recent_events) == 1
        event = plugin._state.recent_events[0]
        assert event["label"] == SOS.LIVE_LABEL and event["emotion"] == 0.0
        assert plugin._state.emotion == pytest.approx(5.0), "睡眠中不吃社交情绪"
        assert plugin._social_inbox == deque_empty(), "信号缓冲要被取空"

    asyncio.run(run())


def deque_empty():
    from collections import deque

    return deque()


def test_social_intake_never_writes_frequency():
    """社交经历只写她的经历与情绪：整条链路都不许碰 frequency.set_adjust。"""

    async def run():
        module, plugin, host = _make_plugin(social={"enabled": True})
        host.api_returns[TARGET] = digest_payload()
        now = time.time()
        await plugin._refresh_social(now)
        plugin._state.day_key = "2026-10-01"
        plugin._intake_social(now, plugin._sim_config())
        plugin._render_status(now)
        assert host.calls_of("frequency.set_adjust") == []
        assert host.calls_of("frequency.get_adjust") == []

    asyncio.run(run())


def test_note_session_buffers_signals_only_when_enabled():
    async def run():
        module, plugin, host = _make_plugin(social={"enabled": True})
        result = await plugin.note_session(
            message={"session_id": "g1", "group_id": "123", "processed_plain_text": "麦麦",
                     "is_mentioned": True}
        )
        assert result == {"action": "continue"}, "这个钩子必须永远返回 continue"
        assert len(plugin._social_inbox) == 1
        assert plugin._social_inbox[0]["session_id"] == "g1"

        await plugin.note_session(message={"session_id": "g2", "processed_plain_text": "/生活",
                                           "is_command": True})
        assert len(plugin._social_inbox) == 1, "命令不该变成她的经历"

        _, off_plugin, _ = _make_plugin(social={"enabled": False})
        await off_plugin.note_session(message={"session_id": "g3",
                                               "processed_plain_text": "在吗"})
        assert len(off_plugin._social_inbox) == 0

    asyncio.run(run())


def test_a_tick_puts_social_experiences_into_her_recent_experiences():
    """端到端：一次 ``_sim_tick`` 之后，她的「近期经历」里真的能看到人际往来。

    这才是这一维存在的意义——经历最终要进活动决策的提示词（``recent_event_tiers``
    就是提示词用的那层）。
    """

    async def run():
        module, plugin, host = _make_plugin(social={"enabled": True})
        now = time.time()
        host.api_returns[TARGET] = digest_payload(
            date="2026-10-01",
            generated_at=S.local_datetime(now, TZ).strftime("%Y-%m-%d %H:%M:%S"),
        )
        plugin._state.day_key = "2026-10-01"
        plugin._state.activity = A.DAILY
        plugin._state.last_tick_at = now - 600
        plugin._social_inbox.append(
            {"at": now - 60, "session_id": "g1", "is_group": True,
             "mentioned": True, "text_len": 5}
        )
        await plugin._sim_tick()

        tiers = S.recent_event_tiers(
            plugin._state, now=time.time(), limit=8, tz_offset_minutes=TZ
        )
        rendered = [line for _, group in tiers for line in group]
        assert any("和阿岚聊天" in line for line in rendered), rendered
        assert any(SOS.LIVE_LABEL in line for line in rendered), rendered
        assert len(plugin._state.recent_events) == 3

    asyncio.run(run())


def test_social_config_is_declared_with_safe_defaults():
    """默认关闭：不装日记插件的用户什么都不该发生。"""

    from fakehost import get_default_config

    _, plugin, _ = _make_plugin()
    config = get_default_config(type(plugin).config_model)["social"]
    assert config["enabled"] is False
    assert config["api_plugin_id"] == "org.orge-8.better-diary"
    assert config["api_name"] == "get_day_digest"
    assert config["daily_emotion_cap"] > 0
    assert config["record_while_asleep"] is True
    assert config["emotion_while_asleep"] is False
    assert config["include_quote"] is False


def test_social_state_is_persisted_and_survives_a_round_trip():
    """新增状态字段必须能落盘再读回（否则每次重启都会重复记同一件事）。"""

    async def run():
        # 同上：落盘往返用例不该被孤独系数改变额度数值
        module, plugin, host = _make_plugin(
            social={"enabled": True}, mood={"loneliness_social_scaling": False}
        )
        host.api_returns[TARGET] = digest_payload()
        # 与上一用例同理：now 与夹具的 generated_at 对齐，时钟漂移不再影响结果。
        now = SOS.local_stamp_to_epoch("2026-10-02 12:00:00", tz_offset_minutes=TZ)
        await plugin._refresh_social(now)
        plugin._state.day_key = "2026-10-01"
        plugin._intake_social(now, plugin._sim_config())
        plugin._save_state()

        raw = json.loads(plugin._state_path().read_text(encoding="utf-8"))
        assert raw["social_daily"]["2026-10-01"] == pytest.approx(0.8)
        assert any(key.startswith("digest!2026-10-01!") for key in raw["social_seen"])

        restored = S.LifeState.from_dict(raw)
        assert restored.social_seen == plugin._state.social_seen
        assert restored.social_daily == plugin._state.social_daily

    asyncio.run(run())
