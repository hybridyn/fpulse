# F-Pulse Connector Validation Report

Generated: `2026-08-23T12:31:49.927945+00:00`

## Summary

- Backend registered connector types: `102`
- Frontend create-connection types: `80`
- Backend implemented testers: `26`
- Saved connections inspected: `9`

## Saved Connection Tests

| Status | Name | Type | Message | Last UI Test |
| --- | --- | --- | --- | --- |
| not-run | `fpulse_test` | `mssql` | Use --test-saved to run live tests | 2026-08-23T12:22:09.879640Z |
| not-run | `Mongo_DB_Test` | `mongodb` | Use --test-saved to run live tests | 2026-08-23T12:22:05.665413Z |
| not-run | `JSONPlaceholder (public)` | `rest_api` | Use --test-saved to run live tests | - |
| not-run | `mssql_test_stub` | `mssql` | Use --test-saved to run live tests | - |
| not-run | `catalog_test_pg` | `postgresql` | Use --test-saved to run live tests | - |
| not-run | `catalog_test_duckdb` | `duckdb` | Use --test-saved to run live tests | - |
| not-run | `Orders DB` | `postgresql` | Use --test-saved to run live tests | - |
| not-run | `Oracle ERP API` | `oracle_api` | Use --test-saved to run live tests | - |
| not-run | `Snowflake Data Warehouse` | `snowflake` | Use --test-saved to run live tests | - |

## Frontend Connector Coverage

| Category | Connector | Type | Backend Tester |
| --- | --- | --- | --- |
| APIs & Integration | GraphQL | `graphql` | yes |
| APIs & Integration | Microsoft Graph | `microsoft_graph` | yes |
| APIs & Integration | OData | `odata` | no |
| APIs & Integration | REST API | `rest_api` | yes |
| Cloud Storage | AWS S3 / MinIO | `s3` | yes |
| Cloud Storage | Azure Blob Storage | `azure_blob` | no |
| Cloud Storage | Azure Data Lake Gen2 | `adls_gen2` | no |
| Cloud Storage | Google Cloud Storage | `gcs` | no |
| Cloud Storage | MinIO | `minio` | no |
| Custom | Custom / Other | `custom` | no |
| Data Warehouses | AWS Athena | `athena` | no |
| Data Warehouses | Azure Synapse | `synapse` | no |
| Data Warehouses | BigQuery | `bigquery` | no |
| Data Warehouses | ClickHouse | `clickhouse` | no |
| Data Warehouses | Databricks | `databricks` | no |
| Data Warehouses | Presto | `presto` | no |
| Data Warehouses | Redshift | `redshift` | no |
| Data Warehouses | Snowflake | `snowflake` | no |
| Data Warehouses | Trino | `trino` | no |
| Databases | CockroachDB | `cockroachdb` | no |
| Databases | IBM Db2 | `db2` | no |
| Databases | MariaDB | `mariadb` | no |
| Databases | MySQL | `mysql` | yes |
| Databases | Oracle DB | `oracle` | no |
| Databases | PostgreSQL | `postgresql` | yes |
| Databases | SAP HANA | `sap_hana` | no |
| Databases | SQL Server | `mssql` | yes |
| Databases | SQLite | `sqlite` | yes |
| Databases | Teradata | `teradata` | no |
| Files & Enterprise | Dropbox | `dropbox` | no |
| Files & Enterprise | FTP / SFTP | `ftp` | yes |
| Files & Enterprise | Google Drive | `gdrive` | no |
| Files & Enterprise | Google Sheets | `gsheet` | no |
| Files & Enterprise | OneDrive | `onedrive` | no |
| Files & Enterprise | SharePoint | `sharepoint` | no |
| NoSQL | Cassandra | `cassandra` | no |
| NoSQL | Cosmos DB | `cosmosdb` | no |
| NoSQL | Couchbase | `couchbase` | no |
| NoSQL | DynamoDB | `dynamodb` | no |
| NoSQL | Firebase | `firebase` | no |
| NoSQL | MongoDB | `mongodb` | yes |
| NoSQL | Neo4j | `neo4j` | no |
| Notifications | SMTP Email | `smtp` | yes |
| Notifications | SendGrid | `sendgrid` | no |
| Notifications | Slack Webhook | `slack` | yes |
| Notifications | Twilio | `twilio` | no |
| Observability | Datadog | `datadog` | no |
| Observability | PagerDuty | `pagerduty` | no |
| Observability | Splunk | `splunk` | no |
| SaaS | Asana | `asana` | yes |
| SaaS | Dynamics 365 | `dynamics365` | no |
| SaaS | GitHub | `github` | yes |
| SaaS | HubSpot | `hubspot` | no |
| SaaS | Jira | `jira` | no |
| SaaS | NetSuite | `netsuite` | no |
| SaaS | Notion | `notion` | yes |
| SaaS | Oracle BI Publisher | `oracle_bip` | yes |
| SaaS | Oracle Fusion Cloud | `oracle_fusion` | yes |
| SaaS | SAP (legacy alias) | `sap` | no |
| SaaS | SAP S/4HANA (OData) | `sap_s4hana` | yes |
| SaaS | SAP SuccessFactors | `sap_successfactors` | yes |
| SaaS | Salesforce | `salesforce` | no |
| SaaS | ServiceNow | `servicenow` | no |
| SaaS | Shopify | `shopify` | yes |
| SaaS | Stripe | `stripe` | yes |
| SaaS | Workday | `workday` | no |
| SaaS | Zendesk | `zendesk` | no |
| Search & Cache | Elasticsearch | `elasticsearch` | yes |
| Search & Cache | OpenSearch | `opensearch` | no |
| Search & Cache | Redis | `redis` | yes |
| Streaming | AWS Kinesis | `kinesis` | no |
| Streaming | Apache Pulsar | `pulsar` | no |
| Streaming | Azure Event Hubs | `eventhub` | no |
| Streaming | Kafka | `kafka` | yes |
| Streaming | RabbitMQ | `rabbitmq` | no |
| Vector / AI | Chroma | `chroma` | no |
| Vector / AI | Pinecone | `pinecone` | no |
| Vector / AI | Qdrant | `qdrant` | no |
| Vector / AI | Weaviate | `weaviate` | no |
| Vector / AI | pgvector | `pgvector` | no |

