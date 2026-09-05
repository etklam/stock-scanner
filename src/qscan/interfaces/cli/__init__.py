"""Minimal package verification command; scan workflows arrive in Phase 3."""

from typing import Annotated

import typer

from qscan import __version__

app = typer.Typer(no_args_is_help=True)


@app.callback(invoke_without_command=True)
def main(
    version: Annotated[bool, typer.Option("--version", help="Show engine version.")] = False,
) -> None:
    if version:
        typer.echo(f"qscan {__version__}")
        raise typer.Exit()
