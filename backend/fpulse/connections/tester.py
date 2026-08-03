"""Real connectivity testing for all connection types.

Each test method attempts a protocol-level connection, measures latency,
retrieves server metadata where possible, and returns actionable error
messages with fix suggestions on failure.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# Timeout used for all connection attempts (seconds)
DEFAULT_TIMEOUT = 8


def _elapsed_ms(start: float) -> float:
    """Milliseconds since *start*."""
    return round((time.time() - start) * 1000, 1)


def _suggestion_for(error: Exception, conn_type: str) -> str:
    """Return a human-friendly fix suggestion based on the exception."""
    msg = str(error).lower()

    if "login failed" in msg or "18456" in msg:
        return "SQL Server login failed. Verify the username/password are correct, the SQL login exists, and SQL Server allows mixed-mode authentication (not Windows-only). For local SQL Server Express, the 'sa' account is disabled by default."
    if "password" in msg or "authentication" in msg or "access denied" in msg or "auth" in msg:
        return "Check your username and password. Ensure the user has the correct permissions."
    if "connection refused" in msg or "actively refused" in msg:
        return f"The {conn_type} server is not accepting connections on this host:port. Verify the server is running and the port is correct."
    if "timed out" in msg or "timeout" in msg:
        return "Connection timed out. Check firewall rules, network connectivity, and ensure the server is reachable."
    if "name or service not known" in msg or "nodename nor servname" in msg or "getaddrinfo" in msg:
        return "Hostname could not be resolved. Verify the hostname/IP address is correct."
    if "no such file" in msg or "does not exist" in msg:
        return "The specified file or path does not exist. Check the path and permissions."
    if "ssl" in msg or "certificate" in msg or "tls" in msg:
        return "SSL/TLS handshake failed. Check certificate configuration or try disabling SSL verification if appropriate."
    if "permission" in msg or "forbidden" in msg:
        return "Permission denied. Check credentials and ensure the user has access to the requested resource."
    if "does not exist" in msg or "unknown database" in msg:
        return "The specified database does not exist. Verify the database name."

    return f"Verify the {conn_type} connection settings (host, port, credentials) and ensure the server is running."


def _ok(message: str, latency_ms: float, **details) -> dict:
    """Build a success result."""
    return {
        "success": True,
        "message": message,
        "details": {"latency_ms": latency_ms, **details},
        "error": None,
        "suggestion": None,
    }


def _fail(message: str, error: Exception | str, conn_type: str) -> dict:
    """Build a failure result."""
    err_str = str(error)
    suggestion = _suggestion_for(error if isinstance(error, Exception) else Exception(error), conn_type)
    return {
        "success": False,
        "message": message,
        "details": {},
        "error": err_str,
        "suggestion": suggestion,
    }


def _addon_unavailable(connector: str, *, system_requirement: str = "") -> dict:
    """Failure result for a connector whose optional runtime package isn't
    available in this F-Pulse install.

    Worded for a non-developer on a packaged desktop build: those users have
    no shell and the app isn't pip-managed, so "pip install X" is a dead end
    (and reads as a bug). Point them at whoever set F-Pulse up, and — for
    connectors like SQL Server — name the system-level driver that also has
    to be present on the machine.
    """
    detail = (
        f"The {connector} connector needs an add-on that isn't included in "
        f"this F-Pulse install."
    )
    suggestion = f"Ask whoever set up F-Pulse to enable the {connector} connector."
    if system_requirement:
        detail += f" It also needs {system_requirement} installed on this computer."
        suggestion = (
            f"Ask whoever set up F-Pulse to enable the {connector} connector, "
            f"and install {system_requirement} on this computer."
        )
    return {
        "success": False,
        "message": f"The {connector} connector isn't available in this install",
        "details": {},
        "error": detail,
        "suggestion": suggestion,
    }


class ConnectionTester:
    """Real connectivity testing for all connection types."""

    # Map connection type strings to test methods
    _TESTERS: dict[str, str] = {
        "postgresql": "_test_postgresql",
        "mysql": "_test_mysql",
        "mssql": "_test_mssql",
        "sqlite": "_test_sqlite",
        "rest_api": "_test_rest_api",
        # 2026-05-23 (T4): `oracle_api` is the legacy alias of
        # `oracle_fusion`. Legacy rows continue to test via the generic
        # REST prober; new rows pick `oracle_fusion` and get the
        # Fusion-specific tester wired in U1.
        "oracle_api": "_test_rest_api",
        "oracle_fusion": "_test_oracle_fusion",
        "oracle_bip": "_test_oracle_bip",
        "sap_s4hana": "_test_sap_s4hana",
        "sap_successfactors": "_test_sap_successfactors",
        "microsoft_graph": "_test_microsoft_graph",
        # 2026-05-23 (W1): manifest-promoted SaaS now have first-class
        # testers so the "Test connection" button actually exercises
        # the auth path instead of falling back to "URL reachable".
        "github": "_test_github",
        "shopify": "_test_shopify",
        "stripe": "_test_stripe",
        "notion": "_test_notion",
        "asana": "_test_asana",
        "s3": "_test_s3",
        "kafka": "_test_kafka",
        "ftp": "_test_ftp",
        "sftp": "_test_ftp",   # protocol-aware: _test_ftp branches on `protocol`
        "smtp": "_test_smtp",
        "slack": "_test_slack",
        "graphql": "_test_graphql",
        "redis": "_test_redis",
        "mongodb": "_test_mongodb",
        "elasticsearch": "_test_elasticsearch",
    }

    def test_connection(self, conn_type: str, config: dict) -> dict:
        """Test a connection and return detailed results.

        Args:
            conn_type: The connection type string (e.g. "postgresql", "redis").
            config: Merged config dict (connection config + credential config).

        Returns:
            {
                "success": bool,
                "message": str,
                "details": {
                    "latency_ms": float,
                    "server_version": str (if applicable),
                    "database_size": str (if applicable),
                    "tables_count": int (if applicable),
                },
                "error": str | None,
                "suggestion": str | None,
            }
        """
        method_name = self._TESTERS.get(conn_type.lower())
        if not method_name:
            return {
                "success": False,
                "message": f"No tester implemented for connection type '{conn_type}'",
                "details": {},
                "error": f"Unsupported connection type: {conn_type}",
                "suggestion": "Use a supported connection type or configure a custom test.",
            }

        method = getattr(self, method_name)
        try:
            return method(config)
        except Exception as exc:
            logger.exception("Unexpected error testing %s connection", conn_type)
            return _fail(f"Unexpected error testing {conn_type} connection", exc, conn_type)

    # ── PostgreSQL ──

    def _test_postgresql(self, config: dict) -> dict:
        try:
            import psycopg2  # type: ignore
        except ImportError:
            return _addon_unavailable("PostgreSQL")

        host = config.get("host", "localhost")
        port = int(config.get("port", 5432))
        database = config.get("database", "postgres")
        user = config.get("user") or config.get("username", "postgres")
        password = config.get("password", "")
        schema = config.get("schema")

        start = time.time()
        try:
            conn = psycopg2.connect(
                host=host,
                port=port,
                dbname=database,
                user=user,
                password=password,
                connect_timeout=DEFAULT_TIMEOUT,
                options=f"-c search_path={schema}" if schema else None,
            )
            latency = _elapsed_ms(start)

            try:
                cur = conn.cursor()

                # Server version
                cur.execute("SELECT version()")
                version_row = cur.fetchone()
                server_version = version_row[0] if version_row else "unknown"

                # Table count
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')"
                )
                tables_count = cur.fetchone()[0]

                # Database size
                cur.execute(f"SELECT pg_size_pretty(pg_database_size(current_database()))")
                db_size = cur.fetchone()[0]

                return _ok(
                    f"Connected to PostgreSQL at {host}:{port}/{database}",
                    latency,
                    server_version=server_version,
                    tables_count=tables_count,
                    database_size=db_size,
                )
            finally:
                conn.close()

        except Exception as exc:
            return _fail(f"PostgreSQL connection failed to {host}:{port}", exc, "postgresql")

    # ── MySQL ──

    def _test_mysql(self, config: dict) -> dict:
        try:
            import pymysql  # type: ignore
        except ImportError:
            return _addon_unavailable("MySQL")

        host = config.get("host", "localhost")
        port = int(config.get("port", 3306))
        database = config.get("database", "")
        user = config.get("user") or config.get("username", "root")
        password = config.get("password", "")

        start = time.time()
        try:
            conn = pymysql.connect(
                host=host,
                port=port,
                database=database or None,
                user=user,
                password=password,
                connect_timeout=DEFAULT_TIMEOUT,
            )
            latency = _elapsed_ms(start)

            try:
                cur = conn.cursor()

                cur.execute("SELECT VERSION()")
                version_row = cur.fetchone()
                server_version = version_row[0] if version_row else "unknown"

                tables_count = 0
                if database:
                    cur.execute(
                        "SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema = %s", (database,)
                    )
                    tables_count = cur.fetchone()[0]

                return _ok(
                    f"Connected to MySQL at {host}:{port}/{database or '(no db)'}",
                    latency,
                    server_version=server_version,
                    tables_count=tables_count,
                )
            finally:
                conn.close()

        except Exception as exc:
            return _fail(f"MySQL connection failed to {host}:{port}", exc, "mysql")

    # ── MSSQL ──

    def _test_mssql(self, config: dict) -> dict:
        try:
            import pyodbc  # type: ignore
        except ImportError:
            return _addon_unavailable(
                "SQL Server",
                system_requirement="the Microsoft ODBC Driver for SQL Server",
            )

        host = config.get("host", "localhost")
        port = int(config.get("port", 1433))
        database = config.get("database", "master")
        raw_user = config.get("user") or config.get("username")
        password = config.get("password", "")
        # Windows Auth when explicitly opted in OR when no SQL credentials are
        # supplied at all (common for default-install local SQL Server).
        windows_auth = bool(config.get("windows_auth")) or (not raw_user and not password)
        user = raw_user or "sa"

        # Pick the best ODBC driver actually installed on this machine.
        # Order of preference: 18 → 17 → SQL Server Native Client → 13 → SQL Server.
        installed = [d for d in pyodbc.drivers() if "SQL Server" in d]
        preferred_order = [
            "ODBC Driver 18 for SQL Server",
            "ODBC Driver 17 for SQL Server",
            "ODBC Driver 13 for SQL Server",
            "SQL Server Native Client 11.0",
            "SQL Server",
        ]
        driver = next((d for d in preferred_order if d in installed), None) or (installed[0] if installed else None)
        if not driver:
            return _fail(
                "No SQL Server ODBC driver installed",
                "Install Microsoft ODBC Driver 17 or 18 for SQL Server from learn.microsoft.com/sql/connect/odbc",
                "mssql",
            )

        # Driver 18 enforces TLS by default — for local dev on a self-signed
        # SQL Server, allow opting out via TrustServerCertificate.
        trust_cert = "yes" if (config.get("trust_server_certificate") or "18" in driver) else "no"
        encrypt = "yes" if config.get("encrypt") else ("optional" if "18" in driver else "no")

        auth_clause = "Trusted_Connection=yes;" if windows_auth else f"UID={user};PWD={password};"
        conn_str = (
            f"DRIVER={{{driver}}};"
            f"SERVER={host},{port};"
            f"DATABASE={database};"
            f"{auth_clause}"
            f"Encrypt={encrypt};TrustServerCertificate={trust_cert};"
            f"Connection Timeout={DEFAULT_TIMEOUT};"
        )

        start = time.time()
        try:
            conn = pyodbc.connect(conn_str)
            latency = _elapsed_ms(start)

            try:
                cur = conn.cursor()

                cur.execute("SELECT @@VERSION")
                version_row = cur.fetchone()
                server_version = version_row[0] if version_row else "unknown"

                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_type = 'BASE TABLE'"
                )
                tables_count = cur.fetchone()[0]

                return _ok(
                    f"Connected to SQL Server at {host}:{port}/{database}",
                    latency,
                    server_version=server_version,
                    tables_count=tables_count,
                )
            finally:
                conn.close()

        except Exception as exc:
            return _fail(f"MSSQL connection failed to {host}:{port}", exc, "mssql")

    # ── SQLite ──

    def _test_sqlite(self, config: dict) -> dict:
        import sqlite3

        db_path = config.get("database") or config.get("file") or config.get("path")
        if not db_path:
            return _fail(
                "No database file path configured",
                "Provide a 'database' or 'file' path in the connection config",
                "sqlite",
            )

        if db_path != ":memory:" and not os.path.isfile(db_path):
            return _fail(
                f"SQLite file not found: {db_path}",
                f"File does not exist: {db_path}",
                "sqlite",
            )

        start = time.time()
        try:
            conn = sqlite3.connect(db_path, timeout=DEFAULT_TIMEOUT)
            latency = _elapsed_ms(start)

            try:
                cur = conn.cursor()

                # SQLite version
                cur.execute("SELECT sqlite_version()")
                server_version = cur.fetchone()[0]

                # Table count
                cur.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
                tables_count = cur.fetchone()[0]

                # File size
                db_size = "in-memory"
                if db_path != ":memory:" and os.path.isfile(db_path):
                    size_bytes = os.path.getsize(db_path)
                    if size_bytes < 1024:
                        db_size = f"{size_bytes} B"
                    elif size_bytes < 1024 * 1024:
                        db_size = f"{size_bytes / 1024:.1f} KB"
                    elif size_bytes < 1024 * 1024 * 1024:
                        db_size = f"{size_bytes / (1024 * 1024):.1f} MB"
                    else:
                        db_size = f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

                return _ok(
                    f"SQLite database opened: {db_path}",
                    latency,
                    server_version=f"SQLite {server_version}",
                    tables_count=tables_count,
                    database_size=db_size,
                )
            finally:
                conn.close()

        except Exception as exc:
            return _fail(f"SQLite connection failed for {db_path}", exc, "sqlite")

    # ── REST API ──

    def _test_rest_api(self, config: dict) -> dict:
        import base64
        import json as _json
        import ssl
        import urllib.parse
        import urllib.request
        import urllib.error

        base_url = config.get("base_url") or config.get("url") or config.get("endpoint")
        if not base_url:
            return _fail(
                "No base_url configured",
                "Provide a 'base_url' in the connection config",
                "rest_api",
            )

        # Z29 (2026-05-23) — honor every auth_type the frontend offers
        # so a green Test reflects the real production behavior. Previous
        # version only sent Bearer when api_key/token was set, regardless
        # of the picker. That made Basic Auth / OAuth2 / custom-header
        # tests false-negative even when the credentials were correct.
        headers: dict[str, str] = {"User-Agent": "F-Pulse/0.6.0", "Accept": "application/json"}
        auth_type = (config.get("auth_type") or "").strip().lower()

        # Z29 — honor the ssl_verify toggle from the frontend form.
        # Defaults to TRUE (verify) when unset because OAuth2 Client
        # Credentials now flows real client_secret values through this
        # endpoint and a MITM with a forged cert would silently capture
        # them. Users with self-signed staging certs can flip the toggle
        # off explicitly. The probe call (final base_url GET) honors the
        # same flag so the user's toggle has a single, predictable effect.
        ssl_verify_raw = config.get("ssl_verify", True)
        if isinstance(ssl_verify_raw, str):
            ssl_verify = ssl_verify_raw.strip().lower() not in ("false", "0", "no", "off")
        else:
            ssl_verify = bool(ssl_verify_raw)
        if ssl_verify:
            ssl_ctx = ssl.create_default_context()
        else:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        # Back-compat: if auth_type is unset but token/api_key is set,
        # treat as Bearer (the historical default).
        if not auth_type:
            if config.get("token") or config.get("api_key"):
                auth_type = "bearer"
            else:
                auth_type = "none"

        try:
            if auth_type == "bearer":
                tok = config.get("token") or config.get("api_key")
                if tok:
                    headers["Authorization"] = f"Bearer {tok}"
            elif auth_type == "api_key":
                tok = config.get("token") or config.get("api_key")
                header_name = (config.get("api_key_header") or "Authorization").strip() or "Authorization"
                if tok:
                    headers[header_name] = tok
            elif auth_type == "api_key_query":
                tok = config.get("token") or config.get("api_key")
                param = (config.get("api_key_param") or "api_key").strip() or "api_key"
                if tok:
                    sep = "&" if "?" in base_url else "?"
                    base_url = f"{base_url}{sep}{urllib.parse.quote(param)}={urllib.parse.quote(tok)}"
            elif auth_type == "basic":
                user = config.get("username") or ""
                pw = config.get("password") or ""
                if user or pw:
                    raw = f"{user}:{pw}".encode("utf-8")
                    headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
            elif auth_type == "oauth2_cc":
                token_url = (config.get("token_url") or "").strip()
                client_id = config.get("client_id") or ""
                client_secret = config.get("client_secret") or ""
                scope = (config.get("oauth_scope") or "").strip()
                if not token_url or not client_id:
                    return _fail(
                        "OAuth 2.0 (Client Credentials) misconfigured",
                        "token_url and client_id are required",
                        "rest_api",
                    )
                # Fetch the bearer token from the OAuth2 token endpoint.
                form = {
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                }
                if scope:
                    form["scope"] = scope
                body = urllib.parse.urlencode(form).encode("utf-8")
                tok_req = urllib.request.Request(
                    token_url,
                    method="POST",
                    data=body,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json",
                    },
                )
                try:
                    # Z29 — uses the user's ssl_verify toggle (defaults
                    # to True). Sending client_secret with TLS verification
                    # disabled is a credential-leak risk; the operator must
                    # explicitly opt in.
                    tok_resp = urllib.request.urlopen(tok_req, timeout=DEFAULT_TIMEOUT, context=ssl_ctx)
                    tok_data = _json.loads(tok_resp.read().decode("utf-8") or "{}")
                except urllib.error.HTTPError as exc:
                    return _fail(
                        f"OAuth 2.0 token request returned HTTP {exc.code}: {exc.reason}",
                        exc,
                        "rest_api",
                    )
                except urllib.error.URLError as exc:
                    return _fail(f"Cannot reach OAuth 2.0 token URL {token_url}", exc, "rest_api")
                access = tok_data.get("access_token")
                if not access:
                    return _fail(
                        "OAuth 2.0 token endpoint did not return access_token",
                        f"Got keys: {sorted(tok_data.keys())}",
                        "rest_api",
                    )
                headers["Authorization"] = f"Bearer {access}"
            elif auth_type == "custom_header":
                name = (config.get("custom_header_name") or "").strip()
                value = config.get("custom_header_value") or ""
                if name and value:
                    headers[name] = value
            # auth_type == "none" → no auth headers added

            # Extra free-form headers (always available, layered on top
            # of whichever auth mode was picked).
            extra = config.get("headers") or ""
            if isinstance(extra, str) and extra.strip():
                for line in extra.splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        k, v = k.strip(), v.strip()
                        if k:
                            headers[k] = v
        except Exception as exc:
            return _fail("Auth setup failed", exc, "rest_api")

        start = time.time()
        try:
            req = urllib.request.Request(base_url, method="GET", headers=headers)
            # Z29 — honor ssl_verify (was unconditionally disabled).
            # The user's toggle now has a real effect on the probe.
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT, context=ssl_ctx)
            latency = _elapsed_ms(start)

            content_type = resp.headers.get("Content-Type", "unknown")
            server = resp.headers.get("Server", "unknown")

            return _ok(
                f"REST API reachable: {base_url} (HTTP {resp.status})",
                latency,
                http_status=resp.status,
                content_type=content_type,
                server=server,
            )
        except urllib.error.HTTPError as exc:
            latency = _elapsed_ms(start)
            if exc.code in (401, 403):
                return _ok(
                    f"REST API reachable (HTTP {exc.code} — auth required): {base_url}",
                    latency,
                    http_status=exc.code,
                    note="Server is reachable but requires authentication",
                )
            if exc.code == 405:
                return _ok(
                    f"REST API reachable (HTTP 405 — method not allowed, but server responded): {base_url}",
                    latency,
                    http_status=405,
                )
            return _fail(f"REST API returned HTTP {exc.code}: {exc.reason}", exc, "rest_api")
        except urllib.error.URLError as exc:
            return _fail(f"Cannot reach REST API at {base_url}", exc, "rest_api")
        except Exception as exc:
            return _fail(f"REST API test failed for {base_url}", exc, "rest_api")

    # ── S3 / MinIO ──

    def _test_s3(self, config: dict) -> dict:
        try:
            import boto3  # type: ignore
            from botocore.exceptions import ClientError, NoCredentialsError, EndpointConnectionError  # type: ignore
        except ImportError:
            return _addon_unavailable("S3")

        endpoint_url = config.get("endpoint") or config.get("endpoint_url")
        region = config.get("region", "us-east-1")
        access_key = config.get("access_key") or config.get("aws_access_key_id")
        secret_key = config.get("secret_key") or config.get("aws_secret_access_key")
        bucket = config.get("bucket")

        kwargs: dict[str, Any] = {
            "service_name": "s3",
            "region_name": region,
        }
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key

        start = time.time()
        try:
            client = boto3.client(**kwargs)

            if bucket:
                # Test specific bucket
                client.head_bucket(Bucket=bucket)
                latency = _elapsed_ms(start)
                return _ok(
                    f"S3 bucket '{bucket}' is accessible",
                    latency,
                    bucket=bucket,
                    endpoint=endpoint_url or f"s3.{region}.amazonaws.com",
                )
            else:
                # List buckets
                response = client.list_buckets()
                latency = _elapsed_ms(start)
                buckets = [b["Name"] for b in response.get("Buckets", [])]
                return _ok(
                    f"S3 connected — {len(buckets)} bucket(s) found",
                    latency,
                    bucket_count=len(buckets),
                    buckets=buckets[:10],  # first 10
                    endpoint=endpoint_url or f"s3.{region}.amazonaws.com",
                )

        except NoCredentialsError:
            return _fail(
                "No AWS credentials found",
                "Provide access_key and secret_key, or configure AWS credentials (env vars / ~/.aws/credentials)",
                "s3",
            )
        except EndpointConnectionError as exc:
            return _fail(f"Cannot reach S3 endpoint", exc, "s3")
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("403", "AccessDenied"):
                latency = _elapsed_ms(start)
                return _ok(
                    f"S3 endpoint reachable (403 — credentials lack permission for this operation)",
                    latency,
                    note="Server reachable but access denied. Check IAM permissions.",
                )
            return _fail(f"S3 error: {error_code}", exc, "s3")
        except Exception as exc:
            return _fail("S3 connection failed", exc, "s3")

    # ── Kafka ──

    def _test_kafka(self, config: dict) -> dict:
        # Try confluent_kafka first, then kafka-python
        brokers = config.get("brokers") or config.get("bootstrap_servers") or config.get("host", "localhost:9092")
        if isinstance(brokers, list):
            brokers = ",".join(brokers)

        # Try confluent_kafka
        try:
            from confluent_kafka.admin import AdminClient  # type: ignore

            start = time.time()
            admin = AdminClient({"bootstrap.servers": brokers, "socket.timeout.ms": DEFAULT_TIMEOUT * 1000})
            metadata = admin.list_topics(timeout=DEFAULT_TIMEOUT)
            latency = _elapsed_ms(start)

            topics = list(metadata.topics.keys())
            broker_count = len(metadata.brokers)

            return _ok(
                f"Kafka connected at {brokers}",
                latency,
                broker_count=broker_count,
                topic_count=len(topics),
                topics=topics[:20],
            )
        except ImportError:
            pass
        except Exception as exc:
            return _fail(f"Kafka connection failed to {brokers}", exc, "kafka")

        # Fallback: kafka-python
        try:
            from kafka import KafkaConsumer  # type: ignore

            start = time.time()
            consumer = KafkaConsumer(
                bootstrap_servers=brokers.split(","),
                request_timeout_ms=DEFAULT_TIMEOUT * 1000,
                api_version_auto_timeout_ms=DEFAULT_TIMEOUT * 1000,
            )
            latency = _elapsed_ms(start)

            topics = list(consumer.topics())
            consumer.close()

            return _ok(
                f"Kafka connected at {brokers}",
                latency,
                topic_count=len(topics),
                topics=topics[:20],
            )
        except ImportError:
            return _addon_unavailable("Kafka")
        except Exception as exc:
            return _fail(f"Kafka connection failed to {brokers}", exc, "kafka")

    # ── FTP ──

    def _test_ftp(self, config: dict) -> dict:
        # 2026-06-17 — protocol-aware. Previously this was ftplib-only and
        # ignored the `protocol` field, so an SFTP server (SSH/:22) was probed
        # over plaintext FTP and reported a FALSE failure even though the
        # pipeline node connects fine, and FTPS tested as plaintext FTP. Now
        # we resolve the protocol the same way FtpSourceNode does.
        host = config.get("host", "localhost")
        user = config.get("user") or config.get("username", "anonymous")
        password = config.get("password", "")
        private_key = config.get("private_key", "")
        protocol = str(config.get("protocol") or "").strip().lower()
        if not protocol:
            protocol = "ftps" if (config.get("tls") or config.get("ftps")) else "ftp"
        port = int(config.get("port", 0) or 0)
        if port == 0:
            port = 22 if protocol == "sftp" else 21
        elif protocol == "sftp" and port == 21:
            port = 22  # 21 is the FTP default; auto-correct for SFTP

        if protocol == "sftp":
            return self._test_sftp(host, port, user, password, private_key)

        import ftplib
        start = time.time()
        try:
            ftp = ftplib.FTP_TLS() if protocol == "ftps" else ftplib.FTP()
            ftp.connect(host, port, timeout=DEFAULT_TIMEOUT)
            ftp.login(user, password)
            latency = _elapsed_ms(start)
            if protocol == "ftps" and isinstance(ftp, ftplib.FTP_TLS):
                ftp.prot_p()  # switch to secure data connection
            files = ftp.nlst()
            welcome = ftp.getwelcome()
            ftp.quit()
            return _ok(
                f"{protocol.upper()} connected to {host}:{port}",
                latency,
                server_version=welcome,
                files_in_root=len(files),
            )
        except ftplib.error_perm as exc:
            return _fail(f"{protocol.upper()} permission error at {host}:{port}", exc, protocol)
        except Exception as exc:
            return _fail(f"{protocol.upper()} connection failed to {host}:{port}", exc, protocol)

    def _test_sftp(self, host: str, port: int, user: str, password: str, private_key: str) -> dict:
        """SFTP (SSH) connectivity test — mirrors FtpSourceNode's paramiko path
        (password or PEM private key). Lazy-imports paramiko with a clear hint."""
        try:
            import paramiko
        except ImportError as exc:
            return _addon_unavailable("SFTP")
        start = time.time()
        ssh = None
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            pkey = None
            if private_key:
                from io import StringIO
                for kcls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
                    try:
                        pkey = kcls.from_private_key(StringIO(str(private_key)))
                        break
                    except Exception:  # noqa: BLE001 — try the next key type
                        continue
            ssh.connect(
                hostname=host, port=port, username=(user or None),
                password=(password or None) if not pkey else None,
                pkey=pkey, timeout=DEFAULT_TIMEOUT,
                allow_agent=False, look_for_keys=False,
            )
            latency = _elapsed_ms(start)
            sftp = ssh.open_sftp()
            files = sftp.listdir(".")
            sftp.close()
            return _ok(
                f"SFTP connected to {host}:{port}",
                latency,
                files_in_root=len(files),
            )
        except Exception as exc:
            return _fail(f"SFTP connection failed to {host}:{port}", exc, "sftp")
        finally:
            if ssh is not None:
                try:
                    ssh.close()
                except Exception:  # noqa: BLE001
                    pass

    # ── SMTP ──

    def _test_smtp(self, config: dict) -> dict:
        import smtplib

        host = config.get("host", "localhost")
        port = int(config.get("port", 587))
        user = config.get("user") or config.get("username")
        password = config.get("password")
        use_tls = config.get("tls", True) or config.get("starttls", True)

        start = time.time()
        try:
            if port == 465:
                # SMTPS (implicit TLS)
                server = smtplib.SMTP_SSL(host, port, timeout=DEFAULT_TIMEOUT)
            else:
                server = smtplib.SMTP(host, port, timeout=DEFAULT_TIMEOUT)

            latency = _elapsed_ms(start)

            ehlo_code, ehlo_msg = server.ehlo()

            if use_tls and port != 465:
                server.starttls()

            server_info = ehlo_msg.decode("utf-8", errors="replace") if isinstance(ehlo_msg, bytes) else str(ehlo_msg)

            if user and password:
                server.login(user, password)

            server.quit()

            return _ok(
                f"SMTP server connected at {host}:{port}",
                latency,
                server_version=server_info.split("\n")[0] if server_info else "unknown",
                tls_enabled=use_tls or port == 465,
                authenticated=bool(user and password),
            )

        except smtplib.SMTPAuthenticationError as exc:
            return _fail(f"SMTP authentication failed at {host}:{port}", exc, "smtp")
        except Exception as exc:
            return _fail(f"SMTP connection failed to {host}:{port}", exc, "smtp")

    # ── Slack Webhook ──

    def _test_slack(self, config: dict) -> dict:
        import json
        import urllib.request
        import urllib.error

        webhook_url = config.get("webhook_url") or config.get("url")
        if not webhook_url:
            return _fail(
                "No webhook_url configured",
                "Provide a 'webhook_url' in the connection config",
                "slack",
            )

        payload = json.dumps({"text": "F-Pulse connection test — this message confirms your Slack webhook is working."}).encode("utf-8")

        start = time.time()
        try:
            req = urllib.request.Request(
                webhook_url,
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT)
            latency = _elapsed_ms(start)

            body = resp.read().decode("utf-8")
            if body == "ok" or resp.status == 200:
                return _ok(
                    "Slack webhook is working — test message sent",
                    latency,
                    http_status=resp.status,
                )
            return _ok(
                f"Slack webhook responded (HTTP {resp.status})",
                latency,
                http_status=resp.status,
                response_body=body[:200],
            )

        except urllib.error.HTTPError as exc:
            return _fail(f"Slack webhook returned HTTP {exc.code}", exc, "slack")
        except Exception as exc:
            return _fail("Slack webhook test failed", exc, "slack")

    # ── GraphQL ──

    def _test_graphql(self, config: dict) -> dict:
        import json
        import ssl
        import urllib.request
        import urllib.error

        url = config.get("base_url") or config.get("url") or config.get("endpoint")
        if not url:
            return _fail(
                "No GraphQL endpoint configured",
                "Provide a 'base_url' or 'endpoint' in the connection config",
                "graphql",
            )

        # Introspection query
        introspection = json.dumps({
            "query": "{ __schema { queryType { name } types { name } } }"
        }).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "F-Pulse/0.6.0",
        }
        api_key = config.get("api_key") or config.get("token")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        start = time.time()
        try:
            req = urllib.request.Request(url, data=introspection, method="POST", headers=headers)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT, context=ctx)
            latency = _elapsed_ms(start)

            body = json.loads(resp.read().decode("utf-8"))
            schema_data = body.get("data", {}).get("__schema", {})
            type_count = len(schema_data.get("types", []))
            query_type = schema_data.get("queryType", {}).get("name", "unknown")

            return _ok(
                f"GraphQL endpoint reachable: {url}",
                latency,
                query_type=query_type,
                type_count=type_count,
            )

        except urllib.error.HTTPError as exc:
            latency = _elapsed_ms(start)
            if exc.code in (401, 403):
                return _ok(
                    f"GraphQL endpoint reachable (HTTP {exc.code} — auth required): {url}",
                    latency,
                    http_status=exc.code,
                )
            return _fail(f"GraphQL endpoint returned HTTP {exc.code}", exc, "graphql")
        except Exception as exc:
            return _fail(f"GraphQL connection failed to {url}", exc, "graphql")

    # ── Microsoft Graph (2026-05-22) ──

    def _test_microsoft_graph(self, config: dict) -> dict:
        """Test a Microsoft Graph connection end-to-end.

        Flow:
          1. Acquire a client-credentials access token from
             ``https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token``
             with the configured scope (default
             ``https://graph.microsoft.com/.default``).
          2. Call ``GET {base_url}/organization`` — the canonical
             "is this Graph reachable with this tenant" probe.
             Falls back to ``GET {base_url}/users?$top=1`` if
             ``/organization`` returns 403 (some tenants restrict
             Directory.Read.All but allow User.Read.All).

        Returns success on a 200 response from either probe;
        surfaces the AAD error description verbatim on failure so
        operators can copy/paste it into the Azure portal.
        """
        import json
        import urllib.parse
        import urllib.request
        import urllib.error

        tenant_id = (config.get("tenant_id") or "common").strip() or "common"
        client_id = (config.get("client_id") or "").strip()
        client_secret = (config.get("client_secret") or "").strip()
        scope = (config.get("scope") or "https://graph.microsoft.com/.default").strip()
        base_url = (config.get("base_url") or "https://graph.microsoft.com/v1.0").rstrip("/")

        if not client_id or not client_secret:
            return _fail(
                "Microsoft Graph: client_id + client_secret are required",
                "Create an Azure App Registration, generate a client secret, "
                "and paste both into the connection.",
                "microsoft_graph",
            )

        # 1) Acquire token (client_credentials grant).
        token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        body = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
            "grant_type": "client_credentials",
        }).encode("utf-8")

        start = time.time()
        try:
            req = urllib.request.Request(
                token_url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "F-Pulse/1.0",
                    "Accept": "application/json",
                },
            )
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT)
            token_payload = json.loads(resp.read().decode("utf-8"))
            access_token = token_payload.get("access_token")
            if not access_token:
                return _fail(
                    "Microsoft Graph: token endpoint returned no access_token",
                    f"Response keys: {sorted(token_payload.keys())}",
                    "microsoft_graph",
                )
        except urllib.error.HTTPError as exc:
            # AAD returns useful JSON error bodies — surface them.
            try:
                detail = exc.read().decode("utf-8")
                err = json.loads(detail)
                msg = err.get("error_description") or err.get("error") or detail
            except Exception:
                msg = exc.reason or "HTTP error"
            return _fail(
                f"Microsoft Graph: token request failed (HTTP {exc.code})",
                str(msg)[:500],
                "microsoft_graph",
            )
        except Exception as exc:
            return _fail(
                f"Microsoft Graph: could not reach {token_url}",
                exc,
                "microsoft_graph",
            )

        # 2) Probe Graph itself. Try /organization first because it
        #    returns the tenant display name (nice UX signal). If the
        #    app permissions don't include Directory.Read.All but DO
        #    include User.Read.All, fall back to /users?$top=1.
        def _graph_probe(path: str):
            url = f"{base_url}{path}"
            req = urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "User-Agent": "F-Pulse/1.0",
                },
            )
            return urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT)

        try:
            resp = _graph_probe("/organization")
            latency = _elapsed_ms(start)
            payload = json.loads(resp.read().decode("utf-8"))
            org = (payload.get("value") or [{}])[0]
            tenant_name = org.get("displayName") or "(unnamed tenant)"
            verified = org.get("verifiedDomains") or []
            domain = next((d.get("name") for d in verified if d.get("isDefault")), None)
            return _ok(
                f"Microsoft Graph reachable for tenant {tenant_name}",
                latency,
                tenant_name=tenant_name,
                tenant_id=tenant_id,
                default_domain=domain,
                probe="/organization",
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                # Fall back to /users probe — many tenants give
                # User.Read.All without Directory.Read.All.
                try:
                    resp = _graph_probe("/users?$top=1")
                    latency = _elapsed_ms(start)
                    return _ok(
                        f"Microsoft Graph reachable (Directory.Read.All missing; users probe ok)",
                        latency,
                        tenant_id=tenant_id,
                        probe="/users?$top=1",
                    )
                except Exception as inner:
                    return _fail(
                        "Microsoft Graph: token issued but probes failed (403 on /organization and /users)",
                        f"Grant the app at least User.Read.All in Azure → App registrations "
                        f"→ API permissions, then click 'Grant admin consent'. Inner: {inner}",
                        "microsoft_graph",
                    )
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                detail = exc.reason or "HTTP error"
            return _fail(
                f"Microsoft Graph: probe failed (HTTP {exc.code})",
                detail,
                "microsoft_graph",
            )
        except Exception as exc:
            return _fail(
                f"Microsoft Graph: could not reach {base_url}",
                exc,
                "microsoft_graph",
            )

    # ── Oracle Fusion Cloud (REST + Basic) ──

    def _test_oracle_fusion(self, config: dict) -> dict:
        """Probe Oracle Fusion Cloud's REST API.

        Oracle Fusion exposes REST under ``/fscmRestApi/resources/{ver}/{entity}``
        for SCM and ``/hcmRestApi/resources/{ver}/{entity}`` for HCM. The
        canonical "is this reachable" probe is GET on the api-versions
        endpoint which returns the supported versions list — that's
        bound to a user-agnostic surface so it survives missing role
        grants.

        Config:
          base_url      e.g. https://my-pod.fa.us2.oraclecloud.com  (required)
          username      Fusion user (required)
          password      Fusion password (required)
          api_family    optional — 'fscm' (default) | 'hcm' | 'crm'
        """
        import base64
        import json as _json
        import ssl
        import urllib.error
        import urllib.request

        base_url = (config.get("base_url") or "").rstrip("/")
        username = config.get("username") or config.get("user") or ""
        password = config.get("password") or ""
        api_family = (config.get("api_family") or "fscm").lower()

        if not base_url:
            return _fail(
                "Oracle Fusion: base_url is required",
                "Provide your Fusion pod URL (e.g. https://my-pod.fa.us2.oraclecloud.com).",
                "oracle_fusion",
            )
        if not username or not password:
            return _fail(
                "Oracle Fusion: username and password are required",
                "Use a service account with REST API access.",
                "oracle_fusion",
            )

        family_root = {
            "fscm": "fscmRestApi", "hcm": "hcmRestApi", "crm": "crmRestApi",
        }.get(api_family, "fscmRestApi")
        url = f"{base_url}/{family_root}/resources/latest"

        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        start = time.time()
        try:
            req = urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Basic {token}",
                    "Accept": "application/json",
                    "User-Agent": "F-Pulse/1.0",
                },
            )
            ctx = ssl.create_default_context()
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT, context=ctx)
            latency = _elapsed_ms(start)
            body = resp.read().decode("utf-8", errors="replace")
            try:
                payload = _json.loads(body)
                items = payload.get("items") if isinstance(payload, dict) else None
                resource_count = len(items) if isinstance(items, list) else None
            except Exception:
                resource_count = None
            return _ok(
                f"Oracle Fusion reachable at {base_url} ({family_root})",
                latency,
                api_family=api_family,
                resource_count=resource_count,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return _fail(
                    f"Oracle Fusion: authentication failed (HTTP {exc.code})",
                    "Verify the username/password and that the user has REST API access.",
                    "oracle_fusion",
                )
            return _fail(
                f"Oracle Fusion: probe failed (HTTP {exc.code})",
                exc, "oracle_fusion",
            )
        except urllib.error.URLError as exc:
            return _fail(
                f"Oracle Fusion: cannot reach {url}",
                exc, "oracle_fusion",
            )
        except Exception as exc:
            return _fail(
                f"Oracle Fusion: probe failed for {url}",
                exc, "oracle_fusion",
            )

    # ── Oracle BI Publisher (Report API) ──

    def _test_oracle_bip(self, config: dict) -> dict:
        """Probe Oracle BI Publisher's report catalog.

        BIP exposes ``/xmlpserver/services/rest/v1/catalog/folders`` as
        the root catalog listing. Hitting it with basic auth confirms
        the server is reachable + credentials are valid + the user has
        catalog browse rights — all three conditions a BIP report run
        requires.

        Config:
          base_url    e.g. https://bipublisher.example.com  (required)
          username    BIP user (required)
          password    BIP password (required)
        """
        import base64
        import ssl
        import urllib.error
        import urllib.request

        base_url = (config.get("base_url") or "").rstrip("/")
        username = config.get("username") or config.get("user") or ""
        password = config.get("password") or ""

        if not base_url:
            return _fail(
                "Oracle BI Publisher: base_url is required",
                "Provide your BI Publisher server URL.",
                "oracle_bip",
            )
        if not username or not password:
            return _fail(
                "Oracle BI Publisher: username and password are required",
                "Use a BIP account with at least Report Consumer role.",
                "oracle_bip",
            )

        url = f"{base_url}/xmlpserver/services/rest/v1/catalog/folders"
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        start = time.time()
        try:
            req = urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Basic {token}",
                    "Accept": "application/json",
                    "User-Agent": "F-Pulse/1.0",
                },
            )
            ctx = ssl.create_default_context()
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT, context=ctx)
            latency = _elapsed_ms(start)
            return _ok(
                f"Oracle BI Publisher reachable at {base_url}",
                latency,
                http_status=resp.status,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return _fail(
                    f"Oracle BI Publisher: authentication failed (HTTP {exc.code})",
                    "Verify the credentials and BIP role assignment.",
                    "oracle_bip",
                )
            if exc.code == 404:
                return _fail(
                    "Oracle BI Publisher: catalog endpoint returned 404",
                    "Confirm BIP REST API is enabled and the URL path is correct.",
                    "oracle_bip",
                )
            return _fail(
                f"Oracle BI Publisher: probe failed (HTTP {exc.code})",
                exc, "oracle_bip",
            )
        except urllib.error.URLError as exc:
            return _fail(
                f"Oracle BI Publisher: cannot reach {url}", exc, "oracle_bip",
            )
        except Exception as exc:
            return _fail(
                f"Oracle BI Publisher: probe failed for {url}", exc, "oracle_bip",
            )

    # ── SAP S/4HANA (OData v2/v4) ──

    def _test_sap_s4hana(self, config: dict) -> dict:
        """Probe SAP S/4HANA via the OData service catalog.

        S/4HANA exposes the OData service catalog at ``/sap/opu/odata/sap/``.
        Hitting it with basic auth confirms reachability + credentials +
        SAP-client routing in one round trip. The same plumbing
        works for both the v2 and v4 services that S/4HANA exposes —
        the catalog endpoint is shared.

        Config:
          base_url      e.g. https://s4hana.example.com  (required)
          username      SAP user (required)
          password      SAP password (required)
          sap_client    Client number, e.g. 100 (optional)
          odata_version "v2" (default) | "v4"  (informational only —
                        doesn't affect this probe)
        """
        import base64
        import ssl
        import urllib.error
        import urllib.parse
        import urllib.request

        base_url = (config.get("base_url") or "").rstrip("/")
        username = config.get("username") or config.get("user") or ""
        password = config.get("password") or ""
        sap_client = (config.get("sap_client") or config.get("client") or "").strip()

        if not base_url:
            return _fail(
                "SAP S/4HANA: base_url is required",
                "Provide your S/4HANA gateway URL (https://host[:port]).",
                "sap_s4hana",
            )
        if not username or not password:
            return _fail(
                "SAP S/4HANA: username and password are required",
                "Use a service account with /IWFND/SU* role granting OData access.",
                "sap_s4hana",
            )

        params = {"$format": "json"}
        if sap_client:
            params["sap-client"] = sap_client
        url = f"{base_url}/sap/opu/odata/sap/?{urllib.parse.urlencode(params)}"
        token = base64.b64encode(f"{username}:{password}".encode()).decode()

        start = time.time()
        try:
            req = urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Basic {token}",
                    "Accept": "application/json",
                    "User-Agent": "F-Pulse/1.0",
                },
            )
            ctx = ssl.create_default_context()
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT, context=ctx)
            latency = _elapsed_ms(start)
            return _ok(
                f"SAP S/4HANA reachable at {base_url}",
                latency,
                sap_client=sap_client or None,
                odata_version=config.get("odata_version", "v2"),
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return _fail(
                    f"SAP S/4HANA: authentication failed (HTTP {exc.code})",
                    "Check user/password and that sap-client matches the tenant.",
                    "sap_s4hana",
                )
            return _fail(
                f"SAP S/4HANA: probe failed (HTTP {exc.code})", exc, "sap_s4hana",
            )
        except urllib.error.URLError as exc:
            return _fail(
                f"SAP S/4HANA: cannot reach {base_url}", exc, "sap_s4hana",
            )
        except Exception as exc:
            return _fail(
                f"SAP S/4HANA: probe failed for {url}", exc, "sap_s4hana",
            )

    # ── SAP SuccessFactors (OData) ──

    def _test_sap_successfactors(self, config: dict) -> dict:
        """Probe SAP SuccessFactors via the OData v2 metadata endpoint.

        SuccessFactors hosts at ``api{N}.successfactors.{eu|com}`` per
        datacenter. The user picks their datacenter URL; we hit
        ``/odata/v2/User?$top=1`` with basic auth (or OAuth2 when a
        token is supplied) and surface the dataCenter / company_id
        echo as the success signal.

        Config:
          base_url   e.g. https://api4.successfactors.com  (required)
          company_id Tenant (required for SF login)
          username   SF username (required)
          password   SF password (required)
        """
        import base64
        import ssl
        import urllib.error
        import urllib.request

        base_url = (config.get("base_url") or "").rstrip("/")
        company_id = (config.get("company_id") or "").strip()
        username = config.get("username") or config.get("user") or ""
        password = config.get("password") or ""

        if not base_url:
            return _fail(
                "SAP SuccessFactors: base_url is required",
                "Provide your datacenter URL (e.g. https://api4.successfactors.com).",
                "sap_successfactors",
            )
        if not company_id:
            return _fail(
                "SAP SuccessFactors: company_id is required",
                "Your SF tenant/company identifier.",
                "sap_successfactors",
            )
        if not username or not password:
            return _fail(
                "SAP SuccessFactors: username and password are required",
                "Use an API user; the SF login format is <user>@<company_id>.",
                "sap_successfactors",
            )

        # SF authenticates as <user>@<company_id> with basic auth on OData.
        sf_user = f"{username}@{company_id}" if "@" not in username else username
        token = base64.b64encode(f"{sf_user}:{password}".encode()).decode()
        url = f"{base_url}/odata/v2/User?$top=1&$format=json"

        start = time.time()
        try:
            req = urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Basic {token}",
                    "Accept": "application/json",
                    "User-Agent": "F-Pulse/1.0",
                },
            )
            ctx = ssl.create_default_context()
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT, context=ctx)
            latency = _elapsed_ms(start)
            return _ok(
                f"SAP SuccessFactors reachable at {base_url}",
                latency,
                company_id=company_id,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return _fail(
                    f"SAP SuccessFactors: authentication failed (HTTP {exc.code})",
                    "Verify username@company_id format and API permissions.",
                    "sap_successfactors",
                )
            return _fail(
                f"SAP SuccessFactors: probe failed (HTTP {exc.code})",
                exc, "sap_successfactors",
            )
        except urllib.error.URLError as exc:
            return _fail(
                f"SAP SuccessFactors: cannot reach {base_url}",
                exc, "sap_successfactors",
            )
        except Exception as exc:
            return _fail(
                f"SAP SuccessFactors: probe failed for {url}",
                exc, "sap_successfactors",
            )

    # ── GitHub (PAT, REST v3) ──

    def _test_github(self, config: dict) -> dict:
        """GET /user — the canonical "is this PAT valid" probe."""
        import json as _json
        import urllib.error
        import urllib.request

        token = (config.get("personal_access_token")
                 or config.get("access_token")
                 or config.get("token") or "").strip()
        if not token:
            return _fail(
                "GitHub: personal_access_token is required",
                "Create a PAT at github.com/settings/tokens with `repo` scope.",
                "github",
            )

        url = "https://api.github.com/user"
        start = time.time()
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "F-Pulse/1.0",
                },
            )
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT)
            payload = _json.loads(resp.read().decode("utf-8", errors="replace"))
            latency = _elapsed_ms(start)
            return _ok(
                f"GitHub PAT valid (user: {payload.get('login', '?')})",
                latency,
                login=payload.get("login"),
                scopes=resp.headers.get("X-OAuth-Scopes"),
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return _fail(
                    "GitHub: PAT rejected (HTTP 401)",
                    "The token is invalid or expired.",
                    "github",
                )
            return _fail(
                f"GitHub: probe failed (HTTP {exc.code})", exc, "github",
            )
        except Exception as exc:
            return _fail("GitHub: probe failed", exc, "github")

    # ── Shopify (Admin API access token) ──

    def _test_shopify(self, config: dict) -> dict:
        """GET /admin/api/2024-01/shop.json — proves shop + token pair."""
        import json as _json
        import urllib.error
        import urllib.request

        shop = (config.get("shop") or "").strip().lower()
        access_token = (config.get("access_token") or "").strip()
        if not shop or not access_token:
            return _fail(
                "Shopify: shop + access_token are required",
                "shop is the subdomain (e.g. mystore for mystore.myshopify.com).",
                "shopify",
            )
        # Strip ".myshopify.com" if the user pasted the full host.
        if ".myshopify.com" in shop:
            shop = shop.split(".myshopify.com", 1)[0]

        url = f"https://{shop}.myshopify.com/admin/api/2024-01/shop.json"
        start = time.time()
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "X-Shopify-Access-Token": access_token,
                    "Accept": "application/json",
                    "User-Agent": "F-Pulse/1.0",
                },
            )
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT)
            payload = _json.loads(resp.read().decode("utf-8", errors="replace"))
            latency = _elapsed_ms(start)
            return _ok(
                f"Shopify shop reachable ({payload.get('shop', {}).get('name', shop)})",
                latency,
                shop=shop,
                plan=(payload.get("shop") or {}).get("plan_name"),
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return _fail(
                    f"Shopify: access token rejected (HTTP {exc.code})",
                    "Verify the token has the required Admin API scopes.",
                    "shopify",
                )
            return _fail(
                f"Shopify: probe failed (HTTP {exc.code})", exc, "shopify",
            )
        except Exception as exc:
            return _fail(f"Shopify: probe failed for {shop}", exc, "shopify")

    # ── Stripe (Secret key, REST v1) ──

    def _test_stripe(self, config: dict) -> dict:
        """GET /v1/account — universally accessible with any sk_ key."""
        import json as _json
        import urllib.error
        import urllib.request

        api_key = (config.get("api_key") or config.get("secret_key") or "").strip()
        if not api_key:
            return _fail(
                "Stripe: api_key is required",
                "Use a sk_live_… or sk_test_… secret key (not a publishable key).",
                "stripe",
            )

        url = "https://api.stripe.com/v1/account"
        start = time.time()
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "User-Agent": "F-Pulse/1.0",
                },
            )
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT)
            payload = _json.loads(resp.read().decode("utf-8", errors="replace"))
            latency = _elapsed_ms(start)
            is_live = api_key.startswith("sk_live_")
            return _ok(
                f"Stripe account reachable ({'live' if is_live else 'test'} mode)",
                latency,
                mode="live" if is_live else "test",
                country=payload.get("country"),
                business_name=(payload.get("business_profile") or {}).get("name"),
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return _fail(
                    "Stripe: API key rejected (HTTP 401)",
                    "The secret key is invalid or revoked.",
                    "stripe",
                )
            return _fail(
                f"Stripe: probe failed (HTTP {exc.code})", exc, "stripe",
            )
        except Exception as exc:
            return _fail("Stripe: probe failed", exc, "stripe")

    # ── Notion (Integration token, v1) ──

    def _test_notion(self, config: dict) -> dict:
        """GET /v1/users/me — proves integration token is valid."""
        import json as _json
        import urllib.error
        import urllib.request

        token = (config.get("integration_token")
                 or config.get("token") or "").strip()
        if not token:
            return _fail(
                "Notion: integration_token is required",
                "Create one at notion.so/my-integrations.",
                "notion",
            )

        url = "https://api.notion.com/v1/users/me"
        start = time.time()
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": "2022-06-28",
                    "Accept": "application/json",
                    "User-Agent": "F-Pulse/1.0",
                },
            )
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT)
            payload = _json.loads(resp.read().decode("utf-8", errors="replace"))
            latency = _elapsed_ms(start)
            bot = payload.get("bot") or {}
            workspace = bot.get("workspace_name") or "(unknown)"
            return _ok(
                f"Notion integration valid (workspace: {workspace})",
                latency,
                workspace=workspace,
                bot_name=payload.get("name"),
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return _fail(
                    "Notion: integration token rejected (HTTP 401)",
                    "The token is invalid or has been revoked.",
                    "notion",
                )
            return _fail(
                f"Notion: probe failed (HTTP {exc.code})", exc, "notion",
            )
        except Exception as exc:
            return _fail("Notion: probe failed", exc, "notion")

    # ── Asana (PAT, REST 1.0) ──

    def _test_asana(self, config: dict) -> dict:
        """GET /users/me — proves PAT + reveals workspace memberships."""
        import json as _json
        import urllib.error
        import urllib.request

        token = (config.get("personal_access_token")
                 or config.get("access_token")
                 or config.get("token") or "").strip()
        if not token:
            return _fail(
                "Asana: personal_access_token is required",
                "Create one at app.asana.com/0/my-apps.",
                "asana",
            )

        url = "https://app.asana.com/api/1.0/users/me"
        start = time.time()
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": "F-Pulse/1.0",
                },
            )
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT)
            payload = _json.loads(resp.read().decode("utf-8", errors="replace"))
            data = payload.get("data") or {}
            latency = _elapsed_ms(start)
            workspaces = data.get("workspaces") or []
            return _ok(
                f"Asana PAT valid ({data.get('name', '?')}, {len(workspaces)} workspace(s))",
                latency,
                name=data.get("name"),
                workspaces=[w.get("name") for w in workspaces[:5]],
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return _fail(
                    "Asana: PAT rejected (HTTP 401)",
                    "The token is invalid or has been revoked.",
                    "asana",
                )
            return _fail(
                f"Asana: probe failed (HTTP {exc.code})", exc, "asana",
            )
        except Exception as exc:
            return _fail("Asana: probe failed", exc, "asana")

    # ── Redis ──

    def _test_redis(self, config: dict) -> dict:
        try:
            import redis as redis_lib  # type: ignore
        except ImportError:
            return _addon_unavailable("Redis")

        host = config.get("host", "localhost")
        port = int(config.get("port", 6379))
        password = config.get("password")
        db = int(config.get("db", 0))
        use_ssl = config.get("ssl", False) or config.get("tls", False)

        start = time.time()
        try:
            client = redis_lib.Redis(
                host=host,
                port=port,
                password=password,
                db=db,
                ssl=use_ssl,
                socket_timeout=DEFAULT_TIMEOUT,
                socket_connect_timeout=DEFAULT_TIMEOUT,
            )

            pong = client.ping()
            latency = _elapsed_ms(start)

            if not pong:
                return _fail(f"Redis PING did not return PONG at {host}:{port}", "No PONG response", "redis")

            # Server info
            info = client.info(section="server")
            server_version = info.get("redis_version", "unknown")

            # Memory info
            mem_info = client.info(section="memory")
            used_memory_human = mem_info.get("used_memory_human", "unknown")

            # Key count
            db_info = client.info(section="keyspace")
            key_count = 0
            db_key = f"db{db}"
            if db_key in db_info:
                key_count = db_info[db_key].get("keys", 0)

            client.close()

            return _ok(
                f"Redis connected at {host}:{port} (db{db})",
                latency,
                server_version=f"Redis {server_version}",
                used_memory=used_memory_human,
                key_count=key_count,
            )

        except Exception as exc:
            return _fail(f"Redis connection failed to {host}:{port}", exc, "redis")

    # ── MongoDB ──

    def _test_mongodb(self, config: dict) -> dict:
        try:
            from pymongo import MongoClient  # type: ignore
            from pymongo.errors import ConnectionFailure, OperationFailure  # type: ignore
        except ImportError:
            return _addon_unavailable("MongoDB")

        uri = config.get("uri") or config.get("connection_string")
        if uri:
            host = uri
        else:
            h = config.get("host", "localhost")
            p = int(config.get("port", 27017))
            user = config.get("user") or config.get("username")
            password = config.get("password")
            database = config.get("database", "admin")

            if user and password:
                host = f"mongodb://{user}:{password}@{h}:{p}/{database}"
            else:
                host = f"mongodb://{h}:{p}/{database}"

        start = time.time()
        try:
            client = MongoClient(
                host,
                serverSelectionTimeoutMS=DEFAULT_TIMEOUT * 1000,
                connectTimeoutMS=DEFAULT_TIMEOUT * 1000,
            )

            # Ping
            result = client.admin.command("ping")
            latency = _elapsed_ms(start)

            if not result.get("ok"):
                return _fail("MongoDB ping did not return ok", "Ping failed", "mongodb")

            # Server info
            server_info = client.server_info()
            server_version = server_info.get("version", "unknown")

            # Database list
            db_names = client.list_database_names()

            client.close()

            return _ok(
                f"MongoDB connected",
                latency,
                server_version=f"MongoDB {server_version}",
                database_count=len(db_names),
                databases=db_names[:10],
            )

        except ConnectionFailure as exc:
            return _fail("MongoDB connection failed", exc, "mongodb")
        except OperationFailure as exc:
            latency = _elapsed_ms(start)
            # Auth failure but server is reachable
            return _fail("MongoDB authentication failed", exc, "mongodb")
        except Exception as exc:
            return _fail("MongoDB connection failed", exc, "mongodb")

    # ── Elasticsearch ──

    def _test_elasticsearch(self, config: dict) -> dict:
        import json
        import ssl
        import urllib.request
        import urllib.error

        host = config.get("host") or config.get("url") or config.get("endpoint", "http://localhost:9200")
        if not host.startswith("http"):
            host = f"http://{host}"
        host = host.rstrip("/")

        user = config.get("user") or config.get("username")
        password = config.get("password")
        api_key = config.get("api_key")

        url = f"{host}/_cluster/health"
        headers = {"Accept": "application/json", "User-Agent": "F-Pulse/0.6.0"}

        if api_key:
            headers["Authorization"] = f"ApiKey {api_key}"
        elif user and password:
            import base64
            creds = base64.b64encode(f"{user}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"

        start = time.time()
        try:
            req = urllib.request.Request(url, method="GET", headers=headers)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT, context=ctx)
            latency = _elapsed_ms(start)

            body = json.loads(resp.read().decode("utf-8"))
            cluster_name = body.get("cluster_name", "unknown")
            status = body.get("status", "unknown")
            node_count = body.get("number_of_nodes", 0)
            index_count = body.get("active_shards", 0)

            return _ok(
                f"Elasticsearch cluster '{cluster_name}' is {status}",
                latency,
                cluster_name=cluster_name,
                cluster_status=status,
                node_count=node_count,
                active_shards=index_count,
            )

        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                latency = _elapsed_ms(start)
                return _fail(
                    f"Elasticsearch reachable but authentication failed (HTTP {exc.code})",
                    exc,
                    "elasticsearch",
                )
            return _fail(f"Elasticsearch returned HTTP {exc.code}", exc, "elasticsearch")
        except Exception as exc:
            return _fail(f"Elasticsearch connection failed to {host}", exc, "elasticsearch")
