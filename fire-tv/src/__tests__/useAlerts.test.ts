import {renderHook, act, waitFor} from '@testing-library/react-native';
import {useAlerts} from '../hooks/useAlerts';
import {Alert, RaceState} from '../api/types';

function makeRaceState(): RaceState {
  return {
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 5,
    lap: 27,
    total_laps: 58,
    race_time_ms: 0,
    track_state: 'GREEN',
    generated_at: new Date().toISOString(),
    scenario_id: 'scenario-demo-01',
    synthetic: true,
    drivers: [],
  };
}

function makeAlert(overrides: Partial<Alert> = {}): Alert {
  return {
    alert_id: 'alert-1',
    evaluation_id: 'eval-1',
    watch_id: 'watch-1',
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 5,
    previous_status: 'CLOSED',
    new_status: 'OPEN',
    triggering_snapshot: makeRaceState(),
    deterministic_result: {
      calculation_id: 'calc-1',
      model_version: 'v1',
      race_id: 'demo-race-001',
      run_id: 'run-a',
      source_state_version: 5,
      candidate: 'CAR_03',
      target: 'CAR_04',
      window: 'OPEN',
      expected_margin_ms: 1200,
      comparison_horizon_lap: 30,
      candidate_tire_age_laps: 22,
      target_tire_age_laps: 18,
      traffic_classification: 'CLEAR_AIR',
      assumptions: ['deterministic pace model'],
      confidence: {
        trials: 500,
        median_margin_ms: 1150,
        p10_margin_ms: 400,
        p90_margin_ms: 1900,
        probability_ahead: 0.82,
        model_seed: 42,
      },
    },
    assumptions: ['deterministic pace model'],
    model_version: 'v1',
    idempotency_key: 'idem-1',
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

describe('useAlerts', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('populates alerts from the first successful poll', async () => {
    const alerts = [makeAlert()];
    global.fetch = jest.fn(() =>
      Promise.resolve({
        ok: true,
        text: () => Promise.resolve(JSON.stringify(alerts)),
      }),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useAlerts('demo-race-001'));

    await waitFor(() => expect(result.current.alerts).toHaveLength(1));
    expect(result.current.alerts[0].alert_id).toBe('alert-1');
    expect(result.current.alertsErrorMessage).toBeNull();
  });

  it('replaces the whole list on each subsequent poll rather than merging', async () => {
    const firstPoll = [makeAlert({alert_id: 'alert-1'})];
    const secondPoll = [makeAlert({alert_id: 'alert-2'}), makeAlert({alert_id: 'alert-1'})];
    const responses = [firstPoll, secondPoll];

    global.fetch = jest.fn(() =>
      Promise.resolve({
        ok: true,
        text: () => Promise.resolve(JSON.stringify(responses.shift())),
      }),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useAlerts('demo-race-001'));
    await waitFor(() => expect(result.current.alerts).toHaveLength(1));

    act(() => {
      jest.advanceTimersByTime(4000);
    });

    await waitFor(() => expect(result.current.alerts).toHaveLength(2));
    expect(result.current.alerts.map(a => a.alert_id)).toEqual(['alert-2', 'alert-1']);
  });

  it('surfaces a quiet error message without clearing the last-known alerts on a failed poll', async () => {
    const firstPoll = [makeAlert()];
    let callCount = 0;
    global.fetch = jest.fn(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve(JSON.stringify(firstPoll)),
        });
      }
      return Promise.resolve({
        ok: false,
        status: 500,
        text: () => Promise.resolve(JSON.stringify({error: 'internal', message: 'boom'})),
      });
    }) as unknown as typeof fetch;

    const {result} = renderHook(() => useAlerts('demo-race-001'));
    await waitFor(() => expect(result.current.alerts).toHaveLength(1));

    act(() => {
      jest.advanceTimersByTime(4000);
    });

    await waitFor(() => expect(result.current.alertsErrorMessage).not.toBeNull());
    // The previously-fetched alert must still be shown; a failed poll is
    // best-effort and must not blank out the panel.
    expect(result.current.alerts).toHaveLength(1);
  });
});
