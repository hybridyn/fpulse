# F-Pulse Performance Targets

Concrete numbers customers and ops teams can rely on. Each target is either currently met (with evidence) or roadmap'd (with target date).

---

## 1. Memory footprint

| Component | Steady-state | How measured |
|---|---|---|
| F-Pulse OSS process | ≤ 100 MB | Verified ~89 MB Apr 19, 2026 |
| F-Pulse OSS + AI layer (idle) | ≤ 150 MB | Tier A target; CI assertion Tier B |
| F-Pulse+ process | ≤ 150 MB | |
| Per agent run (transient peak) | ≤ 200 KB | Step 1 context-discipline target |
| Per agent run (worst case before truncation) | ≤ 1 MB | Hard cap via context budget manager |
| 100 concurrent agent runs (transient total) | ≤ 30 MB | Aggregate cap |
| Per worker process | ≤ 500 MB | Auto-recycle threshold (Tier B) |

**On-prem Ollama path** is GPU-bounded, not RAM-bounded:
- KV cache: ~2.5 MB VRAM per token
- 8K context window: ~20 GB VRAM per active session
- Hardware spec for on-prem F-Pulse+ customers: NVIDIA A100 40GB minimum for 1-2 concurrent sessions; A100 80GB for ~3-4

---

## 2. Latency

| Path | p50 | p95 | Notes |
|---|---|---|---|
| API request (non-AI) | < 50 ms | < 200 ms | Existing |
| Agent response (read-only tools) | < 1 s | < 3 s | LLM-bound; depends on provider |
| Agent response (write tool with confirmation) | < 2 s | < 5 s | Includes confirmation card render |
| Connection test | < 1 s | < 5 s | Existing; bounded by external service |
| Pipeline preview run on cached sample | < 500 ms | < 2 s | DuckDB in-process |
| Dashboard load | < 300 ms | < 1 s | Existing |
| Pre-publish card render (with all 7 sections) | < 1 s | < 3 s | Step 4b target |

LLM call latency is provider-dependent and not under our control. Our targets are for the F-Pulse-controlled portion of the request.

---

## 3. Concurrency

| Scope | OSS | F-Pulse+ | Overflow behavior |
|---|---|---|---|
| Per user (concurrent agent runs) | 3 | 5 (workspace configurable) | Queue 60s, then reject with retry-after header |
| Per workspace | 50 | 200 (configurable) | Queue 60s, then reject |
| Global (single process) | 500 | 500 | Workers handle overflow via process pool |

Priority lanes: admin / PROD-environment actions skip ahead of DEV / OSS queues.

---

## 4. Worker recycle policy

Long-running workers must recycle to prevent leak accumulation:

- **Memory threshold:** auto-recycle at 500 MB resident
- **Job count threshold:** auto-recycle after 100 jobs (configurable per worker class)
- **Wall-clock threshold:** recycle after 4 hours regardless

Memory governor (PR5, shipped Apr 22, F-Pulse+) provides 70/80/90% memory tiers that gate new work before workers approach OOM.

**Anti-leak patterns enforced:**
- All shared resources init in FastAPI lifespan (Rule 7)
- DB engines disposed on shutdown
- Subprocess pipes always closed; full wait/cleanup
- No infinite-lived caches — TTL + LRU on every cache (Rules 5, 8)

---

## 5. Throughput

| Workload | Target | Notes |
|---|---|---|
| Pipeline executions per hour (single F-Pulse+ instance, mixed workload) | ≥ 500 | Existing |
| Concurrent users supported (proper caching) | 100-1000 | With L2 Redis cache (Tier B) |
| Concurrent users (L1-only, OSS Tier A) | 50-100 | Single-process limit |
| API requests per second (non-AI) | ≥ 200 RPS | Existing |
| Agent runs per second | ≥ 10 RPS | LLM-bound; provider-dependent |

---

## 6. Cache TTL + invalidation

| Cache type | TTL | Invalidation trigger |
|---|---|---|
| Schema introspection | 6h | Connection update |
| Pipeline summary | 1h | Pipeline save |
| Metrics summary | 5min | Run completion |
| Connector metadata | 24h | Connector config change |
| Connection health | 15min | Test re-run |
| LLM response (deterministic prompts) | 30min | Prompt template version bump |

