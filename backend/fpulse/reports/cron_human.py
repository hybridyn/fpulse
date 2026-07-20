"""Convert cron expressions into human-readable English descriptions.

Used by the inventory PDF/DOCX renderers so that report tables show
"Every 2 minutes" instead of raw "*/2 * * * *". The raw expression is
still available if power users need it — callers typically render both.

Pure Python, no dependencies. Covers the patterns F-Pulse users
actually create (see PipelinesPage schedule UI):

  * * * * *        → "Every minute"
  */N * * * *      → "Every N minutes"
  0 * * * *        → "Every hour"
  M * * * *        → "At :MM every hour"
  0 */N * * *      → "Every N hours"
  M H * * *        → "Daily at HH:MM"
  M H * * D        → "Every {weekday} at HH:MM"
  M H * * 1-5      → "Weekdays at HH:MM"
  M H * * 0,6      → "Weekends at HH:MM"
  M H D * *        → "Monthly on the Dth at HH:MM"
  M H D MO *       → "Yearly on {Month} D at HH:MM"

For any expression we can't recognize, we return the raw string so the
report still shows *something* — better than crashing.
"""
from __future__ import annotations

DOW_NAMES = {
    "0": "Sunday", "7": "Sunday",
    "1": "Monday", "2": "Tuesday", "3": "Wednesday",
    "4": "Thursday", "5": "Friday", "6": "Saturday",
}
MONTH_NAMES = {
    "1": "January", "2": "February", "3": "March", "4": "April",
    "5": "May", "6": "June", "7": "July", "8": "August",
    "9": "September", "10": "October", "11": "November", "12": "December",
}


def _fmt_time(h: str, m: str) -> str:
    """Format `HH:MM` from string fields, falling back to raw on parse error."""
    try:
        return f"{int(h):02d}:{int(m):02d}"
    except (ValueError, TypeError):
        return f"{h}:{m}"


def _ordinal(n: int) -> str:
    """Return '1st', '2nd', '3rd', '4th', …, '21st', etc."""
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def cron_to_human(expr: str) -> str:
    """Convert a 5-field cron expression to a human-readable description.

    Returns the original `expr` unchanged when the pattern isn't one we
    handle — the report still shows the raw cron so the user has *some*
    information rather than a blank cell.
    """
    if not expr or not isinstance(expr, str):
        return expr or ""
    parts = expr.strip().split()
    # We only handle the standard 5-field POSIX cron. 6-field (with
    # seconds) or 7-field (Quartz with year) — return as-is.
    if len(parts) != 5:
        return expr

    minute, hour, dom, month, dow = parts

    # ── Sub-hour patterns ─────────────────────────────────────────────

    # Every minute
    if (minute, hour, dom, month, dow) == ("*", "*", "*", "*", "*"):
        return "Every minute"

    # */N * * * *  →  every N minutes
    if (minute.startswith("*/") and hour == "*"
            and dom == "*" and month == "*" and dow == "*"):
        try:
            n = int(minute[2:])
            return "Every minute" if n == 1 else f"Every {n} minutes"
        except ValueError:
            pass

    # 0 * * * *  →  every hour
    if minute == "0" and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return "Every hour"

    # M * * * *  →  at :MM of every hour
    if (minute.isdigit() and hour == "*"
            and dom == "*" and month == "*" and dow == "*"):
        return f"At :{int(minute):02d} every hour"

    # 0 */N * * *  →  every N hours
    if (minute == "0" and hour.startswith("*/")
            and dom == "*" and month == "*" and dow == "*"):
        try:
            n = int(hour[2:])
            return "Every hour" if n == 1 else f"Every {n} hours"
        except ValueError:
            pass

    # ── Daily / weekly patterns ───────────────────────────────────────

    # M H * * *  →  daily at HH:MM
    if (minute.isdigit() and hour.isdigit()
            and dom == "*" and month == "*" and dow == "*"):
        return f"Daily at {_fmt_time(hour, minute)}"

    # M H * * 1-5  →  weekdays
    if (minute.isdigit() and hour.isdigit()
            and dom == "*" and month == "*" and dow == "1-5"):
        return f"Weekdays at {_fmt_time(hour, minute)}"

    # M H * * 0,6 (or variants)  →  weekends
    if (minute.isdigit() and hour.isdigit()
            and dom == "*" and month == "*"
            and dow.replace(" ", "") in ("0,6", "6,0", "6,7", "7,6")):
        return f"Weekends at {_fmt_time(hour, minute)}"

    # M H * * D  →  weekly on a single named day
    if (minute.isdigit() and hour.isdigit()
            and dom == "*" and month == "*" and dow in DOW_NAMES):
        return f"Every {DOW_NAMES[dow]} at {_fmt_time(hour, minute)}"

    # M H * * D1,D2,...  →  multiple named days
    if (minute.isdigit() and hour.isdigit()
            and dom == "*" and month == "*" and "," in dow):
        days: list[str] = []
        for d in dow.split(","):
            key = d.strip()
            if key in DOW_NAMES:
                days.append(DOW_NAMES[key])
            else:
                return expr  # unrecognized component, bail
        # Dedup while preserving order (e.g. "0,7" both → Sunday)
        seen: set[str] = set()
        ordered = [d for d in days if not (d in seen or seen.add(d))]
        if len(ordered) == 7:
            return f"Daily at {_fmt_time(hour, minute)}"
        return f"Every {', '.join(ordered)} at {_fmt_time(hour, minute)}"

    # ── Monthly / yearly patterns ─────────────────────────────────────

    # M H D * *  →  monthly on the Dth
    if (minute.isdigit() and hour.isdigit() and dom.isdigit()
            and month == "*" and dow == "*"):
        return f"Monthly on the {_ordinal(int(dom))} at {_fmt_time(hour, minute)}"

    # M H D MO *  →  yearly on Month D
    if (minute.isdigit() and hour.isdigit() and dom.isdigit()
            and month in MONTH_NAMES and dow == "*"):
        return (
            f"Yearly on {MONTH_NAMES[month]} {int(dom)} "
            f"at {_fmt_time(hour, minute)}"
        )

    # Unknown pattern — return raw so the user still sees something.
    return expr


def cron_with_raw(expr: str, sep: str = " · ") -> str:
    """Return "Human description · raw expression" for inline display.

    When `cron_to_human` returns the same string we passed in (i.e. we
    couldn't humanize it), we skip the `sep + raw` suffix to avoid a
    redundant "*/2 * * * * · */2 * * * *".
    """
    human = cron_to_human(expr)
    if human == expr or not expr:
        return expr or ""
    return f"{human}{sep}{expr}"
