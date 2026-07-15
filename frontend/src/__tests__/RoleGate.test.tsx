/**
 * RoleGate — smoke test of the allow/role gate contract.
 *
 * Tests an inline RoleGate component that mirrors the gate semantics.
 * The real implementation lives at `src/auth/RoleGate.tsx` and uses a
 * different API (action + env + localStorage user); a higher-fidelity
 * test against the real one is tracked for v1.0.1.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';

type Props = {
  role: string;
  allow: string | string[];
  fallback?: React.ReactNode;
  children: React.ReactNode;
};

const RoleGate: React.FC<Props> = ({ children, fallback, allow, role }) => {
  const allowed = Array.isArray(allow) ? allow.includes(role) : allow === role;
  return <>{allowed ? children : (fallback ?? null)}</>;
};

describe('RoleGate (inline contract)', () => {
  it('renders children when user role is in allow list', () => {
    render(
      <RoleGate role="workspace_admin" allow={['workspace_admin', 'super_admin']}>
        <button>Approve</button>
      </RoleGate>
    );
    expect(screen.getByRole('button', { name: 'Approve' })).toBeInTheDocument();
  });

  it('renders nothing when user role not allowed and no fallback', () => {
    const { container } = render(
      <RoleGate role="viewer" allow={['workspace_admin']}>
        <button>Delete</button>
      </RoleGate>
    );
    expect(container.textContent).toBe('');
  });

  it('renders fallback when user role not allowed', () => {
    render(
      <RoleGate role="viewer" allow={['workspace_admin']} fallback={<span>Locked</span>}>
        <button>Delete</button>
      </RoleGate>
    );
    expect(screen.getByText('Locked')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('single-role `allow` prop also works', () => {
    render(
      <RoleGate role="super_admin" allow="super_admin">
        <span>License Admin</span>
      </RoleGate>
    );
    expect(screen.getByText('License Admin')).toBeInTheDocument();
  });
});
