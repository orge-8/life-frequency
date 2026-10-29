#!/usr/bin/env python3
"""MaiBot 插件交付门禁编排。

    python run_gates.py --plugin <插件目录> [--skip pytest]

顺序:
    1. check_plugin.py  静态结构自检（零依赖，必须过）
    2. tests/smoke_test.py  FakeHost 生命周期冒烟（缺 SDK 记 SKIP）
    3. pytest（必须执行；未收集到用例记 SKIP）

退出码: 0 = 三步全部 PASS；1 = 存在 FAIL 或 SKIP，禁止交付。

SKIP 语义（未全覆盖，不等于通过）:
    - 冒烟成功退出且 stdout 首行以独立的 SKIP: 标记开头
    - pytest 退出码 5（无用例），或结果摘要包含实际跳过的用例
    - 当前解释器没装 pytest，或通过 --skip 显式跳过
必需脚本、测试目录、解释器缺失及执行异常均记 FAIL。
"""

from __future__ import annotations

import argparse
import os
import re
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


def _child_env() -> dict:
    """子进程环境：强制 UTF-8 标准流。

    v1.8.2 修：Windows 管道下子进程默认用本地编码（GBK）输出，本脚本按 UTF-8
    解码后中文全部变成 U+FFFD——门禁摘要里的关键告警文本不可读。
    """

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def run_step(name: str, cmd: list[str], cwd: str, allow_skip: bool = False) -> tuple[str, str]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=600, env=_child_env())
    except (OSError, ValueError) as exc:
        return "FAIL", f"命令或路径不可用: {exc}"
    except subprocess.TimeoutExpired:
        return "FAIL", "执行超时（600s）"
    out = (proc.stdout or "") + (proc.stderr or "")
    # 摘要优先取 **stdout** 的末行（各步骤的「结果行」都在 stdout）；
    # 直接取 stdout+stderr 的末行会选中运行中途打印到 stderr 的 warning，
    # 把真正的结果行顶掉（v1.8.2 修）。FAIL 时才看合并输出（traceback 在 stderr）。
    stdout_tail = [line for line in (proc.stdout or "").strip().splitlines() if line.strip()]
    combined_tail = [line for line in out.strip().splitlines() if line.strip()]
    summary = (stdout_tail or combined_tail)
    summary = summary[-1] if summary else ""
    if proc.returncode == 0:
        stdout_lines = (proc.stdout or "").splitlines()
        if (allow_skip and name == "smoke_test" and stdout_lines
                and stdout_lines[0].strip().startswith("SKIP:")):
            return "SKIP", stdout_lines[0].strip()
        if name == "pytest":
            # 结果行不一定在末行：插件 cleanup 之类的输出会排在它后面，
            # 只看末行会把「有 skip」误判成全绿。
            for line in stdout_lines:
                if re.search(r"\b[1-9]\d* skipped\b", line):
                    return "SKIP", f"未全覆盖: {line.strip()}"
        return "PASS", summary
    # 仅 pytest 的约定退出码 5 表示无用例；收集错误和业务异常仍为 FAIL。
    if allow_skip and name == "pytest" and proc.returncode == 5:
        return "SKIP", "未收集到用例（pytest 退出码 5）"
    return "FAIL", combined_tail[-1] if combined_tail else f"退出码 {proc.returncode}"


def has_pytest(python: str) -> bool:
    """仅明确未安装 pytest 时返回 False；探活异常交由门禁报告 FAIL。"""
    proc = subprocess.run([python, "-m", "pytest", "--version"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1 and re.fullmatch(
            r"[^\r\n]+: No module named ['\"]?pytest['\"]?", (proc.stderr or "").strip()):
        return False
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    detail = lines[-1] if lines else "无输出"
    raise RuntimeError(f"pytest 探测失败（退出码 {proc.returncode}）: {detail}")


def main() -> int:
    harden_stdio()
    ap = argparse.ArgumentParser(description="MaiBot 插件交付门禁")
    ap.add_argument("--plugin", default=".", help="插件目录")
    ap.add_argument("--python", default=sys.executable, help="解释器（默认当前）")
    ap.add_argument("--skip", default="", help="要跳过的步骤，逗号分隔：check,smoke,pytest")
    args = ap.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    unknown = skip - {"check", "smoke", "pytest"}
    if unknown:
        ap.error(f"--skip 未知步骤: {', '.join(sorted(unknown))}；合法选项: check,smoke,pytest")
    plugin_dir = os.path.abspath(args.plugin)
    python = args.python

    print(f"门禁目标: {plugin_dir}")
    print("-" * 70)
    results: list[tuple[str, str, str]] = []
    for step, name in (("check", "check_plugin"), ("smoke", "smoke_test"), ("pytest", "pytest")):
        if step in skip:
            results.append(("SKIP", name, "--skip 显式跳过，未全覆盖"))
            continue
        try:
            if not os.path.isdir(plugin_dir):
                raise FileNotFoundError(f"插件目录不存在或不可访问: {plugin_dir}")
            if step == "check":
                # 优先用插件目录内的自检脚本，否则回落到 skill 自带版本。
                local_check = os.path.join(plugin_dir, "check_plugin.py")
                check_script = local_check if os.path.isfile(local_check) else \
                    os.path.join(SKILL_SCRIPTS_DIR, "check_plugin.py")
                if not os.path.isfile(check_script):
                    raise FileNotFoundError(f"缺少必需文件: {check_script}")
                cmd = [python, check_script, "--plugin", plugin_dir]
            elif step == "smoke":
                smoke = os.path.join(plugin_dir, "tests", "smoke_test.py")
                if not os.path.isfile(smoke):
                    raise FileNotFoundError(f"缺少必需文件 tests/smoke_test.py: {smoke}")
                cmd = [python, smoke]
            else:
                test_dir = os.path.join(plugin_dir, "tests")
                if not os.path.isdir(test_dir):
                    test_dir = os.path.join(plugin_dir, "test")
                if not os.path.isdir(test_dir):
                    raise FileNotFoundError("缺少必需测试目录 tests/ 或 test/")
                # 缺 pytest 模块可记 SKIP；解释器缺失或探活异常必须 FAIL。
                if not has_pytest(python):
                    results.append(("SKIP", name, "当前解释器未安装 pytest，未全覆盖"))
                    continue
                cmd = [python, "-m", "pytest", "-q", test_dir]
            level, detail = run_step(name, cmd, plugin_dir, allow_skip=step != "check")
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            level, detail = "FAIL", f"无法执行: {exc}"
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
        print(f"\n门禁未全覆盖（SKIP 项：{', '.join(skip_steps)}）；补齐环境或用例并完整复跑后才能交付。")
        return 1
    print("\n门禁全绿，可进入打包部署。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
