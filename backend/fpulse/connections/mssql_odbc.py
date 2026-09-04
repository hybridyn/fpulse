"""Shared SQL Server ODBC connection-string helpers.

Connection testing, catalog browsing, source reads, and destination writes
must build SQL Server ODBC strings the same way. If one path omits encryption
or certificate options, a connection can test successfully but fail during a
real pipeline write.
"""

from __future__ import annotations

from typing import Any


PREFERRED_MSSQL_ODBC_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _encrypt_value(value: Any, driver: str) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"yes", "no", "optional", "mandatory", "strict"}:
            return normalized
    if _truthy(value):
        return "yes"
    return "optional" if "18" in driver else "no"


def select_mssql_odbc_driver(pyodbc_module: Any, preferred_driver: str | None = None) -> str | None:
    installed = [d for d in pyodbc_module.drivers() if "SQL Server" in d]
    if preferred_driver and preferred_driver in installed:
        return preferred_driver
    return next((d for d in PREFERRED_MSSQL_ODBC_DRIVERS if d in installed), None) or (
        installed[0] if installed else None
    )


def build_mssql_odbc_conn_str(config: dict[str, Any], pyodbc_module: Any, timeout: int = 10) -> str:
    driver = select_mssql_odbc_driver(pyodbc_module, config.get("driver"))
    if not driver:
        raise RuntimeError("No SQL Server ODBC driver installed")

    host = config.get("host") or "localhost"
    port = config.get("port") or 1433
    database = config.get("database") or "master"
    raw_user = config.get("user") or config.get("username")
    password = config.get("password") or ""
    windows_auth = _truthy(config.get("windows_auth")) or (not raw_user and not password)
    auth_clause = "Trusted_Connection=yes;" if windows_auth else f"UID={raw_user or 'sa'};PWD={password};"

    trust_cert = "yes" if (_truthy(config.get("trust_server_certificate")) or "18" in driver) else "no"
    encrypt = _encrypt_value(config.get("encrypt"), driver)

    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={host},{port};"
        f"DATABASE={database};"
        f"{auth_clause}"
        f"Encrypt={encrypt};TrustServerCertificate={trust_cert};"
        f"Connection Timeout={timeout};"
    )
