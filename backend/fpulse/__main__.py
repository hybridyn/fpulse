"""F-Pulse package entry point.

Enables ``python -m fpulse <command>`` so operators can invoke the CLI
without a separate console_scripts shim. In particular this is the
canonical way to run the dev-seed:

    python -m fpulse seed-admin

Stage 1 added this file so the seeded super_admin password reset can
happen WITHOUT booting the server — a precondition for moving the
write out of main.py module-import time.
"""

from __future__ import annotations

from fpulse.cli import main


if __name__ == "__main__":
    main()
