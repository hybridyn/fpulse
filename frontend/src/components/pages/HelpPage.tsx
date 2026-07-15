import { useCallback, useEffect, useState } from 'react';
import { navigateToSubRoute } from '../../router';
import { useDarkMode } from '../../hooks/useDarkMode';
import { navigateTo } from '../../router';
import { canAccessAdmin } from '../../auth/permissions';
import { usePageContext } from '../../hooks/usePageContext';
import DocsReference from '../help/DocsReference';
import ConnectorCoverage from '../help/ConnectorCoverage';
import HelpFeedback from '../help/HelpFeedback';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';
import Icon from '../shared/Icon';

type HelpTab = 'getting-started' | 'how-to' | 'shortcuts' | 'nodes' | 'reference';

/* ── Getting Started Steps ── */
const STEP_ICON_PROPS = { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const };

const GETTING_STARTED = [
  {
    step: 1,
    title: 'Create Your First Pipeline',
    // 2026-05-22 — icon swap: was the lightning-bolt polygon (AI /
    // brand semantics). The step's content directs the user to the
    // Editor page, whose canonical icon is the pencil. The
    // lightning bolt is reserved for AI / brand mark per the
    // icon-consistency memory rule — using it here misled new
    // users into reading "create pipeline" as an AI-generated
    // action when in fact step 1 is the manual canvas flow.
    icon: (
      <svg {...STEP_ICON_PROPS}>
        <path d="M12 20h9" />
        <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
      </svg>
    ),
    content: [
      'Go to the **Editor** page from the navigation bar.',
      'You can start in three ways:',
      '- **Chat**: Describe your pipeline in plain English (e.g. "Load orders.csv, filter by status, output to parquet")',
      '- **Template**: Click "Start with a Template" and choose Simple ETL, Dedup, Aggregation, or Data Quality',
      '- **Drag & Drop**: Drag nodes from the right-side panel onto the canvas',
      'Or import 18 ready-made sample pipelines from `samples/free-api-pipelines/` via the included `import.ps1` script.',
    ],
  },
  {
    step: 2,
    title: 'Configure Your Nodes',
    icon: (
      <svg {...STEP_ICON_PROPS}><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" /></svg>
    ),
    content: [
      'Click any node on the canvas to open its configuration panel.',
      'Each node type has different settings — for example:',
      '- **CSV Source**: Set the file path, delimiter, and encoding',
      '- **Filter**: Define column conditions (e.g. amount > 100)',
      '- **Database Sink**: Choose connection, table, and write mode',
      'Node names can be edited by clicking the name label.',
    ],
  },
  {
    step: 3,
    title: 'Connect Nodes Together',
    icon: (
      <svg {...STEP_ICON_PROPS}><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" /><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" /></svg>
    ),
    content: [
      'Drag from the **output handle** (bottom) of one node to the **input handle** (top) of another.',
      'Click the edge label to change conditions:',
      '- **Completion** — always runs next step',
      '- **Success** — only runs if parent step succeeds',
      '- **Failure** — only runs if parent step fails',
      'Click the × on an edge to delete the connection.',
    ],
  },
  {
    step: 4,
    title: 'Run Your Pipeline',
    icon: (
      <svg {...STEP_ICON_PROPS}><polygon points="5 3 19 12 5 21 5 3" /></svg>
    ),
    content: [
      'Click **Run All** in the toolbar to execute the entire pipeline.',
      'You can also right-click any node and choose **Execute from here** to run a single step.',
      'Results appear in the bottom preview panel — switch between **Table**, **Schema**, and **JSON** views.',
      'Failed steps show error details with suggestions on how to fix them.',
    ],
  },
  {
    step: 5,
    title: 'Save, Schedule & Monitor',
    icon: (
      <svg {...STEP_ICON_PROPS}><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" /><polyline points="17 21 17 13 7 13 7 21" /><polyline points="7 3 7 8 15 8" /></svg>
    ),
    content: [
      'Click **Save** to store your pipeline with a name, description, project, and tags.',
      'Use the **Schedule** button in the toolbar (or Save dialog → Schedule tab) to set up automatic runs.',
      'Use the **Alert** button in the toolbar (or Save dialog → Alerts tab) to get notified on failure, success, or long-running pipelines.',
      'Visit the **Executions** page to see run history, step-by-step logs, and performance stats.',
    ],
  },
  {
    step: 6,
    title: 'Trigger From Another App',
    icon: (
      <svg {...STEP_ICON_PROPS}><path d="M5 12h14" /><path d="M12 5l7 7-7 7" /></svg>
    ),
    content: [
      'Pipelines can be triggered over HTTP and accept parameters per run — point any external app, CI job, or webhook at the run endpoint.',
      '- **Authenticated**: `POST /api/execute/workflow/{id}` with body `{"parameter_values": {"NAME": "value"}}`',
      '- **Public webhook**: publish the pipeline as a Gateway endpoint to get a stable URL + API key — `POST /api/published/{your-path}` with params as the body',
      'The values in `parameter_values` populate the pipeline\'s declared **parameters** — reference them in any step field as `${param.NAME}`.',
      'See **Help → Documentation → "Triggering Pipelines from the API"** for the full guide with curl examples, declared inputs, and overlap policies.',
    ],
  },
  {
    step: 7,
    title: 'Use the Copilot for help',
    icon: (
      <svg {...STEP_ICON_PROPS}><rect x="3" y="11" width="18" height="10" rx="2" /><circle cx="12" cy="5" r="2" /><path d="M12 7v4" /><line x1="8" y1="16" x2="8" y2="16" /><line x1="16" y1="16" x2="16" y2="16" /></svg>
    ),
    content: [
      'The **F-Pulse Copilot** dock appears in the bottom-right of every page (including the Editor) by default — it is now the single Copilot across the whole app. Click to open.',
      'Page-aware: on Pipelines it knows your pipelines; on Executions it can diagnose failures; in the Editor it sees the selected node and can write SQL.',
      'Type **/** in the input for slash commands: `/sql`, `/fix`, `/explain-code`, `/diagnose`, `/health`, `/cost`.',
      'When the Copilot drafts a pipeline change, a **diff preview** shows exactly what will be added, removed, or modified before you click Confirm — keys only, never values, so credentials never leak.',
      'Toggle **Safety mode** in Settings → General → AI Assistant to block write tools while exploring. Read-only tools and chat keep working.',
      'Configure your provider in **Insights → AI Provider**. The Copilot works with any compatible LLM provider; pick whichever fits your budget and latency target.',
    ],
  },
];

