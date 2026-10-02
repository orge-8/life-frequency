# -*- coding: utf-8 -*-
"""L3：经济维度 —— 真实月度预算 → 她的生活费账本。

分两层：

1. **纯模块**（``life_economy``）：解析 budget-pacer 的 ``get_budget_status`` 返回、
   净化、分档、生成提示词约束与卡片行。这里穷举坏输入，任何形状都不许抛异常。
2. **桥接**（``plugin._refresh_economy`` / 卡片 / 活动提示词）：只读 RPC 的取数节奏、
   失败退避、降级可见性，以及一条硬纪律——**经济维度绝不写 frequency.set_adjust**。

真机语义对齐（都读过源码）：

- 跨插件调用失败时 Host 返回 ``{"success": False, "error": ...}``，SDK 只对带
  ``result`` 键的成功包装做解包（``maibot_sdk/context.py:282-285``），**不抛异常**。
  ``tests/fakehost.py`` 的 ``api.call`` 分支按这条语义复刻。
- 调用方必须声明 ``api.call`` 能力（Host 1.2.3 按 manifest 授权）。
"""

import asyncio
import dataclasses
import logging
import pathlib
import sys
import time
from datetime import datetime

import pytest

pytest.importorskip("maibot_sdk", reason="需要 maibot-plugin-sdk 才能导入 plugin.py")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fakehost import FakeHost, FakePaths, bind_context, build_context, get_default_config  # noqa: E402

import life_economy as E  # noqa: E402  （conftest 已把插件目录放进 sys.path）

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
TARGET = "org.orge-8.budget-pacer.get_budget_status"
BUDGET = 30.0


# ---------------------------------------------------------------- 夹具


def budget_payload(
    *,
    budget: float = BUDGET,
    spend: float = 15.0,
    today_spend: float = 0.4,
    active: bool = True,
    reason: str = "on_track",
    month: str | None = None,
    pacing: float = 1.0,
    progress: float = 0.5,
) -> dict:
    """造一份与 budget-pacer 契约一致的返回（``tests/test_api.py`` 钉住字段名）。"""

    remaining = budget - spend
    return {
        "ok": True,
        "active": active,
        "enabled": True,
        "paused": False,
        "reason": reason,
        "month": month or datetime.now().strftime("%Y-%m"),
        "budget": budget,
        "spend": spend,
        "today_spend": today_spend,
        "remaining": remaining,
        "remaining_ratio": remaining / budget if budget else 0.0,
        "spend_ratio": spend / budget if budget else 0.0,
        "progress": progress,
        "pacing": pacing,
        "adjust": 0.7,
        "target": 0.7,
        "updated_at": time.time(),
        "stale_seconds": 5.0,
        "source": "refresh",
    }


def snapshot(payload: dict | None = None, *, fetched_at: float | None = None) -> E.EconomySnapshot:
    return E.parse_budget_status(
        payload if payload is not None else budget_payload(),
        fetched_at=time.time() if fetched_at is None else fetched_at,
    )


def _load():
    from fakehost import load_plugin_module

    return load_plugin_module(PLUGIN_DIR, "life_frequency_economy")


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
    """抓插件的日志记录（INFO 默认会被 root 的 WARNING 挡掉，必须自己设级别）。"""

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

    def text(self, level: int = 0) -> str:
        return "\n".join(msg for lv, msg in self.records if lv >= level)


# ================= A. 纯模块：解析 =================


def test_parse_reads_the_documented_contract():
    snap = snapshot(budget_payload(budget=30.0, spend=12.5, today_spend=1.25, pacing=0.83))
    assert snap.ok is True
    assert snap.active is True
    assert snap.budget == pytest.approx(30.0)
    assert snap.spend == pytest.approx(12.5)
    assert snap.today_spend == pytest.approx(1.25)
    assert snap.remaining == pytest.approx(17.5)
    assert snap.remaining_ratio == pytest.approx(17.5 / 30.0)
    assert snap.pacing == pytest.approx(0.83)
    assert snap.reason == "on_track"


