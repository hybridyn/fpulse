# Real-world scenario troubleshooting — 25 symptom-specific Q&A

Each entry names a **specific F-Pulse symptom** and the **specific
tool / page / log / env var** to consult. No generic "check your logs"
answers — every answer is actionable and grounded in the real codebase.

---

## 1. Execution stuck at "queued" for 5+ minutes

**Symptom:** Executions page shows a run with `status = queued` that
hasn't started for over 5 minutes.

**Diagnosis:**
1. Open **Pool page** (sidebar → Pool). Check the **governor banner**
   — it shows "currently running N of M workers." If N = M, the queue
   is full.
2. Check `FPULSE_MAX_CONCURRENT_RUNS` in Settings → General →
   Execution Tuning. If it's set too low (e.g. `1`), raise it.
3. If N < M but the run is still queued, the scheduler may be hung.
   Check backend logs for `_timeout_watchdog_loop` entries — if the
   log shows the loop hasn't fired in >60s, the event loop is blocked.
   Restart the backend.

**Fix:** Raise `FPULSE_MAX_CONCURRENT_RUNS` to match your host's
capacity, or wait for the currently-running pipeline to finish.

---

## 2. Pipeline succeeds in DEV but fails on full data

**Symptom:** Pipeline runs clean in Sample/DEV mode but fails when
run in Live mode with full data.

**Diagnosis:**
1. DEV mode applies `DEV_SAMPLE_ROWS` (default 50) on source nodes.
   Full datasets surface schema issues that 50 rows don't.
2. Executions page → click the failed full run → read `error_message`
   and `failed_step`.
3. Common causes:
   - **Type mismatch**: a column that's all VARCHAR in 50 rows has a
     DOUBLE in row 5,001. Fix: add a TYPECAST node before the sink.
   - **Null in non-nullable column**: the first 50 rows happened to
     have no nulls. Fix: add a Data Quality node with a null check
     upstream.
   - **Memory exceeded**: full dataset blows past DuckDB memory limit.
     Check `peak_memory_mb` vs `FPULSE_DUCKDB_MEMORY_LIMIT`.

---

## 3. Bulk Loader returns "BulkLoaderNotAvailable"

**Symptom:** Error `BulkLoaderNotAvailable: postgresql dialect requires
psycopg2-binary` in the execution logs.

**Diagnosis:** The Postgres bulk loader path (`COPY FROM STDIN`) needs
`psycopg2-binary` or `psycopg` v3 installed. Without it, the
bulk-load runner raises this error and the executor falls back to
row-by-row INSERT.

**Fix:** `pip install psycopg2-binary>=2.9` (or add to
`requirements.txt` for Docker builds). Restart the backend. The Bulk
Loader node automatically uses `COPY FROM STDIN` once the driver is
available.

**Verify:** Run the pipeline → Executions page → click the run → step
detail shows `method: "COPY FROM STDIN"` instead of `INSERT`.

---

## 4. Connector cert score is 0/5

**Symptom:** `GET /api/connectors/cert-matrix` shows depth_score 0 for
a connector.

**Diagnosis:** Score 0 means the manifest is a stub — no schema
declared, no pagination, no incremental field, no primary key, no
fixtures.

**Fix:**
1. Run `python -m fpulse.connectors.certify <connector_id>` — the
   validator lists exactly what's missing.
2. If the manifest is v1 format, migrate first:
   `python -m fpulse.connectors.certify --migrate <connector_id>`
   to scaffold a v2 skeleton.
3. Edit the manifest under
   `backend/fpulse/connectors/manifests/<connector>.v2.json`.
4. Add fixture files under `manifests/<connector>/fixtures/`.
5. Re-run certify until depth-5.

---

## 5. Agent timed out at 300 seconds

**Symptom:** Chat dock shows "Agent timed out after 300s" on local
Ollama.

**Diagnosis:** The wall-clock cap is 300s for local Ollama (120s for
cloud). On CPU, `llama3.1:8b` takes 30-60s per LLM call, and a
tool-using turn is 2-3 calls.

**Fix (immediate):** Use fast-lane phrasings for the same question:
`list pipelines`, `overview`, `what failed today`, `running now` —
sub-1-second, no LLM.

