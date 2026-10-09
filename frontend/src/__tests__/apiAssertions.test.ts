import { describe, expect, it } from 'vitest';
import {
  isIncomplete, resolvePath, runAssertions, describeAssertion,
  type Assertion, type ResponseUnderTest,
} from '../utils/apiAssertions';

const response: ResponseUnderTest = {
  status: 200,
  elapsed_ms: 140,
  headers: { 'Content-Type': 'application/json', Date: 'Tue, 23 Sep 2026 06:00:00 GMT' },
  data: { records: [{ id: 1, name: 'Acme', active: true, parent: null }], total: 1 },
  text: '',
};

const check = (patch: Partial<Assertion>): Assertion => ({
  id: 'a', subject: 'status', target: '', operator: 'equals', expected: '200', ...patch,
});

const run = (patch: Partial<Assertion>) => runAssertions([check(patch)], response)[0];

describe('resolvePath', () => {
  it('walks index and quoted-key tokens', () => {
    expect(resolvePath(response.data, '$["records"][0]["name"]')).toEqual({ found: true, value: 'Acme' });
    expect(resolvePath(response.data, '$')).toEqual({ found: true, value: response.data });
  });

  it('reports absence rather than throwing', () => {
    expect(resolvePath(response.data, '$["missing"]').found).toBe(false);
    expect(resolvePath(response.data, '$["records"][9]').found).toBe(false);
  });

  it('refuses paths outside the bracket grammar, so nothing can be evaluated', () => {
    for (const path of ['records', '$.records', '$["a"].b', '$[alert(1)]', '']) {
      expect(resolvePath(response.data, path).found).toBe(false);
    }
  });

  it('does not walk into inherited properties', () => {
    expect(resolvePath(response.data, '$["constructor"]').found).toBe(false);
    expect(resolvePath(response.data, '$["__proto__"]').found).toBe(false);
  });
});

describe('runAssertions', () => {
  it('checks status and response time', () => {
    expect(run({ subject: 'status', operator: 'equals', expected: '200' }).passed).toBe(true);
    expect(run({ subject: 'status', operator: 'equals', expected: '404' }).passed).toBe(false);
    expect(run({ subject: 'elapsed_ms', operator: 'less_than', expected: '500' }).passed).toBe(true);
    expect(run({ subject: 'elapsed_ms', operator: 'less_than', expected: '100' }).passed).toBe(false);
  });

  it('matches headers case-insensitively', () => {
    expect(run({ subject: 'header', target: 'content-type', operator: 'contains', expected: 'json' }).passed).toBe(true);
    expect(run({ subject: 'header', target: 'X-Missing', operator: 'not_exists', expected: '' }).passed).toBe(true);
    expect(run({ subject: 'header', target: 'X-Missing', operator: 'exists', expected: '' }).passed).toBe(false);
  });

  it('checks values at a JSON path, including type and length', () => {
    expect(run({ subject: 'json_path', target: '$["records"][0]["name"]', operator: 'equals', expected: 'Acme' }).passed).toBe(true);
    expect(run({ subject: 'json_path', target: '$["records"][0]["id"]', operator: 'is_type', expected: 'number' }).passed).toBe(true);
    expect(run({ subject: 'json_path', target: '$["records"][0]["parent"]', operator: 'is_type', expected: 'null' }).passed).toBe(true);
    expect(run({ subject: 'json_path', target: '$["records"]', operator: 'length_at_least', expected: '1' }).passed).toBe(true);
    expect(run({ subject: 'json_path', target: '$["records"]', operator: 'length_at_least', expected: '2' }).passed).toBe(false);
  });

  it('compares numbers and their typed-in text form as equal', () => {
    expect(run({ subject: 'json_path', target: '$["total"]', operator: 'equals', expected: '1' }).passed).toBe(true);
  });

  it('fails rather than passes when the path is absent', () => {
    const outcome = run({ subject: 'json_path', target: '$["nope"]', operator: 'equals', expected: 'x' });
    expect(outcome.passed).toBe(false);
    expect(outcome.actual).toBe('(absent)');
  });

  it('reports the actual value for a failure', () => {
    expect(run({ subject: 'status', operator: 'equals', expected: '201' }).actual).toBe('200');
  });

  it('reports a count, not the payload, when a length check fails', () => {
    const outcome = run({ subject: 'json_path', target: '$["records"]', operator: 'length_at_least', expected: '5' });
    expect(outcome.passed).toBe(false);
    expect(outcome.actual).toBe('1 items');
    expect(outcome.actual).not.toContain('Acme');
  });

  it('reports the type name when a type check fails', () => {
    expect(run({ subject: 'json_path', target: '$["total"]', operator: 'is_type', expected: 'string' }).actual).toBe('number');
  });
});

describe('isIncomplete', () => {
  it('treats a missing target or expected value as unfinished', () => {
    expect(isIncomplete(check({ subject: 'json_path', target: '', expected: '1' }))).toBe(true);
    expect(isIncomplete(check({ subject: 'status', expected: '' }))).toBe(true);
    expect(isIncomplete(check({ subject: 'header', target: 'date', operator: 'exists', expected: '' }))).toBe(false);
  });
});

describe('describeAssertion', () => {
  it('omits the expected value for unary operators', () => {
    expect(describeAssertion(check({ subject: 'header', target: 'date', operator: 'exists', expected: '' })))
      .toBe('Response header date exists');
    expect(describeAssertion(check({ subject: 'status', operator: 'equals', expected: '200' })))
      .toBe('Status code equals 200');
  });
});
