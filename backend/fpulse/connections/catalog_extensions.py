"""Real catalog providers for everything not covered in catalog.py.

Promoted from the planned-placeholder bucket. Imported at the bottom
of catalog.py so the @register decorators populate the same registry.

Per-provider notes:
- Tier 1 (no new deps): uses requests / boto3 / stdlib that are
  already installed.
- Tier 2 (lightweight SDK): lazy-imports the SDK; returns
  _unsupported with a clear `pip install` instruction on ImportError.
- Tier 3 (heavy/vendor SDK): same pattern but the install hint points
  to an extras group like fpulse[snowflake] (snowflake/databricks/
  bigquery already shipped in catalog.py).
- Tier 4 (permanent no-catalog): explicit reason documenting WHY
  there's no canonical catalog API.

Cost contracts (`billed_metadata=False`, `requires_compute=False`)
are encoded on every warehouse / SaaS provider that has a free
metadata path, so anyone tempted to switch to a billed equivalent
breaks tests instead of users' bills.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

from fpulse.connections.catalog import (
    Catalog,
    CatalogItem,
    DEFAULT_TIMEOUT,
    ProviderMeta,
    _build,
    _resolve_user,
    _unsupported,
    register,
)
from fpulse.connections.runtime import resolve_verify_ssl

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
#  Shared helpers
# ════════════════════════════════════════════════════════════════════

def _rget(url: str, *, headers: dict | None = None, params: dict | None = None,
          auth: tuple[str, str] | None = None, timeout: int = DEFAULT_TIMEOUT,
          verify: bool | None = None):
    """HTTP GET wrapper. `verify=None` reads from the FPULSE_VERIFY_SSL
    env var; passing False explicitly disables verification (only do
    that for self-signed internal services)."""
    import requests  # type: ignore
    if verify is None:
        verify = resolve_verify_ssl(None)
    return requests.get(url, headers=headers or {}, params=params or {},
                         auth=auth, timeout=timeout, verify=verify)


def _rpost(url: str, *, headers: dict | None = None, json: Any = None,
           data: Any = None, auth: tuple[str, str] | None = None,
           timeout: int = DEFAULT_TIMEOUT, verify: bool | None = None):
    """HTTP POST wrapper. See `_rget` for the verify semantics."""
    import requests  # type: ignore
    if verify is None:
        verify = resolve_verify_ssl(None)
    return requests.post(url, headers=headers or {}, json=json, data=data,
                          auth=auth, timeout=timeout, verify=verify)


def _bearer(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _basic_auth(config: dict) -> tuple[str, str] | None:
    u = config.get("user") or config.get("username")
    p = config.get("password")
    return (u, p) if (u and p) else None


def _strip_url(s: str) -> str:
    s = (s or "").strip()
    if s and not s.startswith(("http://", "https://")):
        s = "https://" + s
    return s.rstrip("/")


def _aws_kwargs(config: dict, region_default: str = "us-east-1") -> dict:
    """Build kwargs for boto3.client across all AWS-family providers."""
    from botocore.config import Config as BotoConfig  # type: ignore
    kw: dict[str, Any] = {
        "region_name": config.get("region") or config.get("region_name") or region_default,
        "config": BotoConfig(connect_timeout=DEFAULT_TIMEOUT,
                              read_timeout=DEFAULT_TIMEOUT,
                              retries={"max_attempts": 1}),
    }
    if config.get("access_key_id") or config.get("aws_access_key_id"):
        kw["aws_access_key_id"] = config.get("access_key_id") or config.get("aws_access_key_id")
        kw["aws_secret_access_key"] = config.get("secret_access_key") or config.get("aws_secret_access_key")
        if config.get("session_token") or config.get("aws_session_token"):
            kw["aws_session_token"] = config.get("session_token") or config.get("aws_session_token")
    if config.get("endpoint_url"):
        kw["endpoint_url"] = config["endpoint_url"]
    return kw


# ════════════════════════════════════════════════════════════════════
#                  AWS family — boto3 (already installed)
# ════════════════════════════════════════════════════════════════════

@register("dynamodb", ProviderMeta(category="nosql", auth="iam", tier="tier1"))
def _dynamodb_catalog(config: dict) -> Catalog:
    try:
        import boto3  # type: ignore
    except ImportError:
        return _unsupported("boto3 not installed")
    client = boto3.client("dynamodb", **_aws_kwargs(config))
    items: list[CatalogItem] = []
    paginator = client.get_paginator("list_tables")
    for page in paginator.paginate(PaginationConfig={"MaxItems": 1000}):
        for name in page.get("TableNames", []):
            items.append(CatalogItem(name=name, kind="table", parent=""))
    return _build(items)


@register("kinesis", ProviderMeta(category="streaming", auth="iam", tier="tier1"))
def _kinesis_catalog(config: dict) -> Catalog:
    try:
        import boto3  # type: ignore
    except ImportError:
        return _unsupported("boto3 not installed")
    client = boto3.client("kinesis", **_aws_kwargs(config))
    items: list[CatalogItem] = []
    paginator = client.get_paginator("list_streams")
    for page in paginator.paginate(PaginationConfig={"MaxItems": 1000}):
        for name in page.get("StreamNames", []):
            items.append(CatalogItem(name=name, kind="stream", parent=""))
    return _build(items)


@register("sqs", ProviderMeta(category="streaming", auth="iam", tier="tier1"))
def _sqs_catalog(config: dict) -> Catalog:
    try:
        import boto3  # type: ignore
    except ImportError:
        return _unsupported("boto3 not installed")
    client = boto3.client("sqs", **_aws_kwargs(config))
    items: list[CatalogItem] = []
    resp = client.list_queues(MaxResults=1000)
    for url in resp.get("QueueUrls", []) or []:
        # Queue name is the last URL segment.
        name = url.rstrip("/").rsplit("/", 1)[-1]
        items.append(CatalogItem(name=name, kind="queue", parent="",
                                  metadata={"url": url}))
    return _build(items)


@register("athena", ProviderMeta(
    category="warehouse", auth="iam", tier="tier1",
    billed_metadata=False, requires_compute=False,
    notes="reads Glue Data Catalog (free) — never starts an Athena query"))
def _athena_catalog(config: dict) -> Catalog:
    try:
        import boto3  # type: ignore
    except ImportError:
        return _unsupported("boto3 not installed")
    glue = boto3.client("glue", **_aws_kwargs(config))
    items: list[CatalogItem] = []
    db_paginator = glue.get_paginator("get_databases")
    for db_page in db_paginator.paginate(PaginationConfig={"MaxItems": 200}):
        for db in db_page.get("DatabaseList", []) or []:
            db_name = db.get("Name")
            if not db_name:
                continue
            t_paginator = glue.get_paginator("get_tables")
            try:
                for t_page in t_paginator.paginate(DatabaseName=db_name,
                                                    PaginationConfig={"MaxItems": 1000}):
                    for tbl in t_page.get("TableList", []) or []:
                        kind = "view" if (tbl.get("TableType") or "").upper() == "VIRTUAL_VIEW" else "table"
                        items.append(CatalogItem(name=tbl["Name"], kind=kind,
                                                  parent=db_name))
            except Exception as exc:  # noqa: BLE001
                logger.debug("Glue get_tables failed for %s: %s", db_name, exc)
    return _build(items)


# ════════════════════════════════════════════════════════════════════
#                  Trino / Presto — REST + SQL
# ════════════════════════════════════════════════════════════════════

def _trino_like_catalog(config: dict, default_port: int) -> Catalog:
    """Use the Trino/Presto Python clients when present, fall back to
    REST `/v1/statement` + cursor — but the cursor path is paginated
    state-machine that's awkward to do in 30 lines, so we lazy-import
    the official client. Both clients are tiny pure-python wheels."""
    host = config.get("host") or "localhost"
    port = int(config.get("port", default_port))
    user = _resolve_user(config, "anonymous")
    catalog = config.get("catalog")
    schema = config.get("schema")
    is_presto = port == 8080 and config.get("flavor", "").lower() == "presto"
    try:
        if is_presto:
            from prestodb import dbapi  # type: ignore
            conn = dbapi.connect(host=host, port=port, user=user,
                                  catalog=catalog, schema=schema)
        else:
            from trino import dbapi  # type: ignore
            conn = dbapi.connect(host=host, port=port, user=user,
                                  catalog=catalog, schema=schema)
    except ImportError:
        return _unsupported("trino client not installed (pip install trino)")
    try:
        cur = conn.cursor()
        items: list[CatalogItem] = []
        cur.execute("SHOW CATALOGS")
        catalogs = [r[0] for r in cur.fetchall()]
        for cat in catalogs:
            if cat in ("system", "jmx"):
                continue
            try:
                cur.execute(f"SHOW SCHEMAS FROM {cat}")
                schemas = [r[0] for r in cur.fetchall()]
            except Exception:  # noqa: BLE001
                continue
            for sch in schemas:
                if sch == "information_schema":
                    continue
                try:
                    cur.execute(f"SHOW TABLES FROM {cat}.{sch}")
                    for row in cur.fetchall():
                        items.append(CatalogItem(name=row[0], kind="table",
                                                  parent=f"{cat}.{sch}"))
                except Exception:  # noqa: BLE001
                    continue
        return _build(items)
    finally:
        try: conn.close()
        except Exception: pass


@register("trino", ProviderMeta(category="warehouse", auth="basic", tier="tier1"))
def _trino_cat(config): return _trino_like_catalog(config, 8080)


@register("presto", ProviderMeta(category="warehouse", auth="basic", tier="tier1"))
def _presto_cat(config):
    cfg = {**config, "flavor": "presto"}
    return _trino_like_catalog(cfg, 8080)


# ════════════════════════════════════════════════════════════════════
#                       Streaming — REST mgmt
# ════════════════════════════════════════════════════════════════════

@register("rabbitmq", ProviderMeta(category="streaming", auth="basic", tier="tier1"))
def _rabbitmq_catalog(config: dict) -> Catalog:
    host = config.get("host", "localhost")
    mgmt_port = int(config.get("management_port") or config.get("mgmt_port") or 15672)
    base = f"http://{host}:{mgmt_port}"
    auth = _basic_auth(config) or ("guest", "guest")
    try:
        r = _rget(f"{base}/api/queues", auth=auth)
        if r.status_code != 200:
            return _unsupported(f"Management API returned HTTP {r.status_code}")
        queues = r.json() or []
        items = [
            CatalogItem(name=q.get("name", ""), kind="queue",
                         parent=q.get("vhost", "/"),
                         metadata={"messages": q.get("messages", 0)})
            for q in queues if q.get("name")
        ]
        return _build(items)
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Management API request failed: {exc}")


@register("pulsar", ProviderMeta(category="streaming", auth="oauth2", tier="tier1"))
def _pulsar_catalog(config: dict) -> Catalog:
    base = _strip_url(config.get("admin_url") or config.get("host") or "")
    if not base:
        return _unsupported("Pulsar requires 'admin_url' (e.g. http://host:8080)")
    headers = _bearer(config.get("token"))
    try:
        items: list[CatalogItem] = []
        tenants = _rget(f"{base}/admin/v2/tenants", headers=headers).json() or []
        for tenant in tenants:
            try:
                ns_list = _rget(f"{base}/admin/v2/namespaces/{tenant}",
                                  headers=headers).json() or []
            except Exception:  # noqa: BLE001
                continue
            for ns in ns_list:
                try:
                    topics = _rget(f"{base}/admin/v2/persistent/{ns}",
                                     headers=headers).json() or []
                except Exception:  # noqa: BLE001
                    continue
                for full in topics:
                    name = full.rsplit("/", 1)[-1]
                    items.append(CatalogItem(name=name, kind="topic", parent=ns))
        return _build(items)
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Pulsar admin request failed: {exc}")


@register("nats", ProviderMeta(category="streaming", auth="basic", tier="tier2"))
def _nats_catalog(config: dict) -> Catalog:
    """JetStream stream listing via the management subject. Falls back
    gracefully when JetStream isn't enabled."""
    try:
        import asyncio
        import nats  # type: ignore
    except ImportError:
        return _unsupported("nats-py not installed (pip install nats-py)")
    servers = config.get("servers") or [config.get("url") or f"nats://{config.get('host','localhost')}:{int(config.get('port', 4222))}"]
    if isinstance(servers, str):
        servers = [servers]

    async def _list_streams() -> list[str]:
        nc = await nats.connect(servers=servers, allow_reconnect=False,
                                  connect_timeout=DEFAULT_TIMEOUT)
        try:
            js = nc.jetstream()
            streams = []
            async for s in js.streams_info():
                streams.append(s.config.name)
            return streams
        finally:
            await nc.close()

    try:
        names = asyncio.run(_list_streams())
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"NATS JetStream listing failed: {exc}")
    return _build([CatalogItem(name=n, kind="stream", parent="") for n in names])


