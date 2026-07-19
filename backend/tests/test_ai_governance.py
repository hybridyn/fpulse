"""
Tests for the Step 1.5b-1 governance primitives:
  fpulse.ai.rbac           — authorize_tool_call + allowed_tiers_for + role_rank
  fpulse.ai.governance     — PolicyEngine + 3 default rules
  fpulse.ai.idempotency    — generate_key + IdempotencyStore TTL/eviction
  fpulse.ai.prompt_signing — PromptSigner sign/verify

No LLM, no network, no DB.
"""

from __future__ import annotations

import time

import pytest

from fpulse.ai.governance import (
    PolicyContext,
    PolicyDecision,
    PolicyEngine,
    default_engine,
    rule_anonymous_blocked_for_writes,
    rule_high_impact_requires_developer_or_above,
    rule_no_prod_writes_without_approval,
)
from fpulse.ai.idempotency import (
    DEFAULT_TTL_SECONDS,
    IdempotencyStore,
    generate_key,
)
from fpulse.ai.prompt_signing import (
    PromptSigner,
    PromptTamperError,
    default_signer,
    reset_default_signer_for_tests,
)
from fpulse.ai.rbac import (
    allowed_tiers_for,
    authorize_tool_call,
    role_rank,
)
from fpulse.ai.tools.base import ToolTier


# ---------------------------------------------------------------------------
# rbac.py
# ---------------------------------------------------------------------------


def test_rbac_viewer_only_sees_read():
    assert authorize_tool_call(tool_tier=ToolTier.READ, user_role="viewer", environment="dev") is True
    assert authorize_tool_call(tool_tier=ToolTier.SAFE_WRITE, user_role="viewer", environment="dev") is False
    assert authorize_tool_call(tool_tier=ToolTier.HIGH_IMPACT_WRITE, user_role="viewer", environment="prod") is False


def test_rbac_developer_safe_write_dev_only():
    assert authorize_tool_call(tool_tier=ToolTier.SAFE_WRITE, user_role="developer", environment="dev") is True
    assert authorize_tool_call(tool_tier=ToolTier.SAFE_WRITE, user_role="developer", environment="prod") is False
    assert authorize_tool_call(tool_tier=ToolTier.HIGH_IMPACT_WRITE, user_role="developer", environment="dev") is False


def test_rbac_admin_high_impact_dev_only():
    assert authorize_tool_call(tool_tier=ToolTier.HIGH_IMPACT_WRITE, user_role="admin", environment="dev") is True
    assert authorize_tool_call(tool_tier=ToolTier.HIGH_IMPACT_WRITE, user_role="admin", environment="prod") is False
    assert authorize_tool_call(tool_tier=ToolTier.SAFE_WRITE, user_role="admin", environment="prod") is True


def test_rbac_super_admin_unrestricted():
    for tier in ToolTier:
        for env in ("dev", "prod"):
            assert authorize_tool_call(tool_tier=tier, user_role="super_admin", environment=env) is True


def test_rbac_legacy_role_aliases():
    # 'lead' aliases admin; 'member' aliases developer
    assert authorize_tool_call(tool_tier=ToolTier.HIGH_IMPACT_WRITE, user_role="lead", environment="dev") is True
    assert authorize_tool_call(tool_tier=ToolTier.SAFE_WRITE, user_role="member", environment="dev") is True
    assert authorize_tool_call(tool_tier=ToolTier.SAFE_WRITE, user_role="member", environment="prod") is False


def test_rbac_unknown_role_allowed_in_oss_open_world():
    """OSS flipped the unknown-role default to ALLOW (2026-05-17) so single-user
    installs without a configured role can still use read tools. Plus stays
    closed-world via a separate code path."""
    assert authorize_tool_call(tool_tier=ToolTier.READ, user_role="hacker", environment="dev") is True
    assert authorize_tool_call(tool_tier=ToolTier.READ, user_role="", environment="dev") is True


def test_rbac_unknown_environment_denied():
    assert authorize_tool_call(tool_tier=ToolTier.READ, user_role="admin", environment="staging") is False


def test_rbac_case_insensitive():
    assert authorize_tool_call(tool_tier=ToolTier.READ, user_role="ADMIN", environment="DEV") is True
    assert authorize_tool_call(tool_tier=ToolTier.READ, user_role="Viewer", environment="Prod") is True


def test_allowed_tiers_for_returns_sorted_tuple():
    # admin in dev => all three tiers, sorted by .value (read / safe_write / high_impact_write)
    tiers = allowed_tiers_for("admin", "dev")
    assert tiers == (ToolTier.HIGH_IMPACT_WRITE, ToolTier.READ, ToolTier.SAFE_WRITE) or tiers == tuple(sorted([ToolTier.READ, ToolTier.SAFE_WRITE, ToolTier.HIGH_IMPACT_WRITE], key=lambda t: t.value))
    assert len(tiers) == 3


