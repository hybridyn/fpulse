"""Pinned tests for L3 consumer self-attestation (2026-06-08).

Third milestone from docs/design/lineage-1.2.md. Foundation only:
storage + API. L3.1 (frontend "consumers of this output" panel)
deferred to a focused session.

Contracts pinned:
  * record_consumer is idempotent on (output_id, consumer_id, consumer_type)
  * Re-attestation updates last_read_at without creating duplicates
  * list_consumers returns rows for the right output, most-recent first
  * list_all_outputs_with_consumers aggregates correctly
  * delete_consumer removes only the targeted row
  * API endpoints validate required fields + return JSON
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from fpulse.lineage import LineageStore


# Re-use the SQLite fake from earlier lineage tests
class _FakeDB:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def fetchone(self, sql, params=()):
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql, params=()):
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]


@pytest.fixture
def store():
    return LineageStore(_FakeDB())


# ── Storage layer ───────────────────────────────────────────────────


class TestRecordConsumer:
    def test_first_registration_returns_id(self, store):
        rid = store.record_consumer(
            output_id="fpulse://ws/default/pipeline/p1/sink/s2",
            consumer_id="snowflake://prod/analytics/orders_view",
            consumer_type="snowflake_view",
        )
        assert rid.startswith("cn_")

    def test_reregistration_is_idempotent(self, store):
        # Same natural key (output_id, consumer_id, consumer_type) →
        # same row id, no duplicate
        rid_a = store.record_consumer(
            output_id="out-1", consumer_id="cons-1",
            consumer_type="snowflake_view",
        )
        rid_b = store.record_consumer(
            output_id="out-1", consumer_id="cons-1",
            consumer_type="snowflake_view",
            last_read_at=999.0,
            notes="updated note",
        )
        assert rid_a == rid_b
        # Only one row in list
        consumers = store.list_consumers("out-1")
        assert len(consumers) == 1
        assert consumers[0]["last_read_at"] == 999.0
        assert consumers[0]["notes"] == "updated note"

    def test_different_consumer_type_creates_separate_row(self, store):
        # Same output + same consumer_id but different consumer_type =
        # legitimate separate registration (one view + one notebook
        # reading the same export)
        store.record_consumer(
            output_id="out-1", consumer_id="team@example.com",
            consumer_type="snowflake_view",
        )
        store.record_consumer(
            output_id="out-1", consumer_id="team@example.com",
            consumer_type="python_notebook",
        )
        assert len(store.list_consumers("out-1")) == 2

    def test_attested_by_and_notes_round_trip(self, store):
        store.record_consumer(
            output_id="out-1", consumer_id="cons-1",
            consumer_type="snowflake_view",
            attested_by="alice@example.com",
            notes="Refreshed in nightly batch at 02:00 UTC",
        )
        c = store.list_consumers("out-1")[0]
        assert c["attested_by"] == "alice@example.com"
        assert "nightly batch" in c["notes"]


# ── List operations ─────────────────────────────────────────────────


class TestListConsumers:
    def test_empty_output_returns_empty_list(self, store):
        assert store.list_consumers("nonexistent") == []

    def test_lists_only_target_output(self, store):
        store.record_consumer(output_id="out-A", consumer_id="c1",
                                consumer_type="fpulse_pipeline")
        store.record_consumer(output_id="out-B", consumer_id="c2",
                                consumer_type="fpulse_pipeline")
        a = store.list_consumers("out-A")
        b = store.list_consumers("out-B")
        assert len(a) == 1 and a[0]["consumer_id"] == "c1"
        assert len(b) == 1 and b[0]["consumer_id"] == "c2"

    def test_most_recent_first(self, store):
        # Register in oldest-first order; expect newest-first listing
        for cid in ("c-old", "c-mid", "c-new"):
            store.record_consumer(output_id="out-1", consumer_id=cid,
                                    consumer_type="snowflake_view")
            time.sleep(0.001)  # ensure attested_at is monotonic
        consumers = store.list_consumers("out-1")
        ids = [c["consumer_id"] for c in consumers]
        assert ids == ["c-new", "c-mid", "c-old"]


# ── Overview ────────────────────────────────────────────────────────


class TestOverview:
    def test_returns_one_row_per_output(self, store):
        store.record_consumer(output_id="out-A", consumer_id="c1",
                                consumer_type="snowflake_view")
        store.record_consumer(output_id="out-A", consumer_id="c2",
                                consumer_type="python_notebook")
        store.record_consumer(output_id="out-B", consumer_id="c3",
                                consumer_type="snowflake_view")
        overview = store.list_all_outputs_with_consumers()
        by_output = {r["output_id"]: r for r in overview}
        assert by_output["out-A"]["consumer_count"] == 2
        assert by_output["out-B"]["consumer_count"] == 1

    def test_empty_returns_empty(self, store):
        assert store.list_all_outputs_with_consumers() == []


# ── Delete ──────────────────────────────────────────────────────────


class TestDeleteConsumer:
    def test_removes_only_targeted_row(self, store):
        store.record_consumer(output_id="out-A", consumer_id="c1",
                                consumer_type="snowflake_view")
        store.record_consumer(output_id="out-A", consumer_id="c1",
                                consumer_type="python_notebook")
        # Delete one of the two
        removed = store.delete_consumer(
            output_id="out-A", consumer_id="c1",
            consumer_type="snowflake_view",
        )
        assert removed is True
        remaining = store.list_consumers("out-A")
        assert len(remaining) == 1
        assert remaining[0]["consumer_type"] == "python_notebook"

    def test_delete_nonexistent_returns_false(self, store):
        removed = store.delete_consumer(
            output_id="nonexistent", consumer_id="c1",
            consumer_type="snowflake_view",
        )
        assert removed is False


# ── API surface ─────────────────────────────────────────────────────


class TestAPIEndpoints:
    def _make_client(self, tmp_path, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import fpulse.api.lineage as lineage_mod
        import fpulse.main as main_mod
        ls = LineageStore(_FakeDB())
        monkeypatch.setattr(main_mod, "app_state",
                            {"data_dir": str(tmp_path), "lineage_store": ls},
                            raising=False)
        # Bypass feature flag
        monkeypatch.setattr(lineage_mod, "_get_lineage_store",
                            lambda req: ls)
        app = FastAPI()
        from fpulse.auth.deps import current_workspace_id
        app.dependency_overrides[current_workspace_id] = lambda: "default"
        app.include_router(lineage_mod.router)
        return TestClient(app), ls

    def test_post_register_minimal(self, tmp_path, monkeypatch):
        client, ls = self._make_client(tmp_path, monkeypatch)
        r = client.post("/api/lineage/consumers", json={
            "output_id":     "out-1",
            "consumer_id":   "c-1",
            "consumer_type": "snowflake_view",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["recorded"] is True
        assert body["id"].startswith("cn_")

    def test_post_validates_required_fields(self, tmp_path, monkeypatch):
        client, _ = self._make_client(tmp_path, monkeypatch)
        # Missing each field in turn
        for missing in ("output_id", "consumer_id", "consumer_type"):
            payload = {
                "output_id": "out-1", "consumer_id": "c-1",
                "consumer_type": "snowflake_view",
            }
            payload.pop(missing)
            r = client.post("/api/lineage/consumers", json=payload)
            assert r.status_code == 400, f"missing {missing} should 400"

    def test_get_returns_registered_consumers(self, tmp_path, monkeypatch):
        client, _ = self._make_client(tmp_path, monkeypatch)
        client.post("/api/lineage/consumers", json={
            "output_id":     "out-A",
            "consumer_id":   "c-1",
            "consumer_type": "snowflake_view",
            "attested_by":   "alice@example.com",
        })
        body = client.get("/api/lineage/consumers", params={"output_id": "out-A"}).json()
        assert body["count"] == 1
        assert body["consumers"][0]["attested_by"] == "alice@example.com"

    def test_get_requires_output_id(self, tmp_path, monkeypatch):
        client, _ = self._make_client(tmp_path, monkeypatch)
        # Missing query param - FastAPI returns 422 for missing required
        # query, but our explicit check would 400. Either is acceptable;
        # pin that it's NOT 200.
        r = client.get("/api/lineage/consumers")
        assert r.status_code in (400, 422), f"expected 4xx, got {r.status_code}"

    def test_overview_endpoint(self, tmp_path, monkeypatch):
        client, _ = self._make_client(tmp_path, monkeypatch)
        client.post("/api/lineage/consumers", json={
            "output_id": "out-A", "consumer_id": "c1",
            "consumer_type": "snowflake_view",
        })
        client.post("/api/lineage/consumers", json={
            "output_id": "out-B", "consumer_id": "c2",
            "consumer_type": "python_notebook",
        })
        body = client.get("/api/lineage/consumers/_overview").json()
        assert body["count"] == 2
        ids = {row["output_id"] for row in body["outputs"]}
        assert ids == {"out-A", "out-B"}

    def test_delete_endpoint(self, tmp_path, monkeypatch):
        client, _ = self._make_client(tmp_path, monkeypatch)
        client.post("/api/lineage/consumers", json={
            "output_id": "out-A", "consumer_id": "c1",
            "consumer_type": "snowflake_view",
        })
        r = client.request("DELETE", "/api/lineage/consumers", json={
            "output_id": "out-A", "consumer_id": "c1",
            "consumer_type": "snowflake_view",
        })
        assert r.status_code == 200
        assert r.json()["removed"] is True
        # Verify
        body = client.get("/api/lineage/consumers", params={"output_id": "out-A"}).json()
        assert body["count"] == 0

    def test_idempotent_via_api(self, tmp_path, monkeypatch):
        client, _ = self._make_client(tmp_path, monkeypatch)
        # Same triple posted twice → returns same id
        payload = {
            "output_id": "out-A", "consumer_id": "c1",
            "consumer_type": "snowflake_view",
        }
        id_a = client.post("/api/lineage/consumers", json=payload).json()["id"]
        id_b = client.post("/api/lineage/consumers", json=payload).json()["id"]
        assert id_a == id_b
        body = client.get("/api/lineage/consumers", params={"output_id": "out-A"}).json()
        assert body["count"] == 1
