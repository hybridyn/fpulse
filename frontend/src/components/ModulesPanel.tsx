import { useState, useRef, useEffect, useCallback } from 'react';
import { useWorkflowStore } from '../stores/workflowStore';
import {
  CATEGORY_ICONS,
  makeReconciledModulesHook,
  regroupByIntent,
  type ModuleCategory,
  type ModuleItem,
  type ModuleLevel,
} from './modulesPanelData';
import Icon from './shared/Icon';

/* ── Styled Icon Components ── */

function IconBox({ bg, children, size = 32 }: { bg: string; children: React.ReactNode; size?: number }) {
  return (
    <div
      className="rounded-lg flex items-center justify-center shadow-sm border border-black/5"
      style={{ background: bg, width: size, height: size }}
    >
      {children}
    </div>
  );
}

/** Brand colors + monogram for each REST manifest connector. */
const SAAS_BRANDS: Record<string, { bg: string; mono: string }> = {
  'rest:salesforce':       { bg: 'linear-gradient(135deg, #00a1e0, #0070d2)', mono: 'SF' },
  'rest:hubspot':          { bg: 'linear-gradient(135deg, #ff7a59, #ff5c35)', mono: 'HS' },
  'rest:stripe':           { bg: 'linear-gradient(135deg, #635bff, #5851df)', mono: 'S' },
  'rest:shopify':          { bg: 'linear-gradient(135deg, #95bf47, #5e8e3e)', mono: 'Sh' },
  'rest:zendesk':          { bg: 'linear-gradient(135deg, #03363d, #17494d)', mono: 'Zd' },
  'rest:jira':             { bg: 'linear-gradient(135deg, #2684ff, #0052cc)', mono: 'J' },
  'rest:notion':           { bg: 'linear-gradient(135deg, #2f2f2f, #000000)', mono: 'N' },
  'rest:airtable':         { bg: 'linear-gradient(135deg, #fcb400, #f82b60)', mono: 'At' },
  'rest:google_analytics': { bg: 'linear-gradient(135deg, #f9ab00, #e37400)', mono: 'GA' },
  'rest:mailchimp':        { bg: 'linear-gradient(135deg, #ffe01b, #f4b400)', mono: 'Mc' },
  'rest:intercom':         { bg: 'linear-gradient(135deg, #1f8ded, #0057ff)', mono: 'Ic' },
  'rest:pipedrive':        { bg: 'linear-gradient(135deg, #1a1a1a, #404040)', mono: 'Pd' },
  'rest:asana':            { bg: 'linear-gradient(135deg, #f06a6a, #d93636)', mono: 'A' },
  'rest:monday':           { bg: 'linear-gradient(135deg, #ff3d57, #fc0e3a)', mono: 'M' },
  'rest:slack_api':        { bg: 'linear-gradient(135deg, #4a154b, #611f69)', mono: 'Sl' },
  'rest:github':           { bg: 'linear-gradient(135deg, #24292e, #0d1117)', mono: 'Gh' },
};

