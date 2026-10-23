import {act, renderHook, waitFor} from '@testing-library/react-native';
import {useInsightActions} from '../hooks/useInsightActions';
import {Alert, RaceState, UndercutResult} from '../api/types';

// Spec v1.1 repair 11: no real default token anywhere in
// src/config/authToken.ts — createWatch is a mutating (auth: true) call,
// so this test fixture supplies its own token via a module mock rather
// than depending on a bundled constant.
jest.mock('../config/authToken', () => ({
  LOCAL_DEV_AUTH_TOKEN: 'test-fixture-demo-token',
}));

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
  });
}

function makeRaceState(): RaceState {
  return {
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 4,
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

function makeUndercut(): UndercutResult {
  return {
    calculation_id: 'calc-1',
    model_version: 'v1',
    race_id: 'demo-race-001',
    run_id: 'run-a',
    source_state_version: 4,
    candidate: 'CAR_04',
    target: 'CAR_03',
    window: 'OPEN',
    expected_margin_ms: 800,
    comparison_horizon_lap: 29,
    candidate_tire_age_laps: 18,
    target_tire_age_laps: 22,
    traffic_classification: 'CLEAN',
    assumptions: [],
    confidence: {
      trials: 200,
      median_margin_ms: 800,
      p10_margin_ms: -200,
      p90_margin_ms: 1800,
      probability_ahead: 0.78,
      model_seed: 1,
    },
  };
}

function makeAlert(): Alert {
  return {
    alert_id: 'alert-1',
    evaluation_id: 'eval-1',
    watch_id: 'watch-1',
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 4,
    previous_status: 'CLOSED',
    new_status: 'OPEN',
    triggering_snapshot: makeRaceState(),
    deterministic_result: makeUndercut(),
    assumptions: [],
    model_version: 'v1',
    idempotency_key: 'run-a:watch-1:4:OPEN',
    created_at: new Date().toISOString(),
  };
}

describe('useInsightActions', () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('openWhy with a stored alert calls explain_alert and maps grounded_facts to evidence lines (spec 50)', async () => {
    global.fetch = jest.fn(() =>
      jsonResponse({
        explanation_id: 'exp-1',
        alert_id: 'alert-1',
        watch_id: 'watch-1',
        race_id: 'demo-race-001',
        run_id: 'run-a',
        narrative: 'Car 4 is now 2.0 seconds behind Car 3.',
        citations: [],
        grounded_facts: {gap_to_leader_ms: '2000'},
        llm_provider: 'bedrock',
        model_id: 'model-1',
        grounding_warnings: [],
        created_at: new Date().toISOString(),
        degraded: false,
      }),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useInsightActions('demo-race-001', 'CAR_04', 'CAR_03'));

    act(() => {
      result.current.openWhy(makeAlert());
    });

    expect(result.current.activeScreen).toBe('why');
    expect(result.current.why.loading).toBe(true);

    await waitFor(() => expect(result.current.why.loading).toBe(false));

    expect(result.current.why.data?.narrative).toBe('Car 4 is now 2.0 seconds behind Car 3.');
    expect(result.current.why.data?.degraded).toBe(false);
    expect(result.current.why.data?.evidence).toEqual([{label: 'GAP TO LEADER MS', value: '2000'}]);
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/v1/races/demo-race-001/alerts/alert-1/explain'),
      expect.anything(),
    );
  });

  it('openWhy with no stored alert falls back to queryAgent with a canned question', async () => {
    global.fetch = jest.fn(() =>
      jsonResponse({
        query: 'Why is the undercut window between CAR_04 and CAR_03?',
        status: 'SUPPORTED',
        confidence: 0.9,
        narrative: 'The window is open.',
        evidence: [{type: 'RACE_STATE', source_id: 'run-a:4', field: 'gap_to_leader', value: '2.0 s'}],
        created_at: new Date().toISOString(),
      }),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useInsightActions('demo-race-001', 'CAR_04', 'CAR_03'));

    act(() => {
      result.current.openWhy(null);
    });

    await waitFor(() => expect(result.current.why.loading).toBe(false));

    expect(result.current.why.data?.narrative).toBe('The window is open.');
    expect(result.current.why.data?.evidence).toEqual([{label: 'GAP TO LEADER', value: '2.0 s'}]);
    expect(global.fetch).toHaveBeenCalledWith(
      'http://10.0.2.2:8000/v1/agent/query',
      expect.objectContaining({method: 'POST'}),
    );
  });

  it('openSimulate marks isEngineUnavailable on strategy_engine_unavailable', async () => {
    global.fetch = jest.fn(() =>
      jsonResponse({error: 'strategy_engine_unavailable', message: 'STRATEGY MODEL UNAVAILABLE'}, 503),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useInsightActions('demo-race-001', 'CAR_04', 'CAR_03'));

    act(() => {
      result.current.openSimulate();
    });
    expect(result.current.activeScreen).toBe('simulate');

    await waitFor(() => expect(result.current.simulate.loading).toBe(false));
    expect(result.current.simulate.isEngineUnavailable).toBe(true);
    expect(result.current.simulate.errorMessage).toBe('STRATEGY MODEL UNAVAILABLE');
  });

  it('openWatch creates a watch and stores the result', async () => {
    global.fetch = jest.fn(() =>
      jsonResponse({
        watch_id: 'watch-1',
        race_id: 'demo-race-001',
        run_id: 'run-a',
        type: 'UNDERCUT',
        candidate: 'CAR_04',
        target: 'CAR_03',
        open_threshold: 0.6,
        close_threshold: 0.4,
        status: 'CLOSED',
        last_probability_ahead: 0.5,
        last_evaluated_state_version: 4,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useInsightActions('demo-race-001', 'CAR_04', 'CAR_03'));

    act(() => {
      result.current.openWatch();
    });
    expect(result.current.activeScreen).toBe('watch');

    await waitFor(() => expect(result.current.watch.loading).toBe(false));
    expect(result.current.watch.data?.watch_id).toBe('watch-1');
    expect(result.current.watch.errorMessage).toBeNull();
  });

  it('openSources looks up the exact Section 29 degraded text when available=false', async () => {
    global.fetch = jest.fn(() =>
      jsonResponse({query: 'What is an undercut?', found: false, available: false, evidence: []}),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useInsightActions('demo-race-001', 'CAR_04', 'CAR_03'));

    act(() => {
      result.current.openSources();
    });
    expect(result.current.activeScreen).toBe('sources');

    await waitFor(() => expect(result.current.sources.loading).toBe(false));
    expect(result.current.sources.data?.available).toBe(false);
    expect(result.current.sources.data?.found).toBe(false);
  });

  it('openWhatChanged maps explained_changes to evidence lines and closeScreen resets activeScreen', async () => {
    global.fetch = jest.fn(() =>
      jsonResponse({
        race_id: 'demo-race-001',
        run_id: 'run-a',
        from_state_version: 3,
        to_state_version: 4,
        changes: [],
        explained_changes: [
          {
            field: 'gap_to_leader_ms',
            driver_id: 'CAR_04',
            previous_value: '2500',
            current_value: '2000',
            significance: 0.9,
          },
        ],
        narrative: 'CAR_04 closed the gap.',
        created_at: new Date().toISOString(),
      }),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useInsightActions('demo-race-001', 'CAR_04', 'CAR_03'));

    act(() => {
      result.current.openWhatChanged();
    });
    expect(result.current.activeScreen).toBe('whatChanged');

    await waitFor(() => expect(result.current.whatChanged.loading).toBe(false));
    expect(result.current.whatChanged.data?.narrative).toBe('CAR_04 closed the gap.');
    expect(result.current.whatChanged.data?.evidence).toEqual([
      {label: 'CAR 04 GAP TO LEADER MS', value: '2500 -> 2000'},
    ]);

    act(() => {
      result.current.closeScreen();
    });
    expect(result.current.activeScreen).toBeNull();
  });
});
