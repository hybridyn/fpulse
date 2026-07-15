"""Tests for fpulse.ai.draft_diff.compute_diff.

The diff backs the Copilot ConfirmationCard's DiffPreview UX — users
must see exactly what will change before clicking Confirm.
"""

from __future__ import annotations

from fpulse.ai.draft_diff import compute_diff


def _ir(steps=None, conns=None, **kwargs):
    """Convenience IR builder."""
    return {
        "id": kwargs.get("id", "wf-1"),
        "name": kwargs.get("name", "pipeline_a"),
        "steps": steps or [],
        "connections": conns or [],
        **{k: v for k, v in kwargs.items() if k not in ("id", "name")},
    }


# ── New pipeline (before is None) ─────────────────────────────────────────


def test_new_pipeline_all_additions():
    after = _ir(
        steps=[
            {"id": "s1", "type": "csv_source", "label": "Daily CSV"},
            {"id": "s2", "type": "filter", "label": "Drop nulls"},
            {"id": "s3", "type": "postgres_sink", "label": "Save to PG"},
        ],
        conns=[
            {"from_step": "s1", "to_step": "s2"},
            {"from_step": "s2", "to_step": "s3"},
        ],
    )
    d = compute_diff(before_ir=None, after_ir=after)
    assert d.is_new_pipeline is True
    assert d.steps_added == 3
    assert d.steps_removed == 0
    assert d.steps_modified == 0
    assert d.connections_added == 2
    assert d.connections_removed == 0
    # All step_changes are kind="add"
    assert {c.kind for c in d.step_changes} == {"add"}
    # Labels propagate so the UI can show "Daily CSV" not "s1"
    labels = {c.label for c in d.step_changes}
    assert "Daily CSV" in labels and "Drop nulls" in labels


def test_empty_before_treated_as_new():
    """before_ir with no steps == new pipeline."""
    d = compute_diff(
        before_ir=_ir(steps=[]),
        after_ir=_ir(steps=[{"id": "s1", "type": "csv_source"}]),
    )
    assert d.is_new_pipeline is True
    assert d.steps_added == 1


# ── Modification — step add ───────────────────────────────────────────────


def test_modification_adds_one_step():
    before = _ir(
        steps=[
            {"id": "s1", "type": "csv_source"},
            {"id": "s2", "type": "postgres_sink"},
        ],
        conns=[{"from_step": "s1", "to_step": "s2"}],
    )
    after = _ir(
        steps=[
            {"id": "s1", "type": "csv_source"},
            {"id": "filter1", "type": "filter", "label": "Drop nulls"},
            {"id": "s2", "type": "postgres_sink"},
        ],
        conns=[
            {"from_step": "s1", "to_step": "filter1"},
            {"from_step": "filter1", "to_step": "s2"},
        ],
    )
    d = compute_diff(before_ir=before, after_ir=after)
    assert d.is_new_pipeline is False
    assert d.steps_added == 1
    assert d.steps_removed == 0
    assert d.steps_modified == 0
    assert d.connections_added == 2     # 2 new edges
    assert d.connections_removed == 1   # 1 edge removed
    add_changes = [c for c in d.step_changes if c.kind == "add"]
    assert len(add_changes) == 1
    assert add_changes[0].step_id == "filter1"
    assert add_changes[0].label == "Drop nulls"


# ── Modification — step remove ────────────────────────────────────────────


def test_modification_removes_step():
    before = _ir(
        steps=[
            {"id": "s1", "type": "csv_source"},
            {"id": "s2", "type": "validate"},
            {"id": "s3", "type": "postgres_sink"},
        ],
    )
    after = _ir(
        steps=[
            {"id": "s1", "type": "csv_source"},
            {"id": "s3", "type": "postgres_sink"},
        ],
    )
    d = compute_diff(before_ir=before, after_ir=after)
    assert d.steps_removed == 1
    rm = [c for c in d.step_changes if c.kind == "remove"]
    assert len(rm) == 1
    assert rm[0].step_id == "s2"


# ── Modification — param change ───────────────────────────────────────────


def test_modification_param_change_surfaces_keys_not_values():
    before = _ir(
        steps=[{
            "id": "s1", "type": "postgres_source", "label": "PG",
            "params": {"connection_id": "old-conn", "schema": "public", "table": "orders"},
        }],
    )
    after = _ir(
        steps=[{
            "id": "s1", "type": "postgres_source", "label": "PG",
            "params": {"connection_id": "new-conn", "schema": "public", "table": "orders"},
        }],
    )
    d = compute_diff(before_ir=before, after_ir=after)
    assert d.steps_modified == 1
    mod = [c for c in d.step_changes if c.kind == "modify"]
    assert len(mod) == 1
    # The KEY connection_id is surfaced; the VALUE (which could contain
    # credentials) is NOT. This is the trust contract.
    assert mod[0].changed_param_keys == ["connection_id"]


def test_modification_label_change_surfaced():
    before = _ir(steps=[{"id": "s1", "type": "csv_source", "label": "Old name"}])
    after = _ir(steps=[{"id": "s1", "type": "csv_source", "label": "New name"}])
    d = compute_diff(before_ir=before, after_ir=after)
    assert d.steps_modified == 1
    mod = d.step_changes[0]
    assert mod.kind == "modify"
    assert "__label" in mod.changed_param_keys


def test_modification_no_meaningful_change():
    """before == after produces an empty diff."""
    ir = _ir(steps=[{"id": "s1", "type": "csv_source", "params": {"path": "a.csv"}}])
    d = compute_diff(before_ir=ir, after_ir=ir)
    assert d.steps_added == 0
    assert d.steps_removed == 0
    assert d.steps_modified == 0
    assert d.connections_added == 0
    assert d.connections_removed == 0
    assert d.step_changes == []


# ── Serialization ─────────────────────────────────────────────────────────


def test_to_jsonable_shape():
    d = compute_diff(
        before_ir=None,
        after_ir=_ir(steps=[{"id": "s1", "type": "csv_source", "label": "X"}]),
    )
    payload = d.to_jsonable()
    assert payload["is_new_pipeline"] is True
    assert payload["steps_added"] == 1
    assert isinstance(payload["step_changes"], list)
    assert payload["step_changes"][0]["kind"] == "add"
    assert payload["step_changes"][0]["step_id"] == "s1"
    assert payload["step_changes"][0]["changed_param_keys"] == []
