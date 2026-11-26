"""Encode, verify, read regions from, or restore a native image pyramid."""
import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import time

from native_tile_encoder import (ROOT, RUNTIMES, archive, base, run_census_encoder, run_native_encoder,
                                 scan, supports_census_input, write_census_batches, write_tile_batches)
from integrated_encode import MAX_FUSED_WORKERS

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


def _collect_tile_records(census, catalog, conditional_record_paths, workers, *, coarse_effort=9):
    def encode_jxl(gid, effort):
        return base._encode(scan.read_tile(census, gid, catalog=catalog), effort=effort)

    @lru_cache(maxsize=256)
    def encode_constant_jxl(canonical_gid, effort):
        return encode_jxl(canonical_gid, effort)

    records = []
    pending = deque()

    def collect_jxl():
        index, result = pending.popleft()
        records[index] = archive.JXL, result.result()

    with (ThreadPoolExecutor(max_workers=workers) if workers > 1 else nullcontext()) as pool:
        for tile in catalog["tiles"]:
            gid = tile["gid"]
            effort = coarse_effort if tile["level"] >= 2 else 9
            if gid in conditional_record_paths:
                records.append((archive.CONDITIONAL091, conditional_record_paths[gid].read_bytes()))
            elif tile["constant_rgb"] is not None:
                records.append((archive.JXL, encode_constant_jxl(tile["canonical_gid"], effort)))
            elif pool is None:
                records.append((archive.JXL, encode_jxl(gid, effort)))
            else:
                pending.append((len(records), pool.submit(encode_jxl, gid, effort)))
                records.append(None)
                if len(pending) == workers * 2:
                    collect_jxl()
        while pending:
            collect_jxl()
    return records, encode_constant_jxl.cache_info()._asdict()


def _encode_parent_conditioned_tiles(census, catalog, work, timed, workers):
    starts = archive.starts(catalog["levels"])
    record_paths = {}
    pending = deque()
    native_cpu = 0.
    executable = ROOT / "bin/slide_jxl091.exe"
    source = census / "tiles.rgb"
    shared_input = supports_census_input(executable, source, work)

    def encode_partition(tile_input, output):
        if shared_input:
            return run_census_encoder(executable, tile_input, source, output, timeout=7200)
        return run_native_encoder(executable, "encode_direct", tile_input, output, timeout=7200)

    def stage_level(pool, level):
        nonlocal native_cpu
        tile_count = starts[level + 1] - starts[level]
        target_batches = workers * 3 if workers > 1 else 1
        batch_tiles = max(32, (tile_count + target_batches - 1) // target_batches)
        output_dir = work / f"fused_L{level}"
        output_dir.mkdir()
        if shared_input:
            batches = write_census_batches(catalog, work / f"plans_L{level}", level, batch_tiles)
        else:
            batches = write_tile_batches(census, catalog, work / f"spool_L{level}", level, batch_tiles)
        with closing(batches):
            for tile_input, gids in batches:
                output = output_dir / tile_input.stem
                pending.append(pool.submit(encode_partition, tile_input, output))
                for gid in gids:
                    record_paths[gid] = output / "conditional" / f"{gid - starts[level]:06d}.bin"
                if len(pending) == 2 * workers:
                    native_cpu += pending.popleft().result()

    # Both levels use original parent pixels from the census. Submit each staged
    # batch immediately and keep native children within the shared worker budget.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for level in (0, 1):
            timed(f"prepare_native_L{level}", lambda: stage_level(pool, level))
        native_cpu += timed("finish_parent_conditioned_batches", lambda: sum(job.result() for job in pending))
    return record_paths, native_cpu, "shared_census" if shared_input else "staged_rgb"


def encode_native_pyramid(source, package, work, method="fused_l0_l1", on_stage=None, *, prepared_census=None,
                          workers=1):
    process = psutil.Process()
    if method not in {"direct9", "fused_l0_l1"}:
        raise ValueError("Unknown image encoder")
    if type(workers) is not int or not 1 <= workers <= MAX_FUSED_WORKERS:
        raise ValueError(f"Native worker count must be between 1 and {MAX_FUSED_WORKERS}")
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

    if prepared_census is None:
        timed("read_source_and_census", lambda: scan.census(source, census, include_diagnostics=False))
        catalog = timed("verify_census", lambda: base._catalog(census))
    else:
        census, catalog = prepared_census
    archive.geometry(catalog["levels"])
    pins = timed("pin_runtimes", _decoder_runtime_pins)
    if method == "direct9":
        conditional_record_paths, native_cpu = {}, None
        parent_input = None
        record_stage = "encode_direct_tiles"
    else:
        conditional_record_paths, native_cpu, parent_input = timed("parent_conditioned_batches",
            lambda: _encode_parent_conditioned_tiles(census, catalog, work, timed, workers))
        record_stage = "collect_conditional_and_encode_direct_tiles"
    records, constant_cache = timed(record_stage, lambda: _collect_tile_records(
        census, catalog, conditional_record_paths, workers, coarse_effort=9 if method == "direct9" else 3
    ))
    is_direct = method == "direct9"
    timed("assemble_image_package", lambda: archive.write_tile_archive(
        catalog, census, records, package, pins, direct_only=is_direct
    ))
    result = {
        "method": method, "threads": workers,
        "encode_seconds": time.perf_counter() - started,
        "encoder_python_cpu_seconds": sum(process.cpu_times()[:2]) - cpu_started,
        "encoder_fused_native_cpu_seconds": native_cpu,
        "parent_conditioned_input": parent_input,
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
