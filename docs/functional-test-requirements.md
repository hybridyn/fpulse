# F-Pulse OSS Functional Acceptance

Scope: Apache-2.0 OSS, local single-user development. F-Pulse+ certification
is separate. Updated 2026-09-17. Overall status: NOT COMPLETE.

Passing automated tests are evidence for their assertions, not proof that an
entire feature, connector, operating system, or workload is certified. A skipped
test, collection error, absent report, or zero-test run cannot close a requirement.
Mocked connector results cannot close live connector requirements.

## Evidence From This Validation

Reports are local artifacts in `.tmp-validation-logs/` and
`frontend/.test-results/`; overlapping runs must not be added together.

| Run | Result | Evidence |
| --- | --- | --- |
| Backend broad regression, after fixture repair | 3,212 passed; 12 skipped; 3 collection errors | `functional-backend-current.xml` |
| Full API and pipeline E2E | 160 passed | `functional-e2e-current.xml` |
| Persistence/versioning, anonymous access, project exports, strict execution counts | 56 passed | `functional-lifecycle-final.xml` |
| Frontend unit/component tests | 113 passed across 12 files | `functional-vitest.xml` |
| Frontend TypeScript and production build | Passed | `npm run build` |
| PostgreSQL logical replication and schema change | 2 passed against PostgreSQL 16.4 in Docker | `functional-cdc.xml` |
| Bounded stress plus crash-suspect regression files | 64 passed (23 stress, 41 auth/RCA/fanout); 1 long memory test deselected | `functional-stress-bounded.xml` |
| Packaging preflight | Failed: four missing package-data declarations | `python tools/package_preflight.py` |

The earlier broad run had worker crashes. The application arms a process-exit
timer on lifespan shutdown; test configuration now disables it using the existing
`FPULSE_SHUTDOWN_GRACE_S=0` switch. The subsequent broad run had no worker crashes.
This change does not alter production shutdown behavior.

The broad run's 12 skips comprise six unfinished architecture checks, three
Linux/POSIX-only checks on Windows, one missing optional NATS dependency, and two
PostgreSQL CDC checks. The two CDC checks subsequently passed in the separate live
run. The other skips remain unresolved or require their applicable environment.

The unbounded stress attempt was interrupted during
`TestMemoryUsage.test_workflow_store_memory`: after roughly nine minutes inside
that check it had created about 2,700 of the required 5,000 workflows. No final
JUnit report was produced, so that attempt supplies no completed stress-gate
evidence. A separate bounded run excludes exactly that test. This is a traced
memory workload, not a production throughput benchmark; dedicated completion
and profiling remain required.

Docker Desktop was started for this validation. The PostgreSQL container used a
unique name, a loopback-only port (55433), and no shared data volume. It was removed
after the CDC tests; the user's existing containers were not modified.

## Immediate Release Blockers

| ID | Priority | Required Closure |
| --- | --- | --- |
| FT-B01 | P0 | Implement or reconcile the existing record/replay tests: `tools.test_connector.ConnectorCassette` and `install_cassette` are absent. Test redaction, deterministic matching, exhaustion, pagination, and no network fallback. |
| FT-B02 | P0 | Supply the release-checksum implementation expected by `test_release_checksums.py`; verify hashes and signatures, missing-key failure, and tampering. Do not remove the tests merely to make collection green. |
| FT-B03 | P0 | Supply the writable-data-directory diagnostic expected by `test_startup_diagnostics.py`; prove startup fails clearly for a read-only directory. |
| FT-B04 | P0 | Include frontend assets, connector manifests, static files, and packaged docs in the wheel. Inspect an actual built wheel and launch it in a clean environment; preflight alone is insufficient. |
| FT-B05 | P1 | Resolve every skipped architecture/security check or explicitly assign it to F-Pulse+ with an approved rationale. Several current skips are unfinished checks, not unavailable services. |
| FT-B06 | P1 | Complete the 5,000-workflow traced-memory test in a dedicated run and investigate its long runtime. Do not count the interrupted attempt or a deselected case as passed. |

