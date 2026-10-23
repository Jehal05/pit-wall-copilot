// Spec v1.1 repair 13: `npm run lint` (package.json: `eslint . --ext
// .ts,.tsx`) previously had no config file to find, so it failed outright
// rather than linting anything — this file is that missing config.
// `@react-native/eslint-config` is already a devDependency (see
// package.json) and is the standard baseline for a React Native TS
// project; it already understands JSX, React Hooks rules, and TypeScript.
module.exports = {
  root: true,
  extends: '@react-native',
  rules: {
    // This codebase's own convention (see e.g. useRaceSocket.ts,
    // useDemoControls.ts) deliberately reads current option values via a
    // ref inside a long-lived effect rather than re-subscribing the
    // effect on every options-identity change, with an explicit
    // eslint-disable-next-line comment at each such call site — so the
    // rule itself stays on (catching genuine missing-dependency bugs
    // elsewhere) rather than being disabled project-wide.
  },
  ignorePatterns: ['node_modules/', 'android/', 'ios/', 'coverage/'],
};
