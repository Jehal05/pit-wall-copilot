import React from 'react';
import {fireEvent, render, screen} from '@testing-library/react-native';
import {PitWallInsightBar} from '../components/PitWallInsightBar';

describe('PitWallInsightBar', () => {
  it('renders the insight text and all five D-pad primary actions (spec 11)', () => {
    const handlers = {
      onWhy: jest.fn(),
      onSimulate: jest.fn(),
      onWatch: jest.fn(),
      onSources: jest.fn(),
      onWhatChanged: jest.fn(),
    };
    render(<PitWallInsightBar insightText="The undercut window has opened." {...handlers} />);

    expect(screen.getByText('The undercut window has opened.')).toBeTruthy();

    fireEvent.press(screen.getByTestId('action-why'));
    fireEvent.press(screen.getByTestId('action-simulate'));
    fireEvent.press(screen.getByTestId('action-watch'));
    fireEvent.press(screen.getByTestId('action-sources'));
    fireEvent.press(screen.getByTestId('action-what-changed'));

    expect(handlers.onWhy).toHaveBeenCalledTimes(1);
    expect(handlers.onSimulate).toHaveBeenCalledTimes(1);
    expect(handlers.onWatch).toHaveBeenCalledTimes(1);
    expect(handlers.onSources).toHaveBeenCalledTimes(1);
    expect(handlers.onWhatChanged).toHaveBeenCalledTimes(1);
  });

  it('disables every action button when disabled=true', () => {
    render(
      <PitWallInsightBar
        insightText="Calculating…"
        onWhy={jest.fn()}
        onSimulate={jest.fn()}
        onWatch={jest.fn()}
        onSources={jest.fn()}
        onWhatChanged={jest.fn()}
        disabled
      />,
    );
    expect(screen.getByTestId('action-why').props.accessibilityState?.disabled).toBe(true);
    expect(screen.getByTestId('action-simulate').props.accessibilityState?.disabled).toBe(true);
    expect(screen.getByTestId('action-why').props.focusable).toBe(false);
  });
});
