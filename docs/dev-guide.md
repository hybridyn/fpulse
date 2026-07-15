# F-Pulse v1.0.0 — Developer Guide

## Quick Start

```powershell
# Backend
cd backend
pip install -r requirements.txt
python -m fpulse serve            # or: fpulse serve  (after `pip install -e .`)
# → http://localhost:8001 (API)
# → http://localhost:8001/docs (Swagger)

# Frontend
cd frontend
npm install
npm run dev
# → http://localhost:5173
```

## Architecture

```
F-Pulse v1.0.0
├── Backend (Python 3.11 + FastAPI)
│   ├── 13 API Routers (70+ endpoints)
│   ├── 11 In-Memory Stores
│   ├── DuckDB Execution Engine
│   ├── Background Scheduler (threading)
│   ├── Notification Service (Email/Slack/Teams/Webhook)
│   ├── Schema Contract System
│   └── IR Validator
├── Frontend (React 18 + Vite + Tailwind)
│   ├── 13 Pages
│   ├── React Flow Canvas
│   ├── Zustand State Management
│   ├── AI Chat (client-side NLP + backend)
│   └── Global Search (Cmd+K)
└── Tests (pytest)
    ├── 170+ Unit Tests
    ├── 50+ API Integration Tests
    ├── 20+ Load/Stress Tests
    └── 25+ E2E Tests
```

## Backend Structure

```
backend/fpulse/
├── main.py              # App entry, state, routes, startup/shutdown
├── api/                 # FastAPI routers (13 files)
├── ir/                  # Intermediate Representation
│   ├── schema.py        # Workflow, Step, StepType models
│   ├── versioning.py    # WorkflowStore (versioned)
│   ├── validator.py     # Structural validation
│   └── lifecycle.py     # Lifecycle event tracking
├── engine/
│   ├── executor.py      # WorkflowExecutor (DuckDB)
│   └── preview.py       # Data preview helpers
├── nodes/               # Computation nodes
│   ├── base.py          # BaseNode + ExecutionContext
│   ├── registry.py      # Node type registry
│   ├── csv_source.py    # CSV reader
│   ├── filter_node.py   # SQL WHERE filter
│   ├── aggregate.py     # GROUP BY aggregation
│   ├── join.py          # JOIN operation
│   ├── deduplicate.py   # Deduplication
│   ├── output.py        # File writer
│   └── ...
├── projects/            # Project CRUD
├── scheduling/          # Pipeline scheduling
├── alerts/              # Alert rules + notification
├── monitoring/          # Execution history
├── auth/                # User auth + sessions
├── variables/           # Global/project variables
├── credentials/         # Credential storage
├── connections/         # Database/API connections
├── intelligence/        # Schema detection, contracts, suggestions
└── planner/             # AI/rule-based pipeline generation
```

## Key Patterns

### App State
All stores live in `app_state` dict in `main.py`. Routers access them via
`from fpulse.main import app_state`.

```python
app_state["store"]            # WorkflowStore
app_state["project_store"]    # ProjectStore
app_state["schedule_store"]   # ScheduleStore
app_state["alert_store"]      # AlertStore
app_state["execution_store"]  # ExecutionStore
app_state["user_store"]       # UserStore
app_state["variable_store"]   # VariableStore
app_state["credential_store"] # CredentialStore
app_state["connection_store"] # ConnectionStore
app_state["lifecycle_store"]  # LifecycleStore
app_state["contract_store"]   # SchemaContractStore
app_state["scheduler"]        # PipelineScheduler
app_state["notifier"]         # NotificationService
app_state["data_dir"]         # Data file directory
```

### Store Pattern
All stores follow the same pattern:
- In-memory dict/list storage
- Deep copy on writes (mutation isolation)
- `updated_at` timestamps on mutations
- Filtering methods (`list_by_workflow`, `list_by_project`)
- `model_dump(mode="json")` for serialization

