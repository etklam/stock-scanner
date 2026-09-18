import html
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from qscan.adapters.calendar import FixedClock
from qscan.adapters.providers import FixtureProvider
from qscan.adapters.universe import WikipediaSP500Source
from qscan.application.contracts import ApplicationError
from qscan.bootstrap import bootstrap
from qscan.domain.models import ErrorCode


def _api_payload(symbols: list[str], *, revision: int = 123) -> bytes:
    rows = "".join(
        "<tr>"
        f"<td><a>{html.escape(symbol)}</a></td>"
        f"<td>Company {position}</td><td>Industrials</td><td>Industry</td>"
        f"<td>New York, New York</td><td>000{position:07d}</td>"
        f"<td>2000-01-01</td><td>{position}</td><td>1900</td>"
        "</tr>"
        for position, symbol in enumerate(symbols)
    )
    table = (
        '<table id="constituents"><thead><tr>'
        "<th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th>"
        "<th>Headquarters Location</th><th>CIK</th><th>Date added</th>"
        "<th>Founded</th><th>Founded</th></tr></thead><tbody>"
        f"{rows}</tbody></table>"
    )
    return json.dumps(
        {
            "curtimestamp": "2026-09-18T01:02:03Z",
            "parse": {"revid": revision, "text": table},
        }
    ).encode()


def _unavailable(_: str) -> bytes:
    raise OSError("offline")


def test_refresh_accepts_actual_503_members_and_persists_the_managed_watchlist(tmp_path):
    symbols = ["BRK.B", "BF.B", *(f"S{i:03d}" for i in range(501))]
    source = WikipediaSP500Source(fetch=lambda _: _api_payload(symbols))
    clock = FixedClock(datetime(2026, 9, 18, 2, tzinfo=UTC))
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "qscan",
        clock=clock,
        universe_source=source,
    )
    try:
        snapshot = app.universe.refresh()

        assert snapshot.actual_member_count == 503
        assert snapshot.effective_date is None
        assert snapshot.source_revision == "123"
        assert snapshot.observed_at == datetime(2026, 9, 18, 1, 2, 3, tzinfo=UTC)
        assert len(snapshot.content_hash) == 64
        assert app.repository.watchlist(snapshot.watchlist_id).instruments == tuple(
            member.instrument for member in snapshot.members
        )
    finally:
        app.close()


def test_malformed_refresh_keeps_the_durable_last_known_good(tmp_path):
    symbols = [f"S{i:03d}" for i in range(500)]
    responses = [_api_payload(symbols), b'{"parse":{"revid":124,"text":"<p>broken</p>"}}']
    source = WikipediaSP500Source(fetch=lambda _: responses.pop(0))
    clock = FixedClock(datetime(2026, 9, 18, 2, tzinfo=UTC))
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "qscan",
        clock=clock,
        universe_source=source,
    )
    try:
        accepted = app.universe.refresh()
        assert app.universe.refresh() == accepted
        assert app.universe.current() == accepted
    finally:
        app.close()


def test_lkg_survives_reopen_and_an_unavailable_source(tmp_path):
    directory = tmp_path / "qscan"
    clock = FixedClock(datetime(2026, 9, 18, 2, tzinfo=UTC))
    source = WikipediaSP500Source(fetch=lambda _: _api_payload([f"S{i:03d}" for i in range(500)]))
    app = bootstrap(FixtureProvider({}), data_dir=directory, clock=clock, universe_source=source)
    accepted = app.universe.refresh()
    app.close()

    reopened = bootstrap(
        FixtureProvider({}),
        data_dir=directory,
        clock=clock,
        universe_source=WikipediaSP500Source(fetch=_unavailable),
    )
    try:
        assert reopened.universe.refresh() == accepted
    finally:
        reopened.close()


def test_unchanged_successful_refresh_renews_the_seven_day_lkg(tmp_path):
    payload = _api_payload([f"S{i:03d}" for i in range(500)])
    responses = [payload, payload, b"not-json"]
    source = WikipediaSP500Source(fetch=lambda _: responses.pop(0))
    clock = FixedClock(datetime(2026, 9, 1, tzinfo=UTC))
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "qscan",
        clock=clock,
        universe_source=source,
    )
    try:
        first = app.universe.refresh()
        clock.instant += timedelta(days=6)
        second = app.universe.refresh()
        assert second.id != first.id
        assert app.repository.watchlist(second.watchlist_id).revision == 1

        clock.instant += timedelta(days=6)
        assert app.universe.refresh() == second
    finally:
        app.close()


def test_lkg_is_usable_for_exactly_seven_days_and_then_rejected(tmp_path):
    payload = _api_payload([f"S{i:03d}" for i in range(500)])
    source = WikipediaSP500Source(fetch=lambda _: payload)
    clock = FixedClock(datetime(2026, 9, 1, tzinfo=UTC))
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "qscan",
        clock=clock,
        universe_source=source,
    )
    try:
        snapshot = app.universe.refresh()
        clock.instant += timedelta(days=7)
        assert app.universe.current() == snapshot

        clock.instant += timedelta(seconds=1)
        with pytest.raises(ApplicationError) as exc:
            app.universe.current()
        assert exc.value.code == ErrorCode.STALE_DATA
    finally:
        app.close()


