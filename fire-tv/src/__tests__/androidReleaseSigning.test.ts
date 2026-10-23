import fs from 'fs';
import path from 'path';

/**
 * Static (non-Gradle) proof that the release build type never falls
 * back to the debug signing config. This test reads the actual
 * `build.gradle` source rather than running Gradle, so it works in
 * any environment (no Android SDK / Gradle network access required)
 * — see spec v1.1 pass 3, correction 1.
 */
describe('android release signing configuration', () => {
  const buildGradlePath = path.resolve(__dirname, '../../android/app/build.gradle');
  const buildGradleSource = fs.readFileSync(buildGradlePath, 'utf8');

  function extractBlock(source: string, blockHeaderPattern: RegExp): string {
    const match = blockHeaderPattern.exec(source);
    if (!match) {
      throw new Error(`Could not find block matching ${blockHeaderPattern}`);
    }
    const startIndex = match.index + match[0].length - 1; // position of the opening brace
    let depth = 0;
    for (let i = startIndex; i < source.length; i += 1) {
      if (source[i] === '{') {
        depth += 1;
      }
      if (source[i] === '}') {
        depth -= 1;
        if (depth === 0) {
          return source.slice(startIndex, i + 1);
        }
      }
    }
    throw new Error('Unbalanced braces while extracting block');
  }

  it('the release build type block does not reference signingConfigs.debug', () => {
    const buildTypesBlock = extractBlock(buildGradleSource, /buildTypes\s*\{/);
    const releaseBlock = extractBlock(buildTypesBlock, /release\s*\{/);
    expect(releaseBlock).not.toMatch(/signingConfigs\.debug/);
  });

  it('the debug build type block still uses signingConfigs.debug', () => {
    const buildTypesBlock = extractBlock(buildGradleSource, /buildTypes\s*\{/);
    const debugBlock = extractBlock(buildTypesBlock, /debug\s*\{/);
    expect(debugBlock).toMatch(/signingConfigs\.debug/);
  });

  it('release signing is only attached when fully configured from properties/env/Gradle properties, never hardcoded', () => {
    expect(buildGradleSource).toMatch(/releaseSigningIsFullyConfigured/);
    expect(buildGradleSource).toMatch(/PIT_WALL_RELEASE_STORE_FILE/);
    expect(buildGradleSource).toMatch(/PIT_WALL_RELEASE_STORE_PASSWORD/);
    expect(buildGradleSource).toMatch(/PIT_WALL_RELEASE_KEY_ALIAS/);
    expect(buildGradleSource).toMatch(/PIT_WALL_RELEASE_KEY_PASSWORD/);
    // No hardcoded password/alias literals anywhere outside the debug config.
    expect(buildGradleSource).not.toMatch(/storePassword\s+'(?!android')/);
  });

  it('release.keystore.properties is excluded from version control', () => {
    const gitignorePath = path.resolve(__dirname, '../../../.gitignore');
    const gitignore = fs.readFileSync(gitignorePath, 'utf8');
    expect(gitignore).toMatch(/release\.keystore\.properties/);
    expect(gitignore).toMatch(/\*\.jks/);
    expect(gitignore).toMatch(/\*\.keystore/);
    expect(gitignore).toMatch(/!fire-tv\/android\/app\/debug\.keystore/);
  });

  it('no real release keystore or properties file is committed in this checkout', () => {
    const appDir = path.resolve(__dirname, '../../android/app');
    const entries = fs.readdirSync(appDir);
    const suspicious = entries.filter(
      name => (name.endsWith('.jks') || name.endsWith('.keystore')) && name !== 'debug.keystore',
    );
    expect(suspicious).toEqual([]);
    expect(entries).not.toContain('release.keystore.properties');
  });

  // -----------------------------------------------------------------
  // Spec v1.1 pass 4, mandatory correction 9: "fail the production
  // release pipeline if signing credentials are missing when a signed
  // deliverable is required — do NOT silently present an unsigned APK as
  // finished." These are still static-source assertions (no Gradle/JVM
  // execution — see this file's own top-of-file docstring for why), but
  // together they pin down every piece the runtime gate depends on: the
  // opt-in flag name, that it defaults to off (so local unsigned smoke
  // builds keep working), and that it actually throws rather than only
  // warning when signing is missing.
  // -----------------------------------------------------------------
  describe('fail-closed signed-release gate (spec v1.1 pass 4, correction 9)', () => {
    it('reads an opt-in PIT_WALL_REQUIRE_SIGNED_RELEASE flag, defaulting to false', () => {
      expect(buildGradleSource).toMatch(/PIT_WALL_REQUIRE_SIGNED_RELEASE/);
      expect(buildGradleSource).toMatch(/requireSignedRelease\s*=[\s\S]*?'false'/);
    });

    it('the gate inspects the requested task graph for an assemble/bundle *Release task', () => {
      expect(buildGradleSource).toMatch(/gradle\.taskGraph\.whenReady/);
      expect(buildGradleSource).toMatch(/startsWith\('assemble'\)/);
      expect(buildGradleSource).toMatch(/startsWith\('bundle'\)/);
      expect(buildGradleSource).toMatch(/toLowerCase\(\)\.contains\('release'\)/);
    });

    it('the gate throws a GradleException — a hard failure, not merely another warning', () => {
      const requireSignedReleaseBlock = extractBlock(buildGradleSource, /if\s*\(requireSignedRelease\)\s*\{/);
      expect(requireSignedReleaseBlock).toMatch(/throw new GradleException/);
      // Must be conditioned on the SAME fully-configured check the
      // ordinary (non-fatal) warning above uses — a gate that checked a
      // different, looser condition could pass while still shipping an
      // unsigned artifact.
      expect(requireSignedReleaseBlock).toMatch(/!releaseSigningIsFullyConfigured/);
    });

    it('the fatal check only fires for a release artifact task, not for an unrelated task like clean or test', () => {
      const requireSignedReleaseBlock = extractBlock(buildGradleSource, /if\s*\(requireSignedRelease\)\s*\{/);
      expect(requireSignedReleaseBlock).toMatch(
        /releaseArtifactTaskRequested\s*&&\s*!releaseSigningIsFullyConfigured/,
      );
    });
  });
});
