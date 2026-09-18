"""Packaged managed daily workflow; all collaborators are offline fakes."""

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.providers import FixtureProvider
from qscan.application.contracts import RawPrices, UniverseObservation
from qscan.application.daily import DailyCoordinator
from qscan.application.notifications import NotificationOutcome, NotificationResult
from qscan.bootstrap import bootstrap
from qscan.interfaces.api.app import create_app
from qscan.interfaces.api.localauth import ensure_token


class UniverseSource:
    def __init__(self) -> None:
        self.revision = 1

    def fetch(self) -> UniverseObservation:
        return UniverseObservation(
            source_url="https://example.invalid/sp500",
            source_license="test fixture",
            source_revision=str(self.revision),
            observed_at=datetime(2026, 9, 5, tzinfo=UTC),
            symbols=tuple(f"S{index:03d}" for index in range(450)),
        )


class Executor:
    def __init__(self) -> None:
        self.pokes = 0

    def poke(self) -> None:
        self.pokes += 1


class Notifier:
    def __init__(self, outcome: NotificationOutcome = NotificationOutcome.DELIVERED) -> None:
        self.outcome = outcome
        self.calls = []

    def notify(self, notification):
        self.calls.append(notification)
        return NotificationResult(self.outcome, f"fixture-{self.outcome.value.lower()}")


class LifecycleCoordinator:
    def __init__(self) -> None:
        self.alive = False
        self.started = self.requested = self.stopped = 0

    def start(self) -> None:
        self.alive = True
        self.started += 1

    def request_tick(self) -> None:
        self.requested += 1

    def stop(self, timeout: float = 10.0) -> bool:
        self.alive = False
        self.stopped += 1
        return True


def test_repeated_ticks_and_universe_revision_create_one_normal_job(tmp_path):
    source = UniverseSource()
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "daily",
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=NYSECalendar(),
        universe_source=source,
    )
    executor = Executor()
    coordinator = DailyCoordinator(app, executor=executor)
    try:
        coordinator.enable()
        first = coordinator.tick()
        source.revision = 2
        app.universe.refresh()
        second = coordinator.tick()

        assert first.latest_job is not None
        assert second.latest_job is not None
        assert second.latest_job.id == first.latest_job.id
        assert second.latest_job.attempt == 0
        assert second.latest_job.session == date(2026, 9, 4)
        assert len(app.queries.list()) == 1
        assert executor.pokes == 1
    finally:
        app.close()


def test_pause_enable_catches_up_only_newest_session_and_run_now_increments_attempt(tmp_path):
    clock = FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))  # Saturday -> Friday close
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "catchup",
        clock=clock,
        calendar=NYSECalendar(),
        universe_source=UniverseSource(),
    )
    executor = Executor()
    coordinator = DailyCoordinator(app, executor=executor)
    try:
        coordinator.enable()
        coordinator.tick()
        assert coordinator.status().latest_job.session == date(2026, 9, 4)  # type: ignore[union-attr]

        coordinator.pause()
        clock.instant = datetime(2026, 9, 15, 22, tzinfo=UTC)
        coordinator.tick()
        assert len(app.queries.list()) == 1

        coordinator.enable()
        caught_up = coordinator.tick()
        assert caught_up.latest_job is not None
        assert caught_up.latest_job.session == date(2026, 9, 15)
        assert caught_up.latest_job.attempt == 0
        assert len(app.queries.list()) == 2

        forced = coordinator.run_now()
        assert forced.latest_job is not None
        assert forced.latest_job.session == date(2026, 9, 15)
        assert forced.latest_job.attempt == 1
        assert len(app.queries.list()) == 3
        assert executor.pokes == 3
    finally:
        app.close()


