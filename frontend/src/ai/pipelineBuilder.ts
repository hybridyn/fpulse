/**
 * Client-side AI Pipeline Builder
 * Parses natural language intent and generates pipeline nodes + connections.
 * Works without backend AI — this is the local rule-based fallback.
 */
import { migrateLegacySteps } from '../utils/migrateLegacyNodes';

interface PipelineStep {
  id: string;
  type: string;
  label: string;
  params: Record<string, any>;
}

interface PipelineConnection {
  from_step: string;
  to_step: string;
}

interface GeneratedPipeline {
  name: string;
  steps: PipelineStep[];
  connections: PipelineConnection[];
  reply: string;
}

const uid = () => Math.random().toString(36).slice(2, 10);

// ── Source detection ──
const SOURCE_PATTERNS: Array<{
  regex: RegExp;
  type: string;
  label: string;
  extract: (m: RegExpMatchArray) => Record<string, any>;
}> = [
  {
    regex: /(?:load|read|import|ingest|open)\s+(\S+\.csv)/i,
    type: 'file_source', label: 'File Source',
    extract: (m) => ({ file_path: m[1], operation: 'read_file' }),
  },
  {
    regex: /(?:load|read|import|ingest|open)\s+(\S+\.json)/i,
    type: 'json_source', label: 'JSON Source',
    extract: (m) => ({ file_path: m[1] }),
  },
  {
    regex: /(?:load|read|import|ingest|open)\s+(\S+\.parquet)/i,
    type: 'parquet_source', label: 'Parquet Source',
    extract: (m) => ({ file_path: m[1] }),
  },
  {
    regex: /(?:load|read|import|ingest|open)\s+(\S+\.xlsx?)/i,
    type: 'excel_source', label: 'Excel Source',
    extract: (m) => ({ file_path: m[1] }),
  },
  {
    regex: /(?:load|read|import|ingest|open)\s+(\S+\.xml)/i,
    type: 'xml_source', label: 'XML Source',
    extract: (m) => ({ file_path: m[1] }),
  },
  {
    regex: /(?:from|connect(?:.*?)to|query)\s+(?:database|db|postgres|mysql|sql)/i,
    type: 'db_source', label: 'Database Source',
    extract: () => ({ connection: '', query: 'SELECT * FROM table_name' }),
  },
  {
    regex: /(?:from|call|fetch)\s+(?:api|rest|endpoint|http)/i,
    type: 'api_source', label: 'API Source',
    extract: () => ({ url: 'https://api.example.com/data', method: 'GET' }),
  },
  {
    regex: /(?:from|read)\s+(?:s3|minio|bucket)/i,
    type: 's3_source', label: 'S3 Source',
    extract: () => ({ bucket: 'my-bucket', key: 'data/' }),
  },
  {
    regex: /(?:from|consume|subscribe)\s+(?:kafka|topic|stream)/i,
    type: 'kafka_source', label: 'Kafka Source',
    extract: () => ({ broker: 'localhost:9092', topic: 'my-topic' }),
  },
  {
    regex: /(?:from|read)\s+(?:ftp|sftp)/i,
    type: 'ftp_source', label: 'FTP Source',
    extract: () => ({ host: '', path: '/' }),
  },
  {
    regex: /(?:from|read|import)\s+(?:google\s*sheet|gsheet|spreadsheet)/i,
    type: 'gsheet_source', label: 'Google Sheets',
    extract: () => ({ spreadsheet_id: '', sheet_name: 'Sheet1' }),
  },
  {
    regex: /(?:from|read)\s+(?:delta|delta\s*lake)/i,
    type: 'delta_source', label: 'Delta Lake Source',
    extract: () => ({ path: '/data/delta_table' }),
  },
  // Generic file detection — uses universal File Source
  {
    regex: /(?:load|read|import|ingest|open)\s+(\S+\.\w+)/i,
    type: 'file_source', label: 'File Source',
    extract: (m) => ({ file_path: m[1], operation: 'read_file' }),
  },
];