def test_allowed_tiers_for_unknown_role_returns_all_oss():
    """OSS open-world default: unknown roles get all three tiers in dev so
    single-user installs aren't blocked. Plus enforces closed-world."""
    tiers = allowed_tiers_for("hacker", "dev")
    assert set(tiers) == {ToolTier.READ, ToolTier.SAFE_WRITE, ToolTier.HIGH_IMPACT_WRITE}


def test_role_rank_ordering():
    assert role_rank("super_admin") > role_rank("admin")
    assert role_rank("admin") > role_rank("developer")
    assert role_rank("developer") > role_rank("viewer")
    assert role_rank("unknown") == 0


# ---------------------------------------------------------------------------
# governance.py
# ---------------------------------------------------------------------------


def _ctx(**overrides) -> PolicyContext:
    base = dict(
        tool_name="summarize_pipeline",
        tool_tier="read",
        environment="dev",
        user_role="developer",
        workspace_id="ws-1",
        user_id="u-1",
        is_dry_run=False,
        has_approval=False,
    )
    base.update(overrides)
    return PolicyContext(**base)


def test_policy_engine_allow_when_no_rules_fire():
    eng = PolicyEngine()
    decision, fired = eng.evaluate(_ctx())
    assert decision == PolicyDecision.ALLOW
    assert fired == []


def test_policy_engine_first_deny_wins_and_short_circuits():
    eng = PolicyEngine()
    eng.add_rule("a_allows", lambda c: (PolicyDecision.ALLOW, ""))
    eng.add_rule("b_denies", lambda c: (PolicyDecision.DENY, "nope"))
    eng.add_rule("c_should_not_run", lambda c: (PolicyDecision.DENY, "should not see this"))
    decision, fired = eng.evaluate(_ctx())
    assert decision == PolicyDecision.DENY
    assert len(fired) == 1
    assert "policy:b_denies: nope" in fired[0]


def test_rule_no_prod_writes_blocks_safe_write_in_prod():
    decision, _ = rule_no_prod_writes_without_approval(_ctx(tool_tier="safe_write", environment="prod"))
    assert decision == PolicyDecision.DENY


def test_rule_no_prod_writes_allows_with_approval():
    decision, _ = rule_no_prod_writes_without_approval(_ctx(tool_tier="safe_write", environment="prod", has_approval=True))
    assert decision == PolicyDecision.ALLOW


def test_rule_no_prod_writes_allows_with_dry_run():
    decision, _ = rule_no_prod_writes_without_approval(_ctx(tool_tier="high_impact_write", environment="prod", is_dry_run=True))
    assert decision == PolicyDecision.ALLOW


def test_rule_no_prod_writes_passes_for_read():
    decision, _ = rule_no_prod_writes_without_approval(_ctx(tool_tier="read", environment="prod"))
    assert decision == PolicyDecision.ALLOW


def test_rule_high_impact_blocks_viewer():
    decision, _ = rule_high_impact_requires_developer_or_above(_ctx(tool_tier="high_impact_write", user_role="viewer"))
    assert decision == PolicyDecision.DENY


def test_rule_high_impact_allows_developer():
    decision, _ = rule_high_impact_requires_developer_or_above(_ctx(tool_tier="high_impact_write", user_role="developer"))
    assert decision == PolicyDecision.ALLOW


def test_rule_anonymous_blocked_for_writes():
    decision, _ = rule_anonymous_blocked_for_writes(_ctx(tool_tier="safe_write", user_id=None))
    assert decision == PolicyDecision.DENY
    decision2, _ = rule_anonymous_blocked_for_writes(_ctx(tool_tier="read", user_id=None))
    assert decision2 == PolicyDecision.ALLOW


def test_default_engine_loads_three_baseline_rules():
    eng = default_engine()
    assert len(eng) == 3


def test_default_engine_blocks_anonymous_safe_write():
    eng = default_engine()
    decision, fired = eng.evaluate(_ctx(tool_tier="safe_write", user_id=None))
    assert decision == PolicyDecision.DENY
    assert any("anonymous_blocked_for_writes" in f for f in fired)


# ---------------------------------------------------------------------------
# idempotency.py
# ---------------------------------------------------------------------------


def test_generate_key_format_and_stability():
    k1 = generate_key(tier="safe_write", user_id="u-1", action="compose_report", payload={"template": "monthly"})
    k2 = generate_key(tier="safe_write", user_id="u-1", action="compose_report", payload={"template": "monthly"})
    assert k1 == k2
    parts = k1.split(".")
    # tier . user_id . action . hash16 . semver
    assert parts[0] == "safe_write"
    assert parts[1] == "u-1"
    assert parts[2] == "compose_report"
    assert len(parts[3]) == 16
    assert parts[-1] == "v1"


