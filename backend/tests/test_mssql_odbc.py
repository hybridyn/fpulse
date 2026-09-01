from __future__ import annotations

from fpulse.connections.mssql_odbc import (
    build_mssql_odbc_conn_str,
    select_mssql_odbc_driver,
)


class FakePyodbc:
    def __init__(self, drivers: list[str]) -> None:
        self._drivers = drivers

    def drivers(self) -> list[str]:
        return self._drivers


def test_selects_newest_installed_sql_server_driver() -> None:
    pyodbc = FakePyodbc(["SQL Server", "ODBC Driver 17 for SQL Server"])

    assert select_mssql_odbc_driver(pyodbc) == "ODBC Driver 17 for SQL Server"


def test_respects_preferred_installed_driver() -> None:
    pyodbc = FakePyodbc(["ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"])

    assert select_mssql_odbc_driver(pyodbc, "ODBC Driver 17 for SQL Server") == (
        "ODBC Driver 17 for SQL Server"
    )


def test_build_conn_str_defaults_driver_18_to_optional_encryption() -> None:
    pyodbc = FakePyodbc(["ODBC Driver 18 for SQL Server"])

    conn_str = build_mssql_odbc_conn_str(
        {
            "host": "sql.example.com",
            "port": 14330,
            "database": "warehouse",
            "user": "fpulse",
            "password": "secret",
        },
        pyodbc,
        timeout=7,
    )

    assert "DRIVER={ODBC Driver 18 for SQL Server};" in conn_str
    assert "SERVER=sql.example.com,14330;" in conn_str
    assert "DATABASE=warehouse;" in conn_str
    assert "UID=fpulse;PWD=secret;" in conn_str
    assert "Encrypt=optional;" in conn_str
    assert "TrustServerCertificate=yes;" in conn_str
    assert "Connection Timeout=7;" in conn_str


def test_build_conn_str_supports_windows_auth_and_explicit_encrypt() -> None:
    pyodbc = FakePyodbc(["ODBC Driver 17 for SQL Server"])

    conn_str = build_mssql_odbc_conn_str(
        {
            "host": "localhost",
            "database": "master",
            "windows_auth": True,
            "encrypt": "strict",
            "trust_server_certificate": False,
        },
        pyodbc,
    )

    assert "Trusted_Connection=yes;" in conn_str
    assert "Encrypt=strict;" in conn_str
    assert "TrustServerCertificate=no;" in conn_str
