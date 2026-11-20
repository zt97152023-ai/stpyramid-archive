"""Verify a historical integrated archive against its original TIFF and H5AD.

This tool deliberately consumes user-supplied historical archives and their
original source files. No study data or binary archive fixtures are committed.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import tifffile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "codec"))
from h5_types import capture, validate
from stbaseline.integrity import h5_digest


def run(args):
    archive = args.archive.resolve(strict=True)
    image = args.original_tiff.resolve(strict=True)
    counts = args.original_h5ad.resolve(strict=True)
    runtime = args.runtime_dir.resolve(strict=True)
    output = args.output.absolute()
    if output.exists():
        raise FileExistsError(output)
    if any(output == path or output in path.parents or path in output.parents
           for path in (archive, image, counts, runtime)):
        raise ValueError("Compatibility restore output must be disjoint from all inputs")

    def cli(*command):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "stpyramid.py"), *map(str, command)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=args.timeout, check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"STPyramid {' '.join(map(str, command))} failed ({result.returncode}):\n"
                f"{result.stderr[-6000:]}"
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"STPyramid returned invalid JSON: {result.stdout[-2000:]}") from exc

    verification = cli("verify", archive, "--runtime-dir", runtime)
    if verification.get("ok") is not True:
        raise AssertionError("Historical archive verification did not return ok=true")

    restored = cli("restore", archive, "--runtime-dir", runtime, "--output", output)
    restored_root = Path(restored["restored"])
    restored_tiff = restored_root / "native_levels.tif"
    restored_h5ad = restored_root / "expression.h5ad"
    if not restored_tiff.is_file() or not restored_h5ad.is_file():
        raise AssertionError("Restore did not produce native_levels.tif and expression.h5ad")

    with tifffile.TiffFile(image) as source, tifffile.TiffFile(restored_tiff) as decoded:
        if len(source.pages) != len(decoded.pages):
            raise AssertionError(f"TIFF level count differs: {len(source.pages)} != {len(decoded.pages)}")
        levels = []
        for index, (source_page, decoded_page) in enumerate(zip(source.pages, decoded.pages)):
            expected, actual = source_page.asarray(), decoded_page.asarray()
            if expected.shape != actual.shape or expected.dtype != actual.dtype:
                raise AssertionError(f"TIFF level {index} shape/dtype differs")
            np.testing.assert_array_equal(actual, expected, err_msg=f"TIFF level {index} RGB mismatch")
            levels.append({"level": index, "shape": list(actual.shape), "dtype": str(actual.dtype),
                           "pixels": int(actual.shape[0] * actual.shape[1])})

    source_digest = h5_digest(counts)
    restored_digest = h5_digest(restored_h5ad)
    if source_digest != restored_digest:
        raise AssertionError("Restored H5AD semantic digest differs from the original")
    validate(restored_h5ad, capture(counts))

    if args.metadata is not None:
        metadata = args.metadata.resolve(strict=True)
        restored_metadata = restored_root / "HEST_metadata.json"
        if not restored_metadata.is_file() or restored_metadata.read_bytes() != metadata.read_bytes():
            raise AssertionError("Restored metadata attachment differs from the original")

    manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    report = {
        "status": "PASS",
        "archive": str(archive),
        "count_kind": manifest.get("count", {}).get("kind"),
        "archive_bytes": verification.get("ledger", {}).get("total_package_bytes"),
        "tiff_levels": levels,
        "h5ad_semantic_sha256": source_digest,
        "exact_checks": ["full_archive_verify", "native_tiff_pixels", "typed_h5ad_semantics"],
        "metadata_attachment_checked": args.metadata is not None,
        "restored_to": str(restored_root),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--original-tiff", type=Path, required=True)
    parser.add_argument("--original-h5ad", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--timeout", type=int, default=86400)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 86400:
        parser.error("--timeout must be between 1 and 86400 seconds")
    run(args)
