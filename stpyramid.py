"""Command-line entry for lossless WSI and spatial Count archives."""
import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "codec"))


def json_new(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        handle.write("\n")


def runtime_dirs(root):
    root = Path(root).resolve(strict=True)
    return tuple(root / key for key in ("089", "091", "hf"))


def set_resources(cpu, seconds):
    import psutil

    if sys.platform != "win32":
        raise RuntimeError("The integrated native runtime adapter requires Windows")
    if not 1 <= seconds <= 86400:
        raise ValueError("The encoding timeout must be within 1–86400 seconds")
    process = psutil.Process()
    allowed = process.cpu_affinity()
    if cpu is None:
        cpu = allowed[-1]
    if cpu not in allowed:
        raise ValueError("CPU is outside the available affinity set")
    process.cpu_affinity([cpu])
    process.nice(psutil.IDLE_PRIORITY_CLASS)
    os.environ["ST262_CPU"] = str(cpu)
    os.environ["ST262_DEADLINE_EPOCH"] = str(time.time() + seconds)


def make_count(counts, metadata, catalog, identity, output):
    import numpy as np
    from stbaseline.comparison import csr_from_h5
    from stbaseline.integrity import h5_digest, sha256
    from stbaseline.native_wsi import _count_preflight, _exact_skeleton
    from stbaseline.shared_backend import write_count_store

    schema, rows, genes, xy = _count_preflight(counts, "spatial")
    matrix = csr_from_h5(counts)
    output.mkdir()
    _exact_skeleton(counts, output / "science.h5")
    shutil.copyfile(metadata, output / "HEST_metadata.json")
    write_count_store(matrix, {"spot_ids": rows, "gene_ids": genes, "coordinates": xy, "level0_xy": xy},
                      xy, output, "Shared_local", 1024)
    files = {p.relative_to(output).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)}
             for p in sorted(output.rglob("*")) if p.is_file()}
    json_new(output / "manifest.json", {
        "method": "Shared_local", "sources": identity,
        "expression": dict(schema, path="science.h5", semantic_sha256=h5_digest(counts)),
        "image": {"levels": catalog["levels"]},
        "mapping": {"spatial_key": "spatial", "xy_to_level0": np.eye(3).tolist()},
        "attachments": ["HEST_metadata.json"], "files": files,
    })


def encode(args):
    set_resources(args.cpu, args.timeout)
    import container_compare as archive
    import joint_next as j
    from archive_image_encode import encode_images
    from st262_controls import remaining_seconds
    from stbaseline import slide_redundancy as base, slide_redundancy_scan as scan
    from stbaseline.integrity import sha256

    inputs = [archive._plain(p) for p in (args.image, args.counts, args.metadata, args.runtime_dir)]
    image, counts, metadata, runtime = inputs
    sources = {"wsi": image, "h5ad": counts, "metadata": metadata}
    target = archive._fresh(args.output, *inputs)
    work = archive._fresh(args.work or target.with_name(target.name + ".work"), *inputs, target)
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

    def timed(name, operation):
        remaining_seconds()
        started = time.perf_counter()
        try:
            return operation()
        finally:
            events.append({"name": name, "seconds": time.perf_counter() - started})

    try:
        identities = {key: {"bytes": path.stat().st_size, "sha256": sha256(path)}
                      for key, path in sources.items()}
        census = work / "census"
        timed("native_image_census", lambda: scan.census(image, census))
        catalog = base._catalog(census)
        j.geometry(catalog["levels"])
        component = work / "count"
        timed("shared_count_encode", lambda: make_count(counts, metadata, catalog,
              {key: identities[key] for key in ("wsi", "h5ad")}, component))
        images = encode_images(census, catalog, work, work / "image_packages", runtimes, timed)
        for key, path in sources.items():
            if path.stat().st_size != identities[key]["bytes"] or sha256(path) != identities[key]["sha256"]:
                raise ValueError("Source input changed during encoding")
        candidate = work / "candidate"
        timed("assemble_complete_archive", lambda: archive.pack_integrated(
              component, images / "hf_l1_conditional", candidate, count_kind="Shared_local"))
        with archive.IntegratedArchive(candidate, image_factory=lambda p: j.Reader(p, *runtimes)) as reader:
            result = timed("verify_counts_pixels_and_files", reader.verify)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)
        candidate.rename(target)
        receipt = {"ok": True, "archive": str(target), "verification": result, "stages": events}
        json_new(work / "COMPLETE.json", receipt)
        return receipt
    except BaseException as error:
        json_new(work / "FAILURE.json", {"error": str(error), "stages": events})
        raise


def read_archive(args):
    import container_compare as archive
    import joint_next as j

    factory = None if args.runtime_dir is None else lambda p: j.Reader(p, *runtime_dirs(args.runtime_dir))
    with archive.IntegratedArchive(args.archive, image_factory=factory) as reader:
        if args.command == "inspect":
            return reader.ledger()
        if args.command == "verify":
            return reader.verify_files() if args.files_only else reader.verify()
        if args.command == "restore":
            return {"restored": reader.restore(args.output)}
        import numpy as np

        path = archive._fresh(args.output, reader.root)
        if args.command == "image":
            pixels = reader.image(args.level, args.bbox)
            with path.open("xb") as handle:
                np.save(handle, pixels, allow_pickle=False)
        elif args.command == "counts":
            values = reader.counts(rows=args.rows, genes=args.genes)
            with path.open("xb") as handle:
                np.savez(handle, **values)
        return {"output": str(path)}


def main():
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                "NUMBA_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS"):
        os.environ[key] = "1"
    os.environ.pop("ST074_S", None)
    os.environ.pop("ST074_T", None)
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    builder = subs.add_parser("encode")
    for key in ("image", "counts", "metadata", "runtime-dir", "output"):
        builder.add_argument("--" + key, type=Path, required=True)
    builder.add_argument("--work", type=Path)
    builder.add_argument("--cpu", type=int)
    builder.add_argument("--timeout", type=int, default=14400, help="Encoding budget in seconds")
    for action in ("inspect", "verify", "restore", "image", "counts"):
        item = subs.add_parser(action)
        item.add_argument("archive", type=Path)
        item.add_argument("--runtime-dir", type=Path)
        if action in ("restore", "image", "counts"):
            item.add_argument("--output", type=Path, required=True)
        if action == "image":
            item.add_argument("--level", type=int, required=True)
            item.add_argument("--bbox", type=int, nargs=4, required=True)
        if action == "counts":
            item.add_argument("--rows", type=int, nargs="+")
            item.add_argument("--genes", type=int, nargs="+")
        if action == "verify":
            item.add_argument("--files-only", action="store_true")
    args = parser.parse_args()
    try:
        with redirect_stdout(sys.stderr):
            result = encode(args) if args.command == "encode" else read_archive(args)
        print(json.dumps(result, ensure_ascii=True))
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
