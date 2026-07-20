---
name: Bug report
about: Something is broken or behaving unexpectedly
title: "[bug] "
labels: ["bug", "needs-triage"]
assignees: []
---

<!--
Thanks for taking the time to report a bug. Before filing:

  • Run `fpulse version` and `fpulse health` — both should succeed
  • Search existing issues (open + closed) for your symptom
  • If this is a SECURITY issue, do NOT file here. Use GitHub's
    "Report a vulnerability" under the Security tab, or email
    security@hybridyn.com. See security.md.
-->

## What happened?

<!-- One-paragraph summary of the unexpected behavior. -->

## Steps to reproduce

1.
2.
3.

## Expected behavior

<!-- What you thought should happen. -->

## Actual behavior

<!-- What actually happened. Paste error messages verbatim if any. -->

## Environment

- F-Pulse version: <!-- output of `fpulse version` -->
- Install method: <!-- Docker / pip / packaged installer / source -->
- OS + version: <!-- Windows 11 / Ubuntu 22.04 / macOS 14.5 etc. -->
- Python version: <!-- output of `python --version` if running from source -->
- Browser: <!-- only if a UI bug -->

## Logs / screenshots

<!--
Wrap log lines in ``` blocks. Trim to the relevant ~30 lines.
For runtime errors include the request id from the response header
(`X-Request-ID`) — it lets us correlate with server logs.
-->

```

```

## Anything else?

<!-- Optional context, suspected cause, related issues, workaround. -->
