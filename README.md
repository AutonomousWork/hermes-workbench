# Hermes Workbench

Opinionated plugins and themes for shaping Hermes Agent into an integrated command center across the autonomous work stack.

Each addition is intentionally designed to be lightweight and non-duplicative with core functionality.

## Installing plugins

Plugin installers symlink their source directory into `~/.hermes/plugins/`.
The symlink follows the checkout path, not its Git branch, so install from a
persistent checkout of `main`. If you use your primary checkout for feature
branches, keep a dedicated `main` worktree for installed plugins:

```sh
git fetch origin main
git worktree add ../hermes-workbench-main main
../hermes-workbench-main/plugins/buzz-control/scripts/install.sh
```

Update that worktree with `git -C ../hermes-workbench-main pull --ff-only`.
Switching the linked checkout to a branch that omits a plugin makes Hermes stop
discovering it until the checkout or symlink is corrected.

## Plugins

### [Buzz Control](./plugins/buzz-control)

Monitor a local Buzz relay, see its configured location and latest image, and safely apply updates from the Hermes dashboard on demand or every 12 hours.

![Hermes Dashboard using the Buzz Control plugin](./plugins/buzz-control/hermes-buzz.png)

### [Nesquena WebUI Control](./plugins/nesquena-webui-control)

Start, stop, restart, and inspect a launchd-managed Nesquena WebUI from the Hermes Agent web dashboard.

![Hermes Dashboard using the Nesquena WebUI Control plugin](./plugins/nesquena-webui-control/nesquena-webui-control.png)

## Themes

### [Linear](./themes/linear)

An unofficial, Linear-inspired dark theme for the built-in Hermes Agent web dashboard.

![Hermes Dashboard using the Linear theme](./themes/linear/hermes-linear.png)

## Principles

- **Opinionated by design** — strong defaults instead of endless configuration
- **Built for autonomous work** — focused on visibility, control, and operational confidence
- **Modular** — install only the plugins and themes that fit your workflow
- **Cohesive** — individual extensions should feel like parts of the same workbench
