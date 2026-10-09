# Security & Diagnostics

Open Settings > Security & Diagnostics. Legacy #trust and #cert-matrix links
resolve to this Settings section. The Insights navigation no longer lists Trust.

The signed-in view reads /api/trust/diagnostics. Provider resolution uses the
current account and workspace and excludes credentials and private endpoints.
The legacy public /api/trust/posture endpoint remains available; version 2.0
distinguishes unknown values from false and replaces hardcoded OK controls.

Status meanings:
- Verified: the stated, limited check completed, with its check timestamp.
- Configured: configuration was found; enforcement or connectivity was not tested.
- Not checked: no supporting measurement is available.
- Failed: the check could not complete.

Telemetry verification covers reading the consent setting only. It is not a
network-egress test. Unmeasured security controls have no verification timestamp.
The response-generation timestamp is not an audit date.

Connector scores describe manifest validation and declared capabilities, not live
authentication, sink correctness, production readiness or compliance certification.
Evaluation results describe the recorded test run only. Documentation describes
intended behavior and is not proof of enforcement.

No database migration, provider request, connector job or security-setting change
is performed by opening or refreshing this page. Provider resolution retains the
existing local Ollama discovery behavior.
