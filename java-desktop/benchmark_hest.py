"""Benchmark the Windows Java HEST entry point against a fixed, hashed cohort."""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import Counter
import csv
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from uuid import uuid4

import numpy as np
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "codec"))
from h5_types import capture, validate
from stbaseline.integrity import h5_digest, sha256

GIB = 1024 ** 3


def read(path):
    return json.loads(path.read_bytes())


def publish(path, value):
    staged = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    staged.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    replace(staged, path)


def replace(source, dest):
    for iteration in range(30):
        try:
            os.replace(source, dest)
            return
        except PermissionError:
            if iteration == 29:
                raise
            time.sleep(.1)


def check_inputs(row):
    files = {}
    for role in ("image", "count", "metadata"):
        expected = row["sources"][role]
        path = Path(expected["path"])
        if path.stat().st_size != expected["bytes"] or sha256(path) != expected["sha256"]:
            raise ValueError(f"Source differs from frozen cohort: {path}")
        files[role] = path
    return files


def stage_seconds(logfile):
    events = {}
    for line in logfile.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("{"):
            e = json.loads(line)
            if "end" in e:
                events[e["end"]] = e["seconds"]
    if "verify_counts_pixels_and_files" not in events:
        raise ValueError("Missing the native encoding/validation stage timings")
    return events


def ledger(archive):
    manifest_path = archive / "manifest.json"
    manifest = read(manifest_path)
    expected = manifest["files"]
    paths = {p.relative_to(archive).as_posix(): p for p in archive.rglob("*") if p.is_file()}
    if set(paths) != expected.keys() | {"manifest.json"}:
        raise ValueError("Archive file set differs from manifest")
    sizes = {}
    for n, p in paths.items():
        sizes[n] = p.stat().st_size
        if n in expected and (sizes[n] != expected[n]["bytes"] or sha256(p) != expected[n]["sha256"]):
            raise ValueError(f"Archive member hash or length mismatch: {n}")
    return manifest, sizes


def pixel_sha(page, buffer_file):
    pixels = page.asarray(out=str(buffer_file), maxworkers=1)
    try:
        if len(pixels.shape) != 3 or pixels.shape[-1] != 3 or pixels.dtype != np.uint8:
            raise ValueError("Expected a native RGB8 level")
        sha = hashlib.sha256()
        for i in range(0, pixels.shape[0], 64):
            sha.update(np.ascontiguousarray(pixels[i:i + 64]).tobytes())
        return list(pixels.shape), sha.hexdigest()
    finally:
        if isinstance(pixels, np.memmap):
            pixels._mmap.close()
        del pixels
        buffer_file.unlink(missing_ok=True)


def validate_recovery(files, archive, restored, scratch):
    manifest, sizes = ledger(archive)
    if (restored / "INCOMPLETE").exists():
        raise ValueError("Restored output is incomplete")
    digest = h5_digest(files["count"])
    if digest != h5_digest(restored / "expression.h5ad") or digest != manifest["count"]["expression"]["semantic_sha256"]:
        raise ValueError("H5AD scientific content differs after restoration")
    validate(restored / "expression.h5ad", capture(files["count"]))
    original_json = files["metadata"].read_bytes()
    if original_json != (restored / "HEST_metadata.json").read_bytes():
        raise ValueError("Original HEST JSON differs after restoration")
    image_meta = (archive / "image/source.json").read_bytes()
    if (restored / "source.json").read_bytes() != image_meta or read(archive / "image/source.json")["whole_file_sha256"] != sha256(files["image"]):
        raise ValueError("Source image metadata or input identity differs")
    levels = []
    with tempfile.TemporaryDirectory(prefix="pixel-check-", dir=scratch) as cache:
        buffer = Path(cache) / "rgb.bin"
        with tifffile.TiffFile(files["image"]) as src, tifffile.TiffFile(restored / "native_levels.tif") as dst:
            original_levels = manifest["image"]["levels"]
            if len(src.pages) != len(dst.pages) or len(src.pages) != len(original_levels):
                raise ValueError("Native TIFF level count changed")
            for i, (p, q, declared) in enumerate(zip(src.pages, dst.pages, original_levels)):
                shape, digest_pixels = pixel_sha(p, buffer)
                if (shape, digest_pixels) != pixel_sha(q, buffer) or digest_pixels != declared["pixel_sha256"] or shape != declared["shape"]:
                    raise ValueError(f"Original, archived, or restored native TIFF level {i} differs")
                levels.append({"level": i, "shape": shape, "sha256": digest_pixels})
    return {"archive_bytes": sum(sizes.values()), "members": sizes, "package": str(archive),
            "manifest_sha256": sha256(archive / "manifest.json"), "h5ad_exact": True,
            "h5ad_semantic_sha256": digest, "h5ad_type_oracle_passed": True,
            "source_attachment_exact": True, "native_tiff_metadata_exact": True, "levels": levels}


