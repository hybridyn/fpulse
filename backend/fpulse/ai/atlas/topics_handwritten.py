"""
Hand-written atlas topics — pages, glossary, how-to, features, editions.

These topics ANSWER product-knowledge questions that aren't in any
single source file. Adding a new page or feature? Add a topic here too
so the Copilot can talk about it. The drift-guard test enforces that
every HelpPage section / route also has a corresponding topic.

Author conventions:
  * Keep bodies 3-8 sentences. Markdown is fine.
  * Include 5-10 aliases per topic — natural phrasings, synonyms,
    misspellings ("docs" / "documentation" / "documents" / "manual").
  * Set tier=Tier.PLUS only for features that genuinely don't exist
    in OSS — see edition-matrix.md.
  * Cross-link with see_also when there's a clear follow-up topic.
  * Don't name competitor products — see feedback_no_competitor_product_names.
"""

from __future__ import annotations

from .schema import Tier, Topic, TopicCategory


# ─────────────────────────────────────────────────────────────────────
# PAGES — one topic per route in the sidebar
# ─────────────────────────────────────────────────────────────────────


_PAGES: tuple[Topic, ...] = (
    Topic(
        id="page.dashboard",
        category=TopicCategory.PAGE,
        title="Dashboard",
        aliases=(
            "dashboard", "home page", "landing page", "main page",
            "the dashboard", "show me the dashboard", "open dashboard",
            "where is the dashboard", "what's on the dashboard",
        ),
        body=(
            "The **Dashboard** is the global landing page. It shows a workspace overview "
            "(pipeline / project / schedule / connection counts), success rate and run "
            "volume for the last 24 hours, recent failed pipelines, recent runs, active "
            "schedules, and system stats (CPU, memory, threads, storage). It also hosts "
            "the F-Pulse Copilot panel on the right edge."
        ),
        see_also=("page.pipelines", "page.executions", "page.pool"),
    ),
    Topic(
        id="page.pipelines",
        category=TopicCategory.PAGE,
        title="Pipelines",
        aliases=(
            "pipelines", "pipelines page", "workflows", "workflows page",
            "my pipelines", "list of pipelines", "where are my pipelines",
            "show pipelines page", "open pipelines",
        ),
        body=(
            "The **Pipelines** page lists every workflow in the workspace with name, "
            "status, last-run timestamp, schedule, and step count. You can run, edit, "
            "schedule, duplicate, or delete pipelines from here. The new-pipeline "
            "button opens either the visual Editor or the Templates picker. Pipelines "
            "live under Workflows in the sidebar (renamed 'Deployed' on PROD environments)."
        ),
        see_also=("page.editor", "page.executions", "howto.create_pipeline"),
    ),
    Topic(
        id="page.editor",
        category=TopicCategory.PAGE,
        title="Editor",
        aliases=(
            "editor", "editor page", "canvas", "the canvas", "pipeline editor",
            "where do i build pipelines", "drag and drop editor",
            "open editor", "design view",
        ),
        body=(
            "The **Editor** (also called the canvas) is where you build pipelines "
            "visually. Drag node types from the left palette onto the canvas, connect "
            "them with edges, and configure each step in the right-hand config panel. "
            "Save / save-as / run / debug controls are at the top. The Copilot can draft "
            "pipelines from a natural-language description and place them on the canvas. "
            "Editor is a DEV-environment surface only."
        ),
        see_also=("page.templates", "glossary.node", "howto.create_pipeline"),
    ),
    Topic(
        id="page.executions",
        category=TopicCategory.PAGE,
        title="Executions",
        aliases=(
            "executions", "executions page", "runs page", "run history",
            "execution history", "where do i see runs", "past runs",
            "what runs happened", "show executions",
        ),
        body=(
            "The **Executions** page is a chronological list of every pipeline run with "
            "status (success / failed / running / queued), trigger source (manual / "
            "scheduled / API), duration, rows processed, and a link to the full log. "
            "Click a row to see step-by-step traces and any error stack. Filter by "
            "pipeline, status, environment, or time window. Cancel an in-flight run "
            "with the Stop button on its row."
        ),
        see_also=("page.dashboard", "howto.debug_pipeline", "howto.see_failures"),
    ),
    Topic(
        id="page.connections",
        category=TopicCategory.PAGE,
        title="Connections",
        aliases=(
            "connections", "connections page", "data sources", "sources page",
            "where are my connections", "list connections",
            "manage connections", "show connections",
        ),
        body=(
            "The **Connections** page lists every data source / destination registered "
            "in the workspace — databases, APIs, files, and SaaS endpoints. Each entry "
            "shows type (Postgres / Salesforce / S3 / etc.), connection name, last "
            "health check, and which credential it uses. From here you can test, edit, "
            "or delete connections. Credentials are stored separately under the "
            "Credentials child page."
        ),
        see_also=("page.credentials", "glossary.connection", "glossary.connector"),
    ),
    Topic(
        id="page.credentials",
        category=TopicCategory.PAGE,
        title="Credentials",
        aliases=(
            "credentials", "credentials page", "secrets", "secret store",
            "where are passwords stored", "manage credentials",
            "api keys page", "tokens",
        ),
        body=(
            "The **Credentials** page stores encrypted secrets — API keys, OAuth tokens, "
            "passwords, certs — keyed by name so multiple Connections can reuse the "
            "same credential. Secrets are encrypted at rest with the installation's "
            "encryption key and never returned in API responses. Credentials live under "
            "the Connections section in the sidebar and are a DEV-environment surface."
        ),
        see_also=("page.connections", "howto.add_credential"),
    ),
    Topic(
        id="page.pool",
        category=TopicCategory.PAGE,
        title="Pool",
        aliases=(
            "pool", "pool page", "runtime pool", "execution pool",
            "compute page", "workers page", "concurrency settings",
            "spill settings", "scaling page",
        ),
        body=(
            "The **Pool** page is the runtime / compute observatory. It shows the "
            "configured concurrency cap, current in-flight runs, the DuckDB memory + "
            "spill settings (with an SSD vs HDD badge), per-connection rate-limit "
            "governors, and recent throughput. Tune the volume tier (50 / 100 / 500 GB) "
            "here. Pool is read-only on OSS — operator knobs surface as documented "
            "env-var hints rather than live toggles."
        ),
        see_also=("doc.scaling", "feature.scheduling"),
    ),
    Topic(
        id="page.settings",
        category=TopicCategory.PAGE,
        title="Settings",
        aliases=(
            "settings", "settings page", "preferences", "config",
            "configuration", "where are settings", "open settings",
            "customize", "options",
        ),
        body=(
            "The **Settings** page has 4 tabs: **General** (theme, autosave, default "
            "run-safety mode, execution tuning), **Security** (Posture, Operator "
            "Config — env-var hints, Authentication is Plus-only), **Notifications** "
            "(email / Slack / webhook / Discord delivery), and **About** (version, "
            "license, support links). Most controls are wired to live consumers — "
            "autosave, snap-to-grid, minimap, etc. take effect without page reload."
        ),
        see_also=("page.notifications", "page.account"),
    ),
    Topic(
        id="page.help",
        category=TopicCategory.PAGE,
        title="Help",
        aliases=(
            "help", "help page", "help center", "where is help",
            "open help", "user manual", "in-app docs",
            "getting started", "first time using",
        ),
        body=(
            "The **Help** page is the in-app documentation center. It has 5 tabs: "
            "Getting Started (6-step walkthrough from first pipeline to scheduling), "
            "How-To Guides (Building Pipelines, Managing Data, Scheduling & Alerts, "
            "Triggering from API), Shortcuts (keyboard reference), Nodes (per-node-type "
            "reference), and Reference (concept index). Click the Help icon (top-right) "
            "to open it from any page."
        ),
        see_also=("howto.find_docs", "howto.use_copilot"),
    ),
    Topic(
        id="page.account",
        category=TopicCategory.PAGE,
        title="Account",
        aliases=(
            "account", "account page", "profile", "my profile",
            "user settings", "my account", "user info",
        ),
        body=(
            "The **Account** page shows your user identity (email, role, workspace) "
            "and lets you change display name, password (where local auth is enabled), "
            "and personal API tokens. Note: OSS is single-user — there's no team / "
            "invite / role-assignment surface here. Multi-user workspaces and SSO are "
            "Plus features."
        ),
        see_also=("page.settings",),
    ),
    Topic(
        id="page.notifications",
        category=TopicCategory.PAGE,
        title="Notifications",
        aliases=(
            "notifications", "notifications page", "notification history",
            "alert history", "past alerts", "what alerts fired",
            "delivered notifications",
        ),
        body=(
            "The **Notifications** page lists every alert that has fired (pipeline "
            "failures, long-running runs, missed schedules, threshold breaches) with "
            "timestamp, delivery channel, and outcome. Filter by channel, severity, or "
            "pipeline. To configure WHICH alerts fire and WHERE they go, use Settings → "
            "Notifications."
        ),
        see_also=("page.settings", "feature.alerts"),
    ),
    Topic(
        # NOTE: id stays "page.ai_hub" for back-compat — 3 other topics
        # reference it via see_also. Title + aliases updated to reflect
        # the May 17 2026 PR 4 rename (AI-Hub → Insights).
        id="page.ai_hub",
        category=TopicCategory.PAGE,
        title="Insights",
        aliases=(
            "insights", "insights page",
            "ai hub", "ai page",  # back-compat aliases (pre-May-17)
            "copilot settings", "ai settings",
            "ai provider", "configure ai", "model settings",
            "where do i set the model", "switch provider",
        ),
        body=(
            "The **Insights** page consolidates everything Copilot-related: the AI Provider "
            "tab (choose Ollama / Anthropic / OpenAI / OpenRouter and the model), the "
            "Activity tab (recent Copilot runs with traces), the Trust tab (the eval "
            "harness scorecard), and the Reports tab (token / cost usage). For local Ollama "
            "on a CPU laptop, qwen2.5:7b is the recommended floor (~6 GB RAM, 30–60 s per "
            "turn) — anything smaller can't reliably drive the tool-use loop. For faster "
            "responses or production quality, switch to a cloud provider."
        ),
        see_also=("howto.use_copilot", "feature.copilot", "doc.ai"),
    ),
    Topic(
        id="page.templates",
        category=TopicCategory.PAGE,
        title="Templates",
        aliases=(
            "templates", "templates page", "pipeline templates",
            "starter pipelines", "examples", "starters",
            "where are templates", "show templates",
        ),
        body=(
            "The **Templates** page is the template chooser — 20+ pre-built pipeline "
            "starters covering common data-engineering patterns (CSV-to-database, "
            "API-to-warehouse, scheduled report, file watcher, etc.). Click a template "
            "to clone it into a new pipeline with all nodes pre-configured. You can "
            "also save your own pipelines as templates for re-use."
        ),
        see_also=("page.editor", "howto.use_templates"),
    ),
    Topic(
        id="page.projects",
        category=TopicCategory.PAGE,
        title="Projects",
        aliases=(
            "projects", "projects page", "my projects", "organization",
            "where are projects", "list projects", "manage projects",
        ),
        body=(
            "The **Projects** page groups pipelines into organisational buckets — a "
            "project is roughly a 'topic' or 'team area' (e.g. 'Sales analytics', "
            "'Inventory ETL'). Each project has its own pipelines, schedules, and "
            "connection scope. Projects are a DEV-environment concept; on PROD all "
            "pipelines are visible together."
        ),
        see_also=("page.pipelines", "glossary.project"),
    ),
    Topic(
        id="page.cert_matrix",
        category=TopicCategory.PAGE,
        title="Cert Matrix",
        aliases=(
            "cert matrix", "certification matrix", "connector matrix",
            "supported connectors", "which connectors work",
            "connector quality", "what connectors are tested",
        ),
        body=(
            "The **Cert Matrix** page is the connector certification scorecard: every "
            "shipped connector is rated for schema coverage, auth method, incremental "
            "sync, pagination, error handling, and depth score (0-5). Use this when "
            "evaluating whether a given connector is production-grade for your use "
            "case. All 33 connectors are open and unlocked in OSS — Plus does not "
            "gate connector access."
        ),
        see_also=("doc.connectors",),
    ),
    Topic(
        id="page.extraction",
        category=TopicCategory.PAGE,
        title="Extraction",
        aliases=(
            "extraction", "extraction page", "data extraction",
            "extraction monitor", "extract jobs", "data wrangler",
        ),
        body=(
            "The **Extraction** page is the data-extraction monitor — it shows in-flight "
            "and recent extraction jobs (file ingest, API pulls, scheduled syncs) with "
            "row counts, throughput, and error counts. Useful for diagnosing slow or "
            "stuck data fetches before they become pipeline failures."
        ),
        see_also=("page.pool",),
    ),
    Topic(
        id="page.lineage",
        category=TopicCategory.PAGE,
        title="Lineage",
        aliases=(
            "lineage", "lineage page", "data lineage", "where does data come from",
            "upstream downstream", "data flow map", "lineage graph",
        ),
        body=(
            "The **Lineage** page visualises end-to-end data flow — which sources feed "
            "which transforms feed which destinations — across pipelines. Useful for "
            "impact analysis ('if I change this connection, what breaks?'). Lineage is "
            "a **Plus-only feature**; OSS doesn't render this page."
        ),
        tier=Tier.PLUS,
        see_also=("edition.plus",),
    ),
)


