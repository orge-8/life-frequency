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

**只读承诺**：本程序不改动插件目录与运行时数据（只往输出目录写报告）。读 SQLite 走
只读连接，连不上时**复制**一份到临时目录再读，绝不碰原库。

它不依赖插件是否装了 SDK：优先用插件自带的**纯模块**（``life_sim`` 等，无 ctx、无 IO）
做权威校验（例如用 ``LifeState.from_dict`` 真解析状态文件），导入失败则退回内置的最小
校验器，并在报告里注明「未使用插件代码校验」。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

PROGRAM_VERSION = "1.0.0"
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
    conn, _note, _used = _open_readonly(path)
    if conn is None:
        return False
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    except sqlite3.Error:
        return False
    finally:
        conn.close()
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

    if bool(frequency.get("paused", False)):
        report.add("FAIL", "插件处于暂停状态（[frequency] paused = true）",
                   evidence="生活照常推进，但倍率归还外部基数——她不会因为生活状态而变安静/变吵",
                   fix="恢复：`/生活 恢复`（或把 paused 改回 false）", scope="配置")

    raw_window = str(simulation.get("sleep_window") or "").strip()
    detail["sleep_window"] = raw_window
    if raw_window:
        match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", raw_window)
        if not match:
            report.add("WARN", "sleep_window 无法解析（会回退默认 03:00-11:00）",
                       evidence=raw_window, fix='写成 "HH:MM-HH:MM"', scope="配置")
        else:
            start = int(match.group(1)) * 60 + int(match.group(2))
            end = int(match.group(3)) * 60 + int(match.group(4))
            span = (end - start) % 1440
            if span == 0:
                report.add("WARN", "sleep_window 是零长度窗口（等于没有睡眠时段）",
                           evidence=raw_window, fix='例如 "03:00-11:00"', scope="配置")
            elif span >= 20 * 60:
                report.add("FAIL", f"sleep_window 覆盖一天中的 {span / 60:.1f} 小时"
                                   "（≈ 全天）",
                           evidence=f"{raw_window}：她几乎**随时**都有资格入睡，"
                                    "清醒时段会被睡眠吃掉（真机实测：活动长期是 sleep）",
                           fix='改成真实作息，例如 "03:00-11:00"', scope="配置")
            elif span >= 16 * 60:
                report.add("WARN", f"sleep_window 覆盖一天中的 {span / 60:.1f} 小时",
                           evidence=f"{raw_window}（她在窗口内会被判定为「该睡」）",
                           fix="确认这是有意的（过长会把大部分活动挤成睡眠）", scope="配置")

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
        report.add("WARN", "演算模式开着（dry_run = true）",
                   evidence="只算不写：宿主上的倍率不会被改",
                   fix="确认这是调试用；正常运转要关掉", scope="配置")

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
                   "sleep_debt_nights": as_int(state.get("sleep_debt_nights")),
                   "stress": as_float(state.get("stress")),
                   "loneliness": as_float(state.get("loneliness")),
                   "social_battery": as_float(state.get("social_battery"))})
    if energy > cap + 1e-6:
        report.add("WARN", f"{label}：体力超过上限", evidence=f"{energy:.2f} > {cap:.2f}",
                   fix="正常不会发生（settle 会钳）；确认状态文件没被手改", scope="状态")
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
                       fix="看日志有没有「强制入睡」；确认 sleep_window 与睡眠阈值配置合理",
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
        dry_run = bool(_section(config, "simulation").get("dry_run", False))
        report.add("WARN", f"{label}：还没有在任何会话上写过倍率",
                   evidence=("与 [simulation] dry_run = true 互相印证：演算模式从不写宿主"
                             if dry_run else
                             "可能是没命中会话（filter_mode / only_active_sessions），"
                             "或一直处于静默/睡眠"),
                   fix="用 `/生活 状态` 看「本轮命中几个会话」"
                       + ("；确认演算模式是不是调试用" if dry_run else ""),
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
    windows = {
        "被 @ 唤醒窗口": as_float(state.get("at_wake_until"), 0.0) or 0.0,
        "赖床宽限窗": as_float(state.get("wake_grace_until"), 0.0) or 0.0,
        "打断窗口": as_float(state.get("interrupt_until"), 0.0) or 0.0,
        "躺够一轮的抑制": as_float(state.get("rested_until"), 0.0) or 0.0,
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


def _open_readonly(db_path: pathlib.Path) -> tuple[sqlite3.Connection | None, str, pathlib.Path]:
    """只读打开 SQLite；直连失败（例如 WAL 需要恢复）时**复制一份**再读。"""

    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn, "", db_path
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
            return conn, f"直连只读失败（{first}），已复制到临时目录读取", copy_path
        except sqlite3.Error as second:
            return None, f"只读与复制读取都失败：{first} / {second}", copy_path


def collect_store(report: Report, path: pathlib.Path | None, *, now: float,
                  tz: int, pure: "PurePlugin") -> dict[str, Any]:
    if path is None or not path.exists():
        report.add("UNKNOWN", "未找到 life_store.db（习惯日态与关系档案）",
                   evidence="没有它不影响生活循环（会降级成内存态），但看不出关系/习惯在不在跑",
                   fix="用 --data-dir 指向插件的 data 目录", scope="库")
        return {}
    conn, note, used_path = _open_readonly(path)
    detail: dict[str, Any] = {
        "path": str(path), "size_bytes": path.stat().st_size,
        "sha256": sha256_of(path)[:16], "note": note,
        "wal_present": pathlib.Path(str(path) + "-wal").exists(),
    }
    if conn is None:
        report.add("FAIL", "life_store.db 读不出来", evidence=note,
                   fix="库损坏时插件会降级成内存态（习惯每天会重复命中）；备份后删除重建",
                   scope="库")
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
            for needle, _why in LOG_FAIL_SIGNATURES:
                if needle in line:
                    fail_hits[needle] = fail_hits.get(needle, 0) + 1
            for needle, _why in LOG_WARN_SIGNATURES:
                if needle in line:
                    warn_hits[needle] = warn_hits.get(needle, 0) + 1
            if level_match and key in ("ERROR", "CRITICAL") and len(recent_errors) < 8:
                recent_errors.append(redact(line, limit=240))
        entry = {
            "path": str(path), "size_bytes": path.stat().st_size,
            "total_lines": len(lines), "plugin_lines": len(mine),
            "levels": levels, "fail_signatures": fail_hits, "warn_signatures": warn_hits,
            "recent_errors": recent_errors, "newest_stamp": newest_stamp,
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
                       fix="看「最近错误」与状态卡（`/生活 状态`、`/生活 为什么`）",
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
                    now_minutes=facts.now_minutes, energy=facts.energy,
                    sleep_energy_threshold=config_obj.sleep_energy_threshold,
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
        "sleep_window": _section(config_payload, "simulation").get("sleep_window"),
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
        lines.append(f"| 熬夜债 | {state.get('sleep_debt_nights')} 晚 |")
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


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 默认控制台是 GBK：报告里全是中文，不加固的话摘要行会变成乱码
    # （run_gates.py 里踩过同一个坑，这里沿用同一套 workaround）。
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