export function ModuleIcon({ type, size = 32 }: { type: string; size?: number }) {
  const svgSize = Math.round(size * 0.5);
  const icons: Record<string, { bg: string; svg: React.ReactNode }> = {
    // Generic Source — connector chosen inside config panel
    source: {
      bg: 'linear-gradient(135deg, #3b82f6, #1d4ed8)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <ellipse cx="12" cy="5" rx="9" ry="3" />
          <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
          <path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3" />
        </svg>
      ),
    },
    // Generic Destination — connector chosen inside config panel
    destination: {
      bg: 'linear-gradient(135deg, #6366f1, #4f46e5)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <polyline points="7 10 12 15 17 10" />
          <line x1="12" y1="15" x2="12" y2="3" />
        </svg>
      ),
    },
    csv_source: {
      bg: 'linear-gradient(135deg, #3b82f6, #2563eb)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <line x1="8" y1="13" x2="16" y2="13" />
          <line x1="8" y1="17" x2="16" y2="17" />
        </svg>
      ),
    },
    db_source: {
      bg: 'linear-gradient(135deg, #8b5cf6, #7c3aed)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <ellipse cx="12" cy="5" rx="9" ry="3" />
          <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
          <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
        </svg>
      ),
    },
    api_source: {
      bg: 'linear-gradient(135deg, #0ea5e9, #0284c7)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" />
          <line x1="2" y1="12" x2="22" y2="12" />
          <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
        </svg>
      ),
    },
    json_source: {
      bg: 'linear-gradient(135deg, #f59e0b, #d97706)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 6h2a2 2 0 0 1 2 2v1a2 2 0 0 0 2 2 2 2 0 0 0-2 2v1a2 2 0 0 1-2 2H4" />
          <path d="M20 6h-2a2 2 0 0 0-2 2v1a2 2 0 0 1-2 2 2 2 0 0 1 2 2v1a2 2 0 0 0 2 2h2" />
        </svg>
      ),
    },
    parquet_source: {
      bg: 'linear-gradient(135deg, #10b981, #059669)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="4" y="4" width="16" height="16" rx="2" />
          <line x1="4" y1="10" x2="20" y2="10" />
          <line x1="10" y1="4" x2="10" y2="20" />
        </svg>
      ),
    },
    excel_source: {
      bg: 'linear-gradient(135deg, #16a34a, #15803d)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <path d="M8 7l8 10M16 7l-8 10" />
        </svg>
      ),
    },
    xml_source: {
      bg: 'linear-gradient(135deg, #e11d48, #be123c)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="16 18 22 12 16 6" />
          <polyline points="8 6 2 12 8 18" />
          <line x1="14" y1="4" x2="10" y2="20" />
        </svg>
      ),
    },
    s3_source: {
      bg: 'linear-gradient(135deg, #f97316, #ea580c)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 8V6a2 2 0 0 1 2-2h2" /><path d="M4 16v2a2 2 0 0 0 2 2h2" />
          <path d="M16 4h2a2 2 0 0 1 2 2v2" /><path d="M16 20h2a2 2 0 0 0 2-2v-2" />
          <circle cx="12" cy="12" r="4" />
        </svg>
      ),
    },
    kafka_source: {
      bg: 'linear-gradient(135deg, #111827, #0f172a)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="18" r="3" />
          <line x1="12" y1="9" x2="6" y2="15" /><line x1="12" y1="9" x2="18" y2="15" />
        </svg>
      ),
    },
    ftp_source: {
      bg: 'linear-gradient(135deg, #6366f1, #4f46e5)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="2" y="6" width="20" height="12" rx="2" />
          <path d="M12 12v.01" /><path d="M8 12v.01" /><path d="M16 12v.01" />
        </svg>
      ),
    },
    gsheet_source: {
      bg: 'linear-gradient(135deg, #22c55e, #16a34a)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <line x1="3" y1="9" x2="21" y2="9" /><line x1="3" y1="15" x2="21" y2="15" />
          <line x1="9" y1="3" x2="9" y2="21" />
        </svg>
      ),
    },
    delta_source: {
      bg: 'linear-gradient(135deg, #0891b2, #0e7490)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 3L4 21h16L12 3z" />
        </svg>
      ),
    },
    // Universal file (auto-detect by extension)
    file_source: {
      bg: 'linear-gradient(135deg, #6366f1, #4338ca)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <line x1="9" y1="13" x2="15" y2="13" />
          <line x1="9" y1="17" x2="15" y2="17" />
        </svg>
      ),
    },
    // SaaS document storage
    sharepoint_source: {
      bg: 'linear-gradient(135deg, #036ac4, #024a8f)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="9" cy="9" r="5" />
          <circle cx="16" cy="14" r="4" />
          <circle cx="11" cy="18" r="3" />
        </svg>
      ),
    },
    onedrive_source: {
      bg: 'linear-gradient(135deg, #0364b8, #0078d4)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M3 16a4 4 0 0 1 2-7.4A6 6 0 0 1 16 8a4 4 0 0 1 4 8H5a2 2 0 0 1-2-2z" />
        </svg>
      ),
    },
    gdrive_source: {
      bg: 'linear-gradient(135deg, #1fa463, #0f9d58)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M9 3l-6 11 3 5h12l3-5L15 3z" />
          <line x1="9" y1="3" x2="15" y2="14" />
          <line x1="3" y1="14" x2="18" y2="14" />
        </svg>
      ),
    },
    dropbox_source: {
      bg: 'linear-gradient(135deg, #0061ff, #0050d3)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polygon points="6 4 12 8 6 12 0 8 6 4" transform="translate(0,2)" />
          <polygon points="18 4 24 8 18 12 12 8 18 4" transform="translate(0,2)" />
          <polygon points="6 12 12 16 6 20 0 16 6 12" transform="translate(0,2)" />
          <polygon points="18 12 24 16 18 20 12 16 18 12" transform="translate(0,2)" />
        </svg>
      ),
    },
    box_source: {
      bg: 'linear-gradient(135deg, #0061d5, #003a7a)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
        </svg>
      ),
    },
    // Sinks (reuse the same glyphs with download arrow accent via duplication)
    file_sink: {
      bg: 'linear-gradient(135deg, #6366f1, #4338ca)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
          <polyline points="9 14 12 17 15 14" />
        </svg>
      ),
    },
    sharepoint_sink: {
      bg: 'linear-gradient(135deg, #036ac4, #024a8f)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="9" cy="9" r="5" />
          <circle cx="16" cy="14" r="4" />
          <circle cx="11" cy="18" r="3" />
        </svg>
      ),
    },
    onedrive_sink: {
      bg: 'linear-gradient(135deg, #0364b8, #0078d4)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M3 16a4 4 0 0 1 2-7.4A6 6 0 0 1 16 8a4 4 0 0 1 4 8H5a2 2 0 0 1-2-2z" />
        </svg>
      ),
    },
    gdrive_sink: {
      bg: 'linear-gradient(135deg, #1fa463, #0f9d58)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M9 3l-6 11 3 5h12l3-5L15 3z" />
          <line x1="9" y1="3" x2="15" y2="14" />
          <line x1="3" y1="14" x2="18" y2="14" />
        </svg>
      ),
    },
    dropbox_sink: {
      bg: 'linear-gradient(135deg, #0061ff, #0050d3)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polygon points="6 4 12 8 6 12 0 8 6 4" transform="translate(0,2)" />
          <polygon points="18 4 24 8 18 12 12 8 18 4" transform="translate(0,2)" />
          <polygon points="6 12 12 16 6 20 0 16 6 12" transform="translate(0,2)" />
          <polygon points="18 12 24 16 18 20 12 16 18 12" transform="translate(0,2)" />
        </svg>
      ),
    },
    box_sink: {
      bg: 'linear-gradient(135deg, #0061d5, #003a7a)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
        </svg>
      ),
    },
    // Output sinks
    csv_sink: {
      bg: 'linear-gradient(135deg, #3b82f6, #2563eb)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
        </svg>
      ),
    },
    json_sink: {
      bg: 'linear-gradient(135deg, #f59e0b, #d97706)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 6h2a2 2 0 0 1 2 2v1a2 2 0 0 0 2 2 2 2 0 0 0-2 2v1a2 2 0 0 1-2 2H4" />
          <path d="M20 6h-2a2 2 0 0 0-2 2v1a2 2 0 0 1-2 2 2 2 0 0 1 2 2v1a2 2 0 0 0 2 2h2" />
        </svg>
      ),
    },
    excel_sink: {
      bg: 'linear-gradient(135deg, #16a34a, #15803d)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <path d="M8 7l8 10M16 7l-8 10" />
        </svg>
      ),
    },
    s3_sink: {
      bg: 'linear-gradient(135deg, #f97316, #ea580c)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 8V6a2 2 0 0 1 2-2h2" /><path d="M4 16v2a2 2 0 0 0 2 2h2" />
          <path d="M16 4h2a2 2 0 0 1 2 2v2" /><path d="M16 20h2a2 2 0 0 0 2-2v-2" />
          <polyline points="8 13 12 17 16 13" /><line x1="12" y1="8" x2="12" y2="17" />
        </svg>
      ),
    },
    kafka_sink: {
      bg: 'linear-gradient(135deg, #111827, #0f172a)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="18" r="3" />
          <line x1="12" y1="9" x2="6" y2="15" /><line x1="12" y1="9" x2="18" y2="15" />
        </svg>
      ),
    },
    api_sink: {
      bg: 'linear-gradient(135deg, #0ea5e9, #0284c7)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" />
          <polyline points="8 12 12 8 16 12" /><line x1="12" y1="16" x2="12" y2="8" />
        </svg>
      ),
    },
    webhook_sink: {
      bg: 'linear-gradient(135deg, #a855f7, #9333ea)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M18 16.98h1a2 2 0 0 0 2-1.98V7a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v8c0 1.1.9 2 2 2h1" />
          <path d="M12 14l4 4-4 4" />
        </svg>
      ),
    },
    email_sink: {
      bg: 'linear-gradient(135deg, #ec4899, #db2777)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="2" y="4" width="20" height="16" rx="2" />
          <polyline points="22 7 12 13 2 7" />
        </svg>
      ),
    },
    delta_sink: {
      bg: 'linear-gradient(135deg, #0891b2, #0e7490)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 3L4 21h16L12 3z" />
        </svg>
      ),
    },
    warehouse_sink: {
      bg: 'linear-gradient(135deg, #7c3aed, #6d28d9)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
          <polyline points="9 22 9 12 15 12 15 22" />
        </svg>
      ),
    },
    // Data Wrangler — ordered list of inline sub-steps (design-data-wrangler-node.md).
    // Same emerald gradient + six-stacked-lines glyph used on the canvas node.
    data_wrangler: {
      bg: 'linear-gradient(135deg, #10b981, #047857)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="3" y1="6" x2="5" y2="6" />
          <line x1="8" y1="6" x2="21" y2="6" />
          <line x1="3" y1="12" x2="5" y2="12" />
          <line x1="8" y1="12" x2="21" y2="12" />
          <line x1="3" y1="18" x2="5" y2="18" />
          <line x1="8" y1="18" x2="21" y2="18" />
        </svg>
      ),
    },
    filter: {
      bg: 'linear-gradient(135deg, #f59e0b, #d97706)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
        </svg>
      ),
    },
    transform: {
      bg: 'linear-gradient(135deg, #10b981, #059669)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
        </svg>
      ),
    },
    derived_column: {
      bg: 'linear-gradient(135deg, #059669, #047857)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
      ),
    },
    rename: {
      bg: 'linear-gradient(135deg, #14b8a6, #0d9488)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" />
        </svg>
      ),
    },
    typecast: {
      bg: 'linear-gradient(135deg, #a855f7, #9333ea)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="23 4 23 10 17 10" />
          <polyline points="1 20 1 14 7 14" />
          <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
        </svg>
      ),
    },
    sort: {
      bg: 'linear-gradient(135deg, #8b5cf6, #7c3aed)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="12" y1="5" x2="12" y2="19" />
          <polyline points="19 12 12 19 5 12" />
          <line x1="4" y1="3" x2="20" y2="3" />
        </svg>
      ),
    },
    deduplicate: {
      bg: 'linear-gradient(135deg, #ec4899, #db2777)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="7" height="7" rx="1" />
          <rect x="14" y="3" width="7" height="7" rx="1" opacity="0.5" />
          <path d="M10 14l4 4m0-4l-4 4" />
        </svg>
      ),
    },
    join: {
      bg: 'linear-gradient(135deg, #f97316, #ea580c)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="8" cy="12" r="5" />
          <circle cx="16" cy="12" r="5" />
        </svg>
      ),
    },
    lookup: {
      bg: 'linear-gradient(135deg, #ea580c, #c2410c)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="11" cy="11" r="8" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
          <line x1="8" y1="11" x2="14" y2="11" />
          <line x1="11" y1="8" x2="11" y2="14" />
        </svg>
      ),
    },
    union: {
      bg: 'linear-gradient(135deg, #d946ef, #c026d3)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="8" />
          <line x1="12" y1="8" x2="12" y2="16" />
          <line x1="8" y1="12" x2="16" y2="12" />
        </svg>
      ),
    },
    aggregate: {
      bg: 'linear-gradient(135deg, #06b6d4, #0891b2)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="18" y1="20" x2="18" y2="10" />
          <line x1="12" y1="20" x2="12" y2="4" />
          <line x1="6" y1="20" x2="6" y2="14" />
        </svg>
      ),
    },
    pivot: {
      bg: 'linear-gradient(135deg, #0891b2, #0e7490)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="15 3 21 3 21 9" />
          <path d="M21 3l-7 7" />
          <rect x="3" y="14" width="8" height="8" rx="1" />
        </svg>
      ),
    },
    unpivot: {
      bg: 'linear-gradient(135deg, #0e7490, #155e75)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="9 21 3 21 3 15" />
          <path d="M3 21l7-7" />
          <rect x="13" y="2" width="8" height="8" rx="1" />
        </svg>
      ),
    },
    window: {
      bg: 'linear-gradient(135deg, #7c3aed, #6d28d9)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <line x1="3" y1="9" x2="21" y2="9" />
          <line x1="9" y1="21" x2="9" y2="9" />
        </svg>
      ),
    },
    sample: {
      bg: 'linear-gradient(135deg, #84cc16, #65a30d)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="2" y="2" width="20" height="20" rx="4" />
          <circle cx="8" cy="8" r="1.5" fill="white" />
          <circle cx="16" cy="8" r="1.5" fill="white" />
          <circle cx="8" cy="16" r="1.5" fill="white" />
          <circle cx="16" cy="16" r="1.5" fill="white" />
          <circle cx="12" cy="12" r="1.5" fill="white" />
        </svg>
      ),
    },
    validate: {
      bg: 'linear-gradient(135deg, #22c55e, #16a34a)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          <polyline points="9 12 11 14 15 10" />
        </svg>
      ),
    },
    data_quality: {
      bg: 'linear-gradient(135deg, #16a34a, #15803d)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          <path d="M9 12h6" /><path d="M12 9v6" />
        </svg>
      ),
    },
    // Data Profile — column-statistics bar-chart glyph. The node emits
    // one row per source column with null %, distinct, min/max, top value.
    data_profile: {
      bg: 'linear-gradient(135deg, #06b6d4, #0891b2)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="3" y1="20" x2="21" y2="20" />
          <rect x="5" y="12" width="3" height="8" />
          <rect x="10.5" y="6" width="3" height="14" />
          <rect x="16" y="14" width="3" height="6" />
        </svg>
      ),
    },
    // SCD Type 2 — clock + fork glyph for "tracks history per business key".
    scd2: {
      bg: 'linear-gradient(135deg, #8b5cf6, #6d28d9)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7v5l3 2" />
        </svg>
      ),
    },
    schema_mapper: {
      bg: 'linear-gradient(135deg, #0d9488, #0f766e)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" />
          <path d="M10 7h4l3 3v4" /><path d="M14 17h-4l-3-3v-4" />
        </svg>
      ),
    },
    flatten_explode: {
      bg: 'linear-gradient(135deg, #f59e0b, #d97706)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 6h2a2 2 0 0 1 2 2v1a2 2 0 0 0 2 2" />
          <line x1="14" y1="6" x2="20" y2="6" /><line x1="14" y1="11" x2="20" y2="11" /><line x1="14" y1="16" x2="20" y2="16" />
          <circle cx="11" cy="6" r="1.5" fill="white" /><circle cx="11" cy="11" r="1.5" fill="white" /><circle cx="11" cy="16" r="1.5" fill="white" />
        </svg>
      ),
    },
    split_out: {
      bg: 'linear-gradient(135deg, #f59e0b, #d97706)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="4" y1="12" x2="10" y2="12" />
          <path d="M10 12l5 -6" /><path d="M10 12l5 0" /><path d="M10 12l5 6" />
          <circle cx="17" cy="6" r="1.5" fill="white" /><circle cx="17" cy="12" r="1.5" fill="white" /><circle cx="17" cy="18" r="1.5" fill="white" />
        </svg>
      ),
    },
    materialize: {
      bg: 'linear-gradient(135deg, #7c3aed, #6d28d9)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <path d="M12 8v8" /><path d="M8 12h8" />
          <circle cx="12" cy="12" r="3" fill="white" opacity="0.3" />
        </svg>
      ),
    },
    retry_handler: {
      bg: 'linear-gradient(135deg, #ef4444, #dc2626)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="23 4 23 10 17 10" />
          <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
        </svg>
      ),
    },
    embedder: {
      bg: 'linear-gradient(135deg, #8b5cf6, #7c3aed)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="3" /><circle cx="12" cy="3" r="1.5" fill="white" /><circle cx="21" cy="12" r="1.5" fill="white" />
          <circle cx="12" cy="21" r="1.5" fill="white" /><circle cx="3" cy="12" r="1.5" fill="white" />
          <line x1="12" y1="6" x2="12" y2="9" /><line x1="15" y1="12" x2="18" y2="12" />
          <line x1="12" y1="15" x2="12" y2="18" /><line x1="6" y1="12" x2="9" y2="12" />
        </svg>
      ),
    },
    llm_guardrail: {
      bg: 'linear-gradient(135deg, #dc2626, #b91c1c)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          <path d="M12 8v4" /><circle cx="12" cy="16" r="1" fill="white" />
        </svg>
      ),
    },
    semantic_router: {
      bg: 'linear-gradient(135deg, #6366f1, #4f46e5)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="5" r="3" /><circle cx="5" cy="19" r="3" /><circle cx="19" cy="19" r="3" />
          <path d="M12 8v3" /><path d="M8.5 14.5L5.5 16.5" /><path d="M15.5 14.5l3 2" />
        </svg>
      ),
    },
    vector_sink: {
      bg: 'linear-gradient(135deg, #8b5cf6, #6d28d9)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="3" /><circle cx="12" cy="3" r="1.5" fill="white" /><circle cx="21" cy="12" r="1.5" fill="white" />
          <circle cx="12" cy="21" r="1.5" fill="white" /><circle cx="3" cy="12" r="1.5" fill="white" />
          <polyline points="9 14 12 17 15 14" />
        </svg>
      ),
    },
    conditional_split: {
      bg: 'linear-gradient(135deg, #eab308, #ca8a04)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="6" y1="3" x2="6" y2="15" />
          <circle cx="18" cy="6" r="3" />
          <circle cx="6" cy="18" r="3" />
          <path d="M18 9a9 9 0 0 1-9 9" />
          <line x1="6" y1="3" x2="18" y2="3" />
        </svg>
      ),
    },
    output: {
      bg: 'linear-gradient(135deg, #6366f1, #4f46e5)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z" />
          <polyline points="17 21 17 13 7 13 7 21" />
          <polyline points="7 3 7 8 15 8" />
        </svg>
      ),
    },
    db_sink: {
      bg: 'linear-gradient(135deg, #4f46e5, #4338ca)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <ellipse cx="12" cy="5" rx="9" ry="3" />
          <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
          <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
          <polyline points="15 13 17 15 21 11" strokeWidth="2.5" />
        </svg>
      ),
    },
    // Flow Control nodes
    if_condition: {
      bg: 'linear-gradient(135deg, #eab308, #ca8a04)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 3l9 9-9 9-9-9z" />
        </svg>
      ),
    },
    switch_case: {
      bg: 'linear-gradient(135deg, #f59e0b, #d97706)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="3" /><path d="M12 3v6" /><path d="M12 15v6" />
          <path d="M3 12h6" /><path d="M15 12h6" />
        </svg>
      ),
    },
    foreach_loop: {
      bg: 'linear-gradient(135deg, #8b5cf6, #7c3aed)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="17 1 21 5 17 9" />
          <path d="M3 11V9a4 4 0 0 1 4-4h14" />
          <polyline points="7 23 3 19 7 15" />
          <path d="M21 13v2a4 4 0 0 1-4 4H3" />
        </svg>
      ),
    },
    lookup_activity: {
      bg: 'linear-gradient(135deg, #eab308, #b45309)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
      ),
    },
    foreach_pipeline: {
      bg: 'linear-gradient(135deg, #8b5cf6, #6d28d9)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="17 1 21 5 17 9" />
          <path d="M3 11V9a4 4 0 0 1 4-4h14" />
          <polyline points="7 23 3 19 7 15" />
          <path d="M21 13v2a4 4 0 0 1-4 4H3" />
          <rect x="9.5" y="9.5" width="5" height="5" rx="1" fill="white" stroke="none" />
        </svg>
      ),
    },
    until_loop: {
      bg: 'linear-gradient(135deg, #7c3aed, #6d28d9)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="23 4 23 10 17 10" />
          <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
        </svg>
      ),
    },
    wait_delay: {
      bg: 'linear-gradient(135deg, #64748b, #475569)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" />
        </svg>
      ),
    },
    set_variable: {
      bg: 'linear-gradient(135deg, #14b8a6, #0d9488)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 7V4h16v3" /><path d="M9 20h6" /><path d="M12 4v16" />
        </svg>
      ),
    },
    execute_pipeline: {
      bg: 'linear-gradient(135deg, #6366f1, #4f46e5)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polygon points="5 3 19 12 5 21 5 3" />
        </svg>
      ),
    },
    // Actions
    http_request: {
      bg: 'linear-gradient(135deg, #0ea5e9, #0284c7)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" />
          <line x1="2" y1="12" x2="22" y2="12" />
          <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
        </svg>
      ),
    },
    webhook_trigger: {
      bg: 'linear-gradient(135deg, #a855f7, #9333ea)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M18 16.98h1a2 2 0 0 0 2-1.98V7a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v8c0 1.1.9 2 2 2h1" />
          <path d="M12 14l4 4-4 4" />
        </svg>
      ),
    },
    code_script: {
      bg: 'linear-gradient(135deg, rgba(255,255,255,0.08), #111827)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" />
        </svg>
      ),
    },
    copy_data: {
      bg: 'linear-gradient(135deg, #3b82f6, #2563eb)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      ),
    },
    get_metadata: {
      bg: 'linear-gradient(135deg, #8b5cf6, #7c3aed)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" /><line x1="12" y1="16" x2="12" y2="12" /><line x1="12" y1="8" x2="12.01" y2="8" />
        </svg>
      ),
    },
    delete_data: {
      bg: 'linear-gradient(135deg, #ef4444, #dc2626)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
          <line x1="10" y1="11" x2="10" y2="17" /><line x1="14" y1="11" x2="14" y2="17" />
        </svg>
      ),
    },
    send_email: {
      bg: 'linear-gradient(135deg, #ec4899, #db2777)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="2" y="4" width="20" height="16" rx="2" /><polyline points="22 7 12 13 2 7" />
        </svg>
      ),
    },
    slack_notify: {
      bg: 'linear-gradient(135deg, #e11d48, #be123c)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
      ),
    },
  };

  // Framework connector nodes — branded gradients + simple SVG glyphs
  const frameworkIcons: Record<string, { bg: string; svg: React.ReactNode }> = {
    jdbc_source: {
      bg: 'linear-gradient(135deg, #06b6d4, #0891b2)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <ellipse cx="12" cy="5" rx="9" ry="3" />
          <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
          <path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3" />
        </svg>
      ),
    },
    jdbc_sink: {
      bg: 'linear-gradient(135deg, #0891b2, #0e7490)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <ellipse cx="12" cy="5" rx="9" ry="3" />
          <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
          <path d="M12 9v8M8 13l4 4 4-4" />
        </svg>
      ),
    },
    cdc_source: {
      bg: 'linear-gradient(135deg, #ec4899, #db2777)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M3 12a9 9 0 1 0 9-9" />
          <polyline points="3 4 3 12 11 12" />
          <circle cx="18" cy="6" r="2" fill="white" />
        </svg>
      ),
    },
    openapi_source: {
      bg: 'linear-gradient(135deg, #65a30d, #4d7c0f)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <path d="M3 9h18M9 21V9" />
        </svg>
      ),
    },
    vector_source: {
      bg: 'linear-gradient(135deg, #a855f7, #7e22ce)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="3" />
          <circle cx="5" cy="5" r="1.5" />
          <circle cx="19" cy="5" r="1.5" />
          <circle cx="5" cy="19" r="1.5" />
          <circle cx="19" cy="19" r="1.5" />
          <line x1="6" y1="6" x2="10" y2="10" />
          <line x1="18" y1="6" x2="14" y2="10" />
          <line x1="6" y1="18" x2="10" y2="14" />
          <line x1="18" y1="18" x2="14" y2="14" />
        </svg>
      ),
    },
    vector_sink: {
      bg: 'linear-gradient(135deg, #7e22ce, #6b21a8)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="3" />
          <path d="M12 2v6M9 5l3-3 3 3" />
          <circle cx="5" cy="19" r="1.5" />
          <circle cx="19" cy="19" r="1.5" />
        </svg>
      ),
    },
    rest_connector: {
      bg: 'linear-gradient(135deg, #f59e0b, #d97706)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
          <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
        </svg>
      ),
    },
    saas_connector: {
      bg: 'linear-gradient(135deg, #a855f7, #6d28d9)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M9 2v6M15 2v6" />
          <rect x="6" y="8" width="12" height="8" rx="2" />
          <path d="M12 16v4" />
          <path d="M8 20h8" />
        </svg>
      ),
    },
    data_quality: {
      bg: 'linear-gradient(135deg, #10b981, #059669)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M9 11l3 3L22 4" />
          <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
        </svg>
      ),
    },
    data_profile: {
      bg: 'linear-gradient(135deg, #06b6d4, #0891b2)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="3" y1="20" x2="21" y2="20" />
          <rect x="5" y="12" width="3" height="8" />
          <rect x="10.5" y="6" width="3" height="14" />
          <rect x="16" y="14" width="3" height="6" />
        </svg>
      ),
    },
    scd2: {
      bg: 'linear-gradient(135deg, #8b5cf6, #6d28d9)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="9" />
          <path d="M12 7v5l3 2" />
        </svg>
      ),
    },
    upsert: {
      bg: 'linear-gradient(135deg, #6366f1, #4f46e5)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <ellipse cx="12" cy="6" rx="9" ry="3" />
          <path d="M3 6v12c0 1.66 4 3 9 3s9-1.34 9-3V6" />
          <path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3" />
          <path d="M16 16l-2 2 2 2M14 18h6" />
        </svg>
      ),
    },
    schema_mapper: {
      bg: 'linear-gradient(135deg, #f97316, #ea580c)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="2" y="4" width="6" height="16" rx="1" />
          <rect x="16" y="4" width="6" height="16" rx="1" />
          <path d="M8 8h4l4 4M8 16h4l4-4" />
        </svg>
      ),
    },
    embedder: {
      bg: 'linear-gradient(135deg, #8b5cf6, #6d28d9)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M4 7h16M4 12h10M4 17h7" />
          <circle cx="18" cy="15" r="3" />
          <path d="M20 17l2 2" />
        </svg>
      ),
    },
    llm_guardrail: {
      bg: 'linear-gradient(135deg, #dc2626, #991b1b)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 2l8 4v6c0 5-3.5 9-8 10-4.5-1-8-5-8-10V6l8-4z" />
          <path d="M9 12l2 2 4-4" />
        </svg>
      ),
    },
    semantic_router: {
      bg: 'linear-gradient(135deg, #0ea5e9, #0369a1)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="6" cy="12" r="2" />
          <circle cx="18" cy="6" r="2" />
          <circle cx="18" cy="18" r="2" />
          <path d="M8 12l8-6M8 12l8 6" />
        </svg>
      ),
    },
    // ── Control flow & integration primitives ──
    append_variable: {
      bg: 'linear-gradient(135deg, #6366f1 0%, #4f46e5 100%)',
      svg: (
        <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
          <rect x="3" y="6" width="18" height="3" rx="0.5" />
          <rect x="3" y="11" width="14" height="3" rx="0.5" />
          <rect x="3" y="16" width="10" height="3" rx="0.5" />
          <path d="M19 16h3M20.5 14.5v3" strokeLinecap="round" />
        </svg>
      ),
    },
    filter_array: {
      bg: 'linear-gradient(135deg, #14b8a6 0%, #0d9488 100%)',
      svg: (
        <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
          <path d="M3 4h18l-7 9v6l-4 2v-8z" strokeLinejoin="round" />
        </svg>
      ),
    },
    validation: {
      bg: 'linear-gradient(135deg, #06b6d4 0%, #0284c7 100%)',
      svg: (
        <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
          <circle cx="12" cy="12" r="9" />
          <path d="M8 12l3 3 5-6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ),
    },
    fail: {
      bg: 'linear-gradient(135deg, #ef4444 0%, #b91c1c 100%)',
      svg: (
        <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
          <circle cx="12" cy="12" r="9" />
          <path d="M9 9l6 6M15 9l-6 6" strokeLinecap="round" />
        </svg>
      ),
    },
    file_system: {
      bg: 'linear-gradient(135deg, #f59e0b 0%, #d97706 100%)',
      svg: (
        <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
          <path d="M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
        </svg>
      ),
    },
    execute_sql_task: {
      bg: 'linear-gradient(135deg, #0ea5e9 0%, #0369a1 100%)',
      svg: (
        <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
          <ellipse cx="12" cy="6" rx="8" ry="3" />
          <path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6" />
          <path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" />
          <text x="12" y="14.5" textAnchor="middle" fill="white" stroke="none" fontSize="4.5" fontWeight="700">SQL</text>
        </svg>
      ),
    },
    // ── Cloud object storage ──
    adls_gen2_source: {
      bg: 'linear-gradient(135deg, #0078d4 0%, #005a9e 100%)',
      svg: (
        <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
          <path d="M3 18h13a4 4 0 0 0 0-8 6 6 0 0 0-11.7-1.5A4 4 0 0 0 3 18z" strokeLinejoin="round" />
          <path d="M9 13v4M7 15h4" strokeLinecap="round" />
        </svg>
      ),
    },
    adls_gen2_sink: {
      bg: 'linear-gradient(135deg, #005a9e 0%, #003e6b 100%)',
      svg: (
        <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
          <path d="M3 18h13a4 4 0 0 0 0-8 6 6 0 0 0-11.7-1.5A4 4 0 0 0 3 18z" strokeLinejoin="round" />
          <path d="M9 11v6M6 14l3 3 3-3" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ),
    },
    azure_blob_source: {
      bg: 'linear-gradient(135deg, #00bcf2 0%, #0078d4 100%)',
      svg: (
        <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
          <path d="M3 18h13a4 4 0 0 0 0-8 6 6 0 0 0-11.7-1.5A4 4 0 0 0 3 18z" strokeLinejoin="round" />
        </svg>
      ),
    },
    azure_blob_sink: {
      bg: 'linear-gradient(135deg, #0078d4 0%, #00557a 100%)',
      svg: (
        <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
          <path d="M3 18h13a4 4 0 0 0 0-8 6 6 0 0 0-11.7-1.5A4 4 0 0 0 3 18z" strokeLinejoin="round" />
          <path d="M10 13v3M8 14.5l2 2 2-2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ),
    },
    gcs_source: {
      bg: 'linear-gradient(135deg, #4285f4 0%, #34a853 100%)',
      svg: (
        <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
          <path d="M3 17h14a4 4 0 0 0 0-8 6 6 0 0 0-11.66-1.5A4 4 0 0 0 3 17z" strokeLinejoin="round" />
          <text x="11" y="14" textAnchor="middle" fill="white" stroke="none" fontSize="5" fontWeight="700">GCS</text>
        </svg>
      ),
    },
    gcs_sink: {
      bg: 'linear-gradient(135deg, #34a853 0%, #0f9d58 100%)',
      svg: (
        <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2">
          <path d="M3 17h14a4 4 0 0 0 0-8 6 6 0 0 0-11.66-1.5A4 4 0 0 0 3 17z" strokeLinejoin="round" />
          <path d="M10 11v4M8 13l2 2 2-2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ),
    },
    // Z11 (2026-05-23) — managed workspace Parquet tables. Stacked
    // cylinders icon visually echoes the Storage page's Managed Tables
    // tab so the two surfaces feel like siblings. Source = amber (read),
    // Sink = blue (write) — same hue convention as csv_source/sink etc.
    local_table_source: {
      bg: 'linear-gradient(135deg, #f59e0b, #d97706)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <ellipse cx="12" cy="5" rx="9" ry="3" />
          <path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
          <path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" />
        </svg>
      ),
    },
    local_table_sink: {
      bg: 'linear-gradient(135deg, #2563eb, #1d4ed8)',
      svg: (
        <svg width={svgSize} height={svgSize} viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <ellipse cx="12" cy="5" rx="9" ry="3" />
          <path d="M3 5v6c0 1.66 4.03 3 9 3s9-1.34 9-3V5" />
          <path d="M3 11v6c0 1.66 4.03 3 9 3s9-1.34 9-3v-6" />
          <path d="M12 9v4M10 11l2 2 2-2" />
        </svg>
      ),
    },
  };

  // Try base icons, then framework, then SaaS brand monogram, then default.
  const entry =
    icons[type] ||
    frameworkIcons[type] ||
    (SAAS_BRANDS[type]
      ? {
          bg: SAAS_BRANDS[type].bg,
          svg: (
            <span
              style={{
                color: 'white',
                fontWeight: 700,
                fontSize: Math.round(size * 0.42),
                fontFamily: 'system-ui, -apple-system, sans-serif',
                letterSpacing: '-0.02em',
              }}
            >
              {SAAS_BRANDS[type].mono}
            </span>
          ),
        }
      : { bg: '#94a3b8', svg: null });
  return <IconBox bg={entry.bg} size={size}>{entry.svg}</IconBox>;
}

