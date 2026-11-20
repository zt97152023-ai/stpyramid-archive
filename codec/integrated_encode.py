"""Stage and publish verified Count and native-image archive components."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from threading import Lock
import time


def _json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _write_json(path, value):
    with path.open("xb") as handle:
        handle.write(_json_bytes(value))


def runtime_dirs(root):
    root = Path(root).resolve(strict=True)
    public = ("anchor", "context", "hf-detail")
    legacy = ("089", "091", "hf")
    return tuple(root / (new if (root / new).is_dir() else old)
                 for new, old in zip(public, legacy))


def _configure_worker(cpu, seconds, workers=1):
    import psutil

    if sys.platform != "win32":
        raise RuntimeError("The integrated native runtime adapter requires Windows")
    if not 1 <= seconds <= 86400:
        raise ValueError("The encoding timeout must be within 1–86400 seconds")
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("Native worker count must be between 1 and 8")
    process = psutil.Process()
    allowed = process.cpu_affinity()
    if cpu is not None and cpu not in allowed:
        raise ValueError("CPU is outside the available affinity set")
    if cpu is not None:
        selected = [cpu]
    elif workers > 1:
        utilization = psutil.cpu_percent(interval=0.25, percpu=True)
        selected = sorted(allowed, key=lambda core: (utilization[core], core))[:workers]
    else:
        selected = allowed[-workers:]
    process.cpu_affinity(selected)
    process.nice(psutil.IDLE_PRIORITY_CLASS)
    os.environ["ST262_CPU"] = str(selected[-1])
    os.environ["ST262_DEADLINE_EPOCH"] = str(time.time() + seconds)
    return selected


def write_count_component(counts, metadata, catalog, source_identities, output):
    """Choose a grouped Count variant by actual complete component bytes."""
    import numpy as np
    from stbaseline.comparison import csr_from_h5
    from stbaseline.integrity import h5_digest, sha256
    from stbaseline.native_wsi import _count_preflight, _exact_skeleton
    from stbaseline.grouped_count_encode import write_grouped_candidates

    schema, spot_ids, gene_ids, xy = _count_preflight(counts, "spatial")
    matrix = csr_from_h5(counts)
    output.mkdir()
    _exact_skeleton(counts, output / "science.h5")
    shutil.copyfile(metadata, output / "HEST_metadata.json")
    binding = {
        "method": "Shared_2", "sources": source_identities,
        "expression": dict(schema, path="science.h5", semantic_sha256=h5_digest(counts)),
        "image": {"levels": catalog["levels"]},
        "mapping": {"spatial_key": "spatial", "xy_to_level0": np.eye(3).tolist()},
        "attachments": ["HEST_metadata.json"],
    }

    def identities(paths):
        return {p.name: {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in paths}

    axis = {"spot_ids": spot_ids, "gene_ids": gene_ids, "coordinates": xy}
    with tempfile.TemporaryDirectory(prefix="grouped_count_", dir=output.parent) as stage:
        stage = Path(stage)
        folders = write_grouped_candidates(matrix, axis, stage)
        model = stage / "counts.model"
        common = identities([*output.iterdir(), model])
        variants = []
        for folder in folders:
            members = identities(folder.iterdir())
            files = common | members
            manifest = _json_bytes(dict(binding, files=files))
            variants.append((len(manifest) + sum(f["bytes"] for f in files.values()),
                             folder, members, manifest))
        _, selected, members, manifest = min(variants, key=lambda variant: variant[0])
        for name in members:
            (selected / name).rename(output / name)
        model.rename(output / model.name)
        with (output / "manifest.json").open("xb") as handle:
            handle.write(manifest)


def encode_archive(image, counts, metadata, runtime_dir, output, *, work=None, cpu=None, timeout=14400,
                   on_stage=None, image_mode="maximum", fused_workers=1, maximum_workers=1):
    """Build both modalities, verify the integrated package, and publish the verified directory."""
    if image_mode not in {"maximum", "fused"}:
        raise ValueError("Unknown integrated image mode")
    requested = fused_workers if image_mode == "fused" else maximum_workers
    cpus = _configure_worker(cpu, timeout, requested)
    workers = len(cpus)
    import container_compare as archive
    import joint_next as j
    from archive_image_encode import encode_images
    from st262_controls import remaining_seconds
    from stbaseline import slide_redundancy as base, slide_redundancy_scan as scan
    from stbaseline.integrity import sha256

    inputs = [archive._plain(p) for p in (image, counts, metadata, runtime_dir)]
    image, counts, metadata, runtime = inputs
    sources = {"wsi": image, "h5ad": counts, "metadata": metadata}
    target = archive._fresh(output, *inputs)
    work = archive._fresh(work or target.with_name(target.name + ".work"), *inputs, target)
    if target.drive != work.drive:
        raise ValueError("Work and output must be on the same volume")
    if not all(p.is_file() for p in (image, counts, metadata)):
        raise ValueError("TIFF, H5AD and metadata must be files")
    with metadata.open("rb") as handle:
        if not isinstance(json.load(handle), dict):
            raise ValueError("Metadata must contain one JSON object")
    runtimes = runtime_dirs(runtime)
    from native_adapter import qualified_runtimes
    from hf_adapter import qualified_runtimes as qualified_hf
    qualified_runtimes(runtimes[1], runtimes[0])
    qualified_hf(runtimes[2], runtimes[0])
    work.mkdir(parents=True)
    events = []
    stage_lock = Lock()

    def timed(name, operation):
        remaining_seconds()
        if on_stage is not None:
            with stage_lock:
                on_stage({"begin": name})
        started = time.perf_counter()
        try:
            return operation()
        finally:
            elapsed = time.perf_counter() - started
            with stage_lock:
                events.append({"name": name, "seconds": elapsed})
                if on_stage is not None:
                    on_stage({"end": name, "seconds": elapsed})

    try:
        identities = {key: {"bytes": path.stat().st_size, "sha256": sha256(path)}
                      for key, path in sources.items()}
        census = work / "census"
        timed("native_image_census", lambda: scan.census(image, census))
        catalog = base._catalog(census)
        j.geometry(catalog["levels"])
        count_component = work / "count"
        count_source_identities = {key: identities[key] for key in ("wsi", "h5ad")}
        timed("shared_count_encode", lambda: write_count_component(
            counts, metadata, catalog, count_source_identities, count_component))
        if image_mode == "maximum":
            if on_stage is not None:
                on_stage({"native_workers": workers, "candidate_cpu_affinity": cpus})
            image_packages = encode_images(census, catalog, work, work / "image_packages", runtimes, timed, cpus=cpus)
            image_component = image_packages / "hf_l1_conditional"
        else:
            from pyramid_image_archive import RUNTIMES, encode_native_pyramid
            if runtimes != tuple(path.resolve(strict=True) for path in RUNTIMES):
                raise ValueError("Fused image encoding requires the staged desktop runtimes")
            image_component = work / "fused_image"
            if on_stage is not None:
                on_stage({"native_workers": workers, "candidate_cpu_affinity": cpus})
            timed("fused_image_encode", lambda: encode_native_pyramid(
                image, image_component, work / "fused_work", on_stage=on_stage,
                prepared_census=(census, catalog), workers=workers))
        for key, path in sources.items():
            if path.stat().st_size != identities[key]["bytes"] or sha256(path) != identities[key]["sha256"]:
                raise ValueError("Source input changed during encoding")
        candidate = work / "candidate"
        timed("assemble_complete_archive", lambda: archive.pack_integrated(
            count_component, image_component, candidate, count_kind="Shared_2"))
        with archive.IntegratedArchive(candidate, image_factory=lambda p: j.Reader(p, *runtimes, scan_workers=workers)) as reader:
            result = timed("verify_counts_pixels_and_files", reader.verify)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)
        candidate.rename(target)
        receipt = {"ok": True, "archive": str(target), "verification": result, "stages": events}
        _write_json(work / "COMPLETE.json", receipt)
        return receipt
    except BaseException as error:
        _write_json(work / "FAILURE.json", {"error": str(error), "stages": events})
        raise
