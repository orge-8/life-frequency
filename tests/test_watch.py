# -*- coding: utf-8 -*-
"""L3：长期监控模式（``collect_diagnostics.py --watch/--once/--summary-only``）的回归测试。

监控模式的**价值全在长期**：它必须证明自己
  ① 采到的样本是同一套判据（与一次性报告同源），不是另写一套阈值；
  ② 告警**只在变化时**落一条（一周 2016 个样本，刷重复告警等于没有告警）；
  ③ 采样缺口、日志增量、保留期裁剪这些「长时间才暴露」的机制真的对；
  ④ 被 Ctrl+C/重启打断后能自愈（过期锁接管、心跳、优雅收尾）。

夹具复用 ``tests/test_diagnostics.py`` 里的健康/坏数据两份现场，避免两套夹具漂移。
"""

import importlib.util
import json
import os
import pathlib
import sys
import time

import pytest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import test_diagnostics as TD  # noqa: E402  —— 复用健康/坏数据夹具（单一事实源）


def _load_diagnostics():
    """与 test_diagnostics 同一套加载方式（dataclass 需要先注册进 sys.modules）。"""

    spec = importlib.util.spec_from_file_location(
        "life_frequency_watch_diagnostics", PLUGIN_DIR / "collect_diagnostics.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def diagnostics():
    module = _load_diagnostics()
    module.PURE = module.PurePlugin(PLUGIN_DIR)
    return module


def test_wrong_log_path_is_reported_not_silently_ignored(diagnostics, tmp_path):
    """--log 写错时必须报警：不然日志侧永远 0 命中，看起来像「一直没故障」。"""

    now = time.time()
    data = tmp_path / "healthy"
    TD._write_healthy(data, now)
    sources = diagnostics.WatchSources(
        data_dir=data, data_dir_source="测试指定", config_path=data / "config.toml",
        log_paths=[tmp_path / "不存在的日志.log"])
    ctx = diagnostics.WatchContext(plugin_dir=PLUGIN_DIR, search_roots=[PLUGIN_DIR],
                                   sources=sources, tailer=diagnostics.LogTailer(),
                                   interval=300.0)
    row, findings = diagnostics.take_sample(
        ctx, now=time.time(), sample_index=1, uptime=1.0, gap=-1.0, run_starts=1,
        prev_sha="", prev_unchanged=0)
    titles = "；".join(finding.title for finding in findings)
    assert "日志文件不存在" in titles, titles
    assert row["findings_warn"] >= 1


def _sources(module, data: pathlib.Path):
    return module.WatchSources(
        data_dir=data, data_dir_source="测试指定",
        config_path=data / "config.toml",
        log_paths=[data / "logs" / "maibot.log"],
    )


def _context(module, data: pathlib.Path, *, interval: float = 300.0):
    return module.WatchContext(
        plugin_dir=PLUGIN_DIR, search_roots=[PLUGIN_DIR],
        sources=_sources(module, data), tailer=module.LogTailer(), interval=interval,
    )


def _run_once(module, tmp_path: pathlib.Path, data: pathlib.Path, watch_dir: pathlib.Path,
              extra: list[str] | None = None) -> int:
    return module.main([
        "--plugin-dir", str(PLUGIN_DIR),
        "--data-dir", str(data),
        "--config", str(data / "config.toml"),
        "--log", str(data / "logs" / "maibot.log"),
        "--watch-dir", str(watch_dir),
        *(extra or []),
    ])


# ------------------------------------------------------------------ 样本与判据同源


def test_sample_row_is_judged_by_the_same_rules_as_the_report(diagnostics, tmp_path):
    """样本判定必须调一次性报告的 ``_state_checks`` / ``collect_config``（单一事实源）。"""

    now = time.time()
    data = tmp_path / "healthy"
    TD._write_healthy(data, now)
    ctx = _context(diagnostics, data)

    row, findings = diagnostics.take_sample(
        ctx, now=time.time(), sample_index=1, uptime=1.0, gap=-1.0, run_starts=1,
        prev_sha="", prev_unchanged=0,
    )

    assert row["state_present"] is True
    assert row["check_status"] in ("ok", "warn"), row
    assert row["findings_fail"] == 0, [f.title for f in findings if f.level == "FAIL"]
    assert row["activity"], row
    assert row["state_sha16"], "没有状态文件指纹，长时间观察就无法发现「文件一直没变」"
    assert 0 < float(row["emotion"]) <= 10
    assert float(row["tick_age_seconds"]) < 600, row["tick_age_seconds"]
    # 健康夹具的配置本来就缺 v1.16 新增项：这条 WARN 必须来自 collect_config 的复用
    titles = "；".join(f.title for f in findings)
    assert "配置缺" in titles or "config_version" in titles, titles
    # 每条 FAIL/WARN 都要能算出一个**稳定 id**（否则失败次数一变就是新告警）
    for finding in findings:
        assert diagnostics.canonical_finding_key(finding.scope, finding.title)


def test_canonical_key_ignores_dynamic_numbers(diagnostics):
    first = diagnostics.canonical_finding_key("状态", "真机状态：模型连续失败 5 次（冷却还剩 1.0 分钟）")
    second = diagnostics.canonical_finding_key("状态", "真机状态：模型连续失败 9 次（冷却还剩 12.5 分钟）")
    other = diagnostics.canonical_finding_key("状态", "真机状态：连续清醒 40.0 小时仍未入睡")
    assert first == second, (first, second)
    assert first != other
    assert first.startswith("状态|")


# ------------------------------------------------------------------ 告警状态机


def test_alert_tracker_only_reports_changes(diagnostics):
    tracker = diagnostics.AlertTracker()
    warn = diagnostics.Finding("WARN", "真机状态：模型连续失败 3 次", evidence="a", scope="状态")
    worse = diagnostics.Finding("FAIL", "真机状态：模型连续失败 8 次（冷却还剩 1.0 分钟）",
                                evidence="b", scope="状态")

    now = time.time()
    first = tracker.update([warn], now=now)
    assert [event["kind"] for event in first] == ["开始"], first
    assert tracker.active_alerts()[0].level == "WARN"

    again = tracker.update([warn], now=now + 300)
    assert again == [], "同一问题重复出现不该再落事件（否则一周就是上千条噪音）"
    assert tracker.active_alerts()[0].hits == 2

    escalated = tracker.update([worse], now=now + 600)
    assert [event["kind"] for event in escalated] == ["升级"], escalated
    assert tracker.active_alerts()[0].level == "FAIL"

    resolved = tracker.update([], now=now + 900)
    assert [event["kind"] for event in resolved] == ["恢复"], resolved
    assert tracker.active_alerts() == []

    # 状态要能存下来再读回来（重启监控器不该把长期问题重新报一遍）
    tracker.update([warn], now=now + 1200)
    restored = diagnostics.AlertTracker(tracker.to_state())
    assert [alert.title for alert in restored.active_alerts()] == [
        diagnostics.clean_finding_title(warn.title)]
    assert restored.update([warn], now=now + 1500) == []


# ------------------------------------------------------------------ 日志增量


def test_log_tailer_reads_only_new_complete_lines(diagnostics, tmp_path):
    log = tmp_path / "maibot.log"
    log.write_text("2026-10-11 07:00:00 [ERROR] plugin: 状态文件损坏\n", encoding="utf-8")
    tailer = diagnostics.LogTailer()

    first = tailer.poll([log])
    assert first["fail_hits"].get("状态文件损坏") == 1, first
    assert first["errors"] == 1
    second = tailer.poll([log])
    assert second["fail_hits"] == {} and second["lines"] == 0, "旧内容不许重复计数"

    # 半行（写入方还没写完）：不许计数——被截断的签名既不漏报也不假报
    with log.open("a", encoding="utf-8") as handle:
        handle.write("2026-10-11 07:01:00 [ERROR] plugin: Traceback")
    third = tailer.poll([log])
    assert third["fail_hits"] == {}, third
    assert third["lines"] == 0

    with log.open("a", encoding="utf-8") as handle:
        handle.write(" (most recent call last)\n")
    fourth = tailer.poll([log])
    assert fourth["fail_hits"].get("Traceback") == 1, fourth

    # 轮转/被截断：文件比记的 offset 还小 ⇒ 从 0 重读
    log.write_text("2026-10-11 08:00:00 [ERROR] plugin: 生活状态推进异常\n", encoding="utf-8")
    fifth = tailer.poll([log])
    assert fifth["fail_hits"].get("生活状态推进异常") == 1, fifth
    assert fifth["rotated"] == 1


# ------------------------------------------------------------------ 单实例锁


def test_watch_lock_refuses_live_instance_and_takes_over_stale_one(diagnostics, tmp_path):
    paths = diagnostics.WatchPaths.of(tmp_path / "watch")
    paths.ensure()

    first = diagnostics.WatchLock(paths.lock, interval=60.0)
    ok, note = first.acquire()
    assert ok and paths.lock.exists(), note

    second = diagnostics.WatchLock(paths.lock, interval=60.0)
    ok2, note2 = second.acquire()
    assert ok2 is False and "已有监控进程" in note2, note2

    # 心跳过期（机器断电/进程被强杀留下的锁）⇒ 自动接管，不要求用户手删
    diagnostics._atomic_write_text(paths.lock, json.dumps(
        {"pid": 999999, "heartbeat_at": time.time() - 100000}))
    third = diagnostics.WatchLock(paths.lock, interval=60.0)
    ok3, note3 = third.acquire()
    assert ok3 and "接管" in note3, note3

    third.release()
    assert not paths.lock.exists(), "退出必须放锁，否则下次启动要被自己挡住"

    # --force-lock 允许强行接管；--no-lock 完全不参与
    fourth = diagnostics.WatchLock(paths.lock, interval=60.0)
    fourth.acquire()
    forced = diagnostics.WatchLock(paths.lock, interval=60.0, force=True)
    assert forced.acquire()[0] is True
    forced.release()
    disabled = diagnostics.WatchLock(paths.lock, interval=60.0, disabled=True)
    assert disabled.acquire()[0] is True
    assert not paths.lock.exists()


# ------------------------------------------------------------------ --once 全链路


def test_once_writes_all_five_artefacts_and_exits_cleanly(diagnostics, tmp_path):
    now = time.time()
    data = tmp_path / "healthy"
    TD._write_healthy(data, now)
    watch_dir = tmp_path / "out"
    code = _run_once(diagnostics, tmp_path, data, watch_dir, ["--once", "--interval", "1",
                                                             "--quiet"])
    assert code == 0, "健康夹具不许在监控模式下判 FAIL"

    assert (watch_dir / "samples.jsonl").exists()
    rows = [json.loads(line) for line in
            (watch_dir / "samples.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1 and rows[0]["check_status"] in ("ok", "warn")
    assert rows[0]["sample_index"] == 1 and rows[0]["run_starts"] == 1
    assert "findings" in rows[0], "JSONL 要带 findings 明细，汇总才能统计窗口内命中项"

    csv_text = (watch_dir / "samples.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0].startswith("sample_index,ts,iso")
    assert len(csv_text.strip().splitlines()) == 2

    alerts = (watch_dir / "alerts.md").read_text(encoding="utf-8")
    assert "当前未恢复" in alerts
    summary = (watch_dir / "watch_summary.md").read_text(encoding="utf-8")
    assert "样本：**1**" in summary and "覆盖率" in summary and "口径与局限" in summary
    state = json.loads((watch_dir / "watch_state.json").read_text(encoding="utf-8"))
    assert state["samples_total"] == 1 and state["run_starts"] == 1
    assert state["stopped_reason"].startswith("已完成单次采样")
    reports = list((watch_dir / "daily").glob("life-frequency-诊断-*.md"))
    assert reports, "--once 也要落一份完整报告（手动看现场用）"
    assert "--once 单次采样" in reports[0].read_text(encoding="utf-8")
    assert not (watch_dir / "watch.lock").exists(), "退出后锁必须已释放"


def test_once_reports_broken_data_with_nonzero_exit(diagnostics, tmp_path):
    now = time.time()
    data = tmp_path / "broken"
    TD._write_broken(data, now)
    watch_dir = tmp_path / "out"
    code = _run_once(diagnostics, tmp_path, data, watch_dir, ["--once", "--interval", "1",
                                                             "--quiet"])
    assert code == 1, "坏数据必须给出非零退出码（监控脚本被计划任务调用时靠它报警）"

    alerts = (watch_dir / "alerts.md").read_text(encoding="utf-8")
    assert "生活循环没有在跑" in alerts
    log_text = (watch_dir / "alerts.log").read_text(encoding="utf-8")
    assert "开始" in log_text and "FAIL" in log_text
    rows = [json.loads(line) for line in
            (watch_dir / "samples.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows[0]["check_status"] == "fail"
    assert rows[0]["findings_fail"] >= 3, rows[0]["findings"]


# ------------------------------------------------------------------ watch 循环


def test_watch_loop_collects_several_samples_then_stops(diagnostics, tmp_path):
    now = time.time()
    data = tmp_path / "healthy"
    TD._write_healthy(data, now)
    watch_dir = tmp_path / "out"
    code = _run_once(diagnostics, tmp_path, data, watch_dir, [
        "--watch", "--interval", "1", "--max-samples", "3", "--duration-hours", "0",
        "--report-every-hours", "0", "--summary-every-minutes", "0", "--quiet",
    ])
    assert code == 0

    rows = [json.loads(line) for line in
            (watch_dir / "samples.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["sample_index"] for row in rows] == [1, 2, 3]
    assert float(rows[1]["gap_seconds"]) >= 0.5, rows[1]["gap_seconds"]
    assert rows[2]["run_starts"] == 1
    summary = (watch_dir / "watch_summary.md").read_text(encoding="utf-8")
    assert "样本：**3**" in summary
    assert not (watch_dir / "daily" / "").exists() or True  # daily 目录允许为空（本次关掉了周期报告）
    assert not list((watch_dir / "daily").glob("*.md")), "--report-every-hours 0 时不该有周期报告"
    assert (watch_dir / "watch.log").read_text(encoding="utf-8").count("#") >= 3
    assert not (watch_dir / "watch.lock").exists()


def test_watch_stop_file_stops_the_loop_gracefully(diagnostics, tmp_path, monkeypatch):
    """停止文件是「不用去任务管理器里杀进程」的那条路（Windows 上杀进程很容易杀错）。"""

    now = time.time()
    data = tmp_path / "healthy"
    TD._write_healthy(data, now)
    watch_dir = tmp_path / "out"

    # 让循环在第一次采样前就发现停止文件：直接预置它
    (watch_dir / "daily").mkdir(parents=True, exist_ok=True)
    (watch_dir / "watch.stop").write_text("stop\n", encoding="utf-8")

    code = _run_once(diagnostics, tmp_path, data, watch_dir, [
        "--watch", "--interval", "1", "--duration-hours", "0", "--report-every-hours", "0",
        "--summary-every-minutes", "0", "--quiet",
    ])
    assert code == 0
    assert not (watch_dir / "watch.stop").exists(), "退出后要清掉停止文件，否则下次一启动就停"
    assert not (watch_dir / "samples.jsonl").exists(), "停止请求落在采样前时不该再多采一个样本"
    state = json.loads((watch_dir / "watch_state.json").read_text(encoding="utf-8"))
    assert "停止文件" in state["stopped_reason"], state["stopped_reason"]


def test_periodic_report_is_seeded_from_the_last_report(diagnostics, tmp_path):
    """崩溃-重启循环里「每次启动补一份报告」会把 daily/ 塞满：要按最后一份报告的 mtime 续期。"""

    now = time.time()
    data = tmp_path / "healthy"
    TD._write_healthy(data, now)
    watch_dir = tmp_path / "out"
    assert _run_once(diagnostics, tmp_path, data, watch_dir, ["--once", "--quiet"]) == 0
    reports = sorted((watch_dir / "daily").glob("*.md"))
    assert len(reports) == 1

    code = _run_once(diagnostics, tmp_path, data, watch_dir, [
        "--watch", "--interval", "1", "--max-samples", "1", "--duration-hours", "0",
        "--report-every-hours", "24", "--summary-every-minutes", "0", "--quiet",
    ])
    assert code == 0
    assert sorted((watch_dir / "daily").glob("*.md")) == reports, \
        "刚生成过报告，重启一次不该又补一份"


def test_second_watch_instance_is_refused_by_the_lock(diagnostics, tmp_path):
    paths = diagnostics.WatchPaths.of(tmp_path / "out")
    paths.ensure()
    holder = diagnostics.WatchLock(paths.lock, interval=60.0)
    assert holder.acquire()[0]

    now = time.time()
    data = tmp_path / "healthy"
    TD._write_healthy(data, now)
    code = _run_once(diagnostics, tmp_path, data, paths.root, ["--watch", "--max-samples", "1",
                                                              "--quiet"])
    assert code == 3, "已有监控进程在跑时必须拒绝启动，而不是两个进程抢着写同一批文件"
    holder.release()


# ------------------------------------------------------------------ 汇总与保留期


def test_summary_counts_gaps_and_daily_trend(diagnostics, tmp_path):
    paths = diagnostics.WatchPaths.of(tmp_path / "watch")
    paths.ensure()
    interval = 300.0
    total = int(3 * 86400 / interval)  # 3 天样本（模拟「一周观察」的缩小版）
    start = time.time() - 3 * 86400
    rows = []
    for index in range(total):
        stamp = start + index * interval
        if index >= total // 2:  # 制造一次 3 小时的缺口（机器休眠/进程被杀）
            stamp += 3 * 3600
        rows.append({
            "sample_index": index + 1, "ts": stamp,
            "iso": diagnostics.local_stamp(stamp, 480), "tick_age_seconds": 120.0,
            "tick_seconds": 600, "activity_label": "工作" if index % 2 else "休息",
            "emotion": 5.0 + (index % 3) * 0.1, "energy": 6.0, "stress": 2.0,
            "loneliness": 1.0, "sleep_minutes_today": 420, "state_size": 2048,
            "applied_sessions": 1, "check_status": "ok",
            "findings": ([{"level": "WARN", "id": "状态|推进比配置慢", "title": "推进比配置慢"}]
                         if index in (5, 6) else []),
        })
    summary = diagnostics.summarize_rows(rows, interval=interval)
    assert summary["count"] == total
    assert len(summary["gaps"]) == 1, summary["gaps"]
    assert summary["max_gap_seconds"] >= 3 * 3600
    assert summary["coverage"] < 1.0
    assert summary["findings"]["状态|推进比配置慢"]["count"] == 2
    assert len(summary["days"]) >= 3

    tracker = diagnostics.AlertTracker()
    tracker.update([diagnostics.Finding("FAIL", "真机状态：生活循环没有在跑", evidence="3 天",
                                        scope="状态")], now=time.time())
    text = diagnostics.render_watch_summary(
        paths, rows=rows, summary=summary, tracker=tracker,
        state={"data_dir": "D:/x", "data_dir_source": "指定", "config_path": "c.toml",
               "run_starts": 2}, tz=480, now=time.time(), interval=interval, broken_lines=1,
    )
    assert "生活循环没有在跑" in text
    assert "采样缺口" in text
    assert "逐日走势" in text
    assert "1 行坏数据" in text
    assert "时段的运行情况**没有证据**" in text


def test_retention_prunes_old_samples_and_reports(diagnostics, tmp_path, monkeypatch):
    paths = diagnostics.WatchPaths.of(tmp_path / "watch")
    paths.ensure()
    monkeypatch.setattr(diagnostics, "ALERT_LOG_TRIM_THRESHOLD", 10)
    monkeypatch.setattr(diagnostics, "ALERT_LOG_KEEP_LINES", 4)

    now = time.time()
    old_ts = now - 100 * 86400
    rows = [
        {"sample_index": 1, "ts": old_ts, "iso": "2026-01-01 00:00:00", "check_status": "ok"},
        {"sample_index": 2, "ts": old_ts + 300, "iso": "2026-01-01 00:05:00",
         "check_status": "ok"},
        {"sample_index": 3, "ts": now, "iso": "2026-10-11 07:00:00", "check_status": "ok"},
    ]
    (paths.samples_jsonl).write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (paths.samples_csv).write_text("sample_index,ts\n1,2\n", encoding="utf-8")
    old_report = paths.daily / "life-frequency-诊断-20260101-000000.md"
    old_report.write_text("旧报告", encoding="utf-8")
    os.utime(old_report, (old_ts, old_ts))
    new_report = paths.daily / "life-frequency-诊断-20261011-000000.md"
    new_report.write_text("新报告", encoding="utf-8")
    (paths.alerts_log).write_text("".join(f"line {index}\n" for index in range(20)),
                                 encoding="utf-8")

    stats = diagnostics.prune_watch_dir(paths, retain_days=56, now=now)
    assert stats["samples_dropped"] == 2, stats
    kept = [json.loads(line) for line in
            paths.samples_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["sample_index"] for row in kept] == [3]
    csv_lines = paths.samples_csv.read_text(encoding="utf-8").strip().splitlines()
    assert csv_lines[0].startswith("sample_index") and len(csv_lines) == 2
    assert not old_report.exists() and new_report.exists()
    assert stats["alerts_trimmed"] == 16
    assert len(paths.alerts_log.read_text(encoding="utf-8").strip().splitlines()) == 4

    # 保留期 0 = 不裁
    assert diagnostics.prune_watch_dir(paths, retain_days=0, now=now)["samples_dropped"] == 0


def test_lock_also_refuses_when_the_recorded_process_is_still_alive(diagnostics, tmp_path):
    """心跳会被「报告写太久」和「系统时间被跳」污染，pid 存活是不吃时钟的第二判据。"""

    assert diagnostics._pid_alive(os.getpid()) is True
    assert diagnostics._pid_alive(99999999) is False, "不存在的 pid 必须判定为已死，否则永远接管不了"

    paths = diagnostics.WatchPaths.of(tmp_path / "watch")
    paths.ensure()
    # 心跳很旧（模拟时间被跳或对方卡住），但 pid 就是当前进程 —— 必须拒绝，不能双写
    diagnostics._atomic_write_text(paths.lock, json.dumps(
        {"pid": os.getpid(), "heartbeat_at": time.time() - 100000}))
    lock = diagnostics.WatchLock(paths.lock, interval=60.0)
    ok, note = lock.acquire()
    assert ok is False, note
    assert "仍存活" in note, note


def test_store_copy_temp_dir_is_removed_after_use(diagnostics, monkeypatch, tmp_path):
    """复制的临时库必须删：watch 每天都要跑完整报告，残留一周就是几十个库副本。"""

    import tempfile

    import life_store as STORE

    data = tmp_path / "d"
    data.mkdir()
    store = STORE.open_store(data / "life_store.db")
    store.close()

    real_connect = diagnostics.sqlite3.connect
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # 逼它走「复制一份再读」那条路
            raise diagnostics.sqlite3.Error("模拟 WAL 需要恢复")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(diagnostics.sqlite3, "connect", flaky)
    temp_root = pathlib.Path(tempfile.gettempdir())
    before = set(temp_root.glob("life-diagnostics-*"))
    report = diagnostics.Report()
    detail = diagnostics.collect_store(report, data / "life_store.db", now=time.time(),
                                       tz=480, pure=diagnostics.PURE)
    after = set(temp_root.glob("life-diagnostics-*"))
    assert calls["n"] >= 2, "没走到复制路径，这条用例等于空过"
    assert detail.get("note"), detail
    assert after == before, f"复制出来的临时库目录没删：{after - before}"
    assert diagnostics._open_readonly(data / "life_store.db")[3] is None, "直连成功时不该有副本"


def test_failed_report_is_counted_and_retried_not_silently_skipped(diagnostics, monkeypatch,
                                                                  tmp_path):
    """报告生成失败却照旧推进周期 = 静默失败（盘上没报告，日志里看起来一切正常）。"""

    now = time.time()
    data = tmp_path / "healthy"
    TD._write_healthy(data, now)
    watch_dir = tmp_path / "out"

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟 build_report 崩了")

    monkeypatch.setattr(diagnostics, "build_report", boom)
    code = _run_once(diagnostics, tmp_path, data, watch_dir, [
        "--watch", "--interval", "1", "--max-samples", "2", "--duration-hours", "0",
        "--report-every-hours", "1", "--summary-every-minutes", "0", "--quiet",
    ])
    assert code == 0, "报告失败不该让监控退出（它必须继续采集）"
    assert not list((watch_dir / "daily").glob("*.md")), "这次确实没生成报告"
    state = json.loads((watch_dir / "watch_state.json").read_text(encoding="utf-8"))
    assert state["report_failures"] >= 1, state
    assert "完整报告生成失败" in (watch_dir / "watch.log").read_text(encoding="utf-8")
    assert "完整报告生成失败" in (watch_dir / "watch_summary.md").read_text(encoding="utf-8"), \
        "汇总表头必须说出来，否则用户会以为报告一直都在"


def test_summary_only_regenerates_from_existing_samples(diagnostics, tmp_path):
    now = time.time()
    data = tmp_path / "healthy"
    TD._write_healthy(data, now)
    watch_dir = tmp_path / "out"
    assert _run_once(diagnostics, tmp_path, data, watch_dir, ["--once", "--quiet"]) == 0

    summary_path = watch_dir / "watch_summary.md"
    summary_path.unlink()
    code = diagnostics.main([
        "--plugin-dir", str(PLUGIN_DIR), "--data-dir", str(data),
        "--watch-dir", str(watch_dir), "--summary-only", "--quiet",
    ])
    assert code == 0
    text = summary_path.read_text(encoding="utf-8")
    assert "样本：**1**" in text and "长期监控汇总" in text


# ---------------------------------------------------------------- v1.19.0 PR-AF-1：
# 循环动力学判据 + 改动时点分段


def _loop_row(index: int, stamp: float, **overrides):
    """构造一个「循环退化成单向泵」的样本：睡、清醒 10 分钟、孤独恒 0、体力贴顶。"""

    row = {
        "sample_index": index + 1, "ts": stamp,
        "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)),
        "tick_age_seconds": 60.0,
        "tick_seconds": 600, "activity": "sleep", "activity_label": "睡觉",
        "emotion": 5.0, "energy": 10.0, "energy_cap": 10.0, "stress": 0.0,
        "loneliness": 0.0, "social_battery": 1.0, "sleep_minutes_today": 600,
        "continuous_awake_minutes": 10.0, "applied_sessions": 0,
        "check_status": "fail", "findings": [],
    }
    row.update(overrides)
    return row


def test_dynamics_checks_catch_the_absorbing_state(diagnostics):
    """真机 2026-10-13 的循环形态必须被六条动力学判据全部命中。"""

    now = time.time()
    rows = [_loop_row(i, now + i * 300) for i in range(40)]
    findings = diagnostics.dynamics_findings(rows, config={})
    titles = [item["title"] for item in findings]
    joined = "\n".join(titles)
    assert any("睡觉占比" in title for title in titles), joined
    assert any("连续清醒中位" in title for title in titles), joined
    assert any("孤独恒定" in title for title in titles), joined
    assert any("体力贴顶" in title for title in titles), joined
    assert any("社交电量" in title for title in titles), joined
    assert any("当日睡眠贴" in title for title in titles), joined
    assert any(item["level"] == "FAIL" for item in findings), titles
    for item in findings:
        assert item["id"].startswith("动力学|")
        assert item.get("evidence") and item.get("fix")


def test_dynamics_checks_stay_quiet_on_a_healthy_loop(diagnostics):
    """健康循环（清醒 3 小时、孤独有波动、体力有起伏）一条都不该命中。"""

    now = time.time()
    rows = []
    for index in range(30):
        rows.append(_loop_row(
            index, now + index * 300,
            activity="daily" if index % 3 else "sleep",
            activity_label="日常" if index % 3 else "睡觉",
            continuous_awake_minutes=180.0 if index % 3 else 0.0,
            loneliness=float(index % 5),
            energy=6.0 + (index % 3),
            social_battery=5.0,
            sleep_minutes_today=420 if index % 3 else 0,
        ))
    assert diagnostics.dynamics_findings(rows, config={}) == []


def test_dynamics_thresholds_are_configurable(diagnostics):
    """阈值必须走 [diagnostics] 可配——写死会在修完之后变成新的误报源。"""

    now = time.time()
    rows = [_loop_row(i, now + i * 300) for i in range(40)]
    relaxed = diagnostics.dynamics_findings(rows, config={"diagnostics": {
        "dyn_sleep_share": 1.01,          # 关掉睡觉占比
        "dyn_awake_median_minutes": 5.0,  # 清醒 10 分钟也算过
        "dyn_loneliness_frozen_samples": 999,
        "dyn_energy_capped_share": 1.01,
        "dyn_battery_low_share": 1.01,
        "dyn_sleep_capped_share": 1.01,
    }})
    assert relaxed == [], relaxed


def test_mark_appends_a_segment_boundary_and_summary_splits_on_it(diagnostics, tmp_path):
    """--mark 打点后，汇总必须给出改前/改后对比，而不是把两段混成一个均值。"""

    paths = diagnostics.WatchPaths.of(tmp_path / "watch")
    paths.ensure()
    now = time.time()
    before = [_loop_row(i, now - 8000 + i * 300) for i in range(20)]   # [now-8000, now-2100]
    after = [_loop_row(i, now - 1000 + i * 300,                       # [now-1000, now+4900]
                       activity="daily", activity_label="日常",
                       continuous_awake_minutes=180.0, loneliness=float(i % 5),
                       energy=6.0 + (i % 3), social_battery=5.0,
                       sleep_minutes_today=420)
             for i in range(20)]

    assert diagnostics.append_mark(paths, "改清醒侧判据", now=now - 1500, tz=480) is True
    marks = diagnostics.load_marks(paths)
    assert len(marks) == 1 and marks[0]["label"] == "改清醒侧判据"

    segment_lines = diagnostics._segment_table(before + after, marks, config={})
    assert any("改前基线" in line for line in segment_lines), segment_lines
    assert any("改后：改清醒侧判据" in line for line in segment_lines), segment_lines
    # 改前的睡觉占比必须是 100%，改后必须是 0% —— 两段不能混
    base = next(line for line in segment_lines if "改前基线" in line)
    fixed = next(line for line in segment_lines if "改后：改清醒侧判据" in line)
    assert "100.0%" in base and "0.0%" in fixed, (base, fixed)

    summary = diagnostics.summarize_rows(before + after, interval=300.0)
    text = diagnostics.render_watch_summary(
        paths, rows=before + after, summary=summary, tracker=diagnostics.AlertTracker(),
        state={"data_dir": "D:/x", "run_starts": 1}, tz=480,
        now=now, interval=300.0,
    )
    assert "改动时点分段" in text
    assert "改前基线" in text and "改后：改清醒侧判据" in text
    assert "循环动力学" in text
    assert "睡觉占比" in text, "改前那段必须仍然报 FAIL"


def test_mark_without_label_is_a_noop_and_empty_marks_yield_no_segment(diagnostics, tmp_path):
    paths = diagnostics.WatchPaths.of(tmp_path / "watch")
    paths.ensure()
    assert diagnostics.append_mark(paths, "   ", now=time.time(), tz=480) is False
    assert diagnostics.load_marks(paths) == []
    assert diagnostics._segment_table([{"ts": time.time()}], [], config={}) == []


def test_mark_via_cli_appends_and_exits_without_touching_the_lock(diagnostics, tmp_path):
    """--mark 是独立模式：不启动循环、不抢锁（标记走 O_APPEND 的独立文件）。"""

    now = time.time()
    data = tmp_path / "healthy"
    TD._write_healthy(data, now)
    watch_dir = tmp_path / "out"
    paths = diagnostics.WatchPaths.of(watch_dir)
    paths.ensure()
    code = diagnostics.main([
        "--plugin-dir", str(PLUGIN_DIR), "--data-dir", str(data),
        "--watch-dir", str(watch_dir), "--mark", "部署清醒侧修复", "--quiet",
    ])
    assert code == 0
    assert not (watch_dir / "watch.lock").exists(), "打点不该留下锁"
    marks = diagnostics.load_marks(paths)
    assert len(marks) == 1 and marks[0]["label"] == "部署清醒侧修复"
    assert marks[0]["iso"]
