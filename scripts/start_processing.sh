#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/docker-compose.processing.yml"
VENV_PYTHON="$ROOT/venv/bin/python"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/agentic-flow-processing"

if [[ ! -x "$VENV_PYTHON" ]]; then
  printf 'Project virtualenv Python not found: %s\n' "$VENV_PYTHON" >&2
  exit 1
fi

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
else
  DOCKER=(sudo docker)
fi
COMPOSE=("${DOCKER[@]}" compose -f "$COMPOSE_FILE")

mkdir -p "$STATE_DIR"

worker_is_running() {
  local name="$1"
  local pid_file="$STATE_DIR/$name.pid"
  [[ -f "$pid_file" ]] || return 1

  local pid
  pid="$(<"$pid_file")"
  if [[ "$pid" =~ ^[0-9]+$ ]] &&
    kill -0 "$pid" 2>/dev/null &&
    ps -p "$pid" -o args= | grep -Fq "manage.py $name"; then
    return 0
  fi

  rm -f "$pid_file"
  return 1
}

wait_for_service_health() {
  local service="$1"
  local attempts=60
  local container_id state
  container_id="$("${COMPOSE[@]}" ps -q "$service")"
  if [[ -z "$container_id" ]]; then
    printf 'Compose did not create the %s container.\n' "$service" >&2
    return 1
  fi

  for ((i = 0; i < attempts; i++)); do
    state="$("${DOCKER[@]}" inspect --format \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$container_id")"
    if [[ "$state" == "healthy" ]]; then
      printf '%s is healthy.\n' "$service"
      return 0
    fi
    if [[ "$state" == "exited" || "$state" == "dead" ]]; then
      "${COMPOSE[@]}" logs --tail=80 "$service" >&2
      printf '%s exited before becoming healthy.\n' "$service" >&2
      return 1
    fi
    sleep 2
  done

  "${COMPOSE[@]}" logs --tail=80 "$service" >&2
  printf 'Timed out waiting for %s (last state: %s).\n' "$service" "$state" >&2
  return 1
}

wait_for_temporal_port() {
  local attempts=60
  for ((i = 0; i < attempts; i++)); do
    if (echo > /dev/tcp/127.0.0.1/7234) >/dev/null 2>&1; then
      printf 'Temporal gRPC is accepting connections on localhost:7234.\n'
      return 0
    fi
    sleep 2
  done

  "${COMPOSE[@]}" logs --tail=100 temporal >&2
  printf 'Timed out waiting for Temporal on localhost:7234.\n' >&2
  return 1
}

start_worker() {
  local name="$1"
  if worker_is_running "$name"; then
    printf '%s worker is already running (PID %s).\n' \
      "$name" "$(<"$STATE_DIR/$name.pid")"
    return
  fi

  (
    cd "$ROOT"
    nohup env \
      POSTGRES_DB=agentic-db \
      POSTGRES_USER=aiplatform \
      POSTGRES_PASSWORD=aiplatform123 \
      POSTGRES_HOST=localhost \
      POSTGRES_PORT=5432 \
      DJANGO_SETTINGS_MODULE=base.settings \
      "$VENV_PYTHON" manage.py "$name" \
      >>"$STATE_DIR/$name.log" 2>&1 </dev/null &
    echo "$!" >"$STATE_DIR/$name.pid"
  )

  sleep 2
  if ! worker_is_running "$name"; then
    printf '%s worker failed to start. Recent log output:\n' "$name" >&2
    tail -n 80 "$STATE_DIR/$name.log" >&2 || true
    return 1
  fi
  printf '%s worker started (PID %s; log: %s).\n' \
    "$name" "$(<"$STATE_DIR/$name.pid")" "$STATE_DIR/$name.log"
}

"${COMPOSE[@]}" up -d kafka temporal-postgresql temporal temporal-ui
wait_for_service_health kafka
wait_for_service_health temporal-postgresql
wait_for_temporal_port
start_worker kafka_worker
start_worker temporal_worker

printf '\nKafka:       localhost:9093\n'
printf 'Temporal:    localhost:7234\n'
printf 'Temporal UI: http://localhost:8081\n'
printf 'Worker logs: %s\n' "$STATE_DIR"
