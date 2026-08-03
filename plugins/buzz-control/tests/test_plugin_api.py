from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


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

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi.APIRouter = APIRouter
    fastapi.HTTPException = HTTPException
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
        self.assertEqual(self.plugin.HEALTH_PATH, "/_liveness")
        self.assertEqual(
            self.plugin.PUBLIC_RELAY_URL,
            "ws://127.0.0.1:3300",
        )
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
        self.assertEqual(plugin.DEPLOY_DIR, Path("/tmp/buzz-compose"))
        self.assertEqual(plugin.PUBLIC_RELAY_URL, "wss://buzz.example.test")

    def test_runtime_settings_reject_browser_unsafe_values(self):
        with self.assertRaisesRegex(ValueError, "may contain only"):
            load_plugin_api({"BUZZ_CONTROL_COMPOSE_SERVICE": "relay;whoami"})
        with self.assertRaisesRegex(ValueError, "loopback"):
            load_plugin_api({"BUZZ_CONTROL_LOCAL_HOST": "example.com"})
        with self.assertRaisesRegex(ValueError, "between 1 and 65535"):
            load_plugin_api({"BUZZ_CONTROL_LOCAL_PORT": "70000"})
        with self.assertRaisesRegex(ValueError, "must be an absolute path"):
            load_plugin_api({"BUZZ_CONTROL_DEPLOY_DIR": "relative/path"})

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
        connection = MagicMock()
        response = MagicMock(status=200)
        response.read.return_value = b"ok"
        connection.getresponse.return_value = response

        with patch.object(
            self.plugin.http.client,
            "HTTPConnection",
            return_value=connection,
        ) as http_connection:
            result = self.plugin._probe_health()

        http_connection.assert_called_once_with("127.0.0.1", 3300, timeout=3.0)
        connection.request.assert_called_once_with("GET", "/_liveness")
        self.assertTrue(result["healthy"])
        self.assertEqual(result["response"], "ok")

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
        with patch.object(
            self.plugin, "_container_status", return_value=container
        ), patch.object(self.plugin, "_probe_health", return_value=probe):
            status = self.plugin.get_status()

        self.assertTrue(status["healthy"])
        self.assertEqual(status["relay"]["public_url"], self.plugin.PUBLIC_RELAY_URL)
        self.assertEqual(status["deployment"]["project"], "buzz-prod")

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


if __name__ == "__main__":
    unittest.main()