Multi-level cache (Tier B):
- **L1** in-memory per worker, ~5 MB cap, LRU
- **L2** shared Redis (F-Pulse+ only) for cross-worker reuse
- **L3** persisted summaries in SQLite for durable reuse

Strict tenant isolation: cache key prefix is always `{tenant_id}:{cache_type}:{key}`. Cross-tenant key collision is a CI assertion failure (Rule 8).

---

## 7. Token + cost budgets (AI layer)

| Scope | OSS | F-Pulse+ | Action when exceeded |
|---|---|---|---|
| Per request total | 8K tokens | 16K tokens (workspace configurable) | Truncate Tier 3 first, then summarize Tier 2 |
| Per tool output | 2K tokens | 4K tokens | Truncate with `[truncated, N more chars]` marker |
| Per workspace per month | n/a (BYO key) | configurable (`monthly_budget_usd`) | Hard cut-off; agent returns "budget exceeded" |
| Per user per day | n/a | optional admin-set limit | Throttle |
| Max iterations per agent run | 6 | 6 | Hard cap; agent returns partial result |
| Wall-clock per agent run | 30 s | 30 s | Hard cap; outcome classified `timeout` |

Cost visibility (Tier A minimal): inline indicator after every agent response: `~{N} tokens · ~${cost}`. Per-user / per-workspace dashboards in Tier B.

---

## 8. Storage

| Surface | Footprint | Retention |
|---|---|---|
| SQLite database (single workspace, 1 year of activity) | ~500 MB | Per workspace retention policy (default 90 days for time-series tables) |
| Audit log (per workspace, per year) | ~50 MB | Append-only; archived per retention runner |
| Agent execution traces | ~2 MB per 1000 runs | 90 days minimum, configurable per workspace |
| Approval snapshots (SHA-256 hashed pipeline state) | ~10 KB per snapshot | Indefinite (compliance evidence) |

Retention runner (F-Pulse+ Apr 27): two-tier archive (Parquet via DuckDB → JSONL fallback) + batched purge + manual VACUUM. Daily schedule.

---

## 9. Availability targets

| Target | OSS | F-Pulse+ |
|---|---|---|
| Process uptime (assuming healthy host) | best effort | 99.5% (single-instance) |
| HA / clustered deployment | n/a | Roadmap Q4 2026 |
| Graceful degradation when LLM provider unavailable | yes (deterministic fallback) | yes |
| Graceful degradation when license server unavailable | n/a | yes (offline grace period) |

---

## 10. What we measure and expose

Prometheus metrics endpoint (existing, expanded in Step 1.5b for AI):

| Metric | Type | Notes |
|---|---|---|
| `fpulse_http_requests_total` | counter | Per route, status |
| `fpulse_http_request_duration_seconds` | histogram | Per route |
| `fpulse_executions_total` | counter | Per status |
| `fpulse_worker_memory_bytes` | gauge | Per worker (added in Tier B) |
| `fpulse_ai_agent_runs_total` | counter | Per outcome (success/llm_failure/tool_failure/policy_block/timeout/user_rejection) |
| `fpulse_ai_tokens_total` | counter | Per provider, per tier |
| `fpulse_ai_tool_calls_total` | counter | Per tool, per tier, per outcome |
| `fpulse_ai_cache_outcomes_total` | counter | hit / miss / stale |
| `fpulse_ai_redactions_total` | counter | Per category |
| `fpulse_auth_failures_total` | counter | Existing |

Cardinality discipline: no high-cardinality labels (no per-user, no per-pipeline-id labels). Per-tenant aggregates only.

---

## How to verify these claims

- **Memory:** `pytest backend/tests/test_load_stress.py -k memory` (existing harness)
- **Latency:** `pytest backend/tests/test_load_stress.py -k latency` (existing)
- **Architecture invariants:** `pytest backend/tests/architecture/test_invariants.py -v`
- **AI-specific tests** (post Step 1.5b): `pytest backend/tests/test_ai_*.py -v`
- **Prometheus:** scrape `/metrics` endpoint of running instance

If a measurement diverges from these targets, follow the responsible-disclosure policy in the repository's `security.md` file when it's a security-relevant degradation, or open a normal issue otherwise.