/* ── How-To Guides ── */
const HOW_TO_GUIDES = [
  {
    category: 'Install & Run F-Pulse as an App',
    icon: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /><polyline points="3.27 6.96 12 12.01 20.73 6.96" /><line x1="12" y1="22.08" x2="12" y2="12" /></svg>,
    guides: [
      {
        title: 'Run F-Pulse as an always-on app (Windows)',
        steps: [
          'F-Pulse is a local server + a browser UI. To make it feel like a regular app — starts on its own, no terminal to keep open — install it as a background service.',
          'In an **Administrator** PowerShell: `python -m fpulse install-service --at-boot` registers a Windows task that starts F-Pulse at **boot as SYSTEM** — so it is up even before anyone logs in, restarts itself if it crashes, and runs on battery.',
          'Leave off `--at-boot` for a per-user install that starts at **logon** instead (no admin needed).',
          'Open it anytime at **http://localhost:8001**, or use the **F-Pulse desktop icon** — it opens a clean Edge app window (no address bar). The window is only a viewer; closing it never stops the server.',
          'Stop / start / remove: `schtasks /End /TN FPulse`, `schtasks /Run /TN FPulse`, or `python -m fpulse uninstall-service` (your data dir is preserved).',
          'Check a build is healthy without starting a server: `python -m fpulse selftest` imports the whole stack and reports OK.',
        ],
      },
      {
        title: 'Do pipelines keep running after I sign out or close the window?',
        steps: [
          '**Yes.** All execution happens in the F-Pulse *server* (the always-on service), not the browser. Signing out or closing the window only closes your *view*.',
          '**Running pipelines** finish in the server\'s worker pool regardless.',
          '**Scheduled pipelines** fire from the server\'s scheduler loop (checks every ~30s), which runs in the server process and is not tied to any user session — so schedules run on time even with nobody logged in.',
          'The one requirement: the **service must be running** — exactly what the boot-time install guarantees. To truly stop everything: `schtasks /End /TN FPulse`, or uninstall the service.',
        ],
      },
      {
        title: 'Build a double-click installer (.exe)',
        steps: [
          'For distribution, F-Pulse can be packaged into a Windows installer that drops it into Program Files with Start-Menu + uninstall entries.',
          'One-time tooling: install **Inno Setup 6** (https://jrsoftware.org/isdl.php) and add PyInstaller to the venv (`pip install pyinstaller`).',
          'Build: `.\\installer\\windows\\build.ps1` → produces `installer\\windows\\output\\FPulse-Setup-<ver>.exe`. It builds the UI, freezes the backend, and runs a self-test before packaging.',
          'The installer preserves your data dir on uninstall, and its shortcuts open the browser directly (no Python). Unsigned builds trigger a one-time SmartScreen warning until you add a code-signing certificate.',
        ],
      },
      {
        title: 'Check for updates',
        steps: [
          'Open **Help** → the **Help & Feedback** card at the top → click **Check for updates**.',
          'It reads only the public GitHub release list and tells you whether a newer version exists. F-Pulse sends no usage data.',
          'To update a packaged install, download the newer `FPulse-Setup-<ver>.exe` and run it — it upgrades in place and keeps your data.',
        ],
      },
    ],
  },
  {
    category: 'Building Pipelines',
    icon: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" /></svg>,
    guides: [
      {
        title: 'Use the Guided Builder',
        steps: [
          'Open the chat panel in the Editor',
          'Type "guided builder" or "step by step"',
          'Follow the wizard: pick a source, configure it, add transforms, choose an output',
          'The pipeline is built automatically on the canvas as you go',
        ],
      },
      {
        title: 'Join Two Data Sources',
        steps: [
          'Drag two source nodes onto the canvas (e.g. CSV + Database)',
          'Add a **Join** node from the Combine section',
          'Connect both sources to the Join node',
          'Configure the join: select join type (inner, left, etc.) and the key columns to match on',
        ],
      },
      {
        title: 'Write Custom SQL Transforms',
        steps: [
          'Add a **Transform (SQL)** node after your data source',
          'Click the node to open the Expression Editor',
          'Write SQL using `source_table` to reference upstream data',
          'Example: `SELECT *, amount * 1.1 AS amount_with_tax FROM source_table WHERE status = \'active\'`',
        ],
      },
      {
        title: 'Use Flow Control (If/ForEach/Switch)',
        steps: [
          'Drag an **If Condition** node onto the canvas',
          'Set the condition expression (e.g. `row_count > 0`)',
          'Connect two downstream paths using **Success** and **Failure** edge conditions',
          'Click the edge label to toggle between Completion / Success / Failure',
        ],
      },
      {
        title: 'Review Copilot Changes with Diff Preview',
        steps: [
          'Ask the Copilot to draft or modify a pipeline (e.g. "add a Filter step after the CSV source").',
          'When the draft is ready, a **Confirmation card** appears in the chat with a **Change preview** section.',
          'Headline counts show at a glance: `+2 steps`, `~1 modified`, `+3 edges` (or `New pipeline — 5 steps, 4 connections`).',
          'Click **Show details** to expand the per-step list. Each line shows the kind (+, −, ~), step name, type, and (for modifies) which parameter keys changed.',
          'Parameter VALUES are never shown — only the keys — so credentials and SQL bodies stay out of the diff display.',
          'Click **Keep draft** to accept or **Cancel** to discard. Nothing is saved until you confirm.',
        ],
      },
      {
        title: 'Editing a Published Pipeline',
        steps: [
          'Auto-save is intentionally suspended on published pipelines so live scheduled runs can\'t be broken by exploratory edits.',
          'A **violet banner** appears above the editor toolbar reminding you that auto-save is off and any unsaved changes will be lost.',
          'Click **Save** in the editor to commit your changes as a new draft. The published version stays running until you re-publish.',
          'When you\'re ready to ship, click **Publish** to swap the live version with your new draft.',
        ],
      },
    ],
  },
  {
    category: 'Managing Data',
    icon: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14a9 3 0 0 0 18 0V5" /><path d="M3 12a9 3 0 0 0 18 0" /></svg>,
    guides: [
      {
        title: 'Set Up a Database Connection',
        steps: [
          'Go to **Connections** in the navigation',
          'Click **+ New Connection** and choose your database type',
          'Enter host, port, database name, and credentials',
          'Click **Test Connection** to verify, then save',
          'Use this connection in Database Source or Database Sink nodes',
        ],
      },
      {
        title: 'Store Credentials Securely',
        steps: [
          'Go to **Credentials** in the navigation.',
          'Click **Add Credential** and give it a name. Local (F-Pulse) is the only secret source in OSS.',
          'Add fields like Username, Password, API Key, Token. Sensitive fields are masked by default — toggle the eye icon to reveal.',
          'Pick a **Category** to group the entry (Database / Cloud / API / Messaging / Email / Data Warehouse / **AI Provider** / Other). Choosing **AI Provider** on a new credential pre-seeds `provider` / `api_key` / `base_url`, and that credential can then be imported on **Insights → AI Provider** via its *Use a saved credential* option — so your LLM key lives in the same governed store as every other secret.',
          'Set an expiry date to get notified before credentials expire.',
          'Toggle columns shown (Created By, Username, Source, Created, Expires) via the Columns button.',
          'Storage is encrypted at rest. See **Insights → Trust** for the full threat model.',
        ],
      },
      {
        title: 'Parameters & Variables',
        steps: [
          '**Parameters** are typed inputs you declare once and supply per run. Editor toolbar → **Parameters** → add a `name`, `type`, optional `default`, and `required`. Reference one in any field as `${param.<name>}` — e.g. set a CSV Source path to `/data/${param.dataset}.csv`. When a pipeline declares parameters, a small `${ }` button appears on text fields in the node config to insert a reference.',
          'Supply parameter values per run from the **Pipelines** page Run dialog, the API (`parameter_values` body), or a schedule. An empty value falls back to the declared default. System placeholders also work anywhere: `${utcnow:%Y-%m-%d}`, `${utcnow}`, `${run_id}`.',
          '**Expressions** — any field also accepts `{{ ... }}` expressions: `{{ $json.field }}` (current row), `{{ $now.startOf(\'month\').toFormat(\'yyyy-MM-dd\') }}` and `{{ $now.minus({\'days\': 7}).toFormat(\'yyyy-MM-dd\') }}` (dates), `{{ $(\'Node Name\').first().col }}` (an upstream node\'s output). Object literals accept quoted or unquoted keys — both `{\'days\': 7}` and `{ days: 7 }` work.',
          '**Runtime variables** — `{{ $vars.NAME }}` reads values set during the run by the **Set Variable** node (a constant or SQL expression) and the **Lookup (Activity)** node (a fetched value/row); they resolve per step in order, so a downstream step sees what an upstream step set.',
        ],
      },
      {
        title: 'Read or Write Files over FTP / SFTP',
        steps: [
          '**Read**: drag the **FTP / SFTP Source** node (Sources palette). **Write**: drag the **FTP / SFTP Sink** node (Outputs palette). Both share the same connection fields and protocol resolver.',
          'Set **Protocol** to `ftp`, `ftps` (FTP over TLS), or `sftp` (SSH File Transfer). SFTP needs the `paramiko` package — F-Pulse lazy-imports it and shows a clear install hint if it is missing.',
          '**Port** auto-defaults to 21 for ftp/ftps and 22 for sftp — leave it at 0 to use the default, or set an explicit port.',
          '**Auth**: enter Username + Password, or for SFTP paste a PEM **Private Key** for key-based auth (RSA / Ed25519 / ECDSA are tried in turn). Better still, save the host + secrets once as a **Connection** and pick it via the `connection_id` field so credentials never live in the pipeline JSON.',
          '**Write specifics**: choose the output **Format** (csv / json / parquet) and a **Remote File Path** (e.g. `/uploads/orders.csv`). The remote directory must already exist — the sink uploads the file but does not create folders. Like every sink, input rows pass through unchanged so you can chain another step after it.',
        ],
      },
      {
        title: 'Pick a SaaS Connector & Read Its Confidence Tier',
        steps: [
          'Drag a **REST / SaaS Connector** node and open the **Connector** dropdown — it lists every shipped manifest (Salesforce, HubSpot, Stripe, ServiceNow, …) plus any you have generated.',
          'Each option carries a **confidence tier** so you know how battle-tested it is: **Certified** (shown as a plain name — has a `<id>.v2.json` certification spec and curated streams), **Beta**, **Community**, or **Generated**.',
          'Non-certified tiers are suffixed into the label so the signal shows even in a plain dropdown — e.g. `Acme CRM · beta`, `Internal API · generated`. A plain name with no suffix means Certified.',
          'Prefer **Certified** connectors for production pipelines. **Beta / Community / Generated** are fine for exploration but confirm pagination + auth behave as you expect before scheduling them.',
          'After picking a connector, set the **Stream** field to the dataset you want (e.g. `accounts`, `contacts`). Streams come from the manifest.',
        ],
      },
      {
        title: 'Generate a Connector from an OpenAPI / Swagger Spec',
        steps: [
          'Have an OpenAPI 3.x or Swagger 2 spec URL (or JSON)? F-Pulse can scaffold a working connector from it instead of hand-writing a manifest.',
          'Call **POST `/api/connectors/author/from-openapi-runtime`** with `{ "connector_id": "my_api", "openapi_url": "https://…/openapi.json" }` — or pass an already-parsed spec inline as `openapi_spec`. The spec fetch is SSRF-guarded — internal / private-IP URLs are rejected.',
          'You get back a **runtime (v1) manifest** that the SaaS Connector node can use immediately — paths, methods, and pagination are inferred from the spec.',
          'Two sibling endpoints generate the richer **certification (v2)** manifest for review/curation: `/from-openapi` (from a spec) and `/from-samples` (from 1–5 example API responses). A v2 manifest that ships as `<id>.v2.json` auto-promotes the connector to the **Certified** tier.',
          'Generated connectors start at the **Generated** tier in the picker until reviewed — treat them as a fast first draft, then tighten streams + auth before relying on them.',
        ],
      },
    ],
  },
  {
    category: 'Storage — files, managed tables, outputs',
    icon: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5" /><path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" /></svg>,
    guides: [
      {
        title: 'Upload a Data File',
        steps: [
          'Go to **Storage** in the sidebar (stacked-cylinders icon, between Pool and Insights).',
          'Click **+ Upload file** at the top right.',
          'Pick a **Scope** — Global (visible to every project) or Project (scoped to one project + optional folder).',
          'Drag a file into the drop zone OR click to browse. Allowed: CSV, TSV, JSON, NDJSON, Parquet, Excel, XML. Max 100 MB by default.',
          'Optionally add a Description. Click **Upload**.',
          'The file appears in the **Files** tab with its scope chip and "Used by 0 pipelines" until something references it.',
        ],
      },
      {
        title: 'Promote a File to a Managed Table',
        steps: [
          'A managed table is a Parquet-backed table addressable as `schema.name` from Managed Table Source / Sink nodes.',
          'On the **Files** tab, click the **Promote** icon (cylinders icon, green hover) on a row.',
          'Pick a target **Schema** (existing or click **+ New schema** to create one).',
          'Set the **Table name** (auto-suggested from the filename). Optionally rename columns via the `old:new, old2:new2` field.',
          'Click **Promote to table**. The page switches to **Managed Tables** with the new table selected.',
          'Use it from a pipeline: drag a **Source** node and pick `connector_type=local_table` with `schema_name + table_name`, or use the **Managed Table Source** node directly.',
        ],
      },
      {
        title: 'See Which Pipelines Use a File or Table',
        steps: [
          'On the **Storage** page, the **Used by** column shows a blue pill on every row that has references.',
          'Click the pill to open a popover listing each referencing pipeline with name + **Open →** link.',
          'Destructive actions (Delete file / Drop table / Replace bytes) automatically surface the usage list before proceeding so a downstream pipeline doesn\'t break silently.',
          'Detection covers: `local_table_source` / `local_table_sink` references, generic source/destination with `connector_type=local_table`, file-path matches against the storage object\'s path, and promote-to-table provenance.',
        ],
      },
      {
        title: 'Replace the Bytes of an Existing File',
        steps: [
          'On the **Files** tab, click the **Replace** icon (upload-arrow, indigo hover) on the file\'s row.',
          'Pick the new file. Extension must match the original — switching `.csv` to `.parquet` would silently break downstream pipelines, so the API rejects it.',
          'If the file is referenced by pipelines, a confirm dialog lists them first so you don\'t swap data under a live pipeline by accident.',
          'On success the file row keeps the same `object_id`; downstream pipelines pick up the new bytes on their next run.',
        ],
      },
      {
        title: 'Preview File Contents',
        steps: [
          'Click the **Preview** icon (eye) on any file or pipeline output row.',
          'A bottom panel slides up (~440 px tall) — the file list stays visible above so you can click a different file to swap previews without closing.',
          '**Preview** sub-tab — first 100 rows in a tabular view (sticky header, monospace cells).',
          '**Schema** sub-tab — column name, type, and a sample value per column. Disabled for non-tabular JSON.',
          'For non-tabular JSON (configs, OpenAPI specs, accidental pipeline-export uploads), the panel renders a collapsible JSON tree instead of crashing. If the JSON has F-Pulse pipeline shape, an amber **Open in Editor** banner appears with one-click handoff to Workflows → Import.',
        ],
      },
      {
        title: 'Clean Up Old Files',
        steps: [
          'Trashed files older than 30 days can be hard-deleted from the cleanup footer at the bottom of the Storage page.',
          'Click **Clean up files older than 30 days**. A dry-run dialog shows count + bytes that would be freed.',
          'Click **Delete forever** to confirm. The bytes leave disk and the metadata rows are hard-deleted.',
          'Tip: soft-deleted files (Delete row action) appear under the **Show deleted (trash)** toggle on the Files tab. Use **Restore** to move them back to live state.',
        ],
      },
    ],
  },
  {
    category: 'Scheduling & Alerts',
    icon: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>,
    guides: [
      {
        title: 'Schedule a Pipeline to Run Automatically',
        steps: [
          'Open your pipeline in the **Editor** and click the **Schedule** button in the toolbar',
          'Or use the **Schedule** tab in the Save dialog',
          'Choose a schedule type: Interval, Daily, Weekly, or Monthly',
          'For daily/weekly/monthly, you can add multiple run times',
          'Set an active period (start/end date) if the schedule should expire',
          'Enable the schedule and save',
        ],
      },
      {
        title: 'Get Notified When a Pipeline Fails',
        steps: [
          'Open your pipeline in the **Editor** and click the **Alert** button in the toolbar',
          'Or use the **Alerts** tab in the Save dialog',
          'Select conditions: On Failure, On Success, Long Running, or On Overlap',
          'Choose a channel: Email, Slack, Teams, or Webhook',
          'Enter the notification target (email address, webhook URL, etc.)',
          'You can also set alerts directly from the Workflows page using the bell icon',
        ],
      },
      {
        title: 'Compute-Usage Alerts on the Pool (PROD only)',
        plus_only: true,
        steps: [
          'In PROD mode, go to **Pool → Alerts** tab',
          'Click **New Alert Rule** to define a threshold on a compute metric',
          'Metrics available: `utilization_pct`, `queue_depth`, `throughput_per_hour`, `error_rate_pct`, `busy_workers`',
          'Pick an operator (>, >=, <, <=, ==) and threshold value',
          'Set a window in minutes — condition must hold continuously for this window before firing',
          'Choose channels (email / slack / webhook) and save',
          'Rules only fire in PROD so developers never get trained to ignore them',
        ],
      },
      {
        title: 'Set Execution Timeouts & Overlap Protection',
        steps: [
          'Open Save dialog and go to the **Execution** tab',
          'Enable **Maximum Running Time** and choose a limit (e.g. 30 min)',
          'Enable **Overlap Detection** and choose a policy:',
          '- **Skip**: Don\'t start a new run if previous is still running',
          '- **Queue**: Wait for the current run to finish, then start',
          '- **Cancel Previous**: Stop the running execution and start fresh',
        ],
      },
    ],
  },
  {
    category: 'Triggering Pipelines from the API',
    icon: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" /></svg>,
    guides: [
      {
        title: 'Run a pipeline from the REST API',
        steps: [
          'Every saved pipeline can be triggered without opening the UI by calling **POST /api/execute/workflow/{id}**.',
          'The endpoint accepts an optional JSON body with `parameter_values` to override declared pipeline parameters per run.',
          '**curl example** (no parameters):',
          '```\ncurl -X POST http://localhost:8001/api/execute/workflow/abc123def456 \\\n  -H "X-Workspace-Id: default" \\\n  -H "Authorization: Bearer YOUR_TOKEN"\n```',
          '**curl example** (with parameters):',
          '```\ncurl -X POST "http://localhost:8001/api/execute/workflow/abc123def456?safety_mode=live" \\\n  -H "Content-Type: application/json" \\\n  -H "X-Workspace-Id: default" \\\n  -H "Authorization: Bearer YOUR_TOKEN" \\\n  -d \'{ "parameter_values": { "dataset": "orders_2026_04", "batch_size": 5000, "run_date": "2026-04-30" } }\'\n```',
          'Query-string options: `?full_run=true` (skip dev sample cap), `?environment=prod` (target env), `?safety_mode=sample|dry_run|validate_only|live` (Copilot pre-run banner equivalent).',
          'Response includes: `step_results`, `status`, `error_intelligence`, and `metadata.peak_memory_mb` / `metadata.cpu_seconds` from the resource sampler.',
          'Unknown parameters or required-but-missing parameters return **400 Bad Request** with the offending name. Type coercion failures (e.g. `"abc"` for an `int` parameter) also raise 400.',
        ],
      },
      {
        title: 'Define pipeline parameters',
        steps: [
          'Open your pipeline in the **Editor**.',
          'Click the green **Parameters** button in the Editor toolbar.',
          'Click **+ Add parameter** and fill in: `name` (alphanumeric + underscore), `type` (string / int / float / bool / json), optional `default`, `description`, and toggle `required` if the caller MUST provide a value.',
          'Reference a parameter inside any step\'s params with `${param.<name>}` — e.g. set the CSV Source\'s **path** field to `/data/${param.dataset}.csv`.',
          'System placeholders also work: `${utcnow:%Y-%m-%d}` (current date), `${utcnow}` (ISO timestamp), `${run_id}` (per-run UUID).',
          'Click **Apply** to commit the parameters to the pipeline draft, then **Save** to persist.',
        ],
      },
      {
        title: 'Run a pipeline with parameter overrides (UI)',
        steps: [
          'On the **Pipelines** page, click any pipeline that has declared parameters.',
          'Click the blue **Run** button in the detail panel.',
          'A "Run with parameters" modal opens — every declared parameter is pre-filled with its declared default.',
          'Edit any field; leave a field empty to use its default. Required parameters that are empty show a red border and block the Run button.',
          'Click **Run** — the pipeline executes with your overrides. The Executions detail page shows the resolved values as green chips above the tab bar so anyone reviewing the run sees exactly what was passed.',
          'Pipelines with no declared parameters skip the modal entirely — Run still triggers immediately.',
        ],
      },
      {
        title: 'Choose an AI provider (Anthropic / OpenAI / OpenRouter / Ollama)',
        steps: [
          'Open **Insights → AI Provider**. Pick from: **Anthropic Claude** (recommended), **OpenAI** (recommended), **OpenRouter** (single key, 100+ models), Google Gemini, DeepSeek, Groq, Mistral, Azure OpenAI, **Ollama** (local; advanced — slow on CPU), or **Custom** (any OpenAI-compatible endpoint).',
          '**Anthropic / OpenAI** — paste your `sk-ant-…` or `sk-…` key; pick a model (Claude Haiku / GPT-4o mini are the cheapest fast options at ~$0.0006-0.0045 per agent turn).',
          '**Two ways to supply the key**: the API-key field has an *Enter key inline* / *Use a saved credential* toggle. Inline stores the key encrypted in the AI config. *Use a saved credential* points at an entry in **Insights → Credentials** (category **AI Provider**) — the key is resolved from there at request time and never copied here, so it gets the same expiry / audit / vault governance as your other secrets. Either way the page never shows a saved key, only whether one is set.',
          '**OpenRouter** — paste your `sk-or-…` key; the model id is namespaced like `openai/gpt-4o-mini`, `anthropic/claude-sonnet-4`, `meta-llama/llama-3.1-70b-instruct`. One key gets you 100+ models with ~5% markup. Useful when you want flexibility without managing per-provider accounts.',
          '**Ollama** — only practical if you have a GPU. CPU-only laptops take 30-120 s per turn for the 8 B tool-capable models needed by the agent. Set `FPULSE_DISABLE_OLLAMA_AUTOPROBE=1` if you don\'t want F-Pulse silently re-attaching to a running Ollama background service.',
          'The **Provider price comparison** card at the bottom of the AI Provider tab updates from OpenRouter\'s public pricing feed (1 h cache, hardcoded fallback when offline) and badges the cheapest low-latency option as Recommended.',
          'Switching providers is instant — no restart needed. The agent header in the Copilot dock updates on the next turn.',
        ],
      },
      {
        title: 'Costs, providers & rate limits',
        steps: [
          'F-Pulse OSS is free forever (Apache 2.0). All money goes to the LLM provider you pick — never to Hybridyn.',
          '**Cheapest path**: OpenRouter free models (`deepseek/deepseek-chat-v3:free`, `meta-llama/llama-3.3-70b-instruct:free`, `qwen/qwen-2.5-72b-instruct:free`) — **$0/turn**. Limited to ~50 req/day per account; ~1000/day after a one-time $10 credit on openrouter.ai.',
          '**Paid direct**: OpenAI gpt-4o-mini ~$0.0006/turn, Anthropic Claude Haiku ~$0.0045, Sonnet ~$0.01. Best latency + reliability.',
          'Not every TOOLS-badged model closes tool loops well. DeepSeek V3, Llama 3.3 70B, Qwen 2.5 72B do. Smaller variants often stop after announcing a tool call. If you see `[No response.]`, switch to a stronger model.',
          'Wallet caps stop runaway loops: 1M tokens/user/day, 10M/workspace/day. Override with `FPULSE_AGENT_DAILY_TOKENS_USER` / `_WORKSPACE` env vars. Per-call cost is shown under every agent reply (e.g. `~5,000 tokens · ~$0.001`).',
          'Hit HTTP 429? Switch to a free model from a different upstream provider — each upstream has its own quota bucket.',
        ],
      },
      {
        title: 'Make the Copilot answer from your own pipeline history (RAG)',
        steps: [
          'F-Pulse ships with a **retrieval layer** that augments Copilot answers with workspace-scoped context: failed executions from the last 30 days, your pipeline definitions, the connector catalog, and product docs.',
          'It runs by default. The agent calls the `recall_history` tool whenever you ask open-ended questions like *"what failed last week"*, *"what does pipeline X do"*, or *"which connector should I use for Snowflake"*.',
          'Embeddings run **locally** via Ollama using `nomic-embed-text` (a small 768-dim model). Pull it once: `ollama pull nomic-embed-text`. Cloud-LLM users still benefit — only the embedding step is local; retrieved chunks get sent in the prompt to whichever provider is configured.',
          'The corpus is rebuilt **daily at 03:00 UTC** in the background. To force a refresh, restart F-Pulse or hit the reindex endpoint.',
          'Override the embedding model via `FPULSE_EMBEDDING_MODEL=<name>`. Disable retrieval entirely with `FPULSE_DISABLE_RAG=1` if you want the Copilot to answer purely from live tool calls.',
          'All retrieval is workspace-scoped — chunks from one workspace never leak into another.',
        ],
      },
      {
        title: 'Run F-Pulse fully local with the bundled Modelfile',
        steps: [
          'Privacy-sensitive teams can run F-Pulse end-to-end on their own hardware. The repo ships with a Modelfile at `deploy/ollama/fpulse-assistant.Modelfile`.',
          'Install Ollama (https://ollama.com) and pull a base model: `ollama pull llama3.1`.',
          'Build the F-Pulse-tuned variant: `ollama create fpulse-assistant -f deploy/ollama/fpulse-assistant.Modelfile`.',
          'In **Insights → AI Provider**, pick **Ollama** and set the model to `fpulse-assistant`. F-Pulse will detect the locally-running Ollama service automatically.',
          'Hardware target: GPU or modern Apple Silicon. CPU-only laptops will work but expect 30-90 s per turn — see the AI provider guide above for trade-offs.',
          'Optional: set `FPULSE_ENABLE_POLICY_ROUTE=1` to route code/tool-result/sensitive prompts to local Ollama even when a cloud provider is configured (cloud is still used for non-sensitive turns).',
        ],
      },
      {
        title: 'Inspect compute usage in a run',
        steps: [
          'Open the **Executions** page → click a finished run.',
          'The detail panel header shows three tiles next to **Duration**: **Peak memory** (MB sampled at 1 Hz), **CPU** (cumulative CPU-seconds), and the **parameter_values** chips if the run had overrides.',
          'These numbers are sampled by `psutil` and stored in `execution_logs.metadata`. Older runs predate the sampler and hide the tiles.',
          'They also flow into the Dashboard\'s anomaly-detect surface so runs that diverge from the 7-day baseline get flagged.',
        ],
      },
    ],
  },
  {
    category: 'Approval & Deploy Flow',
    plus_only: true,
    icon: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /></svg>,
    guides: [
      {
        title: 'Stop a Running Pipeline',
        steps: [
          'In the **Editor**, the **Run** button flips to a red **Stop** while the pipeline is executing — click it to send a cancel signal',
          'On the **Workflows** page, the run icon on any row flips to a red square when that pipeline is running — click to stop',
          'Already-running steps finish their current operation cleanly, then halt — no half-written rows or partial updates',
          'You can stop your own runs anytime; admins can stop anyone\'s. The cancel call hits POST /api/ws/cancel/{workflow_id} under the hood',
          'A toast confirms the stop signal was sent. Status flips back to its idle state once the executor acknowledges',
        ],
      },
      {
        title: 'Two-Gate Approval Flow (DEV → Sandbox → PROD)',
        plus_only: true,
        steps: [
          '**Dev** builds the pipeline in DEV with DEV connections, runs it, then clicks **Submit for Deploy**',
          'An email + in-app notification goes to all approvers; the request appears on the **Approvals** page',
          '**Gate 1** — an approver clicks **Approve** to release the pipeline into the sandbox stage',
          'The Prod admin then opens the approval and clicks **Run in Sandbox** — the pipeline runs against real PROD connections, but writes only to a scratch namespace (no real PROD tables touched)',
          'Output (rows, schema, errors) appears inline so the admin can verify behavior before going live',
          '**Gate 2** — admin clicks **Submit for Deploy Approval**; the original approver (or a different one if your workspace requires two-person) reviews the sandbox evidence and approves',
          'On approve, the pipeline is deployed and activated in PROD',
        ],
      },
      {
        title: 'Test in Sandbox Before Approving',
        plus_only: true,
        steps: [
          'Open the **Approvals** page → click **Run in Sandbox** on any pending PROD approval (admin-only)',
          'Optionally adjust the row limit (default 10,000; max 100,000 — keeps sandbox runs cheap)',
          'The run uses real PROD source connections, real PROD credentials, and real PROD source data',
          'Destinations are automatically rewritten — DB writes go to a `sandbox_<id>` schema, S3 writes get a `sandbox/<id>/` prefix, Kafka topics get a `.sandbox.<id>` suffix',
          'Notification sinks (email, Slack) are dropped to no-op — no real messages sent',
          'Output panels show row count, schema, sample rows, and any errors',
          'Re-run as many times as needed; sandbox auto-cleans 24h later or as soon as you approve/reject',
        ],
      },
      {
        title: 'Activate or Deactivate a Pipeline',
        steps: [
          'On the **Workflows** page, every row has an **Activate** / **Deactivate** button',
          'Click flips the active flag immediately — no approval needed',
          'When deactivated, scheduled runs are skipped, webhooks return 423 Locked, and manual run attempts are blocked',
          'The deployment artifact stays in place — re-activation is instant, no redeploy needed',
        ],
      },
      {
        title: 'Decide Activate / Deactivate Requests (admin)',
        plus_only: true,
        steps: [
          'Pending lifecycle requests appear at the top of the **Approvals** page in a violet card',
          'Each row shows: action (activate / deactivate), workflow id, requester, target environment, and reason',
          'Click **Approve** to flip the flag immediately, or **Reject** with a reason that goes back to the requester',
          'All decisions are written to the audit log',
        ],
      },
      {
        title: 'Allocate Worker Capacity Between DEV and PROD',
        plus_only: true,
        steps: [
          'Go to **Pool** → Overview tab. The **Worker Allocation** card at the top shows the current split',
          'Default is **60% PROD reserved / 20% DEV reserved / 20% shared burst** — sums always equal 100%',
          'Admins drag the PROD slider; DEV proportionally adjusts; burst absorbs the remainder',
          'Click **Save allocation** to apply — change takes effect on the next admitted task; running tasks are unaffected',
          'PROD tasks always have their reserved share available — DEV traffic can never starve scheduled PROD runs',
          'Burst is grabbed first-come-first-served when either reserved lane is full',
          'Every change is audit-logged with the admin\'s identity and timestamp',
        ],
      },
      {
        title: 'Reuse Pipelines with Execute Pipeline + Parameters',
        steps: [
          'Drag the **Execute Pipeline** node from the Control Flow palette',
          'Pick a sub-pipeline from the dropdown (your current workflow is excluded — calling yourself = infinite loop)',
          'Add **Parameters** as key/value pairs — these become workflow variables in the sub-pipeline',
          'Values can be strings, numbers, booleans, or JSON arrays — they round-trip via JSON parsing',
          'Choose **On Failure** behavior: fail (stops parent), skip, or continue',
          'Toggle **Wait on Completion** off if the parent should continue without waiting',
        ],
      },
    ],
  },
  {
    category: 'Tips & Best Practices',
    icon: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="9" y1="18" x2="15" y2="18" /><line x1="10" y1="22" x2="14" y2="22" /><path d="M15.09 14c.18-.98.65-1.74 1.41-2.5A4.65 4.65 0 0 0 18 8 6 6 0 0 0 6 8c0 1 .23 2.23 1.5 3.5A4.61 4.61 0 0 1 8.91 14" /></svg>,
    guides: [
      {
        title: 'Download a Scoped Inventory Report',
        // 2026-05-25 — moved here from "Approval & Deploy Flow" (Plus-only
        // category) so the OSS-visible Insights → Reports feature has a
        // matching how-to. The System scope row is admin-only at runtime
        // and the report contents already adapt to caller permissions, so
        // this guide is safe in the OSS lane.
        steps: [
          'Open **Insights → Reports** from the navigation bar.',
          'Pick a scope: **System** (admin-only — installation-wide inventory across every project), **Project** (one project + its pipelines), **Pipeline** (one pipeline\'s config + runs), or **User** (your view)',
          'When Project, Pipeline, or User is picked, choose the target from the dropdown',
          '**DEV — Build phase**: drafts in development, your dev test runs, the connections + credentials you use, your personal productivity stats',
          'Pick format (PDF or DOCX) and click **Download**',
          'A copy of the generated report is **also saved to Storage → Files** with a `report` tag, so you can re-download or share it later without regenerating.',
        ],
      },
      {
        title: 'Organize Pipelines with Projects & Tags',
        steps: [
          'Create **Projects** to group related pipelines together',
          'Add **Tags** to pipelines for filtering (e.g. "etl", "daily", "finance")',
          'Use the tag filter on the Workflows page to quickly find pipelines',
          'Each project can have its own connections and variables',
        ],
      },
      {
        title: 'Version Control Your Pipelines',
        steps: [
          'Every time you save, a new version is created automatically',
          'Click the clock icon on the Workflows page to view version history',
          'Compare changes between versions (added/removed steps)',
          'Restore any previous version with one click',
        ],
      },
      {
        title: 'Deploy to PROD with Rollback',
        plus_only: true,
        steps: [
          'Click **Deploy** in the Editor (or the Deploy button on the Workflows page) to open the Pre-Deploy Validation dialog',
          'The dialog runs 8+ automated checks — structure, approval, test runs, connections mapped, schedule set, alerts configured',
          'Expand the **Pipeline structure valid** row and click **View DAG Lineage** to see the exact node/edge graph that will deploy',
          'Expand the **New version to deploy** row to pick any prior version — pre-deploy checks re-run against that snapshot',
          'Selecting an older version rolls back PROD to that exact state without reverting the Editor source of truth',
          'Failing checks block deploy; warnings let you proceed with a confirmation',
        ],
      },
      {
        title: 'Debug a Failed Pipeline',
        steps: [
          'Go to **Executions** to find the failed run',
          'Click the execution to see the step-by-step log',
          'Failed steps show the error message and suggested fixes',
          'Use the **Lineage** tab to see where data flow broke',
          'Fix the issue, then re-run from the Editor or Workflows page',
        ],
      },
      {
        title: 'Use Safety Mode While Exploring',
        steps: [
          'Open **Settings → General → AI Assistant** and turn on **Safety mode**.',
          'The Copilot now blocks every write tool (create pipeline, modify step, draft alert) even if your role permits writes.',
          'Read-only tools and chat still work — ask questions, browse, get explanations, request drafts.',
          'When safety mode is on, every Copilot request sends an `X-FPulse-AI-Safety: 1` header so the backend enforces the block regardless of UI state.',
          'For a stricter, operator-level lockdown that disables LLMs entirely (only deterministic fast-lane tools answer), set `FPULSE_TOOL_ONLY_MODE=1` on the server and restart. The Settings page shows a banner when this is active.',
        ],
      },
    ],
  },
  // 2026-06-05 — F-Pulse Steward (Archeologist sub-agent, 1.1) — the OSS
  // headline differentiator. The how-to-use guidance lives in this
  // category so it sits next to "Best Practices" rather than buried in
  // a settings page that users won't browse until something feels off.
  {
    category: 'F-Pulse Steward',
    icon: <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" /><circle cx="12" cy="12" r="3" /></svg>,
    guides: [
      {
        title: 'What Steward does for you',
        steps: [
          '**Steward is a second pair of eyes on your whole workspace.** It spots when you\'ve accidentally built the same thing twice, catches a source schema changing under you before it breaks downstream, watches your connection health for sustained failures, remembers the fixes that worked — and never touches your pipelines on its own.',
          '**Catches duplicates you didn\'t know existed.** If you (or a teammate) accidentally build two pipelines that both read `orders.csv` into different warehouses, the eye-icon flashes the first time both pipelines exist. You decide: merge them, or click Dismiss with a reason ("intentional, different SLAs"). Either way you find out instantly, not in six months.',
          '**Tells you when a column dropped.** Every successful pipeline run can post its source schema to Steward. The moment the schema differs from the previous one, you get a finding with the exact column + old/new type. P1 for drops and type-changes, P3 for additions.',
          '**Watches your connections quietly.** Every time you click Test on a connection, Steward records the result. Sustained-failure streaks (≥2 consecutive failures over ≥5 minutes) emit a finding classified as auth-failure, unreachable, rate-limit, or timeout. Severity scales with streak length.',
          '**Captures the fix when something breaks.** When a pipeline fails and you fix it, click Resolve and type what worked. That note becomes a PROPOSED lesson the team lead can approve. Three months from now, when the same break happens, the next operator searches Memory and the fix takes 5 minutes instead of 90.',
          '**Doesn\'t cry wolf.** One notification per (user, finding, severity) — you never get 14 emails about the same issue. A 30-second burst never auto-escalates to P1. If you dismissed something yesterday and it returns today, the escalation counter resets from zero.',
          '**Never edits your pipelines.** Read-only by architectural rule. The closest Steward comes to "doing" is showing a button that opens the standard pipeline editor at the right starting point. You\'re always the one who makes the change.',
        ],
      },
      {
        title: 'Where to find it in the UI',
        steps: [
          'Look for the **violet eye icon** in the top header, immediately to the LEFT of the notification bell. A violet (or red, if P1) badge shows the count of open findings.',
          'Click the eye → the dropdown panel opens with five tabs: **Findings** (the live alert list), **Coverage** (every detector + per-detector enable / severity / threshold controls), **Rules** (your own custom rules), **Memory** (the approved-lesson library + journal), and **Settings** (sensitivity / escalation / notification toggles).',
          'Steward findings also flow into the standard notification bell — so even when you\'re not staring at the eye, escalations reach you via email / Slack / Teams (if those channels are configured).',
          'If you turn Steward off (Settings → Enable Steward), the eye icon dims to a muted eye-off glyph — a single click flips it back on. No JSON edit needed to recover.',
        ],
      },
      {
        title: 'What gets detected today (and what doesn\'t — be honest)',
        steps: [
          '**Active today:** duplicate sources + duplicate pipelines (Archeologist); schema drift; automatic volume-anomaly (baseline-variance) plus threshold quality checks (null-rate / freshness / row-count / partition); node-level empty output; **node cardinality — join explosion, join collapse, dedupe over-removal, filter-dropped-all** (run-fed, with tunable ratio/floor on the Coverage tab); **row-count integrity** (1:1 steps that silently drop/duplicate rows); **unused managed table** (state); warehouse-waste (cost); governance (env-crossing / unapproved-destination / PII-leak); connector health (auth-failure / unreachable / rate-limit / credential-near-expiry); user-defined rules for any pattern; resolve-with-fix-note creates PROPOSED lessons.',
          '**Every detector is tunable.** On the **Coverage** tab you can turn any detector off, override its severity, and (for the cardinality detectors) edit its thresholds — so Coverage becomes *your* policy, not a fixed list. Disabled detectors never escalate or notify.',
          '**How the run-fed detectors get their data — be precise:** schema-drift, cost (warehouse-waste), PII, node-cardinality and row-count signals are captured automatically from **full (non-sandbox) pipeline runs** — every real run records per-node row counts, cost events, a per-source schema snapshot, and PII findings to Steward. They do **not** populate from dev sample previews or sandbox runs, so a brand-new workspace shows nothing under these until pipelines have actually run for real. Duplicate / governance / connector-health / unused-table detection works immediately from your saved definitions and Test clicks.',
          '**Contract-only today** (enum + storage + UI handle them, detector deferred): pipeline-level SLA-breach / partial-output / retry-storm; node-level cast-failure; credential-sprawl (governance); cost-drift / cost-recommendation (cost); failure RCA with Memory Layer auto-recall (ships with Incident Analyst in 1.2).',
          'We say the contract is "ready" but be clear: adding the detector for a contract-only kind is real engineering work — schema-drift took 2 weeks, connector-health took a week. The contract just means future detectors slot in without reshaping storage, UI, notification de-dup, or Memory Layer.',
          'Want a check that isn\'t built in? Add your own from the **Rules** tab (a small form — pipeline name contains, or has a node of a given type), or drop a YAML file into `<data_dir>/steward/<workspace>/rules/`. Either way the rules engine emits findings at any level with the same alert-fatigue guarantees as built-in detectors. SQL / expression rules are a Plus feature. See the "Make Coverage your policy" guide below.',
        ],
      },
      {
        title: 'How the Steward learns from your actions',
        steps: [
          'Every emit, dismiss, and resolve is appended to a per-workspace JSONL journal at `<data_dir>/steward/<workspace>/memory.jsonl`. Open the **Memory** tab in the Steward dropdown to see the live audit trail.',
          '**Persistent occurrence counter**: the Steward tracks the number of *distinct scans* a signature has appeared in (not the per-scan workflow count). A finding seen in 5 separate scans without resolution is treated very differently from one seen in 1 scan with 5 workflows.',
          '**Severity escalation**: when persistent occurrences cross the **Escalate after N occurrences** threshold (default 5, configurable in Settings), the next scan bumps severity one step (P3 → P2 → P1). What you keep ignoring gets louder, not quieter.',
          '**Rebound detection**: if you marked a finding resolved and the same signature later re-appears (someone re-introduced the duplicate), the new finding is annotated with `(rebounded)` and the body shows when it was previously resolved.',
          '**Dismiss-with-reason**: when you dismiss a finding, the Steward asks for an optional reason (e.g. "DR replication — intentional"). The reason is recorded in memory so the future **Curator** sub-agent (1.4) can mine the patterns of why findings get dismissed and refine the detectors.',
        ],
      },
      {
        title: 'How to use the Findings tab',
        steps: [
          'Open the Steward dropdown by clicking the eye icon in the header.',
          'For each finding you see: title, severity badge (P1/P2/P3), kind (Duplicate source, Duplicate pipeline, …), the workflows involved, and how many scans it has appeared in.',
          'Click **Mark resolved** when you have taken action that fixes the underlying pattern (deleted the duplicate, consolidated into a managed table, etc.). The finding closes; if the pattern recurs later, you\'ll see `(rebounded)` on the new finding.',
          'Click **Dismiss (intentional)** when the duplicate is deliberate — DR replication, data-vault layering, two pipelines that look the same but have different SLAs. You\'ll be prompted for an optional reason. The signature is then suppressed and won\'t re-appear in future scans.',
          'Click **Re-scan** in the header to force a fresh detection pass — useful right after you delete or restructure a pipeline.',
        ],
      },
      {
        title: 'How to configure the Steward',
        steps: [
          'Open the Steward dropdown and click the **Settings** tab.',
          '**Enable Steward**: master kill-switch. Turn off if findings are noisy for your workspace size. Settings remain stored — flipping back on resumes where it left off.',
          '**Scan on save**: when on (default), every workflow save triggers an immediate re-scan so a duplicate created right now shows up without waiting for the 60-second poll. Sub-50ms on typical OSS workspaces; no executor impact.',
          '**Minimum severity**: hide findings below this threshold in the dropdown. P3 shows everything; P2 hides informational; P1 only surfaces production-blockers.',
          '**Escalate after N occurrences**: how many separate scans a finding must appear in before its severity bumps one step. Default 5; lower to be more aggressive, higher to be more patient.',
          '**Escalate min hours since first**: time-clamp on escalation. A finding only escalates after BOTH the count threshold AND this minimum age (default 24h) are met. Without this, a 60-second cron pipeline would page-out to P1 in 5 minutes. Set to 0 to disable the time clamp and rely on count alone.',
          '**Auto-stale days**: findings open this long without dismiss/resolve auto-age into the `stale` status and hide from the default view. They stay in memory for the Curator to learn from.',
          'Settings are persisted at `<data_dir>/steward/<workspace>/settings.json`. Safe to hand-edit. The Settings tab covers the global toggles; per-detector tuning lives on the **Coverage** tab (next guide).',
        ],
      },
      {
        title: 'Make Coverage your policy (enable / disable, severity, thresholds)',
        steps: [
          'Open the Steward dropdown and click the **Coverage** tab. It lists every detector that actually runs, grouped by level, with its live open-finding count — and now, per-detector controls so Coverage reflects *your* workspace.',
          '**On / Off** — turn any detector off if it doesn\'t fit your workspace. Its findings drop from scans and never escalate or ping the bell; history is kept, so flipping it back On resurfaces still-open findings.',
          '**Severity** — override a detector\'s severity (Default / P1 / P2 / P3) to match your priorities; an override shows a `→ P1` badge.',
          '**Thresholds** — the cardinality detectors expose editable numbers. Example: if your joins legitimately fan out, set Join explosion\'s **ratio** to 100 so it only warns above 100× (your "don\'t warn under 100×" case). Click **reset** to return any value to its default. Threshold changes apply to the next run.',
          'All of this saves to the same per-workspace settings file, so it survives restarts and is safe to GitOps.',
        ],
      },
      {
        title: 'Add your own rules (Custom)',
        steps: [
          'Open the Steward dropdown → **Rules** tab → **+ New rule**. Give it an id + title, pick a severity and level, then a match condition: *pipeline name contains <text>* or *has a node of type <type>* (e.g. `db_sink`). Add recommended actions (one per line) and click **Create rule**.',
          'Matches surface as normal findings (kind = Custom) with the same dismiss / resolve / notification behavior as built-in detectors.',
          'Rules are stored as **YAML files** under `<data_dir>/steward/<workspace>/rules/` — exactly what the form writes — so you can version-control them or hand-edit for richer matches (lacks-node, params_eq, node-count, …). Bad files surface as a load error in the Rules tab rather than silently failing.',
          'Delete a rule from the same tab. SQL / expression rules over a read-only metadata view are a **Plus** feature; the in-app form is declarative-only.',
        ],
      },
      {
        title: 'Notification bell integration',
        steps: [
          'New and newly-escalated findings also write a row to the **notification bell** (the bell icon next to the Steward eye). This means you see Steward findings even when you\'re not actively looking at the eye icon.',
          'Steward notifications show with a violet eye icon (`Steward`) or a red triangle (`Steward — Escalated` for P1 findings escalated from the learning layer).',
          '**De-dup is enforced**: at most one notification per (you, finding, severity, rebound-state) tuple. Re-scans of unchanged findings never spam the bell. New severity (P2 → P1 escalation) or a `(rebounded)` status counts as a *new* event and gets its own ping.',
          '**Clicking a Steward notification** deep-links you to the dashboard and auto-opens the Steward dropdown on the Findings tab so you land in context immediately.',
          '**Dismiss or Resolve a finding** → all related notifications for that finding ID are marked as read automatically. No stale unread badge for issues you\'ve already triaged.',
          'Two toggles in the Settings tab control this: **Notify on new findings** (master on/off, default ON) and **Minimum severity to notify** (default P2 — info-only P3 findings stay in the eye-icon badge without spamming the bell).',
          'If you also have email or Slack channels configured, Steward notifications flow through those channels via the existing notification pipeline — no extra configuration needed.',
        ],
      },
      {
        title: 'How to see the proof — Memory tab',
        steps: [
          'Open the Steward dropdown and click the **Memory** tab.',
          'The top card shows **Persistent occurrence counts** — for each signature the Steward has seen, the number of distinct scans it has appeared in. Counts at or above your escalation threshold show in red.',
          'Below that is the **event stream** — every emit, dismiss, and resolve, newest first, with the signature and (for dismisses) the reason you supplied.',
          'This view is the audit trail that proves the Steward is genuinely learning from your interactions — not just re-running the same detection blindly.',
        ],
      },
      {
        title: 'Safety guarantees you can rely on',
        steps: [
          '**Read-only.** Steward never modifies pipelines, connections, credentials, or schedules. Every action surfaced (consolidate, dismiss, resolve) is yours to take — the Steward only writes to its own suppression / memory / lessons files.',
          '**No alert-fatigue spiral.** If you dismiss a finding with reason, its prior occurrence history is RESET. A previously-dismissed pattern can never inherit its old N-scan count and immediately escalate to P1 on re-emit — it starts fresh from 1.',
          '**Time-clamped escalation.** A 60-second cron pipeline can hit 5 scans in 5 minutes without paging out. Escalation requires BOTH the count AND a minimum age (default 24h, configurable).',
          '**Dismiss-reason sanitization.** Operator notes are scrubbed for accidentally-pasted secrets BEFORE they hit the on-disk journal: AWS access keys, Bearer tokens, `password=…` / `secret=…` key-value pairs, `user:password@host` URI credentials, and private-IP ranges all become `[REDACTED:<kind>]`. Normal prose passes through verbatim.',
          '**Gated learning.** A PROPOSED Memory Layer lesson does NOT influence future Steward reasoning until a human approves it. Rejection is recorded with reason in the lesson\'s evidence trail for audit.',
          '**Per-workspace isolation.** A dismiss in workspace A never silences the same source pattern in workspace B. Signatures include the workspace ID so two tenants never cross-pollinate.',
          '**Corrupt-journal resilience.** A bad line in `memory.jsonl` is skipped silently — every aggregator (`stats / persistent_occurrences / audit_trail`) returns useful data from the remaining valid events. Steward must never crash the scan path on its own state.',
        ],
      },
      {
        title: 'Future sub-agents (roadmap)',
        steps: [
          '**Autopsy (1.2)**: failure root-cause analysis with memory of past incidents. When a pipeline fails, Autopsy checks whether it has failed the same way before and proposes the fix that worked last time.',
          '**Foreseer (1.3)**: volume + schema-drift anomaly detection. Flags when a daily pipeline that normally moves ~10k rows suddenly moves 200, or when a source\'s column shape changes silently.',
          '**Curator (1.4)**: distills recurring guidance from your memory journal into an `EPULSE_RUNBOOK.md` — your personal "things I keep getting reminded of" reference.',
          '**Optimizer (2.0)**: cost + performance recommendations from cross-execution analysis.',
          'Each new sub-agent obeys the same hard rules: read-only, out-of-band, deterministic core + LLM-narration shell, explicit provenance, OSS-first. See `docs/steward/overview.md` for the full architecture.',
        ],
      },
    ],
  },
];

