"""S3 downloads must remain readable after temporary-file cleanup."""
import io
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from fpulse.nodes.sources import S3SourceNode


@pytest.mark.parametrize("fmt", ["csv", "json", "parquet"])
@pytest.mark.parametrize("transport", ["boto3", "http"])
def test_download_survives_cleanup(tmp_path, monkeypatch, fmt, transport):
    with duckdb.connect() as conn:
        path = tmp_path / f"fixture.{fmt}"
        if fmt == "parquet":
            conn.sql("SELECT 7 AS id, 'North' AS region").write_parquet(str(path))
        else:
            path.write_text('id,region\n7,North\n' if fmt == "csv" else '[{"id":7,"region":"North"}]')
        payload = path.read_bytes()
        downloads = []
        if transport == "boto3":
            import boto3
            def download(bucket, key, target):
                downloads.append(target.name)
                target.write(payload)
            monkeypatch.setattr(boto3, "client", lambda *a, **kw: SimpleNamespace(download_fileobj=download))
        else:
            monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: io.BytesIO(payload))
        ctx = SimpleNamespace(conn=conn)
        node = S3SourceNode({})
        def read():
            if transport == "boto3":
                return node._read_with_boto3(ctx, "demo", path.name, "http://localhost", "key", "secret", "us-east-1", "auto")
            return node._read_with_http(ctx, "demo", path.name, "http://localhost", "auto")
        first = read()
        second = read()
        # Both methods have already executed their unlink finally block.
        assert first.fetchall() == [(7, "North")]
        assert second.fetchall() == [(7, "North")]
        assert all(not Path(download).exists() for download in downloads)
