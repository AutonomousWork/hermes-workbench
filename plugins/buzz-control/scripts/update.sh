#!/bin/sh
set -eu

TRIGGER=${1:-manual}
case "$TRIGGER" in
  manual|scheduled) ;;
  *) echo "Usage: update.sh [manual|scheduled]" >&2; exit 2 ;;
esac

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PLUGIN_ROOT=$(dirname -- "$SCRIPT_DIR")
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
HERMES_ROOT=${HERMES_HOME:-"$HOME/.hermes"}

DOCKER_BIN=${BUZZ_CONTROL_DOCKER_BIN:-/usr/local/bin/docker}
COMPOSE_BIN=${BUZZ_CONTROL_COMPOSE_BIN:-/usr/local/bin/docker-compose}
DOCKER_HOST=${BUZZ_CONTROL_DOCKER_HOST:-unix:///var/run/docker.sock}
DEPLOY_DIR=${BUZZ_CONTROL_DEPLOY_DIR:-"$PLUGIN_ROOT/deploy"}
ENV_FILE=${BUZZ_CONTROL_ENV_FILE:-"$CONFIG_HOME/buzz/prod.env"}
COMPOSE_FILE=${BUZZ_CONTROL_COMPOSE_FILE:-"$DEPLOY_DIR/compose.yml"}
OVERRIDE_FILE=${BUZZ_CONTROL_OVERRIDE_FILE:-"$DEPLOY_DIR/compose.local.yml"}
PROJECT=${BUZZ_CONTROL_COMPOSE_PROJECT:-buzz-prod}
SERVICE=${BUZZ_CONTROL_COMPOSE_SERVICE:-relay}
IMAGE=${BUZZ_CONTROL_IMAGE:-ghcr.io/block/buzz:main}
STATE_DIR=${BUZZ_CONTROL_STATE_DIR:-"$HERMES_ROOT/state/buzz-control"}
STATE_FILE="$STATE_DIR/update-state"
LOCK_DIR="$STATE_DIR/update.lock"
DOCKER_CONFIG_DIR="$STATE_DIR/docker-anonymous"
EXECUTION_TIMEOUT=${BUZZ_CONTROL_EXECUTION_TIMEOUT_SECONDS:-900}
OPERATION_OUTPUT="$STATE_DIR/operation.$$"

case "$PROJECT:$SERVICE:$IMAGE" in
  *[!A-Za-z0-9._/:@-]*) echo "Buzz update configuration contains unsupported characters." >&2; exit 2 ;;
esac
case "$DOCKER_HOST" in
  unix:///*) ;;
  *) echo "Buzz updates require a local unix:// Docker endpoint." >&2; exit 2 ;;
esac
case "$EXECUTION_TIMEOUT" in
  ''|*[!0-9]*) echo "Buzz update timeout must be a positive number of seconds." >&2; exit 2 ;;
  0) echo "Buzz update timeout must be a positive number of seconds." >&2; exit 2 ;;
esac

file_owner() {
  stat -f '%u' "$1" 2>/dev/null || stat -c '%u' "$1"
}

file_mode() {
  stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"
}

if [ ! -x "$DOCKER_BIN" ]; then
  echo "Docker CLI is not executable: $DOCKER_BIN" >&2
  exit 1
fi
if [ ! -x "$COMPOSE_BIN" ]; then
  echo "Docker Compose CLI is not executable: $COMPOSE_BIN" >&2
  exit 1
fi
for required in "$COMPOSE_FILE" "$OVERRIDE_FILE"; do
  if [ ! -f "$required" ]; then
    echo "Required Buzz deployment file not found: $required" >&2
    exit 1
  fi
done
if [ -L "$ENV_FILE" ] || [ ! -f "$ENV_FILE" ]; then
  echo "Buzz production environment must be a regular, non-symlink file: $ENV_FILE" >&2
  exit 1
fi
if [ "$(file_owner "$ENV_FILE")" != "$(id -u)" ]; then
  echo "Buzz production environment must be owned by the current user." >&2
  exit 1
fi
if [ "$(file_mode "$ENV_FILE")" != "600" ]; then
  echo "Buzz production environment must have exact mode 0600." >&2
  exit 1
fi

umask 077
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

