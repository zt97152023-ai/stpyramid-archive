"""Unit tests for the desktop task request protocol (standard library only)."""
from unittest import TestCase, main

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

    def test_keep_work_must_be_boolean(self):
        with self.assertRaisesRegex(ValueError, "keep_work"):
            validate_task({"action": "encode", "mode": "image", "image": "slide.tif",
                           "output": "out.stpyramid", "keep_work": "false"})


if __name__ == "__main__":
    main()
