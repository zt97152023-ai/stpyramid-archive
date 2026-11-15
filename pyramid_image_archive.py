"""Encode, verify, read regions from, or restore a native image pyramid."""
import argparse
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import time

from native_tile_encoder import ROOT, RUNTIMES, archive, base, run_native_encoder, scan, write_tile_inputs

import psutil


def file_sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def archive_members(path):
    return {member.name: {"bytes": member.stat().st_size, "sha256": file_sha256(member)}
            for member in sorted(path.iterdir()) if member.is_file()}


def _decoder_runtime_pins():
    return {"089": archive.old._runtime(RUNTIMES[0])[1], "091": archive.cond._runtime(RUNTIMES[1])[1],
            "hf": archive._hf()._runtime(RUNTIMES[2])[1]}


def _collect_tile_records(census, catalog, conditional_record_paths):
    @lru_cache(maxsize=256)
    def encode_constant_jxl(canonical_gid):
        return base._encode(scan.read_tile(census, canonical_gid, catalog=catalog))

    records = []
    for tile in catalog["tiles"]:
        gid = tile["gid"]
        if gid in conditional_record_paths:
            records.append((archive.CONDITIONAL091, conditional_record_paths[gid].read_bytes()))
        elif tile["constant_rgb"] is not None:
            records.append((archive.JXL, encode_constant_jxl(tile["canonical_gid"])))
        else:
            records.append((archive.JXL, base._encode(scan.read_tile(census, gid, catalog=catalog))))
    return records, encode_constant_jxl.cache_info()._asdict()


def _encode_parent_conditioned_tiles(census, catalog, work, timed):
    starts = archive.starts(catalog["levels"])
    record_paths = {}
    native_cpu = 0.0
    for level in (0, 1):
        input_dir = work / f"spool_L{level}"
        inputs = timed(f"prepare_native_L{level}", lambda: write_tile_inputs(
            census, catalog, input_dir, level, include_constant_tiles=False
        ))
        if not inputs:
            continue
        output_dir = work / f"fused_L{level}"
        native_cpu += timed(f"encode_fused_L{level}", lambda: run_native_encoder(
            ROOT / "bin/slide_jxl091.exe", "encode_direct", input_dir, output_dir, timeout=7200
        ))
        for gid in inputs:
            record_paths[gid] = output_dir / "conditional" / f"{gid - starts[level]:06d}.bin"
    return record_paths, native_cpu


def encode_native_pyramid(source, package, work, method="fused_l0_l1", on_stage=None):
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
    archive.geometry(catalog["levels"])
    pins = timed("pin_runtimes", _decoder_runtime_pins)
    if method == "direct9":
        conditional_record_paths, native_cpu = {}, None
        record_stage = "encode_direct_tiles"
    else:
        conditional_record_paths, native_cpu = _encode_parent_conditioned_tiles(census, catalog, work, timed)
        record_stage = "collect_conditional_and_encode_direct_tiles"
    records, constant_cache = timed(record_stage, lambda: _collect_tile_records(
        census, catalog, conditional_record_paths
    ))
    is_direct = method == "direct9"
    # The existing container uses this profile for fixed records with L1 parent links.
    profile = "direct" if is_direct else "l1_conditional"
    timed("assemble_image_package", lambda: archive.write_package(
        catalog, census, records if is_direct else None, records, None, None, None, package, profile, pins
    ))
    result = {
        "method": method, "threads": 1,
        "encode_seconds": time.perf_counter() - started,
        "encoder_python_cpu_seconds": sum(process.cpu_times()[:2]) - cpu_started,
        "encoder_fused_native_cpu_seconds": native_cpu,
        "stages": stages,
        "constant_cache": constant_cache,
        "package": str(package), "files": archive_members(package),
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
        print(json.dumps(encode_native_pyramid(args.source, args.archive, args.work), indent=2))
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
