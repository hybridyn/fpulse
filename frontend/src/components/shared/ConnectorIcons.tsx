import React from 'react';

interface IconProps {
  size?: number;
  className?: string;
}

const CdnImg = ({
  size,
  src,
  alt,
  className,
}: {
  size: number;
  src: string;
  alt: string;
  className?: string;
}) => (
  <img
    src={src}
    alt={alt}
    width={size}
    height={size}
    className={className}
    style={{ width: size, height: size, objectFit: 'contain', display: 'block' }}
  />
);

const Mono = ({
  size,
  color,
  text,
  fg = '#ffffff',
  className,
}: {
  size: number;
  color: string;
  text: string;
  fg?: string;
  className?: string;
}) => {
  const fontSize = text.length >= 3 ? 7 : text.length === 2 ? 9 : 12;
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      className={className}
      aria-hidden="true"
    >
      <rect width="24" height="24" rx="4" fill={color} />
      <text
        x="12"
        y="12"
        textAnchor="middle"
        dominantBaseline="central"
        fill={fg}
        fontSize={fontSize}
        fontWeight={700}
        fontFamily="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
        letterSpacing="0"
      >
        {text}
      </text>
    </svg>
  );
};

// All brand SVGs bundled locally under /public/connector-icons/.
// No runtime CDN dependency — works in air-gapped enterprise deployments.
const LOCAL = (slug: string) => `/connector-icons/${slug}.svg`;

// ─── Databases ───
const PostgreSQL = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="PostgreSQL" src={LOCAL('postgresql')} />;
const MySQL = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="MySQL" src={LOCAL('mysql')} />;
const SQLServer = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="SQL Server" src={LOCAL('mssql')} />;
const OracleDB = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <rect x="1.5" y="7.5" width="21" height="9" rx="4.5" fill="none" stroke="#C74634" strokeWidth="3" />
  </svg>
);
const SQLite = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="SQLite" src={LOCAL('sqlite')} />;
const MariaDB = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="MariaDB" src={LOCAL('mariadb')} />;
const CockroachDB = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="CockroachDB" src={LOCAL('cockroachdb')} />;
const Db2 = ({ size = 24, className }: IconProps) => <Mono size={size} className={className} color="#054ADA" text="Db2" />;
const Teradata = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <rect width="24" height="24" rx="4" fill="#F37440" />
    <path d="M5 6h14v3.2h-5.2V18h-3.6V9.2H5z" fill="#fff" />
  </svg>
);

// ─── NoSQL ───
const MongoDB = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="MongoDB" src={LOCAL('mongodb')} />;
const Cassandra = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Cassandra" src={LOCAL('cassandra')} />;
const Couchbase = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Couchbase" src={LOCAL('couchbase')} />;
const DynamoDB = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="DynamoDB" src={LOCAL('dynamodb')} />;
const CosmosDB = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <circle cx="12" cy="12" r="10" fill="#0078D4" />
    <ellipse cx="12" cy="12" rx="9" ry="3.5" fill="none" stroke="#fff" strokeWidth="1.2" />
    <ellipse cx="12" cy="12" rx="9" ry="3.5" fill="none" stroke="#fff" strokeWidth="1.2" transform="rotate(60 12 12)" />
    <ellipse cx="12" cy="12" rx="9" ry="3.5" fill="none" stroke="#fff" strokeWidth="1.2" transform="rotate(120 12 12)" />
    <circle cx="12" cy="12" r="2" fill="#fff" />
  </svg>
);
const Neo4j = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Neo4j" src={LOCAL('neo4j')} />;
const Firebase = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Firebase" src={LOCAL('firebase')} />;