def test_terminal_run_publishes_once_and_records_one_final_notification(tmp_path):
    calendar = NYSECalendar()
    sessions = calendar.sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
    raw = RawPrices(tuple((session, 100.0 + index * 0.1) for index, session in enumerate(sessions)))
    symbols = tuple(f"S{index:03d}" for index in range(450))
    provider = FixtureProvider({symbol: raw for symbol in symbols})
    notifier = Notifier()
    app = bootstrap(
        provider,
        data_dir=tmp_path / "publication",
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=calendar,
        universe_source=UniverseSource(),
    )
    executor = Executor()
    coordinator = DailyCoordinator(app, executor=executor, notifier=notifier)
    try:
        coordinator.enable()
        accepted = coordinator.tick()
        assert accepted.latest_job is not None
        completed = app.scans.execute_existing(accepted.latest_job.run_id)
        assert completed is not None
        fetches = len(provider.calls)

        published = coordinator.tick()
        coordinator.tick()

        assert published.report is not None
        assert published.report.state == "PUBLISHED"
        assert published.report.relative_path is not None
        assert not published.report.relative_path.startswith("/")
        assert (app.data_dir / "reports" / published.report.relative_path).is_file()
        assert published.notification is not None
        assert published.notification.outcome == "DELIVERED"
        assert published.notification.attempts == 1
        assert len(notifier.calls) == 1
        assert notifier.calls[0].report_url.startswith("http://127.0.0.1:8000/ui/")
        assert "token" not in notifier.calls[0].report_url
        assert len(provider.calls) == fetches

        report_path = app.data_dir / "reports" / published.report.relative_path
        report_path.unlink()
        assert not report_path.exists()

        token_path = app.data_dir / "api-token.json"
        token, _ = ensure_token(token_path)
        api = create_app(
            app,
            token_path=token_path,
            executor=executor,  # type: ignore[arg-type]
            coordinator=coordinator,
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
        )
        with TestClient(api) as client:
            response = client.get(
                f"/api/v1/reports/{accepted.latest_job.run_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            assert report_path.is_file()
            assert "<style>" in response.text
            policy = response.headers["Content-Security-Policy"]
            assert "style-src 'unsafe-inline'" in policy
            assert "img-src data:" in policy
            assert "script-src" not in policy
    finally:
        app.close()


def test_older_report_retry_cannot_rewind_latest_pointer(tmp_path):
    clock = FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "monotonic-latest",
        clock=clock,
        calendar=NYSECalendar(),
        universe_source=UniverseSource(),
    )
    coordinator = DailyCoordinator(app, executor=Executor(), notifier=Notifier())
    try:
        coordinator.enable()
        old = coordinator.tick().latest_job
        assert old is not None
        clock.instant = datetime(2026, 9, 8, 22, tzinfo=UTC)
        new = coordinator.tick().latest_job
        assert new is not None and new.session > old.session

        app.repository.record_report_success(new.run_id, "new.html")
        app.repository.record_report_success(old.run_id, "old-retry.html")

        latest = app.repository.latest_report_publication()
        assert latest is not None and latest.run_id == new.run_id
    finally:
        app.close()


def test_daily_comparison_baseline_is_frozen_at_acceptance(tmp_path):
    calendar = NYSECalendar()
    sessions = calendar.sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
    raw = RawPrices(tuple((session, 100.0 + index * 0.1) for index, session in enumerate(sessions)))
    symbols = tuple(f"S{index:03d}" for index in range(450))
    app = bootstrap(
        FixtureProvider({symbol: raw for symbol in symbols}),
        data_dir=tmp_path / "frozen-baseline",
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=calendar,
        universe_source=UniverseSource(),
    )
    coordinator = DailyCoordinator(app, executor=Executor(), notifier=Notifier())
    try:
        coordinator.enable()
        accepted = coordinator.tick().latest_job
        assert accepted is not None
        queued = app.repository.run_summary(accepted.run_id)
        assert queued.comparison is not None
        assert queued.comparison.previous_run_id is None

        app.scans.scan(queued.watchlist.id, as_of=queued.context.reference_session)
        completed = app.scans.execute_existing(accepted.run_id)

        assert completed is not None
        assert completed.comparison is not None
        assert completed.comparison.previous_run_id is None
    finally:
        app.close()


