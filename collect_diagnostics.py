#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""life-frequency 数据采集与运行诊断（**独立程序**：只读、零第三方依赖、不需要 maibot_sdk）。

它做一件事：把「判断这个插件是否正常运转」所需的所有证据收集到**一个可读文件**里，
并对数据做逐项判定（FAIL / WARN / OK / UNKNOWN），每条都给出「为什么这算不正常」与
「怎么修」。结论里明确区分「确定的故障」「可疑」与「数据不足、无法判断」——不猜。

    python collect_diagnostics.py                          # 采集本目录 + 自动搜索运行时数据
    python collect_diagnostics.py --data-dir <插件数据目录>  # 指定 life_state.json 所在目录
    python collect_diagnostics.py --config <config.toml>    # 指定宿主落盘的配置
    python collect_diagnostics.py --log <MaiBot 日志文件>    # 带上日志（可多次）
    python collect_diagnostics.py --offline-simulate 48     # 无真机数据时补一份引擎自检
    python collect_diagnostics.py --bundle --with-sources   # 额外打包 zip（数据 + 报告）

输出：``<插件目录>/diagnostics/life-frequency-诊断-<时间戳>.md``（外加同名 ``.json``）。
``--bundle`` 再产出一个 zip，便于把现场数据带到另一台机器上读。

**长期监控（至少一周）**：一次性报告只回答「此刻正常吗」，看不出卡顿、停机、模型连续失败
这类**过程性**问题。watch 模式在同一个程序里做时序采集：

    python collect_diagnostics.py --watch                   # 常驻监控：默认 5 分钟一采样、跑 7 天
    python collect_diagnostics.py --watch --duration-hours 0 # 一直跑（重启后重挂即可）
    python collect_diagnostics.py --once                    # 只采一个样本（手动看 / 计划任务）
    python collect_diagnostics.py --summary-only            # 只用已有样本重算汇总报告

输出到 ``<插件目录>/diagnostics/watch/``：``samples.jsonl`` + ``samples.csv``（时序证据）、
``alerts.log`` + ``alerts.md``（去重后的告警流水）、``daily/``（每天一份完整诊断报告）、
``watch_summary.md``（随时可读的汇总：覆盖缺口、卡顿、模型趋势、逐日数值走势）。
watch 模式只往这个目录写，**只读承诺不变**。

**只读承诺**：本程序不改动插件目录与运行时数据（只往输出目录写报告）。读 SQLite 走
只读连接，连不上时**复制**一份到临时目录再读，绝不碰原库。

它不依赖插件是否装了 SDK：优先用插件自带的**纯模块**（``life_sim`` 等，无 ctx、无 IO）
做权威校验（例如用 ``LifeState.from_dict`` 真解析状态文件），导入失败则退回内置的最小
校验器，并在报告里注明「未使用插件代码校验」。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

PROGRAM_VERSION = "1.3.0"
PLUGIN_ID = "org.orge-8.life-frequency"
PLUGIN_NAME = "生活频率"
#: runtime 数据文件名与插件 manifest 名
PLUGIN_MANIFEST = "_manifest.json"
STATE_FILES = ("life_state.json", "adjust_memory.json")
STORE_FILE = "life_store.db"
CONFIG_FILE = "config.toml"
#: 自动搜索时跳过的目录名（噪声大且几乎不可能放运行时数据）
SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
    ".venv", "venv", "site-packages", ".idea", ".vscode",
}
#: 找**插件代码目录**时额外跳过的目录：MaiBot 的 ``.update_backups`` 里塞着几十份别的
#: 插件的完整副本（真机实测 1160 个文件），扫它等于自找噪音。
PLUGIN_SCAN_SKIP = SKIP_DIRS | {".update_backups", ".update_backup", ".trash", "backups"}
#: 附录文件清单最多列多少行（完整清单在 JSON 里；报告是给人读的，不该被 1000 行表格淹掉）
INVENTORY_ROWS = 150
#: 测试残留目录（FakeHost 用的临时目录）——它们看起来像真机数据，必须显式排除并说明
FAKE_DIR_RE = re.compile(r"maibot-fake-", re.I)
#: 判断「某个 config.toml 是不是本插件的」用的分节标记。**必须做这个校验**：
#: 桌面上可能同时放着别的插件，它们也有 config.toml——实测踩过（捡到 cv_lyric_context
#: 的配置，报告于是显示「缺 13 个 v1.16 新增项」，完全是冤枉）。
CONFIG_MARKER_SECTIONS = {
    "emotion_energy", "routines", "physio", "mood", "motives", "interrupt", "dream",
    "world", "economy", "date", "events", "activity", "apply", "relations", "social",
    "calendar", "health", "simulation", "frequency",
}
#: 只有这些分节才是**本插件独有**的（用来兜住「配置很旧、只剩几个分节」的情况）
CONFIG_UNIQUE_SECTIONS = {"emotion_energy", "routines", "motives", "interrupt", "dream",
                          "physio", "world"}
#: ``life_state.json`` 的签名键（命中其中 3 个才认为「这是本插件的状态文件」）
STATE_SIGNATURE_KEYS = {
    "activity", "activity_since", "emotion", "energy", "day_key", "awake_minutes_today",
    "recent_events", "energy_cap", "afterglow", "materials", "social_daily", "stress",
}
#: ``life_store.db`` 的签名表（本插件的两张租户表）
STORE_SIGNATURE_TABLES = {"relationships", "routine_daily"}

_SECRET_RE = re.compile(
    r"((?:p_skey|skey|uin|sid|qzonetoken|token|secret|password|cookie|api[_-]?key)"
    r"\s*[=:]\s*)([^\s;,&\"'）)]+)",
    re.I,
)
#: 身份语境里的号码（``session=123456789`` / ``user_id: 10001`` / ``group-123456``）。
#: 只在**有身份标记**时脱敏：裸的长数字要保留（epoch 时间戳、字节数、哈希都靠它读，
#: 一律打码会把报告读废——这是「脱敏要精准」与「报告要可读」的取舍）。
_ID_CONTEXT_RE = re.compile(
    r"((?:session_id|session|user_id|uid|qq|uin|group_id|群号|号码)\s*[=:：]?\s*)(\d{5,12})"
    r"|((?:group|private|stream|user)-)(\d{5,12})",
    re.I,
)


# ================================================================ 小工具


def redact(text: object, *, limit: int = 400) -> str:
    """外部文本 → 可安全写进报告的片段：脱敏凭据 + 掩码身份号码 + 截断。"""

    raw = str(text if text is not None else "").replace("\r", " ").replace("\n", " ")
    raw = _SECRET_RE.sub(r"\1<redacted>", raw)
    raw = _ID_CONTEXT_RE.sub(lambda m: (m.group(1) or m.group(3) or "") + "***"
                             if m.group(2) or m.group(4) else m.group(0), raw)
    if len(raw) > limit:
        raw = raw[:limit] + f"…（共 {len(str(text))} 字）"
    return raw.strip()


def as_float(value: object, default: float | None = None) -> float | None:
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        return default
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def as_int(value: object, default: int = 0) -> int:
    number = as_float(value)
    return default if number is None else int(number)


def sha256_of(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        return f"<读取失败: {exc.__class__.__name__}>"
    return digest.hexdigest()


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} GB"


