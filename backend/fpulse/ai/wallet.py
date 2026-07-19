"""
Denial-of-wallet protection for the agent loop.

Two complementary guardrails:

  1. **Daily token caps** — per-user AND per-workspace. Hard cut-off when
     the daily total exceeds the limit. Resets at UTC midnight.
  2. **Per-user rate limit** — sliding-window minute counter. Defends against
     the token-bomb pattern where an attacker crafts a script that fires
     small requests at high frequency.

Both run BEFORE the LLM call so quota-exceeded turns never spend tokens
on the provider. Quota-block is recorded in the trace as
``outcome=policy_block`` with ``policy_rules_fired=["wallet:..."]``.

Tunable via env vars (clamped to safe ranges):
  - ``FPULSE_AGENT_DAILY_TOKENS_USER``       default 1_000_000  (clamp 1_000-10_000_000)
  - ``FPULSE_AGENT_DAILY_TOKENS_WORKSPACE``  default 10_000_000 (clamp 1_000-100_000_000)
  - ``FPULSE_AGENT_RATE_PER_MINUTE``         default 60         (clamp 1-1000)

Defaults raised 2026-05-01: original 100K/user was a denial-of-wallet
defense calibrated for an attack scenario, NOT for normal product usage.
With the system prompt + tool schemas + tool results + RAG context, a
single agent turn now consumes 5K-10K tokens; 100K/day capped real users
at ~10-20 questions which is too tight. New defaults = ~100-200 turns
per user per day (normal usage) and 1000-2000 turns per workspace per
day (small team usage). Operators can still tighten via env var if
they're running a public OSS instance with abuse risk.

Daily aggregates are persisted to SQLite (`ai_wallet_usage` table) so
restarts don't reset budgets mid-day. Rate-limit windows are per-process
in memory — restarts release the rate-limit (acceptable; it's a thin
defensive layer, not a financial control).
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return max(lo, min(hi, v))


def daily_user_cap() -> int:
    # 1_000_000 tokens/user/day = ~100-200 agent turns at typical 5K-10K
    # tokens/turn (system prompt + tool schemas + RAG + response). Generous
    # enough for active product use without being a denial-of-wallet hole.
    #
    # 2026-05-22 deep-mode note: ``mode='deep'`` widens extra_context to
    # ~6K and raises max_iterations to 8, so a single deep turn can run
    # 20-40K tokens — roughly ~25-50 deep turns/day at the default cap.
    # If operators rely heavily on deep mode, bump
    # FPULSE_AGENT_DAILY_TOKENS_USER (max 10M) before adoption.
    return _env_int("FPULSE_AGENT_DAILY_TOKENS_USER", 1_000_000, 1_000, 10_000_000)


def daily_workspace_cap() -> int:
    # 10_000_000 tokens/workspace/day = ~1000-2000 turns. Small team headroom.
    # See daily_user_cap for the deep-mode caveat (scales linearly with team size).
    return _env_int("FPULSE_AGENT_DAILY_TOKENS_WORKSPACE", 10_000_000, 1_000, 100_000_000)


def rate_per_minute() -> int:
    # 60 req/min = 1 per second average. Plenty for interactive use; still
    # blocks token-bomb scripts that try to fire 100s of requests in seconds.
    return _env_int("FPULSE_AGENT_RATE_PER_MINUTE", 60, 1, 1000)


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class QuotaCheck:
    """Result of WalletGuard.check_before_run()."""

    allowed: bool
    reason: str = ""
    rule: str = ""

    @property
    def policy_rules_fired(self) -> list[str]:
        return [self.rule] if self.rule else []


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS ai_wallet_usage (
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    day TEXT NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope_type, scope_id, day)
);
"""

_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_ai_wallet_day ON ai_wallet_usage(day)"


