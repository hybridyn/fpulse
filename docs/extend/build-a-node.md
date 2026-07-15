# Add a custom node

F-Pulse ships 40 node types across 6 categories (Source / Destination / Transform / Filter / Control-flow / Storage). When you need a transform that's specific to your domain — apply a custom risk model, hit your internal compliance API mid-pipeline, run a proprietary classifier — you can add a node without touching the F-Pulse core.

This page covers the **OSS path**: nodes added directly to your local install. For a polished plugin format that other F-Pulse installs can drop in unchanged, see [docs/connector-authoring.md](../connector-authoring.md) — the same registration plumbing applies.

## When you need a custom node

You probably need a connector, not a node, if:

- You're integrating with an external system (database / API / file format) → use [Author Connector](build-a-connector.md) instead. Connectors are the right abstraction for "talk to System X."
- You need to ingest from / write to a place F-Pulse doesn't reach yet → connector.

You probably need a node if:

- You have a **transform** that's specific to your data shape and not expressible with the existing transform nodes (derived_column, aggregate, pivot, filter, join, dedupe, sort, unpivot, foreach, conditional, etc.)
- You want to call an **internal microservice** mid-pipeline to enrich rows
- You need a **control-flow primitive** the existing foreach/conditional/router doesn't cover

For everything else, **a SQL transform node or a Python transform node usually solves it without a new node type** — try those first.

## The 5-minute path: Python transform node

The fastest way to add behaviour is to use the built-in **Python transform** node — no registration, no manifest, no restart. It's a node already in the catalog that takes a Python function body and runs it against the input rows.

```python
# In the Python transform node's code field:
def transform(rows):
    for row in rows:
        # call your internal service, apply your custom logic
        row["risk_score"] = compute_risk(row)
    return rows
```

This covers ~80% of "I need a custom node" use cases. If it doesn't cover yours, keep reading.

## The 30-minute path: First-class node type

Use this when:

- The behaviour is reusable across many pipelines
- You want it to show up in the canvas palette with a proper icon / category / param schema
- Other teammates / installs should be able to drop it in without copying Python code into each pipeline

### 1. Pick the base class

In `backend/fpulse/nodes/base.py` you'll find the abstract bases:

| Base | Use when |
|---|---|
| `SourceNode` | Produces rows from outside the pipeline (no input) |
| `DestinationNode` | Consumes rows to outside the pipeline (no output) |
| `TransformNode` | Rows in, rows out |
| `FilterNode` | Rows in, fewer rows out |
| `ControlFlowNode` | Affects step ordering / branching (foreach, conditional, router) |

### 2. Subclass + register

Drop a new file in `backend/fpulse/nodes/`, e.g. `risk_scorer.py`:

```python
from fpulse.nodes.base import TransformNode, register_node
from fpulse.ir.schema import StepType

@register_node(StepType.RISK_SCORER)   # add the enum value in ir/schema.py
class RiskScorerNode(TransformNode):
    """Score each row with the org's risk model."""

    PARAM_SCHEMA = {
        "model_endpoint": {"type": "string", "required": True,
                            "label": "Internal risk-model URL"},
        "threshold": {"type": "number", "default": 0.5,
                       "label": "Flag rows above this score"},
    }

    def execute(self, ctx, params, inputs):
        rows = inputs["in"]
        endpoint = params["model_endpoint"]
        threshold = params.get("threshold", 0.5)
        scored = [{**r, "risk_score": _score(endpoint, r)} for r in rows]
        return {"out": [r for r in scored if r["risk_score"] >= threshold]}
```

### 3. Add the StepType enum value

In `backend/fpulse/ir/schema.py`, add `RISK_SCORER = "risk_scorer"` to the `StepType` enum. This is the only F-Pulse-core file you'll touch — the canvas palette and validator pick it up automatically.

### 4. Add a fixture-based test

In `backend/tests/`, add `test_risk_scorer.py`:

```python
def test_risk_scorer_filters_below_threshold(fpulse_test_db):
    node = RiskScorerNode()
    out = node.execute(
        ctx=None,
        params={"model_endpoint": "http://test", "threshold": 0.8},
        inputs={"in": [{"id": 1, "amount": 100}, {"id": 2, "amount": 50}]},
    )
    assert len(out["out"]) <= 2  # filtered to at-or-above threshold
```

Run it: `pytest backend/tests/test_risk_scorer.py`.

### 5. Reload + use

Restart the backend (or rely on `--reload` if you've enabled it in dev). Your node appears in the canvas palette under the same category as the base class you picked. Drag, configure, run.

## Share your node

Built something reusable? Same paths as connectors:

1. **Internal share** — commit the node file + fixture-test to your fork; teammates get it on next deploy.
2. **Contribute back** — [open a node-contribution PR](https://github.com/hybridyn/fpulse/issues/new/choose). If it's genuinely generic (not org-specific), we ship it first-party.

## See also

- [docs/extend/build-a-connector.md](build-a-connector.md) — when "I need a node" turns out to be "I need a connector"
- [docs/nodes-and-canvas-reference.md](../nodes-and-canvas-reference.md) — full reference for the 40 first-party nodes
- [Request a node](https://github.com/hybridyn/fpulse/issues/new/choose) — if your need is generic enough that we should ship it first-party
