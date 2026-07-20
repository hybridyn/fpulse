/**
 * idempotency.ts — single source of truth for "what happens when
 * you re-run this step?"
 *
 * Every sink in F-Pulse falls into one of five idempotency classes.
 * The class is a function of (stepType, params), not stepType alone —
 * `local_table_sink` is `safe` in replace mode, `merge` in merge mode,
 * `append_risky` in append mode. Don't try to read it from stepType
 * directly; always call classifyIdempotency().
 *
 * Why this exists:
 *   1. The canvas shows a small badge on each sink so the user
 *      knows whether a re-run is safe BEFORE clicking Run.
 *   2. The config panel surfaces the full label + reasoning.
 *   3. Pre-publish checks warn when an `append` sink is wired into
 *      a scheduled pipeline without dedup downstream.
 *   4. Backfill mode (future) treats `safe` and `merge` as backfill-
 *      compatible, gates `append_risky` and `external` behind a
 *      confirmation dialog.
 *
 * The class is presentation-aware (we choose what's safe to RE-RUN,
 * not what's safe to RUN). A first-time run of an `email_sink` is
 * fine; a SECOND run sends a second email. That's the distinction.
 */

export type IdempotencyClass =
  | 'safe'           // Fully idempotent — re-run produces identical state.
  | 'replace'        // Destructive but idempotent — overwrites existing data.
  | 'merge'          // Idempotent IF user set merge keys; warn otherwise.
  | 'append_risky'   // NOT idempotent — re-runs duplicate data.
  | 'external';      // NOT idempotent — real-world side effects per run.

export interface IdempotencyInfo {
  cls: IdempotencyClass;
  /** One-line label for the canvas badge. */
  label: string;
  /** Detailed explanation shown in the config panel / tooltip. */
  detail: string;
  /** Compact emoji-free icon variant for tight UI; matches semantic. */
  tone: 'green' | 'amber' | 'red' | 'slate';
}


// ─── Class definitions ──────────────────────────────────────────


const SAFE: IdempotencyInfo = {
  cls: 'safe',
  label: 'Safe to rerun',
  detail:
    'Re-running this step produces the same result. Output is fully ' +
    'derived from inputs with no time-of-run dependencies.',
  tone: 'green',
};

const REPLACE: IdempotencyInfo = {
  cls: 'replace',
  label: 'Replaces target',
  detail:
    'Each run overwrites the destination. Idempotent for identical ' +
    'inputs, but the previous state is lost. Safe to rerun, but ' +
    'destructive — there is no automatic backup.',
  tone: 'amber',
};

const MERGE_OK: IdempotencyInfo = {
  cls: 'merge',
  label: 'Merge (upsert)',
  detail:
    'Rows are matched by the configured merge keys and updated in ' +
    'place; new rows are inserted. Idempotent as long as the keys ' +
    'uniquely identify rows.',
  tone: 'green',
};

const MERGE_KEYLESS: IdempotencyInfo = {
  cls: 'merge',
  label: 'Merge — no keys set',
  detail:
    'Merge mode is selected but no merge keys are configured. ' +
    'Without keys this falls back to append, which duplicates rows ' +
    'on every re-run. Set merge_on / upsert_keys in the step config.',
  tone: 'amber',
};

const APPEND: IdempotencyInfo = {
  cls: 'append_risky',
  label: 'Re-runs duplicate',
  detail:
    'Append mode adds rows on every run. Re-running this pipeline ' +
    'will create duplicate data downstream. Use replace or merge ' +
    'mode if the pipeline runs on a schedule.',
  tone: 'red',
};

const EXTERNAL: IdempotencyInfo = {
  cls: 'external',
  label: 'External side effect',
  detail:
    'Each run performs a real-world action (sends an email, posts ' +
    'to a webhook, publishes to a queue). Re-running fires it again. ' +
    'Wrap in a manual approval or guard with an idempotency key.',
  tone: 'red',
};


// ─── Classifier ─────────────────────────────────────────────────


/**
 * Returns the idempotency class for a sink given its current
 * params. Returns null for non-sink step types (transforms,
 * sources) — their idempotency is implicit in the sink they
 * eventually feed.
 */
/**
 * Explicit author override for the badge. Set
 * `params.idempotent_override = true` on a sink when an upstream step
 * (typically an `execute_sql_task` with `TRUNCATE` / `DELETE FROM` /
 * `DROP TABLE`) guarantees idempotency that the static classifier
 * can't see. The override is shown in the config panel with a
 * "🟢 Marked safe by author — upstream guard attested" message so
 * reviewers know an attestation was made and by whom.
 *
 * Use case: pipeline 18 in the OSS samples runs
 *   `execute_sql_task(TRUNCATE TABLE products_staging)`
 * immediately before a `warehouse_sink(mode=append)`. The net effect
 * is idempotent (truncate → load) but the badge classifier scans the
 * sink in isolation and (correctly, for the sink alone) labels it
 * `append_risky`. Toggling `idempotent_override` flips the badge to
 * `safe` while leaving the underlying classifier honest.
 */