def test_parse_treats_sdk_failure_dict_as_error_not_data():
    """跨插件调用失败不抛异常：必须把 {'success': False} 认成错误。"""

    snap = E.parse_budget_status(
        {"success": False, "error": "未找到 API 提供方插件: org.orge-8.budget-pacer"},
        fetched_at=time.time(),
    )
    assert snap.ok is False
    assert snap.reason == "api_error"
    assert "未找到 API 提供方插件" in snap.error


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not-a-dict",
        42,
        [],
        {"unexpected": True},
        {"ok": True},                                  # 缺契约字段
        {"ok": True, "active": True, "budget": 30.0},  # 仍缺 spend/remaining
    ],
)
def test_parse_rejects_bad_payload_without_raising(raw):
    snap = E.parse_budget_status(raw, fetched_at=time.time())
    assert snap.ok is False, f"坏负载被当成有效数据：{raw!r}"
    assert snap.reason in ("bad_payload", "api_error")
    # 仍然要能渲染卡片（不能因为没数据就抛）
    lines = E.economy_lines(snap, E.economy_tier(snap))
    assert lines and "未接入" in lines[0]


def test_parse_reports_missing_keys_by_name():
    snap = E.parse_budget_status({"ok": True, "active": True, "budget": 1.0}, fetched_at=0.0)
    assert "spend" in snap.error and "remaining" in snap.error, snap.error


def test_parse_rejects_malformed_month():
    snap = snapshot(budget_payload(month="2026-13"))
    assert snap.ok is False
    assert "月份" in snap.error

    ok = snapshot(budget_payload(month="2026-12"))
    assert ok.ok is True and ok.month == "2026-12"


def test_parse_marks_plugin_disabled_payload_as_inactive():
    snap = snapshot(budget_payload(active=False, reason="disabled"))
    assert snap.ok is True            # 数据有效
    assert snap.active is False       # 但预算没在生效
    assert E.economy_tier(snap) == E.TIER_UNKNOWN


def test_parse_sanitizes_non_finite_and_negative_numbers():
    raw = budget_payload()
    raw.update(
        budget=float("nan"),
        spend=float("inf"),
        today_spend=-3.0,
        remaining=float("nan"),
        remaining_ratio=float("inf"),
        pacing=float("-inf"),
        progress=9.0,
    )
    snap = E.parse_budget_status(raw, fetched_at=time.time())
    for value in (snap.budget, snap.spend, snap.today_spend, snap.remaining,
                  snap.remaining_ratio, snap.spend_ratio, snap.pacing, snap.progress):
        assert value == value and abs(value) < 1e6, f"非有限值漏进来了：{value}"
    assert snap.spend == pytest.approx(0.0)
    assert snap.today_spend == pytest.approx(0.0)
    assert snap.progress == pytest.approx(1.0)


def test_parse_never_raises_on_hostile_objects():
    """``__str__`` 抛异常、递归容器、超大数都不许把插件打挂。"""

    class Boom:
        def __str__(self):
            raise RuntimeError("boom")

        def __float__(self):
            raise RuntimeError("boom")

    weird = [
        {"ok": True, "active": Boom(), "budget": Boom(), "spend": Boom(), "remaining": Boom(),
         "month": Boom(), "reason": Boom()},
        {"ok": False, "reason": Boom(), "error": Boom()},
        {1: 2},
        {"ok": True, "active": "yes", "budget": 1e308, "spend": -1e308, "remaining": 1e400},
    ]
    for raw in weird:
        snap = E.parse_budget_status(raw, fetched_at=time.time())
        assert isinstance(snap, E.EconomySnapshot)
        tier = E.economy_tier(snap)
        assert E.frugal_hint(snap, tier) == "" or isinstance(E.frugal_hint(snap, tier), str)
        assert E.economy_lines(snap, tier)


# ================= B. 纯模块：分档 =================


@pytest.mark.parametrize(
    "spend,expected",
    [
        (30.0, E.TIER_BROKE),   # 已花满，余额 0
        (32.0, E.TIER_BROKE),   # 超支
        (28.0, E.TIER_BROKE),   # 余额 6.7% < 10%
        (22.0, E.TIER_TIGHT),   # 余额 26.7%
        (21.0, E.TIER_TIGHT),   # 余额 30% —— 边界取「紧」
        (15.0, E.TIER_NORMAL),  # 余额 50%
        (10.0, E.TIER_ROOMY),   # 余额 66.7% ≥ 60%
        (0.0, E.TIER_ROOMY),
    ],
)
def test_tier_by_remaining_ratio(spend, expected):
    assert E.economy_tier(snapshot(budget_payload(spend=spend))) == expected