// ─── Data Warehouses ───
const Snowflake = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Snowflake" src={LOCAL('snowflake')} />;
const BigQuery = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="BigQuery" src={LOCAL('bigquery')} />;
const Redshift = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Amazon Redshift" src={LOCAL('redshift')} />;
const Databricks = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Databricks" src={LOCAL('databricks')} />;
const Synapse = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Azure Synapse" src={LOCAL('synapse')} />;
const ClickHouse = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="ClickHouse" src={LOCAL('clickhouse')} />;
const Trino = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Trino" src={LOCAL('trino')} />;
const Athena = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <rect width="24" height="24" rx="4" fill="#232F3E" />
    <path d="M5 16.8c4.2 2.2 9.3 2.1 13.8-.4" fill="none" stroke="#FF9900" strokeWidth="1.8" strokeLinecap="round" />
    <path d="M16.8 15.3l2.2.5-.8 2" fill="none" stroke="#FF9900" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    <text x="12" y="12.2" textAnchor="middle" dominantBaseline="central" fill="#fff" fontSize="7" fontWeight="800" fontFamily="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">A</text>
  </svg>
);

// ─── Search & Cache ───
const Elasticsearch = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Elasticsearch" src={LOCAL('elasticsearch')} />;
const OpenSearch = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="OpenSearch" src={LOCAL('opensearch')} />;
const Redis = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Redis" src={LOCAL('redis')} />;

// ─── Cloud Storage ───
const S3 = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="AWS S3" src={LOCAL('s3')} />;
const AzureBlob = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <path d="M6 2.5h12L22 12l-4 9.5H6L2 12z" fill="#0078D4" />
    <path d="M9 6.5h5.2L17 9.3V18H9z" fill="none" stroke="#fff" strokeWidth="1.1" strokeLinejoin="round" />
    <path d="M14.2 6.5v3h2.8" fill="none" stroke="#fff" strokeWidth="1.1" strokeLinejoin="round" />
    <text x="13" y="12.7" textAnchor="middle" fill="#fff" fontSize="3.2" fontWeight="700" fontFamily="system-ui">10</text>
    <text x="13" y="16.5" textAnchor="middle" fill="#fff" fontSize="3.2" fontWeight="700" fontFamily="system-ui">01</text>
  </svg>
);
const ADLSGen2 = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Azure Data Lake Storage" src={LOCAL('adls')} />;
const GCS = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Google Cloud Storage" src={LOCAL('gcs')} />;
const MinIO = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="MinIO" src={LOCAL('minio')} />;

// ─── Files & Enterprise ───
const SharePoint = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <circle cx="15.5" cy="6.5" r="4.5" fill="#1F9C9F" />
    <circle cx="18" cy="13" r="4" fill="#03787C" />
    <circle cx="13" cy="18.5" r="3.2" fill="#36C5C9" />
    <rect x="2" y="4" width="10" height="10" rx="1.6" fill="#03787C" />
    <text x="7" y="11.6" textAnchor="middle" fill="#fff" fontSize="9" fontWeight="700" fontFamily="-apple-system, system-ui, sans-serif">S</text>
  </svg>
);
const OneDrive = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="OneDrive" src={LOCAL('onedrive')} />;
const GDrive = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Google Drive" src={LOCAL('gdrive')} />;
const Dropbox = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Dropbox" src={LOCAL('dropbox')} />;
const FTP = ({ size = 24, className }: IconProps) => <Mono size={size} className={className} color="#6366f1" text="FTP" />;
const GSheet = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Google Sheets" src={LOCAL('gsheet')} />;

