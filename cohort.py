"""Run a paired, resumable whole-slide benchmark against a fixed cohort."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import msvcrt
from pathlib import Path
from statistics import median
import subprocess
import sys
import time

from benchmark_read import level_bytes, measure_rois
from build_full import ROOT, RUNTIMES, archive, build, digest, file_info, psutil

RUN = ROOT / "cohort_run"
ARMS = ("direct9", "fused_l0_l1")
CPUS = (16, 18, 20, 22, 24, 26, 28, 30)
SECOND_WAVE_CORES = {"SPA45": 17, "NCBI815": 25}
HISTORICAL_RUNTIMES = tuple(ROOT / "historical-runtime" / name for name in ("089", "091", "hf"))
INITIAL_READER_PINS = {
    "cohort.py": "e1a4d1a374c9022dff964c070e4d42f0e349715927c3dea30e53fb0ec32a54de",
    "benchmark_read.py": "04d92fce3f0aac629e79896dee82214842af2f413b42dba917d1f822267fbcf4",
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf8"))


def write_json(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf8")
    tmp.replace(path)


def code_pins():
    files = [ROOT / name for name in ("build_full.py", "build_native.py", "encode_image.py", "cohort.py",
                                     "benchmark_read.py", "select_cohort.py", "COHORT.json")]
    for name in ("codec", "native", "bin", "runtime-windows-source", "historical-runtime"):
        files.extend(p for p in (ROOT / name).rglob("*")
                     if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".obj")
    return {path.relative_to(ROOT).as_posix(): digest(path) for path in sorted(files)}


def measure_read(package, expected, runtimes):
    process = psutil.Process()
    started = time.perf_counter()
    cpu = sum(process.cpu_times()[:2])
    with archive.Reader(package, *runtimes) as reader:
        checked = reader.verify()
        metrics = reader.metrics()
        levels = reader.manifest["image"]["levels"]
        if reader.source_metadata()["whole_file_sha256"] != expected["source_sha256"]:
            raise ValueError("Package source file identity differs from the fixed input")
    elapsed = time.perf_counter() - started
    cpu = sum(process.cpu_times()[:2]) - cpu
    if (not checked["ok"] or not all(item["pixels_exact"] for item in checked["levels"])
            or [item["pixel_sha256"] for item in checked["levels"]] != expected["native_rgb_sha256"]):
        raise ValueError("Whole-slide native RGB round trip failed")
    if [level["shape"] for level in levels] != expected["native_shapes"]:
        raise ValueError("Native geometry changed")
    return {"seconds": elapsed, "cpu_seconds": cpu, "levels": checked["levels"], "metrics": metrics,
            "level_bytes": level_bytes(package, levels), "roi": measure_rois(package, levels, runtimes)}


def check_source_metadata(records):
    names = ("direct9", "fused_l0_l1")
    if len({records[arm]["files"]["source.json"]["sha256"] for arm in names}) != 1:
        raise ValueError("Fresh paired packages retain different source metadata")
    current, historical = (read_json(Path(records[arm]["package"]) / "source.json")
                           for arm in ("direct9", "original"))
    time_keys = ("ctime_ns_fstat", "ctime_ns_path_stat", "mtime_ns")
    changed = [key for key in time_keys if current["identity"].get(key) != historical["identity"].get(key)]
    for source in (current, historical):
        for key in time_keys:
            source["identity"].pop(key, None)
    if current != historical:
        raise ValueError("Historical archive source metadata differs beyond filesystem timestamps")
    return changed


def run_case(index, cpu):
    expected = read_json(ROOT / "COHORT.json")["samples"][index]
    out = RUN / expected["sample"]
    out.mkdir(exist_ok=True)
    with (out / "worker.lock").open("a+b") as guard:
        guard.seek(0, 2)
        if guard.tell() == 0:
            guard.write(b"0")
            guard.flush()
        while True:
            guard.seek(0)
            try:
                msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno != 13:
                    raise
                time.sleep(3)
            else:
                break
        if not (out / "RESULT.json").is_file():
            run_locked_case(index, SECOND_WAVE_CORES.get(expected["sample"], cpu), expected, out)


def run_locked_case(index, cpu, expected, out):
    name = expected["sample"]
    attempts = out / "attempts"
    attempts.mkdir(exist_ok=True)
    attempt = attempts / f"{len(list(attempts.iterdir())) + 1:02d}"
    attempt.mkdir()
    process = psutil.Process()
    process.cpu_affinity([cpu])
    process.nice(psutil.IDLE_PRIORITY_CLASS)
    source = Path(expected["source"])
    if digest(source) != expected["source_sha256"]:
        raise ValueError("Source TIFF digest differs from frozen cohort")

    def log(record):
        with (out / "STAGES.jsonl").open("a", encoding="utf8") as stream:
            stream.write(json.dumps({"epoch": time.time(), "sample": name, "cpu": cpu, **record}) + "\n")

    log({"event": "begin_sample", "attempt": attempt.name})
    records = {}
    order = ARMS if index % 2 == 0 else ARMS[::-1]
    for arm in order:
        receipt = out / f"{arm}.encode.json"
        if receipt.is_file():
            row = read_json(receipt)
            if row["cpu"] != cpu or file_info(Path(row["package"])) != row["files"]:
                raise ValueError("Resumed archive differs from the existing paired measurement")
        else:
            arm_dir = attempt / arm
            arm_dir.mkdir()
            row = build(source, arm_dir / "image", arm_dir / "work", arm, log)
            row["cpu"] = cpu
            if row["native_level_sha256"] != expected["native_rgb_sha256"]:
                raise ValueError("Fresh encoder did not consume the frozen RGB levels")
            write_json(receipt, row)
        records[arm] = row

    original = Path(expected["historical_hf_l1"])
    old_files = file_info(original)
    records["original"] = {"method": "original", "package": str(original), "files": old_files,
                           "bytes": sum(info["bytes"] for info in old_files.values()),
                           "encode_seconds": None, "scope": "Historical archive, fresh read only"}
    historical_direct = file_info(Path(expected["historical_direct"]))
    e9_files = records["direct9"]["files"]
    direct_parity = {member: e9_files[member] == historical_direct[member] for member in historical_direct}

    reads = {}
    read_order = (*order, "original")
    for arm in read_order:
        receipt = out / f"{arm}.read.json"
        if receipt.is_file():
            reads[arm] = read_json(receipt)
        else:
            log({"arm": arm, "begin": "full_read_and_roi"})
            runtimes = HISTORICAL_RUNTIMES if arm == "original" else RUNTIMES
            reads[arm] = measure_read(Path(records[arm]["package"]), expected, runtimes)
            write_json(receipt, reads[arm])
            log({"arm": arm, "end": "full_read_and_roi", "seconds": reads[arm]["seconds"]})
    for level in ("0", "1"):
        values = {tuple(item["roi"][level]["roi_sha256"]) for item in reads.values()}
        if len(values) != 1:
            raise ValueError(f"ROI pixel checksums differ at native L{level}")
    timestamp_changes = check_source_metadata(records)
    result = {"status": "PASS", "sample": name, "source_sha256": expected["source_sha256"], "cpu": cpu,
              "arms": records, "reads": reads, "fresh_e9_historical_identity": direct_parity,
              "historical_filesystem_timestamp_differences": timestamp_changes}
    write_json(out / "RESULT.json", result)
    log({"event": "complete_sample"})
    print(json.dumps({"sample": name, "status": "PASS"}), flush=True)


def summarize(cohort):
    rows = []
    for sample in cohort["samples"]:
        case = read_json(RUN / sample["sample"] / "RESULT.json")
        if case["status"] != "PASS" or case["source_sha256"] != sample["source_sha256"]:
            raise ValueError("Sample record does not match the fixed cohort")
        e9, fused, original = (case["arms"][arm] for arm in (*ARMS, "original"))
        row = {"sample": sample["sample"], "platform": sample["platform"],
               "l0_pixels": sample["l0_pixels"], "levels": len(sample["native_shapes"]),
               "exact": True, "bytes_e9": e9["bytes"], "bytes_fused": fused["bytes"], "bytes_original": original["bytes"],
               "encode_e9": e9["encode_seconds"], "encode_fused": fused["encode_seconds"],
               "read_e9": case["reads"]["direct9"]["seconds"], "read_fused": case["reads"]["fused_l0_l1"]["seconds"],
               "read_original": case["reads"]["original"]["seconds"]}
        row["fused_byte_reduction_vs_e9"] = 1 - row["bytes_fused"] / row["bytes_e9"]
        row["fused_encode_reduction_vs_e9"] = 1 - row["encode_fused"] / row["encode_e9"]
        row["fused_read_ratio_vs_e9"] = row["read_fused"] / row["read_e9"]
        row["fused_vs_original_bytes"] = row["bytes_fused"] - row["bytes_original"]
        rows.append(row)
    report = {
        "status": "PASS", "cohort_rule": cohort["rule"], "samples": rows,
        "native_rgb_exact_count": len(rows),
        "smaller_than_e9_count": sum(row["fused_byte_reduction_vs_e9"] > 0 for row in rows),
        "encodes_faster_than_e9_count": sum(row["fused_encode_reduction_vs_e9"] > 0 for row in rows),
        "reads_faster_than_e9_count": sum(row["fused_read_ratio_vs_e9"] < 1 for row in rows),
        "median_byte_reduction_vs_e9": median(row["fused_byte_reduction_vs_e9"] for row in rows),
        "median_encode_reduction_vs_e9": median(row["fused_encode_reduction_vs_e9"] for row in rows),
        "median_read_ratio_vs_e9": median(row["fused_read_ratio_vs_e9"] for row in rows),
        "scope": "Image-only. Fresh e9 and fused build from source on one core per pair, other pairs concurrent. "
                 "Single encode and full read per arm; full read includes physical member and native RGB verification. "
                 "Historical HF+L1 image archives contribute actual bytes and fresh reads only. "
                 "Five warm-reader ROI passes per level. OS cache uncontrolled.",
    }
    write_json(RUN / "RESULT.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "samples"}), flush=True)


def run():
    import imagecodecs

    cohort = read_json(ROOT / "COHORT.json")
    RUN.mkdir(exist_ok=True)
    queue = sorted(range(len(cohort["samples"])), key=lambda i: -cohort["samples"][i]["l0_pixels"])
    initial_lanes = {cpu: queue[k::len(CPUS)] for k, cpu in enumerate(CPUS)}
    lanes = {cpu: [] for cpu in CPUS}
    for cpu, lane in initial_lanes.items():
        for index in lane:
            assigned = SECOND_WAVE_CORES.get(cohort["samples"][index]["sample"], cpu)
            lanes.setdefault(assigned, []).append(index)
    plan = {"cohort_sha256": digest(ROOT / "COHORT.json"), "code_sha256": code_pins(),
            "jpegxl_version": imagecodecs.jpegxl_version(), "imagecodecs": imagecodecs.__version__,
            "core_assignment": {str(cpu): lane for cpu, lane in lanes.items()}}
    plan_path = RUN / "PLAN.json"
    if plan_path.is_file():
        previous = read_json(plan_path)
        if previous != plan:
            previous_code = previous["code_sha256"]
            retained_code = {key: value for key, value in plan["code_sha256"].items()
                             if not key.startswith("historical-runtime/")}
            initial = RUN / "PLAN.initial.json"
            initial_settings = {**plan, "core_assignment": {str(cpu): lane for cpu, lane in initial_lanes.items()}}
            same_settings = {k: v for k, v in previous.items() if k != "code_sha256"} == {
                k: v for k, v in initial_settings.items() if k != "code_sha256"}
            if (initial.exists() or not same_settings
                    or any(previous_code.get(key) != sha for key, sha in INITIAL_READER_PINS.items())
                    or previous_code != {**retained_code, **INITIAL_READER_PINS}):
                raise ValueError("Frozen cohort, encoder or benchmark setup changed")
            write_json(initial, previous)
            write_json(RUN / "PLAN.amendment.json", {
                "reason": "Historical HF+L1 archives require their distinct pinned native decoder runtime. "
                          "Historical TENX28 file timestamps changed with identical TIFF bytes and native metadata. "
                          "Assign the two unstarted second-wave cases to spare E-cores.",
                "impact": "Only the historical read runtime changes. New and direct e9 encoder code, "
                          "sample selection, and their read runtime are unchanged. "
                          "Each pair uses a single core for all its own timings. "
                          "Fresh source metadata hashes must still match. Historical typed image metadata "
                          "and source SHA-256 must still match, with filesystem timestamps logged separately.",
                "retained_existing_measurements": ["direct9", "fused_l0_l1"],
            })
            write_json(plan_path, plan)
    else:
        write_json(plan_path, plan)

    def run_lane(cpu, lane):
        failures = []
        for index in lane:
            name = cohort["samples"][index]["sample"]
            if (RUN / name / "RESULT.json").is_file():
                continue
            logfile = RUN / f"{name}.log"
            with logfile.open("ab") as stream:
                result = subprocess.run([sys.executable, "-B", __file__, "--index", str(index), "--cpu", str(cpu)],
                                        stdout=stream, stderr=subprocess.STDOUT, check=False)
            if result.returncode:
                failures.append(name)
            print(json.dumps({"sample": name, "cpu": cpu, "exit": result.returncode}), flush=True)
        return failures

    with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        futures = [pool.submit(run_lane, cpu, lane) for cpu, lane in lanes.items()]
        failures = [name for future in as_completed(futures) for name in future.result()]
    if failures:
        raise RuntimeError(f"Failed sample workers: {', '.join(failures)}")
    summarize(cohort)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int)
    parser.add_argument("--cpu", type=int)
    args = parser.parse_args()
    if args.index is None:
        RUN.mkdir(exist_ok=True)
        with (RUN / "controller.lock").open("a+b") as guard:
            guard.seek(0, 2)
            if guard.tell() == 0:
                guard.write(b"0")
                guard.flush()
            guard.seek(0)
            msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
            run()
    else:
        run_case(args.index, args.cpu)
