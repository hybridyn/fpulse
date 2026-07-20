# F-Pulse Steward — Custom rules (YAML)

The Steward ships two built-in detectors today (`duplicate_source`,
`duplicate_pipeline`). The **user-defined rules engine** lets admins
write additional detectors as YAML files — no code, no plugins, no
fork.

Rules live in `<data_dir>/steward/<workspace>/rules/` as `.yaml` files
(one rule per file). The Steward discovers them on every scan; matches
become regular findings that flow through the same surface as
Archeologist findings — eye-icon dropdown, notification bell,
dismiss-with-reason, time-clamped escalation, rebound detection,
de-dup invariant.

## Why YAML, not Python plugins

| Option | Why we didn't pick it |
|---|---|
| Python plugin directory | Admin code = arbitrary execution inside the backend process. Can shell out, exfiltrate credentials, crash the server. Enterprise procurement rejects this on day one. |
| Embedded scripting (WASM / Rhai) | Safer than Python, more expressive than YAML — but adds a heavy runtime dependency in a language nobody uses elsewhere in F-Pulse. Wrong cost/benefit. |
| UI-only rule builder | Looks friendly until you need to GitOps it, code-review it, or diff it across environments. Falls apart for serious teams. |

YAML wins because: it's data (no code execution); GitOps-native
(rules sit in your repo, reviewed via PRs, promoted staging→prod like
any other config); reviewable by non-engineers (a team lead can read
the YAML and understand it); reuses the existing `StewardFinding`
contract (no new surface to build); and matches industry precedent
(Prometheus alerts, Datadog monitors, Soda Core, dbt tests).

## Anatomy of a rule

A complete rule file:

```yaml
# .fpulse/rules/prod_without_dev.yaml
id: prod_without_dev               # filesystem-safe; becomes the finding-id prefix
title: "Pipeline writes to prod with no dev counterpart"
description: |
  Each production pipeline should have a dev-environment equivalent
  so changes can be verified before they hit production data.
level: governance                  # one of: pipeline, node, connector, data, architecture, governance, cost
severity: p2                       # p1 (page), p2 (in-app + notify), p3 (digest)
confidence: high                   # high, medium, low — surfaces as a chip in the UI
enabled: true                      # set false to keep the rule but disable it temporarily
match:
  has_node:                        # at least one node satisfies all these constraints
    type: db_sink
    params_eq:
      environment: prod
  lacks_node:                      # NO node satisfies these constraints (absence detection)
    type: db_sink
    params_eq:
      environment: dev
recommend:                         # rendered as bullet list in the finding body
  - "Create a dev counterpart pipeline before this lands"
  - "Or tag the pipeline `dev_required: false` to suppress"
```

## The matcher DSL

A rule has one top-level `match:` block. All present fields are
AND-combined.

### Workflow-level constraints

| Field | Type | Meaning |
|---|---|---|
| `name_contains` | string | Substring of workflow name (case-insensitive) |
| `has_node` | NodeMatch | At least ONE node satisfies the inner constraints |
| `lacks_node` | NodeMatch | NO node satisfies the inner constraints (absence) |
| `node_count_min` | int | Workflow has ≥ N nodes |
| `node_count_max` | int | Workflow has ≤ N nodes |

### Node-level constraints (inside `has_node` / `lacks_node`)

| Field | Type | Meaning |
|---|---|---|
| `type` | string | Exact step-type match (`db_sink`, `csv_source`, …) |
| `type_in` | list[string] | Step type must be one of |
| `type_endswith` | string | Step type ends with this suffix (convenience for `_source` / `_sink`) |
| `params_eq` | dict | For every key: `str(params[k]) == str(v)` |
| `params_in` | dict | For every key: `params[k]` is in the supplied list |
| `params_contains` | dict | For every key: substring `v` appears in `str(params[k])` |

Both F-Pulse step format (`type`/`params` at the top level) and React
Flow node format (`data.stepType` / `data.params`) are supported
automatically — same dual-format handling as the Archeologist detector.

## Useful patterns

### Detect prod-only pipelines (the canonical governance rule)

Already shown above.

### Flag any pipeline reading from the `raw/` zone without staging

