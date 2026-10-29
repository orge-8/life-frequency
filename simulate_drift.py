#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""身体驱动作息仿真（v1.24.0）：跑 N 天确定性 tick，看睡/醒形态是否稳定。

    python simulate_drift.py --days 10 --eager-after-hours 3.7

v1.24.0 起睡不睡只看**困意**（``sleep_need``，外加昼夜偏置与生病），
``sleep_window`` / ``min_awake_hours_per_day`` / ``energy_full_wake*`` 这些
钟表与电量闸门都已退役。所以本脚本不再扫「清醒下限 × 睡眠目标」的组合，
而是回答三个结构性问题：

* **无模型提议**（``decision=None``）时她能不能自己睡着、按时醒（确定性兜底）；
* 模型**很早**就想睡（``--eager-after-hours``）时，强制层会不会拒绝并把睡意
  攒到入阈——模型不能绕过身体；
* 多日循环里每觉时长与就寝时刻是否稳定，会不会越睡越碎。

两种替身都不调 LLM、不联网、固定种子，可复现。真机的活动分布比这两条规则线
更丰富，所以本脚本的结论是**结构性判定**，不能替代真机观察。
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
    """作息相关参数取 v1.24.0 默认值，情绪/体力曲线取真机同源那一份。"""

    base = dict(
        tz_offset_minutes=TZ,
        tick_seconds=TICK,
        day_boundary_hour=12,
        offline_gap_minutes=30,
        max_sleep_hours=12.0,
        min_sleep_minutes=180,
        min_dwell_minutes=60,
        sleep_need_onset=0.62,
        sleep_need_wake=0.16,
        sleep_deferral_minutes=15,
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
    """跑 N 天，返回睡眠分段 / 周期 / 困意 / 缺觉四类指标。"""

    start = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc).timestamp() - TZ * 60
    now = start
    state = S.new_state(now=start, config=config, energy=energy0)
    rng = random.Random(seed)

    prev = state.activity
    sleep_start = start
    last_wake = start
    bs_start: float | None = None

    episodes: list[tuple[float, float]] = []
    naps: list[float] = []
    bedtimes: list[float] = []
    stalls: list[float] = []
    min_energy = 99.0
    need_at_wake: list[float] = []
    max_awake_hours = 0.0

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

        if state.activity != prev:
            if prev == A.SLEEP:
                episodes.append((sleep_start, now))
                need_at_wake.append(float(state.sleep_need))
                last_wake = now
            if prev == A.NAP:
                naps.append((now - sleep_start) / 3600.0)
                last_wake = now
            if prev == A.BEFORE_SLEEP and bs_start is not None:
                stalls.append((now - bs_start) / 3600.0)
                bs_start = None
            if state.activity == A.SLEEP:
                sleep_start = now
                bedtimes.append(now)
                max_awake_hours = max(max_awake_hours, (now - last_wake) / 3600.0)
            if state.activity == A.BEFORE_SLEEP:
                bs_start = now
            prev = state.activity

    cycles = [
        (bedtimes[i + 1] - bedtimes[i]) / 3600.0 for i in range(len(bedtimes) - 1)
    ]
    spans = [(e - s) / 3600.0 for s, e in episodes]
    deficit, deficit_reason = S.sleep_deficit_minutes(state, now=now, config=config)
    return {
        "觉数": len(episodes),
        "每觉时长": spans,
        "周期": cycles,
        "日均睡眠": sum(spans) / days,
        "就寝时刻": [hhmm(b)[6:] for b in bedtimes],
        "小睡": naps,
        "体力最低": min_energy,
        "最长清醒": max_awake_hours,
        "醒时困意": need_at_wake,
        "滞留段": stalls,
        "缺觉分钟": deficit,
        "缺觉原因": deficit_reason,
        "体力上限": float(state.energy_cap),
    }


def show(title: str, r: dict, days: int) -> None:
    print(f"\n=== {title} ===")
    deficit = r["缺觉分钟"]
    deficit_text = (
        f"记录不足（{r['缺觉原因']}）" if deficit is None else f"{deficit / 60.0:.1f} h"
    )
    print(f"  觉数 {r['觉数']} ｜ 日均睡眠 {r['日均睡眠']:.2f} h ｜ "
          f"累计缺觉 {deficit_text} ｜ 体力上限 {r['体力上限']:.1f}")
    if r["每觉时长"]:
        print(f"  每觉时长 中位 {statistics.median(r['每觉时长']):.2f} h "
              f"（{min(r['每觉时长']):.2f} – {max(r['每觉时长']):.2f}）")
    if r["醒时困意"]:
        print(f"  醒来困意 中位 {statistics.median(r['醒时困意']):.2f} "
              f"（越高说明醒得越早）")
    if r["周期"]:
        print(f"  周期     中位 {statistics.median(r['周期']):.2f} h "
              f"（{min(r['周期']):.2f} – {max(r['周期']):.2f}）  ← 目标 24.00")
    print(f"  就寝时刻 {r['就寝时刻'][:10]}")
    print(f"  体力最低 {r['体力最低']:.2f} ｜ 最长连续清醒 {r['最长清醒']:.1f} h")
    if r["小睡"]:
        print(f"  小睡 {len(r['小睡'])} 次 ｜ 中位 {statistics.median(r['小睡']) * 60:.0f} 分钟")
    else:
        print("  小睡 无")
    if r["滞留段"]:
        print(f"  「准备睡觉」滞留 中位 {statistics.median(r['滞留段']):.2f} h "
              f"（最长 {max(r['滞留段']):.2f} h，共 {len(r['滞留段'])} 段）")
    else:
        print("  「准备睡觉」滞留 无")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 —— 重定向/老解释器时保持原样
            pass

    ap = argparse.ArgumentParser(description="身体驱动作息仿真（v1.24.0，只看困意）")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--eager-after-hours", type=float, default=3.7,
                    help="模型替身：清醒满 N 小时就提议入睡；0 = 关闭（纯确定性底线）")
    args = ap.parse_args()

    config = base_config()
    print(f"{args.days} 天 ｜ seed {args.seed} ｜ tick {TICK}s ｜ "
          f"入睡阈 {config.sleep_need_onset:g} / 醒阈 {config.sleep_need_wake:g}")

    show("确定性底线（decision=None）",
         simulate(config, days=args.days, seed=args.seed, eager_after_hours=0.0),
         args.days)
    if args.eager_after_hours > 0:
        show(f"eager 模型（清醒 {args.eager_after_hours}h 即想睡）",
             simulate(config, days=args.days, seed=args.seed,
                      eager_after_hours=args.eager_after_hours),
             args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
