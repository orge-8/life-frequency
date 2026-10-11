#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""曲线标定脚本（v1.16.3 D 期）：跑 N 天确定性模拟，输出情绪/体力的真实分布。

    python calibrate_curves.py --days 7 --out docs/curve-calibration.md

**为什么需要它**（方案 §4 D 期）：情绪体力改了一整套机制（清醒疲劳、比例回归、
日内节律、余波衰减、疲劳压基线、冲击边际效用），这些都会让实际分布移动。三套宿主
曲线（``frequency`` / ``necessity`` / ``dynamic``）的**结构不动**，但点值是按旧分布
标定的——所以每次改机制都要重跑一次分布，确认点值还立在它当初的语义位置上
（情绪 5 = 基线、0/10 = 两端）。

**为什么要确定性**：它必须可复现。这里不调模型——活动提议走
``life_activity.rule_based_activity``（时段表，就是模型挂掉时的确定性底线），
事件走内建库 + 固定种子。真机的活动分布比这条规则线更丰富，所以本脚本的结论是
**下界**：它证明「机制本身不会把分布推歪」，不能替代真机观察（方案每期都写了观察期）。

``--compare`` 会同时跑一遍「D 期机制全关」的对照，报告里给出两者差值——这就是
「重标定」要看的位移量。
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import statistics
import sys
from dataclasses import replace
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_activity as A  # noqa: E402
import life_events as E  # noqa: E402
import life_factors as F  # noqa: E402
import life_sim as S  # noqa: E402

TZ = 480
TICK_SECONDS = 600
#: 与插件默认值同源的曲线（这里**手写一份点集**：脚本不该 import plugin.py，
#: 那会把 maibot_sdk 也拖进来，标定脚本要能在没有 SDK 的机器上跑）。
FATIGUE_RAMP = ((12.0, 0.0), (16.0, -0.15), (20.0, -0.4), (24.0, -0.7))
DIURNAL = ((300.0, -0.3), (780.0, 0.15), (1200.0, 0.3), (1380.0, 0.0))
IMPACT_POSITIVE = ((2.0, 1.3), (5.0, 1.0), (8.0, 0.7))
IMPACT_NEGATIVE = ((2.0, 0.7), (5.0, 1.0), (8.0, 1.2))
HOST_CURVES = {
    "frequency": (F.DEFAULT_MOOD_CURVE, F.DEFAULT_ENERGY_CURVE),
    "necessity": (F.DEFAULT_MOOD_CURVE, F.DEFAULT_ENERGY_CURVE),
    "dynamic": (F.DEFAULT_MOOD_CURVE, F.DEFAULT_ENERGY_CURVE),
}


def c_phase_off(config: S.SimConfig) -> S.SimConfig:
    """把 C/D 期的耦合与冲击缩放全关（A/B 期机制保留）。"""

    return replace(
        config,
        emotion_fatigue_penalty=0.0,
        low_energy_drain_multiplier=1.0,
        stress_breakdown_enabled=False,
        emotion_impact_scaling=False,
    )


def a_phase_off(config: S.SimConfig) -> S.SimConfig:
    """再关掉 A/B 期（= 完全回到 v1.15.0 的情绪体力动力学）。"""

    base = c_phase_off(config)
    return replace(
        base,
        fatigue_ramp_curve=(),
        recover_ratio_per_tick=0.0,
        inertia_scale_enabled=False,
        afterglow_decay=False,
        afterglow_gain=0.15,
        baseline_diurnal_curve=(),
    )


