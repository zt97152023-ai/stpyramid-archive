"""Join independent archive and restoration receipts for a frozen HEST cohort."""
import argparse
from collections import Counter
import csv
import math
from pathlib import Path
from statistics import median
import subprocess
import time
from uuid import uuid4

import psutil

from benchmark_hest import publish, read as read_json_file, replace, sha256


ARMS = ("maximum_old", "maximum_v022", "fused_v021")
TERMINAL = {"PASS", "FAILED", "EVIDENCE_ERROR"}
VERIFIED = ("h5ad_exact", "h5ad_type_oracle_passed", "native_tiff_metadata_exact", "source_attachment_exact")
MEASURES = ("archive_bytes", "encode_process_wall_seconds", "verify_wall_seconds", "restore_wall_seconds")


def read(path):
    for attempt in range(12):
        try:
            return read_json_file(path)
        except PermissionError:
            if attempt == 11:
                raise
            time.sleep(.25)


def verified_archive(receipt, profile, original_bytes):
    if any(receipt.get(field) is not True for field in VERIFIED) or not receipt.get("levels"):
        raise ValueError("Independent lossless verification is incomplete")
    if receipt["original_bytes"] != original_bytes:
        raise ValueError("Original byte count differs from frozen cohort")
    archive = Path(receipt["package"])
    manifest_path = archive / "manifest.json"
    if sha256(manifest_path) != receipt["manifest_sha256"]:
        raise ValueError("Archive manifest changed after verification")
    manifest = read(manifest_path)
    if manifest["image"]["profile"] != profile or manifest["count"]["kind"] != "Shared_local":
        raise ValueError("Incorrect archive profile or Count codec")
    observations = {key: info["bytes"] for key, info in manifest["files"].items()}
    observations["manifest.json"] = manifest_path.stat().st_size
    if observations != receipt["members"] or sum(observations.values()) != receipt["archive_bytes"]:
        raise ValueError("Archive ledger differs from the verified receipt")
    return {key: item["sha256"] for key, item in manifest["files"].items()}


def observation(config, sample):
    path = Path(config["root"]) / "samples" / sample / "receipt.json"
    if not path.is_file():
        return {"state": "PENDING"}, ""
    receipt = read(path)
    if receipt["state"] == "PASS" and sample == "NCBI29" and config.get("ncbi29_wall_clock"):
        timed = read(Path(config["ncbi29_wall_clock"]))
        if (timed["state"] != "PASS" or timed["sample"] != sample
                or timed["manifest_sha256"] != receipt["manifest_sha256"]
                or timed["levels"] != receipt["levels"]
                or timed["h5ad_semantic_sha256"] != receipt["h5ad_semantic_sha256"]):
            raise ValueError("The supplementary NCBI29 timing identifies different data")
        receipt.update({name: timed[name] for name in MEASURES[1:]})
        return receipt, str(config["ncbi29_wall_clock"])
    return receipt, str(path)


def join_sample(row, configs, cache):
    sample = row["sample"]
    original_bytes = sum(row["sources"][key]["bytes"] for key in ("image", "count", "metadata"))
    item = {"sample": sample, "original_bytes": original_bytes}
    passed = {}
    for arm in ARMS:
        config = configs[arm]
        try:
            receipt, evidence = observation(config, sample)
            item[f"{arm}_state"] = receipt["state"]
            item[f"{arm}_stage"] = receipt.get("stage")
            item[f"{arm}_error"] = receipt.get("error")
            item[f"{arm}_evidence"] = evidence
            item.update({f"{arm}_{key}": receipt.get(key) for key in MEASURES})
            if receipt["state"] != "PASS":
                continue
            for key in MEASURES:
                if not isinstance(receipt[key], (float, int)) or not math.isfinite(receipt[key]) or receipt[key] <= 0:
                    raise ValueError(f"Invalid or missing measurement {key}")
            key = (arm, sample, receipt["manifest_sha256"])
            if key not in cache:
                cache[key] = verified_archive(receipt, config["image_profile"], original_bytes)
            passed[arm] = receipt, cache[key]
        except (KeyError, OSError, ValueError) as error:
            item[f"{arm}_state"] = "EVIDENCE_ERROR"
            item[f"{arm}_error"] = str(error)

    if "maximum_old" in passed and "maximum_v022" in passed:
        old, old_hash = passed["maximum_old"]
        new, new_hash = passed["maximum_v022"]
        item["maximum_pair_lossless"] = same_recovery(old, new)
        item["maximum_pair_payload_identical"] = (
            {k: v for k, v in old_hash.items() if k != "image/manifest.json"}
            == {k: v for k, v in new_hash.items() if k != "image/manifest.json"}
        )
        item["maximum_pair_extra_bytes"] = new["archive_bytes"] - old["archive_bytes"]
        if item["maximum_pair_lossless"]:
            item["maximum_pair_encode_speedup"] = old["encode_process_wall_seconds"] / new["encode_process_wall_seconds"]
            item["maximum_pair_restore_speedup"] = old["restore_wall_seconds"] / new["restore_wall_seconds"]
    if len(passed) == len(ARMS):
        old = passed["maximum_old"][0]
        new = passed["maximum_v022"][0]
        fused = passed["fused_v021"][0]
        item["three_arm_lossless"] = same_recovery(old, new) and same_recovery(new, fused)
        if item["three_arm_lossless"]:
            item["fused_extra_bytes_vs_new_maximum"] = fused["archive_bytes"] - new["archive_bytes"]
            item["maximum_vs_fused_encode_time_ratio"] = new["encode_process_wall_seconds"] / fused["encode_process_wall_seconds"]
    return item


