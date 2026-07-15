import { useState, useEffect } from 'react';
import { api } from '../../api/client';
import { toast } from '../Toast';
import ReadOnlyBanner from '../../auth/ReadOnlyBanner';
import { useCan } from '../../auth/RoleGate';
import TableToolbar, { useTableColumns, TColumn, TColumnGroup } from '../shared/TableToolbar';
import { useDarkMode } from '../../hooks/useDarkMode';
import { uiConfirm } from '../../ui/dialog';
import ProjectContextBar from '../layout/ProjectContextBar';
import TierChip from '../shared/TierChip';
import PageHeader from '../shared/PageHeader';
import ConnectorIcon from '../shared/ConnectorIcons';
import HeroCard from '../shared/HeroCard';
import { DelayedSkeleton, SkeletonCard } from '../shared/Skeleton';
import EmptyState from '../shared/EmptyState';
import ErrorBanner from '../shared/ErrorBanner';
import { DensityToggle, useDensity } from '../shared/DensityToggle';
import MoveToProjectButton from '../shared/MoveToProjectButton';
import RowActionButton from '../shared/RowActionButton';
import HubTabs, { CONNECTIONS_TABS } from '../HubTabs';
import DetailDrawer from '../shared/DetailDrawer';
import TimeAgo from '../shared/TimeAgo';
import { CertChipsForType } from '../shared/CertChips';
import { usePageContext } from '../../hooks/usePageContext';

interface Connection {
  id: string;
  name: string;
  type: string;
  description: string;
  config: Record<string, any>;
  credential_id?: string;
  project_id: string | null;
  tags: string[];
  report_count?: number;
  created_at: string;
  environment?: 'dev' | 'prod' | 'all';
  /** Direction roles this connection can play. Empty / missing = both
   *  (legacy rows pre-Apr 22 2026). Source-node ConnectionPicker filters
   *  to entries with 'read'; sink-node picker filters to 'write'. */
  capabilities?: string[];
  last_test_at?: string | null;
  last_test_ok?: boolean | null;
  last_test_error?: string;
}

// Connector types that should default to write-only when the user creates
// a fresh connection. Frontend pre-uncheck for "Source" on these so the
// admin doesn't have to remember the convention. Mirrors backend's
// WRITE_ONLY_TYPES set in connections/models.py.
const WRITE_ONLY_TYPES = new Set([
  'slack', 'teams', 'smtp', 'sendgrid', 'twilio', 'pagerduty',
]);

// Two-checkbox state → capabilities array on the wire. We always send
// at least one capability (the form blocks save when both are off via
// the disabled save button below).
function capabilitiesFromForm(canRead: boolean, canWrite: boolean): string[] {
  const out: string[] = [];
  if (canRead) out.push('read');
  if (canWrite) out.push('write');
  return out;
}

// Inverse — server may return ['read', 'write'] / ['read'] / ['write'].
// Empty / missing arrays → both true (legacy rows pre-Apr 22 2026).
function formFromCapabilities(caps: string[] | undefined): { canRead: boolean; canWrite: boolean } {
  if (!caps || caps.length === 0) return { canRead: true, canWrite: true };
  return { canRead: caps.includes('read'), canWrite: caps.includes('write') };
}

interface ConnectionReport {
  id: string;
  connection_id: string;
  name: string;
  description: string;
  query_template: string;
  parameters: Array<{ name: string; type: string; default?: string; required: boolean }>;
  created_at: string;
}

interface Project {
  id: string;
  name: string;
  color?: string;
  icon?: string;
}

// 2026-05-19 (P2 #10 of PAGE_BY_PAGE_AUDIT.md): the `icon` emoji column
// on each entry below is DEPRECATED — every render site routes through
// `<ConnectorIcon type={...} />` (shared/ConnectorIcons) which loads the
// canonical SVG by connector type. The emoji field is retained only
// because removing it from 66 entries would create churn; it is never
// consumed. New entries do not need an `icon` value.
const CONNECTION_TYPES = [
  // ── 1. Databases (OLTP) ──
  { type: 'postgresql', label: 'PostgreSQL', icon: '🐘', color: '#336791', category: 'Databases' },
  { type: 'mysql', label: 'MySQL', icon: '🐬', color: '#00758F', category: 'Databases' },
  { type: 'mssql', label: 'SQL Server', icon: '🗃️', color: '#CC2927', category: 'Databases' },
  { type: 'oracle', label: 'Oracle DB', icon: '🔴', color: '#F80000', category: 'Databases' },
  { type: 'sqlite', label: 'SQLite', icon: '📦', color: '#003B57', category: 'Databases' },
  { type: 'mariadb', label: 'MariaDB', icon: '🐬', color: '#003545', category: 'Databases' },
  { type: 'cockroachdb', label: 'CockroachDB', icon: '🪳', color: '#6933FF', category: 'Databases' },
  // 2026-05-23 (T3): enterprise relational engines the backend has
  // testers + catalog providers for but the picker was missing.
  { type: 'db2', label: 'IBM Db2', icon: '🔷', color: '#054ADA', category: 'Databases' },
  { type: 'sap_hana', label: 'SAP HANA', icon: '🔷', color: '#0FAAFF', category: 'Databases' },
  { type: 'teradata', label: 'Teradata', icon: '🟠', color: '#F37440', category: 'Databases' },
  // ── 2. NoSQL / Document / Graph ──
  { type: 'mongodb', label: 'MongoDB', icon: '🍃', color: '#47A248', category: 'NoSQL' },
  { type: 'cassandra', label: 'Cassandra', icon: '👁️', color: '#1287B1', category: 'NoSQL' },
  { type: 'couchbase', label: 'Couchbase', icon: '🔴', color: '#EA2328', category: 'NoSQL' },
  { type: 'dynamodb', label: 'DynamoDB', icon: '⚡', color: '#4053D6', category: 'NoSQL' },
  { type: 'cosmosdb', label: 'Cosmos DB', icon: '🌌', color: '#0078D4', category: 'NoSQL' },
  { type: 'neo4j', label: 'Neo4j', icon: '🔗', color: '#008CC1', category: 'NoSQL' },
  { type: 'firebase', label: 'Firebase', icon: '🔥', color: '#FFCA28', category: 'NoSQL' },
  // ── 3. Data Warehouses (OLAP) ──
  { type: 'snowflake', label: 'Snowflake', icon: '❄️', color: '#29B5E8', category: 'Data Warehouses' },
  { type: 'bigquery', label: 'BigQuery', icon: '📊', color: '#4285F4', category: 'Data Warehouses' },
  { type: 'redshift', label: 'Redshift', icon: '🔶', color: '#8C4FFF', category: 'Data Warehouses' },
  { type: 'databricks', label: 'Databricks', icon: '🧱', color: '#FF3621', category: 'Data Warehouses' },
  { type: 'synapse', label: 'Azure Synapse', icon: '🟦', color: '#0078D4', category: 'Data Warehouses' },
  { type: 'clickhouse', label: 'ClickHouse', icon: '🏠', color: '#FFCC00', category: 'Data Warehouses' },
  { type: 'trino', label: 'Trino', icon: '🔺', color: '#DD00A1', category: 'Data Warehouses' },
  // 2026-05-23 (T3): SQL-on-data-lake engines the backend's catalog
  // providers already cover (athena via Glue, presto sibling of trino).
  { type: 'presto', label: 'Presto', icon: '🔺', color: '#6699CC', category: 'Data Warehouses' },
  { type: 'athena', label: 'AWS Athena', icon: '🔻', color: '#FF9900', category: 'Data Warehouses' },
  // ── 4. Search & Cache ──
  { type: 'elasticsearch', label: 'Elasticsearch', icon: '🔍', color: '#FEC514', category: 'Search & Cache' },
  { type: 'opensearch', label: 'OpenSearch', icon: '🔍', color: '#005EB8', category: 'Search & Cache' },
  { type: 'redis', label: 'Redis', icon: '🔴', color: '#DC382D', category: 'Search & Cache' },
  // ── 5. Cloud Storage ──
  { type: 's3', label: 'AWS S3 / MinIO', icon: '☁️', color: '#FF9900', category: 'Cloud Storage' },
  { type: 'azure_blob', label: 'Azure Blob Storage', icon: '🟦', color: '#0078d4', category: 'Cloud Storage' },
  { type: 'adls_gen2', label: 'Azure Data Lake Gen2', icon: '🟦', color: '#0078d4', category: 'Cloud Storage' },
  { type: 'gcs', label: 'Google Cloud Storage', icon: '🟩', color: '#4285f4', category: 'Cloud Storage' },
  { type: 'minio', label: 'MinIO', icon: '🔴', color: '#C72C48', category: 'Cloud Storage' },
  // ── 6. Files & Enterprise Docs ──
  { type: 'sharepoint', label: 'SharePoint', icon: '📁', color: '#036ac4', category: 'Files & Enterprise' },
  { type: 'onedrive', label: 'OneDrive', icon: '☁️', color: '#0364b8', category: 'Files & Enterprise' },
  { type: 'gdrive', label: 'Google Drive', icon: '📁', color: '#4285F4', category: 'Files & Enterprise' },
  { type: 'dropbox', label: 'Dropbox', icon: '📦', color: '#0061FE', category: 'Files & Enterprise' },
  { type: 'ftp', label: 'FTP / SFTP', icon: '📡', color: '#6366f1', category: 'Files & Enterprise' },
  { type: 'gsheet', label: 'Google Sheets', icon: '📗', color: '#0F9D58', category: 'Files & Enterprise' },
  // ── 7. APIs & Integration ──
  { type: 'rest_api', label: 'REST API', icon: '🌐', color: '#0ea5e9', category: 'APIs & Integration' },
  { type: 'graphql', label: 'GraphQL', icon: '◈', color: '#e535ab', category: 'APIs & Integration' },
  { type: 'odata', label: 'OData', icon: '🔗', color: '#0078D4', category: 'APIs & Integration' },
  // 2026-05-22 — first-class Microsoft Graph connector. Reused by
  // SharePoint / OneDrive / Teams / Outlook / Planner / Users /
  // Groups / Sites flows via one client-credentials OAuth grant
  // against the Azure App Registration. See backend
  // connections/tester.py:_test_microsoft_graph and the
  // microsoft_graph_source node for the read side.
  { type: 'microsoft_graph', label: 'Microsoft Graph', icon: '🟦', color: '#0078d4', category: 'APIs & Integration' },
  // 2026-05-23 (T4): `oracle_api` is the legacy alias of `oracle_fusion`.
  // Hidden from the picker but still loadable for existing rows; new
  // pipelines pick `oracle_fusion` for Oracle Cloud REST.
  // ── 8. Streaming & Messaging ──
  { type: 'kafka', label: 'Kafka', icon: '⚡', color: '#231F20', category: 'Streaming' },
  { type: 'rabbitmq', label: 'RabbitMQ', icon: '🐰', color: '#FF6600', category: 'Streaming' },
  { type: 'pulsar', label: 'Apache Pulsar', icon: '⚡', color: '#188FFF', category: 'Streaming' },
  { type: 'eventhub', label: 'Azure Event Hubs', icon: '🟦', color: '#0078D4', category: 'Streaming' },
  { type: 'kinesis', label: 'AWS Kinesis', icon: '⚡', color: '#FF9900', category: 'Streaming' },
  // ── 9. SaaS (Prebuilt) ──
  { type: 'salesforce', label: 'Salesforce', icon: '☁️', color: '#00A1E0', category: 'SaaS' },
  { type: 'dynamics365', label: 'Dynamics 365', icon: '🟦', color: '#002050', category: 'SaaS' },
  // 2026-05-23 (V1/V2): SAP product families. `sap` stays as a
  // back-compat alias of sap_s4hana but the picker leads with the
  // product-specific entries so new pipelines disambiguate.
  { type: 'sap_s4hana', label: 'SAP S/4HANA (OData)', icon: '🔷', color: '#0FAAFF', category: 'SaaS' },
  { type: 'sap_successfactors', label: 'SAP SuccessFactors', icon: '🟦', color: '#003E7E', category: 'SaaS' },
  // Legacy `sap` entry kept for back-compat. New pipelines use sap_s4hana.
  { type: 'sap', label: 'SAP (legacy alias)', icon: '🔷', color: '#0FAAFF', category: 'SaaS' },
  { type: 'servicenow', label: 'ServiceNow', icon: '🟢', color: '#62D84E', category: 'SaaS' },
  { type: 'jira', label: 'Jira', icon: '🔵', color: '#0052CC', category: 'SaaS' },
  { type: 'workday', label: 'Workday', icon: '🟠', color: '#F68D2E', category: 'SaaS' },
  { type: 'hubspot', label: 'HubSpot', icon: '🟠', color: '#FF7A59', category: 'SaaS' },
  { type: 'zendesk', label: 'Zendesk', icon: '🟢', color: '#03363D', category: 'SaaS' },
  { type: 'netsuite', label: 'NetSuite', icon: '🔶', color: '#125580', category: 'SaaS' },
  // 2026-05-23 (U1/U2): Oracle product families.
  { type: 'oracle_fusion', label: 'Oracle Fusion Cloud', icon: '🔴', color: '#F80000', category: 'SaaS' },
  { type: 'oracle_bip', label: 'Oracle BI Publisher', icon: '🟥', color: '#C74634', category: 'SaaS' },
  // 2026-05-23 (W1): manifest-promoted SaaS now first-class. Each has
  // a backend tester + catalog provider keyed off its v1 manifest.
  { type: 'github', label: 'GitHub', icon: '⬛', color: '#181717', category: 'SaaS' },
  { type: 'shopify', label: 'Shopify', icon: '🟢', color: '#7AB55C', category: 'SaaS' },
  { type: 'stripe', label: 'Stripe', icon: '🟣', color: '#635BFF', category: 'SaaS' },
  { type: 'notion', label: 'Notion', icon: '⬜', color: '#000000', category: 'SaaS' },
  { type: 'asana', label: 'Asana', icon: '🔴', color: '#F06A6A', category: 'SaaS' },
  // ── 10. Notifications ──
  { type: 'smtp', label: 'SMTP Email', icon: '📧', color: '#4A90D9', category: 'Notifications' },
  { type: 'sendgrid', label: 'SendGrid', icon: '📧', color: '#1A82E2', category: 'Notifications' },
  { type: 'slack', label: 'Slack Webhook', icon: '💬', color: '#4A154B', category: 'Notifications' },
  { type: 'twilio', label: 'Twilio', icon: '📱', color: '#F22F46', category: 'Notifications' },
  // ── 11. Observability ──
  { type: 'datadog', label: 'Datadog', icon: '🐕', color: '#632CA6', category: 'Observability' },
  { type: 'pagerduty', label: 'PagerDuty', icon: '🔔', color: '#06AC38', category: 'Observability' },
  { type: 'splunk', label: 'Splunk', icon: '📊', color: '#000000', category: 'Observability' },
  // ── 12. Vector / AI ──
  { type: 'pinecone', label: 'Pinecone', icon: '🌲', color: '#000000', category: 'Vector / AI' },
  { type: 'weaviate', label: 'Weaviate', icon: '🔷', color: '#01CC87', category: 'Vector / AI' },
  { type: 'qdrant', label: 'Qdrant', icon: '🔶', color: '#DC244C', category: 'Vector / AI' },
  { type: 'chroma', label: 'Chroma', icon: '🎨', color: '#FF6B6B', category: 'Vector / AI' },
  { type: 'pgvector', label: 'pgvector', icon: '🐘', color: '#336791', category: 'Vector / AI' },
  // ── 13. Custom ──
  { type: 'custom', label: 'Custom / Other', icon: '🔧', color: '#94a3b8', category: 'Custom' },
];

const CONNECTOR_MENU_GROUPS = [
  { id: 'All', label: 'All', categories: [] as string[] },
  { id: 'Databases', label: 'Databases', categories: ['Databases', 'NoSQL'] },
  { id: 'Warehouses & Lake', label: 'Warehouses & Lake', categories: ['Data Warehouses', 'Search & Cache'] },
  { id: 'Files & Storage', label: 'Files & Storage', categories: ['Cloud Storage', 'Files & Enterprise'] },
  { id: 'APIs & Apps', label: 'APIs & Apps', categories: ['APIs & Integration', 'SaaS'] },
  { id: 'Operations & AI', label: 'Operations & AI', categories: ['Streaming', 'Notifications', 'Observability', 'Vector / AI', 'Custom'] },
];

/**
 * Connector certification status overlay.
 *
 * - 'certified': production-grade, fully tested.
 * - 'beta': functional but limited (e.g. read works, sink stubbed; or auth
 *   works but pagination not wired).
 * - 'roadmap': UI-only today; backend not implemented. Hidden in the
 *   picker until shipped to avoid setting false expectations.
 *
 * All connectors are open and available — there is no Plus-only gating
 * on the connector library. Defaults to 'certified' when a connector
 * type isn't listed here.
 */
type ConnectorStatus = 'certified' | 'beta' | 'roadmap';

const CONNECTOR_STATUS: Record<string, ConnectorStatus> = {
  // Certified database / warehouse dialects
  postgresql: 'certified', mysql: 'certified', mssql: 'certified', oracle: 'certified',
  sqlite: 'certified', snowflake: 'certified', bigquery: 'certified', redshift: 'certified',
  databricks: 'certified', clickhouse: 'certified', mongodb: 'certified',
  // Certified file/storage (basic file ops)
  s3: 'certified', azure_blob: 'certified', gcs: 'certified', minio: 'certified',
  ftp: 'certified',
  // Certified APIs/integration
  rest_api: 'certified', graphql: 'certified', custom: 'certified',
  // Certified messaging
  smtp: 'certified', slack: 'certified',
  // Beta — usable with gaps
  mariadb: 'beta', cockroachdb: 'beta', synapse: 'beta', trino: 'beta',
  // 2026-05-23 (T3): enterprise relational / lake-warehouse engines.
  // Backend has catalog + (db2/sap_hana/teradata also) tester paths;
  // they ship as beta until F0.1 cert and a live instance exercise.
  db2: 'beta', sap_hana: 'beta', teradata: 'beta',
  athena: 'beta', presto: 'beta',
  elasticsearch: 'beta', opensearch: 'beta', redis: 'beta',
  adls_gen2: 'beta', sharepoint: 'beta', onedrive: 'beta', gdrive: 'beta',
  dropbox: 'beta', gsheet: 'beta', odata: 'beta',
  // Streaming: only Kafka has a pipeline source+sink runtime today
  // (nodes/generic.py SOURCE_MAP/DEST_MAP + KAFKA_SOURCE/KAFKA_SINK).
  // RabbitMQ / Kinesis / Event Hubs have connection + catalog (discovery)
  // support but NO pipeline node - so they would dead-end after "test +
  // browse". Marked 'roadmap' (below, with Pulsar) so the pipeline-connection
  // picker does not offer them, matching the backend maturity_label (they
  // compute as 'configurable', never 'production'/runtime). Fixes a prior
  // 'beta' overclaim (2026-07-03).
  kafka: 'beta',
  jira: 'beta', hubspot: 'beta', zendesk: 'beta',
  sendgrid: 'beta', twilio: 'beta',
  pinecone: 'beta', weaviate: 'beta', qdrant: 'beta', chroma: 'beta', pgvector: 'beta',
  // Enterprise SaaS — manifests ship with the box, classified Beta until
  // the depth-3 F0.1 cert pass lands.
  salesforce: 'beta', dynamics365: 'beta', sap: 'beta', servicenow: 'beta',
  workday: 'beta', netsuite: 'beta',
  // 2026-05-23 (U1/U2 + V1/V2): Oracle / SAP product families. Backend
  // testers + catalog providers ship beta; production tag requires F0.1
  // depth-3 cert + a live instance exercise.
  oracle_fusion: 'beta', oracle_bip: 'beta',
  sap_s4hana: 'beta', sap_successfactors: 'beta',
  // 2026-05-23 (W1): manifest-promoted SaaS — backend testers exercise
  // the real auth path; v1 manifests cover the canonical streams.
  github: 'beta', stripe: 'beta',
  notion: 'beta', asana: 'beta',
  // 2026-06-02: shopify manifest marked `tier: hidden` in the cert
  // matrix (out of enterprise-data-engineering scope at 1.0). The
  // 'roadmap' status here piggybacks on the existing filter at line
  // ~2391 so the picker hides it without inventing a new status enum.
  // To bring it back into the picker: remove this entry AND remove
  // the `tier: hidden` line in backend/fpulse/connectors/manifests/shopify*.json.
  shopify: 'roadmap',
  // Roadmap — UI present, backend not yet shipped
  cassandra: 'roadmap', couchbase: 'roadmap', dynamodb: 'roadmap',
  cosmosdb: 'roadmap', neo4j: 'roadmap', firebase: 'roadmap',
  // Streaming connectors with connection+catalog but no pipeline source/sink
  // node yet (only Kafka has runtime). Hidden from the pipeline picker to
  // avoid a "test passes, then no node to use it" dead-end. See the note by
  // the streaming block above.
  pulsar: 'roadmap', rabbitmq: 'roadmap', kinesis: 'roadmap', eventhub: 'roadmap',
  datadog: 'roadmap', pagerduty: 'roadmap', splunk: 'roadmap',
  // Legacy alias — kept as 'beta' so existing rows continue to render
  // their status badge; new pipelines should use oracle_fusion.
  oracle_api: 'beta',
};

function connectorStatus(type: string): ConnectorStatus {
  return CONNECTOR_STATUS[type] || 'certified';
}

const STATUS_BADGE_STYLE: Record<ConnectorStatus, { bg: string; text: string; label: string }> = {
  certified: { bg: 'bg-emerald-100', text: 'text-emerald-700', label: 'Certified' },
  beta: { bg: 'bg-amber-100', text: 'text-amber-700', label: 'Beta' },
  roadmap: { bg: 'bg-slate-200', text: 'text-slate-600', label: 'Coming soon' },
};

/**
 * Connector provenance — who authored the manifest, independent of
 * production-readiness (which is `ConnectorStatus`).
 *
 *   first-party  : shipped in the f-pulse repo's connectors/manifests/
 *                  directory. The starter catalog.
 *   community    : contributed via PR, accepted into the repo. Same
 *                  trust posture as first-party but credit goes to a
 *                  community contributor in the NOTICE file.
 *   user-authored: dropped into the user's local
 *                  backend/fpulse/connectors/manifests/ directory via
 *                  the Author Connector flow or by hand. Only visible
 *                  on this install.
 *
 * Today every shipped manifest is first-party, so the per-card badge
 * is suppressed (clutter for no signal). When manifest discovery starts
 * differentiating shipped vs dropped-in, the badge activates for the
 * non-first-party cases — and the "Build your own" footer (added
 * alongside this) already directs users into the user-authored path.
 *
 * The framework's visibility comes from (a) the legend chip below the
 * connector picker mentioning all three tiers, and (b) the "Don't see
 * your tool?" footer surfacing the user-authored path on every load.
 */
type ConnectorProvenance = 'first-party' | 'community' | 'user-authored';

const PROVENANCE_BADGE_STYLE: Record<ConnectorProvenance, { bg: string; text: string; label: string; title: string }> = {
  'first-party':   { bg: 'bg-slate-100',  text: 'text-slate-700',  label: 'F-Pulse', title: 'Shipped in the F-Pulse catalog (first-party)' },
  'community':     { bg: 'bg-indigo-100', text: 'text-indigo-700', label: 'Community', title: 'Contributed by the community, accepted into the catalog' },
  'user-authored': { bg: 'bg-purple-100', text: 'text-purple-700', label: 'Yours', title: 'Authored on this install via Insights → Author Connector' },
};

/**
 * Resolve provenance for a connector type. Today: everything in the
 * shipped catalog is first-party. Tomorrow: this looks at a manifest
 * field set by the discovery layer (`source: "shipped" | "user" |
 * "community"`) so the same UI lights up automatically without code
 * changes here. Returns `null` when we want to suppress the badge,
 * which today is every connector (first-party = no visual noise).
 */
function connectorProvenance(_type: string): ConnectorProvenance | null {
  // Suppressed-by-default until manifest discovery differentiates.
  // To activate per-connector once that lands: return the manifest's
  // `provenance` field. The badge will appear automatically.
  return null;
}

/** Field definition for connection config forms */
interface ConnField {
  key: string;
  label: string;
  type: 'text' | 'password' | 'number' | 'select' | 'checkbox' | 'textarea';
  placeholder: string;
  required?: boolean;
  hint?: string;
  options?: Array<{ value: string; label: string }>;
  defaultValue?: string;
}

