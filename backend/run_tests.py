#!/usr/bin/env python3
"""
F-Pulse Test Runner — runs all tests and generates a comprehensive report.

Usage:
  python run_tests.py           # Run all tests
  python run_tests.py --unit    # Unit tests only
  python run_tests.py --api     # API integration tests only
  python run_tests.py --load    # Load & stress tests only
  python run_tests.py --e2e     # End-to-end tests only
  python run_tests.py --report  # Generate HTML report
"""

import sys
import os
import time
import subprocess
import argparse
from datetime import datetime


def run_command(cmd: list[str], label: str) -> dict:
    """Run a command and capture output."""
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}\n")

    start = time.time()
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=os.path.dirname(os.path.abspath(__file__)),
    )
    elapsed = time.time() - start

    print(result.stdout)
    if result.stderr:
        print(result.stderr)

    return {
        "label": label,
        "returncode": result.returncode,
        "elapsed": elapsed,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "passed": result.returncode == 0,
    }


def count_tests(output: str) -> dict:
    """Parse pytest output for test counts."""
    counts = {"passed": 0, "failed": 0, "errors": 0, "warnings": 0}
    for line in output.split("\n"):
        if "passed" in line and ("failed" in line or "error" in line or line.strip().startswith("=")):
            parts = line.lower()
            for word in parts.split():
                if word.isdigit():
                    num = int(word)
                    if "passed" in parts and counts["passed"] == 0:
                        counts["passed"] = num
                    elif "failed" in parts and counts["failed"] == 0:
                        counts["failed"] = num
            break
        elif "passed" in line and line.strip().startswith("="):
            for word in line.split():
                if word.isdigit():
                    counts["passed"] = int(word)
                    break
            break
    return counts


def generate_report(results: list[dict]) -> str:
    """Generate Markdown test report."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_time = sum(r["elapsed"] for r in results)
    all_passed = all(r["passed"] for r in results)

    report = f"""# F-Pulse v1.0.0 — Test Report

**Generated:** {now}
**Status:** {'ALL PASSED' if all_passed else 'FAILURES DETECTED'}
**Total Duration:** {total_time:.1f}s

## Summary

| Suite | Status | Duration | Details |
|-------|--------|----------|---------|
"""
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        emoji = "+" if r["passed"] else "-"
        report += f"| {r['label']} | {status} | {r['elapsed']:.1f}s | {count_tests(r['stdout'])} |\n"

    report += f"""
## Test Suites

### 1. Unit Tests — Store Layer
Tests all 11 in-memory stores: WorkflowStore, ProjectStore, ScheduleStore,
AlertStore, ExecutionStore, UserStore, VariableStore, CredentialStore,
ConnectionStore, LifecycleStore, SchemaContractStore.

**Coverage:**
- CRUD operations (Create, Read, Update, Delete)
- Filtering and querying
- Data isolation (deep copy)
- Edge cases (nonexistent IDs, empty stores)
- Secret masking (credentials, variables)
- Password hashing and verification
- Session management
- Variable resolution (project→global fallback)
- Schema drift detection
- Contract validation with type compatibility

### 2. Unit Tests — Engine & Services
- **WorkflowValidator:** structural validation, parameter checks, cycle detection
- **WorkflowExecutor:** topological sort, step execution, dependency resolution, preview
- **PipelineScheduler:** cron field matching, schedule evaluation, start/stop lifecycle
- **NotificationService:** email dry-run, Slack/Teams/Webhook dispatch, message templates

### 3. API Integration Tests
Tests all 70+ API endpoints via FastAPI TestClient:
- Health & utility (health, scheduler status, node types, files)
- Projects CRUD + pipeline listing
- Workflows CRUD + versioning + diff + validate
- Execution (full workflow + single step + preview)
- Schedules CRUD + toggle + run-now
- Alerts CRUD + test alert + logs
- Monitor (executions, stats, active schedules, failed pipelines)
- Auth (login, register, session, logout)
- Variables CRUD + resolve
- Credentials CRUD + test + masking
- Connections CRUD + test + reports
- Intelligence (schema detect, flatten)
- Contracts (create, validate, drift)
- Planner (templates, generate)
- File upload (CSV, reject .exe)

### 4. Load & Stress Tests
- **Store performance:** 1000 workflows, 10K executions, 1000 variables — all < 5s
- **Scheduler speed:** 1000 schedule evaluations < 1s, 10K cron evaluations < 2s
- **Executor throughput:** 10K row pipeline < 10s, 50K row aggregate < 15s, 50 sequential runs < 30s
- **Concurrent API:** 50 health checks, 20 project creates, 20 workflow creates — all concurrent
- **Memory usage:** 5000 workflows < 100MB, 50K executions < 200MB
- **Edge cases:** empty CSV, single row, Unicode data, 10K-char names, 100-step workflows

### 5. End-to-End Pipeline Tests
Full lifecycle test (20 steps):
1. Create project
2. Create pipeline (3 nodes: CSV→Filter→Aggregate)
3. Validate pipeline
4. Execute pipeline (verify row counts at each step)
5. Execute single step
6. Check monitor/executions
7. Create schedule
8. Create alert rule
9. Create variable
10. Create credential (verify masking)
11. Update pipeline (add sort step, verify v2)
12. Diff versions (verify added steps)
13. Test alert notification
14. Archive pipeline
15. Restore pipeline
16. Verify health endpoint
17. Verify execution stats
18. Get project pipelines
19. Toggle schedule
20. Cleanup & verify deletions

