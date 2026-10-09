"""Connector Acceptance Tests — the one connector-agnostic suite.

Every connector that has CAT fixtures recorded under
`backend/tests/fixtures/connectors/<id>/` flows through this single
parametrized test. A connector passes when no acceptance check fails and it
reaches at least `beta`; the goal is to turn the cert matrix's "Verified"
from "a file exists" into "the behavioral suite went green".

Offline / replay only — no secrets, green on forks.
"""

from __future__ import annotations

import pytest

from fpulse.connectors.cat import discover_certifiable, run_cat

_CONNECTORS = discover_certifiable()


@pytest.mark.skipif(not _CONNECTORS, reason="no connector CAT fixtures recorded yet")
@pytest.mark.parametrize("connector_id", _CONNECTORS)
def test_connector_acceptance(connector_id: str) -> None:
    report = run_cat(connector_id, mode="replay")
    assert report.failed == 0, (
        f"\nCAT failures for '{connector_id}':\n{report.failures_pretty()}"
    )
    # A connector with fixtures must at least execute its happy path + spec.
    assert report.cat_level in {"beta", "verified"}, (
        f"'{connector_id}' only reached '{report.cat_level}'"
    )


def test_github_reaches_verified() -> None:
    """The seeded github fixtures (happy_path + empty + schema) should earn
    the full offline 'verified' level — the concrete proof that the pipeline
    spec -> run -> schema -> empty works end to end."""
    if "github" not in _CONNECTORS:
        pytest.skip("github CAT fixtures not present")
    report = run_cat("github", mode="replay")
    assert report.failed == 0, report.failures_pretty()
    assert report.cat_level == "verified", (
        f"github reached '{report.cat_level}', expected 'verified'\n"
        + "\n".join(f"  {c.stream}.{c.check}={c.status} ({c.detail})" for c in report.checks)
    )