def test_report_and_notifier_failures_retry_without_rescanning(tmp_path, monkeypatch):
    calendar = NYSECalendar()
    sessions = calendar.sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
    raw = RawPrices(tuple((session, 100.0) for session in sessions))
    symbols = tuple(f"S{index:03d}" for index in range(450))
    provider = FixtureProvider({symbol: raw for symbol in symbols})
    notifier = Notifier(NotificationOutcome.FAILED)
    app = bootstrap(
        provider,
        data_dir=tmp_path / "retry",
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=calendar,
        universe_source=UniverseSource(),
    )
    coordinator = DailyCoordinator(app, executor=Executor(), notifier=notifier)
    try:
        coordinator.enable()
        accepted = coordinator.tick()
        assert accepted.latest_job is not None
        app.scans.execute_existing(accepted.latest_job.run_id)
        fetches = len(provider.calls)
        build = coordinator.publication.reports.build
        monkeypatch.setattr(
            coordinator.publication.reports,
            "build",
            lambda identity: (_ for _ in ()).throw(OSError("disk unavailable")),
        )

        failed = coordinator.tick()
        assert failed.report is not None and failed.report.state == "FAILED"
        assert len(notifier.calls) == 0
        monkeypatch.setattr(coordinator.publication.reports, "build", build)

        for _ in range(5):
            coordinator.tick()

        status = coordinator.status()
        assert status.report is not None and status.report.state == "PUBLISHED"
        assert status.report.attempts == 2
        assert status.notification is not None
        assert status.notification.outcome == "FAILED"
        assert status.notification.attempts == 3
        assert len(notifier.calls) == 3
        assert len(provider.calls) == fetches
        assert len(app.queries.list()) == 1
    finally:
        app.close()


def test_failed_daily_run_sends_an_honest_failure_notification(tmp_path):
    notifier = Notifier()
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "failed-notification",
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=NYSECalendar(),
        universe_source=UniverseSource(),
    )
    coordinator = DailyCoordinator(app, executor=Executor(), notifier=notifier)
    try:
        coordinator.enable()
        accepted = coordinator.tick()
        assert accepted.latest_job is not None
        completed = app.scans.execute_existing(accepted.latest_job.run_id)
        assert completed is not None and completed.state.value == "FAILED"

        coordinator.tick()

        assert len(notifier.calls) == 1
        assert notifier.calls[0].title == "qscan daily scan failed"
        assert "diagnostics" in notifier.calls[0].body
    finally:
        app.close()


def test_automation_api_keeps_auth_and_mutation_security(tmp_path):
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "api",
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=NYSECalendar(),
        universe_source=UniverseSource(),
    )
    token_path = tmp_path / "api-token.json"
    token, _ = ensure_token(token_path)
    coordinator = DailyCoordinator(app, executor=Executor(), notifier=Notifier())
    api = create_app(
        app,
        token_path=token_path,
        executor=Executor(),  # type: ignore[arg-type]
        coordinator=coordinator,
        allowed_hosts=("testserver",),
        allowed_origins=("http://testserver",),
    )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(api) as client:
            assert client.get("/api/v1/automation/status").status_code == 401
            assert client.post("/api/v1/automation/enable").status_code == 401
            enabled = client.post("/api/v1/automation/enable", headers=headers)
            assert enabled.status_code == 200
            assert enabled.json()["enabled"] is True
            latest = client.get("/api/v1/reports/latest", headers=headers)
            assert latest.status_code == 200
            assert latest.json() == {
                "run_id": None,
                "state": "MISSING",
                "attempts": 0,
                "url": None,
                "error": None,
            }
            paused = client.post("/api/v1/automation/pause", headers=headers)
            assert paused.status_code == 200
            assert paused.json()["enabled"] is False
    finally:
        app.close()


def test_server_lifespan_starts_enabled_coordinator_without_browser_request(tmp_path):
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "lifecycle",
        clock=FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC)),
        calendar=NYSECalendar(),
        universe_source=UniverseSource(),
    )
    app.repository.set_automation_enabled(True)
    token_path = tmp_path / "lifecycle-token.json"
    ensure_token(token_path)
    coordinator = LifecycleCoordinator()
    api = create_app(
        app,
        token_path=token_path,
        executor=Executor(),  # type: ignore[arg-type]
        coordinator=coordinator,  # type: ignore[arg-type]
        allowed_hosts=("testserver",),
    )
    try:
        with TestClient(api):
            assert coordinator.started == 1
            assert coordinator.requested == 1
            assert coordinator.alive is True
        assert coordinator.stopped == 1
        assert coordinator.alive is False
    finally:
        app.close()
