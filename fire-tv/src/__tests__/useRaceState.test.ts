import {renderHook, act, waitFor} from '@testing-library/react-native';
import {useRaceState} from '../hooks/useRaceState';
import {RaceState} from '../api/types';

function makeState(overrides: Partial<RaceState>): RaceState {
  return {
    race_id: 'demo-race-001',
    run_id: 'run-a',
    state_version: 0,
    lap: 24,
    total_laps: 58,
    race_time_ms: 0,
    track_state: 'GREEN',
    generated_at: new Date().toISOString(),
    scenario_id: 'scenario-demo-01',
    synthetic: true,
    drivers: [],
    ...overrides,
  };
}

describe('useRaceState stale-response protection', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('never lets a lower state_version replace a newer displayed state for the same run', async () => {
    const newer = makeState({state_version: 5, lap: 27});
    const staleOutOfOrder = makeState({state_version: 2, lap: 25});

    const responses = [newer, staleOutOfOrder];
    global.fetch = jest.fn(() =>
      Promise.resolve({
        ok: true,
        text: () => Promise.resolve(JSON.stringify(responses.shift())),
      }),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useRaceState('demo-race-001'));

    await waitFor(() => expect(result.current.raceState?.state_version).toBe(5));

    act(() => {
      jest.advanceTimersByTime(2000);
    });

    await waitFor(() => expect((global.fetch as jest.Mock).mock.calls.length).toBeGreaterThanOrEqual(2));

    // The stale (lower state_version, same run_id) response must never win.
    expect(result.current.raceState?.state_version).toBe(5);
    expect(result.current.raceState?.lap).toBe(27);
  });

  it(
    'rejects an ordinary poll response for a different run_id — only establishActiveRun may switch runs ' +
      '(spec v1.1 repair 4)',
    async () => {
      // A plain poll response with a different run_id must NOT switch the
      // displayed run on its own — see the hook's own doc-comment on why
      // the OLD "any different run_id wins" behavior was the bug, not the
      // fix. Only an explicit `establishActiveRun` call (wired to the
      // reset flow / a RACE_RESET event) may do that.
      const runA = makeState({run_id: 'run-a', state_version: 5, lap: 27});
      const strayRunB = makeState({run_id: 'run-b', state_version: 0, lap: 24});

      const responses = [runA, strayRunB];
      global.fetch = jest.fn(() =>
        Promise.resolve({
          ok: true,
          text: () => Promise.resolve(JSON.stringify(responses.shift())),
        }),
      ) as unknown as typeof fetch;

      const {result} = renderHook(() => useRaceState('demo-race-001'));
      await waitFor(() => expect(result.current.raceState?.run_id).toBe('run-a'));

      act(() => {
        jest.advanceTimersByTime(2000);
      });

      await waitFor(() => expect((global.fetch as jest.Mock).mock.calls.length).toBeGreaterThanOrEqual(2));

      // run-b never became active from a poll response alone — run-a
      // stays displayed.
      expect(result.current.raceState?.run_id).toBe('run-a');
      expect(result.current.raceState?.state_version).toBe(5);
    },
  );

  it('establishActiveRun switches the active run immediately, and the new run than accepts only greater versions', async () => {
    global.fetch = jest.fn(() =>
      Promise.resolve({
        ok: true,
        text: () => Promise.resolve(JSON.stringify(makeState({run_id: 'run-a', state_version: 5}))),
      }),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useRaceState('demo-race-001'));
    await waitFor(() => expect(result.current.raceState?.run_id).toBe('run-a'));

    const runB = makeState({run_id: 'run-b', state_version: 0, lap: 24});
    act(() => {
      result.current.establishActiveRun(runB);
    });
    expect(result.current.raceState?.run_id).toBe('run-b');
    expect(result.current.raceState?.state_version).toBe(0);
  });

  it(
    'reproduces the exact spec sequence: run-a/v5 accepted, reset establishes run-b/v0, ' +
      'a delayed run-a/v6 response is rejected, run-b remains displayed',
    async () => {
      global.fetch = jest.fn(() =>
        Promise.resolve({
          ok: true,
          text: () => Promise.resolve(JSON.stringify(makeState({run_id: 'run-a', state_version: 5}))),
        }),
      ) as unknown as typeof fetch;

      const {result} = renderHook(() => useRaceState('demo-race-001'));
      await waitFor(() => expect(result.current.raceState?.run_id).toBe('run-a'));
      expect(result.current.raceState?.state_version).toBe(5);

      // reset establishes run-b/version-0
      const runB0 = makeState({run_id: 'run-b', state_version: 0, lap: 24});
      act(() => {
        result.current.establishActiveRun(runB0);
      });
      expect(result.current.raceState?.run_id).toBe('run-b');
      expect(result.current.raceState?.state_version).toBe(0);

      // delayed run-a/version-6 arrives (simulated as a direct poll
      // response, exactly what a straggling in-flight request from before
      // the reset would deliver)
      global.fetch = jest.fn(() =>
        Promise.resolve({
          ok: true,
          text: () => Promise.resolve(JSON.stringify(makeState({run_id: 'run-a', state_version: 6}))),
        }),
      ) as unknown as typeof fetch;
      await act(async () => {
        await result.current.refreshNow();
      });

      // run-a response is rejected; run-b remains displayed
      expect(result.current.raceState?.run_id).toBe('run-b');
      expect(result.current.raceState?.state_version).toBe(0);
    },
  );
});
