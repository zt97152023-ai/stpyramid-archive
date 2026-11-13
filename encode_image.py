"""Build and verify a native RGB pyramid with optional exact constant tile routing."""
import argparse
import ctypes
from ctypes import wintypes
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[name] = "1"
os.environ.pop("ST074_S", None)
os.environ.pop("ST074_T", None)

import psutil

ROOT = Path(__file__).resolve().parent
RELEASE = ROOT.parent / "STPyramid_Release_20261111"
RUNTIME = ROOT / "runtime-windows-source"
RUNTIMES = tuple(RUNTIME / name for name in ("089", "091", "hf"))
METHODS = ("direct9", "direct9cached", "old091", "fused091", "constant091")
BENCH_CPU = int(os.environ.get("STPYRAMID_BENCH_CPU", "20"))
sys.path.insert(0, str(ROOT / "codec"))
import joint_next as archive
from stbaseline import slide_redundancy as base, slide_redundancy_scan as scan


def make_spool(census, catalog, spool, skip_constants=False, native_level=0):
    spool.mkdir()
    rows = []
    start = archive.starts(catalog["levels"])[native_level]

    @lru_cache(maxsize=32)
    def read_parent(gid):
        return scan.read_tile(census, gid, catalog=catalog)

    for gid, level, x, y, width, height in base._descriptors(catalog["levels"]):
        if level < native_level:
            continue
        if level > native_level:
            break
        if skip_constants and catalog["tiles"][gid]["constant_rgb"] is not None:
            continue
        desc = (gid, level, x, y, width, height)
        rgb = scan.read_tile(census, gid, catalog=catalog)
        reference, _ = archive.anchor_pixels(catalog["levels"], desc, read_parent)
        local_gid = gid - start
        rgb_name, reference_name = f"{local_gid:06d}.rgb", f"{local_gid:06d}.anchor"
        (spool / rgb_name).write_bytes(rgb.tobytes())
        (spool / reference_name).write_bytes(reference.tobytes())
        rows.append("\t".join(map(str, (local_gid, 0, x, y, width, height, rgb_name, reference_name))))
    (spool / "index.tsv").write_text("".join(row + "\n" for row in rows), encoding="ascii")
    return rows


def child_cpu(process):
    get_times = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessTimes
    get_times.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    get_times.restype = wintypes.BOOL
    times = [wintypes.FILETIME() for _ in range(4)]
    if not get_times(wintypes.HANDLE(int(process._handle)), *(ctypes.byref(value) for value in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return sum((value.dwHighDateTime << 32) | value.dwLowDateTime for value in times[2:]) / 10_000_000


def run_native(exe, mode, spool, output, timeout=240):
    with output.with_suffix(".log").open("xb") as log:
        with subprocess.Popen([str(exe), mode, str(spool), str(output)], stdout=log, stderr=subprocess.STDOUT,
                              creationflags=subprocess.CREATE_NO_WINDOW) as process:
            try:
                status = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            cpu = child_cpu(process)
    if status:
        raise RuntimeError(output.with_suffix(".log").read_text(encoding="utf8", errors="replace"))
    return cpu


def build(source, output, work, method):
    if method not in METHODS:
        raise ValueError("Unknown profile")
    process = psutil.Process()
    process.cpu_affinity([BENCH_CPU])
    process.nice(psutil.IDLE_PRIORITY_CLASS)
    stages = {}
    started = time.perf_counter()
    cpu_start = sum(process.cpu_times()[:2])
    native_cpu = 0.0

    def timed(name, operation):
        tick = time.perf_counter()
        result = operation()
        stages[name] = time.perf_counter() - tick
        return result

    work.mkdir()
    census = work / "census"
    timed("read_source_and_census", lambda: scan.census(source, census))
    catalog = timed("verify_census", lambda: base._catalog(census))
    archive.geometry(catalog["levels"])
    pins = {"089": archive.old._runtime(RUNTIMES[0])[1], "091": archive.cond._runtime(RUNTIMES[1])[1],
            "hf": archive._hf()._runtime(RUNTIMES[2])[1]}
    native = None
    skip_constants = method == "constant091"
    reuse_constant_encodes = method in ("direct9cached", "constant091")
    if method not in ("direct9", "direct9cached"):
        spool = work / "spool"
        rows = timed("prepare_native_inputs", lambda: make_spool(census, catalog, spool, skip_constants))
        if method == "old091":
            p089 = work / "089"
            native_cpu += timed("generate_089_candidates", lambda: run_native(RUNTIMES[0] / "slide_jxl089.exe",
                                                                             "encode", spool, p089))

            def stage_correction_inputs():
                indexed = []
                for gid, row in enumerate(rows):
                    name = f"{gid:06d}.bin"
                    shutil.copyfile(p089 / "anchored" / name, spool / name)
                    indexed.append(row + "\t" + name)
                (spool / "index.tsv").write_text("\n".join(indexed) + "\n", encoding="ascii")

            timed("stage_089_records", stage_correction_inputs)
            exe, mode = RUNTIMES[1] / "slide_jxl091.exe", "encode"
        else:
            exe, mode = ROOT / "bin/slide_jxl091.exe", "encode_direct"
        if rows:
            native = work / "091"
            native_cpu += timed("encode_091", lambda: run_native(exe, mode, spool, native))

    @lru_cache(maxsize=256)
    def encode_constant(canonical_gid):
        return base._encode(scan.read_tile(census, canonical_gid, catalog=catalog))

    def collect_records():
        records = []
        for gid, level, *_ in base._descriptors(catalog["levels"]):
            tile = catalog["tiles"][gid]
            if reuse_constant_encodes and tile["constant_rgb"] is not None:
                records.append((archive.JXL, encode_constant(tile["canonical_gid"])))
            elif level == 0 and native is not None:
                records.append((archive.CONDITIONAL091, (native / f"conditional/{gid:06d}.bin").read_bytes()))
            else:
                records.append((archive.JXL, base._encode(scan.read_tile(census, gid, catalog=catalog))))
        return records

    records = timed("read_091_records_and_encode_direct_levels", collect_records)
    timed("assemble", lambda: archive.write_package(catalog, census, records if native is None else None,
          records, None, None, None, output, "direct" if native is None else "retained", pins))

    def verify():
        with archive.Reader(output, *RUNTIMES) as reader:
            return reader.verify()

    checked = timed("decode_and_verify_complete_package", verify)
    elapsed = time.perf_counter() - started
    cpu = sum(process.cpu_times()[:2]) - cpu_start + native_cpu
    if not checked["ok"]:
        raise ValueError("Package verification failed")
    constant_cache = encode_constant.cache_info()
    return {"method": method, "seconds": elapsed, "cpu_seconds": cpu, "native_cpu_seconds": native_cpu,
            "bytes": sum(file.stat().st_size for file in output.iterdir()), "stages": stages,
            "conditional_records": sum(mode == archive.CONDITIONAL091 for mode, _ in records),
            "constant_direct_records": constant_cache.hits + constant_cache.misses,
            "constant_jxl_encodes": constant_cache.misses,
            "exact_levels": checked["levels"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--method", choices=METHODS, default="constant091")
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, args.work, args.method)))
