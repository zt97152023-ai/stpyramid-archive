"""Unit tests for bounded desktop worker configuration (standard library only)."""
from unittest import TestCase, main
from unittest.mock import patch

from runtime_config import (
    bounded_int_env,
    fused_worker_count,
    maximum_worker_count,
    scan_worker_count,
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

    def test_values_outside_supported_range_are_rejected(self):
        for raw in ("0", "9", "-1"):
            with self.subTest(raw=raw), patch.dict("os.environ", {"WORKERS": raw}, clear=True):
                with self.assertRaisesRegex(ValueError, "WORKERS must be an integer"):
                    bounded_int_env("WORKERS", 4)


if __name__ == "__main__":
    main()
