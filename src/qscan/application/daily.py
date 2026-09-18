"""Small durable coordinator for the newest completed managed market session."""

import sys
import threading
from datetime import UTC
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from qscan.application.contracts import ApplicationError, AutomationStatus
from qscan.application.notifications import Notification, NotificationOutcome, Notifier
from qscan.application.publication import PublicationService
from qscan.application.universe import MAX_LKG_AGE
from qscan.domain.models import RunState
from qscan.domain.rules import RuleConfig

if TYPE_CHECKING:
    from qscan.bootstrap import Application


class Executor(Protocol):
    def poke(self) -> None: ...


class DailyCoordinator:
    def __init__(
        self,
        app: "Application",
        *,
        executor: Executor,
        publication: PublicationService | None = None,
        notifier: Notifier | None = None,
        report_base_url: str = "http://127.0.0.1:8000/ui/",
        poll_seconds: float = 30.0,
    ) -> None:
        if notifier is None:
            from qscan.adapters.notifiers import DesktopNotifier

            notifier = DesktopNotifier()
        self.app = app
        self.executor = executor
        self.publication = publication or PublicationService(
            app.repository, app.reports, app.data_dir
        )
        self.notifier = notifier
        self.report_base_url = report_base_url
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.alive:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="qscan-daily-coordinator")
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        self._stop.set()
        self._wakeup.set()
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def request_tick(self) -> None:
        self._wakeup.set()

    def enable(self) -> AutomationStatus:
        self.app.repository.set_automation_enabled(True)
        return self.status()

    def pause(self) -> AutomationStatus:
        self.app.repository.set_automation_enabled(False)
        return self.status()

    def run_now(self) -> AutomationStatus:
        self._process_outputs()
        self._accept(force=True)
        return self.status()

    def tick(self) -> AutomationStatus:
        if not self._tick_lock.acquire(blocking=False):
            return self.status()
        try:
            self._process_outputs()
            if self.app.repository.automation_enabled():
                self._accept(force=False)
            return self.status()
        finally:
            self._tick_lock.release()

    def _accept(self, *, force: bool) -> None:
        context = self.app.scans.current_session()
        latest = self.app.repository.latest_daily_job()
        if (
            not force
            and latest is not None
            and latest.session == context.as_of_session
            and latest.provider == self.app.provider.name
            and latest.config_hash == RuleConfig().config_hash()
            and latest.attempt == 0
        ):
            return
        snapshot = self.app.universe.refresh()
        run = self.app.scans.prepare(snapshot.watchlist_id, as_of=context.as_of_session)
        from qscan.application.reporting import ComparisonService

        run = run.model_copy(
            update={
                "comparison": ComparisonService(self.app.repository, self.app.snapshots).bind(run)
            }
        )
        _, created = self.app.repository.accept_daily_run(run, snapshot.id, force=force)
        if created:
            self.executor.poke()

    def retry_report(self, identity: UUID) -> AutomationStatus:
        """Retry publication/delivery for one accepted run without touching scan state."""
        if self.app.repository.daily_job_for_run(identity) is None:
            raise ValueError("Run is not a managed daily job")
        self._publish_and_notify(identity)
        return self.status()

    def _process_outputs(self) -> None:
        for identity in self.app.repository.daily_runs_pending_report():
            try:
                self._publish_and_notify(identity)
            except ApplicationError:
                continue
        latest = self.app.repository.latest_report_publication()
        if latest is not None:
            try:
                latest = self.publication.publish(latest.run_id)
            except ApplicationError:
                return
            self._notify(latest.run_id)

    def _publish_and_notify(self, identity: UUID) -> None:
        publication = self.publication.publish(identity)
        if publication.state == "PUBLISHED":
            self._notify(identity)

    def _notify(self, identity: UUID) -> None:
        delivery = self.app.repository.notification_delivery(identity)
        if delivery is not None and (
            delivery.outcome == NotificationOutcome.DELIVERED.value or delivery.attempts >= 3
        ):
            return
        run = self.app.repository.run_summary(identity)
        if run.state is RunState.FAILED:
            title = "qscan daily scan failed"
            body = "The completed-session scan failed; open the report for diagnostics."
        elif run.state is RunState.PARTIAL:
            title = "qscan daily report ready with partial coverage"
            body = "The scan completed with data gaps; open the report for coverage details."
        else:
            title = "qscan daily report ready"
            body = "The latest completed-session scan report is ready."
        result = self.notifier.notify(
            Notification(
                title=title,
                body=body,
                report_url=f"{self.report_base_url}?report={identity}",
            )
        )
        self.app.repository.record_notification(identity, result.outcome.value, result.detail)

    def status(self) -> AutomationStatus:
        latest = self.app.repository.latest_daily_job()
        run = self.app.repository.run_summary(latest.run_id) if latest is not None else None
        try:
            context = self.app.scans.current_session()
            due_session = (
                None
                if latest is not None and latest.session == context.as_of_session
                else context.as_of_session
            )
        except ApplicationError:
            due_session = None
        universe = self.app.repository.current_universe("sp500")
        freshness = (
            "MISSING"
            if universe is None
            else (
                "CURRENT"
                if self.app.clock.now().astimezone(UTC) - universe.retrieved_at <= MAX_LKG_AGE
                else "STALE"
            )
        )
        return AutomationStatus(
            enabled=self.app.repository.automation_enabled(),
            latest_job=latest,
            latest_run=run,
            next_due_session=due_session,
            universe=universe,
            universe_freshness=freshness,
            report=(
                self.app.repository.report_publication(latest.run_id)
                if latest is not None
                else self.app.repository.latest_report_publication()
            ),
            notification=(
                self.app.repository.notification_delivery(latest.run_id)
                if latest is not None
                else None
            ),
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._wakeup.wait(self.poll_seconds):
                self._wakeup.clear()
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception as exc:
                print(
                    f"qscan daily coordinator: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
