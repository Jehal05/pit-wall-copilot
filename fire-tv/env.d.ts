// Spec v1.1 repair 13: type declaration for the '@env' virtual module that
// the react-native-dotenv Babel plugin (babel.config.js) resolves at bundle
// time from `.env` (see `.env.example`). TypeScript has no way to see a
// Babel-only module resolution on its own, so without this file every
// `import ... from '@env'` would fail type-checking with "Cannot find
// module '@env'".
declare module '@env' {
  /** Backend base URL, e.g. "http://10.0.2.2:8000" for the Android
   * emulator. See .env.example for per-target values (physical device/Fire
   * TV, iOS simulator). The WebSocket base URL is derived from this at
   * runtime (src/hooks/useRaceSocket.ts) rather than configured separately.
   */
  export const API_BASE_URL: string | undefined;
}