## Acceptance Matrix

Each numbered item below is independently signable. Status is partial/open unless
an exact case and expected result have execution evidence. Existing test filenames
are starting points for traceability, not a claim of exhaustive coverage.

| Area | Required Functional Cases | Existing Evidence / Remaining Work |
| --- | --- | --- |
| FT-01 Installation | 01.1 Fresh supported-Python install; 01.2 wheel contains all runtime assets; 01.3 CLI launch from a path with spaces and Unicode; 01.4 port-conflict recovery; 01.5 upgrade preserves pipelines, credentials, and versions; 01.6 uninstall does not silently delete user data | Package preflight fails. Clean Windows install and Linux/macOS release jobs still required. `test_launcher.py`, `test_cli_runtime_state.py`, `test_package_preflight.py`. |
| FT-02 First Run | 02.1 fresh workspace bootstrap; 02.2 sample pipeline executes; 02.3 permission/disk failure is actionable; 02.4 doctor distinguishes required/optional dependencies; 02.5 offline startup and restart preserve configuration | Startup diagnostic collection blocker. API bootstrap exercised; offline and clean-install checks open. |
| FT-03 Identity | 03.1 login/logout; 03.2 expired/revoked session rejected; 03.3 password change invalidates expected sessions; 03.4 anonymous API denied; 03.5 secrets absent from API errors and exports; 03.6 local-host/origin guard enforced | Auth and anonymous-access suites exercised. Browser refresh/session recovery and full secret-surface audit remain open. |
| FT-04 Projects | 04.1 create/rename/delete; 04.2 folder and pipeline movement; 04.3 export/import preserves references; 04.4 duplicate names and invalid IDs handled; 04.5 deletion cannot affect another project/workspace | Store, API, and export checks exercised. Destructive UI journey requires disposable browser workspace. |
| FT-05 Lifecycle | 05.1 create/save/reopen; 05.2 parameter/edge persistence; 05.3 historical versions immutable; 05.4 version diff correct; 05.5 publishing rules enforced; 05.6 preview cannot create a version or mutate a published pipeline; 05.7 concurrent stale save behavior explicit | Repaired persistence/versioning tests run by default. Browser-observed version increment needs a controlled reproduction; do not label it confirmed autosave defect yet. |
| FT-06 Canvas | 06.1 add/connect/delete/duplicate; 06.2 undo/redo restores exact graph; 06.3 cycle/arity/invalid port rejection; 06.4 zoom/fit/search; 06.5 keyboard/focus behavior; 06.6 large graph navigation without lost state | Frontend validation tests pass. Full browser interaction matrix not completed. |
| FT-07 Configuration | 07.1 required/default fields; 07.2 saved values survive reopen; 07.3 schema-aware selectors; 07.4 dependent-field changes clear invalid values; 07.5 parameters resolve by documented precedence; 07.6 malformed expressions give local errors | DynamicConfig and schema tests pass. Per-node form roundtrips remain required. |
| FT-08 Transforms | 08.1 every registered executable node has a golden result; 08.2 empty/single/null/duplicate inputs; 08.3 Unicode/decimal/date/timezone/nested types; 08.4 joins, aggregate, pivot, window, sort, union; 08.5 schema drift; 08.6 invalid input does not silently lose rows | Node conformance/audit suites exist. Inventory-to-golden-test coverage must be enumerated; do not infer coverage from component counts. |
| FT-09 SQL | 09.1 upstream aliases resolve; 09.2 quoted identifiers and reserved words; 09.3 SQL errors mapped to the node; 09.4 preview row limits; 09.5 accurate decimal/date/null values; 09.6 supported compiled SQL export is reproducible, or explicitly unsupported | SQL preview tests and prior local UI preview evidence. Whole-pipeline export was not verified. |
| FT-10 Run Modes | 10.1 single-node preview; 10.2 sample run; 10.3 complete local run; 10.4 dry-run cannot write destinations; 10.5 preview cannot mutate schedules or published state; 10.6 cancellation stops work and leaves a truthful final status | API E2E and ephemeral execution tests exercised. Verify external no-write guarantees with a sentinel destination. |
| FT-11 Connectors | 11.1 authenticate and reject bad credentials; 11.2 discover schema; 11.3 exact read results; 11.4 pagination without loss/duplication; 11.5 incremental checkpoints/resume; 11.6 rate limits/retries/timeouts; 11.7 type fidelity; 11.8 unsupported operations clearly disabled | PostgreSQL CDC live checks passed. Every advertised connector/operation needs its own sandbox evidence. Live allow-list is empty; local mocks are not certification. |
| FT-12 Destinations | 12.1 append; 12.2 overwrite; 12.3 upsert/key collisions; 12.4 schema mismatch; 12.5 transactional/partial failure; 12.6 retry idempotency; 12.7 counts and checksums match source expectations | Local sink and mocked driver tests exist. Live database/cloud destination matrix remains open. |
| FT-13 Scheduling | 13.1 enable/disable/create/edit; 13.2 next-run calculation; 13.3 timezone/DST boundaries; 13.4 overlap policy; 13.5 restart/misfire behavior; 13.6 backfill bounds/checkpoints | Scheduler/backfill tests exercised. Long-running restart and wall-clock UI journeys need dedicated evidence. |
| FT-14 Execution Pool | 14.1 queue priority; 14.2 concurrency limit; 14.3 cancellation; 14.4 retry policy; 14.5 worker crash recovery; 14.6 timeout/resource limit; 14.7 terminal state cannot revert to running | Existing pool/runner tests plus stress run. Physical memory and long-running process load need measured acceptance thresholds. |
| FT-15 Run Records | 15.1 exact workflow version linked; 15.2 per-step status/count/duration; 15.3 redacted logs; 15.4 input/output capture; 15.5 replay uses retained snapshot; 15.6 missing/expired capture distinguished from execution failure | Execution store and E2E tests exercised. Previously observed missing pivot capture remains a reproduction item. |
| FT-16 Storage | 16.1 upload supported formats; 16.2 reject corrupt/oversized/unsafe paths; 16.3 preview; 16.4 prep recipe persists; 16.5 promotion produces correct managed table; 16.6 rename/trash/restore; 16.7 usage agrees with disk | Storage usage and API tests exercised; full UI roundtrip and disk-full recovery open. |
| FT-17 Quality/Lineage | 17.1 assertions pass/fail correctly; 17.2 row/schema expectations; 17.3 upstream/downstream graph; 17.4 runtime edges linked to run; 17.5 deep links select correct entity; 17.6 stale/deleted lineage handled | Lineage and quality suites exercised. Managed-table producer deep link previously rendered empty; controlled regression required. |
| FT-18 AI/Steward | 18.1 no-provider behavior; 18.2 local model setup; 18.3 provider failure/timeout; 18.4 generated graph validated before save; 18.5 healthy controls do not alert; 18.6 injected faults generate actionable findings; 18.7 tool actions honor permissions; 18.8 UI provider status agrees across pages | Deterministic tests exercised. Live local/cloud provider tests and the previously observed Trust/provider discrepancy remain open. No paid requests made. |
| FT-19 Alerts/Reports | 19.1 rule lifecycle; 19.2 exact triggering condition; 19.3 dedup/cooldown; 19.4 failed-delivery behavior; 19.5 report filters and exports match run data; 19.6 notification links resolve | API checks exercised. Use a local mail catcher/webhook receiver before any delivery certification. No real recipients contacted. |
| FT-20 Backup/Restore | 20.1 create/list backup; 20.2 corrupt archive rejected; 20.3 restore into a different empty directory; 20.4 workflows/versions/files/settings recovered; 20.5 credential encryption keys recovered according to policy; 20.6 restored pipeline executes correctly | Backup suite exists and API paths exercised. Independent restore-and-run drill is required before sign-off. |
| FT-21 UI Compatibility | 21.1 all advertised routes usable; 21.2 Chrome/Edge/Firefox supported-version matrix; 21.3 wide/narrow layouts; 21.4 keyboard and accessible names; 21.5 no hidden controls/overlap; 21.6 loading/empty/error recovery; 21.7 reload preserves saved state | Prior in-app walkthrough and 113 component tests are partial evidence only. Full browser suite and screenshots not executed in this validation. |
| FT-22 Scale | 22.1 deterministic dataset and seed; 22.2 exact results at each size; 22.3 wall time and peak memory; 22.4 concurrent workloads; 22.5 restart/cancel under load; 22.6 repeatable machine/runtime details; 22.7 published limits match evidence | Local stress suite and benchmark tooling do not prove TB/billion-row readiness. Larger dataset and soak runs required. |

