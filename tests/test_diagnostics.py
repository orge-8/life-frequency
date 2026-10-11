# -*- coding: utf-8 -*-
"""L3：诊断采集程序（``collect_diagnostics.py``）的回归测试。

采集程序本身也是交付物，所以它必须**被证明能区分「正常」与「不正常」**——否则它只是
一个把数据抄一遍的装饰品。这里造两份夹具：

* ``healthy``：用插件自己的引擎跑一天，落一份**新鲜**的状态 + 库 + 配置 + 日志；
* ``broken``：陈旧状态、模型连续失败、写不进宿主、连续清醒 40 小时、坏值、被暂停的
  配置、非法 filter_mode、损坏的库、带异常栈的日志。

断言：健康夹具**没有 FAIL** 且退出码 0；坏夹具**必须**逐条报出这些故障、退出码 1。
"""

import importlib.util
import json
import pathlib
import random
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import life_activity as A  # noqa: E402
import life_events as E  # noqa: E402
import life_sim as S  # noqa: E402
import life_store as STORE  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
PLUGIN_ID = "org.orge-8.life-frequency"
#: 一个「生命周期齐全」的最小 plugin.py：用来构造与机器现状无关的干净插件目录
PLUGIN_STUB = (
    "def create_plugin():\n    return None\n\n\n"
    "class _P:\n"
    "    def on_load(self):\n        pass\n\n"
    "    def on_unload(self):\n        pass\n\n"
    "    def on_config_update(self, config):\n        pass\n"
)

CONFIG_TEXT = """[plugin]
config_version = "1.5.0"
enabled = true

[simulation]
tick_seconds = 600
tz_offset_minutes = 480
sleep_window = "03:00-11:00"
sleep_energy_threshold = 3.0
offline_gap_minutes = 30

[emotion_energy]
recover_ratio_per_tick = 0.08
fatigue_ramp_curve = ["12=0", "16=-0.15", "20=-0.4"]
baseline_diurnal_curve = ["300=-0.3", "1200=0.3"]
emotion_fatigue_penalty = 0.3
low_energy_drain_multiplier = 1.25
emotion_impact_scaling = true
afterglow_decay = true
afterglow_gain = 0.0

[frequency]
quiet_hours = ["23:30-08:00"]

[apply]
filter_mode = "all"

[activity]
materials_keep = 20

[activity.llm]
task_name = "planner"

[mood]
enabled = true
stress_breakdown_enabled = true
loneliness_social_scaling = true

[relations]
enabled = true

[social]
enabled = true
relation_emotion_scaling = true
"""

BROKEN_CONFIG_TEXT = (
    CONFIG_TEXT.replace('config_version = "1.5.0"', 'config_version = "1"')
    .replace('filter_mode = "all"', 'filter_mode = "白名单"')
    .replace("[frequency]\nquiet_hours", "[frequency]\npaused = true\nquiet_hours")
)