@register("eventhub", ProviderMeta(category="streaming", auth="oauth2", tier="tier2"))
def _eventhub_catalog(config: dict) -> Catalog:
    """Lists event hubs in a namespace via the management plane —
    requires AAD or a connection string with management rights."""
    try:
        from azure.identity import DefaultAzureCredential  # type: ignore
        from azure.mgmt.eventhub import EventHubManagementClient  # type: ignore
    except ImportError:
        return _unsupported("azure-mgmt-eventhub not installed (pip install fpulse[azure])")
    sub_id = config.get("subscription_id")
    rg = config.get("resource_group")
    namespace = config.get("namespace") or config.get("eventhub_namespace")
    if not (sub_id and rg and namespace):
        return _unsupported("Event Hub catalog needs 'subscription_id', 'resource_group', 'namespace'")
    try:
        cred = DefaultAzureCredential()
        client = EventHubManagementClient(cred, sub_id)
        items = []
        for hub in client.event_hubs.list_by_namespace(rg, namespace):
            items.append(CatalogItem(name=hub.name, kind="event_hub", parent=namespace))
        return _build(items)
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Event Hub mgmt query failed: {exc}")


@register("pubsub", ProviderMeta(category="streaming", auth="service_account", tier="tier2"))
def _pubsub_catalog(config: dict) -> Catalog:
    try:
        from google.cloud import pubsub_v1  # type: ignore
    except ImportError:
        return _unsupported("google-cloud-pubsub not installed (pip install fpulse[google])")
    project = config.get("project_id") or config.get("project")
    if not project:
        return _unsupported("Pub/Sub requires 'project_id'")
    pub = pubsub_v1.PublisherClient()
    sub = pubsub_v1.SubscriberClient()
    items: list[CatalogItem] = []
    project_path = f"projects/{project}"
    try:
        for topic in pub.list_topics(request={"project": project_path}):
            items.append(CatalogItem(name=topic.name.rsplit("/", 1)[-1],
                                      kind="topic", parent=project))
        for sub_obj in sub.list_subscriptions(request={"project": project_path}):
            items.append(CatalogItem(name=sub_obj.name.rsplit("/", 1)[-1],
                                      kind="subscription", parent=project))
    finally:
        try: sub.close()
        except Exception: pass
    return _build(items)


# ════════════════════════════════════════════════════════════════════
#                      Cloud storage (Tier 2)
# ════════════════════════════════════════════════════════════════════

@register("gcs", ProviderMeta(category="object_storage", auth="service_account", tier="tier2"))
def _gcs_catalog(config: dict) -> Catalog:
    try:
        from google.cloud import storage  # type: ignore
    except ImportError:
        return _unsupported("google-cloud-storage not installed (pip install fpulse[google])")
    project = config.get("project_id") or config.get("project")
    bucket_name = config.get("bucket")
    client = storage.Client(project=project)
    items: list[CatalogItem] = []
    if bucket_name:
        bucket = client.bucket(bucket_name)
        prefix = config.get("prefix", "") or ""
        for blob in client.list_blobs(bucket, prefix=prefix, delimiter="/", max_results=500):
            items.append(CatalogItem(name=blob.name, kind="object",
                                      parent=bucket_name,
                                      metadata={"size": blob.size}))
    else:
        for b in client.list_buckets(max_results=200):
            items.append(CatalogItem(name=b.name, kind="bucket", parent=""))
    return _build(items)


@register("azure_blob", ProviderMeta(category="object_storage", auth="api_key", tier="tier2"))
def _azure_blob_catalog(config: dict) -> Catalog:
    try:
        from azure.storage.blob import BlobServiceClient  # type: ignore
    except ImportError:
        return _unsupported("azure-storage-blob not installed (pip install fpulse[azure])")
    conn_str = config.get("connection_string")
    account_url = config.get("account_url")
    if conn_str:
        client = BlobServiceClient.from_connection_string(conn_str)
    elif account_url:
        client = BlobServiceClient(account_url=account_url, credential=config.get("account_key"))
    else:
        return _unsupported("Azure Blob requires 'connection_string' OR 'account_url'+'account_key'")
    items: list[CatalogItem] = []
    container_name = config.get("container")
    if container_name:
        cc = client.get_container_client(container_name)
        for b in cc.walk_blobs(name_starts_with=config.get("prefix", ""), delimiter="/"):
            items.append(CatalogItem(name=b.name,
                                      kind="prefix" if b.name.endswith("/") else "blob",
                                      parent=container_name))
    else:
        for c in client.list_containers(results_per_page=200):
            items.append(CatalogItem(name=c.name, kind="container", parent=""))
    return _build(items)


