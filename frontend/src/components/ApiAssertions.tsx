import { Plus, Trash2, Check, X, Minus } from 'lucide-react';
import {
  OPERATORS, OPERATOR_LABELS, SUBJECT_LABELS, TYPE_OPTIONS, UNARY_OPERATORS,
  blankAssertion, isIncomplete,
  type Assertion, type AssertionOutcome, type AssertionSubject,
} from '../utils/apiAssertions';

const SUBJECTS = Object.keys(SUBJECT_LABELS) as AssertionSubject[];

export default function ApiAssertions({ rows, onChange, outcomes, hasResponse }: {
  rows: Assertion[];
  onChange: (rows: Assertion[]) => void;
  outcomes: AssertionOutcome[];
  hasResponse: boolean;
}) {
  const update = (id: string, patch: Partial<Assertion>) => onChange(rows.map(r => {
    if (r.id !== id) return r;
    const next = { ...r, ...patch };
    // Switching subject can strand an operator the new subject doesn't offer.
    if (patch.subject && !OPERATORS[next.subject].includes(next.operator)) next.operator = OPERATORS[next.subject][0];
    return next;
  }));

  const results = new Map(outcomes.map(o => [o.id, o]));
  const checked = rows.filter(r => !isIncomplete(r));
  const passed = outcomes.filter(o => o.passed).length;

  return <div className="api-assert">
    <div className="api-assert-summary">
      {!hasResponse
        ? <span className="api-assert-idle">Send a request to run {checked.length || 'your'} {checked.length === 1 ? 'check' : 'checks'}</span>
        : checked.length === 0
          ? <span className="api-assert-idle">No checks defined</span>
          : <span className={passed === outcomes.length ? 'api-assert-allpass' : 'api-assert-somefail'}>
              {passed} of {outcomes.length} passed
            </span>}
      <button className="api-assert-add" onClick={() => onChange([...rows, blankAssertion()])} disabled={rows.length >= 25}>
        <Plus size={14} />Add check
      </button>
    </div>

    {rows.length === 0 && <p className="api-assert-empty">
      Add a check to assert on the next response — status code, response time, a header, or a value at a JSON path.
    </p>}

    <ul className="api-assert-list">
      {rows.map((row, index) => {
        const outcome = results.get(row.id);
        const skipped = isIncomplete(row);
        const needsTarget = row.subject === 'header' || row.subject === 'json_path';
        const unary = UNARY_OPERATORS.includes(row.operator);
        return <li key={row.id} className="api-assert-row">
          <span className={`api-assert-state ${!hasResponse || skipped ? 'is-idle' : outcome?.passed ? 'is-pass' : 'is-fail'}`}
            title={!hasResponse ? 'Not run yet' : skipped ? 'Incomplete — skipped' : outcome?.passed ? 'Passed' : 'Failed'}>
            {!hasResponse || skipped ? <Minus size={13} /> : outcome?.passed ? <Check size={13} /> : <X size={13} />}
          </span>

          <div className="api-assert-fields">
            <select aria-label={`Check ${index + 1} subject`} value={row.subject}
              onChange={e => update(row.id, { subject: e.target.value as AssertionSubject })} className="api-field-input">
              {SUBJECTS.map(s => <option key={s} value={s}>{SUBJECT_LABELS[s]}</option>)}
            </select>

            {needsTarget && <input aria-label={`Check ${index + 1} target`} className="api-field-input"
              placeholder={row.subject === 'header' ? 'content-type' : '$[0]["id"]'}
              value={row.target} onChange={e => update(row.id, { target: e.target.value })} />}

            <select aria-label={`Check ${index + 1} operator`} value={row.operator}
              onChange={e => update(row.id, { operator: e.target.value as Assertion['operator'] })} className="api-field-input">
              {OPERATORS[row.subject].map(op => <option key={op} value={op}>{OPERATOR_LABELS[op]}</option>)}
            </select>

            {!unary && (row.operator === 'is_type'
              ? <select aria-label={`Check ${index + 1} expected`} value={row.expected}
                  onChange={e => update(row.id, { expected: e.target.value })} className="api-field-input">
                  <option value="">Select a type</option>
                  {TYPE_OPTIONS.map(t => <option key={t} value={t}>{t}</option>)}
                </select>
              : <input aria-label={`Check ${index + 1} expected`} className="api-field-input" placeholder="Expected value"
                  value={row.expected} onChange={e => update(row.id, { expected: e.target.value })} />)}
          </div>

          <button className="api-icon-button" aria-label={`Remove check ${index + 1}`} title={`Remove check ${index + 1}`}
            onClick={() => onChange(rows.filter(r => r.id !== row.id))}><Trash2 size={15} /></button>

          {hasResponse && !skipped && outcome && !outcome.passed &&
            <p className="api-assert-actual">Actual: <code>{outcome.actual}</code></p>}
          {skipped && rows.length > 0 && <p className="api-assert-actual api-assert-muted">Incomplete — this check is skipped.</p>}
        </li>;
      })}
    </ul>
  </div>;
}
