import React from 'react';
import {StyleSheet, Text, View} from 'react-native';
import {StrategySimulationResult} from '../api/types';

interface SimulationComparisonScreenProps {
  loading: boolean;
  errorMessage: string | null;
  isEngineUnavailable: boolean;
  result: StrategySimulationResult | null;
}

function formatMargin(ms: number): string {
  const sign = ms >= 0 ? '+' : '-';
  return `${sign}${(Math.abs(ms) / 1000).toFixed(1)} s`;
}

/**
 * The "Simulation Comparison" P0 screen (spec 10.2 #3, UJ-3
 * "counterfactual"): PIT NOW vs WAIT N LAPS side by side, backed by
 * simulate_strategy. Only fields the backend actually models are shown
 * (pit lap, wait lap, tire compound after the stop, net margin,
 * confidence) — UJ-3's mockup also shows an "estimated rejoin position"
 * row (P4/P5 vs P6), which this repository's strategy engine does not
 * compute, so it is deliberately NOT fabricated here (see the project's
 * "never invent a number" policy); the real, modeled net-margin
 * (`delta_ms`) is what actually answers "which is faster."
 */
export function SimulationComparisonScreen({
  loading,
  errorMessage,
  isEngineUnavailable,
  result,
}: SimulationComparisonScreenProps): React.JSX.Element {
  if (loading) {
    return (
      <Text style={styles.loadingText} testID="simulation-loading">
        Simulating…
      </Text>
    );
  }

  if (isEngineUnavailable) {
    // Spec v1.1 repair 14 / Section 29 "Strategy engine failure".
    return (
      <Text style={styles.errorText} testID="simulation-unavailable">
        STRATEGY MODEL UNAVAILABLE
      </Text>
    );
  }

  if (errorMessage || !result) {
    return (
      <Text style={styles.errorText} testID="simulation-error">
        {errorMessage ?? 'No simulation result available.'}
      </Text>
    );
  }

  const pitNowFaster = result.delta_ms >= 0;

  return (
    <View testID="simulation-content">
      <View style={styles.columnsRow}>
        <View style={styles.column} testID="simulation-column-pit-now">
          <Text style={styles.columnHeader}>PIT NOW</Text>
          <Text style={styles.columnLine}>Pit on lap {result.pit_now_lap}</Text>
          <Text style={styles.columnLine}>New tires: {result.compound_after_pit}</Text>
        </View>
        <View style={styles.column} testID="simulation-column-wait">
          <Text style={styles.columnHeader}>WAIT {result.wait_laps} LAPS</Text>
          <Text style={styles.columnLine}>Pit on lap {result.wait_lap}</Text>
          <Text style={styles.columnLine}>Comparison horizon: lap {result.comparison_horizon_lap}</Text>
        </View>
      </View>

      <View style={styles.marginRow} testID="simulation-margin">
        <Text style={styles.marginLabel}>NET MARGIN</Text>
        <Text style={[styles.marginValue, pitNowFaster ? styles.marginPositive : styles.marginNegative]}>
          {formatMargin(result.delta_ms)}
        </Text>
        <Text style={styles.marginNote}>
          {pitNowFaster ? 'Pitting now is projected faster.' : 'Waiting is projected faster.'}
        </Text>
      </View>

      <Text style={styles.confidenceText} testID="simulation-confidence">
        {Math.round(result.confidence.probability_ahead * 100)}% confidence across {result.confidence.trials}{' '}
        seeded trials (P10 {formatMargin(result.confidence.p10_margin_ms)}, P90{' '}
        {formatMargin(result.confidence.p90_margin_ms)})
      </Text>

      {result.assumptions.length > 0 && (
        <View style={styles.assumptionsContainer}>
          {result.assumptions.map((assumption, index) => (
            <Text key={index} style={styles.assumptionText}>
              • {assumption}
            </Text>
          ))}
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  loadingText: {
    color: '#7d8492',
    fontSize: 18,
  },
  errorText: {
    color: '#e05a5a',
    fontSize: 18,
    fontWeight: '700',
  },
  columnsRow: {
    flexDirection: 'row',
    marginBottom: 20,
  },
  column: {
    flex: 1,
    borderWidth: 1,
    borderColor: '#2a2e37',
    borderRadius: 8,
    padding: 16,
    marginRight: 12,
  },
  columnHeader: {
    color: '#4fd1c5',
    fontSize: 16,
    fontWeight: '800',
    letterSpacing: 1,
    marginBottom: 10,
  },
  columnLine: {
    color: '#e6e8eb',
    fontSize: 15,
    marginBottom: 4,
  },
  marginRow: {
    borderTopWidth: 1,
    borderTopColor: '#2a2e37',
    paddingTop: 16,
    marginBottom: 16,
  },
  marginLabel: {
    color: '#7d8492',
    fontSize: 14,
    fontWeight: '700',
    letterSpacing: 1,
    marginBottom: 4,
  },
  marginValue: {
    fontSize: 28,
    fontWeight: '800',
  },
  marginPositive: {
    color: '#4fd17e',
  },
  marginNegative: {
    color: '#e05a5a',
  },
  marginNote: {
    color: '#9aa2b1',
    fontSize: 14,
    marginTop: 4,
  },
  confidenceText: {
    color: '#9aa2b1',
    fontSize: 14,
    marginBottom: 12,
  },
  assumptionsContainer: {
    marginTop: 4,
  },
  assumptionText: {
    color: '#7d8492',
    fontSize: 13,
    marginBottom: 2,
  },
});
