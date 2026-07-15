// Z32 (2026-05-23) — Pipeline Data Prep wand removed per user feedback.
// This file used to host the dialog that opened when a user clicked the
// magic-wand icon on a Connections-page row. The dialog browsed the
// connection's catalog, let the user pick a stream, then asked the
// backend to scaffold a 3-node draft pipeline (source → wrangler →
// local_table_sink). The frontend wand + backend endpoint
// (POST /api/connections/{id}/scaffold-cleanup) + helper
// (build_connection_cleanup_workflow) were all removed together.
//
// File kept as a tombstone so any lingering import surfaces as a
// clear "module has no default export" error during build instead of
// resurrecting dead UI accidentally on a future rebase.
//
// The Storage-side equivalent (Z1) is unaffected — that's a separate
// feature with its own endpoint at POST /api/storage/scaffold-cleanup.

export {};