// D11/12/13 — Progressive disclosure (2026-05-18).
// `level` per item drives the palette's Basic / Standard / All filter.
//   beginner     → always visible (Sources, Wrangler, SQL, Join, Aggregate, Output)
//   intermediate → default-visible (most common transforms)
//   advanced     → hidden in Basic + Standard; visible in All only
const MODULES: ModuleCategory[] = [
  {
    name: 'Data Movement',
    items: [
      // Generic Source & Destination — pick connector inside config.
      // Microsoft Graph / Salesforce / SAP / Oracle / Postgres etc. are
      // all surfaced via these two nodes; the connector type is selected
      // in the node config and the saved connection (Connections page)
      // carries auth + endpoint config.
      { type: 'source', label: 'Source', level: 'beginner' },
      { type: 'destination', label: 'Destination', level: 'beginner' },
      { type: 'copy_data', label: 'Copy Data', level: 'intermediate' },
      // Z11 (2026-05-23) — managed workspace Parquet tables (Storage
      // page → Managed Tables). These are NOT generic connectors —
      // they're the workspace's internal materialised storage, so they
      // earn dedicated nodes. Before this fix they fell through to an
      // orphan "Destination" bottom-of-palette category with no icon.
      { type: 'local_table_source', label: 'Managed Table Source', level: 'intermediate', description: 'Read from a workspace-managed Parquet table by schema.name' },
      { type: 'local_table_sink', label: 'Managed Table Sink', level: 'intermediate', description: 'Write to a workspace-managed Parquet table (replace / append / merge)' },
    ],
  },
  {
    name: 'Transform',
    items: [
      { type: 'data_wrangler', label: 'Data Wrangler', level: 'beginner', description: 'Ordered list of inline sub-steps (filter / rename / cast / derive / group) with per-step preview' },
      { type: 'transform', label: 'SQL Transform', level: 'beginner' },
      { type: 'filter', label: 'Filter', level: 'intermediate' },
      { type: 'derived_column', label: 'Derived Column', level: 'intermediate' },
      { type: 'sort', label: 'Sort', level: 'intermediate' },
      { type: 'deduplicate', label: 'Deduplicate', level: 'intermediate' },
      { type: 'sample', label: 'Sample', level: 'intermediate' },
      { type: 'schema_mapper', label: 'Schema Mapper', level: 'advanced' },
      { type: 'data_quality', label: 'Data Quality', level: 'advanced' },
      { type: 'flatten_explode', label: 'Flatten', level: 'advanced' },
      // 2026-06-15 (control-flow alignment): "Split Out" tile removed — it was a duplicate
      // preset of Flatten (mode=explode); use Flatten in explode mode.
      // "Keep Latest" (upsert) likewise removed — Deduplicate (keep last)
      // covers it. Both backend types stay for back-compat (HIDDEN_TYPES).
    ],
  },
  {
    name: 'Combine',
    items: [
      { type: 'join', label: 'Join', level: 'beginner' },
      { type: 'aggregate', label: 'Aggregate', level: 'beginner' },
      { type: 'lookup', label: 'Lookup Join', level: 'intermediate' },
      { type: 'union', label: 'Union', level: 'intermediate' },
      { type: 'pivot', label: 'Pivot', level: 'advanced' },
      { type: 'unpivot', label: 'Unpivot', level: 'advanced' },
      { type: 'window', label: 'Window', level: 'advanced' },
    ],
  },
  {
    name: 'Control Flow',
    // 2026-06-15 (control-flow alignment): standard control-flow names + structure.
    //  • Switch = conditional_split (the real multi-output brancher).
    //  • The old single-case "switch_case" filter is hidden (HIDDEN_TYPES).
    //  • ForEach = foreach_pipeline (per-row sub-pipeline); the batch chunker
    //    foreach_loop is relabeled "Batch Rows" and demoted to advanced.
    //  • Lookup = lookup_activity (Lookup activity); the enrichment join
    //    is "Lookup Join" under Combine.
    items: [
      { type: 'if_condition', label: 'If Condition', level: 'intermediate' },
      { type: 'conditional_split', label: 'Switch', level: 'intermediate' },
      { type: 'foreach_pipeline', label: 'ForEach', level: 'intermediate' },
      { type: 'execute_pipeline', label: 'Execute Pipeline', level: 'intermediate' },
      { type: 'lookup_activity', label: 'Lookup', level: 'intermediate' },
      { type: 'set_variable', label: 'Set Variable', level: 'intermediate' },
      { type: 'wait_delay', label: 'Wait', level: 'intermediate' },
      { type: 'fail', label: 'Fail', level: 'intermediate' },
      { type: 'retry_handler', label: 'Retry', level: 'advanced' },
      { type: 'foreach_loop', label: 'Batch Rows', level: 'advanced' },
    ],
  },
  {
    name: 'Action',
    items: [
      { type: 'http_request', label: 'HTTP Request', level: 'intermediate' },
      { type: 'send_email', label: 'Send Email', level: 'intermediate' },
      { type: 'slack_notify', label: 'Slack / Teams', level: 'intermediate' },
      // 2026-05-22: `webhook_trigger` removed from the palette. Per the
      // 2026-05-22 node audit it has a StepType enum value but no backend
      // registration, so every user who added one ended up with a node
      // that couldn't execute. Inbound-webhook receiver infrastructure
      // (URL routing, signature verification, replay protection) isn't
      // ready for v1.0; promote `api_source` for pull-style integration.
      // Legacy workflows that still carry this type are remapped at load
      // time via fpulse/ir/migrations.py:migrate_legacy_node_types.
      // 2026-06-15: Code / Script gated to Plus (runs user Python in-process,
      // not a sandbox) — hidden from OSS via HIDDEN_TYPES.
      { type: 'get_metadata', label: 'Get Metadata', level: 'advanced' },
    ],
  },
  {
    name: 'AI / Semantic',
    items: [
      { type: 'embedder', label: 'Embedder', level: 'intermediate' },
      { type: 'llm_guardrail', label: 'LLM Guardrail', level: 'advanced' },
      { type: 'semantic_router', label: 'Semantic Router', level: 'advanced' },
    ],
  },
];