# ─────────────────────────────────────────────────────────────────────
# GLOSSARY — product terminology
# ─────────────────────────────────────────────────────────────────────


_GLOSSARY: tuple[Topic, ...] = (
    Topic(
        id="glossary.pipeline",
        category=TopicCategory.GLOSSARY,
        title="Pipeline (a.k.a. Workflow)",
        aliases=(
            "what is a pipeline", "what's a pipeline", "define pipeline",
            "what is a workflow", "pipeline meaning", "workflow meaning",
        ),
        body=(
            "A **pipeline** (also called a workflow) is a directed graph of steps that "
            "moves and transforms data. Each step is a node — a source reads data, "
            "transforms reshape it, and destinations write it out. Pipelines live in "
            "the Pipelines page, are built in the Editor, and run via the Executions "
            "engine. A pipeline definition is saved as IR (intermediate representation) "
            "to the workspace store and versioned on every save."
        ),
        see_also=("glossary.execution", "glossary.node", "page.editor"),
    ),
    Topic(
        id="glossary.execution",
        category=TopicCategory.GLOSSARY,
        title="Execution (a.k.a. Run)",
        aliases=(
            "what is an execution", "what is a run", "define execution",
            "define run", "what does execution mean", "run vs execution",
        ),
        body=(
            "An **execution** (or run) is one invocation of a pipeline — manual, "
            "scheduled, or API-triggered. Each execution gets a unique id, captures "
            "rows-in / rows-out per step, full timing, environment, trigger source, "
            "and any errors. Executions are immutable history; cancelling one stops "
            "the in-flight run but the record remains. Browse them on the Executions "
            "page."
        ),
        see_also=("page.executions", "glossary.pipeline"),
    ),
    Topic(
        id="glossary.node",
        category=TopicCategory.GLOSSARY,
        title="Node (a.k.a. Step)",
        aliases=(
            "what is a node", "what is a step", "node vs step",
            "define node", "what are nodes", "what types of nodes",
        ),
        body=(
            "A **node** (or step) is one unit of work in a pipeline — a source reader, "
            "a transform (filter, join, aggregate, etc.), a conditional, a destination "
            "writer, or an action like sending an email. F-Pulse ships a broad set of "
            "node types across Data Movement, Transform, Combine, Control Flow, Action, "
            "and AI categories. Ask 'what nodes are available' for the live list, or see "
            "the Nodes tab in Help for the full reference."
        ),
        see_also=("page.editor", "doc.nodes"),
    ),
    Topic(
        id="glossary.connection",
        category=TopicCategory.GLOSSARY,
        title="Connection",
        aliases=(
            "what is a connection", "define connection", "connection vs connector",
            "what does connection mean", "what is a dsn",
        ),
        body=(
            "A **connection** is a configured endpoint to a specific data source or "
            "destination — e.g. 'production-postgres', 'sales-salesforce-sandbox'. It "
            "bundles the connector type (Postgres / Salesforce / S3 / …), the host or "
            "URL, the credential reference, and any connector-specific parameters. "
            "Multiple connections can share one credential. Manage them on the "
            "Connections page."
        ),
        see_also=("glossary.connector", "page.connections", "howto.add_connection"),
    ),
    Topic(
        id="glossary.connector",
        category=TopicCategory.GLOSSARY,
        title="Connector",
        aliases=(
            "what is a connector", "define connector", "what connectors are there",
            "supported integrations", "connector list", "available connectors",
        ),
        body=(
            "A **connector** is the integration *type* — Postgres, Salesforce, Stripe, "
            "S3, etc. — versus a connection which is a *specific configured instance* "
            "of one. F-Pulse ships connectors covering databases, files, cloud "
            "storage, and SaaS APIs. All connectors are open in OSS — Plus doesn't "
            "gate connector access. See the Cert Matrix page for per-connector "
            "production-readiness scores."
        ),
        see_also=("glossary.connection", "page.cert_matrix", "doc.connectors"),
    ),
    Topic(
        id="glossary.workspace",
        category=TopicCategory.GLOSSARY,
        title="Workspace",
        aliases=(
            "what is a workspace", "define workspace",
            "what does workspace mean", "is there one workspace or many",
        ),
        body=(
            "A **workspace** is the F-Pulse tenant boundary — one workspace owns one "
            "set of pipelines, projects, schedules, alerts, connections, credentials, "
            "and users. On OSS this is the entire installation (single-tenant). Plus "
            "supports multiple workspaces with RBAC isolation between them."
        ),
        see_also=("glossary.project", "edition.oss"),
    ),
    Topic(
        id="glossary.schedule",
        category=TopicCategory.GLOSSARY,
        title="Schedule",
        aliases=(
            "what is a schedule", "define schedule", "what does schedule mean",
            "scheduled pipelines", "cron",
        ),
        body=(
            "A **schedule** is a cron-based recurring trigger that runs a pipeline at "
            "fixed times. Each schedule has a cron expression (or a friendly preset "
            "like 'every hour' / 'weekdays at 9am'), an optional parameter set, and "
            "active/paused state. Active schedules surface on the Dashboard and on the "
            "owning pipeline's page. Missed schedules can trigger an alert if "
            "configured in Settings → Notifications."
        ),
        see_also=("feature.scheduling", "howto.schedule_pipeline"),
    ),
    Topic(
        id="glossary.project",
        category=TopicCategory.GLOSSARY,
        title="Project",
        aliases=(
            "what is a project", "define project", "project vs workspace",
            "what does project mean",
        ),
        body=(
            "A **project** is an organisational grouping of pipelines inside a "
            "workspace — typically by topic, team, or business area. Each project has "
            "its own pipelines + schedules + connection scope. Projects are a DEV-only "
            "concept; on PROD environments pipelines aren't bucketed by project."
        ),
        see_also=("page.projects", "glossary.workspace"),
    ),
    Topic(
        id="glossary.environment",
        category=TopicCategory.GLOSSARY,
        title="Environment (DEV vs PROD)",
        aliases=(
            "dev vs prod", "what is dev", "what is prod", "environments",
            "what does environment mean", "deploy to prod",
            "promote to production",
        ),
        body=(
            "F-Pulse has two environments: **DEV** (where you build and test) and "
            "**PROD** (where deployed pipelines run for real). On OSS, PROD is not "
            "exposed — everything happens in DEV. The PROD environment, DEV→PROD "
            "promotion, and the approval workflow are **Plus-only** features. The "
            "environment badge in the sidebar shows your current context."
        ),
        tier=Tier.BOTH,
        see_also=("edition.plus",),
    ),
    Topic(
        id="glossary.copilot",
        category=TopicCategory.GLOSSARY,
        title="Copilot",
        aliases=(
            "what is the copilot", "what does copilot do",
            "what is the chat", "what is the assistant",
            "ai chat", "ai assistant",
        ),
        body=(
            "The **Copilot** is the right-side AI chat panel — page-aware, tool-using, "
            "with 23 deterministic backend tools (list pipelines, query metrics, draft "
            "pipeline from intent, etc.). It can answer status / failure / metric "
            "questions instantly via a fast-lane, draft pipelines from natural "
            "language, and walk you through any feature. Provider and model are "
            "configurable in Insights → AI Provider."
        ),
        see_also=("feature.copilot", "page.ai_hub", "howto.use_copilot"),
    ),
    Topic(
        id="glossary.template",
        category=TopicCategory.GLOSSARY,
        title="Template",
        aliases=(
            "what is a template", "define template", "what are templates",
            "starter templates", "pipeline starter",
        ),
        body=(
            "A **template** is a pre-built pipeline you can clone as a starting point. "
            "F-Pulse ships 20+ templates covering common patterns: CSV-to-database, "
            "API-to-warehouse, scheduled report, file watcher, change-data-capture, "
            "and more. You can also save your own pipelines as templates."
        ),
        see_also=("page.templates", "howto.use_templates"),
    ),
)


