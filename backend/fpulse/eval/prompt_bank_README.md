# F-Pulse Copilot Prompt Bank

300 real-world user prompts deduplicated from three external collections
(Oracle/ERP, SQL Server, REST API, file processing, transformation,
scheduling, monitoring, debugging, optimization, governance, etc.)
and tagged with the routing + coverage metadata needed to drive an
eval-harness coverage matrix.

**File:** `prompt_bank.csv`
**Rows:** 300 (`p001`–`p300`)
**Format:** RFC-4180 CSV, UTF-8, one prompt per row, quoted fields.

---

## Schema

| Column | Values | Notes |
|---|---|---|
| `id` | `p001` … `p300` | Stable handle. Reference these in eval reports + bug tickets. |
| `prompt` | The literal user phrasing | Goes verbatim into the agent endpoint as `user_intent`. |
| `category` | one of 18 (see below) | Used for category-level pass-rate gates. |
| `persona` | `developer` / `data_engineer` / `business_user` / `admin` / `oncall` / `devops` | Who would actually type this. Useful for prioritising fixes. |
| `expected_route` | `fast_lane` / `hybrid` / `clarify_first` / `rule_planner` / `agent_loop` / `direct_action` | What dispatch path *should* fire. |
| `expected_tool` | comma-separated tool name(s) or `(none)` | Primary tool the route should invoke. `(none)` = LLM-only or not-supported. |
| `risk_level` | `read` / `draft` / `safe_write` / `high_impact_write` / `explain` | Drives confirmation-card behaviour. |
| `approval_required` | `Y` / `N` | Whether the user should see a ConfirmationCard before the action commits. |
| `works_today` | `Y` / `Partial` / `N` / `Aspirational` | **Honest current state** (post-May 17 2026 sweeps). |
| `notes` | one-line explanation | Brief rationale or gap description. |

### 18 categories

1. `pipeline_creation` — "Create / build a pipeline that…"
2. `scheduling` — cron, intervals, triggers, dependencies
3. `api_ingestion` — REST, SOAP, OAuth, pagination, webhooks
4. `file_ingestion` — CSV/Excel/XML/ZIP/JSON file handling
5. `transformation_cleaning` — dedup, trim, cast, normalize, pivot
6. `sql_server_loading` — upsert, merge, schema, indexes, partitioning
7. `validation_dq` — row counts, schema diff, quality rules, reconciliation
8. `debugging` — "why did X fail", error explanation, root cause
9. `monitoring_ops` — "show failures", "longest runtime", "find inactive"
10. `optimization` — performance tuning, partitioning recommendations
11. `assistant_builder` — clarifying-questions / draft / approve UX
12. `governance_approvals` — DEV/PROD, secrets, audit, approval gates
13. `lineage_impact` — "what depends on", "downstream effect"
14. `security_pii` — masking, encryption, PII detection
15. `devops_deployment` — version control, deploy, rollback, export
16. `ai_assisted_generation` — "build from docs", "infer schema", "auto-fix"
17. `business_user_natural` — non-technical phrasing of operational needs
18. `explanation_sql` — SQL generation, explanation, debugging

### Route values explained

| Route | Means | Today's implementation |
|---|---|---|
| `fast_lane` | Deterministic regex → tool, no LLM | `backend/fpulse/ai/fast_router.py` (~50 intents, ~1s) |
| `hybrid` | 1 tool + 1 LLM format pass | `backend/fpulse/ai/hybrid.py` (4–9s on Ollama) |
| `clarify_first` | Should ask questions before drafting | **Not built today** — biggest UX gap |
| `rule_planner` | Deterministic IR generation from intent | `backend/fpulse/planner/rule_planner.py` |
| `agent_loop` | Full tool-use loop with LLM | `backend/fpulse/ai/agent.py` (30–180s on Ollama) |
| `direct_action` | Deterministic dispatcher (run/cancel/test) | `backend/fpulse/ai/direct_actions.py` |

### `works_today` values explained

