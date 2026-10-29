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
                    now_minutes=facts.now_minutes,
                    sleep_need=float(facts.sleep_need),
                    sleep_need_onset=config.sleep_need_onset,
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
    # v1.24.0：睡眠时段退役（睡不睡只看困意），诊断不再按窗口长短判作息——
    # 无论 24h 还是 03:00-11:00 都只报一条「已退役」，不许再出「覆盖全天」的告警。
    assert "总判定" in report
    assert "sleep_window 覆盖一天中的" not in report
    assert code in (0, 1)

    for needle in (
        "主动开口被硬闸挡住",                       # 电量 0 + proactive 开着
        "所有人都是「陌生」档",                     # 关系系数恒 1.0（M7 等价没开）
        "演算模式开着",                             # dry_run 仍在（没设 expect_dry_run）
    ):
        assert needle in report, needle

    # v1.24.0：窗口字段保留但不再参与判定，报告必须说清楚「改它没用」；
    # dry_run 是有意的观测阶段选择，改报 OK。
    for needle in (
        "sleep_window 已退役（v1.24.0 起作息由困意决定）",
        "演算模式下不写宿主（符合预期）",
    ):
        assert needle in report, needle

    # 生效配置里也不许再列 sleep_window——列出来等于宣称它还在起作用。
    assert "| `sleep_window` |" not in report
    assert "| `sleep_need_onset` |" in report


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


# ---------------------------------------------------------------- v1.2.0（D10）：签名命中原文


def test_log_tailer_captures_signature_excerpts_with_stack(tmp_path):
    """增量采样必须把命中**原文**收走（Traceback 栈后文跟着进），且第二轮不重复计数。"""

    module = _load_diagnostics()
    log_file = tmp_path / "app.log"
    log_file.write_text(
        "2026-10-13 08:46:30 INFO some normal line\n"
        "2026-10-13 08:46:31 WARNING plugin.org.orge-8.life-frequency: Traceback (most recent call last)\n"
        '  File "plugin.py", line 42, in tick\n'
        "RuntimeError: 宿主能力调用超时\n"
        "2026-10-13 08:47:00 INFO nobody tagged me at all\n",
        encoding="utf-8",
    )
    tailer = module.LogTailer()
    result = tailer.poll([log_file])
    assert result["fail_hits"].get("Traceback") == 1
    excerpts = result["signature_excerpts"]
    assert excerpts and "[Traceback]" in excerpts[0], "命中原文必须落进 excerpts"
    assert "RuntimeError" in excerpts[0], "异常栈后文必须跟着进原文，否则归因还是没有证据"
    assert "nobody tagged me at all" in excerpts[0], "FAIL 命中带 4 行后文（栈在这里结束）"
    # 第二轮：offset 已推进，不许重复计数或重复收原文
    again = tailer.poll([log_file])
    assert again["fail_hits"] == {} and again["signature_excerpts"] == []


def test_signature_excerpts_cover_lines_without_plugin_tag():
    """D10 根因回归：宿主日志里**不带插件标签**的 Traceback/E_TIMEOUT 也要收到原文。

    一次性报告旧版只统计「本插件的行」，于是 FAIL 告警说「daily/ 里有原文」、
    原文却永远不在那里（18h 真机窗口实际踩中）。
    """

    module = _load_diagnostics()
    lines = [
        "2026-10-13 09:22:09 [ERROR] maisaka planner: something failed",
        "Traceback (most recent call last):",
        '  File "runtime.py", line 7, in run',
        "TimeoutError: E_TIMEOUT after 30s",
    ]
    blocks = module._signature_excerpts(lines, source_name="app.jsonl")
    assert blocks, "不带插件标签的命中行必须收到原文"
    assert "E_TIMEOUT" in blocks[0] and "TimeoutError" in blocks[0]
    # WARN 签名只收命中行本身
    warn_lines = ["2026-10-13 09:26:30 INFO 写不进去 group-1"]
    warn_blocks = module._signature_excerpts(warn_lines, source_name="app.jsonl")
    assert len(warn_blocks) == 1 and "写不进去" in warn_blocks[0]