@dataclass
class WalletGuard:
    """Daily token + per-minute rate guardrail.

    Construct with a Database. Schema is created idempotently. Stateless
    between requests (counters live in SQLite); rate-limit deque is the
    only in-memory state.
    """

    _db: Any = None
    _rate_deques: dict[str, deque] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self._db is not None:
            self._ensure_schema()

    def set_db(self, db) -> None:
        self._db = db
        if db is not None:
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            self._db.execute(_CREATE_SQL)
            self._db.execute(_INDEX_SQL)
            self._db.commit()
        except Exception as exc:
            logger.warning("WalletGuard: schema init failed: %s", exc)

    # ── Read ────────────────────────────────────────────────────────────

    def daily_total(self, scope_type: str, scope_id: str) -> int:
        """Today's tokens_in + tokens_out for this scope. 0 on miss."""
        if self._db is None or not scope_id:
            return 0
        try:
            rows = self._db.fetchall(
                "SELECT tokens_in, tokens_out FROM ai_wallet_usage "
                "WHERE scope_type = ? AND scope_id = ? AND day = ? LIMIT 1",
                (scope_type, scope_id, _utc_today()),
            )
        except Exception:
            return 0
        if not rows:
            return 0
        r = rows[0]
        return int(r["tokens_in"] or 0) + int(r["tokens_out"] or 0)

    # ── Pre-flight check ────────────────────────────────────────────────

    def check_before_run(
        self,
        *,
        user_id: str | None,
        workspace_id: str,
    ) -> QuotaCheck:
        """Run all gates. Returns the first failing one, else allowed."""
        # 1. Per-user rate limit (in-memory, per-process)
        if user_id:
            now = time.monotonic()
            dq = self._rate_deques.setdefault(user_id, deque())
            cutoff = now - 60.0
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= rate_per_minute():
                return QuotaCheck(
                    allowed=False,
                    reason=f"rate limit exceeded ({rate_per_minute()}/min)",
                    rule="wallet:rate_limit_per_minute",
                )

        # 2. Per-user daily token cap
        if user_id:
            user_total = self.daily_total("user", user_id)
            cap = daily_user_cap()
            if user_total >= cap:
                return QuotaCheck(
                    allowed=False,
                    reason=f"daily user token cap reached ({user_total} >= {cap})",
                    rule="wallet:daily_user_token_cap",
                )

        # 3. Per-workspace daily token cap
        if workspace_id:
            ws_total = self.daily_total("workspace", workspace_id)
            cap = daily_workspace_cap()
            if ws_total >= cap:
                return QuotaCheck(
                    allowed=False,
                    reason=f"daily workspace token cap reached ({ws_total} >= {cap})",
                    rule="wallet:daily_workspace_token_cap",
                )

        return QuotaCheck(allowed=True)

    def note_request_started(self, user_id: str | None) -> None:
        """Record a request timestamp for the rate-limit window. Call after
        check_before_run() succeeds and before the LLM call begins."""
        if not user_id:
            return
        self._rate_deques.setdefault(user_id, deque()).append(time.monotonic())

    # ── Record usage post-run ───────────────────────────────────────────

    def record_usage(
        self,
        *,
        user_id: str | None,
        workspace_id: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float = 0.0,
    ) -> None:
        """Increment today's totals for both user and workspace scopes.

        Best-effort — on DB error we log but never raise; an agent run that
        completed shouldn't fail because billing telemetry was unavailable.
        """
        if self._db is None:
            return
        if tokens_in <= 0 and tokens_out <= 0 and cost_usd <= 0:
            return  # don't allocate a row for zero-usage records
        day = _utc_today()
        scopes: list[tuple[str, str]] = []
        if user_id:
            scopes.append(("user", user_id))
        if workspace_id:
            scopes.append(("workspace", workspace_id))

        for scope_type, scope_id in scopes:
            try:
                # UPSERT — accumulate counts on conflict
                self._db.execute(
                    """
                    INSERT INTO ai_wallet_usage
                    (scope_type, scope_id, day, tokens_in, tokens_out, cost_usd, request_count)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(scope_type, scope_id, day) DO UPDATE SET
                        tokens_in = tokens_in + excluded.tokens_in,
                        tokens_out = tokens_out + excluded.tokens_out,
                        cost_usd = cost_usd + excluded.cost_usd,
                        request_count = request_count + 1
                    """,
                    (scope_type, scope_id, day, tokens_in, tokens_out, cost_usd),
                )
                self._db.commit()
            except Exception as exc:
                logger.warning("WalletGuard.record_usage failed for %s/%s: %s", scope_type, scope_id, exc)

    # ── Diagnostics for the budget endpoint + tests ─────────────────────

    def usage_for(self, scope_type: str, scope_id: str, *, day: str | None = None) -> dict[str, Any]:
        """Return today's (or specified day's) row for this scope. Empty dict on miss."""
        if self._db is None or not scope_id:
            return {}
        d = day or _utc_today()
        try:
            rows = self._db.fetchall(
                "SELECT scope_type, scope_id, day, tokens_in, tokens_out, cost_usd, request_count "
                "FROM ai_wallet_usage WHERE scope_type = ? AND scope_id = ? AND day = ? LIMIT 1",
                (scope_type, scope_id, d),
            )
        except Exception:
            return {}
        if not rows:
            return {}
        return dict(rows[0])

    def reset_rate_for_tests(self) -> None:
        self._rate_deques.clear()
