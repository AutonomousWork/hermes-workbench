"""Authenticated Hermes dashboard operations for the local Buzz relay.

The browser can inspect health and request the one fixed update workflow. Image
checks happen only in ``scripts/update.sh``; page refreshes read its non-secret
state receipt and never contact GitHub or GHCR.
"""

from __future__ import annotations

import http.client
import hashlib
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request, Response


router = APIRouter()


def _load_config_module():
    module_name = "hermes_buzz_control_config_store"
    module_path = Path(__file__).resolve().with_name("config_store.py")
    existing = sys.modules.get(module_name)
    if existing is not None:
        existing_path = Path(getattr(existing, "__file__", "")).resolve()
        if existing_path != module_path:
            raise RuntimeError("Buzz configuration service identity collision")
        return existing
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Buzz configuration service")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


CONFIG_MODULE = _load_config_module()


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


def _optional_port_setting(name: str) -> int | None:
    if not os.environ.get(name, "").strip():
        return None
    return _port_setting(name, 3300)


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
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        allowed = ", ".join(sorted(schemes))
        raise ValueError(
            f"{name} must be a credential-free {allowed} URL with a host"
        )
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
CONFIG_HOME = _absolute_path("XDG_CONFIG_HOME", Path.home() / ".config")
CONFIG_ENV_FILE = _absolute_path(
    "BUZZ_CONTROL_ENV_FILE", CONFIG_HOME / "buzz" / "prod.env"
)
CONFIG_PATHS = CONFIG_MODULE.ConfigPaths.for_desired(CONFIG_ENV_FILE, STATE_DIR)
CONFIG_STORE = CONFIG_MODULE.ConfigStore(CONFIG_PATHS)
TRUSTED_DASHBOARD_ORIGIN = _setting("BUZZ_CONTROL_DASHBOARD_ORIGIN", "")
if TRUSTED_DASHBOARD_ORIGIN:
    parsed_dashboard_origin = urlsplit(TRUSTED_DASHBOARD_ORIGIN)
    if (
        parsed_dashboard_origin.scheme not in {"http", "https"}
        or not parsed_dashboard_origin.hostname
        or parsed_dashboard_origin.username
        or parsed_dashboard_origin.password
        or parsed_dashboard_origin.path not in {"", "/"}
        or parsed_dashboard_origin.query
        or parsed_dashboard_origin.fragment
    ):
        raise ValueError(
            "BUZZ_CONTROL_DASHBOARD_ORIGIN must be one exact http(s) origin"
        )
    TRUSTED_DASHBOARD_ORIGIN = TRUSTED_DASHBOARD_ORIGIN.rstrip("/")