def test_append_log_excerpts_rotates_when_too_big(tmp_path):
    """环形文件：超过上限时旧文件让位（.old.jsonl），新条目照常追加。"""

    module = _load_diagnostics()
    paths = module.WatchPaths.of(tmp_path)
    paths.ensure()
    paths.log_excerpts.write_text("x" * (module.LOG_EXCERPTS_MAX_BYTES + 1), encoding="utf-8")
    module.append_log_excerpts(paths, ["[Traceback] app.log:1\nboom"],
                               now=1_700_000_000.0, tz=0)
    assert paths.log_excerpts.exists()
    assert paths.log_excerpts.read_text(encoding="utf-8").count("[Traceback]") == 1
    assert (tmp_path / "log_excerpts.old.jsonl").exists(), "超限的旧文件要让位"


# ---------------------------------------------------------------- 签名判据（v1.20.1）


def _sig(line):
    import collect_diagnostics as CD

    return CD.match_signatures(line)


def test_signature_ignores_other_plugins_info_startup_lines():
    """误报 1：别的插件的**正常 INFO 启动日志**里带「冷却」二字（真机 42 条）。"""

    for line in (
        '{"event": "已恢复限频状态快照（冷却/每日上限/每分钟反应窗口）", '
        '"logger": "plugin.TAIY2020.smart_poke_plugin", "level": "info"}',
        '{"event": "复读机与自主撤回已加载（复读=True 阈值=5 冷却=3600s；撤回=True 自评=True）", '
        '"logger": "plugin.org.mai-mai.repeater-recall", "level": "info"}',
    ):
        assert _sig(line) == (None, None), line


def test_signature_ignores_our_own_status_card_body():
    """误报 2：本插件**自己发出去的 `/生活 状态` 卡片正文**里含「写不进去」（真机 12 条）。

    卡面里本来就有「写不进去（退避中）：N 个会话」这一行——那是**诊断措辞**，
    不是故障。它出现在 JSON 的 ``event`` 字段里、且在换行之后。
    """

    line = (
        '{"logger_name": "chat", "event": "命令处理完成，跳过后续消息处理: 🌙 生活频率 · 状态\\n'
        '活动：听歌\\n　来源：模型决定 · 已持续 6 分钟\\n'
        '写不进去（退避中）：1 个会话（最早约 3 分钟后重试）", "level": "info"}'
    )
    assert _sig(line) == (None, None)


def test_signature_ignores_model_reasoning_text():
    """误报 3：**模型自己的推理文本**里出现「冷却」（无关命中）。"""

    line = '{"text": "**分析：**\\n\\n当前场景：她社交电量低，先冷却一会儿再开口。", "msg_id": "1"}'
    assert _sig(line) == (None, None)


def test_signature_still_catches_a_real_warn_from_our_plugin():
    """真信号不能丢：本插件的 economy 降级（warning 级，单行）。"""

    line = (
        '{"event": "经济维度：读取预算失败（api_error：未找到 API 提供方插件: '
        'org.orge-8.budget-pacer）；已降级为「未接入」，30 分钟后重试", '
        '"logger": "plugin.org.orge-8.life-frequency", "level": "warning"}'
    )
    assert _sig(line) == (None, "已降级")


def test_signature_keeps_failures_whose_stack_lives_in_the_exception_field():
    """**FAIL 扫整行**：真故障的栈常常只挂在 ``exception`` 字段里（level=error）。

    这一条是漏报守卫——第一版判据为了消误报做了「按行截断」，
    结果把这类真实错误（如宿主「适配器名单禁止创建聊天运行实例」）一起截掉了。
    漏报比误报更糟，所以 FAIL 类签名不截断，只滤 debug/info。
    """

    line = (
        '{"logger_name": "heartflow", "event": "创建心流聊天 e82ef538 失败: '
        '适配器名单禁止创建聊天运行实例: e82ef538", "level": "error", "lineno": 55, '
        '"exception": "Traceback (most recent call last):\\n  File \\"x.py\\", line 1"}'
    )
    assert _sig(line) == ("Traceback", None)

    # 裸的 Python traceback 头（没有 level 字段）同样要留住
    assert _sig("Traceback (most recent call last):") == ("Traceback", None)

    # 关机超时（warning 级、单行）也要留住
    assert _sig(
        '{"event": "Runner shutdown 请求失败: reason=host_stop '
        'error=[E_TIMEOUT] 请求 plugin.shutdown 超时 (5000ms)", "level": "warning"}'
    ) == ("E_TIMEOUT", None)