// ── Transform detection ──
const TRANSFORM_PATTERNS: Array<{
  regex: RegExp;
  type: string;
  label: string;
  extract: (m: RegExpMatchArray, text: string) => Record<string, any>;
}> = [
  {
    regex: /filter\s+(?:where\s+|by\s+)?(.+?)(?:,|\band\b|\bthen\b|$)/i,
    type: 'filter', label: 'Filter',
    extract: (m) => {
      const cond = m[1].trim().replace(/\s*(dedup|join|aggregate|sort|output|save|write).*$/i, '').trim();
      return { condition: cond };
    },
  },
  {
    regex: /(?:transform|sql|query|compute|calculate)(?:\s+(.+?))?(?:,|then|$)/i,
    type: 'transform', label: 'Transform',
    extract: (m) => ({ expression: m[1] ? `SELECT *, ${m[1].trim()} FROM source_table` : 'SELECT * FROM source_table' }),
  },
  {
    regex: /dedup(?:licate)?\s+(?:by|on)\s+(\w+)/i,
    type: 'deduplicate', label: 'Deduplicate',
    extract: (m) => ({ key: [m[1].trim()] }),
  },
  {
    regex: /sort\s+(?:by\s+)?(\S+)\s*(asc|desc)?/i,
    type: 'sort', label: 'Sort',
    extract: (m) => ({ columns: [m[1]], order: m[2] || 'asc' }),
  },
  {
    regex: /rename\s+(\S+)\s+(?:to|as)\s+(\S+)/i,
    type: 'rename', label: 'Rename',
    extract: (m) => ({ mappings: { [m[1]]: m[2] } }),
  },
  {
    regex: /(?:add|create|derive|new)\s+(?:column|field)\s+(\S+)/i,
    type: 'derived_column', label: 'Derived Column',
    extract: (m) => ({ columns: [{ name: m[1], expression: '' }] }),
  },
  {
    regex: /(?:cast|convert|typecast)\s+(\S+)\s+(?:to|as)\s+(\S+)/i,
    type: 'typecast', label: 'Typecast',
    extract: (m) => ({ casts: { [m[1]]: m[2] } }),
  },
  {
    regex: /(?:daily|monthly|weekly|yearly|hourly)\s+(revenue|sales|count|total|sum|avg|average)/i,
    type: 'aggregate', label: 'Aggregate',
    extract: (m) => ({
      group_by: ['date'],
      aggregations: [{ column: m[2] || 'amount', function: 'SUM', alias: `${m[1]}_${m[2] || 'total'}` }],
    }),
  },
  {
    regex: /aggregate\s+(?:by\s+)?(\S+)/i,
    type: 'aggregate', label: 'Aggregate',
    extract: (m) => ({ group_by: [m[1]], aggregations: [] }),
  },
  {
    regex: /group\s+by\s+(\S+)/i,
    type: 'aggregate', label: 'Aggregate',
    extract: (m) => ({ group_by: [m[1]], aggregations: [] }),
  },
  {
    regex: /sample\s+(\d+)/i,
    type: 'sample', label: 'Sample',
    extract: (m) => ({ count: parseInt(m[1]) }),
  },
  {
    regex: /(?:clean|validate|check)\s+(?:data|quality)/i,
    type: 'validate', label: 'Validate',
    extract: () => ({ rules: [{ column: '*', rule: 'not_null' }] }),
  },
  {
    regex: /validate\s+(\S+)/i,
    type: 'validate', label: 'Validate',
    extract: (m) => ({ rules: [{ column: m[1], rule: 'not_null' }] }),
  },
  {
    regex: /^(?:validate|clean|check)$/i,
    type: 'validate', label: 'Validate',
    extract: () => ({ rules: [{ column: '*', rule: 'not_null' }] }),
  },
  {
    regex: /pivot\s+(?:on\s+)?(\S+)/i,
    type: 'pivot', label: 'Pivot',
    extract: (m) => ({ pivot_column: m[1] }),
  },
  {
    regex: /window\s+(\S+)/i,
    type: 'window', label: 'Window',
    extract: (m) => ({ function: m[1].toUpperCase() }),
  },
  {
    regex: /(?:split|branch|route)\s+(?:by|on)\s+(.+?)(?:,|$)/i,
    type: 'conditional_split', label: 'Conditional Split',
    extract: (m) => ({ branches: [{ condition: m[1].trim(), label: 'Branch 1' }] }),
  },
];

