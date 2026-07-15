"""Declarative source-profile dataclasses.

A `SourceProfile` describes how a slow / nested / rate-limited API
behaves so the Extraction Engine can drive it without hard-coded
per-vendor logic. The same profile shape works for:

  - per-resource fanout APIs (slow on-prem inventory products)
  - deeply nested IT-asset APIs (graph / falcon / freshservice / netskope)
  - delta-syncing endpoints with continuation tokens
  - flat REST endpoints with offset/limit pagination

Adding a new connector becomes a ~30-line profile, not bespoke code.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ── Auth ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AuthProfile:
    """How requests authenticate to the source.

    The OAuth2 refresh dance lives in `fpulse.connections.oauth_session`;
    this profile only declares *which* flow applies and where the
    refresh endpoint is. Secrets always come from the credential
    resolver (Vault > config), never the profile itself.
    """
    type: str  # "none" | "basic" | "bearer" | "api_token" | "oauth2" | "iam" | "service_account"
    header: str = "Authorization"
    prefix: str = "Bearer "        # for bearer-style tokens
    refresh_uri: str | None = None  # OAuth2 token endpoint
    scopes: list[str] = field(default_factory=list)
    api_key_param: str = ""        # query-param key for api_token mode


# ── Throughput shape ────────────────────────────────────────────────

@dataclass(frozen=True)
class RateLimitProfile:
    """Sustained RPS budget and burst allowance.

    `respect_header` names a response header (e.g. "Retry-After",
    "X-RateLimit-Reset") that the engine should obey when present —
    overriding the static rps for the duration the server requests.
    """
    rps: float = 10.0
    burst: int = 20
    respect_header: str | None = None


@dataclass(frozen=True)
class ConcurrencyProfile:
    """How many in-flight requests at once.

    Modes:
      "fixed"  — hold concurrency constant at `initial`
      "aimd"   — additive-increase, multiplicative-decrease (TCP-style),
                 ramps from `initial` to `max`, halves on rate-limit
                 signal, never below `min`
      "serial" — strict serial execution (some on-prem APIs require it)
    """
    mode: str = "aimd"   # "fixed" | "aimd" | "serial"
    initial: int = 4
    min: int = 1
    max: int = 12
    success_window: int = 50  # successes before AIMD bumps +1


# ── Pagination + envelope ───────────────────────────────────────────

@dataclass(frozen=True)
class PaginationProfile:
    """How to walk a list endpoint until exhausted.

    `items_path` is the JSON path inside each response that holds the
    array of records — e.g. ["value"] for Microsoft Graph,
    ["resources"] for CrowdStrike, ["data", "items"] for nested
    envelopes. Empty list ([]) means the response IS the array.

    Modes:
      "none"        — single GET, no pagination
      "cursor"      — opaque cursor in body (HubSpot, Slack)
      "offset"      — offset/limit query params (Jira, ServiceNow)
      "link_header" — RFC 5988 Link header (GitHub, Atlassian)
      "page_token"  — opaque pageToken (Google APIs)
    """
    mode: str
    items_path: list[str] = field(default_factory=list)

    # cursor mode
    cursor_path: list[str] | None = None
    cursor_param: str = "after"

    # offset mode
    page_size: int = 100
    offset_param: str = "offset"
    limit_param: str = "limit"
    has_more_path: list[str] | None = None  # explicit stop flag if present

    # page_token mode
    token_path: list[str] | None = None
    token_param: str = "pageToken"


# ── Two-phase fetch (list → enrich) ─────────────────────────────────

@dataclass(frozen=True)
class EnrichmentProfile:
    """List-then-enrich shape: phase 1 returns IDs, phase 2 hits a
    per-resource (or batched) detail endpoint for each.

    `batch_size=1` means per-resource fanout (slow on-prem inventory).
    `batch_size>1` means the detail endpoint accepts a batch of IDs
    in a single request — Falcon's `/devices/entities/devices/v2`
    pattern. The engine chunks IDs and submits batches concurrently.
    """
    list_url: str
    list_id_field: str          # JSON path inside list response (dotted)
    fetch_url: str              # template containing "{id}" or "{ids}"
    batch_size: int = 1
    batch_param: str = "ids"    # query param when batched


# ── Schema mapping (deep JSON → flat columns) ───────────────────────

@dataclass(frozen=True)
class SchemaProfile:
    """Field projection: declarative map of output column → JSON path.

    Path syntax (handled by SchemaMapper):
      "a.b.c"           — nested object access
      "a[0].b"          — first array element
      "a[*].b"          — wildcard, returns list
      "a.b|default=null" — fallback when path is missing

    `coercions` maps an output column to a type name:
      "int" | "float" | "bool" | "str" | "iso_datetime"
    Missing entries leave the value as-is.
    """
    field_paths: dict[str, str]
    coercions: dict[str, str] = field(default_factory=dict)


# ── Checkpointing / resumability ────────────────────────────────────

@dataclass(frozen=True)
class CheckpointProfile:
    """What counts as a 'completed unit' for resume support.

    Modes:
      "per_record"   — track each completed resource ID; resume skips them
      "per_page"     — track completed pagination cursors; resume from
                       the last cursor that succeeded
      "per_batch"    — track completed batch boundaries
      "delta_token"  — store the last delta token; next run uses it
    """
    unit: str = "per_record"
    id_field: str = "id"           # for per_record mode
    delta_field: str | None = None  # for delta_token mode


# ── The composite ──────────────────────────────────────────────────

@dataclass(frozen=True)
class SourceProfile:
    """Complete declarative descriptor of a slow / nested / rate-limited
    source. Read by the Extraction Engine.

    Required: name, auth, pagination, schema.
    Optional: enrichment (only for two-phase APIs), checkpoint
    (defaults to per-record), rate_limit + concurrency (sensible
    defaults if omitted).
    """
    name: str
    auth: AuthProfile
    pagination: PaginationProfile
    schema: SchemaProfile

    # Performance hints — drive engine defaults and scheduler behavior.
    latency_class: str = "fast"        # "fast" | "slow" | "very_slow"
    expected_volume: str = "small"      # "small" | "medium" | "large" | "huge"
    freshness_interval_seconds: int | None = None  # min seconds between runs

    rate_limit: RateLimitProfile = field(default_factory=RateLimitProfile)
    concurrency: ConcurrencyProfile = field(default_factory=ConcurrencyProfile)

    enrichment: EnrichmentProfile | None = None
    checkpoint: CheckpointProfile = field(default_factory=CheckpointProfile)

    # Observability hooks — the engine emits per-stage events tagged
    # with these so the operator UI groups everything for one source.
    category: str = ""        # "it_asset" | "saas" | "on_prem" | etc.
    notes: str = ""

    def __post_init__(self) -> None:
        # Validate enums up front so misconfigured profiles fail at
        # registration, not deep inside a 6-hour run.
        if self.latency_class not in ("fast", "slow", "very_slow"):
            raise ValueError(f"latency_class must be fast|slow|very_slow, got {self.latency_class!r}")
        if self.expected_volume not in ("small", "medium", "large", "huge"):
            raise ValueError(f"expected_volume invalid: {self.expected_volume!r}")
        if self.concurrency.mode not in ("fixed", "aimd", "serial"):
            raise ValueError(f"concurrency.mode invalid: {self.concurrency.mode!r}")
        if self.pagination.mode not in ("none", "cursor", "offset", "link_header", "page_token"):
            raise ValueError(f"pagination.mode invalid: {self.pagination.mode!r}")
        if self.checkpoint.unit not in ("per_record", "per_page", "per_batch", "delta_token"):
            raise ValueError(f"checkpoint.unit invalid: {self.checkpoint.unit!r}")
        if self.auth.type not in ("none", "basic", "bearer", "api_token", "oauth2", "iam", "service_account"):
            raise ValueError(f"auth.type invalid: {self.auth.type!r}")
        if self.checkpoint.unit == "delta_token" and not self.checkpoint.delta_field:
            raise ValueError("checkpoint.unit=delta_token requires checkpoint.delta_field")