def _load_diagnostics():
    spec = importlib.util.spec_from_file_location(
        "life_frequency_diagnostics", PLUGIN_DIR / "collect_diagnostics.py"
    )
    module = importlib.util.module_from_spec(spec)
    # ⚠ 必须先塞进 sys.modules：采集程序里有 dataclass，而 dataclasses 解析字段注解时
    # 会 `sys.modules.get(cls.__module__).__dict__`——不注册就 AttributeError: NoneType。
    # （真机是 `python collect_diagnostics.py` 走廊道，__main__ 本来就在 sys.modules 里。）
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_healthy(base: pathlib.Path, now: float) -> None:
    base.mkdir(parents=True, exist_ok=True)
    config = S.SimConfig(
        tz_offset_minutes=480, tick_seconds=600,
        afterglow_decay=True, afterglow_gain=0.30, recover_ratio_per_tick=0.08,
        inertia_scale_enabled=True,
        fatigue_ramp_curve=((12.0, 0.0), (16.0, -0.15), (20.0, -0.4)),
        baseline_diurnal_curve=((300.0, -0.3), (1200.0, 0.3)),
        emotion_fatigue_penalty=0.3, low_energy_drain_multiplier=1.25,
        emotion_impact_scaling=True,
    )
    events, _warnings = E.merge_events(None, None)
    state = S.new_state(now=now - 86400, config=config, energy=8.0)
    rng = random.Random(11)
    for step in range(1, int(86400 / 600) + 1):
        moment = now - 86400 + 600 * step
        S.settle(state, now=moment, config=config, events=events, rng=rng)
        facts = S.enforce_facts(state, now=moment, config=config)
        if not facts.sick:
            S.enforce_and_apply(
                state, now=moment, config=config,
                decision=A.rule_based_activity(
                    now_minutes=facts.now_minutes, energy=facts.energy,
                    sleep_energy_threshold=config.sleep_energy_threshold,
                ),
            )
    # 真机里这些字段由 plugin 侧维护；夹具补上「健康值」，否则会误报成模型故障
    state.llm_last_success_at = now - 600
    state.llm_fail_streak = 0
    state.applied = {"private-10001": 0.9}
    state.foreign = {}
    state.observed = {"private-10001": 0.7}
    state.unbacked = {}
    (base / "life_state.json").write_text(
        json.dumps(state.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    (base / "adjust_memory.json").write_text(
        json.dumps({"applied": state.applied, "foreign": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (base / "config.toml").write_text(CONFIG_TEXT, encoding="utf-8")
    store = STORE.open_store(base / "life_store.db")
    store.put_relationship({
        "user_id": "10001", "first_seen_at": now - 86400, "last_interaction_at": now - 600,
        "interaction_count": 12, "familiarity": 88.0, "relation_hint": "阿岚",
        "shared_events": 2,
    })
    store.close()
    logs = base / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 600))
    (logs / "maibot.log").write_text(
        f"{stamp} [INFO] plugin.org.orge-8.life-frequency: 生活频率插件已加载\n"
        f"{stamp} [INFO] plugin.org.orge-8.life-frequency: 社交经历：接进 1 条（情绪 +0.43）\n",
        encoding="utf-8",
    )


def _write_broken(base: pathlib.Path, now: float) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / "life_state.json").write_text(json.dumps({
        "state_version": 1,
        "last_tick_at": now - 3 * 86400,     # 三天没推进
        "day_key": "2026-10-07",
        "activity": "work",
        "activity_since": now - 3 * 86400,
        "emotion": 1.2,
        "energy": 11.4,                       # 超过上限
        "energy_cap": 8.5,
        "continuous_awake_minutes": 2400,     # 连续清醒 40 小时
        "llm_fail_streak": 5,
        "llm_cooldown_until": now + 1800,
        "llm_last_success_at": now - 5 * 86400,
        "at_wake_until": float("nan"),        # 坏值（应被净化并报告）
        "unbacked": {"group-1": now + 3600},
        "recent_events": [],
        "materials": [],
    }, ensure_ascii=False), encoding="utf-8")
    (base / "adjust_memory.json").write_text("{不是 JSON", encoding="utf-8")
    (base / "config.toml").write_text(BROKEN_CONFIG_TEXT, encoding="utf-8")
    (base / "life_store.db").write_bytes("这不是 SQLite".encode("utf-8") * 64)
    logs = base / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 3 * 86400))
    (logs / "maibot.log").write_text(
        f"{stamp} [ERROR] plugin.org.orge-8.life-frequency: 生活状态推进异常\n"
        f"{stamp} [ERROR] plugin.org.orge-8.life-frequency: Traceback (most recent call last)\n"
        f"{stamp} [WARNING] plugin.org.orge-8.life-frequency: 写入失败 cookie=p_skey=SECRET123\n",
        encoding="utf-8",
    )


def _run(tmp_path: pathlib.Path, data: pathlib.Path) -> tuple[int, str, str]:
    module = _load_diagnostics()
    out_dir = tmp_path / "out"
    code = module.main([
        "--plugin-dir", str(PLUGIN_DIR),
        "--data-dir", str(data),
        "--config", str(data / "config.toml"),
        "--log", str(data / "logs" / "maibot.log"),
        "--offline-simulate", "0",
        "--out-dir", str(out_dir),
    ])
    reports = sorted(out_dir.glob("*.md"))
    assert reports, "没写出报告"
    return code, reports[-1].read_text(encoding="utf-8"), reports[-1].name


def test_healthy_fixture_is_reported_as_operational(tmp_path):
    now = time.time()
    data = tmp_path / "healthy"
    _write_healthy(data, now)
    code, report, _name = _run(tmp_path, data)

    assert code == 0, report[-2000:]
    assert "❌ 0" in report, "健康数据不该判 FAIL"

    # 关键几条要真的被检查到（而不是「什么都没查所以没红」）
    for needle in ("生活循环正在推进", "状态文件结构与取值都干净", "模型侧健康",
                   "完整性检查通过", "关系档案 1 人"):
        assert needle in report, needle
    # 状态表格里应当能看到她在做什么
    assert "| 活动 |" in report and "| 情绪 / 体力 |" in report