const CONNECTION_FIELDS: Record<string, ConnField[]> = {
  // ── Relational Databases ──
  postgresql: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'localhost', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '5432', required: true, defaultValue: '5432' },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'mydb', required: true },
    { key: 'schema', label: 'Schema', type: 'text', placeholder: 'public', hint: 'Default schema for queries' },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'postgres', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'ssl', label: 'Use SSL', type: 'checkbox', placeholder: '', hint: 'Enable SSL/TLS encryption' },
  ],
  mysql: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'localhost', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '3306', required: true, defaultValue: '3306' },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'mydb', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'root', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'ssl', label: 'Use SSL', type: 'checkbox', placeholder: '' },
  ],
  mariadb: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'localhost', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '3306', required: true, defaultValue: '3306' },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'mydb', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'root', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'ssl', label: 'Use SSL', type: 'checkbox', placeholder: '' },
  ],
  mssql: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'sql-server.database.windows.net', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '1433', required: true, defaultValue: '1433' },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'mydb', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'sa', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'encrypt', label: 'Encrypt Connection', type: 'checkbox', placeholder: '', hint: 'Required for Azure SQL' },
    { key: 'trust_server_certificate', label: 'Trust Server Certificate', type: 'checkbox', placeholder: '' },
  ],
  oracle: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'oracle-server', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '1521', required: true, defaultValue: '1521' },
    { key: 'service_name', label: 'Service Name / SID', type: 'text', placeholder: 'ORCL', required: true, hint: 'Oracle service name or SID identifier' },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'system', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
  ],
  sqlite: [
    { key: 'file_path', label: 'Database File Path', type: 'text', placeholder: '/data/mydb.sqlite', required: true, hint: 'Absolute or relative path to .sqlite / .db file' },
  ],
  cockroachdb: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'free-tier.gcp-us-central1.cockroachlabs.cloud', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '26257', required: true, defaultValue: '26257' },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'defaultdb', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: '', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'ssl', label: 'Use SSL', type: 'checkbox', placeholder: '', defaultValue: 'true' },
  ],
  // ── NoSQL ──
  mongodb: [
    { key: 'connection_mode', label: 'Connection Mode', type: 'select', placeholder: '', required: true, defaultValue: 'uri', options: [
      { value: 'uri', label: 'Connection URI' }, { value: 'fields', label: 'Individual Fields' },
    ]},
    { key: 'uri', label: 'MongoDB URI', type: 'password', placeholder: 'mongodb+srv://user:pass@cluster.mongodb.net/mydb', hint: 'Full connection string (Atlas or self-hosted)' },
    { key: 'host', label: 'Host', type: 'text', placeholder: 'localhost' },
    { key: 'port', label: 'Port', type: 'number', placeholder: '27017', defaultValue: '27017' },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'mydb', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: '' },
    { key: 'password', label: 'Password', type: 'password', placeholder: '' },
    { key: 'auth_source', label: 'Auth Source', type: 'text', placeholder: 'admin', hint: 'Database used for authentication (default: admin)' },
  ],
  cassandra: [
    { key: 'host', label: 'Contact Points', type: 'text', placeholder: 'node1.example.com, node2.example.com', required: true, hint: 'Comma-separated list of seed nodes' },
    { key: 'port', label: 'Port', type: 'number', placeholder: '9042', required: true, defaultValue: '9042' },
    { key: 'keyspace', label: 'Keyspace', type: 'text', placeholder: 'my_keyspace', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: '' },
    { key: 'password', label: 'Password', type: 'password', placeholder: '' },
    { key: 'datacenter', label: 'Local Datacenter', type: 'text', placeholder: 'datacenter1', hint: 'Required for DCAwareRoundRobinPolicy' },
  ],
  couchbase: [
    { key: 'host', label: 'Connection String', type: 'text', placeholder: 'couchbase://localhost', required: true },
    { key: 'bucket', label: 'Bucket', type: 'text', placeholder: 'default', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'Administrator', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'scope', label: 'Scope', type: 'text', placeholder: '_default' },
  ],
  dynamodb: [
    { key: 'region', label: 'AWS Region', type: 'text', placeholder: 'us-east-1', required: true },
    { key: 'access_key', label: 'Access Key ID', type: 'text', placeholder: 'AKIA...', required: true },
    { key: 'secret_key', label: 'Secret Access Key', type: 'password', placeholder: '', required: true },
    { key: 'endpoint_url', label: 'Endpoint URL', type: 'text', placeholder: 'http://localhost:8000', hint: 'For DynamoDB Local or custom endpoints' },
  ],
  cosmosdb: [
    { key: 'endpoint', label: 'Account Endpoint', type: 'text', placeholder: 'https://myaccount.documents.azure.com:443/', required: true },
    { key: 'key', label: 'Account Key', type: 'password', placeholder: '', required: true },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'mydb', required: true },
    { key: 'api_type', label: 'API Type', type: 'select', placeholder: '', defaultValue: 'sql', options: [
      { value: 'sql', label: 'SQL (Core)' }, { value: 'mongo', label: 'MongoDB' }, { value: 'table', label: 'Table' },
    ]},
  ],
  neo4j: [
    { key: 'uri', label: 'Bolt URI', type: 'text', placeholder: 'bolt://localhost:7687', required: true, hint: 'bolt:// or neo4j:// protocol' },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'neo4j', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'neo4j', hint: 'Default database to query' },
  ],
  firebase: [
    { key: 'project_id', label: 'Firebase Project ID', type: 'text', placeholder: 'my-firebase-app', required: true },
    { key: 'credentials_json', label: 'Service Account JSON', type: 'textarea', placeholder: '{ "type": "service_account", ... }', required: true, hint: 'Paste the full service account key JSON' },
    { key: 'database_url', label: 'Realtime DB URL', type: 'text', placeholder: 'https://my-app.firebaseio.com', hint: 'Only for Realtime Database (leave blank for Firestore)' },
  ],
  // ── Data Warehouses ──
  snowflake: [
    { key: 'account', label: 'Account Identifier', type: 'text', placeholder: 'abc12345.us-east-1', required: true, hint: 'e.g. orgname-accountname or locator.region' },
    { key: 'warehouse', label: 'Warehouse', type: 'text', placeholder: 'COMPUTE_WH', required: true },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'ANALYTICS', required: true },
    { key: 'schema', label: 'Schema', type: 'text', placeholder: 'PUBLIC', defaultValue: 'PUBLIC' },
    { key: 'role', label: 'Role', type: 'text', placeholder: 'SYSADMIN', hint: 'Snowflake role for session' },
    { key: 'username', label: 'Username', type: 'text', placeholder: '', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
  ],
  bigquery: [
    { key: 'project', label: 'GCP Project ID', type: 'text', placeholder: 'my-gcp-project', required: true },
    { key: 'dataset', label: 'Default Dataset', type: 'text', placeholder: 'analytics', hint: 'Default dataset for unqualified table names' },
    { key: 'location', label: 'Location', type: 'text', placeholder: 'US', hint: 'Dataset location (US, EU, asia-northeast1, etc.)' },
    { key: 'credentials_json', label: 'Service Account JSON', type: 'textarea', placeholder: '{ "type": "service_account", ... }', required: true, hint: 'Paste the full service account key JSON' },
  ],
  redshift: [
    { key: 'host', label: 'Cluster Endpoint', type: 'text', placeholder: 'mycluster.abc123.us-east-1.redshift.amazonaws.com', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '5439', required: true, defaultValue: '5439' },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'dev', required: true },
    { key: 'schema', label: 'Schema', type: 'text', placeholder: 'public' },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'admin', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'ssl', label: 'Use SSL', type: 'checkbox', placeholder: '', defaultValue: 'true' },
  ],
  databricks: [
    { key: 'host', label: 'Workspace URL', type: 'text', placeholder: 'adb-1234567890.1.azuredatabricks.net', required: true, hint: 'Without https:// prefix' },
    { key: 'http_path', label: 'SQL Warehouse HTTP Path', type: 'text', placeholder: '/sql/1.0/warehouses/abc123def', required: true, hint: 'From SQL warehouse connection details' },
    { key: 'token', label: 'Personal Access Token', type: 'password', placeholder: 'dapi...', required: true },
    { key: 'catalog', label: 'Catalog', type: 'text', placeholder: 'hive_metastore', hint: 'Unity Catalog name' },
    { key: 'schema', label: 'Schema', type: 'text', placeholder: 'default' },
  ],
  synapse: [
    { key: 'host', label: 'Workspace SQL Endpoint', type: 'text', placeholder: 'myworkspace.sql.azuresynapse.net', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '1433', required: true, defaultValue: '1433' },
    { key: 'database', label: 'Database / SQL Pool', type: 'text', placeholder: 'mypool', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'sqladmin', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
  ],
  clickhouse: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'localhost', required: true },
    { key: 'port', label: 'HTTP Port', type: 'number', placeholder: '8123', required: true, defaultValue: '8123' },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'default', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'default', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '' },
    { key: 'ssl', label: 'Use SSL', type: 'checkbox', placeholder: '' },
  ],
  trino: [
    { key: 'host', label: 'Coordinator Host', type: 'text', placeholder: 'trino-coordinator', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '8080', required: true, defaultValue: '8080' },
    { key: 'catalog', label: 'Catalog', type: 'text', placeholder: 'hive', required: true },
    { key: 'schema', label: 'Schema', type: 'text', placeholder: 'default' },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'trino', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '' },
  ],
  // 2026-05-23 (T3): Presto — sibling of Trino. Backend uses the same
  // catalog provider, dispatched via `flavor: 'presto'` to pick the
  // prestodb client over the trino one. Port 8080 is the default for
  // both engines.
  presto: [
    { key: 'host', label: 'Coordinator Host', type: 'text', placeholder: 'presto-coordinator', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '8080', required: true, defaultValue: '8080' },
    { key: 'catalog', label: 'Catalog', type: 'text', placeholder: 'hive', required: true },
    { key: 'schema', label: 'Schema', type: 'text', placeholder: 'default' },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'presto', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '' },
  ],
  // 2026-05-23 (T3): AWS Athena — uses Glue Data Catalog (free) for
  // table listing. Connection-only fields; query execution happens via
  // the standard AWS credential chain when boto3 is invoked.
  athena: [
    { key: 'region', label: 'AWS Region', type: 'text', placeholder: 'us-east-1', required: true, defaultValue: 'us-east-1' },
    { key: 'access_key_id', label: 'Access Key ID', type: 'text', placeholder: 'AKIA...', hint: 'Leave blank to use the IAM instance role.' },
    { key: 'secret_access_key', label: 'Secret Access Key', type: 'password', placeholder: '', hint: 'Required only when Access Key ID is set.' },
    { key: 'workgroup', label: 'Workgroup', type: 'text', placeholder: 'primary', hint: 'Athena workgroup for query routing + billing.' },
    { key: 's3_output_location', label: 'S3 Output Location', type: 'text', placeholder: 's3://my-bucket/athena-results/', hint: 'Where Athena writes query results.' },
  ],
  // 2026-05-23 (T3): IBM Db2 — requires ibm_db client + UDB licence.
  // Default port 50000 (Linux/Unix instance); use 25000 for z/OS DDF.
  db2: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'db2-server', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '50000', required: true, defaultValue: '50000' },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'SAMPLE', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'db2inst1', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'ssl', label: 'Use SSL', type: 'checkbox', placeholder: '' },
  ],
  // 2026-05-23 (T3): SAP HANA — hdbcli driver. Cloud (HANA Cloud)
  // uses port 443 with encrypted=true; on-prem multi-tenant defaults
  // to 30015 / 30041.
  sap_hana: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'my-tenant.hana.cloud.ondemand.com', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '30015', required: true, defaultValue: '30015', hint: '30015 on-prem, 443 for HANA Cloud.' },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'SYSTEM', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'encrypt', label: 'Encrypt Connection', type: 'checkbox', placeholder: '', defaultValue: 'true', hint: 'Required for HANA Cloud (port 443).' },
  ],
  // 2026-05-23 (T3): Teradata — teradatasql client. Single host or
  // load-balancer DNS; no database in the connection (Teradata uses
  // database-qualified table names per query).
  teradata: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'teradata.corp.example.com', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'dbc', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'logmech', label: 'Logon Mechanism', type: 'select', placeholder: '', defaultValue: 'TD2', options: [
      { value: 'TD2', label: 'TD2 (default)' },
      { value: 'LDAP', label: 'LDAP' },
      { value: 'KRB5', label: 'Kerberos' },
    ]},
  ],
  // ── Search & Cache ──
  elasticsearch: [
    { key: 'host', label: 'Host URL', type: 'text', placeholder: 'https://es-cluster:9200', required: true, hint: 'Include protocol and port' },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'elastic' },
    { key: 'password', label: 'Password', type: 'password', placeholder: '' },
    { key: 'api_key', label: 'API Key', type: 'password', placeholder: '', hint: 'Alternative to username/password' },
    { key: 'index', label: 'Default Index', type: 'text', placeholder: 'my-index' },
    { key: 'ssl_verify', label: 'Verify SSL', type: 'checkbox', placeholder: '', defaultValue: 'true' },
  ],
  opensearch: [
    { key: 'host', label: 'Host URL', type: 'text', placeholder: 'https://opensearch:9200', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'admin' },
    { key: 'password', label: 'Password', type: 'password', placeholder: '' },
    { key: 'index', label: 'Default Index', type: 'text', placeholder: 'my-index' },
    { key: 'ssl_verify', label: 'Verify SSL', type: 'checkbox', placeholder: '', defaultValue: 'true' },
  ],
  redis: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'localhost', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '6379', required: true, defaultValue: '6379' },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', hint: 'Leave blank if no auth' },
    { key: 'database', label: 'Database Index', type: 'number', placeholder: '0', defaultValue: '0' },
    { key: 'ssl', label: 'Use SSL/TLS', type: 'checkbox', placeholder: '' },
  ],
  // ── Cloud Storage ──
  s3: [
    { key: 'bucket', label: 'Bucket Name', type: 'text', placeholder: 'my-data-bucket', required: true },
    { key: 'region', label: 'AWS Region', type: 'text', placeholder: 'us-east-1', required: true, defaultValue: 'us-east-1' },
    { key: 'access_key', label: 'Access Key ID', type: 'text', placeholder: 'AKIA...', required: true },
    { key: 'secret_key', label: 'Secret Access Key', type: 'password', placeholder: '', required: true },
    { key: 'prefix', label: 'Key Prefix', type: 'text', placeholder: 'data/raw/', hint: 'Optional folder prefix to scope access' },
    { key: 'endpoint_url', label: 'Custom Endpoint', type: 'text', placeholder: 'http://localhost:9000', hint: 'For MinIO, Ceph, or S3-compatible stores' },
  ],
  minio: [
    { key: 'endpoint_url', label: 'MinIO Endpoint', type: 'text', placeholder: 'http://minio:9000', required: true },
    { key: 'bucket', label: 'Bucket Name', type: 'text', placeholder: 'my-bucket', required: true },
    { key: 'access_key', label: 'Access Key', type: 'text', placeholder: 'minioadmin', required: true },
    { key: 'secret_key', label: 'Secret Key', type: 'password', placeholder: '', required: true },
    { key: 'region', label: 'Region', type: 'text', placeholder: 'us-east-1', defaultValue: 'us-east-1' },
    { key: 'ssl', label: 'Use SSL', type: 'checkbox', placeholder: '' },
  ],
  azure_blob: [
    { key: 'account_name', label: 'Storage Account Name', type: 'text', placeholder: 'mystorageacct', required: true },
    { key: 'container', label: 'Container', type: 'text', placeholder: 'raw-data', required: true },
    { key: 'auth_method', label: 'Authentication', type: 'select', placeholder: '', required: true, defaultValue: 'connection_string', options: [
      { value: 'connection_string', label: 'Connection String' },
      { value: 'account_key', label: 'Account Key' },
      { value: 'sas_token', label: 'SAS Token' },
      { value: 'service_principal', label: 'Service Principal (AAD)' },
    ]},
    { key: 'connection_string', label: 'Connection String', type: 'password', placeholder: 'DefaultEndpointsProtocol=https;AccountName=...' },
    { key: 'account_key', label: 'Account Key', type: 'password', placeholder: '' },
    { key: 'sas_token', label: 'SAS Token', type: 'password', placeholder: '?sv=...' },
    { key: 'tenant_id', label: 'AAD Tenant ID', type: 'text', placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' },
    { key: 'client_id', label: 'AAD Client ID', type: 'text', placeholder: '' },
    { key: 'client_secret', label: 'AAD Client Secret', type: 'password', placeholder: '' },
  ],
  adls_gen2: [
    { key: 'account_name', label: 'Storage Account Name', type: 'text', placeholder: 'mydatalake', required: true },
    { key: 'container', label: 'Filesystem / Container', type: 'text', placeholder: 'bronze', required: true },
    { key: 'auth_method', label: 'Authentication', type: 'select', placeholder: '', required: true, defaultValue: 'account_key', options: [
      { value: 'account_key', label: 'Account Key' },
      { value: 'service_principal', label: 'Service Principal (AAD)' },
      { value: 'sas_token', label: 'SAS Token' },
    ]},
    { key: 'account_key', label: 'Account Key', type: 'password', placeholder: '' },
    { key: 'tenant_id', label: 'AAD Tenant ID', type: 'text', placeholder: '' },
    { key: 'client_id', label: 'AAD Client ID', type: 'text', placeholder: '' },
    { key: 'client_secret', label: 'AAD Client Secret', type: 'password', placeholder: '' },
    { key: 'sas_token', label: 'SAS Token', type: 'password', placeholder: '' },
  ],
  gcs: [
    { key: 'project_id', label: 'GCP Project ID', type: 'text', placeholder: 'my-gcp-project', required: true },
    { key: 'bucket', label: 'Bucket Name', type: 'text', placeholder: 'my-gcs-bucket', required: true },
    { key: 'auth_method', label: 'Authentication', type: 'select', placeholder: '', required: true, defaultValue: 'service_account', options: [
      { value: 'service_account', label: 'Service Account JSON' },
      { value: 'hmac', label: 'HMAC Keys (S3-compatible)' },
    ]},
    { key: 'credentials_json', label: 'Service Account JSON', type: 'textarea', placeholder: '{ "type": "service_account", ... }', hint: 'Paste the full service account key JSON' },
    { key: 'hmac_key_id', label: 'HMAC Key ID', type: 'text', placeholder: 'GOOG1E...' },
    { key: 'hmac_secret', label: 'HMAC Secret', type: 'password', placeholder: '' },
    { key: 'prefix', label: 'Key Prefix', type: 'text', placeholder: 'data/', hint: 'Optional folder prefix' },
  ],
  // ── Files & Enterprise Docs ──
  sharepoint: [
    { key: 'tenant_id', label: 'Azure AD Tenant ID', type: 'text', placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', required: true },
    { key: 'client_id', label: 'App Client ID', type: 'text', placeholder: '', required: true, hint: 'From Azure App Registration' },
    { key: 'client_secret', label: 'Client Secret', type: 'password', placeholder: '', required: true },
    { key: 'site_url', label: 'Site URL', type: 'text', placeholder: 'https://contoso.sharepoint.com/sites/data', hint: 'SharePoint site URL' },
    { key: 'drive_id', label: 'Drive / Library ID', type: 'text', placeholder: '', hint: 'Leave blank for default Documents library' },
  ],
  onedrive: [
    { key: 'tenant_id', label: 'Azure AD Tenant ID', type: 'text', placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', required: true },
    { key: 'client_id', label: 'App Client ID', type: 'text', placeholder: '', required: true },
    { key: 'client_secret', label: 'Client Secret', type: 'password', placeholder: '', required: true },
    { key: 'user_id', label: 'User Principal', type: 'text', placeholder: 'user@contoso.com', hint: 'Blank for app-only /me/drive' },
  ],
  gdrive: [
    { key: 'credentials_json', label: 'Service Account JSON', type: 'textarea', placeholder: '{ "type": "service_account", ... }', required: true },
    { key: 'folder_id', label: 'Root Folder ID', type: 'text', placeholder: '1AbC...', hint: 'Google Drive folder ID (from URL)' },
  ],
  dropbox: [
    { key: 'access_token', label: 'Access Token', type: 'password', placeholder: 'sl.B...', required: true, hint: 'OAuth2 access token from Dropbox App Console' },
    { key: 'root_path', label: 'Root Path', type: 'text', placeholder: '/data', hint: 'Folder path to scope access' },
  ],
  ftp: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'ftp.example.com', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: 'auto', required: false, hint: 'Blank = 21 for FTP/FTPS, 22 for SFTP' },
    { key: 'protocol', label: 'Protocol', type: 'select', placeholder: '', required: true, defaultValue: 'ftp', options: [
      { value: 'ftp', label: 'FTP (port 21)' }, { value: 'ftps', label: 'FTPS (FTP over TLS)' }, { value: 'sftp', label: 'SFTP (port 22)' },
    ]},
    { key: 'username', label: 'Username', type: 'text', placeholder: 'anonymous', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '' },
    { key: 'private_key', label: 'SSH Private Key (SFTP)', type: 'password', placeholder: '-----BEGIN OPENSSH PRIVATE KEY-----', hint: 'SFTP key-based auth (PEM) — optional; leave blank to use password.' },
    { key: 'remote_path', label: 'Remote Path', type: 'text', placeholder: '/upload', hint: 'Default directory on the server' },
  ],
  gsheet: [
    { key: 'credentials_json', label: 'Service Account JSON', type: 'textarea', placeholder: '{ "type": "service_account", ... }', required: true },
    { key: 'spreadsheet_id', label: 'Spreadsheet ID', type: 'text', placeholder: '1BxiM...', required: true, hint: 'From the Google Sheets URL' },
    { key: 'sheet_name', label: 'Sheet Name', type: 'text', placeholder: 'Sheet1', hint: 'Specific tab name (blank for first sheet)' },
  ],
  // ── APIs & Integration ──
  rest_api: [
    { key: 'base_url', label: 'Base URL', type: 'text', placeholder: 'https://api.example.com/v1', required: true },
    // Z29 (2026-05-23) — expanded auth options for REST. Picker covers
    // the most-common auth modes; per-mode fields below appear when
    // relevant (Bearer → token; Basic → user+pass; OAuth2 → token URL +
    // client creds; API Key Header/Query → key + name; Custom Header →
    // free-form name + value). Backend tester + adapter honor these in
    // connections/tester.py::_test_rest_api and nodes/activities.py.
    { key: 'auth_type', label: 'Authentication', type: 'select', placeholder: '', required: true, defaultValue: 'none', options: [
      { value: 'none',           label: 'None' },
      { value: 'bearer',         label: 'Bearer Token' },
      { value: 'api_key',        label: 'API Key (Header)' },
      { value: 'api_key_query',  label: 'API Key (Query string)' },
      { value: 'basic',          label: 'Basic Auth' },
      { value: 'oauth2_cc',      label: 'OAuth 2.0 (Client Credentials)' },
      { value: 'custom_header',  label: 'Custom Header' },
    ]},
    // Common: bearer token / API key value
    { key: 'token', label: 'Token / API Key', type: 'password', placeholder: '', hint: 'Bearer token or API key value' },
    // API Key (Header): which header to put the key in
    { key: 'api_key_header', label: 'API key header name', type: 'text', placeholder: 'Authorization', hint: 'For API Key (Header) auth — defaults to Authorization' },
    // API Key (Query): which query parameter to put the key in
    { key: 'api_key_param', label: 'API key query parameter', type: 'text', placeholder: 'api_key', hint: 'For API Key (Query string) auth' },
    // Basic Auth: username + password
    { key: 'username', label: 'Username', type: 'text', placeholder: '', hint: 'For Basic Auth' },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', hint: 'For Basic Auth' },
    // OAuth 2.0 Client Credentials
    { key: 'token_url', label: 'Token URL', type: 'text', placeholder: 'https://auth.example.com/oauth/token', hint: 'OAuth 2.0 token endpoint (client_credentials grant)' },
    { key: 'client_id', label: 'Client ID', type: 'text', placeholder: '', hint: 'OAuth 2.0 client identifier' },
    { key: 'client_secret', label: 'Client Secret', type: 'password', placeholder: '', hint: 'OAuth 2.0 client secret' },
    { key: 'oauth_scope', label: 'Scope', type: 'text', placeholder: 'read:data write:data', hint: 'Space-separated OAuth scopes (optional)' },
    // Custom header: free-form
    { key: 'custom_header_name', label: 'Custom header name', type: 'text', placeholder: 'X-Auth-Token', hint: 'For Custom Header auth' },
    { key: 'custom_header_value', label: 'Custom header value', type: 'password', placeholder: '', hint: 'For Custom Header auth' },
    // Always available
    { key: 'headers', label: 'Custom Headers', type: 'textarea', placeholder: 'X-Custom: value\nAccept: application/json', hint: 'Additional headers, one per line (Name: Value)' },
    { key: 'ssl_verify', label: 'Verify SSL', type: 'checkbox', placeholder: '', defaultValue: 'true' },
  ],
  graphql: [
    { key: 'endpoint', label: 'GraphQL Endpoint', type: 'text', placeholder: 'https://api.example.com/graphql', required: true },
    { key: 'auth_type', label: 'Authentication', type: 'select', placeholder: '', defaultValue: 'none', options: [
      { value: 'none', label: 'None' }, { value: 'bearer', label: 'Bearer Token' }, { value: 'api_key', label: 'API Key' },
    ]},
    { key: 'token', label: 'Token / API Key', type: 'password', placeholder: '' },
    { key: 'headers', label: 'Custom Headers', type: 'textarea', placeholder: 'X-Custom: value', hint: 'One header per line' },
  ],
  odata: [
    { key: 'base_url', label: 'OData Service URL', type: 'text', placeholder: 'https://services.odata.org/V4/Northwind/', required: true },
    { key: 'auth_type', label: 'Authentication', type: 'select', placeholder: '', defaultValue: 'none', options: [
      { value: 'none', label: 'None' }, { value: 'basic', label: 'Basic Auth' }, { value: 'oauth2', label: 'OAuth 2.0' },
    ]},
    { key: 'username', label: 'Username', type: 'text', placeholder: '' },
    { key: 'password', label: 'Password', type: 'password', placeholder: '' },
  ],
  // 2026-05-22 — Microsoft Graph (first-class generic connector).
  // Reused by SharePoint / OneDrive / Teams / Outlook / Planner /
  // Users / Groups flows via one Azure App Registration. Auth uses
  // client_credentials so admin-consented application permissions
  // flow naturally. Defaults are pre-filled to the public Graph URL +
  // .default scope so a user only has to provide tenant_id +
  // client_id + secret to get to a working Test.
  microsoft_graph: [
    { key: 'tenant_id', label: 'Tenant ID', type: 'text', placeholder: 'common or tenant-guid', required: true, hint: 'Azure AD directory id — also accepted: "common", "organizations", or a verified domain (contoso.onmicrosoft.com).' },
    { key: 'client_id', label: 'Client ID', type: 'text', placeholder: 'Azure App Registration client id', required: true, hint: 'From Azure portal → App registrations → Overview → Application (client) ID.' },
    { key: 'client_secret', label: 'Client Secret', type: 'password', placeholder: '', required: true, hint: 'Generated under Certificates & secrets. Rotate periodically.' },
    { key: 'scope', label: 'Scope', type: 'text', placeholder: 'https://graph.microsoft.com/.default', defaultValue: 'https://graph.microsoft.com/.default', hint: 'Use .default for client_credentials. The actual permissions are decided by Azure App Registration → API permissions (with admin consent).' },
    { key: 'base_url', label: 'Base URL', type: 'text', placeholder: 'https://graph.microsoft.com/v1.0', defaultValue: 'https://graph.microsoft.com/v1.0', hint: 'Override for the beta endpoint or a sovereign cloud (Gov / China / Germany).' },
  ],
  oracle_api: [
    { key: 'base_url', label: 'Oracle ERP Base URL', type: 'text', placeholder: 'https://erp.example.com/fscmRestApi/resources', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: '', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'version', label: 'API Version', type: 'text', placeholder: 'v1', defaultValue: 'v1' },
  ],
  // ── Streaming & Messaging ──
  kafka: [
    { key: 'brokers', label: 'Bootstrap Servers', type: 'text', placeholder: 'broker1:9092, broker2:9092', required: true, hint: 'Comma-separated list of host:port' },
    { key: 'security_protocol', label: 'Security Protocol', type: 'select', placeholder: '', required: true, defaultValue: 'PLAINTEXT', options: [
      { value: 'PLAINTEXT', label: 'PLAINTEXT' }, { value: 'SSL', label: 'SSL' },
      { value: 'SASL_PLAINTEXT', label: 'SASL_PLAINTEXT' }, { value: 'SASL_SSL', label: 'SASL_SSL' },
    ]},
    { key: 'sasl_mechanism', label: 'SASL Mechanism', type: 'select', placeholder: '', options: [
      { value: 'PLAIN', label: 'PLAIN' }, { value: 'SCRAM-SHA-256', label: 'SCRAM-SHA-256' }, { value: 'SCRAM-SHA-512', label: 'SCRAM-SHA-512' },
    ], hint: 'Required when using SASL_* security protocol' },
    { key: 'username', label: 'Username', type: 'text', placeholder: '', hint: 'For SASL authentication' },
    { key: 'password', label: 'Password', type: 'password', placeholder: '' },
    { key: 'group_id', label: 'Consumer Group', type: 'text', placeholder: 'fpulse-consumer', hint: 'Default consumer group ID' },
  ],
  rabbitmq: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'rabbitmq.example.com', required: true },
    { key: 'port', label: 'AMQP Port', type: 'number', placeholder: '5672', required: true, defaultValue: '5672' },
    { key: 'management_port', label: 'Management Port', type: 'number', placeholder: '15672', hint: 'HTTP management API port' },
    { key: 'vhost', label: 'Virtual Host', type: 'text', placeholder: '/', defaultValue: '/' },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'guest', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'ssl', label: 'Use SSL', type: 'checkbox', placeholder: '' },
  ],
  pulsar: [
    { key: 'service_url', label: 'Service URL', type: 'text', placeholder: 'pulsar://localhost:6650', required: true },
    { key: 'admin_url', label: 'Admin URL', type: 'text', placeholder: 'http://localhost:8080', hint: 'For topic management operations' },
    { key: 'token', label: 'Auth Token', type: 'password', placeholder: '' },
    { key: 'tenant', label: 'Tenant', type: 'text', placeholder: 'public' },
    { key: 'namespace', label: 'Namespace', type: 'text', placeholder: 'default' },
  ],
  eventhub: [
    { key: 'connection_string', label: 'Connection String', type: 'password', placeholder: 'Endpoint=sb://myhub.servicebus.windows.net/;...', required: true, hint: 'From Azure Portal → Event Hub → Shared access policies' },
    { key: 'event_hub_name', label: 'Event Hub Name', type: 'text', placeholder: 'my-event-hub', required: true },
    { key: 'consumer_group', label: 'Consumer Group', type: 'text', placeholder: '$Default', defaultValue: '$Default' },
  ],
  kinesis: [
    { key: 'stream_name', label: 'Stream Name', type: 'text', placeholder: 'my-data-stream', required: true },
    { key: 'region', label: 'AWS Region', type: 'text', placeholder: 'us-east-1', required: true },
    { key: 'access_key', label: 'Access Key ID', type: 'text', placeholder: 'AKIA...', required: true },
    { key: 'secret_key', label: 'Secret Access Key', type: 'password', placeholder: '', required: true },
  ],
  // ── SaaS ──
  salesforce: [
    { key: 'instance', label: 'Instance', type: 'text', placeholder: 'mydomain.my', required: true, hint: 'Salesforce My Domain (without .salesforce.com)' },
    { key: 'auth_method', label: 'Authentication', type: 'select', placeholder: '', required: true, defaultValue: 'oauth2', options: [
      { value: 'oauth2', label: 'OAuth 2.0 (Refresh Token)' }, { value: 'username_password', label: 'Username + Password + Token' },
    ]},
    { key: 'client_id', label: 'Connected App Client ID', type: 'password', placeholder: '', required: true },
    { key: 'client_secret', label: 'Client Secret', type: 'password', placeholder: '', required: true },
    { key: 'refresh_token', label: 'Refresh Token', type: 'password', placeholder: '', hint: 'For OAuth 2.0 flow' },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'user@company.com', hint: 'For username/password flow' },
    { key: 'password', label: 'Password + Security Token', type: 'password', placeholder: '', hint: 'Concatenate password and security token' },
    { key: 'api_version', label: 'API Version', type: 'text', placeholder: '59.0', defaultValue: '59.0' },
  ],
  dynamics365: [
    { key: 'instance', label: 'Environment URL', type: 'text', placeholder: 'myorg.crm.dynamics.com', required: true, hint: 'Without https:// prefix' },
    { key: 'tenant_id', label: 'Azure AD Tenant ID', type: 'text', placeholder: 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx', required: true },
    { key: 'client_id', label: 'App Registration Client ID', type: 'text', placeholder: '', required: true },
    { key: 'client_secret', label: 'Client Secret', type: 'password', placeholder: '', required: true },
  ],
  sap: [
    { key: 'base_url', label: 'SAP OData Endpoint', type: 'text', placeholder: 'https://sap-server/sap/opu/odata/sap/', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: '', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'client', label: 'SAP Client', type: 'text', placeholder: '100', hint: 'SAP client number (e.g. 100, 200)' },
    { key: 'ssl_verify', label: 'Verify SSL', type: 'checkbox', placeholder: '', defaultValue: 'true' },
  ],
  // 2026-05-23 (V1): SAP S/4HANA — product-specific OData connector.
  // Same protocol shape as the legacy `sap` type but exposes the
  // odata_version + sap_client fields up front and points the user at
  // the canonical gateway URL.
  sap_s4hana: [
    { key: 'base_url', label: 'S/4HANA Gateway URL', type: 'text', placeholder: 'https://s4hana.example.com', required: true, hint: 'Base URL up to (not including) /sap/opu/odata.' },
    { key: 'username', label: 'Username', type: 'text', placeholder: '', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'sap_client', label: 'SAP Client', type: 'text', placeholder: '100', hint: 'sap-client routing parameter; leave blank if not multi-client.' },
    { key: 'odata_version', label: 'OData Version', type: 'select', placeholder: '', defaultValue: 'v2', options: [
      { value: 'v2', label: 'OData v2 (d.results + d.__next)' },
      { value: 'v4', label: 'OData v4 (value + @odata.nextLink)' },
    ]},
    { key: 'ssl_verify', label: 'Verify SSL', type: 'checkbox', placeholder: '', defaultValue: 'true' },
  ],
  // 2026-05-23 (V2): SAP SuccessFactors — HRIS OData via the api{N}.
  // successfactors datacenter URL. Login format is username@company_id.
  sap_successfactors: [
    { key: 'base_url', label: 'Datacenter URL', type: 'text', placeholder: 'https://api4.successfactors.com', required: true, hint: 'Pick your datacenter (api1 / api2 / api4 / apieu...).' },
    { key: 'company_id', label: 'Company ID', type: 'text', placeholder: 'mycompany', required: true, hint: 'SF tenant identifier; the API user logs in as <user>@<company_id>.' },
    { key: 'username', label: 'API Username', type: 'text', placeholder: 'api_user', required: true },
    { key: 'password', label: 'API Password', type: 'password', placeholder: '', required: true },
  ],
  // 2026-05-23 (U1): Oracle Fusion Cloud — REST API across FSCM / HCM /
  // CRM api_family selectors. Basic auth is the default; OAuth client
  // credentials land when Fusion's IDCS app registration UX is wired.
  oracle_fusion: [
    { key: 'base_url', label: 'Fusion Pod URL', type: 'text', placeholder: 'https://my-pod.fa.us2.oraclecloud.com', required: true },
    { key: 'username', label: 'Service Account Username', type: 'text', placeholder: '', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'api_family', label: 'API Family', type: 'select', placeholder: '', defaultValue: 'fscm', options: [
      { value: 'fscm', label: 'SCM / Finance (fscmRestApi)' },
      { value: 'hcm', label: 'HCM (hcmRestApi)' },
      { value: 'crm', label: 'CRM / Sales (crmRestApi)' },
    ]},
  ],
  // 2026-05-23 (U2): Oracle BI Publisher — report-based connector. The
  // form captures connection identity; per-pipeline report_path /
  // parameters are configured on the source node, not here.
  oracle_bip: [
    { key: 'base_url', label: 'BI Publisher URL', type: 'text', placeholder: 'https://bipublisher.example.com', required: true, hint: 'Server root; the catalog lives at /xmlpserver/...' },
    { key: 'username', label: 'BIP Username', type: 'text', placeholder: '', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'default_output_format', label: 'Default Output Format', type: 'select', placeholder: '', defaultValue: 'csv', options: [
      { value: 'csv', label: 'CSV' },
      { value: 'xml', label: 'XML' },
      { value: 'xlsx', label: 'Excel (XLSX)' },
      { value: 'pdf', label: 'PDF' },
    ]},
  ],
  // 2026-05-23 (W1): manifest-promoted SaaS. Each surfaces only the
  // auth-shaped fields the v1 manifest declares as `secret:true` or
  // `required:true` — connector-specific resource selectors (owner,
  // repo, workspace_gid, etc.) move to the source node so they can
  // vary per pipeline.
  github: [
    { key: 'personal_access_token', label: 'Personal Access Token', type: 'password', placeholder: 'ghp_...', required: true, hint: 'Create at github.com/settings/tokens with `repo` scope.' },
  ],
  shopify: [
    { key: 'shop', label: 'Shop Subdomain', type: 'text', placeholder: 'mystore', required: true, hint: 'Just the subdomain (e.g. mystore for mystore.myshopify.com).' },
    { key: 'access_token', label: 'Admin API Access Token', type: 'password', placeholder: 'shpat_...', required: true, hint: 'From your custom app under Apps and sales channels.' },
  ],
  stripe: [
    { key: 'api_key', label: 'Secret Key', type: 'password', placeholder: 'sk_live_... or sk_test_...', required: true, hint: 'From dashboard.stripe.com/apikeys — secret key only, not publishable.' },
  ],
  notion: [
    { key: 'integration_token', label: 'Internal Integration Token', type: 'password', placeholder: 'secret_...', required: true, hint: 'Create at notion.so/my-integrations.' },
    { key: 'database_id', label: 'Default Database ID', type: 'text', placeholder: '', hint: 'Optional — the source node can override per pipeline.' },
  ],
  asana: [
    { key: 'personal_access_token', label: 'Personal Access Token', type: 'password', placeholder: '1/...', required: true, hint: 'Create at app.asana.com/0/my-apps.' },
    { key: 'workspace_gid', label: 'Workspace GID', type: 'text', placeholder: '12345678', required: true, hint: 'The numeric ID of your Asana workspace.' },
  ],
  servicenow: [
    { key: 'instance', label: 'Instance Name', type: 'text', placeholder: 'mycompany', required: true, hint: 'Just the subdomain (e.g. mycompany for mycompany.service-now.com)' },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'admin', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'api_version', label: 'API Version', type: 'text', placeholder: 'now', defaultValue: 'now' },
  ],
  jira: [
    { key: 'domain', label: 'Atlassian Domain', type: 'text', placeholder: 'mycompany', required: true, hint: 'Just the subdomain (for mycompany.atlassian.net)' },
    { key: 'email', label: 'Email', type: 'text', placeholder: 'user@company.com', required: true },
    { key: 'api_token', label: 'API Token', type: 'password', placeholder: '', required: true, hint: 'Generate from id.atlassian.com → Security → API tokens' },
  ],
  workday: [
    { key: 'host', label: 'Workday Host', type: 'text', placeholder: 'wd5-impl-services1.workday.com', required: true },
    { key: 'tenant', label: 'Tenant Name', type: 'text', placeholder: 'my_tenant', required: true },
    { key: 'client_id', label: 'Client ID', type: 'text', placeholder: '', required: true },
    { key: 'client_secret', label: 'Client Secret', type: 'password', placeholder: '', required: true },
    { key: 'refresh_token', label: 'Refresh Token', type: 'password', placeholder: '' },
  ],
  hubspot: [
    { key: 'access_token', label: 'Private App Access Token', type: 'password', placeholder: 'pat-na1-...', required: true, hint: 'From HubSpot → Settings → Integrations → Private Apps' },
    { key: 'portal_id', label: 'Portal (Hub) ID', type: 'text', placeholder: '12345678', hint: 'Your HubSpot account ID' },
  ],
  zendesk: [
    { key: 'subdomain', label: 'Subdomain', type: 'text', placeholder: 'mycompany', required: true, hint: 'For mycompany.zendesk.com' },
    { key: 'email', label: 'Admin Email', type: 'text', placeholder: 'admin@company.com', required: true },
    { key: 'api_token', label: 'API Token', type: 'password', placeholder: '', required: true, hint: 'From Admin → Channels → API' },
  ],
  netsuite: [
    { key: 'account_id', label: 'Account ID', type: 'text', placeholder: '1234567_SB1', required: true, hint: 'e.g. 1234567 or 1234567_SB1 for sandbox' },
    { key: 'consumer_key', label: 'Consumer Key', type: 'text', placeholder: '', required: true },
    { key: 'consumer_secret', label: 'Consumer Secret', type: 'password', placeholder: '', required: true },
    { key: 'token_key', label: 'Token ID', type: 'text', placeholder: '', required: true },
    { key: 'token_secret', label: 'Token Secret', type: 'password', placeholder: '', required: true },
  ],
  // ── Notifications ──
  smtp: [
    { key: 'host', label: 'SMTP Host', type: 'text', placeholder: 'smtp.gmail.com', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '587', required: true, defaultValue: '587' },
    { key: 'username', label: 'Username / Email', type: 'text', placeholder: 'noreply@company.com', required: true },
    { key: 'password', label: 'Password / App Password', type: 'password', placeholder: '', required: true },
    { key: 'tls', label: 'Use STARTTLS', type: 'checkbox', placeholder: '', defaultValue: 'true' },
    { key: 'from_address', label: 'Default From Address', type: 'text', placeholder: 'noreply@company.com', hint: 'Sender email address' },
  ],
  sendgrid: [
    { key: 'api_key', label: 'API Key', type: 'password', placeholder: 'SG.xxxxx...', required: true },
    { key: 'from_email', label: 'Default From Email', type: 'text', placeholder: 'noreply@company.com', required: true },
    { key: 'from_name', label: 'Default From Name', type: 'text', placeholder: 'My Company' },
  ],
  slack: [
    { key: 'webhook_url', label: 'Webhook URL', type: 'password', placeholder: 'https://hooks.slack.com/services/T.../B.../xxx', required: true, hint: 'Incoming Webhook URL from Slack App settings' },
    { key: 'channel', label: 'Default Channel', type: 'text', placeholder: '#alerts', hint: 'Overrides webhook default channel' },
    { key: 'username', label: 'Bot Username', type: 'text', placeholder: 'F-Pulse Bot', hint: 'Display name for messages' },
  ],
  twilio: [
    { key: 'account_sid', label: 'Account SID', type: 'text', placeholder: 'AC...', required: true },
    { key: 'auth_token', label: 'Auth Token', type: 'password', placeholder: '', required: true },
    { key: 'from_number', label: 'From Phone Number', type: 'text', placeholder: '+1234567890', required: true, hint: 'Your Twilio phone number with country code' },
  ],
  // ── Observability ──
  datadog: [
    { key: 'api_key', label: 'API Key', type: 'password', placeholder: '', required: true },
    { key: 'app_key', label: 'Application Key', type: 'password', placeholder: '', required: true },
    { key: 'site', label: 'Datadog Site', type: 'select', placeholder: '', required: true, defaultValue: 'datadoghq.com', options: [
      { value: 'datadoghq.com', label: 'US1 (datadoghq.com)' },
      { value: 'us3.datadoghq.com', label: 'US3' },
      { value: 'us5.datadoghq.com', label: 'US5' },
      { value: 'datadoghq.eu', label: 'EU (datadoghq.eu)' },
      { value: 'ap1.datadoghq.com', label: 'AP1 (Asia Pacific)' },
    ]},
  ],
  pagerduty: [
    { key: 'api_key', label: 'REST API Key (v2)', type: 'password', placeholder: '', required: true, hint: 'From PagerDuty → Integrations → API Access Keys' },
    { key: 'routing_key', label: 'Integration/Routing Key', type: 'password', placeholder: '', hint: 'For Events API v2 (alerts/incidents)' },
  ],
  splunk: [
    { key: 'host', label: 'Splunk Host', type: 'text', placeholder: 'splunk.example.com', required: true },
    { key: 'port', label: 'Management Port', type: 'number', placeholder: '8089', required: true, defaultValue: '8089' },
    { key: 'token', label: 'HEC Token', type: 'password', placeholder: '', required: true, hint: 'HTTP Event Collector token' },
    { key: 'index', label: 'Default Index', type: 'text', placeholder: 'main' },
    { key: 'ssl_verify', label: 'Verify SSL', type: 'checkbox', placeholder: '', defaultValue: 'true' },
  ],
  // ── Vector / AI ──
  pinecone: [
    { key: 'api_key', label: 'API Key', type: 'password', placeholder: '', required: true },
    { key: 'environment', label: 'Environment', type: 'text', placeholder: 'us-east-1-aws', required: true, hint: 'From Pinecone console (e.g. us-east-1-aws)' },
    { key: 'index_name', label: 'Index Name', type: 'text', placeholder: 'my-index', required: true },
  ],
  weaviate: [
    { key: 'url', label: 'Weaviate URL', type: 'text', placeholder: 'http://localhost:8080', required: true },
    { key: 'api_key', label: 'API Key', type: 'password', placeholder: '', hint: 'Required for Weaviate Cloud' },
    { key: 'class_name', label: 'Default Class', type: 'text', placeholder: 'Document' },
  ],
  qdrant: [
    { key: 'url', label: 'Qdrant URL', type: 'text', placeholder: 'http://localhost:6333', required: true },
    { key: 'api_key', label: 'API Key', type: 'password', placeholder: '', hint: 'Required for Qdrant Cloud' },
    { key: 'collection', label: 'Default Collection', type: 'text', placeholder: 'my-collection' },
  ],
  chroma: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'localhost', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '8000', required: true, defaultValue: '8000' },
    { key: 'collection', label: 'Default Collection', type: 'text', placeholder: 'my-collection' },
    { key: 'token', label: 'Auth Token', type: 'password', placeholder: '', hint: 'For authenticated Chroma server' },
  ],
  pgvector: [
    { key: 'host', label: 'Host', type: 'text', placeholder: 'localhost', required: true },
    { key: 'port', label: 'Port', type: 'number', placeholder: '5432', required: true, defaultValue: '5432' },
    { key: 'database', label: 'Database', type: 'text', placeholder: 'vectors', required: true },
    { key: 'username', label: 'Username', type: 'text', placeholder: 'postgres', required: true },
    { key: 'password', label: 'Password', type: 'password', placeholder: '', required: true },
    { key: 'schema', label: 'Schema', type: 'text', placeholder: 'public' },
  ],
  // ── Custom / Other ──
  custom: [
    { key: 'base_url', label: 'Endpoint URL', type: 'text', placeholder: 'https://my-service.example.com', hint: 'Base URL or connection endpoint' },
    { key: 'auth_type', label: 'Authentication', type: 'select', placeholder: '', defaultValue: 'none', options: [
      { value: 'none', label: 'None' }, { value: 'bearer', label: 'Bearer Token' },
      { value: 'api_key', label: 'API Key' }, { value: 'basic', label: 'Basic Auth' },
    ]},
    { key: 'token', label: 'Token / API Key', type: 'password', placeholder: '' },
    { key: 'username', label: 'Username', type: 'text', placeholder: '' },
    { key: 'password', label: 'Password', type: 'password', placeholder: '' },
    { key: 'extra_config', label: 'Additional Config (JSON)', type: 'textarea', placeholder: '{"timeout": 30, "retry": 3}', hint: 'Any extra key-value pairs as JSON' },
  ],
};

