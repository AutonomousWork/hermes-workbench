#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname -- "$SCRIPT_DIR")
PLUGIN_ROOT=${HERMES_HOME:-"$HOME/.hermes"}/plugins
INSTALL_PATH="$PLUGIN_ROOT/nesquena-webui-control"

mkdir -p "$PLUGIN_ROOT"

if [ -L "$INSTALL_PATH" ] && [ "$(readlink "$INSTALL_PATH")" = "$REPO_DIR" ]; then
  echo "NesQuena WebUI Control is already installed at $INSTALL_PATH"
elif [ -L "$INSTALL_PATH" ] && [ -f "$INSTALL_PATH/plugin.yaml" ] && \
  grep -Eq '^[[:space:]]*name:[[:space:]]*nesquena-webui-control[[:space:]]*$' \
    "$INSTALL_PATH/plugin.yaml"; then
  ln -sfn "$REPO_DIR" "$INSTALL_PATH"
  echo "Updated NesQuena WebUI Control at $INSTALL_PATH"
elif [ -e "$INSTALL_PATH" ] || [ -L "$INSTALL_PATH" ]; then
  echo "Refusing to replace existing path: $INSTALL_PATH" >&2
  exit 1
else
  ln -s "$REPO_DIR" "$INSTALL_PATH"
  echo "Installed NesQuena WebUI Control at $INSTALL_PATH"
fi

if HERMES_BIN=$(command -v hermes 2>/dev/null); then
  :
elif [ -x "$HOME/.local/bin/hermes" ]; then
  HERMES_BIN="$HOME/.local/bin/hermes"
else
  echo "Hermes CLI not found. Enable the plugin manually after installing Hermes." >&2
  exit 1
fi

"$HERMES_BIN" plugins enable --no-allow-tool-override nesquena-webui-control
echo "Restart hermes dashboard once so its Python API routes are mounted."
