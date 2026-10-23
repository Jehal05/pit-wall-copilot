/**
 * Spec v1.1 repair 13: "configurable API/WS base URLs (no manual client.ts
 * edits)". `API_BASE_URL` is inlined from the `.env`-backed `@env` virtual
 * module by the react-native-dotenv Babel plugin (babel.config.js) at
 * transform time — this is a compile-time text substitution, not a runtime
 * `require('@env')`, so it can't be re-mocked per-test the way a normal
 * module import can. This test instead reads the same `.env` file the
 * Babel plugin reads and asserts `client.ts`'s exported value matches it,
 * which is the practical way to verify the wiring end to end: if `.env`
 * changes, this test's own expectation is derived from it, so it can never
 * silently pass against a stale hardcoded literal.
 *
 * The hardcoded fallback inside `client.ts` (used only when `.env` defines
 * no `API_BASE_URL` at all) is intentionally not exercised here — with the
 * plugin's static inlining there is no way to make `@env` "have no value"
 * for a single test without editing the real `.env` file out from under
 * the whole suite. It's documented in client.ts and .env.example instead.
 */
import * as fs from 'fs';
import * as path from 'path';
import {API_BASE_URL} from '../api/client';

function readEnvApiBaseUrl(): string | undefined {
  const envPath = path.resolve(__dirname, '../../.env');
  if (!fs.existsSync(envPath)) {
    return undefined;
  }
  const match = fs.readFileSync(envPath, 'utf8').match(/^API_BASE_URL=(.*)$/m);
  return match?.[1]?.trim();
}

describe('API_BASE_URL', () => {
  it('is a non-empty http(s) URL', () => {
    expect(API_BASE_URL).toMatch(/^https?:\/\//);
  });

  it('matches the value configured in .env, when .env defines one', () => {
    const envValue = readEnvApiBaseUrl();
    if (envValue) {
      expect(API_BASE_URL).toBe(envValue);
    } else {
      // No .env present (e.g. a fresh checkout before copying
      // .env.example) — client.ts's own hardcoded fallback applies.
      expect(API_BASE_URL).toBe('http://10.0.2.2:8000');
    }
  });
});
