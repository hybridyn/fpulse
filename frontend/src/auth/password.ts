/**
 * Frontend password policy — mirror of `backend/fpulse/auth/password_policy.py`.
 *
 * The two implementations MUST agree on what counts as a valid password,
 * otherwise the UI shows "Strong" on something the server then rejects
 * with a 400. Whenever you change a rule here, change the Python version
 * in lock-step (and vice versa).
 *
 * We do NOT call the backend `/check-password` endpoint on every keystroke
 * — that would either rate-limit, lag, or pummel the server. Instead the
 * meter runs locally in real time and the form only round-trips on submit
 * (where the backend re-validates as the source of truth). The shared
 * rule logic below keeps the two in sync.
 */

export const MIN_LENGTH = 12;

// Same lowercased blocklist as the Python module. Don't grow this list
// unbounded — it bloats the bundle. The backend has the same set; if you
// add an entry here, add it there too.
const COMMON_PASSWORDS = new Set([
  'password', 'password1', 'password123', 'passw0rd', 'p@ssword',
  'admin', 'admin123', 'administrator', 'root', 'rootroot',
  'letmein', 'welcome', 'welcome1', 'qwerty', 'qwerty123',
  'abc123', '123456', '12345678', '123456789', '1234567890',
  'iloveyou', 'monkey', 'dragon', 'master', 'shadow',
  'fpulse', 'fpulse123', 'f-pulse', 'hybridyn', 'pulse123',
  'changeme', 'changeme123', 'default', 'default123',
  'test', 'test123', 'testing', 'user', 'user123',
  'super', 'superman', 'batman', 'trustno1', 'starwars',
]);

export interface PasswordCheck {
  ok: boolean;
  score: 0 | 1 | 2 | 3 | 4;
  label: 'Weak' | 'Fair' | 'Good' | 'Strong' | 'Excellent';
  failures: string[];
  suggestions: string[];
}

/**
 * Run every rule in the policy. Pass `email` and `name` if you have them
 * — the validator will reject a password that contains either.
 *
 * Always returns a result; never throws. The caller decides whether to
 * block submit based on `result.ok`.
 */
export function checkPassword(password: string, email = '', name = ''): PasswordCheck {
  const failures: string[] = [];
  const suggestions: string[] = [];

  if (typeof password !== 'string') {
    return { ok: false, score: 0, label: 'Weak', failures: ['Type a password.'], suggestions: [] };
  }

  // Length first — biggest single contributor to strength.
  if (password.length < MIN_LENGTH) {
    const deficit = MIN_LENGTH - password.length;
    failures.push(`At least ${MIN_LENGTH} characters (need ${deficit} more).`);
    suggestions.push(`Add ${deficit} more character${deficit === 1 ? '' : 's'}.`);
  }

  const hasLower = /[a-z]/.test(password);
  const hasUpper = /[A-Z]/.test(password);
  const hasDigit = /[0-9]/.test(password);
  // "Symbol" = anything that's not alphanumeric and not whitespace.
  // Matches the Python `not c.isalnum() and not c.isspace()` rule.
  const hasSymbol = /[^A-Za-z0-9\s]/.test(password);

  if (!hasLower) {
    failures.push('Add at least one lowercase letter.');
    suggestions.push('Mix in a lowercase letter (a-z).');
  }
  if (!hasUpper) {
    failures.push('Add at least one uppercase letter.');
    suggestions.push('Mix in an uppercase letter (A-Z).');
  }
  if (!hasDigit) {
    failures.push('Add at least one number.');
    suggestions.push('Mix in a digit (0-9).');
  }
  if (!hasSymbol) {
    failures.push('Add at least one symbol.');
    suggestions.push('Mix in a symbol (!@#$%^&* etc.).');
  }

  if (COMMON_PASSWORDS.has(password.toLowerCase())) {
    failures.push('This password is on the common-passwords blocklist.');
    suggestions.push('Pick something less predictable — avoid dictionary words.');
  }

  const pwLower = password.toLowerCase();
  if (email) {
    const local = email.split('@', 1)[0].toLowerCase();
    if (local.length >= 4 && pwLower.includes(local)) {
      failures.push('Password contains your email address.');
      suggestions.push("Don't use your email or username inside the password.");
    }
  }
  if (name) {
    const first = name.trim().split(' ', 1)[0].toLowerCase();
    if (first.length >= 4 && pwLower.includes(first)) {
      failures.push('Password contains your name.');
      suggestions.push("Don't use your name inside the password.");
    }
  }

  if (hasLongRun(password, 4)) {
    failures.push("Avoid long runs of the same character (e.g. 'aaaa').");
    suggestions.push('Vary the characters — runs are easy to guess.');
  }
  if (hasSequence(password, 5)) {
    failures.push("Avoid sequences like 'abcde' or '12345'.");
    suggestions.push('Break up sequences with random characters.');
  }

  // Score 0..4 from rules that PASSED + length bonus, knocked down on
  // any failure. Same algorithm as the Python module so the two meters
  // agree on what to call "Strong".
  const classes = [hasLower, hasUpper, hasDigit, hasSymbol].filter(Boolean).length;
  let lengthBonus = 0;
  if (password.length >= 16) lengthBonus += 1;
  if (password.length >= 20) lengthBonus += 1;
  let raw = classes + lengthBonus;
  if (failures.length > 0) raw = Math.min(raw, 1);
  const score = (Math.min(4, Math.max(0, raw - 2)) as 0 | 1 | 2 | 3 | 4);
  const labels: PasswordCheck['label'][] = ['Weak', 'Fair', 'Good', 'Strong', 'Excellent'];

  return {
    ok: failures.length === 0,
    score,
    label: labels[score],
    failures,
    suggestions,
  };
}

