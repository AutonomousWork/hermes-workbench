from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard"
DEPLOY_DIR = PLUGIN_ROOT / "deploy"
INSTALLER = PLUGIN_ROOT / "scripts" / "install.sh"
UPDATER = PLUGIN_ROOT / "scripts" / "update.sh"


class PluginContractTests(unittest.TestCase):
    def test_runtime_manifest_is_an_inert_dashboard_allow_list_shim(self):
        manifest = (PLUGIN_ROOT / "plugin.yaml").read_text()
        runtime = (PLUGIN_ROOT / "__init__.py").read_text()

        self.assertIn("name: buzz-control", manifest)
        self.assertIn("hooks: []", manifest)
        self.assertIn("def register(_context)", runtime)

    def test_manifest_registers_the_buzz_tab_and_backend(self):
        manifest = json.loads((DASHBOARD / "manifest.json").read_text())

        self.assertEqual(manifest["name"], "buzz-control")
        self.assertEqual(manifest["label"], "Buzz")
        self.assertEqual(manifest["tab"]["path"], "/buzz")
        self.assertEqual(manifest["tab"]["position"], "after:achievements")
        self.assertEqual(manifest["entry"], "dist/index.js")
        self.assertEqual(manifest["css"], "dist/style.css")
        self.assertEqual(manifest["api"], "plugin_api.py")

    def test_bundle_uses_authenticated_sdk_and_exposes_update_workflow(self):
        bundle = (DASHBOARD / "dist" / "index.js").read_text()

        self.assertIn("SDK.fetchJSON", bundle)
        self.assertNotIn("window.fetch(", bundle)
        self.assertIn('api("/status")', bundle)
        self.assertIn('api("/updates")', bundle)
        self.assertIn('api("/update", { method: "POST" })', bundle)
        self.assertIn('registry.register("buzz-control"', bundle)
        self.assertIn("Server health", bundle)
        self.assertIn("Relay location", bundle)
        self.assertIn("Latest updates", bundle)
        self.assertIn("Managed schedule", bundle)
        self.assertIn("buzz-control-image-update", bundle)
        self.assertIn('href: "/cron"', bundle)
        self.assertNotIn("Recent changes on main", bundle)

    def test_backend_uses_fixed_argument_arrays_without_a_shell(self):
        backend = (DASHBOARD / "plugin_api.py").read_text()

        self.assertIn("subprocess.run(", backend)
        self.assertNotIn("shell=True", backend)
        self.assertNotIn("request.query", backend)
        self.assertNotIn("request.json", backend)
        self.assertIn('[str(UPDATER_PATH), "manual"]', backend)

    def test_updater_owns_compose_pull_compare_and_apply_logic(self):
        updater = UPDATER.read_text()

        self.assertIn('docker_image pull "$IMAGE"', updater)
        self.assertIn(
            'compose up -d --wait --no-deps --pull never "$SERVICE"', updater
        )
        self.assertIn('--project-name "$PROJECT"', updater)
        self.assertIn("old_image_id=$(container_image_id)", updater)
        self.assertIn("image_metadata=$(", updater)
        self.assertIn("RESULT=updated", updater)
        self.assertIn("RESULT=already-current", updater)
        self.assertNotIn('compose pull "$SERVICE"', updater)
        self.assertTrue(os.access(UPDATER, os.X_OK))

    def test_plugin_vendors_the_production_compose_capsule(self):
        base = (DEPLOY_DIR / "compose.yml").read_text()
        override = (DEPLOY_DIR / "compose.local.yml").read_text()

        self.assertIn("name: buzz-prod", base)
        self.assertIn("ghcr.io/block/buzz:main", base)
        self.assertIn("buzz-git-data:/data/git", base)
        self.assertIn("127.0.0.1:${BUZZ_HTTP_PORT:-3300}:3000", override)
        self.assertIn("${BUZZ_SERVICE_ENV_FILE", override)
        self.assertIn(
            "name: ${PROXIMA_DOCKER_NETWORK:-proxima_default}", override
        )

    def test_styles_are_scoped_and_theme_aware(self):
        stylesheet = (DASHBOARD / "dist" / "style.css").read_text()

        self.assertIn(".buzz-control", stylesheet)
        self.assertIn("var(--color-card)", stylesheet)
        self.assertIn("var(--color-border)", stylesheet)
        self.assertNotIn(".nesquena-control", stylesheet)

    def _run_fake_updater(
        self,
        temp_root: Path,
        docker_body: str,
        *,
        env_text: str = "BUZZ_IMAGE=ghcr.io/block/buzz:main\n",
        updater_args: list[str] | None = None,
        extra_environment: dict[str, str] | None = None,
        prior_receipt: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
        env_file = temp_root / "prod.env"
        env_file.write_text(env_text)
        env_file.chmod(0o600)

        call_log = temp_root / "docker-calls.log"
        fake_docker = temp_root / "docker"
        fake_docker.write_text(
            "#!/bin/sh\n"
            f"printf '%s|%s\\n' \"${{BUZZ_IMAGE-}}\" \"$*\" >> {call_log}\n"
            + docker_body
        )
        fake_docker.chmod(0o755)

        hermes_home = temp_root / "hermes"
        receipt = hermes_home / "state" / "buzz-control" / "update-state"
        if prior_receipt is not None:
            receipt.parent.mkdir(parents=True)
            receipt.write_text(prior_receipt)
            receipt.chmod(0o600)

        environment = os.environ.copy()
        environment.update(
            {
                "BUZZ_CONTROL_DOCKER_BIN": str(fake_docker),
                "BUZZ_CONTROL_COMPOSE_BIN": str(fake_docker),
                "BUZZ_CONTROL_ENV_FILE": str(env_file),
                "HERMES_HOME": str(hermes_home),
            }
        )
        if extra_environment:
            environment.update(extra_environment)

        result = subprocess.run(
            [str(UPDATER), *(updater_args or [])],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )
        return result, call_log, receipt

    def test_updater_pulls_and_applies_a_new_image_with_fake_docker(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            env_file = temp_root / "prod.env"
            env_file.write_text("BUZZ_IMAGE=ghcr.io/block/buzz:main\n")
            env_file.chmod(0o600)

            fake_docker = temp_root / "docker"
            call_log = temp_root / "docker-calls.log"
            state = temp_root / "updated"
            fake_docker.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {call_log}\n"
                "case \"$*\" in\n"
                "  *\"--project-name buzz-prod\"*\"ps -q relay\"*)\n"
                f"    if [ -f {state} ]; then echo new-container; else echo old-container; fi\n"
                "    ;;\n"
                "  *\"inspect --format {{.Image}} old-container\") echo sha256:old ;;\n"
                "  *\"inspect --format {{.Image}} new-container\") echo sha256:new ;;\n"
                "  *\"inspect --format {{if .State.Health}}\"*) echo healthy ;;\n"
                "  *\"image inspect --format {{.Id}}|\"*) echo 'sha256:new|[\"ghcr.io/block/buzz@sha256:newdigest\"]|revision-new|2026-08-03T01:55:25Z|END' ;;\n"
                "  *\"--project-name buzz-prod\"*\"up -d --wait --no-deps --pull never relay\"*) touch "
                f"{state}"
                " ;;\n"
                "esac\n"
            )
            fake_docker.chmod(0o755)

            environment = os.environ.copy()
            environment.update(
                {
                    "BUZZ_CONTROL_DOCKER_BIN": str(fake_docker),
                    "BUZZ_CONTROL_COMPOSE_BIN": str(fake_docker),
                    "BUZZ_CONTROL_ENV_FILE": str(env_file),
                    "HERMES_HOME": str(temp_root / "hermes"),
                }
            )
            result = subprocess.run(
                [str(UPDATER)],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("RESULT=updated", result.stdout)
            calls = call_log.read_text()
            self.assertIn("image pull ghcr.io/block/buzz:main", calls)
            self.assertNotIn("compose pull", calls)
            self.assertIn("--project-name buzz-prod", calls)
            self.assertIn("up -d --wait --no-deps --pull never relay", calls)
            receipt = (
                temp_root / "hermes" / "state" / "buzz-control" / "update-state"
            )
            self.assertIn("result=updated", receipt.read_text())

    def test_image_override_is_used_for_pull_and_compose(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            applied = temp_root / "applied"
            docker_body = (
                "case \"$*\" in\n"
                "  *\"--project-name buzz-prod\"*\"ps -q relay\"*)\n"
                f"    if [ -f {applied} ]; then echo new-container; else echo old-container; fi ;;\n"
                "  *\"inspect --format {{.Image}} old-container\") echo sha256:old ;;\n"
                "  *\"inspect --format {{.Image}} new-container\") echo sha256:new ;;\n"
                "  *\"inspect --format {{if .State.Health}}\"*) echo healthy ;;\n"
                "  *\"image inspect --format {{.Id}}|\"*) echo 'sha256:new|[\"ghcr.io/block/buzz@sha256:newdigest\"]|revision-new|2026-08-03T01:55:25Z|END' ;;\n"
                "  *\"--project-name buzz-prod\"*\"up -d --wait --no-deps --pull never relay\"*)\n"
                "    [ \"${BUZZ_IMAGE-}\" = \"ghcr.io/block/buzz:candidate\" ] || exit 42\n"
                f"    touch {applied} ;;\n"
                "esac\n"
            )
            result, call_log, _receipt = self._run_fake_updater(
                temp_root,
                docker_body,
                env_text="BUZZ_IMAGE=ghcr.io/block/buzz:main\n",
                extra_environment={
                    "BUZZ_CONTROL_IMAGE": "ghcr.io/block/buzz:candidate"
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = call_log.read_text()
            self.assertIn("image pull ghcr.io/block/buzz:candidate", calls)
            self.assertIn(
                "ghcr.io/block/buzz:candidate|", calls
            )

    def test_pull_failure_carries_forward_last_observed_image(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            docker_body = (
                "case \"$*\" in\n"
                "  *\"--project-name buzz-prod\"*\"ps -q relay\"*) echo relay-container ;;\n"
                "  *\"inspect --format {{.Image}} relay-container\") echo sha256:running ;;\n"
                "  *\"image pull \"*) echo registry-unavailable >&2; exit 1 ;;\n"
                "esac\n"
            )
            prior = (
                "schema_version=1\n"
                "result=already_current\n"
                "latest_image_id=sha256:observed\n"
                "latest_image_digest=sha256:observed-digest\n"
                "latest_image_revision=observed-revision\n"
                "latest_image_created_at=2026-08-02T01:00:00Z\n"
            )
            result, _call_log, receipt = self._run_fake_updater(
                temp_root, docker_body, prior_receipt=prior
            )

            self.assertNotEqual(result.returncode, 0)
            state = receipt.read_text()
            self.assertIn("result=pull_failed", state)
            self.assertIn("latest_image_observed_this_attempt=false", state)
            self.assertIn("latest_image_id=sha256:observed", state)
            self.assertIn("latest_image_digest=sha256:observed-digest", state)
            self.assertIn("latest_image_revision=observed-revision", state)
            self.assertIn(
                "latest_image_created_at=2026-08-02T01:00:00Z", state
            )

    def test_apply_failure_preserves_freshly_pulled_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            docker_body = (
                "case \"$*\" in\n"
                "  *\"--project-name buzz-prod\"*\"ps -q relay\"*) echo relay-container ;;\n"
                "  *\"inspect --format {{.Image}} relay-container\") echo sha256:old ;;\n"
                "  *\"image inspect --format {{.Id}}|\"*) echo 'sha256:new|[\"ghcr.io/block/buzz@sha256:newdigest\"]|revision-new|2026-08-03T01:55:25Z|END' ;;\n"
                "  *\"--project-name buzz-prod\"*\"up -d --wait --no-deps --pull never relay\"*) exit 1 ;;\n"
                "esac\n"
            )
            result, _call_log, receipt = self._run_fake_updater(
                temp_root, docker_body
            )

            self.assertNotEqual(result.returncode, 0)
            state = receipt.read_text()
            self.assertIn("result=apply_failed", state)
            self.assertIn("latest_image_observed_this_attempt=true", state)
            self.assertIn("latest_image_id=sha256:new", state)
            self.assertIn("latest_image_digest=sha256:newdigest", state)
            self.assertIn("latest_image_revision=revision-new", state)
            self.assertIn(
                "latest_image_created_at=2026-08-03T01:55:25Z", state
            )

    def test_pull_timeout_is_recorded_as_timed_out(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            docker_body = (
                "case \"$*\" in\n"
                "  *\"--project-name buzz-prod\"*\"ps -q relay\"*) echo relay-container ;;\n"
                "  *\"inspect --format {{.Image}} relay-container\") echo sha256:old ;;\n"
                "  *\"image pull \"*) sleep 2 ;;\n"
                "esac\n"
            )
            result, _call_log, receipt = self._run_fake_updater(
                temp_root,
                docker_body,
                extra_environment={"BUZZ_CONTROL_EXECUTION_TIMEOUT_SECONDS": "1"},
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("result=timed_out", receipt.read_text())
            self.assertIn("safe execution window", result.stderr)

    def test_deadline_is_shared_across_all_docker_commands(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            fake_bin = temp_root / "bin"
            fake_bin.mkdir()
            date_count = temp_root / "date-count"
            fake_date = fake_bin / "date"
            fake_date.write_text(
                "#!/bin/sh\n"
                "if [ \"$*\" = \"+%s\" ]; then\n"
                f"  count=$(cat {date_count} 2>/dev/null || echo 0)\n"
                "  count=$((count + 1))\n"
                f"  printf '%s\\n' \"$count\" > {date_count}\n"
                "  printf '%s\\n' $((100 + count))\n"
                "else\n"
                "  exec /bin/date \"$@\"\n"
                "fi\n"
            )
            fake_date.chmod(0o755)
            fake_timeout = fake_bin / "timeout"
            fake_timeout.write_text(
                "#!/bin/sh\n"
                "shift\n"
                "exec \"$@\"\n"
            )
            fake_timeout.chmod(0o755)
            docker_body = (
                "case \"$*\" in\n"
                "  *\"--project-name buzz-prod\"*\"ps -q relay\"*) echo relay-container ;;\n"
                "  *\"inspect --format {{.Image}} relay-container\") echo sha256:new ;;\n"
                "  *\"inspect --format {{if .State.Health}}\"*) echo healthy ;;\n"
                "  *\"image inspect --format {{.Id}}|\"*) echo 'sha256:new|[\"ghcr.io/block/buzz@sha256:newdigest\"]|revision-new|2026-08-03T01:55:25Z|END' ;;\n"
                "esac\n"
            )
            result, call_log, receipt = self._run_fake_updater(
                temp_root,
                docker_body,
                extra_environment={
                    "BUZZ_CONTROL_EXECUTION_TIMEOUT_SECONDS": "3",
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("result=timed_out", receipt.read_text())
            self.assertNotIn("image pull", call_log.read_text())

    def test_health_inspection_failure_never_records_success(self):
        for health_action in ("exit 1", ":"):
            with self.subTest(health_action=health_action), tempfile.TemporaryDirectory() as td:
                temp_root = Path(td)
                docker_body = (
                    "case \"$*\" in\n"
                    "  *\"--project-name buzz-prod\"*\"ps -q relay\"*) echo relay-container ;;\n"
                    "  *\"inspect --format {{.Image}} relay-container\") echo sha256:new ;;\n"
                    f"  *\"inspect --format {{{{if .State.Health}}}}\"*) {health_action} ;;\n"
                    "  *\"image inspect --format {{.Id}}|\"*) echo 'sha256:new|[\"ghcr.io/block/buzz@sha256:newdigest\"]|revision-new|2026-08-03T01:55:25Z|END' ;;\n"
                    "esac\n"
                )
                result, _call_log, receipt = self._run_fake_updater(
                    temp_root, docker_body
                )

                self.assertNotEqual(result.returncode, 0)
                state = receipt.read_text()
                self.assertIn("result=verification_failed", state)
                self.assertIn("healthy=false", state)

    def test_updater_rejects_an_unsafe_environment_before_docker(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            env_file = temp_root / "prod.env"
            env_file.write_text("BUZZ_IMAGE=ghcr.io/block/buzz:main\n")
            env_file.chmod(0o644)
            fake_docker = temp_root / "docker"
            call_log = temp_root / "docker-calls.log"
            fake_docker.write_text(
                "#!/bin/sh\n" f"printf '%s\\n' \"$*\" >> {call_log}\n"
            )
            fake_docker.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "BUZZ_CONTROL_DOCKER_BIN": str(fake_docker),
                    "BUZZ_CONTROL_COMPOSE_BIN": str(fake_docker),
                    "BUZZ_CONTROL_ENV_FILE": str(env_file),
                    "HERMES_HOME": str(temp_root / "hermes"),
                }
            )

            result = subprocess.run(
                [str(UPDATER)],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mode 0600", result.stderr)
            self.assertFalse(call_log.exists())

    def test_scheduled_update_does_not_start_a_stopped_relay(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            env_file = temp_root / "prod.env"
            env_file.write_text("BUZZ_IMAGE=ghcr.io/block/buzz:main\n")
            env_file.chmod(0o600)
            fake_docker = temp_root / "docker"
            call_log = temp_root / "docker-calls.log"
            fake_docker.write_text(
                "#!/bin/sh\n" f"printf '%s\\n' \"$*\" >> {call_log}\n"
            )
            fake_docker.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "BUZZ_CONTROL_DOCKER_BIN": str(fake_docker),
                    "BUZZ_CONTROL_COMPOSE_BIN": str(fake_docker),
                    "BUZZ_CONTROL_ENV_FILE": str(env_file),
                    "HERMES_HOME": str(temp_root / "hermes"),
                }
            )

            result = subprocess.run(
                [str(UPDATER), "scheduled"],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stopped", result.stderr.lower())
            self.assertNotIn("image pull", call_log.read_text())

    def test_installer_repoints_a_verified_existing_plugin_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            legacy_plugin = temp_root / "legacy-plugin"
            legacy_dashboard = legacy_plugin / "dashboard"
            legacy_dashboard.mkdir(parents=True)
            (legacy_dashboard / "manifest.json").write_text(
                '{"name": "buzz-control"}\n'
            )

            hermes_home = temp_root / "hermes-home"
            plugins_dir = hermes_home / "plugins"
            plugins_dir.mkdir(parents=True)
            install_path = plugins_dir / "buzz-control"
            install_path.symlink_to(legacy_plugin)
            fake_bin, invocation_log = self._fake_hermes(temp_root)

            environment = os.environ.copy()
            environment["HOME"] = str(temp_root)
            environment["HERMES_HOME"] = str(hermes_home)
            environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"
            result = subprocess.run(
                [str(INSTALLER)],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(install_path.resolve(), PLUGIN_ROOT.resolve())
            self.assertIn("Updated Buzz Control", result.stdout)
            invocations = invocation_log.read_text()
            self.assertIn("plugins enable --no-allow-tool-override buzz-control", invocations)
            self.assertIn("cron create every 12h", invocations)
            self.assertIn("--script buzz-control-update.sh", invocations)
            self.assertNotIn(
                f"--script {hermes_home / 'scripts' / 'buzz-control-update.sh'}",
                invocations,
            )

    def test_installer_refuses_to_repoint_an_unrelated_plugin_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            unrelated_plugin = temp_root / "unrelated-plugin"
            unrelated_dashboard = unrelated_plugin / "dashboard"
            unrelated_dashboard.mkdir(parents=True)
            (unrelated_dashboard / "manifest.json").write_text(
                '{"name": "another-plugin"}\n'
            )

            hermes_home = temp_root / "hermes-home"
            plugins_dir = hermes_home / "plugins"
            plugins_dir.mkdir(parents=True)
            install_path = plugins_dir / "buzz-control"
            install_path.symlink_to(unrelated_plugin)

            environment = os.environ.copy()
            environment["HOME"] = str(temp_root)
            environment["HERMES_HOME"] = str(hermes_home)
            environment["PATH"] = "/usr/bin:/bin"
            result = subprocess.run(
                [str(INSTALLER)],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(install_path.resolve(), unrelated_plugin.resolve())
            self.assertIn("Refusing to replace existing path", result.stderr)

    def test_installer_enables_a_fresh_install(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            hermes_home = temp_root / "hermes-home"
            fake_bin, invocation_log = self._fake_hermes(temp_root)

            environment = os.environ.copy()
            environment["HOME"] = str(temp_root)
            environment["HERMES_HOME"] = str(hermes_home)
            environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"
            result = subprocess.run(
                [str(INSTALLER)],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            install_path = hermes_home / "plugins" / "buzz-control"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(install_path.resolve(), PLUGIN_ROOT.resolve())
            invocations = invocation_log.read_text()
            self.assertIn("plugins enable --no-allow-tool-override buzz-control", invocations)
            self.assertIn("cron create every 12h", invocations)
            self.assertIn("Enabled Buzz Control", result.stdout)
            wrapper = hermes_home / "scripts" / "buzz-control-update.sh"
            self.assertTrue(wrapper.is_file())
            self.assertFalse(wrapper.is_symlink())
            self.assertTrue(os.access(wrapper, os.X_OK))
            self.assertIn('$UPDATER" scheduled', wrapper.read_text())
            job_id_file = hermes_home / "state" / "buzz-control" / "cron-job-id"
            self.assertEqual(job_id_file.read_text().strip(), "buzz-job-id")

    def test_installer_preserves_an_existing_schedule_on_reinstall(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            hermes_home = temp_root / "hermes-home"
            fake_bin, invocation_log = self._fake_hermes(temp_root)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(temp_root),
                    "HERMES_HOME": str(hermes_home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                }
            )

            first = subprocess.run(
                [str(INSTALLER)], capture_output=True, check=False, env=environment, text=True
            )
            second = subprocess.run(
                [str(INSTALLER)], capture_output=True, check=False, env=environment, text=True
            )

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            invocations = invocation_log.read_text().splitlines()
            self.assertEqual(sum(line.startswith("cron create ") for line in invocations), 1)
            edit_lines = [line for line in invocations if line.startswith("cron edit ")]
            self.assertGreaterEqual(len(edit_lines), 2)
            self.assertTrue(all("--schedule" not in line for line in edit_lines))

    def test_installer_rejects_a_cron_timeout_below_twenty_minutes(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            hermes_home = temp_root / "hermes-home"
            fake_bin, invocation_log = self._fake_hermes(temp_root)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(temp_root),
                    "HERMES_HOME": str(hermes_home),
                    "HERMES_CRON_SCRIPT_TIMEOUT": "1199",
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                }
            )

            result = subprocess.run(
                [str(INSTALLER)], capture_output=True, check=False, env=environment, text=True
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("at least 1200 seconds", result.stderr)
            self.assertNotIn("cron create", invocation_log.read_text())

    @staticmethod
    def _fake_hermes(temp_root: Path) -> tuple[Path, Path]:
        fake_bin = temp_root / "bin"
        fake_bin.mkdir()
        invocation_log = temp_root / "hermes-invocation.log"
        fake_hermes = fake_bin / "hermes"
        fake_hermes.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {invocation_log}\n"
            f"job_state={temp_root / 'fake-cron-job'}\n"
            "case \"$*\" in\n"
            "  \"cron edit \"*)\n"
            "    if [ -f \"$job_state\" ]; then echo 'Updated job: buzz-job-id'; exit 0; fi\n"
            "    echo 'Job not found: buzz-control-image-update' >&2; exit 0 ;;\n"
            "  \"cron create \"*) touch \"$job_state\"; echo 'Created job: buzz-job-id' ;;\n"
            "  \"cron status\") echo 'Cron ticker: running' ;;\n"
            "esac\n"
        )
        fake_hermes.chmod(0o755)
        return fake_bin, invocation_log


if __name__ == "__main__":
    unittest.main()
