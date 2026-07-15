import { useState, useCallback } from 'react';

const BASE = '/api';

async function aiRequest<T>(path: string, body: any): Promise<T> {
  // The embedded AI router (/api/ai/*) is currently stateless and
  // tenant-neutral — every request carries its own context inline.
  // We still attach the standard auth + workspace headers so that if
  // a future endpoint in this router ever needs to look up a saved
  // workflow or write an audit record, it already receives the
  // tenant boundary for free.
  const token = localStorage.getItem('fpulse_token') || '';
  const workspaceId = localStorage.getItem('fpulse_workspace_id') || 'default';
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Workspace-Id': workspaceId,
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

export interface AISuggestion {
  node_type: string;
  label: string;
  reason: string;
  params?: Record<string, any>;
  ai_powered?: boolean;
}

export interface AutoFillResult {
  params: Record<string, any>;
  explanation: string;
  fields_filled: string[];
  ai_powered?: boolean;
}

export interface DiagnoseResult {
  diagnosis: string;
  suggestion: string;
  severity: 'low' | 'medium' | 'high';
  auto_fix?: Record<string, any>;
  ai_powered?: boolean;
}

export interface GenerateSQLResult {
  sql: string;
  explanation: string;
  columns_used: string[];
}

export interface ProfileResult {
  summary: string;
  column_stats: Array<{
    name: string;
    type: string;
    null_pct: number;
    unique_count: number;
    suggestion?: string;
  }>;
  quality_score: number;
  recommendations: string[];
}

export interface OptimizeResult {
  suggestions: Array<{
    type: string;
    node_id?: string;
    description: string;
    impact: 'low' | 'medium' | 'high';
  }>;
  estimated_improvement: string;
}

/**
 * Hook providing embedded AI capabilities for the canvas builder.
 * Each method calls a corresponding /api/ai/* endpoint with graceful fallback.
 */
export function useEmbeddedAI() {
  const [loading, setLoading] = useState<string | null>(null);

  const suggestNextNode = useCallback(async (
    nodes: Array<{ id: string; type?: string; data: any }>,
    edges: Array<{ source: string; target: string }>,
    lastNodeId: string,
  ): Promise<AISuggestion | null> => {
    setLoading('suggest');
    try {
      const nodesSummary = nodes.map(n => ({
        id: n.id,
        type: n.data?.stepType,
        category: n.data?.category,
        label: n.data?.label,
      }));
      const result = await aiRequest<AISuggestion>('/ai/suggest-next', {
        nodes: nodesSummary,
        edges: edges.map(e => ({ source: e.source, target: e.target })),
        last_node_id: lastNodeId,
      });
      return result;
    } catch (err) {
      // Deterministic fallback: suggest based on last node category
      const lastNode = nodes.find(n => n.id === lastNodeId);
      const cat = lastNode?.data?.category;
      if (cat === 'source') return { node_type: 'filter', label: 'Filter', reason: 'Filter rows after reading data' };
      if (cat === 'transform' || cat === 'combine') return { node_type: 'file_sink', label: 'File Sink', reason: 'Save transformed results' };
      if (cat === 'flow') return { node_type: 'transform', label: 'Transform', reason: 'Add a transformation step' };
      return { node_type: 'transform', label: 'Transform', reason: 'Add next processing step' };
    } finally {
      setLoading(null);
    }
  }, []);

  const autoFillConfig = useCallback(async (
    nodeType: string,
    upstreamSchema?: Array<{ name: string; type: string }>,
    sampleData?: Record<string, any>[],
  ): Promise<AutoFillResult> => {
    setLoading('autofill');
    try {
      return await aiRequest<AutoFillResult>('/ai/auto-fill', {
        node_type: nodeType,
        upstream_schema: upstreamSchema,
        sample_data: sampleData?.slice(0, 5),
      });
    } catch {
      // Deterministic fallback based on node type
      const defaults: Record<string, AutoFillResult> = {
        csv_source: { params: { header: true, delimiter: ',', encoding: 'utf-8' }, explanation: 'Set standard CSV defaults', fields_filled: ['header', 'delimiter', 'encoding'] },
        filter: { params: { condition: upstreamSchema?.length ? `${upstreamSchema[0].name} IS NOT NULL` : '' }, explanation: 'Added basic null filter', fields_filled: ['condition'] },
        aggregate: { params: { group_by: upstreamSchema?.length ? [upstreamSchema[0].name] : [], aggregations: [] }, explanation: 'Grouped by first column', fields_filled: ['group_by'] },
        sort: { params: { column: upstreamSchema?.length ? upstreamSchema[0].name : '', order: 'asc' }, explanation: 'Sort by first column ascending', fields_filled: ['column', 'order'] },
        output: { params: { format: 'parquet', path: 'output/' }, explanation: 'Output as Parquet (best compression)', fields_filled: ['format', 'path'] },
        deduplicate: { params: { key: upstreamSchema?.length ? [upstreamSchema[0].name] : [], keep: 'first' }, explanation: 'Deduplicate by first column, keep first', fields_filled: ['key', 'keep'] },
        file_source: { params: { format: 'auto', header: true, encoding: 'utf-8' }, explanation: 'Auto-detect format, enable headers', fields_filled: ['format', 'header', 'encoding'] },
        file_sink: { params: { format: 'parquet', path: 'output/' }, explanation: 'Output as Parquet', fields_filled: ['format', 'path'] },
        sharepoint_source: { params: { format: 'auto', item_path: 'Shared Documents/' }, explanation: 'Auto-detect format, default Documents library', fields_filled: ['format', 'item_path'] },
        sharepoint_sink: { params: { format: 'csv', item_path: 'Shared Documents/output.csv' }, explanation: 'Write CSV to Documents library', fields_filled: ['format', 'item_path'] },
        onedrive_source: { params: { format: 'auto', item_path: 'Documents/' }, explanation: 'Auto-detect format from Documents folder', fields_filled: ['format', 'item_path'] },
        onedrive_sink: { params: { format: 'csv', item_path: 'Documents/output.csv' }, explanation: 'Write CSV to Documents', fields_filled: ['format', 'item_path'] },
        s3_source: { params: { format: 'parquet', path: 's3://bucket/data/' }, explanation: 'Read Parquet from S3', fields_filled: ['format', 'path'] },
        s3_sink: { params: { format: 'parquet', path: 's3://bucket/output/' }, explanation: 'Write Parquet to S3', fields_filled: ['format', 'path'] },
        gcs_source: { params: { format: 'parquet', path: 'gs://bucket/data/' }, explanation: 'Read Parquet from GCS', fields_filled: ['format', 'path'] },
        gcs_sink: { params: { format: 'parquet', path: 'gs://bucket/output/' }, explanation: 'Write Parquet to GCS', fields_filled: ['format', 'path'] },
        azure_blob_source: { params: { format: 'parquet', path: 'data/' }, explanation: 'Read Parquet from Azure Blob', fields_filled: ['format', 'path'] },
        azure_blob_sink: { params: { format: 'parquet', path: 'output/' }, explanation: 'Write Parquet to Azure Blob', fields_filled: ['format', 'path'] },
        rest_api: { params: { method: 'GET', content_type: 'application/json' }, explanation: 'Default GET with JSON', fields_filled: ['method', 'content_type'] },
        api_source: { params: { method: 'GET', content_type: 'application/json' }, explanation: 'Default GET with JSON', fields_filled: ['method', 'content_type'] },
        db_source: { params: { query: 'SELECT * FROM table_name LIMIT 100' }, explanation: 'Sample query with LIMIT', fields_filled: ['query'] },
        db_sink: { params: { mode: 'append', table: 'target_table' }, explanation: 'Append mode to target table', fields_filled: ['mode', 'table'] },
        transform: { params: { sql: 'SELECT * FROM source_table' }, explanation: 'Pass-through transform', fields_filled: ['sql'] },
        join: { params: { join_type: 'INNER' }, explanation: 'Default INNER join', fields_filled: ['join_type'] },
        validate: { params: { rules: [{ column: '*', rule: 'not_null' }] }, explanation: 'Basic NOT NULL validation', fields_filled: ['rules'] },
        rename: { params: { mappings: [] }, explanation: 'Add column rename mappings', fields_filled: ['mappings'] },
        typecast: { params: { casts: [] }, explanation: 'Add type cast rules', fields_filled: ['casts'] },
        lookup: { params: { match_type: 'exact' }, explanation: 'Exact match lookup', fields_filled: ['match_type'] },
        union: { params: { mode: 'all' }, explanation: 'Union all rows', fields_filled: ['mode'] },
        pivot: { params: {} , explanation: 'Configure pivot columns and values', fields_filled: [] },
        sample: { params: { percent: 10 }, explanation: 'Sample 10% of rows', fields_filled: ['percent'] },
        window: { params: { function: 'ROW_NUMBER' }, explanation: 'ROW_NUMBER window function', fields_filled: ['function'] },
        gdrive_source: { params: { format: 'auto' }, explanation: 'Auto-detect format from Google Drive', fields_filled: ['format'] },
        gdrive_sink: { params: { format: 'csv' }, explanation: 'Write CSV to Google Drive', fields_filled: ['format'] },
        kafka_source: { params: { format: 'json', auto_offset_reset: 'earliest' }, explanation: 'Read JSON from Kafka, start from earliest', fields_filled: ['format', 'auto_offset_reset'] },
        kafka_sink: { params: { format: 'json' }, explanation: 'Write JSON to Kafka', fields_filled: ['format'] },
        ftp_source: { params: { format: 'csv', path: '/' }, explanation: 'Read CSV from FTP root', fields_filled: ['format', 'path'] },
        ftp_sink: { params: { format: 'csv', path: '/output/' }, explanation: 'Write CSV to FTP', fields_filled: ['format', 'path'] },
      };
      return defaults[nodeType] || { params: {}, explanation: 'No auto-fill available for this node type', fields_filled: [] };
    } finally {
      setLoading(null);
    }
  }, []);

  const diagnoseError = useCallback(async (
    error: string,
    nodeType: string,
    params?: Record<string, any>,
    schema?: Array<{ name: string; type: string }>,
  ): Promise<DiagnoseResult> => {
    setLoading('diagnose');
    try {
      return await aiRequest<DiagnoseResult>('/ai/diagnose-error', {
        error_message: error,
        node_type: nodeType,
        node_params: params || {},
        upstream_schema: schema || [],
      });
    } catch {
      // Deterministic fallback: pattern-match common errors
      const lower = error.toLowerCase();
      if (lower.includes('file not found') || lower.includes('no such file')) {
        return { diagnosis: 'The specified file path does not exist.', suggestion: 'Check the file path and ensure the file is uploaded or accessible.', severity: 'high' };
      }
      if (lower.includes('column') && lower.includes('not found')) {
        return { diagnosis: 'A referenced column does not exist in the data.', suggestion: 'Run the upstream node first to load schema, then re-check column names.', severity: 'medium' };
      }
      if (lower.includes('permission') || lower.includes('access denied')) {
        return { diagnosis: 'Insufficient permissions to access the resource.', suggestion: 'Check credentials and file/database permissions.', severity: 'high' };
      }
      if (lower.includes('timeout')) {
        return { diagnosis: 'The operation timed out.', suggestion: 'Increase timeout in Settings tab, or reduce data volume with a filter.', severity: 'medium' };
      }
      return { diagnosis: 'An unexpected error occurred.', suggestion: 'Review the error message and check node configuration.', severity: 'medium' };
    } finally {
      setLoading(null);
    }
  }, []);

  const generateSQL = useCallback(async (
    naturalLanguage: string,
    columns: string[],
    tableName: string,
  ): Promise<GenerateSQLResult> => {
    setLoading('sql');
    try {
      return await aiRequest<GenerateSQLResult>('/ai/generate-sql', {
        prompt: naturalLanguage,
        columns,
        table_name: tableName,
      });
    } catch {
      return {
        sql: `SELECT * FROM ${tableName || 'source_table'}`,
        explanation: 'AI unavailable, generated basic SELECT query',
        columns_used: columns,
      };
    } finally {
      setLoading(null);
    }
  }, []);

  const profileData = useCallback(async (
    columns: Array<{ name: string; type: string }>,
    sampleData: Record<string, any>[],
  ): Promise<ProfileResult> => {
    setLoading('profile');
    try {
      return await aiRequest<ProfileResult>('/ai/profile-data', {
        columns,
        sample_data: sampleData.slice(0, 50),
      });
    } catch {
      // Deterministic fallback: basic profiling from sample data
      const stats = columns.map(col => {
        const values = sampleData.map(r => r[col.name]);
        const nullCount = values.filter(v => v === null || v === undefined).length;
        const uniqueCount = new Set(values.filter(v => v !== null && v !== undefined)).size;
        return {
          name: col.name,
          type: col.type,
          null_pct: sampleData.length > 0 ? Math.round((nullCount / sampleData.length) * 100) : 0,
          unique_count: uniqueCount,
        };
      });
      return {
        summary: `Dataset has ${columns.length} columns and ${sampleData.length} sample rows`,
        column_stats: stats,
        quality_score: Math.round((1 - stats.reduce((s, c) => s + c.null_pct, 0) / (stats.length * 100 || 1)) * 100),
        recommendations: [],
      };
    } finally {
      setLoading(null);
    }
  }, []);

  const optimizePipeline = useCallback(async (
    nodes: Array<{ id: string; data: any }>,
    edges: Array<{ source: string; target: string }>,
  ): Promise<OptimizeResult> => {
    setLoading('optimize');
    try {
      return await aiRequest<OptimizeResult>('/ai/optimize-pipeline', {
        nodes: nodes.map(n => ({ id: n.id, type: n.data?.stepType, category: n.data?.category })),
        edges: edges.map(e => ({ source: e.source, target: e.target })),
      });
    } catch {
      // Deterministic fallback: basic checks
      const suggestions: OptimizeResult['suggestions'] = [];
      const filterAfterJoin = nodes.some(n => {
        if (n.data?.stepType !== 'filter') return false;
        const upstream = edges.filter(e => e.target === n.id).map(e => e.source);
        return upstream.some(u => nodes.find(nd => nd.id === u)?.data?.stepType === 'join');
      });
      if (filterAfterJoin) {
        suggestions.push({ type: 'reorder', description: 'Move filter before join to reduce data volume early', impact: 'high' });
      }
      if (nodes.filter(n => n.data?.stepType === 'sort').length > 1) {
        suggestions.push({ type: 'remove', description: 'Multiple sort nodes detected; consider keeping only the final sort', impact: 'medium' });
      }
      return { suggestions, estimated_improvement: suggestions.length > 0 ? 'Moderate' : 'Pipeline looks well-structured' };
    } finally {
      setLoading(null);
    }
  }, []);

  return {
    suggestNextNode,
    autoFillConfig,
    diagnoseError,
    generateSQL,
    profileData,
    optimizePipeline,
    loading,
  };
}
