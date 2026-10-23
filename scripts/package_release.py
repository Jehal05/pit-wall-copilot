#!/usr/bin/env python3
"""Deterministic release-archive packaging script (spec v1.1 pass 3,
correction 7).

Builds a clean, reproducible ZIP of the repository, EXCLUDING everything
that must never ship in a source archive: secrets, virtual environments,
build output, caches, IDE/OS junk, and locally generated build artifacts
that should be delivered separately (if at all).

Usage:
    python scripts/package_release.py [--output PATH] [--source-root PATH]

Exit codes:
    0  — archive built and passed its own post-build inspection
    1  — a forbidden path would have been included (the archive is
         deleted before exiting, so a bad archive is never left behind)
    2  — usage/argument error

The same exclusion rules and inspection logic are exercised directly (no
subprocess) by backend/tests/test_package_release.py, so a change here
that weakens an exclusion is caught by that test, not just by eyeballing
this file's own output.
"""

from __future__ import annotations

import argparse
import fnmatch
import stat
import sys
import zipfile
from pathlib import Path
from typing import Iterable

# Directory names excluded ENTIRELY (the directory and everything under
# it), matched against any path component, not just the top level — e.g.
# "node_modules" excludes "fire-tv/node_modules" and any nested copy.
EXCLUDED_DIR_NAMES = {
    ".git",
    ".gradle",
    "node_modules",
    ".venv",
    ".venv-review",
    "venv",
    "env",
    ".pytest_cache",
    "__pycache__",
    "htmlcov",
    ".coverage",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".aws-sam",
    "build",  # Android's app/build/ (Gradle build output)
    "_to_delete",  # device-bridge staged-for-deletion files (see system prompt's device_bridge section)
}

# Individual filename GLOB patterns excluded wherever they appear.
EXCLUDED_FILE_GLOBS = [
    ".env",
    ".env.*",  # includes .env.local, .env.production, etc.
    "*.pyc",
    "*.pyo",
    "*.log",
    "npm-debug.log*",
    "yarn-error.log*",
    ".DS_Store",
    "Thumbs.db",
    "*.jks",
    "*.keystore",
    "release.keystore.properties",
    "*.apk",
    "*.aab",
    "*.egg-info",
    "local.properties",  # Android SDK-path-on-this-machine, never portable
    "*.lock~",  # stray Gradle lock artifacts
]

# Explicit exceptions: a file that would otherwise match an excluded glob
# but must be KEPT. Checked by exact relative-path suffix match.
KEPT_EXCEPTIONS = {
    ".env.example",
    "fire-tv/android/app/debug.keystore",  # the standard, non-secret debug keystore
}


def _is_excluded_dir(path_parts: tuple[str, ...]) -> bool:
    return any(part in EXCLUDED_DIR_NAMES for part in path_parts)


def _is_excluded_file(relative_path: Path) -> bool:
    posix = relative_path.as_posix()
    if posix in KEPT_EXCEPTIONS or posix.endswith(tuple(f"/{k}" for k in KEPT_EXCEPTIONS)):
        return False
    name = relative_path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDED_FILE_GLOBS)


def should_include(relative_path: Path) -> bool:
    """The single source of truth both the archive builder and the
    post-build inspector use — so "what was excluded" and "what the
    inspector checks for" can never silently drift apart."""
    if _is_excluded_dir(relative_path.parts[:-1]):
        return False
    if _is_excluded_file(relative_path):
        return False
    return True