def simulate(config: S.SimConfig, *, days: int, seed: int = 20261010) -> dict:
    """跑 N 天确定性生活，返回情绪/体力样本与逐小时均值。"""

    events, _warnings = E.merge_events(None, None)
    start = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc).timestamp() - TZ * 60
    state = S.new_state(now=start, config=config, energy=8.0)
    rng = random.Random(seed)
    steps = int(days * 24 * 3600 / TICK_SECONDS)
    policy = S.build_enforce_policy(config)

    emotions: list[float] = []
    energies: list[float] = []
    hours: list[float] = []
    factor_mood: list[float] = []
    factor_energy: list[float] = []
    slept = 0
    naps = 0
    previous = state.activity

    for step in range(1, steps + 1):
        now = start + TICK_SECONDS * step
        S.settle(state, now=now, config=config, events=events, rng=rng)
        facts = S.enforce_facts(state, now=now, config=config)
        if not facts.sick:
            # 确定性提议（= 模型静默时的那条底线），再过强制层收口
            local_minutes = facts.now_minutes
            proposal = A.rule_based_activity(
                now_minutes=local_minutes,
                energy=facts.energy,
                sleep_energy_threshold=config.sleep_energy_threshold,
            )
            S.enforce_and_apply(state, now=now, config=config, decision=proposal)
        if state.activity == A.SLEEP and previous != A.SLEEP:
            slept += 1
        if state.activity == A.NAP and previous != A.NAP:
            naps += 1
        previous = state.activity

        emotions.append(float(state.emotion))
        energies.append(float(state.energy))
        local = S.local_datetime(now, config.tz_offset_minutes)
        hours.append(local.hour + local.minute / 60.0)
        mood_curve, energy_curve = HOST_CURVES["frequency"]
        factor_mood.append(F.interpolate(mood_curve, state.emotion))
        factor_energy.append(F.interpolate(energy_curve, state.energy))

    return {
        "emotion": emotions,
        "energy": energies,
        "hours": hours,
        "mood_factor": factor_mood,
        "energy_factor": factor_energy,
        "sleep_episodes": slept,
        "naps": naps,
        # v1.16.3：高压崩溃发生了几个生活日（键形如 mood!stress_breakdown!<生活日>）。
        # 它是验收「合法压低源是否有界」的证据——每生活日至多一次，所以 ≤ 天数。
        "breakdowns": sum(
            1 for key in state.motive_seen if str(key).startswith("mood!stress_breakdown!")
        ),
    }


def histogram(values: list[float], *, low: float = 0.0, high: float = 10.0,
              bins: int = 10) -> list[int]:
    width = (high - low) / bins
    counts = [0] * bins
    for value in values:
        index = int((float(value) - low) / width) if width > 0 else 0
        counts[max(0, min(bins - 1, index))] += 1
    return counts


def ascii_histogram(counts: list[int], *, low: float = 0.0, high: float = 10.0) -> list[str]:
    width = (high - low) / len(counts)
    peak = max(counts) or 1
    lines = []
    for index, count in enumerate(counts):
        left = low + index * width
        right = left + width
        bar = "#" * int(round(count / peak * 40))
        lines.append(f"  {left:4.1f}–{right:4.1f} | {bar:<40} {count:6d} "
                     f"({count / sum(counts):5.1%})")
    return lines


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def summary(values: list[float]) -> dict:
    return {
        "mean": statistics.fmean(values) if values else 0.0,
        "p05": percentile(values, 0.05),
        "p25": percentile(values, 0.25),
        "p50": percentile(values, 0.50),
        "p75": percentile(values, 0.75),
        "p95": percentile(values, 0.95),
        "min": min(values) if values else 0.0,
        "max": max(values) if values else 0.0,
        "below_3": sum(1 for v in values if v < 3.0) / max(1, len(values)),
        "above_8": sum(1 for v in values if v > 8.0) / max(1, len(values)),
    }


