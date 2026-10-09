import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import ApiExplorerPage from '../components/pages/ApiExplorerPage';
import { API_REFERENCES, galleryReference } from '../utils/apiGallery';
import { api } from '../api/client';

/**
 * Curated API references — formerly their own "Gallery" tab, now step one of
 * the Explorer. The tab was removed because six links to public specs is a
 * starting point, not a destination, and the name implied a catalogue of
 * shipped connectors (which lives on Trust / Connections).
 */

vi.mock('../hooks/useDarkMode', () => ({ useDarkMode: () => false }));
vi.mock('../hooks/usePageContext', () => ({ usePageContext: () => {} }));
vi.mock('../api/client', () => ({ api: { post: vi.fn() } }));
afterEach(() => { cleanup(); window.location.hash = ''; vi.clearAllMocks(); });

it('offers every reference as a starting point inside the Explorer', () => {
  render(<ApiExplorerPage />);
  for (const entry of API_REFERENCES) {
    expect(screen.getByRole('button', { name: new RegExp(`${entry.name}\\s*${entry.category}`) })).toBeVisible();
  }
  expect(api.post).not.toHaveBeenCalled();
});

it('says these are specifications, not installed connectors, and points at the real catalogue', () => {
  render(<ApiExplorerPage />);
  const note = screen.getByText(/not installed connectors/i);
  expect(note).toBeVisible();
  expect(screen.getByRole('link', { name: 'Connections' })).toHaveAttribute('href', '#connections');
  expect(screen.getByRole('link', { name: 'Trust page' })).toHaveAttribute('href', '#trust');
});

it('selecting a reference routes to it without fetching anything', () => {
  render(<ApiExplorerPage />);
  fireEvent.click(screen.getByRole('button', { name: /Stripe\s*Payments/ }));
  expect(window.location.hash).toContain('reference=stripe');
  expect(api.post).not.toHaveBeenCalled();
});

it('opens the selected reference in import without fetching or sending requests', () => {
  window.location.hash = '#author?reference=github';
  render(<ApiExplorerPage />);
  expect(screen.getByRole('region', { name: 'OpenAPI import' })).toBeVisible();
  expect(screen.getByText('GitHub API reference')).toBeVisible();
  expect(screen.getByText('No specification loaded yet')).toBeVisible();
  expect(screen.getByLabelText('Request URL')).toHaveValue('');
  expect(api.post).not.toHaveBeenCalled();
  window.location.hash = '#author?reference=stripe';
  fireEvent(window, new Event('hashchange'));
  expect(screen.getByText('Stripe API reference')).toBeVisible();
});

it('does not accept arbitrary source URLs from route parameters', () => {
  expect(galleryReference('#author?reference=https://untrusted.test')).toBeUndefined();
  expect(galleryReference('#author?prefill_url=https://untrusted.test')).toBeUndefined();
});

it('loads a reference specification and selects an endpoint without calling the vendor API', async () => {
  window.location.hash = '#author?reference=stripe';
  vi.mocked(api.post).mockResolvedValue({ base_url: 'https://api.stripe.com/', operations: [
    { method: 'GET', path: '/v1/account', summary: 'Retrieve account', security: [{ basicAuth: [] }] },
    { method: 'GET', path: '/v1/customers', summary: 'List customers', security: [] },
  ] });
  render(<ApiExplorerPage />);
  fireEvent.click(screen.getByRole('button', { name: 'Load specification' }));
  expect(await screen.findByText('Imported: 2 endpoints')).toBeVisible();
  expect(api.post).toHaveBeenCalledWith('/connectors/author/explorer/reference', { reference: 'stripe' });
  fireEvent.change(screen.getByLabelText('Search endpoints'), { target: { value: 'customers' } });
  expect(screen.queryByRole('option', { name: /Retrieve account/ })).toBeNull();
  fireEvent.change(screen.getByLabelText('Discovered endpoint'), { target: { value: '1' } });
  expect(screen.getByLabelText('Request URL')).toHaveValue('https://api.stripe.com/v1/customers');
  expect(api.post).toHaveBeenCalledTimes(1);
});

it('shows download failure and permits retry', async () => {
  window.location.hash = '#author?reference=stripe';
  vi.mocked(api.post).mockRejectedValueOnce(new Error('Download timed out'));
  render(<ApiExplorerPage />);
  fireEvent.click(screen.getByRole('button', { name: 'Load specification' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Download timed out');
  expect(screen.getByRole('button', { name: 'Load specification' })).toBeEnabled();
});

it('ignores a stale download when another reference is selected', async () => {
  window.location.hash = '#author?reference=stripe';
  let finish!: (value: any) => void;
  vi.mocked(api.post).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  render(<ApiExplorerPage />);
  fireEvent.click(screen.getByRole('button', { name: 'Load specification' }));
  expect(screen.getByRole('button', { name: 'Loading specification...' })).toBeDisabled();
  window.location.hash = '#author?reference=github';
  fireEvent(window, new Event('hashchange'));
  finish({ base_url: 'https://api.stripe.com', operations: [{ method: 'GET', path: '/old', security: [] }] });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Load specification' })).toBeEnabled());
  expect(screen.queryByLabelText('Discovered endpoint')).toBeNull();
  expect(screen.getByText('No specification loaded yet')).toBeVisible();
});
