"""Opt-in error reporting and usage telemetry for F-Pulse OSS.

DEFAULT POSTURE: Off. Nothing leaves the box unless the operator explicitly
opts in via Settings → Privacy → "Send anonymous error reports".

When opt-in is granted, F-Pulse sends ONLY:
  - Crash reports (uncaught exceptions in the API process)
  - F-Pulse version + Python version + OS family
  - Feature flag state (which features are enabled)
  - The exception type + sanitized stack trace (no row data, no SQL, no
    file paths beyond the F-Pulse package, no env vars)

F-Pulse NEVER sends:
  - Pipeline data, row contents, query results
  - SQL text, configuration values, environment variables
  - Connection strings, credentials, API keys, paths to user files
  - User IDs, workspace names, any identifying metadata

Implementation rules:
  - All telemetry is gated by `telemetry_enabled` in admin_settings (default: false)
  - The send path uses a 5s timeout and silently drops on failure
  - The opt-in is per-installation, not per-user
  - Operators can revoke at any time; revocation flushes the queue
  - The TRUST.md page surfaces the exact payload schema so customers can audit it

May 4 2026: the sender (`sender.py`) is now implemented. The receiving
endpoint (`telemetry.hybridyn.com`) is still pending; until it's live,
events queue locally and silently drop on POST failure. This is the
correct fail-mode — when the receiver lights up, opted-in installs
start posting automatically without any code change on the client.
"""

from .consent import is_telemetry_enabled, set_telemetry_enabled, TELEMETRY_PAYLOAD_SCHEMA
from .sender import (
    build_event,
    send_event,
    revoke_and_flush,
    get_queue,
    get_installation_id,
    TelemetryQueue,
)

__all__ = [
    "is_telemetry_enabled",
    "set_telemetry_enabled",
    "TELEMETRY_PAYLOAD_SCHEMA",
    "build_event",
    "send_event",
    "revoke_and_flush",
    "get_queue",
    "get_installation_id",
    "TelemetryQueue",
]