def human_span(seconds: float) -> str:
    """秒 → 人话（诊断报告里全用这个口径，读者不必心算）。"""

    if seconds is None or not math.isfinite(float(seconds)):
        return "未知"
    seconds = float(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    if seconds < 90:
        return f"{sign}{seconds:.0f} 秒"
    minutes = seconds / 60.0
    if minutes < 90:
        return f"{sign}{minutes:.0f} 分钟"
    hours = minutes / 60.0
    if hours < 48:
        return f"{sign}{hours:.1f} 小时"
    return f"{sign}{hours / 24.0:.1f} 天"


def human_stamp(epoch: object, tz_offset_minutes: int) -> str:
    number = as_float(epoch)
    if number is None or number <= 0:
        return "—"
    try:
        local = datetime.fromtimestamp(number + tz_offset_minutes * 60, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return f"<坏时间戳 {number!r}>"
    return local.strftime("%Y-%m-%d %H:%M") + f"（UTC{tz_offset_minutes / 60:+g}）"


def read_text(path: pathlib.Path, *, limit: int = 4 << 20) -> str:
    try:
        data = path.read_bytes()[:limit]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def read_json(path: pathlib.Path) -> tuple[object | None, str]:
    try:
        raw = path.read_text(encoding="utf-8", errors="strict")
    except OSError as exc:
        return None, f"读取失败：{exc.__class__.__name__}: {exc}"
    except UnicodeDecodeError as exc:
        return None, f"不是 UTF-8 文本：{exc}"
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"


# ================================================================ 发现（数据源定位）


@dataclass
class Source:
    """一个采集到的数据源。``pack`` 表示要不要放进 `--bundle` 的 zip。"""

    kind: str
    path: pathlib.Path | None
    note: str = ""
    simulated: bool = False
    pack: bool = True


def discover_data_dirs(roots: Sequence[pathlib.Path], *, depth: int = 5,
                       max_seconds: float = 25.0) -> list[pathlib.Path]:
    """在若干根目录下**有界**搜索运行时数据（跳过 .git / 测试残留）。

    有界是刻意的：诊断程序不该在别人的磁盘上跑十分钟。超时会提前收工并在报告里注明。
    返回顺序按**可信度**排：目录里有 ``life_state.json`` 的排最前（那才是真机数据目录），
    只有 ``config.toml`` 的排后面（宿主常常把它放在另一个目录）。
    """

    found: dict[pathlib.Path, int] = {}
    deadline = time.monotonic() + max_seconds
    for root in roots:
        if not root.is_dir():
            continue
        stack: list[tuple[pathlib.Path, int]] = [(root, 0)]
        while stack:
            if time.monotonic() > deadline:
                return _rank_dirs(found)
            current, level = stack.pop()
            try:
                children = list(current.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    if level < depth and child.name not in SKIP_DIRS:
                        stack.append((child, level + 1))
                    continue
                if child.name not in STATE_FILES and child.name not in (STORE_FILE, CONFIG_FILE):
                    continue
                parent = child.parent.resolve()
                if FAKE_DIR_RE.search(str(parent)):
                    continue
                score = {"life_state.json": 3, "life_store.db": 2,
                         "adjust_memory.json": 1, "config.toml": 0}.get(child.name, 0)
                found[parent] = max(found.get(parent, -1), score)
    return _rank_dirs(found)


def _rank_dirs(found: Mapping[pathlib.Path, int]) -> list[pathlib.Path]:
    return [path for path, _score in
            sorted(found.items(), key=lambda item: (-item[1], str(item[0])))]


# ---- 归属校验：桌面/磁盘上可能有**别的**插件，文件名一样不代表是这份数据 ----


def looks_like_state(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    hits = STATE_SIGNATURE_KEYS.intersection(payload.keys())
    return len(hits) >= 3


def looks_like_store(path: pathlib.Path) -> bool:
    conn, _note, _used, temp_dir = _open_readonly(path)
    if conn is None:
        _cleanup_temp(temp_dir)
        return False
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    except sqlite3.Error:
        return False
    finally:
        conn.close()
        _cleanup_temp(temp_dir)
    return bool(STORE_SIGNATURE_TABLES.intersection(tables))


def looks_like_config(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if "plugin" not in payload and not CONFIG_MARKER_SECTIONS.intersection(payload.keys()):
        return False
    markers = CONFIG_MARKER_SECTIONS.intersection(payload.keys())
    unique = CONFIG_UNIQUE_SECTIONS.intersection(payload.keys())
    return bool(unique) or len(markers) >= 4


def score_data_dir(path: pathlib.Path) -> tuple[int, list[str]]:
    """给候选目录打「属于本插件的可信度」分，并记录它命中/被排除的原因。"""

    score = 0
    notes: list[str] = []
    state = path / "life_state.json"
    if state.exists():
        payload, _error = read_json(state)
        if looks_like_state(payload):
            score += 8
            notes.append("life_state.json 签名匹配")
        else:
            notes.append("life_state.json 签名不匹配（可能是别的插件）")
    store = path / "life_store.db"
    if store.exists():
        if looks_like_store(store):
            score += 4
            notes.append("life_store.db 有本插件的表")
        else:
            notes.append("life_store.db 没有 relationships/routine_daily 表")
    config = path / "config.toml"
    if config.exists():
        payload, _error = _load_toml(config)
        if looks_like_config(payload):
            score += 2
            notes.append("config.toml 分节匹配")
        else:
            notes.append("config.toml 分节不匹配（可能是别的插件）")
    memory = path / "adjust_memory.json"
    if memory.exists():
        payload, _error = read_json(memory)
        if isinstance(payload, dict) and {"applied", "foreign"} & set(payload.keys()):
            score += 1
            notes.append("adjust_memory.json 签名匹配")
        else:
            notes.append("adjust_memory.json 签名不匹配")
    return score, notes


def _manifest_id(directory: pathlib.Path) -> str:
    payload, _error = read_json(directory / PLUGIN_MANIFEST)
    return str(payload.get("id") or "") if isinstance(payload, dict) else ""


def locate_plugin_dir(given: pathlib.Path, search_roots: Sequence[pathlib.Path], *,
                      plugin_id: str = PLUGIN_ID, depth: int = 3,
                      max_seconds: float = 20.0) -> tuple[pathlib.Path, str]:
    """确认「插件代码目录」到底在哪 —— 并自动纠正指错的路径。

    真机实测踩过（2026-10-10，报告里两条 FAIL 就是它造成的）：把脚本丢进
    ``<MaiBot>/plugins/`` 直接跑（``--plugin-dir`` 默认就是脚本所在目录），于是报告把
    **整个 plugins 根**当成插件目录：``_manifest.json`` / ``plugin.py`` 都不在 ⇒ 误报
    「manifest 不可用」「plugin.py 不存在」，而且读不到真机跑的是哪一版，附录还塞进
    1160 个**别的插件的**文件。

    纠正规则：给定目录没有 manifest 时，在它自己与 ``--search-root`` 下**有界**扫描，
    优先 ``_manifest.json`` 里 ``id`` 完全匹配的目录；退一步按目录名（``life-frequency``）
    匹配。返回 ``(目录, 说明)``，说明为空表示没纠正。
    """

    if (given / PLUGIN_MANIFEST).exists():
        return given, ""
    deadline = time.monotonic() + max_seconds
    fallback: pathlib.Path | None = None
    for root in [given, *[item for item in search_roots if item != given]]:
        if not root.is_dir():
            continue
        stack: list[tuple[pathlib.Path, int]] = [(root, 0)]
        while stack:
            if time.monotonic() > deadline:
                stack.clear()
                break
            current, level = stack.pop()
            try:
                children = list(current.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    if (level < depth and child.name not in PLUGIN_SCAN_SKIP
                            and not child.name.startswith(".")):
                        stack.append((child, level + 1))
                    continue
                if child.name != PLUGIN_MANIFEST:
                    continue
                directory = child.parent.resolve()
                if _manifest_id(child.parent) == plugin_id:
                    return directory, (f"给定的插件目录 `{given}` 里没有 {PLUGIN_MANIFEST}："
                                       f"已自动定位到 `{directory}`（manifest id 匹配）")
                if fallback is None and child.parent.name == "life-frequency":
                    fallback = directory
    if fallback is not None:
        return fallback, (f"给定的插件目录 `{given}` 里没有 {PLUGIN_MANIFEST}："
                          f"已按目录名自动定位到 `{fallback}`（请核对是不是这份插件）")
    return given, (f"给定的插件目录 `{given}` 里没有 {PLUGIN_MANIFEST}："
                   "**第 1 节里关于 manifest / plugin.py 的结论可能是误报**，"
                   "请用 `--plugin-dir <插件目录>` 重新采集")


def pick_existing(candidates: Iterable[pathlib.Path]) -> pathlib.Path | None:
    for item in candidates:
        if item and item.exists():
            return item
    return None


def base_search_roots(plugin_dir: pathlib.Path) -> list[pathlib.Path]:
    """自动搜索的根目录：插件目录的上下两级 + 常见 MaiBot 位置 + 环境变量指向。"""

    roots = [plugin_dir, plugin_dir.parent]
    for env_name in ("MAIBOT_ROOT", "MAIBOT_PATH", "MAIBOT_HOME"):
        raw = os.environ.get(env_name)
        if raw:
            roots.append(pathlib.Path(raw))
    # 常见安装位置（存在才加，避免无谓扫描）
    for guess in ("E:/mai/maibot", "D:/mai/maibot", "C:/mai/maibot",
                  str(pathlib.Path.home() / "mai"), str(pathlib.Path.home() / "maibot")):
        path = pathlib.Path(guess)
        if path.exists():
            roots.append(path)
    deduped: list[pathlib.Path] = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in deduped:
            deduped.append(resolved)
    return deduped


# ================================================================ 插件纯模块（可选）


class PurePlugin:
    """可选地加载插件自带的**纯模块**，用真实现做校验（导入失败则整体降级）。

    ⚠ 只加载无 ctx / 无 IO 的模块（``life_sim`` 及其依赖），绝不 import ``plugin.py``
    ——那会拖进 maibot_sdk，违背「独立程序」。
    """

    def __init__(self, plugin_dir: pathlib.Path) -> None:
        self.available = False
        self.reason = ""
        self.sim: Any = None
        self.activity: Any = None
        self.factors: Any = None
        self.relations: Any = None
        sys.path.insert(0, str(plugin_dir))
        try:
            import life_activity  # noqa: PLC0415
            import life_factors  # noqa: PLC0415
            import life_relations  # noqa: PLC0415
            import life_sim  # noqa: PLC0415

            self.sim, self.activity, self.factors = life_sim, life_activity, life_factors
            self.relations = life_relations
            self.available = True
        except Exception as exc:  # noqa: BLE001 —— 降级是设计的一部分
            self.reason = f"{exc.__class__.__name__}: {exc}"
        finally:
            try:
                sys.path.remove(str(plugin_dir))
            except ValueError:
                pass

    def parse_state(self, payload: object) -> tuple[Any | None, str]:
        """用 ``LifeState.from_dict`` 真解析（它会净化坏值——净化掉的差异本身就是证据）。"""

        if not self.available:
            return None, "未加载插件纯模块，跳过权威解析"
        try:
            return self.sim.LifeState.from_dict(payload), ""
        except Exception as exc:  # noqa: BLE001
            return None, f"LifeState.from_dict 抛错：{exc.__class__.__name__}: {exc}"

    def parse_curve(self, lines: object) -> tuple[tuple[tuple[float, float], ...], list[str]]:
        if not self.available:
            return (), ["未加载插件纯模块"]
        try:
            return self.factors.parse_curve_points(lines)
        except Exception as exc:  # noqa: BLE001
            return (), [f"parse_curve_points 抛错：{exc}"]

    def sim_config_defaults(self, **overrides: Any) -> Any:
        """与插件默认值一致的 ``SimConfig``（供离线自检用；config.toml 只覆盖标量项）。"""

        if not self.available:
            return None
        return self.sim.SimConfig(**overrides)


# ================================================================ 发现结论（Finding）


@dataclass
class Finding:
    level: str          # FAIL / WARN / OK / UNKNOWN
    title: str
    evidence: str = ""
    fix: str = ""
    scope: str = ""     # 代码 / 配置 / 状态 / 库 / 日志 / 采集

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "scope": self.scope, "title": self.title,
                "evidence": self.evidence, "fix": self.fix}


class Report:
    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self.sections: list[tuple[str, str]] = []
        self.sources: list[Source] = []
        self.machine: dict[str, Any] = {}

    def add(self, level: str, title: str, *, evidence: str = "", fix: str = "",
            scope: str = "") -> None:
        self.findings.append(Finding(level.upper(), title, evidence, fix, scope))

    def section(self, title: str, body: str) -> None:
        self.sections.append((title, body.rstrip() + "\n"))

    def counts(self) -> dict[str, int]:
        counts = {"FAIL": 0, "WARN": 0, "OK": 0, "UNKNOWN": 0}
        for finding in self.findings:
            counts[finding.level] = counts.get(finding.level, 0) + 1
        return counts


# ================================================================ 代码侧采集


def collect_code(report: Report, plugin_dir: pathlib.Path) -> dict[str, Any]:
    info: dict[str, Any] = {"plugin_dir": str(plugin_dir)}

    manifest_path = plugin_dir / "_manifest.json"
    manifest, error = read_json(manifest_path) if manifest_path.exists() else (None, "文件不存在")
    if not isinstance(manifest, dict):
        report.add("FAIL", "_manifest.json 不可用", evidence=error or "不是 JSON 对象",
                   fix="manifest 非法宿主会直接拒绝加载：先修 JSON 与必需字段",
                   scope="代码")
        manifest = {}
    else:
        report.add("OK", "_manifest.json 合法", evidence=f"id={manifest.get('id')} "
                   f"version={manifest.get('version')}", scope="代码")
    info["manifest"] = {
        "id": manifest.get("id"), "version": manifest.get("version"),
        "name": manifest.get("name"), "manifest_version": manifest.get("manifest_version"),
        "sdk": manifest.get("sdk"), "host": manifest.get("host_application"),
        "capabilities": manifest.get("capabilities") or [],
    }
    if manifest.get("id") and manifest.get("id") != PLUGIN_ID:
        report.add("WARN", "manifest id 与预期不同",
                   evidence=f"实际 {manifest.get('id')}，预期 {PLUGIN_ID}",
                   fix="确认采集的是不是同一个插件（诊断脚本默认针对 " + PLUGIN_ID + "）",
                   scope="代码")

    # 入口与生命周期（只做文本判定，不 import plugin.py）
    entry = plugin_dir / "plugin.py"
    if not entry.exists():
        report.add("FAIL", "plugin.py 不存在", fix="入口文件固定为 plugin.py", scope="代码")
        source = ""
    else:
        source = read_text(entry)
        required = ("create_plugin", "def on_load", "def on_unload", "def on_config_update")
        missing = [name for name in required if name not in source]
        if missing:
            report.add("FAIL", "plugin.py 缺少生命周期契约",
                       evidence="缺：" + "、".join(missing),
                       fix="on_load / on_unload / on_config_update 三个都覆写，且要有 create_plugin",
                       scope="代码")
        else:
            report.add("OK", "plugin.py 生命周期契约齐全", scope="代码")

    config_version = ""
    match = re.search(r'SUPPORTED_CONFIG_VERSION\s*=\s*"([^"]+)"', source)
    if match:
        config_version = match.group(1)
    info["config_version"] = config_version

    # 语法检查：用内存编译，绝不写 __pycache__（只读承诺）
    broken: list[str] = []
    module_count = 0
    for path in sorted(plugin_dir.glob("*.py")):
        if path.name in ("check_plugin.py", "run_gates.py", "calibrate_curves.py"):
            continue
        module_count += 1
        try:
            compile(read_text(path), str(path), "exec")
        except SyntaxError as exc:
            broken.append(f"{path.name}:{exc.lineno} {exc.msg}")
    if broken:
        report.add("FAIL", "存在语法错误的模块", evidence="；".join(broken[:5]),
                   fix="先修语法（加载期就会失败）", scope="代码")
    else:
        report.add("OK", f"{module_count} 个插件模块语法检查通过", scope="代码")
    info["module_count"] = module_count

    # README 承诺与版本一致性
    readme = read_text(plugin_dir / "README.md")
    info["readme_lines"] = len(readme.splitlines()) if readme else 0
    versions = re.findall(r"^\|\s*(\d+\.\d+\.\d+)\s*\|", readme, re.M)
    if versions:
        latest = max(versions, key=lambda item: tuple(int(part) for part in item.split(".")))
        info["readme_latest_version"] = latest
        if manifest.get("version") and latest != manifest.get("version"):
            report.add("WARN", "README 版本历史与 manifest 版本不一致",
                       evidence=f"README 最新 {latest}，manifest {manifest.get('version')}",
                       fix="补上本版变更记录（或确认 manifest 忘了 bump）", scope="代码")
        else:
            report.add("OK", "manifest 版本与 README 版本历史一致",
                       evidence=f"v{latest}", scope="代码")

    # 交付文件是否都在（README 声称能跑的脚本）
    missing_files = [name for name in ("README.md", "run_gates.py", "tests/smoke_test.py")
                     if not (plugin_dir / name).exists()]
    if missing_files:
        report.add("WARN", "交付文件缺失", evidence="、".join(missing_files),
                   fix="缺少这些文件时 README 里的命令跑不起来（门禁步骤会静默 SKIP）",
                   scope="代码")
    else:
        report.add("OK", "README 里声称可跑的门禁文件都在", scope="代码")

    # git 状态（能看出「跑的是不是提交过的版本」）
    if (plugin_dir / ".git").exists():
        info["git"] = {}
        for key, cmd in (("head", ["git", "rev-parse", "--short", "HEAD"]),
                         ("subject", ["git", "log", "-1", "--pretty=%s"]),
                         ("dirty", ["git", "status", "--porcelain"])):
            try:
                # ⚠ Windows 上不能靠默认编码：仓库里的中文提交信息会抛 UnicodeDecodeError
                # （子进程用 GBK 解码 UTF-8 输出），表现为「诊断程序自己崩了」。
                proc = subprocess.run(cmd, cwd=str(plugin_dir), capture_output=True,
                                      text=True, encoding="utf-8", errors="replace",
                                      timeout=20)
                info["git"][key] = (proc.stdout or "").strip()
            except (OSError, subprocess.SubprocessError) as exc:
                info["git"][key] = f"<{exc.__class__.__name__}>"
        dirty_lines = [line for line in str(info["git"].get("dirty", "")).splitlines()
                       if line.strip()]
        if dirty_lines:
            report.add("WARN", "工作区有未提交改动",
                       evidence=f"{len(dirty_lines)} 个文件；HEAD="
                                f"{info['git'].get('head')}（{info['git'].get('subject')}）",
                       fix="确认真机上跑的是哪一份（附录里有每个文件的 SHA256，可逐一对齐）",
                       scope="代码")
        else:
            report.add("OK", "工作区干净（真机版本可对齐到提交号）",
                       evidence=f"HEAD={info['git'].get('head')}（{info['git'].get('subject')}）",
                       scope="代码")
    return info


# ================================================================ 配置侧采集


#: v1.16.0–1.16.3 新增的配置项（老 config.toml 里不会有，用来发现「字段缺失」）
NEW_V16_FIELDS = (
    ("emotion_energy", "fatigue_ramp_curve"),
    ("emotion_energy", "recover_ratio_per_tick"),
    ("emotion_energy", "recover_min_step"),
    ("emotion_energy", "inertia_scale_enabled"),
    ("emotion_energy", "afterglow_decay"),
    ("emotion_energy", "afterglow_gain"),
    ("emotion_energy", "baseline_diurnal_curve"),
    ("emotion_energy", "emotion_fatigue_penalty"),
    ("emotion_energy", "low_energy_drain_multiplier"),
    ("emotion_energy", "emotion_impact_scaling"),
    ("mood", "stress_breakdown_enabled"),
    ("mood", "loneliness_social_scaling"),
    ("social", "relation_emotion_scaling"),
)


def _load_toml(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    try:
        import tomllib  # noqa: PLC0415 —— Python 3.11+
    except ImportError:  # pragma: no cover
        return {}, "当前解释器没有 tomllib（Python < 3.11），无法解析 config.toml"
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as exc:
        return {}, f"读取失败：{exc.__class__.__name__}"
    except Exception as exc:  # noqa: BLE001 —— tomllib 抛 TOMLDecodeError
        return {}, f"TOML 解析失败：{exc}"
    return data if isinstance(data, dict) else {}, ""


def _section(config: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name)
    return dict(value) if isinstance(value, dict) else {}


def collect_config(report: Report, path: pathlib.Path | None, pure: "PurePlugin",
                   plugin_dir: pathlib.Path) -> dict[str, Any]:
    if path is None or not path.exists():
        report.add("UNKNOWN", "未找到 config.toml（宿主落盘的配置）",
                   evidence="这是宿主生成的运行时文件，源码目录里通常没有",
                   fix="用 --config <路径> 指定，或把 --search-root 指向 MaiBot 根目录",
                   scope="配置")
        return {}
    config, error = _load_toml(path)
    if error:
        report.add("FAIL", "config.toml 无法解析", evidence=error,
                   fix="宿主读不出配置会退回默认值或拒绝加载：先修 TOML 语法", scope="配置")
        return {}
    report.add("OK", "config.toml 解析成功", evidence=str(path), scope="配置")

    plugin_section = _section(config, "plugin")
    enabled = plugin_section.get("enabled", True)
    file_version = str(plugin_section.get("config_version") or "")
    detail: dict[str, Any] = {
        "path": str(path), "config_version": file_version,
        "enabled": bool(enabled), "dry_run": bool(_section(config, "simulation").get("dry_run")),
    }

    if not enabled:
        report.add("FAIL", "插件被禁用（[plugin] enabled = false）",
                   evidence="她不推进也不干预宿主频率",
                   fix="改成 true（或在 WebUI 里重新启用）", scope="配置")

    code_version = ""
    entry = plugin_dir / "plugin.py"
    if entry.exists():
        match = re.search(r'SUPPORTED_CONFIG_VERSION\s*=\s*"([^"]+)"', read_text(entry))
        code_version = match.group(1) if match else ""
    if file_version and code_version:
        detail["code_config_version"] = code_version
        if file_version != code_version:
            report.add("WARN", "config.toml 的 config_version 落后于代码",
                       evidence=f"文件 {file_version} / 代码 {code_version}",
                       fix="宿主只在版本变化时才回填新增字段：重启一次让它重建配置，"
                           "或手动补上缺失项（旧字段的值会保留）", scope="配置")
        else:
            report.add("OK", "config_version 与代码一致", evidence=file_version, scope="配置")

    missing = [f"[{section}] {key}" for section, key in NEW_V16_FIELDS
               if key not in _section(config, section)]
    if missing and file_version:
        report.add("WARN", f"配置缺 {len(missing)} 个 v1.16 新增项（将按内置默认值运行）",
                   evidence="、".join(missing[:8]) + ("…" if len(missing) > 8 else ""),
                   fix="这些项按默认值生效但你在 WebUI/文件里看不到：重启宿主让它回填，"
                       "或在 [plugin] config_version 上补到与代码一致后重启", scope="配置")
    detail["missing_fields"] = missing

    # ---- 危险配置组合（能真的让「看起来在跑、实际什么都没发生」）----
    frequency = _section(config, "frequency")
    apply_section = _section(config, "apply")
    simulation = _section(config, "simulation")
    # v1.19.0（PR-AF-1 配套）：诊断侧的「目标口径」。默认值刻意与 v1.18.1 相反——
    # 24h 睡眠窗口本身不是故障，当设计目标是「状态驱动、不看钟点」时，
    # 24h 覆盖等价于「取消时间限制」，是自洽的。
    diagnostics = _section(config, "diagnostics")

    if bool(frequency.get("paused", False)):
        report.add("FAIL", "插件处于暂停状态（[frequency] paused = true）",
                   evidence="生活照常推进，但倍率归还外部基数——她不会因为生活状态而变安静/变吵",
                   fix="恢复：`/生活 恢复`（或把 paused 改回 false）", scope="配置")

    raw_window = str(simulation.get("sleep_window") or "").strip()
    detail["sleep_window"] = raw_window
    if raw_window:
        # v1.24.0：睡眠时段退役，睡不睡只看困意（sleep_need / 昼夜偏置 / 生病）。
        # 字段仍留在配置里是为了不打断旧配置，但它**不再参与任何判定**；继续按
        # 窗口长短判「作息是否合理」等于告诉用户改一个不生效的开关。
        report.add("OK", "sleep_window 已退役（v1.24.0 起作息由困意决定）",
                   evidence=f"配置里是 {raw_window}；它不再影响入睡——全天覆盖与窄窗口一样",
                   fix="想调作息改 [simulation] 的 sleep_need_onset / sleep_need_wake / "
                       "sleep_target_hours；窗口字段可原样保留（会被忽略）",
                   scope="配置")

    quiet = frequency.get("quiet_hours")
    if isinstance(quiet, list) and quiet:
        detail["quiet_hours"] = quiet
        report.add("OK", f"静默时段 {len(quiet)} 条（她在此期间不说话）",
                   evidence="、".join(str(item) for item in quiet[:4]), scope="配置")
    elif isinstance(quiet, list):
        report.add("OK", "没有静默时段", scope="配置")

    mode = str(apply_section.get("filter_mode") or "all").strip().lower()
    targets = apply_section.get("target_chats") or []
    detail["filter_mode"] = mode
    detail["target_chats"] = targets
    if mode not in ("all", "whitelist", "blacklist"):
        report.add("FAIL", f"[apply] filter_mode 非法：{mode!r}",
                   evidence="会按最保守的 whitelist 处理——若白名单为空则一个会话都不干预",
                   fix="改成 all / whitelist / blacklist",
                   scope="配置")
    elif mode == "whitelist" and not targets:
        report.add("FAIL", "白名单模式但 target_chats 为空",
                   evidence="她不干预任何会话（看起来完全没生效）",
                   fix="填 group:<群号> / private:<QQ号>，或把 filter_mode 改成 all",
                   scope="配置")

    if bool(simulation.get("dry_run", False)):
        # v1.19.0（PR-AF-1 配套）：dry_run 是**有意的阶段选择**（先看数值、再放手），
        # 不再是每轮重复 152 次的告警噪音。只有当你明确说「本该写却没写」时才报。
        if diagnostics.get("expect_dry_run", False):
            report.add("OK", "演算模式开着（dry_run = true，符合 [diagnostics] expect_dry_run）",
                       evidence="只算不写：宿主上的倍率不会被改。这是有意的观测阶段选择，"
                                "关闭它才会改变宿主上的发言频率——那是生产行为变更",
                       scope="配置")
        else:
            report.add("WARN", "演算模式开着（dry_run = true）",
                       evidence="只算不写：宿主上的倍率不会被改",
                       fix="确认这是调试用；正常运转要关掉。"
                           "若这是有意的观测阶段，请在 [diagnostics] 设 expect_dry_run = true",
                       scope="配置")

    factors_mode = str(_section(config, "activity").get("factors_mode") or "merge")
    if factors_mode == "replace":
        factors = _section(config, "activity").get("activity_factors") or []
        has_sleep = any(str(item).strip().startswith("sleep=") for item in factors)
        if not has_sleep:
            report.add("FAIL", "因子表 replace 模式缺 sleep 因子",
                       evidence="她睡觉将不再静默（会按 1.0 说个不停）",
                       fix="把 sleep=0.0 加回 [activity] activity_factors", scope="配置")

    # ---- 曲线配置：用插件自己的解析器验证（坏行会被静默丢弃，必须显式暴露）----
    emotion = _section(config, "emotion_energy")
    for key in ("fatigue_ramp_curve", "baseline_diurnal_curve",
                "impact_positive_curve", "impact_negative_curve"):
        lines = emotion.get(key)
        if lines is None:
            continue
        points, warnings = pure.parse_curve(lines) if pure.available else ((), [])
        detail[key] = {
            "lines": lines, "parsed_points": len(points), "warnings": warnings,
        }
        if warnings and pure.available:
            report.add("WARN", f"[emotion_energy] {key} 有坏行（该曲线按关闭/默认处理）",
                       evidence="；".join(str(item) for item in warnings[:3]),
                       fix="修掉这几行，或清空该配置项以显式关闭", scope="配置")
        elif pure.available and not points and lines:
            report.add("WARN", f"[emotion_energy] {key} 少于两个有效点（整条曲线作废）",
                       evidence=str(lines), fix="至少给两个点，例如 12=0 / 20=-0.4",
                       scope="配置")

    social = _section(config, "social")
    if social and not bool(social.get("enabled", False)):
        report.add("OK", "社交经历层未启用（默认关，属于正常配置）", scope="配置")
    return detail


# ================================================================ 状态侧采集


def _state_checks(report: Report, state: Mapping[str, Any], *, tz: int, now: float,
                  config: Mapping[str, Any], label: str, simulated: bool) -> dict[str, Any]:
    """对状态文件做逐项体检。``label`` 写进每条证据里（真机 / 离线模拟要分得清）。"""

    simulation = _section(config, "simulation") if config else {}
    tick_seconds = max(60, as_int(simulation.get("tick_seconds"), 600))
    offline_gap = max(0, as_int(simulation.get("offline_gap_minutes"), 30))
    health = _section(config, "health") if config else {}
    activity_labels = getattr(PURE.activity, "ACTIVITY_LABELS", {}) if PURE.available else {}

    def activity_label(name: object) -> str:
        text = str(name or "")
        return str(activity_labels.get(text, text)) or text

    detail: dict[str, Any] = {"label": label, "simulated": simulated}

    # ---- 生活循环是否在推进（最重要的一条）----
    last_tick = as_float(state.get("last_tick_at"), 0.0) or 0.0
    age = now - last_tick if last_tick > 0 else None
    detail["last_tick_age_seconds"] = age
    if last_tick <= 0:
        report.add("FAIL", f"{label}：生活循环从未推进（last_tick_at = 0）",
                   evidence="全新状态、或状态文件损坏后按空状态继续",
                   fix="看日志里有没有「状态文件损坏」；确认插件真的被加载并跑起来了",
                   scope="状态")
    elif age is not None and age < -60:
        report.add("WARN", f"{label}：last_tick_at 在未来（时钟回拨痕迹）",
                   evidence=f"距现在 {human_span(age)}",
                   fix="检查系统时间/NTP；状态锚点会被 settle 自愈，不必手动改",
                   scope="状态")
    elif age is not None and age <= tick_seconds * 1.5:
        report.add("OK", f"{label}：生活循环正在推进",
                   evidence=f"最近一次推进距今 {human_span(age)}（tick = {tick_seconds}s）",
                   scope="状态")
    elif age is not None and age <= tick_seconds * 6:
        report.add("WARN", f"{label}：推进比配置慢",
                   evidence=f"距今 {human_span(age)}，而 tick = {tick_seconds}s",
                   fix="可能是进程卡过一次/刚重启；若持续如此，看日志有无异常与长时间停顿",
                   scope="状态")
    elif age is not None:
        breaker = max(offline_gap * 60, tick_seconds * 6)
        report.add("FAIL", f"{label}：生活循环没有在跑",
                   evidence=f"最后一次推进距今 {human_span(age)}（阈值 {human_span(breaker)}）；"
                            "这段间隔会被判成「停机间隙」而不记账",
                   fix="看日志尾部有没有插件异常/宿主没加载插件；确认插件进程在运行",
                   scope="状态")

    # ---- 状态文件是否被净化过（= 文件受损）----
    if PURE.available:
        clean, error = PURE.parse_state(state)
        if clean is None:
            report.add("FAIL", f"{label}：状态文件无法被插件解析", evidence=error,
                       fix="备份后删除 life_state.json 让它重建（会丢生活状态，但对账记忆另存文件）",
                       scope="状态")
        else:
            cleaned = clean.to_dict()
            diffs = [key for key in state
                     if key in cleaned and state[key] != cleaned[key]]
            detail["sanitized_keys"] = diffs
            if diffs:
                report.add("WARN", f"{label}：状态文件里有 {len(diffs)} 个坏值被净化",
                           evidence="、".join(diffs[:8]),
                           fix="这些字段被插件按默认值处理（例如 NaN 时间戳会让状态冻结）；"
                               "若反复出现，检查落盘与磁盘", scope="状态")
            else:
                report.add("OK", f"{label}：状态文件结构与取值都干净（插件解析零净化）",
                           scope="状态")

    # ---- 核心数值 ----
    emotion = as_float(state.get("emotion"), 5.0) or 0.0
    energy = as_float(state.get("energy"), 0.0) or 0.0
    cap = as_float(state.get("energy_cap"), 10.0) or 10.0
    afterglow = as_float(state.get("afterglow"), 0.0) or 0.0
    detail.update({"emotion": emotion, "energy": energy, "energy_cap": cap,
                   "afterglow": afterglow,
                   "activity": state.get("activity"), "activity_label":
                       activity_label(state.get("activity")),
                   "scene": state.get("scene"), "activity_source": state.get("activity_source"),
                   "activity_since": state.get("activity_since"),
                   "sleep_minutes_today": as_int(state.get("sleep_minutes_today")),
                   "awake_minutes_today": as_int(state.get("awake_minutes_today")),
                   "continuous_awake_minutes": as_int(state.get("continuous_awake_minutes")),
                   # v1.24.0：睡不睡只看困意；旧「按醒来次数加减」的睡债已退役，
                   # 迁移时原值转进 legacy 字段留档（仅作证据，不参与任何判定）。
                   "sleep_need": as_float(state.get("sleep_need")),
                   "sleep_debt_nights": as_int(state.get("sleep_debt_nights")),
                   "legacy_sleep_debt_nights": as_int(state.get("legacy_sleep_debt_nights")),
                   "stress": as_float(state.get("stress")),
                   "loneliness": as_float(state.get("loneliness")),
                   "social_battery": as_float(state.get("social_battery")),
                   # v1.20.1（#4）：孤独的**增长锚点**——「有没有人跟我说话」的时刻。
                   # 没有它，「孤独为什么恒 0」只能靠推断：真机 283 样本那一轮的报告
                   # 只能写「推测」，没法证实「清醒时段 6 小时安静到底有没有成立过」。
                   # 有了它 + ``continuous_awake_minutes``，离线就能算清楚。
                   "last_addressed_at": as_float(state.get("last_addressed_at"))})
    if energy > cap + 1e-6:
        report.add("WARN", f"{label}：体力超过上限", evidence=f"{energy:.2f} > {cap:.2f}",
                   fix="正常不会发生（settle 会钳）；确认状态文件没被手改", scope="状态")
    if as_int(state.get("sleep_debt_nights")) > 0:
        report.add("WARN", f"{label}：旧「按醒来次数」的睡债仍在状态里",
                   evidence=f"sleep_debt_nights = {as_int(state.get('sleep_debt_nights'))}"
                            "（v1.24.0 起缺觉只按时间口径重算，这个值应为 0）",
                   fix="确认状态文件没被手改；插件加载时会把它转进 "
                       "legacy_sleep_debt_nights 并清零", scope="状态")
    afterglow_cap = as_float(_section(config, "emotion_energy").get("afterglow_cap"), 0.6) or 0.6
    if abs(afterglow) > afterglow_cap + 1e-6:
        report.add("WARN", f"{label}：余波超过配置上限", evidence=f"{afterglow:+.2f}（上限 ±{afterglow_cap}）",
                   fix="同上：状态被手改或配置改小了上限", scope="状态")

    # ---- 她在干什么 / 睡了多久 ----
    activity = str(state.get("activity") or "")
    minutes_in_activity = None
    since = as_float(state.get("activity_since"), 0.0) or 0.0
    if since > 0:
        minutes_in_activity = int(max(0.0, (last_tick - since) / 60.0)) if last_tick > 0 else None
    detail["minutes_in_activity"] = minutes_in_activity
    if minutes_in_activity is not None and minutes_in_activity > 7 * 24 * 60:
        report.add("WARN", f"{label}：当前活动已持续 {minutes_in_activity / 60:.0f} 小时",
                   evidence=f"活动={activity_label(activity)}（活动切换似乎没发生）",
                   fix="看日志有没有模型连续失败；reseed_after_hours 到点会换活动",
                   scope="状态")
    if activity in ("sleep", "nap"):
        sleep_started = as_float(state.get("sleep_started_at"), 0.0) or 0.0
        if sleep_started <= 0:
            report.add("WARN", f"{label}：正在睡但入睡锚点缺失",
                       evidence="sleep_started_at = 0（本次睡眠时长算不出来）",
                       fix="下一次醒来会自愈；持续出现要看 apply_activity 路径", scope="状态")
    else:
        awake_minutes = as_int(state.get("continuous_awake_minutes"))
        if awake_minutes > 30 * 60:
            report.add("FAIL", f"{label}：连续清醒 {awake_minutes / 60:.1f} 小时仍未入睡",
                       evidence="清醒疲劳会把体力持续压低，说明睡眠链路没有生效",
                       fix="看日志有没有「强制入睡」；确认 [simulation] sleep_need_onset / "
                           "sleep_need_wake 没被改到极端值（睡不睡只看困意）",
                       scope="状态")
        elif awake_minutes > 20 * 60:
            report.add("WARN", f"{label}：连续清醒 {awake_minutes / 60:.1f} 小时",
                       evidence="已经在熬夜区间（清醒疲劳会明显压低体力）", scope="状态")

    # ---- 模型侧健康（离线自检里没有模型可调，判它等于冤判）----
    fail_streak = as_int(state.get("llm_fail_streak"))
    cooldown_left = (as_float(state.get("llm_cooldown_until"), 0.0) or 0.0) - now
    last_success = as_float(state.get("llm_last_success_at"), 0.0) or 0.0
    detail.update({"llm_fail_streak": fail_streak,
                   "llm_cooldown_seconds_left": cooldown_left if cooldown_left > 0 else 0.0,
                   "llm_last_success": last_success,
                   "llm_last_raw": redact(state.get("llm_last_raw"), limit=120)})
    if simulated:
        report.add("OK", f"{label}：不判定模型侧与宿主写入",
                   evidence="离线自检不调模型、不写宿主（它只验证机制能推进）", scope="状态")
    elif fail_streak >= 3 or cooldown_left > 0:
        report.add("FAIL", f"{label}：模型连续失败 {fail_streak} 次"
                           + (f"（冷却还剩 {human_span(cooldown_left)}）" if cooldown_left > 0 else ""),
                   evidence=f"最近一次原始输出：{detail['llm_last_raw'] or '—'}",
                   fix="看日志里的 llm.generate 报错（模型名/任务名是否配错、"
                       "或宿主能力超时）；插件会保持当前活动并退避重试",
                   scope="状态")
    elif last_success <= 0:
        report.add("WARN", f"{label}：从未记录到一次成功的模型决策",
                   evidence="活动会一直停在冷启动种子（时段表）上",
                   fix="确认 [activity.llm] 的 task_name / 模型可用", scope="状态")
    else:
        idle = now - last_success
        if idle > 24 * 3600:
            report.add("WARN", f"{label}：距上次成功的模型决策 {human_span(idle)}",
                       evidence="超过 reseed 窗口时会退回时段表种子",
                       scope="状态")
        else:
            report.add("OK", f"{label}：模型侧健康",
                       evidence=f"连续失败 {fail_streak} 次、最近成功 {human_span(idle)} 前",
                       scope="状态")

    # ---- 写宿主：退避表 = 「我们写不进去」的证据 ----
    unbacked = state.get("unbacked") if isinstance(state.get("unbacked"), dict) else {}
    applied = state.get("applied") if isinstance(state.get("applied"), dict) else {}
    foreign = state.get("foreign") if isinstance(state.get("foreign"), dict) else {}
    observed = state.get("observed") if isinstance(state.get("observed"), dict) else {}
    worst = 0.0
    for value in unbacked.values():
        worst = max(worst, (as_float(value, 0.0) or 0.0) - now)
    detail.update({"applied_sessions": len(applied), "foreign_sessions": len(foreign),
                   "observed_sessions": len(observed), "unbacked_sessions": len(unbacked),
                   "unbacked_worst_seconds": worst if worst > 0 else 0.0})
    if unbacked:
        report.add("WARN", f"{label}：{len(unbacked)} 个会话处于「写不进宿主」退避中",
                   evidence=f"最长退避还剩 {human_span(worst)}；"
                            "宿主对没有 heartflow chat 对象的新会话会静默 no-op",
                   fix="正常：历史会话会退避重试；若活跃会话也一直写不进，"
                       "检查该会话是否真的产生过回复", scope="状态")
    if applied:
        report.add("OK", f"{label}：已经在 {len(applied)} 个会话上写过倍率",
                   evidence=f"其中 {len(foreign)} 个认到外部基数（与别的插件乘性合成）",
                   scope="状态")
    elif not simulated:
        # v1.19.0（PR-AF-1 配套）：dry_run 下「没写倍率」是**必然后果**，不是问题——
        # 它每轮重复 152 次，把真正有价值的告警挤掉了。只有非 dry_run 时才算异常。
        dry_run = bool(_section(config, "simulation").get("dry_run", False))
        if dry_run:
            report.add("OK", f"{label}：演算模式下不写宿主（符合预期）",
                       evidence="[simulation] dry_run = true 时倍率只算不落；"
                                "要看是否命中会话，用 `/生活 状态` 的「本轮命中几个会话」",
                       scope="状态")
        else:
            report.add("WARN", f"{label}：还没有在任何会话上写过倍率",
                       evidence="可能是没命中会话（filter_mode / only_active_sessions），"
                                "或一直处于静默/睡眠",
                       fix="用 `/生活 状态` 看「本轮命中几个会话」",
                       scope="状态")

    # ---- 内心维度 × 主动开口（v1.16.2 的真机教训：配置说允许，硬闸却一直挡着）----
    battery = as_float(state.get("social_battery"))
    lonely = as_float(state.get("loneliness"))
    proactive_on = bool(_section(config, "proactive").get("enabled", False))
    mood_on = bool(_section(config, "mood").get("enabled", True))
    detail["proactive_enabled"] = proactive_on
    detail["loneliness_factor_hint"] = None
    if mood_on and battery is not None:
        if proactive_on and battery < 2.0:
            report.add("WARN", f"{label}：社交电量 {battery:.1f} < 2.0 ⇒ 主动开口被硬闸挡住",
                       evidence="[proactive] enabled = true 允许她主动说话，但电量硬闸会直接"
                                "拒绝（沉默台账里记 low_battery）——配置看起来开着，实际从不开口",
                       fix="确认这是想要的（独处/睡眠会把电量回充；`/生活 为什么` 能看到 "
                           "low_battery 计数）",
                       scope="状态")
        elif proactive_on and battery < 4.0:
            report.add("WARN", f"{label}：社交电量 {battery:.1f} 偏低（2–4 档）",
                       evidence="主动开口的分数阈值会上浮 0.5，她会明显少主动说话",
                       scope="状态")
    if mood_on and lonely is not None:
        # M3b：孤独系数曲线（≤2 → 0.8、≥7 → 1.5）。区间外的极端值值得写出来，
        # 否则「社交情绪怎么总是打折/加成」只能靠猜。
        if lonely <= 2.0:
            report.add("OK", f"{label}：孤独 {lonely:.1f} ⇒ 社交情绪系数 ×0.8（被爱包围、钝感）",
                       scope="状态")
        elif lonely >= 7.0:
            report.add("OK", f"{label}：孤独 {lonely:.1f} ⇒ 社交情绪系数 ×1.5（很想有人陪）",
                       scope="状态")

    # ---- 经历与素材（叙事层是否在长大）----
    events = state.get("recent_events") if isinstance(state.get("recent_events"), list) else []
    materials = state.get("materials") if isinstance(state.get("materials"), list) else []
    newest_event = 0.0
    newest_label = ""
    for item in events:
        if not isinstance(item, dict):
            continue
        at = as_float(item.get("at"), 0.0) or 0.0
        if at > newest_event:
            newest_event, newest_label = at, str(item.get("label") or "")
    live_materials = 0
    for item in materials:
        if isinstance(item, dict):
            expires = as_float(item.get("expires_at"), 0.0) or 0.0
            if expires > now:
                live_materials += 1
    detail.update({"recent_events": len(events), "materials": len(materials),
                   "live_materials": live_materials, "newest_event_at": newest_event,
                   "newest_event_label": newest_label,
                   "materials_keep": as_int(_section(config, "activity").get("materials_keep"), 20)})
    if events:
        report.add("OK", f"{label}：经历库有 {len(events)} 条（最新：{newest_label or '—'}）",
                   evidence=f"最新一条距今 {human_span(now - newest_event) if newest_event else '未知'}",
                   scope="状态")
    else:
        report.add("WARN", f"{label}：经历库是空的",
                   evidence="事件抽取可能从未命中（fire_probability = 0？）或状态刚被重置",
                   scope="状态")

    # ---- 各种临时窗口（能看出机制是否在跑）----
    # v1.24.0：``rested_until``（体力满后的抑制）已退役，换成本轮真正生效的
    # 「睡前陪聊预算」（sleep_defer_until）；否则窗口表会把一个死字段显示成机制。
    windows = {
        "被 @ 唤醒窗口": as_float(state.get("at_wake_until"), 0.0) or 0.0,
        "赖床宽限窗": as_float(state.get("wake_grace_until"), 0.0) or 0.0,
        "打断窗口": as_float(state.get("interrupt_until"), 0.0) or 0.0,
        "睡前陪聊预算": as_float(state.get("sleep_defer_until"), 0.0) or 0.0,
    }
    detail["windows"] = {key: value for key, value in windows.items() if value}
    detail["interrupted_from"] = state.get("interrupted_from")
    active = {key: value for key, value in windows.items() if value > now}
    if active:
        report.add("OK", f"{label}：有 {len(active)} 个临时窗口正在生效",
                   evidence="；".join(f"{key}到 {human_stamp(value, tz)}"
                                     for key, value in active.items()), scope="状态")

    # ---- 去重表规模（TTL 清理是否在跑）----
    sizes = {name: len(state.get(name) or {}) for name in
             ("social_seen", "mood_injected", "interrupt_injected", "motive_seen",
              "world_seen", "medicine_cooldown", "sessions", "skip_ledger")
             if isinstance(state.get(name), (dict, list))}
    detail["table_sizes"] = sizes
    bloated = {name: size for name, size in sizes.items() if size > 5000}
    if bloated:
        report.add("WARN", f"{label}：有几张去重表异常膨胀",
                   evidence="、".join(f"{k}={v}" for k, v in bloated.items()),
                   fix="正常有 TTL/上限清理；持续膨胀说明清理路径没跑到（会撑大状态文件）",
                   scope="状态")
    return detail


def collect_state(report: Report, path: pathlib.Path | None, *, config: Mapping[str, Any],
                  pure: "PurePlugin", label: str, now: float,
                  simulated: bool = False) -> dict[str, Any]:
    if path is None or not path.exists():
        report.add("UNKNOWN", f"未找到 {label} 的状态文件（life_state.json）",
                   evidence="没有它就无法判断生活循环在不在跑",
                   fix="用 --data-dir 指向插件的 data 目录（ctx.paths.data_dir），"
                       "或用 --search-root 指向 MaiBot 根目录",
                   scope="状态")
        return {}
    payload, error = read_json(path)
    if not isinstance(payload, dict):
        report.add("FAIL", f"{label}：状态文件不可用", evidence=error or "顶层不是 JSON 对象",
                   fix="备份后删除让它重建", scope="状态")
        return {"path": str(path), "error": error}
    simulation = _section(config, "simulation")
    tz = as_int(simulation.get("tz_offset_minutes"), 480)
    detail = _state_checks(report, payload, tz=tz, now=now, config=config, label=label,
                           simulated=simulated)
    detail["path"] = str(path)
    detail["size_bytes"] = path.stat().st_size
    detail["sha256"] = sha256_of(path)[:16]
    return detail


def collect_memory(report: Report, path: pathlib.Path | None, *, now: float) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    payload, error = read_json(path)
    if not isinstance(payload, dict):
        report.add("WARN", "对账记忆文件（adjust_memory.json）不可用",
                   evidence=error, fix="它对不上会退回状态文件里的记忆", scope="状态")
        return {}
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "keys": sorted(payload.keys()),
        "applied": len(payload.get("applied") or {}),
        "foreign": len(payload.get("foreign") or {}),
    }


# ================================================================ 库侧采集


def _cleanup_temp(temp_dir: pathlib.Path | None) -> None:
    """删掉 ``_open_readonly`` 复制出来的临时副本目录（没有副本时是 no-op）。"""

    if temp_dir is None:
        return
    try:
        shutil.rmtree(temp_dir, ignore_errors=True)
    except OSError:
        pass


def _open_readonly(db_path: pathlib.Path
                   ) -> tuple[sqlite3.Connection | None, str, pathlib.Path,
                              pathlib.Path | None]:
    """只读打开 SQLite；直连失败（例如 WAL 需要恢复）时**复制一份**再读。

    ⚠ 第四个返回值是**临时副本目录**，调用方读完必须 ``_cleanup_temp``。
    一次性诊断漏删一次不痛不痒，但 watch 模式每天都要跑完整报告，
    一周下来会在系统 temp 里堆几十个库副本（每个可能几十 MB）——那是「看不见的磁盘占用」。
    """

    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn, "", db_path, None
    except sqlite3.Error as first:
        temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="life-diagnostics-"))
        for suffix in ("", "-wal", "-shm"):
            source = pathlib.Path(str(db_path) + suffix)
            if source.exists():
                try:
                    shutil.copy2(source, temp_dir / (db_path.name + suffix))
                except OSError:
                    pass
        copy_path = temp_dir / db_path.name
        try:
            conn = sqlite3.connect(str(copy_path), timeout=5)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            return conn, f"直连只读失败（{first}），已复制到临时目录读取", copy_path, temp_dir
        except sqlite3.Error as second:
            return None, f"只读与复制读取都失败：{first} / {second}", copy_path, temp_dir


def collect_store(report: Report, path: pathlib.Path | None, *, now: float,
                  tz: int, pure: "PurePlugin") -> dict[str, Any]:
    if path is None or not path.exists():
        report.add("UNKNOWN", "未找到 life_store.db（习惯日态与关系档案）",
                   evidence="没有它不影响生活循环（会降级成内存态），但看不出关系/习惯在不在跑",
                   fix="用 --data-dir 指向插件的 data 目录", scope="库")
        return {}
    conn, note, _used_path, temp_dir = _open_readonly(path)
    detail: dict[str, Any] = {
        "path": str(path), "size_bytes": path.stat().st_size,
        "sha256": sha256_of(path)[:16], "note": note,
        "wal_present": pathlib.Path(str(path) + "-wal").exists(),
    }
    if conn is None:
        report.add("FAIL", "life_store.db 读不出来", evidence=note,
                   fix="库损坏时插件会降级成内存态（习惯每天会重复命中）；备份后删除重建",
                   scope="库")
        _cleanup_temp(temp_dir)
        return detail
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        ok = bool(integrity and str(integrity[0]).lower() == "ok")
        detail["integrity"] = str(integrity[0]) if integrity else "?"
        if ok:
            report.add("OK", "life_store.db 完整性检查通过", scope="库")
        else:
            report.add("FAIL", "life_store.db 完整性检查未通过", evidence=str(integrity),
                       fix="备份后删除重建（会丢关系档案与习惯命中记录）", scope="库")

        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()]
        detail["tables"] = tables
        counts: dict[str, int] = {}
        for table in tables:
            try:
                counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            except sqlite3.Error:
                continue
        detail["row_counts"] = counts
        if not counts:
            report.add("WARN", "库里一张表都没有", evidence="表结构可能还没建起来（从未写过）",
                       scope="库")

        # 关系档案：条数与熟悉度分布（M7 的输入）
        if "relationships" in tables:
            rows = conn.execute(
                "SELECT familiarity, last_interaction_at FROM relationships"
            ).fetchall()
            values = sorted((as_float(row[0], 0.0) or 0.0) for row in rows)
            newest = max((as_float(row[1], 0.0) or 0.0) for row in rows) if rows else 0.0
            buckets = {"亲密(>=80)": 0, "熟络(50-79)": 0, "认识(20-49)": 0, "陌生(<20)": 0}
            for value in values:
                if value >= 80:
                    buckets["亲密(>=80)"] += 1
                elif value >= 50:
                    buckets["熟络(50-79)"] += 1
                elif value >= 20:
                    buckets["认识(20-49)"] += 1
                else:
                    buckets["陌生(<20)"] += 1
            detail["relationships"] = {
                "count": len(values), "buckets": buckets,
                "median": values[len(values) // 2] if values else 0.0,
                "max": values[-1] if values else 0.0,
                "newest_interaction": newest,
            }
            if values:
                report.add("OK", f"关系档案 {len(values)} 人（最新互动 "
                                 f"{human_span(now - newest) if newest else '未知'}前）",
                           evidence="、".join(f"{k} {v}" for k, v in buckets.items()),
                           scope="库")
                # v1.16.3 的真机现实：熟悉度涨得很慢（被 @ 一次才 +0.2、共同经历 +1.0、
                # 每 7 天 −0.5），而关系系数曲线的中性锚点是「陌生 <20 ⇒ ×1.0」。
                # 实测一台跑了一阵子的真机：20 人全部 <20（中位 0.2、最高 2.4）——
                # 也就是说 **M7 在这些机器上等价于没开**。这不是缺陷，但必须让读者知道，
                # 否则「熟人找她更开心」会被当成已经生效的功能。
                if max(values) < 20.0:
                    report.add("WARN", "关系档案里所有人都是「陌生」档 ⇒ 社交情绪系数恒 1.0",
                               evidence=f"最高熟悉度 {max(values):.1f}（「认识」档要 ≥20）；"
                                        "被 @ 一次只 +0.2，达到 20 需要约 100 次有效互动，"
                                        "且每 7 天未互动 −0.5",
                               fix="两种选择：① 接受「关系系数只在长期关系里生效」；"
                                   "② 把 `[social] relation_emotion_curve` 的锚点降到实测"
                                   "分布能到的地方（例如 `2=1.05` / `8=1.2` / `20=1.5`），"
                                   "但要注意此时**新装插件的陌生人也会被放大**，"
                                   "「开箱即用 = 旧行为」这条保证就不再成立",
                               scope="库")

        # 习惯层：最近命中记录（证明 routine 在跑）
        if "routine_daily" in tables:
            rows = conn.execute(
                "SELECT day_key, line_id, fired_at FROM routine_daily"
            ).fetchall() if _has_columns(conn, "routine_daily", ("day_key", "fired_at")) else []
            detail["routine_recent"] = [
                {"day_key": str(row[0]), "line": str(row[1])[:24],
                 "at": as_float(row[2], 0.0) or 0.0} for row in rows[-5:]
            ]
    finally:
        conn.close()
        _cleanup_temp(temp_dir)
    if note:
        report.add("WARN", "读库走了复制路径", evidence=note,
                   fix="通常没问题（只读保护）；若频繁发生，检查宿主是否在写库时锁住",
                   scope="库")
    return detail


def _has_columns(conn: sqlite3.Connection, table: str, columns: Sequence[str]) -> bool:
    try:
        names = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    except sqlite3.Error:
        return False
    return all(name in names for name in columns)


# ================================================================ 日志侧采集


LOG_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b", re.I)
LOG_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")

#: 「确定性故障」签名：命中就是真的出问题了，不是观察项
LOG_FAIL_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("插件加载失败", "插件没被宿主加载起来"),
    ("failed_plugins", "宿主报告了加载失败的插件列表"),
    ("生活状态推进异常", "生活循环里抛了异常"),
    ("状态文件损坏", "状态文件不可解析（已按空状态继续）"),
    ("Traceback", "有一次未捕获的异常栈"),
    ("E_TIMEOUT", "宿主能力调用超时（默认 30s 上限）"),
    # v1.20.1：宿主 RPC **单帧超限**。真机实测四个插件在 replyer 链上同时
    # `[E_UNKNOWN] 帧大小 17065957 超过最大限制 16777216`——**插件侧零反馈**
    # （hook 正常返回，`modified_kwargs` 却整帧发不出去），所以插件自己永远告不了警，
    # 只能靠这一条在宿主日志里被发现。看到它要连着看：谁的 hook、载荷从哪来。
    ("帧大小", "宿主 RPC 单帧超限：这个 hook 的改动整帧没送达（插件侧看不见）"),
    ("没有权限", "能力授权缺失（manifest 声明与调用不一致）"),
    ("不允许的能力", "能力授权缺失（manifest 声明与调用不一致）"),
)
#: 「可疑」签名：可能是正常降级，也可能是在持续失败。
#: ⚠ 只放**真的意味着某件事没做成**的措辞：像「社交经历：接进 N 条」这种正常轮次的
#: 日志绝不能进来——每次都命中等于把可疑栏刷成噪音，读者就再也不看它了。
LOG_WARN_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("写入失败", "倍率没写进宿主"),
    ("读不到宿主现值", "读回失败，本次不写"),
    ("已降级", "某个上游/通道降级"),
    ("获取会话列表失败", "chat.get_all_streams 调用失败"),
    ("关系索引刷新失败", "v1.16.3 的内存索引刷新失败（只影响加成，不影响档案）"),
    ("归因输出失败", "/生活 归因 渲染失败"),
    ("写不进去", "会话没有 heartflow chat 对象，写入被宿主静默吃掉"),
    ("冷却", "模型进入冷却（连续失败后）"),
)

#: 行内 JSON 的 ``"level"`` 字段（真机日志是 ``{"event": …, "level": "warning"}`` 形状）。
_LEVEL_FIELD_RE = re.compile(r'"level"\s*:\s*"([a-z]+)"', re.I)
#: 行内 JSON 的 logger 名（``logger_name`` 是宿主风格、``logger`` 是插件 runner 风格）。
LOGGER_FIELD_RE = re.compile(r'"(?:logger_name|logger)"\s*:\s*"([^"]{1,120})"')
#: 扫描时**不参与匹配**的级别：签名描述的是「某件事没做成」，这些级别是正常轮次。
_SIGNATURE_SKIP_LEVELS = frozenset({"debug", "info"})


def _signature_line_is_noise(line: str) -> bool:
    """行内 **JSON 的** ``"level"`` 是 debug/info ⇒ 正常轮次，不参与签名匹配。

    ⚠ 只认 ``"level": "info"`` 这种**字段形式**，不用 ``LOG_LEVEL_RE`` 裸词匹配——
    后者会把正文里出现 "INFO" 字样的行误判成 info 级。
    """

    match = _LEVEL_FIELD_RE.search(str(line or ""))
    return bool(match) and match.group(1).lower() in _SIGNATURE_SKIP_LEVELS


def first_log_line(line: str) -> str:
    """日志**自己的那一行**：在第一个换行处截断（真换行，或 JSON 里的 ``\\n`` 转义）。

    带换行的部分是**嵌入内容**（状态卡正文 / 模型输出 / 消息原文），不是这条日志想说的话。
    """

    text = str(line or "")
    cut = len(text)
    for sep in ("\n", "\\n"):
        idx = text.find(sep)
        if idx >= 0:
            cut = min(cut, idx)
    return text[:cut]


def match_signatures(line: str) -> tuple[str | None, str | None]:
    """一行日志命中的 ``(FAIL 签名, WARN 签名)``；两个都可能是 ``None``。

    **v1.20.1 真机修（2026-10-16，283 样本窗口实测 54 条误报）**。三个扫描点
    （一次性报告 / 增量日志 / 原文摘录）共用这一处判据——分散写三次就会出现
    「报告说不算、原文摘录却还在收」这种自相矛盾。

    两类签名**用不同的判据**，因为误报与漏报的代价不对称：

    * **FAIL 签名扫整行**，只滤 ``debug``/``info`` 级。理由：真故障的说明常常挂在
      ``exception``/``message`` 字段里（如 level=error 的
      「创建心流聊天 … 失败: 适配器名单禁止创建聊天运行实例」，它只有 ``exception``
      字段里带 "Traceback"）。为了消误报而截断，会把这类真实错误一起截掉——
      **漏报比误报更糟**。
    * **WARN 签名先截到日志自己的那一行，再要求不是 debug/info**。理由：``冷却`` /
      ``写不进去`` 这类短词会被**正常日志与正文**命中——实测命中的是别的插件的
      INFO 启动日志（``smart_poke_plugin``「冷却/每日上限…」）、**本插件自己发出去的
      ``/生活 状态`` 卡片正文**（卡面里本来就有「写不进去（退避中）」这一行）
      以及模型自己的推理文本。误报的代价是把真告警挤掉（同窗口 52 个 FAIL 样本
      全部来自这类命中）。
    """

    text = str(line or "")
    if not text.strip() or _signature_line_is_noise(text):
        return None, None
    fail_hit = next((needle for needle, _why in LOG_FAIL_SIGNATURES if needle in text), None)
    if fail_hit:
        return fail_hit, None
    head = first_log_line(text)
    if not head.strip():
        return None, None
    warn_hit = next((needle for needle, _why in LOG_WARN_SIGNATURES if needle in head), None)
    return None, warn_hit


def find_log_candidates(roots: Sequence[pathlib.Path], *, max_seconds: float = 12.0) -> list[pathlib.Path]:
    """在若干根目录下找日志（按修改时间取最近几份）。

    ⚠ 不限定扩展名：MaiBot 的日志可能是 ``.log`` / ``.txt`` / 无扩展名 / 轮转备份
    （``maibot.log.1``）。真机上第一次采集就因为只认 ``*.log`` 而漏掉了日志，
    报告里于是只有「未采集到日志」——那是采集口径问题，不是插件没问题。
    """

    found: list[pathlib.Path] = []
    deadline = time.monotonic() + max_seconds
    for root in roots:
        if not root.is_dir():
            continue
        candidates: list[pathlib.Path] = []
        for name in ("logs", "log", "data/logs"):
            folder = root / name
            if folder.is_dir():
                candidates.append(folder)
        # 根目录下直接放的日志文件（有些部署把 maibot.log 放在根上）
        try:
            for item in root.glob("*.log*"):
                if item.is_file():
                    found.append(item)
        except OSError:
            pass
        for folder in candidates:
            try:
                for item in _walk_files(folder, depth=3):
                    if time.monotonic() > deadline:
                        return _rank_logs(found)
                    if item.is_file():
                        found.append(item)
            except OSError:
                continue
    return _rank_logs(found)


def _walk_files(root: pathlib.Path, *, depth: int) -> Iterable[pathlib.Path]:
    """有界遍历（不跟随符号链接，跳过明显的噪声目录）。"""

    stack: list[tuple[pathlib.Path, int]] = [(root, 0)]
    while stack:
        current, level = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir():
                if level < depth and child.name not in SKIP_DIRS:
                    stack.append((child, level + 1))
                continue
            yield child


def _rank_logs(found: Sequence[pathlib.Path]) -> list[pathlib.Path]:
    def mtime(path: pathlib.Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(set(found), key=mtime, reverse=True)[:8]


def _logger_suffix(line: str) -> str:
    """摘录头尾附的 ``｜来源 logger``（v1.20.1 #4）。

    **为什么必须带上**：真机那一轮里 ``maisaka.context.append`` / ``proactive.trigger``
    失败 213 次，全部集中在**一个不属于本插件的 session** 上——但能力名与本插件的
    manifest 重叠，光看能力名极易算成"本插件在报错"。带上 logger 之后一眼能看出
    是 ``plugin_runtime.integration`` 还是 ``plugin.<别人的 id>``。
    认不出来返回空串（不编造）。
    """

    try:
        text = first_log_line(line)
        match = LOGGER_FIELD_RE.search(text)
        if not match:
            return ""
        name = match.group(1).strip()
        return f"｜{name}" if name else ""
    except Exception:  # noqa: BLE001 —— 摘录头不许因为取 logger 失败而不出
        return ""


def _signature_excerpts(lines: Sequence[str], *, source_name: str, max_blocks: int = 12,
                        stack_lines: int = 4) -> list[str]:
    """从（增量或全量的）日志行里收签名命中原文。

    v1.2.0（D10）：一次性报告的签名统计此前只看「本插件的行」，而 Traceback /
    E_TIMEOUT 的命中大多来自宿主日志里**不带插件标签**的行 ⇒ 报告的 FAIL 说
    「daily/ 里有原文」，原文却永远不在那里。现在全文扫描收原文（已脱敏）：
    FAIL 签名多带 ``stack_lines`` 行后文（异常栈紧跟 Traceback 头），WARN 只收
    命中行本身。每处命中前缀来源与签名名，方便归因。
    """

    blocks: list[str] = []
    for idx, line in enumerate(lines):
        if len(blocks) >= max_blocks:
            break
        fail_hit, warn_hit = match_signatures(line)
        if fail_hit:
            body = lines[idx:idx + 1 + stack_lines]
        elif warn_hit:
            body = [line]
        else:
            continue
        head = f"[{fail_hit or warn_hit}] {source_name}:{idx + 1}{_logger_suffix(line)}"
        blocks.append(head + "\n" + "\n".join(redact(item, limit=240) for item in body))
    return blocks


def collect_logs(report: Report, paths: Sequence[pathlib.Path], *, now: float,
                 max_lines: int = 200000) -> dict[str, Any]:
    if not paths:
        report.add("UNKNOWN", "未采集到日志",
                   evidence="日志是判断「哪一步失败」最直接的证据",
                   fix="用 --log <MaiBot 日志文件> 指定（或 --search-root 指向 MaiBot 根目录）",
                   scope="日志")
        return {"files": []}
    detail_files: list[dict[str, Any]] = []
    missing: list[str] = []
    for path in paths:
        if not path.exists():
            missing.append(str(path))
            continue
        text = read_text(path, limit=8 << 20)
        lines = text.splitlines()[-max_lines:]
        mine = [line for line in lines if PLUGIN_ID in line or "life-frequency" in line
                or PLUGIN_NAME in line]
        levels = {"ERROR": 0, "WARNING": 0, "INFO": 0, "DEBUG": 0}
        fail_hits: dict[str, int] = {}
        warn_hits: dict[str, int] = {}
        recent_errors: list[str] = []
        newest_stamp = ""
        for line in mine:
            level_match = LOG_LEVEL_RE.search(line)
            if level_match:
                key = level_match.group(1).upper().replace("WARN", "WARNING")
                if key in levels:
                    levels[key] += 1
            stamp = LOG_TS_RE.search(line)
            if stamp:
                newest_stamp = stamp.group(1)
            fail_hit, warn_hit = match_signatures(line)
            if fail_hit:
                fail_hits[fail_hit] = fail_hits.get(fail_hit, 0) + 1
            if warn_hit:
                warn_hits[warn_hit] = warn_hits.get(warn_hit, 0) + 1
            if level_match and key in ("ERROR", "CRITICAL") and len(recent_errors) < 8:
                recent_errors.append(redact(line, limit=240))
        entry = {
            "path": str(path), "size_bytes": path.stat().st_size,
            "total_lines": len(lines), "plugin_lines": len(mine),
            "levels": levels, "fail_signatures": fail_hits, "warn_signatures": warn_hits,
            "recent_errors": recent_errors, "newest_stamp": newest_stamp,
            "signature_excerpts": _signature_excerpts(lines, source_name=path.name),
            "sha256": sha256_of(path)[:16],
        }
        detail_files.append(entry)

        if not mine:
            report.add("WARN", f"日志里没有本插件的任何行：{path.name}",
                       evidence=f"读了 {len(lines)} 行",
                       fix="确认插件被加载（宿主日志里搜 " + PLUGIN_ID + "）；"
                           "或确认这份日志的时间范围覆盖了它运行的时间",
                       scope="日志")
            continue
        if fail_hits:
            report.add("FAIL", f"日志里有确定性故障签名（{sum(fail_hits.values())} 次）",
                       evidence="；".join(f"{k}×{v}" for k, v in fail_hits.items()),
                       fix="按上面的签名逐条定位（栈/异常在最下面的「最近错误」里）",
                       scope="日志")
        else:
            report.add("OK", f"日志里没有确定性故障签名（{len(mine)} 行本插件日志）",
                       evidence=f"ERROR {levels['ERROR']} / WARNING {levels['WARNING']}",
                       scope="日志")
        if warn_hits:
            report.add("WARN", "日志里有可疑签名（可能是正常降级，也可能在持续失败）",
                       evidence="；".join(f"{k}×{v}" for k, v in warn_hits.items()),
                       fix="看下面的「签名命中原文」「最近错误」与状态卡"
                           "（`/生活 状态`、`/生活 为什么`）",
                       scope="日志")
    if missing:
        report.add("UNKNOWN", f"指定的日志文件不存在（{len(missing)} 个）",
                   evidence="、".join(missing[:3]),
                   fix="确认路径；或先让宿主跑一会儿再采集", scope="日志")
    if not detail_files:
        report.add("UNKNOWN", "没有读到任何日志内容",
                   fix="用 --log <MaiBot 日志文件> 指定，或 --search-root 指向 MaiBot 根目录",
                   scope="日志")
    return {"files": detail_files, "missing": missing}


# ================================================================ 离线引擎自检（无真机数据时）


def offline_simulate(report: Report, plugin_dir: pathlib.Path, *, hours: float,
                     pure: "PurePlugin", config: Mapping[str, Any]) -> dict[str, Any]:
    """跑一段确定性生活（不调模型），验证引擎能推进并产出合法状态。

    ⚠ 报告里必须写清「这不是真机数据」——它只能证明**机制本身可运行**，
    不能证明部署正常。
    """

    if not pure.available:
        report.add("UNKNOWN", "跳过离线引擎自检（未加载到插件纯模块）",
                   evidence=pure.reason, scope="采集")
        return {}
    simulation = _section(config, "simulation") if config else {}
    emotion = _section(config, "emotion_energy") if config else {}
    overrides: dict[str, Any] = {
        "tz_offset_minutes": as_int(simulation.get("tz_offset_minutes"), 480),
        "tick_seconds": max(60, as_int(simulation.get("tick_seconds"), 600)),
    }
    if as_float(simulation.get("sleep_energy_threshold")) is not None:
        overrides["sleep_energy_threshold"] = as_float(simulation.get("sleep_energy_threshold"))
    for key in ("fatigue_ramp_curve", "baseline_diurnal_curve"):
        if emotion.get(key):
            points, _warnings = pure.parse_curve(emotion.get(key))
            if points:
                overrides[key] = points
    if as_float(emotion.get("recover_ratio_per_tick")) is not None:
        overrides["recover_ratio_per_tick"] = as_float(emotion.get("recover_ratio_per_tick"))
    if as_float(emotion.get("emotion_fatigue_penalty")) is not None:
        overrides["emotion_fatigue_penalty"] = as_float(emotion.get("emotion_fatigue_penalty"))
        overrides["emotion_fatigue_threshold"] = as_float(
            emotion.get("emotion_fatigue_threshold"), 3.0
        )
    if as_float(emotion.get("low_energy_drain_multiplier")) is not None:
        overrides["low_energy_drain_multiplier"] = as_float(
            emotion.get("low_energy_drain_multiplier")
        )
    if emotion.get("afterglow_decay") is not None:
        overrides["afterglow_decay"] = bool(emotion.get("afterglow_decay"))
    if emotion.get("emotion_impact_scaling") is not None:
        overrides["emotion_impact_scaling"] = bool(emotion.get("emotion_impact_scaling"))

    try:
        import random  # noqa: PLC0415

        config_obj = pure.sim_config_defaults(**overrides)
        try:
            from life_events import merge_events  # type: ignore  # noqa: PLC0415

            events, _event_warnings = merge_events(None, None)
        except Exception:  # noqa: BLE001 —— 没有事件库也能跑（只是没有随机事件）
            events = ()
        start = time.time() - hours * 3600.0
        state = pure.sim.new_state(now=start, config=config_obj, energy=8.0)
        rng = random.Random(20261010)
        steps = int(max(1, hours * 3600 / max(60, config_obj.tick_seconds)))
        for step in range(1, steps + 1):
            now_step = start + config_obj.tick_seconds * step
            pure.sim.settle(state, now=now_step, config=config_obj, events=events, rng=rng)
            facts = pure.sim.enforce_facts(state, now=now_step, config=config_obj)
            if not facts.sick:
                proposal = pure.activity.rule_based_activity(
                    now_minutes=facts.now_minutes,
                    sleep_need=float(facts.sleep_need),
                    sleep_need_onset=config_obj.sleep_need_onset,
                )
                pure.sim.enforce_and_apply(state, now=now_step, config=config_obj,
                                           decision=proposal)
        snapshot = state.to_dict()
    except Exception as exc:  # noqa: BLE001 —— 自检失败本身是一条结论
        report.add("FAIL", "离线引擎自检抛异常（机制本身跑不起来）",
                   evidence=f"{exc.__class__.__name__}: {exc}",
                   fix="先跑 `python run_gates.py --plugin .` 与 pytest，定位引擎层缺陷",
                   scope="采集")
        return {"error": f"{exc.__class__.__name__}: {exc}"}

    tz = as_int(overrides.get("tz_offset_minutes"), 480)
    detail = _state_checks(report, snapshot, tz=tz, now=time.time(), config=config,
                           label="离线引擎自检（**非真机数据**）", simulated=True)
    detail["hours"] = hours
    report.add("OK", f"离线引擎自检跑完 {hours:g} 小时",
               evidence=f"结论：情绪 {detail.get('emotion', 0):.2f}、"
                        f"体力 {detail.get('energy', 0):.2f}、"
                        f"经历 {detail.get('recent_events', 0)} 条——机制可运行",
               scope="采集")
    return detail


# ================================================================ 采集编排

PURE: "PurePlugin" = None  # type: ignore[assignment]


def build_report(plugin_dir: pathlib.Path, *, data_dir: pathlib.Path | None,
                 config_path: pathlib.Path | None, log_paths: Sequence[pathlib.Path],
                 search_roots: Sequence[pathlib.Path], simulate_hours: float,
                 run_gates: bool, now: float,
                 out_dir: pathlib.Path | None = None) -> Report:
    report = Report()
    report.machine = {
        "program_version": PROGRAM_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "plugin_dir": str(plugin_dir),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "now_epoch": now,
    }

    # ---- 0. 定位数据源（**必须做归属校验**：磁盘上可能有别的插件）----
    discovered: list[pathlib.Path] = []
    candidates: list[dict[str, Any]] = []
    if data_dir is None:
        discovered = discover_data_dirs(list(search_roots) + [plugin_dir])
        for candidate in discovered:
            score, notes = score_data_dir(candidate)
            candidates.append({"path": str(candidate), "score": score, "notes": notes})
            if score >= 8:  # 至少要有本插件签名的 life_state.json
                data_dir = candidate
                break
        if data_dir is None:
            # 没有状态文件时退一步：库里认得出本插件的表也算（例如状态文件被删/被重置）
            for candidate in discovered:
                score, _notes = score_data_dir(candidate)
                if score >= 4:
                    data_dir = candidate
                    break
    if data_dir is not None:
        data_dir = data_dir.resolve()
    report.machine["data_dir"] = str(data_dir) if data_dir else ""
    report.machine["discovered_dirs"] = [str(item) for item in discovered]
    report.machine["candidates"] = candidates
    report.machine["fake_dirs_ignored"] = _count_fake_dirs(list(search_roots))
    rejected = [item for item in candidates
                if data_dir is None or item["path"] != str(data_dir)]
    report.machine["rejected_candidates"] = rejected

    state_path = (data_dir / "life_state.json") if data_dir else None
    memory_path = (data_dir / "adjust_memory.json") if data_dir else None
    store_path = (data_dir / "life_store.db") if data_dir else None

    if config_path is None:
        # 候选 config.toml 按「是不是本插件的分节」筛一遍，绝不捡别的插件的配置
        for candidate in [(data_dir / CONFIG_FILE) if data_dir else None,
                          plugin_dir / CONFIG_FILE,
                          *(root / CONFIG_FILE for root in search_roots),
                          *(path / CONFIG_FILE for path in discovered)]:
            if candidate is None or not candidate.exists():
                continue
            payload, _error = _load_toml(candidate)
            if looks_like_config(payload):
                config_path = candidate
                break
            report.machine.setdefault("rejected_configs", []).append(
                {"path": str(candidate), "why": "分节不像本插件的配置"}
            )
    if not log_paths:
        log_paths = find_log_candidates(list(search_roots) + [plugin_dir])

    # ---- 先确认插件目录指对了：指到 plugins 根会造成两条假 FAIL，还读不到真机版本 ----
    original_plugin_dir = plugin_dir
    plugin_dir, locate_note = locate_plugin_dir(plugin_dir, search_roots)
    report.machine["plugin_dir_original"] = str(original_plugin_dir)
    report.machine["plugin_dir"] = str(plugin_dir)
    if locate_note:
        corrected = plugin_dir != original_plugin_dir
        report.add("OK" if corrected else "WARN", locate_note,
                   fix="在真机上重跑时用 `--plugin-dir <插件目录>`"
                       "（或把 collect_diagnostics.py 放进插件目录再跑）",
                   scope="采集")

    for source in (
        # 代码目录不进 zip（要源码就 --with-sources；数据包保持小巧）
        Source("插件代码目录", plugin_dir, "诊断目标", pack=False),
        Source("运行时数据目录", data_dir, "由 --data-dir 指定或自动搜索得到"),
        Source("config.toml", config_path, "宿主落盘的配置"),
        Source("life_store.db", store_path),
        Source("日志", log_paths[0] if log_paths else None,
               f"共 {len(log_paths)} 份" if log_paths else "未找到"),
    ):
        report.sources.append(source)

    global PURE
    PURE = PurePlugin(plugin_dir)
    if PURE.available:
        report.add("OK", "已加载插件纯模块用于权威校验",
                   evidence="life_sim / life_activity / life_factors / life_relations", scope="采集")
    elif plugin_dir == original_plugin_dir:
        report.add("WARN", "未能加载插件纯模块（退回内置最小校验）",
                   evidence=PURE.reason,
                   fix="确认插件目录里有 life_sim.py 等模块、且解释器版本与插件一致",
                   scope="采集")

    # ---- 1. 代码侧 ----
    code_info = collect_code(report, plugin_dir)

    # ---- 2. 配置侧 ----
    config_info = collect_config(report, config_path, PURE, plugin_dir)
    config_payload = {}
    if config_path is not None and config_path.exists():
        loaded, error = _load_toml(config_path)
        config_payload = loaded if not error else {}
    tz = as_int(_section(config_payload, "simulation").get("tz_offset_minutes"), 480)

    # ---- 3. 状态侧 ----
    state_info = collect_state(report, state_path, config=config_payload, pure=PURE,
                               label="真机状态", now=now)
    memory_info = collect_memory(report, memory_path, now=now)

    # ---- 4. 库侧 ----
    store_info = collect_store(report, store_path, now=now, tz=tz, pure=PURE)

    # ---- 5. 日志侧 ----
    log_info = collect_logs(report, [path for path in log_paths if path], now=now)

    # ---- 6. 无真机数据时补一份离线引擎自检 ----
    sim_info: dict[str, Any] = {}
    if simulate_hours > 0:
        sim_info = offline_simulate(report, plugin_dir, hours=simulate_hours, pure=PURE,
                                    config=config_payload)
    elif not state_info:
        report.add("UNKNOWN", "没有真机状态数据，也没有开启离线自检",
                   evidence="因此无法判断「运转是否正常」，只能给出代码侧结论",
                   fix="加 `--offline-simulate 24` 做一次引擎自检，"
                       "或用 --data-dir 指向真机数据目录",
                   scope="采集")

    # ---- 7. 可选：跑一遍门禁（会执行插件自带的脚本）----
    gates_info: dict[str, Any] = {}
    if run_gates:
        gates_script = plugin_dir / "run_gates.py"
        if gates_script.exists():
            try:
                proc = subprocess.run(
                    [sys.executable, str(gates_script), "--plugin", str(plugin_dir)],
                    cwd=str(plugin_dir), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=900,
                )
                tail = (proc.stdout or "").strip().splitlines()[-14:]
                gates_info = {"returncode": proc.returncode, "tail": tail}
                if proc.returncode == 0:
                    report.add("OK", "门禁脚本跑通（check/smoke/pytest）",
                               evidence=tail[-1] if tail else "", scope="采集")
                else:
                    report.add("FAIL", "门禁脚本失败", evidence=" | ".join(tail[-3:]),
                               fix="按门禁输出修问题", scope="采集")
            except (OSError, subprocess.SubprocessError) as exc:
                report.add("UNKNOWN", "门禁脚本没跑起来", evidence=str(exc), scope="采集")
        else:
            report.add("UNKNOWN", "没有 run_gates.py，跳过门禁", scope="采集")

    inventory = collect_inventory(plugin_dir, data_dir,
                                  exclude=[out_dir] if out_dir else [])
    # ---- 渲染 ----
    config_detail = dict(config_info)
    config_detail["effective"] = {
        "tick_seconds": as_int(_section(config_payload, "simulation").get("tick_seconds"), 600),
        "tz_offset_minutes": tz,
        # v1.24.0：睡不睡只看这四个值（外加昼夜偏置与生病）。sleep_window 已退役，
        # 不再列进「生效配置」——列出来会让人以为它还在起作用。
        "sleep_need_onset": as_float(
            _section(config_payload, "simulation").get("sleep_need_onset"), 0.62),
        "sleep_need_wake": as_float(
            _section(config_payload, "simulation").get("sleep_need_wake"), 0.16),
        "sleep_target_hours": as_float(
            _section(config_payload, "simulation").get("sleep_target_hours"), 8.0),
        "sleep_deferral_minutes": as_int(
            _section(config_payload, "simulation").get("sleep_deferral_minutes"), 15),
        "sleep_energy_threshold": _section(config_payload, "simulation").get(
            "sleep_energy_threshold"),
        "filter_mode": _section(config_payload, "apply").get("filter_mode"),
        "quiet_hours": _section(config_payload, "frequency").get("quiet_hours"),
        "proactive_enabled": _section(config_payload, "proactive").get("enabled"),
        "relations_enabled": _section(config_payload, "relations").get("enabled"),
        "social_enabled": _section(config_payload, "social").get("enabled"),
        "mood_enabled": _section(config_payload, "mood").get("enabled"),
        "activity_task_name": _section(_section(config_payload, "activity"), "llm").get(
            "task_name"),
    }

    render_markdown(report, now=now, tz=tz, code=code_info, config=config_detail,
                    state=state_info, memory=memory_info, store=store_info, logs=log_info,
                    simulation=sim_info, gates=gates_info, inventory=inventory)
    report.machine["details"] = {
        "code": code_info, "config": config_detail, "state": state_info,
        "memory": memory_info, "store": store_info, "logs": log_info,
        "simulation": sim_info, "gates": gates_info, "inventory": inventory,
    }
    return report


def _count_fake_dirs(roots: Sequence[pathlib.Path]) -> int:
    """数一下被忽略的测试残留目录（FakeHost 的 temp 目录）——它们不是真机数据。"""

    count = 0
    for root in roots:
        try:
            for item in root.iterdir():
                if item.is_dir() and FAKE_DIR_RE.search(item.name):
                    count += 1
        except OSError:
            continue
    return count


def collect_inventory(plugin_dir: pathlib.Path, data_dir: pathlib.Path | None,
                      *, exclude: Sequence[pathlib.Path] = ()) -> dict[str, Any]:
    """文件清单 + 指纹。排除 .git / 缓存 / 本程序的输出目录与临时夹具目录。"""

    excluded = {path.resolve() for path in exclude}
    noise_dirs = SKIP_DIRS | {".update_backups", ".update_backup", "diagnostics"}

    def walk(root: pathlib.Path, *, in_data: bool) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not root or not root.is_dir():
            return items
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.resolve() in excluded:
                continue
            rel = path.relative_to(root).as_posix()
            parts = rel.split("/")
            # 目录部分命中噪声名、或以 "." 开头（.update_backups / .github / .git …）
            # 一律不进清单：真机实测 plugins 根下有几十份别的插件的备份，附录一度 1160 行
            if any(part in noise_dirs or part.startswith(".") for part in parts[:-1]):
                continue
            if not in_data and any(part.startswith("_") for part in parts[:-1]):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            items.append({"path": rel, "size": size, "sha256": sha256_of(path)[:16]})
        return items

    return {
        "plugin_files": walk(plugin_dir, in_data=False),
        "data_files": walk(data_dir, in_data=True) if data_dir else [],
    }


# ================================================================ 渲染


LEVEL_ORDER = {"FAIL": 0, "WARN": 1, "UNKNOWN": 2, "OK": 3}
LEVEL_LABEL = {"FAIL": "❌ FAIL", "WARN": "⚠ WARN", "UNKNOWN": "❔ UNKNOWN", "OK": "✅ OK"}


def verdict(report: Report, has_state: bool, simulated: bool) -> tuple[str, str]:
    """总判定。**刻意不把「离线自检通过」当成「真机正常」**——那是最容易犯的越权结论。"""

    counts = report.counts()
    if counts["FAIL"]:
        return ("不正常的明确证据（FAIL）",
                f"有 {counts['FAIL']} 条确定性故障。逐条看第 1 节的证据与修法。")
    if not has_state:
        detail = ("只采集到代码侧与（可选的）离线自检：**没有 life_state.json 就无法判断"
                  "真机运转是否正常**——代码/机制没问题不等于部署没问题。"
                  "请按第 2 节的采集命令补一份真机数据。")
        if simulated:
            detail = ("代码侧与离线机制自检都通过（没有 FAIL）；但**没有真机数据**，"
                      "所以「真机是否正常运行」仍未验证——离线自检只证明机制能跑。"
                      "请按第 2 节的采集命令补一份真机数据。")
        return ("无法判断真机是否正常（缺运行时数据）", detail)
    if counts["WARN"]:
        return ("基本正常，但有告警（WARN）",
                f"{counts['WARN']} 条告警：多数是配置取舍或历史遗留，但值得逐条确认。")
    return ("正常运转", "所有检查项通过：生活循环在推进、模型侧健康、数据文件完整。")


def render_markdown(report: Report, *, now: float, tz: int, code: Mapping[str, Any],
                    config: Mapping[str, Any], state: Mapping[str, Any],
                    memory: Mapping[str, Any], store: Mapping[str, Any],
                    logs: Mapping[str, Any], simulation: Mapping[str, Any],
                    gates: Mapping[str, Any], inventory: Mapping[str, Any]) -> None:
    counts = report.counts()
    headline, headline_detail = verdict(report, bool(state), bool(simulation))
    manifest = code.get("manifest") or {}
    state_simulated = bool(state.get("simulated"))

    lines: list[str] = []
    lines.append(f"# {PLUGIN_NAME} 运行诊断报告")
    lines.append("")
    lines.append(f"| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 总判定 | **{headline}** |")
    lines.append(f"| 检查项 | ❌ {counts['FAIL']}　⚠ {counts['WARN']}　❔ {counts['UNKNOWN']}"
                 f"　✅ {counts['OK']} |")
    lines.append(f"| 插件版本 | manifest `{manifest.get('version')}`"
                 f"（配置 schema `{code.get('config_version')}`） |")
    lines.append(f"| 代码目录 | `{code.get('plugin_dir')}` |")
    lines.append(f"| 数据目录 | `{report.machine.get('data_dir') or '（未找到）'}` |")
    lines.append(f"| 报告生成 | {report.machine.get('generated_at')}"
                 f"（采集程序 v{PROGRAM_VERSION}） |")
    lines.append("")
    lines.append(f"> {headline_detail}")
    if not state:
        lines.append(">")
        lines.append("> ⚠ **本报告没有真机状态数据**：第 1 节的通过项只覆盖代码/配置/日志"
                     "（有日志时），"
                     "「她此刻在做什么、生活循环还在不在跑」这类结论**这一份给不出来**。")
    if state_simulated:
        lines.append(">")
        lines.append("> ⚠ 本报告里的**状态部分来自离线引擎自检（模拟数据）**，"
                     "它只能证明「机制能跑」，不能证明真机部署正常。")
    lines.append("")

    # ---- 1. 检查清单 ----
    lines.append("## 1. 检查清单（按严重程度排序）")
    lines.append("")
    lines.append("| 级别 | 范围 | 检查项 | 证据 | 怎么修 |")
    lines.append("|---|---|---|---|---|")
    for finding in sorted(report.findings, key=lambda item: LEVEL_ORDER.get(item.level, 9)):
        lines.append(
            f"| {LEVEL_LABEL.get(finding.level, finding.level)} | {finding.scope or '—'} "
            f"| {finding.title} | {finding.evidence or '—'} | {finding.fix or '—'} |"
        )
    lines.append("")

    # ---- 2. 数据源 ----
    lines.append("## 2. 采集到的数据源")
    lines.append("")
    lines.append("| 类型 | 路径 | 说明 |")
    lines.append("|---|---|---|")
    for source in report.sources:
        note = source.note
        if source.simulated:
            note = (note + "；**模拟数据**").strip("；")
        if source.path is None:
            shown = "（未找到）"
        elif not source.path.exists():
            shown = f"`{source.path}`（不存在）"
        else:
            shown = f"`{source.path}`"
        lines.append(f"| {source.kind} | {shown} | {note} |")
    lines.append("")
    rejected = report.machine.get("rejected_candidates") or []
    if rejected:
        lines.append("**被排除的候选目录**（文件名像、但不是本插件的数据）：")
        lines.append("")
        lines.append("| 目录 | 归属分 | 判定依据 |")
        lines.append("|---|---|---|")
        for item in rejected[:8]:
            lines.append(f"| `{item['path']}` | {item['score']} | "
                         + "；".join(item["notes"]) + " |")
        lines.append("")
    rejected_configs = report.machine.get("rejected_configs") or []
    if rejected_configs:
        lines.append("被排除的 config.toml："
                     + "、".join(f"`{item['path']}`（{item['why']}）"
                                 for item in rejected_configs[:5]))
        lines.append("")
    lines.append("**补采集的办法**（缺哪一项就用哪一条）：")
    lines.append("")
    lines.append("```bash")
    lines.append("# 真机数据目录（life_state.json / life_store.db / adjust_memory.json 所在处）")
    lines.append("python collect_diagnostics.py --data-dir <MaiBot>/data/plugins/"
                 + PLUGIN_ID)
    lines.append("# 宿主落盘的配置")
    lines.append("python collect_diagnostics.py --config <MaiBot>/config/plugins/"
                 + PLUGIN_ID + "/config.toml")
    lines.append("# 日志（可多次）")
    lines.append("python collect_diagnostics.py --log <MaiBot>/logs/maibot.log")
    lines.append("# 一把梭：搜出所有数据 + 打包")
    lines.append("python collect_diagnostics.py --search-root <MaiBot根目录> --bundle")
    lines.append("```")
    if report.machine.get("fake_dirs_ignored"):
        lines.append("")
        lines.append(f"> 自动搜索时忽略了 {report.machine['fake_dirs_ignored']} 个"
                     "`maibot-fake-*` 测试残留目录（FakeHost 的临时目录，不是真机数据）。")
    lines.append("")

    # ---- 3. 代码侧 ----
    lines.append("## 3. 代码侧（她跑的是哪一份代码）")
    lines.append("")
    lines.append(f"- manifest：id `{manifest.get('id')}`、version `{manifest.get('version')}`、"
                 f"name `{manifest.get('name')}`、manifest_version `{manifest.get('manifest_version')}`")
    lines.append(f"- 宿主/SDK 版本区间：{manifest.get('host')} / {manifest.get('sdk')}")
    capabilities = manifest.get("capabilities") or []
    lines.append(f"- 能力声明 {len(capabilities)} 项：{'、'.join(str(c) for c in capabilities)}")
    lines.append(f"- 模块数 {code.get('module_count')}；README {code.get('readme_lines')} 行")
    git = code.get("git") or {}
    if git:
        lines.append(f"- git：HEAD `{git.get('head')}`　`{git.get('subject')}`")
    lines.append("")

    # ---- 4. 配置侧 ----
    lines.append("## 4. 配置侧（实际生效的关键参数）")
    lines.append("")
    if not config:
        lines.append("（没有采集到 config.toml —— 见第 2 节的采集命令）")
    else:
        lines.append(f"配置文件：`{config.get('path')}`")
        lines.append("")
        lines.append("| 参数 | 值 |")
        lines.append("|---|---|")
        for key, value in (config.get("effective") or {}).items():
            shown = json.dumps(value, ensure_ascii=False)
            if key == "activity_task_name" and not value:
                shown = "`（空 = 用插件默认任务路由）`"
            else:
                shown = f"`{shown}`"
            lines.append(f"| `{key}` | {shown} |")
        missing = config.get("missing_fields") or []
        if missing:
            lines.append("")
            lines.append(f"**缺 v1.16 新增项 {len(missing)} 个**（会按内置默认值运行）："
                         + "、".join(f"`{item}`" for item in missing))
    lines.append("")

    # ---- 5. 状态侧 ----
    lines.append("## 5. 状态侧：她此刻在做什么")
    lines.append("")
    if not state:
        lines.append("（没有采集到 life_state.json —— 见第 2 节的采集命令）")
    else:
        lines.append(f"数据来源：`{state.get('path')}`"
                     + ("　**（离线模拟，非真机）**" if state_simulated else ""))
        lines.append("")
        lines.append("| 项 | 值 |")
        lines.append("|---|---|")
        lines.append(f"| 活动 | {state.get('activity_label') or '—'}"
                     f"（`{state.get('activity')}`，来源 {state.get('activity_source') or '—'}） |")
        lines.append(f"| 场景 | {redact(state.get('scene'), limit=60) or '—'} |")
        lines.append(f"| 活动已持续 | {state.get('minutes_in_activity')} 分钟 |")
        lines.append(f"| 情绪 / 体力 | {state.get('emotion'):.2f} / "
                     f"{state.get('energy'):.2f}（上限 {state.get('energy_cap'):.1f}） |"
                     if isinstance(state.get("emotion"), (int, float)) else "| 情绪 / 体力 | — |")
        lines.append(f"| 余波 | {state.get('afterglow'):+.2f} |"
                     if isinstance(state.get("afterglow"), (int, float)) else "| 余波 | — |")
        lines.append(f"| 今日已睡 / 清醒 | {state.get('sleep_minutes_today')} 分钟 / "
                     f"{state.get('awake_minutes_today')} 分钟（连续清醒 "
                     f"{(state.get('continuous_awake_minutes') or 0) / 60:.1f} 小时） |")
        need = state.get("sleep_need")
        lines.append(f"| 困意 | {need:.2f}（0=不困，1=最困；睡不睡只看它） |"
                     if isinstance(need, (int, float)) else "| 困意 | — |")
        legacy_nights = state.get("legacy_sleep_debt_nights") or 0
        lines.append(f"| 熬夜债 | {state.get('sleep_debt_nights')} 晚"
                     + (f"（旧模型留档 {legacy_nights} 晚，仅作证据）" if legacy_nights else "")
                     + " |")
        lines.append(f"| 内心 | 压力 {state.get('stress')} / 孤独 {state.get('loneliness')}"
                     f" / 电量 {state.get('social_battery')} |")
        lines.append(f"| 生活循环 | 最近推进距今 "
                     f"{human_span(state.get('last_tick_age_seconds') or 0)} |")
        lines.append(f"| 模型 | 连续失败 {state.get('llm_fail_streak')} 次、冷却剩 "
                     f"{human_span(state.get('llm_cooldown_seconds_left') or 0)}、"
                     f"最近成功 {human_span(now - (state.get('llm_last_success') or 0)) if state.get('llm_last_success') else '从未'}前 |")
        lines.append(f"| 写宿主 | 已写 {state.get('applied_sessions')} 个会话、"
                     f"外部基数 {state.get('foreign_sessions')} 个、"
                     f"退避中 {state.get('unbacked_sessions')} 个 |")
        lines.append(f"| 叙事层 | 经历 {state.get('recent_events')} 条"
                     f"（最新：{state.get('newest_event_label') or '—'}）、"
                     f"素材 {state.get('materials')} 条（未过期 {state.get('live_materials')}） |")
        if state.get("windows"):
            lines.append("| 生效中的窗口 | "
                         + "；".join(f"{k} → {human_stamp(v, tz)}"
                                     for k, v in state["windows"].items()) + " |")
        if state.get("table_sizes"):
            lines.append("| 去重表规模 | "
                         + "、".join(f"{k}={v}" for k, v in state["table_sizes"].items())
                         + " |")
        if state.get("sanitized_keys"):
            lines.append(f"| 被净化的坏值 | {len(state['sanitized_keys'])} 个："
                         + "、".join(state["sanitized_keys"][:6]) + " |")
    lines.append("")

    # ---- 6. 库侧 ----
    lines.append("## 6. 库侧（关系档案 / 习惯日态）")
    lines.append("")
    if not store:
        lines.append("（没有采集到 life_store.db —— 见第 2 节的采集命令）")
    else:
        lines.append(f"`{store.get('path')}`　{human_size(store.get('size_bytes') or 0)}"
                     f"　integrity=`{store.get('integrity')}`"
                     f"　WAL={'有' if store.get('wal_present') else '无'}")
        counts_rows = store.get("row_counts") or {}
        lines.append("")
        lines.append("| 表 | 行数 |")
        lines.append("|---|---|")
        for table, count in counts_rows.items():
            lines.append(f"| `{table}` | {count} |")
        rel = store.get("relationships") or {}
        if rel:
            lines.append("")
            lines.append(f"关系档案 {rel.get('count')} 人，熟悉度中位 "
                         f"{rel.get('median', 0):.1f}、最高 {rel.get('max', 0):.1f}；"
                         + "、".join(f"{k} {v}" for k, v in (rel.get("buckets") or {}).items()))
        recent = store.get("routine_recent") or []
        if recent:
            lines.append("")
            lines.append("习惯层最近命中："
                         + "；".join(f"{item['day_key']} {item['line']}" for item in recent))
    lines.append("")

    # ---- 7. 日志侧 ----
    lines.append("## 7. 日志侧")
    lines.append("")
    files = logs.get("files") or []
    if not files:
        lines.append("（没有采集到日志 —— 见第 2 节的采集命令）")
    for entry in files:
        lines.append(f"### `{entry['path']}`")
        lines.append("")
        lines.append(f"- 读取 {entry['total_lines']} 行，其中本插件 "
                     f"{entry['plugin_lines']} 行；ERROR {entry['levels']['ERROR']}、"
                     f"WARNING {entry['levels']['WARNING']}、INFO {entry['levels']['INFO']}")
        if entry.get("newest_stamp"):
            lines.append(f"- 日志里本插件最新一条时间戳：`{entry['newest_stamp']}`")
        if entry.get("fail_signatures"):
            lines.append("- **确定性故障签名**：" + "；".join(
                f"`{k}`×{v}" for k, v in entry["fail_signatures"].items()))
        if entry.get("warn_signatures"):
            lines.append("- 可疑签名：" + "；".join(
                f"`{k}`×{v}" for k, v in entry["warn_signatures"].items()))
        if entry.get("signature_excerpts"):
            # v1.2.0（D10）：签名命中的**原文**（全文扫描，不限本插件行——
            # 命中可能来自宿主或其它插件；已脱敏）。
            lines.append("- 签名命中原文（全文扫描，不限本插件行；已脱敏）：")
            lines.append("")
            lines.append("```text")
            for block in entry["signature_excerpts"]:
                lines.extend(block.splitlines())
                lines.append("· · ·")
            lines.append("```")
        if entry.get("recent_errors"):
            lines.append("- 最近错误（已脱敏）：")
            lines.append("")
            lines.append("```text")
            lines.extend(entry["recent_errors"])
            lines.append("```")
        lines.append("")

    # ---- 8. 离线自检 / 门禁 ----
    if simulation:
        lines.append("## 8. 离线引擎自检（非真机数据）")
        lines.append("")
        lines.append(f"用插件自己的纯模块跑了 **{simulation.get('hours')} 小时**确定性生活"
                     "（不调模型、活动走时段表、固定种子）：")
        lines.append("")
        lines.append(f"- 结果：活动 `{simulation.get('activity')}`、"
                     f"情绪 {simulation.get('emotion'):.2f}、体力 {simulation.get('energy'):.2f}")
        lines.append(f"- 经历 {simulation.get('recent_events')} 条、"
                     f"素材 {simulation.get('materials')} 条、"
                     f"今日已睡 {simulation.get('sleep_minutes_today')} 分钟")
        lines.append("")
    if gates:
        lines.append("## 9. 门禁（check_plugin / smoke / pytest）")
        lines.append("")
        lines.append(f"- `run_gates.py` 退出码 **{gates.get('returncode')}**")
        lines.append("")
        lines.append("```text")
        lines.extend(str(item) for item in (gates.get("tail") or []))
        lines.append("```")
        lines.append("")

    # ---- 附录：文件清单 ----
    lines.append("## 附录 A. 文件清单与指纹（确认真机跑的就是这一份）")
    lines.append("")
    plugin_files = inventory.get("plugin_files") or []
    data_files = inventory.get("data_files") or []
    lines.append(f"- 插件目录 {len(plugin_files)} 个文件（已排除 .git/__pycache__/.pytest_cache/"
                 ".update_backups 等噪声目录）")
    lines.append(f"- 数据目录 {len(data_files)} 个文件")
    lines.append("")
    lines.append("| 文件 | 大小 | SHA256(前16) |")
    lines.append("|---|---|---|")
    for item in plugin_files[:INVENTORY_ROWS]:
        lines.append(f"| `{item['path']}` | {human_size(item['size'])} | `{item['sha256']}` |")
    for item in data_files[:INVENTORY_ROWS]:
        lines.append(f"| `[data] {item['path']}` | {human_size(item['size'])} | `{item['sha256']}` |")
    if len(plugin_files) + len(data_files) > INVENTORY_ROWS:
        lines.append("")
        lines.append(f"（表格只列前 {INVENTORY_ROWS} 个；完整清单（含每个文件的大小与 SHA256）"
                     f"在配套的 `.json` 里）")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"采集程序 `collect_diagnostics.py` v{PROGRAM_VERSION}（只读；"
                 "不改插件目录与运行时数据）。复现：")
    lines.append("")
    lines.append("```bash")
    lines.append("python collect_diagnostics.py --data-dir <数据目录> --config <config.toml> "
                 "--log <日志> --offline-simulate 24 --bundle")
    lines.append("```")
    lines.append("")
    lines.append("**脱敏口径**：凭据类键值（`p_skey` / `token` / `cookie` …）一律写成 "
                 "`<redacted>`；带身份语境的号码（`session=`、`user_id:`、`group-123456`）"
                 "写成 `***`；其余裸数字（epoch、字节数、哈希）**保留原样**——"
                 "把数字全打码会让报告读不出来。报告里不含任何消息正文。")

    report.section("REPORT", "\n".join(lines))


# ================================================================ 长期监控（watch）
#
# 一次性报告回答「此刻正常吗」；一周以上的观察要回答的是另一个问题：
# **「她是不是一直正常」**。卡顿、停机、模型连续失败、状态表格膨胀、日志里的一次异常栈，
# 都不是某一刻的快照能看出来的——必须靠**时序样本**。
#
# 所以 watch 模式做四件事，且只做这四件：
#   ① 每 ``--interval`` 秒采一个**轻量样本**（状态/配置/库计数 + 增量扫日志尾部），
#      写成 ``samples.jsonl`` 与 ``samples.csv``（时序证据，能直接导入 Excel 画图）；
#   ② 用**与一次性报告完全相同**的判定逻辑（``_state_checks``）逐样本判定，把 FAIL/WARN
#      送进告警状态机（开始/升级/降级/恢复）——同一问题只在**变化**时落一条，
#      一周 2016 个样本也不会刷出噪音（告警疲劳会让人再也不看告警）；
#   ③ 每天（``--report-every-hours``）落一份**完整诊断报告**（复用 ``build_report``），
#      新出现的 FAIL 也立刻补一份完整报告（有节流），把现场证据固定下来；
#   ④ 随时可读的 ``watch_summary.md``：覆盖缺口、卡顿、模型趋势、逐日数值走势、未恢复告警。
#
# ⚠ 两个刻意的设计取舍：
#   * **不另写一套阈值**。样本判定直接调一次性报告的 `_state_checks`，否则「诊断说 FAIL、
#     监控说 OK」这种自相矛盾迟早会出现（两套判据迟早漂移）。
#   * **不做复制式读库**。`_open_readonly` 在直连失败时会**复制**一份库——一次性诊断可以，
#     但每 5 分钟跑一次会在 temp 里堆一周的副本。样本只走直连，读不到就记一条 note，
#     留给每日的完整报告去覆盖（它允许复制，且副本用完即删，见 `_cleanup_temp`）。

WATCH_SCHEMA_VERSION = 1
#: 采样间隔（秒）。插件 tick 默认 600s，5 分钟采样已足够看出卡顿与停机
DEFAULT_WATCH_INTERVAL = 300.0
#: 默认监控时长：7 天（用户要求「至少持续一周」）
DEFAULT_WATCH_HOURS = 168.0
#: 样本保留期：8 周（比监控窗口长，便于「上周和这周对比」）
DEFAULT_RETAIN_DAYS = 56.0
WATCH_SUBDIR = "watch"
#: 第一次见到某份日志时只读末尾这么多字节（一周的日志可能上百 MB，
#: 第一次就全读等于每挂一个监视器给磁盘来一次压力测试）
LOG_TAIL_FIRST_BYTES = 1 << 20
#: 单轮最多读多少新增日志字节（避免某轮被几百 MB 的新增拖住，采样本该是秒级的）
LOG_TAIL_MAX_BYTES_PER_POLL = 4 << 20
#: alerts.log 超阈值就裁到保留行数（一周不裁会到几十万行）
ALERT_LOG_TRIM_THRESHOLD = 20000
ALERT_LOG_KEEP_LINES = 5000
#: watch.log（运行器自己的日志，每样本一行）轮转阈值
WATCH_LOG_MAX_BYTES = 2 << 20
#: 状态文件超过这个体积就报 WARN（去重表膨胀会把它撑大，最终拖慢每次 tick）
STATE_SIZE_WARN_BYTES = 4 << 20

#: 样本表的列（CSV 与 JSONL 共用同一批键；JSONL 另带 ``findings`` 明细）。
#: 顺序就是 CSV 的列序——**只许在末尾追加**，否则已导出的历史表会错列。
SAMPLE_CSV_COLUMNS: tuple[str, ...] = (
    "sample_index", "ts", "iso", "uptime_seconds", "gap_seconds", "run_starts",
    "data_dir", "data_dir_source",
    "state_present", "state_size", "state_age_seconds", "state_unchanged_samples",
    "state_sha16", "last_tick_at", "tick_age_seconds", "tick_seconds",
    "activity", "activity_label", "scene", "minutes_in_activity",
    "emotion", "energy", "energy_cap", "afterglow", "stress", "loneliness",
    "social_battery", "last_addressed_at", "sleep_minutes_today", "sleep_debt_nights",
    "continuous_awake_minutes", "sanitized_key_count",
    "llm_fail_streak", "llm_cooldown_seconds_left", "llm_success_age_seconds",
    "applied_sessions", "foreign_sessions", "observed_sessions",
    "unbacked_sessions", "unbacked_worst_seconds", "table_bloated",
    "mem_applied", "mem_foreign",
    "store_present", "store_size", "store_table_count", "store_relationships",
    "store_routine_daily", "store_note",
    "cfg_enabled", "cfg_paused", "cfg_dry_run", "cfg_filter_mode",
    "cfg_config_version", "cfg_sleep_window",
    "log_files", "log_new_lines", "log_new_errors", "log_new_fail_hits", "log_new_warn_hits",
    "findings_fail", "findings_warn", "check_status", "note",
)

def local_stamp(epoch: float, tz_offset_minutes: int) -> str:
    """epoch → 本地「YYYY-MM-DD HH:MM:SS」（带配置里的时区偏移，不用机器本地时区）。"""

    try:
        local = datetime.fromtimestamp(float(epoch) + tz_offset_minutes * 60, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return "—"
    return local.strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write_text(path: pathlib.Path, text: str) -> None:
    """原子替换写入：**监控器随时可能被杀**（重启/断电），半截文件会毒化下次启动。

    失败时**必须删掉 .tmp**：watch 每 5 分钟就要写几个文件（样本/状态/汇总/告警），
    盘满或文件被占用时残留的 .tmp 会在目录里越堆越多，最后变成没人看得懂的垃圾。
    """

    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _append_text(path: pathlib.Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _trim_lines(path: pathlib.Path, *, keep: int) -> bool:
    """把文件裁到保留最后 ``keep`` 行（用于告警流水，避免无限增长）。"""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    if len(lines) <= keep:
        return False
    body = "\n".join(lines[-keep:]) + "\n"
    _atomic_write_text(path, body)
    return True


class RunLog:
    """监控器自己的日志：每样本一行 + 每件大事一行，超阈值轮转一次。

    ⚠ 它**不替代**告警流水：告警流水是给人看的「发生了什么」，这里是「运行器干了什么」
    （启动、接管过期锁、采了多少样本、生成报告、裁剪、退出原因）。
    """

    def __init__(self, path: pathlib.Path, *, echo: bool = True) -> None:
        self.path = path
        self.echo = echo

    def write(self, message: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}"
        try:
            if self.path.exists() and self.path.stat().st_size > WATCH_LOG_MAX_BYTES:
                backup = self.path.with_name(self.path.name + ".1")
                try:
                    if backup.exists():
                        backup.unlink()
                    os.replace(self.path, backup)
                except OSError:
                    pass
            _append_text(self.path, line + "\n")
        except OSError:
            pass
        if self.echo:
            try:
                print(line)
            except Exception:  # noqa: BLE001 —— 控制台编码/重定向问题不该影响监控
                pass


def clean_finding_title(title: str) -> str:
    """去掉「真机状态：」这类标签前缀（告警表里每条都带前缀，读者会看不见重点）。"""

    text = str(title or "").strip()
    if "：" in text:
        head, tail = text.split("：", 1)
        if len(head) <= 8 and tail.strip():
            return tail.strip()
    return text


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def canonical_finding_key(scope: str, title: str) -> str:
    """把带数字/附带说明的标题归一成**稳定 id**，告警才能「只在新变化时落一条」。

    ⚠ 必须归一，两处都会让 id 漂移：
      * 标题里有动态数字（「模型连续失败 5 次」「连续清醒 40.0 小时」「3 个坏值被净化」）；
      * 同一问题在状态变化时会多带一段括号说明——最典型的是模型侧：
        冷却中会写成「模型连续失败 8 次（冷却还剩 1.0 分钟）」，冷却过去后括号消失。
        不削掉括号就会「开始一条新告警 + 恢复一条旧告警」，一周下来 alerts.log 里
        全是同一个问题的不同副本——正是最典型的告警疲劳。

    显示用的标题仍取当轮原文（见 ``AlertTracker.update``），这里只影响 id。
    """

    text = clean_finding_title(title)
    text = re.sub(r"[（(][^（）()]*[）)]", "", text)
    text = _NUMBER_RE.sub("#", text)
    text = re.sub(r"\s+", " ", text).strip(" 　。；;,")
    return f"{scope or '综合'}|{text}"


ALERT_LEVEL_ORDER = {"OK": 0, "WARN": 1, "FAIL": 2}


@dataclass
class Alert:
    """一条（可能仍在持续的）告警。``first_seen``/``hits`` 用来判断「一直坏着」还是「刚坏」。"""

    id: str
    level: str
    title: str
    scope: str = ""
    evidence: str = ""
    fix: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    hits: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "level": self.level, "title": self.title, "scope": self.scope,
                "evidence": self.evidence, "fix": self.fix, "first_seen": self.first_seen,
                "last_seen": self.last_seen, "hits": self.hits}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Alert":
        return cls(
            id=str(payload.get("id") or ""),
            level=str(payload.get("level") or "WARN").upper(),
            title=str(payload.get("title") or ""),
            scope=str(payload.get("scope") or ""),
            evidence=redact(payload.get("evidence"), limit=300),
            fix=redact(payload.get("fix"), limit=300),
            first_seen=as_float(payload.get("first_seen"), 0.0) or 0.0,
            last_seen=as_float(payload.get("last_seen"), 0.0) or 0.0,
            hits=as_int(payload.get("hits"), 0),
        )


class AlertTracker:
    """告警状态机：只在**等级变化**时产生事件（开始 / 升级 / 降级 / 恢复）。

    状态存在 ``watch_state.json`` 里，所以「重启监控器」不会把长期问题重新报一遍
    （否则每天重挂一次就多一轮重复告警，正是要避免的噪音）。
    """

    def __init__(self, active: Mapping[str, Any] | None = None) -> None:
        self.active: dict[str, Alert] = {}
        for key, value in (active or {}).items():
            if isinstance(value, Mapping) and value.get("level"):
                alert = Alert.from_dict(value)
                alert.id = alert.id or str(key)
                self.active[str(key)] = alert

    def update(self, findings: Sequence[Finding], *, now: float) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        for finding in findings:
            level = finding.level.upper()
            if level not in ("FAIL", "WARN"):
                continue
            key = canonical_finding_key(finding.scope, finding.title)
            seen.add(key)
            title = clean_finding_title(finding.title)
            evidence = redact(finding.evidence, limit=300)
            fix = redact(finding.fix, limit=300)
            current = self.active.get(key)
            if current is None:
                self.active[key] = Alert(key, level, title, finding.scope, evidence, fix,
                                         first_seen=now, last_seen=now, hits=1)
                events.append(self._event(now, "开始", key, level, "", title, evidence, fix))
                continue
            previous = current.level
            current.last_seen = now
            current.hits += 1
            current.title = title
            current.evidence = evidence
            current.fix = fix
            if ALERT_LEVEL_ORDER.get(level, 0) > ALERT_LEVEL_ORDER.get(previous, 0):
                current.level = level
                events.append(self._event(now, "升级", key, level, previous, title, evidence, fix))
            elif ALERT_LEVEL_ORDER.get(level, 0) < ALERT_LEVEL_ORDER.get(previous, 0):
                current.level = level
                events.append(self._event(now, "降级", key, level, previous, title, evidence, fix))
        for key in list(self.active):
            if key in seen:
                continue
            gone = self.active.pop(key)
            events.append(self._event(now, "恢复", key, "OK", gone.level, gone.title,
                                      "本轮检查未再命中", ""))
        return events

    @staticmethod
    def _event(now: float, kind: str, key: str, level: str, previous: str,
               title: str, evidence: str, fix: str) -> dict[str, Any]:
        return {"ts": now, "kind": kind, "id": key, "level": level, "prev_level": previous,
                "title": title, "evidence": evidence, "fix": fix}

    def active_alerts(self) -> list[Alert]:
        order = {"FAIL": 0, "WARN": 1}
        return sorted(self.active.values(),
                      key=lambda item: (order.get(item.level, 2), item.first_seen))

    def to_state(self) -> dict[str, Any]:
        return {key: alert.to_dict() for key, alert in self.active.items()}


class StopFlag:
    """停止标志。信号处理器与主循环共享它（处理器里只允许做赋值这类异步信号安全的事）。"""

    def __init__(self) -> None:
        self.stopped = False
        self.reason = ""

    def request(self, reason: str) -> None:
        self.stopped = True
        self.reason = self.reason or reason


def install_signal_handlers(flag: StopFlag) -> list[tuple[int, Any]]:
    """Ctrl+C / 关窗 / SIGTERM / SIGBREAK 都走**优雅停止**：写完汇总、放掉锁再退。

    ⚠ 不装处理器的话，Ctrl+C 会在随机时刻打断写盘，留下一份半截的汇总或锁文件；
    下次启动还要处理过期锁——「优雅停止」在这里是可用性的一部分。
    返回被替换掉的旧处理器，退出时恢复（本程序被别的程序 import 时不该改它的信号行为）。
    """

    def handler(signum, _frame):  # type: ignore[no-untyped-def]
        flag.request(f"收到信号 {signum}")

    saved: list[tuple[int, Any]] = []
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            saved.append((int(sig), signal.signal(sig, handler)))
        except (ValueError, OSError, RuntimeError):  # 非主线程 / 平台不支持：静默跳过
            continue
    return saved


def restore_signal_handlers(saved: Sequence[tuple[int, Any]]) -> None:
    for sig, previous in saved:
        try:
            signal.signal(sig, previous)
        except (ValueError, OSError, RuntimeError, TypeError):
            continue


def _pid_alive(pid: int) -> bool:
    """进程是否还活着（**只申请只读查询权限**，绝不带终止权限）。

    ⚠ Windows 上不能图省事用 ``os.kill(pid, 0)`` 探活：CPython 在 Windows 把任何非
    CTRL_* 的信号直接翻译成 ``TerminateProcess``——``os.kill(pid, 0)`` 会**真的杀掉**
    那个进程（退出码 0）。用它探活等于「问一句对面还在不在，顺手把它毙了」。
    所以 Windows 走 ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)``：能拿到句柄 = 进程在，
    拿不到 = 已死（或权限不足，那就当它死了，让 ``--force-lock`` 做人工兜底）。
    """

    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes  # noqa: PLC0415 —— 只在 Windows 上需要

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001 —— 判定不了就当「不存活」，由心跳兜底
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class WatchLock:
    """单实例锁：**心跳 + 进程存活双判据**（两个监控器同时写一批文件会毁掉整周的样本）。

    判据：锁文件里的 ``heartbeat_at``（每次采样刷新）距现在不超过 ``max(3×interval, 600s)``，
    **或者** 锁里记的 pid 仍然存活（Windows 走只读的 ``OpenProcess``，见 ``_pid_alive``），
    就认为「另一个监控器还活着」，拒绝启动；否则视为过期锁（进程被强杀/机器断电留下），
    接管并如实记一条日志——这样「重启机器后重挂监控」不需要手动删锁。

    ⚠ 只靠心跳年龄会在两个场景下误判：报告生成耗时导致心跳晚于阈值（已在 ``run_watch`` 里
    提前打点解决）、系统时间被前后跳（NTP 校正 / 改表）。pid 存活判据不吃时钟，
    是这两条的最省事兜底；pid 复用的极小概率由 ``--force-lock`` 人工兜底。
    """

    def __init__(self, path: pathlib.Path, *, interval: float, force: bool = False,
                 disabled: bool = False) -> None:
        self.path = path
        self.interval = interval
        self.force = force
        self.disabled = disabled
        self.acquired = False
        self.note = ""

    def _payload(self) -> str:
        return json.dumps({
            "schema": WATCH_SCHEMA_VERSION, "pid": os.getpid(),
            "python": sys.executable, "started_at": time.time(),
            "started_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "heartbeat_at": time.time(),
        }, ensure_ascii=False, indent=2)

    def acquire(self) -> tuple[bool, str]:
        if self.disabled:
            self.acquired = True
            self.note = "已按 --no-lock 跳过单实例锁"
            return True, self.note
        stale_note = ""
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            heartbeat = as_float(payload.get("heartbeat_at"), 0.0) or 0.0
            age = time.time() - heartbeat if heartbeat > 0 else float("inf")
            limit = max(self.interval * 3, 600.0)
            pid = as_int(payload.get("pid"), 0)
            alive = _pid_alive(pid)
            if not self.force and (age <= limit or alive):
                why = ("进程仍存活" if alive else f"心跳距今 {human_span(age)}")
                return False, (f"已有监控进程在跑（pid={pid}，{why}，"
                               f"心跳阈值 {human_span(limit)}）")
            stale_note = (f"接管过期锁：原 pid={pid}（{'仍存活' if alive else '已不存活'}），"
                          f"心跳距今 {human_span(age) if heartbeat else '未知'}")
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # 走的是「过期锁」路径：原子覆盖
            _atomic_write_text(self.path, self._payload())
        except OSError as exc:
            return False, f"锁文件写不进去：{exc.__class__.__name__}: {exc}"
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self._payload())
        self.acquired = True
        self.note = stale_note
        return True, stale_note

    def heartbeat(self) -> bool:
        """续命（每次采样前后都要打点）。返回 False = 心跳写不进去，调用方应记一条日志。"""

        if not self.acquired or self.disabled:
            return True
        try:
            _atomic_write_text(self.path, self._payload())
        except OSError:
            return False
        return True

    def release(self) -> None:
        if not self.acquired or self.disabled:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if int(payload.get("pid", -1)) == os.getpid():
                self.path.unlink()
        except (OSError, ValueError, json.JSONDecodeError):
            try:
                self.path.unlink()
            except OSError:
                pass
        self.acquired = False


class LogTailer:
    """增量扫日志尾部：只读**新增**字节（一周也不会把日志读爆，也不会重复计数）。

    三条必须处理的现实：
      * 轮转/截断：文件比记的 offset 还小 ⇒ 从 0 重读（否则会永远读不到新内容）；
      * 首次见面：只读末尾 ``LOG_TAIL_FIRST_BYTES``（不重演历史）；
      * **只处理完整行**：末尾没有换行的那段留到下次。写入方随时可能只写到一半，
        故障签名正好被截断 ⇒ 漏报；更糟的是把半行当完整行统计 ⇒ 假报。
        （判据是「读到的不以换行结尾」而不是「没读到文件末尾」——后者在
        「文件本来就没有末尾换行」时会把半行当完整行。）
    """

    def __init__(self, offsets: Mapping[str, Any] | None = None) -> None:
        self.offsets: dict[str, dict[str, int]] = {}
        for path, value in (offsets or {}).items():
            if isinstance(value, Mapping):
                self.offsets[str(path)] = {"offset": as_int(value.get("offset")),
                                           "size": as_int(value.get("size"))}

    def to_state(self) -> dict[str, Any]:
        return {path: dict(value) for path, value in self.offsets.items()}

    def poll(self, paths: Sequence[pathlib.Path]) -> dict[str, Any]:
        result: dict[str, Any] = {"files": 0, "lines": 0, "errors": 0, "rotated": 0,
                                  "oversized": 0, "fail_hits": {}, "warn_hits": {},
                                  "recent_errors": [], "signature_excerpts": []}
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            info = self.offsets.setdefault(str(path), {"offset": 0, "size": 0})
            offset = info.get("offset", 0)
            if size < offset:
                offset = 0
                result["rotated"] += 1
            if offset == 0 and size > LOG_TAIL_FIRST_BYTES:
                offset = size - LOG_TAIL_FIRST_BYTES
            if size <= offset:
                info["size"] = size
                continue
            want = min(size - offset, LOG_TAIL_MAX_BYTES_PER_POLL)
            try:
                with path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read(want)
            except OSError:
                continue
            consumed = offset + len(chunk)
            if not chunk.endswith(b"\n"):
                cut = chunk.rfind(b"\n")
                if cut < 0:
                    if size - offset >= LOG_TAIL_MAX_BYTES_PER_POLL:
                        # 单行比单轮读取上限还长（巨大 JSON/参数 dump）：**必须前进**，
                        # 否则每个采样周期都重读同一段，不但白费力气，还永远读不到后面的新内容。
                        # 代价是这一行的开头被跳过——所以把它记进 oversized，让 WARN 说出来。
                        info["offset"] = consumed
                        info["size"] = size
                        result["oversized"] = as_int(result.get("oversized")) + 1
                    continue  # 行还没写完（写入方只写了一半）：下一轮再读
                chunk = chunk[: cut + 1]
                consumed = offset + cut + 1
            text = chunk.decode("utf-8", errors="replace")
            lines = text.splitlines()
            result["files"] += 1
            result["lines"] += len(lines)
            for line in lines:
                level_match = LOG_LEVEL_RE.search(line)
                if level_match and level_match.group(1).upper() in ("ERROR", "CRITICAL"):
                    result["errors"] += 1
                    if len(result["recent_errors"]) < 5:
                        result["recent_errors"].append(redact(line, limit=200))
                fail_hit, warn_hit = match_signatures(line)
                if fail_hit:
                    result["fail_hits"][fail_hit] = result["fail_hits"].get(fail_hit, 0) + 1
                if warn_hit:
                    result["warn_hits"][warn_hit] = result["warn_hits"].get(warn_hit, 0) + 1
            # v1.2.0（D10）：命中**原文**随增量一起收走——告警只计数的话，
            # 归因永远要人肉上真机翻日志（18h 真机窗口就是这么废掉的）。
            for block in _signature_excerpts(lines, source_name=path.name):
                if len(result["signature_excerpts"]) >= 12:
                    break
                result["signature_excerpts"].append(block)
            info["offset"] = consumed
            info["size"] = size
        return result


@dataclass
class WatchSources:
    """监控目标：数据目录 / 配置 / 日志。数据目录未找到时会**周期重试**（部署是逐步的）。"""

    data_dir: pathlib.Path | None = None
    data_dir_source: str = "未找到"
    config_path: pathlib.Path | None = None
    log_paths: list[pathlib.Path] = field(default_factory=list)


def _auto_data_dir(plugin_dir: pathlib.Path, search_roots: Sequence[pathlib.Path]
                   ) -> tuple[pathlib.Path | None, str]:
    """自动定位数据目录（复用一次性报告的归属校验：签名匹配才算本插件的数据）。"""

    discovered = discover_data_dirs(list(search_roots) + [plugin_dir])
    best: pathlib.Path | None = None
    best_score = -1
    best_notes: list[str] = []
    for candidate in discovered:
        score, notes = score_data_dir(candidate)
        if score > best_score:
            best, best_score, best_notes = candidate, score, notes
    if best is not None and best_score >= 8:
        return best.resolve(), f"自动发现（{('；'.join(best_notes))[:80]}，得分 {best_score}）"
    if best is not None and best_score >= 4:
        return best.resolve(), f"自动发现（{('；'.join(best_notes))[:80]}，得分 {best_score}）"
    return None, "未找到（自动搜索没有命中 life_state.json / life_store.db 签名）"


def resolve_watch_sources(*, plugin_dir: pathlib.Path, search_roots: Sequence[pathlib.Path],
                          data_dir: pathlib.Path | None, config_path: pathlib.Path | None,
                          log_paths: Sequence[pathlib.Path]) -> WatchSources:
    sources = WatchSources()
    if data_dir is not None:
        sources.data_dir = data_dir.resolve()
        sources.data_dir_source = "由 --data-dir 指定"
    else:
        sources.data_dir, sources.data_dir_source = _auto_data_dir(plugin_dir, search_roots)
    if config_path is not None:
        sources.config_path = config_path.resolve()
    else:
        candidates: list[pathlib.Path] = []
        if sources.data_dir is not None:
            candidates.append(sources.data_dir / CONFIG_FILE)
        candidates.append(plugin_dir / CONFIG_FILE)
        candidates.extend(root / CONFIG_FILE for root in search_roots)
        for candidate in candidates:
            if not candidate.exists():
                continue
            payload, _error = _load_toml(candidate)
            if looks_like_config(payload):
                sources.config_path = candidate.resolve()
                break
    if log_paths:
        sources.log_paths = [path.resolve() for path in log_paths]
    else:
        roots = list(search_roots) + [plugin_dir]
        if sources.data_dir is not None:
            roots.insert(0, sources.data_dir)
        sources.log_paths = find_log_candidates(roots)
    return sources


def _read_store_counts_ro(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    """直连只读读表计数（**不复制**——见本节开头的取舍说明）。"""

    detail: dict[str, Any] = {"present": False, "size": 0, "tables": "", "relationships": "",
                              "routine_daily": ""}
    if not path.exists():
        return detail, "文件不存在（关系档案与习惯会降级成内存态）"
    try:
        detail["size"] = path.stat().st_size
    except OSError:
        pass
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error as exc:
        return detail, f"只读打开失败：{exc.__class__.__name__}"
    note = ""
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        detail["present"] = True
        detail["tables"] = len(tables)
        for table in ("relationships", "routine_daily"):
            if table not in tables:
                continue
            try:
                detail[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            except sqlite3.Error as exc:
                note = f"{table} 计数失败：{exc}"
    except sqlite3.Error as exc:
        note = f"库读不出来（{exc}）；每日完整报告会走复制路径再试"
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return detail, note


def _findings_for_log(result: Mapping[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    fail_hits = result.get("fail_hits") or {}
    warn_hits = result.get("warn_hits") or {}
    if fail_hits:
        findings.append(Finding(
            "FAIL", "日志里有确定性故障签名（新增）",
            evidence="；".join(f"{key}×{value}" for key, value in list(fail_hits.items())[:6]),
            fix="按签名逐条定位；命中原文已落盘 watch/log_excerpts.jsonl，"
                "完整报告（daily/）第 7 节也有",
            scope="日志"))
    if warn_hits:
        findings.append(Finding(
            "WARN", "日志里有可疑签名（新增）",
            evidence="；".join(f"{key}×{value}" for key, value in list(warn_hits.items())[:6]),
            fix="可能是正常降级，也可能在持续失败；命中原文见 watch/log_excerpts.jsonl "
                "与 daily/ 报告",
            scope="日志"))
    if result.get("oversized"):
        findings.append(Finding(
            "WARN", "日志里有超长行（超过单轮读取上限，已跳过该段开头）",
            evidence=f"跳过 {result['oversized']} 段（单行 > "
                     f"{human_size(LOG_TAIL_MAX_BYTES_PER_POLL)} 且中间没有换行可切）",
            fix="确认日志里是不是被塞进了巨大的 JSON/参数 dump；"
                "被跳过的那一段不计入签名统计",
            scope="日志"))
    return findings


@dataclass
class WatchPaths:
    """监控目录里的全部文件（都在 ``<插件目录>/diagnostics/watch/`` 下，均不进 git）。"""

    root: pathlib.Path
    lock: pathlib.Path
    state: pathlib.Path
    log: pathlib.Path
    samples_jsonl: pathlib.Path
    samples_csv: pathlib.Path
    alerts_log: pathlib.Path
    alerts_md: pathlib.Path
    summary: pathlib.Path
    daily: pathlib.Path
    stop: pathlib.Path
    log_excerpts: pathlib.Path
    marks: pathlib.Path

    @classmethod
    def of(cls, root: pathlib.Path) -> "WatchPaths":
        return cls(
            root=root, lock=root / "watch.lock", state=root / "watch_state.json",
            log=root / "watch.log", samples_jsonl=root / "samples.jsonl",
            samples_csv=root / "samples.csv", alerts_log=root / "alerts.log",
            alerts_md=root / "alerts.md", summary=root / "watch_summary.md",
            daily=root / "daily", stop=root / "watch.stop",
            log_excerpts=root / "log_excerpts.jsonl",
            marks=root / "watch_marks.jsonl",
        )

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.daily.mkdir(parents=True, exist_ok=True)


@dataclass
class WatchContext:
    """一次监控会话的固定上下文（采样函数只依赖它，便于单测直接构造）。"""

    plugin_dir: pathlib.Path
    search_roots: list[pathlib.Path]
    sources: WatchSources
    tailer: LogTailer
    interval: float


def load_watch_state(paths: WatchPaths) -> dict[str, Any]:
    payload, _error = read_json(paths.state)
    return payload if isinstance(payload, dict) else {}


def save_watch_state(paths: WatchPaths, state: Mapping[str, Any]) -> None:
    try:
        _atomic_write_text(paths.state,
                           json.dumps(state, ensure_ascii=False, indent=2, default=str))
    except OSError:
        pass


# ---------------------------------------------------------------- 改动分段标记（v1.19.0 PR-AF-1）


def append_mark(paths: WatchPaths, label: str, *, now: float, tz: int) -> bool:
    """v1.19.0（PR-AF-1 配套）：给长期监控打一个「改动时点」标记。

    为什么用**独立追加文件**而不是写进 ``watch_state.json``：标记是在部署重启那一刻
    由人补的，而 watch 主循环每一轮都在整份重写 ``watch_state.json``——两个进程同时写
    同一份 JSON 会把整周数据写乱。``watch_marks.jsonl`` 走 O_APPEND 追加，天然无锁。

    用法（部署并完整重启之后）：

        python collect_diagnostics.py --mark "改清醒侧判据" --watch-dir <监控目录>
    """

    text = str(label or "").strip()
    if not text:
        return False
    try:
        paths.root.mkdir(parents=True, exist_ok=True)
        with paths.marks.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": now, "iso": human_stamp(now, tz), "label": text},
                                    ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def load_marks(paths: WatchPaths) -> list[dict[str, Any]]:
    """读回所有标记（按时间排序；坏行跳过）。文件不存在 = 没有任何分段。"""

    if not paths.marks.exists():
        return []
    marks: list[dict[str, Any]] = []
    try:
        with paths.marks.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict) and as_float(payload.get("at"), 0.0) > 0:
                    marks.append(payload)
    except OSError:
        return []
    marks.sort(key=lambda item: as_float(item.get("at"), 0.0) or 0.0)
    return marks


#: 签名原文环形文件的上限：超过就把旧文件让位（原文是现场证据，但没必要无限留）
LOG_EXCERPTS_MAX_BYTES = 1 << 20


def append_log_excerpts(paths: WatchPaths, excerpts: Sequence[str], *,
                        now: float, tz: int) -> None:
    """v1.2.0（D10）：把本轮采到的签名命中原文追加进 ``log_excerpts.jsonl``（环形）。"""

    if not excerpts:
        return
    try:
        if (paths.log_excerpts.exists()
                and paths.log_excerpts.stat().st_size > LOG_EXCERPTS_MAX_BYTES):
            old = paths.log_excerpts.with_name("log_excerpts.old.jsonl")
            try:
                old.unlink()
            except FileNotFoundError:
                pass
            paths.log_excerpts.rename(old)
        payload = {"ts": round(now, 3), "iso": local_stamp(now, tz),
                   "excerpts": list(excerpts)}
        _append_text(paths.log_excerpts, json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_recent_log_excerpts(paths: WatchPaths, *, limit: int = 6) -> list[dict[str, Any]]:
    """读回最近的若干条签名原文（汇总报告渲染用；文件坏了当没有，不拦报告）。"""

    try:
        text = paths.log_excerpts.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("excerpts"):
            rows.append(payload)
    return rows[-limit:]


def append_sample(paths: WatchPaths, row: Mapping[str, Any]) -> None:
    """一行样本同时落 JSONL（保真、带 findings 明细）与 CSV（能直接导入 Excel）。"""

    try:
        _append_text(paths.samples_jsonl,
                     json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass
    try:
        fresh = not paths.samples_csv.exists() or paths.samples_csv.stat().st_size == 0
        with paths.samples_csv.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(SAMPLE_CSV_COLUMNS),
                                    extrasaction="ignore")
            if fresh:
                writer.writeheader()
            writer.writerow(row)
    except OSError:
        pass


def load_samples(paths: WatchPaths) -> tuple[list[dict[str, Any]], int]:
    """读回全部样本（外加坏行计数——坏行只能来自被杀在半截的写入，必须如实统计）。"""

    rows: list[dict[str, Any]] = []
    broken = 0
    try:
        text = paths.samples_jsonl.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows, 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            broken += 1
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows, broken


def take_sample(ctx: WatchContext, *, now: float, sample_index: int, uptime: float,
                gap: float, run_starts: int, prev_sha: str, prev_unchanged: int
                ) -> tuple[dict[str, Any], list[Finding]]:
    """采一个样本 →（一行时序数据, 一组结论）。

    ⚠ 判定**不另写阈值**：状态侧直接调 ``_state_checks``、配置侧直接调 ``collect_config``
    ——都是一次性报告用的同一个函数。两套判据迟早漂移，然后就会出现
    「诊断报告说 FAIL、监控说 OK」这种自相矛盾的现场。
    """

    sources = ctx.sources
    row: dict[str, Any] = {
        "sample_index": sample_index,
        "ts": round(now, 3),
        "uptime_seconds": round(uptime, 1),
        "gap_seconds": round(gap, 1) if gap >= 0 else "",
        "run_starts": run_starts,
        "data_dir": str(sources.data_dir) if sources.data_dir else "",
        "data_dir_source": sources.data_dir_source,
        "state_present": False,
    }
    check = Report()
    config_payload: dict[str, Any] = {}
    if sources.config_path is not None and sources.config_path.exists():
        config_payload, config_error = _load_toml(sources.config_path)
        if config_error:
            check.add("WARN", "监控：config.toml 读不出来", evidence=config_error,
                      fix="宿主读不出配置会退回默认值：先修 TOML 语法", scope="配置")
    else:
        check.add("WARN", "监控：未找到 config.toml",
                  evidence="判定退回内置默认值（tick 600s / 困意阈 0.62 / 时段表 …），"
                           "阈值可能与真机不一致",
                  fix="用 --config 指定宿主落盘的配置", scope="配置")
    tz = as_int(_section(config_payload, "simulation").get("tz_offset_minutes"), 480)
    row["iso"] = local_stamp(now, tz)
    simulation = _section(config_payload, "simulation")
    tick_seconds = max(60, as_int(simulation.get("tick_seconds"), 600))
    frequency = _section(config_payload, "frequency")
    apply_section = _section(config_payload, "apply")
    plugin_section = _section(config_payload, "plugin")
    row.update({
        "tick_seconds": tick_seconds,
        "cfg_enabled": bool(plugin_section.get("enabled", True)),
        "cfg_paused": bool(frequency.get("paused", False)),
        "cfg_dry_run": bool(simulation.get("dry_run", False)),
        "cfg_filter_mode": str(apply_section.get("filter_mode") or ""),
        "cfg_config_version": str(plugin_section.get("config_version") or ""),
        "cfg_sleep_window": str(simulation.get("sleep_window") or ""),
    })
    # 配置侧复用一次性报告的判定（paused / dry_run / filter_mode / sleep_window / 曲线坏行 …）
    if config_payload and sources.config_path is not None:
        try:
            collect_config(check, sources.config_path, PURE, ctx.plugin_dir)
        except Exception as exc:  # noqa: BLE001 —— 配置判定失败不许拖垮采样
            check.add("WARN", "监控：配置侧判定抛异常",
                      evidence=f"{exc.__class__.__name__}: {exc}",
                      fix="修配置判定；或先跑一次 collect_diagnostics.py 看完整报告",
                      scope="配置")

    # ---- 状态侧 ----
    payload: dict[str, Any] | None = None
    detail: dict[str, Any] = {}
    state_path = (sources.data_dir / STATE_FILES[0]) if sources.data_dir else None
    if state_path is not None and state_path.exists():
        row["state_present"] = True
        loaded, error = read_json(state_path)
        if isinstance(loaded, dict):
            payload = loaded
            detail = _state_checks(check, payload, tz=tz, now=now, config=config_payload,
                                   label="真机状态", simulated=False)
        else:
            check.add("FAIL", "真机状态：状态文件不可用", evidence=error or "顶层不是 JSON 对象",
                      fix="备份后删除让它重建（会丢生活状态，但对账记忆另存文件）",
                      scope="状态")
        try:
            stat = state_path.stat()
            row["state_size"] = stat.st_size
            row["state_age_seconds"] = round(max(0.0, now - stat.st_mtime), 1)
            if stat.st_size > STATE_SIZE_WARN_BYTES:
                check.add("WARN", "监控：状态文件体积异常",
                          evidence=f"{human_size(stat.st_size)}（阈值 "
                                   f"{human_size(STATE_SIZE_WARN_BYTES)}）",
                          fix="去重表/历史账本膨胀会拖慢每次 tick：看表规模那一列，"
                              "确认 TTL 清理路径在跑", scope="状态")
        except OSError:
            pass
        row["state_sha16"] = sha256_of(state_path)[:16]
    elif sources.data_dir is None:
        check.add("WARN", "监控：还没有找到本插件的运行时数据目录",
                  evidence=sources.data_dir_source,
                  fix="用 --data-dir 指向数据目录（或 --search-root 指向 MaiBot 根）；"
                      "插件尚未部署时这条会一直是当前告警", scope="监控")
    else:
        check.add("WARN", "监控：数据目录里没有 life_state.json",
                  evidence=f"数据目录 {sources.data_dir}",
                  fix="确认插件已被宿主加载并跑过至少一个 tick；若是刚部署，"
                      "看日志里有没有加载失败", scope="监控")

    sha = str(row.get("state_sha16") or "")
    unchanged = (prev_unchanged + 1) if (sha and sha == prev_sha) else (1 if sha else 0)
    row["state_unchanged_samples"] = unchanged

    def put(target: str, value: Any) -> None:
        row[target] = "" if value is None else value

    put("last_tick_at", (payload or {}).get("last_tick_at"))
    put("tick_age_seconds", detail.get("last_tick_age_seconds"))
    put("activity", detail.get("activity"))
    put("activity_label", detail.get("activity_label"))
    put("scene", detail.get("scene"))
    put("minutes_in_activity", detail.get("minutes_in_activity"))
    for key in ("emotion", "energy", "energy_cap", "afterglow", "stress", "loneliness",
                "social_battery", "last_addressed_at", "sleep_minutes_today",
                "sleep_debt_nights",
                "continuous_awake_minutes", "llm_fail_streak", "llm_cooldown_seconds_left",
                "applied_sessions", "foreign_sessions", "observed_sessions",
                "unbacked_sessions"):
        put(key, detail.get(key))
    last_success = as_float(detail.get("llm_last_success"), 0.0) or 0.0
    put("llm_success_age_seconds", round(now - last_success, 1) if last_success > 0 else "")
    put("unbacked_worst_seconds", round(as_float(detail.get("unbacked_worst_seconds"), 0.0) or 0.0, 1)
        if detail.get("unbacked_worst_seconds") is not None else "")
    put("sanitized_key_count", len(detail.get("sanitized_keys") or []))
    tables = detail.get("table_sizes") or {}
    put("table_bloated", sum(1 for size in tables.values() if as_int(size) > 5000))
    # 「锚点在推进、文件却很久没落盘」是另一种故障：状态说自己活着，磁盘上没有证据
    state_age = as_float(row.get("state_age_seconds"))
    tick_age = as_float(row.get("tick_age_seconds"))
    if (state_age is not None and tick_age is not None
            and state_age > max(tick_seconds * 3, 3600) and tick_age <= tick_seconds * 1.5):
        check.add("WARN", "监控：状态锚点在推进但状态文件长时间没有落盘",
                  evidence=f"文件 {human_span(state_age)} 未更新，而 last_tick_at 只差 "
                           f"{human_span(tick_age)}",
                  fix="确认磁盘/权限；或宿主在写状态时被别的进程锁住", scope="状态")

    # ---- 对账记忆 ----
    memory_path = (sources.data_dir / STATE_FILES[1]) if sources.data_dir else None
    if memory_path is not None and memory_path.exists():
        memory, _error = read_json(memory_path)
        if isinstance(memory, dict):
            put("mem_applied", len(memory.get("applied") or {}))
            put("mem_foreign", len(memory.get("foreign") or {}))

    # ---- 库侧（只读直连，不复制）----
    store_path = (sources.data_dir / STORE_FILE) if sources.data_dir else None
    if store_path is not None:
        store, store_note = _read_store_counts_ro(store_path)
        row["store_present"] = bool(store.get("present"))
        row["store_size"] = store.get("size") or ""
        row["store_table_count"] = store.get("tables") if store.get("tables") != "" else ""
        row["store_relationships"] = store.get("relationships")
        row["store_routine_daily"] = store.get("routine_daily")
        row["store_note"] = store_note
        if not store_path.exists():
            check.add("WARN", "监控：life_store.db 还没生成",
                      evidence="关系档案与习惯日态会降级成内存态",
                      fix="插件写过关系/习惯后会自动建库；一直不出现要看日志里有没有写库异常",
                      scope="库")
        elif not store.get("present"):
            # 与 collect_store 同判据：库存在但读不出来 = FAIL（一次性报告也是这么判的）
            check.add("FAIL", "监控：life_store.db 读不出来", evidence=store_note,
                      fix="库坏了插件会降级成内存态（习惯会每天重复命中）；备份后删除重建",
                      scope="库")
        elif store_note:
            # 库能打开、但某张签名表计数失败：不许当成「库正常」——那会把一条真问题
            # 变成样本里的空值（check_status 还是 ok），属于最坏的静默失败
            check.add("WARN", "监控：库表计数失败", evidence=store_note,
                      fix="看 daily/ 里的完整报告（它允许复制库再读，能看到更具体的原因）",
                      scope="库")

    # ---- 日志侧（增量尾部）----
    log_result = ctx.tailer.poll(sources.log_paths) if sources.log_paths else {}
    row["log_files"] = log_result.get("files", 0)
    row["log_new_lines"] = log_result.get("lines", 0)
    row["log_new_errors"] = log_result.get("errors", 0)
    row["log_new_fail_hits"] = sum((log_result.get("fail_hits") or {}).values())
    row["log_new_warn_hits"] = sum((log_result.get("warn_hits") or {}).values())
    # v1.2.0（D10）：签名原文不进样本行（会把 JSONL/CSV 撑爆），单独走环形文件。
    # 挂在 row 上带出去，由主循环 pop 出来落盘。
    row["log_excerpts"] = list(log_result.get("signature_excerpts") or [])
    if sources.log_paths:
        missing_logs = [path for path in sources.log_paths if not path.exists()]
        if len(missing_logs) == len(sources.log_paths):
            # 路径写错时**必须报**：否则监控照常跑、日志侧永远 0 命中，看起来「没故障」
            check.add("WARN", "监控：日志文件不存在（本次无法从日志取证）",
                      evidence="；".join(str(path) for path in missing_logs[:3]),
                      fix="核对 --log 路径；日志轮转后旧文件名会失效，"
                          "不传 --log 让程序自动找也行", scope="日志")
        check.findings.extend(_findings_for_log(log_result))
    else:
        check.add("WARN", "监控：没找到宿主日志", evidence="日志是判断「哪一步失败」最直接的证据",
                  fix="用 --log 指定 MaiBot 日志（或 --search-root 指向 MaiBot 根目录）",
                  scope="日志")

    findings = [finding for finding in check.findings if finding.level in ("FAIL", "WARN")]
    row["findings_fail"] = sum(1 for finding in findings if finding.level == "FAIL")
    row["findings_warn"] = sum(1 for finding in findings if finding.level == "WARN")
    if row["findings_fail"]:
        row["check_status"] = "fail"
    elif row["findings_warn"]:
        row["check_status"] = "warn"
    elif not row["state_present"]:
        row["check_status"] = "unknown"
    else:
        row["check_status"] = "ok"
    row["note"] = (clean_finding_title(findings[0].title) if findings else "")
    # findings 存成结构化三元组：id 本身含分隔符，用字符串拼接会在汇总时被切错
    row["findings"] = [{"level": finding.level, "id": canonical_finding_key(finding.scope,
                                                                           finding.title),
                        "title": clean_finding_title(finding.title)} for finding in findings]
    for key in SAMPLE_CSV_COLUMNS:
        row.setdefault(key, "")
    return row, findings


#: 新 FAIL 触发完整报告的最小间隔 / 每日上限（现场证据要固定，但也不能刷爆目录）
CAPTURE_MIN_GAP_SECONDS = 600.0
CAPTURE_MAX_PER_DAY = 8


def capture_full_report(paths: WatchPaths, *, plugin_dir: pathlib.Path, sources: WatchSources,
                        search_roots: Sequence[pathlib.Path], now: float, reason: str,
                        log: RunLog | None = None) -> pathlib.Path | None:
    """在监控过程中落一份**完整诊断报告**（复用 ``build_report``，与手动跑完全一致）。"""

    try:
        report = build_report(
            plugin_dir, data_dir=sources.data_dir, config_path=sources.config_path,
            log_paths=[path for path in sources.log_paths if path],
            search_roots=list(search_roots), simulate_hours=0.0, run_gates=False,
            now=now, out_dir=paths.daily,
        )
    except Exception as exc:  # noqa: BLE001 —— 完整报告失败不该中断监控
        if log:
            log.write(f"完整报告生成失败（{reason}）：{exc.__class__.__name__}: {exc}")
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"life-frequency-诊断-{stamp}"
    md_path = paths.daily / f"{base}.md"
    # 同一秒可能触发两次抓取（新 FAIL + --once）：加序号，别让后一份盖掉前一份
    counter = 1
    while md_path.exists() and counter < 100:
        counter += 1
        md_path = paths.daily / f"{base}-{counter}.md"
    json_path = md_path.with_suffix(".json")
    body = dict(report.sections).get("REPORT", "")
    body += f"\n---\n\n> 本报告由 watch 模式自动生成（触发原因：{reason}）。\n"
    try:
        _atomic_write_text(md_path, body)
        _atomic_write_text(json_path, json.dumps({
            "machine": report.machine,
            "counts": report.counts(),
            "findings": [item.to_dict() for item in report.findings],
            "watch_reason": reason,
        }, ensure_ascii=False, indent=2, default=str))
    except OSError as exc:
        if log:
            log.write(f"完整报告写盘失败：{exc.__class__.__name__}: {exc}")
        return None
    if log:
        counts = report.counts()
        log.write(f"完整报告：{md_path.name}（{reason}；FAIL {counts['FAIL']} / "
                  f"WARN {counts['WARN']}）")
    return md_path


def _last_report_age(paths: WatchPaths, *, now: float) -> float | None:
    """最近一份完整报告距今多少秒（None = 一份都没有）。

    用它给「周期报告」续期：崩溃-重启循环里每次启动都补一份报告的话，
    一天下来 `daily/` 会被报告塞满（报告本身有 100+ KB）。
    """

    newest = 0.0
    for item in paths.daily.glob("life-frequency-诊断-*"):
        try:
            newest = max(newest, item.stat().st_mtime)
        except OSError:
            continue
    return (now - newest) if newest > 0 else None


def prune_watch_dir(paths: WatchPaths, *, retain_days: float, now: float,
                    log: RunLog | None = None) -> dict[str, int]:
    """按保留期裁剪样本与历史报告（**只动 watch 目录自己的文件**）。"""

    stats = {"samples_dropped": 0, "reports_dropped": 0, "alerts_trimmed": 0,
             "tmp_removed": 0, "logs_removed": 0}
    if retain_days <= 0:
        return stats
    cutoff = now - retain_days * 86400.0
    rows, _broken = load_samples(paths)
    kept = [row for row in rows
            if (as_float(row.get("ts"), 0.0) or 0.0) >= cutoff]
    dropped = len(rows) - len(kept)
    if dropped > 0:
        try:
            _atomic_write_text(paths.samples_jsonl, "".join(
                json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in kept))
            with paths.samples_csv.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(SAMPLE_CSV_COLUMNS),
                                        extrasaction="ignore")
                writer.writeheader()
                writer.writerows(kept)
            stats["samples_dropped"] = dropped
        except OSError:
            pass
    try:
        for item in sorted(paths.daily.glob("life-frequency-诊断-*")):
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink()
                    stats["reports_dropped"] += 1
            except OSError:
                continue
    except OSError:
        pass
    try:
        if paths.alerts_log.exists():
            lines = paths.alerts_log.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > ALERT_LOG_TRIM_THRESHOLD:
                _atomic_write_text(paths.alerts_log,
                                   "\n".join(lines[-ALERT_LOG_KEEP_LINES:]) + "\n")
                stats["alerts_trimmed"] = len(lines) - ALERT_LOG_KEEP_LINES
    except OSError:
        pass
    # 原子写的 .tmp 残留与轮转出来的 watch.log.1：都不该长期活在这个目录里
    try:
        for item in paths.root.glob("*.tmp"):
            try:
                if now - item.stat().st_mtime > 3600:
                    item.unlink()
                    stats["tmp_removed"] += 1
            except OSError:
                continue
        for item in paths.root.glob(f"{paths.log.name}.*"):
            try:
                if now - item.stat().st_mtime > retain_days * 86400:
                    item.unlink()
                    stats["logs_removed"] += 1
            except OSError:
                continue
    except OSError:
        pass
    if log and any(stats.values()):
        log.write(f"保留期裁剪（{retain_days:g} 天）：样本 -{stats['samples_dropped']}、"
                  f"报告 -{stats['reports_dropped']}、告警流水 -{stats['alerts_trimmed']}、"
                  f"临时残留 -{stats['tmp_removed']}、旧运行日志 -{stats['logs_removed']}")
    return stats


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None or value == "":
        return "—"
    number = as_float(value)
    if number is None:
        return str(value)
    return f"{number:.{digits}f}"


def summarize_rows(rows: Sequence[Mapping[str, Any]], *, interval: float) -> dict[str, Any]:
    """把一周的样本压成可读的统计量（覆盖缺口 / 卡顿 / 模型 / 数值 / 逐日走势）。"""

    summary: dict[str, Any] = {"count": len(rows), "interval": interval}
    if not rows:
        return summary
    ordered = sorted(rows, key=lambda row: as_float(row.get("ts"), 0.0) or 0.0)
    stamps = [as_float(row.get("ts"), 0.0) or 0.0 for row in ordered]
    start, end = stamps[0], stamps[-1]
    summary.update({"start": start, "end": end, "span_seconds": max(0.0, end - start)})
    # 覆盖率用**实际中位间隔**算，而不是配置的 --interval：同一个 watch 目录可能被不同
    # 间隔的几轮监控写过（调试时用 1 分钟、正式跑改成 5 分钟），用配置值会把覆盖率算废。
    # 中位数对少量缺口（机器休眠一晚上）稳健，不会被几次大间隔带偏。
    deltas = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
    positive = [delta for delta in deltas if delta > 0]
    effective = (max(1.0, float(statistics.median(positive))) if positive
                 else max(1.0, interval))
    summary["configured_interval"] = interval
    summary["effective_interval"] = effective
    expected = max(1.0, (end - start) / effective + 1.0) if end > start else 1.0
    summary["expected"] = expected
    summary["coverage"] = min(1.0, len(ordered) / expected)
    summary["missing_samples"] = max(0.0, expected - len(ordered))
    # 缺口阈值取「两个采样周期」：缺一个点是调度抖动（报告生成/磁盘慢），
    # 连丢两个点才是真的停机。再宽就会把「机器睡了 10 分钟」这种整段丢失算成正常。
    threshold = max(effective * 2.0, effective + 120.0)
    gaps: list[dict[str, float]] = []
    for previous, current in zip(ordered, ordered[1:]):
        prev_ts = as_float(previous.get("ts"), 0.0) or 0.0
        cur_ts = as_float(current.get("ts"), 0.0) or 0.0
        if cur_ts - prev_ts > threshold:
            gaps.append({"from": prev_ts, "to": cur_ts, "span": cur_ts - prev_ts})
    summary["gaps"] = gaps
    summary["gap_total_seconds"] = sum(item["span"] for item in gaps)
    summary["max_gap_seconds"] = max((item["span"] for item in gaps), default=0.0)

    ages: list[float] = []
    slow = stalled = no_age = 0
    for row in ordered:
        age = as_float(row.get("tick_age_seconds"))
        tick = as_float(row.get("tick_seconds"), 600.0) or 600.0
        if age is None:
            no_age += 1
            continue
        ages.append(age)
        if age > tick * 6:
            stalled += 1
        elif age > tick * 1.5:
            slow += 1
    summary["tick"] = {"samples": len(ages), "no_age": no_age,
                       "min": min(ages) if ages else None,
                       "median": statistics.median(ages) if ages else None,
                       "p90": _percentile(ages, 0.9), "max": max(ages) if ages else None,
                       "slow": slow, "stalled": stalled}

    activity: dict[str, int] = {}
    for row in ordered:
        label = str(row.get("activity_label") or row.get("activity") or "未知")
        activity[label] = activity.get(label, 0) + 1
    summary["activity"] = activity

    streaks = [as_int(row.get("llm_fail_streak")) for row in ordered]
    cooldowns = [as_float(row.get("llm_cooldown_seconds_left"), 0.0) or 0.0 for row in ordered]
    success_ages = [age for age in
                    (as_float(row.get("llm_success_age_seconds")) for row in ordered)
                    if age is not None]
    summary["llm"] = {
        "fail_samples": sum(1 for streak in streaks if streak > 0),
        "max_streak": max(streaks, default=0),
        "cooldown_samples": sum(1 for value in cooldowns if value > 0),
        "max_cooldown": max(cooldowns, default=0.0),
        "last_success_age": success_ages[-1] if success_ages else None,
    }

    def series(key: str) -> list[float]:
        return [value for value in (as_float(row.get(key)) for row in ordered)
                if value is not None]

    applied = series("applied_sessions")
    observed = series("observed_sessions")
    unbacked = series("unbacked_worst_seconds")
    sizes = series("state_size")
    summary["apply"] = {
        "applied_first": applied[0] if applied else None,
        "applied_last": applied[-1] if applied else None,
        "applied_max": max(applied) if applied else None,
        "observed_max": max(observed) if observed else None,
        "unbacked_max": max(unbacked) if unbacked else 0.0,
        "never_applied_samples": sum(1 for value in applied if value <= 0),
    }
    summary["size"] = {"first": sizes[0] if sizes else None,
                       "last": sizes[-1] if sizes else None,
                       "max": max(sizes) if sizes else None}

    status: dict[str, int] = {}
    findings: dict[str, dict[str, Any]] = {}
    for row in ordered:
        key = str(row.get("check_status") or "unknown")
        status[key] = status.get(key, 0) + 1
        for entry in row.get("findings") or []:
            if isinstance(entry, Mapping):
                level = str(entry.get("level") or "")
                ident = str(entry.get("id") or "")
                title = str(entry.get("title") or "")
            else:  # 兼容更早的 "LEVEL|id|title" 文本形态
                parts = str(entry).split("|", 2)
                if len(parts) < 3:
                    continue
                level, ident, title = parts
            if not ident:
                continue
            item = findings.setdefault(ident, {"level": level, "title": title, "count": 0})
            item["count"] += 1
            if ALERT_LEVEL_ORDER.get(level, 0) > ALERT_LEVEL_ORDER.get(item["level"], 0):
                item["level"] = level
    summary["status"] = status
    summary["findings"] = findings

    days: dict[str, dict[str, Any]] = {}
    for row in ordered:
        day = str(row.get("iso") or "")[:10] or "未知"
        bucket = days.setdefault(day, {"count": 0, "emotion": [], "energy": [], "stress": [],
                                       "loneliness": [], "sleep": [], "activity": {},
                                       "fail": 0, "warn": 0, "tick_max": 0.0})
        bucket["count"] += 1
        bucket["fail"] += as_int(row.get("findings_fail"))
        bucket["warn"] += as_int(row.get("findings_warn"))
        for key in ("emotion", "energy", "stress", "loneliness"):
            value = as_float(row.get(key))
            if value is not None:
                bucket[key].append(value)
        sleep_value = as_float(row.get("sleep_minutes_today"))
        if sleep_value is not None:
            bucket["sleep"].append(sleep_value)
        label = str(row.get("activity_label") or row.get("activity") or "未知")
        bucket["activity"][label] = bucket["activity"].get(label, 0) + 1
        tick_age = as_float(row.get("tick_age_seconds"))
        if tick_age is not None:
            bucket["tick_max"] = max(bucket["tick_max"], tick_age)
    summary["days"] = days
    return summary


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(str(item) for item in headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(str(item) for item in row) + " |")
    return "\n".join(lines)


def _read_tail_lines(path: pathlib.Path, *, max_bytes: int = 32768,
                     limit: int = 30) -> list[str]:
    """只读文件末尾若干字节再切行（alerts.log 可能上万行，不该整份读进内存）。"""

    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            chunk = handle.read()
    except OSError:
        return []
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    return [line for line in lines if line.strip()][-limit:]


def dynamics_findings(rows: Sequence[Mapping[str, Any]],
                      *, config: Mapping[str, Any]) -> list[dict[str, Any]]:
    """v1.19.0（PR-AF-1 配套）：**循环动力学**判据（跨样本，六条）。

    配置侧判据只看得见表面的 ``dry_run`` / 静默时段；真正决定「她过得好不好」
    的是跨样本的循环形态——清醒能不能维持、内心维度是否在动、体力是否失去弹性。
    真机 2026-10-13 的教训是：这三条全绿、配置看着正常，而她 82.8% 的时间在睡觉、
    孤独 152/152 恒为 0、清醒只剩 1 个 tick。

    六条阈值全部走 ``[diagnostics]`` 段可配——修完之后阈值必须跟着调，写死会立刻
    变成新的误报源。

    | 键 | 默认 | 含义 |
    |---|---|---|
    | ``dyn_sleep_share`` | 0.70 | 睡觉占比超过即 FAIL |
    | ``dyn_awake_median_minutes`` | 30 | 连续清醒中位数低于即 WARN |
    | ``dyn_loneliness_frozen_samples`` | 20 | 孤独恒定的样本数达到即 WARN |
    | ``dyn_energy_capped_share`` | 0.80 | 体力贴顶占比超过即 WARN |
    | ``dyn_battery_low`` | 2.0 | 社交电量低于此值算低 |
    | ``dyn_battery_low_share`` | 0.50 | 低电量样本占比超过即 WARN |
    | ``dyn_sleep_minutes_cap`` | 600 | 当日睡眠分钟数达到此值算贴上限 |
    | ``dyn_sleep_capped_share`` | 0.50 | 贴上限样本占比超过即 WARN |
    """

    diag = _section(config, "diagnostics")
    if not rows:
        return []
    total = len(rows)
    findings: list[dict[str, Any]] = []

    def add(level: str, title: str, evidence: str, fix: str) -> None:
        findings.append({"level": level, "id": f"动力学|{title}", "title": title,
                         "evidence": evidence, "fix": fix, "scope": "循环动力学"})

    # 1) 睡觉占比——循环退化成单向泵的最直接信号
    share_limit = as_float(diag.get("dyn_sleep_share"), 0.70)
    if share_limit is None:
        share_limit = 0.70
    sleeping = sum(1 for row in rows if str(row.get("activity") or "") in ("sleep", "nap"))
    share = sleeping / total
    if share > share_limit:
        add("FAIL", f"睡觉占比 {share * 100:.1f}%（阈值 {share_limit * 100:.0f}%）",
            f"{sleeping}/{total} 个采样点在睡觉。真机实测 79.6% 时，"
            "清醒时长中位只有 10 分钟——这不是「睡得偏多」，是循环的吸收态",
            "看清醒侧判据：生病是否短路了每日清醒下限（[activity] cold_onset_awake_floor）")

    # 2) 连续清醒时长中位——清醒能否作为一个状态存在
    awake_floor = as_float(diag.get("dyn_awake_median_minutes"), 30.0)
    if awake_floor is None:
        awake_floor = 30.0
    awake = [value for value in (as_float(row.get("continuous_awake_minutes")) for row in rows)
             if value is not None]
    if awake and statistics.median(awake) < awake_floor:
        add("WARN", f"连续清醒中位 {statistics.median(awake):.0f} 分钟（阈值 {awake_floor:.0f}）",
            "清醒几乎刚建立就被拉回睡眠。真机实测恒为 10 分钟 = 恰好 1 个 tick，"
            "说明「醒 → 下一 tick 又睡」在结构上成立",
            "排查入睡资格是否被无条件短路；睡眠侧有最短睡眠目标，清醒侧应有对等下限")

    # 3) 孤独冻结——内心维度是否还在动
    frozen_min = as_int(diag.get("dyn_loneliness_frozen_samples"), 20) or 20
    lonely = [value for value in (as_float(row.get("loneliness")) for row in rows)
              if value is not None]
    if len(lonely) >= max(2, frozen_min) and len(set(lonely)) == 1:
        add("WARN", f"孤独恒定 {lonely[0]:.2f} 跨 {len(lonely)} 个样本",
            "孤独只在清醒且无互动时增长；清醒过短 ⇒ 只减不增 ⇒ 永久冻结。"
            "它同时是主动开口的驱动输入，冻住了主动节奏也是死的",
            "先修清醒时长；若仍冻结，再看 [mood] loneliness_per_quiet_tick / relief")

    # 4) 体力长期贴顶——恢复侧失去弹性
    cap_share_limit = as_float(diag.get("dyn_energy_capped_share"), 0.80)
    if cap_share_limit is None:
        cap_share_limit = 0.80
    pairs = [(energy, cap) for energy, cap in
             ((as_float(row.get("energy")), as_float(row.get("energy_cap"))) for row in rows)
             if energy is not None and cap is not None and cap > 0]
    if pairs:
        capped = sum(1 for energy, cap in pairs if energy >= cap - 1e-9)
        if capped / len(pairs) > cap_share_limit:
            add("WARN", f"体力贴顶占比 {capped / len(pairs) * 100:.1f}%（阈值 "
                        f"{cap_share_limit * 100:.0f}%）",
                "体力长期满格 ⇒ 体力作为驱动输入失去调节作用，"
                "「状态差就少说话」退化成常数倍率",
                "看她是否根本没在消耗体力（清醒时长）；确认清醒侧消耗率是否生效")

    # 5) 社交电量长期偏低——主动开口被硬闸持续挡住
    low = as_float(diag.get("dyn_battery_low"), 2.0)
    if low is None:
        low = 2.0
    low_share_limit = as_float(diag.get("dyn_battery_low_share"), 0.50)
    if low_share_limit is None:
        low_share_limit = 0.50
    battery = [value for value in (as_float(row.get("social_battery")) for row in rows)
               if value is not None]
    if battery:
        low_count = sum(1 for value in battery if value < low)
        if low_count / len(battery) > low_share_limit:
            add("WARN", f"社交电量 < {low:g} 占比 {low_count / len(battery) * 100:.1f}%"
                        f"（阈值 {low_share_limit * 100:.0f}%）",
                "主动开口的硬闸会持续拒绝——配置看起来开着，实际从不开口。"
                "睡眠中电量会回充，所以这条常与「睡觉占比高」同时出现",
                "确认 [proactive] enabled 是否真的要她主动；"
                "低电量阈值见 /生活 为什么 里的 low_battery 计数")

    # 6) 当日睡眠贴上限——睡眠时长失去调节
    cap_minutes = as_float(diag.get("dyn_sleep_minutes_cap"), 600.0)
    if cap_minutes is None:
        cap_minutes = 600.0
    sleep_share_limit = as_float(diag.get("dyn_sleep_capped_share"), 0.50)
    if sleep_share_limit is None:
        sleep_share_limit = 0.50
    sleeps = [value for value in (as_float(row.get("sleep_minutes_today")) for row in rows)
              if value is not None]
    if sleeps:
        capped = sum(1 for value in sleeps if value >= cap_minutes - 1e-9)
        if capped / len(sleeps) > sleep_share_limit:
            add("WARN", f"当日睡眠贴 {cap_minutes:.0f} 分钟占比 "
                        f"{capped / len(sleeps) * 100:.1f}%（阈值 {sleep_share_limit * 100:.0f}%）",
                "当日睡眠长时间顶在配置上限 ⇒ 睡眠时长不再由状态决定，而由上限决定",
                "确认上限是否合理；看她是否始终睡到上限才醒（体力回满 + 最短睡眠目标）")
    return findings


def _segment_table(rows: Sequence[Mapping[str, Any]], marks: Sequence[Mapping[str, Any]],
                   *, config: Mapping[str, Any]) -> list[str]:
    """v1.19.0（PR-AF-1 配套）：按改动时点把样本分段，输出改前/改后对比表。

    为什么必须分段：改动前后混在一起算均值，得到的数字既不代表改前也不代表改后，
    却会被当成「修完了没有」的证据。
    """

    if not marks or not rows:
        return []
    ordered = sorted(rows, key=lambda row: as_float(row.get("ts"), 0.0) or 0.0)
    stamps = sorted((as_float(mark.get("at"), 0.0) or 0.0 for mark in marks))
    diag = _section(config, "diagnostics")

    def bucket(predicate) -> list[Mapping[str, Any]]:
        return [row for row in ordered if predicate(as_float(row.get("ts"), 0.0) or 0.0)]

    segments: list[tuple[str, list[Mapping[str, Any]]]] = []
    segments.append(("改前基线", bucket(lambda ts: ts < stamps[0])))
    for index, start in enumerate(stamps):
        end = stamps[index + 1] if index + 1 < len(stamps) else float("inf")
        label = next((str(mark.get("label") or "") for mark in marks
                      if (as_float(mark.get("at"), 0.0) or 0.0) == start), f"标记 {index + 1}")
        segments.append((f"改后：{label}", bucket(lambda ts, s=start, e=end: s <= ts < e)))
    segments.append(("改后（最新标记至今）", bucket(lambda ts: ts >= stamps[-1])))

    def stats(items: Sequence[Mapping[str, Any]]) -> tuple[float, float, float, float, float]:
        if not items:
            return (0.0, 0.0, 0.0, 0.0, 0.0)
        sleeping = sum(1 for row in items
                       if str(row.get("activity") or "") in ("sleep", "nap")) / len(items)
        awake = [value for value in (as_float(row.get("continuous_awake_minutes")) for row in items)
                 if value is not None]
        lonely = [value for value in (as_float(row.get("loneliness")) for row in items)
                  if value is not None]
        return (len(items), sleeping * 100.0,
                statistics.median(awake) if awake else 0.0,
                statistics.median(lonely) if lonely else 0.0,
                statistics.mean([as_float(row.get("energy")) or 0.0 for row in items]))

    header = ("| 段 | 样本 | 睡觉占比 | 连续清醒中位(分) | 孤独中位 | 体力均值 |",
              "|---|---|---|---|---|---|")
    body = []
    for label, items in segments:
        if not items:
            continue
        count, sleep_share, awake_median, lonely_median, energy = stats(items)
        body.append(f"| {label} | {count} | {sleep_share:.1f}% | {awake_median:.0f} "
                    f"| {lonely_median:.2f} | {energy:.2f} |")
    if len(body) < 2:
        return []
    return list(header) + body


def _watch_config_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    """v1.19.0（PR-AF-1 配套）：取监控判据用的配置原文。

    优先用 ``watch_state.json`` 里缓存的（监控循环每轮刷新），退回按
    ``config_path`` 现读——这样 ``--summary-only`` 与历史 state 也能判动力学。
    """

    cached = state.get("config")
    if isinstance(cached, dict) and cached:
        return cached
    raw_path = state.get("config_path")
    if not raw_path:
        return {}
    payload, _error = _load_toml(pathlib.Path(str(raw_path)))
    return payload if isinstance(payload, dict) else {}


def render_watch_summary(paths: WatchPaths, *, rows: Sequence[Mapping[str, Any]],
                         summary: Mapping[str, Any], tracker: AlertTracker,
                         state: Mapping[str, Any], tz: int, now: float,
                         interval: float, broken_lines: int = 0) -> str:
    """人读的汇总报告：先给结论，再给证据。随时可以重算（``--summary-only``）。"""

    active = tracker.active_alerts()
    fail_count = sum(1 for alert in active if alert.level == "FAIL")
    warn_count = sum(1 for alert in active if alert.level == "WARN")
    lines: list[str] = ["# life-frequency 长期监控汇总", ""]
    start = as_float(summary.get("start"), 0.0) or 0.0
    end = as_float(summary.get("end"), 0.0) or 0.0
    lines += [
        f"- 生成时间：{local_stamp(now, tz)}（UTC{tz / 60:+g}）",
        f"- 监控窗口：{local_stamp(start, tz) if start else '—'} → "
        f"{local_stamp(end, tz) if end else '—'}"
        f"（跨度 {human_span(summary.get('span_seconds') or 0)}）",
        f"- 样本：**{summary.get('count', 0)}** 个（采样间隔：配置 {human_span(interval)} / "
        f"实际中位 {human_span(summary.get('effective_interval') or interval)}；"
        f"覆盖率 {_fmt((summary.get('coverage') or 0) * 100, 1)}%"
        + (f"；JSONL 里有 {broken_lines} 行坏数据（多为被杀在半截的写入）" if broken_lines else "")
        + "）",
        f"- 数据目录：`{state.get('data_dir') or '（未找到）'}`"
        f"（{state.get('data_dir_source') or '—'}）",
        f"- 配置：`{state.get('config_path') or '（未找到，按内置默认值判定）'}`",
        f"- 运行器：启动 {state.get('run_starts', 1)} 次；样本采集缺口 "
        f"{len(summary.get('gaps') or [])} 次（合计 {human_span(summary.get('gap_total_seconds') or 0)}）",
        f"- 当前未恢复告警：**{len(active)}** 条（FAIL {fail_count} / WARN {warn_count}）",
    ]
    report_failures = as_int(state.get("report_failures"), 0)
    if report_failures:
        lines.append(f"- ⚠ 完整报告生成失败 **{report_failures}** 次（连续计数）："
                     f"见 `{paths.log.name}` 里的「完整报告生成失败」")
    lines.append("")

    if fail_count:
        lines += [f"> ⚠ 有 {fail_count} 条未恢复的**确定性故障（FAIL）**：见下表，"
                  "每条都带证据与修法。", ""]
    elif not summary.get("count"):
        lines += ["> 还没有样本：先跑 `--watch`（或 `--once` 采一个）。", ""]
    elif fail_count == 0 and (summary.get("coverage") or 0) > 0.98 and (
            (summary.get("max_gap_seconds") or 0)
            <= (summary.get("effective_interval") or interval) * 3):
        lines += ["> 窗口内没有未恢复的 FAIL；采样覆盖完整（没有超过三个采样周期的缺口）。"
                  "**注意**：这只说明「按现有判据没发现问题」，不等于每个 tick 都正常"
                  "（判据是抽样，不是逐 tick 记录）。", ""]
    else:
        lines += ["> 没有未恢复的 FAIL，但采样覆盖不完整（见「覆盖与缺口」）——"
                  "缺口时段的运行情况没有证据，不能算通过。", ""]

    # ---- 未恢复告警 ----
    lines += ["## 1. 当前未恢复告警", ""]
    if not active:
        lines += ["（无）", ""]
    else:
        table = [[alert.level, local_stamp(alert.first_seen, tz),
                  local_stamp(alert.last_seen, tz), alert.hits,
                  alert.scope or "—", alert.title] for alert in active]
        lines += [_markdown_table(["级别", "首次出现", "最近出现", "命中次数", "范围", "问题"],
                                  table), ""]
        for alert in active:
            if alert.evidence:
                lines.append(f"- **{alert.title}**：{alert.evidence}")
            if alert.fix:
                lines.append(f"  - 修法：{alert.fix}")
        lines.append("")

        # ---- v1.2.0（D10）：签名命中原文——告警旁边直接给证据 ----
        excerpt_rows = load_recent_log_excerpts(paths, limit=4)
        if excerpt_rows:
            lines += ["### 签名命中原文（最近几轮，全量见 `log_excerpts.jsonl`；"
                      "全文扫描，不限本插件行；已脱敏）", ""]
            for item in excerpt_rows:
                lines.append(f"**{item.get('iso') or local_stamp(as_float(item.get('ts')), tz)}**")
                lines.append("")
                lines.append("```text")
                for block in item.get("excerpts") or []:
                    lines.extend(str(block).splitlines())
                    lines.append("· · ·")
                lines.append("```")

    # ---- 覆盖与缺口 ----
    gaps = list(summary.get("gaps") or [])
    lines += ["## 2. 覆盖与缺口", "",
              _markdown_table(["指标", "值"], [
                  ["样本数", summary.get("count", 0)],
                  ["按实际中位间隔推算的应有样本数", _fmt(summary.get("expected"), 1)],
                  ["推算缺失的样本数", _fmt(summary.get("missing_samples"), 1)],
                  ["覆盖率", f"{_fmt((summary.get('coverage') or 0) * 100, 1)}%"],
                  ["采样缺口次数", len(gaps)],
                  ["缺口合计", human_span(summary.get("gap_total_seconds") or 0)],
                  ["最长一次缺口", human_span(summary.get("max_gap_seconds") or 0)],
                  ["运行器启动次数", state.get("run_starts", 1)],
              ]), ""]
    if gaps:
        lines += ["缺口明细（前 10 条；机器休眠/进程被杀/重启都会留下缺口）：", "",
                  _markdown_table(["从上一点", "到下一点", "时长"], [
                      [local_stamp(item["from"], tz), local_stamp(item["to"], tz),
                       human_span(item["span"])] for item in gaps[:10]]), ""]

    # ---- 生活循环推进 ----
    tick = summary.get("tick") or {}
    lines += ["## 3. 生活循环推进（最重要的一条）", "",
              _markdown_table(["指标", "值"], [
                  ["有效样本（有 last_tick_at）", tick.get("samples", 0)],
                  ["缺失 last_tick_at 的样本", tick.get("no_age", 0)],
                  ["最新推进距今 最小 / 中位", f"{human_span(tick.get('min') or 0)} / "
                                              f"{human_span(tick.get('median') or 0)}"],
                  ["最新推进距今 P90 / 最大", f"{human_span(tick.get('p90') or 0)} / "
                                             f"{human_span(tick.get('max') or 0)}"],
                  ["比配置慢（>1.5 tick）的样本", tick.get("slow", 0)],
                  ["判定「没在跑」（>6 tick 或 停机间隙）的样本", tick.get("stalled", 0)],
              ]), ""]

    # ---- 模型侧 ----
    llm = summary.get("llm") or {}
    lines += ["## 4. 模型侧（活动决策）", "",
              _markdown_table(["指标", "值"], [
                  ["连续失败 > 0 的样本", llm.get("fail_samples", 0)],
                  ["最大连续失败次数", llm.get("max_streak", 0)],
                  ["处于冷却中的样本", llm.get("cooldown_samples", 0)],
                  ["最长冷却剩余", human_span(llm.get("max_cooldown") or 0)],
                  ["距上次成功决策（末样本）", human_span(llm.get("last_success_age") or 0)
                   if llm.get("last_success_age") is not None else "从未成功"],
              ]), ""]

    # ---- 写宿主 ----
    apply_info = summary.get("apply") or {}
    lines += ["## 5. 写宿主（倍率是否真的落下去了）", "",
              _markdown_table(["指标", "值"], [
                  ["已写会话数（首 / 末样本）",
                   f"{apply_info.get('applied_first', '—')} → {apply_info.get('applied_last', '—')}"],
                  ["已写会话数（窗口内最大）", apply_info.get("applied_max", "—")],
                  ["认到外部基数的会话（最大）", apply_info.get("observed_max", "—")],
                  ["「写不进宿主」最长退避", human_span(apply_info.get("unbacked_max") or 0)],
                  ["一次都没写过的样本数", apply_info.get("never_applied_samples", 0)],
              ]), ""]

    # ---- 逐日走势 ----
    days = summary.get("days") or {}
    if days:
        table = []
        for day in sorted(days):
            bucket = days[day]
            top = sorted(bucket["activity"].items(), key=lambda item: -item[1])[:3]
            table.append([
                day, bucket["count"],
                _fmt(_mean(bucket["emotion"]), 2), _fmt(_mean(bucket["energy"]), 2),
                _fmt(min(bucket["energy"]) if bucket["energy"] else None, 2),
                _fmt(_mean(bucket["stress"]), 2), _fmt(_mean(bucket["loneliness"]), 2),
                _fmt(_mean(bucket["sleep"]), 0),
                "、".join(f"{name} {count}" for name, count in top),
                bucket["fail"], bucket["warn"],
            ])
        lines += ["## 6. 逐日走势（均值；体力另给当日最低）", "",
                  _markdown_table(["日期", "样本", "情绪", "体力", "体力最低", "压力",
                                   "孤独", "当日睡眠(分)", "主要活动", "FAIL", "WARN"], table),
                  ""]

    # ---- 活动分布 ----
    activity = summary.get("activity") or {}
    if activity:
        total = sum(activity.values()) or 1
        table = [[name, count, f"{count / total * 100:.1f}%"]
                 for name, count in sorted(activity.items(), key=lambda item: -item[1])]
        lines += ["## 7. 活动分布（按采样点）", "",
                  _markdown_table(["活动", "样本", "占比"], table), ""]

    # ---- v1.19.0（PR-AF-1 配套）：循环动力学（跨样本判据）----
    # 插在「活动分布」之后：那一条只告诉你她在这段时间做了什么，这一节告诉你
    # 这套循环还健不健康——清醒能否维持、内心维度是否在动、体力是否失去弹性。
    config_payload = _watch_config_payload(state)
    dynamics = dynamics_findings(rows, config=config_payload)
    if dynamics:
        lines += ["## 7b. 循环动力学（跨样本判据；阈值见 [diagnostics]）", "",
                  _markdown_table(
                      ["级别", "问题", "证据", "修法"],
                      [[item["level"], item["title"], item.get("evidence") or "—",
                        item.get("fix") or "—"] for item in dynamics]), ""]
    else:
        lines += ["## 7b. 循环动力学（跨样本判据；阈值见 [diagnostics]）", "",
                  "窗口内未命中任何动力学判据（六条全过）。", ""]

    # ---- v1.19.0（PR-AF-1 配套）：改动时点分段 ----
    segment_lines = _segment_table(rows, load_marks(paths), config=config_payload)
    if segment_lines:
        lines += ["## 7c. 改动时点分段（改前 / 改后对比）", "", *segment_lines, "",
                  "> 打点方式：部署并完整重启后执行 "
                  "`python collect_diagnostics.py --mark \"<标签>\" --watch-dir <监控目录>`。"
                  "改前/改后混在一起算均值，得到的数字两边都不代表——所以必须分段。", ""]

    # ---- 数据规模 ----
    size = summary.get("size") or {}
    lines += ["## 8. 数据规模与判定分布", "",
              _markdown_table(["指标", "值"], [
                  ["状态文件大小（首 / 末 / 最大）",
                   f"{human_size(size['first']) if size.get('first') else '—'} → "
                   f"{human_size(size['last']) if size.get('last') else '—'} / "
                   f"{human_size(size['max']) if size.get('max') else '—'}"],
                  ["样本判定 正常/可疑/故障/无数据",
                   " / ".join(str((summary.get("status") or {}).get(key, 0))
                              for key in ("ok", "warn", "fail", "unknown"))],
              ]), ""]

    # ---- 窗口内告警统计 ----
    findings = summary.get("findings") or {}
    if findings:
        table = [[item["level"], item["count"], item["title"]]
                 for item in sorted(findings.values(), key=lambda item: -item["count"])[:15]]
        lines += ["## 9. 窗口内命中过的检查项（按样本数）", "",
                  _markdown_table(["级别", "命中样本数", "检查项"], table), ""]

    # ---- 最近事件 ----
    recent = _read_tail_lines(paths.alerts_log, limit=20)
    if recent:
        lines += ["## 10. 最近告警事件（末 20 条，全文见 alerts.log）", "", "```",
                  *recent, "```", ""]

    # ---- 产物与口径 ----
    lines += [
        "## 11. 产物", "",
        f"- 样本时序：`{paths.samples_jsonl.name}`（JSONL，含 findings 明细）/ "
        f"`{paths.samples_csv.name}`（CSV，可直接导入 Excel 画图）",
        f"- 告警流水：`{paths.alerts_log.name}`（全量事件）/ `{paths.alerts_md.name}`（当前状态）",
        f"- 完整报告：`daily/` 下的 `life-frequency-诊断-<时间戳>.md`"
        f"（每天一份 + 新 FAIL 时补一份）",
        f"- 运行日志：`{paths.log.name}`；重新生成本汇总："
        "`python collect_diagnostics.py --summary-only`",
        "",
        "## 12. 口径与局限（读之前请先看这一段）", "",
        "- 判定阈值与一次性报告**完全同源**（`_state_checks` / `collect_config`），"
        "所以两边不会自相矛盾。",
        "- 样本是**快照**，不是逐 tick 记录：短暂抖动可能落在两次采样之间"
        "（采样间隔见表头；插件默认 tick 600s，间隔远大于它时漏掉的细节更多）。",
        "- 「没有 FAIL」= 按现有判据没发现问题，**不等于**每个 tick 都正常。",
        "- 覆盖率按**实际中位间隔**推算（不是配置值），所以中途改过间隔也能算出合理结果；"
        "但它只是推算——真的想知道有没有整段丢失，看「采样缺口」与「推算缺失的样本数」。",
        "- 采样缺口（机器休眠/进程被杀）时段的运行情况**没有证据**，"
        "不能算通过；缺口明细在「覆盖与缺口」一节。",
        "- 凭据一律脱敏（`<redacted>`），带身份语境的号码写 `***`，"
        "监控产物**不含消息正文**。",
        "",
    ]
    return "\n".join(lines)


def write_alert_events(paths: WatchPaths, events: Sequence[Mapping[str, Any]], *, tz: int) -> None:
    """告警事件流水：一行一个**变化**（不是一行一个样本）。"""

    if not events:
        return
    lines: list[str] = []
    for event in events:
        stamp = local_stamp(as_float(event.get("ts"), 0.0) or 0.0, tz)
        previous = str(event.get("prev_level") or "")
        transition = f"{previous}→{event.get('level')}" if previous else str(event.get("level"))
        line = (f"{stamp} {event.get('kind')} {transition} [{event.get('id')}] "
                f"{event.get('title')}")
        if event.get("evidence"):
            line += f" —— {redact(event.get('evidence'), limit=240)}"
        if event.get("fix") and event.get("kind") in ("开始", "升级"):
            line += f" —— 修法：{redact(event.get('fix'), limit=240)}"
        lines.append(line)
    try:
        _append_text(paths.alerts_log, "\n".join(lines) + "\n")
    except OSError:
        pass


def write_alerts_md(paths: WatchPaths, tracker: AlertTracker, *, now: float, tz: int,
                    interval: float) -> None:
    """当前告警状态（每次采样重写，体积有界）：值班的人先看这一份。"""

    active = tracker.active_alerts()
    lines = ["# life-frequency 监控告警（当前状态）", "",
             f"- 生成时间：{local_stamp(now, tz)}（UTC{tz / 60:+g}）",
             f"- 采样间隔：{human_span(interval)}",
             f"- 当前未恢复：{len(active)} 条"
             f"（FAIL {sum(1 for alert in active if alert.level == 'FAIL')} / "
             f"WARN {sum(1 for alert in active if alert.level == 'WARN')}）",
             "- 事件流水（含已恢复）：`alerts.log`；汇总：`watch_summary.md`", ""]
    if not active:
        lines += ["（当前没有未恢复的告警）", ""]
    else:
        lines += [_markdown_table(["级别", "首次出现", "最近出现", "命中", "范围", "问题"],
                                  [[alert.level, local_stamp(alert.first_seen, tz),
                                    local_stamp(alert.last_seen, tz), alert.hits,
                                    alert.scope or "—", alert.title] for alert in active]), ""]
        for alert in active:
            if alert.evidence:
                lines.append(f"- **{alert.title}**：{alert.evidence}")
            if alert.fix:
                lines.append(f"  - 修法：{alert.fix}")
        lines.append("")
    recent = _read_tail_lines(paths.alerts_log, limit=15)
    if recent:
        lines += ["## 最近事件（末 15 条）", "", "```", *recent, "```", ""]
    try:
        _atomic_write_text(paths.alerts_md, "\n".join(lines))
    except OSError:
        pass


def run_watch(args: argparse.Namespace, *, plugin_dir: pathlib.Path,
              search_roots: Sequence[pathlib.Path], base_output_dir: pathlib.Path) -> int:
    """``--watch`` / ``--once`` / ``--summary-only`` 的统一入口。"""

    watch_root = (pathlib.Path(args.watch_dir).resolve() if args.watch_dir
                  else base_output_dir / WATCH_SUBDIR)
    paths = WatchPaths.of(watch_root)
    try:
        paths.ensure()
    except OSError as exc:
        print(f"监控目录不可写：{watch_root}（{exc.__class__.__name__}: {exc}）", file=sys.stderr)
        return 2
    log = RunLog(paths.log, echo=not args.quiet)

    interval = max(1.0, float(args.interval))
    duration_hours = max(0.0, float(args.duration_hours))
    retain_days = max(0.0, float(args.retain_days))
    report_every = max(0.0, float(args.report_every_hours)) * 3600.0
    summary_every = max(0.0, float(args.summary_every_minutes)) * 60.0
    now = time.time()
    state = load_watch_state(paths)

    if args.mark:
        # --mark 是**独立模式**：只追加一个改动时点标记，不启动监控循环、不抢锁。
        # 标记走 O_APPEND 的 watch_marks.jsonl，与主循环整份重写 watch_state.json 无冲突。
        # 时区只影响 iso 文案，取不到配置就按默认 UTC+8。
        mark_cfg = pathlib.Path(args.config).resolve() if args.config else None
        mark_tz = as_int(_section(_load_toml(mark_cfg)[0] if mark_cfg else {},
                                  "simulation").get("tz_offset_minutes"), 480)
        if append_mark(paths, args.mark, now=now, tz=mark_tz):
            log.write(f"已打改动时点标记「{args.mark}」→ {paths.marks}")
            return 0
        print("--mark 标签为空或写入失败", file=sys.stderr)
        return 2

    tracker = AlertTracker(state.get("alerts") if isinstance(state.get("alerts"), dict) else {})
    tailer = LogTailer(state.get("log_offsets") if isinstance(state.get("log_offsets"), dict) else {})
    explicit_data = pathlib.Path(args.data_dir).resolve() if args.data_dir else None
    explicit_config = pathlib.Path(args.config).resolve() if args.config else None
    explicit_logs = [pathlib.Path(item).resolve() for item in args.log]

    def resolve() -> WatchSources:
        return resolve_watch_sources(plugin_dir=plugin_dir, search_roots=search_roots,
                                     data_dir=explicit_data, config_path=explicit_config,
                                     log_paths=explicit_logs)

    sources = resolve()
    tz = as_int(_section(_load_toml(sources.config_path)[0] if sources.config_path else {},
                         "simulation").get("tz_offset_minutes"), 480)

    if args.summary_only:
        rows, broken = load_samples(paths)
        # 覆盖率与「采样间隔」要按**记录下来的**间隔算：--summary-only 可能用默认值调用，
        # 而样本是上一轮以别的间隔采的（否则覆盖率会算成一个毫无意义的数）
        recorded = as_float(state.get("interval_seconds"), interval) or interval
        text = render_watch_summary(paths, rows=rows,
                                    summary=summarize_rows(rows, interval=recorded),
                                    tracker=tracker, state=state, tz=tz, now=now,
                                    interval=recorded, broken_lines=broken)
        _atomic_write_text(paths.summary, text)
        log.write(f"按现有 {len(rows)} 个样本重算汇总（坏行 {broken}）：{paths.summary}")
        return 0

    lock = WatchLock(paths.lock, interval=interval, force=bool(args.force_lock),
                     disabled=bool(args.no_lock))
    ok, note = lock.acquire()
    if not ok:
        log.write(f"拒绝启动：{note}（想强行接管用 --force-lock；只想重算汇总用 --summary-only）")
        return 3
    if note:
        log.write(note)

    stop = StopFlag()
    saved_handlers = install_signal_handlers(stop)
    global PURE
    PURE = PurePlugin(plugin_dir)
    if not PURE.available:
        log.write(f"提示：未加载插件纯模块（{PURE.reason}），状态侧退回内置最小校验")

    ctx = WatchContext(plugin_dir=plugin_dir, search_roots=list(search_roots),
                       sources=sources, tailer=tailer, interval=interval)
    max_samples = max(0, as_int(args.max_samples)) or (1 if args.once else 0)
    process_start = time.time()
    campaign_started = as_float(state.get("campaign_started_at"), 0.0) or now
    index = as_int(state.get("samples_total"))
    run_starts = as_int(state.get("run_starts")) + 1
    prev_sha = str(state.get("last_state_sha") or "")
    prev_unchanged = as_int(state.get("state_unchanged_samples"))
    next_report_at = as_float(state.get("last_periodic_report_at"), 0.0) or 0.0
    captured = [value for value in (as_float(item, 0.0) or 0.0
                                    for item in (state.get("captures") or [])) if value > 0]
    # 「多久该补一份周期报告」按**最后一份报告的 mtime** 续期，而不是只看状态文件：
    # 崩溃-重启循环里每次启动都补一份的话，daily/ 一天就能被 100+ KB 的报告塞满
    if report_every > 0 and next_report_at <= 0:
        age = _last_report_age(paths, now=now)
        next_report_at = (now - age) if age is not None else 0.0
    # 数据目录还没找到时要**马上**重试（部署是逐步的）；已经找到了就 30 分钟后才刷新日志候选
    last_discovery = as_float(state.get("last_discovery_at"), 0.0) or (
        now - 1800 if sources.data_dir is None else now)
    last_summary_at = 0.0
    last_prune_at = as_float(state.get("last_prune_at"), 0.0) or 0.0
    stopped_reason = ""
    state.update({
        "schema": WATCH_SCHEMA_VERSION, "run_starts": run_starts,
        "campaign_started_at": campaign_started, "plugin_dir": str(plugin_dir),
        "interval_seconds": interval, "duration_hours": duration_hours,
        "started_at": state.get("started_at") or now,
        "python": sys.executable, "program_version": PROGRAM_VERSION,
    })
    log.write(f"监控启动：插件目录 {plugin_dir}；数据目录 "
              f"{sources.data_dir or '（未找到，稍后每 30 分钟重试）'}；"
              f"配置 {sources.config_path or '（未找到）'}；日志 {len(sources.log_paths)} 份；"
              f"间隔 {human_span(interval)}；"
              + (f"目标时长 {duration_hours:g} 小时" if duration_hours > 0 else "不限时长")
              + (f"；本次样本上限 {max_samples}" if max_samples else "")
              + f"；已累计 {index} 个样本")

    try:
        while True:
            # Ctrl+C / 停止文件可能落在上一轮的等待里（或启动前就已经放好）：先退出，别再多采一个样本
            if not stop.stopped and paths.stop.exists():
                stop.request(f"发现停止文件 {paths.stop.name}")
            if stop.stopped:
                stopped_reason = stop.reason or "收到停止请求"
                break
            cycle_wall = time.time()
            now = time.time()
            previous = as_float(state.get("last_sample_at"), 0.0) or 0.0
            gap = now - previous if previous > 0 else -1.0
            index += 1
            if sources.data_dir is None and now - last_discovery >= 1800:
                refreshed = resolve()
                last_discovery = now
                if refreshed.data_dir is not None:
                    sources.data_dir = refreshed.data_dir
                    sources.data_dir_source = refreshed.data_dir_source
                    ctx.sources = sources
                    log.write(f"已找到运行时数据目录：{sources.data_dir}（{sources.data_dir_source}）")
                if not sources.config_path and refreshed.config_path:
                    sources.config_path = refreshed.config_path
                if refreshed.log_paths:
                    sources.log_paths = refreshed.log_paths
            elif not explicit_logs and now - last_discovery >= 1800:
                # 只刷新日志候选：日志会轮转（maibot.log → maibot.log.1），
                # 但没必要每 30 分钟重跑一次「找数据目录」的全盘扫描（那是有界的 25s 扫描）
                last_discovery = now
                candidates = find_log_candidates(
                    [path for path in (sources.data_dir, *search_roots, plugin_dir) if path])
                if candidates:
                    sources.log_paths = candidates
                if sources.config_path is None and sources.data_dir is not None:
                    candidate = sources.data_dir / CONFIG_FILE
                    if candidate.exists() and looks_like_config(_load_toml(candidate)[0]):
                        sources.config_path = candidate
            try:
                row, findings = take_sample(ctx, now=now, sample_index=index,
                                            uptime=now - process_start, gap=gap,
                                            run_starts=run_starts, prev_sha=prev_sha,
                                            prev_unchanged=prev_unchanged)
            except Exception as exc:  # noqa: BLE001 —— 采样本不许杀死监控器
                log.write(f"采样抛异常（已跳过本样本）：{exc.__class__.__name__}: {exc}")
                row, findings = {"sample_index": index, "ts": round(now, 3),
                                 "check_status": "unknown", "note": "采样异常",
                                 "findings_fail": 0, "findings_warn": 0}, []
            # v1.2.0（D10）：签名原文先摘下来单独落盘，别让它进 samples.jsonl
            log_excerpts = row.pop("log_excerpts", None) or []
            append_sample(paths, row)
            if log_excerpts:
                append_log_excerpts(paths, log_excerpts, now=now, tz=tz)
            # 立刻续命：后面可能出现分钟级的完整报告（build_report 会扫目录/读库/算哈希），
            # 心跳要是压在最后写，一次慢周期就会超过锁的过期阈值 ⇒ 第二个进程接管锁 ⇒
            # 两个监控器同时写 samples.jsonl / watch_state.json（整周数据都会被写乱）
            if not lock.heartbeat():
                log.write("⚠ 锁心跳写不进去（锁文件被占用或磁盘只读）："
                          "另一个监控进程可能误判本进程已死")
            prev_sha = str(row.get("state_sha16") or "")
            prev_unchanged = as_int(row.get("state_unchanged_samples"))
            tz = as_int(_section(_load_toml(sources.config_path)[0]
                                 if sources.config_path else {},
                                 "simulation").get("tz_offset_minutes"), tz)
            events = tracker.update(findings, now=now)
            gap_threshold = max(interval * 3.0, interval + 300.0)
            if gap > gap_threshold:
                events.append({"ts": now, "kind": "采样缺口", "id": "运行器|采样缺口",
                               "level": "WARN", "prev_level": "", "title": "采样缺口",
                               "evidence": f"距上一个样本 {human_span(gap)}"
                                           f"（间隔 {human_span(interval)}）",
                               "fix": "机器休眠/进程被杀/重启都会造成缺口；"
                                      "看 watch.log 的启动记录", "scope": "运行器"})
            write_alert_events(paths, events, tz=tz)
            write_alerts_md(paths, tracker, now=now, tz=tz, interval=interval)

            new_fail = [event for event in events
                        if event.get("level") == "FAIL" and event.get("kind") in ("开始", "升级")]
            captured_now = False
            if new_fail:
                same_day = [stamp for stamp in captured if now - stamp < 86400]
                last_capture = max(captured) if captured else 0.0
                if (now - last_capture >= CAPTURE_MIN_GAP_SECONDS
                        and len(same_day) < CAPTURE_MAX_PER_DAY):
                    if capture_full_report(paths, plugin_dir=plugin_dir, sources=sources,
                                           search_roots=search_roots, now=now,
                                           reason=f"新出现的 FAIL：{new_fail[0].get('title')}",
                                           log=log) is not None:
                        captured.append(now)
                        captured_now = True
                else:
                    log.write("新的 FAIL 未补完整报告（触发节流）：看 alerts.md 与样本即可")

            if args.once and not captured_now:
                capture_full_report(paths, plugin_dir=plugin_dir, sources=sources,
                                    search_roots=search_roots, now=now,
                                    reason="--once 单次采样", log=log)
            elif not args.once and report_every > 0 and now - next_report_at >= report_every:
                reason = ("启动时补当日报告" if next_report_at <= 0
                          else f"周期报告（每 {report_every / 3600:g} 小时）")
                if capture_full_report(paths, plugin_dir=plugin_dir, sources=sources,
                                       search_roots=search_roots, now=now, reason=reason,
                                       log=log) is not None:
                    next_report_at = now
                    state["report_failures"] = 0
                else:
                    # 静默失败是这个工具最不能接受的结果：报告没生成，而 watch.log 里
                    # 看起来一切正常。**不推进周期**（过一会儿重试）+ 计数进汇总表头。
                    state["report_failures"] = as_int(state.get("report_failures")) + 1
                    retry = max(60.0, min(report_every, 1800.0))
                    next_report_at = now - report_every + retry

            if now - last_prune_at >= 6 * 3600:
                prune_watch_dir(paths, retain_days=retain_days, now=now, log=log)
                last_prune_at = now

            # 报告生成之后再刷一次心跳（报告可能耗时几十秒）
            lock.heartbeat()
            offsets_state = tailer.to_state()
            if sources.log_paths:
                tracked = {str(path) for path in sources.log_paths}
                offsets_state = {key: value for key, value in offsets_state.items()
                                 if key in tracked}
            state.update({
                "updated_at": now, "last_sample_at": now,
                "heartbeat_iso": local_stamp(now, tz), "samples_total": index,
                "last_gap_seconds": round(gap, 1) if gap >= 0 else "",
                "data_dir": str(sources.data_dir) if sources.data_dir else "",
                "data_dir_source": sources.data_dir_source,
                "config_path": str(sources.config_path) if sources.config_path else "",
                "log_paths": [str(path) for path in sources.log_paths],
                "last_state_sha": prev_sha, "state_unchanged_samples": prev_unchanged,
                "alerts": tracker.to_state(), "log_offsets": offsets_state,
                "alert_events_total": as_int(state.get("alert_events_total")) + len(events),
                "captures": captured[-32:],
                "last_periodic_report_at": next_report_at, "last_prune_at": last_prune_at,
                "last_discovery_at": last_discovery,
                "first_sample_at": state.get("first_sample_at") or now,
                "last_status": row.get("check_status"), "last_note": row.get("note"),
                # v1.19.0（PR-AF-1 配套）：缓存配置原文，让汇总/分段能读
                # [diagnostics] 阈值（否则 --summary-only 时拿不到口径）
                "config": _load_toml(sources.config_path)[0] if sources.config_path else {},
            })
            save_watch_state(paths, state)

            active = tracker.active_alerts()
            log.write(f"#{index} {row.get('check_status')} "
                      f"活动={row.get('activity_label') or '—'} "
                      f"情绪={_fmt(row.get('emotion'))} 体力={_fmt(row.get('energy'))} "
                      f"tick={human_span(as_float(row.get('tick_age_seconds')) or 0) if row.get('tick_age_seconds') != '' else '—'} "
                      f"告警={len(active)}"
                      f"（FAIL {sum(1 for alert in active if alert.level == 'FAIL')}）"
                      + (f" 备注：{row.get('note')}" if row.get("check_status") not in ("ok", "") else ""))

            if summary_every > 0 and (last_summary_at == 0.0
                                      or now - last_summary_at >= summary_every):
                rows, broken = load_samples(paths)
                _atomic_write_text(paths.summary, render_watch_summary(
                    paths, rows=rows, summary=summarize_rows(rows, interval=interval),
                    tracker=tracker, state=state, tz=tz, now=now, interval=interval,
                    broken_lines=broken))
                last_summary_at = now

            if stop.stopped:
                stopped_reason = stop.reason or "收到停止请求"
                break
            if args.once:
                stopped_reason = "已完成单次采样（--once）"
                break
            if max_samples and index >= max_samples:
                stopped_reason = f"达到 --max-samples {max_samples}"
                break
            if duration_hours > 0 and now - campaign_started >= duration_hours * 3600:
                stopped_reason = (f"已达到目标时长 {duration_hours:g} 小时"
                                  "（按首次采样时间累计；想继续用 --duration-hours 0 或删 watch_state.json）")
                break

            next_wall = cycle_wall + interval
            while not stop.stopped and time.time() < next_wall:
                if paths.stop.exists():
                    stop.request(f"发现停止文件 {paths.stop.name}（处理完即退出）")
                    break
                time.sleep(min(5.0, max(0.05, next_wall - time.time())))
    except KeyboardInterrupt:  # 双保险：信号处理器没装上时也一样优雅退出
        stopped_reason = "KeyboardInterrupt"
    finally:
        final = "正常结束"
        try:
            rows, broken = load_samples(paths)
            state.update({"alerts": tracker.to_state(), "log_offsets": tailer.to_state(),
                          "updated_at": time.time(),
                          "stopped_reason": stopped_reason or "未知"})
            save_watch_state(paths, state)
            _atomic_write_text(paths.summary, render_watch_summary(
                paths, rows=rows, summary=summarize_rows(rows, interval=interval),
                tracker=tracker, state=state, tz=tz, now=time.time(), interval=interval,
                broken_lines=broken))
            active = tracker.active_alerts()
            fail_count = sum(1 for alert in active if alert.level == "FAIL")
            log.write(f"监控结束（{stopped_reason or '未知'}）：样本 {len(rows)} 个、"
                      f"未恢复告警 {len(active)} 条（FAIL {fail_count}）；"
                      f"汇总 {paths.summary}")
        except Exception as exc:  # noqa: BLE001 —— 收尾失败也必须放锁
            final = f"收尾失败：{exc.__class__.__name__}: {exc}"
            log.write(final)
        finally:
            lock.release()
            restore_signal_handlers(saved_handlers)
    unresolved = sum(1 for alert in tracker.active_alerts() if alert.level == "FAIL")
    # 只有「因为停止文件才停的」才删它：否则会把用户刚放下的停止文件悄悄吃掉
    # （下一次启动又得再放一次，而用户以为已经放过了）
    if "停止文件" in stopped_reason and paths.stop.exists():
        try:
            paths.stop.unlink()
        except OSError:
            pass
    return 1 if unresolved else 0


# ================================================================ 主流程


def bundle(output_dir: pathlib.Path, base_name: str, report_path: pathlib.Path,
           json_path: pathlib.Path, sources: Sequence[Source],
           inventory: Mapping[str, Any], plugin_dir: pathlib.Path,
           *, with_sources: bool) -> pathlib.Path:
    """把报告 + 现场数据打成一个 zip（供离线阅读）。

    ⚠ 必须排除输出目录本身：zip 文件在 ``zipfile.ZipFile(target, "w")`` 那一刻就存在了，
    不排除的话它会把自己写进自己（体积暴涨甚至死循环）。
    """

    target = (output_dir / f"{base_name}.zip").resolve()
    excluded = output_dir.resolve()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(report_path, report_path.name)
        archive.write(json_path, json_path.name)
        for source in sources:
            if not source.pack or source.path is None or not source.path.exists():
                continue
            path = source.path
            if path.is_file():
                if path.resolve() == target:
                    continue
                archive.write(path, f"data/{path.name}")
            elif path.is_dir():
                for item in path.rglob("*"):
                    if not item.is_file():
                        continue
                    if item.resolve() == target or item.is_relative_to(excluded):
                        continue
                    rel = item.relative_to(path).as_posix()
                    if any(part in SKIP_DIRS for part in rel.split("/")):
                        continue
                    archive.write(item, f"data/{path.name}/{rel}")
        if with_sources:
            for item in (inventory.get("plugin_files") or []):
                source_path = plugin_dir / item["path"]
                if source_path.exists() and source_path.resolve() != target:
                    archive.write(source_path, f"source/{item['path']}")
    return target


def harden_console_encoding() -> None:
    """把 Windows 控制台切到 UTF-8 代码页：强制 UTF-8 输出才不会在窗口里变成乱码。

    ⚠ ``stream.reconfigure(encoding="utf-8")`` 只保证**写出去的是 UTF-8 字节**；
    旧式控制台（默认代码页 936/GBK）会把这些字节按 GBK 解释 ⇒ 中文全花，
    而文件里其实是对的（用 VS Code / 记事本 / `Get-Content -Encoding UTF8` 看都正常）。
    只在**真的连着控制台**时切代码页：管道与重定向（写文件、给别的程序吃）不受影响，
    也不该去动它们的代码页。
    """

    if os.name != "nt":
        return
    try:
        if not (sys.stdout.isatty() or sys.stderr.isatty()):
            return
        import ctypes  # noqa: PLC0415 —— 只有 Windows 控制台需要

        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:  # noqa: BLE001 —— 切不了就算了，输出仍然是合法 UTF-8
        pass


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 默认控制台是 GBK：报告里全是中文，不加固的话摘要行会变成乱码
    # （run_gates.py 里踩过同一个坑，这里沿用同一套 workaround）。
    harden_console_encoding()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 —— 重定向/老解释器时静默跳过
            pass

    parser = argparse.ArgumentParser(
        description="life-frequency 数据采集与运行诊断（独立程序，只读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--plugin-dir", default=str(pathlib.Path(__file__).resolve().parent),
                        help="插件代码目录（默认：本程序所在目录）")
    parser.add_argument("--data-dir", default="", help="运行时数据目录（life_state.json 所在处）")
    parser.add_argument("--config", default="", help="宿主落盘的 config.toml")
    parser.add_argument("--log", action="append", default=[],
                        help="日志文件（可多次；不传则自动在 --search-root 下找）")
    parser.add_argument("--search-root", action="append", default=[],
                        help="自动搜索数据的根目录（可多次；默认插件目录与常见 MaiBot 位置）")
    parser.add_argument("--offline-simulate", type=float, default=24.0,
                        help="无真机数据时跑几小时离线引擎自检（0 = 不跑，默认 24）")
    parser.add_argument("--run-gates", action="store_true",
                        help="额外执行插件自带的 run_gates.py（较慢，会跑 pytest）")
    parser.add_argument("--out-dir", default="", help="报告输出目录（默认 <插件目录>/diagnostics）")
    parser.add_argument("--bundle", action="store_true", help="额外打包 zip")
    parser.add_argument("--with-sources", action="store_true", help="打包时把源码也放进去")
    # ---- 长期监控（见文件开头的「长期监控」一节）----
    watch_group = parser.add_argument_group("长期监控")
    watch_group.add_argument("--watch", action="store_true",
                             help="常驻监控：按 --interval 采样，直到 --duration-hours 用尽或被停止")
    watch_group.add_argument("--once", action="store_true",
                             help="只采一个样本 + 落一份完整报告（手动看 / 计划任务用）")
    watch_group.add_argument("--summary-only", action="store_true",
                             help="不采样，只用已有样本重算 watch_summary.md")
    watch_group.add_argument("--interval", type=float, default=DEFAULT_WATCH_INTERVAL,
                             help=f"采样间隔秒数（默认 {DEFAULT_WATCH_INTERVAL:g}，最小 1）")
    watch_group.add_argument("--duration-hours", type=float, default=DEFAULT_WATCH_HOURS,
                             help=f"累计监控时长（默认 {DEFAULT_WATCH_HOURS:g} 小时 = 7 天；"
                                  "0 = 不限）")
    watch_group.add_argument("--max-samples", type=int, default=0,
                             help="采够多少个样本就停（0 = 不限；测试与短程观察用）")
    watch_group.add_argument("--watch-dir", default="",
                             help="监控产物目录（默认 <插件目录>/diagnostics/watch）")
    watch_group.add_argument("--retain-days", type=float, default=DEFAULT_RETAIN_DAYS,
                             help=f"样本与历史报告保留天数（默认 {DEFAULT_RETAIN_DAYS:g}；0 = 不裁）")
    watch_group.add_argument("--report-every-hours", type=float, default=24.0,
                             help="多久落一份完整诊断报告（默认 24 小时；0 = 只在启动/新 FAIL 时）")
    watch_group.add_argument("--summary-every-minutes", type=float, default=60.0,
                             help="多久重算一次汇总报告（默认 60 分钟；0 = 只在启动与结束时）")
    watch_group.add_argument("--no-lock", action="store_true",
                             help="跳过单实例锁（默认拒绝与已有监控进程并发写同一目录）")
    watch_group.add_argument("--force-lock", action="store_true",
                             help="强行接管锁（确认上一个监控进程已经死了再用）")
    watch_group.add_argument("--quiet", action="store_true", help="不往控制台打日志（仍写 watch.log）")
    watch_group.add_argument("--mark", default="",
                             help="v1.19.0：给长期监控打一个「改动时点」标记并立即退出"
                                  "（部署重启后调用，如 --mark \"改清醒侧判据\"）；"
                                  "汇总会按标记把样本分段统计，改前/改后不再混在一起")
    args = parser.parse_args(argv)

    plugin_dir = pathlib.Path(args.plugin_dir).resolve()
    if not plugin_dir.is_dir():
        print(f"插件目录不存在：{plugin_dir}", file=sys.stderr)
        return 2
    now = time.time()
    search_roots = [pathlib.Path(item).resolve() for item in args.search_root]
    if not search_roots:
        search_roots = base_search_roots(plugin_dir)
    output_dir = pathlib.Path(args.out_dir).resolve() if args.out_dir else plugin_dir / "diagnostics"

    if args.watch or args.once or args.summary_only or args.mark:
        return run_watch(args, plugin_dir=plugin_dir, search_roots=search_roots,
                         base_output_dir=output_dir)

    report = build_report(
        plugin_dir,
        data_dir=pathlib.Path(args.data_dir).resolve() if args.data_dir else None,
        config_path=pathlib.Path(args.config).resolve() if args.config else None,
        log_paths=[pathlib.Path(item).resolve() for item in args.log],
        search_roots=search_roots,
        simulate_hours=max(0.0, float(args.offline_simulate)),
        run_gates=bool(args.run_gates),
        now=now,
        out_dir=output_dir,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name = f"life-frequency-诊断-{stamp}"
    report_path = output_dir / f"{base_name}.md"
    json_path = output_dir / f"{base_name}.json"
    body = dict(report.sections)["REPORT"]
    report_path.write_text(body, encoding="utf-8")
    json_path.write_text(json.dumps({
        "machine": report.machine,
        "counts": report.counts(),
        "findings": [item.to_dict() for item in report.findings],
        "sources": [{"kind": s.kind, "path": str(s.path) if s.path else "",
                     "note": s.note, "simulated": s.simulated} for s in report.sources],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    counts = report.counts()
    headline, _detail = verdict(report, bool(report.machine["details"].get("state")),
                               bool(report.machine["details"].get("simulation")))
    print(f"总判定：{headline}")
    print(f"检查项：FAIL {counts['FAIL']}  WARN {counts['WARN']}  "
          f"UNKNOWN {counts['UNKNOWN']}  OK {counts['OK']}")
    for finding in report.findings:
        if finding.level in ("FAIL", "WARN"):
            print(f"  [{finding.level}] [{finding.scope}] {finding.title}"
                  + (f" —— {finding.evidence}" if finding.evidence else ""))
    print(f"报告：{report_path}")
    print(f"JSON：{json_path}")
    if args.bundle:
        archive = bundle(output_dir, base_name, report_path, json_path, report.sources,
                         report.machine["details"].get("inventory") or {}, plugin_dir,
                         with_sources=bool(args.with_sources))
        print(f"打包：{archive}")
    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
