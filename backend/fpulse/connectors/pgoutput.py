"""PostgreSQL `pgoutput` logical-replication protocol decoder — Sprint B.

Decodes the wire-format messages a `START_REPLICATION` slot emits when
configured with the `pgoutput` plugin. The existing
`_tail_postgres_logical` in `cdc.py` only kept the raw bytes; this module
turns them into row-shaped events with op type + before/after values +
schema metadata.

Reference: PostgreSQL Logical Replication Message Formats
  https://www.postgresql.org/docs/current/protocol-logicalrep-message-formats.html

Supported messages (Sprint B scope):
  - 'B' Begin
  - 'C' Commit
  - 'R' Relation       (schema for a table; cached, drives column-name decode)
  - 'I' Insert
  - 'U' Update
  - 'D' Delete
  - 'T' Truncate

Out of scope (deferred): Origin (O), Type (Y), Message (M), Stream Start /
Stop / Commit / Abort (in-progress txn streaming, requires
`streaming=on` which we don't enable yet).

Usage:
    decoder = PgoutputDecoder()
    for raw_msg in start_replication_iter(...):
        event = decoder.decode(raw_msg.payload, raw_msg.data_start_lsn)
        if event:
            yield event   # {"op": "I"|"U"|"D"|"T"|"B"|"C", ...}

Schema-drift handling: every Relation message replaces the cached schema
for that relation_id. Subsequent Insert/Update/Delete events use the new
column list. Downstream consumers see a `__schema_version` field that
increments per relation when the column set changes.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Column flag bits (per pgoutput protocol) ─────────────────────────


COLUMN_FLAG_KEY = 1  # column is part of the table's PK / replica identity


# ── Tuple data type tags (per pgoutput) ──────────────────────────────


TUPLE_TAG_NULL = b"n"     # null value
TUPLE_TAG_TOAST = b"u"    # unchanged TOAST value (we treat as None)
TUPLE_TAG_TEXT = b"t"     # text-format value
TUPLE_TAG_BINARY = b"b"   # binary-format value (we keep as bytes)


# ── Cached relation metadata ─────────────────────────────────────────


@dataclass
class RelationMeta:
    relation_id: int
    namespace: str
    name: str
    replica_identity: str
    columns: list[dict] = field(default_factory=list)  # [{name, type_oid, type_modifier, is_key}]
    schema_version: int = 1

    @property
    def column_names(self) -> list[str]:
        return [c["name"] for c in self.columns]


# ── Reader helpers (advance an offset over a bytes buffer) ──────────


def _read_uint8(buf: bytes, off: int) -> tuple[int, int]:
    return buf[off], off + 1


def _read_int32(buf: bytes, off: int) -> tuple[int, int]:
    v = struct.unpack_from(">i", buf, off)[0]
    return v, off + 4


def _read_int16(buf: bytes, off: int) -> tuple[int, int]:
    v = struct.unpack_from(">h", buf, off)[0]
    return v, off + 2


def _read_int64(buf: bytes, off: int) -> tuple[int, int]:
    v = struct.unpack_from(">q", buf, off)[0]
    return v, off + 8


def _read_string(buf: bytes, off: int) -> tuple[str, int]:
    """Null-terminated string."""
    end = buf.index(b"\x00", off)
    s = buf[off:end].decode("utf-8", errors="replace")
    return s, end + 1


def _read_tuple_data(buf: bytes, off: int) -> tuple[list[Any], int]:
    """Decode a TupleData payload — N column values, each tagged null /
    toast-unchanged / text / binary."""
    ncols, off = _read_int16(buf, off)
    values: list[Any] = []
    for _ in range(ncols):
        tag = buf[off:off + 1]
        off += 1
        if tag == TUPLE_TAG_NULL:
            values.append(None)
        elif tag == TUPLE_TAG_TOAST:
            # Unchanged TOAST — we don't have the prior value here, signal
            # explicitly so consumers know this isn't a real None.
            values.append({"__unchanged_toast": True})
        elif tag in (TUPLE_TAG_TEXT, TUPLE_TAG_BINARY):
            length, off = _read_int32(buf, off)
            data = buf[off:off + length]
            off += length
            if tag == TUPLE_TAG_TEXT:
                values.append(data.decode("utf-8", errors="replace"))
            else:
                values.append(bytes(data))
        else:
            raise ValueError(f"pgoutput: unknown tuple value tag {tag!r}")
    return values, off


# ── Public decoder ────────────────────────────────────────────────────


class PgoutputDecoder:
    """Stateful decoder — caches the per-relation schema as Relation
    messages arrive, then resolves Insert/Update/Delete column names
    against that cache.

    Thread-safety: a single decoder instance per replication slot.
    Don't share across threads."""

    def __init__(self) -> None:
        self._relations: dict[int, RelationMeta] = {}
        # Track the last column-set hash per relation_id so we can bump
        # schema_version on real changes (column add/drop/rename), not on
        # every Relation message.
        self._last_col_sig: dict[int, str] = {}

    # ── Top-level entry ─────────────────────────────────────────────

    def decode(self, payload: bytes, lsn: int | None = None) -> dict | None:
        """Decode one logical-replication message. Returns the event dict
        or None if the message type is unsupported (Origin/Type/Message)."""
        if not payload:
            return None
        msg_type = payload[0:1]
        body = payload  # full payload — handlers re-read the type byte

        if msg_type == b"B":
            return self._decode_begin(body, lsn)
        if msg_type == b"C":
            return self._decode_commit(body, lsn)
        if msg_type == b"R":
            return self._decode_relation(body, lsn)
        if msg_type == b"I":
            return self._decode_insert(body, lsn)
        if msg_type == b"U":
            return self._decode_update(body, lsn)
        if msg_type == b"D":
            return self._decode_delete(body, lsn)
        if msg_type == b"T":
            return self._decode_truncate(body, lsn)

        # Unsupported: O (origin), Y (type), M (logical message),
        # streaming-mode messages. Return None so callers can skip.
        logger.debug("pgoutput: skipping message type %r", msg_type)
        return None

    # ── Per-message decoders ────────────────────────────────────────

    def _decode_begin(self, buf: bytes, lsn: int | None) -> dict:
        # Layout: 'B' | final_lsn (8) | timestamp (8) | xid (4)
        off = 1
        final_lsn, off = _read_int64(buf, off)
        timestamp_us, off = _read_int64(buf, off)
        xid, off = _read_int32(buf, off)
        return {
            "op": "B",
            "lsn": lsn,
            "final_lsn": final_lsn,
            "timestamp_us": timestamp_us,
            "xid": xid,
        }

    def _decode_commit(self, buf: bytes, lsn: int | None) -> dict:
        # Layout: 'C' | flags (1) | commit_lsn (8) | end_lsn (8) | timestamp (8)
        off = 1
        flags, off = _read_uint8(buf, off)
        commit_lsn, off = _read_int64(buf, off)
        end_lsn, off = _read_int64(buf, off)
        timestamp_us, off = _read_int64(buf, off)
        return {
            "op": "C",
            "lsn": lsn,
            "flags": flags,
            "commit_lsn": commit_lsn,
            "end_lsn": end_lsn,
            "timestamp_us": timestamp_us,
        }

    def _decode_relation(self, buf: bytes, lsn: int | None) -> dict:
        # Layout: 'R' | relation_id (4) | namespace \0 | name \0 |
        #         replica_identity (1) | ncols (2) | for each col:
        #            flags (1) | name \0 | type_oid (4) | type_modifier (4)
        off = 1
        relation_id, off = _read_int32(buf, off)
        namespace, off = _read_string(buf, off)
        name, off = _read_string(buf, off)
        replica_identity_byte, off = _read_uint8(buf, off)
        ncols, off = _read_int16(buf, off)
        columns: list[dict] = []
        for _ in range(ncols):
            flags, off = _read_uint8(buf, off)
            cname, off = _read_string(buf, off)
            type_oid, off = _read_int32(buf, off)
            type_mod, off = _read_int32(buf, off)
            columns.append({
                "name": cname,
                "type_oid": type_oid,
                "type_modifier": type_mod,
                "is_key": bool(flags & COLUMN_FLAG_KEY),
            })

        # Schema-drift detection: bump schema_version only when the
        # column SET changes. Rare in practice, but a fresh-Relation
        # message is sent on every reconnect even when nothing's changed.
        col_sig = "|".join(f"{c['name']}:{c['type_oid']}" for c in columns)
        prev_meta = self._relations.get(relation_id)
        if prev_meta and self._last_col_sig.get(relation_id) == col_sig:
            schema_version = prev_meta.schema_version
        else:
            schema_version = (prev_meta.schema_version + 1) if prev_meta else 1

        meta = RelationMeta(
            relation_id=relation_id,
            namespace=namespace,
            name=name,
            replica_identity=chr(replica_identity_byte),
            columns=columns,
            schema_version=schema_version,
        )
        self._relations[relation_id] = meta
        self._last_col_sig[relation_id] = col_sig

        return {
            "op": "R",
            "lsn": lsn,
            "relation_id": relation_id,
            "namespace": namespace,
            "name": name,
            "replica_identity": meta.replica_identity,
            "columns": columns,
            "schema_version": schema_version,
        }

    def _decode_insert(self, buf: bytes, lsn: int | None) -> dict:
        # Layout: 'I' | relation_id (4) | 'N' | TupleData
        off = 1
        relation_id, off = _read_int32(buf, off)
        new_marker = buf[off:off + 1]
        off += 1
        if new_marker != b"N":
            raise ValueError(f"pgoutput: expected 'N' after Insert relation, got {new_marker!r}")
        values, off = _read_tuple_data(buf, off)
        return self._row_event("I", relation_id, lsn, after=values)

    def _decode_update(self, buf: bytes, lsn: int | None) -> dict:
        # Layout: 'U' | relation_id (4) | optional 'K' or 'O' TupleData
        #              (replica-identity or full old) | 'N' | new TupleData
        off = 1
        relation_id, off = _read_int32(buf, off)
        before: list[Any] | None = None
        marker = buf[off:off + 1]
        off += 1
        if marker in (b"K", b"O"):
            before, off = _read_tuple_data(buf, off)
            marker = buf[off:off + 1]
            off += 1
        if marker != b"N":
            raise ValueError(f"pgoutput: expected 'N' for Update new tuple, got {marker!r}")
        after, off = _read_tuple_data(buf, off)
        return self._row_event("U", relation_id, lsn, before=before, after=after)

    def _decode_delete(self, buf: bytes, lsn: int | None) -> dict:
        # Layout: 'D' | relation_id (4) | 'K' or 'O' | TupleData
        off = 1
        relation_id, off = _read_int32(buf, off)
        marker = buf[off:off + 1]
        off += 1
        if marker not in (b"K", b"O"):
            raise ValueError(f"pgoutput: expected 'K' or 'O' for Delete, got {marker!r}")
        before, off = _read_tuple_data(buf, off)
        return self._row_event("D", relation_id, lsn, before=before)

    def _decode_truncate(self, buf: bytes, lsn: int | None) -> dict:
        # Layout: 'T' | nrelations (4) | flags (1) | relation_ids (4 * nrelations)
        off = 1
        nrelations, off = _read_int32(buf, off)
        flags, off = _read_uint8(buf, off)
        ids: list[int] = []
        for _ in range(nrelations):
            rid, off = _read_int32(buf, off)
            ids.append(rid)
        return {
            "op": "T",
            "lsn": lsn,
            "flags": flags,
            "relation_ids": ids,
            "namespaces": [
                {"namespace": self._relations.get(i, RelationMeta(i, "", "", "d")).namespace,
                 "name": self._relations.get(i, RelationMeta(i, "", "", "d")).name}
                for i in ids
            ],
        }

    # ── Row-event helper ────────────────────────────────────────────

    def _row_event(
        self,
        op: str,
        relation_id: int,
        lsn: int | None,
        *,
        before: list[Any] | None = None,
        after: list[Any] | None = None,
    ) -> dict:
        meta = self._relations.get(relation_id)
        if not meta:
            # Should not happen — Relation always precedes Insert/Update/
            # Delete in a well-formed stream — but handle gracefully.
            return {
                "op": op,
                "lsn": lsn,
                "relation_id": relation_id,
                "namespace": None,
                "name": None,
                "schema_version": 0,
                "before": before,
                "after": after,
                "_warning": "no Relation message seen yet for this relation_id",
            }
        before_dict = self._zip_with_columns(meta, before) if before is not None else None
        after_dict = self._zip_with_columns(meta, after) if after is not None else None
        return {
            "op": op,
            "lsn": lsn,
            "relation_id": relation_id,
            "namespace": meta.namespace,
            "name": meta.name,
            "schema_version": meta.schema_version,
            "before": before_dict,
            "after": after_dict,
        }

    @staticmethod
    def _zip_with_columns(meta: RelationMeta, values: list[Any]) -> dict[str, Any]:
        """Pair tuple-data values with their column names. Extra values
        are dropped; missing values are filled with None — defensive in
        case a Relation update is in flight when a row event arrives."""
        out: dict[str, Any] = {}
        for col, val in zip(meta.column_names, values):
            out[col] = val
        return out

    # ── Inspection helpers ──────────────────────────────────────────

    def known_relations(self) -> dict[int, RelationMeta]:
        return dict(self._relations)

    def reset(self) -> None:
        self._relations.clear()
        self._last_col_sig.clear()


