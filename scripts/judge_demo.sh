#!/usr/bin/env bash
# Judge demo orchestration (AWS hackathon-final pass, P2).
#
# One command that gets a local backend into the EXACT state
# JUDGE_DEMO.md's 3-minute script assumes: running, healthy, a known
# demo-access token, and the golden lap-24 -> lap-27-alert -> lap-28
# narrative independently re-verified end-to-end via
# `scripts/golden_demo.py` (the same script CI/release checklists use —
# this is not a second, divergent verification path). If this script
# exits 0, the golden path is proven to work RIGHT NOW, against the
# backend that is about to be shown to judges — not "it worked in CI
# yesterday."
#
# Usage:
#   scripts/judge_demo.sh                 # start (if needed), verify, leave running
#   scripts/judge_demo.sh --base-url URL  # verify against an already-deployed stack instead
#   scripts/judge_demo.sh --stop          # stop a backend this script itself started
#
# Exit code 0 only if the golden path was actually re-verified and
# passed. Never claims success on a skipped or partial check.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_DIR="${REPO_ROOT}/backend"
PID_FILE="${REPO_ROOT}/.judge_demo_backend.pid"
LOG_FILE="${REPO_ROOT}/.judge_demo_backend.log"
DEFAULT_BASE_URL="http://127.0.0.1:8000"
HEALTH_TIMEOUT_SECONDS=30

BASE_URL="${PIT_WALL_API_BASE_URL:-${DEFAULT_BASE_URL}}"
EXTERNAL_BACKEND=0
STOP_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      BASE_URL="$2"
      EXTERNAL_BACKEND=1
      shift 2
      ;;
    --stop)
      STOP_ONLY=1
      shift
      ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

stop_backend() {
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}")"
    if kill -0 "${pid}" 2>/dev/null; then
      echo "Stopping judge-demo backend (pid ${pid})..."
      kill "${pid}"
      wait "${pid}" 2>/dev/null || true
    fi
    rm -f "${PID_FILE}"
    echo "Stopped."
  else
    echo "No judge-demo-managed backend is running (no ${PID_FILE})."
  fi
}

if [[ "${STOP_ONLY}" -eq 1 ]]; then
  stop_backend
  exit 0
fi

if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
  echo "A judge-demo backend from a previous run is still up (pid $(cat "${PID_FILE}"))." >&2
  echo "Run 'scripts/judge_demo.sh --stop' first if you want a clean restart." >&2
fi

cleanup_on_failure() {
  local exit_code=$?
  if [[ ${exit_code} -ne 0 && "${EXTERNAL_BACKEND}" -eq 0 && -f "${PID_FILE}" ]]; then
    echo "" >&2
    echo "judge_demo.sh failed — leaving the backend log at ${LOG_FILE} for inspection" >&2
    echo "(the backend process itself is left running so you can curl it directly;" >&2
    echo " run 'scripts/judge_demo.sh --stop' to tear it down)." >&2
  fi
  exit "${exit_code}"
}
trap cleanup_on_failure EXIT

DEMO_TOKEN="${DEMO_ACCESS_TOKEN:-${PIT_WALL_DEMO_ACCESS_TOKEN:-}}"

if [[ "${EXTERNAL_BACKEND}" -eq 1 ]]; then
  echo "=== Verifying against an already-running backend: ${BASE_URL} ==="
  if [[ -z "${DEMO_TOKEN}" ]]; then
    echo "PIT_WALL_DEMO_ACCESS_TOKEN (or DEMO_ACCESS_TOKEN) must be set when using --base-url" >&2
    echo "against a deployed stack — see docs/deployment-runbook.md's Secrets Manager section" >&2
    echo "for how to read the deployed token." >&2
    exit 1
  fi