@register("adls_gen2", ProviderMeta(category="object_storage", auth="api_key", tier="tier2"))
def _adls_catalog(config: dict) -> Catalog:
    try:
        from azure.storage.filedatalake import DataLakeServiceClient  # type: ignore
    except ImportError:
        return _unsupported("azure-storage-file-datalake not installed (pip install fpulse[azure])")
    account_url = config.get("account_url")
    if not account_url:
        return _unsupported("ADLS Gen2 requires 'account_url' (https://<account>.dfs.core.windows.net)")
    client = DataLakeServiceClient(account_url=account_url, credential=config.get("account_key"))
    items: list[CatalogItem] = []
    fs_name = config.get("filesystem") or config.get("container")
    if fs_name:
        fs = client.get_file_system_client(fs_name)
        for path in fs.get_paths(path=config.get("directory", ""), recursive=False, max_results=500):
            kind = "directory" if path.is_directory else "file"
            items.append(CatalogItem(name=path.name, kind=kind, parent=fs_name))
    else:
        for fs in client.list_file_systems():
            items.append(CatalogItem(name=fs.name, kind="filesystem", parent=""))
    return _build(items)


# ════════════════════════════════════════════════════════════════════
#                     File systems
# ════════════════════════════════════════════════════════════════════

@register("ftp", ProviderMeta(category="file_system", auth="basic", tier="tier1"))
def _ftp_catalog(config: dict) -> Catalog:
    from ftplib import FTP, error_perm
    host = config.get("host", "localhost")
    port = int(config.get("port", 21))
    path = config.get("path") or config.get("directory") or "/"
    try:
        ftp = FTP()
        ftp.connect(host, port, timeout=DEFAULT_TIMEOUT)
        ftp.login(_resolve_user(config, "anonymous"), config.get("password", "anonymous@"))
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"FTP connect failed: {exc}")
    try:
        names: list[str] = []
        try:
            ftp.cwd(path)
            ftp.retrlines("NLST", names.append)
        except error_perm as exc:
            return _unsupported(f"FTP listing failed: {exc}")
        items = [CatalogItem(name=n, kind="entry", parent=path) for n in names]
        return _build(items)
    finally:
        try: ftp.quit()
        except Exception: pass


@register("local_file", ProviderMeta(category="file_system", auth="none", tier="tier1"))
def _local_file_catalog(config: dict) -> Catalog:
    path = config.get("path") or config.get("directory") or config.get("base_path")
    if not path:
        return _unsupported("local_file catalog needs 'path' on the connection")
    if not os.path.isdir(path):
        return _unsupported(f"Path does not exist or is not a directory: {path}")
    items: list[CatalogItem] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                kind = "directory" if entry.is_dir(follow_symlinks=False) else "file"
                meta: dict = {}
                try:
                    if entry.is_file(follow_symlinks=False):
                        meta["size"] = entry.stat(follow_symlinks=False).st_size
                except Exception:  # noqa: BLE001
                    pass
                items.append(CatalogItem(name=entry.name, kind=kind,
                                          parent=path, metadata=meta))
    except PermissionError as exc:
        return _unsupported(f"Permission denied: {exc}")
    return _build(items)


@register("hdfs", ProviderMeta(category="file_system", auth="basic", tier="tier1"))
def _hdfs_catalog(config: dict) -> Catalog:
    """WebHDFS REST listing — works against any standard HDFS NameNode."""
    base = _strip_url(config.get("namenode_url") or config.get("host") or "")
    if not base:
        return _unsupported("HDFS requires 'namenode_url' (e.g. http://nn:9870)")
    user = _resolve_user(config, "")
    path = config.get("path") or "/"
    params = {"op": "LISTSTATUS"}
    if user:
        params["user.name"] = user
    try:
        r = _rget(f"{base}/webhdfs/v1{path}", params=params)
        if r.status_code != 200:
            return _unsupported(f"WebHDFS returned HTTP {r.status_code}")
        body = r.json()
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"WebHDFS request failed: {exc}")
    statuses = (body.get("FileStatuses") or {}).get("FileStatus") or []
    items = [
        CatalogItem(name=s.get("pathSuffix", ""),
                    kind="directory" if s.get("type") == "DIRECTORY" else "file",
                    parent=path, metadata={"size": s.get("length")})
        for s in statuses if s.get("pathSuffix")
    ]
    return _build(items)


@register("sftp", ProviderMeta(category="file_system", auth="basic", tier="tier2"))
def _sftp_catalog(config: dict) -> Catalog:
    try:
        import paramiko  # type: ignore
    except ImportError:
        return _unsupported("paramiko not installed (pip install fpulse[sftp])")
    host = config.get("host")
    if not host:
        return _unsupported("SFTP requires 'host'")
    port = int(config.get("port", 22))
    user = _resolve_user(config, "")
    if not user:
        return _unsupported("SFTP requires 'user'")
    path = config.get("path") or config.get("directory") or "."
    transport = paramiko.Transport((host, port))
    try:
        if config.get("private_key_path"):
            pkey = paramiko.RSAKey.from_private_key_file(config["private_key_path"])
            transport.connect(username=user, pkey=pkey)
        else:
            transport.connect(username=user, password=config.get("password", ""))
        sftp = paramiko.SFTPClient.from_transport(transport)
        items: list[CatalogItem] = []
        for entry in sftp.listdir_attr(path):
            kind = "directory" if (entry.st_mode and (entry.st_mode & 0o040000)) else "file"
            items.append(CatalogItem(name=entry.filename, kind=kind, parent=path,
                                      metadata={"size": entry.st_size}))
        sftp.close()
        return _build(items)
    finally:
        try: transport.close()
        except Exception: pass


# ════════════════════════════════════════════════════════════════════
#                     APIs — OData / SOAP
# ════════════════════════════════════════════════════════════════════

@register("odata", ProviderMeta(category="api", auth="basic", tier="tier1"))
def _odata_catalog(config: dict) -> Catalog:
    """Parse the $metadata document for entity sets."""
    base = _strip_url(config.get("base_url") or config.get("url") or "")
    if not base:
        return _unsupported("OData requires 'base_url'")
    auth = _basic_auth(config)
    try:
        r = _rget(f"{base}/$metadata", auth=auth, headers={"Accept": "application/xml"})
        if r.status_code != 200:
            return _unsupported(f"OData $metadata returned HTTP {r.status_code}")
        import xml.etree.ElementTree as ET
        root = ET.fromstring(r.text)
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"OData metadata fetch failed: {exc}")
    items: list[CatalogItem] = []
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "EntitySet":
            name = elem.attrib.get("Name")
            if name:
                items.append(CatalogItem(name=name, kind="entity_set",
                                          parent=elem.attrib.get("EntityType", "")))
    return _build(items)


@register("soap", ProviderMeta(category="api", auth="basic", tier="tier2"))
def _soap_catalog(config: dict) -> Catalog:
    try:
        from zeep import Client  # type: ignore
    except ImportError:
        return _unsupported("zeep not installed (pip install zeep)")
    wsdl = config.get("wsdl_url") or config.get("url")
    if not wsdl:
        return _unsupported("SOAP requires 'wsdl_url'")
    try:
        client = Client(wsdl)
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"WSDL load failed: {exc}")
    items: list[CatalogItem] = []
    for service in client.wsdl.services.values():
        for port in service.ports.values():
            for op_name in port.binding._operations:
                items.append(CatalogItem(name=op_name, kind="operation",
                                          parent=f"{service.name}/{port.name}"))
    return _build(items)


# ════════════════════════════════════════════════════════════════════
#                       Search / Cache
# ════════════════════════════════════════════════════════════════════

@register("solr", ProviderMeta(category="search_cache", auth="basic", tier="tier1"))
def _solr_catalog(config: dict) -> Catalog:
    base = _strip_url(config.get("base_url") or config.get("host") or "")
    if not base:
        return _unsupported("Solr requires 'base_url' (e.g. http://host:8983)")
    auth = _basic_auth(config)
    try:
        r = _rget(f"{base}/solr/admin/cores",
                   params={"action": "STATUS", "wt": "json"}, auth=auth)
        cores = (r.json().get("status") or {})
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Solr admin request failed: {exc}")
    items = [CatalogItem(name=name, kind="core", parent="",
                          metadata={"docs": (info.get("index") or {}).get("numDocs")})
              for name, info in cores.items()]
    return _build(items)


# ════════════════════════════════════════════════════════════════════
#                       Observability
# ════════════════════════════════════════════════════════════════════