# ─────────────────────────────────────────────────────────────────────
# HOW-TO — task playbooks
# ─────────────────────────────────────────────────────────────────────


_HOWTOS: tuple[Topic, ...] = (
    Topic(
        id="howto.find_docs",
        category=TopicCategory.HOWTO,
        title="Where are the docs / documentation",
        aliases=(
            "documents", "documentation", "docs",
            "show me docs", "show me documentation", "where are the docs",
            "where is the documentation", "i need to see the documents",
            "i need documentation", "find docs", "open docs", "open documentation",
            "read the manual", "user manual", "user guide",
            "where can i read about this",
        ),
        body=(
            "F-Pulse documentation lives in three places:\n\n"
            "1. **In-app Help center** — click the Help icon (top-right) or go to the "
            "**Help** page in the sidebar. 5 tabs: Getting Started, How-To Guides, "
            "Shortcuts, Nodes, Reference.\n"
            "2. **Inline node help** — in the Editor, click the **?** icon on any "
            "node card to see that node's reference docs in a tooltip.\n"
            "3. **External docs site** — shipped under `docs/` in the repo: "
            "quickstart, connector catalog, node reference, scaling guide, FAQ, "
            "architecture, deployment, and the OSS vs Plus comparison.\n\n"
            "You can also ask the Copilot any 'how do I…' question and it will walk "
            "you through the steps."
        ),
        see_also=("page.help", "doc.quickstart", "howto.use_copilot"),
    ),
    Topic(
        id="howto.create_pipeline",
        category=TopicCategory.HOWTO,
        title="How to create a pipeline",
        aliases=(
            "how do i create a pipeline", "how to create a pipeline",
            "create new pipeline", "new pipeline", "start a pipeline",
            "build a pipeline", "make a pipeline", "first pipeline",
        ),
        body=(
            "Three ways:\n\n"
            "1. **From a template** — Pipelines page → New → Templates → pick one. "
            "The template clones with all nodes pre-configured; rename and save.\n"
            "2. **In the Editor from scratch** — Sidebar → Editor → drag a Source "
            "node onto the canvas, then add transforms / destinations and connect "
            "them with edges. Configure each step in the right-hand panel.\n"
            "3. **From natural language with the Copilot** — open the Copilot panel, "
            "tick **Allow drafts**, then type 'create a pipeline that…' — the Copilot "
            "drafts the IR and previews it for you to accept before saving."
        ),
        see_also=("page.editor", "page.templates", "howto.use_copilot"),
    ),
    Topic(
        id="howto.add_connection",
        category=TopicCategory.HOWTO,
        title="How to add a connection",
        aliases=(
            "how do i add a connection", "how to add a connection",
            "add new connection", "create connection", "new connection",
            "connect to database", "connect to api", "connect data source",
        ),
        body=(
            "1. Sidebar → **Connections** → click **+ New Connection** at the top "
            "right.\n"
            "2. Pick the connector type from the catalog (Postgres / Salesforce / "
            "S3 / 40+ others).\n"
            "3. Fill in the connection parameters (host, port, database, etc.).\n"
            "4. Either pick an existing credential or click **+ New credential** to "
            "create one — credentials are stored encrypted and reusable across "
            "connections.\n"
            "5. Click **Test** to verify; on success, **Save**. The connection now "
            "appears in node configuration dropdowns in the Editor."
        ),
        see_also=("page.connections", "howto.add_credential", "glossary.connection"),
    ),
    Topic(
        id="howto.add_credential",
        category=TopicCategory.HOWTO,
        title="How to add a credential",
        aliases=(
            "how do i add a credential", "how to add credential",
            "add password", "add api key", "store secret", "save secret",
            "where do passwords go",
        ),
        body=(
            "1. Sidebar → **Connections** → **Credentials** → **+ New Credential**.\n"
            "2. Give the credential a memorable name (e.g. 'sales-pg-prod').\n"
            "3. Pick the credential type — Username/Password, API Key, OAuth, "
            "Certificate, etc.\n"
            "4. Paste the secret values; F-Pulse encrypts them at rest with the "
            "installation's encryption key.\n"
            "5. **Save**. The credential is now selectable from any new or existing "
            "Connection. Secrets are never returned by the API once saved."
        ),
        see_also=("page.credentials", "howto.add_connection"),
    ),
    Topic(
        id="howto.run_pipeline",
        category=TopicCategory.HOWTO,
        title="How to run a pipeline",
        aliases=(
            "how do i run a pipeline", "how to run pipeline",
            "execute pipeline", "trigger pipeline", "start pipeline",
            "run it now", "run a workflow",
        ),
        body=(
            "Three ways:\n\n"
            "1. **Manually from the UI** — Pipelines page → click the row's Run "
            "button. You can pass override parameters in the dialog before running.\n"
            "2. **From a schedule** — Pipelines page → row → Schedule → set a cron "
            "expression or pick a preset. The schedule fires at the configured times.\n"
            "3. **From an API call** — `POST /api/execute/workflow/{id}` with an "
            "optional `{\"params\": {...}}` body. Useful for webhooks or external "
            "orchestrators."
        ),
        see_also=("howto.schedule_pipeline", "howto.trigger_from_api", "page.executions"),
    ),
    Topic(
        id="howto.schedule_pipeline",
        category=TopicCategory.HOWTO,
        title="How to schedule a pipeline",
        aliases=(
            "how do i schedule a pipeline", "how to schedule",
            "set up cron", "automate pipeline", "recurring run",
            "run automatically", "scheduled run",
        ),
        body=(
            "1. Pipelines page → click the pipeline row → **Schedule** tab (or the "
            "clock icon).\n"
            "2. Either type a cron expression (`0 9 * * 1-5`) or pick a friendly "
            "preset ('every hour', 'weekdays at 9am').\n"
            "3. Optionally pin parameter values for scheduled runs.\n"
            "4. Toggle **Active** to enable. Pause it any time without losing the "
            "configuration.\n"
            "Active schedules surface on the Dashboard. To get alerts when a schedule "
            "misses, configure the long-running / missed-schedule rules in Settings "
            "→ Notifications."
        ),
        see_also=("glossary.schedule", "feature.scheduling", "howto.set_up_notifications"),
    ),
    Topic(
        id="howto.see_failures",
        category=TopicCategory.HOWTO,
        title="How to see failures / diagnose errors",
        aliases=(
            "how do i see failures", "how to debug",
            "where are the errors", "see what failed",
            "diagnose a failure", "look at logs",
        ),
        body=(
            "1. **Fastest** — ask the Copilot 'recent failures' or 'what failed today' "
            "— the fast-lane returns a list of failed runs with per-row error "
            "messages in under a second.\n"
            "2. **Executions page** — filter by Status = Failed; click any row to see "
            "the step-by-step trace and full error log.\n"
            "3. **Dashboard** → Failed Pipelines panel shows the most recent N "
            "failures.\n"
            "4. For a single specific run, the URL is shareable — paste it anywhere."
        ),
        see_also=("page.executions", "howto.use_copilot", "howto.debug_pipeline"),
    ),
    Topic(
        id="howto.debug_pipeline",
        category=TopicCategory.HOWTO,
        title="How to debug a pipeline",
        aliases=(
            "how do i debug", "how to debug pipeline", "debug a workflow",
            "fix a pipeline", "what went wrong with my pipeline",
        ),
        body=(
            "1. **Read the error** — Executions page → failed run → scroll to the "
            "failing step. The error type + message + stack are right there.\n"
            "2. **Re-run with dry-run mode** — the Run dialog has a dry-run toggle; "
            "it executes the pipeline against a sample without writing any "
            "destination.\n"
            "3. **Validate before running** — in the Editor, the Validate button "
            "catches schema / wiring errors statically.\n"
            "4. **Ask the Copilot** — 'why did {pipeline} fail' invokes the "
            "diagnose_failure flow which inspects the recent error + relevant "
            "connection health automatically."
        ),
        see_also=("howto.see_failures", "page.executions"),
    ),
    Topic(
        id="howto.use_copilot",
        category=TopicCategory.HOWTO,
        title="How to use the Copilot",
        aliases=(
            "how do i use the copilot", "how to use copilot",
            "what can the copilot do", "copilot tips",
            "talk to the assistant", "ask the ai",
        ),
        body=(
            "**Open** — click the gradient Copilot button (bottom-right), or use the "
            "keyboard shortcut.\n\n"
            "**Ask anything** — try natural questions: 'recent failures', 'why did "
            "the latest run fail', 'show me running pipelines', 'create a pipeline "
            "that reads CSV from S3 and loads it into Postgres'.\n\n"
            "**Allow drafts** — tick the checkbox in the panel header to let the "
            "Copilot draft pipelines / alerts / reports (you still approve each draft "
            "before it's saved).\n\n"
            "**Suggestions tab** — page-aware chips show common prompts for the page "
            "you're on. Below the chat input, the four most-used chips appear "
            "whenever the input is empty."
        ),
        see_also=("glossary.copilot", "page.ai_hub"),
    ),
    Topic(
        id="howto.set_up_notifications",
        category=TopicCategory.HOWTO,
        title="How to set up notifications",
        aliases=(
            "how do i set up notifications", "how to set up alerts",
            "configure alerts", "get notified", "email when fails",
            "slack notifications", "webhook alerts",
        ),
        body=(
            "Sidebar → **Settings** → **Notifications** tab.\n\n"
            "Configure delivery channels: **Email** (SMTP host + from-address), "
            "**Slack** (incoming webhook), **Discord** (webhook), or a generic "
            "**Webhook** (any HTTPS URL).\n\n"
            "Then in the same tab, toggle which event types trigger alerts: pipeline "
            "failure, long-running run, missed schedule, threshold breach. Each "
            "event can target a different channel. Past alerts show in the "
            "Notifications page."
        ),
        see_also=("page.notifications", "feature.alerts"),
    ),
    Topic(
        id="howto.use_templates",
        category=TopicCategory.HOWTO,
        title="How to use templates",
        aliases=(
            "how do i use templates", "how to use templates",
            "start from template", "clone a template", "use a starter",
        ),
        body=(
            "1. Sidebar → **Templates** (under Workflows) OR Pipelines page → "
            "**+ New** → **From template**.\n"
            "2. Browse the gallery — each card shows the template name, what it "
            "does, and the connectors it uses.\n"
            "3. Click **Use this template** — it clones into a new pipeline in the "
            "Editor with all nodes pre-configured.\n"
            "4. Update connection / credential bindings in the right-hand panel, "
            "tweak any logic, then save and run.\n"
            "To save your OWN pipeline as a template: in the Editor, menu → Save as "
            "template."
        ),
        see_also=("page.templates", "howto.create_pipeline"),
    ),
    Topic(
        id="howto.trigger_from_api",
        category=TopicCategory.HOWTO,
        title="How to trigger a pipeline from the API",
        aliases=(
            "how do i trigger from api", "how to call api",
            "trigger from webhook", "run pipeline programmatically",
            "rest api", "external trigger",
        ),
        body=(
            "`POST /api/execute/workflow/{pipeline_id}` runs a pipeline. Pass "
            "parameters in the JSON body as `{\"params\": {\"key\": \"value\"}}` — "
            "they override the pipeline's default inputs. The response includes the "
            "new execution id, which you can poll at `GET /api/executions/{id}` for "
            "status. For published pipelines, the friendly URL "
            "`POST /api/published/{path}` accepts the same body. See the API tab in "
            "Help for the full surface and authentication options."
        ),
        see_also=("howto.run_pipeline", "page.help"),
    ),
)


