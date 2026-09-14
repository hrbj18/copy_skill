from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from io import StringIO
from pathlib import Path

from douyin_intelligence import cli
from douyin_intelligence.job_runtime import JobLock
from douyin_intelligence import workbench_launcher
from douyin_intelligence.workbench_launcher import (
    ALREADY_RUNNING_EXIT_CODE,
    launch_workbench,
    safe_launcher_error,
    smoke_auto_close_ms,
)


ROOT = Path(__file__).resolve().parents[1]


def _assert_within(path: Path, root: Path) -> None:
    """Refuse to touch ``path`` unless it is strictly inside ``root``.

    Guards cleanup helpers against a stray delete target -- the exact class of
    bug that once wiped the repository root.  Never the root itself: only
    descendants of the sandbox we created.
    """
    resolved = path.resolve()
    root = root.resolve()
    assert resolved != root, f"refusing to touch {resolved} itself"
    assert root in resolved.parents, f"refusing to touch {resolved} (outside {root})"


def _config(tmp_path: Path) -> dict:
    return {
        "timezone": "Asia/Shanghai",
        "_project_root": str(tmp_path),
        "jobs": {"lock_root": str(tmp_path / "locks")},
    }


def test_chinese_launchers_share_relocatable_helper_and_correct_module_entry() -> None:
    new_entry = (ROOT / "启动工作台.bat").read_text(encoding="utf-8")
    old_entry = (ROOT / "科技内容情报工作台.cmd").read_text(encoding="utf-8")
    helper = (ROOT / "scripts" / "launch_workbench.cmd").read_text(encoding="utf-8")
    assert new_entry == old_entry
    assert "%~dp0scripts\\launch_workbench.cmd" in new_entry
    assert "%~dp0" in new_entry
    assert ".venv\\Scripts\\python.exe" in helper
    assert "-m douyin_intelligence.cli workbench" in helper
    assert "pythonw" not in helper.lower()
    assert "workbench.py" not in helper.lower()
    assert "start " not in helper.lower()
    assert "python3" not in helper.lower()
    assert "PYTHONUTF8=1" in helper
    assert ">\"%RUN_LOG%\"" in helper
    assert "copy /y \"%RUN_LOG%\" \"%LOG_FILE%\"" in helper


def test_missing_virtualenv_is_visible_nonzero_and_never_uses_system_python() -> None:
    test_root = ROOT / "data" / "temp"
    test_root.mkdir(parents=True, exist_ok=True)
    fake_root = Path(tempfile.mkdtemp(prefix="launcher-missing-venv-", dir=test_root)) / "moved-project"
    scripts = fake_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "启动工作台.bat", fake_root / "启动工作台.bat")
    shutil.copy2(ROOT / "scripts" / "launch_workbench.cmd", scripts / "launch_workbench.cmd")
    env = dict(os.environ, COPY_SKILL_LAUNCHER_NO_PAUSE="1")
    try:
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        result = subprocess.run(
            [comspec, "/d", "/c", str(scripts / "launch_workbench.cmd"), str(fake_root)],
            cwd=fake_root.parent,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        output = result.stdout.decode("utf-8", errors="replace")
        assert result.returncode == 10, (result.returncode, result.stdout.decode("gbk", errors="replace"))
        assert "项目虚拟环境不存在，请联系项目维护者" in output
        log = fake_root / "data" / "logs" / "workbench-launch.log"
        assert log.is_file()
        assert "virtual environment is missing" in log.read_text(encoding="utf-8")
    finally:
        _assert_within(fake_root.parent, ROOT / "data" / "temp")
        shutil.rmtree(fake_root.parent, ignore_errors=True)


def test_package_import_failure_is_logged_and_visible_without_silent_exit() -> None:
    test_root = ROOT / "data" / "temp"
    test_root.mkdir(parents=True, exist_ok=True)
    fake_root = Path(tempfile.mkdtemp(prefix="launcher-import-failure-", dir=test_root)) / "moved-project"
    (fake_root / "scripts").mkdir(parents=True)
    (fake_root / ".venv" / "Scripts").mkdir(parents=True)
    (fake_root / "config").mkdir(parents=True)
    (fake_root / "src" / "douyin_intelligence").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "launch_workbench.cmd", fake_root / "scripts" / "launch_workbench.cmd")
    shutil.copy2(ROOT / ".venv" / "Scripts" / "python.exe", fake_root / ".venv" / "Scripts" / "python.exe")
    shutil.copy2(ROOT / ".venv" / "pyvenv.cfg", fake_root / ".venv" / "pyvenv.cfg")
    (fake_root / "config" / "content_intelligence.json").write_text("{}", encoding="utf-8")
    (fake_root / "src" / "douyin_intelligence" / "cli.py").write_text("# presence check only\n", encoding="utf-8")
    env = dict(os.environ, COPY_SKILL_LAUNCHER_NO_PAUSE="1")
    try:
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        result = subprocess.run(
            [comspec, "/d", "/c", str(fake_root / "scripts" / "launch_workbench.cmd"), str(fake_root)],
            cwd=fake_root.parent,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            check=False,
        )
        output = result.stdout.decode("utf-8", errors="replace")
        assert result.returncode != 0
        assert "工作台未能启动" in output
        log = fake_root / "data" / "logs" / "workbench-launch.log"
        persisted = log.read_text(encoding="utf-8")
        assert "No module named" in persisted
        assert log.stat().st_size < 16_384
        assert not list(log.parent.glob("workbench-launch-*.tmp"))
    finally:
        _assert_within(fake_root.parent, ROOT / "data" / "temp")
        shutil.rmtree(fake_root.parent, ignore_errors=True)


