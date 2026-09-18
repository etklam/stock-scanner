import json
import sys
from types import SimpleNamespace

from typer.testing import CliRunner

from qscan.interfaces.api.localauth import ensure_token
from qscan.interfaces.cli import app, autostart_manager


class SuccessfulResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_installed_cli_version():
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "qscan 0.1.0"


def test_autostart_resolves_a_path_command_to_the_installed_entrypoint(tmp_path, monkeypatch):
    executable = tmp_path / "bin" / "qscan"
    monkeypatch.setattr(sys, "argv", ["qscan"])
    monkeypatch.setattr("qscan.interfaces.cli.shutil.which", lambda command: str(executable))

    manager = autostart_manager(SimpleNamespace(obj=(tmp_path / "data", None)))

    assert manager.executable == executable.resolve()


def test_start_rejects_non_loopback_before_creating_storage(tmp_path):
    data_dir = tmp_path / "new data"
    result = CliRunner().invoke(app, ["--data-dir", str(data_dir), "start", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert result.stdout == (
        '{\n  "error": {\n    "code": "FORBIDDEN",\n'
        '    "message": "start is restricted to loopback"\n  }\n}\n'
    )
    assert not data_dir.exists()


def test_start_reuses_only_a_verified_instance_without_exposing_secrets(tmp_path, monkeypatch):
    data_dir = tmp_path / "existing data"
    token_path = data_dir / "api-token.json"
    token, _ = ensure_token(token_path)
    opened: list[str] = []
    monkeypatch.setattr("qscan.runtime.existing_instance", lambda *args, **kwargs: True)
    monkeypatch.setattr("qscan.runtime.open_browser", opened.append)
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: SuccessfulResponse())

    result = CliRunner().invoke(app, ["--data-dir", str(data_dir), "start", "--port", "8123"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "reused": True,
        "url": "http://127.0.0.1:8123/ui/",
    }
    assert opened == ["http://127.0.0.1:8123/ui/"]
    assert token not in result.stdout
    assert not (data_dir / "qscan.sqlite3").exists()


def test_start_no_browser_reuses_instance_without_opening_ui(tmp_path, monkeypatch):
    data_dir = tmp_path / "existing data"
    ensure_token(data_dir / "api-token.json")
    opened: list[str] = []
    monkeypatch.setattr("qscan.runtime.existing_instance", lambda *args, **kwargs: True)
    monkeypatch.setattr("qscan.runtime.open_browser", opened.append)
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: SuccessfulResponse())

    result = CliRunner().invoke(
        app,
        ["--data-dir", str(data_dir), "start", "--port", "8123", "--no-browser"],
    )

    assert result.exit_code == 0
    assert opened == []


def test_start_does_not_claim_reuse_when_verified_instance_disappears(tmp_path, monkeypatch):
    data_dir = tmp_path / "existing data"
    ensure_token(data_dir / "api-token.json")
    monkeypatch.setattr("qscan.runtime.existing_instance", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("gone")),
    )

    result = CliRunner().invoke(app, ["--data-dir", str(data_dir), "start"])

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "INSTANCE_UNAVAILABLE"


def test_autostart_launch_preserves_persisted_pause(tmp_path, monkeypatch):
    data_dir = tmp_path / "fresh data"
    served: list[dict[str, object]] = []
    monkeypatch.setattr("qscan.runtime.existing_instance", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "qscan.interfaces.api.app.run_serve",
        lambda provider, **kwargs: served.append(kwargs) or 0,
    )

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "--provider",
            "fixture",
            "start",
            "--no-browser",
            "--preserve-automation",
        ],
    )

    assert result.exit_code == 0
    assert served[0]["automation"] is False


def test_start_initializes_then_runs_and_opens_only_the_local_ui(tmp_path, monkeypatch):
    data_dir = tmp_path / "fresh data"
    opened: list[tuple[str, object, object]] = []
    served: list[dict[str, object]] = []
    monkeypatch.setattr("qscan.runtime.existing_instance", lambda *args: False)
    monkeypatch.setattr(
        "qscan.runtime.open_browser_when_ready",
        lambda url, token_path, directory, **kwargs: opened.append((url, token_path, directory)),
    )
    monkeypatch.setattr(
        "qscan.interfaces.api.app.run_serve",
        lambda provider, **kwargs: served.append(kwargs) or 0,
    )

    result = CliRunner().invoke(
        app,
        ["--data-dir", str(data_dir), "--provider", "fixture", "start", "--port", "8124"],
    )

    assert result.exit_code == 0
    assert (data_dir / "qscan.sqlite3").is_file()
    credentials = json.loads((data_dir / "api-token.json").read_text())
    stable_fields = ("instance_id", "instance_secret", "browser_session_secret")
    assert all(credentials[name] for name in stable_fields)
    assert opened == [
        ("http://127.0.0.1:8124/ui/", data_dir / "api-token.json", data_dir.resolve())
    ]
    assert served[0]["host"] == "127.0.0.1"
    assert credentials["token"] not in result.stdout


def test_run_serve_itself_rejects_non_loopback_before_touching_storage(tmp_path, capsys):
    from qscan.interfaces.api.app import run_serve

    data_dir = tmp_path / "forbidden"
    result = run_serve(
        object(),
        data_dir=data_dir,
        token_path=data_dir / "api-token.json",
        host="192.0.2.1",
    )

    assert result == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "FORBIDDEN"
    assert not data_dir.exists()
