/**
 * Deterministic client-side conversation summarizer.
 *
 * Builds the `summary` string that ships in `AgentRequest.conversation`
 * when the chat history is longer than the recent window. Runs purely
 * on the frontend — no extra LLM call, no extra latency.
 *
 * Strategy:
 *
 *   - The frontend already sends the last N (default 10) turns verbatim
 *     as `recent_turns`. This summarizer is concerned only with the
 *     OLDER tail that gets rolled out of that window.
 *   - For each older user turn, extract a one-line "intent" — the first
 *     sentence, capped to 140 chars.
 *   - For each older assistant turn, extract a one-line "key point" —
 *     the first non-trivial line (skipping the leading "Sure,"-style
 *     fillers), capped to 140 chars.
 *   - Bullet the result. Cap the whole summary at 1200 chars so it
 *     doesn't blow the backend's hard cap.
 *
 * Lossy on purpose. The recent window has the verbatim text; the
 * summary just gives the model "here's what we've already discussed"
 * without re-injecting the entire chat history.
 *
 * If you want richer summaries later, an LLM-backed summarizer can
 * replace this — same function signature, swap implementations behind
 * a feature flag. The current version is the cheapest thing that beats
 * `summary: ''`.
 */

export interface ChatTurn {
  role: 'user' | 'assistant';
  content: string;
}

const PER_BULLET_MAX = 140;
const TOTAL_SUMMARY_MAX = 1200;

// Common assistant-reply openers that don't carry information. Stripped
// from the start of an assistant turn before extracting the key point.
const FILLER_PREFIXES = [
  'sure',
  'sure!',
  'sure,',
  'here you go',
  'here is',
  'here are',
  "here's",
  'okay',
  'ok',
  'absolutely',
  'of course',
  'great',
  'got it',
];


function extractFirstSentence(text: string, maxLen = PER_BULLET_MAX): string {
  const trimmed = text.replace(/\s+/g, ' ').trim();
  if (!trimmed) return '';
  // Find the first sentence boundary; fall back to the full line if none.
  const m = trimmed.match(/^(.*?[.!?])(\s|$)/);
  const first = (m ? m[1] : trimmed);
  if (first.length <= maxLen) return first;
  return first.slice(0, maxLen).replace(/\s+\S*$/, '') + '…';
}


function stripFiller(text: string): string {
  const lower = text.trim().toLowerCase();
  for (const filler of FILLER_PREFIXES) {
    if (lower.startsWith(filler)) {
      const rest = text.trim().slice(filler.length).trim();
      // Drop a leading punctuation mark left behind by the strip.
      return rest.replace(/^[,.!:;\-—]\s*/, '');
    }
  }
  return text;
}


/**
 * Summarize the `older` portion of a conversation into a bullet list.
 *
 * Empty input → empty string (backend renders nothing).
 *
 * Use this when the frontend has already sliced out the recent-turns
 * window: pass everything that fell off the end into `older`.
 */
export function summarizeOlderTurns(older: ChatTurn[]): string {
  if (!older || older.length === 0) return '';

  const bullets: string[] = [];
  for (const turn of older) {
    const content = (turn.content || '').trim();
    if (!content) continue;
    if (turn.role === 'user') {
      const intent = extractFirstSentence(content);
      if (intent) bullets.push(`User asked: ${intent}`);
    } else if (turn.role === 'assistant') {
      const stripped = stripFiller(content);
      const point = extractFirstSentence(stripped);
      if (point) bullets.push(`Assistant said: ${point}`);
    }
  }

  if (bullets.length === 0) return '';

  // Compose the summary, hard-capping at TOTAL_SUMMARY_MAX. If we'd
  // exceed the cap, keep the EARLIEST bullets (they're the oldest
  // context the model would otherwise have no clue about) and trim
  // newer ones — the verbatim recent_turns window already covers
  // anything close to the present.
  const body: string[] = [];
  let used = 0;
  for (const b of bullets) {
    const line = `- ${b}\n`;
    if (used + line.length > TOTAL_SUMMARY_MAX) break;
    body.push(line);
    used += line.length;
  }
  return body.join('').trimEnd();
}


/**
 * Convenience: given the FULL chat history + the recent-turn window
 * size, build both halves of the conversation payload.
 *
 * Returns:
 *   - recent_turns: the last `recentWindow` turns, verbatim, filtered
 *     to non-empty content.
 *   - summary: deterministic bullet summary of everything BEFORE that
 *     window (empty string when the full history fits in the window).
 */
export function buildConversationPayload(
  allMessages: Array<{ role: string; content: string }>,
  recentWindow = 10,
): { recent_turns: ChatTurn[]; summary: string } {
  const norm: ChatTurn[] = allMessages
    .map((m) => ({
      role: (m.role === 'assistant' ? 'assistant' : 'user') as ('user' | 'assistant'),
      content: String(m.content || ''),
    }))
    .filter((t) => t.content.trim().length > 0);

  if (norm.length <= recentWindow) {
    return { recent_turns: norm, summary: '' };
  }

  const older = norm.slice(0, norm.length - recentWindow);
  const recent = norm.slice(-recentWindow);
  return {
    recent_turns: recent,
    summary: summarizeOlderTurns(older),
  };
}
