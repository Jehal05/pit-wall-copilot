import React, {useState} from 'react';
import {Pressable, StyleSheet, Text, View} from 'react-native';

interface FocusableButtonProps {
  label: string;
  onPress: () => void;
  disabled?: boolean;
  hasTVPreferredFocus?: boolean;
  testID?: string;
}

/**
 * A button styled for 10-foot viewing that shows a clearly visible focus
 * ring when reached via D-pad navigation (Android TV / Fire TV maps D-pad
 * up/down/left/right to the standard focus engine for focusable views;
 * Select fires onPress).
 */
export function FocusableButton({
  label,
  onPress,
  disabled = false,
  hasTVPreferredFocus = false,
  testID,
}: FocusableButtonProps): React.JSX.Element {
  const [isFocused, setIsFocused] = useState(false);

  return (
    <Pressable
      accessible
      accessibilityRole="button"
      accessibilityLabel={label}
      testID={testID}
      focusable={!disabled}
      hasTVPreferredFocus={hasTVPreferredFocus}
      disabled={disabled}
      onFocus={() => setIsFocused(true)}
      onBlur={() => setIsFocused(false)}
      onPress={onPress}
      style={[styles.button, isFocused && styles.buttonFocused, disabled && styles.buttonDisabled]}>
      <View>
        <Text style={[styles.label, isFocused && styles.labelFocused]}>{label}</Text>
      </View>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  button: {
    borderWidth: 2,
    borderColor: '#3a3f4b',
    backgroundColor: '#1c1f26',
    paddingVertical: 14,
    paddingHorizontal: 22,
    borderRadius: 6,
    marginRight: 12,
    marginBottom: 12,
  },
  buttonFocused: {
    borderColor: '#4fd1c5',
    backgroundColor: '#213538',
    transform: [{scale: 1.05}],
  },
  buttonDisabled: {
    opacity: 0.4,
  },
  label: {
    color: '#e6e8eb',
    fontSize: 22,
    fontWeight: '600',
    letterSpacing: 1,
  },
  labelFocused: {
    color: '#4fd1c5',
  },
});
