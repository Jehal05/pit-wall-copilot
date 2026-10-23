/**
 * Local-development-only demo access token override.
 *
 * Spec v1.1 repair 11: "No production credential may be stored in
 * JavaScript, Gradle files, source code, .env.example, tests or
 * documentation" and "Local development [must remain] possible through an
 * explicitly enabled local-demo authentication mode."
 *
 * This file is ALWAYS tracked in git and ALWAYS ships with `null` below.
 * There is deliberately no bundled default token anywhere in this
 * repository (see api/client.ts, which used to export a hardcoded
 * DEMO_ACCESS_TOKEN constant — that export has been removed).
 *
 * To develop locally against a backend running with LOCAL_DEMO_AUTH_MODE=
 * true, that backend prints a freshly generated token to its own stderr
 * at startup. Paste that value in below, in your own local working copy:
 *
 *   export const LOCAL_DEV_AUTH_TOKEN: string | null = 'the-printed-token';
 *
 * Do NOT commit that edit. This file is not git-ignored (a git-ignored
 * file that some developers have and others don't would break Metro's
 * static `require()` resolution for everyone else — see the commit that
 * introduced this file for why that approach was rejected), so treat your
 * local edit the same way you'd treat any other uncommitted local change:
 * keep it out of your commits and PRs.
 *
 * This value is only ever read from `__DEV__` code paths (see
 * `api/client.ts`'s `resolveAuthToken()`), so it has no effect at all in a
 * release/production bundle even if a real value were accidentally left
 * here — `__DEV__` is compiled out to `false` in release builds by React
 * Native's own bundler, and the release path never consults this file.
 */
export const LOCAL_DEV_AUTH_TOKEN: string | null = null;
