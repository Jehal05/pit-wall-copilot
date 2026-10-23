import React, {useEffect, useState} from 'react';
import {BackHandler, Modal, Pressable, ScrollView, StyleSheet, Text, View} from 'react-native';

interface InsightModalProps {
  visible: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  testID: string;
}

/**
 * Shared full-screen overlay for every P0 "screen" beyond the main Race
 * Dashboard (Strategy Battle lives inline on the dashboard itself; this
 * wraps Simulation Comparison, Agent Answer, Evidence/Sources and Strategy
 * Watch Alert — spec 10.2's remaining required screens). Spec 10.2 also
 * says "No additional top-level pages are required," so these are modal
 * overlays on the single dashboard route rather than separate navigator
 * screens — the same architectural choice Gate 0's single-screen App.tsx
 * already made.
 *
 * Fire TV's hardware Back button SHALL close whichever screen is open
 * (spec 11: "Back") rather than exiting the app while a modal is open —
 * App.tsx's own BackHandler only handles the top-level "no screen to go
 * back from" case, so this component owns Back for as long as it's
 * visible, and yields it back (returns false) once closed.
 */
export function InsightModal({
  visible,
  title,
  onClose,
  children,
  testID,
}: InsightModalProps): React.JSX.Element {
  const [isCloseFocused, setIsCloseFocused] = useState(false);

  useEffect(() => {
    if (!visible) {
      return undefined;
    }
    const subscription = BackHandler.addEventListener('hardwareBackPress', () => {
      onClose();
      return true; // handled — do not fall through to App.tsx's own handler (which would exit the app)
    });
    return () => subscription.remove();
  }, [visible, onClose]);

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose} testID={testID}>
      <View style={styles.backdrop}>
        <View style={styles.card}>
          <View style={styles.headerRow}>
            <Text style={styles.title}>{title}</Text>
            <Pressable
              accessible
              accessibilityRole="button"
              accessibilityLabel="Close"
              focusable
              onFocus={() => setIsCloseFocused(true)}
              onBlur={() => setIsCloseFocused(false)}
              onPress={onClose}
              testID={`${testID}-close`}
              style={[styles.closeButton, isCloseFocused && styles.closeButtonFocused]}>
              <Text style={styles.closeButtonText}>CLOSE</Text>
            </Pressable>
          </View>
          <ScrollView contentContainerStyle={styles.body}>{children}</ScrollView>
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: {
    flex: 1,
    backgroundColor: 'rgba(4, 5, 7, 0.86)',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 40,
  },
  card: {
    width: '100%',
    maxWidth: 920,
    maxHeight: '85%',
    backgroundColor: '#12151a',
    borderWidth: 1,
    borderColor: '#2a2e37',
    borderRadius: 12,
    padding: 28,
  },
  headerRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginBottom: 20,
  },
  title: {
    color: '#f2f4f6',
    fontSize: 26,
    fontWeight: '800',
    letterSpacing: 1,
  },
  closeButton: {
    borderWidth: 2,
    borderColor: '#3a3f4b',
    borderRadius: 6,
    paddingVertical: 8,
    paddingHorizontal: 16,
  },
  closeButtonFocused: {
    borderColor: '#4fd1c5',
    backgroundColor: '#213538',
  },
  closeButtonText: {
    color: '#e6e8eb',
    fontSize: 15,
    fontWeight: '700',
    letterSpacing: 1,
  },
  body: {
    paddingBottom: 8,
  },
});
