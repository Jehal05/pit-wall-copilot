"""Resolves which Python interpreter a test suite should use to launch the
`backend` subprocess (uvicorn), without assuming any particular virtual
environment layout or name.

Spec v1.1 pass 3, correction 3: earlier versions of `mcp_server/tests/
conftest.py` and `alexa_plus_client/tests/conftest.py` each hardcoded
`backend/.venv/bin/python` directly. That fails outright in any environment
where the backend's virtual environment is named differently, lives
elsewhere, or is a Windows-style `.venv\\Scripts\\python.exe` layout — even
though a perfectly good interpreter may be available another way (an
explicit override, or the interpreter currently running pytest itself, if
it already has the backend's dependencies installed).

Resolution order (first match wins):

  1. The `PIT_WALL_BACKEND_PYTHON` environment variable, if set — used
     verbatim, no existence check performed here (the caller decides
     whether/how to verify it, e.g. by trying to launch it).
  2. `<backend_dir>/.venv/Scripts/python.exe`, on Windows, if it exists.
  3. `<backend_dir>/.venv/bin/python`, on Unix-like platforms, if it exists.
  4. The current interpreter (`sys.executable`), as a documented fallback,
     but ONLY if it can actually import the backend's own dependencies
     (checked via `current_python_can_import_backend`) — this avoids
     silently trying to run the backend under an interpreter that is
     missing FastAPI/uvicorn/etc. and failing confusingly deep inside the
     subprocess instead of with a clear message here.
  5. Otherwise, raise `BackendPythonNotFoundError` with a clear, actionable
     message — never a skip.

Every parameter that depends on the surrounding OS/filesystem/interpreter
is injectable (`env`, `platform`, `current_python`,
`current_python_can_import_backend`) so this function is exercised by pure
unit tests independent of the host running them — see
`scripts/tests/test_resolve_backend_python.py`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional

ENV_VAR_NAME = "PIT_WALL_BACKEND_PYTHON"


class BackendPythonNotFoundError(RuntimeError):
    """Raised when no usable backend Python interpreter could be resolved."""


def _default_current_python_can_import_backend() -> bool:
    """Best-effort check that the CURRENTLY RUNNING interpreter has the
    backend's key runtime dependencies importable, so falling back to it
    is a documented, safe choice rather than a guess. Deliberately checks
    only a couple of representative top-level packages (fast, no need to
    literally parse requirements.txt) rather than actually importing
    `app.main` (which would have side effects and require `backend/` on
    `sys.path`)."""
    required_modules = ("fastapi", "uvicorn")
    return all(importlib.util.find_spec(name) is not None for name in required_modules)


def resolve_backend_python(
    backend_dir: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
    platform: str = sys.platform,
    current_python: str = sys.executable,
    current_python_can_import_backend: Optional[Callable[[], bool]] = None,
    path_exists: Optional[Callable[[Path], bool]] = None,
) -> str:
    """Returns the absolute path (as a string) of the Python interpreter to
    use for launching the backend subprocess. Raises
    `BackendPythonNotFoundError` if none of the resolution steps succeed.
    """
    if env is None:
        import os

        env = os.environ
    if path_exists is None:
        path_exists = lambda p: p.exists()  # noqa: E731
    if current_python_can_import_backend is None:
        current_python_can_import_backend = _default_current_python_can_import_backend

    # 1. Explicit override — always wins, and is never second-guessed here.
    override = env.get(ENV_VAR_NAME)
    if override:
        return override

    # 2 / 3. A venv at the conventional location for this platform.
    if platform.startswith("win"):
        windows_candidate = backend_dir / ".venv" / "Scripts" / "python.exe"
        if path_exists(windows_candidate):
            return str(windows_candidate)
    else:
        unix_candidate = backend_dir / ".venv" / "bin" / "python"
        if path_exists(unix_candidate):
            return str(unix_candidate)

    # 4. The current interpreter, only if it can actually run the backend.
    if current_python_can_import_backend():
        return current_python

    # 5. Nothing usable — fail with a clear, actionable message.
    raise BackendPythonNotFoundError(
        "Could not resolve a Python interpreter to run the backend with. "
        f"Tried, in order: (1) the {ENV_VAR_NAME} environment variable "
        "(not set), (2) "
        f"{backend_dir / '.venv' / 'Scripts' / 'python.exe'} "
        "(Windows-style venv, not found), (3) "
        f"{backend_dir / '.venv' / 'bin' / 'python'} "
        "(Unix-style venv, not found), (4) the current interpreter "
        f"({current_python}) — but it does not have the backend's "
        "dependencies (fastapi, uvicorn) importable. Set up "
        "backend/.venv per the README, or set "
        f"{ENV_VAR_NAME}=/absolute/path/to/python to point at an "
        "interpreter that already has backend/requirements.txt installed."
    )