Plus: Schema contract E2E, Data intelligence E2E, Full auth flow E2E.

## Architecture Tested

```
┌─────────────────────────────────────────────────┐
│              F-Pulse v1.0.0                      │
├─────────────────────────────────────────────────┤
│ 13 API Routers · 70+ Endpoints                  │
│ 11 In-Memory Stores                             │
│ DuckDB Execution Engine                          │
│ Background Pipeline Scheduler                    │
│ Multi-Channel Notification Service               │
│ Schema Contract & Drift Detection                │
│ Workflow Validation (structural + data-level)    │
│ Pipeline Templates & AI Planner                  │
│ Session-based Auth                               │
└─────────────────────────────────────────────────┘
```

## Test File Inventory

| File | Tests | Focus |
|------|-------|-------|
| test_workflow_store.py | ~25 | WorkflowStore CRUD, versioning, status |
| test_project_store.py | ~12 | ProjectStore CRUD, default protection |
| test_schedule_store.py | ~12 | ScheduleStore CRUD, run recording |
| test_alert_store.py | ~15 | AlertStore rules + logs |
| test_execution_store.py | ~15 | ExecutionStore, stats, step logs |
| test_auth_store.py | ~20 | UserStore, sessions, password hashing |
| test_variable_store.py | ~15 | VariableStore, scoping, resolution |
| test_credential_store.py | ~15 | CredentialStore, masking |
| test_lifecycle_store.py | ~10 | LifecycleStore events |
| test_validator.py | ~15 | IR validation, cycles, connections |
| test_schema_contract.py | ~15 | Contracts, validation, drift |
| test_executor.py | ~12 | Execution, topological sort, chaining |
| test_scheduler.py | ~15 | Cron parsing, schedule evaluation |
| test_notifier.py | ~12 | Email/Slack/Teams/Webhook |
| test_api_integration.py | ~50 | All 70+ API endpoints |
| test_load_stress.py | ~20 | Performance, concurrency, memory |
| test_e2e_pipeline.py | ~25 | Full lifecycle, contracts, auth |
| **TOTAL** | **~300+** | |
"""
    return report


def main():
    parser = argparse.ArgumentParser(description="F-Pulse Test Runner")
    parser.add_argument("--unit", action="store_true", help="Run unit tests only")
    parser.add_argument("--api", action="store_true", help="Run API integration tests only")
    parser.add_argument("--load", action="store_true", help="Run load/stress tests only")
    parser.add_argument("--e2e", action="store_true", help="Run E2E tests only")
    parser.add_argument("--report", action="store_true", help="Generate report after tests")
    args = parser.parse_args()

    python = sys.executable
    results = []

    run_all = not (args.unit or args.api or args.load or args.e2e)

    if run_all or args.unit:
        results.append(run_command(
            [python, "-m", "pytest", "tests/test_workflow_store.py", "tests/test_project_store.py",
             "tests/test_schedule_store.py", "tests/test_alert_store.py",
             "tests/test_execution_store.py", "tests/test_auth_store.py",
             "tests/test_variable_store.py", "tests/test_credential_store.py",
             "tests/test_lifecycle_store.py", "tests/test_validator.py",
             "tests/test_schema_contract.py", "tests/test_executor.py",
             "tests/test_scheduler.py", "tests/test_notifier.py",
             "-v", "--tb=short"],
            "Unit Tests (Stores, Engine, Services)",
        ))

    if run_all or args.api:
        results.append(run_command(
            [python, "-m", "pytest", "tests/test_api_integration.py", "-v", "--tb=short"],
            "API Integration Tests (70+ endpoints)",
        ))

    if run_all or args.load:
        results.append(run_command(
            [python, "-m", "pytest", "tests/test_load_stress.py", "-v", "--tb=short"],
            "Load & Stress Tests (performance, concurrency, memory)",
        ))

    if run_all or args.e2e:
        results.append(run_command(
            [python, "-m", "pytest", "tests/test_e2e_pipeline.py", "-v", "--tb=short"],
            "End-to-End Pipeline Tests (full lifecycle)",
        ))

    # Summary
    print(f"\n{'='*60}")
    print("  TEST SUMMARY")
    print(f"{'='*60}")
    for r in results:
        status = "PASSED" if r["passed"] else "FAILED"
        print(f"  [{status}] {r['label']} ({r['elapsed']:.1f}s)")

    total_passed = all(r["passed"] for r in results)
    print(f"\n  Overall: {'ALL TESTS PASSED' if total_passed else 'SOME TESTS FAILED'}")

    # Generate report
    if args.report or run_all:
        report = generate_report(results)
        report_path = os.path.join(os.path.dirname(__file__), "TEST_REPORT.md")
        with open(report_path, "w") as f:
            f.write(report)
        print(f"\n  Report saved: {report_path}")

    return 0 if total_passed else 1


if __name__ == "__main__":
    sys.exit(main())
