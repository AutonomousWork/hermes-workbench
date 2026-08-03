"""Authenticated Hermes dashboard operations for the local Buzz relay.

The browser can inspect health and request the one fixed update workflow. Image
checks happen only in ``scripts/update.sh``; page refreshes read its non-secret
state receipt and never contact GitHub or GHCR.
"""

from __future__ import annotations

import http.client
import os
import re
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException


router = APIRouter()


def _setting(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _port_setting(name: str, default: int) -> int:
    raw = _setting(name, str(default))
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


def _safe_name(name: str, value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(
            f"{name} may contain only letters, numbers, dots, underscores, and hyphens"
        )
    return value


def _absolute_path(name: str, default: Path) -> Path:
    value = Path(_setting(name, str(default))).expanduser()
    if not value.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return value


def _validated_url(name: str, default: str, schemes: set[str]) -> str:
    value = _setting(name, default)
    parsed = urlsplit(value)
    if parsed.scheme not in schemes or not parsed.hostname:
        allowed = ", ".join(sorted(schemes))
        raise ValueError(f"{name} must be a {allowed} URL with a host")
    return value


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HERMES_ROOT = _absolute_path("HERMES_HOME", Path.home() / ".hermes")
DOCKER_BIN = _absolute_path("BUZZ_CONTROL_DOCKER_BIN", Path("/usr/local/bin/docker"))
DOCKER_HOST = _setting("BUZZ_CONTROL_DOCKER_HOST", "unix:///var/run/docker.sock")
if not DOCKER_HOST.startswith("unix:///"):
    raise ValueError("BUZZ_CONTROL_DOCKER_HOST must be a local unix:// endpoint")
DEPLOY_DIR = _absolute_path("BUZZ_CONTROL_DEPLOY_DIR", PLUGIN_ROOT / "deploy")
STATE_DIR = _absolute_path(
    "BUZZ_CONTROL_STATE_DIR", HERMES_ROOT / "state" / "buzz-control"
)
STATE_FILE = STATE_DIR / "update-state"
DOCKER_CONFIG_DIR = STATE_DIR / "docker-anonymous"
UPDATER_PATH = PLUGIN_ROOT / "scripts" / "update.sh"
COMPOSE_PROJECT = _safe_name(
    "BUZZ_CONTROL_COMPOSE_PROJECT",
    _setting("BUZZ_CONTROL_COMPOSE_PROJECT", "buzz-prod"),
)
COMPOSE_SERVICE = _safe_name(
    "BUZZ_CONTROL_COMPOSE_SERVICE",
    _setting("BUZZ_CONTROL_COMPOSE_SERVICE", "relay"),
)
RELAY_IMAGE = _setting("BUZZ_CONTROL_IMAGE", "ghcr.io/block/buzz:main")
if not re.fullmatch(r"[A-Za-z0-9./:@_-]+", RELAY_IMAGE):
    raise ValueError("BUZZ_CONTROL_IMAGE contains unsupported characters")

LOCAL_HOST = _setting("BUZZ_CONTROL_LOCAL_HOST", "127.0.0.1")
if LOCAL_HOST not in {"127.0.0.1", "localhost", "::1"}:
    raise ValueError("BUZZ_CONTROL_LOCAL_HOST must be a loopback host")
LOCAL_PORT = _port_setting("BUZZ_CONTROL_LOCAL_PORT", 3300)
HEALTH_PATH = _setting("BUZZ_CONTROL_HEALTH_PATH", "/_liveness")
if not re.fullmatch(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*", HEALTH_PATH):
    raise ValueError("BUZZ_CONTROL_HEALTH_PATH must be a safe absolute URL path")

LOCAL_RELAY_URL = _validated_url(
    "BUZZ_CONTROL_LOCAL_URL",
    f"http://{LOCAL_HOST}:{LOCAL_PORT}",
    {"http", "https"},
)
PUBLIC_RELAY_URL = _validated_url(
    "BUZZ_CONTROL_RELAY_URL",
    "ws://127.0.0.1:3300",
    {"ws", "wss"},
)
NETWORK_SCOPE = _setting("BUZZ_CONTROL_NETWORK_SCOPE", "Local only")

_COMMAND_TIMEOUT = 30.0
_HEALTH_TIMEOUT = 3.0
_UPDATE_TIMEOUT = 16 * 60.0
_FIELD_SEPARATOR = "||HERMES_BUZZ||"
_CONTAINER_FORMAT = _FIELD_SEPARATOR.join(
    (
        "{{.Id}}",
        "{{.Name}}",
        "{{.Image}}",
        "{{.State.Status}}",
        "{{.State.Running}}",
        "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
        "{{.State.StartedAt}}",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
        '{{index .Config.Labels "org.opencontainers.image.created"}}',
    )
)
_STATE_KEYS = {
    "schema_version",
    "trigger",
    "result",
    "started_at",
    "completed_at",
    "last_check_at",
    "running_image_before",
    "running_image_after",
    "latest_image_id",
    "latest_image_digest",
    "latest_image_revision",
    "latest_image_created_at",
    "latest_image_observed_this_attempt",
    "healthy",
    "error",
    "last_successful_update_at",
    "last_successful_image_id",
    "latest_failure_at",
    "latest_failure_result",
    "latest_failure_error",
}
_STATE_RESULTS = {
    "already_current",
    "updated",
    "stopped",
    "timed_out",
    "pull_failed",
    "apply_failed",
    "unhealthy",
    "verification_failed",
    "state_write_failed",
}
class BuzzControlError(RuntimeError):
    """A safe, user-displayable failure from a fixed Buzz operation."""


class BuzzControlBusy(BuzzControlError):
    """The shared updater lock is currently owned by another process."""


def _safe_detail(result: subprocess.CompletedProcess[str]) -> str:
    detail = result.stderr or result.stdout or "unknown command error"
    return " ".join(detail.split())[:700]


def _run_command(
    args: list[str],
    *,
    timeout: float = _COMMAND_TIMEOUT,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuzzControlError(f"{Path(args[0]).name} timed out") from exc
    except OSError as exc:
        raise BuzzControlError(f"could not run {Path(args[0]).name}: {exc}") from exc

    if check and result.returncode != 0:
        raise BuzzControlError(f"{Path(args[0]).name} failed: {_safe_detail(result)}")
    return result


def _docker(*args: str, timeout: float = _COMMAND_TIMEOUT) -> subprocess.CompletedProcess[str]:
    try:
        DOCKER_CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        DOCKER_CONFIG_DIR.chmod(0o700)
    except OSError as exc:
        raise BuzzControlError("could not prepare isolated Docker settings") from exc
    return _run_command(
        [
            str(DOCKER_BIN),
            "--config",
            str(DOCKER_CONFIG_DIR),
            "--host",
            DOCKER_HOST,
            *args,
        ],
        timeout=timeout,
    )


def _empty_container(*, error: str | None = None) -> dict[str, Any]:
    return {
        "exists": False,
        "id": None,
        "name": None,
        "image_id": None,
        "state": "not found" if error is None else "unavailable",
        "running": False,
        "health": "unavailable",
        "started_at": None,
        "revision": None,
        "created_at": None,
        "error": error,
    }


def _container_ids() -> list[str]:
    result = _docker(
        "ps",
        "-aq",
        "--filter",
        f"label=com.docker.compose.project={COMPOSE_PROJECT}",
        "--filter",
        f"label=com.docker.compose.service={COMPOSE_SERVICE}",
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _inspect_container(container_id: str) -> dict[str, Any]:
    result = _docker("inspect", "--format", _CONTAINER_FORMAT, container_id)
    fields = result.stdout.rstrip("\n").split(_FIELD_SEPARATOR)
    if len(fields) != 9:
        raise BuzzControlError("Docker returned an unexpected Buzz status payload")
    full_id, name, image_id, state, running, health, started_at, revision, created_at = fields
    return {
        "exists": True,
        "id": full_id[:12] or None,
        "name": name.lstrip("/") or None,
        "image_id": image_id or None,
        "state": state or "unknown",
        "running": running.lower() == "true",
        "health": health or "none",
        "started_at": started_at or None,
        "revision": revision or None,
        "created_at": created_at or None,
        "error": None,
    }


def _container_status() -> dict[str, Any]:
    try:
        ids = _container_ids()
        if not ids:
            return _empty_container()
        inspected = [_inspect_container(container_id) for container_id in ids]
        inspected.sort(
            key=lambda item: (bool(item["running"]), item.get("started_at") or ""),
            reverse=True,
        )
        return inspected[0]
    except BuzzControlError as exc:
        return _empty_container(error=str(exc))


def _probe_health(*, timeout: float = _HEALTH_TIMEOUT) -> dict[str, Any]:
    try:
        with closing(
            http.client.HTTPConnection(LOCAL_HOST, LOCAL_PORT, timeout=timeout)
        ) as connection:
            connection.request("GET", HEALTH_PATH)
            response = connection.getresponse()
            body = response.read(128).decode("utf-8", errors="replace").strip()
            return {
                "reachable": True,
                "healthy": 200 <= response.status < 300,
                "status_code": response.status,
                "response": body[:80] or None,
                "error": None,
            }
    except (OSError, http.client.HTTPException) as exc:
        return {
            "reachable": False,
            "healthy": False,
            "status_code": None,
            "response": None,
            "error": str(exc),
        }


def get_status() -> dict[str, Any]:
    container = _container_status()
    probe = _probe_health()
    healthy = bool(
        container["running"]
        and container["health"] == "healthy"
        and probe["healthy"]
    )
    return {
        "healthy": healthy,
        "container": container,
        "probe": probe,
        "relay": {
            "public_url": PUBLIC_RELAY_URL,
            "local_url": LOCAL_RELAY_URL,
            "scope": NETWORK_SCOPE,
        },
        "deployment": {
            "project": COMPOSE_PROJECT,
            "service": COMPOSE_SERVICE,
            "image": RELAY_IMAGE,
            "directory": str(DEPLOY_DIR),
        },
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _read_update_state() -> tuple[dict[str, str], str | None]:
    if not STATE_FILE.exists():
        return {}, "Hermes has no saved update result yet."
    if STATE_FILE.is_symlink() or not STATE_FILE.is_file():
        return {}, "Hermes saved update result is not valid."
    try:
        if STATE_FILE.stat().st_size > 32_768:
            raise ValueError("receipt is too large")
        lines = STATE_FILE.read_text(encoding="utf-8").splitlines()
        state: dict[str, str] = {}
        for line in lines:
            key, separator, value = line.partition("=")
            if not separator or key not in _STATE_KEYS or key in state or len(value) > 500:
                raise ValueError("receipt contains an invalid field")
            state[key] = value
        if set(state) != _STATE_KEYS:
            raise ValueError("receipt is incomplete")
        if state.get("schema_version") != "1" or state.get("result") not in _STATE_RESULTS:
            raise ValueError("receipt has an unsupported schema or result")
        if state.get("latest_image_observed_this_attempt") not in {"true", "false"}:
            raise ValueError("receipt has an invalid image-observation flag")
    except (OSError, UnicodeError, ValueError):
        return {}, "Hermes saved update result is not valid."
    return state, None


def get_updates() -> dict[str, Any]:
    container = _container_status()
    state, state_error = _read_update_state()
    current_image_id = container.get("image_id")
    latest_image_id = state.get("latest_image_id") or None
    update_available: bool | None = None
    if (
        state.get("latest_image_observed_this_attempt") == "true"
        and current_image_id
        and latest_image_id
    ):
        update_available = current_image_id != latest_image_id

    latest = None
    if latest_image_id or state.get("latest_image_digest") or state.get("latest_image_revision"):
        latest = {
            "image_id": latest_image_id,
            "digest": state.get("latest_image_digest") or None,
            "revision": state.get("latest_image_revision") or None,
            "created_at": state.get("latest_image_created_at") or None,
        }
    return {
        "current": {
            "revision": container.get("revision"),
            "created_at": container.get("created_at"),
            "image_id": current_image_id,
            "digest": None,
        },
        "latest": latest,
        "update_available": update_available,
        "state": {key: value or None for key, value in state.items() if key != "schema_version"},
        "errors": [state_error] if state_error else [],
        "checked_at": state.get("last_check_at") or None,
    }


def update_buzz() -> dict[str, Any]:
    if not UPDATER_PATH.is_file():
        raise BuzzControlError(f"Buzz updater not found: {UPDATER_PATH}")
    result = _run_command(
        [str(UPDATER_PATH), "manual"],
        timeout=_UPDATE_TIMEOUT,
        check=False,
        env=os.environ.copy(),
    )
    if result.returncode == 75:
        raise BuzzControlBusy("another Buzz update is already running")
    if result.returncode != 0:
        raise BuzzControlError(_safe_detail(result))

    after_status = get_status()
    if not after_status["healthy"]:
        raise BuzzControlError("Buzz update finished, but the relay did not return healthy")
    changed = "RESULT=updated" in result.stdout
    return {
        "ok": True,
        "changed": changed,
        "message": (
            "Buzz was updated and is healthy."
            if changed
            else "Buzz is already current and healthy."
        ),
        "status": after_status,
        "updates": get_updates(),
    }


def _run_route(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return action()
    except BuzzControlBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BuzzControlError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/status")
def status_route() -> dict[str, Any]:
    return _run_route(get_status)


@router.get("/updates")
def updates_route() -> dict[str, Any]:
    return _run_route(get_updates)


@router.post("/update")
def update_route() -> dict[str, Any]:
    return _run_route(update_buzz)
