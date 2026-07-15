"""Runtime helpers for pyodbc / ODBC reads.

SQL Server exposes a few extended types that pyodbc cannot decode on its own.
Fetching a column of one of these aborts the whole read with, e.g.::

    ('ODBC SQL type -155 is not yet supported. column-index=3 type=-155', 'HY106')

We register pyodbc *output converters* so those columns come back as native
Python objects — exactly the shape the Postgres / MySQL read paths already
produce — instead of erroring. Downstream (``_rows_to_values`` /
``_rows_to_relation``) already handles native ``datetime`` / ``time`` values, so
no other change is needed.

Covered:
  * -155  SQL_SS_TIMESTAMPOFFSET  (DATETIMEOFFSET) → tz-aware datetime.datetime
  * -154  SQL_SS_TIME2            (TIME)           → datetime.time

Best-effort and idempotent: a registration or decode failure is swallowed, so a
read still proceeds (at worst the original "not supported" error resurfaces,
which is no worse than before).
"""
from __future__ import annotations

import datetime
import re
import struct

# SQL Server-specific ODBC C type codes (outside the standard SQL type range).
SQL_SS_TIMESTAMPOFFSET = -155  # DATETIMEOFFSET
SQL_SS_TIME2 = -154            # TIME


def _handle_datetimeoffset(raw):
    """Decode a SQL_SS_TIMESTAMPOFFSET_STRUCT into a tz-aware datetime.

    Layout: 6 shorts (year, month, day, hour, minute, second), one unsigned
    int (fraction, in nanoseconds), two shorts (tz hour offset, tz minute
    offset).
    """
    if not raw:
        return None
    y, mo, d, h, mi, s, frac, tz_h, tz_m = struct.unpack("<6hI2h", raw)
    return datetime.datetime(
        y, mo, d, h, mi, s, frac // 1000,  # ns → microseconds
        tzinfo=datetime.timezone(datetime.timedelta(hours=tz_h, minutes=tz_m)),
    )


def _handle_time2(raw):
    """Decode a SQL_SS_TIME2_STRUCT into a datetime.time.

    Layout: 3 unsigned shorts (hour, minute, second), one unsigned int
    (fraction, in nanoseconds).
    """
    if not raw:
        return None
    h, mi, s, frac = struct.unpack("<3HI", raw)
    return datetime.time(h, mi, s, frac // 1000)


def register_mssql_odbc_converters(conn) -> None:
    """Register output converters for SQL Server extended types on a pyodbc
    connection. Safe on any connection object; no-op when unsupported."""
    add = getattr(conn, "add_output_converter", None)
    if not callable(add):
        return
    for sql_type, handler in (
        (SQL_SS_TIMESTAMPOFFSET, _handle_datetimeoffset),
        (SQL_SS_TIME2, _handle_time2),
    ):
        try:
            add(sql_type, handler)
        except Exception:  # noqa: BLE001 — never let setup break a read
            continue


_UNSUPPORTED_TYPE_RE = re.compile(
    r"ODBC SQL type (-?\d+) is not yet supported", re.IGNORECASE
)
_COLUMN_INDEX_RE = re.compile(r"column-index=(\d+)", re.IGNORECASE)


def humanize_odbc_read_error(exc) -> str | None:
    """Turn pyodbc's cryptic "ODBC SQL type -N is not yet supported" into a
    self-serve message a user can act on WITHOUT upgrading.

    Returns the actionable message, or None if ``exc`` isn't that error (so
    callers re-raise the original untouched). This is the installed-base
    safety net: F-Pulse decodes the common SQL Server extended types
    natively (see ``register_mssql_odbc_converters``), but if a release
    hasn't yet got a converter for some exotic type, the user still gets a
    fix they can apply today instead of a dead-end stack trace.
    """
    msg = str(exc or "")
    m = _UNSUPPORTED_TYPE_RE.search(msg)
    if not m:
        return None
    type_code = m.group(1)
    col_m = _COLUMN_INDEX_RE.search(msg)
    col_hint = f" (column #{col_m.group(1)})" if col_m else ""
    return (
        f"This SQL Server table has a column{col_hint} whose type your ODBC "
        f"driver build can't return directly (ODBC type {type_code}). "
        "F-Pulse decodes the common ones (DATETIMEOFFSET, TIME) automatically; "
        "your installed version doesn't yet cover this one.\n\n"
        "Fix without changing the source database — switch this Source to "
        "Query mode and cast the column to text, e.g.\n"
        "    SELECT ..., CONVERT(varchar(max), [the_column]) AS [the_column], ...\n"
        "FROM your_table\n\n"
        "Upgrading F-Pulse may also add native support for this type."
    )


__all__ = ["register_mssql_odbc_converters", "humanize_odbc_read_error"]
