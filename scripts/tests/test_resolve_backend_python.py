"""Unit tests for scripts/testing_support/resolve_backend_python.py.

Spec v1.1 pass 3, correction 3: "Add tests for interpreter-path selection
covering Windows path, Unix path, explicit env override, current-interpreter
fallback, missing-interpreter failure." Every OS/filesystem-dependent input
is injected, so these tests exercise all five branches deterministically on
any host, regardless of what platform is actually running pytest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.testing_support.resolve_backend_python import (
    ENV_VAR_NAME,
    BackendPythonNotFoundError,
    resolve_backend_python,
)

BACKEND_DIR = Path("/repo/backend")


def _exists_only(*paths: Path):
    allowed = {p for p in paths}

    def _check(candidate: Path) -> bool:
        return candidate in allowed

    return _check


def test_explicit_env_override_wins_even_if_a_venv_also_exists():
    windows_venv = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
    result = resolve_backend_python(
        BACKEND_DIR,
        env={ENV_VAR_NAME: "/absolute/path/to/custom-python"},
        platform="win32",
        path_exists=_exists_only(windows_venv),
    )
    assert result == "/absolute/path/to/custom-python"


def test_windows_venv_path_is_selected_on_windows_when_present():
    windows_venv = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
    result = resolve_backend_python(
        BACKEND_DIR,
        env={},
        platform="win32",
        path_exists=_exists_only(windows_venv),
    )
    assert result == str(windows_venv)


def test_unix_venv_path_is_selected_on_unix_when_present():
    unix_venv = BACKEND_DIR / ".venv" / "bin" / "python"
    result = resolve_backend_python(
        BACKEND_DIR,
        env={},
        platform="linux",
        path_exists=_exists_only(unix_venv),
    )
    assert result == str(unix_venv)


def test_current_interpreter_fallback_used_when_no_venv_present_but_deps_importable():
    result = resolve_backend_python(
        BACKEND_DIR,
        env={},
        platform="linux",
        current_python="/usr/bin/python3",
        current_python_can_import_backend=lambda: True,
        path_exists=_exists_only(),  # nothing exists
    )
    assert result == "/usr/bin/python3"


def test_missing_interpreter_raises_a_clear_error_rather_than_skipping():
    with pytest.raises(BackendPythonNotFoundError) as exc_info:
        resolve_backend_python(
            BACKEND_DIR,
            env={},
            platform="linux",
            current_python="/usr/bin/python3",
            current_python_can_import_backend=lambda: False,
            path_exists=_exists_only(),  # nothing exists anywhere
        )
    message = str(exc_info.value)
    assert ENV_VAR_NAME in message
    assert "backend/.venv" in message or ".venv" in message


def test_windows_platform_does_not_fall_back_to_unix_venv_path():
    # On Windows, only the Scripts/python.exe layout is checked — a
    # Unix-style bin/python existing (e.g. a stray cross-platform mount)
    # must not be picked up.
    unix_venv = BACKEND_DIR / ".venv" / "bin" / "python"
    result = resolve_backend_python(
        BACKEND_DIR,
        env={},
        platform="win32",
        current_python="C:\\Python\\python.exe",
        current_python_can_import_backend=lambda: True,
        path_exists=_exists_only(unix_venv),
    )
    # Falls through to the current-interpreter fallback rather than the
    # (wrong-platform) Unix venv path.
    assert result == "C:\\Python\\python.exe"


def test_empty_env_var_value_is_treated_as_unset():
    unix_venv = BACKEND_DIR / ".venv" / "bin" / "python"
    result = resolve_backend_python(
        BACKEND_DIR,
        env={ENV_VAR_NAME: ""},
        platform="linux",
        path_exists=_exists_only(unix_venv),
    )
    assert result == str(unix_venv)
