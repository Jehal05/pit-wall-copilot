import React from 'react';
import {StyleSheet, Text, View} from 'react-native';
import {FocusableButton} from './FocusableButton';

interface PitWallInsightBarProps {
  insightText: string;
  onWhy: () => void;
  onSimulate: () => void;
  onWatch: () => void;
  onSources: () => void;
  onWhatChanged: () => void;
  disabled?: boolean;
}

/**
 * The "PIT WALL INSIGHT" bar from spec 10.1's main-screen mockup, with the
 * five D-pad primary actions from spec 11 ("Why?, Simulate, Watch, What
 * Changed?, Sources" — the mockup itself shows 4 of the 5; WHAT CHANGED?
 * is added here as a 5th button rather than left voice-only, since spec
 * 11 requires the ENTIRE demo be completable without voice).
 */
export function PitWallInsightBar({
  insightText,
  onWhy,
  onSimulate,
  onWatch,
  onSources,
  onWhatChanged,
  disabled = false,
}: PitWallInsightBarProps): React.JSX.Element {
  return (
    <View style={styles.container} testID="pit-wall-insight-bar">
      <Text style={styles.header}>PIT WALL INSIGHT</Text>
      <Text style={styles.insightText} testID="pit-wall-insight-text">
        {insightText}
      </Text>
      <View style={styles.buttonRow}>
        <FocusableButton label="WHY?" onPress={onWhy} disabled={disabled} testID="action-why" />
        <FocusableButton label="SIMULATE" onPress={onSimulate} disabled={disabled} testID="action-simulate" />
        <FocusableButton label="WATCH" onPress={onWatch} disabled={disabled} testID="action-watch" />
        <FocusableButton label="SOURCES" onPress={onSources} disabled={disabled} testID="action-sources" />
        <FocusableButton
          label="WHAT CHANGED?"
          onPress={onWhatChanged}
          disabled={disabled}
          testID="action-what-changed"
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    borderWidth: 1,
    borderColor: '#2a2e37',
    borderRadius: 8,
    backgroundColor: '#171a20',
    padding: 20,
    marginTop: 24,
  },
  header: {
    color: '#7d8492',
    fontSize: 15,
    fontWeight: '700',
    letterSpacing: 2,
    marginBottom: 10,
  },
  insightText: {
    color: '#e6e8eb',
    fontSize: 20,
    marginBottom: 18,
  },
  buttonRow: {
    flexDirection: 'row',
    flexWrap: 'wrap',
  },
});