// ── Join detection ──
const JOIN_PATTERNS: Array<{
  regex: RegExp;
  type: string;
  label: string;
  extract: (m: RegExpMatchArray) => Record<string, any>;
  needsSecondSource: boolean;
}> = [
  // Left/right/full join MUST come before generic join to match correctly
  {
    regex: /left\s+join\s+(?:with\s+)?(\S+?)(?:\s+on\s+(\S+))?(?:,|$)/i,
    type: 'join', label: 'Left Join',
    extract: (m) => ({ join_type: 'LEFT', join_key: m[2] || 'id', right_source: m[1] }),
    needsSecondSource: true,
  },
  {
    regex: /(?:inner\s+)?join\s+(?:with\s+)?(\S+?)(?:\s+on\s+(\S+))?(?:,|$)/i,
    type: 'join', label: 'Join',
    extract: (m) => ({ join_type: 'INNER', join_key: m[2] || 'id', right_source: m[1] }),
    needsSecondSource: true,
  },
  {
    regex: /lookup\s+(?:from\s+)?(\S+?)(?:\s+on\s+(\S+))?(?:,|$)/i,
    type: 'lookup', label: 'Lookup',
    extract: (m) => ({ lookup_key: m[2] || 'id', lookup_source: m[1] }),
    needsSecondSource: true,
  },
  {
    regex: /union\s+(?:with\s+)?(\S+)/i,
    type: 'union', label: 'Union',
    extract: (m) => ({ union_type: 'all', right_source: m[1] }),
    needsSecondSource: true,
  },
  {
    regex: /merge\s+(?:with\s+)?(\S+?)(?:\s+on\s+(\S+))?(?:,|$)/i,
    type: 'join', label: 'Merge',
    extract: (m) => ({ join_type: 'INNER', join_key: m[2] || 'id', right_source: m[1] }),
    needsSecondSource: true,
  },
];

// ── Destination detection ──
const DEST_PATTERNS: Array<{
  regex: RegExp;
  type: string;
  label: string;
  extract: (m: RegExpMatchArray) => Record<string, any>;
}> = [
  {
    regex: /(?:output|save|write|export|store)\s+(?:to\s+|as\s+)?(?:parquet|\.parquet)/i,
    type: 'file_sink', label: 'Parquet Output',
    extract: () => ({ file_path: 'output/result.parquet' }),
  },
  {
    regex: /(?:output|save|write|export)\s+(?:to\s+|as\s+)?(\S+\.csv)/i,
    type: 'csv_sink', label: 'CSV Output',
    extract: (m) => ({ file_path: m[1] }),
  },
  {
    regex: /(?:output|save|write|export)\s+(?:to\s+|as\s+)?csv/i,
    type: 'csv_sink', label: 'CSV Output',
    extract: () => ({ file_path: 'output.csv' }),
  },
  {
    regex: /(?:output|save|write|export)\s+(?:to\s+|as\s+)?(\S+\.json)/i,
    type: 'json_sink', label: 'JSON Output',
    extract: (m) => ({ file_path: m[1] }),
  },
  {
    regex: /(?:output|save|write|export)\s+(?:to\s+|as\s+)?json/i,
    type: 'json_sink', label: 'JSON Output',
    extract: () => ({ file_path: 'output.json' }),
  },
  {
    regex: /(?:output|save|write|export)\s+(?:to\s+|as\s+)?(\S+\.xlsx?)/i,
    type: 'excel_sink', label: 'Excel Output',
    extract: (m) => ({ file_path: m[1] }),
  },
  {
    regex: /(?:output|save|write|export|insert)\s+(?:to|into)\s+(?:database|db|postgres|mysql|table)/i,
    type: 'db_sink', label: 'Database Output',
    extract: () => ({ table_name: 'output_table', mode: 'append' }),
  },
  {
    regex: /(?:output|save|write|upload)\s+(?:to\s+)?(?:s3|minio|bucket)/i,
    type: 's3_sink', label: 'S3 Output',
    extract: () => ({ bucket: 'output-bucket', key: 'data/' }),
  },
  {
    regex: /(?:publish|send|produce)\s+(?:to\s+)?(?:kafka|topic)/i,
    type: 'kafka_sink', label: 'Kafka Output',
    extract: () => ({ broker: 'localhost:9092', topic: 'output-topic' }),
  },
  {
    regex: /(?:send|post)\s+(?:to\s+)?(?:api|webhook|endpoint)/i,
    type: 'webhook_sink', label: 'Webhook Output',
    extract: () => ({ url: 'https://api.example.com/webhook' }),
  },
  {
    regex: /(?:send|email|mail)\s+(?:to\s+)?(?:email)/i,
    type: 'email_sink', label: 'Email Output',
    extract: () => ({ to: '', subject: 'Pipeline Results' }),
  },
  {
    regex: /(?:output|save|write)\s+(?:to\s+)?(?:delta|delta\s*lake)/i,
    type: 'delta_sink', label: 'Delta Lake Output',
    extract: () => ({ path: '/output/delta_table' }),
  },
  {
    regex: /(?:output|save|write|load)\s+(?:to|into)\s+(?:warehouse|snowflake|bigquery|redshift)/i,
    type: 'warehouse_sink', label: 'Warehouse Output',
    extract: () => ({ table_name: 'output_table' }),
  },
];

