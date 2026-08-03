# Nesquena WebUI Control for Hermes

A dashboard plugin that controls a launchd-managed
[Nesquena WebUI](https://github.com/nesquena/hermes-webui) from the official
`hermes dashboard`.

It adds a **Nesquena WebUI** tab with four authenticated operations:

- **Start** enables and bootstraps the configured per-user LaunchAgent.
- **Stop** boots the job out of launchd so `KeepAlive` cannot immediately
  respawn it.
- **Restart** performs a forced launchd kickstart.
- **Status** reports launchd state, PID, last exit code, and WebUI health.

## Requirements

- macOS.
- A current Hermes Agent installation with `hermes dashboard`.
- Nesquena WebUI running as a per-user LaunchAgent under the same macOS user as
  the Hermes dashboard.
- The WebUI listening on loopback (port `8787` by default).

If you still run Nesquena WebUI directly with `bootstrap.py`, `start.sh`,
`ctl.sh`, Docker, or systemd, this plugin is not the right process controller.
Configure a macOS LaunchAgent first; Nesquena's
[supervisor guide](https://github.com/nesquena/hermes-webui/blob/master/docs/supervisor.md#launchd-macos)
contains a template.

## Install

From a clone of `hermes-workbench`, run:

```sh
./plugins/nesquena-webui-control/scripts/install.sh
```

The installer creates a source symlink at the user-plugin location from the
[official Hermes dashboard extension documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/extending-the-dashboard):

```text
~/.hermes/plugins/nesquena-webui-control -> <clone>/plugins/nesquena-webui-control
```

It then enables `nesquena-webui-control` in Hermes' plugin allow-list. The
package includes an inert lifecycle shim for that enablement step; it does not
register Hermes tools or hooks.

Because this is a source symlink, keep the repository clone in place. Use the
manual copy method below if you want an installation that is independent of the
clone.

Then start or restart the dashboard:

```sh
hermes dashboard
```

A restart is required because Hermes mounts `plugin_api.py` routes only when
the dashboard process starts.

### Manual installation

On a first install, you can copy the directory instead of using the symlink
installer:

```sh
mkdir -p "$HOME/.hermes/plugins"
cp -R plugins/nesquena-webui-control "$HOME/.hermes/plugins/"
hermes plugins enable --no-allow-tool-override nesquena-webui-control
hermes dashboard
```

The resulting layout must keep the `dashboard/` directory intact:

```text
~/.hermes/plugins/nesquena-webui-control/
├── plugin.yaml
├── __init__.py
└── dashboard/
    ├── manifest.json
    ├── plugin_api.py
    └── dist/
        ├── index.js
        └── style.css
```

The JavaScript bundle is committed and ready to load; users do not need Node,
npm, or a frontend build step.

## Configure the LaunchAgent

The maintainer defaults are useful out of the box for an installation using
`ai.hermes.webui` on port `8787`. LaunchAgent labels are user-defined, so set
these variables in the environment that starts `hermes dashboard` when your
setup differs:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NESQUENA_WEBUI_LAUNCHD_LABEL` | `ai.hermes.webui` | LaunchAgent label passed to `launchctl`. |
| `NESQUENA_WEBUI_PLIST` | `~/Library/LaunchAgents/<label>.plist` | Plist used when starting an unloaded job. |
| `NESQUENA_WEBUI_PORT` | `8787` | Loopback port probed at `/health`. |

For example:

```sh
NESQUENA_WEBUI_LAUNCHD_LABEL=com.example.hermes-webui \
NESQUENA_WEBUI_PLIST="$HOME/Library/LaunchAgents/com.example.hermes-webui.plist" \
NESQUENA_WEBUI_PORT=9000 \
hermes dashboard
```

These values are read at dashboard startup. If the dashboard itself runs under
a supervisor, add them to that service's environment and restart it.

## Verify and troubleshoot

After restarting, open the dashboard and select **Nesquena WebUI**. You can
also confirm Hermes discovered the manifest:

```sh
curl -fsS http://127.0.0.1:9119/api/dashboard/plugins
```

Common failure modes:

- **The tab is missing:** confirm
  `~/.hermes/plugins/nesquena-webui-control/dashboard/manifest.json` exists,
  run `hermes plugins enable --no-allow-tool-override
  nesquena-webui-control`, then restart the dashboard.
- **Controls return 404:** restart `hermes dashboard`; a plugin rescan reloads
  browser assets but does not mount new Python routes.
- **The service always appears stopped:** check the label with
  `launchctl print gui/$(id -u)/<label>` and set
  `NESQUENA_WEBUI_LAUNCHD_LABEL` to the exact plist label.
- **The service runs but is unhealthy:** check
  `curl -fsS http://127.0.0.1:<port>/health` and configure
  `NESQUENA_WEBUI_PORT` if necessary.
- **The plugin fails to import:** inspect `~/.hermes/logs/errors.log`.

## Security model

The browser cannot choose a command, path, service label, host, or port. The
backend invokes `/bin/launchctl` directly without a shell and uses only settings
fixed when the dashboard starts. Hermes' normal dashboard authentication
protects the plugin API routes.

Keep the dashboard on loopback unless you have deliberately configured Hermes'
remote-access authentication. Any authenticated dashboard user can operate the
LaunchAgent.

## API

Hermes mounts the backend at:

```text
GET  /api/plugins/nesquena-webui-control/status
POST /api/plugins/nesquena-webui-control/start
POST /api/plugins/nesquena-webui-control/stop
POST /api/plugins/nesquena-webui-control/restart
```

## Development

Run the checks from the `hermes-workbench` repository root:

```sh
python3 -m unittest discover -s plugins/nesquena-webui-control/tests -v
node --check plugins/nesquena-webui-control/dashboard/dist/index.js
```

The plugin is available under the [MIT License](./LICENSE).
