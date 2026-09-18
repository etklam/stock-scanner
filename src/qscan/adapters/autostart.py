"""User-level autostart definitions for the packaged local runtime."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AutostartStatus:
    supported: bool
    installed: bool
    detail: str


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


class AutostartManager:
    """Install one login-time launcher without running it during installation."""

    def __init__(
        self,
        *,
        executable: Path,
        data_dir: Path,
        platform: str = sys.platform,
        home: Path | None = None,
        runner: Runner = _run,
    ) -> None:
        self.executable = executable.resolve()
        self.data_dir = data_dir.resolve()
        self.platform = platform
        self.home = (home or Path.home()).resolve()
        self.runner = runner

    @property
    def target(self) -> Path | None:
        if self.platform == "darwin":
            return self.home / "Library" / "LaunchAgents" / "com.qscan.start.plist"
        if self.platform.startswith("linux"):
            return self.home / ".config" / "systemd" / "user" / "qscan.service"
        return None

    def status(self) -> AutostartStatus:
        if self.platform == "win32":
            result = self.runner(["schtasks", "/Query", "/TN", "qscan"])
            return AutostartStatus(True, result.returncode == 0, "WINDOWS_TASK")
        target = self.target
        if target is None:
            return AutostartStatus(False, False, "PLATFORM_UNSUPPORTED")
        return AutostartStatus(True, target.is_file(), str(target))

    def install(self) -> AutostartStatus:
        command = [
            str(self.executable),
            "--data-dir",
            str(self.data_dir),
            "start",
            "--no-browser",
            "--preserve-automation",
        ]
        if self.platform == "darwin":
            target = self.target
            assert target is not None
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "Label": "com.qscan.start",
                "ProgramArguments": command,
                "RunAtLoad": True,
                "KeepAlive": True,
                "ProcessType": "Background",
            }
            _atomic_write(target, plistlib.dumps(payload))
            return self.status()
        if self.platform.startswith("linux"):
            target = self.target
            assert target is not None
            target.parent.mkdir(parents=True, exist_ok=True)
            quoted = " ".join(_systemd_quote(item) for item in command)
            content = (
                "[Unit]\nDescription=qscan local daily scanner\n\n"
                "[Service]\nType=simple\nRestart=on-failure\nRestartSec=30\n"
                f"ExecStart={quoted}\n\n[Install]\nWantedBy=default.target\n"
            ).encode()
            _atomic_write(target, content)
            reload_result = self.runner(["systemctl", "--user", "daemon-reload"])
            if reload_result.returncode:
                return AutostartStatus(True, False, "SYSTEMD_DAEMON_RELOAD_FAILED")
            enable_result = self.runner(["systemctl", "--user", "enable", "qscan.service"])
            return AutostartStatus(
                True,
                enable_result.returncode == 0,
                "SYSTEMD_USER_UNIT" if enable_result.returncode == 0 else "SYSTEMD_ENABLE_FAILED",
            )
        if self.platform == "win32":
            task_command = subprocess.list2cmdline(command)
            result = self.runner(
                [
                    "schtasks",
                    "/Create",
                    "/F",
                    "/SC",
                    "ONLOGON",
                    "/TN",
                    "qscan",
                    "/TR",
                    task_command,
                ]
            )
            return AutostartStatus(
                True,
                result.returncode == 0,
                "WINDOWS_TASK" if result.returncode == 0 else "WINDOWS_TASK_CREATE_FAILED",
            )
        return AutostartStatus(False, False, "PLATFORM_UNSUPPORTED")


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
