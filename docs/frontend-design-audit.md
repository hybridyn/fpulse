# F-Pulse OSS Frontend Design Audit

Date: 2026-09-17. Verdict: visual acceptance NOT passed.

Integration note: changes were reapplied on `hybridyn/fpulse` main at e54fc47
in an isolated worktree. Preserved organization release gates, manual suffixed
PyPI publishing, existing Linux installer jobs and packaged documentation
resolution. Revalidated 123 frontend tests, 53 targeted backend/workflow tests,
TypeScript, production build, built CSS and strict packaging preflight.
Original-checkout visual observations below were not rerun against a separately
served integration build; full browser acceptance remains open.
This is an evidence-backed first pass, not full application or accessibility certification.
The initial audit was read-only; subsequent implementation is recorded below.

## Implementation Update (2026-09-17)

User constraint: **do not change colors**. The experimental palette changes
were reverted, including navigation, gradients, canvas, tables, KPI cards,
form text colors, and button fills. Color recommendations below are audit
observations only, not authorization to change the palette.

Retained improvements:
- Wrapping editor/page headers and context bar; responsive table toolbars.
- Compact editor overlay rails below 1280px, one expanded panel at a time,
  Escape dismissal, and a desktop panel-width cap to protect canvas space.
- Configuration bounds track toolbar height; small-screen forms scroll.
- Larger supporting/configuration text without changing foreground colors.
- Visible keyboard focus, named settings switches, dialog focus restoration,
  reduced-motion support, and stationary node hover geometry.
- Original six-second logo rotation restored at user request; it still pauses
  on hover and is disabled when reduced motion is requested.

Validation: live in-app-browser checks measured 944px of canvas at a 1024px
viewport and 660px at 740px (previously 414px and 130px). Toolbar groups did
not overlap in the inspected desktop/narrow layouts. A pivot Test Node journey
returned 10 rows and five columns; no downstream database sink was executed.
The existing test action advanced the displayed workflow version from v25
to v26; no node parameters were manually edited.

The production frontend build and unit suite are rechecked after palette
restoration. A read-only Playwright layout spec was added for six widths,
but has not been executed as a standalone suite or approved as a visual baseline.
Full route/state/browser coverage, 200% zoom, complete keyboard-only journeys,
and remaining contrast findings stay open. OSS currently
forces light mode (`useDarkMode` returns false); dark mode was not enabled.

### Follow-Up Implementation and Checks

- Restored page-centered submenu placement at the user's request across the
  shared page header and editor toolbar. Desktop uses equal flexible side
  columns; actions wrap within their own column. Below 1024px the submenu
  occupies a separate centered row. No colors or animations changed.
  Live geometry at 1920px confirmed centering within 0.01px on Editor,
  Connections, Storage, Insights, Settings, Help and Pool. Editor title/tabs/
  actions were non-overlapping at 1024px. Added a centering assertion to the
  existing layout spec (standalone E2E execution remains pending).

- Consolidated Publish/Revoke, deployment and activation actions into the
  existing pipeline More actions popover. Run/Stop, Edit and Move remain inline.
  Existing permission guards, callbacks and colors were preserved.
- Popover is portaled outside table clipping, flips above low rows, clamps to
  viewport bounds, focuses its first enabled action, supports arrow/Home/End
  navigation, and restores trigger focus on Escape. Outside pointer and focus
  departure dismiss it. Four focused regression tests cover this behavior.
- Empty-editor Copilot prompt now scrolls within the available canvas. Its
  footer wraps; the template picker clamps to narrow/short viewports and
  dismisses with Escape. Empty canvases no longer show an irrelevant minimap
  or density selector. Added two focused layout/accessibility tests.
- Live checks: published-row menu focused Revoke, ArrowDown reached Deactivate,
  and Escape restored More actions. Bottom-row menu opened above its trigger.
  No lifecycle actions or saved pipeline edits were performed in this follow-up.
- Dashboard, Projects, Pipelines, Connections, Pool, Storage, Insights, Settings
  and Help loaded at 1280px. Main-page measurements showed no document-level
  horizontal overflow. Executions, Templates, Credentials, Managed Tables and
  Pipeline Outputs also loaded through visible navigation. These are route
  smoke checks, not certification of every control or error/loading state.
- At 390px, Pipelines and Help reflowed and mobile Menu navigation worked.
  The template popover occupied x=8..382, wholly within the 390px viewport.
  At 1280px the editor canvas measured 670px and toolbar groups were disjoint.
  At 640x450 it measured 560px with no document overflow; the short-height
  inspection exposed the empty-prompt clipping fixed above.
