import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ApiConnectorDraft, { samplesFrom, suggestConnectorId } from '../components/ApiConnectorDraft';
import { api } from '../api/client';

vi.mock('../api/client', () => ({ api: { post: vi.fn() } }));
beforeEach(() => vi.mocked(api.post).mockReset());
afterEach(cleanup);

const runtimeResponse = {
  mode: 'openapi',
  manifest: { id: 'acme' },
  runtime_manifest: { id: 'acme', streams: [{ name: 'customers', path: '/customers', auth: { type: 'openapi', alternatives: [] } }] },
  validation: { connector_id: 'acme', valid: true, declared_depth_score: 1, computed_depth_score: 1, effective_depth_score: 1, errors: [], warnings: [], streams_evaluated: [] },
};
const samplesResponse = {
  mode: 'samples',
  manifest: { id: 'acme' },
  validation: { connector_id: 'acme', valid: false, declared_depth_score: 1, computed_depth_score: 1, effective_depth_score: 1, errors: [], warnings: ['pagination not inferred'], streams_evaluated: [] },
};

describe('suggestConnectorId', () => {
  it('derives a usable id from the request host', () => {
    expect(suggestConnectorId('https://api.stripe.com/v1/customers')).toBe('stripe');
    expect(suggestConnectorId('https://jsonplaceholder.typicode.com/users')).toBe('typicode');
  });

  it('returns empty rather than throwing on a non-URL', () => {
    expect(suggestConnectorId('not a url')).toBe('');
    expect(suggestConnectorId('')).toBe('');
  });
});

describe('samplesFrom', () => {
  it('caps an array at five objects and ignores non-objects', () => {
    expect(samplesFrom(Array.from({ length: 9 }, (_, i) => ({ i })))).toHaveLength(5);
    expect(samplesFrom([1, 'a', null])).toHaveLength(0);
  });

  it('wraps a single object and rejects scalars', () => {
    expect(samplesFrom({ a: 1 })).toEqual([{ a: 1 }]);
    expect(samplesFrom('text')).toEqual([]);
    expect(samplesFrom(null)).toEqual([]);
  });
});

describe('connector draft step', () => {
  it('asks for an input when there is neither a spec nor a response', () => {
    render(<ApiConnectorDraft specText="" sourceUrl="" responseData={undefined} requestUrl="" />);
    expect(screen.getByText(/import an OpenAPI specification/i)).toBeVisible();
  });

  it('generates from the OpenAPI spec and allows saving the runtime definition', async () => {
    vi.mocked(api.post).mockResolvedValue(runtimeResponse);
    render(<ApiConnectorDraft specText='{"openapi":"3.0.0"}' sourceUrl="" responseData={undefined} requestUrl="https://api.acme.com/customers" />);
    expect(screen.getByLabelText('Connector id')).toHaveValue('acme');

    fireEvent.click(screen.getByRole('button', { name: /Generate connector draft/ }));
    expect(await screen.findByText('Validation passed')).toBeVisible();
    expect(api.post).toHaveBeenCalledWith('/connectors/author/from-openapi',
      expect.objectContaining({ connector_id: 'acme', openapi_text: '{"openapi":"3.0.0"}' }));

    vi.mocked(api.post).mockResolvedValue({ name: 'acme', streams: 1 });
    fireEvent.click(screen.getByRole('button', { name: /Save as Beta connector/ }));
    expect(await screen.findByRole('status')).toHaveTextContent('Saved');
    expect(api.post).toHaveBeenLastCalledWith('/connectors/author/save', { manifest: runtimeResponse.runtime_manifest });
  });

  it('uses the gallery source url when no spec text was pasted', async () => {
    vi.mocked(api.post).mockResolvedValue(runtimeResponse);
    render(<ApiConnectorDraft specText="" sourceUrl="https://specs.test/acme.json" responseData={undefined} requestUrl="https://api.acme.com/x" />);
    fireEvent.click(screen.getByRole('button', { name: /Generate connector draft/ }));
    await screen.findByText('Validation passed');
    expect(api.post).toHaveBeenCalledWith('/connectors/author/from-openapi',
      expect.objectContaining({ openapi_url: 'https://specs.test/acme.json' }));
  });

  it('generates from the live response and does not offer an unsupported save', async () => {
    vi.mocked(api.post).mockResolvedValue(samplesResponse);
    render(<ApiConnectorDraft specText="" sourceUrl="" responseData={[{ id: 1 }]} requestUrl="https://api.acme.com/customers" />);
    fireEvent.click(screen.getByRole('button', { name: /Generate connector draft/ }));

    expect(await screen.findByText('Validation flagged issues')).toBeVisible();
    expect(screen.getByText('pagination not inferred')).toBeVisible();
    expect(api.post).toHaveBeenCalledWith('/connectors/author/from-samples',
      expect.objectContaining({ samples: [{ id: 1 }], base_url: 'https://api.acme.com/customers' }));
    // No runtime manifest came back, so saving must not be offered at all.
    expect(screen.queryByRole('button', { name: /Save as Beta connector/ })).toBeNull();
    expect(screen.getByText(/has no runtime definition/i)).toBeVisible();
  });

  it('does not present an older backend response as a runnable definition', async () => {
    // An OpenAPI generate that comes back without runtime_manifest — an older
    // backend. The v2 draft is still shown, but it must not look savable.
    vi.mocked(api.post).mockResolvedValue({ ...runtimeResponse, runtime_manifest: undefined });
    render(<ApiConnectorDraft specText='{"openapi":"3.0.0"}' sourceUrl="" responseData={undefined} requestUrl="https://api.acme.com/x" />);
    fireEvent.click(screen.getByRole('button', { name: /Generate connector draft/ }));

    expect(await screen.findByText('Validation passed')).toBeVisible();
    expect(screen.getByText(/no runtime manifest from this source/i)).toBeVisible();
    expect(screen.queryByRole('button', { name: /Save as Beta connector/ })).toBeNull();
  });

  it('rejects an id the generator would refuse, before calling the server', () => {
    render(<ApiConnectorDraft specText='{"openapi":"3.0.0"}' sourceUrl="" responseData={undefined} requestUrl="https://api.acme.com/x" />);
    fireEvent.change(screen.getByLabelText('Connector id'), { target: { value: 'Acme Corp' } });
    fireEvent.click(screen.getByRole('button', { name: /Generate connector draft/ }));
    expect(screen.getByRole('alert')).toHaveTextContent('lowercase letters, digits and underscores');
    expect(api.post).not.toHaveBeenCalled();
  });

  it('discards a stale draft when the connector id changes', async () => {
    vi.mocked(api.post).mockResolvedValue(runtimeResponse);
    render(<ApiConnectorDraft specText='{"openapi":"3.0.0"}' sourceUrl="" responseData={undefined} requestUrl="https://api.acme.com/x" />);
    fireEvent.click(screen.getByRole('button', { name: /Generate connector draft/ }));
    await screen.findByText('Validation passed');
    fireEvent.change(screen.getByLabelText('Connector id'), { target: { value: 'other' } });
    expect(screen.queryByText('Validation passed')).toBeNull();
  });
});