else
  echo "=== Starting a local backend for the judge demo ==="
  if [[ -z "${DEMO_TOKEN}" ]]; then
    # A fixed-but-random token generated HERE (not scraped from the
    # server's own stderr banner) so this script never depends on
    # parsing another process's log output to get a credential it needs
    # deterministically and immediately.
    DEMO_TOKEN="judge-demo-$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
    echo "Generated a fresh local demo token for this session."
  fi

  # AWS hackathon-final pass, phase 8: this used to silently assume EITHER
  # backend/.venv already existed (from some earlier, unmentioned setup
  # step) OR a bare `uvicorn` happened to be on PATH -- on a judge's clean
  # checkout, neither is guaranteed, and the failure mode was a
  # confusing "Backend did not become healthy within 30s" (the health-wait
  # loop below), not an actionable error naming the real cause. This is a
  # genuinely one-command judge-facing script, so it now builds the venv
  # itself, from `backend/requirements.txt`, exactly once, the first time
  # it's needed -- no manual setup step this script doesn't itself perform
  # or mention.
  UVICORN_BIN=""
  if [[ -x "${BACKEND_DIR}/.venv/bin/uvicorn" ]]; then
    UVICORN_BIN="${BACKEND_DIR}/.venv/bin/uvicorn"
  elif command -v uvicorn >/dev/null 2>&1; then
    UVICORN_BIN="uvicorn"
    echo "Using uvicorn already on PATH ($(command -v uvicorn))."
  else
    echo "No backend/.venv and no uvicorn on PATH -- creating backend/.venv now" \
      "(one-time setup; see backend/requirements.txt)."
    if ! command -v python3 >/dev/null 2>&1; then
      echo "python3 is required to create backend/.venv and was not found on PATH." >&2
      exit 1
    fi
    python3 -m venv "${BACKEND_DIR}/.venv"
    "${BACKEND_DIR}/.venv/bin/pip" install --quiet --upgrade pip
    "${BACKEND_DIR}/.venv/bin/pip" install --quiet -r "${BACKEND_DIR}/requirements.txt"
    UVICORN_BIN="${BACKEND_DIR}/.venv/bin/uvicorn"
    echo "backend/.venv created and dependencies installed."
  fi
  echo "Starting uvicorn (${UVICORN_BIN}) in the background (log: ${LOG_FILE})..."
  (
    cd "${BACKEND_DIR}"
    DEMO_ACCESS_TOKEN="${DEMO_TOKEN}" \
      nohup "${UVICORN_BIN}" app.main:app --host 127.0.0.1 --port 8000 >"${LOG_FILE}" 2>&1 &
    echo $! > "${PID_FILE}"
  )
  BACKEND_PID="$(cat "${PID_FILE}")"
  echo "Backend pid: ${BACKEND_PID}"

  echo -n "Waiting for /health"
  deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
  until curl -sf "${BASE_URL}/health" >/dev/null 2>&1; do
    if ! kill -0 "${BACKEND_PID}" 2>/dev/null; then
      echo ""
      echo "Backend process exited before becoming healthy — see ${LOG_FILE}:" >&2
      tail -n 40 "${LOG_FILE}" >&2
      exit 1
    fi
    if [[ ${SECONDS} -ge ${deadline} ]]; then
      echo ""
      echo "Backend did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s — see ${LOG_FILE}" >&2
      exit 1
    fi
    echo -n "."
    sleep 1
  done
  echo " healthy."
fi

echo ""
echo "=== Re-verifying the golden lap-24 -> lap-27-alert -> lap-28 path ==="
cd "${REPO_ROOT}"
if ! PIT_WALL_API_BASE_URL="${BASE_URL}" PIT_WALL_DEMO_ACCESS_TOKEN="${DEMO_TOKEN}" \
    python3 scripts/golden_demo.py; then
  echo "" >&2
  echo "Golden path verification FAILED — do NOT start the judge demo against this backend." >&2
  exit 1
fi

echo ""
echo "================================================================="
echo " Judge demo pre-flight PASSED."
echo "   Backend:      ${BASE_URL}"
echo "   Demo token:   ${DEMO_TOKEN}"
if [[ "${EXTERNAL_BACKEND}" -eq 0 ]]; then
  echo "   Backend pid:  $(cat "${PID_FILE}") (stop with: scripts/judge_demo.sh --stop)"
fi
echo ""
echo " Next: run 'curl -X POST ${BASE_URL}/v1/demo/reset -H \"X-Demo-Access-Token: ${DEMO_TOKEN}\"'"
echo " immediately before judges arrive so the race is at the known lap-24"
echo " baseline, then follow JUDGE_DEMO.md."
echo "================================================================="
trap - EXIT
