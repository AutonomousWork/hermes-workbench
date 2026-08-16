from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
RECONCILER_PATH = PLUGIN_ROOT / "scripts" / "reconcile.py"


def load_reconciler():
    name = "buzz_control_reconcile_test"
    spec = importlib.util.spec_from_file_location(name, RECONCILER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load scripts/reconcile.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ReconcilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reconcile = load_reconciler()

    def make_runner(
        self,
        root: Path,
        port: int,
        *,
        up_mode: str = "success",
        running: bool = True,
        action: str = "apply",
        config_hash_match: bool = True,
    ):
        config_dir = root / "config"
        state_dir = root / "state"
        config_dir.mkdir(mode=0o700)
        state_dir.mkdir(mode=0o700)
        desired = config_dir / "prod.env"
        initial = f"BUZZ_HTTP_PORT={port}\nBUZZ_DOMAIN=first.test\n".encode()
        desired.write_bytes(initial)
        desired.chmod(0o600)
        paths = self.reconcile.CONFIG_MODULE.ConfigPaths.for_desired(
            desired, state_dir
        )
        paths.applied.write_bytes(initial)
        paths.applied.chmod(0o600)
        store = self.reconcile.CONFIG_MODULE.ConfigStore(paths)
        saved = store.save(
            store.describe().revision,
            {"BUZZ_DOMAIN": "second.test"},
        )

        call_log = root / "calls.log"
        runtime_state = root / "runtime-state"
        runtime_state.write_text("running" if running else "stopped")
        fake = root / "docker"
        if up_mode == "success":
            up_action = f"printf running > {runtime_state}"
        elif up_mode == "rollback":
            up_action = (
                f'case "$BUZZ_SERVICE_ENV_FILE" in "{paths.applied}") '
                f"printf running > {runtime_state} ;; *) exit 41 ;; esac"
            )
        elif up_mode == "start_then_fail":
            up_action = f"printf running > {runtime_state}; exit 42"
        else:
            up_action = "exit 42"
        desired_hash = "a" * 64
        runtime_hash = desired_hash if config_hash_match else "b" * 64
        fake.write_text(
            "#!/bin/sh\n"
            f"printf '%s|%s\\n' \"${{BUZZ_SERVICE_ENV_FILE-}}\" \"$*\" >> {call_log}\n"
            "case \"$*\" in\n"
            f"  *\" config --hash relay\"*) echo 'relay {desired_hash}' ;;\n"
            f"  *\" ps --all -q relay\"*) if [ \"$(cat {runtime_state})\" = running ]; then echo relay-container; else echo stopped-container; fi ;;\n"
            f"  *\" ps -q relay\"*) if [ \"$(cat {runtime_state})\" = running ]; then echo relay-container; fi ;;\n"
            "  *\"inspect --format {{.Image}} relay-container\"*|*\"inspect --format {{.Image}} stopped-container\"*) echo sha256:old ;;\n"
            f"  *\"com.docker.compose.config-hash\"*) echo {runtime_hash} ;;\n"
            "  *\"inspect --format {{if .State.Health}}\"*) echo healthy ;;\n"
            f"  *\" up -d --wait --no-deps --pull never relay\"*) {up_action} ;;\n"
            f"  *\" stop relay\"*) printf stopped > {runtime_state} ;;\n"
            "esac\n"
        )
        fake.chmod(0o755)
        settings = self.reconcile.Settings(
            desired=desired,
            state_dir=state_dir,
            docker=fake,
            compose=fake,
            docker_host="unix:///tmp/fake-docker.sock",
            compose_file=PLUGIN_ROOT / "deploy" / "compose.yml",
            override_file=PLUGIN_ROOT / "deploy" / "compose.local.yml",
            project="buzz-prod",
            service="relay",
            image="ghcr.io/block/buzz:main",
            docker_config=state_dir / "docker-anonymous",
            local_host="127.0.0.1",
            local_port_override=None,
            health_path="/_liveness",
            timeout=10.0,
        )
        runner = self.reconcile.Reconciler(settings)
        # The workspace sandbox cannot bind a loopback test server. The
        # existing API health-probe test covers the HTTP client contract; these
        # tests keep the real file/lock/Compose/Docker chain and isolate only
        # the final loopback response.
        runner._http_healthy = lambda _env_file: True
        token = "A" * 43
        if action == "recover_adopt":
            store.write_journal(
                {
                    "phase": "degraded",
                    "action": "apply",
                    "revision": saved.revision,
                    "completed_at": "2026-08-15T12:00:00Z",
                    "outcome": "rollback_unverified",
                }
            )
        store.record_operation_intent(
            digest=hashlib.sha256(token.encode()).hexdigest(),
            action=action,
            revision=saved.revision,
            expires_at=__import__("time").time() + 60,
        )
        return runner, store, paths, saved, token, call_log

    def test_settings_use_xdg_config_home_for_the_desired_file(self):
        with patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": "/tmp/buzz-reconcile-xdg"},
            clear=True,
        ):
            settings = self.reconcile._settings()

        self.assertEqual(
            settings.desired,
            Path("/tmp/buzz-reconcile-xdg/buzz/prod.env"),
        )

    def test_apply_uses_desired_then_promotes_only_after_full_health(self):
        with tempfile.TemporaryDirectory() as td:
            port = 3300
            runner, store, paths, saved, token, call_log = self.make_runner(
                Path(td), port
            )

            result = runner.apply("apply", saved.revision, token)

            self.assertEqual(result, "applied")
            view = store.describe()
            self.assertFalse(view.pending)
            self.assertEqual(paths.applied.read_bytes(), paths.desired.read_bytes())
            self.assertEqual(view.journal["phase"], "applied")
            calls = call_log.read_text()
            self.assertIn(" config --quiet", calls)
            apply_call = next(line for line in calls.splitlines() if " up -d " in line)
            self.assertIn(str(paths.operation), apply_call)
            self.assertIn("sha256:old", view.journal["runtime_generation"])

    def test_failed_apply_rolls_back_applied_snapshot_and_keeps_desired_pending(self):
        with tempfile.TemporaryDirectory() as td:
            port = 3300
            runner, store, paths, saved, token, call_log = self.make_runner(
                Path(td), port, up_mode="rollback"
            )
            baseline = paths.applied.read_bytes()

            result = runner.apply("apply", saved.revision, token)

            self.assertEqual(result, "rolled_back")
            view = store.describe()
            self.assertTrue(view.pending)
            self.assertEqual(paths.applied.read_bytes(), baseline)
            self.assertEqual(view.journal["phase"], "rolled_back")
            up_calls = [line for line in call_log.read_text().splitlines() if " up -d " in line]
            self.assertEqual(len(up_calls), 2)
            self.assertIn(str(paths.operation), up_calls[0])
            self.assertIn(str(paths.applied), up_calls[1])

    def test_apply_rolls_back_if_live_desired_drifts_after_recreate(self):
        with tempfile.TemporaryDirectory() as td:
            runner, store, paths, saved, token, call_log = self.make_runner(
                Path(td), 3300
            )
            baseline = paths.applied.read_bytes()
            real_compose = runner._compose
            drifted = False

            def compose_with_drift(env_file, image, *arguments, **kwargs):
                nonlocal drifted
                result = real_compose(env_file, image, *arguments, **kwargs)
                if (
                    not drifted
                    and env_file == paths.operation
                    and "up" in arguments
                ):
                    drifted = True
                    replacement = paths.desired.with_name("external.env")
                    replacement.write_bytes(
                        b"BUZZ_HTTP_PORT=3300\nBUZZ_DOMAIN=external.test\n"
                    )
                    replacement.chmod(0o600)
                    os.replace(replacement, paths.desired)
                return result

            runner._compose = compose_with_drift

            result = runner.apply("apply", saved.revision, token)

            self.assertEqual(result, "rolled_back")
            self.assertEqual(paths.applied.read_bytes(), baseline)
            self.assertIn(b"external.test", paths.desired.read_bytes())
            up_calls = [
                line
                for line in call_log.read_text().splitlines()
                if " up -d " in line
            ]
            self.assertIn(str(paths.operation), up_calls[0])
            self.assertIn(str(paths.applied), up_calls[1])
            self.assertEqual(store.describe().journal["phase"], "rolled_back")

    def test_protected_read_failure_after_recreate_enters_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            runner, store, paths, saved, token, call_log = self.make_runner(
                Path(td), 3300
            )

            def protected_health(env_file):
                if env_file == paths.operation:
                    raise self.reconcile.CONFIG_MODULE.ConfigStoreError(
                        "unsafe_storage"
                    )
                return True

            runner._http_healthy = protected_health

            result = runner.apply("apply", saved.revision, token)

            self.assertEqual(result, "rolled_back")
            self.assertEqual(store.describe().journal["phase"], "rolled_back")
            up_calls = [
                line
                for line in call_log.read_text().splitlines()
                if " up -d " in line
            ]
            self.assertEqual(len(up_calls), 2)

    def test_promotion_storage_failure_restores_baseline_before_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            runner, store, paths, saved, token, _call_log = self.make_runner(
                Path(td), 3300
            )
            baseline = paths.applied.read_bytes()
            real_write_journal = runner.store._write_journal_locked

            def fail_terminal_promotion(entry):
                if entry.get("phase") == "applied":
                    raise self.reconcile.CONFIG_MODULE.ConfigStoreError(
                        "unsafe_storage"
                    )
                return real_write_journal(entry)

            runner.store._write_journal_locked = fail_terminal_promotion

            result = runner.apply("apply", saved.revision, token)

            self.assertEqual(result, "rolled_back")
            self.assertEqual(paths.applied.read_bytes(), baseline)
            self.assertEqual(store.describe().journal["phase"], "rolled_back")

    def test_unverified_rollback_enters_degraded_and_blocks_more_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            port = 3300
            runner, store, _paths, saved, token, _call_log = self.make_runner(
                Path(td), port, up_mode="degraded"
            )

            with self.assertRaises(self.reconcile.ReconcileFailure) as raised:
                runner.apply("apply", saved.revision, token)

            self.assertEqual(raised.exception.code, "degraded")
            view = store.describe()
            self.assertEqual(view.journal["phase"], "degraded")
            with self.assertRaises(self.reconcile.CONFIG_MODULE.ConfigStoreError) as blocked:
                store.save(saved.revision, {"BUZZ_DOMAIN": "third.test"})
            self.assertEqual(blocked.exception.code, "recovery_required")

    def test_port_override_conflict_fails_before_recreation(self):
        with tempfile.TemporaryDirectory() as td:
            port = 3300
            runner, _store, _paths, saved, token, call_log = self.make_runner(
                Path(td), port
            )
            runner.settings = runner.settings._replace(local_port_override=port + 1)

            with self.assertRaises(self.reconcile.ReconcileFailure) as raised:
                runner.apply("apply", saved.revision, token)

            self.assertEqual(raised.exception.code, "port_conflict")
            self.assertNotIn(" up -d ", call_log.read_text())

    def test_runtime_port_is_read_even_though_it_is_not_browser_managed(self):
        with tempfile.TemporaryDirectory() as td:
            runner, _store, paths, _saved, _token, _call_log = self.make_runner(
                Path(td), 4400
            )

            self.assertEqual(runner._http_port(paths.desired), 4400)

    def test_apply_start_uses_stopped_container_image_and_promotes(self):
        with tempfile.TemporaryDirectory() as td:
            runner, store, _paths, saved, token, call_log = self.make_runner(
                Path(td),
                3300,
                running=False,
                action="apply_start",
            )

            result = runner.apply("apply_start", saved.revision, token)

            self.assertEqual(result, "applied")
            self.assertFalse(store.describe().pending)
            calls = call_log.read_text()
            self.assertIn(" ps --all -q relay", calls)
            self.assertIn("inspect --format {{.Image}} stopped-container", calls)

    def test_failed_apply_start_restores_stopped_state(self):
        with tempfile.TemporaryDirectory() as td:
            runner, store, _paths, saved, token, call_log = self.make_runner(
                Path(td),
                3300,
                up_mode="start_then_fail",
                running=False,
                action="apply_start",
            )

            result = runner.apply("apply_start", saved.revision, token)

            self.assertEqual(result, "rolled_back")
            self.assertTrue(store.describe().pending)
            self.assertIn(" stop relay", call_log.read_text())

    def test_apply_start_blocks_without_a_stopped_container_identity(self):
        with tempfile.TemporaryDirectory() as td:
            runner, _store, _paths, saved, token, _call_log = self.make_runner(
                Path(td),
                3300,
                running=False,
                action="apply_start",
            )
            runner._container_id = lambda *_args, **kwargs: (
                "" if kwargs.get("all_containers") else ""
            )

            with self.assertRaises(self.reconcile.ReconcileFailure) as raised:
                runner.apply("apply_start", saved.revision, token)

            self.assertEqual(raised.exception.code, "immutable_image_missing")

    def test_adopt_promotes_only_a_matching_healthy_compose_generation(self):
        with tempfile.TemporaryDirectory() as td:
            runner, store, paths, saved, token, call_log = self.make_runner(
                Path(td),
                3300,
                action="adopt",
            )

            result = runner.adopt(saved.revision, token)

            self.assertEqual(result, "adopted")
            view = store.describe()
            self.assertFalse(view.pending)
            self.assertEqual(paths.applied.read_bytes(), paths.desired.read_bytes())
            self.assertEqual(view.journal["action"], "adopt")
            hash_call = next(
                line
                for line in call_log.read_text().splitlines()
                if " config --hash relay" in line
            )
            self.assertIn(str(paths.operation), hash_call)

    def test_adopt_rejects_a_healthy_stale_compose_generation(self):
        with tempfile.TemporaryDirectory() as td:
            runner, store, _paths, saved, token, _call_log = self.make_runner(
                Path(td),
                3300,
                action="adopt",
                config_hash_match=False,
            )

            with self.assertRaises(self.reconcile.ReconcileFailure) as raised:
                runner.adopt(saved.revision, token)

            self.assertEqual(raised.exception.code, "runtime_unverified")
            self.assertTrue(store.describe().pending)

    def test_degraded_runtime_can_be_recovered_by_attested_adoption_once(self):
        with tempfile.TemporaryDirectory() as td:
            runner, store, paths, saved, token, _call_log = self.make_runner(
                Path(td),
                3300,
                action="recover_adopt",
            )
            with self.assertRaises(
                self.reconcile.CONFIG_MODULE.ConfigStoreError
            ) as blocked:
                store.save(saved.revision, {"BUZZ_DOMAIN": "third.test"})
            self.assertEqual(blocked.exception.code, "recovery_required")

            result = runner.adopt(saved.revision, token, recovery=True)

            self.assertEqual(result, "recovered")
            view = store.describe()
            self.assertFalse(view.pending)
            self.assertEqual(paths.applied.read_bytes(), paths.operation.read_bytes())
            self.assertEqual(view.journal["phase"], "applied")
            self.assertEqual(view.journal["action"], "recover_adopt")
            self.assertEqual(view.journal["outcome"], "recovered")
            with self.assertRaises(
                self.reconcile.CONFIG_MODULE.ConfigStoreError
            ) as replay:
                runner.adopt(saved.revision, token, recovery=True)
            self.assertEqual(replay.exception.code, "invalid_intent")

    def test_image_update_always_uses_applied_snapshot_when_baseline_exists(self):
        with tempfile.TemporaryDirectory() as td:
            runner, store, paths, saved, _token, _call_log = self.make_runner(
                Path(td), 3300
            )
            store.promote(saved.revision)
            completed = subprocess.CompletedProcess(["update"], 0)

            with patch.object(
                self.reconcile.subprocess,
                "run",
                return_value=completed,
            ) as run:
                result = runner.image("manual")

            self.assertEqual(result, 0)
            command = run.call_args.args[0]
            self.assertEqual(command[3], str(paths.applied))
            self.assertEqual(command[4], "false")

    def test_image_child_timeout_is_translated_to_safe_failure(self):
        with tempfile.TemporaryDirectory() as td:
            runner, _store, _paths, _saved, _token, _call_log = self.make_runner(
                Path(td), 3300
            )
            with patch.object(
                self.reconcile.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("update.sh", 10),
            ):
                with self.assertRaises(self.reconcile.ReconcileFailure) as raised:
                    runner.image("manual")

            self.assertEqual(raised.exception.code, "timed_out")


if __name__ == "__main__":
    unittest.main()