def same_recovery(left, right):
    return (left["levels"] == right["levels"]
            and left["h5ad_semantic_sha256"] == right["h5ad_semantic_sha256"])


def summarize(rows, configs, threshold):
    result = {}
    for arm in ARMS:
        result[arm] = dict(Counter(row[f"{arm}_state"] for row in rows))
    complete = [row for row in rows if row.get("three_arm_lossless")]
    paired = [row for row in rows if row.get("maximum_pair_lossless")]
    evidence_errors = sum(row[f"{arm}_state"] == "EVIDENCE_ERROR" for row in rows for arm in ARMS)
    terminal = all(row[f"{arm}_state"] in TERMINAL for row in rows for arm in ARMS)
    result["coverage"] = {"expected_per_arm": len(rows), "maximum_pairs_lossless": len(paired),
                          "three_arm_lossless": len(complete), "evidence_errors": evidence_errors}
    result["state"] = ("PASS" if len(complete) == len(rows) else "INCOMPLETE") if terminal else "RUNNING"
    result["scope"] = {arm: configs[arm]["timing_scope"] for arm in ARMS}
    result["NCBI29_reference_encode_speedup"] = threshold
    if paired:
        values = [row["maximum_pair_encode_speedup"] for row in paired]
        validation = [row["maximum_pair_encode_speedup"] for row in paired if row["sample"] != "NCBI29"]
        result["maximum_pairs"] = {
            "median_encode_speedup": median(values),
            "median_restore_speedup": median(row["maximum_pair_restore_speedup"] for row in paired),
            "same_archive_size": sum(row["maximum_pair_extra_bytes"] == 0 for row in paired),
            "bit_identical_payload": sum(row["maximum_pair_payload_identical"] for row in paired),
            "other_samples_paired": len(validation),
            "other_samples_faster": sum(value > 1 for value in validation),
            "other_samples_at_least_NCBI29_speedup": sum(value >= threshold for value in validation),
            "other_samples_median_encode_speedup": median(validation) if validation else None,
        }
    if complete:
        result["three_arm_pairs"] = {
            "median_new_maximum_encode_time_divided_by_fused": median(
                row["maximum_vs_fused_encode_time_ratio"] for row in complete),
            "total_bytes": {arm: sum(row[f"{arm}_archive_bytes"] for row in complete) for arm in ARMS},
        }
    return result


def controller_is_live(pid, root):
    if pid is None:
        return False
    try:
        args = psutil.Process(pid).cmdline()
        return "--output" in args and Path(args[args.index("--output") + 1]).resolve() == root
    except (IndexError, OSError, psutil.Error):
        return False


def supervise(arm, config, rows):
    root = Path(config["root"]).resolve()
    marker = root / "comparison_retry.json"
    status_path = root / "STATUS.json"
    retry = read(marker) if marker.is_file() else {}
    if any(controller_is_live(pid, root) for pid in (retry.get("pid"), config.get("initial_pid"))):
        return "ACTIVE"
    status = read(status_path) if status_path.is_file() else {}
    if controller_is_live(status.get("controller_pid"), root):
        return "ACTIVE"
    unfinished = any(row[f"{arm}_state"] not in TERMINAL for row in rows)
    retryable = any(row[f"{arm}_state"] == "FAILED"
                    and row[f"{arm}_stage"] != "source" for row in rows)
    if not unfinished and not retryable:
        return "FINISHED"
    if retry or not config.get("resume"):
        return "STOPPED_WITH_UNRESOLVED_ROWS" if unfinished else "FINISHED_WITH_FAILURES"
    with (root / "comparison_retry.log").open("ab") as log:
        child = subprocess.Popen([*config["resume"], "--retry"], stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
    publish(marker, {"pid": child.pid, "epoch": time.time()})
    return "RETRY_LAUNCHED"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    plan = read(args.plan)
    cohort = Path(plan["cohort"])
    if sha256(cohort) != plan["cohort_sha256"]:
        raise ValueError("Frozen HEST cohort changed")
    rows = read(cohort)["rows"]
    if len(rows) != 50 or len({row["sample"] for row in rows}) != 50:
        raise ValueError("Expected the locked 50 unique HEST samples")
    cache = {}
    while True:
        paired = [join_sample(row, plan["arms"], cache) for row in rows]
        result = summarize(paired, plan["arms"], plan["ncbi29_reference_encode_speedup"])
        if args.watch:
            controllers = {arm: supervise(arm, plan["arms"][arm], paired) for arm in ARMS}
            result["controllers"] = controllers
            active = {"ACTIVE", "RETRY_LAUNCHED"}.intersection(controllers.values())
            if active and result["state"] != "PASS":
                result["state"] = "RUNNING"
            if not active and result["state"] == "RUNNING":
                result["state"] = "INCOMPLETE"
        fields = list(dict.fromkeys(key for row in paired for key in row))
        staged = args.plan.parent / f"paired_results.{uuid4().hex}.tmp"
        with staged.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(paired)
        try:
            replace(staged, args.plan.parent / "paired_results.csv")
            publish(args.plan.parent / "SUMMARY.json", result)
        except PermissionError as error:
            if not args.watch:
                raise
            staged.unlink(missing_ok=True)
            print(f"Comparison report is open for editing and will be retried: {error}", flush=True)
            time.sleep(30)
            continue
        if not args.watch or result["state"] in {"PASS", "INCOMPLETE"}:
            return
        time.sleep(30)


if __name__ == "__main__":
    main()