function detectSecondSource(text: string, rightSource: string): PipelineStep | null {
  // Try to detect what the second source is
  const ext = rightSource.split('.').pop()?.toLowerCase();
  const id = uid();
  const fileName = rightSource.split('/').pop() || rightSource;
  if (ext === 'csv') return { id, type: 'file_source', label: `Read ${fileName}`, params: { file_path: rightSource, operation: 'read_file' } };
  if (ext === 'json') return { id, type: 'json_source', label: `Read ${fileName}`, params: { file_path: rightSource } };
  if (ext === 'parquet') return { id, type: 'parquet_source', label: `Read ${fileName}`, params: { file_path: rightSource } };
  if (ext === 'xlsx' || ext === 'xls') return { id, type: 'excel_source', label: `Read ${fileName}`, params: { file_path: rightSource } };
  // Default: treat as generic file
  if (rightSource && !rightSource.includes(' ')) {
    return { id, type: 'file_source', label: `Source: ${rightSource}`, params: { file_path: rightSource.includes('.') ? rightSource : `${rightSource}.csv`, operation: 'read_file' } };
  }
  return null;
}

export function parsePipelineIntent(text: string): GeneratedPipeline | null {
  const steps: PipelineStep[] = [];
  const connections: PipelineConnection[] = [];
  let lastMainId: string | null = null;

  // Split text into clauses by comma, "then", "and then"
  const clauses = text.split(/\s*,\s*|\s+then\s+|\s+and\s+then\s+/i).map((c) => c.trim()).filter(Boolean);

  // All patterns merged with category tags
  type PatternDef = { regex: RegExp; type: string; label: string; category: string; extract: (m: RegExpMatchArray) => Record<string, any>; needsSecondSource?: boolean };
  const ALL_PATTERNS: PatternDef[] = [
    ...SOURCE_PATTERNS.map((p) => ({ ...p, category: 'source', extract: p.extract as (m: RegExpMatchArray) => Record<string, any> })),
    ...TRANSFORM_PATTERNS.map((p) => ({ ...p, category: 'transform', extract: (m: RegExpMatchArray) => p.extract(m, text) })),
    ...JOIN_PATTERNS.map((p) => ({ ...p, category: 'join', extract: p.extract as (m: RegExpMatchArray) => Record<string, any> })),
    ...DEST_PATTERNS.map((p) => ({ ...p, category: 'dest', extract: p.extract as (m: RegExpMatchArray) => Record<string, any> })),
  ];

  for (const clause of clauses) {
    let matched = false;

    for (const pat of ALL_PATTERNS) {
      const m = clause.match(pat.regex);
      if (!m) continue;

      // Skip if we already have this exact type (no duplicate filter/dedup etc)
      if (steps.some((s) => s.type === pat.type)) continue;

      const id = uid();
      const params = pat.extract(m);

      if (pat.category === 'join' && pat.needsSecondSource && params.right_source) {
        // Create second source for the join
        const secondSource = detectSecondSource(text, params.right_source);
        if (secondSource) {
          steps.push(secondSource);
          // Connect current chain to join
          if (lastMainId) connections.push({ from_step: lastMainId, to_step: id });
          // Connect second source to join
          connections.push({ from_step: secondSource.id, to_step: id });
          lastMainId = null; // prevent double-connect below
        }
        delete params.right_source;
        delete params.lookup_source;
      }

      // Improve label for source nodes
      let label = pat.label;
      if (pat.category === 'source' && params.file_path) {
        const fileName = params.file_path.split('/').pop() || params.file_path;
        label = `Read ${fileName}`;
      }

      steps.push({ id, type: pat.type, label, params });

      if (lastMainId) {
        connections.push({ from_step: lastMainId, to_step: id });
      }
      lastMainId = id;
      matched = true;
      break;
    }

    // If no pattern matched this clause, skip it
  }

  // Default output if none detected and we have at least a source + 1 step
  const hasOutput = steps.some((s) => s.type.includes('sink') || s.type === 'output');
  if (!hasOutput && steps.length > 1) {
    const id = uid();
    steps.push({ id, type: 'file_sink', label: 'Parquet Output', params: { file_path: 'output/result.parquet' } });
    if (lastMainId) {
      connections.push({ from_step: lastMainId, to_step: id });
    }
  }

  if (steps.length < 2) return null;

  // Improve labels with param context
  for (const step of steps) {
    if (step.type === 'filter' && step.params.condition) {
      step.label = `Filter: ${step.params.condition.substring(0, 30)}`;
    } else if (step.type === 'deduplicate' && step.params.key?.length) {
      step.label = `Dedup by ${step.params.key.join(', ')}`;
    } else if (step.type === 'aggregate' && step.params.group_by?.length) {
      step.label = `Aggregate by ${step.params.group_by.join(', ')}`;
    } else if (step.type === 'sort' && step.params.columns?.length) {
      step.label = `Sort by ${step.params.columns.join(', ')}`;
    } else if (step.type === 'join') {
      step.label = `${step.params.join_type || 'Inner'} Join`;
    } else if (step.type === 'output') {
      const fmt = (step.params.format || 'Parquet');
      step.label = `${fmt.charAt(0).toUpperCase() + fmt.slice(1)} Output`;
    }
  }

  // 2026-06-10 (node-audit fix): normalize generated step types to the
  // modern visible palette BEFORE handing the pipeline to the canvas.
  // The pattern tables above still speak legacy (csv_source, file_sink,
  // validate, …) — previously those landed on the canvas un-remapped,
  // bypassing the same migration the JSON-import path runs.
  //
  // 1. migrateLegacySteps — the shared frontend mirror of the backend
  //    migration table (sources/sinks collapse to generic source/
  //    destination with a connector_type).
  // 2. Local extras the shared table doesn't know:
  //    file_source → source (connector by file extension),
  //    validate → data_quality, conditional_split → switch_case.
  const migrated = migrateLegacySteps(steps).steps;
  for (const step of migrated) {
    if (step.type === 'file_source') {
      const ext = String(step.params?.file_path || '').split('.').pop()?.toLowerCase();
      const connector = ext === 'json' ? 'json' : ext === 'parquet' ? 'parquet'
        : (ext === 'xlsx' || ext === 'xls') ? 'excel' : ext === 'xml' ? 'xml' : 'csv';
      step.type = 'source';
      step.params = { connector_type: connector, ...(step.params || {}) };
      delete step.params.operation;
    } else if (step.type === 'file_sink') {
      step.type = 'destination';
      step.params = { connector_type: 'parquet', ...(step.params || {}) };
    } else if (step.type === 'validate') {
      step.type = 'data_quality';
      step.label = 'Data Quality';
    } else if (step.type === 'conditional_split') {
      step.type = 'switch_case';
      step.label = 'Switch';
    }
  }
  steps.length = 0;
  steps.push(...migrated);

  // Position nodes in a horizontal layout
  let joinIndex = steps.findIndex((s) => s.type === 'join' || s.type === 'lookup' || s.type === 'union');
  steps.forEach((step, i) => {
    // If there's a second source for a join, position it above
    const isSecondSource = step.type.includes('source') && i > 0;
    (step as any).position = { x: i * 300, y: isSecondSource ? 0 : 150 };
  });

  // Generate name from intent
  const name = generatePipelineName(text);

  // Generate reply
  const reply = generateReply(steps, connections, name);

  return { name, steps, connections, reply };
}

