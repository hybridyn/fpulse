/**
 * AIAssistBar / AIAssistFloat — DEAD CODE, removed 2026-05-19
 * (P2 #12 of PAGE_BY_PAGE_AUDIT.md).
 *
 * The previous per-page floating widget that hit `POST /api/ai/page-assist`
 * is superseded by `FloatingAgentWidget` (mounted in App.tsx) which uses
 * the tool-using agent loop instead of a deterministic per-page text
 * helper. The old file shipped ~440 lines of UI code that no caller
 * imported — verified via grep before deletion.
 *
 * Two comment-only references to the old name remain in
 * `components/agent/AgentChatPanel.tsx` and `api/agent.ts`; they are
 * historical commentary explaining why the new widget exists and can
 * stay until the next round of comment cleanup.
 *
 * If you arrived here looking for the page-assist behaviour:
 *   - `useEmbeddedAI()` (`hooks/useEmbeddedAI.ts`) — deterministic
 *     fallback layer used by ConfigPanel autofill / AI Fix / Ghost node.
 *   - `FloatingAgentWidget` — the tool-using Copilot dock.
 */

export {};
