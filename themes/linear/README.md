# Linear theme for the Hermes Dashboard

An unofficial, Linear-inspired dark theme for the built-in Hermes Agent web dashboard. It changes styling only. It does not modify dashboard components, install a plugin, or patch Hermes source code.

![Hermes Dashboard using the Linear theme](./hermes-linear.png)

## What it changes

- Near-black surfaces with quiet borders
- Inter for interface text
- JetBrains Mono for code and terminal text
- Violet focus accents
- Compact Linear-style cards, controls, dialogs, and navigation states
- Disables the decorative animated sheen on buttons

The fonts are loaded from Google Fonts when available. If they cannot be reached, the theme falls back to system sans-serif and monospace fonts.

## Install

Open a terminal in this folder and run:

```bash
mkdir -p "$HOME/.hermes/dashboard-themes"
cp linear.yaml "$HOME/.hermes/dashboard-themes/linear.yaml"
```

Then start the Hermes dashboard if it is not already running:

```bash
hermes dashboard
```

Refresh the dashboard, click the **palette icon** in the header, and select **Linear**. Hermes persists the selection in its configuration.

## Update or remove

To update the theme, replace the installed YAML and refresh the dashboard:

```bash
cp linear.yaml "$HOME/.hermes/dashboard-themes/linear.yaml"
```

To remove it:

```bash
rm "$HOME/.hermes/dashboard-themes/linear.yaml"
```

Switch to another theme before removing it, or select another theme after refreshing.

## Troubleshooting

### Linear does not appear in the theme picker

1. Confirm the file is located at:

   ```text
   ~/.hermes/dashboard-themes/linear.yaml
   ```

2. Confirm the filename ends in `.yaml` or `.yml`.
3. Refresh the browser page.
4. If Hermes is running on the default local port, check theme discovery:

   ```bash
   curl http://127.0.0.1:9119/api/dashboard/themes
   ```

   The response should contain a theme named `linear`.

5. Check `~/.hermes/logs/errors.log` for a YAML parse error.
6. If the installed Hermes version predates dashboard theme support, update Hermes and try again:

   ```bash
   hermes update
   ```

## Reference

Hermes dashboard extension documentation:

https://hermes-agent.nousresearch.com/docs/user-guide/features/extending-the-dashboard

## Note

This is an unofficial community theme inspired by Linear's visual language. It is not affiliated with or endorsed by Linear or Nous Research.
