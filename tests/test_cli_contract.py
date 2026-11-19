"""Fast CLI contract tests; these must not require codec or native runtimes."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "stpyramid.py"
COMMANDS = ("encode", "inspect", "verify", "restore", "image", "counts")


class CliContractTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-B", str(CLI), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    def test_root_help_lists_all_commands_without_loading_codec(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in COMMANDS:
            self.assertIn(command, result.stdout)

    def test_each_command_exposes_help_without_runtime_dependencies(self):
        for command in COMMANDS:
            with self.subTest(command=command):
                result = self.run_cli(command, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout.lower())

    def test_cli_import_does_not_load_scientific_or_native_dependencies(self):
        code = (
            "import sys; import stpyramid; "
            "blocked = {'numpy', 'scipy', 'h5py', 'tifffile', 'imagecodecs', 'psutil'}; "
            "loaded = blocked.intersection(sys.modules); "
            "assert not loaded, sorted(loaded)"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