def test_tier_treats_overspending_pace_as_tight():
    """钱只剩四成、还花得比时间进度快 → 手头紧（与预算插件的刹车同因不同果）。"""

    snap = snapshot(budget_payload(spend=18.0, pacing=1.5))  # 余额 40%
    assert E.economy_tier(snap) == E.TIER_TIGHT


def test_tier_ignores_pacing_noise_early_in_the_month():
    """月初误报回归：钱几乎没动时，被 p_floor_days 放大的节奏比不能判她手头紧。

    真机形态：1 号花了预算的 2%，时间进度下限兜在 1/31 天 ⇒ 节奏比 ≈ 0.6/0.032 ≈ 19，
    此时余额还有 98%，她不应该开始吃泡面。
    """

    snap = snapshot(budget_payload(spend=0.6, pacing=19.0, progress=1 / 31))
    assert E.economy_tier(snap) == E.TIER_ROOMY, E.economy_tier(snap)
    assert E.frugal_hint(snap, E.economy_tier(snap)) == ""

    # 门槛本身可调：把「节奏比生效的最低花费进度」压到 0 就恢复旧的敏感行为
    strict = E.EconomyThresholds(pacing_from_spend_ratio=0.0).normalized()
    assert E.economy_tier(snap, strict) == E.TIER_TIGHT


def test_tier_unknown_for_unavailable_or_inactive():
    assert E.economy_tier(E.unavailable("api_error", "x")) == E.TIER_UNKNOWN
    assert E.economy_tier(snapshot(budget_payload(active=False))) == E.TIER_UNKNOWN


def test_tier_unknown_when_local_data_is_stale():
    now = time.time()
    snap = snapshot(budget_payload(spend=29.0), fetched_at=now - 4 * 3600)
    assert snap.is_stale(now, 3 * 3600) is True
    assert E.economy_tier(snap, now=now, stale_after_seconds=3 * 3600) == E.TIER_UNKNOWN
    # 未过期时照常分档
    assert E.economy_tier(snap, now=now, stale_after_seconds=5 * 3600) == E.TIER_BROKE


def test_stale_check_can_be_disabled():
    now = time.time()
    snap = snapshot(budget_payload(), fetched_at=now - 10 * 24 * 3600)
    assert snap.is_stale(now, 0) is False, "stale_after=0 应当表示关闭过期检查"
    assert E.economy_tier(snap, now=now, stale_after_seconds=0) != E.TIER_UNKNOWN


def test_snapshot_without_fetch_time_is_stale():
    snap = snapshot(budget_payload(), fetched_at=0.0)
    assert snap.age_seconds(time.time()) == -1.0
    assert snap.is_stale(time.time(), 3600) is True


def test_thresholds_are_normalized_for_swapped_or_broken_values():
    limits = E.EconomyThresholds(
        broke_ratio=0.9, tight_ratio=0.2, roomy_ratio=0.1, tight_pacing=float("nan")
    ).normalized()
    assert limits.broke_ratio <= limits.tight_ratio <= limits.roomy_ratio
    assert limits.broke_ratio == pytest.approx(0.1)
    assert limits.tight_ratio == pytest.approx(0.2)
    assert limits.roomy_ratio == pytest.approx(0.9)
    assert limits.tight_pacing == pytest.approx(1.2), "nan 阈值必须回落到默认"


# ================= C. 纯模块：行为与展示 =================


def test_frugal_hint_tells_her_to_cut_costs_and_not_to_beg():
    broke = E.frugal_hint(snapshot(budget_payload(spend=30.0)), E.TIER_BROKE)
    assert "泡面" in broke
    assert "不会主动" in broke and "缺钱" in broke

    tight = E.frugal_hint(snapshot(budget_payload(spend=25.0)), E.TIER_TIGHT)
    assert "犹豫" in tight or "省" in tight
    assert "泡面" not in tight, "还没到见底，别把话写得那么惨"

    assert E.frugal_hint(snapshot(), E.TIER_NORMAL) == ""
    assert E.frugal_hint(snapshot(), E.TIER_ROOMY) == ""
    assert E.frugal_hint(snapshot(), E.TIER_UNKNOWN) == ""