# ─────────────────────────────────────────────────────────────────────
# FEATURES — cross-cutting capabilities
# ─────────────────────────────────────────────────────────────────────


_FEATURES: tuple[Topic, ...] = (
    Topic(
        id="feature.scheduling",
        category=TopicCategory.FEATURE,
        title="Scheduling",
        aliases=(
            "scheduling feature", "what is scheduling", "automated runs",
            "cron support", "recurring schedules",
        ),
        body=(
            "F-Pulse supports cron-based scheduling out of the box: every pipeline "
            "can have one or more schedules with cron expressions or friendly "
            "presets, parameter pinning, and active/paused state. The scheduler "
            "honours timezone settings and surfaces missed-schedule alerts via "
            "Notifications. Schedule-level alerts (different routing per schedule) "
            "are a Plus feature."
        ),
        see_also=("howto.schedule_pipeline", "glossary.schedule"),
    ),
    Topic(
        id="feature.alerts",
        category=TopicCategory.FEATURE,
        title="Alerts & Notifications",
        aliases=(
            "alerts feature", "what is alerting", "notification system",
            "alert rules",
        ),
        body=(
            "OSS supports pipeline-level alert rules — failure, long-running, "
            "missed-schedule, and threshold-breach events — with delivery to email, "
            "Slack, Discord, or generic webhook. Past deliveries surface in the "
            "Notifications page. Plus extends this with per-schedule routing, alert "
            "email domain allowlists, and richer routing logic."
        ),
        see_also=("howto.set_up_notifications", "page.notifications"),
    ),
    Topic(
        id="feature.copilot",
        category=TopicCategory.FEATURE,
        title="Copilot (AI assistant)",
        aliases=(
            "copilot feature", "what is the ai feature", "ai capabilities",
            "ai tools", "assistant capabilities",
        ),
        body=(
            "The Copilot is a page-aware, tool-using AI assistant with 23+ "
            "deterministic backend tools. Supports four providers (Ollama for local, "
            "Anthropic / OpenAI / OpenRouter for cloud) and a multi-lane router: "
            "fast-lane for instant ops questions, hybrid for reasoning + fresh data, "
            "single-shot for pure reasoning, full agent for multi-step tasks. "
            "Cross-session memory, RAG, and Llama-Guard moderation are Plus features."
        ),
        see_also=("glossary.copilot", "howto.use_copilot", "page.ai_hub"),
    ),
)


