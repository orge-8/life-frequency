#!/usr/bin/env python3
"""MaiBot 插件结构自检（零第三方依赖，Python 3.10+）。

用法:
    python check_plugin.py [--plugin <插件目录>] [--json]

退出码: 0 = 无 FAIL；1 = 存在 FAIL（不允许交付）。

检查项覆盖: 结构 / manifest 规则 / 生命周期 / 配置模型 / 组件装饰器配对 /
能力声明一致性 / 安全与卫生。规则基线: MaiBot Host 1.3.1 + maibot_sdk 2.8.1（本机源码实测）。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys


def harden_stdio() -> None:
    """把 stdout/stderr 切成 UTF-8 + errors=replace。

    Windows 默认控制台是 GBK，报告里的 ✓/✗ 等符号或替换字符会直接抛
    UnicodeEncodeError，让自检结果打印不全。这里先兜住。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — 老解释器/被重定向时静默跳过
            pass


MANIFEST_ALLOWED_KEYS = {
    "manifest_version", "id", "version", "name", "description", "author",
    "license", "urls", "host_application", "sdk", "dependencies",
    "capabilities", "i18n", "plugin_type", "llm_providers", "display",
    "changelog",
}
ID_RE = re.compile(r"^[A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)+$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
CTX_CALL_RE = re.compile(r"self\.ctx\.([A-Za-z_][A-Za-z0-9_.]*?)\(")
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "疑似 OpenAI/兼容端点 Key 硬编码"),
    (re.compile(r"(?i)(api[_-]?key|access[_-]?token|secret)\s*[:=]\s*[\"'][A-Za-z0-9_\-]{12,}[\"']"),
     "疑似凭据硬编码"),
    (re.compile(r"SESSDATA\s*=\s*[\"'][A-Za-z0-9%]{10,}"), "B 站 SESSDATA 硬编码"),
]

