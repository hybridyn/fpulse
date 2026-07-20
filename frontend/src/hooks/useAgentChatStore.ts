/**
 * useAgentChatStore — module-level singleton store for the F-Pulse Agent
 * chat. The store outlives any individual component, so the conversation
 * survives navigation between pages, the widget being closed and reopened,
 * and the editor (which historically hid the widget entirely).
 *
 * Design mirrors `usePageContext`: a tiny event-emitter with no React
 * context provider, since the agent chat is a single global surface.
 *
 * State persisted: turns + open/closed toggle.
 * State NOT persisted across reloads: live-streaming flags inside a turn
 * (status / busy) — those are inherently transient.
 */

import { useCallback, useEffect, useState } from 'react';
import type { AgentRunResponse, TraceStep } from '../api/agent';

export interface ChatTurn {
  id: string;
  role: 'user' | 'agent';
  text: string;
  response?: AgentRunResponse;
  liveSteps?: TraceStep[];
  liveStatus?: string;
  streaming?: boolean;
}

/**
 * Layer 2 — dialogue state. Persists across turns of one chat session.
 * Lets the backend's slot-fill + reference-substitution logic answer
 * follow-ups like "first" / "run it" / "what about the failure?"
 * without re-asking. Cleared on `clear()` and on tab close (sessionStorage).
 */
export interface CaseFile {
  active_entity: { kind: string; id: string; name: string } | null;
  active_intent: { name: string; missing_slot: string | null } | null;
}

const EMPTY_CASE_FILE: CaseFile = { active_entity: null, active_intent: null };

interface AgentChatState {
  turns: ChatTurn[];
  open: boolean;
  /** Pending text to inject into the chat input. Read once by the panel
   *  when it mounts / re-renders, then cleared. Lets ConfigPanel and
   *  other surfaces deep-link "Ask Copilot about this code" prompts. */
  pendingInput: string | null;
  /** Layer 2 dialogue state — sent on every request, replaced by the
   *  backend's response on every turn. */
  caseFile: CaseFile;
}

const _CASE_FILE_KEY = 'fpulse_case_file_v1';

function _loadCaseFile(): CaseFile {
  try {
    const raw = sessionStorage.getItem(_CASE_FILE_KEY);
    if (!raw) return EMPTY_CASE_FILE;
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') {
      return {
        active_entity: parsed.active_entity ?? null,
        active_intent: parsed.active_intent ?? null,
      };
    }
  } catch {
    // ignore — corrupt or unavailable storage falls back to empty
  }
  return EMPTY_CASE_FILE;
}

function _saveCaseFile(cf: CaseFile): void {
  try {
    sessionStorage.setItem(_CASE_FILE_KEY, JSON.stringify(cf));
  } catch {
    // ignore — storage unavailable; in-memory copy is still authoritative
  }
}

let _state: AgentChatState = {
  turns: [],
  open: false,
  pendingInput: null,
  caseFile: _loadCaseFile(),
};
const _listeners = new Set<(s: AgentChatState) => void>();

function _publish(next: AgentChatState): void {
  _state = next;
  _listeners.forEach((l) => {
    try {
      l(next);
    } catch {
      // listener errors must not break other listeners
    }
  });
}

export function setOpen(open: boolean): void {
  if (_state.open === open) return;
  _publish({ ..._state, open });
}

export function setTurns(updater: ChatTurn[] | ((prev: ChatTurn[]) => ChatTurn[])): void {
  const next = typeof updater === 'function' ? updater(_state.turns) : updater;
  _publish({ ..._state, turns: next });
}

export function clearTurns(): void {
  // Clearing the chat also clears dialogue state — the user is starting
  // a new conversation; carrying over an old active_entity would surprise.
  _saveCaseFile(EMPTY_CASE_FILE);
  _publish({ ..._state, turns: [], caseFile: EMPTY_CASE_FILE });
}

export function setCaseFile(cf: CaseFile | null): void {
  const next = cf ?? EMPTY_CASE_FILE;
  _saveCaseFile(next);
  _publish({ ..._state, caseFile: next });
}

export function getCaseFile(): CaseFile {
  return _state.caseFile;
}

export function setPendingInput(text: string | null): void {
  _publish({ ..._state, pendingInput: text });
}

/**
 * Convenience: open the dock + pre-fill the chat input.
 * Used by deep-link buttons like "Ask Copilot about this SQL".
 */
export function askCopilot(prompt: string): void {
  _publish({ ..._state, open: true, pendingInput: prompt });
}

export function getSnapshot(): AgentChatState {
  return _state;
}

/**
 * Subscribe a component to chat-state changes. Returns the current
 * snapshot + setters.
 */
export function useAgentChatStore() {
  const [state, setState] = useState<AgentChatState>(_state);

  useEffect(() => {
    const listener = (s: AgentChatState) => setState(s);
    _listeners.add(listener);
    // Sync immediately in case state changed between render and mount.
    setState(_state);
    return () => {
      _listeners.delete(listener);
    };
  }, []);

  const setOpenStable = useCallback((v: boolean) => setOpen(v), []);
  const setTurnsStable = useCallback(
    (updater: ChatTurn[] | ((prev: ChatTurn[]) => ChatTurn[])) => setTurns(updater),
    [],
  );
  const clearStable = useCallback(() => clearTurns(), []);

  const setCaseFileStable = useCallback((cf: CaseFile | null) => setCaseFile(cf), []);

  return {
    turns: state.turns,
    open: state.open,
    pendingInput: state.pendingInput,
    caseFile: state.caseFile,
    setOpen: setOpenStable,
    setTurns: setTurnsStable,
    setPendingInput,
    clearPendingInput: () => setPendingInput(null),
    setCaseFile: setCaseFileStable,
    clear: clearStable,
    askCopilot,
  };
}