- Confirmed live logo CSS remains `logo-flip`, duration `6s`.
- Browser zoom shortcuts did not change viewport or device scale, so true
  200% zoom remains UNVERIFIED. Narrow viewport testing is not a substitute.
- Only the in-app browser was available. Chrome/Firefox/Safari validation,
  exhaustive keyboard journeys and all route states remain release QA work.
  Contrast findings are deferred under the explicit no-color-change constraint.

Verification: TypeScript and production Vite build passed. The complete
single-worker suite passed: 123 tests across 16 files. Vite reports an outdated
Browserslist dataset warning.

## Method and Coverage

Inspected the running application at http://localhost:5174 with screenshots,
accessibility trees, read-only DOM geometry/computed styles, and source inspection.
Chrome automation was unavailable; observations are from the Codex in-app browser.
Transient capture artifacts were excluded from findings. Geometry was checked
after viewport changes rather than relying on a scaled screenshot.

| Surface | Inspection | Coverage |
| --- | --- | --- |
| Dashboard | Populated desktop composition | Partial |
| Pipelines | Populated list, toolbar, KPIs, row actions at 1440x900 | Reviewed |
| Editor | Published 10-node pipeline at 1440x900, 1024x768, 740x900 | Reviewed |
| Pivot configuration | Existing values, form, preview drawer at 1440x900 | Reviewed without execution/editing |
| Connections | Populated desktop table and summary strip | Reviewed |
| Storage / Files | Populated desktop table and summary strip | Reviewed |
| Other routes, themes, empty/error/loading states | Not comprehensively inspected in this pass | Open |

Duckle is a user-supplied visual reference only. Its screenshot supports a
comparison of visual hierarchy, not claims about its reliability, accessibility,
or functional superiority. No code copying or wholesale dark-theme migration is proposed.

## Findings

### VA-01: Editor toolbar controls overlap (P1, measured)

At 1440x900, Templates occupies x=826.01..944.08 and y=133..173.
Parameters occupies x=870.63..996.56 and y=132.20..173.
Their rectangles overlap by approximately 73px horizontally. The screenshot
also shows Templates partially hidden by the action group.

Source to investigate: `frontend/src/components/Toolbar.tsx:627`, which changes
to equal outer grid tracks at the xl breakpoint. This source correlation is not
a completed root-cause fix.

Acceptance: tabs and actions have disjoint hit areas at all supported widths,
including long pipeline names and 200% zoom. Overflow must use an intentional
menu or a second row, never overlapping tracks.

### VA-02: Editor sidebars consume the working canvas (P1, measured)

With both current sidebars open:
- 1024px viewport: canvas x=290..704, width=414px (40.4% of viewport).
- 740px viewport: canvas x=290..420, width=130px (17.6% of viewport).

At 740px, the mini-map and canvas toolbar are clipped; the pipeline is reduced
to a narrow strip while the assistant retains 320px. At 1024px, project/name/
validation content visibly collides in the canvas context bar.

Source to investigate: `frontend/src/components/EditorContextBar.tsx:225`
uses `grid-cols-[1fr_auto_1fr]`; this does not itself prove the sidebar root cause.

Acceptance: adopt a documented minimum useful canvas width (proposed 560px
for desktop editing). Below the available-width threshold, turn side panels into
drawers or show only one at a time. Under the supported editing minimum, provide
an intentional compact/read-only mode rather than a broken editor. Context
labels must reflow or truncate independently without overlapping.

### VA-03: Essential configuration guidance is too small and faint (P1, measured)

Pivot helper text beginning "The columns each output row is keyed by" has
computed font-size 10px and color rgb(148,163,184). That foreground against
white has calculated contrast approximately 2.56:1. The element itself has a
transparent background; this is a foreground-versus-white calculation, not
a full composited contrast audit of every control.

Acceptance: use at least 12px for supporting guidance and a proposed 13-14px
baseline for ordinary form/table content. Validate actual composited colors;
target at least 4.5:1 for ordinary text and 3:1 for meaningful control boundaries.
Font size alone does not establish accessibility compliance. Essential guidance
must not be styled like a disabled control.

### VA-04: Decorative surfaces compete with data and actions (P2, visual judgment)

Observed across Dashboard, Pipelines, Connections, Storage, and Editor:
purple-glowing brand enclosure, amber gradient navigation, slate-gradient page
headers, multiple saturated KPI colors, dark table headers with yellow labels,
cream canvas/page backgrounds, and purple assistant accents. The pivot modal
adds a multicolor outline and gradient AI button.

The problem is not the number of colors alone. Several decorative elements have
similar visual weight to the action or information the user needs next.

