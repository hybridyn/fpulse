"""Seed data — bundled sample files copied into ``FPULSE_DATA_DIR/samples/``
on first startup so the "First pipeline" template in the OSS Templates
gallery can run end-to-end without any external system.

The seed copy is idempotent: it only runs when the target file is missing,
so an operator who deletes / replaces the sample on their host won't have
it silently overwritten on the next restart.

See ``fpulse.main._seed_demo_data`` for the wiring.
"""

from pathlib import Path

SEED_DATA_DIR = Path(__file__).parent
