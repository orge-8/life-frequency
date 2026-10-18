#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作息漂移仿真（D16 验证）：跑 N 天确定性 tick，比较不同「清醒下限 / 睡眠目标」组合。

    python simulate_drift.py --days 10 --eager-after-hours 3.7

**为什么需要它**：``min_awake_hours_per_day``（清醒下限）与 ``energy_full_wake_min_hours``
（睡眠目标）都是**下限**，不是固定时长。只有当模型总在下界处提议入睡时，周期才等于两者之和。
真机观测（2026-10-15 02:38 醒来、3.7 小时后即提议入睡）说明模型确实「能睡就睡」，
所以本脚本用两种模型替身各跑一遍，给出上下界：

* ``decision=None``：**模型无提议**，走硬约束送睡（``life_sim.enforce_and_apply``）
  —— 这是周期的理论下界，不含任何模型偏好；
* eager 模式（``--eager-after-hours``）：清醒满 N 小时后模型提议 ``before_sleep``，
  进入后立刻提议 ``sleep`` —— 复现真机的「想睡却被清醒下限拒绝」滞留，用来量滞留时长。

两种都不调 LLM、不联网、固定种子，可复现。真机的活动分布比这两条规则线更丰富，
所以本脚本的结论是**结构性判定**（周期能否锁到 24h、体力是否会击穿硬底），
不能替代真机观察。

