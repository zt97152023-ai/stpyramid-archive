"""Run one desktop archive job in a separate process and stream stage events."""
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime-windows-source"
sys.path[:0] = [str(ROOT / "codec"), str(ROOT)]
from desktop_i18n import translate
from fetch_count_runtime import count_source_ready, fetch_count_source


def prepare_count_runtime(language, emit):
    if count_source_ready():
        return
    if (ROOT.parent / "OFFLINE").is_file():
        raise RuntimeError("The packaged Count runtime is missing or differs from its pinned source. Re-extract the application.")
    stage = "prepare_count_runtime"
    emit({"begin": stage})
    started = time.perf_counter()
    try:
        fetch_count_source()
    except (OSError, ValueError) as exc:
        raise RuntimeError(translate(language,
            "Could not download the pinned Count runtime. Check your internet connection, then start the job again. {error}",
            error=exc)) from exc
    emit({"end": stage, "seconds": time.perf_counter() - started})


def encode_job(job, emit):
    from container_compare import _fresh, _plain

    language = job.get("language", "en")
    image = _plain(job["image"])
    if not image.is_file():
        raise ValueError(translate(language, "Select a TIFF image file."))
    mode = job["mode"]
    sources = [image]
    if mode in ("integrated", "integrated-fused"):
        counts = _plain(job["counts"])
        metadata = _plain(job["metadata"]) if job.get("metadata") else None
        if not counts.is_file() or (metadata is not None and not metadata.is_file()):
            raise ValueError(translate(language, "Select an H5AD file and a valid JSON attachment."))
        sources += [counts] + ([metadata] if metadata is not None else [])
    elif mode != "image":
        raise ValueError(translate(language, "Unknown compression mode."))

    output = _fresh(job["output"], *sources)
    if mode in ("integrated", "integrated-fused"):
        prepare_count_runtime(language, emit)
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".stpyramid-", dir=output.parent)).resolve()
    emit({"scratch": str(scratch)})

    if mode in ("integrated", "integrated-fused"):
        from integrated_encode import encode_archive

        if metadata is None:
            metadata = scratch / "metadata.json"
            metadata.write_text("{}\n", encoding="utf-8")
        result = encode_archive(image, counts, metadata, RUNTIME, output,
                                work=scratch / "work", on_stage=emit,
                                image_mode="fused" if mode == "integrated-fused" else "maximum",
                                fused_workers=int(os.environ.get("STPYRAMID_FUSED_WORKERS", "8"))
                                if mode == "integrated-fused" else 1,
                                maximum_workers=int(os.environ.get("STPYRAMID_MAXIMUM_WORKERS", "8"))
                                if mode == "integrated" else 1)
        verification = result["verification"]
        total_bytes = verification["ledger"]["total_package_bytes"]
        levels = verification["image"]["levels"]
    else:
        from integrated_encode import _configure_worker
        from native_tile_encoder import RUNTIMES, archive
        from pyramid_image_archive import encode_native_pyramid

        cpus = _configure_worker(None, 14400, int(os.environ.get("STPYRAMID_FUSED_WORKERS", "8")))
        workers = len(cpus)
        emit({"native_workers": workers, "candidate_cpu_affinity": cpus})
        candidate = scratch / "archive"
        result = encode_native_pyramid(image, candidate, scratch / "work", on_stage=emit, workers=workers)
        emit({"begin": "verify_image_archive"})
        with archive.Reader(candidate, *RUNTIMES, scan_workers=workers) as reader:
            verification = reader.verify()
        if not verification["ok"]:
            raise ValueError(translate(language, "Lossless image verification failed."))
        total_bytes = result["bytes"]
        levels = verification["levels"]
        candidate.rename(_fresh(output, *sources))

    if not job.get("keep_work"):
        # Only remove the app-created staging directory, after publishing the result.
        if scratch.resolve() == scratch and scratch.parent == output.parent and scratch.name.startswith(".stpyramid-"):
            try:
                shutil.rmtree(scratch)
            except OSError as exc:
                emit({"message": translate(language, "Archive saved. Could not remove the temporary folder. {error}", error=exc)})
    return {"output": str(output), "bytes": total_bytes, "levels": len(levels)}


def read_job(job, emit):
    import container_compare
    import joint_next
    import psutil
    from integrated_encode import runtime_dirs

    language = job.get("language", "en")
    root = container_compare._plain(job["archive"])
    manifest = container_compare._json(root / "manifest.json")
    runtime = runtime_dirs(Path(job.get("runtime") or RUNTIME))
    image_info = manifest if manifest.get("format") == joint_next.FORMAT else manifest.get("image", {})
    profile = image_info.get("profile")
    requested = int(os.environ.get("STPYRAMID_SCAN_WORKERS", "8" if profile == "l1_conditional" else "4"))
    if not 1 <= requested <= 8:
        raise ValueError("Scan worker count must be between 1 and 8")
    workers = min(requested, len(psutil.Process().cpu_affinity()))
    emit({"native_scan_workers": workers})
    if manifest.get("format") == container_compare.FORMAT:
        reader = container_compare.IntegratedArchive(
            root, image_factory=lambda path: joint_next.Reader(path, *runtime, scan_workers=workers))
    elif manifest.get("format") == joint_next.FORMAT:
        reader = joint_next.Reader(root, *runtime, scan_workers=workers)
    else:
        raise ValueError(translate(language, "Select an archive folder created by STPyramid."))

    with reader:
        if manifest.get("count", {}).get("kind") in container_compare.SHARED_COUNT_KINDS:
            prepare_count_runtime(language, emit)
        if job["action"] == "restore":
            emit({"begin": "restore_archive"})
            return {"output": str(reader.restore(Path(job["output"])))}
        emit({"begin": "verify_archive"})
        result = reader.verify()
        if not result["ok"]:
            raise ValueError(translate(language, "Archive verification failed."))
        return {"output": str(root), "bytes": result["ledger"]["total_package_bytes"]}


def main():
    if os.name == "posix":
        os.umask(0o077)
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                 "NUMBA_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS"):
        os.environ[name] = "1"
    os.environ.pop("ST074_S", None)
    os.environ.pop("ST074_T", None)
    # Numba's cache filenames can exceed the Windows path limit inside a
    # deeply nested portable installation.
    cache_root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    os.environ.setdefault("NUMBA_CACHE_DIR", str(cache_root / "STPyramid" / "numba-cache"))
    protocol = sys.stdout

    def emit(event):
        print(json.dumps(event, ensure_ascii=True), file=protocol, flush=True)

    try:
        job = json.load(sys.stdin)
        with redirect_stdout(sys.stderr):
            action = job["action"]
            if action == "encode":
                result = encode_job(job, emit)
            elif action in ("verify", "restore"):
                result = read_job(job, emit)
            else:
                raise ValueError(translate(job.get("language", "en"), "Unknown archive operation."))
        emit({"result": result})
    except Exception as exc:
        emit({"error": f"{type(exc).__name__}: {exc}"})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
