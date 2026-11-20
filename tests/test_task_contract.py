"""Unit tests for the desktop task request protocol (standard library only)."""
from unittest import TestCase, main
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from task_contract import validate_task


class TaskContractTests(TestCase):
    def test_python_desktop_integrated_request_with_empty_optional_metadata(self):
        request = {
            "action": "encode", "mode": "integrated", "image": "slide.tif",
            "counts": "spots.h5ad", "metadata": "", "output": "out.stpyramid",
            "keep_work": False, "language": "zh",
        }
        self.assertIs(validate_task(request), request)

    def test_java_image_only_request(self):
        request = {"action": "encode", "mode": "image", "image": "slide.tif", "output": "out.stpyramid"}
        self.assertIs(validate_task(request), request)

    def test_java_integrated_fused_request(self):
        request = {
            "action": "encode", "mode": "integrated-fused", "image": "slide.tif",
            "counts": "spots.h5ad", "metadata": "meta.json", "output": "out.stpyramid",
        }
        self.assertIs(validate_task(request), request)

    def test_verify_request_allows_empty_optional_runtime_and_output(self):
        request = {"action": "verify", "archive": "archive.stpyramid", "output": "", "runtime": ""}
        self.assertIs(validate_task(request), request)

    def test_restore_requires_output(self):
        with self.assertRaisesRegex(ValueError, "output"):
            validate_task({"action": "restore", "archive": "archive.stpyramid", "output": ""})

    def test_encode_requires_counts_for_integrated_mode(self):
        with self.assertRaisesRegex(ValueError, "counts"):
            validate_task({"action": "encode", "mode": "integrated", "image": "slide.tif",
                           "output": "out.stpyramid"})

    def test_rejects_unknown_action_mode_and_language(self):
        invalid = (
            {"action": "delete"},
            {"action": "encode", "mode": "recursive", "image": "slide.tif", "output": "out"},
            {"action": "verify", "archive": "a", "language": "fr"},
        )
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                validate_task(request)

    def test_rejects_non_object_and_non_string_action(self):
        for request in (None, [], {"action": []}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                validate_task(request)

    def test_worker_rejects_invalid_request_before_codec_loading(self):
        worker = ROOT / "desktop_worker.py"
        result = subprocess.run(
            [sys.executable, "-B", str(worker)], cwd=ROOT, input='{"action":"unknown"}\n',
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Task action must be one of", result.stdout)
        self.assertNotIn("numpy", result.stderr.lower())

    def test_worker_source_compiles(self):
        source = (ROOT / "desktop_worker.py").read_text(encoding="utf-8")
        compile(source, str(ROOT / "desktop_worker.py"), "exec")

    def test_keep_work_must_be_boolean(self):
        with self.assertRaisesRegex(ValueError, "keep_work"):
            validate_task({"action": "encode", "mode": "image", "image": "slide.tif",
                           "output": "out.stpyramid", "keep_work": "false"})


if __name__ == "__main__":
    main()