def java_call(args, log, *command):
    env = os.environ.copy()
    env.update(TEMP=str(log.parent), TMP=str(log.parent), TMPDIR=str(log.parent),
               NUMBA_CACHE_DIR=str(args.output / "numba-cache"))
    start = time.perf_counter()
    with log.open("xb") as stream:
        result = subprocess.run([str(args.java), "-jar", str(args.jar), *map(str, command), str(args.runtime)],
                                stdout=stream, stderr=subprocess.STDOUT, env=env, check=False)
    elapsed = time.perf_counter() - start
    if result.returncode:
        raise RuntimeError(f"Java exited {result.returncode}; see {log}")
    return elapsed


def sample_run(row, args):
    sid = row["sample"]
    root = args.output / "samples" / sid
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / "receipt.json"
    if receipt.exists():
        state = read(receipt).get("state")
        if state == "PASS" or (state == "FAILED" and not args.retry):
            return
    stage = "source"
    attempt = root / uuid4().hex
    attempt.mkdir()
    def running(phase):
        publish(receipt, {"sample": sid, "state": "RUNNING", "stage": phase, "attempt": str(attempt)})

    running(stage)
    try:
        files = check_inputs(row)
        if shutil.disk_usage(args.output).free < 200 * GIB + 8 * row["native_RGB_bytes"]:
            raise RuntimeError("Output volume is below the reserve for this original slide")
        if sid == "NCBI29":
            pilot = read(args.output / "PILOT.json")
            result = dict(pilot, reused_pilot=True, state="PASS", sample=sid)
            if pilot["manifest_sha256"] != sha256(Path(pilot["package"]) / "manifest.json"):
                raise ValueError("NCBI29 archive changed after the gate")
        else:
            stage = "encode"
            running(stage)
            archive = attempt / f"{sid}.stpyramid"
            command = "paired-fused" if args.profile == "fused" else "paired"
            t_encode = java_call(args, attempt / "encode.log", command, files["image"], files["count"], files["metadata"], archive)
            expected_profile = "l1_conditional" if args.profile == "fused" else "hf_l1_conditional"
            if read(archive / "manifest.json")["image"]["profile"] != expected_profile:
                raise ValueError("Produced image profile differs from requested Java mode")
            stages = stage_seconds(attempt / "encode.log")
            stage = "verify"
            running(stage)
            t_verify = java_call(args, attempt / "verify.log", "verify", archive)
            stage = "restore"
            running(stage)
            # Keep the encoded package and logs. This controlled temporary directory
            # only holds regenerated output from this attempt.
            with tempfile.TemporaryDirectory(prefix="restore-check-", dir=attempt) as generated:
                restored = Path(generated) / "native"
                t_restore = java_call(args, attempt / "restore.log", "restore", archive, restored)
                stage = "compare_source"
                running(stage)
                check_started = time.perf_counter()
                result = validate_recovery(files, archive, restored, attempt)
                result.update(compare_source_wall_seconds=time.perf_counter() - check_started)
            result.update(state="PASS", sample=sid, original_bytes=sum(p.stat().st_size for p in files.values()),
                          encode_process_wall_seconds=t_encode, verify_wall_seconds=t_verify,
                          restore_wall_seconds=t_restore, native_stages_seconds=stages)
        result.update(attempt=str(attempt))
    except Exception as exc:
        result = {"state": "FAILED", "sample": sid, "attempt": str(attempt), "stage": stage,
                  "error": str(exc), "traceback": traceback.format_exc()}
    publish(attempt / "receipt.json", result)
    publish(receipt, result)