**Fix (structural):** Switch to a model at the tool-use floor —
`qwen2.5:7b` (~6 GB RAM, 30-60 s/turn on CPU), `llama3.1:8b`, or
`phi-4`. Insights → AI Provider → Ollama → change model. Sub-7B
Qwen 2.5 models look faster on paper but fail the tool-use loop
silently.

**Fix (escape hatch):** Raise cap to 600s via
`FPULSE_AGENT_WALL_CLOCK_S=600` (max allowed) and restart.

---

## 6. "Database is locked" warning on restart

**Symptom:** Backend log shows `database is locked` warnings after a
restart.

**Diagnosis:** SQLite WAL mode with a writer that didn't drain
cleanly. The connection-tracking system didn't close all connections
at shutdown.

**Fix:** Usually self-heals on the next start. If persistent: stop
all containers (`docker compose down`), wait 5 seconds, then restart
(`docker compose up`). Never delete the WAL file manually.

---

## 7. Memory exceeded — DuckDB OOM

**Symptom:** Execution fails with `exit_reason=budget_memory` and a
DuckDB out-of-memory error.

**Diagnosis:**
1. Executions page → click the run → check `peak_memory_mb`.
2. Settings → General → Execution Tuning shows
   `FPULSE_DUCKDB_MEMORY_LIMIT` (default: 80% of host RAM).

**Fix:**
1. Raise `FPULSE_DUCKDB_MEMORY_LIMIT` if the host has headroom.
2. Ensure spill directory is on SSD (`FPULSE_DUCKDB_TEMP_DIR`) — HDD
   spill is 10-100x slower but can handle larger-than-memory data.
3. Replace Database Sink with Bulk Loader — bulk paths stream data
   rather than materializing the full result.
4. Add a Filter or Sample node upstream to reduce the working set.

---

## 8. Schedule fires but pipeline doesn't start

**Symptom:** Schedules page shows "Last fired: 5 min ago" but no
matching execution on the Executions page.

**Diagnosis:**
1. **Pipeline status:** Only `published` or `testing` pipelines run on
   schedule. Check the pipeline's status badge in the editor toolbar.
2. **Environment mismatch:** The schedule's environment (DEV/PROD) must
   match an environment the pipeline is active in. In OSS Free, only
   DEV exists.
3. **Worker pool full:** Pool page → Queue depth. If non-zero at fire
   time, the run queued but never got a slot.

**Fix:** Publish the pipeline (`draft` → `testing` → `published`) and
verify the schedule's environment is DEV.

---

## 9. Notification watchdog not firing alerts

**Symptom:** A pipeline exceeded the long-running threshold but no
alert fired.

**Diagnosis:**
1. Settings → Notifications → Pipeline Notifications section → check
   the long-running threshold value (default 30 minutes).
2. The watchdog reads config from `GET /api/notifications/config`. If
   the SettingsPage never saved notification config, the watchdog has
   no thresholds to check. Open Settings → Notifications → **Save**
   (even without changes) to initialize the config.
3. Check that at least one alert rule exists: pipeline → Alerts tab →
   must have an `ON_LONG_RUNNING` rule with a channel configured.

**Fix:** Save notification settings, add an `ON_LONG_RUNNING` alert
rule to the pipeline, verify the channel (Slack/email/webhook) has a
valid destination in Settings → Notifications.

---

## 10. Credentials show as "encrypted" but pipeline can't connect

**Symptom:** Credentials page shows the credential exists, but the
pipeline fails with a connection error.

**Diagnosis:**
1. The credential may have been saved before the May 4 2026 encryption
   fix. Pre-1.0 OSS versions stored credentials in plaintext. The
   migration script re-encrypts them.
2. Run: `python -m fpulse.security.migrate_existing --dry-run` to check
   for unencrypted credentials.
3. If the credential IS encrypted but connection fails: the master key
   file may have changed. Verify `~/.fpulse/secret.key` (or
   `$FPULSE_DATA_DIR/secret.key`) is the same file that encrypted the
   credential.

**Fix:** Run `python -m fpulse.security.migrate_existing` to re-encrypt.
If the key file changed, restore the original key from backup, or
re-enter the credential values in the Credentials page.

---

## 11. Checkpoint resume button missing on failed run

**Symptom:** A pipeline failed mid-run but the Executions page shows
no "Resume from step X" button.

**Diagnosis:** Checkpoints have a 7-day TTL. If the failed run is
older than 7 days, the checkpoint Parquet files under
`data/checkpoints/<run_id>/` have been garbage-collected.

