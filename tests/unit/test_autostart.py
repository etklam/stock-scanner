from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from qscan.adapters.autostart import AutostartManager


def completed(command: list[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, "", "")


def test_macos_installs_login_launcher_with_absolute_paths(tmp_path: Path) -> None:
    executable = tmp_path / "venv" / "bin" / "qscan"
    executable.parent.mkdir(parents=True)
    executable.touch()
    manager = AutostartManager(
        executable=executable,
        data_dir=tmp_path / "data",
        platform="darwin",
        home=tmp_path / "home",
    )

    status = manager.install()

    assert status.installed is True
    target = tmp_path / "home" / "Library" / "LaunchAgents" / "com.qscan.start.plist"
    payload = plistlib.loads(target.read_bytes())
    assert payload["ProgramArguments"] == [
        str(executable.resolve()),
        "--data-dir",
        str((tmp_path / "data").resolve()),
        "start",
        "--no-browser",
        "--preserve-automation",
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True


def test_linux_installs_and_enables_user_unit(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return completed(command)

    manager = AutostartManager(
        executable=tmp_path / "qscan",
        data_dir=tmp_path / "data with spaces",
        platform="linux",
        home=tmp_path / "home",
        runner=runner,
    )

    status = manager.install()

    assert status.installed is True
    unit = tmp_path / "home" / ".config" / "systemd" / "user" / "qscan.service"
    content = unit.read_text()
    assert f'ExecStart="{(tmp_path / "qscan").resolve()}"' in content
    assert f'"{(tmp_path / "data with spaces").resolve()}"' in content
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "qscan.service"],
    ]


def test_windows_uses_one_onlogon_task_and_status_query(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return completed(command)

    manager = AutostartManager(
        executable=tmp_path / "qscan.exe",
        data_dir=tmp_path / "data",
        platform="win32",
        home=tmp_path,
        runner=runner,
    )

    assert manager.install().installed is True
    assert manager.status().installed is True
    assert calls[0][:8] == ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "qscan", "/TR"]
    assert calls[1] == ["schtasks", "/Query", "/TN", "qscan"]


def test_unsupported_platform_is_reported_without_side_effects(tmp_path: Path) -> None:
    manager = AutostartManager(
        executable=tmp_path / "qscan",
        data_dir=tmp_path / "data",
        platform="plan9",
        home=tmp_path,
    )

    assert manager.status().supported is False
    assert manager.install().detail == "PLATFORM_UNSUPPORTED"
