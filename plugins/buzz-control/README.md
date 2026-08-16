# Buzz Control for Hermes

Buzz Control manages a local [Buzz](https://github.com/block/buzz) relay from
the authenticated Hermes dashboard. The **Buzz** tab shows runtime health,
image-update state, and the managed schedule. **Configure Buzz** opens a
protected control panel for stable public-address and access-policy settings
without leaving Hermes.

![Hermes Dashboard using the Buzz Control plugin](./hermes-buzz.png)

## What the control panel guarantees

- The browser receives only the bounded operator settings documented below.
  Secrets, database and storage configuration, image and port settings, and
  unknown assignments are not projected by the API at all.
- Save and Apply are separate. **Save changes** atomically stages a desired
  revision and never restarts Buzz. **Review Apply** obtains a fresh one-use
  confirmation for that exact revision.
- Comments, blank lines, assignment order, unknown assignments, Unicode, and
  the existing newline style are preserved. Duplicate keys, multiline values,
  invalid UTF-8, and ambiguous syntax make the document read-only until it is
  repaired manually.
- Saved values are encoded as literal Compose dotenv values, so quotes,
  dollar signs, comment characters, and trailing backslashes are not
  reinterpreted as interpolation or syntax.
- Every assignment outside the managed set is preserved byte-for-byte and
  cannot be created, changed, or named through the browser API.
- The browser cannot supply a file path, Compose file, project, service, image,
  Docker endpoint, or command.

The protected desired environment remains outside Git at:

```text
~/.config/buzz/prod.env
```

It must be a regular, single-link, non-symlink file owned by the current user
with exact mode `0600`, inside a directory that is not writable by another
user. The store revalidates these properties before every read or update.

## Desired, applied, and recovery state

Buzz Control keeps separate secret-bearing artifacts:

| Artifact | Purpose |
| --- | --- |
| `~/.config/buzz/prod.env` | Desired configuration edited by the operator. |
| `~/.hermes/state/buzz-control/applied.env` | Last configuration that passed Apply or verified adoption. |
| `~/.hermes/state/buzz-control/operation.env` | Exact confirmed revision used for one in-flight Apply or adoption. |
| `~/.hermes/state/buzz-control/pre-save.env` | One bounded recovery copy of the desired file before its latest write. |

The state directory is mode `0700`; secret-bearing files are atomically written
with mode `0600`. `operation.json` is a non-secret, names-only operation journal.
`operation-intents.json` contains only short-lived confirmation digests, never
the confirmation token or configuration values. None of these files has a
dashboard download route.

An upgraded installation with no `applied.env` starts in
**`baseline_missing`**. This is intentional: installation and GET requests do
not silently declare an unverified production file safe. Save, Apply, and relay
recreation remain blocked until the operator explicitly adopts the current
healthy runtime. Image checks may still pull and compare the public image.

## Managed settings

| Group | Fields | Save | Automatic Apply |
| --- | --- | --- | --- |
| Public address | `BUZZ_DOMAIN`, `RELAY_URL`, `BUZZ_MEDIA_BASE_URL`, `BUZZ_MEDIA_SERVER_DOMAIN`, `BUZZ_CORS_ORIGINS` | Yes | Yes |
| Access policy | `BUZZ_REQUIRE_AUTH_TOKEN`, `BUZZ_REQUIRE_RELAY_MEMBERSHIP`, `BUZZ_ALLOW_NIP_OA_AUTH` | Yes | Yes, with an elevated warning |
| Owner identity | `RELAY_OWNER_PUBKEY` | Read-only | No |
| Advanced deployment | Every other existing assignment | Outside Hermes only | No |

This boundary is intentionally semantic rather than an exhaustive mirror of
Buzz's runtime environment. New upstream tuning variables remain preserved and
unmanaged until they belong to one of these stable operator concepts.

`BUZZ_HTTP_PORT` is probed from the selected protected snapshot. If a trusted
`BUZZ_CONTROL_LOCAL_PORT` override disagrees, reconciliation stops before any
container mutation.

## Apply and rollback

The reconciler is the sole owner of the cross-process lock and of every Compose
mutation. For configuration Apply it:

1. Consumes the one-use confirmation from standard input and revalidates its
   revision and impact policy under the lock.
2. Copies that revision to protected `operation.env` and uses only that file
   for Compose validation, recreation, port selection, and health checks.
3. Captures the current immutable image ID; a mutable tag is never the rollback
   identity.
4. Recreates only `relay` with `--no-deps --pull never --wait`.
5. Requires Docker health and the loopback HTTP liveness probe.
6. Promotes the verified operation snapshot to last-applied only after both
   checks pass and the live desired file still matches the confirmed revision.

If recreation or verification fails after runtime mutation, the reconciler
recreates `relay` from `applied.env` on the prior immutable image. A verified
rollback leaves the desired revision pending so it can be corrected or
restored. An unverified rollback enters **degraded** state and blocks ordinary
Save, Apply, and image recreation until the runtime is recovered manually and
the attested adoption flow verifies and records that recovery. Interrupted
blocking phases fail closed rather than replaying a mutation and use the same
recovery-adoption path.

A stopped relay requires the distinct **Apply and start Buzz** confirmation.
Scheduled image updates never start a stopped relay.

## Image-update behavior

A `no_agent` Hermes cron job named `buzz-control-image-update` runs every
**12 hours** by default. Manual and scheduled image updates use the same
reconciler and lock:

1. Select `applied.env` whenever an applied baseline exists; a scheduled update
   cannot consume a later external edit or bypass Save/Apply policy.
2. Pull `ghcr.io/block/buzz:main` using an authentication-free Docker config.
3. Compare immutable image IDs.
4. Recreate only `relay` when the ID changed and an applied baseline exists.
5. Verify Docker health and atomically save a non-secret dashboard receipt.

Reinstalling refreshes the wrapper while preserving an operator-edited cadence,
paused state, and delivery setting.

The Buzz dashboard can switch the existing job between **Scheduled** and
**Manual only** without deleting it. Scheduled mode offers conservative cadence
presets from hourly through weekly and updates only the job's schedule through
Hermes's authenticated Cron API. Manual-only mode pauses automatic checks;
**Update Buzz** remains available for an operator-triggered check.

## Advanced maintenance and adoption

Secrets, database, Redis, S3, network, image, port, migration, relay-identity,
HMAC, and unknown-field changes stay outside the browser control panel.

1. Pause `buzz-control-image-update` in Hermes Cron. This prevents image
   reconciliation during the external maintenance window.
2. Acquire an exclusive lock on
   `~/.hermes/state/buzz-control/operation.lock` for the full external
   read/edit/atomic-replace sequence, then edit the protected production
   environment and rotate or recreate affected services using your
   infrastructure runbook. The dashboard's write-boundary identity check
   rejects ordinary races, but it does not replace this shared-lock contract.
   Do not expose credentials in shell history or logs.
3. Verify the relay is running with Docker health and its HTTP liveness probe.
4. Return to the control panel and choose **Adopt current healthy configuration**.
5. Resume the schedule only after adoption reports an established baseline.

Adopt performs Compose validation plus current container, immutable image,
Compose configuration-hash equality, Docker-health, and HTTP checks against one
protected revision snapshot under the shared lock. From degraded or interrupted
state, the same authenticated and attested action becomes a one-use recovery
adoption and records a terminal recovered result. It refuses to adopt a healthy
container whose effective Compose service configuration differs from the
confirmed desired revision. It never restarts a service and never returns
environment contents.

## Recovery choices

- **Restore last applied** replaces only the desired file from `applied.env`;
  it does not restart Buzz.
- After a verified rollback, correct and Save the pending desired revision, or
  restore it.
- In degraded or interrupted state, keep the image schedule paused, inspect the
  runtime outside the dashboard, restore the prior image/configuration if
  needed, verify health, then use the documented adoption flow.
- `pre-save.env` is the single host-side recovery copy for an accidental Save.
  It is intentionally not browsable through the plugin.

## Authentication and trust boundary

Hermes v0.20 protects `/api/plugins/...` in both loopback session-token and
non-loopback gated-session modes. Configuration mutations additionally require
an exact same-origin browser request, JSON content type, bounded declared and
streamed bodies, and stable value-free error responses. Configuration responses
use `Cache-Control: no-store`.

Hermes does not currently expose a plugin-specific administrator role. Any
authenticated dashboard user can operate Buzz Control. Every **enabled plugin**
runs in the same page origin, so enable only trusted dashboard plugins: a
malicious same-origin plugin or host XSS is inside this trust boundary.

For a non-loopback dashboard, configure Hermes or the trusted reverse proxy to
deny framing for the entire dashboard, for example with
`Content-Security-Policy: frame-ancestors 'none'` (and `X-Frame-Options: DENY`
for older clients). The config API emits both headers, but host-level policy is
needed to protect the whole page.

This feature adds **no agent** hook, tool, prompt context, or agent-callable
configuration projection. `plugin.yaml` keeps `hooks: []`, `__init__.py` is an
inert allow-list shim, the UI uses the authenticated SDK, and installation keeps
`--no-allow-tool-override`.

## Install and upgrade

From a persistent checkout, run:

```sh
./plugins/buzz-control/scripts/install.sh
```

The installer symlinks the plugin into `~/.hermes/plugins/`, enables it, creates
private scripts/state directories, installs a regular cron wrapper, and creates
the 12-hour no-agent job only when absent. It never reads, copies, prints, or
adopts `prod.env`. Restart the Hermes dashboard so its Python API routes are
mounted.

The configured Compose executable must support `compose config --hash SERVICE`;
Buzz Control uses that hash to prove that adoption matches the desired service
configuration. Unsupported Compose versions fail closed before adoption.

After upgrading an existing installation:

1. Restart Hermes.
2. Open **Buzz → Configure Buzz** and expect `baseline_missing`.
3. Confirm the existing relay is healthy and its environment is the desired
   production configuration.
4. Pause the image schedule, adopt the current healthy configuration, then
   resume the schedule.

## Trusted server configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BUZZ_CONTROL_DOCKER_BIN` | `/usr/local/bin/docker` | Fixed Docker CLI path. |
| `BUZZ_CONTROL_COMPOSE_BIN` | `/usr/local/bin/docker-compose` | Fixed Compose CLI path. |
| `BUZZ_CONTROL_PYTHON_BIN` | `/usr/bin/python3` | Fixed interpreter used by the scheduled reconciler wrapper. |
| `BUZZ_CONTROL_DOCKER_HOST` | `unix:///var/run/docker.sock` | Local Docker endpoint; only Unix sockets are accepted. |
| `BUZZ_CONTROL_ENV_FILE` | `~/.config/buzz/prod.env` | Protected desired production environment. |
| `BUZZ_CONTROL_STATE_DIR` | `~/.hermes/state/buzz-control` | Protected snapshots, journal, lock, and update receipt. |
| `BUZZ_CONTROL_DEPLOY_DIR` | plugin `deploy/` directory | Vendored Compose capsule. |
| `BUZZ_CONTROL_COMPOSE_PROJECT` | `buzz-prod` | Fixed Compose project. |
| `BUZZ_CONTROL_COMPOSE_SERVICE` | `relay` | Only service reconciled. |
| `BUZZ_CONTROL_IMAGE` | `ghcr.io/block/buzz:main` | Mutable channel for the separate image workflow. |
| `BUZZ_CONTROL_DASHBOARD_ORIGIN` | request's server-validated origin | Optional exact trusted dashboard origin behind a proxy. |
| `BUZZ_CONTROL_LOCAL_HOST` | `127.0.0.1` | Loopback health-probe host. |
| `BUZZ_CONTROL_LOCAL_PORT` | selected `BUZZ_HTTP_PORT` or `3300` | Optional trusted health-port override; conflicts block mutation. |
| `BUZZ_CONTROL_LOCAL_URL` | selected local host and runtime port | Optional trusted local listener URL shown in Hermes. |
| `BUZZ_CONTROL_HEALTH_PATH` | `/_liveness` | Fixed liveness path. |
| `BUZZ_CONTROL_SCHEDULE` | `every 12h` | First-install cadence only. |
| `BUZZ_CONTROL_RELAY_URL` | `ws://127.0.0.1:3300` | Relay location shown in Hermes. |
| `BUZZ_CONTROL_NETWORK_SCOPE` | `Local only` | Access scope shown beside the relay. |

## Verification

Tests use only temporary environment/state directories and fake Docker/Compose
runners; they never read or mutate the installed production file.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s plugins/buzz-control/tests -v
node --check plugins/buzz-control/dashboard/dist/index.js
sh -n plugins/buzz-control/scripts/update.sh plugins/buzz-control/scripts/hermes-cron.sh plugins/buzz-control/scripts/install.sh
```

The plugin is available under the [MIT License](./LICENSE).
