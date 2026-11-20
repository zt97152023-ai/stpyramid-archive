"""HDF5 structural preflight tests; links are rejected before dereferencing."""
import bz2
import importlib.util
from pathlib import Path
import tempfile
import unittest

import h5py
import numpy as np

MODULE_PATH = Path(__file__).resolve().parents[1] / "codec/stbaseline/integrity.py"
SPEC = importlib.util.spec_from_file_location("stpyramid_integrity_test", MODULE_PATH)
INTEGRITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INTEGRITY)
validate_hdf5_links = INTEGRITY.validate_hdf5_links


class Hdf5LinkSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def assert_rejected(self, path):
        with h5py.File(path, "r") as handle:
            with self.assertRaisesRegex(ValueError, "unsupported|aliases/cycles|External/virtual"):
                validate_hdf5_links(handle)

    def test_bz2_decompression_is_exact_and_bounded(self):
        payload = b"A" * 4096
        packed = bz2.compress(payload)
        self.assertEqual(INTEGRITY.bz2_decompress_exact(packed, len(payload), "test"), payload)
        with self.assertRaisesRegex(ValueError, "length/framing mismatch"):
            INTEGRITY.bz2_decompress_exact(packed, len(payload) - 1, "test")
        with self.assertRaisesRegex(ValueError, "Invalid bounded BZ2"):
            INTEGRITY.bz2_decompress_exact(packed, len(payload), "test", max_bytes=1024)
        with self.assertRaisesRegex(ValueError, "length/framing mismatch"):
            INTEGRITY.bz2_decompress_exact(packed + b"trailing", len(payload), "test")

    def test_external_link_is_rejected_without_opening_target(self):
        target = self.root / "external.h5"
        source = self.root / "source.h5"
        with h5py.File(target, "w") as handle:
            handle.create_dataset("payload", data=np.arange(4))
        with h5py.File(source, "w") as handle:
            handle["linked"] = h5py.ExternalLink(str(target), "/payload")
        self.assert_rejected(source)
        with h5py.File(target, "r") as handle:
            np.testing.assert_array_equal(handle["payload"][:], np.arange(4))

    def test_soft_link_is_rejected(self):
        source = self.root / "soft.h5"
        with h5py.File(source, "w") as handle:
            handle.create_dataset("payload", data=np.arange(4))
            handle["soft"] = h5py.SoftLink("/payload")
        self.assert_rejected(source)

    def test_hard_link_cycle_is_rejected(self):
        source = self.root / "cycle.h5"
        with h5py.File(source, "w") as handle:
            group = handle.create_group("group")
            group["self"] = group
        self.assert_rejected(source)

    def test_hard_link_alias_is_rejected(self):
        source = self.root / "alias.h5"
        with h5py.File(source, "w") as handle:
            group = handle.create_group("shared")
            handle["alias"] = group
        self.assert_rejected(source)

    def test_external_raw_storage_is_rejected(self):
        source = self.root / "external-storage.h5"
        raw = self.root / "raw.bin"
        raw.write_bytes(b"\\x00" * 16)
        with h5py.File(source, "w") as handle:
            handle.create_dataset("external", shape=(4,), dtype="i4",
                                  external=[(str(raw), 0, 16)])
        self.assert_rejected(source)


if __name__ == "__main__":
    unittest.main()
