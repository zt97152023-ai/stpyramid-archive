"""P0 timing and verification worker extracted from the original run.py (see hashes)."""
import argparse
from datetime import datetime, timedelta, timezone
from functools import partial
from hashlib import file_digest
import json
import os
from pathlib import Path
import sys
import tempfile
import time

def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def write(path, data, *, update=False):
    target = path.with_suffix(".tmp") if update else path
    with target.open("w" if update else "x", encoding="utf8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if update:
        os.replace(target, path)


def emit(event):
    print(json.dumps({"timestamp": now(), **event}, ensure_ascii=True), flush=True)


def enter_worker(root, cfg, case):
    import psutil

    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                "NUMBA_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS"):
        os.environ[key] = "1"
    for key in ("ST074_S", "ST074_T"):
        os.environ.pop(key, None)
    scratch = case / "temp"
    scratch.mkdir(exist_ok=True)
    tempfile.tempdir = str(scratch)
    os.environ.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch),
                      NUMBA_CACHE_DIR=str(root / "numba"))
    app = root / "app"
    sys.path[:0] = [str(app / "codec"), str(app)]
    process = psutil.Process()
    assert set(cfg["affinity"]) <= set(process.cpu_affinity())
    process.cpu_affinity(cfg["affinity"])
    return app, process


def encode(root, cfg, case, sample, arm):
    app, process = enter_worker(root, cfg, case)
    import pyramid_image_archive as fusion
    from fetch_count_runtime import count_source_ready
    from integrated_encode import encode_archive

    assert count_source_ready()
    inputs = cfg["samples"][sample]["inputs"]
    target = case / "package.stpyramid"
    work = case / "work"
    if arm == "direct_jxl_e3":
        fusion.encode_native_pyramid = partial(fusion.encode_native_pyramid, method="direct3")
    events = []

    def stage(event):
        events.append(event)
        emit(event)

    started = time.perf_counter()
    receipt = encode_archive(Path(inputs["image"]["path"]), Path(inputs["count"]["path"]),
                             Path(inputs["metadata"]["path"]), app / "runtime-windows-source",
                             target, work=work, on_stage=stage, image_mode="fused",
                             fused_workers=cfg["encode_workers"], timeout=21600)
    seconds = time.perf_counter() - started
    assert receipt["ok"] and receipt["verification"] == {"state": "not_run"}
    assert set(process.cpu_affinity()) == set(cfg["affinity"])
    allocation = [event for event in events if "native_workers" in event]
    assert len(allocation) == 1 and allocation[0]["native_workers"] == cfg["encode_workers"]
    assert (work / "fused_work/plans_L0").exists() == (arm == "regular_091")
    assert not list((work / "fused_work").glob("spool_L*"))
    manifest = read(target / "manifest.json")
    image = read(target / "image/manifest.json")
    assert manifest["count"]["kind"] == "Shared_2"
    assert manifest["image"]["profile"] == ("direct" if arm == "direct_jxl_e3" else "l1_conditional")
    if arm == "direct_jxl_e3":
        assert image["routes"]["1"] > 0 and all(not v for k, v in image["routes"].items() if k != "1")
    for src, tag in (("image", "wsi"), ("count", "h5ad")):
        assert manifest["sources"][tag] == {k: inputs[src][k] for k in ("bytes", "sha256")}
    assert sha(target / "count/HEST_metadata.json") == inputs["metadata"]["sha256"]
    assert manifest["count"]["expression"]["semantic_sha256"] == cfg["samples"][sample]["source_semantic_sha256"]
    write(case / "IMPORT.json", {"sample": sample, "arm": arm, "publish_wall_s": seconds,
                                "encode": receipt, "events": events, "routes": image["routes"]})
    emit({"state": "PUBLISHED", "publish_wall_s": seconds,
          "package_bytes": receipt["ledger"]["total_package_bytes"]})


def verify(root, cfg, case, sample, arm):
    app, process = enter_worker(root, cfg, case)
    from container_compare import IntegratedArchive
    from h5_types import capture, validate
    from integrated_encode import runtime_dirs
    from joint_next import Reader

    kind = cfg["samples"][sample]
    imported = read(case / "IMPORT.json")
    target = case / "package.stpyramid"
    runtimes = runtime_dirs(app / "runtime-windows-source")
    factory = lambda path: Reader(path, *runtimes, scan_workers=cfg["readback_workers"], roi_cache=False)
    started = time.perf_counter()
    with IntegratedArchive(target, image_factory=factory) as reader:
        verified = reader.verify()
    seconds = time.perf_counter() - started
    assert verified["ok"] and verified["h5ad_semantics_exact"]
    pixels = [level["pixel_sha256"] for level in verified["image"]["levels"]]
    assert pixels == kind["native_sha256"]
    started = time.perf_counter()
    with IntegratedArchive(target, image_factory=factory) as reader:
        restored = case / "restore_types.h5ad"
        reader.restore_expression(restored)
        validate(restored, capture(Path(kind["inputs"]["count"]["path"])))
    typing_s = time.perf_counter() - started
    manifest = read(target / "manifest.json")
    assert set(process.cpu_affinity()) == set(cfg["affinity"])
    assert verified["ledger"]["total_package_bytes"] == imported["encode"]["ledger"]["total_package_bytes"]
    result = {"state": "PASS", "sample": sample, "arm": arm, "archive": str(target),
              "development": kind["development"], "development_exposure": kind["development_exposure"],
              "independence": kind["independence"], "publish_wall_s": imported["publish_wall_s"],
              "independent_readback_wall_s": seconds, "extra_h5ad_type_check_wall_s": typing_s,
              "package_bytes": verified["ledger"]["total_package_bytes"],
              "image_bytes": verified["ledger"]["categories"]["image"], "native_sha256": pixels,
              "typed_h5ad_exact": True, "original_json_exact": True, "all_members_verified": True,
              "count_members": {k: v for k, v in manifest["files"].items() if k.startswith("count/")},
              "manifest_sha256": sha(target / "manifest.json"), "verified_at": now()}
    write(case / "RESULT.json", result)
    emit({k: result[k] for k in ("state", "package_bytes", "publish_wall_s", "independent_readback_wall_s")})

if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("root", type=Path)
    cli.add_argument("case", type=Path)
    cli.add_argument("sample")
    cli.add_argument("arm", choices=("regular_091", "direct_jxl_e3"))
    cli.add_argument("step", choices=("encode", "verify"))
    args = cli.parse_args()
    root = args.root.resolve()
    case = args.case.resolve()
    cfg = read(root / "CONFIG.json")
    if args.sample not in cfg["samples"] or not case.is_relative_to(root):
        raise ValueError("Unexpected sample or case directory")
    (encode if args.step == "encode" else verify)(root, cfg, case, args.sample, args.arm)