def test_broken_fixture_is_reported_with_concrete_failures(tmp_path):
    now = time.time()
    data = tmp_path / "broken"
    _write_broken(data, now)
    code, report, _name = _run(tmp_path, data)

    assert code == 1, "坏数据必须给出非零退出码"
    for needle in (
        "生活循环没有在跑",                    # 状态陈旧
        "连续清醒 40.0 小时仍未入睡",           # 睡眠链路没生效
        "模型连续失败 5 次",                    # 模型侧故障
        "插件处于暂停状态",                     # 配置：被暂停
        "filter_mode 非法",                     # 配置：非法值
        "life_store.db 读不出来",               # 库损坏
        "日志里有确定性故障签名",               # 日志：异常栈
        "坏值被净化",                           # 状态文件受损
    ):
        assert needle in report, needle


def test_report_redacts_credentials_and_identity_numbers(tmp_path):
    now = time.time()
    data = tmp_path / "broken"
    _write_broken(data, now)
    _code, report, _name = _run(tmp_path, data)

    assert "SECRET123" not in report, "凭据泄漏进报告了"
    assert "<redacted>" in report
    # 日志里的「写入失败」那行确实被采进来了（否则上面的断言是空过）
    assert "写入失败" in report


def test_ownership_check_rejects_another_plugins_config(tmp_path):
    """磁盘上别的插件也有 config.toml —— 绝不能捡错（实测踩过）。"""

    module = _load_diagnostics()
    foreign = tmp_path / "someone-else"
    foreign.mkdir()
    (foreign / "config.toml").write_text(
        '[plugin]\nconfig_version = "1"\nenabled = true\n', encoding="utf-8"
    )
    assert module.looks_like_config(module._load_toml(foreign / "config.toml")[0]) is False

    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "config.toml").write_text(CONFIG_TEXT, encoding="utf-8")
    assert module.looks_like_config(module._load_toml(mine / "config.toml")[0]) is True


def test_plugin_dir_is_auto_located_when_pointed_at_the_plugins_root(tmp_path):
    """真机现场复刻：脚本放在 ``<MaiBot>/plugins/`` 下直接跑。

    后果曾经是两条**假 FAIL**（manifest 不在、plugin.py 不在）+ 读不到真机版本 +
    附录塞进 ``.update_backups`` 里 1160 个别人的文件。现在必须自纠错并说明。
    """

    module = _load_diagnostics()
    plugins = tmp_path / "plugins"
    plugin = plugins / "life-frequency"
    plugin.mkdir(parents=True)
    (plugin / "_manifest.json").write_text(
        json.dumps({"manifest_version": 2, "id": PLUGIN_ID, "version": "9.9.9"}),
        encoding="utf-8",
    )
    (plugin / "plugin.py").write_text("def create_plugin():\n    pass\n", encoding="utf-8")
    backup = plugins / ".update_backups" / "other-plugin"
    backup.mkdir(parents=True)
    for index in range(20):
        (backup / f"file_{index}.py").write_text("x = 1\n", encoding="utf-8")

    located, note = module.locate_plugin_dir(plugins, [plugins])
    assert located == plugin.resolve(), located
    assert "自动定位" in note, note

    # 附录不许再把别人的备份算进来
    inventory = module.collect_inventory(plugin, None)
    paths = [item["path"] for item in inventory["plugin_files"]]
    assert "plugin.py" in paths and "_manifest.json" in paths
    assert len(paths) == 2, paths

    wider = module.collect_inventory(plugins, None)
    assert all(".update_backups" not in item["path"] for item in wider["plugin_files"])


