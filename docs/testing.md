# F-Pulse v1.0.0 — Testing Documentation

## Overview

F-Pulse has a comprehensive test suite covering every layer of the application.
The test suite is designed to validate the system end-to-end before moving to
production with F-Pulse.

## Test Categories

### 1. Unit Tests (~170 tests)

**Location:** `backend/tests/test_*.py` (excluding API/load/E2E)

Tests every in-memory store, the execution engine, scheduler, validator, and
notification service in isolation.

| File | Component | Tests |
|------|-----------|-------|
| `test_workflow_store.py` | WorkflowStore | CRUD, versioning, diff, status lifecycle |
| `test_project_store.py` | ProjectStore | CRUD, default protection, deep copy |
| `test_schedule_store.py` | ScheduleStore | CRUD, filtering, run recording |
| `test_alert_store.py` | AlertStore | Rules CRUD, logs, trigger counting |
| `test_execution_store.py` | ExecutionStore | Records, stats, period filtering |
| `test_auth_store.py` | UserStore | Users, sessions, password hashing |
| `test_variable_store.py` | VariableStore | CRUD, scoping, resolution, masking |
| `test_credential_store.py` | CredentialStore | CRUD, config masking |
| `test_lifecycle_store.py` | LifecycleStore | Events, isolation |
| `test_validator.py` | IR Validator | Params, connections, cycles |
| `test_schema_contract.py` | SchemaContractStore | Contracts, validation, drift |
| `test_executor.py` | WorkflowExecutor | Execution, topological sort, chaining |
| `test_scheduler.py` | PipelineScheduler | Cron parsing, schedule evaluation |
| `test_notifier.py` | NotificationService | Email/Slack/Teams/Webhook |

### 2. API Integration Tests (~50 tests)

**Location:** `backend/tests/test_api_integration.py`

Tests all 70+ API endpoints via FastAPI `TestClient`. Covers:

- **Health & utility** — `/api/health`, `/api/scheduler/status`, `/api/node-types`, `/api/files`
- **Projects** — CRUD, list pipelines, delete protection
- **Workflows** — CRUD, versioning, diff, validate, test, publish, archive, restore
- **Execution** — full workflow, single step, preview
- **Schedules** — CRUD, toggle, run-now
- **Alerts** — rules CRUD, test alert, logs
- **Monitor** — executions, stats, multi-stats, active schedules, failed pipelines
- **Auth** — login, register, duplicate email, session, me endpoint
- **Variables** — CRUD, resolve
- **Credentials** — CRUD, test, masking
- **Connections** — types, CRUD, test, reports
- **Intelligence** — schema detect, flatten
- **Contracts** — create, validate
- **Planner** — templates, generate
- **Upload** — CSV upload, reject unsupported types

### 3. Load & Stress Tests (~20 tests)

**Location:** `backend/tests/test_load_stress.py`

| Test | Target | Threshold |
|------|--------|-----------|
| 1000 workflow inserts | WorkflowStore | < 5s |
| 100 versions per workflow | Versioning | < 2s |
| 500 project creates | ProjectStore | < 2s |
| 10K execution records | ExecutionStore | < 5s |
| 1000 variable CRUD + resolve | VariableStore | < 2s |
| 10K alert logs | AlertStore | < 5s |
| 1000 schedule evaluations | Scheduler | < 1s |
| 10K cron evaluations | Scheduler | < 2s |
| 10K row CSV pipeline | Executor | < 10s |
| 50K row aggregate pipeline | Executor | < 15s |
| 50 sequential executions | Executor | < 30s |
| 50 concurrent health checks | API | < 10s |
| 20 concurrent project creates | API | < 10s |
| 20 concurrent workflow creates | API | < 10s |
| 5000 workflows memory | Memory | < 100MB |
| 50K executions memory | Memory | < 200MB |
| Empty CSV execution | Edge case | Success |
| Single row CSV | Edge case | Success |
| Unicode data (CJK, Arabic, etc.) | Edge case | Success |
| 100-step workflow validation | Edge case | No errors |

### 4. End-to-End Tests (~25 tests)

**Location:** `backend/tests/test_e2e_pipeline.py`

**Full Lifecycle (20 steps):**
1. Create project ("Sales Analytics")
2. Create pipeline (CSV→Filter→Aggregate, 3 nodes)
3. Validate pipeline (structural check)
4. Execute pipeline (verify row counts per step)
5. Execute single step (preview mode)
6. Check monitor/execution records
7. Create schedule (daily 06:00 UTC)
8. Create alert rule (on_failure, email)
9. Create variable (project-scoped)
10. Create credential (verify password masking)
11. Update pipeline (add sort step, v2)
12. Diff versions (verify added steps)
13. Test alert notification
14. Archive pipeline
15. Restore pipeline
16. Verify health endpoint
17. Verify execution stats
18. Get project pipelines
19. Toggle schedule
20. Cleanup & verify deletions

**Additional E2E Suites:**
- Schema contract creation + validation + drift detection
- Data intelligence (schema detection, JSON flattening)
- Full auth flow (register → login → session → logout → invalid session)

## Running Tests

### Prerequisites

