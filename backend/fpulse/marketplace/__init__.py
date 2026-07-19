"""
Pipeline Marketplace — community template sharing, ratings, and discovery.

Extends the built-in template gallery with user-published templates that
can be shared, rated, and imported across workspaces.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class MarketplaceStore:
    """SQLite-backed marketplace for community pipeline templates."""

    def __init__(self, db):
        self._db = db
        self._ensure_tables()

    def _ensure_tables(self):
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_templates (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                category TEXT DEFAULT 'general',
                author_id TEXT DEFAULT '',
                author_name TEXT DEFAULT '',
                workspace_id TEXT DEFAULT 'default',
                tags TEXT DEFAULT '[]',
                difficulty TEXT DEFAULT 'beginner',
                icon TEXT DEFAULT '📦',
                steps TEXT NOT NULL,
                connections TEXT DEFAULT '[]',
                params TEXT DEFAULT '{}',
                is_public INTEGER DEFAULT 1,
                downloads INTEGER DEFAULT 0,
                avg_rating REAL DEFAULT 0.0,
                rating_count INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_ratings (
                id TEXT PRIMARY KEY,
                template_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
                review TEXT DEFAULT '',
                created_at REAL NOT NULL,
                UNIQUE(template_id, user_id)
            )
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mp_templates_cat
            ON marketplace_templates(category)
        """)
        self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_mp_templates_author
            ON marketplace_templates(author_id)
        """)

    # ── Publish ────────────────────────────────────────────────────────

    def publish(
        self,
        name: str,
        description: str,
        category: str,
        steps: list[dict],
        connections: list[dict],
        author_id: str = "",
        author_name: str = "",
        workspace_id: str = "default",
        tags: list[str] | None = None,
        difficulty: str = "beginner",
        icon: str = "📦",
        is_public: bool = True,
    ) -> dict:
        """Publish a pipeline template to the marketplace."""
        template_id = f"mkt_{uuid.uuid4().hex[:8]}"
        now = time.time()
        self._db.execute(
            "INSERT INTO marketplace_templates "
            "(id, name, description, category, author_id, author_name, workspace_id, "
            "tags, difficulty, icon, steps, connections, is_public, downloads, "
            "avg_rating, rating_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0.0, 0, ?, ?)",
            (
                template_id, name, description, category, author_id, author_name,
                workspace_id, json.dumps(tags or []), difficulty, icon,
                json.dumps(steps), json.dumps(connections), 1 if is_public else 0,
                now, now,
            ),
        )
        return self.get(template_id)

    def publish_from_workflow(self, workflow, author_id: str = "", author_name: str = "",
                              category: str = "community", tags: list[str] | None = None,
                              difficulty: str = "intermediate", icon: str = "📦") -> dict:
        """Publish an existing workflow as a marketplace template."""
        # Build portable template (index-based connections)
        step_id_to_idx = {s.id: i for i, s in enumerate(workflow.steps)}
        export_steps = [
            {"type": s.type.value, "label": s.label, "params": s.params,
             "position": {"x": s.position.x, "y": s.position.y}}
            for s in workflow.steps
        ]
        export_connections = []
        for c in workflow.connections:
            from_idx = step_id_to_idx.get(c.from_step)
            to_idx = step_id_to_idx.get(c.to_step)
            if from_idx is not None and to_idx is not None:
                conn = {"from_step": from_idx, "to_step": to_idx}
                if c.from_port != "output":
                    conn["from_port"] = c.from_port
                if c.to_port != "input":
                    conn["to_port"] = c.to_port
                export_connections.append(conn)

        return self.publish(
            name=workflow.name,
            description=workflow.description,
            category=category,
            steps=export_steps,
            connections=export_connections,
            author_id=author_id,
            author_name=author_name,
            workspace_id=workflow.workspace_id,
            tags=tags or [],
            difficulty=difficulty,
            icon=icon,
        )

    # ── Browse ─────────────────────────────────────────────────────────

    def list_all(self, category: str | None = None, search: str | None = None,
                 sort: str = "downloads", limit: int = 100) -> list[dict]:
        """List marketplace templates with optional filtering."""
        query = "SELECT * FROM marketplace_templates WHERE is_public=1"
        params: list = []

        if category:
            query += " AND category=?"
            params.append(category)

        if search:
            query += " AND (name LIKE ? OR description LIKE ? OR tags LIKE ?)"
            like = f"%{search}%"
            params.extend([like, like, like])

        sort_col = {"downloads": "downloads DESC", "rating": "avg_rating DESC",
                     "newest": "created_at DESC", "name": "name ASC"}.get(sort, "downloads DESC")
        query += f" ORDER BY {sort_col} LIMIT ?"
        params.append(limit)

        rows = self._db.fetchall(query, tuple(params))
        return [self._row_to_dict(r) for r in rows]

    def get(self, template_id: str) -> dict | None:
        """Get a single marketplace template."""
        row = self._db.fetchone("SELECT * FROM marketplace_templates WHERE id=?", (template_id,))
        return self._row_to_dict(row) if row else None

    def get_by_author(self, author_id: str) -> list[dict]:
        """List templates published by a specific user."""
        rows = self._db.fetchall(
            "SELECT * FROM marketplace_templates WHERE author_id=? ORDER BY created_at DESC",
            (author_id,),
        )
        return [self._row_to_dict(r) for r in rows]

    def increment_downloads(self, template_id: str):
        """Increment the download counter."""
        self._db.execute(
            "UPDATE marketplace_templates SET downloads=downloads+1 WHERE id=?",
            (template_id,),
        )

    # ── Ratings ────────────────────────────────────────────────────────

    def rate(self, template_id: str, user_id: str, rating: int, review: str = "") -> dict:
        """Rate a marketplace template (1-5 stars). Updates existing rating."""
        rating = max(1, min(5, rating))
        rating_id = f"r_{uuid.uuid4().hex[:8]}"
        self._db.execute(
            "INSERT OR REPLACE INTO marketplace_ratings (id, template_id, user_id, rating, review, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rating_id, template_id, user_id, rating, review, time.time()),
        )
        # Recalculate average
        row = self._db.fetchone(
            "SELECT AVG(rating) as avg, COUNT(*) as cnt FROM marketplace_ratings WHERE template_id=?",
            (template_id,),
        )
        if row:
            self._db.execute(
                "UPDATE marketplace_templates SET avg_rating=?, rating_count=? WHERE id=?",
                (round(row["avg"] or 0, 2), row["cnt"], template_id),
            )
        return {"template_id": template_id, "rating": rating, "avg_rating": row["avg"] if row else 0}

    def get_ratings(self, template_id: str) -> list[dict]:
        """Get all ratings for a template."""
        rows = self._db.fetchall(
            "SELECT * FROM marketplace_ratings WHERE template_id=? ORDER BY created_at DESC",
            (template_id,),
        )
        return [dict(r) for r in rows]

    # ── Delete ─────────────────────────────────────────────────────────

    def delete(self, template_id: str, author_id: str = ""):
        """Delete a marketplace template (author or admin only)."""
        self._db.execute("DELETE FROM marketplace_ratings WHERE template_id=?", (template_id,))
        self._db.execute("DELETE FROM marketplace_templates WHERE id=?", (template_id,))

    def _row_to_dict(self, row) -> dict:
        d = dict(row)
        for key in ("tags", "steps", "connections", "params"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d
