from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import call, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_API_PATH = REPO_ROOT / "dashboard" / "plugin_api.py"


def load_plugin_api():
    spec = importlib.util.spec_from_file_location(
        "nesquena_webui_control_plugin_api", PLUGIN_API_PATH
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
    with patch.dict(sys.modules, {"fastapi": fastapi}):
        spec.loader.exec_module(module)
    return module


def completed(*args: str, returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class PluginApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin_api()

    def test_parse_launchctl_status_reports_running_pid_and_last_exit(self):
        output = """
gui/502/ai.hermes.webui = {
    state = running
    pid = 74523
    last exit code = 0
}
"""

        status = self.plugin._parse_launchctl_status(completed("print", stdout=output))

        self.assertTrue(status["loaded"])
        self.assertTrue(status["running"])
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["pid"], 74523)
        self.assertEqual(status["last_exit_code"], 0)

    def test_parse_launchctl_status_reports_unloaded_service(self):
        status = self.plugin._parse_launchctl_status(
            completed("print", returncode=113, stderr="Could not find service")
        )

        self.assertFalse(status["loaded"])
        self.assertFalse(status["running"])
        self.assertEqual(status["state"], "stopped")
        self.assertIsNone(status["pid"])

    def test_get_status_combines_launchd_and_http_health(self):
        launchd = {
            "loaded": True,
            "running": True,
            "state": "running",
            "pid": 123,
            "last_exit_code": 0,
        }
        http = {"reachable": True, "status_code": 302, "error": None}

        with patch.object(self.plugin, "_service_status", return_value=launchd), patch.object(
            self.plugin, "_probe_http", return_value=http
        ):
            status = self.plugin.get_status()

        self.assertTrue(status["healthy"])
        self.assertEqual(status["endpoint"], "http://127.0.0.1:8787/")
        self.assertEqual(status["http"]["status_code"], 302)
        self.assertEqual(status["service"], "ai.hermes.webui")

    def test_start_bootstraps_an_unloaded_service(self):
        stopped = {
            "loaded": False,
            "running": False,
            "state": "stopped",
            "pid": None,
            "last_exit_code": None,
        }
        running = {
            **stopped,
            "loaded": True,
            "running": True,
            "state": "running",
            "pid": 222,
            "healthy": True,
        }

        with tempfile.TemporaryDirectory() as td:
            plist = Path(td) / "ai.hermes.webui.plist"
            plist.touch()
            with patch.object(self.plugin, "PLIST_PATH", plist), patch.object(
                self.plugin, "_service_status", return_value=stopped
            ), patch.object(
                self.plugin,
                "_run_launchctl",
                side_effect=[completed("enable"), completed("bootstrap")],
            ) as run, patch.object(
                self.plugin, "_wait_for_status", return_value=running
            ):
                result = self.plugin.start_service()

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["status"]["pid"], 222)
        self.assertEqual(
            run.call_args_list,
            [
                call("enable", self.plugin.SERVICE_TARGET),
                call("bootstrap", self.plugin.GUI_DOMAIN, str(plist)),
            ],
        )

    def test_start_is_idempotent_when_already_running(self):
        running = {
            "loaded": True,
            "running": True,
            "state": "running",
            "pid": 333,
            "last_exit_code": 0,
            "healthy": True,
        }
        http = {"reachable": True, "status_code": 200, "error": None}
        with patch.object(
            self.plugin, "_service_status", return_value=running
        ) as service_status, patch.object(
            self.plugin, "_run_launchctl"
        ) as run, patch.object(self.plugin, "_probe_http", return_value=http):
            result = self.plugin.start_service()

        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        service_status.assert_called_once_with()
        run.assert_not_called()

    def test_wait_probes_http_only_after_launchd_reaches_the_goal(self):
        stopped = {
            "loaded": False,
            "running": False,
            "state": "stopped",
            "pid": None,
            "last_exit_code": None,
        }
        running = {
            **stopped,
            "loaded": True,
            "running": True,
            "state": "running",
            "pid": 334,
        }
        http = {"reachable": True, "status_code": 302, "error": None}

        with patch.object(
            self.plugin, "_service_status", side_effect=[stopped, running]
        ) as service_status, patch.object(
            self.plugin, "_probe_http", return_value=http
        ) as probe, patch.object(self.plugin.time, "sleep"):
            result = self.plugin._wait_for_status(
                loaded=True,
                running=True,
                require_healthy=True,
                timeout=1,
            )

        self.assertTrue(result["healthy"])
        self.assertEqual(service_status.call_count, 2)
        probe.assert_called_once_with()

    def test_stop_boots_out_the_keepalive_service(self):
        running = {
            "loaded": True,
            "running": True,
            "state": "running",
            "pid": 444,
            "last_exit_code": 0,
        }
        stopped = {
            "loaded": False,
            "running": False,
            "state": "stopped",
            "pid": None,
            "last_exit_code": None,
            "healthy": False,
        }
        with patch.object(self.plugin, "_service_status", return_value=running), patch.object(
            self.plugin, "_run_launchctl", return_value=completed("bootout")
        ) as run, patch.object(
            self.plugin, "_wait_for_status", return_value=stopped
        ):
            result = self.plugin.stop_service()

        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        run.assert_called_once_with("bootout", self.plugin.SERVICE_TARGET)

    def test_restart_uses_forced_kickstart_for_a_loaded_service(self):
        running = {
            "loaded": True,
            "running": True,
            "state": "running",
            "pid": 555,
            "last_exit_code": 0,
        }
        restarted = {**running, "pid": 556, "healthy": True}
        with patch.object(self.plugin, "_service_status", return_value=running), patch.object(
            self.plugin, "_run_launchctl", return_value=completed("kickstart")
        ) as run, patch.object(
            self.plugin, "_wait_for_status", return_value=restarted
        ):
            result = self.plugin.restart_service()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"]["pid"], 556)
        run.assert_called_once_with("kickstart", "-k", self.plugin.SERVICE_TARGET)

    def test_restart_starts_an_unloaded_service_without_rechecking_status(self):
        stopped = {
            "loaded": False,
            "running": False,
            "state": "stopped",
            "pid": None,
            "last_exit_code": None,
        }
        running = {
            **stopped,
            "loaded": True,
            "running": True,
            "state": "running",
            "pid": 557,
            "healthy": True,
        }

        with tempfile.TemporaryDirectory() as td:
            plist = Path(td) / "ai.hermes.webui.plist"
            plist.touch()
            with patch.object(self.plugin, "PLIST_PATH", plist), patch.object(
                self.plugin, "_service_status", return_value=stopped
            ) as service_status, patch.object(
                self.plugin,
                "_run_launchctl",
                side_effect=[completed("enable"), completed("bootstrap")],
            ), patch.object(
                self.plugin, "_wait_for_status", return_value=running
            ):
                result = self.plugin.restart_service()

        self.assertEqual(result["action"], "restart")
        self.assertTrue(result["changed"])
        self.assertEqual(result["status"]["pid"], 557)
        service_status.assert_called_once_with()

    def test_failed_launchctl_command_raises_a_safe_control_error(self):
        failed = completed("bootstrap", returncode=5, stderr="bootstrap failed: 5")

        with self.assertRaisesRegex(self.plugin.ServiceControlError, "bootstrap failed"):
            self.plugin._require_success("start", failed)


if __name__ == "__main__":
    unittest.main()
