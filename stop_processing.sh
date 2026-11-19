#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$ROOT/docker-compose.processing.yml"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/agentic-flow-processing"

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
else
  DOCKER=(sudo docker)
fi
COMPOSE=("${DOCKER[@]}" compose -f "$COMPOSE_FILE")

VENV_PYTHON="$ROOT/venv/bin/python"

# PIDs of every "manage.py <name>" process from this project, whether or not
# start_processing.sh started it (the PID file may be stale) and whichever
# user owns it (workers started with sudo run as root).
worker_pids() {
  local name="$1" pid args
  for pid in $(pgrep -f "manage\.py ${name}( |$)" || true); do
    args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    if [[ "$args" == "$VENV_PYTHON "* ]] ||
      [[ "$(readlink "/proc/$pid/cwd" 2>/dev/null)" == "$ROOT" ]]; then
      echo "$pid"
    fi
  done
  return 0
}

# Signal PIDs, falling back to sudo for processes owned by another user.
signal_pids() {
  local sig="$1"
  shift
  kill "-$sig" "$@" 2>/dev/null || sudo kill "-$sig" "$@" 2>/dev/null || true
}

stop_worker() {
  local name="$1"
  local pid_file="$STATE_DIR/$name.pid"
  local pids
  pids="$(worker_pids "$name")"

  if [[ -z "$pids" ]]; then
    printf '%s worker is not running.\n' "$name"
    rm -f "$pid_file"
    return 0
  fi

  # shellcheck disable=SC2086
  signal_pids TERM $pids

  # Give the worker time to shut down gracefully, then force it.
  for ((i = 0; i < 15; i++)); do
    pids="$(worker_pids "$name")"
    [[ -z "$pids" ]] && break
    sleep 1
  done
  if [[ -n "$pids" ]]; then
    printf '%s worker did not exit after SIGTERM; sending SIGKILL.\n' "$name"
    # shellcheck disable=SC2086
    signal_pids KILL $pids
    sleep 1
    pids="$(worker_pids "$name")"
    if [[ -n "$pids" ]]; then
      printf 'Could not stop %s worker (PIDs: %s).\n' "$name" "$(echo $pids)" >&2
      return 1
    fi
  fi

  printf 'Stopped %s worker.\n' "$name"
  rm -f "$pid_file"
}

stop_worker temporal_worker
stop_worker kafka_worker
"${COMPOSE[@]}" down
printf 'Processing services stopped. Worker logs remain in %s.\n' "$STATE_DIR"
