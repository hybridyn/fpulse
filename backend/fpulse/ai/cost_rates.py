"""Per-workspace AI cost-rate table.

Replaces the hardcoded `(tokens / 1000) * 0.0006` blended estimate that
shipped before this module — see PAGE_BY_PAGE_AUDIT.md (cost estimate
honesty) for the original problem statement.

Resolution order for a given (provider, model) tuple:

  1. Exact match in ``models[<model>]`` (most specific).
  2. Provider match in ``providers[<provider>]``.
  3. ``fallback`` rate (last-resort approximation).

Rates are expressed as USD per million tokens, split into input + output,
so cloud APIs that price output 3-5x higher than input get costed
correctly. Ollama / local inference is special-cased to $0 — there is no
per-token bill for local compute, and showing a fictional cost there
misleads users who picked OSS precisely for that reason.

Storage uses the existing ``workspace_settings`` row (one row per
workspace, JSON blob). The key inside that blob is ``ai_cost_rates``.
Absent key → return DEFAULT_RATES.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)
# reload-trigger: 2026-05-19-01


# Rates in USD per million tokens. Sourced from public pricing as of
# 2026-05-19. Users can override any of these in Settings → AI Pricing.
# Cached-input rates assume prompt-caching cache-read (the cheaper bucket).
# Anthropic ≈ 10% of input rate, OpenAI ≈ 50%. These are forward-compatible:
# the OSS trace store currently lumps cached + uncached tokens together in
# `total_tokens_in`, so cached rates contribute $0 until per-provider clients
# are instrumented to report a `cached_tokens_in` sub-count. The data shape
# is ready; the data feed is not (yet).
DEFAULT_RATES: dict[str, Any] = {
    "providers": {
        "ollama":     {"input_per_mtok": 0.0,   "cached_input_per_mtok": 0.0,  "output_per_mtok": 0.0,   "label": "Local — no per-token cost"},
        "anthropic":  {"input_per_mtok": 3.0,   "cached_input_per_mtok": 0.30, "output_per_mtok": 15.0,  "label": "Anthropic (Sonnet-tier default)"},
        "openai":     {"input_per_mtok": 2.5,   "cached_input_per_mtok": 1.25, "output_per_mtok": 10.0,  "label": "OpenAI (GPT-4o-tier default)"},
        "openrouter": {"input_per_mtok": 1.0,   "cached_input_per_mtok": 0.50, "output_per_mtok": 3.0,   "label": "OpenRouter (blended default)"},
    },
    "models": {
        "claude-haiku-4-5":   {"input_per_mtok": 0.80, "cached_input_per_mtok": 0.08, "output_per_mtok": 4.0},
        "claude-sonnet-4-6":  {"input_per_mtok": 3.0,  "cached_input_per_mtok": 0.30, "output_per_mtok": 15.0},
        "claude-opus-4-7":    {"input_per_mtok": 15.0, "cached_input_per_mtok": 1.50, "output_per_mtok": 75.0},
        "gpt-4o-mini":        {"input_per_mtok": 0.15, "cached_input_per_mtok": 0.075,"output_per_mtok": 0.60},
        "gpt-4o":             {"input_per_mtok": 2.50, "cached_input_per_mtok": 1.25, "output_per_mtok": 10.0},
    },
    "fallback": {"input_per_mtok": 0.30, "cached_input_per_mtok": 0.15, "output_per_mtok": 0.60, "label": "Default pricing — applied when neither provider nor model is recognised"},
}


_RATE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS workspace_settings (
    workspace_id TEXT PRIMARY KEY,
    settings TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    updated_by TEXT
)
"""


def _coerce_rate(raw: Any) -> Optional[dict[str, Any]]:
    """Return a normalised rate dict or None if `raw` isn't shaped like one."""
    if not isinstance(raw, dict):
        return None
    try:
        out = {
            "input_per_mtok": float(raw.get("input_per_mtok", 0)),
            "cached_input_per_mtok": float(raw.get("cached_input_per_mtok", 0)),
            "output_per_mtok": float(raw.get("output_per_mtok", 0)),
        }
    except (TypeError, ValueError):
        return None
    label = raw.get("label")
    if isinstance(label, str) and label:
        out["label"] = label
    return out


