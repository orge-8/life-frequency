"""交付门禁回归：所有命令与路径均模拟，不执行插件或访问现场数据。"""

import os
import subprocess
from types import SimpleNamespace

import pytest

import run_gates as gates


@pytest.mark.parametrize(
    "name,allow_skip,returncode,stdout,stderr,expected",
    [
        ("check_plugin", False, 0, "说明包含 SKIP 字样\ncheck OK", "", "PASS"),
        ("check_plugin", False, 0, "SKIP: 只是检查项说明\ncheck OK", "", "PASS"),
        ("smoke_test", True, 0, "SKIP: 未安装 maibot_sdk\n", "", "SKIP"),
        ("smoke_test", True, 0, "检查 SKIP: 分支\nsmoke OK", "", "PASS"),
        ("smoke_test", True, 0, "smoke started\nSKIP: 不是首行", "", "PASS"),
        ("smoke_test", True, 0, "SKIPPED is a label\nsmoke OK", "", "PASS"),
        ("smoke_test", True, 0, "smoke OK", "SKIP: stderr warning", "PASS"),
        ("smoke_test", True, 1, "SKIP: 未安装 maibot_sdk", "failed", "FAIL"),
        ("smoke_test", False, 0, "SKIP: 不允许跳过", "", "PASS"),
        ("pytest", True, 5, "no tests ran in 0.01s", "", "SKIP"),
        ("pytest", True, 5, "", "", "SKIP"),
        ("smoke_test", True, 5, "no tests ran", "", "FAIL"),
        ("check_plugin", False, 5, "no tests ran", "", "FAIL"),
        ("pytest", True, 2, "", "ERROR during collection", "FAIL"),
        ("pytest", True, 2, "", "ModuleNotFoundError: No module named 'app'", "FAIL"),
        ("pytest", True, 1, "", "AssertionError: record not found", "FAIL"),
        ("smoke_test", True, 1, "", "business object not found", "FAIL"),
        ("smoke_test", True, 2, "", "invalid arguments", "FAIL"),
        ("smoke_test", True, 1, "", "No module named 'maibot_sdk'", "FAIL"),
        ("smoke_test", True, 1, "", "无法找到业务记录", "FAIL"),
        ("pytest", True, 0, "2 passed, 1 skipped in 0.01s", "", "SKIP"),
        ("pytest", True, 0, "================ 1 skipped in 0.01s ================", "", "SKIP"),
        ("pytest", True, 0, "2 passed, 1 skipped in 0.01s\nplugin cleanup complete", "", "SKIP"),
        ("pytest", True, 0, "2 passed, 0 skipped in 0.01s", "", "PASS"),
        ("pytest", True, 0, "SKIP is mentioned in a log\n3 passed in 0.01s", "", "PASS"),
        ("pytest", True, 1, "1 failed, 1 skipped in 0.01s", "", "FAIL"),
        ("pytest", True, 0, "3 passed in 0.01s", "late warning", "PASS"),
    ],
)
def test_run_step_status(monkeypatch, name, allow_skip, returncode, stdout, stderr, expected):
    proc = subprocess.CompletedProcess(["gate-python"], returncode, stdout, stderr)
    monkeypatch.setattr(gates.subprocess, "run", lambda *args, **kwargs: proc)
    level, detail = gates.run_step(name, ["gate-python"], "virtual-plugin", allow_skip)
    assert level == expected
    if expected == "SKIP" and "skipped" in stdout:
        assert "未全覆盖" in detail
    if stdout == "3 passed in 0.01s":
        assert detail == stdout
    if expected == "FAIL" and stderr:
        assert stderr in detail


@pytest.mark.parametrize("name", ["check_plugin", "smoke_test", "pytest"])
@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("missing command or cwd"),
        PermissionError("permission denied"),
        NotADirectoryError("invalid cwd"),
        OSError("invalid executable"),
        ValueError("embedded null byte"),
        subprocess.TimeoutExpired("gate-python", 600),
    ],
)
def test_run_step_launch_errors_fail(monkeypatch, name, error):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(gates.subprocess, "run", run)
    level, detail = gates.run_step(name, ["gate-python"], "virtual-plugin", name != "check_plugin")
    assert level == "FAIL"
    assert detail


