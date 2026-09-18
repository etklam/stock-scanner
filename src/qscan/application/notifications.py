"""Transport-neutral notification request and outcome contracts."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class NotificationOutcome(StrEnum):
    DELIVERED = "DELIVERED"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    report_url: str | None = None


@dataclass(frozen=True)
class NotificationResult:
    outcome: NotificationOutcome
    detail: str


class Notifier(Protocol):
    def notify(self, notification: Notification) -> NotificationResult: ...
