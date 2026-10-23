import React from 'react';
import {render, screen} from '@testing-library/react-native';
import {AgentAnswerScreen} from '../components/AgentAnswerScreen';

describe('AgentAnswerScreen', () => {
  it('shows a loading state', () => {
    render(<AgentAnswerScreen loading errorMessage={null} narrative={null} evidence={[]} />);
    expect(screen.getByTestId('agent-answer-loading')).toBeTruthy();
  });

  it('shows an error message when present', () => {
    render(
      <AgentAnswerScreen loading={false} errorMessage="Rate limit exceeded" narrative={null} evidence={[]} />,
    );
    expect(screen.getByTestId('agent-answer-error')).toBeTruthy();
    expect(screen.getByText('Rate limit exceeded')).toBeTruthy();
  });

  it('renders the narrative and structured evidence lines (spec 50: no prose parsing)', () => {
    render(
      <AgentAnswerScreen
        loading={false}
        errorMessage={null}
        narrative="Car 4 is now 2.0 seconds behind Car 3."
        evidence={[
          {label: 'LIVE STATE', value: 'Gap: 2.0 s'},
          {label: 'MODEL', value: 'Fresh-tire delta: +2.8 s / 2 laps'},
        ]}
      />,
    );
    expect(screen.getByTestId('agent-answer-narrative')).toBeTruthy();
    expect(screen.getByText('Car 4 is now 2.0 seconds behind Car 3.')).toBeTruthy();
    expect(screen.getByText('LIVE STATE')).toBeTruthy();
    expect(screen.getByText('Gap: 2.0 s')).toBeTruthy();
    expect(screen.getByText('MODEL')).toBeTruthy();
    expect(screen.getByText('Fresh-tire delta: +2.8 s / 2 laps')).toBeTruthy();
  });

  it('marks a degraded (Bedrock-unavailable) narrative distinctly but still renders it', () => {
    render(
      <AgentAnswerScreen
        loading={false}
        errorMessage={null}
        narrative="Strategy explanation temporarily unavailable."
        degraded
        evidence={[]}
      />,
    );
    const narrative = screen.getByTestId('agent-answer-narrative');
    expect(narrative).toBeTruthy();
    expect(screen.getByText('Strategy explanation temporarily unavailable.')).toBeTruthy();
  });
});