/* ── Keyboard Shortcuts ──
 * Documented shortcuts MUST have a real keydown handler somewhere in the app.
 * Anything not bound is removed from this list so the cheat-sheet stays honest.
 * Per docs/PAGE_BY_PAGE_AUDIT.md (P0 #9, 2026-05-19) — F2 (rename), Space
 * (open settings), D (deactivate), Ctrl+D (duplicate) were all listed without
 * handlers and have been stripped until real bindings ship.
 */
// 2026-05-25 — collapsed duplicate rows. The previous structure listed
// each alternate key combo as its own row, which made `Redo` and `Delete
// selected node` appear twice in the cheat-sheet (user-reported defect).
// The new shape supports an optional `altKeys` so equivalent combos
// share one row: `Redo  ⌘⇧Z  or  ⌘Y`. Also stripped the internal audit
// reference `(P2 #20)` that leaked into the description.
const SHORTCUTS: Array<{
  category: string;
  items: Array<{ keys: string[]; altKeys?: string[]; desc: string }>;
}> = [
  { category: 'General', items: [
    { keys: ['Ctrl', 'K'], desc: 'Open global search palette' },
    { keys: ['?'], altKeys: ['Ctrl', '/'], desc: 'Open this Shortcuts cheat-sheet' },
    { keys: ['Ctrl', 'S'], desc: 'Save pipeline' },
    { keys: ['Ctrl', 'Z'], desc: 'Undo' },
    { keys: ['Ctrl', 'Shift', 'Z'], altKeys: ['Ctrl', 'Y'], desc: 'Redo' },
    { keys: ['Ctrl', 'W'], desc: 'Close editor (with unsaved-changes guard)' },
    { keys: ['Esc'], desc: 'Close panel / Deselect node' },
  ]},
  { category: 'Canvas', items: [
    { keys: ['Ctrl', 'A'], desc: 'Select all nodes' },
    { keys: ['Ctrl', 'C'], desc: 'Copy selected nodes' },
    { keys: ['Ctrl', 'V'], desc: 'Paste nodes from clipboard' },
    { keys: ['Delete'], altKeys: ['Backspace'], desc: 'Delete selected node' },
  ]},
  // 2026-06-03 — rebind reflected. Was: Click → open config, Click name
  // → rename, Right-click → menu. Now (select-vs-open separation):
  //   - Single-click selects + passively opens the config panel.
  //   - Double-click opens the node "actively" — scrolls panel to top
  //     and focuses the first editable field (handled by ConfigPanel's
  //     `fpulse-node-opened` event listener).
  //   - F2 is the rename shortcut (was listed in context menu since
  //     launch but never actually wired — now bound in FPulseNode).
  //   - Right-click still opens the full context menu.
  { category: 'Node', items: [
    { keys: ['Click'], desc: 'Select node (highlight only — no modal)' },
    { keys: ['Double-click'], desc: 'Open node for editing (focuses first field)' },
    { keys: ['F2'], desc: 'Rename selected node' },
    { keys: ['Right-click'], desc: 'Context menu (Execute, Open Settings, Rename, Duplicate, Delete)' },
  ]},
  { category: 'Edge', items: [
    { keys: ['Click label'], desc: 'Cycle condition: Completion → Success → Failure' },
    { keys: ['Click ×'], desc: 'Delete edge connection' },
  ]},
];

