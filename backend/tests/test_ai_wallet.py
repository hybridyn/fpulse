"""
Tests for fpulse.ai.wallet.WalletGuard and fpulse.ai.dry_run_promoter.DryRunPromoter.

Uses the per-test SQLite Database fixture from conftest.py.
"""

from __future__ import annotations

import time

import pytest

from fpulse.ai.dry_run_promoter import DryRunPromoter
from fpulse.ai.wallet import (
    QuotaCheck,
    WalletGuard,
    daily_user_cap,
    daily_workspace_cap,
    rate_per_minute,
)


# ---------------------------------------------------------------------------
# WalletGuard
# ---------------------------------------------------------------------------


@pytest.fixture
def wallet(_fpulse_test_db):
    return WalletGuard(_db=_fpulse_test_db)


def test_wallet_allows_when_zero_usage(wallet):
    chk = wallet.check_before_run(user_id="u-1", workspace_id="ws-1")
    assert chk.allowed is True
    assert chk.rule == ""


def test_wallet_record_usage_accumulates(wallet):
    wallet.record_usage(user_id="u-1", workspace_id="ws-1", tokens_in=100, tokens_out=50)
    wallet.record_usage(user_id="u-1", workspace_id="ws-1", tokens_in=200, tokens_out=100)
    user_total = wallet.daily_total("user", "u-1")
    ws_total = wallet.daily_total("workspace", "ws-1")
    assert user_total == 450  # (100+50) + (200+100)
    assert ws_total == 450


def test_wallet_user_cap_blocks_at_threshold(wallet, monkeypatch):
    monkeypatch.setenv("FPULSE_AGENT_DAILY_TOKENS_USER", "1000")
    # Spend right at the cap
    wallet.record_usage(user_id="u-1", workspace_id="ws-1", tokens_in=600, tokens_out=400)
    chk = wallet.check_before_run(user_id="u-1", workspace_id="ws-1")
    assert chk.allowed is False
    assert chk.rule == "wallet:daily_user_token_cap"
    assert "1000" in chk.reason


def test_wallet_workspace_cap_blocks(wallet, monkeypatch):
    monkeypatch.setenv("FPULSE_AGENT_DAILY_TOKENS_USER", "1000000000")
    monkeypatch.setenv("FPULSE_AGENT_DAILY_TOKENS_WORKSPACE", "1500")
    wallet.record_usage(user_id="u-1", workspace_id="ws-shared", tokens_in=1000, tokens_out=500)
    chk = wallet.check_before_run(user_id="u-2", workspace_id="ws-shared")
    assert chk.allowed is False
    assert chk.rule == "wallet:daily_workspace_token_cap"


def test_wallet_rate_limit_enforces_per_minute(wallet, monkeypatch):
    monkeypatch.setenv("FPULSE_AGENT_RATE_PER_MINUTE", "3")
    monkeypatch.setenv("FPULSE_AGENT_DAILY_TOKENS_USER", "1000000")
    for _ in range(3):
        chk = wallet.check_before_run(user_id="u-1", workspace_id="ws-1")
        assert chk.allowed is True
        wallet.note_request_started("u-1")
    # 4th attempt within the same minute → rate-limited
    chk = wallet.check_before_run(user_id="u-1", workspace_id="ws-1")
    assert chk.allowed is False
    assert chk.rule == "wallet:rate_limit_per_minute"


def test_wallet_anonymous_skips_user_cap(wallet, monkeypatch):
    """Anonymous (no user_id) shouldn't be subject to the per-user gate.
    Workspace cap still applies."""
    monkeypatch.setenv("FPULSE_AGENT_DAILY_TOKENS_USER", "10")
    # No user_id passed
    chk = wallet.check_before_run(user_id=None, workspace_id="ws-1")
    assert chk.allowed is True


def test_wallet_record_usage_zero_is_no_op(wallet):
    wallet.record_usage(user_id="u-1", workspace_id="ws-1", tokens_in=0, tokens_out=0)
    assert wallet.daily_total("user", "u-1") == 0
    # No row should have been written
    row = wallet.usage_for("user", "u-1")
    assert row == {}