**Fix:** Re-run from start. For future runs, consider shortening the
pipeline or adding Materialize nodes at expensive intermediate steps
to cache results.

---

## 12. SCD2 node produces duplicate surrogate keys

**Symptom:** The SCD Type 2 output has duplicate `scd_id` values.

**Diagnosis:** The surrogate key is deterministic:
`SHA-256(business_key_values + valid_from)`. Duplicates mean two rows
have the same business key AND the same `valid_from` timestamp — this
happens when the pipeline runs twice in the same second.

**Fix:** Use the Bulk Loader in **merge mode** with `primary_key` set
to the surrogate key column. The merge does
`INSERT ... ON CONFLICT DO UPDATE`, making re-runs idempotent.

---

## 13. Token wallet cap reached mid-conversation

**Symptom:** Chat shows "Daily token cap reached" and refuses to
process the prompt.

**Diagnosis:** The per-user daily token cap (`FPULSE_AGENT_DAILY_TOKENS_USER`)
limits total tokens sent to the LLM provider per user per day.

**Check:** `GET /api/ai/agent/budget` returns `percent_used`,
`tokens_used_today`, and `cap`. The chat dock header also shows the
percentage.

**Fix:** Wait for the daily reset (midnight UTC), or raise the cap
via `FPULSE_AGENT_DAILY_TOKENS_USER=2000000` in the backend env.

---

## 14. RAG returns no results for a valid question

**Symptom:** The agent calls `recall_history` but gets zero results
even though relevant execution data exists.

**Diagnosis:**
1. RAG indexer runs daily at 03:00 UTC. If the data was created today,
   it hasn't been indexed yet.
2. Check if RAG is disabled: env var `FPULSE_DISABLE_RAG=1`.
3. The indexer only indexes failures from the last 30 days + all
   pipelines + catalog + docs. Successes older than 30 days are not
   in the index.

**Fix:** Trigger a manual reindex: `POST /api/ai/rag/reindex` (admin
only). Or wait for the 03:00 UTC daily run.

---

## 15. "Secret key file is world-readable" — startup refused

**Symptom:** Backend refuses to start with an error about
`~/.fpulse/secret.key` permissions.

**Diagnosis:** The master encryption key file must be chmod 600 (owner
read/write only). F-Pulse checks at startup and refuses to start if
the file is world-readable — fail-closed security.

**Fix (Linux/macOS):** `chmod 600 ~/.fpulse/secret.key`

**Fix (Docker):** Ensure the volume mount preserves POSIX permissions.
On Windows hosts with Docker Desktop, this sometimes requires
explicit permission settings in the compose file.

---

## 16. Product knowledge returns stale answers

**Symptom:** The Copilot answers based on outdated information after
you edited `docs/product_facts/*.md`.

**Diagnosis:** The product knowledge indexer runs at startup. Edits
to fact files after startup aren't picked up until reindex.

**Fix:** Settings → Security → AI product knowledge → **Reindex now**
button. Or `POST /api/ai/product-knowledge/reindex`. Or restart the
backend.

---

## 17. OpenRouter model shows "no tool support"

**Symptom:** You selected an OpenRouter model but the agent can't use
tools.

**Diagnosis:** Not all OpenRouter models support tool use (function
calling). The OpenRouter model browser in Insights → AI Provider has
a **tools-only filter** — enable it to see only models with tool
support.

**Fix:** Switch to a model that supports tools. Free-tier models with
tool support include certain Llama and Qwen variants — use the
free-tier + tools-only combined filter.

---

## 18. Snowflake bulk load fails with staging error

**Symptom:** Bulk Loader (Snowflake) fails with a PUT/staging error.

**Diagnosis:** The Snowflake dialect uses `PUT` to upload Parquet to
a user stage, then `COPY INTO` from the stage. Failures are usually:
1. Missing `snowflake-connector-python` package.
2. User doesn't have `WRITE` permission on the stage.
3. Staging directory (`data/staging/<run_id>/`) is full or on a
   read-only filesystem.

**Fix:** Install `snowflake-connector-python`, verify the Snowflake
user's stage permissions, and ensure `FPULSE_DUCKDB_TEMP_DIR` (used
for staging) has enough disk space.

---

## 19. Pipeline hangs on API source with pagination

**Symptom:** Pipeline runs indefinitely, pulling from a REST API
source that has pagination.