@register("datadog", ProviderMeta(category="observability", auth="api_key", tier="tier1"))
def _datadog_catalog(config: dict) -> Catalog:
    site = config.get("site") or "datadoghq.com"
    base = f"https://api.{site}"
    headers = {
        "DD-API-KEY": config.get("api_key", ""),
        "DD-APPLICATION-KEY": config.get("app_key") or config.get("application_key", ""),
    }
    items: list[CatalogItem] = []
    try:
        dashes = _rget(f"{base}/api/v1/dashboard", headers=headers).json().get("dashboards", []) or []
        for d in dashes:
            items.append(CatalogItem(name=d.get("title", ""), kind="dashboard",
                                      parent="", metadata={"id": d.get("id")}))
        monitors = _rget(f"{base}/api/v1/monitor", headers=headers, params={"page_size": 200}).json() or []
        for m in monitors[:200]:
            items.append(CatalogItem(name=m.get("name", ""), kind="monitor",
                                      parent="", metadata={"id": m.get("id")}))
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Datadog API request failed: {exc}")
    return _build(items)


@register("splunk", ProviderMeta(category="observability", auth="basic", tier="tier1"))
def _splunk_catalog(config: dict) -> Catalog:
    base = _strip_url(config.get("base_url") or config.get("host") or "")
    if not base:
        return _unsupported("Splunk requires 'base_url' (e.g. https://host:8089)")
    headers = _bearer(config.get("token"))
    auth = None if config.get("token") else _basic_auth(config)
    try:
        items: list[CatalogItem] = []
        idx = _rget(f"{base}/services/data/indexes",
                     params={"output_mode": "json", "count": 200},
                     headers=headers, auth=auth).json()
        for entry in (idx.get("entry") or []):
            items.append(CatalogItem(name=entry.get("name", ""), kind="index", parent=""))
        searches = _rget(f"{base}/services/saved/searches",
                          params={"output_mode": "json", "count": 200},
                          headers=headers, auth=auth).json()
        for entry in (searches.get("entry") or []):
            items.append(CatalogItem(name=entry.get("name", ""),
                                      kind="saved_search", parent=""))
        return _build(items)
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Splunk API request failed: {exc}")


@register("newrelic", ProviderMeta(category="observability", auth="api_key", tier="tier1"))
def _newrelic_catalog(config: dict) -> Catalog:
    api_key = config.get("api_key") or config.get("user_key")
    account_id = config.get("account_id")
    if not (api_key and account_id):
        return _unsupported("New Relic catalog needs 'api_key' and 'account_id'")
    query = """{
      actor {
        entitySearch(query: "domain in ('APM','BROWSER','MOBILE','INFRA')") {
          results { entities { name domain entityType } }
        }
      }
    }"""
    try:
        r = _rpost("https://api.newrelic.com/graphql",
                    headers={"API-Key": api_key, "Content-Type": "application/json"},
                    json={"query": query})
        ents = (((r.json().get("data") or {}).get("actor") or {})
                .get("entitySearch") or {}).get("results", {}).get("entities", []) or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"New Relic NerdGraph request failed: {exc}")
    items = [CatalogItem(name=e.get("name", ""),
                          kind=str(e.get("entityType", "entity")).lower(),
                          parent=e.get("domain", ""))
              for e in ents]
    return _build(items)


@register("prometheus", ProviderMeta(category="observability", auth="basic", tier="tier1"))
def _prometheus_catalog(config: dict) -> Catalog:
    base = _strip_url(config.get("base_url") or config.get("host") or "")
    if not base:
        return _unsupported("Prometheus requires 'base_url'")
    auth = _basic_auth(config)
    headers = _bearer(config.get("token"))
    try:
        r = _rget(f"{base}/api/v1/label/__name__/values", auth=auth, headers=headers)
        names = (r.json().get("data") or [])
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Prometheus API request failed: {exc}")
    items = [CatalogItem(name=n, kind="metric", parent="") for n in names[:2000]]
    return _build(items)


@register("grafana", ProviderMeta(category="observability", auth="api_key", tier="tier1"))
def _grafana_catalog(config: dict) -> Catalog:
    base = _strip_url(config.get("base_url") or config.get("host") or "")
    if not base:
        return _unsupported("Grafana requires 'base_url'")
    headers = _bearer(config.get("api_key") or config.get("token"))
    try:
        items: list[CatalogItem] = []
        ds = _rget(f"{base}/api/datasources", headers=headers).json() or []
        for d in ds:
            items.append(CatalogItem(name=d.get("name", ""), kind="datasource",
                                      parent="", metadata={"type": d.get("type")}))
        dashes = _rget(f"{base}/api/search", headers=headers,
                         params={"type": "dash-db", "limit": 200}).json() or []
        for d in dashes:
            items.append(CatalogItem(name=d.get("title", ""), kind="dashboard",
                                      parent=d.get("folderTitle", "")))
        return _build(items)
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Grafana API request failed: {exc}")


# ════════════════════════════════════════════════════════════════════
#                       Vector databases
# ════════════════════════════════════════════════════════════════════

@register("pinecone", ProviderMeta(category="vector", auth="api_key", tier="tier1"))
def _pinecone_catalog(config: dict) -> Catalog:
    api_key = config.get("api_key")
    if not api_key:
        return _unsupported("Pinecone requires 'api_key'")
    try:
        # Newer Pinecone uses a single global control-plane host.
        r = _rget("https://api.pinecone.io/indexes",
                   headers={"Api-Key": api_key, "X-Pinecone-API-Version": "2024-07"})
        indexes = r.json().get("indexes", []) or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Pinecone API request failed: {exc}")
    items = [CatalogItem(name=i.get("name", ""), kind="index", parent="",
                          metadata={"dimension": i.get("dimension"),
                                     "metric": i.get("metric")})
              for i in indexes]
    return _build(items)


@register("weaviate", ProviderMeta(category="vector", auth="api_key", tier="tier1"))
def _weaviate_catalog(config: dict) -> Catalog:
    base = _strip_url(config.get("url") or config.get("host") or "")
    if not base:
        return _unsupported("Weaviate requires 'url'")
    headers = _bearer(config.get("api_key") or config.get("token"))
    try:
        r = _rget(f"{base}/v1/schema", headers=headers)
        classes = (r.json().get("classes") or [])
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Weaviate schema fetch failed: {exc}")
    items = [CatalogItem(name=c.get("class", ""), kind="class", parent="",
                          metadata={"vectorizer": c.get("vectorizer")})
              for c in classes if c.get("class")]
    return _build(items)


@register("qdrant", ProviderMeta(category="vector", auth="api_key", tier="tier1"))
def _qdrant_catalog(config: dict) -> Catalog:
    base = _strip_url(config.get("url") or config.get("host") or "")
    if not base:
        return _unsupported("Qdrant requires 'url'")
    headers: dict = {}
    if config.get("api_key"):
        headers["api-key"] = config["api_key"]
    try:
        r = _rget(f"{base}/collections", headers=headers)
        cols = ((r.json().get("result") or {}).get("collections") or [])
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Qdrant API request failed: {exc}")
    items = [CatalogItem(name=c.get("name", ""), kind="collection", parent="")
              for c in cols if c.get("name")]
    return _build(items)


@register("chroma", ProviderMeta(category="vector", auth="none", tier="tier1"))
def _chroma_catalog(config: dict) -> Catalog:
    base = _strip_url(config.get("url") or config.get("host") or "")
    if not base:
        return _unsupported("Chroma requires 'url' to a running server (HTTP mode)")
    try:
        r = _rget(f"{base}/api/v1/collections")
        cols = r.json() or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Chroma server request failed: {exc}")
    items = [CatalogItem(name=c.get("name", ""), kind="collection", parent="")
              for c in cols if c.get("name")]
    return _build(items)


@register("milvus", ProviderMeta(category="vector", auth="basic", tier="tier1"))
def _milvus_catalog(config: dict) -> Catalog:
    """REST API path — works for Milvus 2.3+ standalone or Zilliz Cloud."""
    base = _strip_url(config.get("url") or config.get("host") or "")
    if not base:
        return _unsupported("Milvus requires 'url'")
    headers = _bearer(config.get("token") or config.get("api_key"))
    try:
        r = _rget(f"{base}/v1/vector/collections", headers=headers)
        body = r.json() or {}
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Milvus API request failed: {exc}")
    cols = body.get("data") or body.get("collections") or []
    items = [CatalogItem(name=str(c) if isinstance(c, str) else c.get("name", ""),
                          kind="collection", parent="")
              for c in cols]
    return _build(items)


@register("pgvector", ProviderMeta(
    category="vector", auth="basic", tier="tier1",
    notes="pgvector is PostgreSQL — lists tables that have a 'vector' column"))