// ─── APIs & Integration ───
const RestAPI = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <defs>
      <linearGradient id="rest-globe-grad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stopColor="#22D3EE" />
        <stop offset="100%" stopColor="#2563EB" />
      </linearGradient>
    </defs>
    <g fill="none" stroke="url(#rest-globe-grad)" strokeWidth="1.4" strokeLinecap="round">
      <circle cx="12" cy="12" r="9.5" />
      <ellipse cx="12" cy="12" rx="9.5" ry="4.2" />
      <ellipse cx="12" cy="12" rx="4.2" ry="9.5" />
      <line x1="2.5" y1="12" x2="21.5" y2="12" />
      <line x1="12" y1="2.5" x2="12" y2="21.5" />
    </g>
  </svg>
);
const GraphQL = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="GraphQL" src={LOCAL('graphql')} />;
const OData = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <rect width="24" height="24" rx="3.5" fill="#F5A623" />
    <rect x="3.5" y="5" width="6" height="1.8" rx="0.4" fill="#fff" />
    <rect x="11" y="5" width="9.5" height="1.8" rx="0.4" fill="#fff" />
    <rect x="3.5" y="9" width="6" height="1.8" rx="0.4" fill="#fff" />
    <rect x="11" y="9" width="9.5" height="1.8" rx="0.4" fill="#fff" />
    <rect x="3.5" y="13" width="6" height="1.8" rx="0.4" fill="#fff" />
    <rect x="11" y="13" width="9.5" height="1.8" rx="0.4" fill="#fff" />
    <circle cx="6.5" cy="19" r="2" fill="#fff" />
    <rect x="11" y="18.1" width="9.5" height="1.8" rx="0.4" fill="#fff" />
  </svg>
);
const OracleAPI = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Oracle API" src={LOCAL('oracle')} />;

// ─── Streaming ───
const Kafka = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Kafka" src={LOCAL('kafka')} />;
const RabbitMQ = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="RabbitMQ" src={LOCAL('rabbitmq')} />;
const Pulsar = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Apache Pulsar" src={LOCAL('pulsar')} />;
const EventHub = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Azure Event Hubs" src={LOCAL('eventhub')} />;
const Kinesis = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="AWS Kinesis" src={LOCAL('kinesis')} />;

// ─── SaaS ───
const Salesforce = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Salesforce" src={LOCAL('salesforce')} />;
const Dynamics365 = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <defs>
      <linearGradient id="d365-outer" x1="0" y1="0" x2="0.7" y2="1">
        <stop offset="0%" stopColor="#1A4FCC" />
        <stop offset="100%" stopColor="#7B83EB" />
      </linearGradient>
      <linearGradient id="d365-inner" x1="0" y1="0" x2="0.6" y2="1">
        <stop offset="0%" stopColor="#C7C4F4" />
        <stop offset="100%" stopColor="#8E91E6" />
      </linearGradient>
    </defs>
    <path d="M4 2 H10.5 a7.5 7.5 0 0 1 7.5 7.5 v5 a7.5 7.5 0 0 1 -7.5 7.5 H4 Z" fill="url(#d365-outer)" />
    <path d="M8 7 H11 a4 4 0 0 1 4 4 v2 a4 4 0 0 1 -4 4 H8 Z" fill="url(#d365-inner)" opacity="0.85" />
  </svg>
);
const SAP = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="SAP" src={LOCAL('sap')} />;
const ServiceNow = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <path d="M12.07 1.275C5.407 1.275 0 6.65 0 13.285c0 3.316 1.351 6.51 3.778 8.784.86.798 2.15.89 3.102.153a8.57 8.57 0 0 1 10.259 0c.952.707 2.272.645 3.102-.184 4.822-4.577 5.037-12.194.46-17.047-2.272-2.334-5.375-3.685-8.63-3.716m-.062 18.06c-3.225.092-5.897-2.457-5.989-5.682v-.307a5.977 5.977 0 0 1 5.99-5.99 5.977 5.977 0 0 1 5.989 5.99c.092 3.225-2.458 5.897-5.683 5.989h-.307z" fill="#62D84E" />
  </svg>
);
const Jira = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Jira" src={LOCAL('jira')} />;
const Workday = ({ size = 24, className }: IconProps) => <Mono size={size} className={className} color="#F38B00" text="W" />;
const HubSpot = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <g stroke="#FF7A59" strokeLinecap="round" fill="#FF7A59">
      <line x1="4" y1="4.5" x2="11" y2="10.5" strokeWidth="1.8" />
      <line x1="16" y1="3" x2="15.5" y2="9" strokeWidth="1.8" />
      <line x1="5" y1="20.5" x2="11.5" y2="17" strokeWidth="1.8" />
      <circle cx="4" cy="4.5" r="2" />
      <circle cx="16" cy="3" r="2" />
      <circle cx="5" cy="20.5" r="2" />
      <circle cx="15.5" cy="14" r="5.5" fill="none" strokeWidth="2.4" />
      <circle cx="15.5" cy="14" r="1.5" />
    </g>
  </svg>
);
const Zendesk = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Zendesk" src={LOCAL('zendesk')} />;
const NetSuite = ({ size = 24, className }: IconProps) => <Mono size={size} className={className} color="#125580" text="NS" />;
const GitHub = ({ size = 24, className }: IconProps) => <Mono size={size} className={className} color="#181717" text="GH" />;
const Shopify = ({ size = 24, className }: IconProps) => <Mono size={size} className={className} color="#95BF47" text="S" />;
const Stripe = ({ size = 24, className }: IconProps) => <Mono size={size} className={className} color="#635BFF" text="S" />;
const Notion = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <rect x="3" y="3" width="18" height="18" rx="2" fill="#fff" stroke="#111" strokeWidth="1.6" />
    <path d="M7.4 7.2h3.2l4.3 6.5V8.9l-1.4-.2V7.2h3.9v1.5l-1.2.2v8h-1.9L9.1 9.3v5.8l1.5.3v1.5H6.5v-1.5l1.3-.3V8.9l-1.3-.2V7.2z" fill="#111" />
  </svg>
);
const Asana = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <circle cx="12" cy="7" r="3.4" fill="#F06A6A" />
    <circle cx="8" cy="15.2" r="3.4" fill="#FFB84D" />
    <circle cx="16" cy="15.2" r="3.4" fill="#8B5CF6" />
  </svg>
);

