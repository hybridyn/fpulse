"""One-shot helper: reset a user's password in the F-Pulse OSS user store.

Usage (from the backend dir so the imports resolve):
    cd D:\\Siva\\hybridyn-f-pulse-oss\\backend
    python ..\\samples\\free-api-pipelines\\reset_password.py admin@fpulse.local NewPass123!

After running, log into the UI with that email/password to confirm, then run:
    cd ..\\samples\\free-api-pipelines
    .\\import.ps1 -Email admin@fpulse.local -Password "NewPass123!"
"""

from __future__ import annotations

import os
import sys

# Make sure the backend package is importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "backend"))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python reset_password.py <email> <new_password>")
        return 2

    email, new_password = sys.argv[1], sys.argv[2]

    # Match how the running backend resolves its DB path
    from fpulse.main import _resolve_db_path  # type: ignore

    try:
        db_path = _resolve_db_path()
        print(f"Using DB at: {db_path}")
    except Exception:
        # Fallback: try the path inspect_users.py reported
        from fpulse.storage.database_factory import get_database  # type: ignore
        db = get_database()
        print(f"Using DB (factory): {db}")
        db_path = None

    from fpulse.auth.models import User  # type: ignore
    from fpulse.auth.store import UserStore  # type: ignore

    if db_path:
        from fpulse.storage.sqlite_db import SqliteDb  # type: ignore
        db = SqliteDb(db_path)

    store = UserStore(db=db)
    user = store.get_user_by_email(email)
    if not user:
        print(f"No user with email '{email}'. Existing users:")
        for u in store.list():
            print(f"  - {u.email}  (role={u.role})")
        return 1

    user.set_password(new_password)
    store.save(user)
    print(f"OK: password reset for {email}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
