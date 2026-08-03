from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


RUNTIME_TEST = Path(__file__).with_suffix(".js")


class DashboardRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for dashboard tests")
    def test_poll_refresh_and_update_request_ordering(self):
        result = subprocess.run(
            ["node", str(RUNTIME_TEST)],
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