UPDATER_PATH = PLUGIN_ROOT / "scripts" / "update.sh"
RECONCILER_PATH = PLUGIN_ROOT / "scripts" / "reconcile.py"
RECONCILER_PYTHON = Path(sys.executable).resolve()
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
LOCAL_PORT_OVERRIDE = _optional_port_setting("BUZZ_CONTROL_LOCAL_PORT")
LOCAL_PORT = LOCAL_PORT_OVERRIDE or 3300
HEALTH_PATH = _setting("BUZZ_CONTROL_HEALTH_PATH", "/_liveness")
if not re.fullmatch(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*", HEALTH_PATH):
    raise ValueError("BUZZ_CONTROL_HEALTH_PATH must be a safe absolute URL path")

LOCAL_RELAY_URL_OVERRIDE = os.environ.get("BUZZ_CONTROL_LOCAL_URL", "").strip() or None
_LOCAL_URL_HOST = f"[{LOCAL_HOST}]" if ":" in LOCAL_HOST else LOCAL_HOST
LOCAL_RELAY_URL = _validated_url(
    "BUZZ_CONTROL_LOCAL_URL",
    f"http://{_LOCAL_URL_HOST}:{LOCAL_PORT}",
    {"http", "https"},
)
DEFAULT_PUBLIC_RELAY_URL = "ws://127.0.0.1:3300"
PUBLIC_RELAY_URL_OVERRIDE = (
    _validated_url(
        "BUZZ_CONTROL_RELAY_URL",
        DEFAULT_PUBLIC_RELAY_URL,
        {"ws", "wss"},
    )
    if os.environ.get("BUZZ_CONTROL_RELAY_URL", "").strip()
    else None
)
NETWORK_SCOPE = _setting("BUZZ_CONTROL_NETWORK_SCOPE", "Local only")

_COMMAND_TIMEOUT = 30.0
_HEALTH_TIMEOUT = 3.0
_UPDATE_TIMEOUT = 16 * 60.0
MAX_CONFIG_REQUEST_BYTES = 32 * 1024
_INTENT_TTL_SECONDS = 5 * 60.0
_MAX_OPERATION_INTENTS = 32
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
    "baseline_missing",
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


def _runtime_local_port(*, desired_config: bool = False) -> int:
    if LOCAL_PORT_OVERRIDE is not None:
        return LOCAL_PORT_OVERRIDE
    raw = CONFIG_STORE.runtime_value(
        "BUZZ_HTTP_PORT", desired=desired_config
    ) or "3300"
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise CONFIG_MODULE.ConfigStoreError("invalid_value") from exc
    if not 1 <= port <= 65535:
        raise CONFIG_MODULE.ConfigStoreError("invalid_value")
    return port


def _runtime_local_url(*, desired_config: bool = False) -> str:
    if LOCAL_RELAY_URL_OVERRIDE is not None:
        return LOCAL_RELAY_URL
    port = _runtime_local_port(desired_config=desired_config)
    return f"http://{_LOCAL_URL_HOST}:{port}"


def _runtime_public_url_details(
    *, desired_config: bool = False
) -> tuple[str, bool]:
    value = PUBLIC_RELAY_URL_OVERRIDE
    if value is None:
        value = CONFIG_STORE.runtime_value("RELAY_URL", desired=desired_config)
    if not value:
        return DEFAULT_PUBLIC_RELAY_URL, False
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            return DEFAULT_PUBLIC_RELAY_URL, True
        port = parsed.port
    except ValueError:
        return DEFAULT_PUBLIC_RELAY_URL, True
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    redacted = bool(
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    )
    if not redacted:
        return value, False
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", "")), True


def _runtime_public_url(*, desired_config: bool = False) -> str:
    return _runtime_public_url_details(desired_config=desired_config)[0]


def _probe_health(
    *, timeout: float = _HEALTH_TIMEOUT, desired_config: bool = False
) -> dict[str, Any]:
    try:
        port = _runtime_local_port(desired_config=desired_config)
        with closing(
            http.client.HTTPConnection(LOCAL_HOST, port, timeout=timeout)
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
    except CONFIG_MODULE.ConfigStoreError:
        return {
            "reachable": False,
            "healthy": False,
            "status_code": None,
            "response": None,
            "error": "Buzz configuration is unavailable.",
        }
    except (OSError, http.client.HTTPException) as exc:
        return {
            "reachable": False,
            "healthy": False,
            "status_code": None,
            "response": None,
            "error": str(exc),
        }


def get_status(*, desired_config: bool = False) -> dict[str, Any]:
    container = _container_status()
    probe = _probe_health(desired_config=desired_config)
    public_url, public_url_redacted = _runtime_public_url_details(
        desired_config=desired_config
    )
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
            "public_url": public_url,
            "public_url_redacted": public_url_redacted,
            "local_url": _runtime_local_url(desired_config=desired_config),
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


class ConfigApiError(RuntimeError):
    def __init__(self, code: str, status_code: int):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class _OperationIntent(NamedTuple):
    action: str
    reconciler_action: str
    revision: str
    principal: str
    expires_at: float


_CONFIG_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
}
_REVISION_PATTERN = CONFIG_MODULE._OPAQUE_REVISION
_FIELD_NAME_PATTERN = CONFIG_MODULE._KEY
_INTENT_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")
_INTENTS: dict[str, _OperationIntent] = {}
_INTENTS_LOCK = threading.Lock()


def _config_response(payload: Mapping[str, Any], status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        status_code=status_code,
        headers=_CONFIG_HEADERS,
        media_type="application/json",
    )


def _config_error(code: str, status_code: int) -> Response:
    return _config_response(
        {
            "error": {
                "code": code,
                "correlation_id": secrets.token_urlsafe(9),
            }
        },
        status_code,
    )


def _store_error_response(error: Exception) -> Response:
    code = getattr(error, "code", "config_operation_failed")
    status_by_code = {
        "busy": 409,
        "stale_revision": 409,
        "baseline_missing": 409,
        "invalid_value": 400,
        "field_not_editable": 400,
        "invalid_document": 422,
        "document_too_large": 413,
        "invalid_journal": 503,
        "unsafe_storage": 503,
        "invalid_intent": 409,
        "recovery_required": 409,
    }
    return _config_error(code, status_by_code.get(code, 500))


def _request_principal(request: Request) -> str:
    session = getattr(request.state, "session", None)
    for name in ("subject", "sub", "user_id", "id"):
        value = (
            session.get(name)
            if isinstance(session, dict)
            else getattr(session, name, None)
        )
        if isinstance(value, str) and 0 < len(value) <= 256:
            return f"session:{value}"
    return "authenticated-dashboard-session"


def _header_values(request: Request, name: str) -> list[str]:
    return list(request.headers.getlist(name))


def _require_same_origin(request: Request) -> None:
    if request.headers.get("authorization") or getattr(
        request.state, "token_authenticated", False
    ):
        raise ConfigApiError("browser_session_required", 403)

    origins = _header_values(request, "origin")
    hosts = _header_values(request, "host")
    if len(origins) != 1 or len(hosts) != 1:
        raise ConfigApiError("origin_required", 403)
    origin = origins[0].strip()
    host = hosts[0].strip()
    if not origin or not host or "," in origin or "," in host:
        raise ConfigApiError("origin_invalid", 403)

    forwarded = _header_values(request, "forwarded")
    if forwarded:
        raise ConfigApiError("origin_ambiguous", 403)
    forwarded_hosts = _header_values(request, "x-forwarded-host")
    if forwarded_hosts and (
        len(forwarded_hosts) != 1
        or "," in forwarded_hosts[0]
        or forwarded_hosts[0].strip().lower() != host.lower()
    ):
        raise ConfigApiError("origin_ambiguous", 403)
    forwarded_schemes = _header_values(request, "x-forwarded-proto")
    request_scheme = str(request.url.scheme).lower()
    if forwarded_schemes and (
        len(forwarded_schemes) != 1
        or "," in forwarded_schemes[0]
        or forwarded_schemes[0].strip().lower() != request_scheme
    ):
        raise ConfigApiError("origin_ambiguous", 403)

    expected = TRUSTED_DASHBOARD_ORIGIN or f"{request_scheme}://{host}"
    if origin.rstrip("/") != expected.rstrip("/"):
        raise ConfigApiError("origin_mismatch", 403)
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigApiError("origin_invalid", 403)

    fetch_sites = _header_values(request, "sec-fetch-site")
    if fetch_sites and (len(fetch_sites) != 1 or fetch_sites[0] != "same-origin"):
        raise ConfigApiError("origin_mismatch", 403)


async def _read_config_json(request: Request) -> dict[str, Any]:
    _require_same_origin(request)
    content_types = _header_values(request, "content-type")
    if len(content_types) != 1:
        raise ConfigApiError("json_required", 415)
    content_type = content_types[0].split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ConfigApiError("json_required", 415)

    lengths = _header_values(request, "content-length")
    if len(lengths) > 1:
        raise ConfigApiError("invalid_request", 400)
    if lengths:
        try:
            declared_length = int(lengths[0])
        except ValueError as exc:
            raise ConfigApiError("invalid_request", 400) from exc
        if declared_length < 0:
            raise ConfigApiError("invalid_request", 400)
        if declared_length > MAX_CONFIG_REQUEST_BYTES:
            raise ConfigApiError("request_too_large", 413)

    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_CONFIG_REQUEST_BYTES:
                raise ConfigApiError("request_too_large", 413)
            chunks.append(chunk)
    except ConfigApiError:
        raise
    except Exception as exc:
        raise ConfigApiError("invalid_request", 400) from exc
    try:
        decoded = b"".join(chunks).decode("utf-8", errors="strict")
        payload = json.loads(decoded)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigApiError("invalid_json", 400) from exc
    if not isinstance(payload, dict):
        raise ConfigApiError("invalid_request", 400)
    return payload


def _validate_exact_object(
    payload: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    keys = set(payload)
    if not required.issubset(keys) or keys - required - (optional or set()):
        raise ConfigApiError("invalid_request", 400)


def _validate_revision(value: Any) -> str:
    if not isinstance(value, str) or not _REVISION_PATTERN.fullmatch(value):
        raise ConfigApiError("invalid_request", 400)
    return value


def _browser_changed_keys(values: Any) -> list[str]:
    return [
        value
        for value in values
        if isinstance(value, str) and value in CONFIG_MODULE.FIELD_CATALOG
    ]


def _browser_operation(journal: Mapping[str, Any]) -> dict[str, Any]:
    operation = dict(journal)
    if "changed_keys" in operation:
        operation["changed_keys"] = _browser_changed_keys(
            operation["changed_keys"]
        )
    return operation


def _config_payload(view: Any) -> dict[str, Any]:
    return {
        "revision": view.revision,
        "baseline_state": view.baseline_state,
        "pending": view.pending,
        "changed_keys": _browser_changed_keys(view.changed_keys),
        "impact_classes": list(view.impact_classes),
        "automatic_apply_allowed": view.automatic_apply_allowed,
        "fields": list(view.fields),
        "operation": _browser_operation(view.journal),
    }


def get_config() -> dict[str, Any]:
    return _config_payload(CONFIG_STORE.describe())


def save_config(payload: Mapping[str, Any]) -> dict[str, Any]:
    _validate_exact_object(
        payload, required={"base_revision", "replacements"}
    )
    base_revision = _validate_revision(payload.get("base_revision"))
    replacements = payload.get("replacements")
    if not isinstance(replacements, dict) or len(replacements) > 64:
        raise ConfigApiError("invalid_request", 400)
    validated: dict[str, str | None] = {}
    for name, value in replacements.items():
        if not isinstance(name, str) or not _FIELD_NAME_PATTERN.fullmatch(name):
            raise ConfigApiError("invalid_request", 400)
        if value is not None and not isinstance(value, str):
            raise ConfigApiError("invalid_request", 400)
        validated[name] = value
    result = CONFIG_STORE.save(base_revision, validated)
    payload_out = _config_payload(result.view)
    payload_out.update(
        {
            "wrote": result.wrote,
            "saved_changed_keys": _browser_changed_keys(result.changed_keys),
        }
    )
    return payload_out


def _purge_intents_locked(now: float) -> None:
    for digest, intent in list(_INTENTS.items()):
        if intent.expires_at <= now:
            _INTENTS.pop(digest, None)
    while len(_INTENTS) >= _MAX_OPERATION_INTENTS:
        oldest = min(_INTENTS, key=lambda item: _INTENTS[item].expires_at)
        _INTENTS.pop(oldest, None)


def prepare_config_intent(
    payload: Mapping[str, Any], request: Request
) -> dict[str, Any]:
    _validate_exact_object(
        payload,
        required={"action", "revision"},
        optional={"attestation"},
    )
    action = payload.get("action")
    if action not in {"apply", "apply_start", "restore", "adopt"}:
        raise ConfigApiError("invalid_request", 400)
    revision = _validate_revision(payload.get("revision"))
    view = CONFIG_STORE.describe()
    if view.revision != revision:
        raise ConfigApiError("stale_revision", 409)

    if action in {"apply", "apply_start"}:
        if view.baseline_state == "baseline_missing":
            raise ConfigApiError("baseline_missing", 409)
        if not view.pending:
            raise ConfigApiError("no_pending_changes", 409)
        if not view.automatic_apply_allowed:
            raise ConfigApiError("manual_maintenance_required", 409)
        running = bool(_container_status().get("running"))
        if action == "apply" and not running:
            raise ConfigApiError("relay_stopped", 409)
        if action == "apply_start" and running:
            raise ConfigApiError("relay_already_running", 409)
    elif action == "restore":
        if view.baseline_state == "baseline_missing":
            raise ConfigApiError("baseline_missing", 409)
        if not view.pending:
            raise ConfigApiError("no_pending_changes", 409)
    else:
        if payload.get("attestation") != "external_maintenance_complete":
            raise ConfigApiError("attestation_required", 400)
        status = get_status(desired_config=True)
        if not status.get("healthy"):
            raise ConfigApiError("runtime_unhealthy", 409)

    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    now = time.monotonic()
    reconciler_action = action
    if (
        action == "adopt"
        and view.journal.get("phase")
        in CONFIG_MODULE.RECOVERY_ADOPTION_PHASES
    ):
        reconciler_action = "recover_adopt"
    intent = _OperationIntent(
        action=action,
        reconciler_action=reconciler_action,
        revision=revision,
        principal=_request_principal(request),
        expires_at=now + _INTENT_TTL_SECONDS,
    )
    if action in {"apply", "apply_start", "adopt"}:
        CONFIG_STORE.record_operation_intent(
            digest=digest,
            action=reconciler_action,
            revision=revision,
            expires_at=time.time() + _INTENT_TTL_SECONDS,
        )
    with _INTENTS_LOCK:
        _purge_intents_locked(now)
        _INTENTS[digest] = intent
    return {
        "intent": token,
        "action": action,
        "revision": revision,
        "expires_in_seconds": int(_INTENT_TTL_SECONDS),
        "review": {
            "changed_keys": _browser_changed_keys(view.changed_keys),
            "impact_classes": list(view.impact_classes),
            "high_impact": (
                CONFIG_MODULE.ImpactClass.RELAY_HIGH_IMPACT.value
                in view.impact_classes
            ),
        },
    }


def _consume_config_intent(
    payload: Mapping[str, Any], request: Request, action: str
) -> _OperationIntent:
    _validate_exact_object(payload, required={"intent", "revision"})
    token = payload.get("intent")
    revision = _validate_revision(payload.get("revision"))
    if not isinstance(token, str) or not _INTENT_PATTERN.fullmatch(token):
        raise ConfigApiError("invalid_intent", 409)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    now = time.monotonic()
    with _INTENTS_LOCK:
        _purge_intents_locked(now)
        intent = _INTENTS.pop(digest, None)
    if (
        intent is None
        or intent.expires_at <= now
        or intent.action != action
        or intent.revision != revision
        or intent.principal != _request_principal(request)
    ):
        raise ConfigApiError("invalid_intent", 409)
    current = CONFIG_STORE.describe()
    if current.revision != revision:
        raise ConfigApiError("stale_revision", 409)
    if action in {"apply", "apply_start", "restore"} and not current.pending:
        raise ConfigApiError("no_pending_changes", 409)
    return intent


def _run_config_reconciler(
    action: str, revision: str, token: str
) -> dict[str, Any]:
    arguments = [str(RECONCILER_PYTHON), str(RECONCILER_PATH)]
    if action in {"apply", "apply_start"}:
        arguments.extend(["apply", action, revision])
    elif action == "adopt":
        arguments.extend(["adopt", revision])
    elif action == "recover_adopt":
        arguments.extend(["recover-adopt", revision])
    else:
        raise ConfigApiError("invalid_request", 400)
    try:
        result = subprocess.run(
            arguments,
            input=token + "\n",
            capture_output=True,
            check=False,
            env=os.environ.copy(),
            text=True,
            timeout=_UPDATE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConfigApiError("reconcile_timed_out", 504) from exc
    except OSError as exc:
        raise ConfigApiError("reconciler_unavailable", 503) from exc
    if result.returncode == 75:
        raise ConfigApiError("busy", 409)
    if result.returncode == 78:
        raise ConfigApiError("policy_changed", 409)
    if result.returncode == 70:
        raise ConfigApiError("degraded", 500)
    if result.returncode != 0:
        raise ConfigApiError("reconcile_failed", 500)
    lines = [line for line in result.stdout.splitlines() if line]
    if len(lines) != 1 or lines[0] not in {
        "RESULT=applied",
        "RESULT=rolled_back",
        "RESULT=adopted",
        "RESULT=recovered",
    }:
        raise ConfigApiError("reconcile_failed", 500)
    payload = get_config()
    payload["reconcile_result"] = lines[0].removeprefix("RESULT=")
    return payload


@router.get("/status")
def status_route() -> dict[str, Any]:
    return _run_route(get_status)


@router.get("/updates")
def updates_route() -> dict[str, Any]:
    return _run_route(get_updates)


@router.post("/update")
def update_route() -> dict[str, Any]:
    return _run_route(update_buzz)


@router.get("/config")
async def config_route(_request: Request) -> Response:
    try:
        return _config_response(get_config())
    except CONFIG_MODULE.ConfigStoreError as exc:
        return _store_error_response(exc)
    except Exception:
        return _config_error("config_operation_failed", 500)


@router.put("/config")
async def config_save_route(request: Request) -> Response:
    try:
        payload = await _read_config_json(request)
        return _config_response(save_config(payload))
    except ConfigApiError as exc:
        return _config_error(exc.code, exc.status_code)
    except CONFIG_MODULE.ConfigStoreError as exc:
        return _store_error_response(exc)
    except Exception:
        return _config_error("config_operation_failed", 500)


@router.post("/config/intent")
async def config_intent_route(request: Request) -> Response:
    try:
        payload = await _read_config_json(request)
        return _config_response(prepare_config_intent(payload, request))
    except ConfigApiError as exc:
        return _config_error(exc.code, exc.status_code)
    except CONFIG_MODULE.ConfigStoreError as exc:
        return _store_error_response(exc)
    except Exception:
        return _config_error("config_operation_failed", 500)


@router.post("/config/apply")
async def config_apply_route(request: Request) -> Response:
    try:
        payload = await _read_config_json(request)
        action = payload.get("action")
        if action not in {"apply", "apply_start"}:
            raise ConfigApiError("invalid_request", 400)
        _validate_exact_object(
            payload, required={"action", "intent", "revision"}
        )
        intent = _consume_config_intent(
            {"intent": payload["intent"], "revision": payload["revision"]},
            request,
            action,
        )
        result = _run_config_reconciler(
            intent.action, intent.revision, payload["intent"]
        )
        return _config_response(result)
    except ConfigApiError as exc:
        return _config_error(exc.code, exc.status_code)
    except CONFIG_MODULE.ConfigStoreError as exc:
        return _store_error_response(exc)
    except Exception:
        return _config_error("config_operation_failed", 500)


@router.post("/config/restore")
async def config_restore_route(request: Request) -> Response:
    try:
        payload = await _read_config_json(request)
        intent = _consume_config_intent(payload, request, "restore")
        restored = CONFIG_STORE.restore(intent.revision)
        return _config_response(_config_payload(restored))
    except ConfigApiError as exc:
        return _config_error(exc.code, exc.status_code)
    except CONFIG_MODULE.ConfigStoreError as exc:
        return _store_error_response(exc)
    except Exception:
        return _config_error("config_operation_failed", 500)


@router.post("/config/adopt")
async def config_adopt_route(request: Request) -> Response:
    try:
        payload = await _read_config_json(request)
        intent = _consume_config_intent(payload, request, "adopt")
        result = _run_config_reconciler(
            intent.reconciler_action, intent.revision, payload["intent"]
        )
        return _config_response(result)
    except ConfigApiError as exc:
        return _config_error(exc.code, exc.status_code)
    except CONFIG_MODULE.ConfigStoreError as exc:
        return _store_error_response(exc)
    except Exception:
        return _config_error("config_operation_failed", 500)
