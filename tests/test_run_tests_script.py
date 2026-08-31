import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class RunTestsScriptTests(unittest.TestCase):
    def test_rejects_noop_application_as_python_runtime(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell, "PowerShell is required to test run-tests.ps1")
        powershell_path = Path(powershell).resolve()
        self.assertTrue(powershell_path.is_absolute())
        source_script = Path(__file__).resolve().parents[1] / "scripts" / "run-tests.ps1"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            tests = root / "tests"
            fake_bin = root / "fake-bin"
            scripts.mkdir()
            tests.mkdir()
            fake_bin.mkdir()
            (root / "src").mkdir()
            script = scripts / "run-tests.ps1"
            shutil.copy2(source_script, script)
            (tests / "test_smoke.py").write_text(
                "import unittest\n\n"
                "class SmokeTests(unittest.TestCase):\n"
                "    def test_smoke(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            noop_application = fake_bin / "python.cmd"
            noop_application.write_text("@exit /b 0\n", encoding="ascii")
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            result = subprocess.run(
                [powershell_path, "-NoProfile", "-NonInteractive", "-File", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                env=environment,
            )
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("Python 3.12 or newer runtime not found", output)


if __name__ == "__main__":
    unittest.main()
