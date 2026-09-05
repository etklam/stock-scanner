"""Synchronous CLI transports over shared application services."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import typer
from filelock import Timeout
from pydantic import ValidationError
from typer import _click as click
from typer.core import TyperGroup

from qscan import __version__
from qscan.application.contracts import ApplicationError, Run
from qscan.bootstrap import Application
from qscan.domain.models import DataMode, RunState


class Output(StrEnum):
    TEXT = "text"
    JSON = "json"


class ExportFormat(StrEnum):
    JSON = "json"
    CSV = "csv"
    HTML = "html"


def emit(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2))


class CommandGroup(TyperGroup):
    def main(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["standalone_mode"] = False
        try:
            result = super().main(*args, **kwargs)
            if isinstance(result, int):
                raise SystemExit(result)
            return result
        except typer.Exit as exc:
            raise SystemExit(exc.exit_code) from exc
        except (click.ClickException, ValueError, ValidationError) as exc:
            emit(
                {
                    "error": {
                        "code": "VALIDATION_ERROR",
                        "message": "Invalid command input",
                        "details": {"hint": "Check --help, UUID, ISO date and absolute data-dir"},
                    }
                }
            )
            raise SystemExit(2) from exc
        except Timeout as exc:
            emit({"error": {"code": "EXECUTOR_LOCKED", "message": "Executor busy; retry later"}})
            raise SystemExit(4) from exc
        except ApplicationError as exc:
            emit({"error": {"code": exc.code.value, "message": str(exc)}})
            code = 1 if exc.code.value in {"INTERNAL_ERROR", "SCAN_FAILED", "REPORT_ERROR"} else 2
            raise SystemExit(code) from exc
        except FileNotFoundError as exc:
            emit({"error": {"code": "NOT_FOUND", "message": "Input file not found"}})
            raise SystemExit(2) from exc
        except PermissionError as exc:
            emit({"error": {"code": "FORBIDDEN", "message": "Local file permission denied"}})
            raise SystemExit(2) from exc
        except Exception as exc:
            emit(
                {
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "Operation failed; local data retained",
                    }
                }
            )
            raise SystemExit(1) from exc


app = typer.Typer(no_args_is_help=True, cls=CommandGroup)
watchlists = typer.Typer(no_args_is_help=True)
scans = typer.Typer(no_args_is_help=True)
data = typer.Typer(no_args_is_help=True)
app.add_typer(watchlists, name="watchlist")
app.add_typer(scans, name="scans")
app.add_typer(data, name="data")


class Source(StrEnum):
    YAHOO = "yahoo"
    FIXTURE = "fixture"


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show engine version.")] = False,
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Absolute local data path.")
    ] = None,
    provider: Annotated[
        Source, typer.Option("--provider", help="Fixture is SYNTHETIC / DEMO.")
    ] = Source.YAHOO,
) -> None:
    ctx.obj = (data_dir, provider)
    if version:
        typer.echo(f"qscan {__version__}")
        raise typer.Exit()


@contextmanager
def application(
    ctx: typer.Context, *, initialize: bool = False, readonly: bool = False
) -> Iterator[Application]:
    from qscan.adapters.providers import FixtureProvider, YahooProvider
    from qscan.bootstrap import bootstrap, resolve_data_dir

    directory, source = ctx.obj
    directory = resolve_data_dir(directory)
    provider = FixtureProvider({}) if source == Source.FIXTURE else YahooProvider()
    instance = bootstrap(provider, data_dir=directory, initialize=initialize, readonly=readonly)
    try:
        yield instance
    finally:
        instance.close()


@app.command()
def init(ctx: typer.Context) -> None:
    """Create or upgrade local storage, preserving existing data."""
    with application(ctx, initialize=True):
        emit({"initialized": True, "provider_release": "BLOCKED"})


@watchlists.command("import")
def import_watchlist(
    ctx: typer.Context,
    name: Annotated[str, typer.Option()],
    file: Annotated[Path, typer.Option()],
    replace: Annotated[bool, typer.Option()] = False,
    expected_revision: Annotated[int | None, typer.Option()] = None,
) -> None:
    with application(ctx) as instance:
        with file.open("rb") as stream:
            content = stream.read(1_000_001)
        value = instance.watchlists.import_named(
            name,
            content,
            file.suffix.lower().lstrip("."),
            replace=replace,
            expected_revision=expected_revision,
        )
        emit(value)


@watchlists.command("list")
def list_watchlists(ctx: typer.Context) -> None:
    with application(ctx, readonly=True) as instance:
        emit([w.model_dump(mode="json") for w in instance.watchlists.list()])


def run_output(run: Run, format: Output, directory: Path) -> None:
    warnings = sorted(
        {
            w
            for r in run.results
            for w in (*r.warnings, *(r.provenance.warnings if r.provenance else ()))
        }
    )
    if any(r.provenance and r.provenance.provider == "fixture" for r in run.results):
        warnings.append("SYNTHETIC / DEMO")
    for warning in warnings:
        typer.echo(warning, err=True)
    if any(
        r.analysis.reasons and r.analysis.reasons[0].code == "ADJUSTMENT_REVIEW_REQUIRED"
        for r in run.results
    ):
        typer.echo(
            "Yahoo release BLOCKED: basis, metadata and incomplete-session review pending. "
            "Use demo, cache_only, report or snapshot replay.",
            err=True,
        )
    if format == Output.JSON:
        emit(run)
    else:
        typer.echo(
            f"Run {run.id} | {run.state.value}\nAs-of {run.context.as_of_session} | "
            f"reference {run.context.reference_session}\nCounts {run.counts.model_dump()}\n"
            f'Report: qscan --data-dir "{directory}" report {run.id} '
            "--format html --output ./output"
        )
    raise typer.Exit(1 if not run.counts.evaluated else 3 if run.state == RunState.PARTIAL else 0)


@app.command()
def scan(
    ctx: typer.Context,
    watchlist: Annotated[str, typer.Option()],
    ruleset: Annotated[str, typer.Option()] = "breakout-v1",
    as_of: Annotated[str | None, typer.Option()] = None,
    data_mode: Annotated[DataMode, typer.Option()] = DataMode.AUTO,
    format: Annotated[Output, typer.Option()] = Output.TEXT,
) -> None:
    if ruleset != "breakout-v1":
        raise ValueError("Unknown ruleset")
    requested = date.fromisoformat(as_of) if as_of else None
    with application(ctx) as instance:
        run_output(
            instance.scans.scan(
                instance.watchlists.lookup(watchlist).id, as_of=requested, mode=data_mode
            ),
            format,
            instance.data_dir,
        )


@data.command()
def refresh(
    ctx: typer.Context,
    watchlist: Annotated[str, typer.Option()],
    as_of: Annotated[str | None, typer.Option()] = None,
) -> None:
    with application(ctx) as instance:
        result = instance.market.refresh(
            instance.watchlists.lookup(watchlist).id,
            instance.scans.calendar,
            date.fromisoformat(as_of) if as_of else None,
        )
        emit(result)
        for item in result.items:
            for warning in item.warnings:
                typer.echo(warning, err=True)
        if any(i.error and i.error.value == "ADJUSTMENT_REVIEW_REQUIRED" for i in result.items):
            typer.echo(
                "Yahoo release BLOCKED: basis, metadata and incomplete-session review pending. "
                "Offline demo/cache_only/report/replay remain available.",
                err=True,
            )
        raise typer.Exit(
            1 if not result.successful else 3 if result.failed or result.has_warnings else 0
        )


@scans.command("list")
def list_scans(
    ctx: typer.Context, limit: Annotated[int, typer.Option(min=1, max=200)] = 30
) -> None:
    with application(ctx, readonly=True) as instance:
        emit(
            [
                r.model_dump(mode="json", exclude={"results", "watchlist", "comparison", "rules"})
                | {"watchlist_id": str(r.watchlist.id), "watchlist_name": r.watchlist.name}
                for r in instance.queries.summaries(limit)
            ]
        )


@scans.command()
def show(
    ctx: typer.Context, scan_id: UUID, format: Annotated[Output, typer.Option()] = Output.JSON
) -> None:
    with application(ctx, readonly=True) as instance:
        emit(instance.queries.get(scan_id))


@scans.command()
def changes(
    ctx: typer.Context, scan_id: UUID, format: Annotated[Output, typer.Option()] = Output.JSON
) -> None:
    with application(ctx, readonly=True) as instance:
        emit(instance.comparisons.get(scan_id))


@app.command()
def replay(
    ctx: typer.Context, scan_id: UUID, format: Annotated[Output, typer.Option()] = Output.TEXT
) -> None:
    with application(ctx) as instance:
        run_output(instance.scans.replay(scan_id), format, instance.data_dir)


@app.command()
def report(
    ctx: typer.Context,
    scan_id: UUID,
    output: Annotated[Path, typer.Option()],
    format: Annotated[ExportFormat, typer.Option()] = ExportFormat.HTML,
    top: Annotated[int, typer.Option(min=1, max=50)] = 30,
    all_results: Annotated[bool, typer.Option()] = False,
) -> None:
    from qscan.adapters.report_renderer import export

    with application(ctx, readonly=True) as instance:
        value = instance.reports.build(scan_id, top)
        path = export(value, output, format.value, all_results=all_results)
        emit(
            {
                "scan_id": str(scan_id),
                "state": value.run.state.value,
                "output": str(path.resolve()),
                "chart_error": value.chart_error,
            }
        )


@app.command()
def doctor(ctx: typer.Context, online: Annotated[bool, typer.Option()] = False) -> None:
    from qscan.adapters.diagnostics import diagnose
    from qscan.bootstrap import resolve_data_dir

    result = diagnose(resolve_data_dir(ctx.obj[0]), online)
    emit(result)
    raise typer.Exit(0 if result.local_healthy else 2)


@app.command()
def demo(ctx: typer.Context, output: Annotated[Path | None, typer.Option()] = None) -> None:
    """Seed independent SYNTHETIC watchlists, cache and adjacent-session runs."""
    from qscan.bootstrap import resolve_data_dir
    from qscan.demo import seed_demo

    directory = resolve_data_dir(ctx.obj[0])
    result = seed_demo(directory)
    if output is not None:
        from qscan.adapters.providers import FixtureProvider
        from qscan.adapters.report_renderer import export
        from qscan.bootstrap import bootstrap

        instance = bootstrap(
            FixtureProvider({}), data_dir=directory, initialize=False, readonly=True
        )
        try:
            value = instance.reports.build(UUID(str(result["scan_id"])))
            formats: tuple[Literal["json", "csv", "html"], ...] = ("json", "csv", "html")
            result["reports"] = [str(export(value, output, format).resolve()) for format in formats]
        finally:
            instance.close()
    emit(result)
