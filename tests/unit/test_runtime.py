import io
import json
from urllib.parse import parse_qs, urlsplit

from qscan.interfaces.api.localauth import ensure_token
from qscan.runtime import existing_instance, instance_challenge


def test_existing_instance_rejects_a_different_provider(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    token_path = data_dir / "api-token.json"
    ensure_token(token_path)
    credentials = json.loads(token_path.read_text())

    def urlopen(request, timeout):
        nonce = parse_qs(urlsplit(str(request)).query)["nonce"][0]
        challenge = instance_challenge(
            credentials["instance_id"],
            credentials["instance_secret"],
            data_dir,
            nonce,
        ) | {"provider": "fixture"}
        return io.BytesIO(json.dumps(challenge).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    assert existing_instance(
        "http://127.0.0.1:8000",
        token_path,
        data_dir,
        expected_provider="fixture",
    )
    assert not existing_instance(
        "http://127.0.0.1:8000",
        token_path,
        data_dir,
        expected_provider="yahoo",
    )