# ─────────────────────────────────────────────────────────────────────
# EDITIONS — OSS vs Plus
# ─────────────────────────────────────────────────────────────────────


_EDITIONS: tuple[Topic, ...] = (
    Topic(
        id="edition.oss",
        category=TopicCategory.EDITION,
        title="F-Pulse (OSS / free)",
        aliases=(
            "what is the oss version", "what is free version",
            "what's in oss", "what does free include",
            "open source features",
        ),
        body=(
            "**F-Pulse OSS** is the free, open-source, single-user, single-tenant "
            "build. Includes: visual pipeline editor, 40 node types, 33 "
            "connectors, scheduling, alerts (pipeline-level), templates, the "
            "Copilot with full 23-tool surface, basic eval harness, vertical "
            "scaling up to ~500 GB / single node, telemetry opt-in, and a Help "
            "center. Designed for the solo developer on their laptop. Multi-user, "
            "RBAC, audit, SSO, lineage, CDC, drift detection, and PROD environment "
            "are Plus."
        ),
        see_also=("edition.plus", "edition.comparison"),
    ),
    Topic(
        id="edition.plus",
        category=TopicCategory.EDITION,
        title="F-Pulse+ (paid)",
        aliases=(
            "what is plus", "what is paid version", "what's in plus",
            "what does plus add", "upgrade to plus", "plus features",
        ),
        body=(
            "**F-Pulse+** adds the team / production / governance layer on top of "
            "OSS: multi-user workspaces with RBAC, DEV→PROD promotion with "
            "two-person approval, audit log with retention, SSO (OIDC + SAML), "
            "Lineage view, drift detection, CDC + bulk-load connectors, Python "
            "Transform node, schedule-level alerts, alert email domain "
            "allowlists, vault integration, cross-session Copilot memory, "
            "Llama-Guard moderation, and operator dashboards. Pricing: $49/mo "
            "or $449/yr per install + Enterprise."
        ),
        tier=Tier.BOTH,  # describing Plus, visible to OSS users for upgrade context
        see_also=("edition.oss", "edition.comparison"),
    ),
    Topic(
        id="edition.comparison",
        category=TopicCategory.EDITION,
        title="OSS vs Plus comparison",
        aliases=(
            "oss vs plus", "free vs paid", "compare editions",
            "what's the difference", "should i upgrade",
            "what do i get with plus",
        ),
        body=(
            "**OSS is the engine; Plus is the operating layer.** OSS gives you "
            "everything to build, run, and schedule pipelines as a solo developer. "
            "Plus adds the things teams and production environments need: shared "
            "workspaces with RBAC, a PROD environment with approval workflows, "
            "audit logging, SSO, lineage tracking, drift detection, enterprise "
            "data-engineering features (CDC, bulk loaders), and "
            "operator-grade Copilot features (cross-session memory, RAG, "
            "Llama-Guard). All connectors are open in both editions — Plus does "
            "NOT gate connector access."
        ),
        tier=Tier.BOTH,
        see_also=("edition.oss", "edition.plus"),
    ),
)


# Exported tuple consumed by atlas.schema._load_atlas
HANDWRITTEN_TOPICS: tuple[Topic, ...] = (
    *_PAGES,
    *_GLOSSARY,
    *_HOWTOS,
    *_FEATURES,
    *_EDITIONS,
)
