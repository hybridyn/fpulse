"""
CDC (Change Data Capture) source — Debezium-style change streams.

Two execution modes:

  1. **kafka_topic** (production) — consume Debezium-emitted CDC events from a
     Kafka topic. Each Debezium event has `op` (c/u/d/r) and `after`/`before`
     payloads. We flatten `after` into rows for downstream processing.

  2. **direct_logical** (in-process tail) — for Postgres only, tail the
     logical replication slot directly using psycopg2's logical decoding,
     bypassing Kafka entirely. Lighter weight for small workloads.

Source databases supported (via Debezium connectors):
  postgres, mysql, mssql, oracle, mongodb

This node returns a *snapshot batch* of changes (bounded by max_events). For
true streaming, the workflow scheduler re-runs the node periodically and the
node tracks an offset/LSN cursor in the params (or in workflow state).
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, TYPE_CHECKING

# Stage 2.5b: duckdb only used for type annotations on _events_to_relation
# and execute(). Runtime data flow is through ctx.conn.
if TYPE_CHECKING:
    import duckdb

from fpulse.ir.schema import StepType
from fpulse.nodes.base import BaseNode, ExecutionContext
from fpulse.nodes.registry import register


_SUPPORTED_SOURCES = ["postgres", "mysql", "mssql", "oracle", "mongodb"]


def _events_to_relation(conn: duckdb.DuckDBPyConnection, events: list[dict]) -> duckdb.DuckDBPyRelation:
    if not events:
        return conn.sql("SELECT NULL AS empty WHERE false")
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, default=str) + "\n")
        return conn.sql(f"SELECT * FROM read_json_auto('{path}', format='newline_delimited')")
    except Exception:
        return conn.sql("SELECT NULL AS empty WHERE false")


def _consume_kafka_debezium(
    bootstrap: str, topic: str, group: str, max_events: int, timeout_s: float
) -> list[dict]:
    """Consume Debezium-formatted CDC events from a Kafka topic."""
    try:
        from kafka import KafkaConsumer
    except ImportError as e:
        raise RuntimeError("kafka-python not installed. Run: pip install kafka-python") from e

    consumer = KafkaConsumer(
        topic,
        bootstrap_servers=bootstrap.split(","),
        group_id=group,
        auto_offset_reset="earliest",
        enable_auto_commit=True,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")) if v else None,
        consumer_timeout_ms=int(timeout_s * 1000),
    )

    rows: list[dict] = []
    try:
        for msg in consumer:
            if not msg.value:
                continue
            event = msg.value
            payload = event.get("payload") if isinstance(event, dict) else None
            if not payload and isinstance(event, dict):
                payload = event  # already-flattened format
            if not isinstance(payload, dict):
                continue
            op = payload.get("op", "r")
            row = payload.get("after") or payload.get("before") or {}
            if not isinstance(row, dict):
                continue
            row = dict(row)
            row["__op"] = op
            row["__source_ts_ms"] = (payload.get("source") or {}).get("ts_ms")
            row["__topic"] = msg.topic
            row["__partition"] = msg.partition
            row["__offset"] = msg.offset
            rows.append(row)
            if len(rows) >= max_events:
                break
    finally:
        consumer.close()
    return rows


def _tail_postgres_logical(
    host: str, port: int, database: str, user: str, password: str,
    slot: str, publication: str, max_events: int
) -> list[dict]:
    """Tail a Postgres logical replication slot directly."""
    try:
        import psycopg2
        from psycopg2.extras import LogicalReplicationConnection, ReplicationCursor
    except ImportError as e:
        raise RuntimeError("psycopg2 not installed. Run: pip install psycopg2-binary") from e

    conn = psycopg2.connect(
        host=host, port=port, dbname=database, user=user, password=password,
        connection_factory=LogicalReplicationConnection,
    )
    cur: ReplicationCursor = conn.cursor()
    rows: list[dict] = []

    try:
        try:
            cur.create_replication_slot(slot, output_plugin="pgoutput")
        except psycopg2.errors.DuplicateObject:
            pass

        cur.start_replication(
            slot_name=slot, decode=True,
            options={"publication_names": publication, "proto_version": "1"},
        )

        def consume(msg):
            try:
                payload = msg.payload
                if isinstance(payload, (bytes, bytearray)):
                    payload = payload.decode("utf-8", errors="replace")
                rows.append({"lsn": str(msg.data_start), "payload": payload})
                msg.cursor.send_feedback(flush_lsn=msg.data_start)
            except Exception:
                pass
            if len(rows) >= max_events:
                raise StopIteration

        try:
            cur.consume_stream(consume)
        except StopIteration:
            pass
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass
    return rows


def drop_replication_slot(
    host: str, port: int, database: str, user: str, password: str, slot: str,
) -> bool:
    """Drop a logical replication slot on the source Postgres.

    An orphaned slot pins WAL on the source forever — Postgres cannot
    recycle WAL segments a slot still references, so a slot left behind
    by a deleted workflow eventually fills the source server's disk.
    Returns True if the slot was dropped, False if it didn't exist or
    the drop failed (logged; never raises).
    """
    import logging
    log = logging.getLogger(__name__)
    try:
        import psycopg2
    except ImportError:
        log.warning("cdc: psycopg2 not installed — cannot drop slot %r", slot)
        return False
    try:
        conn = psycopg2.connect(
            host=host, port=port, dbname=database, user=user, password=password,
        )
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_drop_replication_slot(slot_name) "
                    "FROM pg_replication_slots WHERE slot_name = %s",
                    (slot,),
                )
                dropped = cur.rowcount > 0
            if dropped:
                log.info("cdc: dropped replication slot %r on %s/%s", slot, host, database)
            return dropped
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — cleanup must never break the caller
        log.warning("cdc: could not drop replication slot %r on %s: %s", slot, host, exc)
        return False


def cleanup_workflow_cdc_slots(workflow: Any) -> int:
    """Drop the replication slots of every direct_logical CDC source in
    ``workflow``. Called from the workflow-delete path so slots don't
    outlive the pipeline that created them. Best-effort; returns the
    number of slots dropped.
    """
    dropped = 0
    for step in getattr(workflow, "steps", None) or []:
        try:
            if getattr(step, "type", None) != StepType.CDC_SOURCE:
                continue
            params = dict(getattr(step, "params", {}) or {})
            if params.get("mode") != "direct_logical":
                continue
            host = params.get("host")
            database = params.get("database")
            if not host or not database:
                continue
            if drop_replication_slot(
                host=host,
                port=int(params.get("port", 5432)),
                database=database,
                user=params.get("user", ""),
                password=params.get("password", ""),
                slot=params.get("slot", "fpulse_slot"),
            ):
                dropped += 1
        except Exception:  # noqa: BLE001
            continue
    return dropped


@register(StepType.CDC_SOURCE)
class CdcSourceNode(BaseNode):
    """Change Data Capture source — Debezium via Kafka or Postgres logical decoding."""

    display_name = "CDC Source (Debezium)"
    category = "source"
    description = "Stream INSERT/UPDATE/DELETE events from Postgres/MySQL/MSSQL/Oracle/MongoDB"

    def execute(self, ctx: ExecutionContext) -> duckdb.DuckDBPyRelation:
        mode = self.params.get("mode", "kafka_topic")
        max_events = int(self.params.get("max_events", 1000))

        if mode == "kafka_topic":
            bootstrap = self.params.get("kafka_bootstrap") or "localhost:9092"
            topic = self.params.get("kafka_topic")
            if not topic:
                raise ValueError("CDC Source: kafka_topic is required for kafka_topic mode")
            group = self.params.get("consumer_group") or "fpulse-cdc"
            timeout_s = float(self.params.get("poll_timeout_s", 5.0))
            events = _consume_kafka_debezium(bootstrap, topic, group, max_events, timeout_s)
            return _events_to_relation(ctx.conn, events)

        if mode == "direct_logical":
            source_type = self.params.get("source_type", "postgres")
            if source_type != "postgres":
                raise ValueError(f"direct_logical only supports postgres (got {source_type})")
            events = _tail_postgres_logical(
                host=self.params["host"],
                port=int(self.params.get("port", 5432)),
                database=self.params["database"],
                user=self.params["user"],
                password=self.params["password"],
                slot=self.params.get("slot", "fpulse_slot"),
                publication=self.params.get("publication", "fpulse_pub"),
                max_events=max_events,
            )
            return _events_to_relation(ctx.conn, events)

        raise ValueError(f"CDC Source: unknown mode '{mode}'")

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "mode": "kafka_topic",
            "source_type": "postgres",
            "max_events": 1000,
            "poll_timeout_s": 5.0,
        }

    @staticmethod
    def param_schema() -> list[dict]:
        return [
            {"name": "mode", "type": "string", "label": "Mode", "required": True,
             "options": [
                 {"value": "kafka_topic", "label": "Kafka topic (Debezium)"},
                 {"value": "direct_logical", "label": "Direct logical (Postgres only)"},
             ]},
            {"name": "source_type", "type": "string", "label": "Source DB",
             "options": [{"value": s, "label": s.title()} for s in _SUPPORTED_SOURCES]},
            {"name": "kafka_bootstrap", "type": "string", "label": "Kafka bootstrap"},
            {"name": "kafka_topic", "type": "string", "label": "Kafka topic"},
            {"name": "consumer_group", "type": "string", "label": "Consumer group"},
            {"name": "host", "type": "string", "label": "DB host (direct mode)"},
            {"name": "database", "type": "string", "label": "Database (direct mode)"},
            {"name": "slot", "type": "string", "label": "Replication slot"},
            {"name": "publication", "type": "string", "label": "Publication"},
            {"name": "max_events", "type": "number", "label": "Max events / batch", "default": 1000},
        ]
