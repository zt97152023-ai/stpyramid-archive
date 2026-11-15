"""Run one desktop archive job in a separate process and stream stage events."""
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime-windows-source"
sys.path[:0] = [str(ROOT / "codec"), str(ROOT)]


def encode_job(job, emit):
    from container_compare import _fresh, _plain

    image = _plain(job["image"])
    if not image.is_file():
        raise ValueError("请选择 TIFF 图像文件。")
    mode = job["mode"]
    sources = [image]
    if mode == "integrated":
        counts = _plain(job["counts"])
        metadata = _plain(job["metadata"]) if job.get("metadata") else None
        if not counts.is_file() or (metadata is not None and not metadata.is_file()):
            raise ValueError("请选择 H5AD 文件和有效的 JSON 附件。")
        sources += [counts] + ([metadata] if metadata is not None else [])
    elif mode != "image":
        raise ValueError("未知压缩模式。")

    output = _fresh(job["output"], *sources)
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=".stpyramid-", dir=output.parent)).resolve()
    emit({"scratch": str(scratch)})

    if mode == "integrated":
        from integrated_encode import encode_archive

        if metadata is None:
            metadata = scratch / "metadata.json"
            metadata.write_text("{}\n", encoding="utf-8")
        result = encode_archive(image, counts, metadata, RUNTIME, output,
                                work=scratch / "work", on_stage=emit)
        verification = result["verification"]
        total_bytes = verification["ledger"]["total_package_bytes"]
        levels = verification["image"]["levels"]
    else:
        from native_tile_encoder import RUNTIMES, archive
        from pyramid_image_archive import encode_native_pyramid

        candidate = scratch / "archive"
        result = encode_native_pyramid(image, candidate, scratch / "work", on_stage=emit)
        emit({"begin": "verify_image_archive"})
        with archive.Reader(candidate, *RUNTIMES) as reader:
            verification = reader.verify()
        if not verification["ok"]:
            raise ValueError("图像无损核验失败。")
        total_bytes = result["bytes"]
        levels = verification["levels"]
        candidate.rename(_fresh(output, *sources))

    if not job.get("keep_work"):
        # Only remove the app-created staging directory, after publishing the result.
        if scratch.resolve() == scratch and scratch.parent == output.parent and scratch.name.startswith(".stpyramid-"):
            try:
                shutil.rmtree(scratch)
            except OSError as exc:
                emit({"message": f"归档已保存，临时目录清理失败。{exc}"})
    return {"output": str(output), "bytes": total_bytes, "levels": len(levels)}


def read_job(job, emit):
    import container_compare
    import joint_next
    from integrated_encode import runtime_dirs

    root = container_compare._plain(job["archive"])
    manifest = container_compare._json(root / "manifest.json")
    runtime = runtime_dirs(Path(job.get("runtime") or RUNTIME))
    if manifest.get("format") == container_compare.FORMAT:
        reader = container_compare.IntegratedArchive(
            root, image_factory=lambda path: joint_next.Reader(path, *runtime))
    elif manifest.get("format") == joint_next.FORMAT:
        reader = joint_next.Reader(root, *runtime)
    else:
        raise ValueError("请选择由 STPyramid 写出的归档目录。")

    with reader:
        if job["action"] == "restore":
            emit({"begin": "restore_archive"})
            return {"output": str(reader.restore(Path(job["output"])))}
        emit({"begin": "verify_archive"})
        result = reader.verify()
        if not result["ok"]:
            raise ValueError("归档核验失败。")
        return {"output": str(root), "bytes": result["ledger"]["total_package_bytes"]}


def main():
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                 "NUMBA_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS"):
        os.environ[name] = "1"
    os.environ.pop("ST074_S", None)
    os.environ.pop("ST074_T", None)
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
                raise ValueError("未知归档操作。")
        emit({"result": result})
    except Exception as exc:
        emit({"error": f"{type(exc).__name__}: {exc}"})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
