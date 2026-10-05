# -*- coding: utf-8 -*-
"""状态卡「外面的世界」几行的措辞回归。

真机反馈（2026-10-05，v1.8.0）：``/生活 状态`` 上出现了

    动态：降级（暂无推送记录）
    歌曲：降级（暂无歌曲命中记录）

可上游是**健康**的（``active=True``），只是还没有数据 —— 「还没数据」被写成了「坏了」。
本文件钉住三件事：

1. 上游契约里的「空窗」措辞 → ``已接入（…）``，不再误报劣化；
2. 真降级（记录不可读、探测未成功）仍必须是 ``降级``；
3. 未知措辞一律按 ``降级`` 显示（宁可误报，不可漏报）。
"""

import life_world as W

# ---------------------------------------------------------------- 小工具


def _card(**sources) -> list[str]:
    return W.world_lines(W.WorldStatus(**sources), enabled=True)


def _line(lines: list[str], label: str) -> str:
    for line in lines:
        if line.startswith(f"　{label}："):
            return line
    raise AssertionError(f"没有「{label}」这一行：{lines}")


def _push(ok=True, active=True, reason="", error="", items=()):
    return W.PushSource(ok=ok, active=active, reason=reason, error=error, items=tuple(items))


def _newcomer(ok=True, active=True, reason="", error="", items=()):
    return W.NewcomerSource(ok=ok, active=active, reason=reason, error=error, items=tuple(items))


def _song(ok=True, active=True, reason="", error="", items=()):
    return W.SongSource(ok=ok, active=active, reason=reason, error=error, items=tuple(items))


# ------------------------------------------------- 1. 空窗不是降级（本次修复）


def test_empty_push_log_reads_as_connected_not_degraded():
    lines = _card(pushes=_push(reason="暂无推送记录"))
    assert _line(lines, "动态") == "　动态：已接入（暂无推送记录）"


def test_window_empty_push_log_reads_as_connected():
    lines = _card(pushes=_push(reason="时间窗内没有推送记录"))
    assert _line(lines, "动态") == "　动态：已接入（时间窗内没有推送记录）"


def test_empty_newcomer_records_read_as_connected():
    for reason in ("暂无任何成员记录", "时间窗内没有新成员"):
        lines = _card(newcomers=_newcomer(reason=reason))
        assert _line(lines, "新人") == f"　新人：已接入（{reason}）"


def test_empty_song_records_read_as_connected():
    lines = _card(songs=_song(reason="暂无歌曲命中记录"))
    assert _line(lines, "歌曲") == "　歌曲：已接入（暂无歌曲命中记录）"


def test_healthy_empty_source_without_reason_is_also_connected():
    lines = _card(pushes=_push(reason=""))
    assert _line(lines, "动态") == "　动态：已接入（暂无推送记录）"


# ------------------------------------------------- 2. 真降级仍然叫降级


def test_unreadable_push_log_still_reads_as_degraded():
    reason = "推送记录不可读，已降级：磁盘只读"
    lines = _card(pushes=_push(reason=reason))
    assert _line(lines, "动态") == f"　动态：降级（{reason}）"


def test_unknown_reason_is_treated_as_degraded_conservatively():
    lines = _card(songs=_song(reason="上游返回了没见过的说明"))
    assert _line(lines, "歌曲") == "　歌曲：降级（上游返回了没见过的说明）"


def test_reason_with_items_keeps_degraded_wording():
    item = W.PushItem(uid="1", name="UP", dyn_type="video", title="t", url="u", at=1.0)
    lines = _card(pushes=_push(reason="部分条目被截断", items=(item,)))
    assert _line(lines, "动态") == "　动态：降级（部分条目被截断）"


def test_pending_live_probe_is_degraded_not_connected():
    live = W.LiveStatus(ok=True, active=True, reason="尚未探测成功")
    assert _line(_card(live=live), "直播") == "　直播：降级（尚未探测成功）"


# ------------------------------------------------- 3. 其余措辞不变


def test_never_fetched():
    assert W.world_lines(None, enabled=True) == ["外面的世界：未取过"]


def test_disabled():
    assert W.world_lines(None, enabled=False) == ["外面的世界：未接入（[world] enabled = false）"]


def test_api_error_shows_upstream_error_text():
    lines = _card(songs=_song(ok=False, error="未找到 API 提供方插件: org.mai-mai.cv-lyric-context"))
    assert _line(lines, "歌曲") == "　歌曲：未取到（未找到 API 提供方插件: org.mai-mai.cv-lyric-context）"


def test_upstream_not_ready_shows_upstream_wording():
    lines = _card(newcomers=_newcomer(active=False, reason="插件尚未完成启动（状态未加载）"))
    assert _line(lines, "新人") == "　新人：上游未启用（插件尚未完成启动（状态未加载））"


def test_live_not_configured():
    live = W.LiveStatus(ok=True, active=False, reason="live.room_id 未配置")
    assert _line(_card(live=live), "直播") == "　直播：上游未就绪（live.room_id 未配置）"


def test_live_normal_state_and_subscription_line_and_tally():
    live = W.LiveStatus(
        ok=True, active=True, reason="", anchor_name="共鸣电台", is_live=False, live_status_name="未开播"
    )
    subs = W.SubscriptionSource(ok=True, active=True, count=1, names=("共鸣电台_FMInfinity",))
    item = W.SongItem(name="一半一半", artist="鸟爷ToriSama", at=1.0)
    lines = _card(live=live, pushes=_push(items=()), songs=_song(items=(item,)), subscriptions=subs)
    assert lines[0] == "外面的世界：订阅 1 位 UP 主（共鸣电台_FMInfinity…）"
    assert _line(lines, "直播") == "　直播：共鸣电台 未开播（未开播）"
    assert _line(lines, "歌曲") == "　歌曲：已接入（1 条）"
    assert lines[-1].startswith("　今日已记 ")


# ------------------------------------------------- 4. 白名单本身钉住


def test_benign_reason_set_is_exactly_the_upstream_contract():
    """多一个/少一个都要有人来解释 —— 这是消费方唯一「假装没问题」的入口。"""

    assert W.BENIGN_EMPTY_REASONS == frozenset(
        {
            "暂无推送记录",
            "时间窗内没有推送记录",
            "暂无任何成员记录",
            "时间窗内没有新成员",
            "暂无歌曲命中记录",
        }
    )