def _pgvector_catalog(config: dict) -> Catalog:
    try:
        import psycopg2  # type: ignore
    except ImportError:
        return _unsupported("psycopg2 not installed (pip install psycopg2-binary)")
    conn = psycopg2.connect(
        host=config.get("host", "localhost"),
        port=int(config.get("port", 5432)),
        dbname=config.get("database", "postgres"),
        user=_resolve_user(config, "postgres"),
        password=config.get("password", ""),
        connect_timeout=DEFAULT_TIMEOUT,
    )
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT table_schema, table_name
            FROM information_schema.columns
            WHERE udt_name = 'vector'
              AND table_schema NOT IN ('pg_catalog','information_schema')
            ORDER BY 1, 2
        """)
        items = [CatalogItem(name=r[1], kind="table", parent=r[0],
                              metadata={"has_vector_column": True})
                  for r in cur.fetchall()]
        return _build(items)
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════
#                  NoSQL — Tier 2 SDKs
# ════════════════════════════════════════════════════════════════════

@register("cassandra", ProviderMeta(category="nosql", auth="basic", tier="tier2"))
def _cassandra_catalog(config: dict) -> Catalog:
    try:
        from cassandra.cluster import Cluster  # type: ignore
        from cassandra.auth import PlainTextAuthProvider  # type: ignore
    except ImportError:
        return _unsupported("cassandra-driver not installed (pip install cassandra-driver)")
    contact_points = config.get("contact_points") or [config.get("host", "localhost")]
    if isinstance(contact_points, str):
        contact_points = [contact_points]
    auth = None
    if config.get("user") or config.get("username"):
        auth = PlainTextAuthProvider(
            username=_resolve_user(config, ""),
            password=config.get("password", ""),
        )
    cluster = Cluster(contact_points=contact_points,
                       port=int(config.get("port", 9042)),
                       auth_provider=auth, connect_timeout=DEFAULT_TIMEOUT)
    session = cluster.connect()
    try:
        rows = session.execute(
            "SELECT keyspace_name, table_name FROM system_schema.tables"
        )
        items = [CatalogItem(name=r.table_name, kind="table",
                              parent=r.keyspace_name)
                  for r in rows
                  if not r.keyspace_name.startswith("system")]
        return _build(items)
    finally:
        try: cluster.shutdown()
        except Exception: pass


@register("couchbase", ProviderMeta(category="nosql", auth="basic", tier="tier1"))
def _couchbase_catalog(config: dict) -> Catalog:
    """Uses the Couchbase REST mgmt API on port 8091 — no Couchbase SDK
    required, just `requests`."""
    host = config.get("host", "localhost")
    port = int(config.get("management_port") or 8091)
    base = f"http://{host}:{port}"
    auth = _basic_auth(config) or ("Administrator", "")
    try:
        r = _rget(f"{base}/pools/default/buckets", auth=auth)
        if r.status_code != 200:
            return _unsupported(f"Couchbase mgmt returned HTTP {r.status_code}")
        buckets = r.json() or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Couchbase mgmt request failed: {exc}")
    items = [CatalogItem(name=b.get("name", ""), kind="bucket", parent="",
                          metadata={"type": b.get("bucketType")})
              for b in buckets if b.get("name")]
    return _build(items)


@register("neo4j", ProviderMeta(category="nosql", auth="basic", tier="tier2"))
def _neo4j_catalog(config: dict) -> Catalog:
    try:
        from neo4j import GraphDatabase  # type: ignore
    except ImportError:
        return _unsupported("neo4j driver not installed (pip install neo4j)")
    uri = config.get("uri") or f"bolt://{config.get('host', 'localhost')}:{int(config.get('port', 7687))}"
    user = _resolve_user(config, "neo4j")
    driver = GraphDatabase.driver(uri, auth=(user, config.get("password", "")),
                                    connection_timeout=DEFAULT_TIMEOUT)
    try:
        items: list[CatalogItem] = []
        with driver.session() as session:
            for r in session.run("CALL db.labels()"):
                items.append(CatalogItem(name=r["label"], kind="label", parent="node"))
            for r in session.run("CALL db.relationshipTypes()"):
                items.append(CatalogItem(name=r["relationshipType"],
                                          kind="relationship_type", parent="edge"))
        return _build(items)
    finally:
        driver.close()


@register("arangodb", ProviderMeta(category="nosql", auth="basic", tier="tier1"))
def _arangodb_catalog(config: dict) -> Catalog:
    base = _strip_url(config.get("url") or config.get("host") or "")
    if not base:
        return _unsupported("ArangoDB requires 'url'")
    db = config.get("database", "_system")
    auth = _basic_auth(config)
    try:
        r = _rget(f"{base}/_db/{db}/_api/collection", auth=auth)
        cols = (r.json().get("result") or [])
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"ArangoDB API request failed: {exc}")
    items = [CatalogItem(name=c.get("name", ""), kind="collection", parent=db,
                          metadata={"type": c.get("type")})
              for c in cols
              if c.get("name") and not c.get("name", "").startswith("_")]
    return _build(items)


@register("firebase", ProviderMeta(category="nosql", auth="service_account", tier="tier2"))
def _firebase_catalog(config: dict) -> Catalog:
    try:
        from google.cloud import firestore  # type: ignore
    except ImportError:
        return _unsupported("google-cloud-firestore not installed (pip install fpulse[google])")
    project = config.get("project_id") or config.get("project")
    if not project:
        return _unsupported("Firebase requires 'project_id'")
    client = firestore.Client(project=project)
    items: list[CatalogItem] = []
    try:
        for col in client.collections():
            items.append(CatalogItem(name=col.id, kind="collection", parent=project))
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Firestore listing failed: {exc}")
    return _build(items)


@register("cosmosdb", ProviderMeta(category="nosql", auth="api_key", tier="tier2"))
def _cosmosdb_catalog(config: dict) -> Catalog:
    try:
        from azure.cosmos import CosmosClient  # type: ignore
    except ImportError:
        return _unsupported("azure-cosmos not installed (pip install fpulse[azure])")
    endpoint = config.get("endpoint") or config.get("url")
    key = config.get("account_key") or config.get("key")
    if not (endpoint and key):
        return _unsupported("Cosmos DB requires 'endpoint' and 'account_key'")
    client = CosmosClient(endpoint, credential=key)
    items: list[CatalogItem] = []
    try:
        for db in client.list_databases():
            db_name = db["id"]
            db_client = client.get_database_client(db_name)
            for c in db_client.list_containers():
                items.append(CatalogItem(name=c["id"], kind="container",
                                          parent=db_name))
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Cosmos DB listing failed: {exc}")
    return _build(items)


# ════════════════════════════════════════════════════════════════════
#                   Heavy SDK / vendor-locked relational
# ════════════════════════════════════════════════════════════════════

@register("oracle", ProviderMeta(category="relational", auth="basic", tier="tier3"))
def _oracle_catalog(config: dict) -> Catalog:
    try:
        import oracledb  # type: ignore
    except ImportError:
        return _unsupported("oracledb not installed (pip install fpulse[oracle])")
    host = config.get("host", "localhost")
    port = int(config.get("port", 1521))
    service = config.get("service_name") or config.get("sid") or "XE"
    user = _resolve_user(config, "")
    if not user:
        return _unsupported("Oracle requires 'user'")
    dsn = oracledb.makedsn(host, port, service_name=service)
    conn = oracledb.connect(user=user, password=config.get("password", ""), dsn=dsn)
    try:
        cur = conn.cursor()
        items: list[CatalogItem] = []
        cur.execute(
            "SELECT owner, table_name FROM all_tables "
            "WHERE owner NOT IN ('SYS','SYSTEM','XDB','MDSYS','CTXSYS','OUTLN') "
            "ORDER BY 1, 2"
        )
        for owner, name in cur.fetchall():
            items.append(CatalogItem(name=name, kind="table", parent=owner))
        cur.execute(
            "SELECT owner, view_name FROM all_views "
            "WHERE owner NOT IN ('SYS','SYSTEM','XDB','MDSYS','CTXSYS','OUTLN') "
            "ORDER BY 1, 2"
        )
        for owner, name in cur.fetchall():
            items.append(CatalogItem(name=name, kind="view", parent=owner))
        return _build(items)
    finally:
        conn.close()


@register("db2", ProviderMeta(category="relational", auth="basic", tier="tier3"))
def _db2_catalog(config: dict) -> Catalog:
    try:
        import ibm_db  # type: ignore
        import ibm_db_dbi  # type: ignore
    except ImportError:
        return _unsupported("ibm_db not installed (pip install fpulse[db2])")
    dsn = (
        f"DATABASE={config.get('database', 'SAMPLE')};"
        f"HOSTNAME={config.get('host', 'localhost')};"
        f"PORT={int(config.get('port', 50000))};"
        f"PROTOCOL=TCPIP;"
        f"UID={_resolve_user(config, 'db2inst1')};"
        f"PWD={config.get('password', '')};"
    )
    raw = ibm_db.connect(dsn, "", "")
    conn = ibm_db_dbi.Connection(raw)
    try:
        cur = conn.cursor()
        cur.execute("SELECT tabschema, tabname FROM syscat.tables "
                     "WHERE type='T' AND tabschema NOT LIKE 'SYS%' "
                     "ORDER BY 1, 2")
        items = [CatalogItem(name=r[1], kind="table", parent=r[0])
                  for r in cur.fetchall()]
        return _build(items)
    finally:
        conn.close()


@register("sap_hana", ProviderMeta(category="relational", auth="basic", tier="tier3"))
def _sap_hana_catalog(config: dict) -> Catalog:
    try:
        from hdbcli import dbapi  # type: ignore
    except ImportError:
        return _unsupported("hdbcli not installed (pip install fpulse[hana])")
    conn = dbapi.connect(
        address=config.get("host"),
        port=int(config.get("port", 30015)),
        user=_resolve_user(config, ""),
        password=config.get("password", ""),
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT schema_name, table_name FROM sys.tables "
                     "WHERE schema_name NOT IN ('SYS','_SYS_BIC','_SYS_REPO') "
                     "ORDER BY 1, 2")
        items = [CatalogItem(name=r[1], kind="table", parent=r[0])
                  for r in cur.fetchall()]
        return _build(items)
    finally:
        conn.close()


@register("teradata", ProviderMeta(category="relational", auth="basic", tier="tier3"))
def _teradata_catalog(config: dict) -> Catalog:
    try:
        import teradatasql  # type: ignore
    except ImportError:
        return _unsupported("teradatasql not installed (pip install fpulse[teradata])")
    conn = teradatasql.connect(
        host=config.get("host", ""),
        user=_resolve_user(config, ""),
        password=config.get("password", ""),
    )
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT databasename, tablename FROM dbc.tablesv "
            "WHERE tablekind IN ('T','V') ORDER BY 1, 2"
        )
        items = [CatalogItem(name=r[1].strip(), kind="table",
                              parent=r[0].strip())
                  for r in cur.fetchall()]
        return _build(items)
    finally:
        conn.close()


@register("informix", ProviderMeta(category="relational", auth="basic", tier="tier3"))
def _informix_catalog(config: dict) -> Catalog:
    """Informix ships with poor pure-python driver support; fall back
    via ibm_db when it's installed (Informix client libs needed)."""
    try:
        import ibm_db  # type: ignore
        import ibm_db_dbi  # type: ignore
    except ImportError:
        return _unsupported("ibm_db not installed; Informix needs the IBM client libs (pip install fpulse[db2])")
    dsn = (
        f"DATABASE={config.get('database', '')};"
        f"HOSTNAME={config.get('host', 'localhost')};"
        f"PORT={int(config.get('port', 9088))};"
        f"PROTOCOL=ONSOCTCP;"
        f"UID={_resolve_user(config, 'informix')};"
        f"PWD={config.get('password', '')};"
    )
    raw = ibm_db.connect(dsn, "", "")
    conn = ibm_db_dbi.Connection(raw)
    try:
        cur = conn.cursor()
        cur.execute("SELECT tabname FROM systables WHERE tabid > 99 ORDER BY 1")
        items = [CatalogItem(name=r[0], kind="table", parent="")
                  for r in cur.fetchall()]
        return _build(items)
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════
#                   SaaS — REST + (mostly) OAuth2
# ════════════════════════════════════════════════════════════════════
#
# SaaS providers expect the connection's credentials (access_token,
# refresh_token, etc.) to flow through resolve_credentials → vault.
# For Tier-1 wiring, we accept either a bearer `token` in config or
# rely on the connection-level OAuth refresh path. The header builder
# below prefers the static token when present so a quick PAT-style
# config still works.