Source anchors: `frontend/src/components/Sidebar.tsx:541`,
`frontend/src/components/Sidebar.tsx:717`,
`frontend/src/components/pages/DashboardPage.tsx:284`,
`frontend/src/components/shared/PageHeader.tsx:90`,
`frontend/src/styles/globals.css:5`.

Acceptance: define shared semantic tokens for neutral surfaces, primary action,
selection, success, warning, error, and focus. Use quiet neutral KPI surfaces;
emphasize the number or a small meaningful status marker. Reserve strong fills
for primary actions and genuinely urgent states. Approve light and dark variants
separately; making everything dark is not a remedy.

### VA-05: Selected navigation attracts attention indefinitely (P2, source-confirmed)

`globals.css:150` gives the active navigation item an infinite brightness cycle;
its pseudo-element has an infinite shimmer. The logo also flips repeatedly.
The inspected reduced-motion rule at line 215 disables the logo animation but
does not include the menu animations; no other reduced-motion rule was found
in this stylesheet. Browser-level reduced-motion emulation was not tested.

Acceptance: ordinary navigation selection is static. Motion communicates loading,
execution, or a user-triggered transition. All nonessential animations respect
reduced-motion preferences. Hover does not move a graph node's apparent position.

### VA-06: Repeated secondary controls occupy excessive attention (P2, visual judgment)

Pipeline rows expose Run, lifecycle actions, Deactivate, Edit, Move, and More
simultaneously. Multiple pill/outlined treatments compete with names and status.
The editor likewise presents strong Run and Publish actions alongside several
other bordered controls and duplicated context badges.

Acceptance: define one primary action per context, clear secondary actions, and
a consistent overflow menu for occasional operations. Destructive/lifecycle
actions remain discoverable and distinctly labeled, not mistaken for routine Run.
Do not replace unfamiliar actions with unexplained icons.

### VA-07: Form structure is useful but its framing is heavy (P2, visual judgment)

The pivot configuration has good semantic grouping: Data In, parameters, Data Out,
and input/output/schema/JSON inspection. However, the blurred backdrop, bright
outline, adjacent navigation cards, preview drawer, and separate AI treatment
create several competing layers. Important graph context is obscured.

Acceptance: preserve the schema-aware form and preview functionality. Prototype
a restrained inspector or docked configuration surface against the same task;
retain a modal where field complexity justifies it. A side inspector is a design
candidate, not an automatic requirement for every node.

## Strengths to Preserve

- Existing global navigation and reusable PageHeader provide a consolidation point.
- Schema-aware field selection and per-node previews support real developer work.
- Table filters, column controls, and exports already support dense workflows.
- Published-state messaging and version/validation indicators are useful context.
- The light theme can work well; it does not need to imitate Duckle's dark theme.

## Visual Acceptance Gate

Test these independently of functional unit tests:

| Check | Required Evidence |
| --- | --- |
| Layout | Screenshots and element geometry at 1920x1080, 1440x900, 1280x800, 1024x768, and narrow widths; zero unintended overlaps or inaccessible actions |
| Density | Editor retains its minimum useful area; tables scroll within their own surface; filters and primary actions remain reachable |
| Typography | Shared type scale, readable supporting text, consistent line height; long names and localized text do not break controls |
| Contrast | Measured text, focus, border, and state contrast on actual backgrounds in each theme |
| Color and hierarchy | Semantic color tokens; one clear primary action per context; selection remains distinct from error/warning |
| Components | Consistent button/input heights, icon sizing, spacing, radius, tabs, tables, badges, and dialogs |
| States | Default, hover, keyboard focus, disabled, selected, loading, empty, validation error, success, and failure captured |
| Accessibility | Keyboard order, visible focus, accessible names, zoom/reflow, reduced motion; automated checks plus manual inspection |
| Content stress | Long names, many nodes/columns, empty datasets, many errors, and overflowing menus inspected |
| Regression | Approved screenshots tied to a commit and deterministic fixture; dynamic timestamps/data masked; intentional visual changes reviewed |

No overall numeric maturity score is assigned: coverage is incomplete and an
arbitrary score would obscure the confirmed P1 failures.

## Recommended Order

1. Fix toolbar/context-bar overlap and sidebar breakpoints.
2. Correct text readability and keyboard/focus/reduced-motion gaps.
3. Consolidate tokens and shared controls; remove competing decorative treatments.
4. Validate one complete editor/configuration/preview journey before rolling the
   styling system across tables, dashboard, and settings.
5. Complete the route/state/theme/browser matrix and approve regression baselines.

This document is the initial design backlog and acceptance checklist. It does not
sign off every route, responsive size, theme, browser, or accessibility requirement.
