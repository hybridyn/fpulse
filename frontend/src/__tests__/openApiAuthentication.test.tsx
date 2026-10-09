import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import OpenApiSecurityReview, { type OpenApiSecurity } from '../components/OpenApiSecurityReview';

/**
 * Authentication review for a generated connector definition.
 *
 * These exercise the review component directly. The generate → review → save
 * flow that used to wrap it (ConnectorAuthorPage) is now step two of the API
 * Explorer — see apiConnectorDraft.test.tsx.
 */

const auth: OpenApiSecurity = { type: 'openapi', alternatives: [
  { supported: true, schemes: [{ name: 'Key', type: 'api_key', scopes: [], fields: { key: 'auth_key' } }] },
  { supported: false, schemes: [{ name: 'OAuth', type: 'unsupported', scopes: ['read'], fields: {}, reason: 'OAuth not enabled' }] },
  { supported: true, schemes: [] },
] };
const streams = [{ name: 'records', path: '/records', auth }];

afterEach(cleanup);

describe('OpenAPI authentication review', () => {
  it('shows alternatives, scopes, anonymous access and unsupported methods', () => {
    render(<OpenApiSecurityReview streams={streams} />);
    expect(screen.getByRole('region', { name: 'Operation authentication' })).toBeVisible();
    expect(screen.getByText('OAuth')).toBeVisible();
    expect(screen.getByText('Scopes: read')).toBeVisible();
    expect(screen.getByText('No authentication')).toBeVisible();
    expect(screen.getByText('Unsupported')).toBeVisible();
  });

  it('changes endpoint review without losing the definition', () => {
    render(<OpenApiSecurityReview streams={[...streams, { name: 'status', path: '/status', auth: { type: 'openapi', alternatives: [{ supported: true, schemes: [] }] } }]} />);
    fireEvent.change(screen.getByRole('combobox', { name: 'Endpoint' }), { target: { value: 'status' } });
    expect(screen.queryByText('OAuth')).not.toBeInTheDocument();
    expect(screen.getByText('No authentication')).toBeVisible();
    expect(screen.getByText('API access not tested')).toBeVisible();
    expect(screen.getByText('Credentials not required')).toBeVisible();
  });

  it('reports when no endpoint has a supported authentication option', () => {
    render(<OpenApiSecurityReview streams={[{ name: 'locked', path: '/locked', auth: {
      type: 'openapi',
      alternatives: [{ supported: false, schemes: [{ name: 'OAuth', type: 'unsupported', scopes: [], fields: {}, reason: 'OAuth not enabled' }] }],
    } }]} />);
    expect(screen.getByText('0 with supported authentication')).toBeVisible();
  });
});