def _saas_token(config: dict) -> str | None:
    return config.get("access_token") or config.get("token") or config.get("api_key")


def _saas_headers(config: dict) -> dict:
    h = {"Accept": "application/json"}
    tok = _saas_token(config)
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


@register("salesforce", ProviderMeta(category="saas", auth="oauth2", tier="tier1"))
def _salesforce_catalog(config: dict) -> Catalog:
    instance = _strip_url(config.get("instance_url") or "")
    if not instance:
        return _unsupported("Salesforce requires 'instance_url' (e.g. https://acme.my.salesforce.com)")
    api_v = config.get("api_version", "59.0")
    try:
        r = _rget(f"{instance}/services/data/v{api_v}/sobjects/",
                   headers=_saas_headers(config))
        if r.status_code != 200:
            return _unsupported(f"Salesforce API returned HTTP {r.status_code}")
        body = r.json()
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Salesforce API request failed: {exc}")
    items = [CatalogItem(name=o.get("name", ""), kind="sobject", parent="",
                          metadata={"label": o.get("label"),
                                     "queryable": o.get("queryable")})
              for o in (body.get("sobjects") or []) if o.get("name")]
    return _build(items)


@register("hubspot", ProviderMeta(category="saas", auth="oauth2", tier="tier1"))
def _hubspot_catalog(config: dict) -> Catalog:
    try:
        r = _rget("https://api.hubapi.com/crm/v3/schemas",
                   headers=_saas_headers(config))
        body = r.json()
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"HubSpot API request failed: {exc}")
    items = [CatalogItem(name=s.get("name", ""), kind="object_type", parent="",
                          metadata={"label": (s.get("labels") or {}).get("plural")})
              for s in (body.get("results") or []) if s.get("name")]
    return _build(items)


@register("zendesk", ProviderMeta(category="saas", auth="oauth2", tier="tier1"))
def _zendesk_catalog(config: dict) -> Catalog:
    subdomain = config.get("subdomain")
    if not subdomain:
        return _unsupported("Zendesk requires 'subdomain'")
    base = f"https://{subdomain}.zendesk.com/api/v2"
    headers = _saas_headers(config)
    auth = None
    if config.get("email") and config.get("api_token"):
        auth = (f"{config['email']}/token", config["api_token"])
    items: list[CatalogItem] = []
    for resource in ("tickets", "users", "organizations", "groups"):
        try:
            r = _rget(f"{base}/{resource}.json", headers=headers, auth=auth,
                       params={"per_page": 1})
            if r.status_code == 200:
                items.append(CatalogItem(name=resource, kind="resource_type",
                                          parent=""))
        except Exception:  # noqa: BLE001
            continue
    return _build(items)


@register("jira", ProviderMeta(category="saas", auth="oauth2", tier="tier1"))
def _jira_catalog(config: dict) -> Catalog:
    base = _strip_url(config.get("base_url") or config.get("url") or "")
    if not base:
        return _unsupported("Jira requires 'base_url' (e.g. https://acme.atlassian.net)")
    headers = _saas_headers(config)
    auth = None
    if config.get("email") and config.get("api_token"):
        auth = (config["email"], config["api_token"])
    try:
        projects = _rget(f"{base}/rest/api/3/project/search",
                          headers=headers, auth=auth,
                          params={"maxResults": 100}).json()
        proj_list = projects.get("values") or []
        issuetypes = _rget(f"{base}/rest/api/3/issuetype",
                            headers=headers, auth=auth).json() or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Jira API request failed: {exc}")
    items: list[CatalogItem] = []
    for p in proj_list:
        items.append(CatalogItem(name=p.get("name", ""), kind="project",
                                  parent="", metadata={"key": p.get("key")}))
    for it in issuetypes:
        items.append(CatalogItem(name=it.get("name", ""), kind="issue_type", parent=""))
    return _build(items)


@register("servicenow", ProviderMeta(category="saas", auth="oauth2", tier="tier1"))
def _servicenow_catalog(config: dict) -> Catalog:
    instance = config.get("instance")
    if not instance:
        return _unsupported("ServiceNow requires 'instance' (e.g. acme — used as <instance>.service-now.com)")
    base = f"https://{instance}.service-now.com"
    headers = _saas_headers(config)
    auth = _basic_auth(config) if not _saas_token(config) else None
    try:
        r = _rget(f"{base}/api/now/table/sys_db_object",
                   headers=headers, auth=auth,
                   params={"sysparm_fields": "name,label", "sysparm_limit": 500})
        rows = r.json().get("result") or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"ServiceNow API request failed: {exc}")
    items = [CatalogItem(name=row.get("name", ""), kind="table", parent="",
                          metadata={"label": row.get("label")})
              for row in rows if row.get("name")]
    return _build(items)


