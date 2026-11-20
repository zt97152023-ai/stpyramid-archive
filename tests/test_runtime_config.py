"""Unit tests for bounded desktop worker configuration (standard library only)."""
from unittest import TestCase, main
from unittest.mock import patch
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime_config import (
    bounded_int_env,
    fused_worker_count,
    maximum_worker_count,
    scan_worker_count,
    encoding_timeout_seconds,
)


class RuntimeConfigTests(TestCase):
    def test_documented_defaults_are_preserved(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(fused_worker_count(), 8)
            self.assertEqual(maximum_worker_count(), 8)
            self.assertEqual(scan_worker_count("l1_conditional"), 8)
            self.assertEqual(scan_worker_count("direct"), 4)

    def test_existing_environment_overrides_are_preserved(self):
        values = {
            "STPYRAMID_FUSED_WORKERS": "3",
            "STPYRAMID_MAXIMUM_WORKERS": "2",
            "STPYRAMID_SCAN_WORKERS": "5",
        }
        with patch.dict("os.environ", values, clear=True):
            self.assertEqual(fused_worker_count(), 3)
            self.assertEqual(maximum_worker_count(), 2)
            self.assertEqual(scan_worker_count("direct"), 5)

    def test_empty_value_uses_default(self):
        with patch.dict("os.environ", {"WORKERS": ""}, clear=True):
            self.assertEqual(bounded_int_env("WORKERS", 6), 6)

    def test_non_integer_value_has_actionable_error(self):
        with patch.dict("os.environ", {"WORKERS": "many"}, clear=True):
            with self.assertRaisesRegex(ValueError, "WORKERS must be an integer"):
                bounded_int_env("WORKERS", 4)

    def test_encoding_timeout_default_and_override(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(encoding_timeout_seconds(), 14400)
        with patch.dict("os.environ", {"STPYRAMID_TIMEOUT_SECONDS": "3600"}, clear=True):
            self.assertEqual(encoding_timeout_seconds(), 3600)

    def test_timeout_outside_supported_range_is_rejected(self):
        for raw in ("0", "86401", "many"):
            with self.subTest(raw=raw), patch.dict("os.environ", {"STPYRAMID_TIMEOUT_SECONDS": raw}, clear=True):
                with self.assertRaisesRegex(ValueError, "STPYRAMID_TIMEOUT_SECONDS"):
                    encoding_timeout_seconds()

    def test_values_outside_supported_range_are_rejected(self):
        for raw in ("0", "9", "-1"):
            with self.subTest(raw=raw), patch.dict("os.environ", {"WORKERS": raw}, clear=True):
                with self.assertRaisesRegex(ValueError, "WORKERS must be an integer"):
                    bounded_int_env("WORKERS", 4)


if __name__ == "__main__":
    main()
