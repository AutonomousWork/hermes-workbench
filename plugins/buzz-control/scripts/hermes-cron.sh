#!/bin/sh
set -eu

HERMES_ROOT=${HERMES_HOME:-"$HOME/.hermes"}
UPDATER="$HERMES_ROOT/plugins/buzz-control/scripts/update.sh"

if [ ! -x "$UPDATER" ]; then
  echo "Buzz scheduled updater is not installed or executable." >&2
  exit 1
fi

# Successful scheduled checks are deliberately silent. Hermes records and
# delivers only the updater's sanitized failure text.
if ! output=$("$UPDATER" scheduled 2>&1); then
  printf '%s\n' "$output" >&2
  exit 1
fi
