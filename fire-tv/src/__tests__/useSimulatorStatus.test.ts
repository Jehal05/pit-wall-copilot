import {renderHook, act, waitFor} from '@testing-library/react-native';
import {useSimulatorStatus} from '../hooks/useSimulatorStatus';
import {DemoStatus} from '../api/types';

function makeDemoStatus(overrides: Partial<DemoStatus> = {}): DemoStatus {
  return {
    status: 'STOPPED',
    race_id: 'demo-race-001',
    run_id: 'run-a',
    current_lap: 24,
    state_version: 0,
    ...overrides,
  };
}

describe('useSimulatorStatus', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('reflects the authoritative /v1/demo/status value, not a value inferred from lap', async () => {
    // Spec v1.1 repair 5: the OLD App.tsx inferred RUNNING purely from
    // `lap < 28`. This test uses a lap number that would have been
    // (wrongly) inferred as RUNNING under that old logic, while the
    // authoritative status endpoint says PAUSED — proving the hook uses
    // the real source of truth, not the lap.
    global.fetch = jest.fn(() =>
      Promise.resolve({
        ok: true,
        text: () => Promise.resolve(JSON.stringify(makeDemoStatus({status: 'PAUSED', current_lap: 26}))),
      }),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useSimulatorStatus());
    await waitFor(() => expect(result.current.status).toBe('PAUSED'));
  });

  it('picks up STOPPED -> RUNNING -> PAUSED -> COMPLETED transitions across polls', async () => {
    const sequence: DemoStatus['status'][] = ['STOPPED', 'RUNNING', 'PAUSED', 'COMPLETED'];
    let callIndex = 0;
    global.fetch = jest.fn(() => {
      const status = sequence[Math.min(callIndex, sequence.length - 1)];
      callIndex += 1;
      return Promise.resolve({
        ok: true,
        text: () => Promise.resolve(JSON.stringify(makeDemoStatus({status}))),
      });
    }) as unknown as typeof fetch;

    const {result} = renderHook(() => useSimulatorStatus());
    await waitFor(() => expect(result.current.status).toBe('STOPPED'));

    act(() => {
      jest.advanceTimersByTime(2000);
    });
    await waitFor(() => expect(result.current.status).toBe('RUNNING'));

    act(() => {
      jest.advanceTimersByTime(2000);
    });
    await waitFor(() => expect(result.current.status).toBe('PAUSED'));

    act(() => {
      jest.advanceTimersByTime(2000);
    });
    await waitFor(() => expect(result.current.status).toBe('COMPLETED'));
  });

  it('keeps the last known status on a transient poll failure rather than guessing', async () => {
    let callCount = 0;
    global.fetch = jest.fn(() => {
      callCount += 1;
      if (callCount === 1) {
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve(JSON.stringify(makeDemoStatus({status: 'RUNNING'}))),
        });
      }
      return Promise.reject(new Error('network down'));
    }) as unknown as typeof fetch;

    const {result} = renderHook(() => useSimulatorStatus());
    await waitFor(() => expect(result.current.status).toBe('RUNNING'));

    act(() => {
      jest.advanceTimersByTime(2000);
    });
    await waitFor(() => expect((global.fetch as jest.Mock).mock.calls.length).toBeGreaterThanOrEqual(2));

    expect(result.current.status).toBe('RUNNING'); // unchanged, not blanked out
  });

  it('applyAuthoritativeStatus overrides immediately (for a future WebSocket control event)', async () => {
    global.fetch = jest.fn(() =>
      Promise.resolve({
        ok: true,
        text: () => Promise.resolve(JSON.stringify(makeDemoStatus({status: 'STOPPED'}))),
      }),
    ) as unknown as typeof fetch;

    const {result} = renderHook(() => useSimulatorStatus());
    await waitFor(() => expect(result.current.status).toBe('STOPPED'));

    act(() => {
      result.current.applyAuthoritativeStatus('RUNNING');
    });
    expect(result.current.status).toBe('RUNNING');
  });
});