# ctx 调用 -> 需要声明的能力名（74 个能力名的常用子集，未收录的调用会 WARN）
CAPABILITY_MAP = {
    "send.text": "send.text", "send.image": "send.image", "send.emoji": "send.emoji",
    "send.forward": "send.forward", "send.hybrid": "send.hybrid",
    "send.command": "send.command", "send.custom": "send.custom",
    "llm.generate": "llm.generate", "llm.generate_with_tools": "llm.generate_with_tools",
    "llm.embed": "llm.embed", "llm.transcribe_audio": "llm.transcribe_audio",
    "llm.get_available_models": "llm.get_available_models",
    "config.get": "config.get", "config.get_plugin": "config.get_plugin",
    "config.get_all": "config.get_all",
    "db.query": "database.query", "db.save": "database.save", "db.get": "database.get",
    "db.delete": "database.delete", "db.count": "database.count",
    "chat.get_all_streams": "chat.get_all_streams",
    "chat.get_group_streams": "chat.get_group_streams",
    "chat.get_private_streams": "chat.get_private_streams",
    "chat.open_session": "chat.open_session",
    "chat.get_stream_by_group_id": "chat.get_stream_by_group_id",
    "chat.get_stream_by_user_id": "chat.get_stream_by_user_id",
    "message.get_by_time": "message.get_by_time",
    "message.get_by_time_in_chat": "message.get_by_time_in_chat",
    "message.get_by_id": "message.get_by_id",
    "message.get_recent": "message.get_recent",
    "message.count_new": "message.count_new",
    "message.build_readable": "message.build_readable",
    "maisaka.context.append": "maisaka.context.append",
    "maisaka.proactive.trigger": "maisaka.proactive.trigger",
    "person.get_id": "person.get_id", "person.get_value": "person.get_value",
    "person.get_id_by_name": "person.get_id_by_name",
    "emoji.get_by_description": "emoji.get_by_description",
    "emoji.get_random": "emoji.get_random", "emoji.get_count": "emoji.get_count",
    "emoji.get_emotions": "emoji.get_emotions", "emoji.get_all": "emoji.get_all",
    "emoji.get_info": "emoji.get_info", "emoji.register": "emoji.register",
    "emoji.delete": "emoji.delete",
    "frequency.get_current_talk_value": "frequency.get_current_talk_value",
    "frequency.set_adjust": "frequency.set_adjust",
    "frequency.get_adjust": "frequency.get_adjust",
    "tool.get_definitions": "tool.get_definitions",
    "api.call": "api.call", "api.get": "api.get", "api.list": "api.list",
    "component.get_all_plugins": "component.get_all_plugins",
    "component.get_plugin_info": "component.get_plugin_info",
    "component.get_plugin_config_schema": "component.get_plugin_config_schema",
    "component.update_plugin_config": "component.update_plugin_config",
    "component.list_loaded_plugins": "component.list_loaded_plugins",
    "component.list_registered_plugins": "component.list_registered_plugins",
    "component.enable": "component.enable", "component.disable": "component.disable",
    "component.load_plugin": "component.load_plugin",
    "component.unload_plugin": "component.unload_plugin",
    "component.reload_plugin": "component.reload_plugin",
    "knowledge.search": "knowledge.search",
    "render.html2png": "render.html2png",
}
FREE_CAPABILITIES = {"api.replace_dynamic"}
NON_CAPABILITY_PROXIES = {"paths", "logger", "call_capability", "call_host_method"}
"""不是能力代理的名字。

`paths` / `logger` 是辅助对象；`call_capability` / `call_host_method` 是**动态分发器**
——能力名在运行时参数里传，静态推不出来，因此不能按 `self.ctx.X(...)` 反推能力，
也不该报「未能映射」。
"""
STATISTICS_PREFIX = "statistics.local."


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, check: str, detail: str) -> None:
        self.rows.append((level, check, detail))

    def ok(self, check: str, detail: str = "") -> None:
        self.add("PASS", check, detail)

    def warn(self, check: str, detail: str) -> None:
        self.add("WARN", check, detail)

    def fail(self, check: str, detail: str) -> None:
        self.add("FAIL", check, detail)

    def counts(self) -> dict[str, int]:
        c = {"PASS": 0, "WARN": 0, "FAIL": 0}
        for level, _, _ in self.rows:
            c[level] += 1
        return c


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def check_structure(plugin_dir: str, rep: Report) -> tuple[dict | None, str | None]:
    manifest_path = os.path.join(plugin_dir, "_manifest.json")
    plugin_path = os.path.join(plugin_dir, "plugin.py")
    if os.path.isfile(manifest_path):
        rep.ok("结构/_manifest.json", "存在")
    else:
        rep.fail("结构/_manifest.json", "缺失（Runner 要求 _manifest.json 与 plugin.py 同时存在）")
        return None, None
    if os.path.isfile(plugin_path):
        rep.ok("结构/plugin.py", "存在")
    else:
        rep.fail("结构/plugin.py", "缺失")
        return None, None
    try:
        manifest = json.loads(read_text(manifest_path))
    except json.JSONDecodeError as exc:
        rep.fail("manifest/JSON 合法", f"解析失败: {exc}")
        return None, read_text(plugin_path)
    rep.ok("manifest/JSON 合法", "可解析")
    return manifest, read_text(plugin_path)


