/**
 * Jest manual mock for `react-native-keychain` (a top-level `__mocks__/`
 * entry for a `node_modules` package is auto-applied by Jest to every
 * test — no per-file `jest.mock('react-native-keychain')` call needed).
 *
 * The real package's Android implementation talks to `NativeModules.
 * RNKeychainManager`, which does not exist in the Node/jsdom environment
 * this suite runs in (there is no real Android Keystore here — see this
 * repo's own honest NOT RUN notes for anything that needs a real
 * device). This mock is a simple in-memory stand-in with the same
 * `setGenericPassword`/`getGenericPassword`/`resetGenericPassword` shape
 * `src/config/secureTokenStore.ts` calls, scoped per `service` the same
 * way the real keychain namespaces entries — enough for
 * `secureTokenStore.test.ts` and every hook/component test that mounts
 * `useDeviceAuth` (which calls these at startup) to run without touching
 * real platform storage.
 */
let store = {};

function setGenericPassword(username, password, options) {
  const service = options && options.service ? options.service : 'default';
  store[service] = {username, password};
  return Promise.resolve({service, storage: 'mock'});
}

function getGenericPassword(options) {
  const service = options && options.service ? options.service : 'default';
  const entry = store[service];
  if (!entry) {
    return Promise.resolve(false);
  }
  return Promise.resolve({service, username: entry.username, password: entry.password, storage: 'mock'});
}

function resetGenericPassword(options) {
  const service = options && options.service ? options.service : 'default';
  delete store[service];
  return Promise.resolve(true);
}

function __reset() {
  store = {};
}

module.exports = {
  setGenericPassword,
  getGenericPassword,
  resetGenericPassword,
  __reset,
};
