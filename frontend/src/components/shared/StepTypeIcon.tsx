/**
 * StepTypeIcon — canonical icon glyph for a pipeline step type.
 *
 * One source of truth shared by the Executions lineage canvas and the
 * step-IO inspector panel. When the user clicks a node on the lineage
 * view, the same icon shows up in the inspector header — keeps the
 * visual identity of "this is the Source node" consistent.
 */

import Icon, { type IconName } from './Icon';

export const STEP_TYPE_ICONS: Record<string, IconName> = {
  csv_source: 'file-text',
  json_source: 'braces',
  parquet_source: 'box',
  excel_source: 'file-spreadsheet',
  xml_source: 'file-text',
  database_source: 'database',
  rest_api_source: 'globe',
  s3_source: 'cloud',
  kafka_source: 'rss',
  filter: 'filter',
  transform: 'rotate-cw',
  aggregate: 'bar-chart',
  deduplicate: 'broom',
  sort: 'sort-vertical',
  join: 'merge',
  validate: 'check-circle',
  sql: 'database',
  script: 'code',
  lookup: 'search',
  pivot: 'pivot',
  unpivot: 'pivot',
  csv_sink: 'save',
  parquet_sink: 'box',
  database_sink: 'database',
  rest_api_sink: 'globe',
  output: 'upload',
  sink: 'upload',
  source: 'download',
  destination: 'upload',
};

export function StepTypeIcon({
  type,
  size = 14,
  className,
}: {
  type: string;
  size?: number;
  className?: string;
}) {
  const name = STEP_TYPE_ICONS[type];
  if (name) return <Icon name={name} size={size} className={className} />;
  return <span className={className}>⬡</span>;
}

export default StepTypeIcon;
