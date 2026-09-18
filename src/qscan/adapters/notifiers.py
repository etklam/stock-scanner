"""Native best-effort desktop notifications with bounded subprocess execution."""

import base64
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from qscan.application.notifications import Notification, NotificationOutcome, NotificationResult


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str], float], CommandResult]

_POWERSHELL_TOAST = r"""
param($title64, $body64, $url64)
$decode = { param($value) [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($value)) }
$title = [Security.SecurityElement]::Escape((& $decode $title64))
$body = [Security.SecurityElement]::Escape((& $decode $body64))
$url = & $decode $url64
$action = if ($url) {
    '<actions><action content="Open report" arguments="' +
    [Security.SecurityElement]::Escape($url) + '" activationType="protocol"/></actions>'
} else { '' }
$document = New-Object Windows.Data.Xml.Dom.XmlDocument
$document.LoadXml('<toast><visual><binding template="ToastGeneric"><text>' + $title +
    '</text><text>' + $body + '</text></binding></visual>' + $action + '</toast>')
$toast = [Windows.UI.Notifications.ToastNotification]::new($document)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('qscan').Show($toast)
""".strip()


def _run(command: Sequence[str], timeout: float) -> CommandResult:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _apple_script_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _is_loopback_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        return parsed.hostname == "localhost" or ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _permission_denied(completed: CommandResult) -> bool:
    message = f"{completed.stdout}\n{completed.stderr}".casefold()
    return any(
        marker in message
        for marker in ("permission denied", "not authorized", "notifications are disabled")
    )


class DesktopNotifier:
    def __init__(
        self,
        *,
        platform: str = sys.platform,
        runner: Runner = _run,
        timeout: float = 5,
    ) -> None:
        self.platform = platform
        self.runner = runner
        self.timeout = timeout

    def notify(self, notification: Notification) -> NotificationResult:
        if notification.report_url and not _is_loopback_url(notification.report_url):
            return NotificationResult(NotificationOutcome.FAILED, "UNSAFE_REPORT_URL")
        if self.platform == "win32":
            encoded = [
                base64.b64encode(value.encode()).decode()
                for value in (
                    notification.title,
                    notification.body,
                    notification.report_url or "",
                )
            ]
            return self._execute(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _POWERSHELL_TOAST,
                    *encoded,
                ]
            ).notification
        if self.platform.startswith("linux"):
            command = ["notify-send", "--app-name=qscan"]
            if notification.report_url:
                command.append("--action=open=Open report")
            command.extend((notification.title, notification.body))
            result = self._execute(command)
            if (
                result.outcome is NotificationOutcome.DELIVERED
                and notification.report_url
                and result.command_stdout.strip() == "open"
            ):
                return self._execute(["xdg-open", notification.report_url]).notification
            return result.notification
        if self.platform != "darwin":
            return NotificationResult(NotificationOutcome.UNAVAILABLE, "PLATFORM_UNSUPPORTED")
        script = (
            f'display notification "{_apple_script_text(notification.body)}" '
            f'with title "{_apple_script_text(notification.title)}"'
        )
        mac_result = self._execute(["osascript", "-e", script]).notification
        if mac_result.outcome is NotificationOutcome.DELIVERED and notification.report_url:
            return NotificationResult(
                NotificationOutcome.DELIVERED,
                "DELIVERY_ATTEMPT_ACKNOWLEDGED_ACTION_UNAVAILABLE",
            )
        return mac_result

    def _execute(self, command: Sequence[str]) -> "_Execution":
        try:
            completed = self.runner(command, self.timeout)
        except subprocess.TimeoutExpired:
            return _Execution(
                NotificationResult(NotificationOutcome.FAILED, "DELIVERY_COMMAND_TIMED_OUT")
            )
        except OSError:
            return _Execution(
                NotificationResult(NotificationOutcome.UNAVAILABLE, "COMMAND_UNAVAILABLE")
            )
        if completed.returncode:
            if _permission_denied(completed):
                return _Execution(
                    NotificationResult(
                        NotificationOutcome.DENIED,
                        "NOTIFICATION_PERMISSION_DENIED",
                    )
                )
            return _Execution(
                NotificationResult(NotificationOutcome.FAILED, "DELIVERY_COMMAND_FAILED")
            )
        return _Execution(
            NotificationResult(
                NotificationOutcome.DELIVERED,
                "DELIVERY_ATTEMPT_ACKNOWLEDGED",
            ),
            completed.stdout,
        )


@dataclass(frozen=True)
class _Execution:
    notification: NotificationResult
    command_stdout: str = ""

    @property
    def outcome(self) -> NotificationOutcome:
        return self.notification.outcome