# ── LSN persistence (Sprint B / DESIGN_SPRINT1_BULK_LOADERS handoff) ──


def ensure_cdc_offsets_table(conn) -> None:
    """Create `cdc_offsets` if it doesn't already exist.

    Stores the last confirmed_flush_lsn per (workflow_id, slot_name) so
    subsequent runs can resume from the same point in the stream rather
    than re-creating the slot or re-replaying.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cdc_offsets (
            workflow_id     TEXT NOT NULL,
            slot_name       TEXT NOT NULL,
            publication     TEXT,
            last_lsn        INTEGER NOT NULL,
            last_seen_at    TEXT NOT NULL,
            event_count     INTEGER DEFAULT 0,
            PRIMARY KEY (workflow_id, slot_name)
        )
    """)
    conn.commit()


def save_cdc_offset(
    conn,
    *,
    workflow_id: str,
    slot_name: str,
    last_lsn: int,
    publication: str | None = None,
    event_count: int | None = None,
) -> None:
    """Upsert the offset row. Idempotent."""
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO cdc_offsets (workflow_id, slot_name, publication, last_lsn, last_seen_at, event_count)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(workflow_id, slot_name) DO UPDATE SET
            publication = COALESCE(excluded.publication, publication),
            last_lsn = excluded.last_lsn,
            last_seen_at = excluded.last_seen_at,
            event_count = COALESCE(?, event_count)
    """, (
        workflow_id, slot_name, publication, last_lsn, now_iso, event_count or 0,
        event_count,
    ))
    conn.commit()


def load_cdc_offset(conn, workflow_id: str, slot_name: str) -> dict | None:
    """Return the last persisted offset for (workflow_id, slot_name) or None."""
    row = conn.execute("""
        SELECT workflow_id, slot_name, publication, last_lsn, last_seen_at, event_count
        FROM cdc_offsets WHERE workflow_id = ? AND slot_name = ?
    """, (workflow_id, slot_name)).fetchone()
    if not row:
        return None
    keys = ("workflow_id", "slot_name", "publication", "last_lsn", "last_seen_at", "event_count")
    return dict(zip(keys, row))
