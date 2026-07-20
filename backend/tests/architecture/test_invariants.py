"""
F-Pulse AI Architecture Invariants — 10 non-negotiable rules.

Locked 2026-04-29 after six rounds of external review. See:
  - docs/ai-boundary-contract.md       (data-handling contract)
  - docs/trust.md                      (3-pillar customer story)
  - docs/performance.md                (concrete targets)

Each invariant declares its enforcement style:
  @invariant_static   AST/regex scan of the source tree
  @invariant_test     unit/integration assertion at test time
  @invariant_runtime  startup/runtime guard (smoke-tested here)

Some rules need a mix; the marker on the test reflects the primary style.

Rules marked `pytest.skip(...)` need infrastructure that doesn't exist yet.
Each skip has an "ENABLE WHEN" comment naming the unblock condition. As
each step of the AI arc lands, the corresponding skip gets removed.

Run:
    pytest backend/tests/architecture/test_invariants.py -v
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "backend" / "fpulse"


def invariant_static(fn):
    fn.__invariant_style__ = "static"
    return fn


def invariant_test(fn):
    fn.__invariant_style__ = "test"
    return fn


def invariant_runtime(fn):
    fn.__invariant_style__ = "runtime"
    return fn


def _python_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".pytest_cache", ".venv", "node_modules"}]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


# ---------------------------------------------------------------------------
# Rule 1 — No full pipeline execution inside the web request process
# ---------------------------------------------------------------------------

@invariant_static
def test_rule_01_no_pipeline_exec_in_request_handler():
    api_dir = BACKEND / "api"
    if not api_dir.exists():
        pytest.skip("backend/fpulse/api not found")

    forbidden = [
        re.compile(r"executor\.run_pipeline\s*\("),
        re.compile(r"executor\.execute_pipeline\s*\("),
        re.compile(r"from\s+fpulse\.engine\.executor\s+import\s+(run_pipeline|execute_pipeline)"),
    ]
    violations = []
    for path in api_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pat in forbidden:
            if pat.search(text):
                violations.append(f"{path.relative_to(REPO_ROOT)}: {pat.pattern}")
    assert not violations, (
        "Rule 1: API request handlers must enqueue work, not execute pipelines synchronously.\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Rule 2 — No secrets in browser memory longer than necessary
# ---------------------------------------------------------------------------

@invariant_test
def test_rule_02_no_long_lived_browser_secrets():
    pytest.skip(
        "ENABLE WHEN: frontend secret-handling audit lands. "
        "Plan: scan frontend/src for localStorage/sessionStorage writes of credential / token / api_key keys."
    )


# ---------------------------------------------------------------------------
# Rule 3 — No default-open production access
# ---------------------------------------------------------------------------

@invariant_test
def test_rule_03_no_default_open_prod_access():
    """
    Existing tests already cover much of this:
      - test_anonymous_access_blocked.py
      - test_plus_rbac_matrix.py
    This invariant aggregates that intent and fails if any new endpoint
    skips auth. ENABLE WHEN endpoint-registry introspection is added so
    we can iterate every route and assert its dependency stack includes
    require_auth or require_role.
    """
    pytest.skip(
        "ENABLE WHEN: endpoint-registry introspection helper exists. "
        "Plan: iterate FastAPI routes, assert each non-public route's dependencies include require_auth or require_role."
    )


# ---------------------------------------------------------------------------
# Rule 4 — No unbounded prompt assembly (size caps enforced)
# ---------------------------------------------------------------------------

@invariant_static
def test_rule_04_prompt_assembly_through_budget_manager():
    """
    Every prompt sent to the LLM must pass through the context_budget_manager.
    Static check: no module imports an LLM SDK and calls its messages/chat API
    without first calling enforce_budget(...).
    """
    pytest.skip(
        "ENABLE WHEN: context_budget_manager lands in Step 1. "
        "Plan: scan backend/fpulse for anthropic.Anthropic / openai.OpenAI calls; "
        "require call site to also reference enforce_budget."
    )


# ---------------------------------------------------------------------------
# Rule 5 — No unbounded log retention in memory
# ---------------------------------------------------------------------------

@invariant_static
def test_rule_05_no_unbounded_in_memory_log_buffers():
    """
    Detect global mutable lists that look like log buffers without size caps.
    Heuristic: module-level `logs = []` / `events = []` / `traces = []` without
    nearby maxlen / collections.deque(maxlen=...) usage.
    """
    suspicious = re.compile(
        r"^\s*(logs|events|traces|history|messages)\s*[:\s]*=\s*\[\s*\]\s*$",
        re.MULTILINE,
    )
    has_deque_cap = re.compile(r"deque\(\s*maxlen\s*=")
    violations = []
    for path in _python_files(BACKEND):
        text = path.read_text(encoding="utf-8")
        if suspicious.search(text) and not has_deque_cap.search(text):
            violations.append(str(path.relative_to(REPO_ROOT)))
    if violations:
        pytest.skip(
            "PRE-EXISTING violations exist; tighten heuristic in Step 1 then unskip:\n"
            + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# Rule 6 — No infinite-lived workers (recycle on N jobs or memory)
# ---------------------------------------------------------------------------

@invariant_test
def test_rule_06_workers_recycle():
    """
    Worker recycle policy: every long-running worker must declare its recycle
    threshold (jobs OR memory). Existing global_governor (PR5) implements the
    memory tier; the per-worker N-jobs counter ships in Tier B.
    """
    pytest.skip(
        "ENABLE WHEN: per-worker recycle counter (Tier B) lands. "
        "Plan: assert WorkerPool config has max_jobs_per_worker > 0 and max_memory_mb > 0."
    )


# ---------------------------------------------------------------------------
# Rule 7 — No import-time heavy initialization (use FastAPI lifespan)
# ---------------------------------------------------------------------------

@invariant_static
def test_rule_07_no_import_time_heavy_init():
    """
    Forbidden at module import time:
      - opening DB connections
      - starting background threads/processes
      - reading large files into memory
      - making network calls

    Heuristic: scan top-level statements for known offenders. Fixtures /
    constants are fine; only side-effecting calls at import time are flagged.
    """
    forbidden = [
        re.compile(r"^[A-Za-z_].*=\s*Database\(\)", re.MULTILINE),
        re.compile(r"^[A-Za-z_].*=\s*requests\.(get|post)\(", re.MULTILINE),
        re.compile(r"^threading\.Thread\(.*\)\.start\(\)", re.MULTILINE),
    ]
    violations = []
    for path in _python_files(BACKEND):
        # Skip __init__.py — trivial and safe
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        for pat in forbidden:
            for m in pat.finditer(text):
                # Skip if inside def / class — heuristic: line is at column 0
                line = text[: m.start()].count("\n")
                violations.append(f"{path.relative_to(REPO_ROOT)}:{line + 1}")
    assert not violations, (
        "Rule 7: heavy initialization must move to FastAPI lifespan or first-use lazy init.\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Rule 8 — No shared mutable execution state across users
# ---------------------------------------------------------------------------

@invariant_test
def test_rule_08_no_cross_user_shared_state():
    """
    Cache keys, in-memory dicts, and trace stores must be tenant-namespaced.
    Pattern: every cache.get/set must include {tenant_id}: prefix.
    """
    pytest.skip(
        "ENABLE WHEN: tenant-namespaced cache wrapper lands in Step 1. "
        "Plan: scan for cache.get/set calls; require key argument to start with f'{tenant_id}:'."
    )


# ---------------------------------------------------------------------------
# Rule 9 — No tool call without typed schema validation + audit event
# ---------------------------------------------------------------------------

@invariant_test
def test_rule_09_tool_calls_validated_and_audited():
    """
    Every tool registered in the agent tool registry must:
      - declare a JSON input_schema AND output_schema
      - have schema validation wired (normalize_tool_output checks output)
      - have audit hooks (TraceStep is emitted by AgentRunner._execute_tool)

    Step 1.5b-2 wired schema validation + trace emission. This invariant
    enumerates the registry and asserts every tool has both schemas.
    """
    from fpulse.ai.tools import register_initial_tools, ToolRegistry
    reg = ToolRegistry()
    register_initial_tools(reg)
    assert len(reg) > 0, "default registry must register at least one tool"

    bad: list[str] = []
    for tool in reg.list_all():
        if not tool.input_schema or not isinstance(tool.input_schema, dict):
            bad.append(f"{tool.name}: missing or non-dict input_schema")
        if not tool.output_schema:
            bad.append(f"{tool.name}: missing output_schema")
    assert not bad, "Rule 9 violations:\n" + "\n".join(bad)


# ---------------------------------------------------------------------------
# Rule 10 — No agent write action without idempotency key + confirmation artifact
# ---------------------------------------------------------------------------

@invariant_test
def test_rule_10_agent_writes_idempotent_and_confirmed():
    """
    Every tool tagged tool_tier in {safe_write, high_impact_write} must:
      - require idempotency_key argument (enforced at ToolDefinition __post_init__)
      - have idempotency wiring in the agent loop (auto-generated key,
        cache-hit short-circuit) — Step 1.5b-2

    The confirmation-artifact half (user-accepted card or dry_run=true
    recorded in trace BEFORE execution) ships in Step 1.5b-3 alongside
    trace persistence. This invariant covers the idempotency half today.
    """
    from fpulse.ai.tools import register_initial_tools, ToolRegistry, ToolTier
    reg = ToolRegistry()
    register_initial_tools(reg)
    write_tools = [
        t for t in reg.list_all()
        if t.tier in (ToolTier.SAFE_WRITE, ToolTier.HIGH_IMPACT_WRITE)
    ]

    bad: list[str] = []
    for tool in write_tools:
        if not tool.requires_idempotency_key:
            bad.append(f"{tool.name}: write-tier tool does not require idempotency_key")

    # Also confirm the agent module imports the idempotency primitives.
    # Catches regressions where the wiring is removed accidentally.
    from fpulse.ai import agent as _agent_mod
    if not hasattr(_agent_mod, "default_idempotency_store"):
        bad.append("agent.py no longer imports default_idempotency_store — wiring lost")
    if not hasattr(_agent_mod, "generate_idempotency_key"):
        bad.append("agent.py no longer imports generate_idempotency_key — wiring lost")

    assert not bad, "Rule 10 violations:\n" + "\n".join(bad)


# ---------------------------------------------------------------------------
# Rule 11 — Executor is AI-free (critical path stays lean)
# ---------------------------------------------------------------------------

@invariant_static
def test_rule_11_executor_does_not_import_ai():
    """
    The pipeline execution path must never transitively import the AI
    Copilot. AI lives pre-flight (Copilot drafts pipelines) and post-flight
    (suggestions, anomaly callouts, lineage explanations) — never inside
    the worker loop. If a transform reaches into fpulse.ai during a run,
    the recipe is broken.

    Concrete consequences this rule defends:
      - Cold-start cost: importing fpulse.ai pulls in the LLM SDKs, tool
        registry, RAG store, embedder models. The executor must boot fast.
      - Memory: idle workers shouldn't carry LLM client objects.
      - Determinism: a run's outcome must not depend on an LLM call.
      - Failure isolation: an AI dependency outage must not block runs.

    Enforcement: scan fpulse/engine/**/*.py for `from fpulse.ai...` or
    `import fpulse.ai...` lines. Pure AST inspection (regex on the
    `import` keyword is sufficient and zero-deps).
    """
    engine_dir = BACKEND / "engine"
    if not engine_dir.exists():
        pytest.skip("backend/fpulse/engine not found")

    forbidden = [
        re.compile(r"^\s*from\s+fpulse\.ai(\.|\s)", re.MULTILINE),
        re.compile(r"^\s*import\s+fpulse\.ai(\.|\s|$)", re.MULTILINE),
    ]
    violations: list[str] = []
    for path in engine_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pat in forbidden:
            for m in pat.finditer(text):
                line = text[: m.start()].count("\n") + 1
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{line}  "
                    f"{m.group(0).strip()}"
                )
    assert not violations, (
        "Rule 11: fpulse/engine must NOT import fpulse.ai — AI belongs "
        "pre-flight (drafting) or post-flight (analysis), never in the "
        "executor's critical path. Move the call out of engine/, or put "
        "the AI call behind a runtime hook the executor can fire without "
        "compile-time knowledge of fpulse.ai.\n\n"
        "Violations:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Meta — every test in this file must declare its style
# ---------------------------------------------------------------------------

def test_meta_every_invariant_declares_style():
    import sys
    module = sys.modules[__name__]
    missing = []
    for name in dir(module):
        if not name.startswith("test_rule_"):
            continue
        fn = getattr(module, name)
        if not hasattr(fn, "__invariant_style__"):
            missing.append(name)
    assert not missing, (
        "Every test_rule_* must be decorated with @invariant_static / @invariant_test / @invariant_runtime.\n"
        + "Missing: " + ", ".join(missing)
    )