cleanup() {
  rm -f "$OPERATION_OUTPUT"
  if [ -d "$LOCK_DIR" ]; then
    owner=$(sed -n 's/^pid=//p' "$LOCK_DIR/owner" 2>/dev/null || true)
    if [ "$owner" = "$$" ]; then
      rm -f "$LOCK_DIR/owner"
      rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
  fi
}

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf 'pid=%s\nstarted_at=%s\n' "$$" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$LOCK_DIR/owner"
    return 0
  fi

  owner=$(sed -n 's/^pid=//p' "$LOCK_DIR/owner" 2>/dev/null || true)
  case "$owner" in
    ''|*[!0-9]*) return 1 ;;
  esac
  if kill -0 "$owner" 2>/dev/null; then
    return 1
  fi

  rm -f "$LOCK_DIR/owner"
  rmdir "$LOCK_DIR" 2>/dev/null || return 1
  mkdir "$LOCK_DIR" 2>/dev/null || return 1
  printf 'pid=%s\nstarted_at=%s\n' "$$" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$LOCK_DIR/owner"
}

if ! acquire_lock; then
  if [ "$TRIGGER" = "scheduled" ]; then
    exit 0
  fi
  echo "Another Buzz update is already running." >&2
  exit 75
fi
trap cleanup EXIT HUP INT TERM

mkdir -p "$DOCKER_CONFIG_DIR"
chmod 700 "$DOCKER_CONFIG_DIR"
if [ ! -f "$DOCKER_CONFIG_DIR/config.json" ]; then
  printf '{}\n' > "$DOCKER_CONFIG_DIR/config.json"
fi
chmod 600 "$DOCKER_CONFIG_DIR/config.json"

run_bounded() {
  now=$(date +%s)
  remaining=$((DEADLINE_EPOCH - now))
  if [ "$remaining" -le 0 ]; then
    return 124
  fi

  if command -v timeout >/dev/null 2>&1; then
    if timeout "$remaining" "$@"; then
      return 0
    else
      status=$?
    fi
    [ "$status" -eq 124 ] && return 124
    return "$status"
  elif command -v perl >/dev/null 2>&1; then
    if perl -e 'alarm shift; exec @ARGV' "$remaining" "$@"; then
      return 0
    else
      status=$?
    fi
    [ "$status" -eq 142 ] && return 124
    return "$status"
  else
    echo "Buzz updates require timeout or perl to enforce the execution deadline." >&2
    return 125
  fi
}

docker_cmd() {
  run_bounded "$DOCKER_BIN" --config "$DOCKER_CONFIG_DIR" --host "$DOCKER_HOST" "$@"
}

docker_image() {
  docker_cmd image "$@"
}

compose() {
  BUZZ_IMAGE="$IMAGE" \
    BUZZ_SERVICE_ENV_FILE="$ENV_FILE" \
    DOCKER_CONFIG="$DOCKER_CONFIG_DIR" \
    DOCKER_HOST="$DOCKER_HOST" \
    run_bounded "$COMPOSE_BIN" \
      --project-name "$PROJECT" \
      --env-file "$ENV_FILE" \
      -f "$COMPOSE_FILE" \
      -f "$OVERRIDE_FILE" \
      "$@"
}

container_image_id() {
  container_id=$(compose ps -q "$SERVICE") || return $?
  if [ -n "$container_id" ]; then
    docker_cmd inspect --format '{{.Image}}' "$container_id"
  fi
}

container_health() {
  container_id=$(compose ps -q "$SERVICE") || return $?
  if [ -n "$container_id" ]; then
    docker_cmd inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id"
  fi
}

state_value() {
  key=$1
  if [ -f "$STATE_FILE" ]; then
    sed -n "s/^${key}=//p" "$STATE_FILE" | tail -n 1
  fi
}

safe_text() {
  printf '%s' "$1" | tr '\r\n=' '   ' | cut -c 1-500
}

