"""Tests for fpulse.ai.ai_metrics.AIMetricsStore.

In-process counter store backing GET /api/metrics/ai. Verifies the
counters increment correctly, the per-lane averages are right, the
date-rollover reset works, and the unknown-lane bucket is preserved
for operator visibility.
"""

from __future__ import annotations

from fpulse.ai.ai_metrics import AIMetricsStore


def test_fresh_store_is_empty():
    store = AIMetricsStore()
    snap = store.get_snapshot()
    assert snap.total_requests == 0
    assert snap.fallback_hits == 0
    assert snap.escalations == 0
    # Every known lane is present with zeros so the UI can render even
    # before any traffic.
    for lane in ("fast_lane", "hybrid", "single_shot", "agent_loop"):
        assert snap.per_lane[lane]["count"] == 0
        assert snap.per_lane[lane]["avg_latency_ms"] == 0.0


def test_record_request_increments_counters():
    store = AIMetricsStore()
    store.record_request(lane="fast_lane", latency_ms=120)
    store.record_request(lane="fast_lane", latency_ms=80)
    store.record_request(
        lane="hybrid",
        latency_ms=4500,
        tool_hops=1,
        tokens_in=200,
        tokens_out=80,
    )
    snap = store.get_snapshot()
    assert snap.total_requests == 3
    assert snap.per_lane["fast_lane"]["count"] == 2
    assert snap.per_lane["fast_lane"]["avg_latency_ms"] == 100.0  # (120+80)/2
    assert snap.per_lane["hybrid"]["count"] == 1
    assert snap.per_lane["hybrid"]["tokens_in_total"] == 200
    assert snap.per_lane["hybrid"]["tokens_out_total"] == 80
    assert snap.per_lane["hybrid"]["avg_tool_hops"] == 1.0


def test_record_fallback():
    store = AIMetricsStore()
    store.record_fallback()
    store.record_fallback()
    snap = store.get_snapshot()
    assert snap.fallback_hits == 2


def test_record_escalation():
    store = AIMetricsStore()
    store.record_escalation()
    snap = store.get_snapshot()
    assert snap.escalations == 1


def test_unknown_lane_surfaces_in_snapshot():
    """Operator visibility: a new lane added without updating the enum
    should still appear in the snapshot so it gets noticed."""
    store = AIMetricsStore()
    store.record_request(lane="experimental_lane", latency_ms=50)
    snap = store.get_snapshot()
    assert "experimental_lane" in snap.per_lane
    assert snap.per_lane["experimental_lane"]["count"] == 1


def test_reset_clears_state():
    store = AIMetricsStore()
    store.record_request(lane="agent_loop", latency_ms=3000)
    store.record_fallback()
    store.record_escalation()
    store.reset()
    snap = store.get_snapshot()
    assert snap.total_requests == 0
    assert snap.fallback_hits == 0
    assert snap.escalations == 0


def test_negative_inputs_are_clamped():
    """Defensive: callers may pass weird timings from clock skew."""
    store = AIMetricsStore()
    store.record_request(
        lane="agent_loop", latency_ms=-50,
        tool_hops=-1, tokens_in=-100, tokens_out=-50,
    )
    snap = store.get_snapshot()
    lane = snap.per_lane["agent_loop"]
    assert lane["avg_latency_ms"] == 0.0
    assert lane["avg_tool_hops"] == 0.0
    assert lane["tokens_in_total"] == 0
    assert lane["tokens_out_total"] == 0


def test_concurrent_writes_are_safe():
    """Threaded smoke test — store must survive concurrent recorders."""
    import threading
    store = AIMetricsStore()

    def _writer():
        for _ in range(100):
            store.record_request(lane="agent_loop", latency_ms=10)

    threads = [threading.Thread(target=_writer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = store.get_snapshot()
    assert snap.total_requests == 8 * 100
    assert snap.per_lane["agent_loop"]["count"] == 800
