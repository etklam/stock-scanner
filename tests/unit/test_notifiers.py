import base64
import subprocess

import pytest

from qscan.adapters.notifiers import CommandResult, DesktopNotifier
from qscan.application.notifications import Notification, NotificationOutcome, NotificationResult


def test_macos_success_acknowledges_delivery_attempt_without_exposing_output():
    calls = []

    def runner(command, timeout):
        calls.append((command, timeout))
        return CommandResult(returncode=0, stdout="secret output", stderr="secret error")

    result = DesktopNotifier(platform="darwin", runner=runner, timeout=3).notify(
        Notification(title='Daily "scan"', body="Finished\\safely")
    )

    assert result.outcome is NotificationOutcome.DELIVERED
    assert result.detail == "DELIVERY_ATTEMPT_ACKNOWLEDGED"
    assert "secret" not in repr(result)
    assert calls == [
        (
            [
                "osascript",
                "-e",
                'display notification "Finished\\\\safely" with title "Daily \\"scan\\""',
            ],
            3,
        )
    ]


def test_linux_notification_opens_only_the_loopback_report_selected_by_user():
    calls = []

    def runner(command, timeout):
        calls.append((command, timeout))
        if command[0] == "notify-send":
            return CommandResult(returncode=0, stdout="open\n")
        return CommandResult(returncode=0)

    result = DesktopNotifier(platform="linux", runner=runner, timeout=4).notify(
        Notification(
            title="Daily scan",
            body="Finished",
            report_url="http://127.0.0.1:8765/reports/latest",
        )
    )

    assert result.outcome is NotificationOutcome.DELIVERED
    assert calls == [
        (
            [
                "notify-send",
                "--app-name=qscan",
                "--action=open=Open report",
                "Daily scan",
                "Finished",
            ],
            4,
        ),
        (["xdg-open", "http://127.0.0.1:8765/reports/latest"], 4),
    ]


def test_windows_uses_a_protocol_action_and_keeps_content_out_of_the_script():
    calls = []

    def runner(command, timeout):
        calls.append((command, timeout))
        return CommandResult(returncode=0)

    notification = Notification(
        title="Daily scan 'quoted'",
        body='Finished <&> "safely"',
        report_url="http://[::1]:8765/reports/latest?session=2026-09-17",
    )
    result = DesktopNotifier(platform="win32", runner=runner).notify(notification)

    assert result.outcome is NotificationOutcome.DELIVERED
    command, timeout = calls[0]
    assert command[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    assert 'activationType="protocol"' in command[4]
    assert notification.title not in command[4]
    assert [base64.b64decode(value).decode() for value in command[-3:]] == [
        notification.title,
        notification.body,
        notification.report_url,
    ]
    assert timeout == 5


def test_permission_denial_is_explicit_and_redacted():
    def runner(command, timeout):
        return CommandResult(returncode=1, stderr="Notifications are disabled: token=secret")

    result = DesktopNotifier(platform="linux", runner=runner).notify(
        Notification(title="Daily scan", body="Finished")
    )

    assert result.outcome is NotificationOutcome.DENIED
    assert result.detail == "NOTIFICATION_PERMISSION_DENIED"
    assert "secret" not in repr(result)


def test_timeout_is_a_failed_bounded_attempt_not_an_unavailable_adapter():
    def runner(command, timeout):
        assert timeout == 0.25
        raise subprocess.TimeoutExpired(command, timeout, output="secret")

    result = DesktopNotifier(platform="darwin", runner=runner, timeout=0.25).notify(
        Notification(title="Daily scan", body="Finished")
    )

    assert result == NotificationResult(NotificationOutcome.FAILED, "DELIVERY_COMMAND_TIMED_OUT")


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (FileNotFoundError("secret path"), NotificationOutcome.UNAVAILABLE),
        (OSError("secret diagnostic"), NotificationOutcome.UNAVAILABLE),
    ],
)
def test_missing_native_command_is_unavailable_and_redacted(exception, expected):
    def runner(command, timeout):
        raise exception

    result = DesktopNotifier(platform="linux", runner=runner).notify(
        Notification(title="Daily scan", body="Finished")
    )

    assert result == NotificationResult(expected, "COMMAND_UNAVAILABLE")
    assert "secret" not in repr(result)


def test_generic_command_failure_is_failed_without_command_diagnostics():
    result = DesktopNotifier(
        platform="darwin",
        runner=lambda command, timeout: CommandResult(9, stderr="token=secret"),
    ).notify(Notification(title="Daily scan", body="Finished"))

    assert result == NotificationResult(NotificationOutcome.FAILED, "DELIVERY_COMMAND_FAILED")
    assert "secret" not in repr(result)


@pytest.mark.parametrize(
    "report_url",
    [
        "https://example.com/report",
        "file:///tmp/report.html",
        "http://user:password@127.0.0.1/report",
        "not a url",
    ],
)
def test_report_action_rejects_non_loopback_or_credentialed_urls(report_url):
    calls = []
    result = DesktopNotifier(
        platform="linux",
        runner=lambda command, timeout: calls.append(command),
    ).notify(Notification(title="Daily scan", body="Finished", report_url=report_url))

    assert result == NotificationResult(NotificationOutcome.FAILED, "UNSAFE_REPORT_URL")
    assert calls == []


def test_macos_reports_that_native_notification_has_no_action_support():
    result = DesktopNotifier(
        platform="darwin", runner=lambda command, timeout: CommandResult(0)
    ).notify(
        Notification(
            title="Daily scan",
            body="Finished",
            report_url="http://localhost:8765/reports/latest",
        )
    )

    assert result == NotificationResult(
        NotificationOutcome.DELIVERED,
        "DELIVERY_ATTEMPT_ACKNOWLEDGED_ACTION_UNAVAILABLE",
    )