```yaml
id: raw_zone_without_staging
title: "Pipeline reads from raw/ without writing to staging/"
level: data
severity: p3
match:
  has_node:
    type_endswith: _source
    params_contains:
      file_path: /raw/
  lacks_node:
    type_endswith: _sink
    params_contains:
      file_path: /staging/
recommend:
  - "Insert an intermediate landing step that writes to /staging/"
```

### Catch large pipelines (architectural smell)

```yaml
id: oversized_pipeline
title: "Pipeline has more than 30 nodes — consider splitting"
level: architecture
severity: p3
confidence: medium
match:
  node_count_min: 31
recommend:
  - "Split into composable sub-pipelines for testability"
  - "Or document why this needs to stay monolithic"
```

### Block writes to a banned destination

```yaml
id: no_writes_to_legacy_warehouse
title: "Pipeline writes to the deprecated legacy warehouse"
level: governance
severity: p1                       # this one DOES page
match:
  has_node:
    type: db_sink
    params_in:
      connection_id: [legacy_dw, legacy_dw_prod, legacy_dw_v2]
recommend:
  - "Migrate destination to the new warehouse"
  - "Reach out to data-platform if blocked"
```

## What happens when a rule matches

For every (rule, workflow) pair that matches, the Steward produces
one finding:

- `kind` = `user_defined`
- `level` = whatever the rule declared
- `severity` = whatever the rule declared
- `title` = the rule's `title`
- `body` = the rule's `description` + a "Recommended actions" bullet
  list from `recommend`
- `evidence.rule_id` = the rule's id (lets the UI group user-rule
  findings by which rule produced them)
- `evidence.workflow_id` + `evidence.workflow_name` = which pipeline
- `evidence.source_signature` = `user_rule:<rule_id>:<workflow_id>`
  (unique per (rule, workflow) pair, so a user can dismiss ONE match
  of a rule without silencing every match)

## Operational behaviour

User-rule findings get the **same** alert-fatigue guarantees as
built-in findings:

- **Time-clamped escalation.** Won't escalate to P1 until both a
  count threshold and a min-hours-since-first window pass.
- **Notification de-dup.** At-most-one notification per
  `(user, finding, severity, rebound)`.
- **Dismiss resets the counter.** A finding the operator marked
  intentional that returns later rebuilds escalation from zero.
- **Rebound detection.** Previously resolved → returns → tagged
  `rebounded` with `previously_resolved_at` evidence.
- **Suppression by signature.** Dismiss the finding with a reason →
  that `source_signature` joins the workspace's
  `suppressed_signatures` list and won't fire again.

## Failure modes (we surface them)

The loader returns load errors alongside the rules so the UI can
show admins WHY a rule isn't taking effect:

```
GET /api/steward/rules
  → {
      "count": 4,
      "rules": [ ... 4 successful rules ... ],
      "errors": [
        {
          "path": ".../rules/typo.yaml",
          "message": "rule id 'Bad ID' must match ^[a-z0-9]...$"
        }
      ]
    }
```

A bad rule never silences the good rules in the same directory.

## OSS vs Plus split

| Capability | OSS | Plus |
|---|---|---|
| YAML schema | ✓ | ✓ |
| Rule evaluator runtime | ✓ | ✓ |
| Rules from `<data_dir>/steward/<ws>/rules/*.yaml` on disk | ✓ | ✓ |
| `GET /api/steward/rules` (read-only listing + errors) | ✓ | ✓ |
| In-app authoring UI (editor with validation + preview) | — | ✓ |
| Rule library / catalog (browse community-contributed rules) | — | ✓ |
| SQL escape hatch (DuckDB query over pipeline metadata) | — | ✓ |
| Cross-workspace rule sharing | — | ✓ |
| RBAC on who can author rules | — | ✓ |
| Two-gate approval before a rule activates | — | ✓ |

The capability is in OSS so power users aren't locked out and the
format is forkable. Plus adds the polished product experience around
the capability — same shape as how OSS gets Steward and Plus adds
cross-workspace correlation around it.

## See also

- [`overview.md`](overview.md) — what Steward is + the 7-level finding contract
- [`architecture.md`](architecture.md) — the 7 hard architectural rules
- [`memory-layer.md`](memory-layer.md) — the gated lesson store
- [`positioning.md`](positioning.md) — 4-pillar product framing
