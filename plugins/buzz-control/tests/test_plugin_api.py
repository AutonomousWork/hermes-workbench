from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_API_PATH = PLUGIN_ROOT / "dashboard" / "plugin_api.py"


def load_plugin_api(environment: dict[str, str] | None = None):
    spec = importlib.util.spec_from_file_location(
        "buzz_control_plugin_api", PLUGIN_API_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load dashboard/plugin_api.py")
    module = importlib.util.module_from_spec(spec)

    fastapi = types.ModuleType("fastapi")

    class APIRouter:
        def get(self, *_args, **_kwargs):
            return lambda function: function

        def post(self, *_args, **_kwargs):
            return lambda function: function

        def put(self, *_args, **_kwargs):
            return lambda function: function

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    class Request:
        pass

    class Response:
        def __init__(
            self,
            content=b"",
            status_code=200,
            headers=None,
            media_type=None,
        ):
            self.body = content.encode() if isinstance(content, str) else content
            self.status_code = status_code
            self.headers = dict(headers or {})
            self.media_type = media_type

    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
    fastapi.Request = Request
    fastapi.Response = Response
    clean_environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("BUZZ_CONTROL_")
    }
    if environment:
        clean_environment.update(environment)

    with patch.dict(sys.modules, {"fastapi": fastapi}), patch.dict(
        os.environ, clean_environment, clear=True
    ):
        spec.loader.exec_module(module)
    return module


class Headers:
    def __init__(self, entries: list[tuple[str, str]]):
        self.entries = [(key.lower(), value) for key, value in entries]

    def get(self, key: str, default=None):
        values = self.getlist(key)
        return values[-1] if values else default

    def getlist(self, key: str):
        lowered = key.lower()
        return [value for name, value in self.entries if name == lowered]


class FakeRequest:
    def __init__(
        self,
        body: bytes = b"",
        *,
        origin: str | None = "http://127.0.0.1:9119",
        content_type: str = "application/json",
        extra_headers: list[tuple[str, str]] | None = None,
        chunks: list[bytes] | None = None,
    ):
        headers = [("host", "127.0.0.1:9119")]
        if origin is not None:
            headers.append(("origin", origin))
        if content_type:
            headers.append(("content-type", content_type))
        headers.append(("content-length", str(len(body))))
        headers.append(("sec-fetch-site", "same-origin"))
        headers.extend(extra_headers or [])
        self.headers = Headers(headers)
        self.url = types.SimpleNamespace(scheme="http")
        self.state = types.SimpleNamespace(session=types.SimpleNamespace(subject="operator"))
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(auth_required=True))
        self._chunks = chunks if chunks is not None else [body]

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


