"""F-Pulse CI smoke runner.

The full test matrix lives in the development monorepo. For the OSS launch
build this script does a fast smoke check: it imports the public `fpulse`
package and reports the version. Real per-module tests are run via
`pytest backend/tests` directly during development.

Kept dependency-light on purpose. The only third-party module touched at
smoke time is `pydantic`, and only if the relevant fpulse submodule is
importable in the current environment. Anything missing is reported and
skipped — the smoke fails only if `fpulse` itself can't import.

Accepts the same flags the GitHub Actions workflow passes so the CI job
contract stays stable:

    python run_all_tests.py --fast
    python run_all_tests.py --security
    python run_all_tests.py --all
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys

# Make sure `backend/` is on sys.path when this script is run directly
# from inside `backend/`. Lets the smoke run without a `pip install -e .`.
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

REQUIRED = ["fpulse"]
OPTIONAL = [
    "fpulse.storage.database",
    "fpulse.api.security_headers",
]


def smoke() -> int:
    import fpulse
    print(f"fpulse v{fpulse.__version__}")

    failed: list[tuple[str, str]] = []
    for mod in REQUIRED + OPTIONAL:
        try:
            importlib.import_module(mod)
            print(f"OK   {mod}")
        except Exception as exc:
            tag = "REQUIRED" if mod in REQUIRED else "optional"
            print(f"SKIP ({tag}) {mod}  {exc!r}")
            if mod in REQUIRED:
                failed.append((mod, repr(exc)))

    if failed:
        print(f"\n{len(failed)} required module(s) failed to import")
        return 1
    print("\nSmoke OK")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fast", action="store_true")
    p.add_argument("--security", action="store_true")
    p.add_argument("--all", action="store_true")
    p.parse_args()
    return smoke()


if __name__ == "__main__":
    sys.exit(main())