// CATEGORY_ICONS, useReconciledModules, useBackendNodeTypes,
// ModuleItem, ModuleCategory all live in ./modulesPanelData (the
// non-component data file). They are imported at the top so React
// Fast Refresh sees this file as component-only — no more
// "Could not Fast Refresh ('CATEGORY_ICONS' export is incompatible)"
// HMR cascade that wipes module-level state on every file change.

// Bind the reconciliation hook to our local MODULES array.
const useReconciledModulesLocal = makeReconciledModulesHook(MODULES);

const PANEL_WIDTH_KEY = 'fpulse.nodesPanelWidth';
const PANEL_WIDTH_DEFAULT = 290;
const PANEL_WIDTH_MIN = 220;
const PANEL_WIDTH_MAX = 520;

function loadPanelWidth(): number {
  try {
    const raw = localStorage.getItem(PANEL_WIDTH_KEY);
    if (!raw) return PANEL_WIDTH_DEFAULT;
    const n = parseInt(raw, 10);
    if (Number.isNaN(n)) return PANEL_WIDTH_DEFAULT;
    return Math.max(PANEL_WIDTH_MIN, Math.min(PANEL_WIDTH_MAX, n));
  } catch { return PANEL_WIDTH_DEFAULT; }
}

/**
 * Palette level filter (D11/12/13, 2026-05-18).
 * 'basic'    → show only beginner-tagged items
 * 'standard' → beginner + intermediate (default — first-time UX)
 * 'all'      → everything including advanced (retry_handler, materialize,
 *              SCD2, semantic_router, code_script, etc.)
 */