@register("netsuite", ProviderMeta(category="saas", auth="oauth2", tier="tier1"))
def _netsuite_catalog(config: dict) -> Catalog:
    """SuiteTalk REST metadata catalog. Auth is OAuth1 (TBA) — heavy
    to do from scratch in 30 lines, so we accept a pre-baked
    Authorization header in `auth_header` config when the user has
    already signed it (OR pass through the OAuthSession path)."""
    account = config.get("account_id")
    if not account:
        return _unsupported("NetSuite requires 'account_id'")
    base = f"https://{account.lower().replace('_', '-')}.suitetalk.api.netsuite.com/services/rest/record/v1/metadata-catalog"
    headers = _saas_headers(config)
    if config.get("auth_header"):
        headers["Authorization"] = config["auth_header"]
    try:
        r = _rget(base, headers=headers, params={"select": "items"})
        items_resp = r.json().get("items") or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"NetSuite metadata-catalog request failed: {exc}")
    items = [CatalogItem(name=str(o.get("name", "")), kind="record_type", parent="")
              for o in items_resp if o.get("name")]
    return _build(items)


# ════════════════════════════════════════════════════════════════════
#  W1 (2026-05-23) — manifest-promoted SaaS as first-class connections
# ════════════════════════════════════════════════════════════════════
#
# Each of github / shopify / stripe / notion / asana already ships as a
# v1 REST manifest under backend/fpulse/connectors/manifests/. The W1
# work promotes them to first-class saved-connection types so users
# don't have to bounce through the saas_connector palette every time.
# Catalog browsing enumerates the manifest's declared streams — that's
# already a curated list of the canonical resources per connector, so
# it's a faster + safer initial UX than a live introspection call.

def _manifest_stream_catalog(connector_id: str, parent_label: str) -> Catalog:
    """Build a Catalog from a v1 REST manifest's stream list.

    The streams a manifest declares are the canonical "what can I read"
    list for that connector. Listing them as catalog items lets the UI
    pre-populate the table picker with vetted endpoints rather than a
    free-text field; the user can still switch to free-text when they
    need a non-canonical stream.
    """
    try:
        from fpulse.connectors.rest_framework import get_manifest
    except Exception:  # pragma: no cover
        return _unsupported("Manifest framework not available.")
    manifest = get_manifest(connector_id)
    if not manifest:
        return _unsupported(f"Manifest for '{connector_id}' not loaded.")
    items: list[CatalogItem] = []
    for stream in manifest.streams:
        name = stream.get("name") or ""
        if not name:
            continue
        items.append(CatalogItem(
            name=name,
            kind="stream",
            parent=parent_label,
            metadata={
                "label": stream.get("label") or name,
                "path": stream.get("path"),
                "manifest_id": connector_id,
            },
        ))
    return _build(items)


@register("github", ProviderMeta(category="saas", auth="api_key", tier="tier1",
                                   notes="Drives the github v1 manifest's stream list."))
def _github_catalog(config: dict) -> Catalog:
    return _manifest_stream_catalog("github", "github")


@register("shopify", ProviderMeta(category="saas", auth="api_key", tier="tier1",
                                    notes="Drives the shopify v1 manifest's stream list."))
def _shopify_catalog(config: dict) -> Catalog:
    return _manifest_stream_catalog("shopify", "shopify")


@register("stripe", ProviderMeta(category="saas", auth="api_key", tier="tier1",
                                   notes="Drives the stripe v1 manifest's stream list."))
def _stripe_catalog(config: dict) -> Catalog:
    return _manifest_stream_catalog("stripe", "stripe")


@register("notion", ProviderMeta(category="saas", auth="oauth2", tier="tier1",
                                   notes="Drives the notion v1 manifest's stream list."))
def _notion_catalog(config: dict) -> Catalog:
    return _manifest_stream_catalog("notion", "notion")


@register("asana", ProviderMeta(category="saas", auth="oauth2", tier="tier1",
                                  notes="Drives the asana v1 manifest's stream list."))
def _asana_catalog(config: dict) -> Catalog:
    return _manifest_stream_catalog("asana", "asana")


@register("dynamics365", ProviderMeta(category="saas", auth="oauth2", tier="tier1"))
def _dynamics365_catalog(config: dict) -> Catalog:
    base = _strip_url(config.get("instance_url") or config.get("url") or "")
    if not base:
        return _unsupported("Dynamics365 requires 'instance_url'")
    api_v = config.get("api_version", "9.2")
    headers = _saas_headers(config)
    headers["OData-Version"] = "4.0"
    try:
        r = _rget(f"{base}/api/data/v{api_v}/EntityDefinitions",
                   headers=headers,
                   params={"$select": "LogicalName,SchemaName,DisplayName"})
        ents = r.json().get("value") or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Dynamics365 API request failed: {exc}")
    items = [CatalogItem(name=e.get("LogicalName", ""), kind="entity",
                          parent="", metadata={"schema": e.get("SchemaName")})
              for e in ents if e.get("LogicalName")]
    return _build(items)


# Microsoft Graph helpers (sharepoint / onedrive)

def _graph_get(path: str, token: str | None) -> Any:
    return _rget(f"https://graph.microsoft.com/v1.0{path}",
                  headers=_bearer(token) | {"Accept": "application/json"})


# ── Microsoft Graph (generic) ──────────────────────────────────────
#
# 2026-05-22 — first-class Graph connector. Unlike SharePoint /
# OneDrive (which are product-specific drives/lists views), this
# catalog enumerates the canonical Graph RESOURCE endpoints — users,
# groups, sites, drives, teams, planner, mail, calendars — so the
# microsoft_graph_source node UI can offer them as presets.
#
# The catalog is static (the resource ENDPOINTS don't change — only
# the row data behind them). This keeps the catalog call cheap +
# tenant-independent, so it works even when the connection hasn't
# been tested yet.

_GRAPH_ENDPOINTS: list[tuple[str, str, str]] = [
    # (name, path, kind)
    ("users",          "/users",          "collection"),
    ("groups",         "/groups",         "collection"),
    ("sites",          "/sites",          "collection"),
    ("drives",         "/drives",         "collection"),
    ("teams",          "/teams",          "collection"),
    ("me",             "/me",             "singleton"),
    ("organization",   "/organization",   "collection"),
    ("planner_plans",  "/planner/plans",  "collection"),
    ("planner_tasks",  "/planner/tasks",  "collection"),
    ("mail_messages",  "/me/messages",    "collection"),
    ("calendars",      "/me/calendars",   "collection"),
    ("calendar_events", "/me/events",     "collection"),
    ("directory_roles", "/directoryRoles", "collection"),
    ("applications",   "/applications",   "collection"),
    ("service_principals", "/servicePrincipals", "collection"),
]


@register("microsoft_graph", ProviderMeta(category="api", auth="oauth2", tier="tier1"))
def _microsoft_graph_catalog(config: dict) -> Catalog:
    """List the canonical Microsoft Graph resource endpoints.

    Unlike most catalog providers, this one does NOT hit the live
    API to enumerate items — Graph's "what endpoints exist" doesn't
    have a single discovery URL, and the canonical list is small +
    stable. Surfacing it statically here lets the source-node UI
    populate a "pick a resource" dropdown without requiring a
    working tenant connection.
    """
    items = [
        CatalogItem(
            name=name,
            kind=kind,
            parent="microsoft_graph",
            metadata={"path": path, "doc_url": f"https://learn.microsoft.com/graph/api/resources/{name.split('_')[0]}"},
        )
        for name, path, kind in _GRAPH_ENDPOINTS
    ]
    return _build(items)


@register("sharepoint", ProviderMeta(category="collaboration", auth="oauth2", tier="tier1"))
def _sharepoint_catalog(config: dict) -> Catalog:
    site_id = config.get("site_id")
    if not site_id:
        return _unsupported("SharePoint requires 'site_id'")
    token = _saas_token(config)
    try:
        r = _graph_get(f"/sites/{site_id}/lists", token)
        lists = r.json().get("value") or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Graph request failed: {exc}")
    items = [CatalogItem(name=l.get("displayName", ""), kind="list",
                          parent=site_id, metadata={"id": l.get("id")})
              for l in lists if l.get("displayName")]
    return _build(items)


@register("onedrive", ProviderMeta(category="collaboration", auth="oauth2", tier="tier1"))
def _onedrive_catalog(config: dict) -> Catalog:
    token = _saas_token(config)
    path = config.get("path", "")
    drive_id = config.get("drive_id")
    if drive_id:
        url = f"/drives/{drive_id}/root{':/' + path + ':' if path else ''}/children"
    else:
        url = f"/me/drive/root{':/' + path + ':' if path else ''}/children"
    try:
        r = _graph_get(url, token)
        children = r.json().get("value") or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Graph request failed: {exc}")
    items = []
    for c in children:
        kind = "folder" if c.get("folder") else "file"
        items.append(CatalogItem(name=c.get("name", ""), kind=kind,
                                  parent=path or "/",
                                  metadata={"size": c.get("size")}))
    return _build(items)