def test_signature_skips_debug_and_info_entirely():
    """debug/info 级整行不参与判据——正常轮次不是故障。"""

    assert _sig('{"event": "Traceback 处理完成（重试成功）", "level": "info"}') == (None, None)
    assert _sig('{"event": "E_TIMEOUT 已恢复", "level": "debug"}') == (None, None)


# ---------------------------------------------------------------- 签名判据（v1.20.1）


def _sig(line):
    import collect_diagnostics as CD

    return CD.match_signatures(line)


def test_signature_ignores_other_plugins_info_startup_lines():
    """误报 1：别的插件的**正常 INFO 启动日志**里带「冷却」二字（真机 42 条）。"""

    for line in (
        '{"event": "已恢复限频状态快照（冷却/每日上限/每分钟反应窗口）", '
        '"logger": "plugin.TAIY2020.smart_poke_plugin", "level": "info"}',
        '{"event": "复读机与自主撤回已加载（复读=True 阈值=5 冷却=3600s；撤回=True 自评=True）", '
        '"logger": "plugin.org.mai-mai.repeater-recall", "level": "info"}',
    ):
        assert _sig(line) == (None, None), line


def test_signature_ignores_our_own_status_card_body():
    """误报 2：本插件**自己发出去的 `/生活 状态` 卡片正文**里含「写不进去」（真机 12 条）。

    卡面里本来就有「写不进去（退避中）：N 个会话」这一行——那是**诊断措辞**，
    不是故障。它出现在 JSON 的 ``event`` 字段里、且在换行之后。
    """

    line = (
        '{"logger_name": "chat", "event": "命令处理完成，跳过后续消息处理: 🌙 生活频率 · 状态\n'
        '活动：听歌\n　来源：模型决定 · 已持续 6 分钟\n'
        '写不进去（退避中）：1 个会话（最早约 3 分钟后重试）", "level": "info"}'
    )
    assert _sig(line) == (None, None)


def test_signature_ignores_model_reasoning_text():
    """误报 3：**模型自己的推理文本**里出现「冷却」（无关命中）。"""

    line = '{"text": "**分析：**\n\n当前场景：她社交电量低，先冷却一会儿再开口。", "msg_id": "1"}'
    assert _sig(line) == (None, None)


def test_signature_still_catches_a_real_warn_from_our_plugin():
    """真信号不能丢：本插件的 economy 降级（warning 级，单行）。"""

    line = (
        '{"event": "经济维度：读取预算失败（api_error：未找到 API 提供方插件: '
        'org.orge-8.budget-pacer）；已降级为「未接入」，30 分钟后重试", '
        '"logger": "plugin.org.orge-8.life-frequency", "level": "warning"}'
    )
    assert _sig(line) == (None, "已降级")


def test_signature_keeps_failures_whose_stack_lives_in_the_exception_field():
    """**FAIL 扫整行**：真故障的栈常常只挂在 ``exception`` 字段里（level=error）。

    这一条是漏报守卫——第一版判据为了消误报做了「按行截断」，
    结果把这类真实错误（如宿主「适配器名单禁止创建聊天运行实例」）一起截掉了。
    漏报比误报更糟，所以 FAIL 类签名不截断，只滤 debug/info。
    """

    line = (
        '{"logger_name": "heartflow", "event": "创建心流聊天 e82ef538 失败: '
        '适配器名单禁止创建聊天运行实例: e82ef538", "level": "error", "lineno": 55, '
        '"exception": "Traceback (most recent call last):\n  File \\"x.py\\", line 1"}'
    )
    assert _sig(line) == ("Traceback", None)

    # 裸的 Python traceback 头（没有 level 字段）同样要留住
    assert _sig("Traceback (most recent call last):") == ("Traceback", None)

    # 关机超时（warning 级、单行）也要留住
    assert _sig(
        '{"event": "Runner shutdown 请求失败: reason=host_stop '
        'error=[E_TIMEOUT] 请求 plugin.shutdown 超时 (5000ms)", "level": "warning"}'
    ) == ("E_TIMEOUT", None)


def test_signature_skips_debug_and_info_entirely():
    """debug/info 级整行不参与判据——正常轮次不是故障。"""

    assert _sig('{"event": "Traceback 处理完成（重试成功）", "level": "info"}') == (None, None)
    assert _sig('{"event": "E_TIMEOUT 已恢复", "level": "debug"}') == (None, None)