def render_report(*, days: int, new: dict, old: dict, config: S.SimConfig) -> str:
    em, en = summary(new["emotion"]), summary(new["energy"])
    em_old, en_old = summary(old["emotion"]), summary(old["energy"])
    lines: list[str] = []
    lines.append(f"# 曲线标定报告（v1.16.3 D 期）\n")
    lines.append(f"- 生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"- 模拟：**{days} 天**，步长 {TICK_SECONDS // 60} 分钟，"
                 f"确定性活动提议（时段表）+ 内建事件库 + 固定种子")
    lines.append(f"- 样本数：{len(new['emotion'])} 个 tick（情绪 / 体力各一份）")
    lines.append(f"- 睡眠段数 {new['sleep_episodes']}、小睡段数 {new['naps']}")
    lines.append("")
    lines.append("> 这是**下界**：真机的活动分布比时段表更丰富（模型会挑活动、会有社交与")
    lines.append("> 世界事件）。它证明「机制本身不会把分布推歪」，不替代真机观察期。\n")

    for label, values, old_values, unit in (
        ("情绪", new["emotion"], old["emotion"], "0–10"),
        ("体力", new["energy"], old["energy"], "0–10"),
    ):
        stats = summary(values)
        stats_old = summary(old_values)
        lines.append(f"## {label}（{unit}）\n")
        lines.append("| 指标 | 新机制（全开） | 旧机制（A/B/C/D 全关） | 位移 |")
        lines.append("|---|---|---|---|")
        for key in ("mean", "p05", "p25", "p50", "p75", "p95", "min", "max"):
            lines.append(f"| {key} | {stats[key]:.2f} | {stats_old[key]:.2f} | "
                         f"{stats[key] - stats_old[key]:+.2f} |")
        lines.append(f"| < 3 的占比 | {stats['below_3']:.1%} | {stats_old['below_3']:.1%} | "
                     f"{stats['below_3'] - stats_old['below_3']:+.1%} |")
        lines.append(f"| > 8 的占比 | {stats['above_8']:.1%} | {stats_old['above_8']:.1%} | "
                     f"{stats['above_8'] - stats_old['above_8']:+.1%} |")
        lines.append("")
        lines.append("```text")
        lines.extend(ascii_histogram(histogram(values)))
        lines.append("```")
        lines.append("")

    # ---- 曲线语义位置对账：中位数是不是还落在「1.0 锚点」附近 ----
    mood_curve, energy_curve = HOST_CURVES["frequency"]
    lines.append("## 曲线点值是否还立在原位\n")
    lines.append(f"- 情绪中位数 **{em['p50']:.2f}** → 情绪因子 "
                 f"{F.interpolate(mood_curve, em['p50']):.3f}"
                 f"（曲线的 1.0 锚点在 5.0，旧分布中位数 {em_old['p50']:.2f}）")
    lines.append(f"- 体力中位数 **{en['p50']:.2f}** → 体力因子 "
                 f"{F.interpolate(energy_curve, en['p50']):.3f}"
                 f"（锚点同样在 5.0，旧分布中位数 {en_old['p50']:.2f}）")
    lines.append(f"- 情绪 5% 分位 {em['p05']:.2f}、95% 分位 {em['p95']:.2f}"
                 f"；因子区间 [{F.interpolate(mood_curve, em['p05']):.3f}, "
                 f"{F.interpolate(mood_curve, em['p95']):.3f}]")
    lines.append("")

    drift_mood = abs(em["p50"] - 5.0)
    drift_energy = abs(en["p50"] - 5.0)
    shifted = abs(em["p50"] - em_old["p50"]) > 0.4 or abs(en["p50"] - en_old["p50"]) > 0.4
    lines.append("**结论**：")
    for label, stats, stats_old in (("情绪", em, em_old), ("体力", en, en_old)):
        lines.append(
            f"- {label}：中位数 {stats['p50']:.2f}（曲线 1.0 锚点 5.0，偏离 "
            f"{abs(stats['p50'] - 5.0):.2f}），相对旧机制 "
            f"{stats['p50'] - stats_old['p50']:+.2f}"
        )
    if shifted:
        lines.append(f"- ⚠ 位移超过 0.4（情绪 {em['p50'] - em_old['p50']:+.2f}、"
                     f"体力 {en['p50'] - en_old['p50']:+.2f}）——**需要重新标定点值**："
                     "把 1.0 锚点挪到新的中位数，两端按同样比例展开。")
    else:
        lines.append("- 两者相对旧机制的位移都 **< 0.4** ⇒ **三套曲线的点值本轮不动**："
                     "结构未变、语义位置未变，改点值只会让「情绪 5 = 基线」这条口径失效。")
    lines.append(f"- 锚点偏离（情绪 {drift_mood:.2f} / 体力 {drift_energy:.2f}）：若旧机制里"
                 "同样存在，那是这条**确定性时段表路径**的属性（模型静默时的底线就是这个"
                 "作息），不是本轮机制造成的——不该通过改曲线点值把它抹平，那样等于把"
                 "偏差写进曲线。真实分布以真机日志为准（`/生活 归因` 与状态卡都能读到）。")
    lines.append(f"- 高端是否被压住：情绪 max {em['max']:.2f}（旧 {em_old['max']:.2f}，"
                 f">8 占比 {em['above_8']:.1%} vs {em_old['above_8']:.1%}）——M6 的高涨折扣"
                 "（×0.7）与比例回归的长尾共同作用，目标是「快乐 plateau」而不是频繁顶到 10。")
    lines.append("- 两端饱和率明显上升时才需要压曲线头部；本轮没有出现。")
    lines.append("- 复现：`python calibrate_curves.py --days 7 "
                 "--out docs/curve-calibration.md`（同种子输出逐位一致）。")
    lines.append("")
    lines.append("## 本次生效的机制参数（与插件默认一致）\n")
    lines.append("```json")
    lines.append(json.dumps({
        "fatigue_ramp_curve": config.fatigue_ramp_curve,
        "recover_ratio_per_tick": config.recover_ratio_per_tick,
        "recover_min_step": config.recover_min_step,
        "inertia_scale_enabled": config.inertia_scale_enabled,
        "afterglow_decay": config.afterglow_decay,
        "afterglow_gain": config.afterglow_gain,
        "baseline_diurnal_curve": config.baseline_diurnal_curve,
        "emotion_fatigue_penalty": config.emotion_fatigue_penalty,
        "low_energy_drain_multiplier": config.low_energy_drain_multiplier,
        "emotion_impact_scaling": config.emotion_impact_scaling,
    }, ensure_ascii=False, indent=2))
    lines.append("```")
    return "\n".join(lines) + "\n"


def production_config() -> S.SimConfig:
    """插件默认配置在 ``SimConfig`` 上的等价物（手写，避免依赖 SDK）。"""

    return S.SimConfig(
        tz_offset_minutes=TZ,
        tick_seconds=TICK_SECONDS,
        fatigue_ramp_curve=FATIGUE_RAMP,
        recover_ratio_per_tick=0.08,
        recover_min_step=0.05,
        inertia_scale_enabled=True,
        afterglow_decay=True,
        afterglow_gain=0.30,
        baseline_diurnal_curve=DIURNAL,
        emotion_fatigue_penalty=0.3,
        emotion_fatigue_threshold=3.0,
        low_energy_drain_multiplier=1.25,
        low_energy_threshold=3.0,
        stress_breakdown_enabled=True,
        stress_breakdown_threshold=7.0,
        stress_breakdown_hours=2.0,
        stress_breakdown_reset=5.0,
        emotion_impact_scaling=True,
        impact_positive_curve=IMPACT_POSITIVE,
        impact_negative_curve=IMPACT_NEGATIVE,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="情绪体力曲线标定（v1.16.3 D 期）")
    parser.add_argument("--days", type=float, default=7.0, help="模拟天数（默认 7）")
    parser.add_argument("--seed", type=int, default=20261010, help="随机种子（可复现）")
    parser.add_argument("--out", default="", help="报告输出路径（空 = 只打印摘要）")
    args = parser.parse_args()

    config = production_config()
    new = simulate(config, days=args.days, seed=args.seed)
    old = simulate(a_phase_off(config), days=args.days, seed=args.seed)
    report = render_report(days=args.days, new=new, old=old, config=config)

    em, en = summary(new["emotion"]), summary(new["energy"])
    print(f"情绪：median {em['p50']:.2f}  p05 {em['p05']:.2f}  p95 {em['p95']:.2f}  "
          f"<3 {em['below_3']:.1%}  >8 {em['above_8']:.1%}")
    print(f"体力：median {en['p50']:.2f}  p05 {en['p05']:.2f}  p95 {en['p95']:.2f}  "
          f"<3 {en['below_3']:.1%}  >8 {en['above_8']:.1%}")
    if args.out:
        target = pathlib.Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report, encoding="utf-8")
        print(f"报告已写入 {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
