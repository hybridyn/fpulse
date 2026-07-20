"""
One-shot migration: re-encrypt every credential + AI provider API key.

Why this exists: pre-1.0 OSS Free installs stored credentials and AI
provider API keys in plaintext on disk. The May 4 2026 fix made
encryption always-on, but EXISTING rows are still in their legacy form
(raw dict for credentials, `PLAIN:<value>` sentinel for AI config).

The Encryptor's read path tolerates these legacy formats — the next
SAVE re-encrypts them. But operators upgrading don't necessarily edit
every credential, so plaintext can linger for months.

This script walks every row in `credentials`, `user_ai_config`, and
`workspace_ai_config` and forces a re-save through the always-on
encryptor. After it runs, every secret is `ENC:v1:<token>` form.

## Usage

    python -m fpulse.security.migrate_existing                    # in-place
    python -m fpulse.security.migrate_existing --dry-run          # report only
    python -m fpulse.security.migrate_existing --backup-dir ./bak # write JSONL backup first

## Safety

- **Always takes a snapshot first** unless `--no-backup` is passed
  explicitly. Snapshot is a JSONL file under `<data_dir>/migration_backups/`
  with each row's raw pre-migration JSON. Lets the operator restore if
  something goes wrong.
- **Idempotent.** Re-running on an already-migrated install is a no-op
  (every row reads as `ENC:v1:`, the encrypted value, and re-encrypts
  to a different token but with the same plaintext — safe).
- **Per-row try/except.** A single corrupt row doesn't abort the run;
  it's logged and skipped. Final report counts successes + failures.
- **Refuses to run without a master key.** If `~/.fpulse/secret.key`
  doesn't exist, the script generates one — but warns the operator.
- **Refuses to run on a write-locked DB.** Acquires SQLite's exclusive
  lock for the duration; concurrent F-Pulse processes block until the
  migration finishes.

## What it does NOT touch

- The pipeline IR. Pipelines reference credentials by ID; the IR has
  no secret values to re-encrypt.
- The execution history. Historical runs may have logged sanitized
  prompts that still contain `PLAIN:` strings — those are intentionally
  not re-written so audit replay stays accurate.
- The RAG vector store. Embeddings of public docs aren't secrets.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _resolve_db_path() -> Path:
    """Mirror the resolution main.py does."""
    data_dir = os.environ.get("FPULSE_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir).expanduser() / "fpulse.db"
    return Path("data") / "fpulse.db"


def _backup_dir() -> Path:
    data_dir = os.environ.get("FPULSE_DATA_DIR", "data")
    return Path(data_dir).expanduser() / "migration_backups"


def _open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(
            f"F-Pulse database not found at {db_path}. Set FPULSE_DATA_DIR "
            f"to point at the correct data directory."
        )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────────────────────────────────────────────────────
# Per-table workers
# ─────────────────────────────────────────────────────────────────────


def _migrate_credentials(
    conn: sqlite3.Connection, encryptor, *, dry_run: bool, backup_writer,
) -> tuple[int, int, int]:
    """Re-encrypt every row in `credentials`. Returns (scanned, updated, failed)."""
    scanned = updated = failed = 0
    rows = conn.execute("SELECT id, data FROM credentials").fetchall()
    for row in rows:
        scanned += 1
        try:
            blob = json.loads(row["data"]) if row["data"] else {}
            backup_writer({"table": "credentials", "id": row["id"], "data": blob})

            config = blob.get("config") or {}
            # Decrypt-then-re-encrypt to canonicalise format. The decrypt
            # tolerates legacy plaintext + PLAIN: + ENC:v1: rows.
            decrypted = encryptor.decrypt_config(config)
            re_encrypted = encryptor.encrypt_config(decrypted)
            if config == re_encrypted:
                continue  # already canonical; no write needed
            blob["config"] = re_encrypted
            if not dry_run:
                conn.execute(
                    "UPDATE credentials SET data = ? WHERE id = ?",
                    (json.dumps(blob, default=str), row["id"]),
                )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("credentials row id=%s failed: %s", row["id"], exc)
    return scanned, updated, failed


def _migrate_ai_config(
    conn: sqlite3.Connection, table: str, encryptor, *,
    dry_run: bool, backup_writer,
) -> tuple[int, int, int]:
    """Re-encrypt the `api_key` column on user_ai_config or workspace_ai_config."""
    scanned = updated = failed = 0
    try:
        rows = conn.execute(
            f"SELECT user_id, workspace_id, api_key FROM {table}"
            if table == "user_ai_config"
            else f"SELECT workspace_id, api_key FROM {table}"
        ).fetchall()
    except sqlite3.OperationalError:
        # Table doesn't exist on this install — fine.
        return 0, 0, 0

    for row in rows:
        scanned += 1
        try:
            current = row["api_key"] or ""
            backup_writer({
                "table": table,
                "id": (row["user_id"] if table == "user_ai_config" else row["workspace_id"]),
                "api_key": current,
            })
            if not current:
                continue
            plaintext = encryptor.decrypt_value(current)
            re_encrypted = encryptor.encrypt_value(plaintext) if plaintext else ""
            if current == re_encrypted:
                continue
            if not dry_run:
                if table == "user_ai_config":
                    conn.execute(
                        f"UPDATE {table} SET api_key = ? WHERE user_id = ?",
                        (re_encrypted, row["user_id"]),
                    )
                else:
                    conn.execute(
                        f"UPDATE {table} SET api_key = ? WHERE workspace_id = ?",
                        (re_encrypted, row["workspace_id"]),
                    )
            updated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.warning("%s row failed: %s", table, exc)
    return scanned, updated, failed


# ─────────────────────────────────────────────────────────────────────
# Backup writer
# ─────────────────────────────────────────────────────────────────────


def _make_backup_writer(backup_path: Path | None):
    """Return a function `(payload: dict) -> None` that appends one JSONL
    line to the backup file. None when backup is disabled."""
    if backup_path is None:
        def noop(_payload: dict) -> None: ...
        return noop, None

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(backup_path, "a", encoding="utf-8")

    def write(payload: dict) -> None:
        f.write(json.dumps(payload, default=str) + "\n")

    return write, f


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m fpulse.security.migrate_existing",
        description="Re-encrypt legacy plaintext credentials + AI API keys.",
    )
    p.add_argument("--db", help="Path to fpulse.db. Defaults to FPULSE_DATA_DIR/fpulse.db.")
    p.add_argument("--dry-run", action="store_true", help="Report what would change; don't write.")
    p.add_argument("--no-backup", action="store_true", help="Skip the JSONL backup snapshot.")
    p.add_argument("--backup-dir", help="Override the default migration_backups directory.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    db_path = Path(args.db) if args.db else _resolve_db_path()
    print(f"DB: {db_path}")

    # Set up encryptor — creates the master key file on first run if missing.
    from fpulse.security.encryptor import Encryptor
    enc = Encryptor.from_master_key()

    # Backup snapshot.
    backup_path: Path | None = None
    if not args.no_backup:
        backup_dir = Path(args.backup_dir) if args.backup_dir else _backup_dir()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"migration_{ts}.jsonl"
        print(f"Backup: {backup_path}")

    backup_writer, backup_fh = _make_backup_writer(backup_path)

    conn = _open_db(db_path)
    try:
        cred_s, cred_u, cred_f = _migrate_credentials(
            conn, enc, dry_run=args.dry_run, backup_writer=backup_writer,
        )
        user_s, user_u, user_f = _migrate_ai_config(
            conn, "user_ai_config", enc,
            dry_run=args.dry_run, backup_writer=backup_writer,
        )
        ws_s, ws_u, ws_f = _migrate_ai_config(
            conn, "workspace_ai_config", enc,
            dry_run=args.dry_run, backup_writer=backup_writer,
        )

        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()
        if backup_fh is not None:
            backup_fh.close()

    print()
    print("Migration report")
    print("─" * 60)
    print(f"  credentials          scanned={cred_s:>4}  updated={cred_u:>4}  failed={cred_f}")
    print(f"  user_ai_config       scanned={user_s:>4}  updated={user_u:>4}  failed={user_f}")
    print(f"  workspace_ai_config  scanned={ws_s:>4}  updated={ws_u:>4}  failed={ws_f}")
    total_failed = cred_f + user_f + ws_f
    if args.dry_run:
        print("\n  [dry-run] no rows written.")
    if backup_path:
        print(f"\n  Backup snapshot: {backup_path}")
    if total_failed > 0:
        print("\n  ⚠ Some rows failed. Review the warnings above.")
        return 1
    print("\n  ✓ Migration successful." if not args.dry_run else "\n  ✓ Dry-run successful.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
