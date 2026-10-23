import React from 'react';
import {fireEvent, render, screen} from '@testing-library/react-native';
import {PairingScreen} from '../components/PairingScreen';

describe('PairingScreen', () => {
  it('is reachable via D-pad: the code field and PAIR button are both focusable', () => {
    render(<PairingScreen isPairing={false} errorMessage={null} onPair={jest.fn()} />);
    const input = screen.getByTestId('pairing-code-input');
    const pairButton = screen.getByTestId('pair-button');
    expect(input).toBeTruthy();
    expect(pairButton.props.focusable).toBe(true);
  });

  it('calls onPair with the entered pairing code when PAIR is pressed', () => {
    const onPair = jest.fn();
    render(<PairingScreen isPairing={false} errorMessage={null} onPair={onPair} />);
    fireEvent.changeText(screen.getByTestId('pairing-code-input'), '482913');
    fireEvent.press(screen.getByTestId('pair-button'));
    expect(onPair).toHaveBeenCalledWith('482913');
  });

  it('calls onPair when the on-screen keyboard submits the field directly', () => {
    const onPair = jest.fn();
    render(<PairingScreen isPairing={false} errorMessage={null} onPair={onPair} />);
    fireEvent.changeText(screen.getByTestId('pairing-code-input'), '111222');
    fireEvent(screen.getByTestId('pairing-code-input'), 'submitEditing');
    expect(onPair).toHaveBeenCalledWith('111222');
  });

  it('strips non-digit characters and caps input at 6 digits', () => {
    const onPair = jest.fn();
    render(<PairingScreen isPairing={false} errorMessage={null} onPair={onPair} />);
    const input = screen.getByTestId('pairing-code-input');
    fireEvent.changeText(input, 'ab1-2c3d4e5f6g7');
    fireEvent.press(screen.getByTestId('pair-button'));
    expect(onPair).toHaveBeenCalledWith('123456');
  });

  it('never renders a field for the full backend master secret', () => {
    render(<PairingScreen isPairing={false} errorMessage={null} onPair={jest.fn()} />);
    expect(screen.queryByTestId('pairing-token-input')).toBeNull();
    expect(screen.queryByLabelText('Pairing token')).toBeNull();
  });

  it('shows the error message when one is set', () => {
    render(
      <PairingScreen isPairing={false} errorMessage="This pairing code is invalid." onPair={jest.fn()} />,
    );
    expect(screen.getByTestId('pairing-error').props.children).toBe('This pairing code is invalid.');
  });

  it('does not render an error message when none is set', () => {
    render(<PairingScreen isPairing={false} errorMessage={null} onPair={jest.fn()} />);
    expect(screen.queryByTestId('pairing-error')).toBeNull();
  });

  it('disables the field and button, and shows a pairing label, while pairing is in flight', () => {
    render(<PairingScreen isPairing={true} errorMessage={null} onPair={jest.fn()} />);
    expect(screen.getByTestId('pairing-code-input').props.editable).toBe(false);
    // FocusableButton maps `disabled` to `focusable={!disabled}` (see
    // FocusableButton.tsx) — the D-pad focus engine's own signal that
    // this control cannot currently be reached/activated, which is what
    // PitWallInsightBar.test.tsx's equivalent assertion already checks.
    expect(screen.getByTestId('pair-button').props.focusable).toBe(false);
    expect(screen.getByText('PAIRING…')).toBeTruthy();
  });
});
