"""Generate synthetic inputs and exercise each CLI stage in its own process."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import h5py
import numpy as np
from scipy.sparse import csr_matrix
import tifffile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "codec"))
from stbaseline.integrity import h5_digest
from h5_types import capture, validate


def fixture(folder):
    folder.mkdir(parents=True)
    rng = np.random.default_rng(8)
    coarse = rng.integers(16, 240, size=(33, 34, 3), dtype=np.uint8)
    middle = coarse.repeat(2, axis=0).repeat(2, axis=1)
    middle[1::2, 1::2] += 1
    fine = middle.repeat(2, axis=0).repeat(2, axis=1)
    fine[::2, 1::2] += 1
    levels = [fine, middle, coarse]
    with tifffile.TiffWriter(folder / "pyramid.tif") as writer:
        for level, rgb in enumerate(levels):
            writer.write(rgb, tile=(256, 256), photometric="rgb", metadata=None,
                         subfiletype=int(bool(level)), description=f"synthetic level {level}")
    counts = csr_matrix(np.array([[0, 1, 256, 70000], [3, 0, 0, 4], [0, 8, 0, 0]], dtype=np.float32))
    with h5py.File(folder / "counts.h5ad", "x") as h:
        h.attrs["encoding-type"] = "anndata"
        strings = h5py.string_dtype("utf-8")
        for name, labels in (("obs", ["spot-1", "spot-2", "spot-3"]), ("var", ["g0", "g1", "g1", "g3"])):
            group = h.create_group(name)
            group.attrs["_index"] = "_index"
            group.create_dataset("_index", data=labels, dtype=strings)
        h.create_group("obsm").create_dataset("spatial", data=[[1.25, 3], [90, 18], [15, 50]])
        h.create_group("uns").create_dataset("signed", data=[-0.0, -2.5])
        matrix = h.create_group("X")
        matrix.attrs["encoding-type"] = "csr_matrix"
        matrix.attrs["shape"] = counts.shape
        for name in ("data", "indices", "indptr"):
            matrix.create_dataset(name, data=getattr(counts, name))
    (folder / "metadata.json").write_text('{"synthetic": true}\n', encoding="utf-8")
    return levels, counts.toarray()


def run(runtime, output):
    source, package, restored = (output / name for name in ("source", "archive", "restored"))
    levels, counts = fixture(source)

    def invoke(*args):
        argv = [sys.executable, "-B", str(ROOT / "stpyramid.py"), *map(str, args)]
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300)

    def command(*args):
        result = invoke(*args)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)

    encoded = command("encode", "--image", source / "pyramid.tif", "--counts", source / "counts.h5ad",
            "--metadata", source / "metadata.json", "--output", package, "--runtime-dir", runtime,
            "--timeout", 240)
    assert encoded["verification"] == {"state": "not_run"}
    assert encoded["native_levels"] == len(levels)
    assert encoded["ledger"]["total_package_bytes"] == sum(
        path.stat().st_size for path in package.rglob("*") if path.is_file()
    )
    assert "verify_counts_pixels_and_files" not in {stage["name"] for stage in encoded["stages"]}
    import hf_adapter
    from stbaseline.wsi_conditioned091 import NativeRuntime as Runtime091

    work = package.with_name(package.name + ".work")
    scan = json.loads((work / "census/scan.json").read_bytes())
    assert scan["diagnostic_seconds"] == 0
    fine, middle, _ = levels
    height, width, _ = fine.shape
    record091 = (work / "prior091/native/conditional/000000.bin").read_bytes()
    recordhf = (work / "hf/native/ms/000000.bin").read_bytes()
    np.testing.assert_array_equal(Runtime091(runtime / "091").decode(record091, width, height, 0, middle), fine)
    np.testing.assert_array_equal(hf_adapter.NativeRuntime(runtime / "hf").decode(recordhf, width, height, 0, middle), fine)
    routes = json.loads((package / "image/manifest.json").read_bytes())["routes"]
    assert sum(value for route, value in routes.items() if route != "1") > 0, routes
    assert json.loads((package / "manifest.json").read_bytes())["count"]["kind"] == "Shared_2"
    command("inspect", package)
    assert command("verify", package, "--runtime-dir", runtime)["ok"]
    command("restore", package, "--runtime-dir", runtime, "--output", restored)
    command("image", package, "--runtime-dir", runtime, "--level", 0, "--bbox", 1, 2, 14, 19,
            "--output", output / "image.npy")
    command("counts", package, "--rows", 0, "--genes", 0, 3, "--output", output / "count.npz")
    with tifffile.TiffFile(restored / "native_levels.tif") as image:
        assert len(image.pages) == len(levels)
        for index, (page, reference) in enumerate(zip(image.pages, levels)):
            np.testing.assert_array_equal(page.asarray(), reference)
            assert page.description == f"synthetic level {index}"
    assert h5_digest(restored / "expression.h5ad") == h5_digest(source / "counts.h5ad")
    validate(restored / "expression.h5ad", capture(source / "counts.h5ad"))
    assert (restored / "HEST_metadata.json").read_bytes() == (source / "metadata.json").read_bytes()
    np.testing.assert_array_equal(np.load(output / "image.npy"), levels[0][2:19, 1:14])
    with np.load(output / "count.npz", allow_pickle=False) as result:
        np.testing.assert_array_equal(result["counts"], counts[:1, [0, 3]])
    from container_compare import IntegratedArchive
    from joint_next import Reader

    # The upper X edge excludes the spot at x=15. L1 must cover the L0 box,
    # including the final partial pixel in X and Y.
    bbox = [0, 2, 15, 51]
    with IntegratedArchive(package) as reader:
        result = reader.region(bbox, level=1, genes=[3, 1, 2], modalities=("counts",))
        np.testing.assert_array_equal(result["counts"], counts[:1, [3, 1, 2]])
        np.testing.assert_array_equal(result["spot_ids"], ["spot-1"])
        np.testing.assert_array_equal(result["gene_positions"], [3, 1, 2])
        np.testing.assert_array_equal(result["level0_xy"], [[1.25, 3]])
        assert reader.metrics()["image"] is None
    runtimes = tuple(runtime / name for name in ("089", "091", "hf"))
    with IntegratedArchive(package, image_factory=lambda path: Reader(path, *runtimes)) as reader:
        result = reader.region(bbox, level=1, genes=[3, 1, 2])
        np.testing.assert_array_equal(result["image_bbox"], [0, 1, 8, 26])
        np.testing.assert_array_equal(result["image"], middle[1:26, :8])
        np.testing.assert_array_equal(result["counts"], counts[:1, [3, 1, 2]])
        initial = reader.metrics()["image"]
        repeated = reader.region(bbox, level=1, genes=[3, 1, 2])
        np.testing.assert_array_equal(repeated["image"], result["image"])
        after = reader.metrics()["image"]
        assert after["decoded_pixels"] == initial["decoded_pixels"]
        assert after["roi_cache_hits"] > initial["roi_cache_hits"]
        assert 0 < after["decoded_pixel_cache_bytes"] <= 8 * 1024**2
    corrupted = output / "corrupted"
    shutil.copytree(package, corrupted)
    with Reader(corrupted / "image", *runtimes) as reader:
        roi = [0, 0, 8, 8]
        np.testing.assert_array_equal(reader.image(0, roi), fine[:8, :8])
        manifest = corrupted / "image" / "manifest.json"
        current = manifest.stat()
        os.utime(manifest, ns=(current.st_atime_ns, current.st_mtime_ns + 2_000_000_000))
        try:
            reader.image(0, roi)
        except ValueError as exc:
            assert "reopen the reader" in str(exc)
        else:
            raise AssertionError("ROI cache returned pixels from a changed archive")
        assert reader.metrics()["decoded_pixel_cache_bytes"] == 0
    with (corrupted / "image" / "images.bank").open("r+b") as handle:
        value = handle.read(1)
        handle.seek(0)
        handle.write(bytes([value[0] ^ 1]))
    rejected = invoke("verify", corrupted, "--files-only")
    assert rejected.returncode != 0 and "mismatch" in rejected.stderr.lower(), rejected.stderr
    print(json.dumps({"status": "PASS", "levels": len(levels), "count_shape": list(counts.shape), "image_routes": routes,
                      "verified": ["full_rgb", "tiff_tags", "typed_h5ad", "metadata", "image_roi",
                                   "count_subset", "aligned_region", "roi_cache_reuse", "roi_cache_invalidation",
                                   "091_and_hf_native_decode", "altered_member_rejected"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.runtime_dir.resolve(), args.output.resolve())

