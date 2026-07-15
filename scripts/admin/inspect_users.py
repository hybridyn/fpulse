"""Diagnostic: dump users from the F-Pulse SQLite DB.

Usage:
    python inspect_users.py                  # uses $FPULSE_DATA_DIR/fpulse.db
    python inspect_users.py /path/to/fpulse.db
    FPULSE_DATA_DIR=/data python inspect_users.py

Resolution order for the DB path:
    1. CLI arg (sys.argv[1])
    2. $FPULSE_DB environment variable (explicit DB path)
    3. $FPULSE_DATA_DIR/fpulse.db
    4. ./data/samples/fpulse.db (repo default)
"""
import sqlite3, json, sys, os


def _resolve_db_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    if os.environ.get("FPULSE_DB"):
        return os.environ["FPULSE_DB"]
    data_dir = os.environ.get("FPULSE_DATA_DIR")
    if data_dir:
        return os.path.join(data_dir, "fpulse.db")
    # Repo default — relative to this script's repo root.
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "data", "samples", "fpulse.db")


DB = os.path.abspath(_resolve_db_path())
if not os.path.exists(DB):
    print("DB not found:", DB)
    print("Set FPULSE_DB or FPULSE_DATA_DIR, or pass the path as the first argument.")
    sys.exit(1)

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
rows = c.execute("SELECT id, email, data FROM users").fetchall()
print(f"Found {len(rows)} user(s) in {DB}")
for r in rows:
    d = json.loads(r["data"])
    print(
        f"  id={r['id']!r:20}  email={r['email']!r:30}  role={d.get('role')!r:15}"
        f"  active={d.get('is_active', True)}  has_pw={'password_hash' in d}"
    )

# Look for INITIAL_ADMIN_PASSWORD.txt next to the db
pwfile = os.path.join(os.path.dirname(DB), "INITIAL_ADMIN_PASSWORD.txt")
print("\nInitial password file:", "EXISTS" if os.path.exists(pwfile) else "MISSING", "-", pwfile)
if os.path.exists(pwfile):
    print("---")
    print(open(pwfile).read())
    print("---")

# Session count
try:
    sessions = c.execute("SELECT COUNT(*) as n FROM sessions").fetchone()
    print(f"\nActive sessions: {sessions['n']}")
except Exception as e:
    print("Session lookup failed:", e)

# Workspaces
try:
    ws = c.execute("SELECT id, data FROM workspaces").fetchall()
    print(f"\nWorkspaces: {len(ws)}")
    for w in ws:
        d = json.loads(w["data"])
        print(f"  id={w['id']!r}  name={d.get('name')!r}")
except Exception as e:
    print("Workspace lookup failed:", e)