function generatePipelineName(text: string): string {
  const words = text.split(/\s+/).filter((w) => w.length > 3).slice(0, 3);
  if (words.length > 0) {
    return words.map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join(' ') + ' Pipeline';
  }
  return 'New Pipeline';
}

function generateReply(steps: PipelineStep[], connections: PipelineConnection[], name: string): string {
  let reply = `I've created a pipeline: **${name}**\n\n`;
  reply += `Steps: ${steps.map((s) => s.label).join(' → ')}\n\n`;
  reply += `The pipeline is on the canvas. Click any node to configure it, or click **Run All** to execute.`;
  return reply;
}

// ── Modification commands ──

export interface ModificationResult {
  action: 'add' | 'remove' | 'modify' | 'none';
  reply: string;
  nodeType?: string;
  nodeParams?: Record<string, any>;
  nodeId?: string;
  params?: Record<string, any>;
  afterNodeId?: string;
}

export function parseModification(text: string, existingNodes: Array<{ id: string; data: any }>): ModificationResult {
  const lower = text.toLowerCase().trim();

  // Build node name list for context-aware matching
  const nodeNames = existingNodes.map(n => ({
    id: n.id,
    label: (n.data.label as string).toLowerCase(),
    type: (n.data.stepType as string).toLowerCase(),
  }));

  // ── Add node patterns ──

  // Helper: check if a name is generic (activity/node/step) rather than a specific type
  const isGenericName = (name: string) => /^(activity|node|step|block|item|thing)?$/.test(name.trim());

  // "add X after Y" / "insert X after Y" / "add X before Y"
  const addAfterMatch = lower.match(/(?:add|insert|include|put)\s+(?:a\s+|an\s+)?(?:new\s+)?(\w[\w\s]*?)\s+(?:after|before|next to)\s+(?:the\s+)?(\w[\w\s]*?)(?:\s+(?:node|step|activity|block))?\s*$/);
  if (addAfterMatch) {
    const nodeName = addAfterMatch[1].replace(/\s*(node|step|activity|block)\s*/g, '').trim();
    const afterName = addAfterMatch[2].replace(/\s*(node|step|activity|block)\s*/g, '').trim();
    const afterNode = nodeNames.find(n => n.label.includes(afterName) || n.type.includes(afterName.replace(/\s+/g, '_')));

    // If user said a specific type (e.g. "add aggregate after filter")
    if (!isGenericName(nodeName)) {
      const type = findNodeType(nodeName);
      if (type && afterNode) {
        return {
          action: 'add',
          reply: `Added **${type.label}** after **${afterNode.label.replace(/\b\w/g, c => c.toUpperCase())}**. Click it to configure.`,
          nodeType: type.type,
          afterNodeId: afterNode.id,
        };
      }
      if (type) {
        return {
          action: 'add',
          reply: `Added **${type.label}** to the pipeline. Click it to configure.`,
          nodeType: type.type,
        };
      }
    }

    // Generic name (e.g. "add an activity after filter") — ask what type
    if (isGenericName(nodeName) || !findNodeType(nodeName)) {
      const suggestions = getSuggestedNodes(existingNodes, afterNode?.id);
      const suggestionList = suggestions.map(s => `• **${s}**`).join('\n');
      return {
        action: 'none',
        reply: `What type of activity would you like to add${afterNode ? ` after **${afterNode.label.replace(/\b\w/g, c => c.toUpperCase())}**` : ''}?\n\n${suggestionList}\n\nJust say: *"Add [type] after ${afterNode?.label || 'node'}"*`,
      };
    }
  }

  // "after X add Y" / "after filter, add aggregate"
  const afterFirstMatch = lower.match(/(?:after|before)\s+(?:the\s+)?(\w[\w\s]*?)\s*,?\s*(?:add|insert|include|put)\s+(?:a\s+|an\s+)?(?:new\s+)?(\w[\w\s]*?)(?:\s+(?:node|step|activity|block))?\s*$/);
  if (afterFirstMatch) {
    const afterName = afterFirstMatch[1].replace(/\s*(node|step|activity|block)\s*/g, '').trim();
    const nodeName = afterFirstMatch[2].replace(/\s*(node|step|activity|block)\s*/g, '').trim();
    const afterNode = nodeNames.find(n => n.label.includes(afterName) || n.type.includes(afterName.replace(/\s+/g, '_')));

    if (!isGenericName(nodeName)) {
      const type = findNodeType(nodeName);
      if (type) {
        return {
          action: 'add',
          reply: `Added **${type.label}**${afterNode ? ` after **${afterNode.label.replace(/\b\w/g, c => c.toUpperCase())}**` : ''}. Click it to configure.`,
          nodeType: type.type,
          afterNodeId: afterNode?.id,
        };
      }
    }

    // Generic — ask what type
    const suggestions = getSuggestedNodes(existingNodes, afterNode?.id);
    const suggestionList = suggestions.map(s => `• **${s}**`).join('\n');
    return {
      action: 'none',
      reply: `What type of activity would you like to add${afterNode ? ` after **${afterNode.label.replace(/\b\w/g, c => c.toUpperCase())}**` : ''}?\n\n${suggestionList}\n\nJust say: *"Add [type] after ${afterNode?.label || 'node'}"*`,
    };
  }

  // "add X" (simple) / "I need to add X" / "please add X"
  const addSimpleMatch = lower.match(/(?:add|insert|include|put)\s+(?:a\s+|an\s+)?(?:new\s+)?(\w[\w\s]*?)(?:\s+(?:node|step|activity|block))?\s*$/);
  if (addSimpleMatch) {
    const name = addSimpleMatch[1].replace(/\s*(node|step|activity|block)\s*/g, '').trim();
    if (!isGenericName(name)) {
      const type = findNodeType(name);
      if (type) {
        return {
          action: 'add',
          reply: `Added **${type.label}** to the pipeline. Click it to configure.`,
          nodeType: type.type,
        };
      }
    }
  }

  // Generic "add a node/activity/step" — no specific type
  if (/\b(add|insert|need|want)\b/i.test(lower) && /\b(activity|node|step)\b/i.test(lower)) {
    // Check if "after X" is mentioned
    const afterMatch = lower.match(/(?:after|before)\s+(?:the\s+)?(\w[\w\s]*?)(?:\s+(?:node|step|activity|block))?\s*$/);
    const afterName = afterMatch ? afterMatch[1].replace(/\s*(node|step|activity|block)\s*/g, '').trim() : null;
    const afterNode = afterName ? nodeNames.find(n => n.label.includes(afterName) || n.type.includes(afterName.replace(/\s+/g, '_'))) : null;

    const suggestions = getSuggestedNodes(existingNodes, afterNode?.id);
    const suggestionList = suggestions.map(s => `• **${s}**`).join('\n');

    return {
      action: 'none',
      reply: `What type of activity would you like to add${afterNode ? ` after **${afterNode.label.replace(/\b\w/g, c => c.toUpperCase())}**` : ''}? Here are some suggestions based on your pipeline:\n\n${suggestionList}\n\nJust say: *"Add filter"* or *"Add aggregate after ${afterNode?.label || 'Transform'}"*`,
    };
  }

  // ── Remove node ──
  const removeMatch = lower.match(/(?:remove|delete|drop)\s+(?:the\s+)?(\w[\w\s]*?)(?:\s+(?:node|step|block|activity))?\s*$/);
  if (removeMatch) {
    const name = removeMatch[1].replace(/\s*(node|step|activity|block)\s*/g, '').trim();
    const node = existingNodes.find(
      (n) =>
        (n.data.label as string).toLowerCase().includes(name) ||
        (n.data.stepType as string).toLowerCase().includes(name.replace(/\s+/g, '_')),
    );
    if (node) {
      return {
        action: 'remove',
        reply: `Removed **${node.data.label}** from the pipeline.`,
        nodeId: node.id,
      };
    }
  }

  // ── Change/modify node params ──
  const modifyMatch = lower.match(/(?:change|set|update|modify)\s+(?:the\s+)?(\w[\w\s]*?)\s+(?:to|=|:)\s+(.+)/);
  if (modifyMatch) {
    const field = modifyMatch[1].trim();
    const value = modifyMatch[2].trim();
    const selectedNode = existingNodes.find((n) => {
      const params = n.data.params || {};
      return Object.keys(params).some((k) => k.toLowerCase().includes(field.replace(/\s+/g, '_')));
    });
    if (selectedNode) {
      const paramKey = Object.keys(selectedNode.data.params || {}).find((k) => k.toLowerCase().includes(field.replace(/\s+/g, '_')));
      if (paramKey) {
        return {
          action: 'modify',
          reply: `Updated **${paramKey}** to \`${value}\` on ${selectedNode.data.label}.`,
          nodeId: selectedNode.id,
          params: { [paramKey]: value },
        };
      }
    }
  }

  return { action: 'none', reply: '' };
}

