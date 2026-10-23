import React from 'react';
import {StyleSheet, Text, View} from 'react-native';
import {DriverState} from '../api/types';

interface StandingsTableProps {
  drivers: DriverState[];
}

function formatGap(ms: number): string {
  return `+${(ms / 1000).toFixed(1)}s`;
}

const COMPOUND_LABEL: Record<string, string> = {
  SOFT: 'S',
  MEDIUM: 'M',
  HARD: 'H',
};

export function StandingsTable({drivers}: StandingsTableProps): React.JSX.Element {
  const sorted = [...drivers].sort((a, b) => a.position - b.position);

  return (
    <View style={styles.container} testID="standings-table">
      <View style={styles.headerRow}>
        <Text style={[styles.headerCell, styles.posCol]}>POS</Text>
        <Text style={[styles.headerCell, styles.nameCol]}>DRIVER</Text>
        <Text style={[styles.headerCell, styles.teamCol]}>TEAM</Text>
        <Text style={[styles.headerCell, styles.compoundCol]}>TYRE</Text>
        <Text style={[styles.headerCell, styles.gapCol]}>GAP AHEAD</Text>
      </View>
      {sorted.map(driver => (
        <View key={driver.driver_id} style={styles.row} testID={`standings-row-${driver.driver_id}`}>
          <Text style={[styles.cell, styles.posCol, styles.posText]}>{driver.position}</Text>
          <Text style={[styles.cell, styles.nameCol]}>{driver.display_name}</Text>
          <Text style={[styles.cell, styles.teamCol, styles.dim]}>{driver.team_name}</Text>
          <Text style={[styles.cell, styles.compoundCol]}>
            {COMPOUND_LABEL[driver.compound] ?? driver.compound} {driver.tire_age_laps}L
          </Text>
          <Text style={[styles.cell, styles.gapCol]}>{formatGap(driver.gap_to_ahead_ms)}</Text>
        </View>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    borderWidth: 1,
    borderColor: '#2a2e37',
    borderRadius: 8,
    overflow: 'hidden',
  },
  headerRow: {
    flexDirection: 'row',
    backgroundColor: '#171a20',
    paddingVertical: 10,
    paddingHorizontal: 12,
  },
  headerCell: {
    color: '#7d8492',
    fontSize: 14,
    fontWeight: '700',
    letterSpacing: 1,
  },
  row: {
    flexDirection: 'row',
    paddingVertical: 14,
    paddingHorizontal: 12,
    borderTopWidth: 1,
    borderTopColor: '#22262e',
    backgroundColor: '#12151a',
  },
  cell: {
    color: '#e6e8eb',
    fontSize: 20,
  },
  posCol: {width: 50},
  nameCol: {flex: 2},
  teamCol: {flex: 2},
  compoundCol: {width: 90},
  gapCol: {width: 120, textAlign: 'right'},
  posText: {fontWeight: '700', color: '#4fd1c5'},
  dim: {color: '#9aa2b1'},
});
