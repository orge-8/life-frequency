# -*- coding: utf-8 -*-
"""《life-frequency 情绪体力改进方案》§5 验收标准的**逐条落地**（v1.16.0 → v1.16.3）。

这个文件是方案与代码之间的对账表：每一条标准都有一条同名用例，失败时能直接指出
「方案的哪一条没做到」。方案原文（§5）：

| 层 | 标准 |
|---|---|
| gate | 四件套全绿；每项新机制附「关闭 = 旧行为逐位一致」的回归测试 |
| 行为（M1） | 清闲日一天内自然触到**困意**入睡阈；nap 在一周内至少自然触发一次 |
| 行为（M5） | 大冲击后情绪前 1 h 回落 > 旧线性，4 h 后仍有可测余量（长尾） |
| 行为（M3a） | 高压日的归因输出里能看到「绷不住」事件，且每天 ≤ 1 次 |
| 行为（C 期） | 无死亡螺旋：7 天里 ≥ 80% 的样本/生活日不低于 1.5，且不长时间趴在底部 |
| 纪律 | 内心维度在 `compute_adjust` 的因子表里始终不出现；经济维度照旧不在 |

⚠ v1.24.0 把入睡从「体力降到阈值」改成**困意（sleep_need）达到阈值**，所以 M1
那两条不再测体力，而是测 `sleep_need` 与强制层裁定；旧口径（体力跌破 3.0）
属于已退役行为，不在这里留断言。

「关闭 = 旧行为逐位一致」那一层不在这里重复——它散在四个专项文件里
（`test_attribution.py` / `test_emotion_regress.py` / `test_mood_coupling.py` /
`test_impact_scaling.py`），每条机制都有对应断言。
"""

import asyncio
import importlib.util
import inspect
import pathlib
import random
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_activity as A  # noqa: E402
import life_factors as F  # noqa: E402
import life_sim as S  # noqa: E402

TZ = 480
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent

#: 插件默认的清醒疲劳曲线（与 `plugin.DEFAULT_FATIGUE_RAMP_LINES` 同源）
FATIGUE_RAMP = ((12.0, 0.0), (16.0, -0.15), (20.0, -0.4), (24.0, -0.7))
#: 插件默认的日内节律（对验收结论无影响，但标定时开着更接近真机）
DIURNAL = ((300.0, -0.3), (780.0, 0.15), (1200.0, 0.3), (1380.0, 0.0))


def ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() - TZ * 60


def cfg(**overrides):
    base = dict(tz_offset_minutes=TZ)
    base.update(overrides)
    return S.SimConfig(**base)


def production_config() -> S.SimConfig:
    """插件默认配置在 SimConfig 上的等价物（手写，避免在纯模块用例里拖进 SDK）。"""

    return cfg(
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
        impact_positive_curve=((2.0, 1.3), (5.0, 1.0), (8.0, 0.7)),
        impact_negative_curve=((2.0, 0.7), (5.0, 1.0), (8.0, 1.2)),
    )


