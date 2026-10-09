import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ApiExplorerPage from '../components/pages/ApiExplorerPage';
import { api } from '../api/client';
import { stageApiConnection, takeApiConnection } from '../utils/apiExplorerDraft';
import { requestFields } from '../components/ApiRequestFields';

vi.mock('../api/client', () => ({ api: { post: vi.fn() } }));
const response = { status: 200, elapsed_ms: 42, bytes: 120, data: { records: [{ id: 1, name: 'Acme' }] }, text: '', headers: {}, truncated: false, sample_limit: 100 };
beforeEach(() => { vi.mocked(api.post).mockReset(); takeApiConnection(); });
afterEach(cleanup);
function show() { render(<ApiExplorerPage />); fireEvent.change(screen.getByLabelText('Request URL'), { target: { value: 'https://example.com/records' } }); }

it('sends and inspects nested output without creating a connector', async () => {
  vi.mocked(api.post).mockResolvedValue(response);
  show(); fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  expect(await screen.findByText(/HTTP 200/)).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: 'Table' }));
  fireEvent.change(screen.getByLabelText('Record path'), { target: { value: '$["records"]' } });
  // Cells render unquoted; the type row under the header carries what the quotes used to.
  expect(screen.getByText('Acme')).toBeVisible();
  expect(within(screen.getByRole('table')).getByText('string')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Create connection' }));
  expect(takeApiConnection()?.config.base_url).toBe('https://example.com/records');
  expect(api.post).toHaveBeenCalledTimes(1);
});

it('requires confirmation for a write request', async () => {
  vi.mocked(api.post).mockResolvedValue(response);
  show(); fireEvent.change(screen.getByLabelText('HTTP method'), { target: { value: 'DELETE' } });
  fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  expect(api.post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and send' }));
  await screen.findByText(/HTTP 200/);
  expect(api.post).toHaveBeenCalledWith('/connectors/author/explorer/request', expect.objectContaining({ method: 'DELETE', confirm_write: true }));
});

it('clears results when request configuration changes', async () => {
  vi.mocked(api.post).mockResolvedValue(response);
  show(); fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  await screen.findByText(/HTTP 200/);
  fireEvent.change(screen.getByLabelText('Request URL'), { target: { value: 'https://example.com/other' } });
  expect(screen.queryByRole('button', { name: 'Create connection' })).toBeNull();
});

it('imports methods and path hierarchy without connector generation', async () => {
  vi.mocked(api.post).mockResolvedValue({ base_url: 'https://example.com', operations: [{ path: '/accounts/{id}/orders', method: 'GET', summary: '', security: [] }] });
  show();
  fireEvent.change(screen.getByLabelText('OpenAPI specification'), { target: { value: '{"paths":{}}' } });
  fireEvent.click(screen.getByRole('button', { name: 'Discover endpoints' }));
  const select = await screen.findByLabelText('Discovered endpoint');
  fireEvent.change(select, { target: { value: '0' } });
  expect(screen.getByLabelText('Request URL')).toHaveValue('https://example.com/accounts/{id}/orders');
});

it('keeps errors visible and does not invent a response', async () => {
  vi.mocked(api.post).mockRejectedValue(new Error('Network blocked'));
  show(); fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Network blocked');
  expect(screen.queryByRole('button', { name: 'Create connection' })).toBeNull();
});

it('never transfers query secrets or persists a handoff', () => {
  stageApiConnection('https://example.com/path?api_key=CANARY', 'api_key_query', 'api_key');
  const draft = takeApiConnection();
  expect(JSON.stringify(draft)).not.toContain('CANARY');
  expect(takeApiConnection()).toBeNull();
});

it('sends enabled key-value rows and removes disabled ones', async () => {
  vi.mocked(api.post).mockResolvedValue(response);
  show();
  // Request settings are one scrolling form now — no tab click needed.
  fireEvent.change(screen.getByLabelText('parameter name 1'), { target: { value: 'limit' } });
  fireEvent.change(screen.getByLabelText('parameter value 1'), { target: { value: '20' } });
  fireEvent.click(screen.getByRole('button', { name: 'Add parameter' }));
  fireEvent.change(screen.getByLabelText('parameter name 2'), { target: { value: 'ignored' } });
  fireEvent.click(screen.getByLabelText('Enable parameter 2'));
  fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  await screen.findByText(/HTTP 200/);
  expect(api.post).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ query: { limit: '20' } }));
  fireEvent.click(screen.getByRole('button', { name: 'Remove parameter 1' }));
  expect(screen.queryByDisplayValue('limit')).toBeNull();
});

it('shows the whole request at once, with credentials kept in the form', () => {
  show();
  fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'bearer' } });
  fireEvent.change(screen.getByLabelText('Bearer token'), { target: { value: 'ephemeral' } });
  // Auth, params, headers and body are all present without navigating.
  expect(screen.getByLabelText('parameter name 1')).toBeVisible();
  expect(screen.getByLabelText('header name 1')).toBeVisible();
  expect(screen.getByLabelText('Bearer token')).toHaveValue('ephemeral');
  // The request pane no longer has a tab strip; only the response pane does.
  expect(screen.queryByRole('tablist', { name: 'Request settings' })).toBeNull();
});

it('guides arrival: later steps are unreachable until earlier ones are done', () => {
  render(<ApiExplorerPage />);
  expect(screen.getByRole('button', { name: /Choose an endpoint/ })).toBeEnabled();
  expect(screen.getByRole('button', { name: /Test it/ })).toBeDisabled();
  expect(screen.getByRole('button', { name: /Generate a connector/ })).toBeDisabled();
  // A greyed Send button must not be the only explanation of what to do.
  expect(screen.getByText(/Enter the URL you want to test/i)).toBeVisible();
});

it('navigates to the connector step from the rail once a response exists', async () => {
  vi.mocked(api.post).mockResolvedValue(response);
  show();
  fireEvent.click(screen.getByRole('button', { name: 'Send request' }));
  await screen.findByText(/HTTP 200/);

  const step3 = screen.getByRole('button', { name: /Generate a connector/ });
  expect(step3).toBeEnabled();
  fireEvent.click(step3);
  expect(screen.getByRole('tab', { name: 'Connector' })).toHaveAttribute('aria-selected', 'true');
  expect(screen.getByLabelText('Connector id')).toBeVisible();
});

it('rejects duplicate or unnamed enabled fields', () => {
  const row = { id: '1', enabled: true, key: 'a', value: 'b' };
  expect(() => requestFields([row, { ...row, id: '2' }])).toThrow('Duplicate');
  expect(() => requestFields([{ ...row, key: '' }])).toThrow('Enter a name');
});
