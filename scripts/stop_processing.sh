#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/docker-compose.processing.yml"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/agentic-flow-processing"

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
else
  DOCKER=(sudo docker)
fi
COMPOSE=("${DOCKER[@]}" compose -f "$COMPOSE_FILE")

stop_worker() {
  local name="$1"
  local pid_file="$STATE_DIR/$name.pid"
  [[ -f "$pid_file" ]] || return 0

  local pid
  pid="$(<"$pid_file")"
  if [[ "$pid" =~ ^[0-9]+$ ]] &&
    kill -0 "$pid" 2>/dev/null &&
    ps -p "$pid" -o args= | grep -Fq "manage.py $name"; then
    kill -TERM "$pid"
    printf 'Stopped %s worker (PID %s).\n' "$name" "$pid"
  else
    printf 'Removed stale %s worker PID file.\n' "$name"
  fi
  rm -f "$pid_file"
}

stop_worker temporal_worker
stop_worker kafka_worker
"${COMPOSE[@]}" -f "$COMPOSE_FILE" down
printf 'Processing services stopped. Worker logs remain in %s.\n' "$STATE_DIR"
