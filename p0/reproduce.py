"""Prepare and run the fixed P0 experiment with locally supplied HEST inputs."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from worker import read, sha, write


HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "source"
GIB = 1024**3


def prepare(args):
    import psutil

    if sys.platform != "win32" or sys.version_info[:2] != (3, 12):
        raise ValueError("Use 64-bit CPython 3.12 on Windows.")
    config = read(HERE / "CONFIG.json")
    if args.cpus is not None:
        config["affinity"] = [int(cpu) for cpu in args.cpus.split(",")]
    if (len(config["affinity"]) != config["encode_workers"]
            or len(set(config["affinity"])) != config["encode_workers"]
            or not set(config["affinity"]) <= set(psutil.Process().cpu_affinity())):
        raise ValueError("Provide a legal 24-CPU affinity set with --cpus.")

    work = args.work.resolve()
    data = args.hest_root.resolve(strict=True)
    if work == data or work.is_relative_to(data):
        raise ValueError("Create a work directory outside the HEST inputs.")
    if not str(work).isascii():
        raise ValueError("The tested shared-spool path requires ASCII scratch paths.")
    for sample in config["samples"].values():
        for item in sample["inputs"].values():
            path = (data / item["path"]).resolve(strict=True)
            if not path.is_relative_to(data) or path.stat().st_size != item["bytes"]:
                raise ValueError(f"HEST input does not match its size pin: {path.name}")
            item["path"] = str(path)

    runtime = args.runtime_dir.resolve(strict=True)
    native = args.native_encoder.resolve(strict=True)
    resolved = {}
    actual = {}
    pins = read(HERE / "RUNTIME_PINS.json")["files"]
    for name in pins:
        path = native if name.startswith("bin/") else runtime / Path(name).relative_to("runtime-windows-source")
        resolved[name] = path
        actual[name] = {"bytes": path.stat().st_size, "sha256": sha(path)}
    if actual != pins and not args.allow_rebuilt_runtime:
        raise ValueError("Native runtime differs from P0 pins. For a source-build replication, use --allow-rebuilt-runtime.")
    probe = subprocess.run([str(native), "census_input_version"], check=True, capture_output=True, timeout=15)
    if probe.stdout.strip() != b"091RGB01":
        raise ValueError("The native encoder lacks the P0 shared-spool protocol.")

    work.mkdir(parents=True)
    app = work / "app"
    shutil.copytree(SOURCE / "codec", app / "codec")
    for name in ("fetch_count_runtime.py", "native_tile_encoder.py"):
        shutil.copyfile(SOURCE / name, app / name)
    shutil.copyfile(HERE / "overlay/pyramid_image_archive.py", app / "pyramid_image_archive.py")
    for name, expected in config["app_sources_sha256"].items():
        if sha(app / name) != expected:
            raise ValueError(f"Frozen application source mismatch: {name}")
    for name, path in resolved.items():
        destination = app / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        if sha(destination) != actual[name]["sha256"]:
            raise ValueError(f"Runtime changed while staging: {name}")
    for kind in ("089", "091", "hf"):
        shutil.copyfile(runtime / kind / "build_receipt.json", app / "runtime-windows-source" / kind / "build_receipt.json")
    subprocess.run([sys.executable, "-B", str(app / "fetch_count_runtime.py")], check=True)
    config["native_exe_sha256"] = actual["bin/slide_jxl091.exe"]["sha256"]
    config["replication_runtime"] = {"exact_native_pins": actual == pins, "files": actual}
    write(work / "CONFIG.json", config)


def run_arm(work, group, sample, arm, native_rgb_bytes):
    case = group / arm
    case.mkdir(parents=True)
    if shutil.disk_usage(case).free < 7 * GIB + 3 * native_rgb_bytes:
        raise RuntimeError("Insufficient space for the case and the scratch reserve.")
    env = os.environ.copy()
    env.pop("PYTHONOPTIMIZE", None)
    for step in ("encode", "verify"):
        command = [sys.executable, "-B", "-u", str(HERE / "worker.py"), str(work), str(case), sample, arm, step]
        with (case / f"{step}.stdout.log").open("xb") as stdout, (case / f"{step}.stderr.log").open("xb") as stderr:
            completed = subprocess.run(command, cwd=work, env=env, stdout=stdout, stderr=stderr)
        if completed.returncode:
            error = {"state": "FAIL", "sample": sample, "arm": arm, "stage": step, "exit_code": completed.returncode}
            write(case / "ERROR.json", error)
            return error
    result = read(case / "RESULT.json")
    # The verified case was created above. The census can be rebuilt from the pinned input.
    spool = case / "work/census/tiles.rgb"
    if spool.is_symlink() or spool.resolve(strict=True) != spool:
        raise ValueError("Unexpected census path")
    spool.unlink()
    return result


def run_pair(work, group, config, sample, order):
    group.mkdir(parents=True)
    rows = {arm: run_arm(work, group, sample, arm, config["samples"][sample]["native_rgb_bytes"]) for arm in order}
    passed = all(row["state"] == "PASS" for row in rows.values())
    if passed:
        left, right = (rows[arm] for arm in config["arms"])
        passed = left["count_members"] == right["count_members"] and left["native_sha256"] == right["native_sha256"]
    state = "PASS" if passed else "INCOMPLETE"
    write(group / "PAIR.json", {"sample": sample, "state": state, "arms": rows})
    print(f"{sample}: {state}", flush=True)
    return passed


def run(args):
    work = args.work.resolve(strict=True)
    config = read(work / "CONFIG.json")
    gate = work / "preflight/NCBI29"
    mode = args.mode
    if mode == "preflight":
        return run_pair(work, gate, config, "NCBI29", config["preflight_order"])
    if read(gate / "PAIR.json")["state"] != "PASS":
        raise ValueError("The local NCBI29 preflight must pass before formal runs.")
    formal = config["formal_pairs"]
    if mode == "formal":
        (work / "formal").mkdir()
        pairs = [run_pair(work, work / "formal" / row["sample"], config, row["sample"], row["order"]) for row in formal]
        return all(pairs)
    selected = next((row for row in formal if row["sample"] == args.sample), None)
    if selected is None or read(work / "formal" / args.sample / "PAIR.json")["state"] != "INCOMPLETE":
        raise ValueError("Retry requires an incomplete formal pair from this run.")
    group = work / "retries" / f"{args.sample}_attempt02"
    return run_pair(work, group, config, args.sample, selected["order"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="mode", required=True)
    stage = actions.add_parser("prepare")
    for name in ("work", "hest-root", "runtime-dir", "native-encoder"):
        stage.add_argument(f"--{name}", type=Path, required=True)
    stage.add_argument("--cpus")
    stage.add_argument("--allow-rebuilt-runtime", action="store_true")
    for name in ("preflight", "formal", "retry"):
        cmd = actions.add_parser(name)
        cmd.add_argument("--work", type=Path, required=True)
        if name == "retry":
            cmd.add_argument("--sample", required=True)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args)
    elif not run(args):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