const SAFE_AUTHOR_OVERRIDE: IdempotencyInfo = {
  cls: 'safe',
  label: 'Safe — author override',
  detail:
    'Pipeline author has attested that an upstream guard (e.g. a ' +
    'TRUNCATE / DELETE / DROP via execute_sql_task) makes this sink ' +
    'idempotent across re-runs. The badge classifier can\'t verify ' +
    'cross-step orchestration on its own, so this override is on ' +
    'trust — review the upstream guard before relying on it.',
  tone: 'green',
};

export function classifyIdempotency(
  stepType: string,
  params: any = {},
): IdempotencyInfo | null {
  // 2026-05-26 — author override. Honoured on every sink type so the
  // workflow IR has one consistent way to say "trust me, this is safe
  // because of upstream orchestration the classifier can't see".
  if (params?.idempotent_override === true) return SAFE_AUTHOR_OVERRIDE;

  // Mode-driven sinks: behaviour depends on params.mode.
  const mode = String(params?.mode || params?.write_mode || '').toLowerCase();

  switch (stepType) {
    case 'local_table_sink':
    case 'parquet_sink':
    case 'delta_sink':
    case 'warehouse_sink':
    case 'db_sink': {
      // 2026-05-26 — added 'create' and 'truncate' to the REPLACE
      // bucket. The warehouse_sink backend does CREATE OR REPLACE
      // TABLE for mode=create and DELETE+INSERT for mode=truncate;
      // both fully replace the destination each run and are therefore
      // idempotent. The previous classifier missed these and showed
      // a misleading red "Re-runs duplicate" badge on every demo
      // pipeline shipped with mode=create.
      if (mode === 'replace' || mode === 'overwrite' || mode === 'create' || mode === 'truncate') return REPLACE;
      if (mode === 'merge' || mode === 'upsert') {
        const keys = params?.merge_on || params?.upsert_keys || params?.keys;
        return Array.isArray(keys) && keys.length > 0 ? MERGE_OK : MERGE_KEYLESS;
      }
      if (mode === 'append' || mode === '') return APPEND;
      // Unknown mode — default to the safest assumption.
      return APPEND;
    }

    // File sinks: writing to a path overwrites by default.
    case 'csv_sink':
    case 'json_sink':
    case 'excel_sink':
    case 'file_sink':
    case 's3_sink':
    case 'adls_gen2_sink':
    case 'azure_blob_sink':
    case 'gcs_sink':
    case 'sharepoint_sink':
    case 'onedrive_sink':
    case 'gdrive_sink':
    case 'dropbox_sink':
    case 'box_sink':
      // Append-to-file mode (rare for these) flips the class.
      if (mode === 'append') return APPEND;
      return REPLACE;

    // External side-effect sinks: every run is a fresh real-world
    // action; idempotency requires an external dedupe (idempotency
    // key, message id, etc.) that F-Pulse cannot guarantee on its own.
    case 'email_sink':
    case 'api_sink':
    case 'kafka_sink':
    case 'webhook_sink':
      return EXTERNAL;

    // Generic placeholders that route via config — treat as
    // mode-driven; default to append (safest pessimistic call).
    case 'output':
    case 'destination':
      if (mode === 'replace' || mode === 'overwrite') return REPLACE;
      if (mode === 'merge' || mode === 'upsert') return MERGE_KEYLESS;
      return APPEND;

    default:
      return null;
  }
}


/**
 * Lookup tone → Tailwind class fragment. Kept here so call sites
 * don't sprinkle duplicate color tables.
 */
export const IDEMPOTENCY_TONE_CLASSES: Record<IdempotencyInfo['tone'], {
  bg: string;
  text: string;
  border: string;
  dot: string;
}> = {
  green: {
    bg: 'bg-emerald-50',
    text: 'text-emerald-700',
    border: 'border-emerald-200',
    dot: 'bg-emerald-500',
  },
  amber: {
    bg: 'bg-amber-50',
    text: 'text-amber-700',
    border: 'border-amber-200',
    dot: 'bg-amber-500',
  },
  red: {
    bg: 'bg-red-50',
    text: 'text-red-700',
    border: 'border-red-200',
    dot: 'bg-red-500',
  },
  slate: {
    bg: 'bg-slate-50',
    text: 'text-slate-600',
    border: 'border-slate-200',
    dot: 'bg-slate-400',
  },
};