### Execution Flow
1. Validate workflow (structural)
2. Open DuckDB in-memory connection
3. Topological sort steps (Kahn's algorithm)
4. Execute each step via its registered node class
5. Collect results with preview data
6. Close connection

### Node Registration
```python
from fpulse.nodes.base import BaseNode
from fpulse.nodes.registry import register
from fpulse.ir.schema import StepType

@register(StepType.CSV_SOURCE)
class CsvSourceNode(BaseNode):
    display_name = "CSV Source"
    category = "source"

    def execute(self, ctx):
        path = os.path.join(ctx.data_dir, self.params["file_path"])
        return ctx.conn.read_csv(path)
```

## Frontend Structure

```
frontend/src/
├── main.tsx               # Entry point
├── App.tsx                # Routing, layout, keyboard shortcuts
├── api/client.ts          # Centralized API client (70+ endpoints)
├── stores/
│   └── workflowStore.ts   # Zustand store (nodes, edges, undo/redo)
├── ai/
│   └── pipelineBuilder.ts # Client-side NLP pipeline builder
├── components/
│   ├── Canvas.tsx          # React Flow editor
│   ├── Toolbar.tsx         # Workflow name, import/export
│   ├── ConfigPanel.tsx     # Node parameter editor
│   ├── ModulesPanel.tsx    # Node palette (drag-drop)
│   ├── ChatPanel.tsx       # AI chat interface
│   ├── PreviewPanel.tsx    # Step result preview (Table/Schema/JSON)
│   ├── Sidebar.tsx         # Navigation
│   ├── GlobalSearch.tsx    # Cmd+K search
│   ├── Toast.tsx           # Notification system
│   ├── ErrorBoundary.tsx   # React error boundary
│   ├── nodes/
│   │   ├── FPulseNode.tsx  # Canvas node component
│   │   ├── CustomEdge.tsx  # Edge renderer
│   │   └── StickyNote.tsx  # Annotation node
│   └── pages/
│       ├── DashboardPage.tsx
│       ├── ProjectsPage.tsx
│       ├── PipelinesPage.tsx
│       ├── ExecutionsPage.tsx
│       ├── SchedulesPage.tsx
│       ├── AlertsPage.tsx
│       ├── CredentialsPage.tsx
│       ├── VariablesPage.tsx
│       ├── ConnectionsPage.tsx
│       ├── SettingsPage.tsx
│       ├── IntelligencePage.tsx
│       ├── MonitorPage.tsx
│       ├── LoginPage.tsx
│       └── HelpPage.tsx
└── styles/globals.css
```

## Running Tests

```powershell
cd <repo-root>\backend

# Install test dependencies
pip install pytest httpx

# Run all tests
python run_tests.py

# Run specific suites
python run_tests.py --unit
python run_tests.py --api
python run_tests.py --load
python run_tests.py --e2e

# Generate test report
python run_tests.py --report
```

## Pipeline Status Lifecycle

```
DRAFT → TESTING → PUBLISHED
  ↑        ↓         ↓
  ↑      FAILED    ARCHIVED
  ↑                   ↓
  └── RESTORED ← ─ ─ ┘
```

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| Cmd/Ctrl+K | Global search |
| Cmd/Ctrl+S | Save workflow |
| Cmd/Ctrl+Z | Undo |
| Cmd/Ctrl+Y | Redo |
| Cmd/Ctrl+A | Select all nodes |
| Cmd/Ctrl+C | Copy selected |
| Cmd/Ctrl+V | Paste |
| Delete | Delete selected node |
| Escape | Deselect / close panel |
| Arrow Left/Right | Navigate nodes |

## F-Pulse OSS vs F-Pulse+

F-Pulse OSS Free shipped at v1.0.0. The paid tier (F-Pulse+) adds team-production capabilities — multi-user RBAC, two-gate approvals, audit log retention, sandbox runs, drift detection, OIDC/SAML, multi-worker scaling, enterprise connectors.

See `edition-matrix.md` at the repository root for the canonical Free vs Plus capability matrix, and the [Editions guide](editions.md) for the user-facing summary.
