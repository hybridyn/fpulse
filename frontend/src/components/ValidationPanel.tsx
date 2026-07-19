import React, { useMemo } from 'react';
import type { ValidationIssue } from '../utils/validateWorkflow';

interface ValidationPanelProps {
  issues: ValidationIssue[];
  onSelectNode: (nodeId: string) => void;
  onRunAnyway: () => void;
  onClose: () => void;
}

export const ValidationPanel: React.FC<ValidationPanelProps> = ({
  issues,
  onSelectNode,
  onRunAnyway,
  onClose,
}) => {
  const errorCount = useMemo(() => issues.filter((i) => i.level === 'error').length, [issues]);
  const warningCount = useMemo(() => issues.filter((i) => i.level === 'warning').length, [issues]);
  const hasErrors = errorCount > 0;

  // Group issues by nodeId
  const grouped = useMemo(() => {
    const map = new Map<string, ValidationIssue[]>();
    for (const issue of issues) {
      const list = map.get(issue.nodeId) || [];
      list.push(issue);
      map.set(issue.nodeId, list);
    }
    return map;
  }, [issues]);

  if (issues.length === 0) return null;

  return (
    <div
      style={{
        position: 'absolute',
        bottom: 0,
        left: 0,
        right: 0,
        zIndex: 50,
        background: '#ffffff',
        borderTop: '1px solid #e2e8f0',
        borderRadius: '12px 12px 0 0',
        boxShadow: '0 -4px 24px rgba(0,0,0,0.12)',
        maxHeight: 240,
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      {/* Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '10px 16px',
          borderBottom: '1px solid #f1f5f9',
          flexShrink: 0,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontWeight: 600, fontSize: 13, color: '#111827' }}>
            Validation Results
          </span>
          <span style={{ fontSize: 12, color: '#64748b' }}>
            {errorCount > 0 && (
              <span style={{ color: '#ef4444', fontWeight: 500 }}>
                {errorCount} error{errorCount !== 1 ? 's' : ''}
              </span>
            )}
            {errorCount > 0 && warningCount > 0 && ', '}
            {warningCount > 0 && (
              <span style={{ color: '#f59e0b', fontWeight: 500 }}>
                {warningCount} warning{warningCount !== 1 ? 's' : ''}
              </span>
            )}
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {!hasErrors && (
            <button
              onClick={onRunAnyway}
              style={{
                padding: '5px 14px',
                fontSize: 12,
                fontWeight: 500,
                background: '#f59e0b',
                color: '#fff',
                border: 'none',
                borderRadius: 6,
                cursor: 'pointer',
              }}
            >
              Run Anyway
            </button>
          )}
          {hasErrors && (
            <span
              style={{
                padding: '5px 14px',
                fontSize: 12,
                fontWeight: 500,
                background: '#fef2f2',
                color: '#ef4444',
                borderRadius: 6,
              }}
            >
              Fix Issues
            </span>
          )}
          <button
            onClick={onClose}
            style={{
              padding: '4px 8px',
              fontSize: 14,
              background: 'transparent',
              color: '#94a3b8',
              border: 'none',
              borderRadius: 4,
              cursor: 'pointer',
              lineHeight: 1,
            }}
            title="Close"
          >
            x
          </button>
        </div>
      </div>

      {/* Issue list */}
      <div style={{ overflowY: 'auto', flex: 1, padding: '6px 0' }}>
        {Array.from(grouped.entries()).map(([nodeId, nodeIssues]) => (
          <div key={nodeId} style={{ padding: '0 16px', marginBottom: 4 }}>
            {nodeIssues.map((issue, idx) => (
              <button
                key={`${nodeId}-${idx}`}
                onClick={() => onSelectNode(issue.nodeId)}
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 8,
                  width: '100%',
                  padding: '6px 8px',
                  background: 'transparent',
                  border: 'none',
                  borderRadius: 6,
                  cursor: 'pointer',
                  textAlign: 'left',
                  transition: 'background 0.15s',
                }}
                onMouseEnter={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.background = '#f8fafc';
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.background = 'transparent';
                }}
              >
                {/* Icon */}
                <span
                  style={{
                    flexShrink: 0,
                    width: 18,
                    height: 18,
                    display: 'inline-flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: 12,
                    fontWeight: 700,
                    borderRadius: '50%',
                    color: '#fff',
                    background: issue.level === 'error' ? '#ef4444' : '#f59e0b',
                    lineHeight: 1,
                  }}
                >
                  {issue.level === 'error' ? '\u2715' : '\u26A0'}
                </span>

                {/* Content */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <span
                    style={{
                      fontSize: 12,
                      fontWeight: 600,
                      color: 'rgba(255,255,255,0.08)',
                      marginRight: 6,
                    }}
                  >
                    {issue.nodeLabel}
                  </span>
                  <span style={{ fontSize: 12, color: '#64748b' }}>
                    {issue.message}
                    {issue.field && (
                      <span style={{ color: '#94a3b8' }}> ({issue.field})</span>
                    )}
                  </span>
                </div>
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
};

export default ValidationPanel;