## Gaps

Frontend types without backend tester:

`adls_gen2`, `athena`, `azure_blob`, `bigquery`, `cassandra`, `chroma`, `clickhouse`, `cockroachdb`, `cosmosdb`, `couchbase`, `custom`, `databricks`, `datadog`, `db2`, `dropbox`, `dynamics365`, `dynamodb`, `eventhub`, `firebase`, `gcs`, `gdrive`, `gsheet`, `hubspot`, `jira`, `kinesis`, `mariadb`, `minio`, `neo4j`, `netsuite`, `odata`, `onedrive`, `opensearch`, `oracle`, `pagerduty`, `pgvector`, `pinecone`, `presto`, `pulsar`, `qdrant`, `rabbitmq`, `redshift`, `salesforce`, `sap`, `sap_hana`, `sendgrid`, `servicenow`, `sharepoint`, `snowflake`, `splunk`, `synapse`, `teradata`, `trino`, `twilio`, `weaviate`, `workday`, `zendesk`

Backend model types not exposed in the create-connection picker:

`airbyte`, `arangodb`, `box`, `dbt`, `duckdb`, `fivetran`, `grafana`, `hdfs`, `informix`, `local_file`, `memcached`, `milvus`, `nats`, `newrelic`, `oracle_api`, `prometheus`, `pubsub`, `sftp`, `soap`, `solr`, `sqs`, `teams`

## Saved Connection Details

```json
[
  {
    "id": "d9880bfabd79",
    "name": "fpulse_test",
    "type": "mssql",
    "status": "not-run",
    "message": "Use --test-saved to run live tests",
    "last_test_ok": true,
    "last_test_at": "2026-08-23T12:22:09.879640Z",
    "config": {
      "host": "localhost",
      "database": "fpulse_test",
      "username": "fpulse_user_test",
      "password": "[set]",
      "trust_server_certificate": "true",
      "port": "1433"
    }
  },
  {
    "id": "e3d6daf95400",
    "name": "Mongo_DB_Test",
    "type": "mongodb",
    "status": "not-run",
    "message": "Use --test-saved to run live tests",
    "last_test_ok": true,
    "last_test_at": "2026-08-23T12:22:05.665413Z",
    "config": {
      "uri": "[set]",
      "database": "sample_mflix",
      "username": "nsivakumarraj_db_user",
      "password": "[set]",
      "auth_source": "admin",
      "port": "27017",
      "host": "localhost",
      "connection_mode": "uri"
    }
  },
  {
    "id": "f532d4211985",
    "name": "JSONPlaceholder (public)",
    "type": "rest_api",
    "status": "not-run",
    "message": "Use --test-saved to run live tests",
    "last_test_ok": null,
    "last_test_at": null,
    "config": {
      "base_url": "https://jsonplaceholder.typicode.com",
      "auth_type": "none"
    }
  },
  {
    "id": "74ffa082f8b2",
    "name": "mssql_test_stub",
    "type": "mssql",
    "status": "not-run",
    "message": "Use --test-saved to run live tests",
    "last_test_ok": null,
    "last_test_at": null,
    "config": {}
  },
  {
    "id": "a8268512cda3",
    "name": "catalog_test_pg",
    "type": "postgresql",
    "status": "not-run",
    "message": "Use --test-saved to run live tests",
    "last_test_ok": null,
    "last_test_at": null,
    "config": {}
  },
  {
    "id": "f7128b3bb84f",
    "name": "catalog_test_duckdb",
    "type": "duckdb",
    "status": "not-run",
    "message": "Use --test-saved to run live tests",
    "last_test_ok": null,
    "last_test_at": null,
    "config": {}
  },
  {
    "id": "conn_orders_db",
    "name": "Orders DB",
    "type": "postgresql",
    "status": "not-run",
    "message": "Use --test-saved to run live tests",
    "last_test_ok": null,
    "last_test_at": null,
    "config": {
      "host": "db.example.com",
      "port": 5432,
      "database": "orders",
      "schema": "public"
    }
  },
  {
    "id": "conn_oracle_erp",
    "name": "Oracle ERP API",
    "type": "oracle_api",
    "status": "not-run",
    "message": "Use --test-saved to run live tests",
    "last_test_ok": null,
    "last_test_at": null,
    "config": {
      "base_url": "https://erp.example.com/api/v2",
      "auth_type": "oauth2",
      "timeout_seconds": 30
    }
  },
  {
    "id": "conn_snowflake_dw",
    "name": "Snowflake Data Warehouse",
    "type": "snowflake",
    "status": "not-run",
    "message": "Use --test-saved to run live tests",
    "last_test_ok": null,
    "last_test_at": null,
    "config": {
      "account": "abc12345.us-east-1",
      "warehouse": "COMPUTE_WH",
      "database": "ANALYTICS",
      "schema": "PUBLIC"
    }
  }
]
```
