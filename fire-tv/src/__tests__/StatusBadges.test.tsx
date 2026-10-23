import React from 'react';
import {render, screen} from '@testing-library/react-native';
import {ConnectionBadge, SimulatorBadge, StaleDataBadge} from '../components/StatusBadges';

describe('StaleDataBadge', () => {
  it('renders the exact spec v1.1 Section 29 literal text "DATA STALE"', () => {
    render(<StaleDataBadge />);
    expect(screen.getByTestId('stale-data-badge')).toBeTruthy();
    expect(screen.getByText('DATA STALE')).toBeTruthy();
  });
});

describe('ConnectionBadge', () => {
  it('renders a label for every ConnectionStatus value', () => {
    const {rerender} = render(<ConnectionBadge status="loading" />);
    expect(screen.getByText('CONNECTING…')).toBeTruthy();

    rerender(<ConnectionBadge status="connected" />);
    expect(screen.getByText('LIVE')).toBeTruthy();

    rerender(<ConnectionBadge status="disconnected" />);
    expect(screen.getByText('DISCONNECTED')).toBeTruthy();

    rerender(<ConnectionBadge status="error" />);
    expect(screen.getByText('API ERROR')).toBeTruthy();
  });
});

describe('SimulatorBadge', () => {
  it('renders the mapped label for a known status', () => {
    render(<SimulatorBadge status="RUNNING" />);
    expect(screen.getByText('LIVE')).toBeTruthy();
  });

  it('falls back to the raw status string for an unmapped value', () => {
    render(<SimulatorBadge status="SOMETHING_NEW" />);
    expect(screen.getByText('SOMETHING_NEW')).toBeTruthy();
  });
});
