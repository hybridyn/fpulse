/**
 * requireNamedWorkflow — DELETED 2026-05-19 (OSS-8 of PAGE_BY_PAGE_AUDIT.md).
 *
 * The locked 2026-05-09 "no silent pipeline create" rule now lives inside
 * the workflow store's `ensureWorkflow({ allowCreate: true })` action.
 * Callers should invoke `ensureWorkflow` directly and treat a null return
 * as "user cancelled OR backend create failed — abort the surrounding
 * flow". The previous 3 callsites (Toolbar Save / ConfigPanel Test Node /
 * Canvas Sample) have been migrated.
 *
 * If you arrived here looking for the helper:
 *   - Use `useWorkflowStore.getState().ensureWorkflow({ allowCreate: true })`.
 *   - Check the return value: `null` means abort, a string id means saved.
 *
 * The file is kept as a stub so a stale import doesn't error at build
 * time, but no runtime export is provided.
 */

export {};
