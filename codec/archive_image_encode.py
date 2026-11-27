"""Build and select complete image records for the integrated archive."""
import copy
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
import json
import os
from pathlib import Path
import subprocess
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hf_adapter as hf
import joint_next as archive
import l1_adapter
import native_adapter as native
from stbaseline import slide_redundancy as base, slide_redundancy_scan as scan
from stbaseline import wsi_squeeze089 as codec089
from stbaseline.integrity import write_json
from receipt_bounds import install

install(codec089, native.conditioned)


def _encode_direct_records(census, catalog, directory, workers=1):
    def encode(gid):
        return base._encode(scan.read_tile(census, gid, catalog=catalog))

    encoded = {}
    if workers > 1:
        canonical = (tile["gid"] for tile in catalog["tiles"] if tile["gid"] == tile["canonical_gid"])
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while batch := list(islice(canonical, workers * 4)):
                encoded.update(zip(batch, pool.map(encode, batch), strict=True))
    records = []
    for tile in catalog["tiles"]:
        gid = tile["gid"]
        blob = encoded[tile["canonical_gid"]] if workers > 1 else encode(gid)
        records.append((archive.JXL, blob))
        (directory / f"{gid:06d}.bin").write_bytes(blob)
    return records


def _write_anchored_tile_inputs(census, catalog, level, stage, spool):
    def read_tile(gid):
        return scan.read_tile(census, gid, catalog=catalog)

    lines, mappings, anchor_hashes = [], [], {}
    descriptors = (desc for desc in base._descriptors(catalog["levels"]) if desc[1] == level)
    for record_id, desc in enumerate(descriptors):
        gid, _, x, y, width, height = desc
        rgb_name = f"{record_id:06d}.rgb"
        anchor_name = f"{record_id:06d}.anchor"
        rgb = read_tile(gid)
        anchor, parent_id = archive.anchor_pixels(catalog["levels"], desc, read_tile)
        (spool / rgb_name).write_bytes(rgb.tobytes())
        anchor_rgb = anchor.tobytes()
        (spool / anchor_name).write_bytes(anchor_rgb)
        anchor_hashes[record_id] = archive.digest(anchor_rgb)
        lines.append(f"{record_id}\t0\t{x}\t{y}\t{width}\t{height}\t{rgb_name}\t{anchor_name}")
        mappings.append({"global_gid": gid, "local_gid": record_id, "level": level,
                         "abi_level": 0, "parent_global_gid": parent_id})
    (spool / "index.tsv").write_text("\n".join(lines) + "\n", encoding="ascii")
    write_json(stage / "mappings.json", mappings)
    return anchor_hashes


def _encode_anchored_level(census, catalog, level, work, runtime089, timed):
    stage = work / f"P089_L{level}"
    spool = stage / "spool"
    spool.mkdir(parents=True)
    records = stage / "native"
    anchor_hashes = timed(f"P089_L{level}_source_spooling", lambda: _write_anchored_tile_inputs(
        census, catalog, level, stage, spool))
    timed(f"P089_L{level}_native_all_candidates", lambda: native.native_encode(
        runtime089, "089", spool, records))

    # Both native inputs expose their finest encoded level as ABI level 0.
    virtual_levels = copy.deepcopy(catalog["levels"][level:])
    for local_level, info in enumerate(virtual_levels):
        info["level"] = local_level
    start = archive.starts(catalog["levels"])[level]
    virtual_catalog = {"levels": virtual_levels,
                       "tiles": [{"native_sha256": tile["native_sha256"]} for tile in catalog["tiles"][start:]]}
    timed(f"P089_L{level}_native_receipt_validation", lambda: codec089._validate_native(
        records, virtual_catalog, list(base._descriptors(virtual_levels)), anchor_hashes))
    return records


def _select_retained_records(catalog, direct_records, anchored_dirs, level0_conditioned_dir):
    retained = list(direct_records)
    level_start = archive.starts(catalog["levels"])
    for gid, level, *_ in base._descriptors(catalog["levels"]):
        if level > 1:
            continue
        filename = f"{gid - level_start[level]:06d}.bin"
        anchored = (anchored_dirs[level] / "anchored" / filename).read_bytes()
        selected = archive.choose(retained[gid], [(archive.ANCHORED089, anchored)])
        if level == 0:
            conditioned = (level0_conditioned_dir / "conditional" / filename).read_bytes()
            selected = archive.choose(selected, [(archive.CONDITIONAL091, conditioned)])
        retained[gid] = selected
    return retained


def _candidate_child(kind, cpu, *args):
    import psutil
    from st262_controls import remaining_seconds

    process = psutil.Process()
    process.cpu_affinity([int(cpu)])
    process.nice(psutil.IDLE_PRIORITY_CLASS)
    os.environ["ST262_CPU"] = str(cpu)
    remaining_seconds()
    if kind in ("P089_L0", "P089_L1"):
        census, work, runtime = map(Path, args)
        _encode_anchored_level(census, base._catalog(census), int(kind[-1]), work, runtime,
                               lambda _name, operation: operation())
    else:
        builders = {"L0_091": native.build_native, "L0_HF": hf.build_native, "L1_091": l1_adapter.build_native}
        builders[kind](*map(Path, args))


