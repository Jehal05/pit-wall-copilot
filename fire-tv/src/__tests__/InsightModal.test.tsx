import React from 'react';
import {Text} from 'react-native';
import {BackHandler} from 'react-native';
import {fireEvent, render, screen} from '@testing-library/react-native';
import {InsightModal} from '../components/InsightModal';

describe('InsightModal', () => {
  it('renders nothing when visible=false (RN Modal does not mount its subtree while hidden)', () => {
    render(
      <InsightModal visible={false} title="WHY?" onClose={jest.fn()} testID="why-modal">
        <Text>body</Text>
      </InsightModal>,
    );
    expect(screen.queryByTestId('why-modal')).toBeNull();
    expect(screen.queryByText('WHY?')).toBeNull();
    expect(screen.queryByText('body')).toBeNull();
  });

  it('shows the title and children when visible=true', () => {
    render(
      <InsightModal visible title="WHY?" onClose={jest.fn()} testID="why-modal">
        <Text>The undercut window has opened.</Text>
      </InsightModal>,
    );
    expect(screen.getByTestId('why-modal').props.visible).toBe(true);
    expect(screen.getByText('WHY?')).toBeTruthy();
    expect(screen.getByText('The undercut window has opened.')).toBeTruthy();
  });

  it('calls onClose when the CLOSE button is pressed', () => {
    const onClose = jest.fn();
    render(
      <InsightModal visible title="WHY?" onClose={onClose} testID="why-modal">
        <Text>body</Text>
      </InsightModal>,
    );
    fireEvent.press(screen.getByTestId('why-modal-close'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('registers a hardwareBackPress handler while visible and calls onClose from it (spec 11: Back)', () => {
    const addSpy = jest.spyOn(BackHandler, 'addEventListener');
    const onClose = jest.fn();
    render(
      <InsightModal visible title="WHY?" onClose={onClose} testID="why-modal">
        <Text>body</Text>
      </InsightModal>,
    );

    const call = addSpy.mock.calls.find(([eventName]) => eventName === 'hardwareBackPress');
    expect(call).toBeTruthy();
    const handler = call?.[1] as () => boolean;

    const handled = handler();

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(handled).toBe(true); // consumed — must not fall through and exit the app
    addSpy.mockRestore();
  });

  it('does not register a hardwareBackPress handler while hidden', () => {
    const addSpy = jest.spyOn(BackHandler, 'addEventListener');
    render(
      <InsightModal visible={false} title="WHY?" onClose={jest.fn()} testID="why-modal">
        <Text>body</Text>
      </InsightModal>,
    );
    const call = addSpy.mock.calls.find(([eventName]) => eventName === 'hardwareBackPress');
    expect(call).toBeUndefined();
    addSpy.mockRestore();
  });
});
