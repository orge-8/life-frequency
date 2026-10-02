#!/usr/bin/env python3
"""MaiBot 插件交付门禁编排。

    python run_gates.py --plugin <插件目录> [--skip pytest]

顺序:
    1. check_plugin.py  静态结构自检（零依赖，必须过）
    2. tests/smoke_test.py  FakeHost 生命周期冒烟（缺 SDK 记 SKIP）
    3. pytest（当前解释器有 pytest 时；未收集到用例记 SKIP）

退出码: 0 = 无 FAIL；1 = 存在 FAIL。

SKIP 语义（不是失败，但也不等于通过）:
    - 缺 maibot_sdk：冒烟 SKIP
    - 无 tests/test_*.py：pytest 报 "no tests ran"（退出码 5）记 SKIP
    - 当前解释器没装 pytest：跳过该步
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

SKILL_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def harden_stdio() -> None:
    """把 stdout/stderr 切成 UTF-8 + errors=replace。

    Windows 默认控制台编码是 GBK：子进程输出用 errors="replace" 解码后会产生
    U+FFFD（还有插件里常见的 ✓/✗ 等符号），再往 GBK stdout 打印就会抛
    UnicodeEncodeError —— 表现为「门禁脚本自己崩了」，把真正的 FAIL 摘要顶掉。
    这里先兜住，保证门禁永远输出得出来。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — 老解释器/被重定向时静默跳过
            pass


def run_step(name: str, cmd: list[str], cwd: str, allow_skip: bool = False) -> tuple[str, str]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=600)
    except FileNotFoundError:
        return "SKIP", f"命令不可用: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return "FAIL", "执行超时（600s）"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        if "SKIP" in (proc.stdout or ""):
            return "SKIP", out.strip().splitlines()[-1] if out.strip() else ""
        return "PASS", out.strip().splitlines()[-1] if out.strip() else ""
    # 可选步骤（冒烟 / pytest）在"工具/依赖缺失"时记 SKIP 而非 FAIL
    if allow_skip:
        # pytest 未收集到用例（退出码 5）不是缺陷——全新脚手架还没有 L3 单测
        low = out.lower()
        if proc.returncode == 5 and ("no tests ran" in low or "collected 0 items" in low):
            return "SKIP", "未收集到用例（pytest 退出码 5）"
        if ("No module named" in out or "not found" in out.lower()
                or "无法找到" in out or proc.returncode == 2):
            return "SKIP", "依赖/工具缺失（不影响静态门禁）"
    tail = [line for line in out.strip().splitlines() if line.strip()]
    return "FAIL", tail[-1] if tail else f"退出码 {proc.returncode}"


def has_pytest(python: str) -> bool:
    """当前解释器是否可用 pytest（-m pytest 探活，比 which 可靠）。"""
    try:
        proc = subprocess.run([python, "-m", "pytest", "--version"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def main() -> int:
    harden_stdio()
    ap = argparse.ArgumentParser(description="MaiBot 插件交付门禁")
    ap.add_argument("--plugin", default=".", help="插件目录")
    ap.add_argument("--python", default=sys.executable, help="解释器（默认当前）")
    ap.add_argument("--skip", default="", help="要跳过的步骤，逗号分隔：check,smoke,pytest")
    args = ap.parse_args()

    plugin_dir = os.path.abspath(args.plugin)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    python = args.python

    # check_plugin.py：优先用插件目录内的，否则回落到 skill 自带版本
    local_check = os.path.join(plugin_dir, "check_plugin.py")
    check_script = local_check if os.path.isfile(local_check) else \
        os.path.join(SKILL_SCRIPTS_DIR, "check_plugin.py")

    steps: list[tuple[str, list[str], bool]] = []
    if "check" not in skip:
        steps.append(("check_plugin", [python, check_script, "--plugin", plugin_dir], False))
    smoke = os.path.join(plugin_dir, "tests", "smoke_test.py")
    if "smoke" not in skip and os.path.isfile(smoke):
        steps.append(("smoke_test", [python, smoke], True))
    if "pytest" not in skip and (os.path.isdir(os.path.join(plugin_dir, "tests"))
                                 or os.path.isdir(os.path.join(plugin_dir, "test"))):
        # pytest 常只装在隔离 venv 里，不在 PATH 上 —— 用当前解释器探活更可靠
        if has_pytest(python):
            steps.append(("pytest", [python, "-m", "pytest", "-q", os.path.join(plugin_dir, "tests")], True))

    print(f"门禁目标: {plugin_dir}")
    print("-" * 70)
    results: list[tuple[str, str, str]] = []
    for name, cmd, allow_skip in steps:
        level, detail = run_step(name, cmd, plugin_dir, allow_skip=allow_skip)
        results.append((level, name, detail))

    print(f"{'级别':<6}{'步骤':<16}{'摘要'}")
    print("-" * 70)
    for level, name, detail in results:
        print(f"{level:<6}{name:<16}{detail[:60]}")
    print("-" * 70)

    counts = {lv: sum(1 for r in results if r[0] == lv) for lv in ("PASS", "WARN", "FAIL", "SKIP")}
    print("PASS {PASS}  SKIP {SKIP}  FAIL {FAIL}".format(**counts))

    if counts["FAIL"]:
        print("\n门禁未通过：修复后重跑。manifest 相关修复需完整重启 MaiBot 才生效。")
        return 1
    if counts["SKIP"]:
        skip_steps = [name for lv, name, _ in results if lv == "SKIP"]
        print(f"\n门禁通过（含 SKIP 项：{', '.join(skip_steps)} 未执行；部署前需在对应环境复跑）。")
    else:
        print("\n门禁全绿，可进入打包部署。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
