"""Regression tests for the pinned Count dependency trust boundary."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fetch_count_runtime as runtime


class PinnedRuntimeSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vendor = Path(self.temp.name) / "vendor"
        self.vendor.mkdir()
        self.rows = []
        for index in range(runtime.EXPECTED_FILES):
            relative = f"baseline/test_{index}.py"
            payload = f"trusted source {index}\n".encode()
            path = self.vendor / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            self.rows.append({
                "path": relative,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
        self.write_manifest()
        self.patcher = patch.object(runtime, "VENDOR", self.vendor)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def write_manifest(self):
        (self.vendor / "SOURCE.json").write_text(json.dumps({
            "origin": runtime.ORIGIN.rstrip("/"),
            "commit": runtime.COMMIT,
            "files": self.rows,
        }), encoding="utf-8")

    def test_valid_cached_files_are_ready(self):
        self.assertTrue(runtime.count_source_ready())

    def test_cached_file_is_rehashed_not_trusted_by_existence(self):
        (self.vendor / self.rows[0]["path"]).write_bytes(b"tampered")
        self.assertFalse(runtime.count_source_ready())
        with self.assertRaisesRegex(ValueError, "differs from its pin"):
            runtime.fetch(self.rows[0])

    def test_parent_traversal_is_rejected(self):
        self.rows[0]["path"] = "baseline/../outside.py"
        self.write_manifest()
        self.assertFalse(runtime.count_source_ready())
        with self.assertRaisesRegex(ValueError, "Unexpected Count source member"):
            runtime.pinned_files()

    def test_duplicate_pin_is_rejected(self):
        self.rows[1] = dict(self.rows[0])
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "Duplicate Count source pin"):
            runtime.pinned_files()

    def test_symlinked_cached_file_is_rejected(self):
        target = self.vendor / self.rows[0]["path"]
        actual = target.with_suffix(".actual")
        target.rename(actual)
        try:
            target.symlink_to(actual)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable on this platform")
        self.assertFalse(runtime.count_source_ready())
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            runtime.fetch(self.rows[0])

    def test_untrusted_download_redirect_is_rejected(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def geturl(self): return "https://attacker.invalid/payload"
            def read(self, _limit): return b"payload"
        with patch.object(runtime, "urlopen", return_value=Response()):
            with self.assertRaisesRegex(ValueError, "untrusted host"):
                runtime.download("https://raw.githubusercontent.com/example", 100)


if __name__ == "__main__":
    unittest.main()