def test_launcher_lock_prevents_second_instance_without_calling_runner(tmp_path: Path) -> None:
    config = _config(tmp_path)
    existing = JobLock(config, "workbench_ui")
    assert existing.acquire()
    called = []
    stream = StringIO()
    try:
        code = launch_workbench(config, "config.json", runner=lambda *_args, **_kwargs: called.append(True), error_stream=stream)
    finally:
        existing.release()
    assert code == ALREADY_RUNNING_EXIT_CODE
    assert not called
    assert "已经在运行" in stream.getvalue()


def test_launcher_releases_lock_after_success(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls = []
    assert launch_workbench(config, "config.json", runner=lambda *args, **kwargs: calls.append((args, kwargs))) == 0
    assert calls and calls[0][1]["auto_close_ms"] is None
    recovered = JobLock(config, "workbench_ui")
    assert recovered.acquire()
    recovered.release()


def test_launcher_exception_is_visible_sanitized_and_releases_lock(tmp_path: Path) -> None:
    config = _config(tmp_path)
    stream = StringIO()

    def fail(*_args, **_kwargs) -> None:
        raise RuntimeError("password=hunter2 Bearer abc123 https://example.test/private")

    assert launch_workbench(config, "config.json", runner=fail, error_stream=stream) == 2
    message = stream.getvalue()
    assert "工作台启动失败" in message
    assert "RuntimeError" in message
    assert "hunter2" not in message
    assert "abc123" not in message
    assert "example.test" not in message
    assert "已脱敏" in message and "已隐藏地址" in message
    recovered = JobLock(config, "workbench_ui")
    assert recovered.acquire()
    recovered.release()


def test_smoke_auto_close_is_explicit_and_bounded() -> None:
    assert smoke_auto_close_ms({}) is None
    assert smoke_auto_close_ms({"COPY_SKILL_WORKBENCH_SMOKE_MS": "invalid"}) is None
    assert smoke_auto_close_ms({"COPY_SKILL_WORKBENCH_SMOKE_MS": "1"}) == 250
    assert smoke_auto_close_ms({"COPY_SKILL_WORKBENCH_SMOKE_MS": "999999"}) == 60_000


def test_launcher_scripts_have_no_automatic_external_work() -> None:
    scripts = "\n".join(
        (ROOT / name).read_text(encoding="utf-8")
        for name in ("启动工作台.bat", "科技内容情报工作台.cmd", "scripts/launch_workbench.cmd")
    ).lower()
    for forbidden in ("browser-start", "trusted-account-news", "collect-creators", "ffmpeg", "rapidocr", "scheduler run"):
        assert forbidden not in scripts
    assert "-m douyin_intelligence.cli workbench" in scripts


def test_cli_workbench_command_uses_guarded_launcher(monkeypatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(workbench_launcher, "launch_workbench", lambda value, path: calls.append((value, path)) or 0)
    assert cli.main(["workbench"]) == 0
    assert calls == [(config, "config/content_intelligence.json")]


def test_safe_launcher_error_has_finite_length() -> None:
    assert len(safe_launcher_error(RuntimeError("x" * 5000))) == 800


def test_launcher_scripts_are_crlf_and_guard_their_delete() -> None:
    # ``del /q ""`` (empty target) is harmless on a cmd *command line* -- it only
    # raises a syntax error.  Inside a *batch file* it is catastrophic: Del reads
    # the empty quoted argument as "everything in the CWD" and deletes every file
    # there -- never recursing, so subdirectories survive.
    #
    # The launcher used to be stored LF-only.  That made cmd's batch reader desync
    # line by line under ``chcp 65001`` + CJK text: ``set "RUN_LOG=..."`` was
    # swallowed and ``cd /d "%PROJECT_ROOT%"`` never ran, so the trailing
    # ``del /q "%RUN_LOG%"`` degraded to ``del /q ""`` and executed in the
    # *caller's* cwd.  A bare ``pytest tests`` runs the launcher with cwd=ROOT,
    # which is how the repository root's top-level files got wiped.
    for name in ("启动工作台.bat", "科技内容情报工作台.cmd", "scripts/launch_workbench.cmd"):
        raw = (ROOT / name).read_bytes()
        assert b"\r\n" in raw, f"{name} must use CRLF line endings"
        assert raw.count(b"\n") == raw.count(b"\r\n"), f"{name} contains a bare LF"
    helper = (ROOT / "scripts" / "launch_workbench.cmd").read_bytes()
    assert b'if defined RUN_LOG if exist "%RUN_LOG%" del /q "%RUN_LOG%"' in helper
