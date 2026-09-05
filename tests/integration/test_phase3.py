import csv
import io
import json
import socket
from datetime import UTC, date, datetime, timedelta

import pytest
from filelock import FileLock
from sqlalchemy import text
from typer.testing import CliRunner

from qscan.adapters.calendar import FixedClock, NYSECalendar
from qscan.adapters.providers import FixtureProvider, YahooProvider
from qscan.adapters.report_renderer import csv_text, export, render
from qscan.application.contracts import ApplicationContext, ApplicationError, RawPrices
from qscan.bootstrap import bootstrap
from qscan.domain.models import DataMode, RunState
from qscan.interfaces.cli import app as cli

runner = CliRunner()


@pytest.fixture
def setup(tmp_path):
    calendar = NYSECalendar()
    clock = FixedClock(datetime(2026, 9, 5, 12, tzinfo=UTC))
    sessions = calendar.sessions(date(2025, 1, 1), date(2026, 9, 4))[-130:]
    closes = [50 + i * 0.5 for i in range(88)] + [99.0, 100.0] * 20 + [100.0, 101.0]
    raw = RawPrices(tuple(zip(sessions, closes, strict=True)))
    provider = FixtureProvider({"GOOD": raw})
    instance = bootstrap(provider, data_dir=tmp_path / "資料 space", clock=clock, calendar=calendar)
    yield instance, provider, raw, clock
    instance.close()


def call(instance, *args):
    return runner.invoke(
        cli, ["--data-dir", str(instance.data_dir), "--provider", "fixture", *args]
    )


def pair(setup):
    instance, _, raw, clock = setup
    watchlist = instance.watchlists.import_content("test", b"GOOD")
    first = instance.scans.scan(watchlist.id, as_of=raw.rows[-2][0])
    clock.instant += timedelta(seconds=1)
    second = instance.scans.scan(watchlist.id)
    return watchlist, first, second