| Value | Means |
|---|---|
| `Y` | F-Pulse OSS handles this prompt cleanly today via the listed route + tool. |
| `Partial` | The underlying capability exists but UX is incomplete (e.g., needs a template, recipe, atlas topic, or clarifying-questions flow). |
| `N` | Capability does not exist; would need real feature work to land. |
| `Aspirational` | Genuinely research-grade; do NOT promise this in OSS docs (e.g., "self-healing pipelines", "predict failures before execution"). |

---

## Distribution (as tagged)

### By category

| Category | Count |
|---|---:|
| transformation_cleaning | 34 |
| api_ingestion | 25 |
| pipeline_creation | 25 |
| scheduling | 20 |
| file_ingestion | 20 |
| sql_server_loading | 20 |
| debugging | 20 |
| validation_dq | 19 |
| monitoring_ops | 18 |
| optimization | 14 |
| assistant_builder | 13 |
| ai_assisted_generation | 13 |
| governance_approvals | 12 |
| business_user_natural | 12 |
| security_pii | 10 |
| devops_deployment | 10 |
| lineage_impact | 8 |
| explanation_sql | 7 |
| **Total** | **300** |

### By `works_today` (approximate, from tagging)

| Bucket | Count | % |
|---|---:|---:|
| `Y` (works cleanly today) | ~100 | ~33% |
| `Partial` (capability exists, UX incomplete) | ~135 | ~45% |
| `N` (feature missing) | ~55 | ~18% |
| `Aspirational` (defer / don't promise) | ~10 | ~3% |

Headline: **~78% of these prompts are either solved or solvable with focused UX work today.** That's the leverage zone for phase 2.

### By expected_route

| Route | Count | Notes |
|---|---:|---|
| `fast_lane` | ~145 | Single-tool deterministic answers — Copilot's strongest surface |
| `clarify_first` | ~70 | The biggest UX gap — flow doesn't exist yet |
| `agent_loop` | ~50 | Complex / LLM-driven reasoning |
| `rule_planner` | ~25 | Direct pipeline scaffolding |
| `hybrid` | ~8 | 1-tool + 1-LLM format pass |
| `direct_action` | ~2 | Run/cancel/test dispatchers |

---

## How to use

### Phase A (next, ~1 week) — wire into the eval harness

1. Add a new category to `backend/fpulse/eval/cases.py`: `realtime_intent_routing`
2. Loader reads `prompt_bank.csv` → produces `EvalCase` objects keyed by `id`.
3. New judge `_judge_realtime_routing` compares observed route + tool against `expected_route` + `expected_tool`. Score = 1.0 when both match; 0.5 when route matches but tool diverges; 0.0 when prompt fell through to LLM (route = `agent_loop`) when `clarify_first` or `fast_lane` was expected.
4. Wire into `python -m fpulse.eval.run` and CI.
5. Establish baseline: "Copilot currently routes N/300 prompts correctly."

### Phase 2 (~2–3 weeks) — fix the highest-leverage gaps

Use the coverage report to prioritise. The four likely biggest wins (already visible in this dataset):

1. **Clarifying-questions flow** — unblocks ~70 prompts tagged `clarify_first`.
2. **Oracle BIP + SQL Server upsert templates** — moves ~15 `Partial` prompts to `Y`.
3. **5–7 new fast-lane intents** for debug/ops phrasings (most prompts in `debugging` and `monitoring_ops` that are tagged `Partial`).
4. **LLM-driven failure summariser** — replaces raw-log dumps with user-language explanations.

### Maintenance

- When you add a new fast-lane intent or planner template, **add the matching prompts here first** with `works_today=Y`.
- When a user reports a misroute, add the exact phrasing as a new row with the correct `expected_route` and `works_today=N` — fix in code, then flip to `Y`.
- Keep the file under ~500 rows; over that point, split by category for review-ability.

---

## Provenance

Compiled May 17 2026 from three independent external prompt collections,
deduplicated and re-tagged against actual F-Pulse OSS code state as of
the same date. Original raw lists captured in session notes — see
session memory `session-2026-05-17-*.md`.

Honest accuracy disclaimer: `works_today` reflects post-sweep state
(after the AI-Hub → Insights rename, the Help → Nodes catalog
expansion, the sendChat reorder for create/modify, etc.). Backend
restart may still be required for some recent changes to be live —
the source code state is what's reflected here, not necessarily the
running backend.