def check_manifest(manifest: dict, rep: Report) -> list[str]:
    caps: list[str] = []
    if manifest.get("manifest_version") == 2:
        rep.ok("manifest/manifest_version", "= 2")
    else:
        rep.fail("manifest/manifest_version", f"必须为 2，当前 {manifest.get('manifest_version')!r}")

    pid = str(manifest.get("id", ""))
    if ID_RE.match(pid):
        rep.ok("manifest/id", pid)
    else:
        rep.fail("manifest/id", f"{pid!r} 不合法：需匹配 ^[A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)+$（必须含 . 或 -）")

    ver = str(manifest.get("version", ""))
    if VERSION_RE.match(ver):
        rep.ok("manifest/version", f"{ver}（三段式）")
    else:
        rep.fail("manifest/version", f"{ver!r} 必须为严格三段式 X.Y.Z")

    for field in ("name", "description", "license"):
        if str(manifest.get(field, "")).strip():
            rep.ok(f"manifest/{field}", "非空")
        else:
            rep.fail(f"manifest/{field}", "缺失或为空")

    author = manifest.get("author") or {}
    if isinstance(author, dict) and str(author.get("name", "")).strip():
        rep.ok("manifest/author.name", "非空")
    else:
        rep.fail("manifest/author.name", "缺失")
    if isinstance(author, dict) and str(author.get("url", "")).startswith(("http://", "https://")):
        rep.ok("manifest/author.url", author["url"])
    else:
        rep.fail("manifest/author.url", "必须为 http(s) URL")

    urls = manifest.get("urls") or {}
    if isinstance(urls, dict) and str(urls.get("repository", "")).startswith(("http://", "https://")):
        rep.ok("manifest/urls.repository", urls["repository"])
    else:
        rep.fail("manifest/urls.repository", "必填且必须为 http(s) URL")

    for field in ("host_application", "sdk"):
        spec = manifest.get(field) or {}
        if not isinstance(spec, dict) or not spec.get("min_version") or not spec.get("max_version"):
            rep.fail(f"manifest/{field}", "需同时给出 min_version 与 max_version")
            continue
        mn, mx = str(spec["min_version"]), str(spec["max_version"])
        if not (VERSION_RE.match(mn) and VERSION_RE.match(mx)):
            rep.fail(f"manifest/{field}", f"{mn} / {mx} 必须三段式")
        else:
            rep.ok(f"manifest/{field}", f"{mn} ~ {mx}")
    ha = manifest.get("host_application") or {}
    if isinstance(ha, dict) and str(ha.get("max_version", "")).startswith("1.") and \
            str(ha.get("max_version", "")).split(".")[1] < "2":
        rep.warn("manifest/host_application", f"max_version={ha.get('max_version')} 低于当前 Host 1.x，"
                                              "通用插件建议 1.99.99")

    raw_caps = manifest.get("capabilities")
    if isinstance(raw_caps, list) and raw_caps and all(isinstance(c, str) and c.strip() for c in raw_caps):
        caps = [c.strip() for c in raw_caps]
        if len(set(caps)) != len(caps):
            rep.warn("manifest/capabilities", "存在重复项（Runner 要求去重）")
        else:
            rep.ok("manifest/capabilities", f"{len(caps)} 项")
    else:
        rep.fail("manifest/capabilities", "必须是非空字符串数组")

    extra = sorted(set(manifest.keys()) - MANIFEST_ALLOWED_KEYS)
    if extra:
        rep.fail("manifest/无多余字段", f"schema 外字段（extra=forbid）: {', '.join(extra)}")
    else:
        rep.ok("manifest/无多余字段", "OK")

    deps = manifest.get("dependencies", [])
    if not isinstance(deps, list):
        # 真实宿主 manifest_validator 是 List[ManifestDependencyDefinition] + extra=forbid，
        # 写成字符串/字典会被拒；以前这里整个分支静默跳过（一行结论都没有）。
        rep.fail("manifest/dependencies", f"必须是数组，当前是 {type(deps).__name__}")
    elif not deps:
        rep.ok("manifest/dependencies", "无依赖")
    else:
        ok = True
        for dep in deps:
            if not isinstance(dep, dict):
                rep.fail("manifest/dependencies", f"依赖项必须是对象: {dep!r}")
                ok = False
                continue
            if dep.get("type") == "plugin" and not dep.get("id"):
                rep.fail("manifest/dependencies", f"plugin 依赖缺少 id: {dep}")
                ok = False
        if ok:
            rep.ok("manifest/dependencies", f"{len(deps)} 条")
    return caps


