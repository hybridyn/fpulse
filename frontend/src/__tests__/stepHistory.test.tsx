import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import StepIODrawer from '../components/pages/StepIODrawer';
import { api } from '../api/client';

const sample = { step_id: 'step', label: 'Filter', status: 'success', row_count: 2,
  sample_rows: [{ value: 'historical-value' }], schema: [], sample_pruned: false,
  sample_truncated: true, captured_at: '2026-09-18', missing: false };
const show = () => render(<StepIODrawer open executionId="history-run" stepId="step" onClose={() => {}} />);

describe('historical step samples', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, 'getStepOutput').mockResolvedValue(sample);
    vi.spyOn(api, 'getStepInput').mockResolvedValue({ inputs: [] });
  });
  afterEach(cleanup);

  it('shows actual historical rows', async () => {
    show();
    expect(await screen.findByText('historical-value')).toBeInTheDocument();
  });
  it('distinguishes missing capture from failed execution', async () => {
    vi.mocked(api.getStepOutput).mockResolvedValue({ ...sample, sample_rows: [], missing: true });
    show();
    expect(await screen.findByText(/Sample not captured for this run/)).toBeInTheDocument();
    expect(screen.queryByText(/Failed to load/)).toBeNull();
  });
  it('labels expired samples', async () => {
    vi.mocked(api.getStepOutput).mockResolvedValue({ ...sample, sample_rows: [], sample_pruned: true });
    show();
    expect(await screen.findByText('Sample expired after 30 days.')).toBeInTheDocument();
  });
  it('keeps input inspectable when the output request fails', async () => {
    vi.mocked(api.getStepOutput).mockRejectedValue(new Error('Output unavailable'));
    vi.mocked(api.getStepInput).mockResolvedValue({ inputs: [{ ...sample, source_step_id: 'source' }] });
    show();
    await screen.findByText(/Failed to load step data/);
    fireEvent.click(screen.getByRole('button', { name: 'Input', exact: true }));
    expect(await screen.findByText('historical-value')).toBeInTheDocument();
    expect(screen.queryByText(/Failed to load/)).toBeNull();
  });
});