def test_stable_instrument_and_watchlist_identities_preserve_share_classes(tmp_path):
    classes = ["BRK.A", "BRK.B", "BF.A", "BF.B"]
    first_symbols = [*classes, *(f"S{i:03d}" for i in range(496))]
    second_symbols = [*classes, *(f"S{i:03d}" for i in range(495)), "NEW"]
    responses = [_api_payload(first_symbols, revision=1), _api_payload(second_symbols, revision=2)]
    source = WikipediaSP500Source(fetch=lambda _: responses.pop(0))
    clock = FixedClock(datetime(2026, 9, 18, tzinfo=UTC))
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "qscan",
        clock=clock,
        universe_source=source,
    )
    try:
        manual = app.watchlists.create("manual", ["MANUAL"])
        first = app.universe.refresh()
        clock.instant += timedelta(seconds=1)
        second = app.universe.refresh()
        first_members = {member.canonical_symbol: member for member in first.members}
        second_members = {member.canonical_symbol: member for member in second.members}

        assert first.watchlist_id == second.watchlist_id
        assert app.repository.watchlist(manual.id) == manual
        assert {symbol: first_members[symbol].provider_symbol for symbol in classes} == {
            "BRK.A": "BRK-A",
            "BRK.B": "BRK-B",
            "BF.A": "BF-A",
            "BF.B": "BF-B",
        }
        assert len({first_members[symbol].instrument.id for symbol in classes}) == 4
        assert all(
            first_members[symbol].instrument.id == second_members[symbol].instrument.id
            for symbol in classes
        )
        assert first.source_url.startswith("https://en.wikipedia.org/wiki/")
        assert "CC BY-SA 4.0" in first.source_license
        assert first.retrieved_at == clock.instant - timedelta(seconds=1)
    finally:
        app.close()


def test_manual_watchlist_operations_cannot_mutate_the_managed_universe(tmp_path):
    symbols = [f"S{i:03d}" for i in range(500)]
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "qscan",
        clock=FixedClock(datetime(2026, 9, 18, tzinfo=UTC)),
        universe_source=WikipediaSP500Source(fetch=lambda _: _api_payload(symbols)),
    )
    try:
        snapshot = app.universe.refresh()
        managed = app.watchlists.lookup(str(snapshot.watchlist_id))

        for operation in (
            lambda: app.watchlists.rename(managed.id, "renamed", managed.revision),
            lambda: app.watchlists.delete(managed.id),
            lambda: app.watchlists.import_content(
                managed.name,
                b"MANUAL",
                watchlist_id=managed.id,
                expected_revision=managed.revision,
            ),
        ):
            with pytest.raises(ApplicationError) as exc:
                operation()
            assert exc.value.code == ErrorCode.FORBIDDEN

        assert app.watchlists.lookup(str(managed.id)) == managed
    finally:
        app.close()


def test_future_effective_universe_does_not_replace_the_current_snapshot(tmp_path):
    symbols = tuple(f"S{i:03d}" for i in range(500))

    class FutureSource:
        def fetch(self):
            from qscan.application.contracts import UniverseObservation

            return UniverseObservation(
                source_url="https://example.invalid/future",
                source_license="test fixture",
                source_revision="future",
                observed_at=datetime(2026, 9, 18, tzinfo=UTC),
                effective_date=date(2026, 9, 22),
                symbols=symbols,
            )

    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "qscan",
        clock=FixedClock(datetime(2026, 9, 18, tzinfo=UTC)),
        universe_source=FutureSource(),
    )
    try:
        with pytest.raises(ApplicationError) as exc:
            app.universe.refresh()
        assert exc.value.code == ErrorCode.STALE_DATA
        assert app.repository.current_universe("sp500") is None
    finally:
        app.close()


@pytest.mark.parametrize(
    "symbols",
    [
        [f"S{i:03d}" for i in range(449)],
        ["DUP", "DUP", *(f"S{i:03d}" for i in range(498))],
    ],
    ids=["suspicious_count", "duplicate_symbol"],
)
def test_invalid_refresh_cannot_succeed_without_a_valid_lkg(tmp_path, symbols):
    source = WikipediaSP500Source(fetch=lambda _: _api_payload(symbols))
    app = bootstrap(
        FixtureProvider({}),
        data_dir=tmp_path / "qscan",
        clock=FixedClock(datetime(2026, 9, 18, tzinfo=UTC)),
        universe_source=source,
    )
    try:
        with pytest.raises(ApplicationError) as exc:
            app.universe.refresh()
        assert exc.value.code == ErrorCode.STALE_DATA
    finally:
        app.close()
