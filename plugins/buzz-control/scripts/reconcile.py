#!/usr/bin/env python3
"""Single lock owner for Buzz image and configuration reconciliation."""

from __future__ import annotations

import http.client
import importlib.util
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CONFIG_MODULE_PATH = PLUGIN_ROOT / "dashboard" / "config_store.py"
UPDATER_PATH = PLUGIN_ROOT / "scripts" / "update.sh"


def _load_config_module():
    name = "hermes_buzz_control_config_store"
    existing = sys.modules.get(name)
    if existing is not None:
        if Path(getattr(existing, "__file__", "")).resolve() != CONFIG_MODULE_PATH:
            raise RuntimeError("Buzz configuration service identity collision")
        return existing
    spec = importlib.util.spec_from_file_location(name, CONFIG_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Buzz configuration service")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


CONFIG_MODULE = _load_config_module()


def _setting(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _absolute(name: str, default: Path) -> Path:
    path = Path(_setting(name, str(default))).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be absolute")
    return path


def _safe_name(name: str, default: str) -> str:
    value = _setting(name, default)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(f"{name} is invalid")
    return value


def _positive_seconds(name: str, default: int) -> float:
    value = _setting(name, str(default))
    if not value.isdigit() or int(value) <= 0:
        raise ValueError(f"{name} must be positive")
    return float(value)


class Settings(NamedTuple):
    desired: Path
    state_dir: Path
    docker: Path
    compose: Path
    docker_host: str
    compose_file: Path
    override_file: Path
    project: str
    service: str
    image: str
    docker_config: Path
    local_host: str
    local_port_override: int | None
    health_path: str
    timeout: float


def _settings() -> Settings:
    config_home = Path(_setting("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    hermes_root = Path(_setting("HERMES_HOME", str(Path.home() / ".hermes")))
    deploy_dir = _absolute("BUZZ_CONTROL_DEPLOY_DIR", PLUGIN_ROOT / "deploy")
    desired = _absolute("BUZZ_CONTROL_ENV_FILE", config_home / "buzz" / "prod.env")
    state_dir = _absolute(
        "BUZZ_CONTROL_STATE_DIR", hermes_root / "state" / "buzz-control"
    )
    try:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise ValueError("Buzz state directory is unavailable") from exc
    metadata = state_dir.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise ValueError("Buzz state directory is unsafe")
    docker_host = _setting(
        "BUZZ_CONTROL_DOCKER_HOST", "unix:///var/run/docker.sock"
    )
    if not docker_host.startswith("unix:///"):
        raise ValueError("Buzz reconciliation requires local Docker")
    image = _setting("BUZZ_CONTROL_IMAGE", "ghcr.io/block/buzz:main")
    if not re.fullmatch(r"[A-Za-z0-9./:@_-]+", image):
        raise ValueError("Buzz image is invalid")
    local_host = _setting("BUZZ_CONTROL_LOCAL_HOST", "127.0.0.1")
    if local_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Buzz health host must be loopback")
    port_override_raw = os.environ.get("BUZZ_CONTROL_LOCAL_PORT", "").strip()
    local_port_override = None
    if port_override_raw:
        if not port_override_raw.isdigit() or not 1 <= int(port_override_raw) <= 65535:
            raise ValueError("Buzz health port is invalid")
        local_port_override = int(port_override_raw)
    health_path = _setting("BUZZ_CONTROL_HEALTH_PATH", "/_liveness")
    if not re.fullmatch(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*", health_path):
        raise ValueError("Buzz health path is invalid")
    return Settings(
        desired=desired,
        state_dir=state_dir,
        docker=_absolute("BUZZ_CONTROL_DOCKER_BIN", Path("/usr/local/bin/docker")),
        compose=_absolute(
            "BUZZ_CONTROL_COMPOSE_BIN", Path("/usr/local/bin/docker-compose")
        ),
        docker_host=docker_host,
        compose_file=_absolute(
            "BUZZ_CONTROL_COMPOSE_FILE", deploy_dir / "compose.yml"
        ),
        override_file=_absolute(
            "BUZZ_CONTROL_OVERRIDE_FILE", deploy_dir / "compose.local.yml"
        ),
        project=_safe_name("BUZZ_CONTROL_COMPOSE_PROJECT", "buzz-prod"),
        service=_safe_name("BUZZ_CONTROL_COMPOSE_SERVICE", "relay"),
        image=image,
        docker_config=state_dir / "docker-anonymous",
        local_host=local_host,
        local_port_override=local_port_override,
        health_path=health_path,
        timeout=_positive_seconds("BUZZ_CONTROL_EXECUTION_TIMEOUT_SECONDS", 900),
    )


class ReconcileFailure(RuntimeError):
    def __init__(self, code: str, exit_code: int = 1):
        self.code = code
        self.exit_code = exit_code
        super().__init__(code)


class Reconciler:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.paths = CONFIG_MODULE.ConfigPaths.for_desired(
            settings.desired, settings.state_dir
        )
        self.store = CONFIG_MODULE.ConfigStore(self.paths)
        self.deadline = time.monotonic() + settings.timeout

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ReconcileFailure("timed_out")
        return remaining

    def _command(
        self,
        arguments: list[str],
        *,
        environment: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        try:
            output_options = (
                {"capture_output": True}
                if capture
                else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            )
            result = subprocess.run(
                arguments,
                check=False,
                text=True,
                timeout=self._remaining(),
                env=environment,
                **output_options,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReconcileFailure("timed_out") from exc
        except OSError as exc:
            raise ReconcileFailure("command_failed") from exc
        if result.returncode != 0:
            raise ReconcileFailure("command_failed")
        return result

    def _base_environment(self, env_file: Path, image: str) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "BUZZ_IMAGE": image,
                "BUZZ_SERVICE_ENV_FILE": str(env_file),
                "DOCKER_CONFIG": str(self.settings.docker_config),
                "DOCKER_HOST": self.settings.docker_host,
            }
        )
        return environment

    def _compose(
        self,
        env_file: Path,
        image: str,
        *arguments: str,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            str(self.settings.compose),
            "--project-name",
            self.settings.project,
            "--env-file",
            str(env_file),
            "-f",
            str(self.settings.compose_file),
            "-f",
            str(self.settings.override_file),
            *arguments,
        ]
        return self._command(
            command,
            environment=self._base_environment(env_file, image),
            capture=capture,
        )

    def _docker(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self._command(
            [
                str(self.settings.docker),
                "--config",
                str(self.settings.docker_config),
                "--host",
                self.settings.docker_host,
                *arguments,
            ],
            capture=True,
        )

    def _container_id(
        self, env_file: Path, image: str, *, all_containers: bool = False
    ) -> str:
        arguments = ["ps"]
        if all_containers:
            arguments.append("--all")
        arguments.extend(["-q", self.settings.service])
        result = self._compose(
            env_file, image, *arguments, capture=True
        )
        identifiers = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(identifiers) > 1:
            raise ReconcileFailure("runtime_ambiguous")
        return identifiers[0] if identifiers else ""

    def _container_image(self, container_id: str) -> str:
        if not container_id:
            return ""
        result = self._docker("inspect", "--format", "{{.Image}}", container_id)
        values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(values) != 1 or not values[0].startswith("sha256:"):
            raise ReconcileFailure("immutable_image_missing")
        return values[0]

    def _container_health(self, container_id: str) -> str:
        if not container_id:
            return "stopped"
        result = self._docker(
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
            container_id,
        )
        values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(values) != 1:
            raise ReconcileFailure("runtime_unverified")
        return values[0]

    def _service_config_hash(self, env_file: Path, image: str) -> str:
        result = self._compose(
            env_file,
            image,
            "config",
            "--hash",
            self.settings.service,
            capture=True,
        )
        values = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if (
            len(values) != 1
            or len(values[0]) != 2
            or values[0][0] != self.settings.service
            or not re.fullmatch(r"[0-9a-f]{64}", values[0][1])
        ):
            raise ReconcileFailure("runtime_unverified")
        return values[0][1]

    def _container_config_hash(self, container_id: str) -> str:
        result = self._docker(
            "inspect",
            "--format",
            '{{index .Config.Labels "com.docker.compose.config-hash"}}',
            container_id,
        )
        values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(values) != 1 or not re.fullmatch(r"[0-9a-f]{64}", values[0]):
            raise ReconcileFailure("runtime_unverified")
        return values[0]

    def _http_port(self, env_file: Path) -> int:
        content = CONFIG_MODULE._read_protected(
            env_file, os.getuid(), CONFIG_MODULE.MAX_CONFIG_BYTES
        )
        document = CONFIG_MODULE.DotenvDocument.parse(content.data)
        raw = document.value("BUZZ_HTTP_PORT") or "3300"
        try:
            port = int(raw)
        except (TypeError, ValueError) as exc:
            raise ReconcileFailure("port_conflict") from exc
        if not 1 <= port <= 65535:
            raise ReconcileFailure("port_conflict")
        if (
            self.settings.local_port_override is not None
            and self.settings.local_port_override != port
        ):
            raise ReconcileFailure("port_conflict")
        return self.settings.local_port_override or port

    def _http_healthy(self, env_file: Path) -> bool:
        port = self._http_port(env_file)
        timeout = min(3.0, self._remaining())
        try:
            with http.client.HTTPConnection(
                self.settings.local_host, port, timeout=timeout
            ) as connection:
                connection.request("GET", self.settings.health_path)
                response = connection.getresponse()
                response.read(128)
                return 200 <= response.status < 300
        except (OSError, http.client.HTTPException):
            return False

    def _journal_blocks(self, view: object) -> bool:
        return view.journal.get("phase") in CONFIG_MODULE.BLOCKING_JOURNAL_PHASES

    def image(self, trigger: str) -> int:
        if trigger not in {"manual", "scheduled"}:
            raise ReconcileFailure("invalid_invocation", 2)
        try:
            with self.store.lock():
                view = self.store._describe_locked()
                if self._journal_blocks(view):
                    raise ReconcileFailure("recovery_required", 78)
                if view.baseline_state == "baseline_missing":
                    selected = self.paths.desired
                    block_recreate = "true"
                else:
                    selected = self.paths.applied
                    block_recreate = "false"
                environment = os.environ.copy()
                environment["BUZZ_CONTROL_RECONCILER_CHILD"] = "1"
                try:
                    result = subprocess.run(
                        [
                            str(UPDATER_PATH),
                            "--reconciler-image",
                            trigger,
                            str(selected),
                            block_recreate,
                        ],
                        check=False,
                        # The inner updater owns the operation deadline and still
                        # needs a bounded moment to persist its safe timeout receipt.
                        timeout=self._remaining() + 3.0,
                        env=environment,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise ReconcileFailure("timed_out") from exc
                return result.returncode
        except CONFIG_MODULE.ConfigStoreError as exc:
            if exc.code == "busy":
                if trigger == "scheduled":
                    return 0
                raise ReconcileFailure("busy", 75) from exc
            raise

    def _write_phase(self, phase: str, action: str, view: object, **extra: str) -> None:
        entry = {
            "phase": phase,
            "action": action,
            "revision": view.revision,
            "changed_keys": list(view.changed_keys),
            "impact_classes": list(view.impact_classes),
            **extra,
        }
        self.store._write_journal_locked(entry)

    def _verify_runtime(self, env_file: Path, image: str) -> bool:
        try:
            container_id = self._container_id(env_file, image)
            return bool(
                container_id
                and self._container_image(container_id) == image
                and self._container_health(container_id) == "healthy"
                and self._http_healthy(env_file)
            )
        except (ReconcileFailure, CONFIG_MODULE.ConfigStoreError):
            return False

    def _rollback(
        self,
        view: object,
        prior_image: str,
        prior_running: bool,
    ) -> str:
        self._write_phase(
            "rolling_back",
            "apply",
            view,
            started_at=CONFIG_MODULE._utc_now(),
        )
        try:
            if prior_running:
                self._compose(
                    self.paths.applied,
                    prior_image,
                    "up",
                    "-d",
                    "--wait",
                    "--no-deps",
                    "--pull",
                    "never",
                    self.settings.service,
                )
                verified = self._verify_runtime(self.paths.applied, prior_image)
            else:
                self._compose(
                    self.paths.applied,
                    prior_image,
                    "stop",
                    self.settings.service,
                )
                verified = not self._container_id(
                    self.paths.applied, prior_image
                )
        except ReconcileFailure:
            verified = False
        if verified:
            self._write_phase(
                "rolled_back",
                "apply",
                view,
                completed_at=CONFIG_MODULE._utc_now(),
                outcome="rolled_back",
            )
            return "rolled_back"
        self._write_phase(
            "degraded",
            "apply",
            view,
            completed_at=CONFIG_MODULE._utc_now(),
            outcome="rollback_unverified",
        )
        raise ReconcileFailure("degraded", 70)

    def apply(self, action: str, revision: str, token: str) -> str:
        if action not in {"apply", "apply_start"}:
            raise ReconcileFailure("invalid_invocation", 2)
        with self.store.lock():
            self.store.consume_operation_intent_locked(
                token=token, action=action, revision=revision
            )
            view = self.store._describe_locked()
            if self._journal_blocks(view):
                raise ReconcileFailure("recovery_required", 78)
            if (
                view.revision != revision
                or view.baseline_state == "baseline_missing"
                or not view.pending
                or not view.automatic_apply_allowed
            ):
                raise ReconcileFailure("policy_changed", 78)

            snapshot = self.store.operation_snapshot_locked(revision)
            operation = self.paths.operation
            self._compose(operation, self.settings.image, "config", "--quiet")
            self._http_port(operation)
            current_id = self._container_id(self.paths.applied, self.settings.image)
            prior_running = bool(current_id)
            if action == "apply" and not prior_running:
                raise ReconcileFailure("relay_stopped", 78)
            if action == "apply_start" and prior_running:
                raise ReconcileFailure("relay_already_running", 78)
            if prior_running:
                prior_image = self._container_image(current_id)
            else:
                stopped_id = self._container_id(
                    self.paths.applied,
                    self.settings.image,
                    all_containers=True,
                )
                if not stopped_id:
                    raise ReconcileFailure("immutable_image_missing", 78)
                prior_image = self._container_image(stopped_id)

            self._write_phase(
                "applying",
                action,
                view,
                started_at=CONFIG_MODULE._utc_now(),
                runtime_generation=prior_image,
            )
            try:
                self._compose(
                    operation,
                    prior_image,
                    "up",
                    "-d",
                    "--wait",
                    "--no-deps",
                    "--pull",
                    "never",
                    self.settings.service,
                )
                self._write_phase(
                    "verifying",
                    action,
                    view,
                    started_at=CONFIG_MODULE._utc_now(),
                    runtime_generation=prior_image,
                )
                self.store.assert_operation_current_locked(snapshot)
                if not self._verify_runtime(operation, prior_image):
                    raise ReconcileFailure("runtime_unverified")
                self._write_phase(
                    "promoting",
                    action,
                    view,
                    started_at=CONFIG_MODULE._utc_now(),
                    runtime_generation=prior_image,
                )
                self.store.promote_operation_locked(
                    snapshot,
                    phase="applied",
                    action=action,
                    runtime_generation=prior_image,
                )
            except (ReconcileFailure, CONFIG_MODULE.ConfigStoreError):
                return self._rollback(view, prior_image, prior_running)
            return "applied"

    def adopt(self, revision: str, token: str, *, recovery: bool = False) -> str:
        action = "recover_adopt" if recovery else "adopt"
        with self.store.lock():
            self.store.consume_operation_intent_locked(
                token=token, action=action, revision=revision
            )
            view = self.store._describe_locked()
            phase = view.journal.get("phase")
            if view.revision != revision or (
                recovery
                and phase not in CONFIG_MODULE.RECOVERY_ADOPTION_PHASES
            ) or (not recovery and self._journal_blocks(view)):
                raise ReconcileFailure("policy_changed", 78)
            snapshot = self.store.operation_snapshot_locked(revision)
            operation = self.paths.operation
            self._compose(operation, self.settings.image, "config", "--quiet")
            self._http_port(operation)
            desired_hash = self._service_config_hash(
                operation, self.settings.image
            )
            container_id = self._container_id(operation, self.settings.image)
            if not container_id:
                raise ReconcileFailure("runtime_unverified", 78)
            image = self._container_image(container_id)
            if (
                self._container_config_hash(container_id) != desired_hash
                or self._container_health(container_id) != "healthy"
                or not self._http_healthy(operation)
            ):
                raise ReconcileFailure("runtime_unverified", 78)
            self._write_phase(
                "recovering" if recovery else "adopting",
                action,
                view,
                started_at=CONFIG_MODULE._utc_now(),
                runtime_generation=image,
            )
            self.store.promote_operation_locked(
                snapshot,
                phase="applied",
                action=action,
                runtime_generation=image,
                outcome="recovered" if recovery else "applied",
            )
            return "recovered" if recovery else "adopted"


def _read_token() -> str:
    token = sys.stdin.read(513)
    if len(token) > 512:
        raise ReconcileFailure("invalid_intent", 78)
    return token.strip()


def main(arguments: list[str]) -> int:
    try:
        settings = _settings()
        reconciler = Reconciler(settings)
        if len(arguments) == 2 and arguments[0] == "image":
            return reconciler.image(arguments[1])
        if len(arguments) == 3 and arguments[0] == "apply":
            result = reconciler.apply(arguments[1], arguments[2], _read_token())
            print(f"RESULT={result}")
            return 0
        if len(arguments) == 2 and arguments[0] == "adopt":
            result = reconciler.adopt(arguments[1], _read_token())
            print(f"RESULT={result}")
            return 0
        if len(arguments) == 2 and arguments[0] == "recover-adopt":
            result = reconciler.adopt(
                arguments[1], _read_token(), recovery=True
            )
            print(f"RESULT={result}")
            return 0
        raise ReconcileFailure("invalid_invocation", 2)
    except CONFIG_MODULE.ConfigStoreError as exc:
        code = getattr(exc, "code", "storage_failed")
        exit_code = 75 if code == "busy" else 78 if code in {
            "stale_revision",
            "invalid_intent",
            "baseline_missing",
            "recovery_required",
        } else 1
        print(f"Buzz reconciliation stopped safely ({code}).", file=sys.stderr)
        return exit_code
    except ReconcileFailure as exc:
        print(f"Buzz reconciliation stopped safely ({exc.code}).", file=sys.stderr)
        return exc.exit_code
    except (OSError, RuntimeError, ValueError):
        print("Buzz reconciliation stopped safely (configuration_failed).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