def response_json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def completed(
    *args: str,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def complete_update_state(**overrides: str) -> dict[str, str]:
    state = {
        "schema_version": "1",
        "trigger": "scheduled",
        "result": "already_current",
        "started_at": "2026-08-03T01:59:00Z",
        "completed_at": "2026-08-03T02:00:00Z",
        "last_check_at": "2026-08-03T02:00:00Z",
        "running_image_before": "sha256:running",
        "running_image_after": "sha256:running",
        "latest_image_id": "sha256:latest",
        "latest_image_digest": "sha256:index",
        "latest_image_revision": "revision-new",
        "latest_image_created_at": "2026-08-03T01:55:25Z",
        "latest_image_observed_this_attempt": "true",
        "healthy": "true",
        "error": "",
        "last_successful_update_at": "2026-08-02T02:00:00Z",
        "last_successful_image_id": "sha256:running",
        "latest_failure_at": "",
        "latest_failure_result": "",
        "latest_failure_error": "",
    }
    state.update(overrides)
    return state


def serialize_update_state(state: dict[str, str]) -> str:
    return "".join(f"{key}={value}\n" for key, value in state.items())


class PluginApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin_api()

    def test_defaults_match_the_local_buzz_deployment(self):
        self.assertEqual(self.plugin.COMPOSE_PROJECT, "buzz-prod")
        self.assertEqual(self.plugin.COMPOSE_SERVICE, "relay")
        self.assertEqual(self.plugin.LOCAL_PORT, 3300)
        self.assertIsNone(self.plugin.LOCAL_PORT_OVERRIDE)
        self.assertIsNone(self.plugin.LOCAL_RELAY_URL_OVERRIDE)
        self.assertEqual(self.plugin.HEALTH_PATH, "/_liveness")
        self.assertEqual(
            self.plugin.DEFAULT_PUBLIC_RELAY_URL,
            "ws://127.0.0.1:3300",
        )
        self.assertIsNone(self.plugin.PUBLIC_RELAY_URL_OVERRIDE)
        self.assertEqual(self.plugin.NETWORK_SCOPE, "Local only")
        self.assertEqual(self.plugin.RELAY_IMAGE, "ghcr.io/block/buzz:main")
        self.assertEqual(self.plugin.DEPLOY_DIR, PLUGIN_ROOT / "deploy")

    def test_runtime_settings_can_be_overridden(self):
        plugin = load_plugin_api(
            {
                "BUZZ_CONTROL_COMPOSE_PROJECT": "buzz-stage",
                "BUZZ_CONTROL_COMPOSE_SERVICE": "buzz-relay",
                "BUZZ_CONTROL_LOCAL_PORT": "4400",
                "BUZZ_CONTROL_RELAY_URL": "wss://buzz.example.test",
                "BUZZ_CONTROL_LOCAL_URL": "http://127.0.0.1:4400",
                "BUZZ_CONTROL_DEPLOY_DIR": "/tmp/buzz-compose",
            }
        )

        self.assertEqual(plugin.COMPOSE_PROJECT, "buzz-stage")
        self.assertEqual(plugin.COMPOSE_SERVICE, "buzz-relay")
        self.assertEqual(plugin.LOCAL_PORT, 4400)
        self.assertEqual(plugin.LOCAL_PORT_OVERRIDE, 4400)
        self.assertEqual(plugin.LOCAL_RELAY_URL_OVERRIDE, "http://127.0.0.1:4400")
        self.assertEqual(plugin.DEPLOY_DIR, Path("/tmp/buzz-compose"))
        self.assertEqual(
            plugin.PUBLIC_RELAY_URL_OVERRIDE,
            "wss://buzz.example.test",
        )

    def test_xdg_config_home_selects_the_same_production_file(self):
        plugin = load_plugin_api({"XDG_CONFIG_HOME": "/tmp/buzz-xdg"})

        self.assertEqual(
            plugin.CONFIG_ENV_FILE,
            Path("/tmp/buzz-xdg/buzz/prod.env"),
        )

    def test_runtime_settings_reject_browser_unsafe_values(self):
        with self.assertRaisesRegex(ValueError, "may contain only"):
            load_plugin_api({"BUZZ_CONTROL_COMPOSE_SERVICE": "relay;whoami"})
        with self.assertRaisesRegex(ValueError, "loopback"):
            load_plugin_api({"BUZZ_CONTROL_LOCAL_HOST": "example.com"})
        with self.assertRaisesRegex(ValueError, "between 1 and 65535"):
            load_plugin_api({"BUZZ_CONTROL_LOCAL_PORT": "70000"})
        with self.assertRaisesRegex(ValueError, "must be an absolute path"):
            load_plugin_api({"BUZZ_CONTROL_DEPLOY_DIR": "relative/path"})
        with self.assertRaisesRegex(ValueError, "credential-free"):
            load_plugin_api(
                {"BUZZ_CONTROL_RELAY_URL": "wss://secret@example.test"}
            )

    def test_inspect_container_parses_only_safe_status_fields(self):
        fields = self.plugin._FIELD_SEPARATOR.join(
            (
                "b14e3327f92e3087",
                "/buzz-prod-relay-1",
                "sha256:running",
                "running",
                "true",
                "healthy",
                "2026-08-03T02:26:57Z",
                "318fbf896ec335bc7bcb40edafde0b6ebca53428",
                "2026-08-02T20:18:23Z",
            )
        )
        with patch.object(
            self.plugin,
            "_docker",
            return_value=completed("inspect", stdout=fields + "\n"),
        ) as docker:
            status = self.plugin._inspect_container("b14e")

        self.assertTrue(status["running"])
        self.assertEqual(status["health"], "healthy")
        self.assertEqual(status["name"], "buzz-prod-relay-1")
        self.assertEqual(status["id"], "b14e3327f92e")
        self.assertEqual(status["revision"], "318fbf896ec335bc7bcb40edafde0b6ebca53428")
        docker.assert_called_once_with(
            "inspect", "--format", self.plugin._CONTAINER_FORMAT, "b14e"
        )

    def test_container_status_reports_docker_failure_without_crashing_page(self):
        with patch.object(
            self.plugin,
            "_container_ids",
            side_effect=self.plugin.BuzzControlError("Docker is unavailable"),
        ):
            status = self.plugin._container_status()

        self.assertFalse(status["exists"])
        self.assertFalse(status["running"])
        self.assertEqual(status["state"], "unavailable")
        self.assertEqual(status["error"], "Docker is unavailable")

    def test_health_probe_uses_the_buzz_liveness_endpoint(self):
        connection = Mock()
        response = Mock(status=200)
        response.read.return_value = b"ok"
        connection.getresponse.return_value = response

        with patch.object(
            self.plugin.CONFIG_STORE,
            "runtime_value",
            return_value="3300",
        ), patch.object(
            self.plugin.http.client,
            "HTTPConnection",
            return_value=connection,
        ) as http_connection:
            result = self.plugin._probe_health()

        http_connection.assert_called_once_with("127.0.0.1", 3300, timeout=3.0)
        connection.request.assert_called_once_with("GET", "/_liveness")
        connection.close.assert_called_once_with()
        self.assertTrue(result["healthy"])
        self.assertEqual(result["response"], "ok")

    def test_health_probe_uses_the_applied_snapshot_port(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        response = MagicMock(status=200)
        response.read.return_value = b"ok"
        connection.getresponse.return_value = response

        with patch.object(
            self.plugin.CONFIG_STORE,
            "runtime_value",
            return_value="4400",
        ) as runtime_value, patch.object(
            self.plugin.http.client,
            "HTTPConnection",
            return_value=connection,
        ) as http_connection:
            result = self.plugin._probe_health()

        runtime_value.assert_called_once_with("BUZZ_HTTP_PORT", desired=False)
        http_connection.assert_called_once_with("127.0.0.1", 4400, timeout=3.0)
        self.assertTrue(result["healthy"])

    def test_get_status_requires_container_and_http_health(self):
        container = {
            "running": True,
            "health": "healthy",
            "revision": "abc",
            "error": None,
        }
        probe = {
            "reachable": True,
            "healthy": True,
            "status_code": 200,
            "response": "ok",
            "error": None,
        }
        def runtime_value(name, *, desired=False):
            self.assertFalse(desired)
            return {
                "BUZZ_HTTP_PORT": "4400",
                "RELAY_URL": "wss://relay.example.test",
            }[name]

        with patch.object(
            self.plugin, "_container_status", return_value=container
        ), patch.object(self.plugin, "_probe_health", return_value=probe), patch.object(
            self.plugin.CONFIG_STORE,
            "runtime_value",
            side_effect=runtime_value,
        ):
            status = self.plugin.get_status()

        self.assertTrue(status["healthy"])
        self.assertEqual(
            status["relay"]["public_url"],
            "wss://relay.example.test",
        )
        self.assertFalse(status["relay"]["public_url_redacted"])
        self.assertEqual(status["relay"]["local_url"], "http://127.0.0.1:4400")
        self.assertEqual(status["deployment"]["project"], "buzz-prod")

    def test_status_uses_valid_applied_values_when_desired_is_invalid(self):
        desired = b"export BUZZ_HTTP_PORT=secret-canary\n"
        applied = (
            b"BUZZ_HTTP_PORT=4400\n"
            b"RELAY_URL=wss://relay.example.test\n"
        )
        container = {
            "running": True,
            "health": "healthy",
            "revision": "abc",
            "error": None,
        }
        probe = {
            "reachable": True,
            "healthy": True,
            "status_code": 200,
            "response": "ok",
            "error": None,
        }
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(
                Path(td), desired, applied=applied
            )
            with patch.object(
                self.plugin, "CONFIG_STORE", store
            ), patch.object(
                self.plugin, "_container_status", return_value=container
            ), patch.object(
                self.plugin, "_probe_health", return_value=probe
            ):
                status = self.plugin.get_status()

        self.assertTrue(status["healthy"])
        self.assertEqual(status["relay"]["local_url"], "http://127.0.0.1:4400")
        self.assertEqual(
            status["relay"]["public_url"], "wss://relay.example.test"
        )

    def test_public_relay_url_never_discloses_embedded_credentials(self):
        with patch.object(
            self.plugin.CONFIG_STORE,
            "runtime_value",
            return_value=(
                "wss://operator:secret-canary@relay.example.test/events"
                "?token=second-canary#third-canary"
            ),
        ):
            value, redacted = self.plugin._runtime_public_url_details()

        self.assertEqual(value, "wss://relay.example.test/events")
        self.assertTrue(redacted)
        self.assertNotIn("canary", value)

    def test_update_state_parser_accepts_only_the_versioned_allow_list(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "update-state"
            receipt = complete_update_state(
                result="updated",
                latest_image_id="sha256:new",
            )
            state_file.write_text(serialize_update_state(receipt))
            with patch.object(self.plugin, "STATE_FILE", state_file):
                state, error = self.plugin._read_update_state()

        self.assertIsNone(error)
        self.assertEqual(set(state), self.plugin._STATE_KEYS)
        self.assertEqual(state["result"], "updated")
        self.assertEqual(state["latest_image_id"], "sha256:new")

    def test_update_state_parser_marks_missing_and_corrupt_receipts_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "update-state"
            with patch.object(self.plugin, "STATE_FILE", state_file):
                missing, missing_error = self.plugin._read_update_state()
                state_file.write_text("schema_version=2\nsecret=value\n")
                corrupt, corrupt_error = self.plugin._read_update_state()

        self.assertEqual(missing, {})
        self.assertIn("no saved update result", missing_error)
        self.assertEqual(corrupt, {})
        self.assertIn("not valid", corrupt_error)

    def test_update_state_parser_rejects_a_truncated_v1_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "update-state"
            receipt = complete_update_state()
            receipt.pop("completed_at")
            state_file.write_text(serialize_update_state(receipt))
            with patch.object(self.plugin, "STATE_FILE", state_file):
                state, error = self.plugin._read_update_state()

        self.assertEqual(state, {})
        self.assertIn("not valid", error)

    def test_updates_compare_running_image_to_the_latest_saved_receipt(self):
        container = {
            "revision": "running-revision",
            "created_at": "2026-08-02T20:18:23Z",
            "image_id": "sha256:running",
        }
        receipt = {
            "schema_version": "1",
            "result": "already_current",
            "latest_image_id": "sha256:latest",
            "latest_image_digest": "sha256:index",
            "latest_image_revision": "latest-revision",
            "latest_image_created_at": "2026-08-03T01:55:25Z",
            "latest_image_observed_this_attempt": "true",
            "last_check_at": "2026-08-03T02:00:00Z",
        }
        with patch.object(
            self.plugin, "_container_status", return_value=container
        ), patch.object(
            self.plugin, "_read_update_state", return_value=(receipt, None)
        ):
            updates = self.plugin.get_updates()

        self.assertTrue(updates["update_available"])
        self.assertEqual(updates["latest"]["revision"], "latest-revision")
        self.assertEqual(updates["state"]["result"], "already_current")
        self.assertNotIn("updates", updates)
        self.assertEqual(updates["errors"], [])

    def test_failed_checks_keep_latest_image_but_make_availability_unknown(self):
        container = {"image_id": "sha256:running"}
        for result in ("pull_failed", "stopped", "timed_out", "verification_failed"):
            with self.subTest(result=result):
                receipt = complete_update_state(
                    result=result,
                    latest_image_id="sha256:previous-observation",
                    latest_image_observed_this_attempt="false",
                    healthy="false",
                )
                with patch.object(
                    self.plugin, "_container_status", return_value=container
                ), patch.object(
                    self.plugin, "_read_update_state", return_value=(receipt, None)
                ):
                    updates = self.plugin.get_updates()

                self.assertIsNone(updates["update_available"])
                self.assertEqual(
                    updates["latest"]["image_id"], "sha256:previous-observation"
                )

    def test_post_pull_failures_can_compare_the_observed_image(self):
        container = {"image_id": "sha256:running"}
        for result in ("apply_failed", "unhealthy", "verification_failed"):
            with self.subTest(result=result):
                receipt = complete_update_state(result=result, healthy="false")
                with patch.object(
                    self.plugin, "_container_status", return_value=container
                ), patch.object(
                    self.plugin, "_read_update_state", return_value=(receipt, None)
                ):
                    updates = self.plugin.get_updates()

                self.assertTrue(updates["update_available"])

    def test_update_runs_the_plugin_owned_updater_and_returns_fresh_state(self):
        healthy_status = {
            "healthy": True,
            "container": {"image_id": "sha256:new"},
        }
        updates = {"update_available": False}
        with tempfile.TemporaryDirectory() as td:
            updater = Path(td) / "update.sh"
            updater.touch()
            with patch.object(self.plugin, "UPDATER_PATH", updater), patch.object(
                self.plugin,
                "_run_command",
                return_value=completed("update", stdout="RESULT=updated\n"),
            ) as run, patch.object(
                self.plugin, "get_status", return_value=healthy_status
            ), patch.object(self.plugin, "get_updates", return_value=updates):
                result = self.plugin.update_buzz()

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["status"], healthy_status)
        self.assertEqual(run.call_args.args[0], [str(updater), "manual"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_update_rejects_an_unhealthy_result(self):
        unhealthy_status = {
            "healthy": False,
            "container": {"image_id": "sha256:new"},
        }
        with tempfile.TemporaryDirectory() as td:
            updater = Path(td) / "update.sh"
            updater.touch()
            with patch.object(self.plugin, "UPDATER_PATH", updater), patch.object(
                self.plugin,
                "_run_command",
                return_value=completed("update", stdout="RESULT=updated\n"),
            ), patch.object(self.plugin, "get_status", return_value=unhealthy_status):
                with self.assertRaisesRegex(
                    self.plugin.BuzzControlError,
                    "did not return healthy",
                ):
                    self.plugin.update_buzz()

    def test_update_maps_cross_process_lock_contention_to_busy(self):
        with tempfile.TemporaryDirectory() as td:
            updater = Path(td) / "update.sh"
            updater.touch()
            with patch.object(self.plugin, "UPDATER_PATH", updater), patch.object(
                self.plugin,
                "_run_command",
                return_value=completed(
                    "update", returncode=75, stderr="Another update is running."
                ),
            ):
                with self.assertRaisesRegex(self.plugin.BuzzControlBusy, "already"):
                    self.plugin.update_buzz()

    def test_failed_command_raises_a_safe_control_error(self):
        with patch.object(
            self.plugin.subprocess,
            "run",
            return_value=completed(
                "docker", returncode=1, stderr="registry request failed"
            ),
        ):
            with self.assertRaisesRegex(
                self.plugin.BuzzControlError,
                "registry request failed",
            ):
                self.plugin._run_command(["/usr/local/bin/docker", "pull"])

    def test_update_route_returns_the_shared_safe_error_envelope(self):
        with patch.object(
            self.plugin,
            "update_buzz",
            side_effect=self.plugin.BuzzControlError("update failed safely"),
        ), self.assertRaises(self.plugin.HTTPException) as raised:
            self.plugin.update_route()

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "update failed safely")

    def test_update_route_returns_conflict_for_a_busy_updater(self):
        with patch.object(
            self.plugin,
            "update_buzz",
            side_effect=self.plugin.BuzzControlBusy("another update is already running"),
        ), self.assertRaises(self.plugin.HTTPException) as raised:
            self.plugin.update_route()

        self.assertEqual(raised.exception.status_code, 409)

    def make_config_store(
        self,
        root: Path,
        desired: bytes,
        *,
        applied: bytes | None = None,
    ):
        config_dir = root / "config"
        state_dir = root / "state"
        config_dir.mkdir(mode=0o700)
        state_dir.mkdir(mode=0o700)
        desired_path = config_dir / "prod.env"
        desired_path.write_bytes(desired)
        desired_path.chmod(0o600)
        paths = self.plugin.CONFIG_MODULE.ConfigPaths.for_desired(
            desired_path, state_dir
        )
        if applied is not None:
            paths.applied.write_bytes(applied)
            paths.applied.chmod(0o600)
        return self.plugin.CONFIG_MODULE.ConfigStore(paths), paths

    def test_config_get_is_redacted_and_never_cached(self):
        canary = "secret-canary-with-distinct-length"
        raw = (
            "BUZZ_DOMAIN=example.test\n"
            f"POSTGRES_PASSWORD={canary}\n"
            "UNKNOWN_LOCAL=unknown-canary\n"
        ).encode()
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(Path(td), raw, applied=raw)
            with patch.object(self.plugin, "CONFIG_STORE", store):
                response = asyncio.run(
                    self.plugin.config_route(FakeRequest(origin=None))
                )

        serialized = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertNotIn(canary, serialized)
        self.assertNotIn("unknown-canary", serialized)
        fields = {field["name"]: field for field in response_json(response)["fields"]}
        self.assertEqual(fields["BUZZ_DOMAIN"]["value"], "example.test")
        self.assertNotIn("POSTGRES_PASSWORD", fields)
        self.assertNotIn("UNKNOWN_LOCAL", fields)

    def test_config_save_rejects_unmanaged_assignments(self):
        raw = b"BUZZ_DOMAIN=example.test\nPOSTGRES_PASSWORD=old-secret\n"
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(Path(td), raw, applied=raw)
            revision = store.describe().revision
            body = json.dumps(
                {
                    "base_revision": revision,
                    "replacements": {"POSTGRES_PASSWORD": "new-secret-canary"},
                }
            ).encode()
            with patch.object(self.plugin, "CONFIG_STORE", store):
                response = asyncio.run(
                    self.plugin.config_save_route(FakeRequest(body))
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response_json(response)["error"]["code"], "field_not_editable"
        )
        self.assertNotIn("new-secret-canary", response.body.decode())

    def test_config_save_requires_exact_same_origin_json(self):
        raw = b"BUZZ_DOMAIN=example.test\n"
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(Path(td), raw, applied=raw)
            revision = store.describe().revision
            body = json.dumps(
                {
                    "base_revision": revision,
                    "replacements": {"BUZZ_DOMAIN": "next.test"},
                }
            ).encode()
            with patch.object(self.plugin, "CONFIG_STORE", store):
                missing = asyncio.run(
                    self.plugin.config_save_route(FakeRequest(body, origin=None))
                )
                cross_site = asyncio.run(
                    self.plugin.config_save_route(
                        FakeRequest(body, origin="https://evil.example")
                    )
                )
                wrong_type = asyncio.run(
                    self.plugin.config_save_route(
                        FakeRequest(body, content_type="text/plain")
                    )
                )
                success = asyncio.run(
                    self.plugin.config_save_route(FakeRequest(body))
                )

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(cross_site.status_code, 403)
        self.assertEqual(wrong_type.status_code, 415)
        self.assertEqual(success.status_code, 200)
        self.assertTrue(response_json(success)["wrote"])

    def test_config_parser_errors_never_echo_canary_input(self):
        raw = b"BUZZ_DOMAIN=example.test\n"
        canary = b"secret-canary-malformed"
        cases = (
            (b'{"base_revision": "' + canary, 400),
            (json.dumps({"unknown": canary.decode()}).encode(), 400),
            (b"x" * (self.plugin.MAX_CONFIG_REQUEST_BYTES + 1), 413),
        )
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(Path(td), raw, applied=raw)
            with patch.object(self.plugin, "CONFIG_STORE", store):
                for body, expected_status in cases:
                    with self.subTest(status=expected_status):
                        response = asyncio.run(
                            self.plugin.config_save_route(FakeRequest(body))
                        )
                        serialized = response.body.decode()
                        self.assertEqual(response.status_code, expected_status)
                        self.assertNotIn(canary.decode(), serialized)
                        payload = response_json(response)
                        self.assertRegex(payload["error"]["correlation_id"], r"^[A-Za-z0-9_-]+$")

    def test_streamed_body_cap_does_not_trust_declared_length(self):
        raw = b"BUZZ_DOMAIN=example.test\n"
        canary = b"secret-canary-chunked"
        chunks = [b"{" + canary, b"x" * self.plugin.MAX_CONFIG_REQUEST_BYTES]
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(Path(td), raw, applied=raw)
            with patch.object(self.plugin, "CONFIG_STORE", store):
                request = FakeRequest(b"{}", chunks=chunks)
                response = asyncio.run(self.plugin.config_save_route(request))

        self.assertEqual(response.status_code, 413)
        self.assertNotIn(canary.decode(), response.body.decode())

    def test_bearer_and_ambiguous_origin_mutations_fail_closed(self):
        body = b"{}"
        ambiguous = FakeRequest(
            body,
            extra_headers=[("origin", "http://127.0.0.1:9119")],
        )
        bearer = FakeRequest(
            body,
            extra_headers=[("authorization", "Bearer secret-canary")],
        )

        ambiguous_response = asyncio.run(
            self.plugin.config_save_route(ambiguous)
        )
        bearer_response = asyncio.run(self.plugin.config_save_route(bearer))

        self.assertEqual(ambiguous_response.status_code, 403)
        self.assertEqual(bearer_response.status_code, 403)
        self.assertNotIn("secret-canary", bearer_response.body.decode())

    def test_manual_only_pending_change_cannot_prepare_apply(self):
        desired = b"POSTGRES_PASSWORD=new-secret-canary\n"
        applied = b"POSTGRES_PASSWORD=old-secret\n"
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(
                Path(td), desired, applied=applied
            )
            current = store.describe()
            body = json.dumps(
                {"action": "apply", "revision": current.revision}
            ).encode()
            with patch.object(self.plugin, "CONFIG_STORE", store):
                response = asyncio.run(
                    self.plugin.config_intent_route(FakeRequest(body))
                )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response_json(response)["error"]["code"],
            "manual_maintenance_required",
        )
        self.assertNotIn("new-secret-canary", response.body.decode())

    def test_config_contract_hides_advanced_changed_key_names(self):
        desired = (
            b"BUZZ_DOMAIN=example.test\n"
            b"POSTGRES_PASSWORD=secret-canary\n"
            b"UNLISTED_RUNTIME_FLAG=enabled\n"
        )
        applied = (
            b"BUZZ_DOMAIN=example.test\n"
            b"POSTGRES_PASSWORD=old-secret\n"
            b"UNLISTED_RUNTIME_FLAG=disabled\n"
        )
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(
                Path(td), desired, applied=applied
            )
            view = store.describe()
            store.write_journal(
                {
                    "phase": "saved",
                    "action": "save",
                    "revision": view.revision,
                    "changed_keys": [
                        "POSTGRES_PASSWORD",
                        "UNLISTED_RUNTIME_FLAG",
                    ],
                    "impact_classes": ["unknown_manual_only"],
                    "outcome": "pending",
                }
            )
            with patch.object(self.plugin, "CONFIG_STORE", store), patch.object(
                self.plugin,
                "get_status",
                return_value={"healthy": True},
            ):
                config_payload = self.plugin.get_config()
                intent_payload = self.plugin.prepare_config_intent(
                    {
                        "action": "adopt",
                        "revision": view.revision,
                        "attestation": "external_maintenance_complete",
                    },
                    FakeRequest(),
                )

        serialized = json.dumps(
            {"config": config_payload, "intent": intent_payload}
        )
        self.assertEqual(config_payload["changed_keys"], [])
        self.assertEqual(config_payload["operation"]["changed_keys"], [])
        self.assertEqual(intent_payload["review"]["changed_keys"], [])
        self.assertNotIn("POSTGRES_PASSWORD", serialized)
        self.assertNotIn("UNLISTED_RUNTIME_FLAG", serialized)
        self.assertNotIn("secret-canary", serialized)

    def test_apply_route_consumes_the_bound_intent_once(self):
        initial = b"BUZZ_DOMAIN=first.test\n"
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(
                Path(td), initial, applied=initial
            )
            saved = store.save(
                store.describe().revision, {"BUZZ_DOMAIN": "second.test"}
            )
            intent_body = json.dumps(
                {"action": "apply", "revision": saved.revision}
            ).encode()
            result_payload = self.plugin._config_payload(store.describe())
            result_payload["reconcile_result"] = "applied"
            with patch.object(self.plugin, "CONFIG_STORE", store), patch.object(
                self.plugin,
                "_container_status",
                return_value={"running": True},
            ), patch.object(
                self.plugin,
                "_run_config_reconciler",
                return_value=result_payload,
            ) as reconcile:
                intent_response = asyncio.run(
                    self.plugin.config_intent_route(FakeRequest(intent_body))
                )
                token = response_json(intent_response)["intent"]
                apply_body = json.dumps(
                    {
                        "action": "apply",
                        "intent": token,
                        "revision": saved.revision,
                    }
                ).encode()
                first = asyncio.run(
                    self.plugin.config_apply_route(FakeRequest(apply_body))
                )
                replay = asyncio.run(
                    self.plugin.config_apply_route(FakeRequest(apply_body))
                )

        self.assertEqual(first.status_code, 200)
        reconcile.assert_called_once_with("apply", saved.revision, token)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(response_json(replay)["error"]["code"], "invalid_intent")

    def test_confirmation_is_bound_to_action_principal_and_expiry(self):
        initial = b"BUZZ_DOMAIN=first.test\n"
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(
                Path(td), initial, applied=initial
            )
            saved = store.save(
                store.describe().revision, {"BUZZ_DOMAIN": "second.test"}
            )
            intent_body = json.dumps(
                {"action": "apply", "revision": saved.revision}
            ).encode()
            with patch.object(self.plugin, "CONFIG_STORE", store), patch.object(
                self.plugin,
                "_container_status",
                return_value={"running": True},
            ):
                action_response = asyncio.run(
                    self.plugin.config_intent_route(FakeRequest(intent_body))
                )
                action_token = response_json(action_response)["intent"]
                wrong_action = asyncio.run(
                    self.plugin.config_restore_route(
                        FakeRequest(
                            json.dumps(
                                {
                                    "intent": action_token,
                                    "revision": saved.revision,
                                }
                            ).encode()
                        )
                    )
                )

                principal_response = asyncio.run(
                    self.plugin.config_intent_route(FakeRequest(intent_body))
                )
                principal_token = response_json(principal_response)["intent"]
                principal_request = FakeRequest(
                    json.dumps(
                        {
                            "action": "apply",
                            "intent": principal_token,
                            "revision": saved.revision,
                        }
                    ).encode()
                )
                principal_request.state.session.subject = "different-operator"
                wrong_principal = asyncio.run(
                    self.plugin.config_apply_route(principal_request)
                )

                expiry_response = asyncio.run(
                    self.plugin.config_intent_route(FakeRequest(intent_body))
                )
                expiry_token = response_json(expiry_response)["intent"]
                digest = self.plugin.hashlib.sha256(
                    expiry_token.encode("ascii")
                ).hexdigest()
                stored = self.plugin._INTENTS[digest]
                self.plugin._INTENTS[digest] = stored._replace(expires_at=0)
                expired = asyncio.run(
                    self.plugin.config_apply_route(
                        FakeRequest(
                            json.dumps(
                                {
                                    "action": "apply",
                                    "intent": expiry_token,
                                    "revision": saved.revision,
                                }
                            ).encode()
                        )
                    )
                )

        for response in (wrong_action, wrong_principal, expired):
            with self.subTest(response=response):
                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response_json(response)["error"]["code"],
                    "invalid_intent",
                )

    def test_restore_intent_is_one_use_and_bound_to_revision(self):
        initial = b"BUZZ_DOMAIN=first.test\n"
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(
                Path(td), initial, applied=initial
            )
            saved = store.save(
                store.describe().revision, {"BUZZ_DOMAIN": "second.test"}
            )
            intent_body = json.dumps(
                {"action": "restore", "revision": saved.revision}
            ).encode()
            with patch.object(self.plugin, "CONFIG_STORE", store):
                intent_response = asyncio.run(
                    self.plugin.config_intent_route(FakeRequest(intent_body))
                )
                token = response_json(intent_response)["intent"]
                restore_body = json.dumps(
                    {
                        "intent": token,
                        "revision": saved.revision,
                    }
                ).encode()
                first = asyncio.run(
                    self.plugin.config_restore_route(FakeRequest(restore_body))
                )
                replay = asyncio.run(
                    self.plugin.config_restore_route(FakeRequest(restore_body))
                )

        self.assertEqual(first.status_code, 200)
        self.assertFalse(response_json(first)["pending"])
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(response_json(replay)["error"]["code"], "invalid_intent")

    def test_adopt_requires_attestation_and_healthy_runtime(self):
        raw = b"BUZZ_DOMAIN=example.test\n"
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(Path(td), raw)
            revision = store.describe().revision
            without_attestation = json.dumps(
                {"action": "adopt", "revision": revision}
            ).encode()
            with_attestation = json.dumps(
                {
                    "action": "adopt",
                    "revision": revision,
                    "attestation": "external_maintenance_complete",
                }
            ).encode()
            with patch.object(self.plugin, "CONFIG_STORE", store), patch.object(
                self.plugin,
                "get_status",
                return_value={"healthy": False, "container": {"running": True}},
            ):
                missing = asyncio.run(
                    self.plugin.config_intent_route(
                        FakeRequest(without_attestation)
                    )
                )
                unhealthy = asyncio.run(
                    self.plugin.config_intent_route(FakeRequest(with_attestation))
                )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(unhealthy.status_code, 409)
        self.assertEqual(response_json(unhealthy)["error"]["code"], "runtime_unhealthy")

    def test_adopt_route_uses_desired_health_and_bound_reconciler_intent(self):
        raw = b"BUZZ_HTTP_PORT=4400\nBUZZ_DOMAIN=example.test\n"
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(Path(td), raw)
            revision = store.describe().revision
            intent_body = json.dumps(
                {
                    "action": "adopt",
                    "revision": revision,
                    "attestation": "external_maintenance_complete",
                }
            ).encode()
            result_payload = self.plugin._config_payload(store.describe())
            result_payload["reconcile_result"] = "adopted"
            with patch.object(self.plugin, "CONFIG_STORE", store), patch.object(
                self.plugin,
                "get_status",
                return_value={"healthy": True, "container": {"running": True}},
            ) as status, patch.object(
                self.plugin,
                "_run_config_reconciler",
                return_value=result_payload,
            ) as reconcile:
                intent_response = asyncio.run(
                    self.plugin.config_intent_route(FakeRequest(intent_body))
                )
                token = response_json(intent_response)["intent"]
                adopt_body = json.dumps(
                    {"intent": token, "revision": revision}
                ).encode()
                adopted = asyncio.run(
                    self.plugin.config_adopt_route(FakeRequest(adopt_body))
                )

        self.assertEqual(adopted.status_code, 200)
        status.assert_called_once_with(desired_config=True)
        reconcile.assert_called_once_with("adopt", revision, token)

    def test_degraded_adopt_uses_one_use_recovery_reconciler_intent(self):
        initial = b"BUZZ_HTTP_PORT=4400\nBUZZ_DOMAIN=first.test\n"
        with tempfile.TemporaryDirectory() as td:
            store, _paths = self.make_config_store(
                Path(td), initial, applied=initial
            )
            saved = store.save(
                store.describe().revision, {"BUZZ_DOMAIN": "second.test"}
            )
            store.write_journal(
                {
                    "phase": "degraded",
                    "action": "apply",
                    "revision": saved.revision,
                    "completed_at": "2026-08-15T12:00:00Z",
                    "outcome": "rollback_unverified",
                }
            )
            intent_body = json.dumps(
                {
                    "action": "adopt",
                    "revision": saved.revision,
                    "attestation": "external_maintenance_complete",
                }
            ).encode()
            result_payload = self.plugin._config_payload(store.describe())
            result_payload["reconcile_result"] = "recovered"
            with patch.object(
                self.plugin, "CONFIG_STORE", store
            ), patch.object(
                self.plugin,
                "get_status",
                return_value={"healthy": True, "container": {"running": True}},
            ), patch.object(
                self.plugin,
                "_run_config_reconciler",
                return_value=result_payload,
            ) as reconcile:
                intent_response = asyncio.run(
                    self.plugin.config_intent_route(FakeRequest(intent_body))
                )
                token = response_json(intent_response)["intent"]
                adopt_body = json.dumps(
                    {"intent": token, "revision": saved.revision}
                ).encode()
                recovered = asyncio.run(
                    self.plugin.config_adopt_route(FakeRequest(adopt_body))
                )
                replay = asyncio.run(
                    self.plugin.config_adopt_route(FakeRequest(adopt_body))
                )

        self.assertEqual(intent_response.status_code, 200)
        self.assertEqual(recovered.status_code, 200)
        reconcile.assert_called_once_with("recover_adopt", saved.revision, token)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(response_json(replay)["error"]["code"], "invalid_intent")


if __name__ == "__main__":
    unittest.main()