def _decorator_component_name(node: ast.expr) -> tuple[str | None, str | None]:
    """返回 (装饰器名, 期望组件名)。期望组件名为 None 表示无法静态判定。"""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None, None
    deco = node.func.id
    if deco not in {"Tool", "Command", "HookHandler", "API", "MessageGateway", "HomeCard"}:
        return None, None
    if deco in {"Tool", "Command", "API", "HomeCard"}:
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            return deco, node.args[0].value
        return deco, None
    # HookHandler / MessageGateway: 组件名在 name= 关键字里
    for kw in node.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return deco, kw.value.value
    return deco, None


def _name_related(expected: str, actual: str) -> bool:
    """装饰器声明的组件名与紧随其后的函数名是否相关。

    约定形态: ping -> ping / cmd_ping / ping_command。完全不相关时大概率是
    辅助方法插队（真机报 unexpected keyword argument 的经典成因）。

    归一化保留 Unicode 字母数字，否则中文组件名（点歌 / 选歌 / 动态…）会被
    整体抹掉，导致 `@Command("点歌") def cmd_点歌` 这种正确写法永远只能出警告。
    """
    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    e, a = norm(expected), norm(actual)
    return bool(e) and bool(a) and (a == e or a.endswith(e) or e.endswith(a))


def _function_decorators(tree: ast.AST):
    """产出 (装饰器名, 期望组件名, 实际函数名, 行号)。"""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            name, expected = _decorator_component_name(deco)
            if name:
                yield name, expected, node.name, node.lineno


def _has_config_version_field(tree: ast.AST) -> bool:
    """AST 级判断配置模型里是否真的**声明了** ``config_version`` 字段。

    不能用文本匹配：``re.search(r"config_version\\s*[:=]", source)`` 会被一行注释
    或任意一处字符串字面量满足——把所有同名字段改名、只在文件头留一行
    ``# config_version: 1.0.0``，旧检查照样报 PASS（真机契约其实是 SDK 强制的
    ``PluginConfigVersionError``）。这里要求类体里存在
    ``config_version = ...`` 或 ``config_version: str = ...``。
    """

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign):
                if getattr(stmt.target, "id", None) == "config_version":
                    return True
            elif isinstance(stmt, ast.Assign):
                if any(getattr(tgt, "id", None) == "config_version" for tgt in stmt.targets):
                    return True
    return False


def _has_future_annotations(tree: ast.AST) -> bool:
    """AST 级判断是否真的 import 了 `__future__.annotations`。

    不能用文本匹配：文档字符串里提到这条禁令（很常见，插件作者会写"不要加
    from __future__ import annotations"）会被误判成违规。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(alias.name == "annotations" for alias in node.names):
                return True
    return False


def _has_os_path_dirname_call(tree: ast.AST) -> bool:
    """AST 级判断是否真的调用了 `os.path.dirname(...)`。

    同样不能用文本匹配：插件作者在文档字符串里写"不要用
    os.path.dirname(__file__) 拼路径"本身是正确做法，文本匹配会把它误报成违规。
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "dirname"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os"
        ):
            return True
    return False


