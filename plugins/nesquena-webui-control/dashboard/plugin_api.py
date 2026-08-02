"""Authenticated Hermes dashboard controls for the NesQuena WebUI LaunchAgent.

Hermes mounts this router at ``/api/plugins/nesquena-webui-control``.  The
service is a macOS per-user LaunchAgent with KeepAlive enabled, so a real stop
must boot the job out of launchd; sending SIGTERM would immediately relaunch it.
"""

from __future__ import annotations

import http.client
import os
import re
import subprocess
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException


router = APIRouter()

SERVICE_LABEL = "ai.hermes.webui"
GUI_DOMAIN = f"gui/{os.getuid()}"
SERVICE_TARGET = f"{GUI_DOMAIN}/{SERVICE_LABEL}"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
WEBUI_HOST = "127.0.0.1"
WEBUI_PORT = 8787
WEBUI_ENDPOINT = f"http://{WEBUI_HOST}:{WEBUI_PORT}/"

_STATE_RE = re.compile(r"^\s*state\s*=\s*([^\n]+)$", re.MULTILINE)
_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)\s*$", re.MULTILINE)
_LAST_EXIT_RE = re.compile(r"^\s*last exit code\s*=\s*(-?\d+)\s*$", re.MULTILINE)


class ServiceControlError(RuntimeError):
    """A safe, user-displayable failure from a fixed launchctl operation."""


def _run_launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a fixed launchctl command without a shell or caller-provided input."""

    try:
        return subprocess.run(
            ["/bin/launchctl", *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceControlError(f"launchctl {args[0]} timed out") from exc
    except OSError as exc:
        raise ServiceControlError(f"could not run launchctl: {exc}") from exc


def _parse_launchctl_status(
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    """Turn ``launchctl print`` output into a stable dashboard payload."""

    if result.returncode != 0:
        return {
            "loaded": False,
            "running": False,
            "state": "stopped",
            "pid": None,
            "last_exit_code": None,
        }

    output = result.stdout or ""
    state_match = _STATE_RE.search(output)
    pid_match = _PID_RE.search(output)
    exit_match = _LAST_EXIT_RE.search(output)
    state = state_match.group(1).strip() if state_match else "loaded"
    pid = int(pid_match.group(1)) if pid_match else None

    return {
        "loaded": True,
        "running": state == "running" and pid is not None,
        "state": state,
        "pid": pid,
        "last_exit_code": int(exit_match.group(1)) if exit_match else None,
    }


def _service_status() -> dict[str, Any]:
    return _parse_launchctl_status(_run_launchctl("print", SERVICE_TARGET))


def _probe_http() -> dict[str, Any]:
    try:
        with closing(
            http.client.HTTPConnection(WEBUI_HOST, WEBUI_PORT, timeout=2)
        ) as connection:
            connection.request("GET", "/")
            response = connection.getresponse()
            return {
                "reachable": True,
                "status_code": response.status,
                "error": None,
            }
    except (OSError, http.client.HTTPException) as exc:
        return {
            "reachable": False,
            "status_code": None,
            "error": str(exc),
        }


def _status_payload(launchd: dict[str, Any]) -> dict[str, Any]:
    http = _probe_http()
    status_code = http["status_code"]
    healthy_http = bool(
        http["reachable"]
        and isinstance(status_code, int)
        and 200 <= status_code < 400
    )

    return {
        "service": SERVICE_LABEL,
        "service_target": SERVICE_TARGET,
        "plist": str(PLIST_PATH),
        "endpoint": WEBUI_ENDPOINT,
        **launchd,
        "http": http,
        "healthy": bool(launchd["running"] and healthy_http),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def get_status() -> dict[str, Any]:
    return _status_payload(_service_status())


def _require_success(action: str, result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout or "unknown launchctl error").strip()
    detail = " ".join(detail.split())[:500]
    raise ServiceControlError(f"{action} failed: {detail}")


def _wait_for_status(
    *,
    loaded: bool,
    running: bool,
    require_healthy: bool = False,
    timeout: float = 12.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = _service_status()
        reached = last["loaded"] is loaded and last["running"] is running
        if reached:
            status = _status_payload(last)
            if not require_healthy or status["healthy"]:
                return status
        time.sleep(0.25)

    observed = "unknown" if last is None else str(last.get("state", "unknown"))
    goal = "healthy and running" if require_healthy else (
        "running" if running else "stopped"
    )
    raise ServiceControlError(
        f"service did not become {goal} within {timeout:g}s (state: {observed})"
    )


def _result(
    action: str,
    *,
    changed: bool,
    message: str,
    status: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": True,
        "action": action,
        "changed": changed,
        "message": message,
        "status": status,
    }


def _start_from(current: dict[str, Any]) -> dict[str, Any]:
    if current["loaded"]:
        result = _run_launchctl("kickstart", SERVICE_TARGET)
        _require_success("start", result)
    else:
        if not PLIST_PATH.is_file():
            raise ServiceControlError(f"LaunchAgent plist not found: {PLIST_PATH}")
        enable = _run_launchctl("enable", SERVICE_TARGET)
        _require_success("enable", enable)
        bootstrap = _run_launchctl("bootstrap", GUI_DOMAIN, str(PLIST_PATH))
        _require_success("start", bootstrap)

    return _wait_for_status(loaded=True, running=True, require_healthy=True)


def start_service() -> dict[str, Any]:
    current = _service_status()
    if current["running"]:
        return _result(
            "start",
            changed=False,
            message="NesQuena WebUI is already running.",
            status=_status_payload(current),
        )

    status = _start_from(current)
    return _result(
        "start",
        changed=True,
        message="NesQuena WebUI started.",
        status=status,
    )


def stop_service() -> dict[str, Any]:
    current = _service_status()
    if not current["loaded"]:
        return _result(
            "stop",
            changed=False,
            message="NesQuena WebUI is already stopped.",
            status=_status_payload(current),
        )

    result = _run_launchctl("bootout", SERVICE_TARGET)
    _require_success("stop", result)
    status = _wait_for_status(loaded=False, running=False)
    return _result(
        "stop",
        changed=True,
        message="NesQuena WebUI stopped.",
        status=status,
    )


def restart_service() -> dict[str, Any]:
    current = _service_status()
    if not current["loaded"]:
        status = _start_from(current)
        return _result(
            "restart",
            changed=True,
            message="NesQuena WebUI was stopped and has been started.",
            status=status,
        )

    result = _run_launchctl("kickstart", "-k", SERVICE_TARGET)
    _require_success("restart", result)
    status = _wait_for_status(loaded=True, running=True, require_healthy=True)
    return _result(
        "restart",
        changed=True,
        message="NesQuena WebUI restarted.",
        status=status,
    )


def _run_route(action: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return action()
    except ServiceControlError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/status")
def status_route() -> dict[str, Any]:
    return get_status()


@router.post("/start")
def start_route() -> dict[str, Any]:
    return _run_route(start_service)


@router.post("/stop")
def stop_route() -> dict[str, Any]:
    return _run_route(stop_service)


@router.post("/restart")
def restart_route() -> dict[str, Any]:
    return _run_route(restart_service)
