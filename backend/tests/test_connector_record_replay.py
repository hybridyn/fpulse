import json

import fpulse.connectors.rest_framework as rest_framework
from tools.test_connector import ConnectorCassette, cmd_run, install_cassette


def test_connector_record_cassette_redacts_headers_and_params(monkeypatch, tmp_path):
    cassette_path = tmp_path / "github.cassette.json"
    cassette = ConnectorCassette(cassette_path, "record")

    def fake_http(url, headers, method="GET", body=None, body_text=None):
        assert headers["Authorization"] == "Bearer ghp_secret"
        return [{"id": 1, "name": "demo"}], {"Link": ""}

    monkeypatch.setattr(rest_framework, "_http_request", fake_http)
    original = install_cassette(cassette)
    params = {"owner": "octo", "personal_access_token": "ghp_secret"}
    try:
        rc = cmd_run("github", "repos", params, max_rows=1)
    finally:
        rest_framework._http_request = original

    assert rc == 0
    cassette.write(connector_id="github", stream_name="repos", params=params)
    payload = json.loads(cassette_path.read_text(encoding="utf-8"))
    assert payload["params"]["personal_access_token"] == "[REDACTED]"
    assert payload["calls"][0]["request"]["headers"]["Authorization"] == "[REDACTED]"
    assert payload["calls"][0]["response"]["payload"] == [{"id": 1, "name": "demo"}]


def test_connector_replay_cassette_uses_recorded_response(tmp_path, capsys):
    cassette_path = tmp_path / "github.cassette.json"
    cassette_path.write_text(
        json.dumps(
            {
                "version": 1,
                "connector_id": "github",
                "stream": "repos",
                "params": {"owner": "octo", "personal_access_token": "[REDACTED]"},
                "calls": [
                    {
                        "request": {
                            "method": "GET",
                            "url": "https://api.github.com/users/octo/repos?per_page=100",
                            "headers": {"Authorization": "[REDACTED]"},
                            "body": None,
                            "body_text": None,
                        },
                        "response": {
                            "payload": [{"id": 2, "name": "from-cassette"}],
                            "headers": {"Link": ""},
                        },
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    cassette = ConnectorCassette(cassette_path, "replay")
    original = install_cassette(cassette)
    try:
        rc = cmd_run(
            "github",
            "repos",
            {"owner": "octo", "personal_access_token": "dummy"},
            max_rows=1,
        )
    finally:
        rest_framework._http_request = original

    assert rc == 0
    out = capsys.readouterr().out
    assert "OK: 1 row(s) returned" in out
    assert "row[0] keys" in out
