module.exports = {
  presets: ['module:@react-native/babel-preset'],
  plugins: [
    // Spec v1.1 repair 13: "configurable API/WS base URLs (no manual
    // client.ts edits)". react-native-dotenv is a pure Babel transform (no
    // native module/linking involved, unlike e.g. react-native-config), so
    // it works today even though no native android/ios project exists yet.
    // It inlines values from a git-ignored `.env` file (see `.env.example`
    // for the documented keys) as `import {API_BASE_URL} from '@env'` at
    // bundle time. Metro/Jest must be restarted after editing `.env` since
    // this is a build-time inline, not a runtime read.
    [
      'module:react-native-dotenv',
      {
        moduleName: '@env',
        path: '.env',
        safe: false,
        allowUndefined: true,
      },
    ],
  ],
};
