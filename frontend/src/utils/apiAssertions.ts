/**
 * Declarative response assertions — the "Tests" tab of the API Explorer.
 *
 * Postman lets a test be arbitrary JavaScript. F-Pulse deliberately does not:
 * the Explorer runs inside the app's no-eval posture, and a saved assertion is
 * data that other people's browsers may later render. So an assertion here is a
 * struct the engine interprets, never code it executes.
 */

export type AssertionSubject = 'status' | 'elapsed_ms' | 'header' | 'json_path' | 'body_text';
export type AssertionOperator =
  | 'equals' | 'not_equals' | 'contains' | 'less_than' | 'greater_than'
  | 'exists' | 'not_exists' | 'is_type' | 'length_at_least';

export interface Assertion {
  id: string;
  subject: AssertionSubject;
  /** Header name, or a JSON path such as `$[0]["id"]`. Unused for status/elapsed/body. */
  target: string;
  operator: AssertionOperator;
  expected: string;
}

export interface AssertionOutcome {
  id: string;
  passed: boolean;
  /** What the response actually produced, rendered for display. */
  actual: string;
  /** Human-readable restatement of what was checked. */
  label: string;
}

export interface ResponseUnderTest {
  status: number;
  elapsed_ms: number;
  headers: Record<string, string>;
  data: unknown;
  text: string;
}

export const blankAssertion = (): Assertion => ({
  id: crypto.randomUUID(),
  subject: 'status',
  target: '',
  operator: 'equals',
  expected: '200',
});

/** Operators that make sense per subject — drives the UI's operator list. */
export const OPERATORS: Record<AssertionSubject, AssertionOperator[]> = {
  status: ['equals', 'not_equals', 'less_than', 'greater_than'],
  elapsed_ms: ['less_than', 'greater_than'],
  header: ['exists', 'not_exists', 'equals', 'not_equals', 'contains'],
  json_path: ['exists', 'not_exists', 'equals', 'not_equals', 'contains', 'is_type', 'length_at_least'],
  body_text: ['contains', 'not_equals', 'equals'],
};

export const SUBJECT_LABELS: Record<AssertionSubject, string> = {
  status: 'Status code',
  elapsed_ms: 'Response time (ms)',
  header: 'Response header',
  json_path: 'JSON path',
  body_text: 'Body text',
};

export const OPERATOR_LABELS: Record<AssertionOperator, string> = {
  equals: 'equals',
  not_equals: 'does not equal',
  contains: 'contains',
  less_than: 'is less than',
  greater_than: 'is greater than',
  exists: 'exists',
  not_exists: 'does not exist',
  is_type: 'is of type',
  length_at_least: 'has at least (items)',
};

/** Operators that ignore the `expected` field entirely. */
export const UNARY_OPERATORS: AssertionOperator[] = ['exists', 'not_exists'];

const TYPE_NAMES = ['string', 'number', 'boolean', 'object', 'array', 'null'];

function typeOf(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

/**
 * Resolve a bracket path such as `$[0]["name"]` against parsed JSON.
 *
 * The same restricted grammar the response Structure tab emits: bracket tokens
 * only, each either a decimal index or a JSON string. Nothing is evaluated, so
 * a hostile path can only fail to resolve.
 */
export function resolvePath(root: unknown, path: string): { found: boolean; value: unknown } {
  if (!path || path[0] !== '$') return { found: false, value: undefined };
  let current: unknown = root;
  let index = 1;
  while (index < path.length) {
    const match = /^\[("(?:[^"\\]|\\.)*"|\d+)\]/.exec(path.slice(index));
    if (!match) return { found: false, value: undefined };
    let key: string | number;
    try {
      key = JSON.parse(match[1]);
    } catch {
      return { found: false, value: undefined };
    }
    if (current === null || typeof current !== 'object') return { found: false, value: undefined };
    const container = current as Record<string | number, unknown>;
    // hasOwnProperty, not `in` — `in` walks the prototype chain, so a path of
    // `$["constructor"]` would otherwise resolve to Object.prototype.constructor.
    if (!Object.prototype.hasOwnProperty.call(container, key)) return { found: false, value: undefined };
    current = container[key];
    index += match[0].length;
  }
  return { found: true, value: current };
}

function display(value: unknown): string {
  if (value === undefined) return '(absent)';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    return String(value);
  }
}

