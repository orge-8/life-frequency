# -*- coding: utf-8 -*-
"""L3：内心维度的时长锚点必须取自 ``settle`` **之前**（v1.17.1 回归）。

背景（真机故障，两份诊断报告对照得出）：``plugin._sim_tick`` 先调
``life_sim.settle``，再用 ``now - state.last_tick_at`` 算内心维度的时长。但
``settle`` 的**每一个出口**都会把 ``last_tick_at`` 推进到 ``now``（正常步进 /
``elapsed <= 0`` / 停机间隙），于是那个差分恒为 0，``mood_evolve`` 在
``hours <= 0`` 处直接 return ⇒ 压力 / 孤独 / 社交电量三个维度永远冻在初值。

真机证据：22:16（睡 80 分钟）与 00:22（睡 199 分钟）两个快照的内心维度都是
``3.0 / 0.0 / 0.0``，中间两小时一动不动——而睡眠本该按 1.0/小时回充电量。
连带后果：``battery_gate`` 恒返回 ``low_battery``，``[proactive] enabled = true``
形同虚设；``injection_lines`` 因电量恒 <3 而每天给每个会话注入「她今天社交得有点累」。

同一个坑在 ``life_sim.should_reseed`` 里已经被踩过一次（那里有注释警告），
本文件把它钉成可执行的判据，避免第三次。

钉住四件事：

1. ``settle`` 的三个出口都把 ``last_tick_at`` 推到 ``now``；
2. 「settle 之后取锚点」算出的时长恒为 0，mood 一动不动（**旧 bug 的复现**）；
3. 「settle 之前取锚点」算出的时长正确，睡眠能回充电量（**修好的行为**）；
4. 源码级守卫：锚点确实在 ``settle`` 之前取，且旧写法不再出现。
"""

import pathlib
import random

import pytest

import life_activity as A
import life_mood as M
import life_sim as S

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
TZ = 480
NOW = 1_700_000_000.0


def cfg(**overrides):
    base = dict(tz_offset_minutes=TZ)
    base.update(overrides)
    return S.SimConfig(**base)


def asleep_state(*, at: float = NOW) -> S.LifeState:
    """一个「正在睡觉」的状态，锚点定在 ``at``。"""

    state = S.LifeState()
    state.last_tick_at = at
    state.activity = A.SLEEP
    state.activity_since = at
    state.day_key = S.day_key_of(S.local_datetime(at, TZ), 12)
    return state


def _evolve(state: S.LifeState, *, minutes: float, now: float) -> None:
    """按睡眠结算一次内心维度（``activity`` 显式传 SLEEP：本文件只考时长口径，
    不考 ``settle`` 会不会改活动）。"""

    M.evolve(
        state,
        activity=A.SLEEP,
        minutes=minutes,
        had_contact=False,
        had_mention=False,
        policy=M.MoodPolicy(),
        last_contact_at=0.0,
        now=now,
    )


def test_settle_advances_anchor_on_every_exit():
    """1. 三个出口都把锚点推到 now——这是整条 bug 的前提。"""

    # 正常步进
    out = S.settle(asleep_state(), now=NOW + 1200, config=cfg(), events=[], rng=random.Random(1))
    assert out.last_tick_at == pytest.approx(NOW + 1200)

    # elapsed <= 0（同刻再推进一次）
    out = S.settle(asleep_state(), now=NOW, config=cfg(), events=[], rng=random.Random(1))
    assert out.last_tick_at == pytest.approx(NOW)

    # 停机间隙：超过 offline_gap_minutes 的宽限，走 _skip_offline_gap
    out = S.settle(
        asleep_state(),
        now=NOW + 6 * 3600,
        config=cfg(offline_gap_minutes=60),
        events=[],
        rng=random.Random(1),
    )
    assert out.last_tick_at == pytest.approx(NOW + 6 * 3600)


def test_anchor_after_settle_freezes_mood():
    """2. 旧 bug 的复现：settle 之后取锚点 ⇒ 时长 0 ⇒ 内心维度冻结。"""

    now = NOW + 1200
    out = S.settle(asleep_state(), now=now, config=cfg(), events=[], rng=random.Random(1))
    stale_minutes = max(0.0, (now - out.last_tick_at) / 60.0)
    assert stale_minutes == 0.0

    out.social_battery = 0.0
    _evolve(out, minutes=stale_minutes, now=now)
    # 睡了 20 分钟却一点没回血——真机「3.0 / 0.0 / 0.0 两小时不动」就是这么来的
    assert out.social_battery == 0.0


def test_anchor_before_settle_recovers_battery():
    """3. 修好的行为：settle 之前取锚点 ⇒ 时长正确 ⇒ 睡眠回血、电量闸放行。"""

    state = asleep_state()
    now = NOW + 1800  # 睡 30 分钟
    prev_tick_at = float(state.last_tick_at)
    out = S.settle(state, now=now, config=cfg(), events=[], rng=random.Random(1))

    fresh_minutes = max(0.0, (now - prev_tick_at) / 60.0)
    assert fresh_minutes == pytest.approx(30.0)

    out.social_battery = 0.0
    _evolve(out, minutes=fresh_minutes, now=now)
    # 1.0/小时 × 0.5 小时
    assert out.social_battery == pytest.approx(0.5)

    # 睡够两小时就能越过主动开口的硬闸（2.0），proactive 配置才真正生效
    long_state = asleep_state()
    long_now = NOW + 2 * 3600
    prev = float(long_state.last_tick_at)
    out2 = S.settle(long_state, now=long_now, config=cfg(), events=[], rng=random.Random(1))
    out2.social_battery = 0.0
    _evolve(out2, minutes=max(0.0, (long_now - prev) / 60.0), now=long_now)
    assert out2.social_battery >= M.BATTERY_MIN_PROACTIVE
    assert M.battery_gate(out2) != "low_battery"


def test_plugin_source_captures_anchor_before_settle():
    """4. 源码级守卫：锚点在 settle 之前取，mood 用那个锚点，旧写法不得复活。"""

    src = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")

    anchor = 'prev_tick_at = float(getattr(self._state, "last_tick_at", 0.0) or 0.0)'
    assert anchor in src, "锚点取值语句不见了"
    assert src.index(anchor) < src.index("self._state = settle("), "锚点必须在 settle 之前取"
    assert "minutes=mood_minutes" in src, "mood_evolve 必须用修复后的时长"
    assert "(now - self._state.last_tick_at) / 60.0" not in src, "旧写法（settle 之后取锚点）复活了"