@pytest.fixture
def gate_env(monkeypatch):
    plugin = os.path.abspath(os.path.join(os.path.dirname(gates.__file__), "virtual-plugin"))
    tooling = os.path.join(plugin, "tooling")
    state = SimpleNamespace(
        plugin=plugin,
        check=os.path.join(plugin, "check_plugin.py"),
        fallback=os.path.join(tooling, "check_plugin.py"),
        smoke=os.path.join(plugin, "tests", "smoke_test.py"),
        tests=os.path.join(plugin, "tests"),
        test=os.path.join(plugin, "test"),
        calls=[],
        pytest_available=True,
        responses={
            "check_plugin": (0, "check OK", ""),
            "smoke_test": (0, "smoke OK", ""),
            "pytest": (0, "3 passed in 0.01s", ""),
        },
    )
    state.files = {state.check, state.fallback, state.smoke}
    state.dirs = {plugin, state.tests}
    # 替换本模块的 os 视图，避免模拟路径影响 pytest 自己的文件访问。
    path = SimpleNamespace(
        abspath=os.path.abspath,
        join=os.path.join,
        isfile=lambda p: p in state.files,
        isdir=lambda p: p in state.dirs,
    )
    monkeypatch.setattr(gates, "os", SimpleNamespace(path=path, environ=os.environ))
    monkeypatch.setattr(gates, "SKILL_SCRIPTS_DIR", tooling)
    monkeypatch.setattr(gates, "harden_stdio", lambda: None)
    monkeypatch.setattr(gates, "has_pytest", lambda python: state.pytest_available)
    monkeypatch.setattr(gates.sys, "argv", ["run_gates.py", "--plugin", plugin, "--python", "gate-python"])

    def run(cmd, **kwargs):
        state.calls.append((cmd, kwargs))
        if cmd[1:3] == ["-m", "pytest"]:
            name = "pytest"
        elif cmd[1] == state.smoke:
            name = "smoke_test"
        else:
            assert cmd[1] in {state.check, state.fallback}
            name = "check_plugin"
        response = state.responses[name]
        if isinstance(response, Exception):
            raise response
        return subprocess.CompletedProcess(cmd, *response)

    monkeypatch.setattr(gates.subprocess, "run", run)
    return state


def assert_report(capsys, code, expected_code, levels):
    output = capsys.readouterr().out
    assert code == expected_code, output
    rows = {}
    for line in output.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) >= 2 and parts[1] in {"check_plugin", "smoke_test", "pytest"}:
            rows[parts[1]] = parts[0]
    assert rows == dict(zip(("check_plugin", "smoke_test", "pytest"), levels)), output
    if expected_code:
        assert "门禁全绿" not in output
        assert "门禁通过" not in output
    else:
        assert "门禁全绿" in output
    return output


def test_main_all_three_pass(gate_env, capsys):
    assert_report(capsys, gates.main(), 0, ("PASS", "PASS", "PASS"))
    assert [cmd[0] for cmd, _ in gate_env.calls] == ["gate-python"] * 3
    assert all(kwargs["cwd"] == gate_env.plugin for _, kwargs in gate_env.calls)
    assert gate_env.calls[0][0] == ["gate-python", gate_env.check, "--plugin", gate_env.plugin]
    assert gate_env.calls[2][0] == ["gate-python", "-m", "pytest", "-q", gate_env.tests]


@pytest.mark.parametrize(
    "step,response,levels",
    [
        ("check_plugin", (0, "SKIP is documentation\ncheck OK", ""), ("PASS", "PASS", "PASS")),
        ("smoke_test", (0, "SKIP: 未安装 SDK", ""), ("PASS", "SKIP", "PASS")),
        ("pytest", (5, "no tests ran", ""), ("PASS", "PASS", "SKIP")),
        ("pytest", (0, "2 passed, 1 skipped in 0.01s", ""), ("PASS", "PASS", "SKIP")),
        ("pytest", (0, "2 passed, 1 skipped in 0.01s\nplugin cleanup complete", ""), ("PASS", "PASS", "SKIP")),
        ("pytest", (2, "", "ERROR during collection"), ("PASS", "PASS", "FAIL")),
        ("pytest", (1, "", "business record not found"), ("PASS", "PASS", "FAIL")),
        ("smoke_test", (1, "", "business record not found"), ("PASS", "FAIL", "PASS")),
    ],
)
def test_main_step_results_control_exit(gate_env, capsys, step, response, levels):
    gate_env.responses[step] = response
    output = assert_report(capsys, gates.main(), int(any(level != "PASS" for level in levels)), levels)
    if step == "pytest" and "skipped" in response[1]:
        assert "未全覆盖" in output


@pytest.mark.parametrize("missing", ["smoke", "check", "tests", "plugin", "pytest"])
def test_main_missing_prerequisites_are_reported(gate_env, capsys, missing):
    levels = ["PASS", "PASS", "PASS"]
    if missing == "smoke":
        gate_env.files.remove(gate_env.smoke)
        levels[1] = "FAIL"
    elif missing == "check":
        gate_env.files.difference_update({gate_env.check, gate_env.fallback})
        levels[0] = "FAIL"
    elif missing == "tests":
        gate_env.dirs.remove(gate_env.tests)
        gate_env.files.remove(gate_env.smoke)
        levels[1:] = ["FAIL", "FAIL"]
    elif missing == "plugin":
        gate_env.dirs.remove(gate_env.plugin)
        levels[:] = ["FAIL"] * 3
    else:
        gate_env.pytest_available = False
        levels[2] = "SKIP"
    output = assert_report(capsys, gates.main(), 1, levels)
    if missing == "tests":
        assert "tests" in output
    if missing == "pytest":
        assert "未安装 pytest" in output


