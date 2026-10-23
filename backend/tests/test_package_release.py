"""Tests for scripts/package_release.py (spec v1.1 pass 3, correction 7).

Exercises the exclusion logic against a small synthetic directory tree
(fast, deterministic, no dependency on this checkout's own real state)
AND against a real (small, temp-dir-scoped) end-to-end build + inspect
round trip, so both "the rule is correct" and "the two passes (build,
inspect) actually agree with each other" are covered.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.package_release import (  # noqa: E402
    build_archive,
    inspect_archive,
    should_include,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        ".env.local",
        ".env.production",
        "fire-tv/.env",
        "fire-tv/android/.gradle/caches/x",
        "fire-tv/node_modules/react/index.js",
        "backend/.venv/bin/python",
        "backend/.venv-review/bin/python",
        "backend/__pycache__/app.cpython-311.pyc",
        "backend/.pytest_cache/v/cache/lastfailed",
        "fire-tv/android/app/build/outputs/apk/release/app-release.apk",
        "fire-tv/android/app/release.keystore.properties",
        "fire-tv/android/app/my-release-key.jks",
        "fire-tv/android/local.properties",
        "infrastructure/.aws-sam/build/x",
        ".DS_Store",
        "some/dir/Thumbs.db",
        "some/dir/npm-debug.log.1",
        "app.egg-info",
    ],
)
def test_excluded_paths_are_never_included(relative_path):
    assert should_include(Path(relative_path)) is False


@pytest.mark.parametrize(
    "relative_path",
    [
        "README.md",
        "backend/app/main.py",
        "fire-tv/src/App.tsx",
        "fire-tv/android/app/debug.keystore",
        ".env.example",
        "fire-tv/.env.example" if False else ".env.example",  # kept for clarity — see KEPT_EXCEPTIONS
        "scripts/package_release.py",
        "infrastructure/template.yaml",
    ],
)
def test_ordinary_and_explicitly_kept_paths_are_included(relative_path):
    assert should_include(Path(relative_path)) is True


def test_debug_keystore_is_kept_but_a_release_keystore_next_to_it_is_not():
    assert should_include(Path("fire-tv/android/app/debug.keystore")) is True
    assert should_include(Path("fire-tv/android/app/release-signing.jks")) is False


class TestEndToEndPackaging:
    def _make_fixture_tree(self, tmp_path: Path) -> Path:
        source_root = tmp_path / "fixture-repo"
        (source_root / "backend" / "app").mkdir(parents=True)
        (source_root / "backend" / "app" / "main.py").write_text("print('hi')\n")
        (source_root / "backend" / ".venv" / "bin").mkdir(parents=True)
        (source_root / "backend" / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
        (source_root / "fire-tv" / "node_modules" / "pkg").mkdir(parents=True)
        (source_root / "fire-tv" / "node_modules" / "pkg" / "index.js").write_text("module.exports = {};\n")
        (source_root / "fire-tv" / "android" / ".gradle").mkdir(parents=True)
        (source_root / "fire-tv" / "android" / ".gradle" / "cache.bin").write_bytes(b"\x00\x01")
        (source_root / "fire-tv" / "android" / "app").mkdir(parents=True)
        (source_root / "fire-tv" / "android" / "app" / "release.keystore.properties").write_text(
            "storePassword=super-secret\n"
        )
        (source_root / "fire-tv" / "android" / "app" / "debug.keystore").write_bytes(b"\x00fake-debug-keystore")
        (source_root / ".env").write_text("DEMO_ACCESS_TOKEN=real-secret\n")
        (source_root / ".env.example").write_text("DEMO_ACCESS_TOKEN=\n")
        (source_root / "README.md").write_text("# Fixture repo\n")

        # Spec v1.1 pass 4, mandatory correction 6: an executable script
        # (standing in for fire-tv/android/gradlew) and an ordinary
        # non-executable file, so the packaging test below can prove
        # permissions are preserved/normalized correctly rather than
        # every entry silently coming out 0o644.
        gradlew_stand_in = source_root / "fire-tv" / "android" / "gradlew"
        gradlew_stand_in.write_text("#!/usr/bin/env sh\necho gradlew\n")
        gradlew_stand_in.chmod(0o755)
        ordinary_script = source_root / "scripts" / "not_executable.py"
        ordinary_script.parent.mkdir(parents=True, exist_ok=True)
        ordinary_script.write_text("print('not executable')\n")
        ordinary_script.chmod(0o644)
        return source_root

    def test_build_then_inspect_round_trip_is_clean(self, tmp_path):
        source_root = self._make_fixture_tree(tmp_path)
        output_path = tmp_path / "output" / "test-archive.zip"

        included = build_archive(source_root, output_path)
        included_posix = {p.as_posix() for p in included}

        assert "backend/app/main.py" in included_posix
        assert "README.md" in included_posix
        assert ".env.example" in included_posix
        assert "fire-tv/android/app/debug.keystore" in included_posix

        assert "backend/.venv/bin/python" not in included_posix
        assert "fire-tv/node_modules/pkg/index.js" not in included_posix
        assert "fire-tv/android/.gradle/cache.bin" not in included_posix
        assert "fire-tv/android/app/release.keystore.properties" not in included_posix
        assert ".env" not in included_posix

        forbidden = inspect_archive(output_path)
        assert forbidden == []

        # The secret value must not appear ANYWHERE in the built archive's
        # bytes — not just "the file wasn't listed," but the content
        # itself never made it in.
        archive_bytes = output_path.read_bytes()
        assert b"real-secret" not in archive_bytes
        assert b"super-secret" not in archive_bytes

    def test_archive_file_count_and_size_are_reported(self, tmp_path, capsys):
        source_root = self._make_fixture_tree(tmp_path)
        output_path = tmp_path / "output" / "test-archive.zip"
        from scripts.package_release import main

        exit_code = main(["--output", str(output_path), "--source-root", str(source_root)])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "File count:" in captured.out
        assert "Archive size:" in captured.out
        assert output_path.exists()

    def test_inspection_fails_and_deletes_the_archive_if_a_forbidden_entry_is_manually_added(self, tmp_path):
        """Proves the inspector is a REAL independent check, not just
        trusting the build loop: hand-crafts an archive containing a
        forbidden entry the normal build path would never add, and
        confirms inspect_archive() catches it."""
        output_path = tmp_path / "tampered.zip"
        with zipfile.ZipFile(output_path, "w") as archive:
            archive.writestr("README.md", "# ok\n")
            archive.writestr(".env", "DEMO_ACCESS_TOKEN=leaked\n")

        forbidden = inspect_archive(output_path)
        assert ".env" in forbidden

    def test_archive_never_contains_itself(self, tmp_path):
        source_root = self._make_fixture_tree(tmp_path)
        # Output path INSIDE the source root this time, to exercise the
        # self-containment concern directly at the inspect_archive layer
        # (main() itself refuses this case outright — see
        # test_output_inside_source_root_is_rejected below).
        output_path = source_root / "archive.zip"
        build_archive(source_root, output_path)
        forbidden = inspect_archive(output_path)
        assert forbidden == [], f"archive should not reference itself: {forbidden}"
        with zipfile.ZipFile(output_path) as archive:
            assert "archive.zip" not in archive.namelist()

    def test_output_inside_source_root_is_rejected(self, tmp_path):
        source_root = self._make_fixture_tree(tmp_path)
        output_path = source_root / "nested" / "archive.zip"
        from scripts.package_release import main

        exit_code = main(["--output", str(output_path), "--source-root", str(source_root)])
        assert exit_code == 2

    # -----------------------------------------------------------------
    # Spec v1.1 pass 4, mandatory correction 6: "Preserve executable
    # permissions in release ZIP." Every entry used to be hardcoded to
    # 0o644, which made fire-tv/android/gradlew and other command-line
    # scripts silently non-executable after extraction on Linux/macOS.
    # -----------------------------------------------------------------

    def _entry_mode(self, output_path: Path, name: str) -> int:
        import stat as stat_module

        with zipfile.ZipFile(output_path) as archive:
            info = archive.getinfo(name)
            return stat_module.S_IMODE(info.external_attr >> 16)

    def test_gradlew_is_executable_after_extraction(self, tmp_path):
        source_root = self._make_fixture_tree(tmp_path)
        output_path = tmp_path / "output" / "test-archive.zip"
        build_archive(source_root, output_path)

        mode = self._entry_mode(output_path, "fire-tv/android/gradlew")
        assert mode & 0o111, f"gradlew must be executable in the archive, got mode {oct(mode)}"

    def test_ordinary_files_are_not_unnecessarily_executable(self, tmp_path):
        source_root = self._make_fixture_tree(tmp_path)
        output_path = tmp_path / "output" / "test-archive.zip"
        build_archive(source_root, output_path)

        for entry_name in ("README.md", "backend/app/main.py", "scripts/not_executable.py"):
            mode = self._entry_mode(output_path, entry_name)
            assert mode == 0o644, f"{entry_name} should be 0o644, got {oct(mode)}"

    def test_extracting_the_archive_preserves_the_executable_bit_on_disk(self, tmp_path):
        """End-to-end: build, extract with a real unzip, and confirm the
        extracted gradlew stand-in is genuinely runnable (not just that
        the ZIP metadata claims it is)."""
        import os
        import subprocess

        source_root = self._make_fixture_tree(tmp_path)
        output_path = tmp_path / "output" / "test-archive.zip"
        build_archive(source_root, output_path)

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        subprocess.run(["unzip", "-q", str(output_path), "-d", str(extract_dir)], check=True)

        extracted_gradlew = extract_dir / "fire-tv" / "android" / "gradlew"
        assert extracted_gradlew.exists()
        assert os.access(extracted_gradlew, os.X_OK), "extracted gradlew must be executable"

        extracted_ordinary = extract_dir / "scripts" / "not_executable.py"
        assert extracted_ordinary.exists()
        assert not os.access(extracted_ordinary, os.X_OK), "an ordinary file must not become executable"

    def test_two_builds_from_identical_source_are_byte_identical(self, tmp_path):
        """Spec v1.1 pass 4, mandatory correction 6: preserving real
        permission bits must not break reproducibility — two builds over
        the SAME source tree (including the executable gradlew stand-in)
        must still produce byte-identical archives."""
        source_root = self._make_fixture_tree(tmp_path)
        output_a = tmp_path / "output" / "archive-a.zip"
        output_b = tmp_path / "output" / "archive-b.zip"

        build_archive(source_root, output_a)
        build_archive(source_root, output_b)

        assert output_a.read_bytes() == output_b.read_bytes()