write_state() {
  result=$1
  error=$2
  running_before=$3
  running_after=$4
  latest_id=$5
  latest_digest=$6
  latest_revision=$7
  latest_created=$8
  latest_observed=$9
  healthy=${10}
  completed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  last_success_at=$(state_value last_successful_update_at)
  last_success_image=$(state_value last_successful_image_id)
  latest_failure_at=$(state_value latest_failure_at)
  latest_failure_result=$(state_value latest_failure_result)
  latest_failure_error=$(state_value latest_failure_error)

  if [ "$latest_observed" != "true" ]; then
    latest_id=$(state_value latest_image_id)
    latest_digest=$(state_value latest_image_digest)
    latest_revision=$(state_value latest_image_revision)
    latest_created=$(state_value latest_image_created_at)
  fi

  if [ "$result" = "updated" ]; then
    last_success_at=$completed_at
    last_success_image=$running_after
  fi
  case "$result" in
    stopped|timed_out|pull_failed|apply_failed|unhealthy|verification_failed|state_write_failed)
      latest_failure_at=$completed_at
      latest_failure_result=$result
      latest_failure_error=$error
      ;;
  esac

  temp_state="$STATE_FILE.tmp.$$"
  if ! {
    printf 'schema_version=1\n'
    printf 'trigger=%s\n' "$(safe_text "$TRIGGER")"
    printf 'result=%s\n' "$(safe_text "$result")"
    printf 'started_at=%s\n' "$(safe_text "$STARTED_AT")"
    printf 'completed_at=%s\n' "$(safe_text "$completed_at")"
    printf 'last_check_at=%s\n' "$(safe_text "$completed_at")"
    printf 'running_image_before=%s\n' "$(safe_text "$running_before")"
    printf 'running_image_after=%s\n' "$(safe_text "$running_after")"
    printf 'latest_image_id=%s\n' "$(safe_text "$latest_id")"
    printf 'latest_image_digest=%s\n' "$(safe_text "$latest_digest")"
    printf 'latest_image_revision=%s\n' "$(safe_text "$latest_revision")"
    printf 'latest_image_created_at=%s\n' "$(safe_text "$latest_created")"
    printf 'latest_image_observed_this_attempt=%s\n' "$(safe_text "$latest_observed")"
    printf 'healthy=%s\n' "$(safe_text "$healthy")"
    printf 'error=%s\n' "$(safe_text "$error")"
    printf 'last_successful_update_at=%s\n' "$(safe_text "$last_success_at")"
    printf 'last_successful_image_id=%s\n' "$(safe_text "$last_success_image")"
    printf 'latest_failure_at=%s\n' "$(safe_text "$latest_failure_at")"
    printf 'latest_failure_result=%s\n' "$(safe_text "$latest_failure_result")"
    printf 'latest_failure_error=%s\n' "$(safe_text "$latest_failure_error")"
  } > "$temp_state" || ! chmod 600 "$temp_state" || ! mv "$temp_state" "$STATE_FILE"; then
    rm -f "$temp_state"
    return 1
  fi
}

failure_detail() {
  if [ -f "$OPERATION_OUTPUT" ]; then
    safe_text "$(tail -n 8 "$OPERATION_OUTPUT")"
  fi
}

fail_update() {
  result=$1
  message=$2
  before=$3
  after=$4
  latest=$5
  latest_digest=$6
  latest_revision=$7
  latest_created=$8
  latest_observed=$9
  if ! write_state "$result" "$message" "$before" "$after" "$latest" "$latest_digest" "$latest_revision" "$latest_created" "$latest_observed" "false"; then
    echo "Buzz operation completed, but Hermes could not save its update state." >&2
  fi
  echo "$message" >&2
  exit 1
}

STARTED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
DEADLINE_EPOCH=$(($(date +%s) + EXECUTION_TIMEOUT))
if old_image_id=$(container_image_id); then
  :
else
  status=$?
  if [ "$status" -eq 124 ]; then
    fail_update "timed_out" "The Buzz update exceeded its safe execution window while inspecting the running relay." "" "" "" "" "" "" "false"
  fi
  fail_update "verification_failed" "Hermes could not inspect the running Buzz relay before the update." "" "" "" "" "" "" "false"
fi
if [ "$TRIGGER" = "scheduled" ] && [ -z "$old_image_id" ]; then
  fail_update "stopped" "Automatic update failed because Buzz is stopped. Pause the Hermes job during maintenance, or update manually to start it." "" "" "" "" "" "" "false"
fi

if docker_image pull "$IMAGE" > "$OPERATION_OUTPUT" 2>&1; then
  :
else
  status=$?
  if [ "$status" -eq 124 ]; then
    fail_update "timed_out" "The Buzz update exceeded its safe execution window while pulling the relay image; the running relay was not changed." "$old_image_id" "$old_image_id" "" "" "" "" "false"
  fi
  detail=$(failure_detail)
  message="Buzz could not check or pull the relay image; the running relay was not changed."
  [ -z "$detail" ] || message="$message $detail"
  fail_update "pull_failed" "$message" "$old_image_id" "$old_image_id" "" "" "" "" "false"
fi
rm -f "$OPERATION_OUTPUT"

if image_metadata=$(docker_image inspect \
    --format '{{.Id}}|{{json .RepoDigests}}|{{index .Config.Labels "org.opencontainers.image.revision"}}|{{index .Config.Labels "org.opencontainers.image.created"}}|END' \
    "$IMAGE" 2>/dev/null); then
  :
else
  status=$?
  if [ "$status" -eq 124 ]; then
    fail_update "timed_out" "The Buzz update exceeded its safe execution window while inspecting the pulled relay image." "$old_image_id" "$old_image_id" "" "" "" "" "false"
  fi
  fail_update "pull_failed" "Buzz pulled the relay tag but could not inspect its image identity." "$old_image_id" "$old_image_id" "" "" "" "" "false"