/* ═══ TableToolbar column config ═══ */
const CONN_COLUMNS: TColumn[] = [
  // Core
  { key: 'name',          label: 'Connection',   default: true,  group: 'core' },
  { key: 'type',          label: 'Type',         default: true,  group: 'core' },
  { key: 'scope',         label: 'Scope',        default: true,  group: 'core' },
  // Z25 (2026-05-23) — Used by N pipelines. Backed by GET /api/connections/usage.
  { key: 'used_by',       label: 'Used by',      default: true,  group: 'core' },
  { key: 'actions',       label: 'Actions',      default: true,  group: 'core' },
  // Details
  { key: 'description',   label: 'Description',  default: true,  group: 'details' },
  { key: 'tags',          label: 'Tags',         default: false, group: 'details' },
  { key: 'reports',       label: 'Reports',      default: true,  group: 'details' },
  { key: 'last_test',     label: 'Last Test',    default: true,  group: 'details' },
  { key: 'created',       label: 'Created',      default: true,  group: 'details' },
  // Metadata
  { key: 'environment',   label: 'Environment',  default: false, group: 'metadata' },
  { key: 'credential_id', label: 'Credential',   default: false, group: 'metadata' },
  { key: 'project_id',    label: 'Project ID',   default: false, group: 'metadata' },
  { key: 'config',        label: 'Config',       default: false, group: 'metadata' },
];
const CONN_GROUPS: TColumnGroup[] = [
  { key: 'core',     label: 'Core',     icon: '◆' },
  { key: 'details',  label: 'Details',  icon: '◇' },
  { key: 'metadata', label: 'Metadata', icon: '⚙' },
];

type View = 'list' | 'create' | 'detail' | 'edit';
type ScopeFilter = 'all' | 'global' | 'project';

/**
 * Z25 (2026-05-23) — "Used by" pill + drilldown popover for the
 * Connections table. Mirrors the Storage page's UsedByPill / UsagePopover
 * shape so users get a consistent affordance across surfaces.
 *
 * Source of truth: `GET /api/connections/usage` returns
 *   { connection_id: [{ workflow_id, name, role }] }
 * where `role` is 'source' | 'sink' | 'activity' (matching the IR field
 * the workflow scanner found `connection_id` on).
 */
