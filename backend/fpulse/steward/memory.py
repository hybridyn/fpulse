"""Steward memory - the durable learning layer.

# Dismiss-reason sanitization (2026-06-06)

Operator free-text dismiss reasons land in the append-only memory.jsonl
journal verbatim - they need to. They are the *captured tribal knowledge*
the future Curator sub-agent will mine. But "verbatim" must not mean
"leaks secrets into a plaintext audit file."

The ``_sanitize_reason`` function below applies a conservative regex
sweep before writing. It catches the four most common accidental leaks
operators have shipped to incident comments in the wild:

  1. AWS-style access-key IDs (AKIA / ASIA prefix + 16 chars)
  2. Bearer tokens / API keys in `Bearer XXXX` form
  3. Email-style URIs that look like SMTP credentials
     (`user:password@host`)
  4. JDBC-style connection strings with `password=` query params

Sanitization is opt-in-aggressive (false positives are fine - a
redacted operator note is annoying, but a leaked secret in
git-committable steward memory is a security incident). The redacted
text becomes ``[REDACTED:<kind>]`` so the operator can see *something*
got stripped and re-phrase if they meant to mention the prefix.

# How the Memory Layer learns

The Archeologist as-is is *stateless*: every scan re-derives findings
from the workflow store. That's correct, but it isn't *learning*. This
module is where "learning from mistakes" actually happens.

Every emit / dismiss / resolve event is appended to a per-workspace
JSONL journal. From that journal we derive:

  1. **Persistent occurrence counts** - a finding seen across 5 separate
     scans is more urgent than one seen 5 times in a single scan. We use
     "distinct scan_id buckets the signature appeared in" so a tight
     re-scan loop doesn't artificially inflate the counter.

  2. **Severity escalation** - once persistent_occurrences crosses the
     workspace's ``escalate_after_n_occurrences`` threshold (default 5),
     the next scan promotes a P3 to P2, or a P2 to P1. The user wanted
     a real consequence for "you've ignored this 5 times" - this is it.

  3. **Rebound detection** - if a signature was dismissed-then-resolved
     and re-appears later, we mark it as "rebounded" and surface that in
     the finding body. Common signal: someone deleted a duplicate
     pipeline, but a teammate re-built it. We want them to see *that
     specific story*, not the generic title again.

  4. **False-positive feedback** - the Curator (1.4) will use the
     dismiss-with-reason history to retrain heuristics. We collect the
     raw signal now so 1.4 has data to learn from when it ships.

# JSONL event shape

Each line is a single event:

    {"ts": "...", "scan_id": "...", "kind": "emit|dismiss|resolve",
     "finding_id": "...", "signature": "...", "severity": "...",
     "evidence_summary": {...},  // only on emit
     "reason": "..."}            // only on dismiss

# Why JSONL not SQLite

The Steward must remain useful when DuckDB / SQLite is busy serving
the executor. A plain append-only file is:
  * lock-free in append mode on Windows + POSIX
  * trivially rebuildable from log replay (no schema migration)
  * grep-able for ops debugging
  * portable across the data dir
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import FindingKind, FindingSeverity, FindingStatus, StewardFinding

# Housekeeping / hygiene findings that should NOT auto-escalate on
# repetition. An orphaned managed table is the same low-priority fact every
# scan — leaving it un-dismissed (common in dev workspaces full of leftover
# test tables) is not evidence of rising risk, so bumping P3 -> P2 -> P1 just
# manufactures false urgency. These kinds stay at the severity the detector
# assigned; a recurring *reliability* finding (a failing connector, drifting
# schema) still escalates, because there repetition genuinely means worse.
_NON_ESCALATING_KINDS = frozenset({FindingKind.ORPHANED_TABLE})


# Regex sweep applied to operator-supplied dismiss reasons before they
# hit the append-only journal. Conservative on purpose - see module
# docstring §"Dismiss-reason sanitization".
_REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # AWS access keys (AKIA / ASIA + 16 alnum chars)
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED:aws-key]"),
    # Bearer tokens (Authorization headers pasted into reasons)
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer [REDACTED:token]"),
    # Generic high-entropy secrets in `password=...` or `secret=...` form
    (re.compile(r"(?i)\b(password|passwd|secret|api_key|apikey|token)\s*=\s*[^\s,;]+"),
     r"\1=[REDACTED:secret]"),
    # `user:password@host` credentials inside URIs
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[REDACTED:credentials]@"),
    # Private IPv4 ranges (intentional - operators shouldn't paste prod IPs
    # into a runbook lesson; if they need a host id, they should reference
    # the connection_id instead)
    (re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
     "[REDACTED:private-ip]"),
]


def _sanitize_reason(reason: str | None) -> str:
    """Strip the four common accidental-secret patterns from a dismiss
    reason before it hits the audit log. See module docstring."""
    if not reason:
        return ""
    out = reason
    for pat, replacement in _REDACTION_PATTERNS:
        out = pat.sub(replacement, out)
    return out


# 2026-06-07 - public alias for cross-module callers (api/steward.py
# now uses the same sanitiser to scrub `fix_note` text on the resolve
# endpoint before it becomes a Memory-Layer lesson candidate). Same
# implementation, just a public name to avoid the underscore-prefixed
# private-import smell.
sanitize_user_note = _sanitize_reason


# Per-file lock - JSONL appends are atomic on Linux up to PIPE_BUF
# (4096B), but on Windows the OS doesn't guarantee it. Cheap lock keeps
# concurrent /scan calls from interleaving lines.
_FILE_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bump_severity(current: FindingSeverity) -> FindingSeverity:
    """One-step promotion: P3 → P2 → P1. P1 stays P1 (already top)."""
    if current == FindingSeverity.P3:
        return FindingSeverity.P2
    if current == FindingSeverity.P2:
        return FindingSeverity.P1
    return FindingSeverity.P1


class StewardMemory:
    """Per-workspace memory backed by a JSONL journal.

    Instances are cheap to construct - they re-read the journal on
    every property access that needs aggregate state. For workspaces
    larger than ~10 000 events we'd cache, but at OSS scale the file
    is small enough that re-reading is fast and avoids cache-invalidation
    bugs entirely.
    """

    def __init__(self, journal_path: Path):
        self.path = journal_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── Write side ───────────────────────────────────────────────

    def _append(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with _FILE_LOCK:
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")

    def record_emit(self, scan_id: str, finding: StewardFinding) -> None:
        """Called once per scan per finding produced. Records the
        signature + severity at emit-time so we can answer "did the
        severity escalate between scans?"."""
        sig = (
            finding.evidence.get("source_signature")
            or finding.evidence.get("shape_signature")
            or finding.id
        )
        self._append({
            "ts": _iso_now(),
            "scan_id": scan_id,
            "kind": "emit",
            "finding_id": finding.id,
            "finding_kind": finding.kind.value,
            "signature": sig,
            "severity_at_emit": finding.severity.value,
            # Compact summary - keeps the journal small. Full evidence
            # is reconstructable from the current scan if needed.
            "evidence_summary": {
                "workflow_count": len(finding.evidence.get("workflows") or []),
                "workflow_ids": [
                    w.get("id") for w in (finding.evidence.get("workflows") or [])
                ][:10],
            },
        })

    def record_dismiss(self, finding_id: str, signature: str, reason: str | None = None) -> None:
        """Records user's explicit suppression. ``reason`` is optional
        free-text the user supplied (the curator-1.4 will mine this).

        The reason text is passed through ``_sanitize_reason`` (see
        module docstring §"Dismiss-reason sanitization") so accidental
        secrets in operator notes never reach the append-only log.
        Sanitization is conservative - a redacted note is better than
        an AWS key in git-committable steward memory.
        """
        self._append({
            "ts": _iso_now(),
            "scan_id": None,
            "kind": "dismiss",
            "finding_id": finding_id,
            "signature": signature,
            "reason": _sanitize_reason(reason),
        })

    def record_resolve(self, finding_id: str, signature: str | None) -> None:
        self._append({
            "ts": _iso_now(),
            "scan_id": None,
            "kind": "resolve",
            "finding_id": finding_id,
            "signature": signature or "",
        })

    # ── Read side ────────────────────────────────────────────────

    def _events(self) -> Iterable[dict[str, Any]]:
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        # Skip corrupt lines - don't crash the scan path.
                        continue
        except OSError:
            return

    def persistent_occurrences(self) -> dict[str, int]:
        """For each signature, count the distinct scan_ids it appeared
        in. This is the canonical "how many times have we *bothered*
        the user about this" counter - re-running the same scan twice
        in 30 seconds doesn't inflate it.

        **Dismiss-resets-the-counter rule** (2026-06-06, per Review 1
        alert-fatigue concern). If a signature has been dismissed at
        least once, only scans whose timestamp is AFTER the most
        recent dismiss event contribute. Otherwise: a signature with
        an old 8-scan history would, on re-creation, immediately
        inherit the old count and escalate to P1 on the very first
        re-emit - exactly the spam disaster the dismiss-with-reason
        loop is supposed to prevent. The dismiss acts as a clean
        slate; if the pattern persists past the dismiss, the counter
        starts fresh and the escalation guard requires N MORE scans
        before bumping severity.
        """
        # Walk the journal in append order. A `dismiss` event for
        # signature X resets the scan-id set for X. Subsequent `emit`
        # events for X are counted again from zero.
        #
        # Why journal order, not timestamp:
        # ISO timestamps resolve to microseconds but tight test loops
        # (and real high-frequency cron pipelines) can squeeze many
        # events into the same microsecond. Using `ts >= cutoff` then
        # either over-counts (same-µs emits look post-dismiss) or
        # under-counts (same-µs emits look pre-dismiss). Journal
        # append order is the authoritative sequence.
        scans_by_signature: dict[str, set[str]] = defaultdict(set)
        for ev in self._events():
            sig = ev.get("signature") or ""
            if not sig:
                continue
            kind = ev.get("kind")
            if kind == "dismiss":
                # Reset — pre-dismiss history is no longer relevant
                # for escalation purposes (alert-fatigue prevention,
                # Architectural Review 1).
                scans_by_signature.pop(sig, None)
            elif kind == "emit":
                scan_id = ev.get("scan_id")
                if scan_id:
                    scans_by_signature[sig].add(scan_id)
        return {sig: len(scans) for sig, scans in scans_by_signature.items()}

    def dismissal_history(self) -> dict[str, list[dict[str, Any]]]:
        """Per-signature list of every dismiss event. The presence of
        an entry here doesn't mean the signature is currently suppressed
        (that's the suppressions.json file's job) - it means the user
        has dismissed it at least once historically. We use this to
        detect "rebounded" findings."""
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ev in self._events():
            if ev.get("kind") == "dismiss":
                sig = ev.get("signature") or ""
                if sig:
                    out[sig].append({"ts": ev["ts"], "reason": ev.get("reason", "")})
        return out

    def first_seen_per_signature(self) -> dict[str, str]:
        """Earliest emit timestamp per signature. Used by
        ``apply_learning()`` to time-clamp the severity-escalation
        check - a finding only escalates once its first emit is at
        least ``escalate_min_hours_since_first`` old, preventing
        high-frequency micro-pipelines from racing to P1 in minutes."""
        earliest: dict[str, str] = {}
        for ev in self._events():
            if ev.get("kind") != "emit":
                continue
            sig = ev.get("signature") or ""
            if not sig:
                continue
            ts = ev.get("ts", "")
            if not ts:
                continue
            if sig not in earliest or ts < earliest[sig]:
                earliest[sig] = ts
        return earliest

    def resolved_signatures(self) -> dict[str, str]:
        """Most-recent resolve timestamp per signature. Used for
        rebound detection - if a signature was resolved last week and
        re-emerges now, that's a regression worth highlighting."""
        latest: dict[str, str] = {}
        for ev in self._events():
            if ev.get("kind") == "resolve":
                sig = ev.get("signature") or ""
                if not sig:
                    continue
                ts = ev.get("ts", "")
                if ts > latest.get(sig, ""):
                    latest[sig] = ts
        return latest

    def audit_trail(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Most-recent ``limit`` events, newest first. Backing for the
        ``GET /api/steward/memory`` endpoint."""
        all_events = list(self._events())
        all_events.sort(key=lambda e: e.get("ts", ""), reverse=True)
        return all_events[:limit]

    def stats(self) -> dict[str, Any]:
        """High-level summary for the UI 'Memory' tab. Counts only -
        no sensitive content, safe to surface as an at-a-glance card."""
        scans: set[str] = set()
        emits = dismisses = resolves = 0
        signatures: set[str] = set()
        for ev in self._events():
            k = ev.get("kind")
            if k == "emit":
                emits += 1
                if ev.get("scan_id"):
                    scans.add(ev["scan_id"])
                if ev.get("signature"):
                    signatures.add(ev["signature"])
            elif k == "dismiss":
                dismisses += 1
            elif k == "resolve":
                resolves += 1
        return {
            "total_events": emits + dismisses + resolves,
            "total_scans": len(scans),
            "total_emits": emits,
            "total_dismisses": dismisses,
            "total_resolves": resolves,
            "distinct_signatures_seen": len(signatures),
        }


# ── Public helpers used by the scan pipeline ─────────────────────────

def new_scan_id() -> str:
    """A short opaque ID. We don't need cryptographic randomness -
    just enough entropy that two scans within the same millisecond
    don't collide for the persistent_occurrences counter."""
    return uuid.uuid4().hex[:12]


def apply_learning(
    findings: list[StewardFinding],
    memory: StewardMemory,
    *,
    escalate_after_n_occurrences: int = 5,
    escalate_min_hours_since_first: int = 24,
) -> list[StewardFinding]:
    """Mutate the in-memory findings list to reflect what we've learned
    from history.

    Three effects:

      1. **Time-clamped severity escalation** - if a signature has been
         emitted in ``escalate_after_n_occurrences`` distinct scans AND
         the first emit was at least ``escalate_min_hours_since_first``
         ago, bump severity one step (P3 -> P2 -> P1) and annotate the
         body. The time clamp (2026-06-05, per architectural review
         block 1C) prevents high-frequency micro-pipelines from
         escalating in minutes - a 60-second scheduled flow hitting 5
         scans in 5 minutes is NOT a chronic ignored issue, it's a
         normal cadence. The default 24-hour minimum window means an
         issue has to persist across at least one operator workday
         before its severity bumps.

      2. **Rebound state** - if a signature was resolved historically
         and is back, set ``status = REBOUNDED`` (a first-class enum
         value as of 2026-06-05, was previously just a title prefix)
         AND prefix the title for backward-compat with the existing UI
         label code. Body explains the prior resolution timestamp so
         the user can investigate the regression.

      3. **Persistent occurrence backfill** - ``occurrences`` is bumped
         to the cross-scan distinct-scan count when that's larger than
         the within-scan workflow count. UI shows "seen in N scans"
         rather than just "affects N workflows this scan."
    """
    from datetime import datetime, timezone, timedelta

    occ_by_sig = memory.persistent_occurrences()
    resolved_at = memory.resolved_signatures()
    first_seen_at = memory.first_seen_per_signature()
    now = datetime.now(timezone.utc)
    min_window = timedelta(hours=max(0, escalate_min_hours_since_first))

    enriched: list[StewardFinding] = []
    for f in findings:
        sig = (
            f.evidence.get("source_signature")
            or f.evidence.get("shape_signature")
        )
        if not sig:
            enriched.append(f)
            continue

        # Persistent (across-scan) occurrence count overrides the
        # per-scan count if it's larger.
        persistent = occ_by_sig.get(sig, 0)
        if persistent > f.occurrences:
            f.occurrences = persistent

        # ── Time-clamped severity escalation ────────────────────────
        # Housekeeping kinds (orphaned tables) are exempt — repetition of a
        # known-harmless leftover isn't rising risk, just an un-dismissed fact.
        if (
            persistent >= escalate_after_n_occurrences
            and f.severity != FindingSeverity.P1
            and f.kind not in _NON_ESCALATING_KINDS
        ):
            first_ts = first_seen_at.get(sig)
            age_ok = True
            if min_window.total_seconds() > 0 and first_ts:
                try:
                    first_dt = datetime.fromisoformat(first_ts)
                    age_ok = (now - first_dt) >= min_window
                except (ValueError, TypeError):
                    age_ok = True  # fall safe - escalate rather than silently swallow
            if age_ok:
                old_sev = f.severity
                f.severity = _bump_severity(f.severity)
                f.body = (
                    f.body
                    + f"\n\n_Severity escalated from {old_sev.value.upper()} to "
                    f"{f.severity.value.upper()} because this finding has been "
                    f"surfaced in {persistent} separate scans without resolution "
                    f"over a window of more than {escalate_min_hours_since_first}h._"
                )

        # ── Rebound state (formal enum value as of 2026-06-05) ─────
        if sig in resolved_at:
            f.status = FindingStatus.REBOUNDED
            # Keep the title prefix too - existing UI label code reads
            # the title string and we don't want to break it before the
            # frontend picks up the new status enum.
            if not f.title.startswith("(rebounded)"):
                f.title = "(rebounded) " + f.title
            # Stash the prior resolve timestamp under a known key so the
            # frontend can render it as a chip instead of body-text-only.
            f.evidence["previously_resolved_at"] = resolved_at[sig]
            f.body = (
                f.body
                + f"\n\n_This finding had been **resolved previously** "
                f"(last on {resolved_at[sig]}) and has re-appeared. "
                f"Likely a regression - review whether the original fix "
                f"was reverted or a teammate re-introduced the pattern._"
            )

        enriched.append(f)

    return enriched
