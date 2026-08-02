# Hermes NesQuena WebUI Control

A standalone plugin for the official `hermes dashboard` that controls the
NesQuena WebUI LaunchAgent on a Mac Mini.

The plugin adds a **Nesquena WebUI** tab alongside the built-in Kanban and
Achievements tabs. It provides four authenticated controls:

- **Start** — enables and bootstraps `ai.hermes.webui` from its existing plist.
- **Stop** — boots the job out of launchd so its `KeepAlive` policy cannot
  immediately respawn it.
- **Restart** — runs a forced launchd kickstart.
- **Status** — reports launchd state, PID, last exit code, and HTTP health on
  `127.0.0.1:8787`.

The backend never invokes a shell and accepts no command, path, label, or port
from the browser. Hermes' normal dashboard authentication protects every
plugin API route.

## Requirements

- macOS, with the Hermes dashboard and NesQuena WebUI running as the same user.
- LaunchAgent plist at `~/Library/LaunchAgents/ai.hermes.webui.plist`.
- NesQuena WebUI on `127.0.0.1:8787`.

## Install

Clone this repository anywhere, then run:

```sh
./scripts/install.sh
```

The installer creates this symlink and enables the plugin without granting it
permission to override any built-in Hermes tools:

```text
~/.hermes/plugins/nesquena-webui-control -> <this repository>
```

Restart `hermes dashboard` once after installation. Backend API routes are
mounted only at dashboard startup; a plugin rescan alone is not sufficient.

If you install manually, explicitly allow-list the user plugin before
restarting the dashboard:

```sh
hermes plugins enable --no-allow-tool-override nesquena-webui-control
```

## API

Hermes mounts the plugin backend at:

```text
GET  /api/plugins/nesquena-webui-control/status
POST /api/plugins/nesquena-webui-control/start
POST /api/plugins/nesquena-webui-control/stop
POST /api/plugins/nesquena-webui-control/restart
```

## Tests

```sh
python3 -m unittest discover -s tests -v
node --check dashboard/dist/index.js
```
