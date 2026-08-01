#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname -- "$SCRIPT_DIR")
PLUGIN_ROOT=${HERMES_HOME:-"$HOME/.hermes"}/plugins
INSTALL_PATH="$PLUGIN_ROOT/nesquena-webui-control"

mkdir -p "$PLUGIN_ROOT"

if [ -L "$INSTALL_PATH" ] && [ "$(readlink "$INSTALL_PATH")" = "$REPO_DIR" ]; then
  echo "NesQuena WebUI Control is already installed at $INSTALL_PATH"
  exit 0
fi

if [ -e "$INSTALL_PATH" ] || [ -L "$INSTALL_PATH" ]; then
  echo "Refusing to replace existing path: $INSTALL_PATH" >&2
  exit 1
fi

ln -s "$REPO_DIR" "$INSTALL_PATH"
echo "Installed NesQuena WebUI Control at $INSTALL_PATH"
echo "Restart hermes dashboard once so its Python API routes are mounted."
