import React from 'react';
import {StyleSheet, Text, View} from 'react-native';
import {Alert} from '../api/types';

interface AlertsPanelProps {
  alerts: Alert[];
  errorMessage: string | null;
}

function formatMargin(ms: number): string {
  const sign = ms >= 0 ? '+' : '-';
  return `${sign}${(Math.abs(ms) / 1000).toFixed(1)}s`;
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  } catch {
    return iso;
  }
}

const STATUS_LABEL: Record<string, string> = {
  OPEN: 'WINDOW OPEN',
  CLOSED: 'WINDOW CLOSED',
};

const STATUS_COLOR: Record<string, string> = {
  OPEN: '#4fd17e',
  CLOSED: '#9aa2b1',
};

/**
 * Renders the alerts fed by useAlerts (GET /v1/races/{race_id}/alerts,
 * spec 49). Newest-first, matching the order the backend already returns
 * (see that route's docstring) — this component does not re-sort.
 *
 * Each card shows the watch-state transition (OPEN/CLOSED) plus enough of
 * the alert's own immutable deterministic_result (candidate vs target,
 * expected margin, confidence) to answer "what changed and by how much"
 * at a glance, without a "why now?" drill-down — that deeper narrative is
 * what /explain is for (Gate 3), which is out of scope for this glanceable
 * TV panel.
 */
export function AlertsPanel({alerts, errorMessage}: AlertsPanelProps): React.JSX.Element {
  return (
    <View style={styles.container} testID="alerts-panel">
      <View style={styles.headerRow}>
        <Text style={styles.header}>ALERTS</Text>
        {alerts.length > 0 && (
          <Text style={styles.countBadge} testID="alerts-count">
            {alerts.length}
          </Text>
        )}
      </View>

      {errorMessage && (
        <Text style={styles.errorText} testID="alerts-error">
          {errorMessage}
        </Text>
      )}

      {alerts.length === 0 && !errorMessage ? (
        <Text style={styles.emptyText} testID="alerts-empty">
          No alerts yet — open a strategy watch to get notified when it changes.
        </Text>
      ) : (
        alerts.map(alert => {
          const result = alert.deterministic_result;
          return (
            <View key={alert.alert_id} style={styles.card} testID={`alert-card-${alert.alert_id}`}>
              <View style={styles.cardHeaderRow}>
                <Text style={[styles.statusText, {color: STATUS_COLOR[alert.new_status] ?? '#e6e8eb'}]}>
                  {STATUS_LABEL[alert.new_status] ?? alert.new_status}
                </Text>
                <Text style={styles.timeText}>{formatTime(alert.created_at)}</Text>
              </View>
              <Text style={styles.driversText}>
                {result.candidate} vs {result.target}
              </Text>
              <Text style={styles.marginText}>
                Expected margin {formatMargin(result.expected_margin_ms)} · lap{' '}
                {result.comparison_horizon_lap} · {Math.round(result.confidence.probability_ahead * 100)}%
                confidence
              </Text>
            </View>
          );
        })
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    marginTop: 24,
  },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    marginBottom: 12,
  },
  header: {
    color: '#7d8492',
    fontSize: 16,
    fontWeight: '700',
    letterSpacing: 2,
  },
  countBadge: {
    color: '#0b0d11',
    backgroundColor: '#4fd1c5',
    fontSize: 12,
    fontWeight: '800',
    marginLeft: 10,
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 10,
    overflow: 'hidden',
  },
  errorText: {
    color: '#e05a5a',
    fontSize: 14,
    marginBottom: 8,
  },
  emptyText: {
    color: '#7d8492',
    fontSize: 16,
  },
  card: {
    borderWidth: 1,
    borderColor: '#2a2e37',
    borderRadius: 8,
    backgroundColor: '#12151a',
    padding: 14,
    marginBottom: 10,
  },
  cardHeaderRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    marginBottom: 6,
  },
  statusText: {
    fontSize: 15,
    fontWeight: '800',
    letterSpacing: 1,
  },
  timeText: {
    color: '#7d8492',
    fontSize: 13,
  },
  driversText: {
    color: '#e6e8eb',
    fontSize: 18,
    fontWeight: '700',
    marginBottom: 4,
  },
  marginText: {
    color: '#9aa2b1',
    fontSize: 14,
  },
});
