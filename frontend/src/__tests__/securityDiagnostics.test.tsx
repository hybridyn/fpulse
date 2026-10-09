import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import TrustPage from '../components/pages/TrustPage';
import { api } from '../api/client';
import { readCurrentPage, readSubRoute } from '../router';

vi.mock('../hooks/usePageContext', () => ({ usePageContext: vi.fn() }));
const posture = {
  as_of: '2026-09-18T12:00:00Z',
  sovereignty: { telemetry_currently_enabled: null,
    active_provider_summary: { provider: 'openai', model: 'test-model', is_local: false, status: 'configured' } },
  security_baseline: [{ key: 'encryption', label: 'Credential storage', status: 'not_checked',
    checked_at: null, detail: 'No storage audit recorded.' }],
};
beforeEach(() => {
  vi.spyOn(api, 'getTrustPosture').mockResolvedValue(posture);
  vi.spyOn(api, 'getTrustEvalSummary').mockResolvedValue({ ran: false });
  vi.spyOn(api, 'getCertMatrix').mockResolvedValue({ rows: [], total: 0 } as any);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); window.location.hash = ''; });

it('does not present unknown settings or unmeasured controls as verified', async () => {
  render(<TrustPage embedded />);
  expect(await screen.findByText('openai test-model')).toBeInTheDocument();
  expect(screen.getByText('Unknown')).toBeInTheDocument();
  expect(screen.getAllByText('Not checked').length).toBeGreaterThan(0);
  expect(screen.queryByText('Verified')).toBeNull();
  expect(screen.getByText(/This is not an audit timestamp/)).toBeInTheDocument();
  expect(screen.getByText('No recorded evaluation run.')).toBeInTheDocument();
});

it('shows independent endpoint failures and supports retry', async () => {
  vi.mocked(api.getTrustPosture).mockRejectedValueOnce(new Error('offline'));
  render(<TrustPage embedded />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Installation diagnostics unavailable');
  expect(screen.getByText('No recorded evaluation run.')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  expect(await screen.findByText('openai test-model')).toBeInTheDocument();
});

it('preserves legacy bookmarks under Settings security', () => {
  for (const hash of ['#trust', '#cert-matrix', '#settings/security']) {
    window.location.hash = hash;
    expect(readCurrentPage()).toBe('settings');
    expect(readSubRoute()).toBe('security');
  }
});
