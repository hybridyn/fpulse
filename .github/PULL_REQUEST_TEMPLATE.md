# Summary

<!-- One paragraph: what does this PR do, and why? -->

# Changes

<!-- Bullet list of concrete changes. -->

# Testing

<!--
How did you verify this works?
- Unit tests added / modified?
- Manual test steps?
- Screenshots for UI changes?
-->

# Checklist

- [ ] Code passes `pytest -q` from `backend/`
- [ ] Frontend builds with `npm run build` (if frontend touched)
- [ ] Architecture invariants pass: `pytest backend/tests/test_architecture_invariants.py`
- [ ] changelog.md updated under the unreleased section (for user-visible changes)
- [ ] No competitor product names in user-visible text or comments (ADF / SSIS / Talend / n8n etc.) — describe behavior plainly
- [ ] If this touches the open-core boundary: OSS code does not import `fpulse.plus.*` (the architecture test will fail otherwise)
- [ ] CLA signed (the bot will tell you if not)
