from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "dashboard"
INSTALLER = REPO_ROOT / "scripts" / "install.sh"


class PluginContractTests(unittest.TestCase):
    def test_runtime_manifest_is_an_inert_dashboard_allow_list_shim(self):
        manifest = (REPO_ROOT / "plugin.yaml").read_text()
        runtime = (REPO_ROOT / "__init__.py").read_text()

        self.assertIn("name: nesquena-webui-control", manifest)
        self.assertIn("hooks: []", manifest)
        self.assertIn("def register(_context)", runtime)

    def test_manifest_registers_a_dashboard_tab_and_backend(self):
        manifest = json.loads((DASHBOARD / "manifest.json").read_text())

        self.assertEqual(manifest["name"], "nesquena-webui-control")
        self.assertEqual(manifest["label"], "Nesquena WebUI")
        self.assertEqual(manifest["tab"]["path"], "/nesquena")
        self.assertEqual(manifest["tab"]["position"], "after:achievements")
        self.assertEqual(manifest["entry"], "dist/index.js")
        self.assertEqual(manifest["api"], "plugin_api.py")

    def test_bundle_uses_authenticated_sdk_and_exposes_all_actions(self):
        bundle = (DASHBOARD / "dist" / "index.js").read_text()

        self.assertIn("SDK.fetchJSON", bundle)
        self.assertNotIn("window.fetch(", bundle)
        self.assertIn('api("/status")', bundle)
        self.assertIn('api("/" + action, { method: "POST" })', bundle)
        for action in ("start", "stop", "restart"):
            self.assertIn(f'runAction("{action}")', bundle)
        self.assertIn(
            'registry.register("nesquena-webui-control"',
            bundle,
        )

    def test_styles_are_scoped_and_theme_aware(self):
        stylesheet = (DASHBOARD / "dist" / "style.css").read_text()

        self.assertIn(".nesquena-control", stylesheet)
        self.assertIn("var(--color-card)", stylesheet)
        self.assertIn("var(--color-border)", stylesheet)

    def test_installer_repoints_a_verified_existing_plugin_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            legacy_plugin = temp_root / "legacy-plugin"
            legacy_plugin.mkdir()
            (legacy_plugin / "plugin.yaml").write_text(
                "name: nesquena-webui-control\n"
            )

            hermes_home = temp_root / "hermes-home"
            plugins_dir = hermes_home / "plugins"
            plugins_dir.mkdir(parents=True)
            install_path = plugins_dir / "nesquena-webui-control"
            install_path.symlink_to(legacy_plugin)

            fake_bin = temp_root / "bin"
            fake_bin.mkdir()
            fake_hermes = fake_bin / "hermes"
            fake_hermes.write_text("#!/bin/sh\nexit 0\n")
            fake_hermes.chmod(0o755)

            environment = os.environ.copy()
            environment["HERMES_HOME"] = str(hermes_home)
            environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            result = subprocess.run(
                [str(INSTALLER)],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(install_path.resolve(), REPO_ROOT.resolve())
            self.assertIn("Updated NesQuena WebUI Control", result.stdout)

    def test_installer_refuses_to_repoint_an_unrelated_plugin_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            unrelated_plugin = temp_root / "unrelated-plugin"
            unrelated_plugin.mkdir()
            (unrelated_plugin / "plugin.yaml").write_text("name: another-plugin\n")

            hermes_home = temp_root / "hermes-home"
            plugins_dir = hermes_home / "plugins"
            plugins_dir.mkdir(parents=True)
            install_path = plugins_dir / "nesquena-webui-control"
            install_path.symlink_to(unrelated_plugin)

            environment = os.environ.copy()
            environment["HERMES_HOME"] = str(hermes_home)
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


if __name__ == "__main__":
    unittest.main()