def iter_included_files(source_root: Path) -> Iterable[Path]:
    for path in sorted(source_root.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(source_root)
        if should_include(relative):
            yield relative


def _archive_mode_for(source_file: Path) -> int:
    """Spec v1.1 pass 4, mandatory correction 6: "Preserve executable
    permissions from source files ... a real archive is useless if
    `fire-tv/android/gradlew` and other command-line scripts come out
    non-executable on Linux/macOS after extraction." Previously every
    entry was hardcoded to 0o644 regardless of the source file's real
    mode, which silently stripped the executable bit from `gradlew` and
    any shell/Python script meant to be run directly.

    This reads the SOURCE file's actual permission bits
    (`stat.S_IMODE`, not the raw `st_mode`, which also carries the file
    -type bits that don't belong in a ZIP entry's external_attr) and
    normalizes them to exactly one of two values, deliberately NOT a
    pass-through of arbitrary bits (which would make the archive's
    reproducibility depend on the exact umask of whoever last touched
    the file, e.g. 0o755 vs 0o775): any of the three execute bits set on
    the source (owner, group, or other — `git`'s own tracked executable
    bit is the owner one) means the archived entry is 0o755
    (rwxr-xr-x — executable and readable by everyone, matching how
    `gradlew`/scripts are actually meant to run once extracted); anything
    else is the ordinary 0o644 (rw-r--r--) every other tracked file gets.
    This keeps two builds from identical source content byte-for-byte
    reproducible while still preserving the one bit of metadata release
    scripts genuinely depend on.
    """
    mode = stat.S_IMODE(source_file.stat().st_mode)
    is_executable = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    return 0o755 if is_executable else 0o644


def build_archive(source_root: Path, output_path: Path) -> list[Path]:
    included: list[Path] = list(iter_included_files(source_root))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Deterministic ordering (sorted above) and a fixed date_time for every
    # entry (ZipInfo defaults to the file's mtime, which is NOT
    # reproducible across machines/checkouts) — two runs over identical
    # source content byte-for-byte produce byte-identical archives.
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in included:
            source_file = source_root / relative
            mode = _archive_mode_for(source_file)
            info = zipfile.ZipInfo(str(relative.as_posix()), date_time=(2024, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (mode & 0xFFFF) << 16
            with source_file.open("rb") as fh:
                archive.writestr(info, fh.read())
    return included


class ForbiddenPathError(Exception):
    pass


def inspect_archive(output_path: Path) -> list[str]:
    """Re-opens the JUST-BUILT archive and checks every entry against
    `should_include` — a real, independent verification pass rather than
    trusting that the build loop above did the right thing (the loop and
    this check share `should_include`, but this still catches a bug where
    an entry was added some OTHER way, e.g. a future contributor adding a
    second `archive.write(...)` call that bypasses the exclusion filter).
    Returns the list of forbidden entries found (empty if clean)."""
    forbidden: list[str] = []
    with zipfile.ZipFile(output_path) as archive:
        for name in archive.namelist():
            if name == output_path.name:
                forbidden.append(f"{name} (the archive must not contain itself)")
                continue
            if not should_include(Path(name)):
                forbidden.append(name)
    return forbidden


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("pit-wall-copilot-v1.1-production-ready.zip"),
        help="Output archive path (default: pit-wall-copilot-v1.1-production-ready.zip in the current directory)",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root to package (default: this script's repo)",
    )
    args = parser.parse_args(argv)

    source_root: Path = args.source_root.resolve()
    output_path: Path = args.output.resolve()

    if output_path.is_relative_to(source_root):
        # Never build the archive INSIDE the tree being packaged — even
        # with should_include's own filtering, this avoids the archive
        # trying to read itself mid-write on a second run.
        print(
            f"ERROR: --output ({output_path}) must not be inside --source-root "
            f"({source_root}) — write it to a separate directory.",
            file=sys.stderr,
        )
        return 2

    print(f"Packaging {source_root} -> {output_path}")
    included = build_archive(source_root, output_path)
    print(f"Wrote {len(included)} files.")

    forbidden = inspect_archive(output_path)
    if forbidden:
        print("ERROR: forbidden paths found in the built archive:", file=sys.stderr)
        for path in forbidden:
            print(f"  - {path}", file=sys.stderr)
        output_path.unlink(missing_ok=True)
        return 1

    size_bytes = output_path.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    print("Archive inspection PASSED: no forbidden paths found.")
    print(f"File count: {len(included)}")
    print(f"Archive size: {size_bytes:,} bytes ({size_mb:.2f} MiB)")
    print(f"Wrote: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