def check_source(source: str, caps: list[str], rep: Report) -> None:
    if re.search(r"^\s*import\s+src\.|^\s*from\s+src\.", source, re.M):
        rep.fail("源码/禁止 import src", "插件只允许导入标准库、第三方库与 maibot_sdk")
    else:
        rep.ok("源码/禁止 import src", "OK")

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        rep.fail("源码/语法", f"AST 解析失败: {exc}")
        return
    rep.ok("源码/语法", "可解析")

    # 放在 AST 解析之后：必须用 AST 判断，不能文本匹配
    # （文档字符串里提到这条禁令很常见，文本匹配会误报）
    if _has_future_annotations(tree):
        rep.fail("源码/__future__ annotations", "plugin.py 禁止写 from __future__ import annotations"
                                                "（会让 pydantic 解析配置模型失败）")
    else:
        rep.ok("源码/__future__ annotations", "未使用")

    func_names = {n.name for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for required in ("create_plugin", "on_load", "on_unload", "on_config_update"):
        if required in func_names:
            rep.ok(f"生命周期/{required}", "存在")
        else:
            rep.fail(f"生命周期/{required}", "缺失（Runner 加载时调用失败即插件加载失败）")

    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    plugin_cls = next((c for c in classes
                       if any(isinstance(b, ast.Name) and b.id == "MaiBotPlugin" for b in c.bases)), None)
    if plugin_cls is None:
        rep.fail("源码/插件类", "未找到继承 MaiBotPlugin 的类")
    else:
        rep.ok("源码/插件类", plugin_cls.name)
        methods = {n.name for n in plugin_cls.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        missing = [m for m in ("on_load", "on_unload", "on_config_update") if m not in methods]
        if missing:
            rep.fail("源码/生命周期在插件类内", f"缺少 {', '.join(missing)}")
        else:
            rep.ok("源码/生命周期在插件类内", "三个方法齐全")
        has_model = False
        for n in plugin_cls.body:
            if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == "config_model":
                has_model = True
                break
            if isinstance(n, ast.Assign):
                for tgt in n.targets:
                    if getattr(tgt, "id", None) == "config_model":
                        has_model = True
                        break
                if has_model:
                    break
        if has_model:
            rep.ok("源码/config_model", "已声明")
        else:
            rep.fail("源码/config_model", "插件类未声明 config_model（Runner 无法生成默认配置）")

    if _has_config_version_field(tree):
        rep.ok("源码/config_version", "配置模型含 config_version 字段")
    else:
        rep.fail(
            "源码/config_version",
            "配置模型里没有 config_version 字段（1.2.3 硬性要求，缺失即加载失败）",
        )

    # 装饰器-函数配对
    pairs = list(_function_decorators(tree))
    if not pairs:
        rep.warn("源码/组件装饰器", "未发现任何组件装饰器")
    for deco, expected, actual, lineno in pairs:
        if expected is None:
            rep.warn(f"源码/@{deco} 配对", f"第 {lineno} 行无法静态判定组件名，人工确认紧跟的 def 是 {actual}")
        elif _name_related(expected, actual):
            rep.ok(f"源码/@{deco} 配对", f"{expected} -> def {actual} (L{lineno})")
        else:
            rep.warn(f"源码/@{deco} 配对",
                     f"第 {lineno} 行：装饰器声明 {expected!r}，紧随的 def 是 {actual!r}，二者名称无关 —— "
                     "确认此处不是辅助方法插队（插队会静默把组件注册到辅助方法上）")

    # 能力声明一致性
    used: set[str] = set()
    unknown: set[str] = set()
    for call in CTX_CALL_RE.findall(source):
        proxy = call
        if proxy.split(".")[0] in NON_CAPABILITY_PROXIES:
            continue
        if proxy.startswith(STATISTICS_PREFIX):
            used.add(proxy)
            continue
        mapped = CAPABILITY_MAP.get(proxy)
        if mapped:
            used.add(mapped)
        else:
            unknown.add(proxy)
    declared = set(caps)
    missing = sorted(used - declared - FREE_CAPABILITIES)
    if missing:
        rep.fail("能力/声明完整性",
                 f"源码用到但未声明: {', '.join(missing)}（补完必须完整重启 MaiBot）")
    else:
        rep.ok("能力/声明完整性", f"源码用到的 {len(used)} 项均已声明")
    unused = sorted(declared - used - FREE_CAPABILITIES)
    if unused:
        rep.warn("能力/无多余声明", f"声明了但源码未用到: {', '.join(unused)}")
    else:
        rep.ok("能力/无多余声明", "OK")
    if unknown:
        rep.warn("能力/映射表", f"未能映射的 ctx 调用（人工核对能力名）: {', '.join(sorted(unknown))}")
    if "api.call" in used and "api.call" not in declared:
        rep.warn("能力/api.call", "官方手册标注免声明，实测 Host 1.2.3 仍按 manifest 授权 —— 必须显式声明")

    # 安全与卫生
    hits = []
    for pattern, desc in SECRET_PATTERNS:
        if pattern.search(source):
            hits.append(desc)
    if hits:
        rep.fail("安全/凭据硬编码", "；".join(hits))
    else:
        rep.ok("安全/凭据硬编码", "未发现")

    if _has_os_path_dirname_call(tree):
        rep.warn("安全/路径绕出", "调用了 os.path.dirname(...)：持久化请直接用 self.ctx.paths.data_dir")
    else:
        rep.ok("安全/路径绕出", "OK")


def check_hygiene(plugin_dir: str, rep: Report) -> None:
    gi = os.path.join(plugin_dir, ".gitignore")
    if os.path.isfile(gi):
        content = read_text(gi)
        if re.search(r"^\s*/?config\.toml\s*$", content, re.M):
            rep.ok("卫生/.gitignore", "含 /config.toml")
        else:
            rep.fail("卫生/.gitignore", "缺少 /config.toml（运行时配置由 Runner 生成，不应入库）")
    else:
        rep.warn("卫生/.gitignore", "文件不存在")

    cfg = os.path.join(plugin_dir, "config.toml")
    if os.path.isfile(cfg):
        with open(cfg, "rb") as fh:
            head = fh.read(3)
        if head == b"\xef\xbb\xbf":
            rep.fail("卫生/config.toml BOM", "带 UTF-8 BOM 会导致 TOML Invalid statement"
                                             "（PowerShell Set-Content -Encoding UTF8 会写 BOM）")
        else:
            rep.ok("卫生/config.toml BOM", "无 BOM")
    else:
        rep.ok("卫生/config.toml", "不存在（由 Runner 生成，符合预期）")

    readme = os.path.join(plugin_dir, "README.md")
    if os.path.isfile(readme):
        content = read_text(readme)
        need = ["安装", "配置", "命令", "故障排查"]
        lack = [k for k in need if k not in content]
        if lack:
            rep.warn("卫生/README", f"建议补齐章节: {', '.join(lack)}")
        else:
            rep.ok("卫生/README", "含安装/配置/命令/故障排查")
    else:
        rep.warn("卫生/README", "缺失（建议含安装、启用、配置、命令、权限、故障排查）")


def derive_capabilities(source: str) -> list[str]:
    """从源码反推应声明的能力名（供 scaffold 生成 manifest 使用）。"""
    used: set[str] = set()
    for call in CTX_CALL_RE.findall(source):
        if call.split(".")[0] in NON_CAPABILITY_PROXIES:
            continue
        if call.startswith(STATISTICS_PREFIX):
            used.add(call)
            continue
        mapped = CAPABILITY_MAP.get(call)
        if mapped:
            used.add(mapped)
    return sorted(used)


def run(plugin_dir: str) -> Report:
    rep = Report()
    manifest, source = check_structure(plugin_dir, rep)
    caps: list[str] = []
    if manifest is not None:
        caps = check_manifest(manifest, rep)
    if source is not None:
        check_source(source, caps, rep)
    check_hygiene(plugin_dir, rep)
    return rep


def main() -> int:
    harden_stdio()
    ap = argparse.ArgumentParser(description="MaiBot 插件结构自检")
    ap.add_argument("--plugin", default=".", help="插件目录（默认当前目录）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    plugin_dir = os.path.abspath(args.plugin)
    rep = run(plugin_dir)

    if args.json:
        print(json.dumps({"plugin": plugin_dir,
                          "rows": [{"level": l, "check": c, "detail": d} for l, c, d in rep.rows],
                          "counts": rep.counts()}, ensure_ascii=False, indent=2))
    else:
        print(f"插件目录: {plugin_dir}")
        print("-" * 78)
        print(f"{'级别':<6}{'检查项':<28}{'详情'}")
        print("-" * 78)
        for level, check, detail in rep.rows:
            print(f"{level:<6}{check:<28}{detail}")
        print("-" * 78)
        c = rep.counts()
        print(f"PASS {c['PASS']}  WARN {c['WARN']}  FAIL {c['FAIL']}")
    return 1 if rep.counts()["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