def resolve_rate(rates: dict[str, Any], provider: Optional[str], model: Optional[str]) -> dict[str, Any]:
    """Pick the most-specific rate for (provider, model). Always returns a dict
    with input_per_mtok + output_per_mtok keys."""
    rates = rates or DEFAULT_RATES
    if model:
        models = rates.get("models") or {}
        # 1. Exact match.
        r = _coerce_rate(models.get(model))
        if r:
            return r
        # 2. Strip a provider namespace prefix ("openai/gpt-4o-mini" → "gpt-4o-mini").
        tail = model.split("/")[-1] if "/" in model else model
        if tail != model:
            r = _coerce_rate(models.get(tail))
            if r:
                return r
        # 3. Longest registered key that is a base of the id, so dated /
        # suffixed variants resolve to their family rate
        # ("gpt-4o-mini-2024-07-18" → "gpt-4o-mini"). A separator guard
        # prevents "gpt-4o" from greedily matching "gpt-4o-mini".
        best: Optional[str] = None
        for key in models:
            if tail == key or tail.startswith(key + "-") or tail.startswith(key + ":"):
                if best is None or len(key) > len(best):
                    best = key
        if best is not None:
            r = _coerce_rate(models.get(best))
            if r:
                return r
    if provider:
        p = (rates.get("providers") or {}).get(provider.lower())
        r = _coerce_rate(p)
        if r:
            return r
        if provider.lower() == "ollama":
            return {"input_per_mtok": 0.0, "cached_input_per_mtok": 0.0, "output_per_mtok": 0.0, "label": "Local — no per-token cost"}
    fb = _coerce_rate(rates.get("fallback")) or _coerce_rate(DEFAULT_RATES["fallback"])
    return fb or {"input_per_mtok": 0.0, "cached_input_per_mtok": 0.0, "output_per_mtok": 0.0}


def compute_cost_usd(
    rates: dict[str, Any],
    provider: Optional[str],
    model: Optional[str],
    tokens_in: int,
    tokens_out: int,
    cached_tokens_in: int = 0,
) -> float:
    """USD cost for one agent run.

    ``cached_tokens_in`` is the subset of ``tokens_in`` that was served from
    a prompt cache (provider-reported). It is OPTIONAL today because the
    OSS trace store does not yet capture the cached-vs-uncached split —
    when ``cached_tokens_in == 0`` the cached rate contributes nothing and
    every input token is priced at the full input rate, matching today's
    behaviour. Cost simulators and Plus governance can pass the breakdown
    once it's available.
    """
    r = resolve_rate(rates, provider, model)
    uncached_in = max(0, int(tokens_in) - int(cached_tokens_in))
    cached_in = max(0, int(cached_tokens_in))
    return (
        (uncached_in / 1_000_000.0) * r["input_per_mtok"]
        + (cached_in / 1_000_000.0) * r["cached_input_per_mtok"]
        + (tokens_out / 1_000_000.0) * r["output_per_mtok"]
    )


# ── Storage (workspace_settings row, key `ai_cost_rates`) ────────────────────

def _get_db():
    from fpulse.main import app_state
    return app_state.get("db")


def _ensure_table(db) -> None:
    """Workspace_settings may not exist yet on a stale dev DB. Idempotent."""
    try:
        db.execute(_RATE_TABLE_DDL)
        db.commit()
    except Exception as exc:
        logger.warning("cost_rates: workspace_settings DDL failed: %s", exc)


def _read_blob(db, workspace_id: str) -> dict[str, Any]:
    try:
        row = db.fetchone(
            "SELECT settings FROM workspace_settings WHERE workspace_id = ?",
            (workspace_id,),
        )
    except Exception as exc:
        logger.warning("cost_rates._read_blob DB error: %s", exc)
        return {}
    if not row:
        return {}
    raw = row.get("settings")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_rates(workspace_id: str) -> dict[str, Any]:
    """Read effective rates for a workspace. Defaults merged with overrides."""
    db = _get_db()
    if db is None:
        return DEFAULT_RATES
    _ensure_table(db)
    blob = _read_blob(db, workspace_id)
    overrides = blob.get("ai_cost_rates") if isinstance(blob, dict) else None
    if not isinstance(overrides, dict):
        return DEFAULT_RATES
    return _apply_tombstones(_merge_rates(DEFAULT_RATES, overrides), overrides)