@register("dropbox", ProviderMeta(category="collaboration", auth="oauth2", tier="tier1"))
def _dropbox_catalog(config: dict) -> Catalog:
    token = _saas_token(config)
    if not token:
        return _unsupported("Dropbox requires an OAuth2 access token")
    path = config.get("path", "")
    try:
        r = _rpost("https://api.dropboxapi.com/2/files/list_folder",
                    headers={"Authorization": f"Bearer {token}",
                              "Content-Type": "application/json"},
                    json={"path": path, "limit": 500})
        entries = r.json().get("entries") or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Dropbox API request failed: {exc}")
    items = [CatalogItem(name=e.get("name", ""),
                          kind="folder" if e.get(".tag") == "folder" else "file",
                          parent=path or "/")
              for e in entries if e.get("name")]
    return _build(items)


@register("box", ProviderMeta(category="collaboration", auth="oauth2", tier="tier1"))
def _box_catalog(config: dict) -> Catalog:
    token = _saas_token(config)
    folder_id = config.get("folder_id", "0")
    try:
        r = _rget(f"https://api.box.com/2.0/folders/{folder_id}/items",
                   headers=_bearer(token), params={"limit": 500})
        entries = r.json().get("entries") or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Box API request failed: {exc}")
    items = [CatalogItem(name=e.get("name", ""),
                          kind=e.get("type", "item"),
                          parent=folder_id)
              for e in entries if e.get("name")]
    return _build(items)


@register("gdrive", ProviderMeta(category="collaboration", auth="oauth2", tier="tier2"))
def _gdrive_catalog(config: dict) -> Catalog:
    token = _saas_token(config)
    try:
        r = _rget("https://www.googleapis.com/drive/v3/files",
                   headers=_bearer(token),
                   params={"q": "'root' in parents and trashed=false",
                           "pageSize": 500,
                           "fields": "files(id,name,mimeType,size)"})
        files = r.json().get("files") or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Google Drive API request failed: {exc}")
    items = []
    for f in files:
        is_folder = f.get("mimeType") == "application/vnd.google-apps.folder"
        items.append(CatalogItem(name=f.get("name", ""),
                                  kind="folder" if is_folder else "file",
                                  parent="root",
                                  metadata={"id": f.get("id"),
                                             "mime": f.get("mimeType")}))
    return _build(items)


@register("gsheet", ProviderMeta(category="collaboration", auth="oauth2", tier="tier2"))
def _gsheet_catalog(config: dict) -> Catalog:
    token = _saas_token(config)
    sheet_id = config.get("spreadsheet_id") or config.get("sheet_id")
    if sheet_id:
        try:
            r = _rget(f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}",
                       headers=_bearer(token),
                       params={"fields": "sheets(properties(title,sheetId))"})
            sheets = r.json().get("sheets") or []
        except Exception as exc:  # noqa: BLE001
            return _unsupported(f"Sheets API request failed: {exc}")
        items = [CatalogItem(name=(s.get("properties") or {}).get("title", ""),
                              kind="tab", parent=sheet_id,
                              metadata={"id": (s.get("properties") or {}).get("sheetId")})
                  for s in sheets]
        return _build(items)
    # No explicit sheet → list spreadsheets via Drive.
    try:
        r = _rget("https://www.googleapis.com/drive/v3/files",
                   headers=_bearer(token),
                   params={"q": "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
                           "pageSize": 200, "fields": "files(id,name)"})
        files = r.json().get("files") or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"Drive API request failed: {exc}")
    items = [CatalogItem(name=f.get("name", ""), kind="spreadsheet",
                          parent="", metadata={"id": f.get("id")})
              for f in files]
    return _build(items)


# ════════════════════════════════════════════════════════════════════
#                       Integration metadata
# ════════════════════════════════════════════════════════════════════
# These connectors describe pipelines / models, not data tables.
# Already categorised as integration_metadata in catalog.py — here we
# replace the planned placeholder with a real listing.

@register("airbyte", ProviderMeta(
    category="integration_metadata", auth="api_key", tier="tier1",
    notes="lists configured sources/connections, NOT data tables"))
def _airbyte_catalog(config: dict) -> Catalog:
    base = _strip_url(config.get("base_url") or "")
    if not base:
        return _unsupported("Requires 'base_url' (e.g. http://airbyte:8001)")
    headers = _saas_headers(config)
    items: list[CatalogItem] = []
    try:
        # Try Airbyte Cloud / OSS Public API path first.
        for resource in (("sources", "source"), ("connections", "connection")):
            url = f"{base}/api/v1/{resource[0]}/list"
            r = _rpost(url, headers=headers,
                        json={"workspaceId": config.get("workspace_id", "")})
            if r.status_code != 200:
                continue
            for entry in r.json().get(f"{resource[0]}", []) or []:
                items.append(CatalogItem(name=entry.get("name", ""),
                                          kind=resource[1], parent=""))
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"API request failed: {exc}")
    return _build(items)


@register("fivetran", ProviderMeta(
    category="integration_metadata", auth="api_key", tier="tier1",
    notes="lists configured connectors, NOT data tables"))
def _fivetran_catalog(config: dict) -> Catalog:
    api_key = config.get("api_key")
    api_secret = config.get("api_secret")
    if not (api_key and api_secret):
        return _unsupported("Requires 'api_key' and 'api_secret'")
    try:
        r = _rget("https://api.fivetran.com/v1/connectors",
                   auth=(api_key, api_secret), params={"limit": 500})
        body = r.json().get("data") or {}
        connectors = body.get("items") or []
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"API request failed: {exc}")
    items = [CatalogItem(name=c.get("schema", "") or c.get("id", ""),
                          kind="connector", parent="",
                          metadata={"service": c.get("service")})
              for c in connectors]
    return _build(items)


@register("dbt", ProviderMeta(
    category="integration_metadata", auth="none", tier="tier1",
    notes="reads target/manifest.json — lists models and sources"))
def _dbt_catalog(config: dict) -> Catalog:
    """Reads the local manifest.json artifact. Path can be a directory
    (we look for target/manifest.json under it) OR an explicit file."""
    path = config.get("manifest_path") or config.get("path")
    if not path:
        return _unsupported("Requires 'manifest_path' to a project's target/manifest.json")
    if os.path.isdir(path):
        candidate = os.path.join(path, "target", "manifest.json")
        if os.path.isfile(candidate):
            path = candidate
        else:
            return _unsupported(f"No target/manifest.json found under {path}")
    if not os.path.isfile(path):
        return _unsupported(f"manifest.json not found: {path}")
    try:
        import json as _json
        with open(path, encoding="utf-8") as f:
            manifest = _json.load(f)
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"manifest.json read failed: {exc}")
    items: list[CatalogItem] = []
    for unique_id, node in (manifest.get("nodes") or {}).items():
        if not isinstance(node, dict):
            continue
        kind = node.get("resource_type", "model")
        items.append(CatalogItem(
            name=node.get("name", unique_id), kind=kind,
            parent=node.get("schema") or node.get("package_name", ""),
        ))
    for unique_id, src in (manifest.get("sources") or {}).items():
        if not isinstance(src, dict):
            continue
        items.append(CatalogItem(
            name=src.get("name", unique_id), kind="source",
            parent=src.get("source_name", ""),
        ))
    return _build(items)


# ════════════════════════════════════════════════════════════════════
#         Tier 4 — explicit no-catalog with documented reason
# ════════════════════════════════════════════════════════════════════
# These are "real" in the sense that they document WHY they can't
# offer a browsable catalog, rather than returning a generic
# "planned" message. The reason text is permanent guidance.

@register("memcached", ProviderMeta(
    category="search_cache", auth="none", tier="tier4",
    verification="n_a", supports_catalog=False))
def _memcached_catalog(_config: dict) -> Catalog:
    return _unsupported(
        "Memcached has no metadata API — it's a flat key-value cache "
        "with no enumeration primitive. Use specific keys directly."
    )


@register("sap", ProviderMeta(
    category="saas", auth="basic", tier="tier4",
    verification="n_a", supports_catalog=False))
def _sap_catalog(_config: dict) -> Catalog:
    return _unsupported(
        "SAP RFC catalog browsing requires the proprietary SAP NW RFC SDK "
        "(non-pip-installable). For OData-exposed S/4HANA modules, configure "
        "the connection as 'odata' instead."
    )


@register("workday", ProviderMeta(
    category="saas", auth="basic", tier="tier4",
    verification="n_a", supports_catalog=False))
def _workday_catalog(_config: dict) -> Catalog:
    return _unsupported(
        "Workday has no canonical catalog API — its WSDL is custom per tenant "
        "and reports are configured server-side. Build connections against "
        "specific RaaS report URLs as 'rest_api' instead."
    )


@register("custom", ProviderMeta(
    category="other", auth="none", tier="tier4",
    verification="n_a", supports_catalog=False))
def _custom_catalog(_config: dict) -> Catalog:
    return _unsupported(
        "Custom connectors have no schema by definition. Configure browsable "
        "objects manually in the source/sink config."
    )
