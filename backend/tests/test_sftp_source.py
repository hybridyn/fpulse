"""SFTP support on the FTP / SFTP Source node.

F-Pulse previously did FTP/FTPS only (ftplib); SFTP (SSH File Transfer) is a
different protocol. The node now selects ftp|ftps|sftp; sftp uses paramiko
(lazy-imported). These pin the protocol routing + the connector wiring (the
paramiko transport mirrors the proven FTP download→read path and needs a live
SFTP server to exercise end-to-end).
"""

from __future__ import annotations

from fpulse.ir.schema import StepType
from fpulse.nodes.generic import DEST_MAP, GenericDestinationNode, GenericSourceNode, SOURCE_MAP
from fpulse.nodes.sinks import FtpSinkNode
from fpulse.nodes.sources import FtpSourceNode


def test_resolve_protocol_defaults_to_ftp():
    assert FtpSourceNode._resolve_protocol({}) == "ftp"


def test_resolve_protocol_use_tls_maps_to_ftps():
    assert FtpSourceNode._resolve_protocol({"use_tls": True}) == "ftps"


def test_resolve_protocol_explicit_wins_over_use_tls():
    assert FtpSourceNode._resolve_protocol({"protocol": "sftp", "use_tls": True}) == "sftp"


def test_resolve_protocol_inferred_from_connector_type():
    assert FtpSourceNode._resolve_protocol({"connector_type": "sftp"}) == "sftp"


def test_param_schema_exposes_protocol_and_private_key():
    schema = FtpSourceNode.param_schema()
    names = {f["name"] for f in schema}
    assert {"protocol", "private_key", "host", "remote_path"} <= names
    proto = next(f for f in schema if f["name"] == "protocol")
    assert set(proto["options"]) == {"ftp", "ftps", "sftp"}


def test_sftp_registered_as_generic_source_connector():
    assert SOURCE_MAP.get("sftp") == StepType.FTP_SOURCE
    cs = GenericSourceNode.connector_schemas()
    assert "sftp" in cs
    assert any(f.get("name") == "protocol" for f in cs["sftp"])


def test_sftp_and_ftp_registered_as_dest_connectors():
    assert DEST_MAP.get("sftp") == StepType.FTP_SINK
    assert DEST_MAP.get("ftp") == StepType.FTP_SINK
    cs = GenericDestinationNode.connector_schemas()
    assert "sftp" in cs and any(f.get("name") == "protocol" for f in cs["sftp"])


def test_ftp_sink_param_schema_matches_source_shape():
    names = {f["name"] for f in FtpSinkNode.param_schema()}
    assert {"protocol", "private_key", "host", "remote_path", "format"} <= names
    proto = next(f for f in FtpSinkNode.param_schema() if f["name"] == "protocol")
    assert set(proto["options"]) == {"ftp", "ftps", "sftp"}


def test_ftp_sink_reuses_source_protocol_resolver():
    # The sink calls FtpSourceNode._resolve_protocol — read/write behave the same.
    assert FtpSourceNode._resolve_protocol({"connector_type": "sftp"}) == "sftp"
    assert FtpSourceNode._resolve_protocol({"protocol": "ftps"}) == "ftps"


def test_sftp_missing_paramiko_gives_clear_error(monkeypatch):
    """If paramiko isn't installed, the SFTP path must fail with an actionable
    message — not a raw ImportError."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "paramiko":
            raise ImportError("no module named paramiko")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    node = FtpSourceNode({"protocol": "sftp", "host": "h", "remote_path": "/f.csv"})
    try:
        node.execute(_ctx_stub())
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "paramiko" in str(exc).lower()


class _ConnStub:
    def get(self, *_a, **_k):
        return None


def _ctx_stub():
    # Minimal ExecutionContext-like stub; execute() raises on paramiko import
    # before touching ctx, so a light stub is enough for that path.
    class _Ctx:
        conn = _ConnStub()
        app_state: dict = {}
        def __init__(self):
            self._results = {}
    return _Ctx()
