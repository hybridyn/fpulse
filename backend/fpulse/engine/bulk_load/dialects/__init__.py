"""Dialect plugins.

Each plugin module imports `register` from the parent package's registry
and calls it at import time so the runner just needs to `import dialects`
to wire everything up.

Adding a new dialect:
  1. Drop a new file here, e.g. `snowflake.py`.
  2. Implement `BulkLoaderProtocol` (see `..types`).
  3. Call `register(YourPlugin())` at module bottom.
  4. Add the import to this `__init__` so it's discovered.
  5. Write tests under `backend/tests/test_bulk_load_<dialect>.py`.

Optional drivers (psycopg2, snowflake-connector-python, etc.) MUST be
imported lazily inside `is_available()` and `load()` so a host without
the driver can still import the package without crashing.
"""

from __future__ import annotations

# Importing each dialect module triggers its register() call. Each plugin
# guards its optional driver import inside is_available() so this is safe
# on hosts that don't have every database driver installed.
from . import postgres  # noqa: F401
from . import snowflake  # noqa: F401
from . import bigquery  # noqa: F401
from . import redshift  # noqa: F401
from . import mssql  # noqa: F401

__all__ = ["postgres", "snowflake", "bigquery", "redshift", "mssql"]