def test_generate_key_differs_per_payload():
    k1 = generate_key(tier="safe_write", user_id="u-1", action="x", payload={"a": 1})
    k2 = generate_key(tier="safe_write", user_id="u-1", action="x", payload={"a": 2})
    assert k1 != k2


def test_generate_key_anonymous_user_id_default():
    k = generate_key(tier="read", user_id=None, action="x", payload={})
    assert k.split(".")[1] == "anonymous"


def test_generate_key_canonical_payload_order():
    k1 = generate_key(tier="x", user_id="u", action="a", payload={"b": 2, "a": 1})
    k2 = generate_key(tier="x", user_id="u", action="a", payload={"a": 1, "b": 2})
    assert k1 == k2


def test_idempotency_store_put_get_roundtrip():
    store = IdempotencyStore()
    store.put("k", {"draft_id": "d-1"})
    hit, value = store.get("k")
    assert hit is True
    assert value == {"draft_id": "d-1"}


def test_idempotency_store_miss():
    store = IdempotencyStore()
    hit, value = store.get("missing")
    assert hit is False
    assert value is None


def test_idempotency_store_eviction_when_over_max():
    store = IdempotencyStore(max_entries=2)
    store.put("a", 1)
    store.put("b", 2)
    store.put("c", 3)  # evicts oldest-expiry
    assert len(store) == 2


def test_idempotency_store_replace_does_not_evict():
    store = IdempotencyStore(max_entries=2)
    store.put("a", 1)
    store.put("b", 2)
    store.put("a", "updated")  # not a new key — must NOT evict
    assert len(store) == 2
    hit_a, val_a = store.get("a")
    assert hit_a and val_a == "updated"


def test_idempotency_store_rejects_nonpositive_ttl():
    store = IdempotencyStore()
    with pytest.raises(ValueError):
        store.put("k", 1, ttl_seconds=0)


def test_idempotency_store_in_operator():
    store = IdempotencyStore()
    store.put("k", 1)
    assert "k" in store
    assert "missing" not in store


# ---------------------------------------------------------------------------
# prompt_signing.py
# ---------------------------------------------------------------------------


def test_prompt_signer_sign_and_verify_roundtrip():
    signer = PromptSigner.with_key(b"test-key" * 4)
    signer.sign("system", "the canonical system prompt")
    assert signer.verify("system", "the canonical system prompt") is True


def test_prompt_signer_detects_tampering():
    signer = PromptSigner.with_key(b"test-key" * 4)
    signer.sign("system", "the canonical system prompt")
    assert signer.verify("system", "TAMPERED prompt") is False


def test_prompt_signer_unknown_name_returns_false_not_raise():
    signer = PromptSigner.with_key(b"test-key" * 4)
    assert signer.verify("never_signed", "anything") is False


def test_prompt_signer_resign_overwrites_prior():
    signer = PromptSigner.with_key(b"test-key" * 4)
    signer.sign("system", "v1")
    signer.sign("system", "v2")
    assert signer.verify("system", "v2") is True
    assert signer.verify("system", "v1") is False  # old one no longer matches


def test_prompt_signer_different_keys_produce_different_sigs():
    s1 = PromptSigner.with_key(b"key1" * 8)
    s2 = PromptSigner.with_key(b"key2" * 8)
    sig1 = s1.sign("a", "x")
    sig2 = s2.sign("a", "x")
    assert sig1 != sig2


def test_prompt_signer_sign_rejects_empty_name():
    signer = PromptSigner.with_key(b"k" * 32)
    with pytest.raises(ValueError):
        signer.sign("", "anything")


def test_default_signer_reset_for_tests():
    reset_default_signer_for_tests()
    s1 = default_signer()
    s2 = default_signer()
    assert s1 is s2  # singleton
    reset_default_signer_for_tests()
    s3 = default_signer()
    assert s3 is not s1  # fresh instance after reset


def test_prompt_tamper_error_is_runtime_error():
    # Signal-only test — the type exists for agent loop to raise.
    assert issubclass(PromptTamperError, RuntimeError)


def test_prompt_signer_resolves_env_key(monkeypatch):
    monkeypatch.setenv("FPULSE_AI_PROMPT_SIGNING_KEY", "deadbeef" * 8)
    reset_default_signer_for_tests()
    signer = default_signer()
    sig = signer.sign("a", "x")
    # Re-create a signer with the same env-derived key — should produce the same sig
    signer2 = PromptSigner.with_key()
    sig2 = signer2.sign("a", "x")
    assert sig == sig2
    reset_default_signer_for_tests()