```powershell
# From the repo root:
cd .\backend
pip install pytest httpx
```

### Run All Tests

```powershell
python run_tests.py
```

### Run Specific Suites

```powershell
python run_tests.py --unit     # Unit tests only
python run_tests.py --api      # API integration tests only
python run_tests.py --load     # Load & stress tests only
python run_tests.py --e2e      # End-to-end tests only
```

### Run with pytest Directly

```powershell
# All tests
python -m pytest tests/ -v

# Specific file
python -m pytest tests/test_executor.py -v

# Specific test class
python -m pytest tests/test_workflow_store.py::TestWorkflowStoreCRUD -v

# Specific test
python -m pytest tests/test_auth_store.py::TestUserModel::test_verify_password_correct -v

# With coverage
python -m pytest tests/ -v --tb=short
```

### Generate Test Report

```powershell
python run_tests.py --report
# Generates TEST_REPORT.md in backend/
```

## Test Architecture

```
backend/
├── tests/
│   ├── __init__.py              # Package marker
│   ├── conftest.py              # Shared fixtures (stores, models, data)
│   ├── test_workflow_store.py   # WorkflowStore unit tests
│   ├── test_project_store.py    # ProjectStore unit tests
│   ├── test_schedule_store.py   # ScheduleStore unit tests
│   ├── test_alert_store.py      # AlertStore unit tests
│   ├── test_execution_store.py  # ExecutionStore unit tests
│   ├── test_auth_store.py       # UserStore + auth unit tests
│   ├── test_variable_store.py   # VariableStore unit tests
│   ├── test_credential_store.py # CredentialStore unit tests
│   ├── test_lifecycle_store.py  # LifecycleStore unit tests
│   ├── test_validator.py        # IR validator unit tests
│   ├── test_schema_contract.py  # Schema contracts unit tests
│   ├── test_executor.py         # Execution engine unit tests
│   ├── test_scheduler.py        # Scheduler unit tests
│   ├── test_notifier.py         # Notification service unit tests
│   ├── test_api_integration.py  # All 70+ API endpoints
│   ├── test_load_stress.py      # Performance + concurrency + memory
│   └── test_e2e_pipeline.py     # Full lifecycle E2E
├── pytest.ini                   # pytest configuration
└── run_tests.py                 # Test runner with report generation
```

## Fixtures (conftest.py)

| Fixture | Type | Description |
|---------|------|-------------|
| `workflow_store` | Store | Fresh WorkflowStore instance |
| `project_store` | Store | Fresh ProjectStore (includes "default") |
| `schedule_store` | Store | Fresh ScheduleStore |
| `alert_store` | Store | Fresh AlertStore |
| `execution_store` | Store | Fresh ExecutionStore |
| `user_store` | Store | Fresh UserStore (includes "admin" user) |
| `variable_store` | Store | Fresh VariableStore |
| `credential_store` | Store | Fresh CredentialStore |
| `connection_store` | Store | Fresh ConnectionStore |
| `contract_store` | Store | Fresh SchemaContractStore |
| `lifecycle_store` | Store | Fresh LifecycleStore |
| `sample_workflow` | Data | 3-step pipeline (CSV→Filter→Output) |
| `sample_project` | Data | Test project with custom settings |
| `sample_schedule` | Data | Daily schedule at 09:00 UTC |
| `sample_alert_rule` | Data | Email alert on failure |
| `sample_variable` | Data | Global string variable |
| `sample_credential` | Data | PostgreSQL credential with password |
| `temp_data_dir` | Path | Temporary directory for test files |
| `sample_csv_file` | Path | 5-row CSV with orders data |

## What's Tested vs. What's Not

### Tested
- All 11 in-memory stores (CRUD + edge cases)
- Workflow execution with DuckDB
- Topological sort + dependency resolution
- Schema validation + drift detection
- Cron expression parsing (5-field)
- Schedule evaluation (interval, daily, weekly, cron, event)
- Email/Slack/Teams/Webhook notification dispatch
- Password hashing + session management
- Variable scoping + resolution
- Credential masking
- All 70+ API endpoints
- Concurrent API access
- Memory usage at scale
- Unicode data handling

### Not Tested (Requires External Services)
- Real SMTP email delivery (uses dry-run mode)
- Real Slack/Teams webhook delivery (mocked)
- Real database connections (PostgreSQL, MySQL, etc.)
- Real S3/cloud storage
- Frontend UI rendering (no Vitest/RTL yet)
- Browser-based E2E (no Playwright/Cypress yet)

## Performance Baselines

These thresholds are validated by the load tests:

| Operation | Threshold | Notes |
|-----------|-----------|-------|
| 1K workflow inserts | < 5s | In-memory store |
| 10K execution records | < 5s | In-memory store |
| List 1K workflows | < 2s | Linear scan |
| 10K row pipeline execution | < 10s | DuckDB in-memory |
| 50K row aggregate | < 15s | DuckDB in-memory |
| 50 concurrent API requests | < 10s | FastAPI async |
| 5K workflows memory | < 100MB | Pydantic models |
| 50K executions memory | < 200MB | Pydantic models |
