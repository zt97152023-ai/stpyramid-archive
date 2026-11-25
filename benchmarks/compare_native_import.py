"""Compare Fast fusion with independent lossless JPEG XL effort 3."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
from statistics import median
import sys
import tempfile
import time


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--fast-encoder", type=Path, required=True,
                        help="091 executable supporting encode_recursive_fast")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--counts", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cpus", type=int, nargs="+", required=True)
    parser.add_argument("--arm", choices=("independent_jxl_e3", "fast_fusion"), required=True,
                        help="Use a fresh process and output directory for each arm")
    args = parser.parse_args()
    app, output, native = (p.resolve() for p in (args.app, args.output, args.fast_encoder))
    source = {k: getattr(args, k).resolve() for k in ("image", "counts", "metadata")}
    output.mkdir(parents=False, exist_ok=False)
    tmp = output / "temp"
    tmp.mkdir()
    tempfile.tempdir = str(tmp)
    os.environ.update(TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp))
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "NUMBA_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
                "BLIS_NUM_THREADS"):
        os.environ[key] = "1"
    os.environ.pop("ST074_S", None)
    os.environ.pop("ST074_T", None)
    sys.path[:0] = [str(app / "codec"), str(app)]
    import imagecodecs
    import numpy as np
    import psutil
    import pyramid_image_archive as fusion
    from container_compare import IntegratedArchive
    from h5_types import capture, validate
    from integrated_encode import encode_archive, runtime_dirs
    from joint_next import Reader
    from stbaseline.integrity import h5_digest

    me = psutil.Process()
    workers = len(args.cpus)
    assert workers == len(set(args.cpus)) and set(args.cpus) <= set(me.cpu_affinity())
    rt_root = app / "runtime-windows-source"
    runtimes = runtime_dirs(rt_root)
    identities = {k: {"bytes": v.stat().st_size, "sha256": digest(v)} for k, v in source.items()}
    semantic = h5_digest(source["counts"])
    scientific_types = capture(source["counts"])
    original_fusion = fusion.encode_native_pyramid
    native_runner = fusion.run_native_encoder

    # Move the final pixel check outside both encode timings. Both modes
    # still use the same archive reader for the independent full validation.
    def direct(rgb, *, effort=9):
        blob = imagecodecs.jpegxl_encode(np.ascontiguousarray(rgb), lossless=True, distance=0,
                                        effort=3, numthreads=1)
        if not 0 < len(blob) <= fusion.base.MAX_PAYLOAD:
            raise ValueError("JPEG XL record exceeds the archive payload bounds")
        return blob

    def fast_conditional(exe, mode, spool, target, timeout):
        assert mode == "encode_direct"
        return native_runner(native, "encode_recursive_fast", spool, target, timeout=timeout)

    fusion.base._encode = direct
    fusion.run_native_encoder = fast_conditional
    results = []
    arm = args.arm
    me.cpu_affinity(args.cpus)
    package = output / f"{arm}.stpyramid"
    work = output / f"{arm}_work"
    events = []

    def encode_image(image, archive, scratch, **kwargs):
        method = "direct9" if arm == "independent_jxl_e3" else "fused_l0_l1"
        return original_fusion(image, archive, scratch, method=method, **kwargs)

    def event(info):
        events.append(info)
        print(json.dumps({**info, "arm": arm}), flush=True)

    fusion.encode_native_pyramid = encode_image
    started = time.perf_counter()
    result = encode_archive(source["image"], source["counts"], source["metadata"], rt_root,
                            package, work=work, timeout=21600, on_stage=event,
                            image_mode="fused", fused_workers=workers)
    wall = time.perf_counter() - started
    assert result["verification"] == {"state": "not_run"}
    assert any(e.get("native_workers") == workers for e in events)
    manifest = json.loads((package / "manifest.json").read_bytes())
    assert manifest["count"]["kind"] == "Shared_2"
    assert manifest["image"]["profile"] == ("direct" if arm == "independent_jxl_e3" else "l1_conditional")
    assert digest(package / "count/HEST_metadata.json") == identities["metadata"]["sha256"]
    factory = lambda p: Reader(p, *runtimes, scan_workers=min(workers, 8), roi_cache=False)
    started = time.perf_counter()
    with IntegratedArchive(package, image_factory=factory) as reader:
        verified = reader.verify()
    independent = time.perf_counter() - started
    assert verified["ok"] and verified["h5ad_semantics_exact"]
    levels = [v["pixel_sha256"] for v in verified["image"]["levels"]]
    with IntegratedArchive(package, image_factory=factory) as reader:
        h5ad = tmp / f"{arm}.h5ad"
        reader.restore_expression(h5ad)
        assert h5_digest(h5ad) == semantic
        validate(h5ad, scientific_types)
    h, w, _ = manifest["image"]["levels"][0]["shape"]
    rng = random.Random(20261125)
    boxes = [(x, y, x + 256, y + 256)
             for x, y in ((rng.randrange(w - 255), rng.randrange(h - 255)) for _ in range(25))]
    with IntegratedArchive(package, image_factory=factory) as reader:
        reader.image(0, boxes[0])
        pixels, times = [], []
        for box in boxes[1:]:
            started = time.perf_counter()
            data = reader.image(0, box)
            times.append(time.perf_counter() - started)
            pixels.append(hashlib.sha256(data.tobytes()).hexdigest())
    ledger = verified["ledger"]
    outcome = {
        "arm": arm, "import_seconds": wall, "independent_verify_seconds": independent,
        "package_bytes": ledger["total_package_bytes"],
        "image_component_bytes": sum(v for k, v in ledger["members"].items() if k.startswith("image/")),
        "image_roi_samples_seconds": times, "image_roi_median_seconds": median(times),
        "image_roi_sha256": pixels, "native_level_sha256": levels,
        "native_levels_exact": len(levels), "typed_h5ad_exact": True, "json_exact": True,
        "count_member_hashes": {name: info for name, info in manifest["files"].items() if name.startswith("count/")},
        "stages": result["stages"], "events": events,
    }
    results.append(outcome)
    print(json.dumps({k: v for k, v in outcome.items() if k not in {"count_member_hashes", "events"}}), flush=True)
    evidence = {
        "state": "PASS", "workers": workers, "cpu_affinity": args.cpus,
        "verification_scan_workers": min(workers, 8),
        "input_identities": identities, "fast_encoder_sha256": digest(native),
        "roi_scope": "24 fixed distinct L0 windows, reader open and warmed, decoded tile cache disabled; OS cache uncontrolled",
        "verification_scope": "Full member and native pixel verification plus H5AD content; typed H5AD check repeated outside timing",
        "imagecodecs": imagecodecs.version(), "arms": results,
    }
    (output / "RESULT.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