def test_economy_lines_show_balance_today_and_tier():
    snap = snapshot(budget_payload(budget=30.0, spend=25.0, today_spend=1.5))
    tier = E.economy_tier(snap)
    text = "\n".join(E.economy_lines(snap, tier, now=time.time(), stale_after_seconds=3600))
    assert "经济" in text and tier in text
    assert "5.00" in text, text      # 余额 30-25
    assert "25.00" in text           # 本月已花
    assert "1.50" in text            # 今日开销
    assert "每月 1 日到账" in text


def test_economy_lines_flag_stale_data():
    now = time.time()
    snap = snapshot(budget_payload(), fetched_at=now - 4 * 3600)
    text = "\n".join(E.economy_lines(snap, E.economy_tier(snap), now=now,
                                     stale_after_seconds=3600))
    assert "⚠" in text and "过期" in text


def test_economy_lines_explain_inactive_reason():
    snap = snapshot(budget_payload(active=False, reason="paused"))
    text = "\n".join(E.economy_lines(snap, E.TIER_UNKNOWN))
    assert "调节已暂停" in text


def test_economy_lines_for_no_snapshot():
    text = "\n".join(E.economy_lines(None, E.TIER_UNKNOWN))
    assert "未接入" in text and "尚未取数" in text


# ================= D. 桥接：取数 =================


def test_refresh_calls_the_public_api_with_version_and_refresh():
    async def run():
        module, plugin, host = _make_plugin()
        host.api_returns[TARGET] = budget_payload(spend=25.0)
        now = time.time()
        await plugin._refresh_economy(now)
        calls = host.calls_of("api.call")
        assert len(calls) == 1, calls
        assert calls[0]["api_name"] == TARGET
        assert calls[0]["version"] == "1"
        assert calls[0]["args"] == {"refresh": True}, calls[0]
        assert plugin._economy is not None and plugin._economy.ok
        assert plugin._economy.spend == pytest.approx(25.0)
        return plugin, host, now

    asyncio.run(run())


def test_refresh_never_writes_frequency():
    """经济维度是只读的：整条链路都不许碰 frequency.set_adjust。"""

    async def run():
        module, plugin, host = _make_plugin()
        host.api_returns[TARGET] = budget_payload(spend=30.0)   # 见底
        now = time.time()
        await plugin._refresh_economy(now)
        plugin._render_status(now)
        await plugin._refresh_economy(now + 4 * 3600)           # 强制再取一次
        assert host.calls_of("frequency.set_adjust") == [], "经济维度动了频率"
        assert host.calls_of("frequency.get_adjust") == []

    asyncio.run(run())


def test_refresh_is_rate_limited_by_interval():
    async def run():
        module, plugin, host = _make_plugin()
        host.api_returns[TARGET] = budget_payload()
        now = time.time()
        await plugin._refresh_economy(now)
        await plugin._refresh_economy(now + 60)          # 间隔内
        assert len(host.calls_of("api.call")) == 1
        await plugin._refresh_economy(now + 31 * 60)     # 越过 30 分钟
        assert len(host.calls_of("api.call")) == 2

    asyncio.run(run())


def test_refresh_degrades_and_backs_off_when_provider_missing():
    async def run():
        module, plugin, host = _make_plugin()   # 没登记 api.call 返回 → 真机「找不到提供方」
        now = time.time()
        with _Capture(module) as cap:
            await plugin._refresh_economy(now)
            assert plugin._economy is not None and plugin._economy.ok is False
            assert plugin._economy.reason == "api_error"
            assert "未找到 API 提供方插件" in plugin._economy.error
            await plugin._refresh_economy(now + 60)      # 退避窗口内不重试
            assert len(host.calls_of("api.call")) == 1
            assert "读取预算失败" in cap.text(logging.WARNING)
            # 只告警一次（退避期间不刷屏）
            assert cap.text(logging.WARNING).count("读取预算失败") == 1
        # 卡片必须如实说明「未接入」，而不是显示上一次的假余额
        assert "未接入" in plugin._render_status(now)

    asyncio.run(run())


