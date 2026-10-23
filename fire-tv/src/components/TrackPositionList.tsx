import React from 'react';
import {StyleSheet, Text, View} from 'react-native';
import {DriverState} from '../api/types';

interface TrackPositionListProps {
  drivers: DriverState[];
}

/**
 * A simplified stand-in for spec 10.1's "TRACK MAP" panel (the mockup
 * shows car numbers as dots — "● 3 / ● 4 / ● 7"). RaceState carries no
 * track geometry or x/y car coordinates (see app/domain/models.py), so a
 * literal 2D map would have to invent positions this backend never
 * computed — this instead renders the same "who's running where, in
 * order" information honestly, as a vertical running-order list of
 * position dots, rather than fabricating map coordinates.
 */
export function TrackPositionList({drivers}: TrackPositionListProps): React.JSX.Element {
  const sorted = [...drivers].sort((a, b) => a.position - b.position);
  return (
    <View style={styles.container} testID="track-position-list">
      <Text style={styles.header}>TRACK POSITIONS</Text>
      {sorted.map(driver => (
        <View key={driver.driver_id} style={styles.row} testID={`track-position-${driver.driver_id}`}>
          <View style={styles.dot} />
          <Text style={styles.carNumber}>{driver.driver_id.replace('CAR_0', '').replace('CAR_', '')}</Text>
          <Text style={styles.driverName}>{driver.display_name}</Text>
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
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    marginBottom: 10,
  },
  dot: {
    width: 10,
    height: 10,
    borderRadius: 5,
    backgroundColor: '#4fd1c5',
    marginRight: 10,
  },
  carNumber: {
    color: '#f2f4f6',
    fontSize: 18,
    fontWeight: '800',
    width: 36,
  },
  driverName: {
    color: '#9aa2b1',
    fontSize: 15,
  },
});