// ─── Notifications ───
const SMTP = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <rect x="2" y="5" width="20" height="14" rx="2" fill="#4A90D9" />
    <path d="M2 7l10 7 10-7" fill="none" stroke="#fff" strokeWidth="1.6" strokeLinejoin="round" />
  </svg>
);
const SendGrid = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <rect x="9" y="2" width="6.5" height="6.5" fill="#00B2E3" />
    <rect x="15.5" y="2" width="6.5" height="6.5" fill="#1A82E2" />
    <rect x="2" y="8.5" width="6.5" height="6.5" fill="#9DDDF1" />
    <rect x="9" y="8.5" width="6.5" height="6.5" fill="#0096D6" />
    <rect x="15.5" y="8.5" width="6.5" height="6.5" fill="#00B2E3" />
    <rect x="2" y="15" width="6.5" height="6.5" fill="#1A82E2" />
    <rect x="9" y="15" width="6.5" height="6.5" fill="#9DDDF1" />
  </svg>
);
const Slack = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Slack" src={LOCAL('slack')} />;
const Twilio = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Twilio" src={LOCAL('twilio')} />;

// ─── Observability ───
const Datadog = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Datadog" src={LOCAL('datadog')} />;
const PagerDuty = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="PagerDuty" src={LOCAL('pagerduty')} />;
const Splunk = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Splunk" src={LOCAL('splunk')} />;