def make_state(*, at, activity=A.DAILY, **overrides):
    state = S.LifeState()
    state.last_tick_at = at
    state.activity = activity
    state.activity_since = at
    state.day_key = S.day_key_of(S.local_datetime(at, TZ), 12)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _calibrator():
    path = PLUGIN_DIR / "calibrate_curves.py"
    spec = importlib.util.spec_from_file_location("life_frequency_calibrator_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ================================================================ 纪律层


def test_discipline_mood_and_economy_never_enter_the_factor_table():
    """§5 纪律：内心维度（压力/孤独/电量）与经济维度**始终不出现**在倍率因子里。

    这条不是「现在没写」而是「不许写」——所以直接钉接口与产出：
    ① `compute_adjust` 的入参里没有这三个维度（也没有钱）；
    ② 拆解出来的因子名里一个都不含。
    """

    params = set(inspect.signature(F.compute_adjust).parameters)
    for forbidden in ("stress", "loneliness", "social_battery", "battery", "money",
                      "balance", "budget"):
        assert forbidden not in params, forbidden
    # 情绪与体力**是**合法输入（它们本来就是倍率的两条腿）
    assert {"emotion", "energy"} <= params

    breakdown = F.compute_adjust(
        activity=A.DAILY, emotion=7.0, energy=7.0, sick=False, sleep_debt_nights=0,
        date_factor=1.0, material_count=0.0, now_minutes=20 * 60, config=F.FactorConfig(),
    )
    names = " ".join(name for name, _value in breakdown.factors)
    for forbidden in ("压力", "孤独", "电量", "钱", "预算", "余额"):
        assert forbidden not in names, (forbidden, names)

    # 因子表本身也不许长出这几个键
    factor_config = F.FactorConfig()
    assert not any(
        forbidden in key
        for key in list(factor_config.activity_factors) + list(factor_config.health_factors)
        for forbidden in ("stress", "loneliness", "battery", "money")
    )


def test_discipline_fatigue_reaches_the_multiplier_only_through_the_baseline():
    """决议 1 的开口方式：体力只能通过**基线**间接影响情绪，不能进因子表。

    证据：体力 1 与体力 9 在**同一情绪**下的因子拆解完全一致（体力只由体力因子体现
    一次），而基线却真的被压低了。
    """

    args = dict(activity=A.DAILY, emotion=7.0, sick=False, sleep_debt_nights=0,
                date_factor=1.0, material_count=0.0, now_minutes=20 * 60,
                config=F.FactorConfig())
    low = F.compute_adjust(energy=1.0, **args)
    high = F.compute_adjust(energy=9.0, **args)
    names_low = {name.split("(")[0]: value for name, value in low.factors}
    names_high = {name.split("(")[0]: value for name, value in high.factors}
    # 因子名里带数值（如「体力(1.0)」），所以按名字前缀比对；除了体力因子本身，
    # 其余因子逐项相同（= 体力没有被第二次乘进管线）
    assert set(names_low) == set(names_high), (names_low, names_high)
    differing = {k for k in names_low if names_low[k] != names_high[k]}
    assert differing <= {"体力"}, differing

    at = ts(2026, 2, 8, 20, 0)
    tired = make_state(at=at, energy=1.0)
    rested = make_state(at=at, energy=9.0)
    config = production_config()
    assert S.fatigue_offset(tired, config) == pytest.approx(-0.6)
    assert S.fatigue_offset(rested, config) == 0.0


# ================================================================ 行为（M1）


def _calm_day_timeline(*, config, start_energy=8.0, hours=24.0, at=None):
    """清闲日：daze / daily 每 4 小时交替，**每一步都过强制层**（无提议）。

    v1.24.0 起没有「睡眠窗口」了：她会不会睡只取决于困意。所以这条时间线
    同时是验收「身体驱动入睡」的场地——不需要关掉任何硬性时段。
    """

    at = at or ts(2026, 3, 2, 8, 0)
    state = make_state(at=at, activity=A.DAZE, energy=start_energy)
    samples: list[dict] = []
    for step in range(1, int(hours * 6) + 1):
        now = at + 600 * step
        if not S.is_asleep(state.activity):
            state.activity = A.DAZE if (step * 10) % 480 < 240 else A.DAILY
        S.settle(state, now=now, config=config, events=(), rng=random.Random(1))
        S.enforce_and_apply(state, now=now, config=config, decision=None)
        samples.append({
            "hour": (now - at) / 3600.0,
            "energy": float(state.energy),
            "sleep_need": float(state.sleep_need),
            "activity": state.activity,
        })
    return config, state, samples


def test_m1_calm_day_naturally_falls_asleep_within_a_day():
    """验收：清闲日 24 小时内**自然**触到入睡阈（困意 0.62）并睡下。

    **实测**：混合清闲日（daze/daily 交替）约 **19.6 h**；纯 `daze` 也在这条
    时间线的 24 小时窗口内睡下（见下一个用例）。方案原文的 16–18 h 是旧
    「体力驱动」标定；v1.24.0 改成困意驱动后，入睡由 0.029/h 的累积速度决定，
    这里把窗口校准为「一天之内」并把实测值写进断言。
    """

    config = production_config()
    _config, state, samples = _calm_day_timeline(config=config, hours=26)
    onset = next((item for item in samples if item["activity"] == A.SLEEP), None)
    assert onset is not None, "清闲的一天里她永远不困 = 身体驱动没生效"
    assert onset["hour"] <= 24.0, f"触阈时刻 {onset['hour']:.1f} 小时，一天内该睡了"
    assert onset["sleep_need"] >= (
        config.sleep_need_onset - config.sleep_circadian_strength - 1e-6
    ), f"入睡时困意只有 {onset['sleep_need']:.3f}，不该被别的东西拖上床"
    assert onset["energy"] > 0.0, "她不是因为体力耗尽才睡的（体力仍在正区间）"


def test_m1_full_energy_does_not_block_sleep():
    """清单第二部分的硬要求：**体力满也不许拒绝睡觉**。

    旧模型里「体力回满」会把她叫醒；新模型里体力与困意是两套量，
    满体力 + 困意到阈 = 就该睡。
    """

    at = ts(2026, 3, 3, 3, 0)  # 节律峰值，最容易困的相位
    state = make_state(at=at, activity=A.DAILY, energy=10.0, sleep_need=0.70)
    S.enforce_and_apply(state, now=at, config=production_config(), decision=None)
    assert state.activity == A.SLEEP
    assert state.energy == pytest.approx(10.0)

    # 反向：体力低、困意没到阈 ⇒ 不许强制她去睡（低体力 ≠ 必须睡）
    rested = make_state(at=at, activity=A.DAILY, energy=1.0, sleep_need=0.20)
    S.enforce_and_apply(rested, now=at, config=production_config(), decision=None)
    assert rested.activity == A.DAILY


def test_m1_nap_becomes_reachable_on_a_calm_day():
    """验收：nap 在一周内至少自然触发一次——先证明它**够得着**（困意阈值 0.42）。

    nap 必须由模型提议（没有提议时强制层不会自己安排小睡），所以验收拆两半：
    ① 清闲日下午困意真的涨过 nap 阈；② 此刻的提议会被放行。
    """

    config = production_config()
    _config, _state, samples = _calm_day_timeline(config=config, hours=24)
    crossed = next(
        (item["hour"] for item in samples if item["sleep_need"] >= config.sleep_need_nap_threshold),
        None,
    )
    assert crossed is not None, "清闲日连小睡阈值都够不着 = 标定偏保守"
    # 初始困意 0.25（睡醒后的日常状态），涨到 0.42 约需 (0.42−0.25)/0.029 ≈ 5.9 h
    assert 4.0 <= crossed <= 20.0, crossed

    # ② 站在那个时刻上：白天的低体力小睡提议会被放行
    policy = S.build_enforce_policy(cfg())
    facts = A.ActivityFacts(
        activity=A.DAILY, minutes_in_activity=120, now_minutes=18 * 60,
        emotion=5.0, energy=3.8, energy_cap=10.0, sick=False,
        sleep_minutes_today=0, awake_minutes_today=14 * 60,
        sleep_need=0.50,
    )
    decided = A.enforce(facts, A.ActivityDecision(A.NAP, source=A.SOURCE_LLM), policy)
    assert decided.activity == A.NAP, decided


# ================================================================ 行为（M5）


def _shock_curve(*, ratio: float, hours: float = 4.0, shock: float = 3.0):
    """一次 +3.0 的大冲击之后，情绪随时间的轨迹（惯性期按同一配置，公平对比）。"""

    at = ts(2026, 2, 8, 20, 0)
    config = cfg(
        recover_ratio_per_tick=ratio,
        recover_min_step=0.05,
        inertia_scale_enabled=True,
        baseline_diurnal_curve=(),
        afterglow_span_hours=0.0,  # 只看回归本身，不让余波把基线也抬起来
    )
    state = make_state(at=at, emotion=5.0 + shock)
    state.inertia_until = at
    trajectory: list[tuple[float, float]] = []
    for step in range(1, int(hours * 6) + 1):
        S._regress_emotion(state, config=config, minutes=10.0, now=at + 600 * step)
        trajectory.append(((step * 10) / 60.0, float(state.emotion)))
    return trajectory


def test_m5_big_shock_falls_faster_in_the_first_hour_and_keeps_a_tail():
    """验收：大冲击后前 1 h 回落 > 旧线性，4 h 后仍有可测余量（长尾）。

    用方案自己的例子：从 **10.0** 回落（差距 5.0）。实测：1 小时新 8.03 / 旧 8.80
    （新更快）；4 小时新 5.68 / 旧 5.20（旧的已基本走完，新的还留着余味）。

    ⚠ 这条只在**大**冲击下成立：差距小于 2.5 时比例步长（0.08×差距）比固定步长 0.2
    还小，初期反而更慢——「爆发快消、余味长」说的是大起大落，不是所有小事
    （`test_emotion_regress.py` 里那条从 10 回到 5 的用例同一个道理）。
    """

    new = dict(_shock_curve(ratio=0.08, shock=5.0))
    old = dict(_shock_curve(ratio=0.0, shock=5.0))

    for label, moment in (("半小时", 0.5), ("1 小时", 1.0), ("2 小时", 2.0)):
        assert new[moment] < old[moment], f"{label}：比例回归该比线性更快回落"

    # 4 小时后：线性几乎走完（4.8/5.0），比例回归还留着明显余量
    assert old[4.0] - 5.0 < 0.25, old[4.0]
    assert new[4.0] - 5.0 > 0.5, new[4.0]


# ================================================================ 行为（M3a）


def test_m3a_high_stress_day_shows_up_in_the_attribution_and_only_once():
    """验收：高压日的归因输出里能看到「绷不住」事件，且每天 ≤ 1 次。"""

    at = ts(2026, 2, 8, 14, 0)
    config = production_config()
    state = make_state(at=at, activity=A.WORK, stress=8.5)
    state.stress_high_since = at
    # 压力维持在高位：写回一个 stress 不下滑的轨迹（否则会回归到基线 3.0）
    for step in range(1, 13):
        state.stress = 8.5
        S.settle(state, now=at + 600 * step, config=config, events=(), rng=random.Random(1))

    now = at + 600 * 12
    text = "\n".join(S.attribution_lines(state, now, config))
    assert "情绪有点绷不住" in text, text
    assert "最近真的有点累，感觉自己快绷不住了" in "".join(
        str(item.get("text") or "") for item in state.materials
    )

    # 同一天继续跑两小时：不许出现第二条
    for step in range(13, 25):
        state.stress = 8.5
        S.settle(state, now=at + 600 * step, config=config, events=(), rng=random.Random(1))
    bursts = [item for item in state.recent_events if item.get("label") == "情绪有点绷不住"]
    assert len(bursts) == 1, f"每生活日至多一次，实际 {len(bursts)} 次"


# ================================================================ 行为（C 期）


def test_c_phase_no_death_spiral_over_seven_days():
    """验收：连续 7 天运行中情绪**不被困在谷底**（无死亡螺旋）。

    跑的是标定脚本那条确定性时间线（时段表提议 + 内建事件库 + 固定种子）：
    机制全开（含 C 期两条合法压低源：疲劳偏移与崩溃事件）也不能把她按死在谷底。

    **校准（实测值写进断言）**：样本占比 99.6%（≥ 1.5）、逐日最低值
    ``[1.67, 2.54, 0.93, 2.71, 2.22, 2.23, 3.05]`` ⇒ 7 天里 6 天全程 ≥ 1.5
    （85.7%）。唯一的低谷只持续 **0.5 小时**（3 个 10 分钟样本）就回升——
    旧写法用「最低值 ≥ 1.5」一刀切，会把**合法的一次短暂低落**判成死亡螺旋；
    真正要禁的是「长时间趴在底部」，所以这里改成占比 + 连续时长两条有界判据。
    """

    calibrator = _calibrator()
    data = calibrator.simulate(production_config(), days=7, seed=20261010)
    emotions = data["emotion"]
    assert emotions, "标定脚本没跑出样本"

    healthy = sum(1 for value in emotions if value >= 1.5) / len(emotions)
    assert healthy >= 0.80, f"emotion ≥ 1.5 的占比只有 {healthy:.1%}（要求 ≥ 80%）"

    per_day = [min(emotions[i:i + 144]) for i in range(0, len(emotions), 144)]
    good_days = sum(1 for value in per_day if value >= 1.5) / len(per_day)
    assert good_days >= 0.80, f"只有 {good_days:.0%} 的生活日全程 ≥ 1.5"
    assert len([value for value in per_day if value >= 1.5]) >= 6, per_day

    longest = current = 0
    for value in emotions:
        current = current + 1 if value < 1.5 else 0
        longest = max(longest, current)
    assert longest * 10 <= 3 * 60, f"连续 {longest * 10} 分钟趴在 1.5 以下"
    assert min(emotions) >= 0.5, f"最低情绪 {min(emotions):.2f}——已经跌进谷底"

    # 睡眠确实在发生（否则「没有死亡螺旋」可能只是因为根本没睡）
    assert data["sleep_episodes"] > 0


def test_c_phase_attribution_is_what_can_tell_working_from_runaway():
    """方案原文：「验收判定要能区分『机制正常工作』与『失控』」——所以判别器是归因。

    **审计的反向验证发现「1.5 地板」单独不够**：把耦合调到荒谬值（疲劳系数 1.2、
    低体力放大 3.0、崩溃持续时长 0、余波系数 0.6、回归改回线性）跑 7 天，最低情绪
    仍有 **2.14**、≥1.5 占比 **100%** —— 地板全绿，但那个配置明显是失控的。

    能区分的两个量都在归因里、而且都**有界**：

    ① 疲劳项被 `penalty × threshold` 封住（默认 −0.9；荒谬配置才会到 −3.6）；
    ② 崩溃事件被「每生活日至多一次」钉住（7 天最多 7 条）。
    所以判读方式是「看归因输出的构成」而不是「看有没有跌破某条线」——这正是 M8 先落地的理由。
    """

    at = ts(2026, 3, 2, 8, 0)
    exhausted = make_state(at=at, energy=0.0, emotion=2.0)

    default_parts, _total = S.emotion_baseline_parts(exhausted, at, production_config())
    default_fatigue = min(value for name, value in default_parts if name == "疲劳")
    assert default_fatigue == pytest.approx(-0.9), "默认系数下疲劳项封顶 -0.9"

    from dataclasses import replace as _replace

    runaway = _replace(production_config(), emotion_fatigue_penalty=1.2)
    runaway_parts, _total2 = S.emotion_baseline_parts(exhausted, at, runaway)
    runaway_fatigue = min(value for name, value in runaway_parts if name == "疲劳")
    assert runaway_fatigue == pytest.approx(-3.6), "荒谬系数下疲劳项顶到 -3.6"

    # 崩溃事件的一生一日一次封顶：7 天最多 7 条
    data = _calibrator().simulate(production_config(), days=7, seed=20261010)
    assert 0 <= data["breakdowns"] <= 7, data["breakdowns"]