def _merge_rates(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge `over` into `base`. `over` wins per key; unknown/invalid
    rate dicts are dropped so user-supplied junk can't crash the resolver."""
    out: dict[str, Any] = {
        "providers": dict(base.get("providers") or {}),
        "models": dict(base.get("models") or {}),
        "fallback": dict(base.get("fallback") or {}),
    }
    for section in ("providers", "models"):
        section_over = over.get(section)
        if isinstance(section_over, dict):
            for k, v in section_over.items():
                r = _coerce_rate(v)
                if r:
                    out[section][k] = r
    fb = _coerce_rate(over.get("fallback"))
    if fb:
        out["fallback"] = fb
    return out


def _apply_tombstones(effective: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Drop built-in providers/models the user explicitly deleted.

    DEFAULT_RATES is always merged in on read, so a deleted built-in would
    otherwise reappear. ``set_rates`` records deletions as ``removed_providers``
    / ``removed_models`` lists in the overrides blob; this removes them from the
    effective table. Custom (non-default) entries need no tombstone — they're
    simply absent from the overrides section, so the merge never re-adds them.
    Mutates and returns ``effective``.
    """
    for section, tomb_key in (("providers", "removed_providers"), ("models", "removed_models")):
        removed = overrides.get(tomb_key)
        if isinstance(removed, list):
            table = effective.get(section)
            if isinstance(table, dict):
                for name in removed:
                    table.pop(name, None)
    return effective


def set_rates(workspace_id: str, updates: dict[str, Any], *, updated_by: Optional[str] = None) -> dict[str, Any]:
    """Merge `updates` into the persisted ai_cost_rates blob. Returns the
    effective rate table (defaults merged with the new overrides)."""
    db = _get_db()
    if db is None:
        raise RuntimeError("Database not available")
    _ensure_table(db)
    blob = _read_blob(db, workspace_id) or {}
    existing = blob.get("ai_cost_rates") if isinstance(blob.get("ai_cost_rates"), dict) else {}
    existing = existing or {}
    # Section-level REPLACE for any section the caller sends, so REMOVING a
    # provider/model actually persists. (A key-wise merge could only add or
    # overwrite — deletions silently reappeared on the next read.) Sections the
    # caller omits are left untouched. DEFAULT_RATES still merges on READ, so a
    # deleted built-in is also recorded as a tombstone (below) and stripped by
    # _apply_tombstones — otherwise the default merge would resurrect it.
    merged: dict[str, Any] = {
        "providers": dict(existing.get("providers") or {}),
        "models": dict(existing.get("models") or {}),
        "fallback": dict(existing.get("fallback") or {}),
    }
    for section in ("providers", "models"):
        section_in = (updates or {}).get(section)
        if isinstance(section_in, dict):
            cleaned: dict[str, Any] = {}
            for k, v in section_in.items():
                r = _coerce_rate(v)
                if r:
                    cleaned[k] = r
            merged[section] = cleaned  # replace wholesale → removals take effect
    fb_in = _coerce_rate((updates or {}).get("fallback"))
    if fb_in:
        merged["fallback"] = fb_in

    # Tombstones — when the caller sends a section, any DEFAULT entry missing
    # from it was deleted by the user. Record it so the always-on default merge
    # on read doesn't resurrect it; re-adding a built-in (it's back in the sent
    # set) clears its tombstone. Sections the caller omits keep their existing
    # tombstones. Custom entries need none (they aren't in DEFAULT_RATES).
    merged["removed_providers"] = list(existing.get("removed_providers") or [])
    merged["removed_models"] = list(existing.get("removed_models") or [])
    for section, tomb_key in (("providers", "removed_providers"), ("models", "removed_models")):
        section_in = (updates or {}).get(section)
        if isinstance(section_in, dict):
            default_keys = set(DEFAULT_RATES.get(section, {}).keys())
            merged[tomb_key] = sorted(default_keys - set(section_in.keys()))

    blob["ai_cost_rates"] = merged

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO workspace_settings (workspace_id, settings, updated_at, updated_by)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(workspace_id) DO UPDATE SET
            settings = excluded.settings,
            updated_at = excluded.updated_at,
            updated_by = excluded.updated_by
        """,
        (workspace_id, json.dumps(blob), now, updated_by or "system"),
    )
    db.commit()
    return _apply_tombstones(_merge_rates(DEFAULT_RATES, merged), merged)


def reset_rates(workspace_id: str, *, updated_by: Optional[str] = None) -> dict[str, Any]:
    """Drop the user's ai_cost_rates overrides; subsequent reads return DEFAULT_RATES."""
    db = _get_db()
    if db is None:
        raise RuntimeError("Database not available")
    _ensure_table(db)
    blob = _read_blob(db, workspace_id) or {}
    blob.pop("ai_cost_rates", None)
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO workspace_settings (workspace_id, settings, updated_at, updated_by)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(workspace_id) DO UPDATE SET
            settings = excluded.settings,
            updated_at = excluded.updated_at,
            updated_by = excluded.updated_by
        """,
        (workspace_id, json.dumps(blob), now, updated_by or "system"),
    )
    db.commit()
    return DEFAULT_RATES