/** Suggest node types based on what's already on the pipeline */
function getSuggestedNodes(existingNodes: Array<{ id: string; data: any }>, afterNodeId?: string): string[] {
  const types = existingNodes.map(n => (n.data.stepType as string).toLowerCase());
  const suggestions: string[] = [];

  // If after a source → suggest transforms
  const afterType = afterNodeId ? existingNodes.find(n => n.id === afterNodeId)?.data.stepType?.toLowerCase() : null;
  if (afterType?.includes('source')) {
    suggestions.push('Filter', 'SQL Transform', 'Derived Column', 'Sort', 'Rename', 'Deduplicate');
  } else if (afterType === 'filter' || afterType === 'transform' || afterType === 'sort') {
    suggestions.push('Aggregate', 'Deduplicate', 'Sort', 'Rename', 'Validate', 'Join');
  }

  // General suggestions based on what's missing
  if (!types.includes('filter')) suggestions.push('Filter');
  if (!types.includes('aggregate')) suggestions.push('Aggregate');
  if (!types.includes('deduplicate')) suggestions.push('Deduplicate');
  if (!types.includes('sort')) suggestions.push('Sort');
  if (!types.includes('validate')) suggestions.push('Validate');
  if (!types.includes('join') && !types.includes('lookup')) suggestions.push('Join', 'Lookup');

  // Deduplicate and limit
  return [...new Set(suggestions)].slice(0, 6);
}

