import React from 'react';
import { createRoot } from 'react-dom/client';
import ApiExplorerPage from '../../src/components/pages/ApiExplorerPage';
import { api } from '../../src/api/client';
import '../../src/styles/globals.css';

// Isolated visual fixture: no requests, credentials or backend writes.
api.post = (async (path: string) => path.endsWith('/reference')
  ? { base_url: 'https://api.stripe.com/', operations: [{ method: 'GET', path: '/v1/account', summary: 'Retrieve account', security: [{ basicAuth: [] }, { bearerAuth: [] }] }] }
  : { status: 200, elapsed_ms: 42, bytes: 152, truncated: false, headers: { 'content-type': 'application/json' }, data: { records: [{ id: 1, customer: 'Acme', amount: 120, details: { region: 'APAC' } }] }, text: '', sample_limit: 100 }) as typeof api.post;
createRoot(document.getElementById('root')!).render(<main className="p-4"><p className="text-xs text-slate-500 mb-4">Visual test fixture - no backend requests</p><ApiExplorerPage /></main>);
