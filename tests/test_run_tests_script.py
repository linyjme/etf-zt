import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


def _contains_private_windows_path(text: str) -> bool:
    normalized = text
    while "\\\\" in normalized:
        normalized = normalized.replace("\\\\", "\\")
    normalized = normalized.replace("\\/", "/")
    return re.search(
        r"[A-Za-z]:[\\/](?:Users|plan|dev)[\\/]",
        normalized,
        re.IGNORECASE,
    ) is not None


class RunTestsScriptTests(unittest.TestCase):
    def test_history_cleanup_audits_reachable_blob_contents(self) -> None:
        root = Path(__file__).resolve().parents[1]
        guide = (root / "docs" / "git-history-cleanup.md").read_text(encoding="utf-8")
        self.assertIn("Read-Host", guide)
        self.assertIn("$revisions = @(git rev-list --all)", guide)
        self.assertIn("if ($LASTEXITCODE -ne 0)", guide)
        self.assertIn("if ($revisions.Count -eq 0)", guide)
        self.assertLess(
            guide.index("if ($LASTEXITCODE -ne 0)"),
            guide.index("foreach ($revision in $revisions)"),
        )
        self.assertIn("git grep -n -E -- $sensitivePattern $revision", guide)
        self.assertNotIn("git grep -I", guide)
        self.assertNotIn("git rev-list --objects --all | Select-String", guide)
        for name_pattern in (
            "^var/", "quotes\\.jsonl?", "alerts\\.jsonl",
            "history(?:/.*)?", "__pycache__", "\\.pyc$",
        ):
            self.assertIn(name_pattern, guide)

    def test_history_name_audit_parses_object_lines_and_matches_pure_paths(self) -> None:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        self.assertIsNotNone(powershell, "PowerShell is required for history-audit test")
        root = Path(__file__).resolve().parents[1]
        guide = (root / "docs" / "git-history-cleanup.md").read_text(encoding="utf-8")
        start = guide.index("function Test-RuntimeHistoryObjectLine")
        end = guide.index("$objects = @(git rev-list --objects --all)", start)
        function_source = guide[start:end]
        oid = "a" * 40
        cases = {
            f"{oid} var/swing/trades.jsonl": True,
            f"{oid} data/monitor/quotes.jsonl": True,
            f"{oid} data/monitor/history/2026-08-28/quotes.jsonl": True,
            f"{oid} nested/__pycache__/module.pyc": True,
            f"{oid} nested/module.pyc": True,
            f"{oid} docs/path with spaces.md": False,
            oid: False,
        }
        with tempfile.TemporaryDirectory(prefix="history name audit ") as temporary:
            harness = Path(temporary) / "audit.ps1"
            assertions = "\n".join(
                "if ((Test-RuntimeHistoryObjectLine '"
                + line.replace("'", "''")
                + f"') -ne ${str(expected).lower()}) {{ throw 'unexpected audit result' }}"
                for line, expected in cases.items()
            )
            harness.write_text(function_source + "\n" + assertions + "\n", encoding="utf-8")
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", harness],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_private_path_scanner_rejects_raw_and_markdown_escaped_paths(self) -> None:
        raw = "C:" + "\\" + "Users" + "\\" + "somebody" + "\\" + "project"
        escaped = "F:" + "\\\\" + "plan" + "\\\\" + "money"
        forward = "D:" + "/" + "dev" + "/" + "checkout"
        self.assertTrue(_contains_private_windows_path(raw))
        self.assertTrue(_contains_private_windows_path(escaped))
        self.assertTrue(_contains_private_windows_path(forward))
        self.assertFalse(_contains_private_windows_path("<project-root>/src"))

    def test_tracked_text_does_not_expose_windows_user_or_workspace_paths(self) -> None:
        root = Path(__file__).resolve().parents[1]
        listed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        hits: list[str] = []
        for encoded_relative in listed.stdout.split(b"\0"):
            if not encoded_relative:
                continue
            relative = os.fsdecode(encoded_relative)
            path = root / relative
            try:
                payload = path.read_bytes()
                if b"\0" in payload:
                    continue
                text = payload.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if _contains_private_windows_path(line):
                    hits.append(f"{relative}:{number}")
        self.assertEqual(hits, [], "tracked private paths: " + ", ".join(hits))

    def test_pid_write_failure_stops_process_and_removes_partial_pid(self) -> None:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        self.assertIsNotNone(powershell, "PowerShell is required for launcher cleanup test")
        source = Path(__file__).resolve().parents[1] / "scripts" / "start-monitor.ps1"
        with tempfile.TemporaryDirectory(prefix="monitor pid cleanup ") as temporary:
            root = Path(temporary)
            harness = root / "pid-cleanup.ps1"
            harness.write_text(
                "param($SourceScript, $PowerShellExecutable, $RuntimeRoot)\n"
                "$tokens=$null; $errors=$null\n"
                "$ast=[System.Management.Automation.Language.Parser]::ParseFile($SourceScript,[ref]$tokens,[ref]$errors)\n"
                "$fn=$ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Write-MonitorPidSafely'},$true)\n"
                "if ($null -eq $fn) { throw 'PID safety helper missing' }\n"
                ". ([scriptblock]::Create($fn.Extent.Text))\n"
                "$child=Start-Process -FilePath $PowerShellExecutable -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 30' -WindowStyle Hidden -PassThru\n"
                "$pidPath=Join-Path $RuntimeRoot 'monitor.pid'\n"
                "$writer={param($Path,$Value) Set-Content -LiteralPath $Path -Value 'partial'; throw 'simulated pid write failure'}\n"
                "$message=''\n"
                "try { Write-MonitorPidSafely -Process $child -PidPath $pidPath -RuntimeRoot $RuntimeRoot -Writer $writer } catch { $message=$_.Exception.Message }\n"
                "$child.Refresh()\n"
                "if (-not $child.HasExited) { Stop-Process -Id $child.Id -Force; throw 'orphan process remained' }\n"
                "if (Test-Path -LiteralPath $pidPath) { throw 'partial PID remained' }\n"
                "if ($message -notmatch 'simulated pid write failure') { throw \"original error lost: $message\" }\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", harness,
                 source, powershell, root],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_start_monitor_passes_all_swing_paths_to_same_process(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "start-monitor.ps1").read_text(encoding="utf-8")
        for argument in (
            "--swing-watchlist",
            "--swing-strategy",
            "--swing-daily-history",
            "--swing-portfolio",
            "--swing-trades",
            "--swing-alerts",
            "--swing-backtests",
        ):
            self.assertIn(argument, script)
        self.assertIn("var\\swing", script)
        self.assertEqual(script.count("Start-Process"), 1)

    def test_all_swing_runtime_paths_are_git_ignored(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [
                "git",
                "check-ignore",
                "var/swing/daily_quotes.jsonl",
                "var/swing/portfolio.json",
                "var/swing/trades.jsonl",
                "var/swing/alerts.jsonl",
                "var/swing/backtests/result.json",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 5)

    def test_start_monitor_quotes_each_argument_for_windows_process_launch(self) -> None:
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        self.assertIsNotNone(powershell, "PowerShell is required to test argument quoting")
        source_script = Path(__file__).resolve().parents[1] / "scripts" / "start-monitor.ps1"
        expected = [
            r"C:\path with spaces\quotes.json",
            "plain",
            "tab\tvalue",
            "C:\\trailing slash\\",
            'quote"inside',
            "",
        ]

        with tempfile.TemporaryDirectory(prefix="monitor launch ") as temporary:
            root = Path(temporary)
            probe = root / "argv probe.py"
            output = root / "received arguments.json"
            harness = root / "quote harness.ps1"
            probe.write_text(
                "import json\n"
                "from pathlib import Path\n"
                "import sys\n\n"
                "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')\n",
                encoding="utf-8",
            )
            harness.write_text(
                "param($SourceScript, $PythonExecutable, $ProbeScript, $OutputPath)\n"
                "$tokens = $null\n"
                "$errors = $null\n"
                "$ast = [System.Management.Automation.Language.Parser]::ParseFile(\n"
                "    $SourceScript, [ref]$tokens, [ref]$errors\n"
                ")\n"
                "if ($errors.Count -gt 0) { throw $errors[0] }\n"
                "$functionAst = $ast.Find({\n"
                "    param($node)\n"
                "    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and\n"
                "        $node.Name -eq 'ConvertTo-WindowsCommandLineArgument'\n"
                "}, $true)\n"
                "if ($null -eq $functionAst) { throw 'Argument quoting helper not found' }\n"
                ". ([scriptblock]::Create($functionAst.Extent.Text))\n"
                "$rawArguments = @(\n"
                "    $ProbeScript, $OutputPath,\n"
                "    'C:\\path with spaces\\quotes.json', 'plain', \"tab`tvalue\",\n"
                "    'C:\\trailing slash\\', 'quote\"inside', ''\n"
                ")\n"
                "$quotedArguments = @($rawArguments | ForEach-Object {\n"
                "    ConvertTo-WindowsCommandLineArgument -Argument ([string]$_)\n"
                "})\n"
                "$process = Start-Process -FilePath $PythonExecutable `\n"
                "    -ArgumentList $quotedArguments -Wait -PassThru -WindowStyle Hidden\n"
                "exit $process.ExitCode\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    str(Path(powershell).resolve()),
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(harness),
                    str(source_script),
                    str(Path(sys.executable).resolve()),
                    str(probe),
                    str(output),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            diagnostic = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, diagnostic)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), expected)

    def test_start_monitor_uses_one_minute_refresh_interval(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "start-monitor.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("'--refresh-interval', '60'", script)
        self.assertNotIn("'--refresh-interval', '5'", script)

    def test_start_monitor_probes_compatible_runtime_before_writing_pid(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "start-monitor.ps1"
        ).read_text(encoding="utf-8")
        self.assertNotIn("-3.14", script)
        self.assertIn("sys.version_info < (3, 12)", script)
        self.assertIn("Python 3.12 or newer runtime not found", script)
        self.assertIn("$env:VIRTUAL_ENV", script)
        self.assertIn(".workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe", script)
        self.assertIn("[IO.Path]::IsPathRooted($candidate.Command)", script)
        self.assertIn("function Test-CompatiblePythonRuntime", script)
        self.assertIn("System.Diagnostics.ProcessStartInfo", script)
        self.assertIn("if ($process.HasExited)", script)
        self.assertLess(
            script.index("if ($process.HasExited)"),
            script.index("Write-MonitorPidSafely -Process $process"),
        )

    def test_start_monitor_ignores_stale_pid_and_rejects_foreign_listener(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "start-monitor.ps1"
        ).read_text(encoding="utf-8")
        stale_check = "if (Test-Path -LiteralPath $pidPath -PathType Leaf)"
        listener_check = "$listeners = @(Get-NetTCPConnection -LocalPort 8765"
        runtime_probe = "$probeMarker = \"__ETF_ROTATION_MONITOR_PYTHON_312_"
        self.assertIn(stale_check, script)
        self.assertIn("Ignoring stale monitor PID record", script)
        self.assertNotIn("Remove-Item -LiteralPath $pidPath", script)
        self.assertIn("Monitor service is already running", script)
        self.assertIn("Port 8765 is already in use by another process", script)
        self.assertLess(script.index(stale_check), script.index(runtime_probe))
        self.assertLess(script.index(listener_check), script.index(runtime_probe))

    def test_run_tests_prefers_virtual_and_workbuddy_python_candidates(self) -> None:
        script = (
            Path(__file__).resolve().parents[1] / "scripts" / "run-tests.ps1"
        ).read_text(encoding="utf-8")
        virtual_candidate = "Command = Join-Path $env:VIRTUAL_ENV 'Scripts\\python.exe'"
        workbuddy_candidate = "Command = Join-Path $HOME '.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe'"
        path_candidate = "[pscustomobject]@{ Command = 'python'; Arguments = @() }"
        self.assertIn(virtual_candidate, script)
        self.assertIn(workbuddy_candidate, script)
        self.assertIn("[IO.Path]::IsPathRooted($candidate.Command)", script)
        self.assertIn("function Test-CompatiblePythonRuntime", script)
        self.assertIn("System.Diagnostics.ProcessStartInfo", script)
        self.assertLess(script.index(virtual_candidate), script.index(workbuddy_candidate))
        self.assertLess(script.index(workbuddy_candidate), script.index(path_candidate))

    def test_runtime_probe_accepts_real_python_and_rejects_wrong_marker(self) -> None:
        import base64

        powershell = shutil.which("powershell") or shutil.which("pwsh")
        self.assertIsNotNone(powershell)
        root = Path(__file__).resolve().parents[1]
        for filename in ("run-tests.ps1", "start-monitor.ps1"):
            with self.subTest(script=filename):
                source = str(root / "scripts" / filename).replace("'", "''")
                executable = str(Path(sys.executable).resolve()).replace("'", "''")
                command = (
                    "$ErrorActionPreference='Stop'; $tokens=$null; $errors=$null; "
                    f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{source}',[ref]$tokens,[ref]$errors); "
                    "if ($errors.Count -gt 0) { throw 'parse error' }; "
                    "$fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Test-CompatiblePythonRuntime'},$true); "
                    ". ([scriptblock]::Create($fn.Extent.Text)); "
                    f"$exe='{executable}'; "
                    "$code=\"import sys; print('probe marker with spaces')\"; "
                    "if (-not (Test-CompatiblePythonRuntime -Command $exe -ProbeCode $code -ExpectedOutput 'probe marker with spaces')) { throw 'real runtime rejected' }; "
                    "if (Test-CompatiblePythonRuntime -Command $exe -ProbeCode $code -ExpectedOutput 'wrong marker') { throw 'wrong marker accepted' }; "
                    "if (Test-CompatiblePythonRuntime -Command $exe -ProbeCode 'import sys; sys.exit(1)' -ExpectedOutput 'anything') { throw 'failure accepted' }"
                )
                encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
                result = subprocess.run(
                    [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=20, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
            environment["HOME"] = str(root)
            environment["USERPROFILE"] = str(root)
            environment.pop("VIRTUAL_ENV", None)
            result = subprocess.run(
                [powershell_path, "-NoProfile", "-NonInteractive", "-File", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
                env=environment,
            )
            output = (result.stdout or "") + (result.stderr or "")
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("Python 3.12 or newer runtime not found", output)


if __name__ == "__main__":
    unittest.main()
