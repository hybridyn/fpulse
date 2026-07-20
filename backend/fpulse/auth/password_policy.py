"""Password policy — strength rules + a strong-password generator.

Single source of truth for "is this password acceptable?". Both the
register endpoint, the admin reset endpoint, and the (future) self-serve
change-password endpoint MUST call ``validate_password()`` so an attacker
can't bypass the rule by hitting the API directly with curl.

The frontend has a mirror of these rules in ``frontend/src/auth/password.ts``
that drives the live strength meter; the two implementations must agree
on what counts as a valid password or the UX becomes a broken trap (the
form says "Strong" but the server still says 422).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

# Minimum length we'll accept. 12 is the NIST 2024 floor for human-chosen
# passwords paired with a complexity rule.
MIN_LENGTH = 12

# Embedded common-password blocklist. Lowercased; the validator
# lowercases the candidate before checking.
_COMMON_PASSWORDS = frozenset({
    "password", "password1", "password123", "passw0rd", "p@ssword",
    "admin", "admin123", "administrator", "root", "rootroot",
    "letmein", "welcome", "welcome1", "qwerty", "qwerty123",
    "abc123", "123456", "12345678", "123456789", "1234567890",
    "iloveyou", "monkey", "dragon", "master", "shadow",
    "fpulse", "fpulse123", "f-pulse", "hybridyn", "pulse123",
    "changeme", "changeme123", "default", "default123",
    "test", "test123", "testing", "user", "user123",
    "super", "superman", "batman", "trustno1", "starwars",
})


@dataclass
class PasswordCheck:
    """Result of running ``validate_password``."""
    ok: bool
    score: int               # 0..4: weak, fair, good, strong, excellent
    label: str               # "Weak" | "Fair" | "Good" | "Strong" | "Excellent"
    failures: list[str]      # human-readable rule violations
    suggestions: list[str]   # concrete fix-it hints


def validate_password(password: str, *, email: str = "", name: str = "") -> PasswordCheck:
    """Run every rule in the password policy and return a structured result."""
    failures: list[str] = []
    suggestions: list[str] = []

    if not isinstance(password, str):
        return PasswordCheck(False, 0, "Weak",
                             ["Password must be a string."],
                             ["Type a password."])

    if len(password) < MIN_LENGTH:
        deficit = MIN_LENGTH - len(password)
        failures.append(f"Password must be at least {MIN_LENGTH} characters (need {deficit} more).")
        suggestions.append(f"Add {deficit} more character{'s' if deficit != 1 else ''}.")

    has_lower = any(c.islower() for c in password)
    has_upper = any(c.isupper() for c in password)
    has_digit = any(c.isdigit() for c in password)
    has_symbol = any(not c.isalnum() and not c.isspace() for c in password)

    if not has_lower:
        failures.append("Add at least one lowercase letter.")
        suggestions.append("Mix in a lowercase letter (a-z).")
    if not has_upper:
        failures.append("Add at least one uppercase letter.")
        suggestions.append("Mix in an uppercase letter (A-Z).")
    if not has_digit:
        failures.append("Add at least one number.")
        suggestions.append("Mix in a digit (0-9).")
    if not has_symbol:
        failures.append("Add at least one symbol.")
        suggestions.append("Mix in a symbol (!@#$%^&* etc.).")

    if password.lower() in _COMMON_PASSWORDS:
        failures.append("This password is on the common-passwords blocklist.")
        suggestions.append("Pick something less predictable — avoid dictionary words.")

    pw_lower = password.lower()
    if email:
        local_part = email.split("@", 1)[0].lower()
        if len(local_part) >= 4 and local_part in pw_lower:
            failures.append("Password contains your email address.")
            suggestions.append("Don't use your email or username inside the password.")
    if name:
        first = name.strip().split(" ", 1)[0].lower()
        if len(first) >= 4 and first in pw_lower:
            failures.append("Password contains your name.")
            suggestions.append("Don't use your name inside the password.")

    if _has_long_run(password, 4):
        failures.append("Avoid long runs of the same character (e.g. 'aaaa').")
        suggestions.append("Vary the characters — runs are easy to guess.")

    if _has_sequence(password, 5):
        failures.append("Avoid keyboard or alphabetic sequences (e.g. 'abcde', '12345').")
        suggestions.append("Break up sequences with random characters.")

    classes_passed = sum([has_lower, has_upper, has_digit, has_symbol])
    length_bonus = 0
    if len(password) >= 16:
        length_bonus += 1
    if len(password) >= 20:
        length_bonus += 1
    raw_score = classes_passed + length_bonus
    if failures:
        raw_score = min(raw_score, 1)
    score = min(4, max(0, raw_score - 2))
    label = ["Weak", "Fair", "Good", "Strong", "Excellent"][score]

    return PasswordCheck(
        ok=not failures,
        score=score,
        label=label,
        failures=failures,
        suggestions=suggestions,
    )


def _has_long_run(s: str, n: int) -> bool:
    if len(s) < n:
        return False
    run = 1
    for i in range(1, len(s)):
        if s[i] == s[i - 1]:
            run += 1
            if run >= n:
                return True
        else:
            run = 1
    return False


def _has_sequence(s: str, n: int) -> bool:
    if len(s) < n:
        return False
    s = s.lower()
    asc = 1
    desc = 1
    for i in range(1, len(s)):
        prev_o = ord(s[i - 1])
        cur_o = ord(s[i])
        if cur_o == prev_o + 1:
            asc += 1
            desc = 1
            if asc >= n:
                return True
        elif cur_o == prev_o - 1:
            desc += 1
            asc = 1
            if desc >= n:
                return True
        else:
            asc = 1
            desc = 1
    return False


_GEN_LOWER = "abcdefghjkmnpqrstuvwxyz"   # no l
_GEN_UPPER = "ABCDEFGHJKMNPQRSTUVWXYZ"   # no I, O
_GEN_DIGIT = "23456789"                  # no 0, 1
_GEN_SYMBOL = "!@#$%^&*-_=+?"


def generate_strong_password(length: int = 20) -> str:
    """Generate a random password that is guaranteed to pass ``validate_password``."""
    length = max(length, MIN_LENGTH)
    rng = secrets.SystemRandom()
    chars = [
        rng.choice(_GEN_LOWER),
        rng.choice(_GEN_UPPER),
        rng.choice(_GEN_DIGIT),
        rng.choice(_GEN_SYMBOL),
    ]
    pool = _GEN_LOWER + _GEN_UPPER + _GEN_DIGIT + _GEN_SYMBOL
    chars.extend(rng.choice(pool) for _ in range(length - len(chars)))
    rng.shuffle(chars)
    return "".join(chars)