function hasLongRun(s: string, n: number): boolean {
  if (s.length < n) return false;
  let run = 1;
  for (let i = 1; i < s.length; i++) {
    if (s[i] === s[i - 1]) {
      run += 1;
      if (run >= n) return true;
    } else {
      run = 1;
    }
  }
  return false;
}

function hasSequence(s: string, n: number): boolean {
  if (s.length < n) return false;
  const lower = s.toLowerCase();
  let asc = 1;
  let desc = 1;
  for (let i = 1; i < lower.length; i++) {
    const prev = lower.charCodeAt(i - 1);
    const cur = lower.charCodeAt(i);
    if (cur === prev + 1) {
      asc += 1;
      desc = 1;
      if (asc >= n) return true;
    } else if (cur === prev - 1) {
      desc += 1;
      asc = 1;
      if (desc >= n) return true;
    } else {
      asc = 1;
      desc = 1;
    }
  }
  return false;
}

// Generator alphabet — same exclusions as the Python module to keep
// generated passwords readable when an admin needs to dictate one.
const GEN_LOWER = 'abcdefghjkmnpqrstuvwxyz';
const GEN_UPPER = 'ABCDEFGHJKMNPQRSTUVWXYZ';
const GEN_DIGIT = '23456789';
const GEN_SYMBOL = '!@#$%^&*-_=+?';

/**
 * Local password generator — used when the user clicks "Generate" before
 * the API call resolves, or as a fallback if the network is offline.
 * The server `/auth/generate-password` endpoint is preferred (single
 * source of truth) but this exists so the UI never feels broken.
 */
export function generateStrongPasswordLocal(length = 20): string {
  const len = Math.max(MIN_LENGTH, length);
  const pool = GEN_LOWER + GEN_UPPER + GEN_DIGIT + GEN_SYMBOL;
  // Crypto-strong randomness via window.crypto. We pull `len` random
  // bytes and map each byte modulo the pool size — at pool sizes well
  // under 256 the modulo bias is negligible for password use.
  const bytes = new Uint8Array(len);
  // Guard against SSR / very old environments — fall back to Math.random
  // is acceptable for the local-only fallback path; the server endpoint
  // is the real source of randomness.
  const cryptoObj: Crypto | undefined = typeof window !== 'undefined' ? window.crypto : undefined;
  if (cryptoObj && cryptoObj.getRandomValues) {
    cryptoObj.getRandomValues(bytes);
  } else {
    for (let i = 0; i < len; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  // Force-include one of each required class.
  const required = [
    GEN_LOWER[bytes[0] % GEN_LOWER.length],
    GEN_UPPER[bytes[1] % GEN_UPPER.length],
    GEN_DIGIT[bytes[2] % GEN_DIGIT.length],
    GEN_SYMBOL[bytes[3] % GEN_SYMBOL.length],
  ];
  const fill: string[] = [];
  for (let i = 4; i < len; i++) {
    fill.push(pool[bytes[i] % pool.length]);
  }
  // Shuffle the combined list with Fisher-Yates seeded by another byte
  // sequence so the required-class chars aren't always at the front.
  const all = [...required, ...fill];
  const shuffleBytes = new Uint8Array(all.length);
  if (cryptoObj && cryptoObj.getRandomValues) cryptoObj.getRandomValues(shuffleBytes);
  for (let i = all.length - 1; i > 0; i--) {
    const j = shuffleBytes[i] % (i + 1);
    [all[i], all[j]] = [all[j], all[i]];
  }
  return all.join('');
}

/** Tailwind colour for each strength score, used by the meter component. */
export function strengthColour(score: number): string {
  switch (score) {
    case 0: return 'bg-rose-500';
    case 1: return 'bg-orange-500';
    case 2: return 'bg-amber-500';
    case 3: return 'bg-emerald-500';
    case 4: return 'bg-emerald-600';
    default: return 'bg-slate-300';
  }
}
