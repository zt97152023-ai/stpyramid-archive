"""Experimental whole-level recursive image encoder and reader."""
import argparse
from functools import lru_cache
import json
from pathlib import Path
import time

from native_tile_encoder import RUNTIMES, archive, base, run_native_encoder, scan, write_tile_inputs
from pyramid_image_archive import _decoder_runtime_pins, archive_members
from joint_recursive import PRESETS, PyramidLayout, Reader, write_archive


def build(source, output, work, native_encoder, *, preset="fast", max_depth=15):
    source, output, work, native_encoder = map(archive._plain, (source, output, work, native_encoder))
    if (output.exists() or work.exists() or output.drive != work.drive
            or output == work or output in work.parents or work in output.parents
            or output in source.parents or work in source.parents):
        raise ValueError("Fresh disjoint work and archive paths on the same volume required")
    if not output.parent.is_dir() or not work.parent.is_dir() or not native_encoder.is_file():
        raise FileNotFoundError("Existing output parent, work parent and experimental encoder required")
    mode = PRESETS[preset]
    work.mkdir()
    census, staged = work / "census", work / "archive"
    elapsed = {}
    started = time.perf_counter()

    def timed(name, operation):
        tick = time.perf_counter()
        result = operation()
        elapsed[name] = time.perf_counter() - tick
        return result

    timed("scan_source", lambda: scan.census(source, census))
    catalog = timed("verify_census", lambda: base._catalog(census))
    layout = PyramidLayout(catalog["levels"], max_depth)
    native_cpu = 0.0
    native_records = {}
    # Process conditional levels from coarse to fine. Each predictor uses the
    # source parent RGB that must be reconstructed exactly before publication.
    for level in sorted(layout.dependent, reverse=True):
        spool = work / f"spool_L{level}"
        encoded = work / f"native_L{level}"
        inputs = timed(f"prepare_L{level}", lambda: write_tile_inputs(
            census, catalog, spool, level, include_constant_tiles=False, parent_geometry=layout.parent))
        if inputs:
            native_cpu += timed(f"encode_L{level}", lambda: run_native_encoder(native_encoder, mode, spool, encoded, timeout=7200))
            for gid in inputs:
                native_records[gid] = encoded / "conditional" / f"{gid - layout.starts[level]:06d}.bin"

    @lru_cache(maxsize=256)
    def encode_constant(gid):
        return base._encode(scan.read_tile(census, gid, catalog=catalog))

    def record(descriptor):
        gid = descriptor[0]
        if gid in native_records:
            return archive.CONDITIONAL091, native_records[gid].read_bytes()
        tile = catalog["tiles"][gid]
        blob = (base._encode(scan.read_tile(census, gid, catalog=catalog)) if tile["constant_rgb"] is None
                else encode_constant(tile["canonical_gid"]))
        return archive.JXL, blob

    pins = timed("pin_decoder_runtimes", _decoder_runtime_pins)
    level_stats = timed("encode_direct_and_assemble", lambda: write_archive(
        catalog, census, staged, layout, record, pins, max_depth=max_depth, preset=preset))
    encoded = time.perf_counter() - started
    with Reader(staged, *RUNTIMES) as reader:
        check = timed("full_lossless_verification", reader.verify)
        decode_metrics = reader.metrics()
    staged.rename(output)
    return {"preset": preset, "max_reference_depth": max_depth, "referenced_levels": sorted(layout.dependent),
            "encode_seconds": encoded, "publish_seconds": time.perf_counter() - started,
            "native_encoder_cpu_seconds": native_cpu, "stages": elapsed, "per_level": level_stats,
            "ledger": check["ledger"], "verify": check, "verify_metrics": decode_metrics,
            "members": archive_members(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    encode = sub.add_parser("encode")
    encode.add_argument("source", type=Path)
    encode.add_argument("archive", type=Path)
    encode.add_argument("--work", required=True, type=Path)
    encode.add_argument("--native-encoder", required=True, type=Path)
    encode.add_argument("--preset", choices=PRESETS, default="fast")
    encode.add_argument("--max-depth", type=int, default=15)
    verify = sub.add_parser("verify")
    verify.add_argument("archive", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("archive", type=Path)
    restore.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "encode":
        print(json.dumps(build(args.source, args.archive, args.work, args.native_encoder,
                               preset=args.preset, max_depth=args.max_depth), indent=2))
    else:
        with Reader(args.archive, *RUNTIMES) as reader:
            result = reader.verify() if args.command == "verify" else {"restored": reader.restore(args.output)}
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
