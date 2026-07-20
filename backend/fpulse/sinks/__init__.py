"""
fpulse.sinks — sink-side cross-cutting helpers (idempotency, retries, etc.).

Why this package exists alongside ``fpulse.nodes`` (which holds the actual
sink ``BaseNode`` classes):

  • ``fpulse.nodes.sinks`` and ``fpulse.nodes.flow_control`` contain
    node *executors*. They are loaded at workflow-execution time and
    register themselves in the IR's StepType-keyed registry. Putting a
    cross-cutting concern (the dedupe store) inside those files would
    couple it to a single sink module and force everyone else (tests,
    background jobs, API handlers) to import the registry just to ask
    "have we sent this email before?".

  • ``fpulse.sinks`` is the shared concern surface for *all* external
    sinks — email, webhook, api, kafka, slack. The dedupe store lives
    here because the storage is shared (a single ``sink_idempotency``
    table inside ``fpulse.db``) and the lookup helper is shared (one
    ``compute_row_hash`` + ``should_skip`` pair used by every sink
    class).

Public API:
    dedupe_store.get_dedupe_store()          → IdempotencyDedupeStore
    idempotency_helper.compute_row_hash(...) → str (sha256 hex)
    idempotency_helper.should_skip(...)      → (bool, str)
"""