def test_refresh_recovers_and_warns_again_after_a_later_outage():
    async def run():
        module, plugin, host = _make_plugin()
        now = time.time()
        with _Capture(module) as cap:
            await plugin._refresh_economy(now)
            assert "读取预算失败" in cap.text(logging.WARNING)

            host.api_returns[TARGET] = budget_payload()      # 提供方「装好了」
            await plugin._refresh_economy(now + 3600)
            assert plugin._economy.ok is True
            assert "经济维度已接入" in cap.text(logging.INFO)

            host.api_errors[TARGET] = "rpc 断了"             # 又坏了
            del host.api_returns[TARGET]
            await plugin._refresh_economy(now + 2 * 3600)
            assert plugin._economy.ok is False
            assert cap.text(logging.WARNING).count("读取预算失败") == 2, "恢复后再坏应当重新告警"

    asyncio.run(run())


def test_refresh_disabled_makes_no_cross_plugin_call():
    async def run():
        module, plugin, host = _make_plugin(economy={"enabled": False})
        await plugin._refresh_economy(time.time())
        assert host.calls_of("api.call") == [], "关掉经济维度后不该再调 API"
        assert plugin._economy is None
        assert "[economy] enabled = false" in plugin._render_status(time.time())

    asyncio.run(run())


def test_refresh_reports_unconfigured_target_without_calling_anything():
    async def run():
        module, plugin, host = _make_plugin(economy={"api_name": ""})
        await plugin._refresh_economy(time.time())
        assert host.calls_of("api.call") == []
        assert plugin._economy is not None and plugin._economy.ok is False
        assert plugin._economy.reason == "未配置"

    asyncio.run(run())


def test_refresh_accepts_a_full_api_name_without_double_prefix():
    async def run():
        module, plugin, host = _make_plugin(economy={"api_name": TARGET})
        assert plugin._economy_api_target() == TARGET
        host.api_returns[TARGET] = budget_payload()
        await plugin._refresh_economy(time.time())
        assert host.calls_of("api.call")[0]["api_name"] == TARGET

    asyncio.run(run())


def test_refresh_timeout_degrades_instead_of_hanging():
    async def run():
        module, plugin, host = _make_plugin(economy={"api_timeout_seconds": 1})

        async def never_returns(*args, **kwargs):
            await asyncio.sleep(30)

        plugin.ctx.api.call = never_returns
        started = time.time()
        await plugin._refresh_economy(started)
        assert time.time() - started < 5, "超时没有生效，生活循环会被拖住"
        assert plugin._economy.ok is False and plugin._economy.reason == "超时"

    asyncio.run(run())


def test_cancellation_propagates_out_of_refresh():
    """卸载时取消任务不能被吞成「取数失败」（否则关插件会留下悬空状态）。"""

    async def run():
        module, plugin, host = _make_plugin()

        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        plugin.ctx.api.call = cancelled
        with pytest.raises(asyncio.CancelledError):
            await plugin._refresh_economy(time.time())

    asyncio.run(run())


def test_payday_is_logged_once_per_month_change():
    async def run():
        module, plugin, host = _make_plugin()
        host.api_returns[TARGET] = budget_payload(month="2026-10", budget=30.0)
        plugin._economy_month_seen = "2026-09"
        with _Capture(module) as cap:
            await plugin._refresh_economy(time.time())
            assert "生活费到账" in cap.text(logging.INFO), cap.text()
            assert "2026-10" in cap.text(logging.INFO)
            before = cap.text(logging.INFO).count("生活费到账")
            plugin._economy_next_try_at = 0.0
            await plugin._refresh_economy(time.time() + 1)   # 同一个月不再重复播报
            assert cap.text(logging.INFO).count("生活费到账") == before

    asyncio.run(run())


# ================= E. 桥接：行为与卡片 =================


