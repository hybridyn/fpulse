"""Prepare, seed, execute, verify and import five real connector demos."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import secrets
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO / "backend"))
RUNTIME = ROOT / "runtime"
ROWS = [
    {"id": 1, "region": "North", "quantity": 2, "unit_price": 100, "status": "approved"},
    {"id": 2, "region": "South", "quantity": 3, "unit_price": 50, "status": "approved"},
    {"id": 3, "region": "North", "quantity": 1, "unit_price": 75, "status": "approved"},
    {"id": 3, "region": "North", "quantity": 1, "unit_price": 75, "status": "approved"},
    {"id": 4, "region": "South", "quantity": 5, "unit_price": 200, "status": "cancelled"},
    {"id": 5, "region": "North", "quantity": -1, "unit_price": 10, "status": "approved"},
]
EXPECTED = [{"region": "North", "total_amount": 275}, {"region": "South", "total_amount": 150}]
SYSTEMS = ["sqlserver", "api", "postgres", "mysql", "s3"]
TARGETS = ["postgres", "sqlserver", "mysql", "s3", "api"]
NAMES = ["Sales summary", "API order report", "Subscription totals", "Inventory export", "Shipment API delivery"]


def password():
    env_file = ROOT / ".env"
    if not env_file.exists():
        raise SystemExit("Run demo.py prepare first.")
    return env_file.read_text().strip().split("=", 1)[1]


def configs():
    pw = password()
    result = {
        "sqlserver": ("mssql", {"host": "127.0.0.1", "port": 21433, "database": "fpulse_demo", "user": "sa", "password": pw, "odbc_driver": "ODBC Driver 18 for SQL Server"}),
        "postgres": ("postgresql", {"host": "127.0.0.1", "port": 25432, "database": "fpulse_demo", "user": "demo", "password": pw}),
        "mysql": ("mysql", {"host": "127.0.0.1", "port": 23306, "database": "fpulse_demo", "user": "demo", "password": pw}),
        "api": ("rest_api", {"base_url": "http://127.0.0.1:28080", "auth_type": "none"}),
        "s3": ("s3", {"endpoint": "http://127.0.0.1:29000", "access_key": "fpulse_demo", "secret_key": pw, "region": "us-east-1", "bucket": "fpulse-demo"}),
    }
    override = RUNTIME / "sqlserver-config.json"
    if override.exists():
        result["sqlserver"] = ("mssql", json.loads(override.read_text()))
    return result


def prepare():
    RUNTIME.mkdir(exist_ok=True)
    if not (ROOT / ".env").exists():
        (ROOT / ".env").write_text("DEMO_PASSWORD=Demo9!" + secrets.token_hex(16) + "\n")
    (ROOT / "fixtures.json").write_text(json.dumps(ROWS, indent=2) + "\n")
    (ROOT / "pipelines").mkdir(exist_ok=True)
    for index, (source, target, name) in enumerate(zip(SYSTEMS, TARGETS, NAMES), 19):
        src = {"connector_type": "database", "connection_id": f"Demo {source}", "source_mode": "query", "query": "SELECT id, region, quantity, unit_price, status FROM fpulse_demo_input"}
        if source == "api":
            src = {"connector_type": "rest_api", "connection_id": "Demo api", "path": "/orders", "pagination": "none", "max_retries": 0}
        elif source == "s3":
            src = {"connector_type": "s3", "connection_id": "Demo s3", "bucket": "fpulse-demo", "key": "input/shipments.json", "format": "json"}
        dest = {"connector_type": "warehouse", "connection_id": f"Demo {target}", "table": f"fpulse_demo_{index}_summary", "schema": "dbo" if target == "sqlserver" else ("fpulse_demo" if target == "mysql" else "public"), "mode": "create"}
        if target == "s3":
            dest = {"connector_type": "s3", "connection_id": "Demo s3", "bucket": "fpulse-demo", "key": "output/inventory-summary.parquet", "format": "parquet"}
        elif target == "api":
            dest = {"connector_type": "rest_api", "connection_id": "Demo api", "path": "/results/shipments", "method": "POST", "batch_mode": "bulk", "max_retries": 0}
        nodes = [
            ("source", f"Read {source}", src),
            ("filter", "Approved positive quantities", {"condition": "status = 'approved' AND quantity > 0 AND unit_price >= 0"}),
            ("transform", "Remove exact duplicate records", {"expression": "SELECT DISTINCT * FROM source_table"}),
            ("transform", "Calculate line amount", {"expression": "SELECT region, quantity * unit_price AS amount FROM source_table"}),
            ("aggregate", "Total by region", {"group_by": ["region"], "functions": [{"column": "amount", "function": "SUM", "alias": "total_amount"}]}),
            ("sort", "Largest region first", {"sort_by": ["total_amount DESC", "region ASC"]}),
            ("destination", f"Write {target}", dest),
        ]
        wf = {
            "name": f"{index} - {name}: {source} to {target}",
            "description": "Seven-node cross-system demo with synthetic data. Six input rows become two region totals: North 275, South 150. Requires the multi-system-demo setup. Reruns replace only dedicated demo outputs.",
            "steps": [{"id": f"n{i}", "type": kind, "label": label, "params": params, "position": {"x": 100 + 280 * i, "y": 120}} for i, (kind, label, params) in enumerate(nodes)],
            "connections": [{"from_step": f"n{i}", "to_step": f"n{i+1}"} for i in range(6)],
            "metadata": {"sample": True, "required_connections": [f"Demo {source}", f"Demo {target}"]},
        }
        (ROOT / "pipelines" / f"{index}-{source}-to-{target}.json").write_text(json.dumps(wf, indent=2) + "\n")
    print("Prepared five seven-node pipelines and local demo credentials.")


def connect(system, database=True):
    pw = password()
    if system == "sqlserver":
        import pyodbc
        cfg = configs()["sqlserver"][1]
        def quoted(value):
            return "{" + str(value).replace("}", "}}") + "}"
        return pyodbc.connect(f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={cfg['host']},{cfg.get('port', 1433)};DATABASE={quoted(cfg['database'] if database else 'master')};UID={quoted(cfg.get('user') or cfg.get('username'))};PWD={quoted(cfg['password'])};TrustServerCertificate=yes;", autocommit=True, timeout=5)
    if system == "postgres":
        import psycopg2
        return psycopg2.connect(host="127.0.0.1", port=25432, user="demo", password=pw, dbname="fpulse_demo", connect_timeout=5)
    import pymysql
    return pymysql.connect(host="127.0.0.1", port=23306, user="demo", password=pw, database="fpulse_demo", connect_timeout=5)


def s3_client():
    import boto3
    return boto3.client("s3", endpoint_url="http://127.0.0.1:29000", aws_access_key_id="fpulse_demo", aws_secret_access_key=password(), region_name="us-east-1")


def seed(skip_sqlserver=False, existing_sqlserver_database=False):
    database = configs()["sqlserver"][1]["database"]
    if not existing_sqlserver_database and (not database.startswith("fpulse_demo") or not database.replace("_", "").isalnum()):
        raise ValueError("SQL Server seeding requires a dedicated fpulse_demo database.")
    if not skip_sqlserver and not existing_sqlserver_database:
        conn = connect("sqlserver", database=False)
        try:
            cur = conn.cursor()
            cur.execute("SELECT DB_ID(?)", database)
            if cur.fetchone()[0] is None:
                cur.execute(f"CREATE DATABASE [{database}]")
        finally:
            conn.close()
    for system in ("sqlserver", "postgres", "mysql"):
        if skip_sqlserver and system == "sqlserver":
            continue
        conn = connect(system)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'fpulse_demo_input'")
            exists = cur.fetchone()[0] > 0
            if exists:
                cur.execute("SELECT id, region, quantity, unit_price, status FROM fpulse_demo_input ORDER BY id")
                assert [tuple(row) for row in cur.fetchall()] == [tuple(row.values()) for row in ROWS], "Existing demo fixture differs; refusing to overwrite it."
                print(f"Already seeded {system}")
                continue
            cur.execute("CREATE TABLE fpulse_demo_input (id INTEGER, region VARCHAR(30), quantity INTEGER, unit_price INTEGER, status VARCHAR(20))")
            marker = "?" if system == "sqlserver" else "%s"
            cur.executemany("INSERT INTO fpulse_demo_input VALUES (" + ",".join([marker] * 5) + ")", [tuple(row.values()) for row in ROWS])
            conn.commit()
        finally:
            conn.close()
        print(f"Seeded {system}: {len(ROWS)} rows")
    client = s3_client()
    buckets = {item["Name"] for item in client.list_buckets()["Buckets"]}
    if "fpulse-demo" not in buckets:
        client.create_bucket(Bucket="fpulse-demo")
    client.put_object(Bucket="fpulse-demo", Key="input/shipments.json", Body=json.dumps(ROWS).encode(), ContentType="application/json")
    with urllib.request.urlopen("http://127.0.0.1:28080/health", timeout=5) as response:
        assert json.load(response)["service"] == "fpulse-multi-system-demo"
    print("Seeded S3 and checked the sample API.")


def workflows():
    return [json.loads(p.read_text()) for p in sorted((ROOT / "pipelines").glob("*.json"))]


def export_ui():
    """Write the version-2 envelope required by Pipelines > Import."""
    from datetime import datetime, timezone
    from fpulse.api.workflows import build_pipeline_export_payload
    from fpulse.ir.schema import Workflow

    destination = ROOT / "exports"
    destination.mkdir(exist_ok=True)
    for number, raw in enumerate(workflows(), 19):
        raw["id"] = f"multidemo_{number}"
        for step in raw["steps"]:
            reference = step["params"].get("connection_id")
            if reference:
                step["params"]["connection_id"] = reference.replace("Demo ", "multidemo_")
        workflow = Workflow.model_validate(raw)
        envelope = {
            "fpulse_version": "1.0.0",
            "format_version": 2,
            "export_type": "pipeline",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "pipeline": build_pipeline_export_payload(workflow, "default"),
        }
        name = f"{number}-{SYSTEMS[number - 19]}-to-{TARGETS[number - 19]}.fpulse"
        (destination / name).write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
        print(f"Exported: {name}")


def verify_destination(index, target):
    if target in ("sqlserver", "postgres", "mysql"):
        conn = connect(target)
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT region, total_amount FROM fpulse_demo_{index}_summary ORDER BY total_amount DESC")
            rows = [{"region": row[0], "total_amount": float(row[1])} for row in cur.fetchall()]
        finally:
            conn.close()
    elif target == "s3":
        import pyarrow.parquet as pq
        obj = s3_client().get_object(Bucket="fpulse-demo", Key="output/inventory-summary.parquet")
        rows = pq.read_table(io.BytesIO(obj["Body"].read())).to_pylist()
    else:
        with urllib.request.urlopen("http://127.0.0.1:28080/results/shipments", timeout=5) as response:
            rows = json.load(response)
    assert rows == EXPECTED, f"Destination {target}: {rows!r} != {EXPECTED!r}"
    return rows


def run(skip_sqlserver=False):
    # Scope the private-network opt-in to this dedicated local demo process.
    os.environ["FPULSE_API_SOURCE_ALLOW_PRIVATE"] = "1"
    os.environ["FPULSE_DATA_DIR"] = str(RUNTIME)
    from fpulse.connections.models import Connection
    from fpulse.connections.store import ConnectionStore
    from fpulse.engine.executor import WorkflowExecutor
    from fpulse.ir.schema import Workflow
    from fpulse.main import app_state
    from fpulse.storage.database import Database
    from fpulse.datastore.store import DataStore

    db = Database(str(RUNTIME / "verification.db"))
    store = ConnectionStore(db)
    for name, (kind, config) in configs().items():
        store.create(Connection(id=f"demo_{name}", name=f"Demo {name}", type=kind, config=config))
    app_state.update(connection_store=store, datastore=DataStore(db), data_dir=str(RUNTIME))
    executor = WorkflowExecutor(data_dir=str(RUNTIME), app_state=app_state)
    report = []
    try:
        for index, (raw, target) in enumerate(zip(workflows(), TARGETS), 19):
            if skip_sqlserver and index in (19, 20):
                continue
            for step in raw["steps"]:
                if "connection_id" in step["params"]:
                    step["params"]["connection_id"] = step["params"]["connection_id"].replace("Demo ", "demo_")
            wf = Workflow.model_validate(raw)
            assert len(wf.steps) >= 5
            # Two real executions prove the replace semantics do not duplicate rows.
            for repeat in range(2):
                result = executor.execute_workflow(wf, full_run=True)
                if result.status != "success":
                    raise RuntimeError(json.dumps({sid: step.error for sid, step in result.step_results.items() if step.error}, indent=2))
                counts = [result.step_results[f"n{i}"].row_count for i in range(7)]
                assert counts == [6, 4, 3, 3, 2, 2, 2], counts
                rows = verify_destination(index, target)
                report.append({"pipeline": wf.name, "run": repeat + 1, "status": result.status, "step_rows": counts, "destination_rows": rows})
                print(f"PASS run {repeat + 1}: {wf.name}", flush=True)
        (RUNTIME / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        db.close()


def import_live(base_url, workspace):
    token = os.environ.get("FPULSE_TOKEN")
    if not token:
        raise SystemExit("Set FPULSE_TOKEN to your F-Pulse session token before importing.")
    def api(method, path, body=None):
        req = urllib.request.Request(base_url.rstrip("/") + path, method=method, data=json.dumps(body).encode() if body is not None else None, headers={"Authorization": f"Bearer {token}", "X-Workspace-Id": workspace, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    existing = api("GET", "/api/connections/")
    if isinstance(existing, dict):
        existing = existing.get("connections", [])
    ids = {}
    for name, (kind, config) in configs().items():
        match = next((c for c in existing if c["name"] == f"Demo {name}" and c["type"] == kind), None)
        if match is None:
            match = api("POST", "/api/connections/", {"name": f"Demo {name}", "type": kind, "config": config, "environment": "dev"})
        ids[f"Demo {name}"] = match["id"]
    for wf in workflows():
        for step in wf["steps"]:
            if "connection_id" in step["params"]:
                step["params"]["connection_id"] = ids[step["params"]["connection_id"]]
        created = api("POST", "/api/workflows/", wf)
        print(f"Imported: {wf['name']} ({created['id']})")


def import_local(db_path, workspace):
    """Install sample assets through the same stores used by the local app."""
    from fpulse.connections.models import Connection
    from fpulse.connections.store import ConnectionStore
    from fpulse.ir.schema import Workflow
    from fpulse.ir.versioning import WorkflowStore
    from fpulse.storage.database import Database
    if not Path(db_path).is_file():
        raise ValueError("Pass the existing F-Pulse database path.")
    db = Database(db_path)
    try:
        connection_store = ConnectionStore(db)
        workflow_store = WorkflowStore(db)
        ids = {}
        for name, (kind, config) in configs().items():
            cid = f"multidemo_{name}"
            existing = connection_store.get(cid)
            if existing and (existing.workspace_id != workspace or existing.config != config):
                raise ValueError(f"Connection ID conflict: {cid}")
            if not existing:
                connection_store.create(Connection(id=cid, name=f"Demo {name}", type=kind, config=config, workspace_id=workspace, environment="dev"))
            ids[f"Demo {name}"] = cid
        for number, raw in enumerate(workflows(), 19):
            raw.update(id=f"multidemo_{number}", workspace_id=workspace)
            for step in raw["steps"]:
                if "connection_id" in step["params"]:
                    step["params"]["connection_id"] = ids[step["params"]["connection_id"]]
            existing = workflow_store.get(raw["id"], workspace_id=workspace)
            if existing:
                if existing.workflow.name != raw["name"]:
                    raise ValueError(f"Pipeline ID conflict: {raw['id']}")
                print(f"Already imported: {raw['name']}")
                continue
            workflow_store.save(Workflow.model_validate(raw), change_summary="Added verified seven-node multi-system demo")
            print(f"Imported: {raw['name']}")
    finally:
        db.close()


def setup_connections(db_path, workspace):
    """Create missing demo connections, expose them in DEV, and test them."""
    from datetime import datetime, timezone
    from fpulse.connections.models import Connection
    from fpulse.connections.store import ConnectionStore
    from fpulse.connections.tester import ConnectionTester
    from fpulse.storage.database import Database

    if not db_path or not Path(db_path).is_file():
        raise ValueError("Pass the existing F-Pulse database with --local-db.")
    db = Database(db_path)
    failures = []
    try:
        store = ConnectionStore(db)
        tester = ConnectionTester()
        for name, (kind, config) in configs().items():
            cid = f"multidemo_{name}"
            connection = store.get(cid)
            if connection and (connection.workspace_id != workspace or connection.config != config):
                raise ValueError(f"Connection ID/configuration conflict: {cid}")
            if not connection:
                connection = store.create(Connection(id=cid, name=f"Demo {name}", type=kind, config=config, workspace_id=workspace, environment="dev"))
            result = tester.test_connection(connection.type, dict(connection.config))
            ok = bool(result.get("success"))
            store.update(cid, {
                "environment": "dev",
                "project_id": None,
                "capabilities": ["read", "write"],
                "description": "Sample-data source and destination for seven-node demo pipelines 19-23.",
                "tags": ["demo", "multi-system"],
                "last_test_at": datetime.now(timezone.utc),
                "last_test_ok": ok,
                "last_test_error": "" if ok else result.get("message", "Connection test failed"),
            }, workspace_id=workspace)
            print(f"{'PASS' if ok else 'FAIL'}: {connection.name}: {result.get('message', '')}", flush=True)
            if not ok:
                failures.append(connection.name)
        if failures:
            raise RuntimeError("Connection tests failed: " + ", ".join(failures))
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "seed", "run", "import", "import-local", "export", "connections"])
    parser.add_argument("--base-url", default="http://127.0.0.1:8002")
    parser.add_argument("--workspace", default="default")
    parser.add_argument("--skip-sqlserver", action="store_true")
    parser.add_argument("--existing-sqlserver-database", action="store_true", help="Explicitly seed only dedicated demo tables in an existing SQL Server database.")
    parser.add_argument("--local-db", help="Existing local F-Pulse SQLite database, for import-local.")
    args = parser.parse_args()
    {"prepare": prepare, "seed": lambda: seed(args.skip_sqlserver, args.existing_sqlserver_database), "run": lambda: run(args.skip_sqlserver), "import": lambda: import_live(args.base_url, args.workspace), "import-local": lambda: import_local(args.local_db, args.workspace), "export": export_ui, "connections": lambda: setup_connections(args.local_db, args.workspace)}[args.command]()