参数默认取真机 ``config.toml``（2026-10-15 贴出）的实际值，不是仓库默认值。
"""

from __future__ import annotations

import argparse
import pathlib
import random
import statistics
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_activity as A  # noqa: E402
import life_sim as S  # noqa: E402

TZ = 480
TICK = 600

#: 真机曲线（与 config.toml 同源；这里手抄一份，脚本不 import plugin.py 以免拖进 SDK）
FATIGUE_RAMP = ((12.0, 0.0), (16.0, -0.15), (20.0, -0.4), (24.0, -0.7))
DIURNAL = ((300.0, -0.3), (780.0, 0.15), (1200.0, 0.3), (1380.0, 0.0))


def base_config(**overrides) -> S.SimConfig:
    """真机 config.toml 的仿真等价物（睡眠相关 + 情绪体力曲线）。"""

    base = dict(
        tz_offset_minutes=TZ,
        tick_seconds=TICK,
        day_boundary_hour=12,
        offline_gap_minutes=30,
        sleep_window=(0, 1439),
        sleep_window_text="00:00-23:59",
        sleep_energy_threshold=3.0,
        max_sleep_hours=10.0,
        min_sleep_minutes=120,
        min_dwell_minutes=60,
        sleep_hard_floor=0.5,
        energy_full_wake=True,
        fatigue_ramp_curve=FATIGUE_RAMP,
        baseline_diurnal_curve=DIURNAL,
        low_energy_drain_multiplier=1.25,
        low_energy_threshold=3.0,
        emotion_fatigue_penalty=0.3,
        emotion_fatigue_threshold=3.0,
        inertia_minutes=40,
        recover_per_tick=0.2,
        sleep_recover_multiplier=2.0,
        recover_ratio_per_tick=0.08,
        recover_min_step=0.05,
        inertia_scale_enabled=True,
        afterglow_decay=True,
        afterglow_gain=0.0,
        emotion_impact_scaling=True,
    )
    base.update(overrides)
    return S.SimConfig(**base)


def hhmm(ts: float) -> str:
    return S.local_datetime(ts, TZ).strftime("%m-%d %H:%M")


def simulate(config: S.SimConfig, *, days: int, seed: int,
             eager_after_hours: float, energy0: float = 5.0) -> dict:
    """跑 N 天，返回周期 / 睡眠 / 体力 / 滞留四类指标。"""

    start = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc).timestamp() - TZ * 60
    now = start
    state = S.new_state(now=start, config=config, energy=energy0)
    rng = random.Random(seed)

    prev = state.activity
    sleep_start = start
    last_wake = start
    bs_start: float | None = None

    episodes: list[tuple[float, float]] = []
    bedtimes: list[float] = []
    stalls: list[float] = []
    min_energy = 99.0
    hard_floor_hits = 0

    for _ in range(days * 24 * 6):
        now += TICK
        state = S.settle(state, now=now, config=config, events=(), rng=rng)

        decision = None
        if eager_after_hours > 0 and state.activity not in (A.SLEEP, A.NAP):
            if state.activity == A.BEFORE_SLEEP:
                decision = A.ActivityDecision(A.SLEEP, "", A.SOURCE_LLM, "想睡")
            elif (now - last_wake) / 3600.0 >= eager_after_hours:
                decision = A.ActivityDecision(A.BEFORE_SLEEP, "", A.SOURCE_LLM, "想睡了")

        state = S.enforce_and_apply(state, now=now, config=config, decision=decision)

        min_energy = min(min_energy, float(state.energy))
        if float(state.energy) <= float(config.sleep_hard_floor):
            hard_floor_hits += 1

        if state.activity != prev:
            if prev == A.SLEEP:
                episodes.append((sleep_start, now))
                last_wake = now
            if prev == A.BEFORE_SLEEP and bs_start is not None:
                stalls.append((now - bs_start) / 3600.0)
                bs_start = None
            if state.activity == A.SLEEP:
                sleep_start = now
                bedtimes.append(now)
            if state.activity == A.BEFORE_SLEEP:
                bs_start = now
            prev = state.activity

    cycles = [
        (bedtimes[i + 1] - bedtimes[i]) / 3600.0 for i in range(len(bedtimes) - 1)
    ]
    spans = [(e - s) / 3600.0 for s, e in episodes]
    total_sleep = sum(spans)
    return {
        "觉数": len(episodes),
        "每觉时长": spans,
        "周期": cycles,
        "日均睡眠": total_sleep / days,
        "就寝时刻": [hhmm(b)[6:] for b in bedtimes],
        "体力最低": min_energy,
        "击穿硬底次数": hard_floor_hits,
        "滞留段": stalls,
        "熬夜天数": state.sleep_debt_nights,
        "体力上限": float(state.energy_cap),
    }


def show(title: str, r: dict, days: int) -> None:
    print(f"\n=== {title} ===")
    print(f"  觉数 {r['觉数']} ｜ 日均睡眠 {r['日均睡眠']:.2f} h ｜ "
          f"熬夜天数 {r['熬夜天数']} ｜ 体力上限 {r['体力上限']:.1f}")
    if r["每觉时长"]:
        print(f"  每觉时长 中位 {statistics.median(r['每觉时长']):.2f} h "
              f"（{min(r['每觉时长']):.2f} – {max(r['每觉时长']):.2f}）")
    if r["周期"]:
        print(f"  周期     中位 {statistics.median(r['周期']):.2f} h "
              f"（{min(r['周期']):.2f} – {max(r['周期']):.2f}）  ← 目标 24.00")
    print(f"  就寝时刻 {r['就寝时刻'][:10]}")
    print(f"  体力最低 {r['体力最低']:.2f} ｜ 击穿 sleep_hard_floor 次数 {r['击穿硬底次数']}")
    if r["滞留段"]:
        print(f"  「准备睡觉」滞留 中位 {statistics.median(r['滞留段']):.2f} h "
              f"（最长 {max(r['滞留段']):.2f} h，共 {len(r['滞留段'])} 段）")
    else:
        print("  「准备睡觉」滞留 无")


def parse_window(text: str) -> tuple[int, int]:
    """``"19:00-11:00"`` ⇒ ``(1140, 660)``（start > end 表示跨零点）。"""

    left, right = text.split("-")
    h1, m1 = left.split(":")
    h2, m2 = right.split(":")
    return int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)


def main() -> int:
    ap = argparse.ArgumentParser(description="作息漂移仿真（清醒下限 × 睡眠目标）")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--window", default="00:00-23:59",
                    help="睡眠窗口，如 19:00-11:00（跨零点）")
    ap.add_argument("--eager-after-hours", type=float, default=3.7,
                    help="模型替身：清醒满 N 小时就提议入睡；0 = 关闭（纯确定性底线）")
    args = ap.parse_args()

    window = parse_window(args.window)
    scenarios = [
        ("当前真机  min_awake=8.0  target=6.5", dict(
            min_awake_hours_per_day=8.0, energy_full_wake_min_hours=6.5)),
        ("仅调目标  min_awake=8.0  target=8.0", dict(
            min_awake_hours_per_day=8.0, energy_full_wake_min_hours=8.0)),
        ("D16 方案  min_awake=16.0 target=8.0", dict(
            min_awake_hours_per_day=16.0, energy_full_wake_min_hours=8.0)),
    ]

    print(f"睡眠窗口 {args.window} ｜ {args.days} 天 ｜ seed {args.seed}")

    for label, overrides in scenarios:
        overrides = dict(overrides)
        overrides["sleep_window"] = window
        overrides["sleep_window_text"] = args.window
        config = base_config(**overrides)
        show(f"{label} ｜ 确定性底线（decision=None）",
             simulate(config, days=args.days, seed=args.seed, eager_after_hours=0.0),
             args.days)
        if args.eager_after_hours > 0:
            show(f"{label} ｜ eager 模型（清醒 {args.eager_after_hours}h 即想睡）",
                 simulate(config, days=args.days, seed=args.seed,
                          eager_after_hours=args.eager_after_hours),
                 args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