/* ── Node Catalog ── */
// `icon` values come from the shared Icon set in `shared/Icon.tsx`. The
// rendering site below pipes them through the Icon component so the
// category headers render consistently across OS / browser fonts.
const NODE_CATALOG: Array<{ category: string; icon: import('../shared/Icon').IconName; color: string; nodes: Array<{ name: string; desc: string }> }> = [
  { category: 'Sources', icon: 'download', color: 'bg-blue-50 border-blue-200', nodes: [
    { name: 'CSV', desc: 'Load data from CSV files with configurable delimiter, encoding, and header options' },
    { name: 'JSON', desc: 'Parse JSON or JSONL files into tabular data' },
    { name: 'Parquet', desc: 'Read Apache Parquet columnar files' },
    { name: 'Excel', desc: 'Import Excel spreadsheets (.xlsx) with sheet selection' },
    { name: 'XML', desc: 'Parse XML documents with XPath row selection' },
    { name: 'File (auto)', desc: 'Auto-detect CSV / JSON / Parquet / Excel / XML / NDJSON / TSV by extension' },
    { name: 'Database', desc: 'Query SQL databases (PostgreSQL, MySQL, SQLite)' },
    { name: 'JDBC', desc: 'Generic JDBC / warehouse source via dialect registry (Snowflake, BigQuery, Redshift, etc.)' },
    { name: 'CDC', desc: 'Debezium-style change data capture stream from a source database' },
    { name: 'REST API', desc: 'Fetch data from HTTP endpoints with auth support' },
    { name: 'OpenAPI', desc: 'Generic source driven by an OpenAPI / Swagger spec — paginates + auths automatically' },
    { name: 'REST / SaaS Connector', desc: 'Universal connector for SaaS APIs (Salesforce, HubSpot, Stripe, ServiceNow, etc.)' },
    { name: 'Vector DB', desc: 'Pinecone / Weaviate / Qdrant / Chroma / pgvector — read embeddings + metadata' },
    { name: 'S3 / MinIO', desc: 'Read objects from S3-compatible storage' },
    { name: 'Azure Blob', desc: 'Azure Blob Storage (wasbs://) — flat namespace, SAS / Key / AAD auth' },
    { name: 'ADLS Gen2', desc: 'Azure Data Lake Storage Gen2 (abfss://) — hierarchical namespace, AAD / SAS / Key auth' },
    { name: 'GCS', desc: 'Google Cloud Storage (gs://) with service-account or HMAC auth' },
    { name: 'SharePoint', desc: 'Microsoft Graph: /sites/{site}/drives/{drive}/items — read files from a SharePoint site' },
    { name: 'OneDrive', desc: 'Microsoft Graph: /me/drive or /users/{id}/drive — read files from OneDrive' },
    { name: 'Google Drive', desc: 'Google Drive API v3 — read files / folders by ID' },
    { name: 'Dropbox', desc: 'Dropbox API v2 — read files by path' },
    { name: 'Box', desc: 'Box API v2 — read files / folders by ID' },
    { name: 'Kafka', desc: 'Consume messages from Kafka / Redpanda topics' },
    { name: 'FTP / SFTP', desc: 'Download files from FTP, FTPS, or SFTP servers (password or SSH key auth)' },
    { name: 'Google Sheets', desc: 'Import from Google Sheets via public CSV export' },
    { name: 'Delta Lake', desc: 'Read Delta Lake tables with time-travel support' },
  ]},
  { category: 'Transform', icon: 'zap', color: 'bg-amber-50 border-amber-200', nodes: [
    { name: 'Data Wrangler', desc: 'Stepwise transform builder — chain Filter / Select / Rename / Cast / Derive / Group-By sub-steps in one node with per-step preview. Compiles to a single DuckDB CTE pipeline at run time.' },
    { name: 'Transform (SQL)', desc: 'Write SQL against upstream data. Reference as source_table or by node name.' },
    { name: 'Filter', desc: 'Filter rows using column conditions (equals, greater than, contains, etc.)' },
    { name: 'Sort', desc: 'Order rows by one or more columns (ascending/descending, NULLS first/last)' },
    { name: 'Derived Column', desc: 'Add or replace columns using SQL expressions' },
    { name: 'Window', desc: 'Window functions: rank, lag, lead, row_number, running totals over partitions' },
    { name: 'Flatten', desc: 'Flatten nested JSON structs into columns, or explode array columns into one row per element' },
    { name: 'Schema Mapper', desc: 'Source-to-target field mapping with type coercion — rename + cast + reorder in one step' },
    { name: 'Sample', desc: 'Take the first N rows, a percentage, or a random sample (seed for repeatable runs)' },
    { name: 'Data Quality', desc: 'Rule-based row checks (not null / unique / range / accepted values / regex). On failure: drop the bad rows, fail the run, tag them, or split them out a separate "reject" branch.' },
    { name: 'Data Profile', desc: 'Compute per-column statistics: null %, distinct count, min / max, top values. Useful as a first-step diagnostic.' },
    { name: 'SCD2', desc: 'Slowly-Changing Dimension Type 2 — track historical versions per business key (effective_from / effective_to / is_current columns)' },
  ]},
  { category: 'Combine', icon: 'link', color: 'bg-purple-50 border-purple-200', nodes: [
    { name: 'Join', desc: 'SQL-style join (inner, left, right, full, cross) on key columns' },
    { name: 'Union', desc: 'Stack rows from multiple inputs (union all or distinct)' },
    { name: 'Lookup Join', desc: 'Enrich rows with columns from a reference dataset matched on a key (data lookup; distinct from the control-flow Lookup activity)' },
    { name: 'Deduplicate', desc: 'Remove duplicate rows by key columns (keep first or last)' },
    { name: 'Aggregate', desc: 'Group by columns and apply SUM, COUNT, AVG, MIN, MAX functions' },
    { name: 'Pivot', desc: 'Pivot rows into columns (crosstab / spreadsheet-style)' },
    { name: 'Unpivot', desc: 'Melt columns into rows (normalize wide data)' },
  ]},
  { category: 'Flow Control', icon: 'shuffle', color: 'bg-indigo-50 border-indigo-200', nodes: [
    { name: 'If Condition', desc: 'Route each row to a True or False output by a condition. Legacy single-output edges map to True.' },
    { name: 'Switch', desc: 'Route each row to a named output branch by condition — true multi-output.' },
    { name: 'ForEach', desc: 'Run a saved sub-pipeline once per input row, injecting the row\'s columns as parameters.' },
    { name: 'Execute Pipeline', desc: 'Execute another saved pipeline as a child step, optionally passing parameters (runs synchronously).' },
    { name: 'Lookup', desc: 'Lookup activity — fetch a value or reference row into {{ $vars.X }} for control flow (watermarks, config lookups, row-count gates).' },
    { name: 'Set Variable', desc: 'Set runtime variables ({{ $vars.NAME }}) from a constant or SQL expression — read by any downstream step. Input rows pass through unchanged.' },
    { name: 'Wait', desc: 'Pause execution for a fixed duration before continuing.' },
    { name: 'Fail', desc: 'Deliberately halt the run with a custom error message — use to fast-fail when invariants break.' },
    { name: 'Retry', desc: 'Wrap a step with a retry policy: N attempts with backoff before bubbling the failure.' },
    { name: 'Batch Rows', desc: 'Advanced — split rows into fixed-size batches, tagging each with its batch index.' },
  ]},
  { category: 'Action', icon: 'globe', color: 'bg-teal-50 border-teal-200', nodes: [
    { name: 'HTTP Request', desc: 'Make HTTP calls (GET, POST, PUT, DELETE) with headers, body, and auth' },
    { name: 'Code / Script', desc: 'Run custom Python or JavaScript code against pipeline data' },
    { name: 'Execute SQL Task', desc: 'Run an arbitrary SQL statement against a connection — no result rows needed (DDL, maintenance, etc.)' },
    { name: 'File System', desc: 'File system operations: copy / move / delete / mkdir on local paths or remote storage' },
    { name: 'Copy Data', desc: 'Copy data between sources (database, file, S3) in bulk' },
    { name: 'Delete', desc: 'Delete files, database rows, or S3 objects' },
    { name: 'Get Metadata', desc: 'Inspect source metadata (size, type, schema, row count)' },
    { name: 'Send Email', desc: 'Send notification emails with pipeline data or attachments' },
    { name: 'Slack / Teams', desc: 'Post notifications to Slack or Teams channels via webhook' },
  ]},
  { category: 'AI', icon: 'zap', color: 'bg-violet-50 border-violet-200', nodes: [
    { name: 'Embedder', desc: 'Turn a text column into a vector column using Ollama, OpenAI, Cohere, sentence-transformers, or a deterministic hash fallback' },
    { name: 'LLM Guardrail', desc: 'PII / profanity / prompt-injection detection — routes flagged rows to a separate output for review' },
    { name: 'Semantic Router', desc: 'Classify each row into a label via embeddings or LLM. Use to dispatch downstream branches based on content.' },
  ]},
  { category: 'Outputs', icon: 'upload', color: 'bg-emerald-50 border-emerald-200', nodes: [
    { name: 'CSV Sink', desc: 'Write results to CSV files' },
    { name: 'JSON Sink', desc: 'Write results to JSON or JSONL files' },
    { name: 'Parquet Sink', desc: 'Write results to Parquet columnar format (via File Sink with .parquet extension)' },
    { name: 'Excel Sink', desc: 'Export results to Excel spreadsheets' },
    { name: 'File Sink (auto)', desc: 'CSV / JSON / Parquet / Excel — picks writer from output extension' },
    { name: 'Database Sink', desc: 'Insert / upsert results into SQL database tables' },
    { name: 'JDBC Sink', desc: 'Generic JDBC / warehouse sink via dialect registry (Snowflake, BigQuery, Redshift, etc.)' },
    { name: 'Warehouse Sink', desc: 'Load into cloud warehouses with schema-evolution support' },
    { name: 'Vector DB Sink', desc: 'Pinecone / Weaviate / Qdrant / Chroma / pgvector — write embeddings + metadata' },
    { name: 'S3 Sink', desc: 'Upload results to S3-compatible object storage' },
    { name: 'Azure Blob Sink', desc: 'Write to Azure Blob Storage (wasbs://)' },
    { name: 'ADLS Gen2 Sink', desc: 'Write to Azure Data Lake Storage Gen2 (abfss://)' },
    { name: 'GCS Sink', desc: 'Write to Google Cloud Storage (gs://)' },
    { name: 'SharePoint Sink', desc: 'Upload files to a SharePoint site via Microsoft Graph' },
    { name: 'OneDrive Sink', desc: 'Upload files to OneDrive via Microsoft Graph' },
    { name: 'Google Drive Sink', desc: 'Upload files to Google Drive via API v3' },
    { name: 'Dropbox Sink', desc: 'Upload files to Dropbox via API v2' },
    { name: 'Box Sink', desc: 'Upload files to Box via API v2' },
    { name: 'Kafka Sink', desc: 'Publish messages to Kafka / Redpanda topics' },
    { name: 'FTP / SFTP Sink', desc: 'Upload results as a CSV / JSON / Parquet file to an FTP, FTPS, or SFTP server (password or SSH key auth)' },
    { name: 'API Sink', desc: 'POST results to REST API endpoints' },
    { name: 'Webhook Sink', desc: 'Send results to webhook URLs' },
    { name: 'Email Sink', desc: 'Email pipeline results as attachments' },
    { name: 'Delta Sink', desc: 'Write to Delta Lake tables with merge / overwrite modes' },
  ]},
];