def _write_real_world(tmp_path: pathlib.Path, now: float) -> pathlib.Path:
    """复刻 2026-10-10 真机现场：24 小时睡眠窗、dry_run、电量 0、20 人全陌生。"""

    data = tmp_path / "real"
    _write_healthy(data, now)
    (data / "config.toml").write_text(
        CONFIG_TEXT.replace('sleep_window = "03:00-11:00"', 'sleep_window = "00:00-23:59"')
        .replace('config_version = "1.5.0"', 'config_version = "1.4.0"'),
        encoding="utf-8",
    )
    text = (data / "config.toml").read_text(encoding="utf-8")
    if "[proactive]" not in text:
        text += "\n[proactive]\nenabled = true\n"
    (data / "config.toml").write_text(
        text.replace("[simulation]\n", "[simulation]\ndry_run = true\n"),
        encoding="utf-8",
    )
    payload = json.loads((data / "life_state.json").read_text(encoding="utf-8"))
    payload["social_battery"] = 0.0
    payload["loneliness"] = 0.0
    # 真机那台「已写 0 个会话」：配合 dry_run 应该能互相印证
    payload["applied"] = {}
    payload["foreign"] = {}
    payload["observed"] = {}
    (data / "life_state.json").write_text(json.dumps(payload, ensure_ascii=False),
                                          encoding="utf-8")
    store = STORE.open_store(data / "life_store.db")
    store.close()
    # 真机那台的档案是「20 人全陌生」：先把夹具里那个 88 分的熟人删掉
    import sqlite3

    conn = sqlite3.connect(str(data / "life_store.db"))
    conn.execute("DELETE FROM relationships")
    conn.commit()
    conn.close()
    store = STORE.open_store(data / "life_store.db")
    for index in range(20):
        store.put_relationship({
            "user_id": f"2000{index}", "first_seen_at": now - 86400,
            "last_interaction_at": now - 600, "interaction_count": 2,
            "familiarity": 0.2 + 0.1 * index, "relation_hint": "", "shared_events": 0,
        })
    store.close()
    return data


def test_real_world_problems_are_all_reported(tmp_path):
    """真机报告里那些「看起来配了、其实没生效」的问题必须逐条报出来。"""

    now = time.time()
    data = _write_real_world(tmp_path, now)
    code, report, _name = _run(tmp_path, data)
    assert code == 1, report[-1500:]

    for needle in (
        "sleep_window 覆盖一天中的 24.0 小时",      # 全天睡眠窗 ⇒ FAIL
        "主动开口被硬闸挡住",                       # 电量 0 + proactive 开着
        "所有人都是「陌生」档",                     # 关系系数恒 1.0（M7 等价没开）
        "与 [simulation] dry_run = true 互相印证",  # 没写过倍率的原因说清楚了
        "演算模式开着",
    ):
        assert needle in report, needle


def test_no_runtime_data_never_claims_normal_operation(tmp_path):
    """没有真机数据时，**离线自检通过也不许说「正常运转」**。

    这是最容易犯的越权结论：代码/机制没问题 ≠ 部署没问题。判定必须落成
    「无法判断真机是否正常」，并给出补采集的办法。

    ⚠ 夹具用一个**最小插件目录**：直接拿本仓库当 plugin_dir 会让「机器上恰好在插件
    目录里留了假数据」污染结论（第一次写这条用例就是这么红的——本地 `_fake_maibot`
    被扫进了 data_dir），那就变成测机器而不是测逻辑了。
    """

    module = _load_diagnostics()
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "_manifest.json").write_text(json.dumps({
        "manifest_version": 2, "id": PLUGIN_ID, "version": "1.0.0",
    }), encoding="utf-8")
    (plugin / "plugin.py").write_text(PLUGIN_STUB, encoding="utf-8")

    report = module.build_report(
        plugin,
        data_dir=None, config_path=None, log_paths=[], search_roots=[tmp_path],
        simulate_hours=0.0, run_gates=False, now=time.time(), out_dir=tmp_path / "out",
    )
    headline, detail = module.verdict(report, has_state=False, simulated=True)
    assert "无法判断" in headline, (headline, [f.title for f in report.findings])
    assert "真机" in detail, detail

    body = dict(report.sections)["REPORT"]
    assert "正常运转" not in body.split("## 1.")[0], "摘要里不许出现「正常运转」"
    assert "本报告没有真机状态数据" in body


def test_state_and_store_signature_helpers(tmp_path):
    module = _load_diagnostics()

    assert module.looks_like_state({"activity": "daily", "emotion": 5.0, "energy": 6.0})
    assert not module.looks_like_state({"someone": "else"})
    assert not module.looks_like_state([])

    foreign_db = tmp_path / "foreign.db"
    import sqlite3

    conn = sqlite3.connect(str(foreign_db))
    conn.execute("CREATE TABLE lyrics (id INTEGER)")
    conn.commit()
    conn.close()
    assert module.looks_like_store(foreign_db) is False

    mine = tmp_path / "mine.db"
    store = STORE.open_store(mine)
    store.close()
    assert module.looks_like_store(mine) is True
