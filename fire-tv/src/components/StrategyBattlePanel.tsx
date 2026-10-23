import React from 'react';
import {StyleSheet, Text, View} from 'react-native';
import {DriverState, UndercutResult} from '../api/types';

interface StrategyBattlePanelProps {
  candidate: DriverState | undefined;
  target: DriverState | undefined;
  result: UndercutResult | null;
  isEngineUnavailable: boolean;
  errorMessage: string | null;
}

const COMPOUND_LABEL: Record<string, string> = {SOFT: 'S', MEDIUM: 'M', HARD: 'H'};

function formatSeconds(ms: number): string {
  return `${(Math.abs(ms) / 1000).toFixed(1)} sec`;
}

function formatMargin(ms: number): string {
  const sign = ms >= 0 ? '+' : '-';
  return `${sign}${(Math.abs(ms) / 1000).toFixed(1)} sec`;
}

/**
 * The "STRATEGY BATTLE" side panel from spec 10.1's main-screen mockup:
 * two drivers' compound/tire age, the live gap between them, and the
 * current undercut window status with its projected gain. `result` comes
 * from useStrategyBattle (calculate_undercut, re-run each lap).
 */
export function StrategyBattlePanel({
  candidate,
  target,
  result,
  isEngineUnavailable,
  errorMessage,
}: StrategyBattlePanelProps): React.JSX.Element {
  return (
    <View style={styles.container} testID="strategy-battle-panel">
      <Text style={styles.header}>STRATEGY BATTLE</Text>

      {candidate && (
        <Text style={styles.driverLine} testID="strategy-battle-candidate">
          {candidate.driver_id.replace('CAR_0', 'CAR ').replace('CAR_', 'CAR ')} {'  '}
          {COMPOUND_LABEL[candidate.compound] ?? candidate.compound} {'  '}
          {candidate.tire_age_laps} laps
        </Text>
      )}
      {target && (
        <Text style={styles.driverLine} testID="strategy-battle-target">
          {target.driver_id.replace('CAR_0', 'CAR ').replace('CAR_', 'CAR ')} {'  '}
          {COMPOUND_LABEL[target.compound] ?? target.compound} {'  '}
          {target.tire_age_laps} laps
        </Text>
      )}

      {candidate && target && (
        <Text style={styles.gapLine} testID="strategy-battle-gap">
          GAP {formatSeconds(candidate.gap_to_leader_ms - target.gap_to_leader_ms)}
        </Text>
      )}

      {isEngineUnavailable ? (
        // Spec v1.1 repair 14 / Section 29 "Strategy engine failure": the
        // exact literal text, never a substituted guess.
        <Text style={styles.unavailableText} testID="strategy-battle-unavailable">
          STRATEGY MODEL UNAVAILABLE
        </Text>
      ) : result ? (
        <>
          <Text
            style={[styles.windowText, result.window === 'OPEN' ? styles.windowOpen : styles.windowClosed]}
            testID="strategy-battle-window">
            UNDERCUT {result.window}
          </Text>
          <Text style={styles.gainText} testID="strategy-battle-gain">
            PROJECTED GAIN {formatMargin(result.expected_margin_ms)}
          </Text>
        </>
      ) : errorMessage ? (
        <Text style={styles.unavailableText} testID="strategy-battle-error">
          {errorMessage}
        </Text>
      ) : (
        <Text style={styles.loadingText} testID="strategy-battle-loading">
          Calculating…
        </Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    borderWidth: 1,
    borderColor: '#2a2e37',
    borderRadius: 8,
    backgroundColor: '#12151a',
    padding: 18,
  },
  header: {
    color: '#7d8492',
    fontSize: 16,
    fontWeight: '700',
    letterSpacing: 2,
    marginBottom: 14,
  },
  driverLine: {
    color: '#e6e8eb',
    fontSize: 18,
    fontWeight: '600',
    marginBottom: 6,
  },
  gapLine: {
    color: '#9aa2b1',
    fontSize: 16,
    marginTop: 10,
    marginBottom: 10,
  },
  windowText: {
    fontSize: 20,
    fontWeight: '800',
    letterSpacing: 1,
    marginBottom: 6,
  },
  windowOpen: {
    color: '#4fd17e',
  },
  windowClosed: {
    color: '#9aa2b1',
  },
  gainText: {
    color: '#4fd1c5',
    fontSize: 16,
    fontWeight: '600',
  },
  unavailableText: {
    color: '#e05a5a',
    fontSize: 16,
    fontWeight: '700',
  },
  loadingText: {
    color: '#7d8492',
    fontSize: 15,
  },
});
