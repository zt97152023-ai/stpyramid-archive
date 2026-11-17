"""Measure fast-fusion worker budgets and check all restored data and archive bytes."""
import argparse
import json
import os
from pathlib import Path
import sys
import traceback

import psutil

from benchmark_hest import check_inputs, java_call, publish, read, sha256, stage_seconds, validate_recovery


def logged_budget(path, field):
    events = (json.loads(line) for line in path.read_text("utf-8").splitlines() if line.startswith("{"))
    return next(event[field] for event in events if field in event)


def run_case(args, files, workers):
    directory = args.output / f"workers-{workers}"
    directory.mkdir()
    archive = directory / f"{args.sample}.stpyramid"
    restored = directory / "restored"
    for option in ("STPYRAMID_FUSED_WORKERS", "STPYRAMID_SCAN_WORKERS"):
        if workers == 8:
            os.environ.pop(option, None)
        else:
            os.environ[option] = str(workers)
    wall = {}
    for action, arguments in (
        ("encode", ["paired-fused", files["image"], files["count"], files["metadata"], archive]),
        ("verify", ["verify", archive]),
        ("restore", ["restore", archive, restored]),
    ):
        publish(args.output / "STATUS.json", {"state": "RUNNING", "workers": workers, "action": action})
        wall[action] = java_call(args, directory / f"{action}.log", *arguments)
    if logged_budget(directory / "encode.log", "native_workers") != workers:
        raise ValueError(f"Did not obtain {workers} encoder workers")
    for action in ("verify", "restore"):
        if logged_budget(directory / f"{action}.log", "native_scan_workers") != workers:
            raise ValueError(f"Did not obtain {workers} decoder workers for {action}")
    manifest = read(archive / "manifest.json")
    if manifest["image"]["profile"] != "l1_conditional" or manifest["count"]["kind"] != "Shared_local":
        raise ValueError("Wrong integrated compression profile")
    result = validate_recovery(files, archive, restored, directory)
    result.update(state="PASS", sample=args.sample, profile="fused", native_workers=workers,
                  original_bytes=sum(path.stat().st_size for path in files.values()),
                  encode_process_wall_seconds=wall["encode"], verify_wall_seconds=wall["verify"],
                  restore_wall_seconds=wall["restore"], native_stages_seconds=stage_seconds(directory / "encode.log"),
                  candidate_cpu_affinity=logged_budget(directory / "encode.log", "candidate_cpu_affinity"))
    publish(directory / "RESULT.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cohort", "output", "java", "jar", "runtime"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--sample", default="NCBI29")
    parser.add_argument("--order", choices=("4-first", "8-first"), default="8-first")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        process = psutil.Process()
        utilization = psutil.cpu_percent(interval=.25, percpu=True)
        cpus = sorted(process.cpu_affinity(), key=lambda core: (utilization[core], core))[:8]
        if len(cpus) < 8:
            raise RuntimeError("The 4/8 comparison requires eight permitted logical CPUs")
        process.cpu_affinity(cpus)
        files = check_inputs(next(row for row in read(args.cohort)["rows"] if row["sample"] == args.sample))
        order = (8, 4) if args.order == "8-first" else (4, 8)
        arms = {workers: run_case(args, files, workers) for workers in order}
        four, eight = arms[4], arms[8]
        old, new = (Path(result["package"]) for result in (four, eight))
        if (four["members"] != eight["members"] or four["levels"] != eight["levels"]
                or four["h5ad_semantic_sha256"] != eight["h5ad_semantic_sha256"]
                or any(sha256(old / member) != sha256(new / member) for member in four["members"])):
            raise ValueError("The four- and eight-worker archives or restored scientific content differ")
        publish(args.output / "COMPARISON.json", {
            "state": "PASS", "order": order, "eligible_logical_cpus": cpus,
            "cohort_sha256": sha256(args.cohort), "jar_sha256": sha256(args.jar),
            "worker_sha256": sha256(args.runtime / "app/desktop_worker.py"),
            "scope": "Sequential full Java process wall times on the same release; shared machine and natural OS caches.",
            "bit_identical_complete_packages": True, "four": four, "eight": eight,
        })
        publish(args.output / "STATUS.json", {"state": "PASS"})
    except Exception as error:
        publish(args.output / "STATUS.json", {"state": "FAILED", "error": str(error),
                                              "traceback": traceback.format_exc()})
        raise


if __name__ == "__main__":
    sys.exit(main())