function findNodeType(name: string): { type: string; label: string } | null {
  const normalized = name.toLowerCase().replace(/\s+/g, '_');
  const types: Record<string, string> = {
    csv: 'csv_source', json: 'json_source', parquet: 'parquet_source',
    excel: 'excel_source', xml: 'xml_source', database: 'db_source', db: 'db_source',
    api: 'api_source', s3: 's3_source', kafka: 'kafka_source',
    ftp: 'ftp_source', gsheet: 'gsheet_source', google_sheets: 'gsheet_source',
    delta: 'delta_source', delta_lake: 'delta_source',
    filter: 'filter', transform: 'transform', deduplicate: 'deduplicate', dedup: 'deduplicate',
    sort: 'sort', rename: 'rename', typecast: 'typecast', derived_column: 'derived_column',
    aggregate: 'aggregate', join: 'join', lookup: 'lookup', union: 'union',
    pivot: 'pivot', unpivot: 'unpivot', window: 'window',
    sample: 'sample', validate: 'validate', conditional_split: 'conditional_split',
    output: 'output', csv_output: 'csv_sink', json_output: 'json_sink',
    excel_output: 'excel_sink', db_output: 'db_sink', s3_output: 's3_sink',
    kafka_output: 'kafka_sink', webhook: 'webhook_sink', email: 'email_sink',
    delta_output: 'delta_sink', warehouse: 'warehouse_sink',
  };

  for (const [key, type] of Object.entries(types)) {
    if (normalized.includes(key)) {
      const label = type.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
      return { type, label };
    }
  }
  return null;
}

