"""Encode, verify or restore one complete native-pyramid image archive."""
import argparse
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import time

from encode_image import BENCH_CPU, ROOT, RUNTIMES, archive, base, make_spool, psutil, run_native, scan

def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_info(path):
    return {member.name: {"bytes": member.stat().st_size, "sha256": digest(member)}
            for member in sorted(path.iterdir()) if member.is_file()}


def select_process_resources():
    process = psutil.Process()
    process.cpu_affinity([BENCH_CPU])
    process.nice(psutil.IDLE_PRIORITY_CLASS)
    return process


def runtime_pins():
    return {"089": archive.old._runtime(RUNTIMES[0])[1], "091": archive.cond._runtime(RUNTIMES[1])[1],
            "hf": archive._hf()._runtime(RUNTIMES[2])[1]}


def collect_records(census, catalog, conditional_records):
    @lru_cache(maxsize=256)
    def encode_constant(canonical_gid):
        return base._encode(scan.read_tile(census, canonical_gid, catalog=catalog))

    records = []
    for tile in catalog["tiles"]:
        gid = tile["gid"]
        if gid in conditional_records:
            records.append((archive.CONDITIONAL091, conditional_records[gid].read_bytes()))
        elif tile["constant_rgb"] is not None:
            records.append((archive.JXL, encode_constant(tile["canonical_gid"])))
        else:
            records.append((archive.JXL, base._encode(scan.read_tile(census, gid, catalog=catalog))))
    return records, encode_constant.cache_info()._asdict()


def encode_fused(census, catalog, work, package, timed):
    if len(catalog["levels"]) < 3:
        raise ValueError("L0/L1 conditional encoding requires three or more native levels")
    pins = timed("pin_runtimes", runtime_pins)
    starts = archive.starts(catalog["levels"])
    conditional_records = {}
    native_cpu = 0.0
    for level in (0, 1):
        spool = work / f"spool_L{level}"
        rows = timed(f"prepare_native_L{level}",
                     lambda: make_spool(census, catalog, spool, skip_constants=True, native_level=level))
        if not rows:
            continue
        native = work / f"fused_L{level}"
        native_cpu += timed(f"encode_fused_L{level}", lambda: run_native(
            ROOT / "bin/slide_jxl091.exe", "encode_direct", spool, native, timeout=7200
        ))
        for gid in range(starts[level], starts[level + 1]):
            if catalog["tiles"][gid]["constant_rgb"] is None:
                conditional_records[gid] = native / "conditional" / f"{gid - starts[level]:06d}.bin"
    records, cache = timed("collect_conditional_and_encode_direct_tiles",
                           lambda: collect_records(census, catalog, conditional_records))
    timed("assemble_image_package", lambda: archive.write_package(
        catalog, census, None, records, None, None, None, package, "l1_conditional", pins
    ))
    return cache, native_cpu


def build(source, package, work, method="fused_l0_l1", on_stage=None):
    process = psutil.Process()
    if method not in {"direct9", "fused_l0_l1"}:
        raise ValueError("Unknown image encoder")
    if package.exists() or work.exists():
        raise FileExistsError("Image package and work directory must be fresh paths")
    work.mkdir()
    census = work / "census"
    stages = {}
    started = time.perf_counter()
    cpu_started = sum(process.cpu_times()[:2])

    def timed(name, operation):
        if on_stage is not None:
            on_stage({"arm": method, "begin": name})
        tick = time.perf_counter()
        result = operation()
        stages[name] = time.perf_counter() - tick
        if on_stage is not None:
            on_stage({"arm": method, "end": name, "seconds": stages[name]})
        return result

    timed("read_source_and_census", lambda: scan.census(source, census))
    catalog = timed("verify_census", lambda: base._catalog(census))
    native_cpu = None
    if method == "direct9":
        pins = timed("pin_runtimes", runtime_pins)
        records, constant_cache = timed("encode_direct_tiles", lambda: collect_records(census, catalog, {}))
        timed("assemble_image_package", lambda: archive.write_package(catalog, census, records, records, None,
              None, None, package, "direct", pins))
    else:
        constant_cache, native_cpu = encode_fused(census, catalog, work, package, timed)
    result = {
        "method": method, "threads": 1,
        "encode_seconds": time.perf_counter() - started,
        "encoder_python_cpu_seconds": sum(process.cpu_times()[:2]) - cpu_started,
        "encoder_fused_native_cpu_seconds": native_cpu,
        "stages": stages,
        "constant_cache": constant_cache,
        "package": str(package), "files": file_info(package),
        "native_level_sha256": [level["pixel_sha256"] for level in catalog["levels"]],
    }
    result["bytes"] = sum(info["bytes"] for info in result["files"].values())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    encode = commands.add_parser("encode")
    encode.add_argument("source", type=Path)
    encode.add_argument("archive", type=Path)
    encode.add_argument("--work", type=Path, required=True)
    for command in ("verify", "restore", "roi"):
        sub = commands.add_parser(command)
        sub.add_argument("archive", type=Path)
        if command in ("restore", "roi"):
            sub.add_argument("output", type=Path)
        if command == "roi":
            sub.add_argument("level", type=int)
            sub.add_argument("box", type=int, nargs=4, metavar=("X0", "Y0", "X1", "Y1"))
    args = parser.parse_args()
    if args.command == "encode":
        print(json.dumps(build(args.source, args.archive, args.work), indent=2))
        return
    with archive.Reader(args.archive, *RUNTIMES) as reader:
        if args.command == "verify":
            print(json.dumps(reader.verify(), indent=2))
        elif args.command == "restore":
            print(reader.restore(args.output))
        else:
            import tifffile
            pixels = reader.image(args.level, args.box)
            with args.output.open("xb") as stream:
                tifffile.imwrite(stream, pixels, photometric="rgb", metadata=None)


if __name__ == "__main__":
    main()
