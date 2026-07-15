"""
F-Pulse feature flag module — runtime kill-switches.

Addresses reviewer critique: "rollback = redeploy is too slow." Every
new behavior introduced in Week 1 / Week 2 / Q2 is gated behind a flag
that can be flipped via env var OR /api/admin/flags without a restart.

Public surface:
    from fpulse.flags import flags
    if flags.idempotent_sink.enabled:
        ...
"""
from .feature_flags import flags, FeatureFlag, FlagStore

__all__ = ["flags", "FeatureFlag", "FlagStore"]