type ConnPipelineRef = { workflow_id: string; name: string; role?: string };
function ConnUsedByPill({
  pipelines,
  connName,
  onOpenPipeline,
}: {
  pipelines: ConnPipelineRef[];
  connName: string;
  onOpenPipeline: (workflowId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const count = pipelines.length;
  if (!count) {
    return <span className="text-slate-300 text-xs">—</span>;
  }
  return (
    <>
      <button
        onClick={(e) => { e.stopPropagation(); setOpen(true); }}
        className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-semibold rounded-full bg-blue-50 text-blue-700 hover:bg-blue-100 transition-colors"
        title={`Used by ${count} pipeline${count === 1 ? '' : 's'}`}
      >
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
          <rect width="8" height="8" x="3" y="3" rx="2" />
          <path d="M7 11v4a2 2 0 0 0 2 2h4" />
          <rect width="8" height="8" x="13" y="13" rx="2" />
        </svg>
        {count}
      </button>
      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm"
          onClick={(e) => { if (e.target === e.currentTarget) setOpen(false); }}
        >
          <div className="bg-white rounded-2xl shadow-2xl border border-slate-200/60 w-[460px] max-w-[95vw] overflow-hidden">
            <div className="px-5 py-4 border-b border-slate-200/70 bg-gradient-to-b from-slate-50 to-white">
              <div className="text-xs uppercase tracking-wide text-slate-500 font-semibold">Connection usage</div>
              <div className="text-base font-bold text-slate-900 mt-0.5 truncate">{connName}</div>
              <div className="text-xs text-slate-500 mt-1">
                {count} pipeline{count === 1 ? '' : 's'} reference{count === 1 ? 's' : ''} this connection.
              </div>
            </div>
            <div className="px-5 py-3 max-h-[60vh] overflow-auto">
              <ul className="divide-y divide-slate-100">
                {pipelines.map((p, i) => (
                  <li
                    key={`${p.workflow_id}_${i}`}
                    className="flex items-center justify-between py-2.5 gap-3"
                  >
                    <div className="min-w-0 flex items-center gap-2">
                      <div className="text-sm font-medium text-slate-900 truncate">{p.name}</div>
                      {p.role && (
                        <span
                          className={`text-[10px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded ${
                            p.role === 'source'
                              ? 'bg-emerald-50 text-emerald-700 border border-emerald-200'
                              : p.role === 'destination' || p.role === 'sink'
                              ? 'bg-amber-50 text-amber-700 border border-amber-200'
                              : 'bg-slate-100 text-slate-600 border border-slate-200'
                          }`}
                          title={
                            p.role === 'source' ? 'This pipeline READS from this connection'
                            : (p.role === 'destination' || p.role === 'sink') ? 'This pipeline WRITES to this connection'
                            : 'This pipeline uses this connection in an activity step'
                          }
                        >
                          {p.role}
                        </span>
                      )}
                    </div>
                    {p.workflow_id && (
                      <button
                        onClick={() => { onOpenPipeline(p.workflow_id); setOpen(false); }}
                        className="text-xs font-semibold text-blue-600 hover:text-blue-700 whitespace-nowrap"
                      >
                        Open →
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            </div>
            <div className="px-5 py-3 border-t border-slate-200/70 bg-slate-50/60 flex justify-end">
              <button
                onClick={() => setOpen(false)}
                className="px-4 py-2 text-sm font-medium rounded-lg text-slate-600 hover:bg-slate-200/70"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export default function ConnectionsPage({ projectId, projectName: activeProjectName = '', onClearProject, onGoToProjects, environment = 'dev', tier = 'free' }: { projectId?: string | null; projectName?: string; onClearProject?: () => void; onGoToProjects?: () => void; environment?: 'dev' | 'prod'; tier?: string } = {}) {
  const dark = useDarkMode();
  const [connections, setConnections] = useState<Connection[]>([]);
  // Stats panel toggle — same pattern as the Executions page Stats
  // button. Persisted to localStorage so the user's preference
  // survives reloads.
  const [showDashboard, setShowDashboard] = useState<boolean>(() => {
    try { return localStorage.getItem('fpulse_connections_show_stats') !== '0'; }
    catch { return true; }
  });
  useEffect(() => {
    try { localStorage.setItem('fpulse_connections_show_stats', showDashboard ? '1' : '0'); } catch {}
  }, [showDashboard]);
  const [projects, setProjects] = useState<Project[]>([]);
  const colState = useTableColumns('fpulse_connections_cols', CONN_COLUMNS);
  const canCreate = useCan('create', environment);
  const canDelete = useCan('delete', environment);
  const [view, setView] = useState<View>('list');
  const [selectedConnection, setSelectedConnection] = useState<Connection | null>(null);
  // Quick-detail drawer (master-detail pattern). Row clicks open this
  // lightweight drawer showing metadata + actions. The full edit page
  // (`view === 'detail'`) is reachable from the drawer's "Open full
  // details" button.
  const [drawerConn, setDrawerConn] = useState<Connection | null>(null);
  // Z32 (2026-05-23) — Pipeline Data Prep wand removed per user feedback.
  // The on-row wand opened a 3-step scaffold dialog (catalog browse →
  // pick stream → 3-node draft pipeline). User flagged the affordance as
  // unhelpful — discovery happens better through the Editor's source
  // palette + the connection's catalog endpoint. Backend endpoint
  // /api/connections/{id}/scaffold-cleanup also removed.
  const [reports, setReports] = useState<ConnectionReport[]>([]);
  // 2026-05-25 — Connection Detail polish: pipelines that reference the
  // selected connection. Computed client-side from /workflows because the
  // server has no /connections/{id}/used-by endpoint. List is bounded
  // (< few hundred pipelines for SMB customers) so this is acceptable.
  const [usedByPipelines, setUsedByPipelines] = useState<Array<{ id: string; name: string; project_id?: string | null }>>([]);
  const [usedByLoading, setUsedByLoading] = useState(false);
  // Mask state for sensitive config fields (password / token / secret).
  // Click-to-reveal per field, keyed by field name. Cleared on connection
  // switch so a peek doesn't survive into another connection.
  const [revealedSecrets, setRevealedSecrets] = useState<Set<string>>(new Set());
  // Visual feedback for the "copy to clipboard" buttons on config rows.
  // Keyed by field name; reset to null after 1.5s.
  const [copiedField, setCopiedField] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // 2026-05-19 (P1 #4 of PAGE_BY_PAGE_AUDIT.md): explicit load error so
  // the body can render <ErrorBanner> instead of falling through to the
  // empty state with an inviting "+ New Connection" CTA.
  const [loadError, setLoadError] = useState<string | null>(null);
  // Density (D-003 default Comfortable, persisted per page in localStorage).
  const { density, setDensity } = useDensity('connections');
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>('all');
  const [projectFilter, setProjectFilter] = useState<string>('');
  const [searchQuery, setSearchQuery] = useState('');

  // Create form
  const [formName, setFormName] = useState('');
  const [formType, setFormType] = useState('');
  const [createStep, setCreateStep] = useState<0 | 1 | 2>(0);
  const [connectorSearch, setConnectorSearch] = useState('');
  const [activeConnectorCategory, setActiveConnectorCategory] = useState<string>('All');
  // Beta confirmation modal state (May 3 2026). Pending connector type
  // is set when the user clicks a Beta tile they haven't acknowledged.
  // Acknowledgment is persisted to localStorage as a Set of types so the
  // modal only appears once per connector per browser.
  const [pendingBetaType, setPendingBetaType] = useState<string | null>(null);
  const ACK_BETA_KEY = 'fpulse-beta-acks';
  const getBetaAcks = (): Set<string> => {
    try {
      const raw = localStorage.getItem(ACK_BETA_KEY);
      return new Set<string>(raw ? JSON.parse(raw) : []);
    } catch {
      return new Set<string>();
    }
  };
  const ackBeta = (type: string) => {
    try {
      const set = getBetaAcks();
      set.add(type);
      localStorage.setItem(ACK_BETA_KEY, JSON.stringify(Array.from(set)));
    } catch {
      // localStorage disabled — modal will appear every time, but the
      // connector still works after acknowledgment.
    }
  };
  const [formDesc, setFormDesc] = useState('');
  const [formConfig, setFormConfig] = useState<Record<string, string>>({});
  const [formTags, setFormTags] = useState('');
  const [formScope, setFormScope] = useState<'global' | 'project'>('global');
  const [formProjectId, setFormProjectId] = useState('');
  // Environment visibility on the connection. Default to current page env
  // so a connection created in DEV stays in DEV unless the user explicitly
  // picks 'Both'. Matches CredentialsPage behaviour.
  const [formEnvironment, setFormEnvironment] = useState<'dev' | 'prod' | 'all'>(environment || 'dev');
  // Direction capabilities. Both checked by default; auto-unchecks 'read'
  // when the user picks a notifier type (slack/smtp/etc). User can flip
  // either box manually.
  const [formCanRead, setFormCanRead] = useState(true);
  const [formCanWrite, setFormCanWrite] = useState(true);

  // Report form
  const [showReportForm, setShowReportForm] = useState(false);
  const [reportName, setReportName] = useState('');
  const [reportDesc, setReportDesc] = useState('');
  const [reportQuery, setReportQuery] = useState('');
  const [reportParams, setReportParams] = useState<Array<{ name: string; type: string; default: string; required: boolean }>>([]);

  // Run report
  const [runningReport, setRunningReport] = useState<string | null>(null);
  const [reportResult, setReportResult] = useState<any>(null);
  const [paramValues, setParamValues] = useState<Record<string, string>>({});

  // Sync projectId prop into projectFilter
  useEffect(() => {
    if (projectId) setProjectFilter(projectId);
  }, [projectId]);

  useEffect(() => {
    loadConnections();
    loadProjects();
  }, []);

  useEffect(() => {
    loadConnections();
  }, [scopeFilter, projectFilter]);

  // Z25 (2026-05-23) — lineage: which pipelines reference each connection.
  // Shape: { conn_id: [{ workflow_id, name, role }, ...] }. Loaded in
  // parallel with the connection list so the Used-by column populates
  // on first paint instead of fetching per-row.
  const [usageMap, setUsageMap] = useState<Record<string, Array<{ workflow_id: string; name: string; role?: string }>>>({});

  const loadConnections = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const params: { project_id?: string; scope?: string } = {};
      if (scopeFilter === 'global') params.scope = 'global';
      else if (scopeFilter === 'project') params.scope = 'project';
      if (projectFilter) params.project_id = projectFilter;
      const [data, usage] = await Promise.all([
        api.listConnections(params),
        api.get<Record<string, any[]>>('/api/connections/usage').catch(() => ({})),
      ]);
      setConnections(data);
      setUsageMap(usage as any);
    } catch (err: any) {
      // 2026-05-19 (P1 #4 of PAGE_BY_PAGE_AUDIT.md): silent catch made a
      // backend hiccup look like "no connections" — the empty-state CTA
      // then invited the user to create one rather than retry. We now
      // surface the failure so the user can distinguish API-down from
      // genuinely-empty.
      setConnections([]);
      setLoadError(err?.message || 'Failed to load connections');
    }
    setLoading(false);
  };

  const loadProjects = async () => {
    try {
      const data = await api.listProjects();
      setProjects(data);
    } catch { setProjects([]); }
  };

  // Publish page context for the AI Copilot — lets the agent answer
  // "which connections are write-only?" / "find Postgres ones" without
  // a discovery tool call.
  usePageContext({
    page: 'connections',
    visible_ids: connections.map((c) => c.id),
    filters: { scope: scopeFilter, project: projectFilter ?? null },
    environment,
    visible_items: connections.map((c) => ({
      id: c.id,
      name: c.name,
      kind: 'connection',
      meta: {
        type: c.type,
        environment: c.environment ?? null,
        capabilities: (c.capabilities ?? []).join(',') || null,
        project_id: c.project_id ?? null,
        report_count: c.report_count ?? null,
        last_test_ok: c.last_test_ok ?? null,
        last_test_at: c.last_test_at ?? null,
        last_test_error: c.last_test_error ? c.last_test_error.slice(0, 180) : null,
      },
    })),
  });

  const loadReports = async (connId: string) => {
    try {
      const data = await api.listConnectionReports(connId);
      setReports(data);
    } catch { setReports([]); }
  };

  const handleCreate = async () => {
    if (!formName || !formType) return;
    // Block save when neither read nor write is checked — backend would
    // happily store the empty list but the connection would be unusable.
    if (!formCanRead && !formCanWrite) {
      toast.error('Pick at least one role', 'A connection needs Source or Sink (or both) to be usable.');
      return;
    }
    try {
      await api.createConnection({
        name: formName, type: formType, description: formDesc,
        config: formConfig,
        tags: formTags.split(',').map(t => t.trim()).filter(Boolean),
        project_id: formScope === 'project' ? formProjectId || null : null,
        environment: formEnvironment,
        capabilities: capabilitiesFromForm(formCanRead, formCanWrite),
      });
      setView('list');
      resetCreateForm();
      loadConnections();
    } catch (e: any) { toast.error('Operation failed', e?.message || 'Unknown error'); }
  };

  const handleUpdate = async () => {
    if (!selectedConnection || !formName) return;
    if (!formCanRead && !formCanWrite) {
      toast.error('Pick at least one role', 'A connection needs Source or Sink (or both) to be usable.');
      return;
    }
    try {
      await api.updateConnection(selectedConnection.id, {
        name: formName, description: formDesc,
        config: formConfig,
        tags: formTags.split(',').map(t => t.trim()).filter(Boolean),
        project_id: formScope === 'project' ? formProjectId || null : null,
        environment: formEnvironment,
        capabilities: capabilitiesFromForm(formCanRead, formCanWrite),
      });
      toast.success('Connection updated');
      setView('list');
      resetCreateForm();
      loadConnections();
    } catch (e: any) { toast.error('Update failed', e?.message || 'Unknown error'); }
  };

  const resetCreateForm = () => {
    setFormName(''); setFormType(''); setFormDesc(''); setFormConfig({});
    setFormTags(''); setFormScope('global'); setFormProjectId('');
    setCreateStep(0);
    setFormEnvironment(environment || 'dev');
    setFormCanRead(true);
    setFormCanWrite(true);
  };

  const handleDelete = async (id: string) => {
    // 2026-06-04 — upgraded to match the Storage gold-standard
    // delete-confirm pattern (StoragePage.onDeleteFile). The previous
    // generic "Delete this connection?" message gave users no signal
    // about downstream impact, so a connection used by 12 pipelines
    // would silently break all 12 on next run. Now: read the cached
    // usage map (already populated by /api/connections/usage on page
    // load), surface the dependent count + first three pipeline names
    // in the dialog, and warn "they will fail on next run."
    const conn = connections.find((c) => c.id === id);
    const name = conn?.name || 'this connection';
    const usedBy = (usageMap as any)?.[id] || [];
    const usageBlurb =
      usedBy.length > 0
        ? ` ${usedBy.length} pipeline${usedBy.length === 1 ? '' : 's'} reference this connection (${usedBy
            .slice(0, 3)
            .map((p: any) => p?.name || p?.workflow_name || p?.id || 'unnamed')
            .join(', ')}${usedBy.length > 3 ? `, +${usedBy.length - 3} more` : ''}) — they will fail on next run until you wire them to a different connection.`
        : '';
    const ok = await uiConfirm({
      title: `Delete "${name}"?`,
      message: `This deletes the connection and all its reports.${usageBlurb}`,
      confirmLabel: 'Delete',
      destructive: true,
    });
    if (!ok) return;
    try {
      await api.deleteConnection(id);
      setView('list');
      loadConnections();
    } catch (e: any) { toast.error('Operation failed', e?.message || 'Unknown error'); }
  };

  const handleCreateReport = async () => {
    if (!selectedConnection || !reportName || !reportQuery) return;
    try {
      await api.createConnectionReport(selectedConnection.id, {
        name: reportName, description: reportDesc,
        query_template: reportQuery, parameters: reportParams,
      });
      setShowReportForm(false);
      setReportName(''); setReportDesc(''); setReportQuery(''); setReportParams([]);
      loadReports(selectedConnection.id);
    } catch (e: any) { toast.error('Operation failed', e?.message || 'Unknown error'); }
  };

  const handleRunReport = async (reportId: string) => {
    if (!selectedConnection) return;
    setRunningReport(reportId);
    setReportResult(null);
    try {
      const result = await api.runConnectionReport(selectedConnection.id, reportId, paramValues);
      setReportResult(result);
    } catch (e: any) { setReportResult({ error: e.message }); }
    setRunningReport(null);
  };

  // Connection test wizard state
  const [testDialogOpen, setTestDialogOpen] = useState(false);
  const [testConnectionId, setTestConnectionId] = useState<string | null>(null);
  const [testSteps, setTestSteps] = useState<Array<{ label: string; status: 'pending' | 'running' | 'success' | 'error'; detail?: string }>>([]);
  const [testDone, setTestDone] = useState(false);

  // Z28 (2026-05-23) — inline test for the New Connection form. Posts
  // the current draft (type + config) to /api/connections/test-inline
  // so the user can validate credentials BEFORE saving. No persistence;
  // pure read-only probe on the F-Pulse host.
  const [inlineTestRunning, setInlineTestRunning] = useState(false);
  const [inlineTestResult, setInlineTestResult] = useState<{
    ok: boolean;
    latency_ms?: number;
    detail?: string;
    at: number; // wall-clock for the "tested 5s ago" hint
  } | null>(null);
  const handleInlineTest = async () => {
    if (!formType) return;
    setInlineTestRunning(true);
    setInlineTestResult(null);
    const started = Date.now();
    try {
      const res = await api.post<any>('/api/connections/test-inline', {
        type: formType,
        config: { ...formConfig },
        // credential_id intentionally omitted — the form is in CREATE
        // mode so there's no saved credential to reference. If the user
        // later switches to "use saved credential", we can add a picker.
      });
      const ok = res?.success === true || res?.status === 'ok' || res?.ok === true;
      setInlineTestResult({
        ok,
        latency_ms: typeof res?.latency_ms === 'number' ? res.latency_ms : Date.now() - started,
        detail: res?.detail || res?.message || (ok ? 'Connection successful' : 'Connection failed'),
        at: Date.now(),
      });
    } catch (err: any) {
      setInlineTestResult({
        ok: false,
        latency_ms: Date.now() - started,
        detail: err?.message || 'Test request failed',
        at: Date.now(),
      });
    }
    setInlineTestRunning(false);
  };
  // Clear stale test result when the user changes type or edits config
  // — the previous "✓ connected" green is misleading after the URL changes.
  useEffect(() => { setInlineTestResult(null); }, [formType, formConfig]);

  const handleTestConnection = async (id: string) => {
    const conn = connections.find(c => c.id === id);
    const meta = conn ? typeMeta(conn.type) : null;
    const connType = meta?.category || 'Connection';

    // 2026-05-19 (P0 #5 of PAGE_BY_PAGE_AUDIT.md): the previous wizard
    // animated five steps (resolve host → connect → authenticate → query →
    // measure latency) but the backend test is a single atomic round-trip,
    // so the first four steps were pure setTimeout choreography that lied
    // about what was happening. A failure on the wire still marked the
    // earlier "resolved / authenticated" steps green even though none of
    // them ran. We now show one honest step ("Testing <type> connection")
    // that flips to success/error with the real backend result.
    const honestLabel =
      connType === 'Database' ? 'Testing database connection (SELECT 1)'
      : connType === 'API'    ? 'Testing API endpoint (health check)'
      : `Testing ${meta?.label || conn?.type || 'connection'}`;
    const steps = [{ label: honestLabel, status: 'running' as const }];

    setTestSteps(steps);
    setTestConnectionId(id);
    setTestDialogOpen(true);
    setTestDone(false);

    try {
      const result = await api.testConnection(id);
      if (result.success === true || result.status === 'ok') {
        const latency = result.details?.latency_ms ?? result.latency_ms;
        const detail = latency != null ? `${latency}ms round-trip` : 'reachable';
        setTestSteps([{ label: honestLabel, status: 'success', detail }]);
      } else {
        const baseDetail = [result.message, result.error].filter(Boolean).join(' — ') || 'Unknown error';
        const detail = result.suggestion ? `${baseDetail} · ${result.suggestion}` : baseDetail;
        setTestSteps([{ label: honestLabel, status: 'error', detail }]);
      }
    } catch (e: any) {
      setTestSteps([{ label: honestLabel, status: 'error', detail: e?.message || 'Connection refused' }]);
    }
    setTestDone(true);
  };

  const openDetail = (conn: Connection) => {
    setSelectedConnection(conn);
    setView('detail');
    loadReports(conn.id);
    setReportResult(null);
    // Reset peek state when switching connections so a revealed password
    // from one connection doesn't leak into the next.
    setRevealedSecrets(new Set());
    setCopiedField(null);
    // Compute the "Used By" list — pipelines whose IR mentions this
    // connection. We deep-scan node configs because connection refs can
    // appear under various keys (`connection_id`, `source_connection_id`,
    // `sink_connection_id`, nested inside `params`, etc.). Match any
    // string value equal to the connection's UUID.
    setUsedByLoading(true);
    api.listWorkflows()
      .then((wfs: any[]) => {
        const matches: Array<{ id: string; name: string; project_id?: string | null }> = [];
        const target = conn.id;
        const refsConnection = (obj: any): boolean => {
          if (obj == null) return false;
          if (typeof obj === 'string') return obj === target;
          if (Array.isArray(obj)) return obj.some(refsConnection);
          if (typeof obj === 'object') {
            return Object.values(obj).some(refsConnection);
          }
          return false;
        };
        for (const wf of wfs) {
          if (refsConnection(wf.ir) || refsConnection(wf.nodes) || refsConnection(wf.graph)) {
            matches.push({ id: wf.id, name: wf.name, project_id: wf.project_id ?? null });
          }
        }
        setUsedByPipelines(matches);
      })
      .catch(() => setUsedByPipelines([]))
      .finally(() => setUsedByLoading(false));
  };

  const typeMeta = (type: string) => CONNECTION_TYPES.find(t => t.type === type) || CONNECTION_TYPES[CONNECTION_TYPES.length - 1];
  const projectName = (pid: string | null) => {
    if (!pid) return 'Global';
    const p = projects.find(pr => pr.id === pid);
    return p ? p.name : pid;
  };

  // ── Scope badge ──
  const ScopeBadge = ({ projectId }: { projectId: string | null }) => (
    projectId ? (
      <span className="text-[9px] px-1.5 py-0.5 bg-blue-100 text-blue-600 rounded-full font-semibold">
        {projectName(projectId)}
      </span>
    ) : (
      <span className="text-[9px] px-1.5 py-0.5 bg-emerald-100 text-emerald-600 rounded-full font-semibold">
        Global
      </span>
    )
  );

  // ── TEST CONNECTION DIALOG (overlay) ──
  const TestDialog = () => {
    if (!testDialogOpen) return null;
    const conn = connections.find(c => c.id === testConnectionId);
    const meta = conn ? typeMeta(conn.type) : null;
    const hasError = testSteps.some(s => s.status === 'error');
    const allSuccess = testDone && !hasError;

    return (
      <div className="fixed inset-0 bg-black/40 backdrop-blur-sm z-[60] flex items-center justify-center p-4">
        <div className="bg-white rounded-2xl shadow-2xl max-w-md w-full p-6">
          <div className="flex items-center gap-3 mb-6">
            <div className="w-10 h-10 rounded-lg flex items-center justify-center" style={{ background: meta?.color ? `${meta.color}20` : '#f1f5f9' }}>
              <ConnectorIcon type={conn?.type || 'custom'} size={24} />
            </div>
            <div>
              <h3 className="text-sm font-bold text-slate-800">Testing Connection</h3>
              <p className="text-xs text-slate-500">{conn?.name || 'Unknown'} · {meta?.label || conn?.type}</p>
            </div>
          </div>
          <div className="space-y-3 mb-6">
            {testSteps.map((step, i) => (
              <div key={i} className="flex items-center gap-3">
                <div className="w-6 h-6 rounded-full flex items-center justify-center shrink-0">
                  {step.status === 'pending' && <div className="w-2 h-2 rounded-full bg-slate-200" />}
                  {step.status === 'running' && <div className="w-4 h-4 border-2 border-amber-400 border-t-transparent rounded-full animate-spin" />}
                  {step.status === 'success' && (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#22c55e" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                  )}
                  {step.status === 'error' && (
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ef4444" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <span className={`text-xs font-medium ${step.status === 'error' ? 'text-red-600' : step.status === 'success' ? 'text-slate-700' : step.status === 'running' ? 'text-amber-600' : 'text-slate-400'}`}>
                    {step.label}
                  </span>
                  {step.detail && <span className={`ml-2 text-xs ${step.status === 'error' ? 'text-red-400' : 'text-green-500'}`}>{step.status === 'error' ? step.detail : `✓ ${step.detail}`}</span>}
                </div>
              </div>
            ))}
          </div>
          {testDone && (
            <div className={`rounded-lg p-3 mb-4 flex items-center gap-2 ${allSuccess ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'}`}>
              <span className="text-lg">{allSuccess ? '✅' : '❌'}</span>
              <div>
                <p className={`text-xs font-bold ${allSuccess ? 'text-green-700' : 'text-red-700'}`}>{allSuccess ? 'Connection successful!' : 'Connection failed'}</p>
                <p className={`text-xs ${allSuccess ? 'text-green-500' : 'text-red-400'}`}>{allSuccess ? 'All checks passed. Ready to use.' : 'Check your settings and try again.'}</p>
              </div>
            </div>
          )}
          <div className="flex gap-2 justify-end">
            {testDone && hasError && (
              <button onClick={() => { setTestDialogOpen(false); if (testConnectionId) { const c = connections.find(co => co.id === testConnectionId); if (c) openDetail(c); } }} className="px-3 py-1.5 text-xs font-medium rounded-lg bg-amber-50 text-amber-700 border border-amber-200 hover:bg-amber-100">Edit Connection</button>
            )}
            {testDone && hasError && (
              <button onClick={() => { if (testConnectionId) handleTestConnection(testConnectionId); }} className="px-3 py-1.5 text-xs font-medium rounded-lg bg-blue-50 text-blue-700 border border-blue-200 hover:bg-blue-100">Retry</button>
            )}
            <button onClick={() => setTestDialogOpen(false)} className="px-3 py-1.5 text-xs font-medium rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200">{testDone ? 'Close' : 'Cancel'}</button>
          </div>
        </div>
      </div>
    );
  };

  // ── LIST VIEW ──
  if (view === 'list') {
    // Strict env filter (2026-04-21): PROD connections never appear in DEV
    // and vice versa. Untagged legacy rows are hidden — edit them to pick
    // dev/prod/all, or run a one-time backfill.
    const envFiltered = environment
      ? connections.filter(c => c.environment === 'all' || c.environment === environment)
      : connections;
    const searchFiltered = searchQuery.trim()
      ? envFiltered.filter(c => {
          const q = searchQuery.toLowerCase();
          return (c.name || '').toLowerCase().includes(q) || (c.type || '').toLowerCase().includes(q) || (c.description || '').toLowerCase().includes(q) || (c.tags || []).some(t => (t || '').toLowerCase().includes(q));
        })
      : envFiltered;
    const globalCount = searchFiltered.filter(c => !c.project_id).length;
    const projectCount = searchFiltered.filter(c => !!c.project_id).length;

    return (
      <>
      <TestDialog />
      <div className="flex-1 flex flex-col overflow-hidden">
        <ReadOnlyBanner environment={environment} />
        {/* Z31 (2026-05-23) — push-don't-overlay for the connection detail
            drawer. `--fp-drawer-w` is published by the open DetailDrawer
            (pushContent=true below). Padding-right reflows the list +
            sticky header so the rightmost columns aren't clipped. */}
        <div
          className="flex-1 overflow-auto"
          style={{ paddingRight: 'var(--fp-drawer-w, 0px)', transition: 'padding-right 250ms ease-out' }}
        >
        {/* Header — 3-col grid (matches Insights / Settings):
            • LEFT:   page title cluster (this page is "All Connections"
                      since the Connections hub family treats this as the
                      list-view tab; the sibling Credentials tab renders
                      its own header with title "Credentials").
            • CENTER: HubTabs — the Connections-family submenu.
            • RIGHT:  page-specific actions (Stats, + New Connection). */}
        <PageHeader
          environment={environment}
          icon={
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={environment === 'prod' ? 'text-red-400' : 'text-blue-500'}>
              <path d="M4 11a9 9 0 0 1 9 9" /><path d="M4 4a16 16 0 0 1 16 16" /><circle cx="5" cy="19" r="1" />
            </svg>
          }
          title={environment === 'prod' ? 'Production Connections' : 'All Connections'}
          titleAccessory={<TierChip tier={tier} environment={environment} />}
          subtitle={environment === 'prod'
            ? 'Production data connections and integration endpoints'
            : 'Manage data connections — Global or Project-scoped'}
          tabs={
            <HubTabs
              tabs={CONNECTIONS_TABS}
              active="connections"
              onNavigate={(p) => { window.location.hash = p; }}
              environment={environment}
            />
          }
          actions={
            <div className="flex justify-end items-center gap-2">
              <button
                onClick={() => setShowDashboard(s => !s)}
                title={showDashboard ? 'Hide KPI cards' : 'Show KPI cards'}
                className={`px-4 py-2 text-sm font-medium rounded-lg border transition-colors ${
                  showDashboard
                    ? 'bg-blue-50 text-blue-600 border-blue-200'
                    : 'bg-white text-slate-400 border-slate-200 hover:text-slate-600'
                }`}
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="inline mr-1 -mt-0.5">
                  <rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18" /><path d="M9 21V9" />
                </svg>
                Stats
              </button>
              {canCreate && (
              <button
                onClick={() => setView('create')}
                className="px-4 py-2 text-white text-sm font-bold rounded-lg transition-all shadow-sm hover:shadow-md"
                style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
              >
                + New Connection
              </button>
              )}
            </div>
          }
        />

        <ProjectContextBar
          projectId={projectId}
          projectName={activeProjectName}
          onGoToProjects={onGoToProjects || (() => {})}
          onClear={onClearProject || (() => {})}
        />

        <div className="w-full max-w-[1500px] mx-auto p-6">
          {/* Hero KPI cards — matches Executions / Pipelines / Pool visual
              family (HeroCard gradient + centered icon + value). DEV uses
              lighter 400→500 gradients; PROD uses richer 500→600.
              Wrapped in showDashboard so the Stats button collapses
              the entire KPI strip. */}
          {showDashboard && (() => {
            const isProd = environment === 'prod';
            // KPI counters count what the user can SEE in the table below
            // (envFiltered + searchFiltered), not the raw fetch. The prior
            // implementation counted `connections` (raw) and produced an
            // alarming Total: 3 / Stale: 3 with a "0 connections" table when
            // the env filter dropped every row — a 2026-05-22 user report.
            // Keep counters and table tied to the same source.
            const visible = searchFiltered;
            const total = visible.length;
            const healthy = visible.filter((c: any) => {
              const last = c.last_test_at || c.last_tested_at;
              if (!last) return false;
              const ms = new Date(last).getTime();
              if (!ms || isNaN(ms)) return false;
              return c.last_test_ok !== false && (Date.now() - ms) < 24 * 3600 * 1000;
            }).length;
            const stale = visible.filter((c: any) => {
              const last = c.last_test_at || c.last_tested_at;
              if (!last) return true;
              const ms = new Date(last).getTime();
              if (!ms || isNaN(ms)) return true;
              return (Date.now() - ms) >= 24 * 3600 * 1000;
            }).length;
            const failing = visible.filter((c: any) => c.last_test_ok === false).length;
            const projectScoped = visible.filter((c: any) => !!c.project_id && c.project_id !== 'global').length;
            return (
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3 mb-5">
                <HeroCard
                  gradient={isProd ? 'from-indigo-500 to-indigo-600' : 'from-indigo-400 to-indigo-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 11a9 9 0 0 1 9 9" /><path d="M4 4a16 16 0 0 1 16 16" /><circle cx="5" cy="19" r="1.5" fill="currentColor" /></svg>}
                  label="Total"
                  value={String(total)}
                />
                <HeroCard
                  gradient={isProd ? 'from-emerald-500 to-emerald-600' : 'from-emerald-400 to-emerald-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>}
                  label="Healthy"
                  value={String(healthy)}
                />
                <HeroCard
                  gradient={isProd ? 'from-amber-500 to-orange-600' : 'from-amber-400 to-orange-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>}
                  label="Stale"
                  value={String(stale)}
                />
                <HeroCard
                  gradient={isProd ? 'from-red-500 to-rose-600' : 'from-red-400 to-rose-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>}
                  label="Failing"
                  value={String(failing)}
                />
                <HeroCard
                  gradient={isProd ? 'from-violet-500 to-purple-600' : 'from-violet-400 to-purple-500'}
                  icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></svg>}
                  label="Project-scoped"
                  value={String(projectScoped)}
                />
              </div>
            );
          })()}

          {/* Scope filter tabs */}
          <div className="flex items-center gap-4 mb-5">
            <div className="flex bg-slate-100 rounded-lg p-0.5">
              {([
                // 2026-06-03 — `All` was using raw `connections.length` while
                // Global/Project used the search/env-filtered base, so the
                // tab labels read "All 5 · Global 1 · Project 0" while the
                // stat tile showed TOTAL: 1. Switch `All` to the same
                // filtered base so the three counts always sum correctly
                // and match the visible row count.
                { key: 'all' as ScopeFilter, label: 'All', count: globalCount + projectCount },
                { key: 'global' as ScopeFilter, label: 'Global', count: globalCount },
                { key: 'project' as ScopeFilter, label: 'Project', count: projectCount },
              ]).map(tab => (
                <button
                  key={tab.key}
                  onClick={() => setScopeFilter(tab.key)}
                  className={`px-3 py-1.5 text-xs font-semibold rounded-md transition-all ${
                    scopeFilter === tab.key
                      ? 'bg-white text-slate-800 shadow-sm'
                      : 'text-slate-500 hover:text-slate-700'
                  }`}
                >
                  {tab.label} <span className="text-slate-400 ml-0.5">{tab.count}</span>
                </button>
              ))}
            </div>

            {/* Project dropdown filter */}
            {scopeFilter !== 'global' && projects.length > 0 && (
              <select
                value={projectFilter}
                onChange={e => setProjectFilter(e.target.value)}
                className="text-xs px-2.5 py-1.5 border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-300"
              >
                <option value="">All Projects</option>
                {projects.map(p => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </select>
            )}

          </div>

          {loadError && !loading && (
            <div className="mb-4">
              <ErrorBanner
                title="Couldn't load connections"
                message={`${loadError} — this is different from "no connections yet". Retry, or open the API logs.`}
                onRetry={loadConnections}
              />
            </div>
          )}
          {loading ? (
            <DelayedSkeleton>
              <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 xl:grid-cols-3">
                {Array.from({ length: 6 }).map((_, i) => <SkeletonCard key={i} />)}
              </div>
            </DelayedSkeleton>
          ) : connections.length === 0 && !loadError ? (
            <EmptyState
              icon={
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <path d="M4 11a9 9 0 0 1 9 9" /><path d="M4 4a16 16 0 0 1 16 16" /><circle cx="5" cy="19" r="1" />
                </svg>
              }
              title="No connections yet"
              body="Create a connection to a database, API, or cloud service. Connections are reusable across all your pipelines."
              primaryCta={canCreate ? { label: '+ Create First Connection', onClick: () => setView('create') } : undefined}
              // 2026-05-29: surface the "you can build it yourself" path on
              // the empty state itself. The most common new-user reaction to
              // "37 connectors in the catalog" is "is mine in there?" — and
              // when it's not, the old-ETL-tool reflex is to file a ticket
              // and wait. We want the reflex to be "open Author Connector".
              // The two secondary CTAs cover both paths: build in-product
              // (90 sec from OpenAPI, lives under Insights → Author Connector
              // at the `#ai?tab=author` route), or request a first-party
              // build via GitHub issue templates.
              secondaryCtas={canCreate ? [
                { label: 'Build your own (90s)', href: '#author', variant: 'secondary' },
                { label: 'Request a connector', href: 'https://github.com/hybridyn/fpulse/issues/new?template=connector-request.md', variant: 'ghost' },
              ] : undefined}
              hint={(
                <>
                  Postgres · MySQL · S3 · HTTP API · CSV / Parquet on disk · and 30+ more.{' '}
                  Don't see yours? <a
                    href="https://github.com/hybridyn/fpulse/blob/main/docs/extend/build-a-connector.md"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-indigo-600 hover:underline"
                  >Build it in 30 min</a>.
                </>
              )}
            />
          ) : searchFiltered.length === 0 ? (
            // 2026-05-22: surface WHY the table is empty when raw connections
            // exist but every row was filtered out. The earlier behaviour
            // dropped straight to an empty table while the KPI counters
            // (now also filter-aware) showed 0 / 0 / 0 — leaving the user
            // wondering whether the data was hidden, broken, or unauthorized.
            <EmptyState
              icon={
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
                </svg>
              }
              title={searchQuery.trim() ? 'No matches for your search' : `No connections visible in ${environment.toUpperCase()}`}
              body={
                searchQuery.trim()
                  ? `Your workspace has ${connections.length} connection(s) total, but none match "${searchQuery}". Clear the search to see them.`
                  : `Your workspace has ${connections.length} connection(s) total, but none are tagged for the ${environment.toUpperCase()} environment. Either edit the existing rows to pick dev/prod/all, or create a connection scoped to this environment.`
              }
              primaryCta={
                searchQuery.trim()
                  ? { label: 'Clear search', onClick: () => setSearchQuery('') }
                  : canCreate
                  ? { label: `+ New ${environment.toUpperCase()} connection`, onClick: () => setView('create') }
                  : undefined
              }
            />
          ) : (
            <div className={`rounded-lg border overflow-x-auto shadow-sm ${dark ? 'bg-[#111827] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
              <TableToolbar
                data={searchFiltered}
                columns={CONN_COLUMNS}
                columnGroups={CONN_GROUPS}
                visibleColumns={colState.visibleColumns}
                activeColumnCount={colState.activeColumns.length}
                onToggleColumn={colState.toggleColumn}
                onResetDefaults={colState.resetToDefaults}
                onSelectAll={colState.selectAll}
                searchValue={searchQuery}
                onSearchChange={setSearchQuery}
                searchPlaceholder="Search connections..."
                exportRowBuilder={(c: Connection) => ({
                  id: c.id,
                  name: c.name,
                  type: c.type,
                  description: c.description,
                  project_id: c.project_id || 'global',
                  tags: (c.tags || []).join('; '),
                  report_count: c.report_count ?? 0,
                  created_at: c.created_at,
                  environment: c.environment || '',
                  credential_id: c.credential_id || '',
                })}
                exportFilename="connections"
                recordLabel="connection"
                projectGrouper={(c: Connection) => c.project_id || 'global'}
              />
              <table className={`w-full border-collapse ${
                density === 'compact'
                  ? '[&_tbody_td]:!py-1.5'
                  : density === 'spacious'
                  ? '[&_tbody_td]:!py-5'
                  : ''
              }`}>
                <thead>
                  {/* Canonical navy-blue/amber header */}
                  <tr className="border-b-2 border-amber-400/40 bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900">
                    {colState.isVisible('name') && <th className="text-left text-xs font-bold text-amber-300 uppercase tracking-wider px-5 py-3">Connection</th>}
                    {colState.isVisible('type') && <th className="text-left text-xs font-bold text-amber-300 uppercase tracking-wider px-4 py-3">Type</th>}
                    {colState.isVisible('scope') && <th className="text-left text-xs font-bold text-amber-300 uppercase tracking-wider px-4 py-3">Scope</th>}
                    {colState.isVisible('used_by') && <th className="text-left text-xs font-bold text-amber-300 uppercase tracking-wider px-4 py-3">Used by</th>}
                    {colState.isVisible('description') && <th className="text-left text-xs font-bold text-amber-300 uppercase tracking-wider px-4 py-3">Description</th>}
                    {colState.isVisible('tags') && <th className="text-left text-xs font-bold text-amber-300 uppercase tracking-wider px-4 py-3">Tags</th>}
                    {colState.isVisible('reports') && <th className="text-center text-xs font-bold text-amber-300 uppercase tracking-wider px-4 py-3">Reports</th>}
                    {colState.isVisible('last_test') && <th className="text-left text-xs font-bold text-amber-300 uppercase tracking-wider px-4 py-3">Last Test</th>}
                    {colState.isVisible('created') && <th className="text-left text-xs font-bold text-amber-300 uppercase tracking-wider px-4 py-3">Created</th>}
                    {colState.isVisible('actions') && <th className="text-right text-xs font-bold text-amber-300 uppercase tracking-wider px-5 py-3">Actions</th>}
                  </tr>
                </thead>
                <tbody className={`divide-y ${dark ? 'divide-white/[0.04]' : 'divide-slate-100'}`}>
                  {searchFiltered.map((conn) => {
                    const meta = typeMeta(conn.type);
                    return (
                      <tr
                        key={conn.id}
                        onClick={() => setDrawerConn(conn)}
                        className={`transition-colors cursor-pointer group ${dark ? 'hover:bg-white/[0.03]' : 'hover:bg-slate-50/60'}`}
                      >
                        {/* Name + Icon */}
                        {colState.isVisible('name') && (
                        <td className="px-5 py-3 max-w-[300px]">
                          <div className="flex items-center gap-3">
                            <div
                              className="w-8 h-8 rounded-lg flex items-center justify-center shrink-0"
                              style={{ background: meta.color + '15', border: `1px solid ${meta.color}25` }}
                            >
                              <ConnectorIcon type={conn.type} size={20} />
                            </div>
                            <div className="min-w-0 flex flex-col gap-0.5">
                              <span
                                className={`text-sm font-semibold transition-colors truncate ${dark ? 'text-slate-200 group-hover:text-blue-400' : 'text-slate-700 group-hover:text-blue-600'}`}
                                title={conn.name}
                              >
                                {conn.name}
                              </span>
                              {/* Direction chips — Apr 22 2026. Empty / missing
                                  capabilities array means legacy row, treated
                                  as both. Tooltip explains the picker filter. */}
                              <div className="flex gap-1">
                                {(!conn.capabilities || conn.capabilities.includes('read')) && (
                                  <span
                                    className="text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded bg-emerald-50 text-emerald-700 border border-emerald-200"
                                    title="Available to source nodes (read)"
                                  >R</span>
                                )}
                                {(!conn.capabilities || conn.capabilities.includes('write')) && (
                                  <span
                                    className="text-[9px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200"
                                    title="Available to sink nodes (write)"
                                  >W</span>
                                )}
                              </div>
                            </div>
                          </div>
                        </td>
                        )}
                        {/* Type */}
                        {colState.isVisible('type') && (
                        <td className="px-4 py-3">
                          <span className="text-xs text-slate-500 flex items-center gap-1.5">
                            <span className="w-2 h-2 rounded-full shrink-0" style={{ background: meta.color }} />
                            {meta.label}
                          </span>
                        </td>
                        )}
                        {/* Scope */}
                        {colState.isVisible('scope') && (
                        <td className="px-4 py-3">
                          <ScopeBadge projectId={conn.project_id} />
                        </td>
                        )}
                        {/* Used by — Z25 (2026-05-23). Pipeline lineage:
                            click the pill to see which pipelines reference
                            this connection (as source/sink/activity). */}
                        {colState.isVisible('used_by') && (
                        <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                          <ConnUsedByPill
                            pipelines={usageMap[conn.id] || []}
                            connName={conn.name}
                            onOpenPipeline={(wid) => { window.location.hash = `editor/${wid}`; }}
                          />
                        </td>
                        )}
                        {/* Description */}
                        {colState.isVisible('description') && (
                        <td className="px-4 py-3">
                          <span className="text-xs text-slate-400 line-clamp-1 max-w-[180px] block">
                            {conn.description || <span className="italic text-slate-300">—</span>}
                          </span>
                        </td>
                        )}
                        {/* Tags */}
                        {colState.isVisible('tags') && (
                        <td className="px-4 py-3">
                          <div className="flex gap-1 flex-wrap">
                            {conn.tags.slice(0, 3).map(tag => (
                              <span key={tag} className="text-[9px] px-1.5 py-0.5 bg-slate-100 text-slate-500 rounded-full">{tag}</span>
                            ))}
                            {conn.tags.length > 3 && (
                              <span className="text-[9px] text-slate-400">+{conn.tags.length - 3}</span>
                            )}
                          </div>
                        </td>
                        )}
                        {/* Reports */}
                        {colState.isVisible('reports') && (
                        <td className="px-4 py-3 text-center">
                          <span className="text-xs text-slate-500 font-medium">{conn.report_count ?? 0}</span>
                        </td>
                        )}
                        {/* Last Test */}
                        {colState.isVisible('last_test') && (
                        <td className="px-4 py-3">
                          {conn.last_test_at ? (
                            <span className="flex items-center gap-1.5" title={conn.last_test_ok === false ? (conn.last_test_error || 'Test failed') : 'Test passed'}>
                              <span className={`w-2 h-2 rounded-full shrink-0 ${conn.last_test_ok === false ? 'bg-red-500' : 'bg-emerald-500'}`} />
                              <TimeAgo value={conn.last_test_at} className={`text-xs ${conn.last_test_ok === false ? '!text-red-500' : '!text-emerald-600'}`} />
                            </span>
                          ) : (
                            <span className="text-xs text-slate-300 italic">never</span>
                          )}
                        </td>
                        )}
                        {/* Created */}
                        {colState.isVisible('created') && (
                        <td className="px-4 py-3">
                          <TimeAgo value={conn.created_at} className="text-xs !text-slate-400" />
                        </td>
                        )}
                        {/* Actions */}
                        {colState.isVisible('actions') && (
                        <td className="px-5 py-3">
                          <div className="flex gap-1 justify-end" onClick={e => e.stopPropagation()}>
                            <RowActionButton
                              onClick={() => openDetail(conn)}
                              title="Edit Connection"
                              tone="blue"
                            >
                              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
                              </svg>
                            </RowActionButton>
                            <RowActionButton
                              onClick={() => handleTestConnection(conn.id)}
                              title="Test Connection"
                              tone="green"
                            >
                              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" />
                              </svg>
                            </RowActionButton>
                            <RowActionButton
                              onClick={() => openDetail(conn)}
                              title="View Details"
                              tone="blue"
                            >
                              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <polyline points="9 18 15 12 9 6" />
                              </svg>
                            </RowActionButton>
                          </div>
                        </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
        </div>
      </div>

      {/* Quick-detail drawer — opens on row click. Master-detail pattern.
          The full edit/test/reports page is still reachable via the
          "Open full details" button below. */}
      {drawerConn && (() => {
        const meta = typeMeta(drawerConn.type);
        const lastTest = (drawerConn as any).last_test_at || (drawerConn as any).last_tested_at;
        const lastTestOk = (drawerConn as any).last_test_ok;
        return (
          <DetailDrawer
            open={!!drawerConn}
            onClose={() => setDrawerConn(null)}
            widthPx={520}
            pushContent
            ariaLabel="Connection details"
            title={
              <div className="flex items-center gap-2.5">
                <div
                  className="w-8 h-8 rounded-md flex items-center justify-center shrink-0"
                  style={{ background: `${meta.color}18`, border: `1px solid ${meta.color}30` }}
                >
                  <ConnectorIcon type={drawerConn.type} size={20} />
                </div>
                <span className="truncate">{drawerConn.name}</span>
              </div>
            }
            subtitle={
              <span className="flex items-center gap-2 flex-wrap">
                <span>{meta.label}</span>
                <span>·</span>
                <ScopeBadge projectId={drawerConn.project_id} />
                {drawerConn.environment && (
                  <>
                    <span>·</span>
                    <span className="uppercase font-semibold">{drawerConn.environment}</span>
                  </>
                )}
              </span>
            }
            footer={
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <button
                  onClick={() => setDrawerConn(null)}
                  className="px-3 py-1.5 text-[12px] font-semibold rounded-lg border border-slate-200 bg-white text-slate-700 hover:bg-slate-50 transition-colors"
                >
                  Close
                </button>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => { handleTestConnection(drawerConn.id); }}
                    className="px-3 py-1.5 text-[12px] font-semibold text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg hover:bg-emerald-100 transition-colors"
                  >
                    Test
                  </button>
                  {canCreate && (
                    <button
                      onClick={() => { const c = drawerConn; setDrawerConn(null); openDetail(c); }}
                      className="px-3 py-1.5 text-[12px] font-semibold text-blue-700 bg-blue-50 border border-blue-200 rounded-lg hover:bg-blue-100 transition-colors"
                    >
                      Open full details
                    </button>
                  )}
                  {canCreate && (
                    <MoveToProjectButton
                      currentProjectId={(drawerConn as any).project_id || ''}
                      allowGlobal
                      onMove={async (target) => {
                        try {
                          await api.moveConnection(drawerConn.id, target);
                          toast.success('Connection moved');
                          loadConnections();
                          setDrawerConn(null);
                        } catch (err: any) {
                          toast.error('Move failed', err?.message || 'Could not move connection');
                        }
                      }}
                    />
                  )}
                  {canDelete && (
                    <button
                      onClick={() => { handleDelete(drawerConn.id); setDrawerConn(null); }}
                      className="px-3 py-1.5 text-[12px] font-semibold text-red-700 bg-red-50 border border-red-200 rounded-lg hover:bg-red-100 transition-colors"
                    >
                      Delete
                    </button>
                  )}
                </div>
              </div>
            }
          >
            {/* Status banner — most visible signal first. */}
            {lastTestOk === true && (
              <div className="mb-4 px-3 py-2 rounded-lg bg-emerald-50 border border-emerald-200 text-[12px] text-emerald-800 font-semibold flex items-center gap-2">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><polyline points="20 6 9 17 4 12" /></svg>
                Last test passed{lastTest && <> — <TimeAgo value={lastTest} className="!text-emerald-700" /></>}
              </div>
            )}
            {lastTestOk === false && (
              <div className="mb-4 px-3 py-2 rounded-lg bg-red-50 border border-red-200 text-[12px] text-red-700 font-semibold flex items-center gap-2">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><circle cx="12" cy="12" r="10" /><line x1="15" y1="9" x2="9" y2="15" /><line x1="9" y1="9" x2="15" y2="15" /></svg>
                Last test failed{lastTest && <> — <TimeAgo value={lastTest} className="!text-red-700" /></>}
              </div>
            )}
            {!lastTest && (
              <div className="mb-4 px-3 py-2 rounded-lg bg-amber-50 border border-amber-200 text-[12px] text-amber-800 font-semibold flex items-center gap-2">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></svg>
                Not tested yet — click Test to verify.
              </div>
            )}

            {/* Description */}
            {drawerConn.description && (
              <p className="text-sm text-slate-700 leading-relaxed mb-4">
                {drawerConn.description}
              </p>
            )}

            {/* Metadata grid */}
            <dl className="grid grid-cols-1 gap-2.5 text-[12px]">
              <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                <dt className="text-slate-500 font-medium">Type</dt>
                <dd className="text-slate-800 font-semibold">{meta.label}</dd>
              </div>
              <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                <dt className="text-slate-500 font-medium">Category</dt>
                <dd className="text-slate-800">{meta.category}</dd>
              </div>
              <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                <dt className="text-slate-500 font-medium">Scope</dt>
                <dd className="text-slate-800">{drawerConn.project_id ? projectName(drawerConn.project_id) : 'Global'}</dd>
              </div>
              {drawerConn.environment && (
                <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                  <dt className="text-slate-500 font-medium">Environment</dt>
                  <dd className="text-slate-800 uppercase font-semibold">{drawerConn.environment}</dd>
                </div>
              )}
              <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                <dt className="text-slate-500 font-medium">Created</dt>
                <dd className="text-slate-800"><TimeAgo value={drawerConn.created_at} /></dd>
              </div>
              {(drawerConn as any).updated_at && (
                <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                  <dt className="text-slate-500 font-medium">Last modified</dt>
                  <dd className="text-slate-800"><TimeAgo value={(drawerConn as any).updated_at} /></dd>
                </div>
              )}
              {lastTest && (
                <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                  <dt className="text-slate-500 font-medium">Last tested</dt>
                  <dd className="text-slate-800"><TimeAgo value={lastTest} /></dd>
                </div>
              )}
              <div className="flex justify-between gap-3 py-2 border-b border-slate-100">
                <dt className="text-slate-500 font-medium">Connection ID</dt>
                <dd className="text-slate-700 font-mono text-xs">{drawerConn.id.slice(0, 16)}</dd>
              </div>
            </dl>

            {/* Tags */}
            {drawerConn.tags && drawerConn.tags.length > 0 && (
              <div className="mt-4">
                <div className="text-xs font-semibold text-slate-500 mb-1.5">Tags</div>
                <div className="flex flex-wrap gap-1.5">
                  {drawerConn.tags.map((t: string) => (
                    <span key={t} className="text-xs px-2 py-0.5 rounded-full bg-slate-100 text-slate-700 border border-slate-200">
                      {t}
                    </span>
                  ))}
                </div>
              </div>
            )}

          </DetailDrawer>
        );
      })()}
      </>
    );
  }

  // ── CREATE VIEW ──
  if (view === 'create') {
    const fields = formType ? (CONNECTION_FIELDS[formType] || CONNECTION_FIELDS['rest_api'] || []) : [];
    const categories = [...new Set(CONNECTION_TYPES.map(t => t.category))];
    const missingRequired = fields.filter(f => f.required && !formConfig[f.key]);
    // Z30 (2026-05-23) — `basicsReady` must include `formName`. Step 0
    // ("Basics — Name, scope, environment") renders the Connection Name
    // input alongside scope + role pickers; previously the readiness flag
    // only checked scope + capabilities, so the step indicator showed
    // ✓ Complete with an empty name. The misleading green tick survived
    // until the user reached step 2 and saw "Connection name is required
    // after test". Now the name is part of basicsReady; the step turns
    // green only when name + scope + role are all set.
    const nameReady = formName.trim().length > 0;
    const scopeRoleReady = (formScope !== 'project' || !!formProjectId) && (formCanRead || formCanWrite);
    const basicsReady = nameReady && scopeRoleReady;
    const detailsReady = missingRequired.length === 0;
    const testedReady = inlineTestResult?.ok === true;
    // `saveReady` reuses nameReady so the late "Identity & Tags" panel
    // on step 2 (which re-renders the same Connection Name input bound
    // to the same `formName` state) doesn't disagree with the step-0 chip.
    const saveReady = nameReady && testedReady;
    const canCreateConnection = basicsReady && detailsReady && saveReady;
    const createSteps: Array<{ key: 0 | 1 | 2; label: string; detail: string }> = [
      { key: 0, label: 'Basics', detail: 'Name, scope, environment' },
      { key: 1, label: 'Connection', detail: 'Endpoint and credentials' },
      { key: 2, label: 'Test & Save', detail: 'Validate and create' },
    ];

    // Apply search filter — matches label, type, or category (case-insensitive).
    // Hide 'roadmap' connectors (UI-only stubs) until their backend ships.
    // All other connectors are open — no Plus-only gating on the library.
    const q = connectorSearch.trim().toLowerCase();
    const filteredConnectors = (
      !q
        ? CONNECTION_TYPES
        : CONNECTION_TYPES.filter(t =>
            t.label.toLowerCase().includes(q) ||
            t.type.toLowerCase().includes(q) ||
            t.category.toLowerCase().includes(q)
          )
    ).filter(t => connectorStatus(t.type) !== 'roadmap');
    const visibleGroups = CONNECTOR_MENU_GROUPS
      .map(group => ({
        ...group,
        count: group.id === 'All'
          ? filteredConnectors.length
          : filteredConnectors.filter(t => group.categories.includes(t.category)).length,
      }))
      .filter(group => group.count > 0);
    const selectedConnectorGroup = visibleGroups.some(group => group.id === activeConnectorCategory)
      ? activeConnectorCategory
      : visibleGroups[0]?.id || 'All';
    const selectedConnectorGroupMeta = CONNECTOR_MENU_GROUPS.find(group => group.id === selectedConnectorGroup) || CONNECTOR_MENU_GROUPS[0];
    const categoryConnectors = selectedConnectorGroup === 'All'
      ? filteredConnectors
      : filteredConnectors.filter(t => selectedConnectorGroupMeta.categories.includes(t.category));
    const selectConnector = (type: string) => {
      setFormType(type);
      setCreateStep(0);
      if (WRITE_ONLY_TYPES.has(type)) {
        setFormCanRead(false);
        setFormCanWrite(true);
      } else {
        setFormCanRead(true);
        setFormCanWrite(true);
      }
    };

    return (
      <div className="flex-1 flex flex-col overflow-hidden">
        <ReadOnlyBanner environment={environment} />
        <div className="flex-1 overflow-auto">
        {/* Canonical page header — matches the LIST view header so the
            New Connection screen feels like part of Connections, not a
            standalone page. Same env-aware chrome (slate-900 PROD, light
            DEV) and the same TierChip placement. */}
        <div className={`sticky top-0 z-30 border-b ${
          environment === 'prod'
            ? 'bg-slate-900 border-slate-700'
            : 'bg-gradient-to-b from-slate-200 to-slate-300 border-slate-400/70'
        }`}>
          <div className="px-8 h-[78px] flex items-center justify-between gap-4">
            {/* LEFT — title only; "Back to Connections" lives in the
                canvas body below the header (Siva's call: nav links go in
                the content area, the header stays clean). */}
            <div className="min-w-0">
              <h1 className={`text-xl font-bold flex items-center gap-2 ${
                environment === 'prod' ? 'text-white' : 'text-slate-800'
              }`}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={environment === 'prod' ? 'text-red-400' : 'text-blue-500'}>
                  <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
                </svg>
                New Connection
                {environment === 'prod' && (
                  <span className="text-xs font-bold px-2.5 py-0.5 rounded-full bg-red-500/20 text-red-300 border border-red-500/30 uppercase tracking-wider">
                    PROD
                  </span>
                )}
                <TierChip tier={tier} environment={environment} />
              </h1>
              <p className={`text-xs mt-0.5 ${
                environment === 'prod' ? 'text-slate-400' : 'text-slate-500'
              }`}>
                {formType ? `Configure your ${typeMeta(formType).label} connection` : 'Pick a connector type to get started'}
              </p>
            </div>

            {/* RIGHT — Search lives in the header so the connector grid
                below stays uncluttered. Hidden once a type is picked. */}
            {!formType && (
              <div className="relative w-72 shrink-0">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="absolute left-3 top-1/2 -translate-y-1/2">
                  <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
                </svg>
                <input
                  type="text"
                  value={connectorSearch}
                  onChange={(e) => setConnectorSearch(e.target.value)}
                  placeholder="Search connectors…"
                  className={`w-full pl-9 pr-9 py-2 text-sm rounded-lg outline-none border transition-colors ${
                    environment === 'prod'
                      ? 'bg-slate-800 border-slate-700 text-slate-200 placeholder:text-slate-500 focus:border-slate-600'
                      : 'bg-white border-slate-300 text-slate-700 focus:ring-2 focus:ring-blue-200 focus:border-blue-400'
                  }`}
                />
                {connectorSearch && (
                  <button
                    onClick={() => setConnectorSearch('')}
                    className={`absolute right-2 top-1/2 -translate-y-1/2 w-6 h-6 flex items-center justify-center ${
                      environment === 'prod' ? 'text-slate-500 hover:text-slate-300' : 'text-slate-400 hover:text-slate-600'
                    }`}
                    title="Clear search"
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                  </button>
                )}
              </div>
            )}
          </div>
        </div>

        <div className="w-full max-w-[1500px] mx-auto px-8 py-6">
          {/* Back link — sits above the connector grid so navigation stays
              inside the content area, not in the header. */}
          <button
            onClick={() => {
              if (formType) {
                resetCreateForm();
              } else {
                setView('list');
                resetCreateForm();
                setConnectorSearch('');
              }
            }}
            className="text-sm font-semibold flex items-center gap-1.5 mb-4 text-slate-600 hover:text-blue-600 transition-colors"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="15 18 9 12 15 6" /></svg>
            {formType ? 'Back to connector list' : 'Back to Connections'}
          </button>

          {/* Type selector */}
          {!formType ? (
            <div className="space-y-5">
              {/* Centered legend — white card with subtle border so the
                  badge meanings are clearly visible above the grid.
                  2026-05-29: extended to include provenance tiers so
                  evaluators see the catalog is a 3-tier framework
                  (F-Pulse / community / user-authored), not a closed
                  shipped-only set. Provenance pills only appear on
                  cards that are NOT first-party — first-party = no
                  badge (otherwise every card is cluttered with the
                  same word). */}
              <div className="flex justify-center">
                <div className="inline-flex flex-wrap items-center gap-x-5 gap-y-1 px-5 py-2.5 rounded-full bg-white border border-slate-200 shadow-sm text-xs text-slate-600">
                  <span className="inline-flex items-center gap-1.5">
                    <span className="w-2.5 h-2.5 rounded-full bg-emerald-500" /> <span className="font-semibold text-slate-700">Certified</span> — production-grade
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <span className="w-2.5 h-2.5 rounded-full bg-amber-500" /> <span className="font-semibold text-slate-700">Beta</span> — usable with gaps
                  </span>
                  <span className="hidden lg:inline-block w-px h-3.5 bg-slate-200" aria-hidden />
                  <span className="inline-flex items-center gap-1.5" title="All shown are first-party (shipped in the catalog). Community contributions and your own connectors show their provenance pill on the card.">
                    <span className="font-semibold text-slate-700">Provenance:</span>
                    <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-slate-100 text-slate-700">F-Pulse</span>
                    <span className="text-slate-400">·</span>
                    <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700">Community</span>
                    <span className="text-slate-400">·</span>
                    <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-purple-100 text-purple-700">Yours</span>
                  </span>
                </div>
              </div>

              {filteredConnectors.length === 0 && (
                <div className="text-center py-12 text-sm text-slate-500">
                  No connectors match "<span className="font-semibold text-slate-700">{connectorSearch}</span>".
                  <button onClick={() => setConnectorSearch('')} className="ml-2 text-blue-600 hover:text-blue-700 font-medium">Clear</button>
                </div>
              )}

              {filteredConnectors.length > 0 && (
                <div className="grid grid-cols-1 lg:grid-cols-[260px_minmax(0,1fr)] gap-4 items-start">
                  <aside className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
                    <div className="px-4 py-3 border-b border-slate-100">
                      <div className="text-xs font-bold uppercase tracking-wider text-slate-500">Source groups</div>
                      <div className="text-xs text-slate-400 mt-0.5">{filteredConnectors.length} connector{filteredConnectors.length === 1 ? '' : 's'} visible</div>
                    </div>
                    <div className="p-2 space-y-1">
                      {visibleGroups.map(group => {
                        const active = selectedConnectorGroup === group.id;
                        return (
                          <button
                            key={group.id}
                            type="button"
                            onClick={() => setActiveConnectorCategory(group.id)}
                            className={`w-full flex items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-left transition-colors ${
                              active
                                ? 'bg-blue-50 text-blue-700 border border-blue-200'
                                : 'text-slate-600 hover:bg-slate-50 border border-transparent'
                            }`}
                          >
                            <span className="text-sm font-semibold truncate">{group.label}</span>
                            <span className={`text-[11px] font-bold rounded-full px-2 py-0.5 ${
                              active ? 'bg-blue-100 text-blue-700' : 'bg-slate-100 text-slate-500'
                            }`}>{group.count}</span>
                          </button>
                        );
                      })}
                    </div>
                  </aside>

                  <section className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
                    <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between gap-3">
                      <div>
                        <div className="text-lg font-bold text-slate-800">{selectedConnectorGroupMeta.label}</div>
                        <div className="text-sm text-slate-500 mt-0.5">
                          Select a connector type to configure source/sink access.
                        </div>
                      </div>
                      <span className="text-xs font-bold text-slate-500 bg-slate-100 rounded-full px-2.5 py-1">
                        {categoryConnectors.length} item{categoryConnectors.length === 1 ? '' : 's'}
                      </span>
                    </div>
                    <div className="p-4 grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-3">
                      {categoryConnectors.map(ct => {
                        const cs = connectorStatus(ct.type);
                        const badge = STATUS_BADGE_STYLE[cs];
                        // 2026-05-29: provenance hook. Returns null for
                        // first-party (suppress noise). Activates the
                        // Community / Yours pill when manifest discovery
                        // tags the connector accordingly.
                        const prov = connectorProvenance(ct.type);
                        const provBadge = prov ? PROVENANCE_BADGE_STYLE[prov] : null;
                        return (
                          <button
                            key={ct.type}
                            onClick={() => {
                              // Beta connectors: show one-time acknowledgment
                              // modal so users know what to expect before they
                              // wire up a pipeline against an unstable backend.
                              if (cs === 'beta' && !getBetaAcks().has(ct.type)) {
                                setPendingBetaType(ct.type);
                                return;
                              }
                              selectConnector(ct.type);
                            }}
                            className="flex items-center gap-3 px-4 py-3.5 rounded-lg border border-slate-200 hover:border-blue-300 hover:bg-blue-50/50 transition-all text-left relative min-h-[72px]"
                          >
                            <ConnectorIcon type={ct.type} size={42} />
                            <span className="min-w-0 flex-1">
                              <span className="block text-sm font-semibold text-slate-700 truncate">{ct.label}</span>
                              <span className="block text-xs text-slate-400 mt-0.5 truncate">{ct.type}</span>
                            </span>
                            <span className="flex flex-col items-end gap-0.5 shrink-0">
                              {cs !== 'certified' && (
                                <span className={`text-[8px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${badge.bg} ${badge.text}`}>
                                  {badge.label}
                                </span>
                              )}
                              {provBadge && (
                                <span
                                  className={`text-[8px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded ${provBadge.bg} ${provBadge.text}`}
                                  title={provBadge.title}
                                >
                                  {provBadge.label}
                                </span>
                              )}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                    {/* 2026-05-29: "Don't see your tool?" footer banner.
                        Sits below the connector grid in EVERY category, not
                        just empty/no-match states — the natural disappointment
                        point is after a user scrolls all 30-odd cards looking
                        for their system and doesn't find it. Three first-class
                        paths surfaced inline so the response isn't "file a
                        ticket and wait" but "build it yourself in 90 seconds
                        OR request first-party, your choice." Reuses the same
                        URLs the empty-state on the list view uses. */}
                    <div className="border-t border-slate-100 px-5 py-4 bg-slate-50/60">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div className="text-sm text-slate-600">
                          <span className="font-semibold text-slate-800">Don't see your tool?</span>{' '}
                          Build a connector from any OpenAPI spec or sample API responses — no compile step.
                        </div>
                        <div className="flex items-center gap-2 shrink-0">
                          <a
                            href="#author"
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-gradient-to-r from-indigo-500 to-purple-500 text-white shadow-sm hover:shadow-md hover:from-indigo-600 hover:to-purple-600 transition-all"
                          >
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                              <polyline points="13 2 13 9 20 9" /><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                            </svg>
                            Build your own (90s)
                          </a>
                          <a
                            href="https://github.com/hybridyn/fpulse/issues/new?template=connector-request.md"
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg border border-slate-200 bg-white text-slate-700 hover:border-indigo-300 hover:text-indigo-700 hover:shadow-sm transition-all"
                          >
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                              <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
                            </svg>
                            Suggest a connector
                          </a>
                        </div>
                      </div>
                    </div>
                  </section>
                </div>
              )}
            </div>
          ) : (
            // Z28 (2026-05-23) — two-column layout matches the
            // Author Connector page. Form lives on the LEFT (2fr);
            // a sticky "Test connection" panel lives on the RIGHT
            // (1fr) so the user can validate credentials before
            // saving without scrolling away from the inputs.
            <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1.75fr)_minmax(380px,0.9fr)] gap-6 items-start">
            <div className="min-w-0 space-y-4">
              <div className="flex items-center gap-2 mb-1 px-1">
                <ConnectorIcon type={formType} size={22} />
                <span className="text-sm font-semibold text-slate-700">{typeMeta(formType).label}</span>
                <button onClick={() => setFormType('')} className="text-xs text-blue-500 ml-2 hover:text-blue-700">Change type</button>
              </div>

              <div className="rounded-xl border border-slate-200 bg-white shadow-sm px-3 py-3">
                <div className="grid grid-cols-3 gap-2">
                  {createSteps.map((step, idx) => {
                    const active = createStep === step.key;
                    const complete = step.key === 0 ? basicsReady : step.key === 1 ? detailsReady : canCreateConnection;
                    return (
                      <button
                        key={step.key}
                        type="button"
                        onClick={() => setCreateStep(step.key)}
                        className={`text-left rounded-lg border px-3 py-2 transition-all ${
                          active
                            ? 'border-blue-400 bg-blue-50 shadow-sm'
                            : complete
                              ? 'border-emerald-200 bg-emerald-50/60 hover:border-emerald-300'
                              : 'border-slate-200 bg-slate-50 hover:border-slate-300'
                        }`}
                      >
                        <div className="flex items-center gap-3">
                          <span className={`w-5 h-5 rounded-full text-[10px] font-bold flex items-center justify-center ${
                            active
                              ? 'bg-blue-600 text-white'
                              : complete
                                ? 'bg-emerald-500 text-white'
                                : 'bg-white text-slate-500 border border-slate-200'
                          }`}>
                            {complete && !active ? '✓' : idx + 1}
                          </span>
                          <span className="text-xs font-bold text-slate-800">{step.label}</span>
                        </div>
                        <div className="mt-1 text-[11px] text-slate-500 truncate">{step.detail}</div>
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* ── Card: Visibility & Access ── */}
              <div className="hidden">
                <div className="flex items-center gap-2 pb-2 border-b border-slate-100">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
                  </svg>
                  <span className="text-sm font-bold uppercase tracking-wider text-slate-800">Visibility & Access</span>
                </div>

                <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)] gap-5 items-start">
                  <div className="space-y-4">

              {/* Scope selector */}
              <div>
                <label className="text-xs font-semibold text-slate-500 uppercase block mb-2">Scope *</label>
                <div className="flex gap-3">
                  <button
                    onClick={() => setFormScope('global')}
                    className={`flex-1 px-4 py-3 rounded-lg border-2 transition-all text-left ${
                      formScope === 'global'
                        ? 'border-emerald-400 bg-emerald-50'
                        : 'border-slate-200 hover:border-slate-300'
                    }`}
                  >
                    <div className="text-sm font-semibold text-slate-800">Global</div>
                    <div className="text-xs text-slate-500 mt-0.5">Available to all projects</div>
                  </button>
                  <button
                    onClick={() => setFormScope('project')}
                    className={`flex-1 px-4 py-3 rounded-lg border-2 transition-all text-left ${
                      formScope === 'project'
                        ? 'border-blue-400 bg-blue-50'
                        : 'border-slate-200 hover:border-slate-300'
                    }`}
                  >
                    <div className="text-sm font-semibold text-slate-800">Project</div>
                    <div className="text-xs text-slate-500 mt-0.5">Scoped to a specific project</div>
                  </button>
                </div>
              </div>

              {formScope === 'project' && (
                <div>
                  <label className="text-sm font-semibold text-slate-700 block mb-1.5">Project *</label>
                  <select
                    value={formProjectId}
                    onChange={e => setFormProjectId(e.target.value)}
                    className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300 bg-white"
                  >
                    <option value="">Select a project...</option>
                    {projects.map(p => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                </div>
              )}

              {/* Environment visibility — enforces no-leak between DEV/PROD */}
              <div>
                <label className="text-sm font-semibold text-slate-700 block mb-1.5">Environment</label>
                <div className="flex gap-1.5">
                  {([
                    { value: 'dev',  label: 'DEV only',  color: '#10b981' },
                    { value: 'prod', label: 'PROD only', color: '#ef4444' },
                    { value: 'all',  label: 'Both',      color: '#64748b' },
                  ] as const).map(opt => (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => setFormEnvironment(opt.value)}
                      className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-all border ${
                        formEnvironment === opt.value
                          ? 'text-white shadow-sm border-transparent'
                          : 'bg-slate-50 text-slate-600 hover:bg-slate-100 border-slate-200'
                      }`}
                      style={formEnvironment === opt.value ? { background: opt.color } : undefined}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-slate-400 mt-1">
                  PROD connections never appear in DEV and vice versa. Pick "Both" only for connections that are genuinely shared.
                </p>
              </div>

              {/* Direction capabilities — Apr 22 2026. Source-node and
                  sink-node ConnectionPickers filter by these flags so a
                  user doesn't have to maintain two duplicate connections
                  for the same DSN. Smart default by type (write-only for
                  notifiers); user can flip either box. */}
              <div>
                <label className="text-sm font-semibold text-slate-700 block mb-1.5">Use as</label>
                <div className="flex gap-2">
                  <label className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-semibold border cursor-pointer transition-all ${
                    formCanRead ? 'bg-emerald-50 text-emerald-700 border-emerald-300' : 'bg-slate-50 text-slate-500 border-slate-200'
                  }`}>
                    <input type="checkbox" checked={formCanRead} onChange={e => setFormCanRead(e.target.checked)} className="w-3.5 h-3.5" />
                    Source (read)
                  </label>
                  <label className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-semibold border cursor-pointer transition-all ${
                    formCanWrite ? 'bg-amber-50 text-amber-700 border-amber-300' : 'bg-slate-50 text-slate-500 border-slate-200'
                  }`}>
                    <input type="checkbox" checked={formCanWrite} onChange={e => setFormCanWrite(e.target.checked)} className="w-3.5 h-3.5" />
                    Sink (write)
                  </label>
                </div>
                <p className="text-xs text-slate-400 mt-1">
                  One connection can serve both roles. Source-only and Sink-only nodes will only see connections with the matching capability.
                </p>
                {!formCanRead && !formCanWrite && (
                  <p className="text-xs text-red-500 mt-1 font-semibold">Pick at least one — a connection with neither role can't be used by any node.</p>
                )}
              </div>
                  </div>

                  <div className="rounded-lg border border-blue-100 bg-blue-50/60 p-4 text-sm text-blue-900">
                    <div className="font-bold">Name it after the test</div>
                    <p className="mt-1 text-xs leading-relaxed text-blue-700">
                      Configure scope and access first. Once the endpoint test succeeds,
                      add the connection name, description, and tags before saving.
                    </p>
                  </div>
                </div>
              </div>
              {/* ── End: Visibility & Access ── */}

              {/* ── Card: Identity ── */}
              <div className={`${createStep === 0 ? '' : 'hidden'} rounded-xl border border-slate-200 bg-white p-5 shadow-sm space-y-4`}>
                <div className="flex items-center gap-2 pb-2 border-b border-slate-100">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 12a5 5 0 1 0 0-10 5 5 0 0 0 0 10z" /><path d="M20 21a8 8 0 0 0-16 0" />
                  </svg>
                  <span className="text-sm font-bold uppercase tracking-wider text-slate-800">Identity</span>
                </div>

                <div>
                  <label className="text-sm font-semibold text-slate-700 block mb-1.5">Connection Name *</label>
                  <input value={formName} onChange={e => setFormName(e.target.value)} placeholder="e.g. Production Oracle ERP" className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300" />
                </div>

                <div>
                  <label className="text-sm font-semibold text-slate-700 block mb-1.5">Description</label>
                  <input value={formDesc} onChange={e => setFormDesc(e.target.value)} placeholder="What is this connection used for?" className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300" />
                </div>
              </div>
              {/* ── End: Identity ── */}

              {fields.length > 0 && createStep === 1 && (
                <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm space-y-3">
                  <div className="flex items-center justify-between pb-2 border-b border-slate-100">
                    <div className="flex items-center gap-2">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
                        <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
                      </svg>
                      <span className="text-sm font-bold uppercase tracking-wider text-slate-800">Connection Details</span>
                    </div>
                  </div>

                  {fields.map(f => (
                    <div key={f.key}>
                      <label className="text-sm font-semibold text-slate-700 block mb-1.5">
                        {f.label}{f.required && <span className="text-red-400 ml-0.5">*</span>}
                      </label>
                      {f.type === 'select' && f.options ? (
                        <select
                          value={formConfig[f.key] || f.defaultValue || ''}
                          onChange={e => setFormConfig({ ...formConfig, [f.key]: e.target.value })}
                          className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300 bg-white"
                        >
                          <option value="">— Select —</option>
                          {f.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                        </select>
                      ) : f.type === 'checkbox' ? (
                        <label className="flex items-center gap-2 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={formConfig[f.key] === 'true' || (formConfig[f.key] as unknown) === true || (formConfig[f.key] === undefined && f.defaultValue === 'true')}
                            onChange={e => setFormConfig({ ...formConfig, [f.key]: e.target.checked ? 'true' : 'false' })}
                            className="w-4 h-4 rounded border-slate-300 text-blue-600 focus:ring-blue-300"
                          />
                          <span className="text-xs text-slate-500">{f.hint || 'Enable'}</span>
                        </label>
                      ) : f.type === 'textarea' ? (
                        <textarea
                          value={formConfig[f.key] || ''}
                          onChange={e => setFormConfig({ ...formConfig, [f.key]: e.target.value })}
                          placeholder={f.placeholder}
                          rows={3}
                          className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300 font-mono text-xs"
                        />
                      ) : (
                        <input
                          type={f.type === 'number' ? 'text' : f.type}
                          inputMode={f.type === 'number' ? 'numeric' : undefined}
                          value={formConfig[f.key] ?? f.defaultValue ?? ''}
                          onChange={e => setFormConfig({ ...formConfig, [f.key]: e.target.value })}
                          placeholder={f.placeholder}
                          className={`w-full text-sm px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300 ${
                            f.required && !formConfig[f.key] ? 'border-slate-200' : 'border-slate-200'
                          }`}
                        />
                      )}
                      {f.hint && f.type !== 'checkbox' && (
                        <p className="text-xs text-slate-400 mt-0.5">{f.hint}</p>
                      )}
                    </div>
                  ))}

                </div>
              )}

              {/* ── Card: Organization ── */}
              <div className="hidden">
                <div className="flex items-center gap-2 pb-2 mb-3 border-b border-slate-100">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z" />
                    <line x1="7" y1="7" x2="7.01" y2="7" />
                  </svg>
                  <span className="text-sm font-bold uppercase tracking-wider text-slate-800">Organization</span>
                </div>
                <label className="text-sm font-semibold text-slate-700 block mb-1.5">Tags (comma-separated)</label>
                <input value={formTags} onChange={e => setFormTags(e.target.value)} placeholder="finance, production" className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300" />
              </div>

              {createStep === 2 && (
                <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm space-y-3">
                  <div className="flex items-center gap-2 pb-2 border-b border-slate-100">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" />
                    </svg>
                    <span className="text-sm font-bold uppercase tracking-wider text-slate-800">Ready to test and save</span>
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
                    <div className={`rounded-lg border px-3 py-2 ${basicsReady ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-amber-50 border-amber-200 text-amber-800'}`}>
                      <div className="font-bold">Basics</div>
                      <div>
                        {basicsReady
                          ? 'Complete'
                          : !nameReady
                            ? 'Connection name is required.'
                            : 'Project scope or source/sink role needs attention.'}
                      </div>
                    </div>
                    <div className={`rounded-lg border px-3 py-2 ${detailsReady ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-amber-50 border-amber-200 text-amber-800'}`}>
                      <div className="font-bold">Connection details</div>
                      <div>{detailsReady ? 'Required fields complete' : `${missingRequired.length} required field${missingRequired.length === 1 ? '' : 's'} missing.`}</div>
                    </div>
                    <div className={`rounded-lg border px-3 py-2 ${testedReady ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-amber-50 border-amber-200 text-amber-800'}`}>
                      <div className="font-bold">Connection test</div>
                      <div>{testedReady ? 'Succeeded' : 'Run test successfully before saving.'}</div>
                    </div>
                    <div className={`rounded-lg border px-3 py-2 ${formName ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-amber-50 border-amber-200 text-amber-800'}`}>
                      <div className="font-bold">Save details</div>
                      <div>{formName ? 'Name added' : 'Connection name is required after test.'}</div>
                    </div>
                  </div>

                  <div className={`rounded-lg border p-4 space-y-3 ${testedReady ? 'bg-white border-slate-200' : 'bg-slate-50 border-slate-200 opacity-70'}`}>
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <div className="text-sm font-bold text-slate-800">Identity & Tags</div>
                        <div className="text-xs text-slate-500">Fill this after a successful test, then save the connection.</div>
                      </div>
                      {!testedReady && <span className="text-[10px] font-bold uppercase tracking-wider text-amber-700 bg-amber-50 border border-amber-200 rounded-full px-2 py-1">Test first</span>}
                    </div>
                    <div>
                      <label className="text-sm font-semibold text-slate-700 block mb-1.5">Connection Name *</label>
                      <input disabled={!testedReady} value={formName} onChange={e => setFormName(e.target.value)} placeholder="e.g. Production Oracle ERP" className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300 disabled:bg-slate-100 disabled:text-slate-400" />
                    </div>
                    <div>
                      <label className="text-sm font-semibold text-slate-700 block mb-1.5">Description</label>
                      <input disabled={!testedReady} value={formDesc} onChange={e => setFormDesc(e.target.value)} placeholder="What is this connection used for?" className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300 disabled:bg-slate-100 disabled:text-slate-400" />
                    </div>
                    <div>
                      <label className="text-sm font-semibold text-slate-700 block mb-1.5">Tags (comma-separated)</label>
                      <input disabled={!testedReady} value={formTags} onChange={e => setFormTags(e.target.value)} placeholder="finance, production" className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300 disabled:bg-slate-100 disabled:text-slate-400" />
                    </div>
                  </div>
                </div>
              )}

              {/* Validation warning */}
              {createStep === 2 && (
                missingRequired.length > 0 ? (
                  <p className="text-xs text-amber-500 flex items-center gap-1">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M12 9v4m0 4h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>
                    {missingRequired.length} required field{missingRequired.length > 1 ? 's' : ''} missing: {missingRequired.map(f => f.label).join(', ')}
                  </p>
                ) : null
              )}

              <div className="flex gap-3 pt-2">
                {createStep > 0 && (
                  <button
                    type="button"
                    onClick={() => setCreateStep((createStep - 1) as 0 | 1 | 2)}
                    className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50"
                  >
                    Back
                  </button>
                )}
                {createStep < 2 ? (
                  <button
                    type="button"
                    onClick={() => setCreateStep((createStep + 1) as 0 | 1 | 2)}
                    className="px-5 py-2 text-white text-sm font-bold rounded-lg"
                    style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
                  >
                    Continue
                  </button>
                ) : (
                  <button
                    onClick={handleCreate}
                    disabled={!canCreateConnection}
                    className="px-5 py-2 text-white text-sm font-bold rounded-lg disabled:opacity-40"
                    style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
                  >
                    Create Connection
                  </button>
                )}
                <button onClick={() => { setView('list'); resetCreateForm(); }} className="px-4 py-2 text-sm text-slate-500 border border-slate-200 rounded-lg hover:bg-slate-50">Cancel</button>
              </div>
            </div>
            {/* ── RIGHT column: live summary + Test panel ─────────────── */}
            <aside className="min-w-0 lg:sticky lg:top-[94px] space-y-4">
              {/* Live summary */}
              <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
                <div className="px-5 py-4 border-b border-slate-100 bg-gradient-to-b from-slate-50 to-white">
                  <div className="text-xs font-bold uppercase tracking-wider text-slate-500">Live summary</div>
                  <div className="mt-2 flex items-center gap-2.5">
                    <ConnectorIcon type={formType} size={22} />
                    <span className="text-base font-semibold text-slate-800 truncate">{formName || <span className="italic text-slate-400">Unnamed connection</span>}</span>
                  </div>
                  <div className="mt-1 text-sm text-slate-500">{typeMeta(formType).label}</div>
                </div>
                <div className="px-5 py-4 space-y-2.5 text-sm">
                  {(() => {
                    // Pick whichever field looks like the primary endpoint
                    // (host / base_url / endpoint / file_path / account /
                    // bucket) and show it as the "target" line.
                    const target = formConfig['base_url'] || formConfig['host'] || formConfig['endpoint_url']
                      || formConfig['endpoint'] || formConfig['file_path'] || formConfig['bucket']
                      || formConfig['account_name'] || formConfig['account'] || '';
                    const auth = formConfig['auth_method'] || (formConfig['username'] ? 'Basic auth' : formConfig['token'] || formConfig['api_key'] || formConfig['access_token'] ? 'API key' : 'None');
                    const scopeLabel = formScope === 'global' ? 'Global' : (formProjectId ? `Project · ${(projects.find(p => p.id === formProjectId)?.name) || formProjectId}` : 'Project (not picked)');
                    return (
                      <>
                        <div className="flex items-center gap-3">
                          <span className="w-20 text-xs font-semibold uppercase tracking-wider text-slate-400 shrink-0">Target</span>
                          <code className="font-mono text-slate-700 break-all">{target || <span className="italic text-slate-300">—</span>}</code>
                        </div>
                        <div className="flex items-center gap-3">
                          <span className="w-20 text-xs font-semibold uppercase tracking-wider text-slate-400 shrink-0">Auth</span>
                          <span className="text-slate-700">{auth || 'None'}</span>
                        </div>
                        <div className="flex items-center gap-3">
                          <span className="w-20 text-xs font-semibold uppercase tracking-wider text-slate-400 shrink-0">Scope</span>
                          <span className="text-slate-700">{scopeLabel}</span>
                        </div>
                        <div className="flex items-center gap-3">
                          <span className="w-20 text-xs font-semibold uppercase tracking-wider text-slate-400 shrink-0">Env</span>
                          <span className="text-slate-700 uppercase">{formEnvironment === 'all' ? 'DEV + PROD' : formEnvironment}</span>
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="w-20 text-xs font-semibold uppercase tracking-wider text-slate-400 shrink-0">Use as</span>
                          <span className="text-slate-700">
                            {formCanRead && formCanWrite ? 'Source + Sink' : formCanRead ? 'Source (read)' : formCanWrite ? 'Sink (write)' : <span className="text-red-600">Neither</span>}
                          </span>
                        </div>
                      </>
                    );
                  })()}
                </div>
              </div>

              {/* Test connection panel */}
              <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
                <div className="px-5 py-4 border-b border-slate-100 bg-gradient-to-b from-emerald-50/60 to-white">
                  <div className="text-xs font-bold uppercase tracking-wider text-emerald-700">Test connection</div>
                  <p className="text-sm text-slate-500 mt-1 leading-relaxed">Validates credentials + reachability without saving. Probes from the F-Pulse host.</p>
                </div>
                <div className="px-5 py-4 space-y-3.5">
                  <button
                    type="button"
                    onClick={handleInlineTest}
                    disabled={inlineTestRunning || !detailsReady}
                    className="w-full px-4 py-2.5 text-sm font-bold rounded-lg text-white transition-all shadow-sm hover:shadow-md disabled:opacity-40 disabled:cursor-not-allowed"
                    style={{ background: inlineTestRunning ? '#94a3b8' : 'linear-gradient(135deg, #10B981, #059669)' }}
                  >
                    {inlineTestRunning ? (
                      <span className="inline-flex items-center gap-2">
                        <span className="w-3 h-3 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                        Testing…
                      </span>
                    ) : 'Run test'}
                  </button>

                  {/* Result */}
                  {inlineTestResult && (
                    <div className={`px-3.5 py-3 rounded-lg border text-sm ${inlineTestResult.ok
                      ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
                      : 'bg-red-50 border-red-200 text-red-800'
                    }`}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-bold">
                          {inlineTestResult.ok ? '✓ Connected' : '✗ Failed'}
                        </span>
                        {inlineTestResult.latency_ms != null && (
                          <span className={`text-xs tabular-nums ${inlineTestResult.ok ? 'text-emerald-600' : 'text-red-600'}`}>
                            {Math.round(inlineTestResult.latency_ms)} ms
                          </span>
                        )}
                      </div>
                      {inlineTestResult.detail && (
                        <div className="mt-1 leading-relaxed break-words">{inlineTestResult.detail}</div>
                      )}
                    </div>
                  )}

                  {!inlineTestResult && !inlineTestRunning && (
                    <div className="text-sm text-slate-400 italic leading-relaxed">
                      Fill in the required connection fields, then click Run test. Save details unlock after a successful test.
                    </div>
                  )}
                </div>
              </div>

              {/* Help/tip card */}
              <div className="rounded-xl border border-slate-200 bg-slate-50/60 shadow-sm p-4">
                <div className="text-xs font-bold uppercase tracking-wider text-slate-500 mb-1.5">Tip</div>
                <p className="text-sm text-slate-600 leading-relaxed">
                  A successful test confirms the network path + credentials. It does not validate per-stream permissions — those surface when a pipeline first reads the stream.
                </p>
              </div>
            </aside>
            </div>
          )}
        </div>
        </div>

        {/* Beta confirmation modal — shown once per connector type per
            browser. Tells the user about the gaps before they wire up a
            pipeline so they're not surprised when (e.g.) the sink uses
            basic INSERT instead of bulk load. */}
        {pendingBetaType && (() => {
          const meta = typeMeta(pendingBetaType);
          return (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm" onClick={() => setPendingBetaType(null)}>
              <div className="bg-white rounded-xl shadow-2xl border border-slate-200 max-w-md w-full mx-4 p-5" onClick={(e) => e.stopPropagation()}>
                <div className="flex items-start gap-3">
                  <div className="w-10 h-10 rounded-lg bg-amber-100 flex items-center justify-center shrink-0">
                    <ConnectorIcon type={pendingBetaType} size={24} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <h3 className="text-sm font-bold text-slate-800">{meta.label}</h3>
                      <span className="text-[9px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-amber-100 text-amber-700">Beta</span>
                    </div>
                    <p className="text-xs text-slate-600 leading-relaxed">
                      This connector works but has known gaps relative to a Certified one. Typical limitations:
                    </p>
                    <ul className="mt-2 text-xs text-slate-600 space-y-1 list-disc pl-4">
                      <li>Pagination may be partial — large result sets can be capped</li>
                      <li>Sink writes use basic INSERT (no bulk-load optimization yet)</li>
                      <li>Schema drift is logged but not enforced</li>
                      <li>Test fixtures are incomplete — re-runs may reveal edge cases</li>
                    </ul>
                    <p className="text-xs text-slate-500 mt-3 leading-relaxed">
                      Use it for prototyping and testing freely; double-check with smaller data volumes before relying on it in scheduled production runs.
                    </p>
                  </div>
                </div>
                <div className="mt-4 flex items-center justify-end gap-2">
                  <button
                    onClick={() => setPendingBetaType(null)}
                    className="px-3 py-2 text-xs font-semibold text-slate-600 hover:text-slate-800 rounded-lg hover:bg-slate-100 transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={() => {
                      ackBeta(pendingBetaType);
                      const t = pendingBetaType;
                      setPendingBetaType(null);
                      setFormType(t);
                      setCreateStep(0);
                      if (WRITE_ONLY_TYPES.has(t)) {
                        setFormCanRead(false);
                        setFormCanWrite(true);
                      } else {
                        setFormCanRead(true);
                        setFormCanWrite(true);
                      }
                    }}
                    className="px-4 py-2 text-xs font-bold text-white bg-amber-600 hover:bg-amber-700 rounded-lg transition-colors"
                  >
                    Got it — continue
                  </button>
                </div>
              </div>
            </div>
          );
        })()}
      </div>
    );
  }

  // ── EDIT VIEW ──
  if (view === 'edit' && selectedConnection) {
    const fields = formType ? (CONNECTION_FIELDS[formType] || CONNECTION_FIELDS['rest_api'] || []) : [];
    const meta = typeMeta(selectedConnection.type);
    const editMissingRequired = fields.filter(f => f.required && !formConfig[f.key]);
    const editBasicsReady = !!formName && (formScope !== 'project' || !!formProjectId) && (formCanRead || formCanWrite);
    const editDetailsReady = editMissingRequired.length === 0;
    const editCanSave = editBasicsReady && editDetailsReady;
    const editSteps: Array<{ key: 0 | 1 | 2; label: string; detail: string }> = [
      { key: 0, label: 'Basics', detail: 'Identity and access' },
      { key: 1, label: 'Connection', detail: 'Endpoint and credentials' },
      { key: 2, label: 'Test & Save', detail: 'Validate changes' },
    ];

    return (
      <>
      <TestDialog />
      <div className={`flex-1 flex flex-col overflow-hidden ${dark ? 'bg-[#0B1220]' : 'bg-canvas-bg'}`}>
        <ReadOnlyBanner environment={environment} />
        <div className="flex-1 overflow-auto">

        {/* ── Canonical page header ── */}
        <div className={`sticky top-0 z-30 border-b ${
          environment === 'prod'
            ? 'bg-slate-900 border-slate-700'
            : 'bg-gradient-to-b from-slate-200 to-slate-300 border-slate-400/70'
        }`}>
          <div className="px-8 h-[78px] flex items-center gap-4 relative">
            {/* Left — back + title */}
            <div className="flex items-center gap-3 shrink-0">
              <button
                onClick={() => setView('detail')}
                className={`shrink-0 w-8 h-8 rounded-lg flex items-center justify-center transition-colors ${
                  environment === 'prod'
                    ? 'text-slate-400 hover:bg-white/[0.06] hover:text-white'
                    : 'text-slate-500 hover:bg-white/50 hover:text-slate-800'
                }`}
                title="Back to Connection"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
              </button>
              <div className={`w-10 h-10 rounded-lg flex items-center justify-center shrink-0 ${
                environment === 'prod' ? 'bg-blue-500/20 border border-blue-400/30' : 'bg-blue-100 border border-blue-200'
              }`}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke={environment === 'prod' ? '#93c5fd' : '#3b82f6'} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                  <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
                </svg>
              </div>
              <div>
                <h1 className={`text-lg font-bold flex items-center gap-2 ${
                  environment === 'prod' ? 'text-white' : 'text-slate-800'
                }`}>
                  Connection Editor
                  <TierChip tier={tier} environment={environment} />
                </h1>
                <p className={`text-xs ${
                  environment === 'prod' ? 'text-slate-400' : 'text-slate-500'
                }`}><ConnectorIcon type={selectedConnection.type} size={14} /> {meta.label}</p>
              </div>
            </div>

            {/* Center — connection name */}
            <div className="absolute left-1/2 -translate-x-1/2 text-center pointer-events-none">
              <span className={`text-base font-bold tracking-tight ${
                environment === 'prod' ? 'text-white' : 'text-slate-800'
              }`}>{formName || selectedConnection.name}</span>
            </div>

            {/* Right — actions */}
            <div className="flex items-center gap-3 shrink-0 ml-auto">
              <button
                onClick={() => { setView('detail'); resetCreateForm(); }}
                className={`px-4 py-2 text-sm font-bold rounded-lg transition-all shadow-sm hover:shadow-md ${
                  environment === 'prod'
                    ? 'bg-slate-700 text-slate-200 hover:bg-slate-600'
                    : 'bg-white text-slate-700 border border-slate-300 hover:bg-slate-50'
                }`}
              >
                Cancel
              </button>
              <button
                onClick={async () => {
                  // Save form changes first so the test reflects what's on
                  // screen, but stay on the editor so the user can iterate.
                  // (handleUpdate redirects to list — we don't want that here.)
                  if (!selectedConnection) return;
                  try {
                    if (formName && (formCanRead || formCanWrite)) {
                      await api.updateConnection(selectedConnection.id, {
                        name: formName,
                        description: formDesc,
                        config: formConfig,
                        tags: formTags.split(',').map(t => t.trim()).filter(Boolean),
                        project_id: formScope === 'project' ? formProjectId || null : null,
                        environment: formEnvironment,
                        capabilities: capabilitiesFromForm(formCanRead, formCanWrite),
                      });
                      loadConnections();
                    }
                  } catch (e: any) {
                    toast.error('Save failed before test', e?.message || 'Unknown error');
                  }
                  handleTestConnection(selectedConnection.id);
                }}
                className={`px-4 py-2 text-sm font-bold rounded-lg transition-all shadow-sm hover:shadow-md ${
                  environment === 'prod'
                    ? 'bg-emerald-700 text-white hover:bg-emerald-600'
                    : 'bg-emerald-600 text-white hover:bg-emerald-700'
                }`}
              >
                <span className="flex items-center gap-1.5">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" /></svg>
                  Test
                </span>
              </button>
              <button
                onClick={handleUpdate}
                disabled={!editCanSave}
                className="px-4 py-2 text-sm font-bold text-white rounded-lg transition-all shadow-sm hover:shadow-md disabled:opacity-40"
                style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
              >
                Save Changes
              </button>
            </div>
          </div>
        </div>

        <div className="p-8">
        <div className="max-w-[1400px] mx-auto">
          <div className="rounded-xl border border-slate-200 bg-white shadow-sm px-3 py-3 mb-5">
            <div className="grid grid-cols-3 gap-2">
              {editSteps.map((step, idx) => {
                const active = createStep === step.key;
                const complete = step.key === 0 ? editBasicsReady : step.key === 1 ? editDetailsReady : editCanSave;
                return (
                  <button
                    key={step.key}
                    type="button"
                    onClick={() => setCreateStep(step.key)}
                    className={`text-left rounded-lg border px-3 py-2 transition-all ${
                      active
                        ? 'border-blue-400 bg-blue-50 shadow-sm'
                        : complete
                          ? 'border-emerald-200 bg-emerald-50/60 hover:border-emerald-300'
                          : 'border-slate-200 bg-slate-50 hover:border-slate-300'
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span className={`w-5 h-5 rounded-full text-[10px] font-bold flex items-center justify-center ${
                        active
                          ? 'bg-blue-600 text-white'
                          : complete
                            ? 'bg-emerald-500 text-white'
                            : 'bg-white text-slate-500 border border-slate-200'
                      }`}>
                        {complete && !active ? '✓' : idx + 1}
                      </span>
                      <span className="text-xs font-bold text-slate-800">{step.label}</span>
                    </div>
                    <div className="mt-1 text-[11px] text-slate-500 truncate">{step.detail}</div>
                  </button>
                );
              })}
            </div>
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 items-start">

            {/* ── Left column ── */}
            <div className="space-y-6">
              {/* ── Card: Identity ── */}
              <div className={`${createStep === 0 ? '' : 'hidden'} rounded-xl border border-slate-200 bg-white p-6 shadow-sm space-y-5`}>
                <div className="flex items-center gap-2.5 pb-3 border-b border-slate-100">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 12a5 5 0 1 0 0-10 5 5 0 0 0 0 10z" /><path d="M20 21a8 8 0 0 0-16 0" />
                  </svg>
                  <span className="text-sm font-bold uppercase tracking-wider text-slate-700">Identity</span>
                </div>

                <div>
                  <label className="text-sm font-semibold text-slate-600 block mb-1.5">Connection Name *</label>
                  <input value={formName} onChange={e => setFormName(e.target.value)} placeholder="e.g. Production Oracle ERP" className="w-full text-sm px-4 py-2.5 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300" />
                </div>

                <div>
                  <label className="text-sm font-semibold text-slate-600 block mb-1.5">Description</label>
                  <input value={formDesc} onChange={e => setFormDesc(e.target.value)} placeholder="What is this connection used for?" className="w-full text-sm px-4 py-2.5 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300" />
                </div>
              </div>

              {/* ── Card: Visibility & Access ── */}
              <div className={`${createStep === 0 ? '' : 'hidden'} rounded-xl border border-slate-200 bg-white p-6 shadow-sm space-y-5`}>
                <div className="flex items-center gap-2.5 pb-3 border-b border-slate-100">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="3" y="11" width="18" height="11" rx="2" ry="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" />
                  </svg>
                  <span className="text-sm font-bold uppercase tracking-wider text-slate-700">Visibility & Access</span>
                </div>

                <div>
                  <label className="text-sm font-semibold text-slate-600 block mb-1.5">Scope</label>
                  <div className="flex gap-2">
                    <button type="button" onClick={() => setFormScope('global')} className={`px-4 py-2 rounded-lg text-sm font-semibold border transition-all ${formScope === 'global' ? 'bg-blue-600 text-white border-transparent shadow-sm' : 'bg-slate-50 text-slate-600 hover:bg-slate-100 border-slate-200'}`}>Global</button>
                    <button type="button" onClick={() => setFormScope('project')} className={`px-4 py-2 rounded-lg text-sm font-semibold border transition-all ${formScope === 'project' ? 'bg-blue-600 text-white border-transparent shadow-sm' : 'bg-slate-50 text-slate-600 hover:bg-slate-100 border-slate-200'}`}>Project-scoped</button>
                  </div>
                </div>

                <div>
                  <label className="text-sm font-semibold text-slate-600 block mb-1.5">Environment</label>
                  <div className="flex gap-2">
                    {([
                      { value: 'dev',  label: 'DEV only',  color: '#10b981' },
                      { value: 'prod', label: 'PROD only', color: '#ef4444' },
                      { value: 'all',  label: 'Both',      color: '#64748b' },
                    ] as const).map(opt => (
                      <button
                        key={opt.value}
                        type="button"
                        onClick={() => setFormEnvironment(opt.value)}
                        className={`px-4 py-2 rounded-lg text-sm font-semibold transition-all border ${
                          formEnvironment === opt.value
                            ? 'text-white shadow-sm border-transparent'
                            : 'bg-slate-50 text-slate-600 hover:bg-slate-100 border-slate-200'
                        }`}
                        style={formEnvironment === opt.value ? { background: opt.color } : undefined}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                </div>

                <div>
                  <label className="text-sm font-semibold text-slate-600 block mb-1.5">Use as</label>
                  <div className="flex gap-2.5">
                    <label className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold border cursor-pointer transition-all ${
                      formCanRead ? 'bg-emerald-50 text-emerald-700 border-emerald-300' : 'bg-slate-50 text-slate-500 border-slate-200'
                    }`}>
                      <input type="checkbox" checked={formCanRead} onChange={e => setFormCanRead(e.target.checked)} className="w-4 h-4" />
                      Source (read)
                    </label>
                    <label className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold border cursor-pointer transition-all ${
                      formCanWrite ? 'bg-amber-50 text-amber-700 border-amber-300' : 'bg-slate-50 text-slate-500 border-slate-200'
                    }`}>
                      <input type="checkbox" checked={formCanWrite} onChange={e => setFormCanWrite(e.target.checked)} className="w-4 h-4" />
                      Sink (write)
                    </label>
                  </div>
                  {!formCanRead && !formCanWrite && (
                    <p className="text-xs text-red-500 mt-1.5 font-semibold">Pick at least one — a connection with neither role can't be used by any node.</p>
                  )}
                </div>
              </div>

              {/* ── Card: Organization ── */}
              <div className={`${createStep === 0 ? '' : 'hidden'} rounded-xl border border-slate-200 bg-white p-6 shadow-sm space-y-5`}>
                <div className="flex items-center gap-2.5 pb-3 border-b border-slate-100">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z" /><line x1="7" y1="7" x2="7.01" y2="7" />
                  </svg>
                  <span className="text-sm font-bold uppercase tracking-wider text-slate-700">Organization</span>
                </div>

                <div>
                  <label className="text-sm font-semibold text-slate-600 block mb-1.5">Tags (comma-separated)</label>
                  <input value={formTags} onChange={e => setFormTags(e.target.value)} placeholder="e.g. finance, reporting, daily" className="w-full text-sm px-4 py-2.5 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300" />
                </div>
              </div>
            </div>

            {/* ── Right column ── */}
            <div className="space-y-6">
              {/* ── Card: Connection Details ── */}
              {fields.length > 0 && createStep === 1 && (
                <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm space-y-4">
                  <div className="flex items-center gap-2.5 pb-3 border-b border-slate-100">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
                      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
                    </svg>
                    <span className="text-sm font-bold uppercase tracking-wider text-slate-700">Connection Details</span>
                  </div>

                  {fields.map(f => {
                    const inputCls = 'w-full text-sm px-4 py-2.5 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300';
                    return (
                    <div key={f.key}>
                      <label className="text-sm font-medium text-slate-600 block mb-1.5">
                        {f.label}{f.required && <span className="text-red-400 ml-0.5">*</span>}
                      </label>
                      {f.type === 'select' && f.options ? (
                        <select
                          value={formConfig[f.key] || f.defaultValue || ''}
                          onChange={e => setFormConfig({ ...formConfig, [f.key]: e.target.value })}
                          className={`${inputCls} bg-white`}
                        >
                          <option value="">— Select —</option>
                          {f.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                        </select>
                      ) : f.type === 'checkbox' ? (
                        <label className="flex items-center gap-2.5 cursor-pointer">
                          <input
                            type="checkbox"
                            checked={formConfig[f.key] === 'true' || (formConfig[f.key] as unknown) === true || (formConfig[f.key] === undefined && f.defaultValue === 'true')}
                            onChange={e => setFormConfig({ ...formConfig, [f.key]: e.target.checked ? 'true' : 'false' })}
                            className="w-4 h-4 rounded border-slate-300 text-blue-600 focus:ring-blue-300"
                          />
                          <span className="text-sm text-slate-600">{f.hint || 'Enable'}</span>
                        </label>
                      ) : f.type === 'textarea' ? (
                        <textarea
                          value={formConfig[f.key] || ''}
                          onChange={e => setFormConfig({ ...formConfig, [f.key]: e.target.value })}
                          placeholder={f.placeholder}
                          rows={3}
                          className={`${inputCls} font-mono text-sm`}
                        />
                      ) : (
                        <input
                          type={f.type === 'number' ? 'text' : f.type}
                          inputMode={f.type === 'number' ? 'numeric' : undefined}
                          value={formConfig[f.key] ?? f.defaultValue ?? ''}
                          onChange={e => setFormConfig({ ...formConfig, [f.key]: e.target.value })}
                          placeholder={f.placeholder}
                          className={inputCls}
                        />
                      )}
                      {f.hint && f.type !== 'checkbox' && (
                        <p className="text-xs mt-1 text-slate-400">{f.hint}</p>
                      )}
                    </div>
                    );
                  })}
                </div>
              )}
              {createStep === 2 && (
                <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm space-y-4">
                  <div className="flex items-center gap-2.5 pb-3 border-b border-slate-100">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" />
                    </svg>
                    <span className="text-sm font-bold uppercase tracking-wider text-slate-700">Ready to test and save</span>
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
                    <div className={`rounded-lg border px-3 py-2 ${editBasicsReady ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-amber-50 border-amber-200 text-amber-800'}`}>
                      <div className="font-bold">Basics</div>
                      <div>{editBasicsReady ? 'Complete' : 'Name, project scope, or source/sink role needs attention.'}</div>
                    </div>
                    <div className={`rounded-lg border px-3 py-2 ${editDetailsReady ? 'bg-emerald-50 border-emerald-200 text-emerald-800' : 'bg-amber-50 border-amber-200 text-amber-800'}`}>
                      <div className="font-bold">Connection details</div>
                      <div>{editDetailsReady ? 'Required fields complete' : `${editMissingRequired.length} required field${editMissingRequired.length === 1 ? '' : 's'} missing.`}</div>
                    </div>
                  </div>
                  {editMissingRequired.length > 0 && (
                    <p className="text-xs text-amber-600">
                      Missing: {editMissingRequired.map(f => f.label).join(', ')}
                    </p>
                  )}
                </div>
              )}
            </div>

          </div>
          <div className="flex gap-3 pt-5">
            {createStep > 0 && (
              <button
                type="button"
                onClick={() => setCreateStep((createStep - 1) as 0 | 1 | 2)}
                className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50"
              >
                Back
              </button>
            )}
            {createStep < 2 ? (
              <button
                type="button"
                onClick={() => setCreateStep((createStep + 1) as 0 | 1 | 2)}
                className="px-5 py-2 text-white text-sm font-bold rounded-lg"
                style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
              >
                Continue
              </button>
            ) : (
              <button
                onClick={handleUpdate}
                disabled={!editCanSave}
                className="px-5 py-2 text-white text-sm font-bold rounded-lg disabled:opacity-40"
                style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
              >
                Save Changes
              </button>
            )}
            <button onClick={() => { setView('detail'); resetCreateForm(); }} className="px-4 py-2 text-sm text-slate-500 border border-slate-200 rounded-lg hover:bg-slate-50">Cancel</button>
          </div>
        </div>
        </div>
        </div>
      </div>
      </>
    );
  }

  // ── DETAIL VIEW — Connection + Reports ──
  if (view === 'detail' && selectedConnection) {
    const meta = typeMeta(selectedConnection.type);
    const openEdit = () => {
      setFormName(selectedConnection.name);
      setFormType(selectedConnection.type);
      setFormDesc(selectedConnection.description || '');
      setFormConfig(selectedConnection.config as Record<string, string>);
      setFormTags((selectedConnection.tags || []).join(', '));
      setFormScope(selectedConnection.project_id ? 'project' : 'global');
      setFormProjectId(selectedConnection.project_id || '');
      setFormEnvironment((selectedConnection.environment as 'dev' | 'prod' | 'all') || 'all');
      const cf = formFromCapabilities(selectedConnection.capabilities);
      setFormCanRead(cf.canRead);
      setFormCanWrite(cf.canWrite);
      setCreateStep(0);
      setView('edit');
    };

    return (
      <>
      <TestDialog />
      <div className="flex-1 flex flex-col overflow-hidden">
        <ReadOnlyBanner environment={environment} />
        <div className="flex-1 overflow-auto">

        {/* ── Canonical page header ── */}
        <div className={`sticky top-0 z-30 border-b ${
          environment === 'prod'
            ? 'bg-slate-900 border-slate-700'
            : 'bg-gradient-to-b from-slate-200 to-slate-300 border-slate-400/70'
        }`}>
          <div className="px-8 h-[78px] flex items-center justify-between gap-4">
            <div className="min-w-0 flex items-center gap-3">
              <button
                onClick={() => { setView('list'); setReportResult(null); }}
                className={`shrink-0 w-8 h-8 rounded-lg flex items-center justify-center transition-colors ${
                  environment === 'prod'
                    ? 'text-slate-400 hover:bg-white/[0.06] hover:text-white'
                    : 'text-slate-500 hover:bg-white/50 hover:text-slate-800'
                }`}
                title="Back to Connections"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
              </button>
              <div className="w-10 h-10 rounded-lg flex items-center justify-center shrink-0" style={{ background: meta.color + '18', border: `1px solid ${meta.color}30` }}>
                <ConnectorIcon type={selectedConnection.type} size={24} />
              </div>
              <div className="min-w-0">
                <h1 className={`text-xl font-bold flex items-center gap-2 truncate ${
                  environment === 'prod' ? 'text-white' : 'text-slate-800'
                }`}>
                  {selectedConnection.name}
                  <ScopeBadge projectId={selectedConnection.project_id} />
                  <TierChip tier={tier} environment={environment} />
                </h1>
                <p className={`text-xs mt-0.5 flex items-center gap-2 flex-wrap ${
                  environment === 'prod' ? 'text-slate-400' : 'text-slate-500'
                }`}>
                  <span>{meta.label}</span>
                  <span aria-hidden="true">·</span>
                  <span>Created {new Date(selectedConnection.created_at).toLocaleDateString()}</span>
                  {selectedConnection.description && (
                    <>
                      <span aria-hidden="true">·</span>
                      <span className="truncate">{selectedConnection.description}</span>
                    </>
                  )}
                  {/* 2026-05-25 — Health pill, surfaces last_test_at +
                      last_test_ok in the header so users see freshness
                      WITHOUT scrolling. Three states: Healthy / Failed /
                      Never tested. */}
                  {(() => {
                    const lastTest = selectedConnection.last_test_at;
                    const ok = selectedConnection.last_test_ok;
                    const fmtAgo = (iso: string) => {
                      const diffMs = Date.now() - new Date(iso).getTime();
                      const m = Math.floor(diffMs / 60000);
                      if (m < 1) return 'just now';
                      if (m < 60) return `${m}m ago`;
                      const h = Math.floor(m / 60);
                      if (h < 24) return `${h}h ago`;
                      const d = Math.floor(h / 24);
                      return `${d}d ago`;
                    };
                    if (!lastTest) {
                      return (
                        <>
                          <span aria-hidden="true">·</span>
                          <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-slate-100 text-slate-500 text-[10px] font-semibold uppercase tracking-wide">
                            <span className="w-1.5 h-1.5 rounded-full bg-slate-400" />
                            Never tested
                          </span>
                        </>
                      );
                    }
                    if (ok === false) {
                      return (
                        <>
                          <span aria-hidden="true">·</span>
                          <span
                            title={selectedConnection.last_test_error || 'Last test failed'}
                            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-50 text-red-600 text-[10px] font-semibold uppercase tracking-wide border border-red-200"
                          >
                            <span className="w-1.5 h-1.5 rounded-full bg-red-500" />
                            Failed · {fmtAgo(lastTest)}
                          </span>
                        </>
                      );
                    }
                    return (
                      <>
                        <span aria-hidden="true">·</span>
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 text-[10px] font-semibold uppercase tracking-wide border border-emerald-200">
                          <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
                          Healthy · tested {fmtAgo(lastTest)}
                        </span>
                      </>
                    );
                  })()}
                </p>
                {/* #12 step 3 — cert/capability chips for the connector
                    type. Pulled from /api/connectors/cert-matrix (cached
                    module-level after first fetch). Renders nothing
                    while loading or if the connector id is missing
                    from the matrix, so the header doesn't jitter. */}
                <div className="mt-1.5">
                  <CertChipsForType type={selectedConnection.type} />
                </div>
              </div>
            </div>

            <div className="flex items-center gap-2 shrink-0">
              {canCreate && (
                <button onClick={openEdit} className={`px-3.5 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
                  environment === 'prod'
                    ? 'text-blue-300 border border-blue-500/30 hover:bg-blue-500/10'
                    : 'text-blue-600 border border-blue-200 hover:bg-blue-50'
                }`}>
                  <span className="flex items-center gap-1.5">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></svg>
                    Edit
                  </span>
                </button>
              )}
              <button
                onClick={() => handleTestConnection(selectedConnection.id)}
                className={`px-3.5 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
                  environment === 'prod'
                    ? 'text-emerald-300 border border-emerald-500/30 hover:bg-emerald-500/10'
                    : 'text-emerald-600 border border-emerald-200 hover:bg-emerald-50'
                }`}
              >
                <span className="flex items-center gap-1.5">
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><polyline points="22 4 12 14.01 9 11.01" /></svg>
                  Test
                </span>
              </button>
              {canDelete && (
                <button
                  onClick={() => handleDelete(selectedConnection.id)}
                  className={`px-3.5 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
                    environment === 'prod'
                      ? 'text-red-300 border border-red-500/30 hover:bg-red-500/10'
                      : 'text-red-500 border border-red-200 hover:bg-red-50'
                  }`}
                >
                  Delete
                </button>
              )}
            </div>
          </div>
        </div>

        <div className="p-6">
        <div className="max-w-[1400px] mx-auto">

          {/* ── STAT STRIP — at-a-glance metrics (2026-05-25 polish) ──
              Four lightweight tiles: reports, used-by pipelines,
              capabilities, environment. Anchors the top of the body so
              the page doesn't open with the eye dropping into config-
              field labels. */}
          <div className="mb-5 grid grid-cols-2 sm:grid-cols-4 gap-3">
            {([
              {
                label: 'Reports',
                value: reports.length,
                suffix: undefined,
                accent: 'bg-blue-500',
              },
              {
                label: 'Used by',
                value: usedByLoading ? '…' : usedByPipelines.length,
                suffix: usedByPipelines.length === 1 ? 'pipeline' : 'pipelines',
                accent: 'bg-emerald-500',
              },
              {
                label: 'Capabilities',
                value: ((selectedConnection.capabilities && selectedConnection.capabilities.length > 0)
                  ? selectedConnection.capabilities
                  : ['read', 'write']).map(c => c[0].toUpperCase() + c.slice(1)).join(' · '),
                suffix: undefined,
                accent: 'bg-cyan-500',
              },
              {
                label: 'Environment',
                value: (selectedConnection.environment || 'all').toUpperCase(),
                suffix: undefined,
                accent: selectedConnection.environment === 'prod' ? 'bg-red-500' : 'bg-slate-400',
              },
            ] as Array<{ label: string; value: number | string; suffix?: string; accent: string }>).map((s) => (
              <div
                key={s.label}
                className={`relative rounded-xl border shadow-sm overflow-hidden ${dark ? 'bg-[#0f1726] border-white/[0.08]' : 'bg-white border-slate-200'}`}
              >
                <span aria-hidden="true" className={`absolute left-0 top-0 bottom-0 w-1 ${s.accent}`} />
                <div className="pl-4 pr-3 py-3">
                  <div className={`text-[10px] font-bold uppercase tracking-wider ${dark ? 'text-slate-500' : 'text-slate-400'}`}>{s.label}</div>
                  <div className="flex items-baseline gap-1.5 mt-1">
                    <span className={`text-lg font-bold tabular-nums ${dark ? 'text-slate-100' : 'text-slate-800'}`}>{s.value}</span>
                    {('suffix' in s && s.suffix) ? (
                      <span className={`text-[11px] font-semibold ${dark ? 'text-slate-500' : 'text-slate-400'}`}>{s.suffix}</span>
                    ) : null}
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* ── TWO-COLUMN BODY ──────────────────────────────────────
              Left (2/3): Configuration (improved with copy + reveal).
              Right (1/3): Used By + Recent Activity panels.
              Stacks to single column under 1024px. */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 mb-5">

            {/* CONFIGURATION (improved) */}
            <div className={`lg:col-span-2 rounded-xl border shadow-sm p-5 ${dark ? 'bg-[#0f1726] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
              <div className="flex items-center gap-2 pb-2 border-b border-slate-100 mb-3">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
                  <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
                </svg>
                <span className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-700'}`}>Configuration</span>
              </div>
              {Object.keys(selectedConnection.config).length === 0 ? (
                <div className="text-xs text-slate-400 italic py-2">No configuration fields set.</div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {(() => {
                    // Group host+port into a single "Server" row so a
                    // DB connection reads naturally as host:port instead
                    // of two disconnected cells. Falls back gracefully
                    // when either side is missing.
                    const entries = Object.entries(selectedConnection.config);
                    const hostKey = entries.find(([k]) => k === 'host' || k === 'server' || k === 'hostname')?.[0];
                    const portKey = entries.find(([k]) => k === 'port')?.[0];
                    const rows: Array<{ key: string; label: string; value: string; secret: boolean }> = [];
                    let mergedHost = false;
                    for (const [k, v] of entries) {
                      if (k === hostKey && portKey) {
                        const p = selectedConnection.config[portKey];
                        rows.push({ key: 'server', label: 'Server', value: `${v}${p !== undefined && p !== '' ? ':' + p : ''}`, secret: false });
                        mergedHost = true;
                      } else if (k === portKey && mergedHost) {
                        continue;
                      } else {
                        const lk = k.toLowerCase();
                        const isSecret = typeof v === 'string' && (
                          lk.includes('password') || lk.includes('token') ||
                          lk.includes('secret') || lk.includes('api_key') ||
                          lk.includes('apikey')
                        );
                        rows.push({ key: k, label: k.replace(/_/g, ' '), value: String(v), secret: isSecret });
                      }
                    }
                    return rows.map(({ key, label, value, secret }) => {
                      const revealed = revealedSecrets.has(key);
                      const displayValue = secret && !revealed ? '••••••••' : value;
                      const canCopy = !secret || revealed;
                      return (
                        <div key={key} className={`group flex items-center gap-2 px-3 py-2 rounded-lg ${dark ? 'bg-white/[0.03]' : 'bg-slate-50'}`}>
                          <div className="flex-1 min-w-0">
                            <div className={`text-[10px] font-semibold uppercase tracking-wider ${dark ? 'text-slate-500' : 'text-slate-400'}`}>{label}</div>
                            <div className={`text-sm font-medium font-mono truncate ${dark ? 'text-slate-200' : 'text-slate-700'}`} title={displayValue}>{displayValue}</div>
                          </div>
                          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
                            {secret && (
                              <button
                                onClick={() => {
                                  setRevealedSecrets(prev => {
                                    const next = new Set(prev);
                                    if (next.has(key)) next.delete(key); else next.add(key);
                                    return next;
                                  });
                                }}
                                title={revealed ? 'Hide value' : 'Reveal value'}
                                aria-label={revealed ? 'Hide value' : 'Reveal value'}
                                className="p-1 rounded hover:bg-slate-200 text-slate-500"
                              >
                                {revealed ? (
                                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" /><line x1="1" y1="1" x2="23" y2="23" /></svg>
                                ) : (
                                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" /></svg>
                                )}
                              </button>
                            )}
                            {canCopy && (
                              <button
                                onClick={() => {
                                  navigator.clipboard.writeText(value).then(() => {
                                    setCopiedField(key);
                                    setTimeout(() => setCopiedField(curr => curr === key ? null : curr), 1500);
                                  }).catch(() => {});
                                }}
                                title={copiedField === key ? 'Copied!' : 'Copy to clipboard'}
                                aria-label="Copy to clipboard"
                                className={`p-1 rounded hover:bg-slate-200 ${copiedField === key ? 'text-emerald-600' : 'text-slate-500'}`}
                              >
                                {copiedField === key ? (
                                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="20 6 9 17 4 12" /></svg>
                                ) : (
                                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
                                )}
                              </button>
                            )}
                          </div>
                        </div>
                      );
                    });
                  })()}
                </div>
              )}
            </div>

            {/* SIDEBAR: Used By + Recent Activity */}
            <div className="space-y-5">

              {/* Used By — pipelines that reference this connection.
                  Click a row to jump to the Pipelines page. */}
              <div className={`rounded-xl border shadow-sm p-5 ${dark ? 'bg-[#0f1726] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                <div className="flex items-center gap-2 pb-2 border-b border-slate-100 mb-3">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="4 17 10 11 4 5" /><line x1="12" y1="19" x2="20" y2="19" />
                  </svg>
                  <span className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-700'}`}>Used By</span>
                  <span className="ml-auto text-[10px] font-semibold text-slate-400 tabular-nums">{usedByLoading ? '…' : usedByPipelines.length}</span>
                </div>
                {usedByLoading ? (
                  <div className="text-xs text-slate-400 italic">Scanning pipelines…</div>
                ) : usedByPipelines.length === 0 ? (
                  <div className="text-xs text-slate-400 italic">No pipelines reference this connection yet.</div>
                ) : (
                  <ul className="space-y-1">
                    {usedByPipelines.slice(0, 6).map(p => (
                      <li key={p.id}>
                        <button
                          onClick={() => { window.location.hash = 'pipelines'; }}
                          className="w-full text-left flex items-center gap-1.5 px-2 py-1 text-xs text-slate-700 rounded hover:bg-slate-50 truncate group"
                          title={`Open ${p.name}`}
                        >
                          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-slate-400 shrink-0 group-hover:text-blue-500"><polyline points="9 18 15 12 9 6" /></svg>
                          <span className="truncate group-hover:text-blue-600">{p.name}</span>
                        </button>
                      </li>
                    ))}
                    {usedByPipelines.length > 6 && (
                      <li className="text-[10px] text-slate-400 italic px-2 pt-1">+ {usedByPipelines.length - 6} more…</li>
                    )}
                  </ul>
                )}
              </div>

              {/* Recent Activity — for v1 the only durable signal is the
                  last test result; surfaced as a single event card. When
                  /connections/{id}/activity ships this will expand to a
                  full feed. Empty state nudges to test. */}
              <div className={`rounded-xl border shadow-sm p-5 ${dark ? 'bg-[#0f1726] border-white/[0.08]' : 'bg-white border-slate-200'}`}>
                <div className="flex items-center gap-2 pb-2 border-b border-slate-100 mb-3">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#475569" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
                  </svg>
                  <span className={`text-xs font-bold uppercase tracking-wider ${dark ? 'text-slate-400' : 'text-slate-700'}`}>Recent Activity</span>
                </div>
                {(() => {
                  const lt = selectedConnection.last_test_at;
                  const ok = selectedConnection.last_test_ok;
                  if (!lt) {
                    return (
                      <div className="text-xs text-slate-400 italic">
                        No activity yet.
                        {canCreate && (
                          <button
                            onClick={() => handleTestConnection(selectedConnection.id)}
                            className="ml-1 text-blue-600 hover:underline not-italic font-medium"
                          >Run test now →</button>
                        )}
                      </div>
                    );
                  }
                  const dot = ok === false ? 'bg-red-500' : 'bg-emerald-500';
                  const label = ok === false ? 'Test failed' : 'Test passed';
                  const labelColor = ok === false ? 'text-red-700' : 'text-emerald-700';
                  return (
                    <ul className="space-y-2">
                      <li className="flex items-start gap-2 text-xs">
                        <span className={`mt-1 w-1.5 h-1.5 rounded-full shrink-0 ${dot}`} />
                        <div className="flex-1 min-w-0">
                          <div className={`font-semibold ${labelColor}`}>{label}</div>
                          <div className="text-slate-500">{new Date(lt).toLocaleString()}</div>
                          {ok === false && selectedConnection.last_test_error && (
                            <div className="mt-1 text-[11px] text-red-600 line-clamp-2" title={selectedConnection.last_test_error}>{selectedConnection.last_test_error}</div>
                          )}
                        </div>
                      </li>
                    </ul>
                  );
                })()}
              </div>

            </div>
          </div>
          {/* Reports section */}
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-bold text-slate-700 uppercase tracking-wider">
              Reports & Queries ({reports.length})
            </h3>
            {canCreate && (
            <button
              onClick={() => setShowReportForm(true)}
              className="px-3 py-1.5 text-xs font-bold text-blue-600 border border-blue-200 rounded-lg hover:bg-blue-50"
            >
              + New Report
            </button>
            )}
          </div>

          {/* Create Report form */}
          {showReportForm && (
            <div className="bg-blue-50/50 rounded-lg border border-blue-200 p-4 mb-4 space-y-3">
              <div className="text-xs font-bold text-blue-600 uppercase">New Report</div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-sm font-semibold text-slate-700 block mb-1.5">Report Name *</label>
                  <input value={reportName} onChange={e => setReportName(e.target.value)} placeholder="e.g. Monthly Revenue" className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-300" />
                </div>
                <div>
                  <label className="text-sm font-semibold text-slate-700 block mb-1.5">Description</label>
                  <input value={reportDesc} onChange={e => setReportDesc(e.target.value)} placeholder="What does this report do?" className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-300" />
                </div>
              </div>
              <div>
                <label className="text-sm font-semibold text-slate-700 block mb-1.5">Query / API Template * <span className="text-slate-400 font-normal">(use {'{{param_name}}'} for parameters)</span></label>
                <textarea value={reportQuery} onChange={e => setReportQuery(e.target.value)} rows={4} placeholder={`SELECT * FROM orders WHERE date >= '{{start_date}}' AND status = '{{status}}'`} className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg bg-white font-mono focus:outline-none focus:ring-2 focus:ring-blue-300 resize-none" />
              </div>

              {/* Parameters */}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="text-xs font-medium text-slate-500">Parameters</label>
                  <button
                    onClick={() => setReportParams([...reportParams, { name: '', type: 'string', default: '', required: true }])}
                    className="text-xs text-blue-500 font-semibold hover:text-blue-700"
                  >+ Add Parameter</button>
                </div>
                {reportParams.map((p, i) => (
                  <div key={i} className="flex gap-2 mb-2 items-center">
                    <input value={p.name} onChange={e => { const np = [...reportParams]; np[i].name = e.target.value; setReportParams(np); }} placeholder="name" className="flex-1 text-xs px-2 py-1.5 border border-slate-200 rounded-lg bg-white" />
                    <select value={p.type} onChange={e => { const np = [...reportParams]; np[i].type = e.target.value; setReportParams(np); }} className="text-xs px-2 py-1.5 border border-slate-200 rounded-lg bg-white">
                      <option value="string">String</option>
                      <option value="number">Number</option>
                      <option value="date">Date</option>
                      <option value="boolean">Boolean</option>
                    </select>
                    <input value={p.default} onChange={e => { const np = [...reportParams]; np[i].default = e.target.value; setReportParams(np); }} placeholder="default" className="w-24 text-xs px-2 py-1.5 border border-slate-200 rounded-lg bg-white" />
                    <button onClick={() => setReportParams(reportParams.filter((_, j) => j !== i))} className="text-red-400 hover:text-red-600 text-xs">x</button>
                  </div>
                ))}
              </div>

              <div className="flex gap-2">
                <button onClick={handleCreateReport} disabled={!reportName || !reportQuery} className="px-4 py-1.5 text-xs font-bold text-white rounded-lg disabled:opacity-40" style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}>Create Report</button>
                <button onClick={() => setShowReportForm(false)} className="px-3 py-1.5 text-xs text-slate-500 border border-slate-200 rounded-lg hover:bg-slate-50">Cancel</button>
              </div>
            </div>
          )}

          {/* Reports list */}
          {reports.length === 0 && !showReportForm ? (
            <div className="rounded-xl border border-slate-200 shadow-sm bg-white p-6">
              <div className="flex items-start gap-4">
                <div className="w-10 h-10 rounded-lg bg-blue-50 text-blue-600 flex items-center justify-center shrink-0">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18" /><path d="M9 21V9" /></svg>
                </div>
                <div className="flex-1 min-w-0">
                  <h4 className="text-sm font-bold text-slate-800">No reports yet</h4>
                  <p className="text-xs text-slate-500 mt-0.5">
                    Reports let you save a parameterised query against this connection — run it ad-hoc or schedule it. Results stream straight into Storage so dashboards stay fresh.
                  </p>
                  {canCreate && (
                    <div className="mt-3 flex items-center gap-2 flex-wrap">
                      <button
                        onClick={() => setShowReportForm(true)}
                        className="px-3 py-1.5 text-xs font-bold text-white rounded-lg"
                        style={{ background: 'linear-gradient(135deg, #3B7DD8, #1E5AAF)' }}
                      >+ Create your first report</button>
                      <span className="text-[11px] text-slate-400">e.g. <code className="px-1 py-0.5 bg-slate-100 rounded text-slate-600">{`SELECT * FROM orders WHERE date >= {{start}}`}</code></span>
                    </div>
                  )}
                </div>
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              {reports.map((report) => (
                <div key={report.id} className="rounded-lg border border-slate-200 shadow-sm hover:shadow-lg hover:-translate-y-0.5 transition-all duration-200 p-4" style={{ background: dark ? '#111827' : 'linear-gradient(135deg, #FFFFFF 0%, #FAFBFF 100%)' }}>
                  <div className="flex items-start justify-between mb-3">
                    <div>
                      <div className="font-semibold text-slate-800 text-sm">{report.name}</div>
                      {report.description && <div className="text-xs text-slate-400 mt-0.5">{report.description}</div>}
                    </div>
                    {canDelete && (
                    <button
                      onClick={async () => {
                        if (!(await uiConfirm({ message: 'Delete this report?', danger: true, confirmLabel: 'Delete' }))) return;
                        api.deleteConnectionReport(selectedConnection.id, report.id).then(() => loadReports(selectedConnection.id));
                      }}
                      className="text-xs text-red-400 hover:text-red-600"
                    >Delete</button>
                    )}
                  </div>

                  {/* Query template */}
                  <div className="bg-slate-50 rounded-lg p-2.5 mb-3 font-mono text-xs text-slate-600 whitespace-pre-wrap border border-slate-100">
                    {report.query_template}
                  </div>

                  {/* Parameters + Run */}
                  {report.parameters.length > 0 && (
                    <div className="flex flex-wrap gap-3 mb-3">
                      {report.parameters.map(p => (
                        <div key={p.name} className="flex-1 min-w-[150px]">
                          <label className="text-sm font-semibold text-slate-700 block mb-1.5">
                            {p.name} {p.required && <span className="text-red-400">*</span>}
                          </label>
                          <input
                            type={p.type === 'date' ? 'date' : p.type === 'number' ? 'number' : 'text'}
                            value={paramValues[p.name] || p.default || ''}
                            onChange={e => setParamValues({ ...paramValues, [p.name]: e.target.value })}
                            placeholder={p.default || p.name}
                            className="w-full text-xs px-2.5 py-1.5 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-300"
                          />
                        </div>
                      ))}
                    </div>
                  )}

                  <button
                    onClick={() => handleRunReport(report.id)}
                    disabled={runningReport === report.id}
                    className="px-4 py-1.5 text-white text-xs font-bold rounded-lg disabled:opacity-50 flex items-center gap-1.5"
                    style={{ background: runningReport === report.id ? '#94a3b8' : 'linear-gradient(135deg, #22c55e, #16a34a)' }}
                  >
                    {runningReport === report.id ? (
                      <><span className="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin" />Running...</>
                    ) : (
                      <><svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3" /></svg>Run Report</>
                    )}
                  </button>

                  {/* Results */}
                  {reportResult && reportResult.report_id === report.id && (
                    <div className="mt-3 border-t border-slate-100 pt-3">
                      {reportResult.error ? (
                        <div className="bg-red-50 border border-red-200 rounded-lg p-3 text-xs text-red-500">{reportResult.error}</div>
                      ) : (
                        <>
                          <div className="flex items-center gap-2 mb-2">
                            <span className="text-xs font-bold text-emerald-600 uppercase">Result</span>
                            <span className="text-xs text-slate-400">{reportResult.row_count} rows · {reportResult.duration_ms}ms</span>
                          </div>
                          <div className="overflow-auto max-h-60 rounded-lg border border-slate-200">
                            <table className="w-full text-xs border-collapse">
                              <thead className="sticky top-0 bg-gradient-to-r from-slate-900 via-blue-950 to-slate-900">
                                <tr>
                                  {reportResult.columns?.map((col: string) => (
                                    <th key={col} className="px-3 py-1.5 text-left text-xs font-bold text-amber-300 uppercase tracking-wider border-b-2 border-amber-400/40 whitespace-nowrap">{col}</th>
                                  ))}
                                </tr>
                              </thead>
                              <tbody>
                                {reportResult.sample_data?.map((row: any, i: number) => (
                                  <tr key={i} className="hover:bg-blue-50/30 border-b border-slate-100">
                                    {reportResult.columns?.map((col: string) => (
                                      <td key={col} className="px-3 py-1.5 text-slate-600 whitespace-nowrap">{row[col] === null ? <span className="italic text-slate-400">null</span> : String(row[col])}</td>
                                    ))}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        </>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
        </div>
        </div>
      </div>
      </>
    );
  }

  return null;
}
