import {act, renderHook, waitFor} from '@testing-library/react-native';
import {useStrategyBattle} from '../hooks/useStrategyBattle';
import {UndercutResult} from '../api/types';

function makeUndercut(overrides: Partial<UndercutResult> = {}): UndercutResult {
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
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
  });
}

describe('useStrategyBattle', () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('does nothing while lap is undefined (race not yet loaded)', () => {
    global.fetch = jest.fn() as unknown as typeof fetch;
    const {result} = renderHook(() => useStrategyBattle('demo-race-001', 'CAR_04', 'CAR_03', undefined));
    expect(result.current.result).toBeNull();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('fetches calculate_undercut and returns the result once the lap is known', async () => {
    global.fetch = jest.fn(() => jsonResponse(makeUndercut())) as unknown as typeof fetch;
    const {result} = renderHook(() => useStrategyBattle('demo-race-001', 'CAR_04', 'CAR_03', 27));

    await waitFor(() => expect(result.current.result).not.toBeNull());

    expect(result.current.result?.window).toBe('OPEN');
    expect(result.current.isEngineUnavailable).toBe(false);
    expect(result.current.errorMessage).toBeNull();
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/v1/races/demo-race-001/strategy/undercut'),
      expect.objectContaining({method: 'POST'}),
    );
  });

  it('re-fetches when the lap changes', async () => {
    global.fetch = jest.fn(() => jsonResponse(makeUndercut())) as unknown as typeof fetch;
    const {result, rerender} = renderHook(
      ({lap}: {lap: number}) => useStrategyBattle('demo-race-001', 'CAR_04', 'CAR_03', lap),
      {initialProps: {lap: 27}},
    );
    await waitFor(() => expect(result.current.result).not.toBeNull());
    expect((global.fetch as jest.Mock).mock.calls.length).toBe(1);

    rerender({lap: 28});
    await waitFor(() => expect((global.fetch as jest.Mock).mock.calls.length).toBe(2));
  });

  it('sets isEngineUnavailable and the exact STRATEGY MODEL UNAVAILABLE message on a strategy_engine_unavailable error (repair 14)', async () => {
    global.fetch = jest.fn(() =>
      jsonResponse({error: 'strategy_engine_unavailable', message: 'STRATEGY MODEL UNAVAILABLE'}, 503),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useStrategyBattle('demo-race-001', 'CAR_04', 'CAR_03', 27));

    await waitFor(() => expect(result.current.isEngineUnavailable).toBe(true));
    expect(result.current.errorMessage).toBe('STRATEGY MODEL UNAVAILABLE');
    expect(result.current.result).toBeNull();
  });

  it('sets a generic errorMessage (not isEngineUnavailable) for an unrelated failure', async () => {
    global.fetch = jest.fn(() =>
      jsonResponse({error: 'unknown_driver', message: 'Unknown driver_id'}, 404),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useStrategyBattle('demo-race-001', 'CAR_04', 'CAR_03', 27));

    await waitFor(() => expect(result.current.errorMessage).not.toBeNull());
    expect(result.current.isEngineUnavailable).toBe(false);
    expect(result.current.errorMessage).toBe('Unknown driver_id');
  });

  it('refreshNow triggers another fetch without a lap change', async () => {
    global.fetch = jest.fn(() => jsonResponse(makeUndercut())) as unknown as typeof fetch;
    const {result} = renderHook(() => useStrategyBattle('demo-race-001', 'CAR_04', 'CAR_03', 27));
    await waitFor(() => expect(result.current.result).not.toBeNull());
    expect((global.fetch as jest.Mock).mock.calls.length).toBe(1);

    act(() => {
      result.current.refreshNow();
    });

    await waitFor(() => expect((global.fetch as jest.Mock).mock.calls.length).toBe(2));
  });
});