def test_help_version_doctor_no_init(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network")

    monkeypatch.setattr(socket, "socket", forbidden)
    for args in (["--help"], ["--version"], ["scan", "--help"]):
        result = runner.invoke(cli, ["--data-dir", str(tmp_path / "missing"), *args])
        assert result.exit_code == 0, result.output
        assert not (tmp_path / "missing").exists()
    result = runner.invoke(cli, ["--data-dir", str(tmp_path / "missing"), "doctor"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["initialized"] is False
    assert not (tmp_path / "missing").exists()


def test_init_import_replace_and_invalid(setup, tmp_path):
    instance, *_ = setup
    path = tmp_path / "中文 名單.csv"
    path.write_text("\ufeffsymbol\nGOOD\n", encoding="utf-8")
    assert call(instance, "init").exit_code == 0
    result = call(instance, "watchlist", "import", "--name", "名單", "--file", str(path))
    assert result.exit_code == 0, result.output
    saved = json.loads(result.stdout)
    assert call(instance, "init").exit_code == 0
    assert json.loads(call(instance, "watchlist", "list").stdout)[0]["id"] == saved["id"]
    args = ["watchlist", "import", "--name", "名單", "--file", str(path)]
    assert call(instance, *args).exit_code == 2
    assert call(instance, *args, "--replace").exit_code == 0
    result = call(instance, *args, "--replace", "--expected-revision", "1")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "WATCHLIST_VERSION_CONFLICT"
    path.write_bytes(b"\xff")
    assert call(instance, *args, "--replace").exit_code == 2
    for args in (
        ["scans", "show", "bad-id"],
        ["scan", "--watchlist", "名單", "--as-of", "bad"],
        ["scan", "--watchlist", "名單", "--as-of", "2026-09-05"],
    ):
        result = call(instance, *args)
        assert result.exit_code == 2
        assert "error" in json.loads(result.stdout)


def test_cli_matches_service_json_and_partial_query(setup):
    instance, *_ = setup
    watchlist, _, second = pair(setup)
    result = call(
        instance,
        "scan",
        "--watchlist",
        str(watchlist.id),
        "--as-of",
        "2026-09-04",
        "--data-mode",
        "cache_only",
        "--format",
        "json",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["results"][0]["analysis"] == second.results[
        0
    ].analysis.model_dump(mode="json")
    assert "SYNTHETIC" in result.stderr
    partial_list = instance.watchlists.import_content("partial", b"GOOD\nBAD")
    result = call(
        instance,
        "scan",
        "--watchlist",
        str(partial_list.id),
        "--as-of",
        "2026-09-04",
        "--data-mode",
        "cache_only",
        "--format",
        "json",
    )
    assert result.exit_code == 3
    identity = json.loads(result.stdout)["id"]
    assert call(instance, "scans", "show", identity).exit_code == 0
    failed = instance.watchlists.import_content("fail", b"BAD")
    assert (
        call(
            instance,
            "scan",
            "--watchlist",
            str(failed.id),
            "--as-of",
            "2026-09-04",
            "--data-mode",
            "cache_only",
            "--format",
            "json",
        ).exit_code
        == 1
    )


def test_cache_provider_isolation(setup):
    instance, provider, _, clock = setup
    watchlist = instance.watchlists.import_content("test", b"GOOD")
    instance.scans.scan(watchlist.id)
    other = bootstrap(YahooProvider(), data_dir=instance.data_dir, clock=clock)
    try:
        assert other.repository.cache(watchlist.instruments[0]) is None
        assert instance.repository.cache(watchlist.instruments[0]) is not None
    finally:
        other.close()


def test_snapshot_reports_immutable_and_no_network(setup, monkeypatch):
    instance, provider, raw, _ = setup
    watchlist, _, second = pair(setup)
    report = instance.reports.build(second.id)
    provider.data["GOOD"] = RawPrices(tuple((s, c * 2) for s, c in raw.rows))
    instance.scans.scan(watchlist.id, mode=DataMode.FORCE)

    def forbidden(*args, **kwargs):
        raise AssertionError("network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(provider, "fetch", forbidden)
    assert instance.reports.build(second.id) == report
    assert instance.scans.replay(second.id).results == second.results
    assert call(instance, "scans", "list").exit_code == 0
    assert call(instance, "scans", "show", str(second.id)).exit_code == 0
    assert call(instance, "doctor").exit_code == 0
    assert (
        call(
            instance,
            "scan",
            "--watchlist",
            str(watchlist.id),
            "--as-of",
            "2026-09-04",
            "--data-mode",
            "cache_only",
            "--format",
            "json",
        ).exit_code
        == 0
    )


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_report_snapshot_damage_and_write_failure(setup, tmp_path, monkeypatch, damage):
    instance, *_ = setup
    _, _, run = pair(setup)
    report = instance.reports.build(run.id)
    path = export(report, tmp_path, "json")
    original = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("private/path secret")

    with monkeypatch.context() as m:
        m.setattr("qscan.adapters.report_renderer.os.replace", fail)
        with pytest.raises(ApplicationError) as exc:
            export(report, tmp_path, "json")
        assert "private/path" not in str(exc.value)
    assert path.read_bytes() == original
    assert instance.queries.get(run.id) == run
    snapshot = instance.snapshots.path(run.input_hash)
    if damage == "missing":
        snapshot.unlink()
    else:
        snapshot.write_bytes(b"broken")
    report = instance.reports.build(run.id)
    assert report.chart_error and not report.charts
    with pytest.raises(ApplicationError):
        instance.scans.replay(run.id)
    assert instance.queries.get(run.id) == run


def test_exports_safety_top_and_chart_geometry(setup, tmp_path):
    instance, *_ = setup
    _, _, run = pair(setup)
    report = instance.reports.build(run.id, top=1)
    chart = report.charts[0]
    assert chart.window_end == run.context.reference_session
    assert (
        chart.sessions.index(chart.window_end) - chart.sessions.index(chart.window_start) + 1
        == run.results[0].analysis.selected_window.window_sessions
    )
    assert chart.sma["50"][48] is None
    assert chart.sma["50"][49] == sum(chart.closes[:50]) / 50
    assert report.run.counts == run.counts
    assert csv_text(-0.2) == -0.2 and csv_text("=SUM(A1)").startswith("'")
    escaped = report.model_copy(update={"warnings": ("<script>alert(1)</script>",)})
    html = render(escaped, "html")
    assert b"&lt;script&gt;" in html and b"<script>" not in html
    assert b"data:image/png;base64," in html and b"https://" not in html
    rows = list(csv.DictReader(io.StringIO(render(report, "csv").decode("utf-8-sig"))))
    assert len(rows) == 1 and rows[0]["score"] == str(run.results[0].analysis.score)
    assert json.loads(render(report, "json"))["schema_version"] == 1
    assert (
        call(
            instance, "report", str(run.id), "--format", "html", "--output", str(tmp_path)
        ).exit_code
        == 0
    )


def test_comparison_binding_stage_and_replay(setup):
    instance, _, raw, clock = setup
    watchlist, first, second = pair(setup)
    assert first.comparison.changes[0].codes == ("NO_BASELINE",)
    assert second.comparison.previous_run_id == first.id
    assert "STAGE_CHANGED" in second.comparison.changes[0].codes
    clock.instant += timedelta(seconds=10)
    replay = instance.scans.replay(first.id)
    assert replay.comparison.binding == "replay_unavailable"
    assert instance.scans.scan(watchlist.id).comparison.previous_run_id == first.id
    later = instance.scans.scan(watchlist.id, as_of=raw.rows[-2][0], mode=DataMode.CACHE_ONLY)
    assert instance.comparisons.get(second.id) == second.comparison
    assert instance.scans.scan(watchlist.id).comparison.previous_run_id == later.id


def test_comparison_universe_invalid_revision_and_legacy(setup):
    instance, provider, raw, clock = setup
    watchlist, first, _ = pair(setup)
    instance.watchlists.import_content(
        "test", b"GOOD\nNEW", watchlist_id=watchlist.id, expected_revision=1
    )
    updated = instance.scans.scan(watchlist.id)
    assert any(c.codes == ("UNIVERSE_CHANGED",) for c in updated.comparison.changes)
    provider.data["GOOD"] = RawPrices(tuple((s, c * 0.5) for s, c in raw.rows))
    revised = instance.scans.scan(watchlist.id, mode=DataMode.FORCE)
    assert any("DATA_REVISION_DIFF" in c.codes for c in revised.comparison.changes)
    with instance.engine.begin() as connection:
        document = first.model_dump(mode="json", exclude={"results", "comparison"})
        connection.execute(
            text("UPDATE scan_runs SET document=:d WHERE id=:id"),
            {"d": json.dumps(document), "id": str(first.id)},
        )
    assert instance.comparisons.get(first.id).binding == "legacy_unavailable"


def test_cross_owner_report_comparison_and_query(setup):
    instance, provider, _, clock = setup
    _, _, run = pair(setup)
    other = bootstrap(
        provider, data_dir=instance.data_dir, clock=clock, context=ApplicationContext("other")
    )
    try:
        for fn in (other.queries.get, other.reports.build, other.comparisons.get):
            with pytest.raises(ApplicationError):
                fn(run.id)
        assert not other.queries.summaries()
    finally:
        other.close()


def test_readonly_schema_never_migrates(setup, monkeypatch):
    instance, *_ = setup
    pair(setup)

    def fail(*args, **kwargs):
        raise AssertionError("migration")

    monkeypatch.setattr("qscan.bootstrap.migrate", fail)
    assert call(instance, "scans", "list").exit_code == 0
    with instance.engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num='future'"))
    result = call(instance, "scans", "list")
    assert result.exit_code == 2
    with instance.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "future"


def test_lock_exit_and_query_while_busy(setup, monkeypatch):
    instance, *_ = setup
    _, _, run = pair(setup)
    from qscan import bootstrap as wiring

    original = wiring.bootstrap

    def fast(*args, **kwargs):
        result = original(*args, **kwargs)
        result.scans.lock.timeout = 0.01
        return result

    monkeypatch.setattr(wiring, "bootstrap", fast)
    with FileLock(instance.data_dir / "executor.lock"):
        result = call(instance, "replay", str(run.id), "--format", "json")
        assert result.exit_code == 4
        assert json.loads(result.stdout)["error"]["code"] == "EXECUTOR_LOCKED"
        assert call(instance, "scans", "show", str(run.id)).exit_code == 0
        diagnosis = json.loads(call(instance, "doctor").stdout)
        assert diagnosis["checks"]["lock"] == "BUSY"
    assert instance.queries.get(run.id) == run


def test_refresh_no_run_and_zero_candidate_exit(setup):
    instance, provider, raw, _ = setup
    watchlist = instance.watchlists.import_content("test", b"GOOD\nBAD")
    items = instance.market.refresh(watchlist.id, instance.scans.calendar).items
    assert len(items) == 2 and items[0].available and items[1].error
    assert instance.queries.list() == ()
    result = call(
        instance, "data", "refresh", "--watchlist", str(watchlist.id), "--as-of", "2026-09-04"
    )
    assert result.exit_code == 3
    assert instance.queries.list() == ()
    provider.data["GOOD"] = RawPrices(
        tuple((s, 100 - i * 0.1) for i, (s, _) in enumerate(raw.rows))
    )
    watchlist = instance.watchlists.import_content("zero", b"GOOD")
    zero = instance.scans.scan(watchlist.id, mode=DataMode.FORCE)
    assert zero.counts.candidate == 0 and zero.state == RunState.SUCCEEDED
    assert call(instance, "replay", str(zero.id), "--format", "json").exit_code == 0


@pytest.mark.parametrize("mismatch", ["config", "engine", "basis", "session", "future", "failed"])
def test_baseline_exact_compatible_before_start(setup, mismatch):
    from qscan.domain.rules import RuleConfig

    instance, _, raw, clock = setup
    watchlist = instance.watchlists.import_content("test", b"GOOD")
    prior = instance.scans.scan(watchlist.id, as_of=raw.rows[-2][0])
    changes = {}
    if mismatch == "config":
        rules = RuleConfig(version="1.0.1")
        changes = {"config_hash": rules.config_hash(), "rules": rules}
    elif mismatch == "engine":
        changes = {"context": prior.context.model_copy(update={"engine_version": "incompatible"})}
    elif mismatch == "basis":
        changes = {"price_basis": "other"}
    elif mismatch == "session":
        changes = {
            "context": prior.context.model_copy(
                update={"as_of_session": raw.rows[-3][0], "reference_session": raw.rows[-4][0]}
            )
        }
    elif mismatch == "future":
        changes = {"finished_at": clock.instant + timedelta(days=1)}
    else:
        changes = {"state": RunState.FAILED}
    changed = prior.model_copy(update=changes)
    with instance.engine.begin() as connection:
        connection.execute(
            text("UPDATE scan_runs SET document=:d WHERE id=:id"),
            {"d": changed.model_dump_json(exclude={"results"}), "id": str(prior.id)},
        )
    today = instance.scans.scan(watchlist.id)
    assert today.comparison.previous_run_id is None
    assert not any("NEW_CANDIDATE" in c.codes for c in today.comparison.changes)
    if mismatch in {"config", "engine", "basis"}:
        assert today.comparison.changes[0].codes == ("COMPARISON_UNAVAILABLE",)


def test_comparison_invalid_evaluations_not_dropped(setup):
    instance, provider, raw, clock = setup
    watchlist = instance.watchlists.import_content("test", b"GOOD\nSECOND")
    provider.data["SECOND"] = raw
    prior = instance.scans.scan(watchlist.id, as_of=raw.rows[-2][0])
    clock.instant += timedelta(seconds=1)
    provider.data["GOOD"] = RawPrices(raw.rows[-20:])
    today = instance.scans.scan(watchlist.id, mode=DataMode.FORCE)
    change = next(c for c in today.comparison.changes if c.symbol == "GOOD")
    assert change.codes == ("COMPARISON_UNAVAILABLE",)
    assert today.comparison.previous_run_id == prior.id


def test_baseline_tie_stable_and_top_keeps_all(setup):
    instance, provider, raw, _ = setup
    provider.data["SECOND"] = raw
    watchlist = instance.watchlists.import_content("test", b"GOOD\nSECOND")
    a = instance.scans.scan(watchlist.id, as_of=raw.rows[-2][0])
    b = instance.scans.scan(watchlist.id, as_of=raw.rows[-2][0])
    today = instance.scans.scan(watchlist.id)
    assert today.comparison.previous_run_id == max(a.id, b.id)
    report = instance.reports.build(today.id, top=1)
    assert len(report.charts) == 1
    assert len(report.run.results) == 2 and report.run.counts.candidate == 2
    assert len(list(csv.DictReader(io.StringIO(render(report, "csv").decode("utf-8-sig"))))) == 2


def test_migration_preserves_v1_cache_and_runs(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import insert

    from qscan.adapters.persistence.migrations.schema_v1 import instruments, prices
    from qscan.adapters.persistence.repository import migrate, open_database
    from qscan.adapters.providers import resolve_us

    directory = tmp_path / "old"
    directory.mkdir()
    engine = open_database(directory / "qscan.sqlite3")
    config = Config()
    import qscan.adapters.persistence.repository as repository

    config.set_main_option("script_location", str(Path(repository.__file__).parent / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001")
        instrument = resolve_us("OLD")
        provenance = {"provider": "fixture", "fetched_at": "2026-09-05T12:00:00Z"}
        connection.execute(
            insert(instruments).values(
                id=str(instrument.id),
                provider_symbol="OLD",
                market="US",
                document=instrument.model_dump(mode="json"),
                cache_info={
                    "provenance": provenance,
                    "reviewed_at": "2026-09-05T12:00:00Z",
                    "trusted": True,
                },
            )
        )
        connection.execute(
            insert(prices).values(
                instrument_id=str(instrument.id),
                session="2026-09-04",
                close=100,
                price_basis="split_adjusted_close",
                provider="fixture",
                fetched_at="2026-09-05T12:00:00Z",
            )
        )
    migrate(engine)
    migrate(engine)
    engine.dispose()
    instance = bootstrap(FixtureProvider({}), data_dir=directory)
    try:
        assert instance.repository.cache(instrument).series.closes == (100.0,)
        with instance.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM prices")) == 1
    finally:
        instance.close()


def test_csv_formula_and_negative_value_in_actual_export(setup):
    instance, *_ = setup
    _, _, run = pair(setup)
    report = instance.reports.build(run.id)
    result = run.results[0]
    window = result.analysis.selected_window.model_copy(
        update={"features": {"distance_to_resistance": -0.2}}
    )
    changed = result.model_copy(
        update={
            "instrument": result.instrument.model_copy(update={"display_symbol": "=evil()"}),
            "analysis": result.analysis.model_copy(update={"selected_window": window}),
        }
    )
    report = report.model_copy(update={"run": run.model_copy(update={"results": (changed,)})})
    row = next(csv.DictReader(io.StringIO(render(report, "csv").decode("utf-8-sig"))))
    assert row["symbol"] == "'=evil()"
    assert row["resistance_distance_ratio"] == "-0.2"


def test_report_render_failure_keeps_run_and_export(setup, tmp_path, monkeypatch):
    instance, *_ = setup
    _, _, run = pair(setup)
    report = instance.reports.build(run.id)
    target = export(report, tmp_path, "html")
    original = target.read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError("render")

    monkeypatch.setattr("qscan.adapters.report_renderer.chart_png", fail)
    result = call(instance, "report", str(run.id), "--output", str(tmp_path))
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "REPORT_ERROR"
    assert target.read_bytes() == original
    assert instance.queries.get(run.id) == run


@pytest.mark.parametrize("transition", ["new", "dropped", "window"])
def test_comparison_candidate_and_window_codes(setup, transition):
    instance, *_ = setup
    _, first, second = pair(setup)
    prior = first.results[0]
    current = second.results[0]
    if transition == "new":
        prior = prior.model_copy(
            update={
                "analysis": prior.analysis.model_copy(
                    update={
                        "is_candidate": False,
                        "score": None,
                        "stage": None,
                        "selected_window": None,
                    }
                )
            }
        )
    elif transition == "dropped":
        current = current.model_copy(
            update={
                "analysis": current.analysis.model_copy(
                    update={
                        "is_candidate": False,
                        "score": None,
                        "stage": None,
                        "selected_window": None,
                    }
                )
            }
        )
    else:
        current = current.model_copy(
            update={
                "analysis": current.analysis.model_copy(
                    update={
                        "selected_window": current.analysis.selected_window.model_copy(
                            update={"window_sessions": 10}
                        )
                    }
                )
            }
        )
    with instance.engine.begin() as connection:
        connection.execute(
            text("UPDATE scan_results SET document=:d WHERE run_id=:id"),
            {"d": prior.model_dump_json(), "id": str(first.id)},
        )
    changed = second.model_copy(update={"results": (current,)})
    comparison = instance.comparisons.complete(changed, instance.snapshots.read(second.input_hash))
    codes = comparison.changes[0].codes
    assert {"new": "NEW_CANDIDATE", "dropped": "DROPPED_CANDIDATE", "window": "WINDOW_CHANGED"}[
        transition
    ] in codes
    if transition != "window":
        assert "SCORE_CHANGED" in codes


def test_provider_caches_can_coexist(setup):
    from qscan.application.contracts import CacheEntry, Provenance

    instance, _, _, clock = setup
    watchlist = instance.watchlists.import_content("test", b"GOOD")
    instance.scans.scan(watchlist.id)
    instrument = watchlist.instruments[0]
    cached = instance.repository.cache(instrument)
    other = bootstrap(YahooProvider(), data_dir=instance.data_dir, clock=clock)
    try:
        other.repository.replace_cache(
            instrument,
            CacheEntry(
                series=cached.series,
                provenance=Provenance(provider="yahoo", fetched_at=clock.instant),
                reviewed_at=clock.instant,
            ),
        )
        assert other.repository.cache(instrument).provenance.provider == "yahoo"
        assert instance.repository.cache(instrument) == cached
    finally:
        other.close()


def test_data_dir_precedence_and_summaries(setup, tmp_path, monkeypatch):
    instance, *_ = setup
    _, _, run = pair(setup)
    monkeypatch.setenv("QSCAN_DATA_DIR", str(tmp_path / "wrong"))
    assert call(instance, "scans", "show", str(run.id)).exit_code == 0
    monkeypatch.setenv("QSCAN_DATA_DIR", str(instance.data_dir))
    result = runner.invoke(cli, ["scans", "list", "--limit", "1"])
    values = json.loads(result.stdout)
    assert len(values) == 1 and "results" not in values[0]
    assert "snapshots" not in result.stdout


def test_ambiguous_lookup_demands_id(setup, monkeypatch):
    instance, *_ = setup
    watchlist = instance.watchlists.import_content("same", b"GOOD")
    monkeypatch.setattr(instance.repository, "watchlists", lambda: (watchlist, watchlist))
    with pytest.raises(ApplicationError, match="UUID"):
        instance.watchlists.lookup("same")
    assert instance.watchlists.lookup(str(watchlist.id)) == watchlist


def test_partial_failed_running_exports_preserve_state(setup, tmp_path):
    instance, *_ = setup
    pair(setup)
    for symbols, expected in [(b"GOOD\nBAD", RunState.PARTIAL), (b"BAD", RunState.FAILED)]:
        watchlist = instance.watchlists.import_content(expected.value, symbols)
        run = instance.scans.scan(watchlist.id, mode=DataMode.CACHE_ONLY)
        assert run.state == expected
        result = call(
            instance, "report", str(run.id), "--format", "json", "--output", str(tmp_path)
        )
        assert result.exit_code == 0
        exported = json.loads((tmp_path / f"scan-{run.id}.json").read_text())
        assert exported["run"]["state"] == expected
        if expected == RunState.FAILED:
            assert "RUN_FAILED" in exported["chart_error"]
        assert instance.queries.get(run.id) == run
    from uuid import uuid4

    running = run.model_copy(
        update={
            "id": uuid4(),
            "state": RunState.RUNNING,
            "input_hash": None,
            "finished_at": None,
            "results": (),
        }
    )
    instance.repository.create_run(running)
    assert "RUN_RUNNING" in instance.reports.build(running.id).chart_error


def test_baseline_snapshot_missing_reports_unavailable(setup):
    instance, _, raw, _ = setup
    watchlist = instance.watchlists.import_content("test", b"GOOD")
    first = instance.scans.scan(watchlist.id, as_of=raw.rows[-2][0])
    instance.snapshots.path(first.input_hash).unlink()
    today = instance.scans.scan(watchlist.id)
    assert today.state == RunState.SUCCEEDED
    assert today.comparison.previous_run_id == first.id
    assert today.comparison.reasons == ("BASELINE_SNAPSHOT_UNAVAILABLE",)


def test_doctor_detects_missing_schema_table(setup):
    instance, *_ = setup
    with instance.engine.begin() as connection:
        connection.execute(text("DROP TABLE scan_results"))
    result = call(instance, "doctor")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["checks"]["database"] == "SCHEMA_UNAVAILABLE"
