"""F-Pulse package entry point.

Enables ``python -m fpulse <command>`` so operators can invoke the CLI
without relying on the console_scripts shim being discoverable on PATH.
On Windows this is the canonical way to avoid "fpulse is not recognized"
when Python's Scripts directory was not added to PATH.

In particular this is the canonical way to run the dev-seed:

    python -m fpulse seed-admin

Stage 1 added this file so the seeded super_admin password reset can
happen WITHOUT booting the server — a precondition for moving the
write out of main.py module-import time.
"""

from __future__ import annotations

from fpulse.cli import main


if __name__ == "__main__":
    main()
