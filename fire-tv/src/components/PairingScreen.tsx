import React, {useState} from 'react';
import {StyleSheet, Text, TextInput, View} from 'react-native';
import {FocusableButton} from './FocusableButton';

interface PairingScreenProps {
  /** True while a pairing attempt is in flight (`useDeviceAuth`'s
   * `status === 'pairing'`) — disables the PAIR button and shows a
   * "Verifying…" state rather than letting a second D-pad Select press
   * fire a second concurrent verify call. */
  isPairing: boolean;
  errorMessage: string | null;
  onPair: (token: string) => void;
}

/**
 * Spec v1.1 pass 3, correction 5: "Runtime Pairing/Authorization" screen,
 * reworked from a shared-secret-entry form into a REAL device-pairing
 * exchange. Shown by App.tsx whenever `useDeviceAuth().status` is
 * `'unpaired'` (no device token stored yet, or a previously-stored one
 * was found expired/invalid/revoked).
 *
 * The person never types the backend's master mutation secret here — an
 * administrator generates a SHORT, 6-digit, one-time pairing code
 * elsewhere (`POST /v1/auth/pairing-codes`, which requires that master
 * secret) and reads/sends it to whoever is sitting at this Fire TV. This
 * screen only ever collects that short code, which is exchanged
 * (`POST /v1/auth/pair`) for a brand-new, device-specific token.
 *
 * D-pad navigation: the code field is a standard `TextInput` with a
 * numeric keyboard, which Android TV's focus engine treats as an ordinary
 * focusable view — D-pad Select opens the platform's on-screen keyboard,
 * and D-pad Down moves focus from the field to the PAIR button below it.
 * No touch input is required at any point.
 */
export function PairingScreen({isPairing, errorMessage, onPair}: PairingScreenProps): React.JSX.Element {
  const [codeInput, setCodeInput] = useState('');

  return (
    <View style={styles.container} testID="pairing-screen">
      <Text style={styles.title}>PAIR THIS FIRE TV</Text>
      <Text style={styles.body}>
        Ask your Pit Wall Copilot administrator for a 6-digit pairing code (they generate it from the backend
        — it is never the same as the backend&apos;s own master credential). The code is valid for a few
        minutes and can only be used once. This Fire TV then stores its own device-specific credential
        securely — it never holds a copy of the backend&apos;s master secret.
      </Text>

      <TextInput
        testID="pairing-code-input"
        style={styles.input}
        value={codeInput}
        onChangeText={text => setCodeInput(text.replace(/[^0-9]/g, '').slice(0, 6))}
        placeholder="000000"
        placeholderTextColor="#5b6270"
        autoCapitalize="none"
        autoCorrect={false}
        keyboardType="number-pad"
        maxLength={6}
        editable={!isPairing}
        accessibilityLabel="Pairing code"
        returnKeyType="done"
        onSubmitEditing={() => onPair(codeInput)}
      />

      {errorMessage && (
        <Text style={styles.errorText} testID="pairing-error">
          {errorMessage}
        </Text>
      )}

      <FocusableButton
        label={isPairing ? 'PAIRING…' : 'PAIR'}
        onPress={() => onPair(codeInput)}
        disabled={isPairing}
        testID="pair-button"
      />

      <Text style={styles.footnote}>
        Local development without pairing remains available by setting LOCAL_DEV_AUTH_TOKEN in
        src/config/authToken.ts (dev builds only — never available in a release build, and never the
        backend&apos;s master secret either).
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    padding: 48,
    justifyContent: 'center',
    backgroundColor: '#0b0d11',
  },
  title: {
    color: '#f2f4f6',
    fontSize: 30,
    fontWeight: '800',
    letterSpacing: 2,
    marginBottom: 16,
  },
  body: {
    color: '#9aa2b1',
    fontSize: 18,
    lineHeight: 26,
    marginBottom: 28,
    maxWidth: 720,
  },
  input: {
    borderWidth: 2,
    borderColor: '#3a3f4b',
    backgroundColor: '#1c1f26',
    borderRadius: 6,
    paddingVertical: 14,
    paddingHorizontal: 18,
    color: '#e6e8eb',
    fontSize: 20,
    marginBottom: 16,
    maxWidth: 560,
  },
  errorText: {
    color: '#e05a5a',
    fontSize: 16,
    fontWeight: '600',
    marginBottom: 16,
    maxWidth: 560,
  },
  footnote: {
    color: '#5b6270',
    fontSize: 14,
    marginTop: 28,
    maxWidth: 560,
  },
});
