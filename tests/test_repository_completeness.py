"""Regression checks for a clean checkout's runtime source closure."""

from __future__ import annotations

import importlib
import subprocess
import unittest
from pathlib import Path


REQUIRED_SOURCE = (
    "src/etf_rotation/holdings_snapshot.py",
    "src/etf_rotation/swing_minutes.py",
    "src/etf_rotation/pr_page.py",
)


class RepositoryCompletenessTests(unittest.TestCase):
    def test_runtime_source_modules_are_tracked(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", *REQUIRED_SOURCE],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "required runtime source is not tracked; missing paths: "
                + ", ".join(REQUIRED_SOURCE)
            ),
        )

    def test_service_and_web_entrypoints_import(self) -> None:
        imported = []
        for module_name in ("etf_rotation.swing_service", "etf_rotation.t_web"):
            try:
                importlib.import_module(module_name)
            except ModuleNotFoundError as exc:
                self.fail(f"{module_name} has an incomplete source dependency: {exc}")
            imported.append(module_name)
        self.assertEqual(imported, ["etf_rotation.swing_service", "etf_rotation.t_web"])


if __name__ == "__main__":
    unittest.main()