def summary(rows, output):
    statuses = []
    for row in rows:
        sid = row["sample"]
        p = output / "samples" / sid / "receipt.json"
        item = read(p) if p.exists() else {"state": "PENDING", "sample": sid}
        statuses.append({k: item.get(k) for k in ("sample", "state", "archive_bytes", "original_bytes",
            "encode_process_wall_seconds", "verify_wall_seconds", "restore_wall_seconds", "stage", "error", "package", "attempt")})
    counts = dict(Counter(item["state"] for item in statuses))
    publish(output / "STATUS.json", {"total": len(rows), "counts": counts, "epoch": time.time(),
                                    "controller_pid": os.getpid(), "samples": statuses})
    temporary = output / f"results.{uuid4().hex}.tmp"
    with temporary.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(statuses[0]))
        writer.writeheader()
        writer.writerows(statuses)
    replace(temporary, output / "results.csv")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("cohort", "output", "java", "jar", "runtime", "acceptance"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("mode", choices=("pilot", "cohort"))
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1)
    parser.add_argument("--profile", choices=("maximum", "fused"), default="maximum")
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = read(args.cohort)["rows"]
    if len(rows) != 50 or len({r["sample"] for r in rows}) != 50 or not any(r["sample"] == "NCBI29" for r in rows):
        raise ValueError("Expected the fixed 50-unique-sample cohort including NCBI29")
    pins = {"cohort_sha256": sha256(args.cohort), "jar_sha256": sha256(args.jar),
            "worker_sha256": sha256(args.runtime / "app/desktop_worker.py")}
    if args.mode == "pilot":
        row = next(r for r in rows if r["sample"] == "NCBI29")
        files = check_inputs(row)
        a = args.acceptance
        timing = read(a / "NCBI29-java-read-timing.json")
        result = validate_recovery(files, a / "NCBI29.stpyramid", a / "NCBI29-native-restored", a)
        stages = stage_seconds(a / "NCBI29-native.log")
        result.update(pins, **timing, state="PASS", sample="NCBI29", native_stages_seconds=stages,
                      encode_jar_sha256=sha256(a.parent / "STPyramidJava.jar"),
                      original_bytes=sum(p.stat().st_size for p in files.values()),
                      encode_process_wall_seconds=None,
                      encode_stage_seconds_without_final_verification=sum(stages.values()) - stages["verify_counts_pixels_and_files"],
                      native_stage_seconds_with_final_verification=sum(stages.values()),
                      encode_timing_scope="Prior run: sum of native stage events; dedicated Java process stopwatch not recorded.")
        publish(args.output / "PILOT.json", result)
        print(json.dumps(result, indent=2))
        return
    gate = read(args.output / "PILOT.json")
    if gate["state"] != "PASS" or gate.get("profile", "maximum") != args.profile or any(gate[k] != v for k, v in pins.items()):
        raise ValueError("NCBI29 gate or pinned inputs failed")
    with (args.output / "controller.lock").open("a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        summary(rows, args.output)
        # Small specimens are admitted first. The cohort and order are recorded.
        ordered = sorted(rows, key=lambda r: (r["sample"] != "NCBI29", r["native_RGB_bytes"], r["sample"]))
        publish(args.output / "PLAN.json", dict(pins, profile=args.profile, workers=args.workers, order=[r["sample"] for r in ordered]))
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            pending = {pool.submit(sample_run, row, args) for row in ordered}
            while pending:
                completed, pending = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
                for job in completed:
                    job.result()
                try:
                    summary(rows, args.output)
                except OSError as exc:
                    print(f"Status refresh will be retried: {exc}", flush=True)
        summary(rows, args.output)
        print("Cohort processing completed.", flush=True)


if __name__ == "__main__":
    main()
