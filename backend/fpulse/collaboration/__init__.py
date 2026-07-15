"""
Collaboration — inline comments, @mentions, and discussion threads on pipelines.

Supports node-level and pipeline-level comments with @mention notifications,
thread replies, and resolution tracking.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class CollaborationStore:
    """SQLite-backed collaboration: comments, threads, mentions."""

    def __init__(self, db):
        self._db = db
        self._ensure_tables()

    def _ensure_tables(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL,
                step_id TEXT DEFAULT '',
                parent_id TEXT DEFAULT '',
                author_id TEXT NOT NULL,
                author_name TEXT DEFAULT '',
                body TEXT NOT NULL,
                mentions TEXT DEFAULT '[]',
                resolved INTEGER DEFAULT 0,
                resolved_by TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_comments_wf
            ON comments(workflow_id)
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_comments_step
            ON comments(workflow_id, step_id)
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_comments_parent
            ON comments(parent_id)
        """)

    # ── Create ─────────────────────────────────────────────────────────

    def add_comment(
        self,
        workflow_id: str,
        author_id: str,
        body: str,
        step_id: str = "",
        parent_id: str = "",
        author_name: str = "",
    ) -> dict:
        """Add a comment to a pipeline or specific node."""
        comment_id = f"c_{uuid.uuid4().hex[:8]}"
        mentions = self._extract_mentions(body)
        now = time.time()
        self._db.execute(
            "INSERT INTO comments "
            "(id, workflow_id, step_id, parent_id, author_id, author_name, body, mentions, resolved, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (comment_id, workflow_id, step_id, parent_id, author_id, author_name,
             body, json.dumps(mentions), now, now),
        )
        return self.get_comment(comment_id)

    def update_comment(self, comment_id: str, body: str, author_id: str) -> dict | None:
        """Update a comment's body (author only)."""
        existing = self.get_comment(comment_id)
        if not existing or existing["author_id"] != author_id:
            return None
        mentions = self._extract_mentions(body)
        self._db.execute(
            "UPDATE comments SET body=?, mentions=?, updated_at=? WHERE id=?",
            (body, json.dumps(mentions), time.time(), comment_id),
        )
        return self.get_comment(comment_id)

    def resolve_comment(self, comment_id: str, resolved_by: str) -> dict | None:
        """Mark a comment thread as resolved."""
        self._db.execute(
            "UPDATE comments SET resolved=1, resolved_by=?, updated_at=? WHERE id=?",
            (resolved_by, time.time(), comment_id),
        )
        return self.get_comment(comment_id)

    def unresolve_comment(self, comment_id: str) -> dict | None:
        """Reopen a resolved comment thread."""
        self._db.execute(
            "UPDATE comments SET resolved=0, resolved_by='', updated_at=? WHERE id=?",
            (time.time(), comment_id),
        )
        return self.get_comment(comment_id)

    def delete_comment(self, comment_id: str, author_id: str = "") -> bool:
        """Delete a comment and its replies."""
        # Delete replies first
        self._db.execute("DELETE FROM comments WHERE parent_id=?", (comment_id,))
        self._db.execute("DELETE FROM comments WHERE id=?", (comment_id,))
        return True

    # ── Read ───────────────────────────────────────────────────────────

    def get_comment(self, comment_id: str) -> dict | None:
        """Get a single comment."""
        row = self._db.fetchone("SELECT * FROM comments WHERE id=?", (comment_id,))
        return self._row_to_dict(row) if row else None

    def list_comments(self, workflow_id: str, step_id: str | None = None,
                      include_resolved: bool = True) -> list[dict]:
        """List all comments for a workflow (optionally filtered by step)."""
        if step_id:
            query = "SELECT * FROM comments WHERE workflow_id=? AND step_id=? AND parent_id=''"
            params = (workflow_id, step_id)
        else:
            query = "SELECT * FROM comments WHERE workflow_id=? AND parent_id=''"
            params = (workflow_id,)

        if not include_resolved:
            query += " AND resolved=0"

        query += " ORDER BY created_at DESC"
        rows = self._db.fetchall(query, params)

        # Attach replies to each top-level comment
        comments = []
        for row in rows:
            comment = self._row_to_dict(row)
            replies = self._db.fetchall(
                "SELECT * FROM comments WHERE parent_id=? ORDER BY created_at ASC",
                (comment["id"],),
            )
            comment["replies"] = [self._row_to_dict(r) for r in replies]
            comment["reply_count"] = len(replies)
            comments.append(comment)

        return comments

    def get_mentions(self, user_id: str, limit: int = 50) -> list[dict]:
        """Get all comments that @mention a specific user."""
        rows = self._db.fetchall(
            "SELECT * FROM comments WHERE mentions LIKE ? ORDER BY created_at DESC LIMIT ?",
            (f'%"{user_id}"%', limit),
        )
        return [self._row_to_dict(r) for r in rows]

    def get_stats(self, workflow_id: str) -> dict:
        """Get comment stats for a workflow."""
        total = self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM comments WHERE workflow_id=?", (workflow_id,),
        )
        unresolved = self._db.fetchone(
            "SELECT COUNT(*) as cnt FROM comments WHERE workflow_id=? AND parent_id='' AND resolved=0",
            (workflow_id,),
        )
        node_comments = self._db.fetchall(
            "SELECT step_id, COUNT(*) as cnt FROM comments WHERE workflow_id=? AND step_id!='' GROUP BY step_id",
            (workflow_id,),
        )
        return {
            "total": total["cnt"] if total else 0,
            "unresolved": unresolved["cnt"] if unresolved else 0,
            "by_node": {r["step_id"]: r["cnt"] for r in node_comments},
        }

    # ── Helpers ────────────────────────────────────────────────────────

    def _extract_mentions(self, body: str) -> list[str]:
        """Extract @mentions from comment body."""
        return re.findall(r"@(\w+)", body)

    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        if "mentions" in d and isinstance(d["mentions"], str):
            try:
                d["mentions"] = json.loads(d["mentions"])
            except (json.JSONDecodeError, TypeError):
                d["mentions"] = []
        return d
