"""The /api/files data-file picker must never surface bootstrap secrets.

Regression guard for the node-review finding: the data dir doubles as the
location of the one-time INITIAL_ADMIN_PASSWORD.txt and the master secret
key, and /api/files listed every .csv/.txt/etc in it -> the Source node's
file picker exposed the initial admin password.
"""
from fpulse.main import _is_listable_data_file


def test_secret_and_internal_files_excluded():
    for name in (
        "INITIAL_ADMIN_PASSWORD.txt",
        "initial_admin_password.txt",
        "secret.key",
        "fpulse_secret.key",
        ".env",
        ".fpulse_runtime",
        "db_password.txt",
        "my_secret_notes.txt",
    ):
        assert _is_listable_data_file(name) is False, name


def test_real_data_files_listed():
    for name in (
        "customers.csv",
        "orders.parquet",
        "report.xlsx",
        "events.json",
        "notes.txt",
        "data.tsv",
        "feed.xml",
    ):
        assert _is_listable_data_file(name) is True, name


def test_non_data_extensions_excluded():
    for name in ("app.db", "archive.zip", "script.py", "image.png"):
        assert _is_listable_data_file(name) is False, name
