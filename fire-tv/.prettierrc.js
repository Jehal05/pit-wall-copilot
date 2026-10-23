// Spec v1.1 repair 13: matches this codebase's actual established style
// (single quotes, no bracket spacing, trailing commas) rather than
// prettier's own defaults, which `@react-native/eslint-config`'s
// `plugin:prettier/recommended` would otherwise enforce and which would
// have required reformatting every existing file to pass lint.
module.exports = {
  arrowParens: 'avoid',
  bracketSameLine: true,
  bracketSpacing: false,
  singleQuote: true,
  trailingComma: 'all',
  printWidth: 110,
};