def test_wallet_usage_for_returns_row(wallet):
    wallet.record_usage(user_id="u-1", workspace_id="ws-1", tokens_in=100, tokens_out=50, cost_usd=0.05)
    row = wallet.usage_for("user", "u-1")
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 50
    assert row["request_count"] == 1
    assert row["cost_usd"] == pytest.approx(0.05)


def test_wallet_handles_missing_db():
    w = WalletGuard(_db=None)
    chk = w.check_before_run(user_id="u-1", workspace_id="ws-1")
    assert chk.allowed is True
    w.record_usage(user_id="u-1", workspace_id="ws-1", tokens_in=100, tokens_out=50)
    assert w.daily_total("user", "u-1") == 0  # no-op


def test_env_clamps_apply(monkeypatch):
    monkeypatch.setenv("FPULSE_AGENT_DAILY_TOKENS_USER", "0")     # below floor
    monkeypatch.setenv("FPULSE_AGENT_RATE_PER_MINUTE", "999999")  # above ceiling
    assert daily_user_cap() == 1_000        # clamped UP
    assert rate_per_minute() == 1000        # clamped DOWN


# ---------------------------------------------------------------------------
# DryRunPromoter
# ---------------------------------------------------------------------------


@pytest.fixture
def promoter(_fpulse_test_db):
    return DryRunPromoter(_db=_fpulse_test_db)


def test_promoter_forces_dry_run_below_threshold(promoter, monkeypatch):
    monkeypatch.setenv("FPULSE_AGENT_DRY_RUN_THRESHOLD", "3")
    assert promoter.should_force_dry_run("u-1", "compose_report") is True


def test_promoter_unlocks_after_threshold(promoter, monkeypatch):
    monkeypatch.setenv("FPULSE_AGENT_DRY_RUN_THRESHOLD", "3")
    for _ in range(3):
        promoter.record_success("u-1", "compose_report")
    assert promoter.should_force_dry_run("u-1", "compose_report") is False


def test_promoter_per_user_per_tool_isolation(promoter, monkeypatch):
    monkeypatch.setenv("FPULSE_AGENT_DRY_RUN_THRESHOLD", "2")
    promoter.record_success("u-1", "compose_report")
    promoter.record_success("u-1", "compose_report")
    # u-1 unlocked for compose_report
    assert promoter.should_force_dry_run("u-1", "compose_report") is False
    # but not for other tools
    assert promoter.should_force_dry_run("u-1", "send_to_destination") is True
    # and not for other users
    assert promoter.should_force_dry_run("u-2", "compose_report") is True


def test_promoter_anonymous_always_forced(promoter, monkeypatch):
    monkeypatch.setenv("FPULSE_AGENT_DRY_RUN_THRESHOLD", "0")  # disable for named users
    # Even with threshold=0, anonymous still forced
    assert promoter.should_force_dry_run("anonymous", "compose_report") is True
    assert promoter.should_force_dry_run(None, "compose_report") is True


def test_promoter_threshold_zero_disables_for_named(promoter, monkeypatch):
    monkeypatch.setenv("FPULSE_AGENT_DRY_RUN_THRESHOLD", "0")
    assert promoter.should_force_dry_run("u-1", "compose_report") is False


def test_promoter_record_success_anonymous_is_noop(promoter, monkeypatch):
    monkeypatch.setenv("FPULSE_AGENT_DRY_RUN_THRESHOLD", "1")
    promoter.record_success(None, "compose_report")
    promoter.record_success("anonymous", "compose_report")
    # Counters unchanged for both — anonymous is never tracked toward unlock
    assert promoter.success_count("anonymous", "compose_report") == 0


def test_promoter_handles_missing_db():
    p = DryRunPromoter(_db=None)
    assert p.success_count("u-1", "compose_report") == 0
    p.record_success("u-1", "compose_report")  # no-op
    assert p.success_count("u-1", "compose_report") == 0