**Diagnosis:** The SaaS Connector / REST Connector follows pagination
links until the API returns no `next` link. If the API has a bug in
its pagination response (always returns a `next` link), the pipeline
loops forever.

**Check:** Executions page → click the run → Logs tab shows per-page
fetch entries. If the same URL repeats, it's a pagination loop.

**Fix:**
1. Kill the run (Executions page → run row → Stop button).
2. Set a `max_pages` param in the source node config to cap pagination.
3. If using a manifest: check the manifest's `pagination` section for
   a `stop_condition` or `max_pages` field.

---

## 20. Data Quality node silently dropping rows

**Symptom:** Row count drops after the Data Quality node but no errors
appear.

**Diagnosis:** The Data Quality node has two modes:
- **Drop mode** (default): rows failing validation are silently removed.
- **DLQ split mode**: failed rows are routed to a separate dead-letter
  output.

**Check:** Click the Data Quality node → ConfigPanel → check the
**mode** parameter. If it's `drop`, failed rows disappear.

**Fix:** Switch to **DLQ split mode** and connect the DLQ output to a
separate sink (CSV, JSON, or Database Sink) to capture and investigate
failed rows.

---

## 21. Expression `$vars.NAME` returns null

**Symptom:** A node param using `$vars.MY_VAR` renders as null at
runtime.

**Diagnosis:**
1. Variable must be created BEFORE the run. Check sidebar → Variables
   page (or pipeline → Settings → Variables).
2. Variable scope: `global` (workspace-wide) or `pipeline` (single
   pipeline). A pipeline-scoped variable from pipeline A is not visible
   in pipeline B.
3. Variable name is case-sensitive. `$vars.my_var` != `$vars.MY_VAR`.

**Fix:** Verify the variable exists, matches the exact case, and has
the correct scope.

---

## 22. Auto-save not working

**Symptom:** Canvas changes aren't auto-saving to the backend.

**Diagnosis:** Auto-save is controlled by the **Auto-save canvas
changes to the backend** toggle in Settings → General → Editor
Preferences.

**Check:** Settings → General → Editor Preferences → verify the
toggle is ON. Auto-save fires 2 seconds after the last canvas edit.

**Fix:** Enable the toggle and click Save in Settings. The setting is
persisted via `useEditorPreferences` hook using localStorage +
backend `admin_settings`.

---

## 23. Eval harness returns failing cases

**Symptom:** Running `python -m fpulse.eval.run` shows failures.

**Diagnosis:** The eval harness has 18 cases across 6 categories
(planner_intent, sql_helper, agent_tools, agent_safety, sanitization,
gate1_core_etl). Gate 1 cases are code-presence probes — they fail if
SCD2, Data Profile, checkpoint store, or bulk-load runner aren't
importable.

**Check:** Results land in `data/eval/latest.json`. The Trust page
(Insights → Trust subtab) shows the pass rate.

**Fix:** For gate1 failures: verify the modules exist in the backend
(they should — Sprint 1 shipped all four). For AI-quality failures:
check the configured provider can handle tool use. `qwen2.5:7b` (the
2026-05-19 tool-use floor) is the minimum model that passes all 18
cases — sub-7B models fail the tool-use cases silently.

---

## 24. Worker-role guard refusing startup

**Symptom:** Starting with `FPULSE_ROLE=worker` fails immediately
with "worker-only mode is F-Pulse+ only."

**Diagnosis:** OSS Free is single-binary; the multi-worker queue
isn't shipped. Running multiple OSS containers against the same SQLite
would corrupt state. The worker-role guard catches this at startup.

**Fix:** Don't set `FPULSE_ROLE=worker` in OSS Free. Run a single
F-Pulse container. Multi-worker horizontal scaling requires F-Pulse+.

---

## 25. Pipeline version hash mismatch

**Symptom:** The Versions tab shows a content hash that doesn't match
what you expect, or a tamper-detection warning.

**Diagnosis:** Every pipeline save computes a SHA-256 content hash of
the IR. If the hash at run time doesn't match the hash at save time,
something modified the IR outside the normal save path.

**Check:** Pipeline → Versions tab → click two versions to see the
IR diff. Look for unexpected node additions or parameter changes.

**Fix:** If the diff shows unexpected changes, restore the previous
version from the Versions tab. If the hash was corrupted, re-save
the pipeline (which computes a fresh hash).
