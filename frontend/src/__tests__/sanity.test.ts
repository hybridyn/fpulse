/**
 * Sanity smoke — proves the vitest pipeline actually runs end-to-end
 * (config resolves, jsdom boots, setup file imports, assertions fire).
 *
 * When real tests land, this file can stay or be deleted — it has no
 * dependencies on application code, so it's a stable canary for the
 * test infrastructure itself rather than the product.
 */
import { describe, it, expect } from 'vitest';
import { isOllamaToolCapable, isOllamaBelowToolUseFloor, OLLAMA_CPU_RECOMMENDATION } from '../util/aiModels';
import { buildConversationPayload, summarizeOlderTurns } from '../util/conversationSummary';

describe('vitest pipeline smoke', () => {
  it('arithmetic works', () => {
    expect(1 + 1).toBe(2);
  });

  it('jsdom is available', () => {
    const el = document.createElement('div');
    el.textContent = 'hello';
    expect(el.textContent).toBe('hello');
  });
});

describe('aiModels — exercises the 2026-05-19 tool-use floor wiring', () => {
  it('recommends qwen2.5:7b on CPU', () => {
    expect(OLLAMA_CPU_RECOMMENDATION).toBe('qwen2.5:7b');
  });

  it('classifies qwen2.5:7b as tool-capable', () => {
    expect(isOllamaToolCapable('qwen2.5:7b')).toBe(true);
  });

  it('flags sub-floor Qwen models as below the tool-use floor', () => {
    expect(isOllamaBelowToolUseFloor('qwen2.5:1.5b')).toBe(true);
    expect(isOllamaBelowToolUseFloor('qwen2.5:3b')).toBe(true);
  });

  it('does not flag the recommended floor as sub-floor', () => {
    expect(isOllamaBelowToolUseFloor('qwen2.5:7b')).toBe(false);
    expect(isOllamaBelowToolUseFloor('llama3.1:8b')).toBe(false);
  });
});

describe('conversationSummary — deterministic client-side summarizer', () => {
  it('returns empty for empty history', () => {
    const { recent_turns, summary } = buildConversationPayload([], 10);
    expect(recent_turns).toHaveLength(0);
    expect(summary).toBe('');
  });

  it('returns the full history as recent_turns when under the window', () => {
    const msgs = [
      { role: 'user', content: 'hi' },
      { role: 'assistant', content: 'hello' },
    ];
    const { recent_turns, summary } = buildConversationPayload(msgs, 10);
    expect(recent_turns).toHaveLength(2);
    expect(summary).toBe('');
  });

  it('summarizes older turns when history exceeds the window', () => {
    const msgs = Array.from({ length: 15 }, (_, i) => ({
      role: i % 2 === 0 ? 'user' : 'assistant',
      content: `message ${i}`,
    }));
    const { recent_turns, summary } = buildConversationPayload(msgs, 10);
    expect(recent_turns).toHaveLength(10);
    expect(summary).toContain('User asked: message');
    // Older turns (0-4) should be in summary; recent (5-14) should be in recent_turns.
    expect(recent_turns[recent_turns.length - 1].content).toBe('message 14');
  });

  it('strips assistant filler openers from key points', () => {
    const summary = summarizeOlderTurns([
      { role: 'assistant', content: 'Sure! Here is the answer you need.' },
    ]);
    // "Sure" + "Here is" are filler; the real point is what follows.
    expect(summary.toLowerCase()).not.toMatch(/^- assistant said: sure/);
  });

  it('caps long turns within bullet length', () => {
    const longContent = 'word '.repeat(200); // ~1000 chars
    const summary = summarizeOlderTurns([
      { role: 'user', content: longContent },
    ]);
    // The bullet line should be capped well under the long content.
    const firstLine = summary.split('\n')[0];
    expect(firstLine.length).toBeLessThan(200);
  });
});