/**
 * Detect if message is a pipeline creation request vs a modification vs a question
 */
export function classifyIntent(text: string): 'create' | 'modify' | 'question' | 'greeting' {
  const lower = text.toLowerCase().trim();

  // Greetings
  if (/^(hi|hello|hey|howdy|good\s+(morning|afternoon|evening))[\s!.,]*$/i.test(lower)) {
    return 'greeting';
  }

  // Polite-request creates — caught BEFORE the question check below so
  // phrases like "Can we build a pipeline that...", "Could you create
  // a workflow...", "Please build me a pipeline...", "I want a
  // pipeline that..." are recognised as CREATE intent even though
  // they start with a question word. Without this, anything starting
  // with "can/could/would" routes to the question handler and the
  // user gets "the canvas is empty" instead of an actual build.
  const buildVerb = '(build|create|make|generate|design|set\\s?up|setup|construct|put\\s+together)';
  const polite = new RegExp(
    `^(can|could|would|will)\\s+(you|we|i|someone)\\s+${buildVerb}\\b`,
    'i',
  );
  const pleasePolite = new RegExp(`^(please|kindly)\\s+${buildVerb}\\b`, 'i');
  const wantNeed = new RegExp(
    `^(i)\\s+(want|need|would\\s+like|wanna)\\s+(to\\s+)?(${buildVerb}\\s+)?(a\\s+)?(new\\s+)?(pipeline|workflow|etl)\\b`,
    'i',
  );
  const letsBuild = new RegExp(`^let'?s\\s+${buildVerb}\\b`, 'i');
  if (polite.test(lower) || pleasePolite.test(lower) || wantNeed.test(lower) || letsBuild.test(lower)) {
    return 'create';
  }

  // Questions
  if (/^(what|how|why|when|where|who|can|does|is|are|do|will|should)\b/i.test(lower)) {
    return 'question';
  }

  // Modification — starts with modify verbs
  if (/^(add|remove|delete|drop|change|set|update|modify|rename)\b/i.test(lower)) {
    return 'modify';
  }

  // Modification — "I need to add...", "I want to add...", "in the pipeline add...", "can you add...", "after X add Y"
  if (/\b(need to|want to|please|could you|can you)\s+(add|remove|delete|insert|put|include)\b/i.test(lower)) {
    return 'modify';
  }
  if (/\b(add|insert|put|include)\s+(?:a\s+)?(?:new\s+)?\w+\s+(?:after|before|between|to)\b/i.test(lower)) {
    return 'modify';
  }
  if (/\b(after|before)\s+(?:the\s+)?\w+\s+(?:add|insert|put|include)\b/i.test(lower)) {
    return 'modify';
  }

  // Creation — anything with pipeline-related verbs (only if it looks like a new pipeline request)
  if (/\b(load|read|import|ingest)\b/i.test(lower) && /\b(csv|json|parquet|excel|database|api|file)\b/i.test(lower)) {
    return 'create';
  }
  if (/^(create|build|make|generate|design|setup)\s+(a\s+)?(new\s+)?(pipeline|etl|workflow)\b/i.test(lower)) {
    return 'create';
  }

  // If it mentions pipeline verbs with specific source+output pattern, treat as creation
  if (/\b(load|read|import)\b/i.test(lower) && /\b(output|save|write|export|store)\b/i.test(lower)) {
    return 'create';
  }

  // Fallback: mentions of node types in a modifying context
  if (/\b(add|insert|include)\b/i.test(lower) && /\b(filter|transform|join|aggregate|deduplicate|sort|rename|validate|sample|lookup|union)\b/i.test(lower)) {
    return 'modify';
  }

  return 'question';
}