## Per-Connector Sign-Off Record

Create one record per advertised connector and operation, not just per connector
name: connector ID/version, operation, service version, fixture seed, expected
schema/count/checksum, actual values, test ID, execution timestamp, report path,
status, and reason for blocked/unsupported cases. Use disposable test accounts.
Never put credentials or customer data into committed fixtures or reports.

## F-Pulse+ Separate Gate

Enterprise-only requirements are not satisfied by OSS passes: SSO/SCIM, enterprise
role policies, multi-user/multi-tenant isolation, approvals/deployment separation,
distributed workers, HA/failover, shared durable event transport, centralized
secrets/audit retention, production upgrades, and contractual recovery objectives.
Reuse applicable OSS cases, but execute enterprise-specific cases against the
F-Pulse+ build and deployment topology. Do not remove OSS security checks merely
because an enterprise feature has a similar name.

## Definition of Complete

Every in-scope case has passing evidence for the release revision and environment;
all P0/P1 defects are closed and retested; intentional unsupported cases are
documented in the product; no unexplained skips, collection errors, or missing
reports remain. Cross-platform, browser, live-service, and recovery gates must
have their own evidence. The current checkout does not meet this definition.

## Reproduce Locally

From the repository root in PowerShell, using the existing test environment:

```powershell
$python = (Resolve-Path .venv-ci/Scripts/python.exe).Path
$run = Join-Path (Get-Location) ('.codex-validation-data/functional-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $run | Out-Null
$env:FPULSE_DATA_DIR = Join-Path $run 'workspace'
$env:FPULSE_MODE = 'dev'
$env:TEMP = $run
$env:TMP = $run

& $python -m pytest -c backend/pytest.ini backend/tests -o addopts= -p no:rerunfailures -n 2 --continue-on-collection-errors -m 'not stress and not external and not e2e' --basetemp="$run/unit-temp" --junitxml="$run/backend.xml" -q -rs
& $python -m pytest -c backend/pytest.ini backend/tests/test_e2e_complete.py backend/tests/test_e2e_pipeline.py -o addopts= -p no:rerunfailures --basetemp="$run/api-temp" --junitxml="$run/api.xml" -q -rs
& $python tools/package_preflight.py
npm.cmd --prefix frontend test -- --reporter=default
npm.cmd --prefix frontend run build
```

Check each command's exit code, not merely the last command in the block. The
backend is currently expected to report the three missing-implementation errors.
Use a disposable shell so the test environment variables do not affect normal
application launches. The explicitly separate long stress run is:

```powershell
& $python -m pytest -c backend/pytest.ini backend/tests/test_load_stress.py -o addopts= -p no:rerunfailures --basetemp="$run/stress-temp" --junitxml="$run/stress.xml" -q -rs
```

For live PostgreSQL CDC, point `FPULSE_TEST_PG_*` only at a disposable database:
the fixture creates and drops its own `users` table and replication publication.
For browser mutation tests, use a separate disposable application instance, not
the user's active localhost workspace. A successful process exit without a final
report is not sufficient evidence; the initial stress attempt demonstrated why.