// ─── Vector / AI ───
const Pinecone = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <path d="M12 1.5 C 16 1.5, 18.5 4.5, 18.5 9 C 18.5 14, 17 19, 12 22.5 C 7 19, 5.5 14, 5.5 9 C 5.5 4.5, 8 1.5, 12 1.5 Z" fill="#7B3F1D" />
    <g fill="none" stroke="#3D1F0F" strokeWidth="0.7" strokeLinecap="round" opacity="0.85">
      <path d="M8 5 Q10 7 12 5 Q14 7 16 5" />
      <path d="M7 8 Q9.5 10.5 12 8 Q14.5 10.5 17 8" />
      <path d="M6 11 Q9 13.5 12 11 Q15 13.5 18 11" />
      <path d="M6.5 14 Q9 16.5 12 14 Q15 16.5 17.5 14" />
      <path d="M7.5 17 Q10 18.8 12 17 Q14 18.8 16.5 17" />
      <path d="M9 19.5 Q10.5 20.6 12 19.5 Q13.5 20.6 15 19.5" />
    </g>
  </svg>
);
const Weaviate = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <path d="M12 2l9 5.2v9.6L12 22l-9-5.2V7.2L12 2z" fill="#01CC87" />
    <path d="M12 6l5 3v6l-5 3-5-3V9l5-3z" fill="#fff" opacity="0.9" />
    <circle cx="12" cy="12" r="2" fill="#01CC87" />
  </svg>
);
const Qdrant = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <g fill="#DC244C">
      <path fillRule="evenodd" clipRule="evenodd" d="M12 1.5 L21.5 6.75 L21.5 17.25 L12 22.5 L2.5 17.25 L2.5 6.75 Z M12 4.5 L19 8.4 L19 15.6 L12 19.5 L5 15.6 L5 8.4 Z" />
      <rect x="8.5" y="9" width="7" height="7" />
      <path d="M15.5 15.5 L20 22 L22 20.5 L17.5 14 Z" />
    </g>
  </svg>
);
const Chroma = ({ size = 24, className }: IconProps) => <CdnImg size={size} className={className} alt="Chroma" src={LOCAL('chroma')} />;
const PgVector = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <rect width="24" height="24" rx="4" fill="#336791" />
    <path d="M5 18l4-8 3 5 3-3 4 6" fill="none" stroke="#fff" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    <circle cx="9" cy="10" r="1.4" fill="#fff" />
    <circle cx="12" cy="15" r="1.4" fill="#fff" />
    <circle cx="15" cy="12" r="1.4" fill="#fff" />
    <circle cx="19" cy="18" r="1.4" fill="#fff" />
  </svg>
);

// ─── Microsoft Graph (Z11, 2026-05-23) ───
// Four-square Microsoft brand mark — same shape Microsoft itself uses for
// the Graph SDK, Office, Azure portal app launcher etc. Self-contained
// inline SVG so we don't add a CDN dependency.
const MicrosoftGraph = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <rect width="24" height="24" rx="4" fill="#ffffff" />
    <rect x="3" y="3" width="8.5" height="8.5" fill="#F25022" />
    <rect x="12.5" y="3" width="8.5" height="8.5" fill="#7FBA00" />
    <rect x="3" y="12.5" width="8.5" height="8.5" fill="#00A4EF" />
    <rect x="12.5" y="12.5" width="8.5" height="8.5" fill="#FFB900" />
  </svg>
);

// ─── Custom ───
const Custom = ({ size = 24, className }: IconProps) => (
  <svg xmlns="http://www.w3.org/2000/svg" width={size} height={size} viewBox="0 0 24 24" className={className} aria-hidden="true">
    <rect width="24" height="24" rx="4" fill="#94a3b8" />
    <path d="M12 8a4 4 0 100 8 4 4 0 000-8zm0 6a2 2 0 110-4 2 2 0 010 4zm7-1.5l1.8-1-1.5-2.6-2 .6c-.3-.3-.6-.5-1-.7l-.3-2H13l-.3 2c-.4.2-.7.4-1 .7l-2-.6-1.5 2.6 1.8 1c-.1.3-.1.7-.1 1s0 .7.1 1l-1.8 1 1.5 2.6 2-.6c.3.3.6.5 1 .7l.3 2h3l.3-2c.4-.2.7-.4 1-.7l2 .6 1.5-2.6-1.8-1c.1-.3.1-.7.1-1s0-.7-.1-1z" fill="#fff" opacity="0.95" />
  </svg>
);

