#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname -- "$SCRIPT_DIR")
PLUGIN_ROOT=${HERMES_HOME:-"$HOME/.hermes"}/plugins
INSTALL_PATH="$PLUGIN_ROOT/buzz-control"

mkdir -p "$PLUGIN_ROOT"

if [ -L "$INSTALL_PATH" ] && [ "$(readlink "$INSTALL_PATH")" = "$REPO_DIR" ]; then
  echo "Buzz Control is already installed at $INSTALL_PATH"
elif [ -L "$INSTALL_PATH" ] && \
  [ -f "$INSTALL_PATH/dashboard/manifest.json" ] && \
  grep -Eq '"name"[[:space:]]*:[[:space:]]*"buzz-control"' \
    "$INSTALL_PATH/dashboard/manifest.json"; then
  ln -sfn "$REPO_DIR" "$INSTALL_PATH"
  echo "Updated Buzz Control at $INSTALL_PATH"
elif [ -e "$INSTALL_PATH" ] || [ -L "$INSTALL_PATH" ]; then
  echo "Refusing to replace existing path: $INSTALL_PATH" >&2
  exit 1
else
  ln -s "$REPO_DIR" "$INSTALL_PATH"
  echo "Installed Buzz Control at $INSTALL_PATH"
fi

if HERMES_BIN=$(command -v hermes 2>/dev/null); then
  :
elif [ -x "$HOME/.local/bin/hermes" ]; then
  HERMES_BIN="$HOME/.local/bin/hermes"
else
  echo "Hermes CLI not found. Install Hermes, then run:" >&2
  echo "  hermes plugins enable --no-allow-tool-override buzz-control" >&2
  exit 1
fi

"$HERMES_BIN" plugins enable --no-allow-tool-override buzz-control
echo "Enabled Buzz Control."

SCRIPTS_DIR=${HERMES_HOME:-"$HOME/.hermes"}/scripts
STATE_DIR=${HERMES_HOME:-"$HOME/.hermes"}/state/buzz-control
WRAPPER_NAME=buzz-control-update.sh
WRAPPER_PATH="$SCRIPTS_DIR/$WRAPPER_NAME"
JOB_ID_FILE="$STATE_DIR/cron-job-id"
JOB_NAME=buzz-control-image-update
DEFAULT_SCHEDULE=${BUZZ_CONTROL_SCHEDULE:-"every 12h"}

mkdir -p "$SCRIPTS_DIR" "$STATE_DIR"
chmod 700 "$SCRIPTS_DIR" "$STATE_DIR"
wrapper_temp="$SCRIPTS_DIR/.buzz-control-update.$$"
cp "$SCRIPT_DIR/hermes-cron.sh" "$wrapper_temp"
chmod 700 "$wrapper_temp"
mv "$wrapper_temp" "$WRAPPER_PATH"

effective_timeout=${HERMES_CRON_SCRIPT_TIMEOUT:-}
HERMES_PY=${HERMES_HOME:-"$HOME/.hermes"}/hermes-agent/venv/bin/python
if [ -z "$effective_timeout" ] && [ -x "$HERMES_PY" ]; then
  effective_timeout=$(
    "$HERMES_PY" -c 'from cron.scheduler import _get_script_timeout; print(int(_get_script_timeout()))' \
      2>/dev/null || true
  )
fi
effective_timeout=${effective_timeout:-3600}
case "$effective_timeout" in
  ''|*[!0-9]*)
    echo "Could not determine the Hermes cron script timeout." >&2
    exit 1
    ;;
esac
if [ "$effective_timeout" -lt 1200 ]; then
  echo "Hermes cron scripts must be allowed at least 1200 seconds for safe Buzz updates." >&2
  exit 1
fi

job_ref=$JOB_NAME
if [ -f "$JOB_ID_FILE" ]; then
  stored_id=$(sed -n '1p' "$JOB_ID_FILE")
  case "$stored_id" in
    ''|*[!A-Za-z0-9_-]*) ;;
    *) job_ref=$stored_id ;;
  esac
fi

edit_job() {
  output=$("$HERMES_BIN" cron edit "$1" --script "$WRAPPER_NAME" --no-agent 2>&1) || {
    printf '%s\n' "$output"
    return 1
  }
  printf '%s\n' "$output"
  case "$output" in
    *"Updated job:"*) return 0 ;;
    *) return 1 ;;
  esac
}

edit_output=""
if edit_output=$(edit_job "$job_ref"); then
  :
elif [ "$job_ref" != "$JOB_NAME" ] && edit_output=$(edit_job "$JOB_NAME"); then
  :
else
  case "$edit_output" in
    *"Job not found:"*)
      if ! edit_output=$(
        "$HERMES_BIN" cron create "$DEFAULT_SCHEDULE" \
          --name "$JOB_NAME" \
          --deliver local \
          --script "$WRAPPER_NAME" \
          --no-agent 2>&1
      ); then
        printf '%s\n' "$edit_output" >&2
        exit 1
      fi
      case "$edit_output" in
        *"Created job:"*) ;;
        *)
          printf '%s\n' "$edit_output" >&2
          exit 1
          ;;
      esac
      ;;
    *)
      printf '%s\n' "$edit_output" >&2
      exit 1
      ;;
  esac
fi

job_id=$(printf '%s\n' "$edit_output" | sed -E -n 's/.*(Created|Updated) job: ([A-Za-z0-9_-]*).*/\2/p' | tail -n 1)
if [ -n "$job_id" ]; then
  printf '%s\n' "$job_id" > "$JOB_ID_FILE"
  chmod 600 "$JOB_ID_FILE"
fi

echo "Installed the Buzz image check in Hermes (new jobs default to every 12 hours)."
if ! "$HERMES_BIN" cron status >/dev/null 2>&1; then
  echo "Warning: Hermes cron is installed, but its scheduler status could not be verified." >&2
fi
echo "Restart hermes dashboard so its Python API routes are mounted."