fi
old_ifs=$IFS
IFS='|'
set -f
set -- $image_metadata
set +f
IFS=$old_ifs
if [ "$#" -lt 5 ] || [ -z "$1" ]; then
  fail_update "pull_failed" "Buzz pulled the relay tag but could not inspect its image identity." "$old_image_id" "$old_image_id" "" "" "" "" "false"
fi
new_image_id=$1
repo_digests=$2
latest_revision=$3
latest_created=$4
latest_digest=$(printf '%s' "$repo_digests" | sed -n 's/.*@\(sha256:[A-Za-z0-9]*\).*/\1/p')

if [ -n "$old_image_id" ] && [ "$old_image_id" = "$new_image_id" ]; then
  if health=$(container_health); then
    :
  else
    status=$?
    if [ "$status" -eq 124 ]; then
      fail_update "timed_out" "The Buzz update exceeded its safe execution window while verifying relay health." "$old_image_id" "$old_image_id" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
    fi
    fail_update "verification_failed" "Hermes could not inspect the running Buzz relay health." "$old_image_id" "$old_image_id" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
  fi
  if [ -z "$health" ]; then
    fail_update "verification_failed" "Hermes received no health state for the running Buzz relay." "$old_image_id" "$old_image_id" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
  fi
  if [ "$health" != "healthy" ]; then
    fail_update "unhealthy" "Buzz is on the latest image, but the running relay is not healthy." "$old_image_id" "$old_image_id" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
  fi
  if ! write_state "already_current" "" "$old_image_id" "$old_image_id" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true" "true"; then
    echo "Buzz is current, but Hermes could not save its update state." >&2
    exit 1
  fi
  if [ "$TRIGGER" = "manual" ]; then
    echo "RESULT=already-current"
  fi
  exit 0
fi

if compose up -d --wait --no-deps --pull never "$SERVICE" > "$OPERATION_OUTPUT" 2>&1; then
  :
else
  status=$?
  if [ "$status" -eq 124 ]; then
    fail_update "timed_out" "The Buzz update exceeded its safe execution window while applying the pulled relay image; inspect the actual relay state before retrying." "$old_image_id" "" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
  fi
  if running_after=$(container_image_id); then
    :
  else
    inspect_status=$?
    if [ "$inspect_status" -eq 124 ]; then
      fail_update "timed_out" "The Buzz update exceeded its safe execution window while inspecting a failed apply." "$old_image_id" "" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
    fi
    running_after=""
  fi
  detail=$(failure_detail)
  message="Buzz could not apply the pulled relay image; inspect the relay logs before retrying."
  [ -z "$detail" ] || message="$message $detail"
  fail_update "apply_failed" "$message" "$old_image_id" "$running_after" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
fi
rm -f "$OPERATION_OUTPUT"

if running_image_id=$(container_image_id); then
  :
else
  status=$?
  if [ "$status" -eq 124 ]; then
    fail_update "timed_out" "The Buzz update exceeded its safe execution window while verifying the running image." "$old_image_id" "" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
  fi
  fail_update "verification_failed" "Hermes could not inspect the running Buzz relay after applying the image." "$old_image_id" "" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
fi
if [ -z "$running_image_id" ]; then
  fail_update "verification_failed" "Hermes found no running Buzz relay after applying the image." "$old_image_id" "" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
fi
if [ "$running_image_id" != "$new_image_id" ]; then
  fail_update "verification_failed" "The running Buzz relay does not match the image pulled for this update." "$old_image_id" "$running_image_id" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
fi
if health=$(container_health); then
  :
else
  status=$?
  if [ "$status" -eq 124 ]; then
    fail_update "timed_out" "The Buzz update exceeded its safe execution window while verifying relay health." "$old_image_id" "$running_image_id" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
  fi
  fail_update "verification_failed" "Hermes could not inspect the updated Buzz relay health." "$old_image_id" "$running_image_id" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
fi
if [ -z "$health" ]; then
  fail_update "verification_failed" "Hermes received no health state for the updated Buzz relay." "$old_image_id" "$running_image_id" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
fi
if [ "$health" != "healthy" ]; then
  fail_update "unhealthy" "The updated Buzz relay did not become healthy; no rollback was attempted." "$old_image_id" "$running_image_id" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true"
fi

if ! write_state "updated" "" "$old_image_id" "$running_image_id" "$new_image_id" "$latest_digest" "$latest_revision" "$latest_created" "true" "true"; then
  echo "Buzz was updated, but Hermes could not save its update state." >&2
  exit 1
fi
if [ "$TRIGGER" = "manual" ]; then
  echo "RESULT=updated"
fi