type PaletteLevel = 'basic' | 'standard' | 'all';
const PALETTE_LEVEL_KEY = 'fpulse.paletteLevel';
function loadPaletteLevel(): PaletteLevel {
  try {
    const raw = localStorage.getItem(PALETTE_LEVEL_KEY);
    if (raw === 'basic' || raw === 'standard' || raw === 'all') return raw;
  } catch { /* ignore */ }
  return 'standard';
}
function levelAllows(item: ModuleItem, level: PaletteLevel): boolean {
  const itemLevel: ModuleLevel = item.level || 'intermediate';
  if (level === 'all') return true;
  if (level === 'standard') return itemLevel !== 'advanced';
  return itemLevel === 'beginner';
}

export default function ModulesPanel() {
  const addNode = useWorkflowStore((s) => s.addNode);
  const rfInstance = useWorkflowStore((s) => s.reactFlowInstance);
  const [search, setSearch] = useState('');
  const [paletteLevel, setPaletteLevel] = useState<PaletteLevel>(loadPaletteLevel);
  useEffect(() => {
    try { localStorage.setItem(PALETTE_LEVEL_KEY, paletteLevel); } catch { /* ignore */ }
  }, [paletteLevel]);
  // C1 — start on 'Import' for intent-grouped views; 'Data Movement' for
  // engine view. Switching the palette level resets to the first
  // category of the new view so the user always sees something open.
  const [activeCategory, setActiveCategory] = useState<string>(
    loadPaletteLevel() === 'all' ? 'Data Movement' : 'Import',
  );
  useEffect(() => {
    setActiveCategory(paletteLevel === 'all' ? 'Data Movement' : 'Import');
  }, [paletteLevel]);
  // Collapsed state lives in the workflow store so ConfigPanel can
  // collapse the rail while a node config modal is open. `open=true`
  // shows the full palette; `open=false` shows the 40px icon rail.
  // `?? true` guards against stale HMR snapshots where the store hasn't
  // yet picked up the new field — without it a hot-reloaded ModulesPanel
  // would briefly render its collapsed form by accident.
  const open = useWorkflowStore((s) => s.nodesPanelOpen ?? true);
  const setOpen = useWorkflowStore((s) => s.setNodesPanelOpen);
  const collapsed = !open;
  const setCollapsed = (next: boolean) => setOpen?.(!next);
  const [panelWidth, setPanelWidth] = useState<number>(loadPanelWidth);
  const dragStateRef = useRef<{ startX: number; startWidth: number } | null>(null);

  const onResizeStart = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    dragStateRef.current = { startX: e.clientX, startWidth: panelWidth };
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  }, [panelWidth]);

  const onResizeMove = useCallback((e: React.PointerEvent) => {
    const s = dragStateRef.current;
    if (!s) return;
    // Panel sits on the LEFT edge of the viewport (May 10 2026 layout
    // swap) — dragging the right-edge handle RIGHT grows the panel,
    // so add dx to start width.
    const dx = e.clientX - s.startX;
    const next = Math.max(PANEL_WIDTH_MIN, Math.min(PANEL_WIDTH_MAX, s.startWidth + dx));
    setPanelWidth(next);
  }, []);

  const onResizeEnd = useCallback((e: React.PointerEvent) => {
    if (!dragStateRef.current) return;
    dragStateRef.current = null;
    try { (e.target as HTMLElement).releasePointerCapture(e.pointerId); } catch {}
    try { localStorage.setItem(PANEL_WIDTH_KEY, String(panelWidth)); } catch {}
  }, [panelWidth]);

  useEffect(() => {
    // Persist debounced — covers cases where pointerup fires off-handle.
    const t = setTimeout(() => {
      try { localStorage.setItem(PANEL_WIDTH_KEY, String(panelWidth)); } catch {}
    }, 250);
    return () => clearTimeout(t);
  }, [panelWidth]);

  // ReactFlow (and any chart libs) listens for window resize to re-measure
  // the canvas. The flex parent shrinks the canvas automatically when this
  // panel grows, but ReactFlow only re-fits on resize events. Dispatch one
  // synchronously on every width tick so nodes/edges stay correctly placed.
  // Also expose the live width via a CSS variable on :root so fixed-position
  // overlays (open-node modal, etc.) can match the panel edge instead of
  // using a hardcoded offset.
  useEffect(() => {
    try {
      // When collapsed the panel renders a 40px (w-10) icon rail, not
      // its draggable `panelWidth`. ConfigPanel's modal-centering
      // overlay reads this CSS var to position its left edge — without
      // the collapsed branch the modal would keep a ~290px ghost
      // margin on the left after a node opens.
      const effectiveWidth = collapsed ? 40 : panelWidth;
      document.documentElement.style.setProperty('--fpulse-nodes-panel-width', `${effectiveWidth}px`);
      window.dispatchEvent(new Event('resize'));
    } catch {}
  }, [panelWidth, collapsed]);

  // Reconciled against the backend registry via the shared hook so this
  // panel and ActivitiesRibbon never diverge on what nodes exist.
  const reconciledModules = useReconciledModulesLocal();

  // D11/12/13: apply the palette level filter BEFORE search/category.
  // 'basic' = beginner only; 'standard' = + intermediate; 'all' = everything.
  const filteredByLevel = reconciledModules.map((cat) => ({
    ...cat,
    items: cat.items.filter((item) => levelAllows(item, paletteLevel)),
  })).filter((cat) => cat.items.length > 0);
  // C1 — Basic + Standard regroup by user intent
  // (Import / Prepare / Analyze / Automate / Publish). All keeps the
  // engine-categorization because power users think in primitives.
  const leveledModules = paletteLevel === 'all'
    ? filteredByLevel
    : regroupByIntent(filteredByLevel);

  // Filter nodes by search. A name/type search spans ALL complexity
  // levels — searching for an advanced node (Data Quality, Window,
  // Flatten/Explode, Get Metadata, …) must find it regardless of the
  // chosen palette tier. Previously search filtered `leveledModules`
  // (already level-restricted), so on the default 'standard' tier those
  // nodes were unreachable by search — the node-review reachability gap.
  // Browsing (no search) still respects the level + active category.
  const searchTerm = search.trim().toLowerCase();
  const filteredModules = searchTerm
    ? reconciledModules.map((cat) => ({
        ...cat,
        items: cat.items.filter((item) =>
          item.label.toLowerCase().includes(searchTerm) ||
          item.type.toLowerCase().includes(searchTerm)
        ),
      })).filter((cat) => cat.items.length > 0)
    : leveledModules.filter((cat) => cat.name === activeCategory);

  const handleAdd = (type: string) => {
    addNode(type);
    setTimeout(() => rfInstance?.fitView({ padding: 0.3, duration: 300 }), 50);
  };

  if (collapsed) {
    return (
      <div className="w-10 bg-slate-50 border-r border-pipe-200 flex flex-col items-center py-2 gap-1 shrink-0">
        <button
          onClick={() => setCollapsed(false)}
          className="w-8 h-8 rounded-lg flex items-center justify-center text-slate-400 hover:text-slate-600 hover:bg-slate-50 transition-all"
          title="Expand activities panel"
        >
          {/* Chevron points RIGHT — panel sits on the left edge, so
              "expand" means it grows rightward. */}
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="9 18 15 12 9 6" />
          </svg>
        </button>
        <div className="w-6 border-t border-slate-200 my-1" />
        {reconciledModules.map((cat) => {
          const ci = CATEGORY_ICONS[cat.name];
          return (
            <button
              key={cat.name}
              onClick={() => { setActiveCategory(cat.name); setCollapsed(false); }}
              className="w-8 h-8 rounded-lg flex items-center justify-center hover:bg-slate-50 transition-all group relative"
              style={{ color: ci?.color }}
              title={cat.name}
            >
              {ci?.icon ? <Icon name={ci.icon} size={16} /> : null}
              <span className="absolute left-full ml-2 px-2 py-1 bg-slate-800 text-white text-xs font-medium rounded-md opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity whitespace-nowrap z-50">
                {cat.name}
              </span>
            </button>
          );
        })}
      </div>
    );
  }

  return (
    <div
      data-fpulse-panel="nodes"
      className="bg-slate-50 border-r border-pipe-200 flex flex-col shrink-0 overflow-hidden relative"
      style={{ width: panelWidth }}
    >
      {/* Resize handle — 4px-wide invisible strip on the RIGHT edge.
          Panel now sits on the LEFT side of the editor; drag the
          handle RIGHT to grow, LEFT to shrink. */}
      <div
        onPointerDown={onResizeStart}
        onPointerMove={onResizeMove}
        onPointerUp={onResizeEnd}
        onPointerCancel={onResizeEnd}
        className="absolute right-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-indigo-400/40 transition-colors z-10"
        title="Drag to resize"
      />
      {/* Header — clear name + always-visible affordance hint. Sizes
          bumped Apr 22 after readability feedback (subline was too faint). */}
      <div className="px-3 pt-3 pb-2 shrink-0">
        <div className="flex items-center justify-between">
          <span className="text-[18px] font-bold text-slate-800">Nodes</span>
          <button
            onClick={() => setCollapsed(true)}
            className="w-6 h-6 rounded flex items-center justify-center text-slate-500 hover:text-slate-700 hover:bg-slate-100 transition-all"
            title="Collapse panel"
          >
            {/* Chevron points LEFT — panel sits on the left edge, so
                "collapse" means it folds back leftward. Mirrors the
                rail's expand-arrow which points RIGHT. */}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="15 18 9 12 15 6" />
            </svg>
          </button>
        </div>
        <div className="text-[14px] text-slate-600 mt-1 leading-snug">
          Drag to canvas, or click to insert
        </div>
      </div>

      {/* Palette level (progressive disclosure) — D11/12/13.
          Basic = beginner-only; Standard = + intermediate (default);
          All = everything (retry_handler, materialize, SCD2, etc.). */}
      <div className="px-2.5 pb-2 shrink-0">
        <div
          className="inline-flex items-center bg-slate-100 rounded-md p-0.5 text-[11px] font-semibold"
          role="tablist"
          aria-label="Palette complexity level"
        >
          {(['basic', 'standard', 'all'] as PaletteLevel[]).map((lv) => (
            <button
              key={lv}
              type="button"
              role="tab"
              aria-selected={paletteLevel === lv}
              onClick={() => setPaletteLevel(lv)}
              className={`px-2.5 py-1 rounded transition-colors ${
                paletteLevel === lv
                  ? 'bg-white text-slate-800 shadow-sm'
                  : 'text-slate-500 hover:text-slate-700'
              }`}
              title={
                lv === 'basic'
                  ? 'Only must-have nodes (Sources, Wrangler, SQL, Join, Aggregate, Output)'
                  : lv === 'standard'
                    ? 'Basic + commonly-used transforms (default)'
                    : 'Everything, including specialist primitives (retry_handler, SCD2, semantic_router, …)'
              }
            >
              {lv === 'basic' ? 'Basic' : lv === 'standard' ? 'Standard' : 'All'}
            </button>
          ))}
        </div>
      </div>

      {/* Search */}
      <div className="px-2.5 pb-2 shrink-0">
        <div className="relative">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#64748b" strokeWidth="2" className="absolute left-2.5 top-1/2 -translate-y-1/2">
            <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search nodes…"
            className="w-full pl-8 pr-2.5 py-1.5 text-[12px] border border-slate-300 rounded-lg outline-none focus:ring-2 focus:ring-pipe-200 focus:border-pipe-400 text-slate-800 placeholder:text-slate-500"
          />
          {search && (
            <button onClick={() => setSearch('')} className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-700">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
            </button>
          )}
        </div>
      </div>

      {/* Node list — vertical category accordion (search bypasses categories
          entirely so "redshift" jumps straight to the matching nodes without
          the user hunting through a category dropdown). */}
      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {/* Only show "No nodes match" in search mode — in browse mode the
            accordion below can be non-empty even when filteredModules is
            (e.g. the active category was filtered out by the level filter). */}
        {search.trim() && filteredModules.length === 0 && (
          <div className="text-center py-6 text-xs text-slate-500">No nodes match "{search}"</div>
        )}

        {/* Search mode: flat list grouped by category header (no expand/collapse) */}
        {search.trim() && filteredModules.map((cat) => (
          <div key={cat.name} className="mb-2">
            <div className="text-xs font-bold text-slate-500 uppercase tracking-wider px-1 pt-2 pb-1">
              {cat.name}
            </div>
            <div className="space-y-0.5">
              {cat.items.map((item) => renderNodeButton(item, handleAdd))}
            </div>
          </div>
        ))}

        {/* Browse mode: vertical accordion, one open at a time.
            Renders from `leveledModules` (level-filtered) so the
            Basic/Standard/All chip selector actually hides items in
            the accordion view, not just in search. The category count
            in the header also reflects the filtered total. */}
        {!search.trim() && leveledModules.map((cat) => {
          const ci = CATEGORY_ICONS[cat.name];
          const isOpen = activeCategory === cat.name;
          return (
            <div key={cat.name} className="mb-1">
              <button
                onClick={() => setActiveCategory(isOpen ? '' : cat.name)}
                className={`w-full flex items-center gap-3 px-3 py-3.5 rounded-md text-[16px] font-semibold transition-all ${
                  isOpen
                    ? 'bg-pipe-50 text-pipe-800 border border-pipe-200'
                    : 'text-slate-700 hover:text-slate-900 hover:bg-slate-50 border border-transparent'
                }`}
              >
                <span className="shrink-0" style={{ color: ci?.color }}>
                  {ci?.icon ? <Icon name={ci.icon} size={20} /> : null}
                </span>
                <span className="flex-1 text-left truncate">{cat.name}</span>
                <span className="text-sm text-slate-600 font-mono font-bold shrink-0 bg-slate-100 px-2.5 py-0.5 rounded">{cat.items.length}</span>
                <svg
                  width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
                  className="shrink-0 transition-transform text-slate-500"
                  style={{ transform: isOpen ? 'rotate(90deg)' : 'rotate(0deg)' }}
                >
                  <polyline points="9 18 15 12 9 6" />
                </svg>
              </button>
              {isOpen && (
                <div className="space-y-0.5 mt-1 mb-2 p-1.5 border border-pipe-200 rounded-lg bg-white">
                  {cat.items.map((item) => renderNodeButton(item, handleAdd))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* Single node button — extracted so search-mode and accordion-mode share the
   exact same visual contract. Icon 32, label 13 (bumped from 12 after the
   Apr 22 readability feedback — labels were still too faint at 12px). */
function renderNodeButton(item: ModuleItem, handleAdd: (type: string) => void) {
  return (
    <button
      key={item.type}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData('application/fpulse-node', item.type);
        e.dataTransfer.effectAllowed = 'move';
      }}
      onClick={() => handleAdd(item.type)}
      className="w-full flex items-start gap-2.5 px-2 py-2 rounded-lg border border-transparent hover:border-slate-200 hover:bg-slate-50 hover:shadow-sm transition-all group cursor-grab active:cursor-grabbing active:scale-[0.97] text-left"
      title={item.description ? `${item.label} — ${item.description}` : `Drag to canvas or click to insert: ${item.label}`}
    >
      <div className="group-hover:scale-110 transition-transform shrink-0 mt-0.5">
        <ModuleIcon type={item.type} size={32} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm text-slate-800 group-hover:text-slate-900 font-semibold truncate">
          {item.label}
        </div>
        {item.description && (
          <div className="text-xs text-slate-500 leading-snug line-clamp-2">
            {item.description}
          </div>
        )}
      </div>
    </button>
  );
}