function compare(operator: AssertionOperator, actual: unknown, expected: string, found: boolean): boolean {
  switch (operator) {
    case 'exists':
      return found;
    case 'not_exists':
      return !found;
    case 'equals':
      // Compare on the rendered form so `1` matches the number 1 and the
      // string "1" — the user typed text into a box, not a typed literal.
      return found && display(actual) === expected;
    case 'not_equals':
      return !found || display(actual) !== expected;
    case 'contains':
      return found && display(actual).includes(expected);
    case 'less_than':
      return found && Number(actual) < Number(expected);
    case 'greater_than':
      return found && Number(actual) > Number(expected);
    case 'is_type':
      return found && typeOf(actual) === expected;
    case 'length_at_least': {
      if (!found) return false;
      const size = Array.isArray(actual) ? actual.length
        : typeof actual === 'string' ? actual.length
        : actual && typeof actual === 'object' ? Object.keys(actual).length
        : NaN;
      return Number.isFinite(size) && size >= Number(expected);
    }
    default:
      return false;
  }
}

function subjectValue(assertion: Assertion, response: ResponseUnderTest): { found: boolean; value: unknown } {
  switch (assertion.subject) {
    case 'status':
      return { found: true, value: response.status };
    case 'elapsed_ms':
      return { found: true, value: response.elapsed_ms };
    case 'body_text':
      return { found: true, value: response.data != null ? display(response.data) : response.text };
    case 'header': {
      const wanted = assertion.target.trim().toLowerCase();
      const hit = Object.entries(response.headers ?? {}).find(([name]) => name.toLowerCase() === wanted);
      return hit ? { found: true, value: hit[1] } : { found: false, value: undefined };
    }
    case 'json_path':
      return resolvePath(response.data, assertion.target.trim());
    default:
      return { found: false, value: undefined };
  }
}

export function describeAssertion(assertion: Assertion): string {
  const subject = assertion.subject === 'header' || assertion.subject === 'json_path'
    ? `${SUBJECT_LABELS[assertion.subject]} ${assertion.target || '—'}`
    : SUBJECT_LABELS[assertion.subject];
  const operator = OPERATOR_LABELS[assertion.operator];
  return UNARY_OPERATORS.includes(assertion.operator)
    ? `${subject} ${operator}`
    : `${subject} ${operator} ${assertion.expected || '—'}`;
}

/** Size of a value for the length operator, or NaN when it has no length. */
function sizeOf(value: unknown): number {
  if (Array.isArray(value)) return value.length;
  if (typeof value === 'string') return value.length;
  if (value && typeof value === 'object') return Object.keys(value).length;
  return NaN;
}

/**
 * What to show the user when a check fails. A length check that dumps a
 * 10,000-element array tells them nothing — the count is the answer.
 */
function reportActual(assertion: Assertion, value: unknown, found: boolean): string {
  if (!found) return '(absent)';
  if (assertion.operator === 'length_at_least') {
    const size = sizeOf(value);
    if (!Number.isFinite(size)) return `${typeOf(value)} (no length)`;
    const unit = Array.isArray(value) ? 'items' : typeof value === 'string' ? 'characters' : 'keys';
    return `${size} ${unit}`;
  }
  if (assertion.operator === 'is_type') return typeOf(value);
  const rendered = display(value);
  return rendered.length > 200 ? `${rendered.slice(0, 200)}…` : rendered;
}

export function runAssertions(assertions: Assertion[], response: ResponseUnderTest): AssertionOutcome[] {
  return assertions.map(assertion => {
    const { found, value } = subjectValue(assertion, response);
    return {
      id: assertion.id,
      passed: compare(assertion.operator, value, assertion.expected, found),
      actual: reportActual(assertion, value, found),
      label: describeAssertion(assertion),
    };
  });
}

/** Assertions the user has not finished filling in — skipped rather than failed. */
export function isIncomplete(assertion: Assertion): boolean {
  if ((assertion.subject === 'header' || assertion.subject === 'json_path') && !assertion.target.trim()) return true;
  return !UNARY_OPERATORS.includes(assertion.operator) && assertion.expected === '';
}

export const TYPE_OPTIONS = TYPE_NAMES;