def test_broke_economy_makes_the_activity_prompt_frugal():
    async def run():
        module, plugin, host = _make_plugin()
        host.api_returns[TARGET] = budget_payload(spend=29.0)   # 余额 3.3%
        now = time.time()
        await plugin._refresh_economy(now)
        hint = plugin._economy_hint(now)
        assert "泡面" in hint and "不会主动" in hint
        assert plugin._economy_tier_now(now) == E.TIER_BROKE

        # 提示词真的带上去了（不是只存在变量里）
        prompt = module.build_prompt(
            module.PromptInput(bot_name="麦麦", persona="十九岁", economy_hint=hint)
        )
        assert "手头" in prompt and "泡面" in prompt

    asyncio.run(run())


def test_roomy_or_stale_economy_does_not_constrain_her():
    async def run():
        module, plugin, host = _make_plugin()
        host.api_returns[TARGET] = budget_payload(spend=2.0)    # 余额 93%
        now = time.time()
        await plugin._refresh_economy(now)
        assert plugin._economy_tier_now(now) == E.TIER_ROOMY
        assert plugin._economy_hint(now) == ""

        # 数据过期后即使余额很低也不再约束行为（卡片仍展示并带 ⚠）
        host.api_returns[TARGET] = budget_payload(spend=29.0)
        plugin._economy_next_try_at = 0.0
        plugin._economy = dataclasses.replace(plugin._economy, fetched_at=now - 10 * 3600)
        assert plugin._economy_tier_now(now) == E.TIER_UNKNOWN
        assert plugin._economy_hint(now) == ""
        assert "过期" in plugin._render_status(now)

    asyncio.run(run())


def test_hint_can_be_turned_off_while_economy_stays_visible():
    async def run():
        module, plugin, host = _make_plugin(economy={"hint_in_prompt": False})
        host.api_returns[TARGET] = budget_payload(spend=29.0)
        now = time.time()
        await plugin._refresh_economy(now)
        assert plugin._economy_hint(now) == ""
        card = plugin._render_status(now)
        assert "经济" in card and "见底" in card, card

    asyncio.run(run())


def test_status_card_shows_economy_block():
    async def run():
        module, plugin, host = _make_plugin()
        host.api_returns[TARGET] = budget_payload(budget=30.0, spend=25.0, today_spend=1.5,
                                                  pacing=1.35, reason="brake")
        now = time.time()
        await plugin._refresh_economy(now)
        card = plugin._render_status(now)
        assert "经济：余额 5.00 元 / 生活费 30.00 元（紧张）" in card, card
        assert "本月已花 25.00 元" in card
        assert "今日 1.50 元" in card
        assert "节奏比 1.35" in card and "正在压低" in card

    asyncio.run(run())


def test_economy_hint_is_not_injected_into_the_reply_prompt():
    """按需求：余额只影响她的生活与行为，**不**往聊天上下文里塞钱的话题。"""

    async def run():
        module, plugin, host = _make_plugin()
        host.api_returns[TARGET] = budget_payload(spend=29.0)
        now = time.time()
        await plugin._refresh_economy(now)
        digest = plugin._life_digest()
        assert "余额" not in digest and "生活费" not in digest, digest

    asyncio.run(run())


def test_sim_tick_refreshes_economy_before_asking_the_model():
    """取数必须发生在活动决策之前，否则这一轮的提示词拿不到「手头紧」。"""

    async def run():
        # skip_when_forced 默认开启，新状态未满最短停留期时会整轮跳过模型提问，
        # _ask_activity 根本不会被调（那是插件省调用的正确行为，不是缺陷）。
        # 本测试要验证的是「取数 → 决策」的先后顺序，必须把跳过关掉。
        module, plugin, host = _make_plugin(activity={"llm": {"skip_when_forced": False}})
        host.api_returns[TARGET] = budget_payload(spend=29.0)
        order: list[str] = []

        original = plugin.ctx.api.call

        async def spy(*args, **kwargs):
            order.append("economy")
            return await original(*args, **kwargs)

        plugin.ctx.api.call = spy
        plugin._fetch_identity = lambda: asyncio.sleep(0)  # 不读人设，专心看顺序
        plugin._llm_ready = lambda now: True

        async def fake_ask(now):
            order.append("activity")
            return None

        plugin._ask_activity = fake_ask
        await plugin._sim_tick()
        assert order[:2] == ["economy", "activity"], order

    asyncio.run(run())
