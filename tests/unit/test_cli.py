from typer.testing import CliRunner

from qscan.interfaces.cli import app


def test_installed_cli_version():
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "qscan 0.1.0"
