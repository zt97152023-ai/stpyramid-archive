"""Compare both Java HEST modes on NCBI29 and admit the fused cohort on success."""
import argparse
import csv
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

import psutil

from benchmark_hest import check_inputs, java_call, publish, read, sha256, stage_seconds, validate_recovery
from container_compare import SHARED_COUNT_KINDS
from joint_next import CONDITIONAL091


def compare_arm(args, files, profile):
    root = args.output / profile
    root.mkdir()
    archive = root / "NCBI29.stpyramid"
    restored = root / "restored"
    publish(args.output / "STATUS.json", {"state": "RUNNING", "profile": profile, "stage": "encode", "pid": os.getpid()})
    command = "paired" if profile == "maximum" else "paired-fused"
    encode = java_call(args, root / "encode.log", command, files["image"], files["count"], files["metadata"], archive)
    requested = "hf_l1_conditional" if profile == "maximum" else "l1_conditional"
    manifest = read(archive / "manifest.json")
    if manifest["image"]["profile"] != requested or manifest["count"]["kind"] not in SHARED_COUNT_KINDS:
        raise ValueError("Archive profile differs from requested configuration")
    if profile == "fused":
        routes = read(archive / "image/manifest.json")["routes"]
        # A real fixture must exercise the conditional path.
        if not int(routes.get(str(CONDITIONAL091), 0)):
            raise ValueError(f"No 091 conditional tiles in fused archive: {routes}")
    publish(args.output / "STATUS.json", {"state": "RUNNING", "profile": profile, "stage": "verify and restore", "pid": os.getpid()})
    verify = java_call(args, root / "verify.log", "verify", archive)
    restore = java_call(args, root / "restore.log", "restore", archive, restored)
    result = validate_recovery(files, archive, restored, root)
    result.update(state="PASS", sample="NCBI29", profile=profile, count_kind=manifest["count"]["kind"],
                  encode_process_wall_seconds=encode,
                  verify_wall_seconds=verify, restore_wall_seconds=restore,
                  native_stages_seconds=stage_seconds(root / "encode.log"),
                  original_bytes=sum(p.stat().st_size for p in files.values()), cpu=args.cpu)
    publish(root / "RESULT.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("cohort", "output", "java", "jar", "runtime", "batch"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--cpu", type=int, default=22)
    parser.add_argument("--pilot-only", action="store_true", help="Do not start the full cohort after the comparison")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    psutil.Process().cpu_affinity([args.cpu])
    row = next(r for r in read(args.cohort)["rows"] if r["sample"] == "NCBI29")
    files = check_inputs(row)
    try:
        fused = compare_arm(args, files, "fused")
        maximum = compare_arm(args, files, "maximum")
        if (fused["levels"] != maximum["levels"] or
            fused["h5ad_semantic_sha256"] != maximum["h5ad_semantic_sha256"]):
            raise ValueError("Restored content differs between the two modes")
        tree = lambda arm: {name: sha256(Path(arm["package"]) / name)
                            for name in arm["members"] if name.startswith("count/")}
        if tree(fused) != tree(maximum):
            raise ValueError("Count components differ across the two Java modes")
        pin = {"cohort_sha256": sha256(args.cohort), "jar_sha256": sha256(args.jar),
               "worker_sha256": sha256(args.runtime / "app/desktop_worker.py")}
        change = {"extra_bytes_fused_minus_maximum": fused["archive_bytes"] - maximum["archive_bytes"],
                  "space_change_percent": 100 * (fused["archive_bytes"] / maximum["archive_bytes"] - 1),
                  "encode_speedup": maximum["encode_process_wall_seconds"] / fused["encode_process_wall_seconds"],
                  "encode_time_saved_seconds": maximum["encode_process_wall_seconds"] - fused["encode_process_wall_seconds"]}
        publish(args.output / "COMPARISON.json", dict(pin, maximum=maximum, fused=fused, comparison=change,
                timing_scope="Sequential, same logical CPU and backend, elapsed from Java launch to exit, one run per profile, OS cache not purged.",
                count_members_bit_identical=True, epoch=time.time()))
        with (args.output / "comparison.csv").open("x", encoding="utf-8-sig", newline="") as stream:
            columns = ("profile", "archive_bytes", "encode_process_wall_seconds", "verify_wall_seconds", "restore_wall_seconds",
                       "h5ad_exact", "native_tiff_metadata_exact", "source_attachment_exact")
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows((maximum, fused))
        publish(args.output / "STATUS.json", {"state": "COMPARISON_PASS", "pid": os.getpid(), "comparison": change})
        if args.pilot_only or change["encode_time_saved_seconds"] <= 0:
            return
        args.batch.mkdir(parents=True, exist_ok=False)
        publish(args.batch / "PILOT.json", dict(fused, **pin))
        # The existing maximum cohort runs independently. Keep this new lane on
        # the qualification core to avoid starting duplicate competing workers.
        with (args.batch / "controller.log").open("xb") as stream:
            launch = [sys.executable, "-B", "-X", "utf8", str(Path(__file__).with_name("benchmark_hest.py")),
                      "cohort", "--profile", "fused", "--workers", "1",
                      "--cohort", str(args.cohort), "--output", str(args.batch), "--java", str(args.java),
                      "--jar", str(args.jar), "--runtime", str(args.runtime), "--acceptance", str(args.output)]
            child = subprocess.Popen(launch, stdout=stream, stderr=subprocess.STDOUT)
            publish(args.output / "STATUS.json", {"state": "COHORT_LAUNCHED", "pid": os.getpid(),
                    "cohort_pid": child.pid, "cohort": str(args.batch), "comparison": change})
            code = child.wait()
            publish(args.output / "STATUS.json", {"state": "COHORT_FINISHED" if code == 0 else "COHORT_EXIT_ERROR",
                    "pid": os.getpid(), "cohort": str(args.batch), "cohort_exit_code": code, "comparison": change})
            if code:
                raise SystemExit(code)
    except Exception as exc:
        publish(args.output / "STATUS.json", {"state": "FAILED", "error": str(exc),
                                             "traceback": traceback.format_exc()})
        raise


if __name__ == "__main__":
    main()
