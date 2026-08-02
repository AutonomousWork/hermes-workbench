from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "dashboard"


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


if __name__ == "__main__":
    unittest.main()
