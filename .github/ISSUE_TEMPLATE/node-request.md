---
name: 🧩 Node request
about: Request a new transform / control-flow / utility node
title: "[Node Request] <node name>"
labels: ["node-request", "needs-triage"]
assignees: []
---

> **Before you file:** could the existing **SQL Transform** (DuckDB) node solve this? It's a catch-all for custom logic that doesn't require us to ship anything new. See [docs/extend/build-a-node.md](https://github.com/hybridyn/fpulse/blob/main/docs/extend/build-a-node.md). If you've tried it and it's not the right shape, please continue.

## What node do you need?

<!-- e.g. "Sliding-window join", "Bloom-filter dedup", "Type-2 SCD writer", "Approval-gate that pauses pipeline" -->

## Which existing nodes are closest, and why don't they cover it?

<!-- e.g. "aggregate node can group but can't do windowed aggregations" -->

## Inputs + outputs

- **Inputs:** <!-- e.g. "two row streams: left + right" -->
- **Outputs:** <!-- e.g. "one stream of matched rows" -->
- **Parameters the node would expose:** <!-- e.g. "join key, window size, late-arrival tolerance" -->

## Example pipeline that needs it

<!-- Paste a JSON snippet OR describe the upstream + downstream nodes. -->

## Sample input → expected output

```
Input rows:
  ...

Expected output:
  ...
```

## Category

- [ ] Source (no input, produces rows)
- [ ] Destination (consumes rows, no output)
- [ ] Transform (rows in → rows out)
- [ ] Filter (rows in → fewer rows out)
- [ ] Control-flow (affects step ordering)
- [ ] Other:

## Anything else

<!-- References to how other orchestrators / ETL tools handle this would help. -->