export default function HelpPage({
  environment,
  tier = 'free',
  user,
}: {
  environment?: 'dev' | 'prod';
  tier?: string;
  user?: { role?: string } | null;
} = {}) {
  const dark = useDarkMode();
  // OSS Free has no PROD environment — defence in depth so the runbook
  // branches below never render even if `environment` is forced via dev tools.
  const isProd = environment === 'prod' && tier === 'plus';
  const isAdmin = canAccessAdmin(user);

  usePageContext({
    page: 'help',
    visible_ids: [],
    selected_ids: [],
    filters: {},
    environment: environment ?? 'dev',
  });
  // Initial tab honors:
  //   - `fpulse_help_initial_tab` sessionStorage breadcrumb (set by
  //     deep-links from other pages); cleared on read so a refresh
  //     returns to default.
  //   - 2026-05-19 (P2 #20 of PAGE_BY_PAGE_AUDIT.md): the URL hash
  //     subroute (`#help/shortcuts`, `#help/nodes`, etc.) so the global
  //     "?" shortcut + future deep links can land on a specific tab
  //     without round-tripping through sessionStorage.
  const [tab, setTab] = useState<HelpTab>(() => {
    try {
      const t = sessionStorage.getItem('fpulse_help_initial_tab');
      if (t) {
        sessionStorage.removeItem('fpulse_help_initial_tab');
        if (['getting-started', 'how-to', 'shortcuts', 'nodes', 'reference'].includes(t)) {
          return t as HelpTab;
        }
      }
    } catch { /* sessionStorage unavailable */ }
    try {
      const sub = (window.location.hash || '').split('/')[1];
      if (sub && ['getting-started', 'how-to', 'shortcuts', 'nodes', 'reference'].includes(sub)) {
        return sub as HelpTab;
      }
    } catch { /* SSR / non-DOM context */ }
    return 'getting-started';
  });

  // Also react to runtime hash changes — clicking the in-app cheat-sheet
  // link from anywhere should switch the tab without a remount. This is
  // also what makes browser Back/Forward step through tab history (see
  // navigateToTab below — each tab click pushes a new hash entry, this
  // listener observes the navigation and updates the React state to match).
  useEffect(() => {
    const onHashChange = () => {
      const sub = (window.location.hash || '').split('/')[1];
      if (sub && ['getting-started', 'how-to', 'shortcuts', 'nodes', 'reference'].includes(sub)) {
        setTab(sub as HelpTab);
      } else if ((window.location.hash || '').replace('#', '') === 'help') {
        // Bare `#help` (no sub-route) — reset to the default tab so
        // Back from a sub-tab lands on Getting Started instead of
        // leaving the active tab visually stuck.
        setTab('getting-started');
      }
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  // 2026-06-01: Browser Back from a Help sub-tab (e.g. Documentation)
  // was jumping all the way out to Dashboard because tab clicks only
  // updated React state — the URL hash stayed at `#help` for every tab,
  // so the entire Help visit was a SINGLE browser history entry. By
  // routing through the canonical navigateToSubRoute helper, each tab
  // becomes its own history entry, and Back navigates Documentation →
  // Node Reference → Shortcuts → ... → Dashboard the way users expect.
  // The hashchange listener above keeps React state in sync when
  // Back/Forward fires.
  const navigateToTab = useCallback((next: HelpTab) => {
    navigateToSubRoute('help', next);
    setTab(next);
  }, []);
  const [nodeSearch, setNodeSearch] = useState('');
  const [expandedGuides, setExpandedGuides] = useState<Set<string>>(new Set());
  const [runbookSection, setRunbookSection] = useState<'overview' | 'deploy' | 'incidents' | 'rollback' | 'contacts'>('overview');

  // 2026-05-22 — single source of truth for the Help sub-tabs (key +
  // label + icon + subtitle). Both the page header and the tab strip
  // below render from this same array, matching the Insights page
  // pattern. The page H1 reflects the ACTIVE tab's icon + label —
  // it's the active sub-page, not a generic "Help" header.
  const HELP_TABS: Array<{
    key: HelpTab;
    label: string;
    subtitle: string;
    icon: React.ReactNode;
  }> = [
    {
      key: 'getting-started',
      label: 'Getting Started',
      subtitle: 'Five steps from install to your first pipeline.',
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M5 12V7a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v5" /><path d="M3 12h18" /><path d="M5 12v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6" />
        </svg>
      ),
    },
    {
      key: 'how-to',
      label: 'How-To Guides',
      subtitle: 'Recipes for common tasks — connections, transforms, alerts, scheduling.',
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" /><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
        </svg>
      ),
    },
    {
      key: 'shortcuts',
      label: 'Shortcuts',
      subtitle: 'Keyboard shortcuts for the editor, canvas, and chat.',
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="2" y="6" width="20" height="12" rx="2" /><path d="M6 10h.01" /><path d="M10 10h.01" /><path d="M14 10h.01" /><path d="M18 10h.01" /><path d="M6 14h12" />
        </svg>
      ),
    },
    {
      key: 'nodes',
      label: 'Node Reference',
      subtitle: 'Every node in F-Pulse OSS — what it does and what to put in it.',
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /><polyline points="3.27 6.96 12 12.01 20.73 6.96" /><line x1="12" y1="22.08" x2="12" y2="12" />
        </svg>
      ),
    },
    {
      key: 'reference',
      label: 'Documentation',
      subtitle: 'Reference for everyday tasks — install, run, connect, schedule.',
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="16" y1="13" x2="8" y2="13" /><line x1="16" y1="17" x2="8" y2="17" />
        </svg>
      ),
    },
  ];

  // Deep-link receiver. Other pages (e.g. Insights → AI Provider tab) navigate
  // here via `window.location.hash = '#help'` and stash a target guide title
  // prefix in sessionStorage under `fpulse_help_target_guide`. We pick it up,
  // switch to the How-To tab, expand the matching guide, scroll it into view,
  // and clear the key. Title-prefix match is forgiving enough to survive
  // small copy edits without breaking the link.
  useEffect(() => {
    const apply = () => {
      let target: string | null = null;
      try { target = sessionStorage.getItem('fpulse_help_target_guide'); } catch { /* ignore */ }
      if (!target) return;

      setTab('how-to');

      // Find the matching guide across all categories. Match on title prefix
      // (case-insensitive, normalized whitespace).
      const norm = (s: string) => s.toLowerCase().replace(/\s+/g, ' ').trim();
      const targetNorm = norm(target);
      let matchKey: string | null = null;
      for (const category of HOW_TO_GUIDES) {
        for (const g of category.guides) {
          if (norm(g.title).startsWith(targetNorm)) {
            matchKey = `${category.category}-${g.title}`;
            break;
          }
        }
        if (matchKey) break;
      }

      if (matchKey) {
        setExpandedGuides((prev) => {
          const next = new Set(prev);
          next.add(matchKey!);
          return next;
        });
        // Scroll into view after a short delay so the tab swap + expand
        // animation has a moment to land.
        setTimeout(() => {
          const el = document.querySelector<HTMLElement>(`[data-guide-key="${CSS.escape(matchKey!)}"]`);
          el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }, 250);
      }

      try { sessionStorage.removeItem('fpulse_help_target_guide'); } catch { /* ignore */ }
    };
    apply();
    // Re-apply on hash changes too (covers in-page navigations to #help)
    window.addEventListener('hashchange', apply);
    return () => window.removeEventListener('hashchange', apply);
  }, []);

  const toggleGuide = (key: string) => {
    setExpandedGuides((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  };

  // ── PROD: Operational Runbook ── (admin / super_admin only)
  // Non-admins viewing the PROD Help page see a viewer-friendly summary
  // instead of the full Operations Runbook (which contains deployment
  // commands, vault config locations, RBAC matrix, on-call contacts).
  if (isProd && !isAdmin) {
    return (
      <div className="flex-1 overflow-auto bg-canvas-bg">
        <div className="bg-slate-900 border-b border-slate-700">
          <div className="px-8 h-[78px] pt-3 pb-2 flex items-end">
            <div className="pb-1">
              <h1 className="text-xl font-bold text-white flex items-center gap-2">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-red-400">
                  <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" /><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
                </svg>
                Help
                <span className="text-xs font-bold px-2.5 py-0.5 rounded-full bg-red-500/20 text-red-300 border border-red-500/30 uppercase tracking-wider">PROD</span>
                <TierChip tier={tier} environment={environment} />
              </h1>
              <p className="text-xs text-slate-400 mt-0.5">Reference for production users</p>
            </div>
          </div>
        </div>
        <div className="w-full px-6 py-5 space-y-4 max-w-[1100px] mx-auto">
          <div className={`rounded-lg border p-6 ${dark ? 'bg-[#111827] border-slate-700' : 'bg-white border-slate-200'}`}>
            <h2 className={`text-base font-bold mb-2 ${dark ? 'text-slate-100' : 'text-slate-800'}`}>What you can do in PROD</h2>
            <ul className={`text-sm space-y-1.5 ml-4 list-disc ${dark ? 'text-slate-300' : 'text-slate-700'}`}>
              <li>View deployed pipelines and their schedules</li>
              <li>Monitor recent executions on the <strong>Executions</strong> page</li>
              <li>View dashboards and reports for pipelines you have access to</li>
              <li>Submit DEV pipelines for review (in DEV mode → Pipelines page)</li>
            </ul>
          </div>
          <div className={`rounded-lg border p-6 ${dark ? 'bg-[#111827] border-slate-700' : 'bg-white border-slate-200'}`}>
            <h2 className={`text-base font-bold mb-2 ${dark ? 'text-slate-100' : 'text-slate-800'}`}>What requires admin access</h2>
            <ul className={`text-sm space-y-1.5 ml-4 list-disc ${dark ? 'text-slate-300' : 'text-slate-700'}`}>
              <li>Approving / deploying pipelines to PROD</li>
              <li>Managing PROD credentials, vaults, and connections</li>
              <li>Incident response, rollback, and on-call contact lists</li>
              <li>Managing users, roles, and the workspace license</li>
            </ul>
            <p className={`mt-3 text-xs ${dark ? 'text-slate-400' : 'text-slate-500'}`}>
              Contact your workspace administrator if you need help with one of these.
            </p>
          </div>
          <div className={`rounded-lg border p-6 ${dark ? 'bg-[#111827] border-slate-700' : 'bg-white border-slate-200'}`}>
            <h2 className={`text-base font-bold mb-2 ${dark ? 'text-slate-100' : 'text-slate-800'}`}>Need DEV help instead?</h2>
            <p className={`text-sm ${dark ? 'text-slate-300' : 'text-slate-700'}`}>
              Switch to DEV from the environment toggle in the top nav for the full guide on building, testing, and submitting pipelines.
            </p>
          </div>
        </div>
      </div>
    );
  }

  if (isProd) {
    const RUNBOOK_SECTIONS = [
      { key: 'overview' as const, label: 'Overview', icon: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2" /><line x1="16" y1="2" x2="16" y2="6" /><line x1="8" y1="2" x2="8" y2="6" /><line x1="3" y1="10" x2="21" y2="10" /></svg> },
      { key: 'deploy' as const, label: 'Deployment', icon: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line x1="12" y1="3" x2="12" y2="15" /></svg> },
      { key: 'incidents' as const, label: 'Incident Response', icon: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg> },
      { key: 'rollback' as const, label: 'Rollback', icon: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="11 17 6 12 11 7" /><polyline points="18 17 13 12 18 7" /></svg> },
      { key: 'contacts' as const, label: 'Contacts', icon: <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z" /></svg> },
    ];

    return (
      <div className="flex-1 overflow-auto bg-canvas-bg">
        {/* Header + Tabs — single canonical banner */}
        <div className="bg-slate-900 border-b border-slate-700">
          <div className="px-8 h-[78px] pt-3 pb-2 grid grid-cols-3 items-end content-end gap-6">
            <div className="min-w-0 pb-1">
              <h1 className="text-xl font-bold text-white flex items-center gap-2">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-red-400">
                  <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" /><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
                </svg>
                Operations Runbook
                <span className="text-xs font-bold px-2.5 py-0.5 rounded-full bg-red-500/20 text-red-300 border border-red-500/30 uppercase tracking-wider">PROD</span>
                <TierChip tier={tier} environment={environment} />
              </h1>
              <p className="text-xs text-slate-400 mt-0.5">Standard operating procedures for production environment</p>
            </div>
            <div className="flex gap-0.5 justify-center items-center">
              {RUNBOOK_SECTIONS.map(s => (
                <button
                  key={s.key}
                  onClick={() => setRunbookSection(s.key)}
                  className={`px-4 py-2.5 text-sm font-semibold rounded-lg transition-all ${
                    runbookSection === s.key
                      ? 'border-red-400 text-slate-900 font-bold bg-gradient-to-b from-slate-200 to-slate-400 shadow-[inset_0_0_0_1.5px_rgba(203,213,225,0.70),inset_0_0_10px_rgba(148,163,184,0.30),inset_0_1px_0_rgba(255,255,255,0.85)]'
                      : 'border-transparent text-slate-400 hover:text-slate-200 hover:bg-white/[0.03]'
                  }`}
                >
                  {s.icon} {s.label}
                </button>
              ))}
            </div>
            <div className="flex justify-end items-center" />
          </div>
        </div>

        <div className="w-full px-8 py-6 max-w-[1100px] mx-auto">

          {/* Overview */}
          {runbookSection === 'overview' && (
            <div className="space-y-4">
              <div className="rounded-lg border border-slate-200 shadow-sm p-6" style={{ background: dark ? '#111827' : 'linear-gradient(135deg, #FFFFFF 0%, #FAFBFF 100%)' }}>
                <h2 className="text-base font-bold text-slate-800 mb-3 flex items-center gap-2">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" /><rect x="9" y="3" width="6" height="4" rx="1" /></svg> Production Environment
                </h2>
                <div className="grid grid-cols-2 gap-4">
                  <div className="bg-slate-50 rounded-lg p-4">
                    <p className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Architecture</p>
                    <ul className="text-xs text-slate-600 space-y-1.5">
                      <li className="flex items-center gap-2"><span className="w-1.5 h-1.5 rounded-full bg-emerald-400" /> Backend: FastAPI on port 8001</li>
                      <li className="flex items-center gap-2"><span className="w-1.5 h-1.5 rounded-full bg-emerald-400" /> Frontend: Vite/React on port 5174</li>
                      <li className="flex items-center gap-2"><span className="w-1.5 h-1.5 rounded-full bg-emerald-400" /> Database: SQLite (embedded)</li>
                      <li className="flex items-center gap-2"><span className="w-1.5 h-1.5 rounded-full bg-emerald-400" /> Execution: Local process</li>
                    </ul>
                  </div>
                  <div className="bg-slate-50 rounded-lg p-4">
                    <p className="text-xs font-bold text-slate-400 uppercase tracking-wider mb-2">Security</p>
                    <ul className="text-xs text-slate-600 space-y-1.5">
                      <li className="flex items-center gap-2"><span className="w-1.5 h-1.5 rounded-full bg-blue-400" /> Credential obfuscation at rest</li>
                      <li className="flex items-center gap-2"><span className="w-1.5 h-1.5 rounded-full bg-blue-400" /> RBAC: Admin / Member / Viewer</li>
                      <li className="flex items-center gap-2"><span className="w-1.5 h-1.5 rounded-full bg-blue-400" /> Session timeout: 8 hours</li>
                      <li className="flex items-center gap-2"><span className="w-1.5 h-1.5 rounded-full bg-blue-400" /> Audit trail on all write actions</li>
                    </ul>
                  </div>
                </div>
              </div>

              <div className="rounded-lg border border-slate-200 shadow-sm p-6" style={{ background: dark ? '#111827' : 'linear-gradient(135deg, #FFFFFF 0%, #FAFBFF 100%)' }}>
                <h2 className="text-base font-bold text-slate-800 mb-3">Health Checks</h2>
                <div className="space-y-2 text-xs text-slate-600">
                  <div className="flex items-center justify-between bg-slate-50 rounded-lg px-4 py-3">
                    <span className="font-medium">Backend API</span>
                    <code className="text-xs bg-slate-200 px-2 py-0.5 rounded font-mono">GET /api/health</code>
                  </div>
                  <div className="flex items-center justify-between bg-slate-50 rounded-lg px-4 py-3">
                    <span className="font-medium">All workflows list</span>
                    <code className="text-xs bg-slate-200 px-2 py-0.5 rounded font-mono">GET /api/workflows</code>
                  </div>
                  <div className="flex items-center justify-between bg-slate-50 rounded-lg px-4 py-3">
                    <span className="font-medium">Execution history</span>
                    <code className="text-xs bg-slate-200 px-2 py-0.5 rounded font-mono">GET /api/monitor/executions</code>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Deployment */}
          {runbookSection === 'deploy' && (
            <div className="space-y-4">
              <div className="rounded-lg border border-slate-200 shadow-sm p-6" style={{ background: dark ? '#111827' : 'linear-gradient(135deg, #FFFFFF 0%, #FAFBFF 100%)' }}>
                <h2 className="text-base font-bold text-slate-800 mb-4 flex items-center gap-2">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" /><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" /><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0" /><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" /></svg> Deployment Procedure
                </h2>
                <ol className="space-y-4">
                  {[
                    { step: 1, title: 'Review pipeline in DEV', desc: 'Verify the pipeline runs successfully in DEV environment with test data. Check all node configurations are correct.', color: 'bg-blue-500' },
                    { step: 2, title: 'Configure production credentials', desc: 'Go to Credentials page in PROD and ensure all required API keys, database passwords, and tokens are set.', color: 'bg-amber-500' },
                    { step: 3, title: 'Verify production connections', desc: 'Test all connections on the Connections page. Ensure endpoints are reachable and authenticated.', color: 'bg-emerald-500' },
                    { step: 4, title: 'Set production values', desc: 'Declare environment-specific values (paths, URLs, thresholds) as pipeline Parameters (Editor → Parameters) and supply PROD values per run via the Run dialog, API, or schedule. Secrets live on the Credentials page.', color: 'bg-purple-500' },
                    { step: 5, title: 'Deploy from queue', desc: 'Go to Approvals → Pending Review tab → click "Deploy to PROD". Monitor the run in Executions page.', color: 'bg-red-500' },
                    { step: 6, title: 'Set up alerts', desc: 'Open each pipeline in the Editor and use the Alert toolbar button to configure notifications on failure, long-running, or SLA breach.', color: 'bg-orange-500' },
                  ].map(item => (
                    <li key={item.step} className="flex items-start gap-4">
                      <div className={`w-8 h-8 rounded-full ${item.color} text-white text-sm font-bold flex items-center justify-center shrink-0`}>
                        {item.step}
                      </div>
                      <div>
                        <h3 className="text-sm font-bold text-slate-800">{item.title}</h3>
                        <p className="text-xs text-slate-500 mt-0.5">{item.desc}</p>
                      </div>
                    </li>
                  ))}
                </ol>
              </div>
            </div>
          )}

          {/* Incident Response */}
          {runbookSection === 'incidents' && (
            <div className="space-y-4">
              <div className="bg-red-50 border border-red-200 rounded-lg p-6">
                <h2 className="text-base font-bold text-red-800 mb-3 flex items-center gap-2">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg> Incident Response Protocol
                </h2>
                <div className="space-y-3">
                  {[
                    { severity: 'P1 — Critical', color: 'bg-red-500', desc: 'Production pipeline is down, data not flowing. Immediate action required.', actions: ['Check Executions page for failed execution', 'Review error logs in Executions → step details', 'Verify connections are reachable', 'Rollback to last known good state if needed'] },
                    { severity: 'P2 — High', color: 'bg-orange-500', desc: 'Pipeline running but producing incorrect data or running significantly slower.', actions: ['Compare recent execution times in Executions', 'Check if upstream data source changed schema', 'Review the resolved parameter values on the run (Executions → run detail)', 'Disable schedule temporarily if data corruption risk'] },
                    { severity: 'P3 — Medium', color: 'bg-amber-500', desc: 'Non-critical pipeline delayed or alerting. No immediate data impact.', actions: ['Check schedule configuration in Editor toolbar', 'Review Admin → Logs for system activity', 'Monitor next scheduled run'] },
                  ].map(inc => (
                    <div key={inc.severity} className="bg-white rounded-lg border border-red-100 p-4">
                      <div className="flex items-center gap-2 mb-2">
                        <span className={`w-3 h-3 rounded-full ${inc.color}`} />
                        <h3 className="text-sm font-bold text-slate-800">{inc.severity}</h3>
                      </div>
                      <p className="text-xs text-slate-600 mb-2">{inc.desc}</p>
                      <ul className="text-xs text-slate-500 space-y-1 ml-5">
                        {inc.actions.map((a, i) => (
                          <li key={i} className="list-disc">{a}</li>
                        ))}
                      </ul>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* Rollback */}
          {runbookSection === 'rollback' && (
            <div className="space-y-4">
              <div className="rounded-lg border border-slate-200 shadow-sm p-6" style={{ background: dark ? '#111827' : 'linear-gradient(135deg, #FFFFFF 0%, #FAFBFF 100%)' }}>
                <h2 className="text-base font-bold text-slate-800 mb-4 flex items-center gap-2">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="11 19 2 12 11 5 11 19" /><polyline points="22 19 13 12 22 5 22 19" /></svg> Rollback Procedures
                </h2>
                <div className="space-y-3">
                  <div className="bg-amber-50 border border-amber-200 rounded-lg p-4">
                    <h3 className="text-sm font-bold text-amber-800 mb-1">Pipeline Rollback</h3>
                    <ol className="text-xs text-amber-700 space-y-1.5 ml-4 list-decimal">
                      <li>Go to DEV → Workflows → select the pipeline</li>
                      <li>Open version history (clock icon)</li>
                      <li>Select the last known good version → Restore</li>
                      <li>Re-deploy from PROD → Deployed → Pending Review</li>
                    </ol>
                  </div>
                  <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                    <h3 className="text-sm font-bold text-blue-800 mb-1">Credential Rollback</h3>
                    <ol className="text-xs text-blue-700 space-y-1.5 ml-4 list-decimal">
                      <li>Go to Credentials page</li>
                      <li>Delete the compromised/incorrect credential</li>
                      <li>Re-create with the previous known-good values</li>
                      <li>Test connection before re-running pipeline</li>
                    </ol>
                  </div>
                  <div className="bg-red-50 border border-red-200 rounded-lg p-4">
                    <h3 className="text-sm font-bold text-red-800 mb-1">Emergency Stop</h3>
                    <p className="text-xs text-red-700">If a pipeline is actively corrupting production data:</p>
                    <ol className="text-xs text-red-700 space-y-1.5 ml-4 list-decimal mt-1.5">
                      <li>Open the pipeline in Editor → disable its schedule via the toolbar</li>
                      <li>If currently running, check Executions page and wait for completion or timeout</li>
                      <li>Review Admin → Logs tab for error context</li>
                      <li>Investigate root cause in DEV before re-enabling</li>
                    </ol>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Contacts — 2026-05-19 (P2 #7 of PAGE_BY_PAGE_AUDIT.md):
              previously this rendered three rows of fake contact data
              (admin@company.com, infra@company.com) which a Plus admin
              would see on their very first visit and might assume were
              real on-call destinations. We now render an explicit setup
              card pointing to Admin → Users until real on-call contacts
              ship. The role guidance is preserved as a static reference
              so admins still know which roles to invite. */}
          {runbookSection === 'contacts' && (
            <div className="space-y-4">
              <div className="rounded-lg border border-amber-200 bg-amber-50 p-5 flex items-start gap-3">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#d97706" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 mt-0.5">
                  <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
                  <line x1="12" y1="9" x2="12" y2="13" />
                  <line x1="12" y1="17" x2="12.01" y2="17" />
                </svg>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-bold text-amber-900">No on-call contacts configured yet</p>
                  <p className="text-xs text-amber-800 mt-1 leading-relaxed">
                    Add Platform Admin, Data Engineer, and Infrastructure contacts so this runbook shows real escalation paths instead of placeholders. The runbook will then surface the right people for each incident severity.
                  </p>
                  <a
                    href="#settings"
                    onClick={(e) => { e.preventDefault(); navigateTo('settings'); }}
                    className="inline-flex items-center gap-1 mt-2 text-xs font-semibold text-amber-900 hover:text-amber-950 underline"
                  >
                    Open Settings
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                      <line x1="7" y1="17" x2="17" y2="7" /><polyline points="7 7 17 7 17 17" />
                    </svg>
                  </a>
                </div>
              </div>

              <div className="rounded-lg border border-slate-200 shadow-sm p-6" style={{ background: dark ? '#111827' : 'linear-gradient(135deg, #FFFFFF 0%, #FAFBFF 100%)' }}>
                <h2 className="text-base font-bold text-slate-800 mb-4 flex items-center gap-2">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z" /></svg> Escalation Roles
                </h2>
                <p className="text-xs text-slate-500 mb-4">
                  Reference for who to invite. Live contact details surface here once contact management is available.
                </p>
                <div className="space-y-3">
                  {[
                    { role: 'Platform Admin', responsibility: 'Owns the F-Pulse install — provisioning, license, backups, RBAC.', escalation: 'P1 & P2 incidents' },
                    { role: 'Data Engineer', responsibility: 'Owns pipeline correctness — see the failing pipeline\'s metadata for the named owner.', escalation: 'P2 & P3 — pipeline-specific issues' },
                    { role: 'Infrastructure', responsibility: 'Owns the host — server, network, storage, disk space, OS patching.', escalation: 'Server, network, storage issues' },
                  ].map(c => (
                    <div key={c.role} className="flex items-start gap-4 bg-slate-50 rounded-lg p-4">
                      <div className="w-10 h-10 rounded-full bg-slate-200 flex items-center justify-center text-sm font-bold text-slate-600 shrink-0">
                        {c.role[0]}
                      </div>
                      <div className="flex-1">
                        <h3 className="text-sm font-bold text-slate-800">{c.role}</h3>
                        <p className="text-xs text-slate-500 mt-0.5">{c.responsibility}</p>
                      </div>
                      <span className="text-xs text-slate-400 bg-white border border-slate-200 px-2 py-1 rounded-lg shrink-0">
                        {c.escalation}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  // ── DEV: Original Help & Documentation ──
  // 2026-05-22 — H1 reflects the ACTIVE sub-tab (icon + label + subtitle).
  // HELP_TABS is the single source of truth shared with the tab strip.
  const activeTab = HELP_TABS.find((t) => t.key === tab) ?? HELP_TABS[0];
  return (
    <div className={`flex-1 overflow-auto ${dark ? 'bg-[#0B1220]' : 'bg-canvas-bg'}`}>
      {/* Header + Tabs — canonical shared PageHeader shell */}
      <PageHeader
        environment={environment}
        icon={<span className="text-blue-500">{activeTab.icon}</span>}
        title={activeTab.label}
        titleAccessory={<TierChip tier={tier} environment={environment} />}
        subtitle={activeTab.subtitle}
        tabs={
          <div className="flex gap-0.5 justify-center items-center">
            {/* 2026-05-22 — tab strip reads from the same HELP_TABS
                array the header uses, so adding or renaming a sub-tab
                only touches one place. */}
            {HELP_TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => navigateToTab(t.key)}
                className={`flex items-center gap-2 px-4 py-2.5 text-sm font-semibold rounded-lg transition-all capitalize whitespace-nowrap [&>svg]:shrink-0 ${
                  tab === t.key
                    ? dark
                      ? 'border-violet-400 text-violet-200 font-bold bg-gradient-to-b from-violet-400/30 to-violet-600/20 shadow-[inset_0_0_0_1.5px_rgba(167,139,250,0.55),inset_0_0_10px_rgba(139,92,246,0.30),inset_0_1px_0_rgba(255,255,255,0.22)]'
                      : 'text-white font-bold bg-gradient-to-b from-slate-600 to-slate-800 shadow-[inset_0_0_0_1.5px_rgba(148,163,184,0.65),inset_0_0_10px_rgba(100,116,139,0.35),inset_0_1px_0_rgba(255,255,255,0.22)]'
                    : dark
                      ? 'border-transparent text-slate-500 hover:text-slate-300 hover:bg-white/[0.03]'
                      : 'border-transparent text-slate-900 font-bold hover:text-violet-700 hover:bg-violet-50/50'
                }`}
              >
                {t.icon} {t.label}
              </button>
            ))}
          </div>
        }
      />

      {/* 2026-06-03 — outer width tightened from 1500px → 1024px
          (max-w-5xl). Every Help tab is documentation-shaped content
          (list rows, accordions, prose, code samples) and rows used
          `justify-between` so descriptions sat left and controls /
          kbd chips sat right; at 1500px that opened a hundreds-of-px
          dead gap mid-row (most visible on Shortcuts: "Open global
          search palette ........ Ctrl + K"). 1024px is the standard
          docs-site reading width and gives every Help tab consistent
          breathing room without cramping any grid.

          2026-06-04 — Documentation tab (`reference`) bumped to
          max-w-7xl (1280px) because it ships a two-pane layout
          (left index sidebar + right content with tables). The 5xl
          ceiling left the right content panel ~600px wide, squeezing
          the User-guides table. 7xl gives the content pane ~900px
          while the other Help tabs (single-column reading) stay at
          5xl where they belong. */}
      <div className={`w-full mx-auto px-8 py-6 ${tab === 'reference' ? 'max-w-7xl' : 'max-w-5xl'}`}>

        {/* In-app contact point — report issues, request connectors, check
            updates without leaving the app. Always visible across Help tabs. */}
        <HelpFeedback dark={dark} />

        {/* Getting Started Tab */}
        {tab === 'getting-started' && (
          <div className="space-y-4">
            {/* Welcome banner */}
            <div className="bg-gradient-to-r from-blue-50 to-indigo-50 border border-blue-200/60 rounded-2xl p-6 mb-2">
              <h2 className="text-lg font-bold text-slate-800 mb-1">Welcome to F-Pulse OSS</h2>
              <p className="text-sm text-slate-600 mb-4 max-w-[75ch]">
                Build data pipelines visually — connect sources, transforms, and outputs without writing code.
                Follow these 5 steps to get up and running.
              </p>
              <div className="flex items-center gap-3">
                <div className="flex -space-x-1">
                  {/* Reuse the same SVG icons as the Step rows below so the
                      preview strip mirrors the real steps in order
                      (Create → Configure → Connect → Run → Save) and renders
                      consistently across OS/browser fonts. The previous
                      emoji array (📥 ⚡ 🔗 ▶️ 💾) rendered as monochrome
                      tofu boxes on systems without an emoji font. */}
                  {GETTING_STARTED.map((item) => (
                    <span
                      key={item.step}
                      className="w-7 h-7 bg-white rounded-full flex items-center justify-center border border-blue-200 text-blue-600 shadow-sm"
                    >
                      {item.icon}
                    </span>
                  ))}
                </div>
                <span className="text-xs text-blue-600 font-medium">5 steps to your first pipeline</span>
              </div>
            </div>

            {/* Steps */}
            {GETTING_STARTED.map((item) => (
              <div key={item.step} className="rounded-lg border border-slate-200 shadow-sm bg-white overflow-hidden hover:shadow-sm transition-shadow">
                <div className="flex items-start gap-4 p-5">
                  <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-blue-50 to-indigo-50 border border-blue-200/60 flex items-center justify-center text-blue-600 shrink-0 shadow-sm">
                    {item.icon}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-xs font-bold text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">Step {item.step}</span>
                      <h3 className="text-sm font-bold text-slate-800">{item.title}</h3>
                    </div>
                    <div className="space-y-1.5">
                      {item.content.map((line, i) => {
                        // Simple markdown-like bold rendering
                        const parts = line.split(/(\*\*.*?\*\*)/g);
                        return (
                          <p key={i} className={`text-xs leading-relaxed ${line.startsWith('-') ? 'text-slate-500 pl-3' : 'text-slate-600'}`}>
                            {parts.map((part, j) =>
                              part.startsWith('**') && part.endsWith('**')
                                ? <strong key={j} className="text-slate-700 font-semibold">{part.slice(2, -2)}</strong>
                                : <span key={j}>{part}</span>
                            )}
                          </p>
                        );
                      })}
                    </div>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* How-To Guides Tab — May 3 2026: filter out plus_only guides on
            F-Pulse Free so users don't see how-to content for features
            their installation doesn't have. Categories with no remaining
            guides after filtering are hidden entirely. */}
        {tab === 'how-to' && (
          <div className="space-y-6">
            {HOW_TO_GUIDES.map((category) => {
              if ((category as any).plus_only && tier !== 'plus') return null;
              const visibleGuides = (category.guides as Array<any>).filter(
                (g) => tier === 'plus' || !g.plus_only
              );
              if (visibleGuides.length === 0) return null;
              return (
              <div key={category.category}>
                <h2 className="text-sm font-bold text-slate-700 mb-3 flex items-center gap-2">
                  <span className="text-violet-600 inline-flex">{category.icon}</span>
                  {category.category}
                </h2>
                <div className="space-y-2">
                  {visibleGuides.map((guide) => {
                    const key = `${category.category}-${guide.title}`;
                    const isOpen = expandedGuides.has(key);
                    return (
                      <div key={guide.title} data-guide-key={key} className="rounded-lg border border-slate-200 shadow-sm bg-white overflow-hidden">
                        <button
                          onClick={() => toggleGuide(key)}
                          className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-slate-50/50 transition-colors"
                        >
                          <span className="text-sm font-medium text-slate-700">{guide.title}</span>
                          <svg
                            width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
                            className={`text-slate-400 transition-transform shrink-0 ${isOpen ? 'rotate-180' : ''}`}
                          >
                            <polyline points="6 9 12 15 18 9" />
                          </svg>
                        </button>
                        {isOpen && (
                          <div className="px-4 pb-4 border-t border-slate-100">
                            <ol className="space-y-2 mt-3">
                              {guide.steps.map((step, i) => {
                                const parts = step.split(/(\*\*.*?\*\*)/g);
                                const isSubItem = step.startsWith('-');
                                return (
                                  <li key={i} className={`flex gap-2.5 ${isSubItem ? 'pl-6' : ''}`}>
                                    {!isSubItem && (
                                      <span className="w-5 h-5 rounded-full bg-blue-50 text-blue-600 text-xs font-bold flex items-center justify-center shrink-0 mt-0.5 border border-blue-200">
                                        {i + 1}
                                      </span>
                                    )}
                                    {isSubItem && (
                                      <span className="text-slate-400 shrink-0 mt-0.5">•</span>
                                    )}
                                    <span className="text-xs text-slate-600 leading-relaxed">
                                      {parts.map((part, j) =>
                                        part.startsWith('**') && part.endsWith('**')
                                          ? <strong key={j} className="text-slate-700 font-semibold">{part.slice(2, -2)}</strong>
                                          : <span key={j}>{part}</span>
                                      )}
                                    </span>
                                  </li>
                                );
                              })}
                            </ol>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
              );
            })}
          </div>
        )}

        {/* Shortcuts Tab */}
        {tab === 'shortcuts' && (
          <div className="space-y-6">
            {SHORTCUTS.map((section) => (
              <div key={section.category} className="rounded-lg border border-slate-200 shadow-sm bg-white overflow-hidden">
                <div className="px-4 py-2.5 bg-slate-50 border-b border-slate-100">
                  <h3 className="text-xs font-bold text-slate-600 uppercase tracking-wider">{section.category}</h3>
                </div>
                <div className="divide-y divide-slate-100">
                  {section.items.map((shortcut, i) => {
                    // 2026-05-25 — supports `altKeys` so equivalent
                    // combos share one row (Redo: Ctrl+Shift+Z OR Ctrl+Y).
                    const renderCombo = (keys: string[], comboKey: string) => (
                      <span key={comboKey} className="inline-flex items-center gap-1">
                        {keys.map((key, j) => (
                          <span key={j} className="inline-flex items-center">
                            {j > 0 && <span className="text-slate-300 mx-0.5">+</span>}
                            <kbd className="px-2 py-1 text-xs font-bold bg-slate-100 text-slate-600 rounded-md border border-slate-200 shadow-sm">
                              {key}
                            </kbd>
                          </span>
                        ))}
                      </span>
                    );
                    return (
                      <div key={i} className="flex items-center justify-between px-4 py-2.5 hover:bg-slate-50/50">
                        <span className="text-sm text-slate-600">{shortcut.desc}</span>
                        <div className="flex items-center gap-2">
                          {renderCombo(shortcut.keys, 'primary')}
                          {shortcut.altKeys && (
                            <>
                              <span className="text-[10px] uppercase tracking-wider text-slate-400 font-semibold">or</span>
                              {renderCombo(shortcut.altKeys, 'alt')}
                            </>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Node Reference Tab */}
        {tab === 'nodes' && (
          <div className="space-y-6">
            <div className="relative">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400">
                <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
              </svg>
              <input
                value={nodeSearch}
                onChange={(e) => setNodeSearch(e.target.value)}
                placeholder="Search nodes..."
                className="w-full pl-9 pr-4 py-2.5 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-pipe-200 bg-white"
              />
            </div>

            {/* Live connector-coverage + cert strip (data-driven; fails closed).
                Hidden during search so it doesn't sit above filtered results. */}
            {!nodeSearch && <ConnectorCoverage />}

            {(() => {
              // 2026-05-19 (P1 #3 of PAGE_BY_PAGE_AUDIT.md): the previous
              // pass dropped every category that matched zero nodes, so a
              // search that matched nothing left a blank canvas under the
              // input — looked broken. Compute total matches first so we
              // can render a real "no results" empty state when nothing
              // matched, and only group by category when there's content.
              const grouped = NODE_CATALOG.map((cat) => {
                const filtered = nodeSearch
                  ? cat.nodes.filter((n) => n.name.toLowerCase().includes(nodeSearch.toLowerCase()) || n.desc.toLowerCase().includes(nodeSearch.toLowerCase()))
                  : cat.nodes;
                return { cat, filtered };
              });
              const totalMatches = grouped.reduce((acc, g) => acc + g.filtered.length, 0);
              if (nodeSearch && totalMatches === 0) {
                return (
                  <div className="rounded-lg border border-dashed border-slate-300 bg-slate-50/40 p-10 text-center">
                    <svg className="mx-auto mb-3 text-slate-400" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
                      <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
                    </svg>
                    <p className="text-sm font-semibold text-slate-700">No nodes match "{nodeSearch}"</p>
                    <p className="text-xs text-slate-500 mt-1 max-w-md mx-auto">
                      Try searching by category (e.g. <code className="bg-slate-100 px-1 rounded">source</code>, <code className="bg-slate-100 px-1 rounded">transform</code>) or by behaviour (<code className="bg-slate-100 px-1 rounded">join</code>, <code className="bg-slate-100 px-1 rounded">aggregate</code>).
                    </p>
                    <button
                      onClick={() => setNodeSearch('')}
                      className="mt-3 text-xs font-semibold text-pipe-600 hover:text-pipe-700"
                    >
                      Clear search
                    </button>
                  </div>
                );
              }
              return grouped.map(({ cat, filtered }) => {
                if (!filtered.length) return null;
                return (
                  <div key={cat.category}>
                    <h3 className="text-base font-bold text-slate-800 mb-3 flex items-center gap-2">
                      <Icon name={cat.icon} size={18} className="text-slate-500" />
                      {cat.category}
                      <span className="text-xs font-medium text-slate-500">{filtered.length} nodes</span>
                    </h3>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                      {filtered.map((node) => (
                        <div key={node.name} className={`rounded-lg border p-4 ${cat.color}`}>
                          <div className="text-base font-semibold text-slate-800">{node.name}</div>
                          <div className="text-sm text-slate-600 mt-1 leading-relaxed">{node.desc}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              });
            })()}
          </div>
        )}

        {/* Reference / Documentation Tab */}
        {tab === 'reference' && <DocsReference />}

        {/* Footer */}
        <div className="mt-10 pt-6 border-t border-slate-200/60 text-center">
          <p className="text-xs text-slate-400">
            Need more help? Use the pipeline chat to ask questions, or describe what you want to build.
          </p>
          <p className="text-xs text-slate-300 mt-1">F-Pulse v1.0.0</p>
        </div>
      </div>
    </div>
  );
}