def _run_candidate(kind, cpu, log_dir, *args):
    from st262_controls import remaining_seconds

    remaining_seconds()
    path = log_dir / f"{kind}.candidate.log"
    command = [sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()), kind, str(cpu), *map(str, args)]
    with path.open("xb") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(f"{kind} failed: {path.read_text(encoding='utf-8', errors='replace')[-3000:]}")


def _prepare_inputs(census, catalog, work, runtime089, direct_dir, timed, cpus):
    direct_name = "direct_JXL9_all_tiles_including_immediate_decode_check"
    if len(cpus) < 2:
        records = timed(direct_name, lambda: _encode_direct_records(census, catalog, direct_dir))
        return records, [_encode_anchored_level(census, catalog, level, work, runtime089, timed) for level in (0, 1)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        direct = pool.submit(timed, direct_name, lambda: _encode_direct_records(
            census, catalog, direct_dir, max(2, len(cpus) - 2)))
        anchored = []
        for level in (0, 1):
            kind = f"P089_L{level}"
            anchored.append(pool.submit(timed, kind + "_candidate_encode_and_checks", lambda kind=kind, level=level:
                _run_candidate(kind, cpus[level], work, census, work, runtime089)))
        for job in anchored:
            job.result()
        return direct.result(), [work / f"P089_L{level}" / "native" for level in (0, 1)]


def _encode_candidate_banks(census, work, runtimes, correction_source, level1_anchored, timed, cpus):
    runtime089, runtime091, runtime_hf = runtimes
    jobs = [
        ("L0_091", native.build_native, correction_source, work / "prior091", runtime091),
        ("L0_HF", hf.build_native, correction_source, work / "hf", runtime_hf),
        ("L1_091", l1_adapter.build_native, level1_anchored, work / "l1", runtime091),
    ]

    def encode(index, job):
        kind, builder, dependency, output, runtime = job
        args = (census, dependency, output, runtime, runtime089)
        if len(cpus) < 2:
            return timed(kind + "_all_candidate_adapter_including_checks", lambda: builder(*args))
        timed(kind + "_all_candidate_adapter_including_checks",
              lambda: _run_candidate(kind, cpus[index % len(cpus)], work, *args))
        with (output / "adapter.json").open(encoding="utf-8") as handle:
            return json.load(handle)

    if len(cpus) < 2:
        return [encode(index, job) for index, job in enumerate(jobs)]
    with ThreadPoolExecutor(max_workers=min(len(cpus), len(jobs))) as pool:
        return list(pool.map(encode, range(len(jobs)), jobs))


def encode_images(census, catalog, work, output, runtimes, timed, *, cpus=()):
    """Produce the direct baseline and the selected HF + L1 image component."""
    runtime089, runtime091, runtime_hf = runtimes
    direct_dir = work / "direct_records"
    direct_dir.mkdir()
    direct_records, anchored_dirs = _prepare_inputs(census, catalog, work, runtime089, direct_dir, timed, cpus)
    level0_anchored, level1_anchored = anchored_dirs
    direct_dependency, correction_source = work / "old_direct", work / "anchor"
    timed("P089_direct_dependency_package_assembly", lambda: codec089._assemble(
        catalog, census, direct_dir, level0_anchored, direct_dependency, "direct", codec089._runtime(runtime089)[1], {}))
    timed("P089_correction_source_package_assembly", lambda: codec089._assemble(
        catalog, census, direct_dir, level0_anchored, correction_source, "anchor_pure",
        codec089._runtime(runtime089)[1], base._members(direct_dependency)))

    level0_conditioned, level0_hf, level1_conditioned = _encode_candidate_banks(
        census, work, runtimes, correction_source, level1_anchored, timed, cpus)
    retained = timed("retained_089_091_strict_paid_record_selection", lambda: _select_retained_records(
        catalog, direct_records, anchored_dirs, Path(level0_conditioned["native_dir"])))

    hf_dir = Path(level0_hf["native_dir"])
    hf_records = {tile["gid"]: (archive.HF, (hf_dir / tile["ms_path"]).read_bytes())
                  for tile in level0_hf["native_receipt"]["tiles"]}
    pins = dict(level1_conditioned["runtime_pins"], hf=hf._runtime(runtime_hf)[1])
    output.mkdir()
    choices = {}
    for profile in ("direct", "hf_l1_conditional"):
        choices[profile] = timed(profile + "_selection_and_image_package_assembly",
            lambda profile=profile: archive.write_package(
                catalog, census, direct_records, retained, hf_records, Path(level1_conditioned["native_dir"]),
                level1_anchored, output / profile, profile, pins))
    _, level1_start, level2_start, *_ = archive.starts(catalog["levels"])
    write_json(work / "candidate_accounting.json", {
        "all_tiles_direct_encoded": len(direct_records), "L0_tiles": level1_start,
        "L1_tiles": level2_start - level1_start, "choices": choices,
        "adapter_timings": {"091_L0": level0_conditioned["timing"], "HF_L0": level0_hf["timing"],
                            "091_L1": level1_conditioned["timing"]},
        "scope": "Every source-derived candidate was generated in this job. Adapter wording about historical inputs describes its local dependency; those dependencies were freshly encoded and charged above. No reused NCBI466 streams.",
    })
    return output


if __name__ == "__main__":
    _candidate_child(*sys.argv[1:])