def test_main_check_falls_back_to_bundled_script(gate_env, capsys):
    gate_env.files.remove(gate_env.check)
    assert_report(capsys, gates.main(), 0, ("PASS", "PASS", "PASS"))
    assert gate_env.calls[0][0][1] == gate_env.fallback


def test_main_singular_test_directory_is_used(gate_env, capsys):
    gate_env.dirs.remove(gate_env.tests)
    gate_env.dirs.add(gate_env.test)
    gate_env.files.remove(gate_env.smoke)
    assert_report(capsys, gates.main(), 1, ("PASS", "FAIL", "PASS"))
    assert gate_env.calls[-1][0][-1] == gate_env.test


@pytest.mark.parametrize("error", [FileNotFoundError("no python"), PermissionError("denied"), NotADirectoryError("bad cwd")])
def test_main_missing_or_unusable_tools_fail(gate_env, capsys, error):
    gate_env.responses = dict.fromkeys(gate_env.responses, error)
    assert_report(capsys, gates.main(), 1, ("FAIL", "FAIL", "FAIL"))


@pytest.mark.parametrize("method", ["isfile", "isdir"])
@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("bad path"), ValueError("embedded null byte")])
def test_main_path_errors_are_reported(gate_env, monkeypatch, capsys, method, error):
    def invalid_path(path):
        raise error

    monkeypatch.setattr(gates.os.path, method, invalid_path)
    levels = ("FAIL", "FAIL", "PASS") if method == "isfile" else ("FAIL", "FAIL", "FAIL")
    assert_report(capsys, gates.main(), 1, levels)


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("no python"), PermissionError("denied"), subprocess.TimeoutExpired("pytest", 60), RuntimeError("probe failed")],
)
def test_main_pytest_probe_errors_fail(gate_env, monkeypatch, capsys, error):
    def probe(python):
        raise error

    monkeypatch.setattr(gates, "has_pytest", probe)
    assert_report(capsys, gates.main(), 1, ("PASS", "PASS", "FAIL"))


@pytest.mark.parametrize(
    "skip,levels,call_count",
    [
        ("check", ("SKIP", "PASS", "PASS"), 2),
        ("smoke", ("PASS", "SKIP", "PASS"), 2),
        ("pytest", ("PASS", "PASS", "SKIP"), 2),
        ("check, smoke,pytest", ("SKIP", "SKIP", "SKIP"), 0),
    ],
)
def test_main_explicit_skip_is_not_green(gate_env, monkeypatch, capsys, skip, levels, call_count):
    monkeypatch.setattr(gates.sys, "argv", gates.sys.argv + ["--skip", skip])
    assert_report(capsys, gates.main(), 1, levels)
    assert len(gate_env.calls) == call_count


def test_main_skip_avoids_missing_files_and_probe(gate_env, monkeypatch, capsys):
    gate_env.files.clear()
    gate_env.dirs.clear()

    def forbidden_probe(python):
        pytest.fail("显式跳过不应探测 pytest")

    monkeypatch.setattr(gates, "has_pytest", forbidden_probe)
    monkeypatch.setattr(gates.sys, "argv", gates.sys.argv + ["--skip", "check,smoke,pytest"])
    assert_report(capsys, gates.main(), 1, ("SKIP", "SKIP", "SKIP"))
    assert not gate_env.calls


@pytest.mark.parametrize("skip", ["typo", "check,typo", "PYTEST"])
def test_main_unknown_skip_rejected_by_argparse(gate_env, monkeypatch, capsys, skip):
    monkeypatch.setattr(gates.sys, "argv", gates.sys.argv + ["--skip", skip])
    with pytest.raises(SystemExit) as exc:
        gates.main()
    assert exc.value.code == 2
    assert "--skip" in capsys.readouterr().err
    assert not gate_env.calls


@pytest.mark.parametrize(
    "returncode,stdout,stderr,expected",
    [
        (0, "pytest 8.0.0", "", True),
        (1, "", "gate-python: No module named pytest\n", False),
        (1, "", "gate-python: No module named 'pytest'\n", False),
    ],
)
def test_has_pytest_recognizes_installation(monkeypatch, returncode, stdout, stderr, expected):
    monkeypatch.setattr(gates.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode, stdout, stderr))
    assert gates.has_pytest("gate-python") is expected


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("no python"), PermissionError("denied"), NotADirectoryError("bad path"), subprocess.TimeoutExpired("pytest", 60)],
)
def test_has_pytest_does_not_hide_launch_errors(monkeypatch, error):
    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(gates.subprocess, "run", run)
    with pytest.raises(type(error)):
        gates.has_pytest("gate-python")


@pytest.mark.parametrize(
    "returncode,stderr",
    [
        (2, "ERROR during collection"),
        (1, "business record not found"),
        (1, "ModuleNotFoundError: No module named 'dependency'"),
        (1, "Traceback: plugin raised No module named pytest"),
    ],
)
def test_has_pytest_does_not_hide_broken_installation(monkeypatch, returncode, stderr):
    monkeypatch.setattr(gates.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode, "", stderr))
    with pytest.raises(RuntimeError):
        gates.has_pytest("gate-python")