const ICON_MAP: Record<string, React.FC<IconProps>> = {
  postgresql: PostgreSQL,
  mysql: MySQL,
  mssql: SQLServer,
  oracle: OracleDB,
  sqlite: SQLite,
  mariadb: MariaDB,
  cockroachdb: CockroachDB,
  db2: Db2,
  sap_hana: SAP,
  teradata: Teradata,
  mongodb: MongoDB,
  cassandra: Cassandra,
  couchbase: Couchbase,
  dynamodb: DynamoDB,
  cosmosdb: CosmosDB,
  neo4j: Neo4j,
  firebase: Firebase,
  snowflake: Snowflake,
  bigquery: BigQuery,
  redshift: Redshift,
  databricks: Databricks,
  synapse: Synapse,
  clickhouse: ClickHouse,
  trino: Trino,
  presto: Trino,
  athena: Athena,
  elasticsearch: Elasticsearch,
  opensearch: OpenSearch,
  redis: Redis,
  s3: S3,
  azure_blob: AzureBlob,
  adls_gen2: ADLSGen2,
  gcs: GCS,
  minio: MinIO,
  sharepoint: SharePoint,
  onedrive: OneDrive,
  gdrive: GDrive,
  dropbox: Dropbox,
  ftp: FTP,
  gsheet: GSheet,
  rest_api: RestAPI,
  graphql: GraphQL,
  odata: OData,
  // Z11 (2026-05-23) — Microsoft Graph as a first-class connector. The
  // dedicated `microsoft_graph_source` node is hidden from the palette;
  // users reach Graph via Generic Source picking this connector type.
  microsoft_graph: MicrosoftGraph,
  oracle_api: OracleAPI,
  // 2026-05-23 (U1/U2): Oracle product families reuse the Oracle API icon
  // until a Fusion-specific glyph ships.
  oracle_fusion: OracleAPI,
  oracle_bip: OracleAPI,
  kafka: Kafka,
  rabbitmq: RabbitMQ,
  pulsar: Pulsar,
  eventhub: EventHub,
  kinesis: Kinesis,
  salesforce: Salesforce,
  dynamics365: Dynamics365,
  sap: SAP,
  // 2026-05-23 (V1/V2): SAP product families reuse the SAP icon.
  sap_s4hana: SAP,
  sap_successfactors: SAP,
  servicenow: ServiceNow,
  jira: Jira,
  workday: Workday,
  hubspot: HubSpot,
  zendesk: Zendesk,
  netsuite: NetSuite,
  github: GitHub,
  shopify: Shopify,
  stripe: Stripe,
  notion: Notion,
  asana: Asana,
  smtp: SMTP,
  sendgrid: SendGrid,
  slack: Slack,
  twilio: Twilio,
  datadog: Datadog,
  pagerduty: PagerDuty,
  splunk: Splunk,
  pinecone: Pinecone,
  weaviate: Weaviate,
  qdrant: Qdrant,
  chroma: Chroma,
  pgvector: PgVector,
  custom: Custom,
};

export function ConnectorIcon({
  type,
  size = 24,
  className,
}: {
  type: string;
  size?: number;
  className?: string;
}) {
  const Icon = ICON_MAP[type] || Custom;
  const inner = Math.max(8, Math.round(size * 0.78));
  return (
    <span
      className={className}
      style={{
        width: size,
        height: size,
        background: '#ffffff',
        borderRadius: Math.max(4, Math.round(size * 0.18)),
        boxSizing: 'border-box',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        boxShadow: '0 1px 2px rgba(15, 23, 42, 0.08), inset 0 0 0 1px rgba(15, 23, 42, 0.06)',
        flexShrink: 0,
      }}
      aria-hidden="true"
    >
      <Icon size={inner} />
    </span>
  );
}

export default ConnectorIcon;
