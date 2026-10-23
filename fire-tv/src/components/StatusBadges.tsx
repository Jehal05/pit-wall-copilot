import React from 'react';
import {StyleSheet, Text, View} from 'react-native';
import {ConnectionStatus} from '../hooks/useRaceState';

const CONNECTION_LABEL: Record<ConnectionStatus, string> = {
  loading: 'CONNECTING…',
  connected: 'LIVE',
  disconnected: 'DISCONNECTED',
  error: 'API ERROR',
};

const CONNECTION_COLOR: Record<ConnectionStatus, string> = {
  loading: '#d8a94f',
  connected: '#4fd17e',
  disconnected: '#e05a5a',
  error: '#e05a5a',
};

export function ConnectionBadge({status}: {status: ConnectionStatus}): React.JSX.Element {
  return (
    <View style={[styles.badge, {borderColor: CONNECTION_COLOR[status]}]} testID="connection-badge">
      <View style={[styles.dot, {backgroundColor: CONNECTION_COLOR[status]}]} />
      <Text style={[styles.text, {color: CONNECTION_COLOR[status]}]}>{CONNECTION_LABEL[status]}</Text>
    </View>
  );
}

const SIM_LABEL: Record<string, string> = {
  STOPPED: 'STOPPED',
  RUNNING: 'LIVE',
  PAUSED: 'PAUSED',
  COMPLETED: 'COMPLETED',
};

export function SimulatorBadge({status}: {status: string}): React.JSX.Element {
  return (
    <View style={styles.badge} testID="simulator-badge">
      <Text style={styles.text}>{SIM_LABEL[status] ?? status}</Text>
    </View>
  );
}

/**
 * Spec v1.1 Section 29 "Race data stale" / repair 14: "If current_time -
 * generated_at > 5 seconds, display DATA STALE." This is the literal
 * display text the spec requires — not a paraphrase — rendered whenever
 * `useRaceSocket`'s `isStale` flag is true (see its own docstring for the
 * matching 5000ms threshold). Deliberately a separate, more urgent-looking
 * badge from `ConnectionBadge`: staleness can occur even while the socket
 * itself reports "open" (a slow/stalled backend still technically
 * connected), so it's not just a restatement of connection status.
 */
export function StaleDataBadge(): React.JSX.Element {
  return (
    <View style={[styles.badge, styles.staleBadge]} testID="stale-data-badge">
      <View style={[styles.dot, styles.staleDot]} />
      <Text style={[styles.text, styles.staleText]}>DATA STALE</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    flexDirection: 'row',
    alignItems: 'center',
    borderWidth: 1,
    borderColor: '#3a3f4b',
    borderRadius: 20,
    paddingVertical: 6,
    paddingHorizontal: 14,
    marginLeft: 10,
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    marginRight: 8,
  },
  text: {
    color: '#e6e8eb',
    fontSize: 14,
    fontWeight: '700',
    letterSpacing: 1,
  },
  staleBadge: {
    borderColor: '#d8a94f',
    backgroundColor: '#332a14',
  },
  staleDot: {
    backgroundColor: '#d8a94f',
  },
  staleText: {
    color: '#d8a94f',
  },
});
