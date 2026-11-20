"""Command-line entry for lossless WSI and spatial Count archives."""
import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "codec"))


def encode(args):
    from integrated_encode import encode_archive

    return encode_archive(args.image, args.counts, args.metadata, args.runtime_dir, args.output,
                          work=args.work, cpu=args.cpu, timeout=args.timeout)


def read_archive(args):
    import container_compare as archive
    import joint_next as j
    from integrated_encode import runtime_dirs

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
    if os.name == "posix":
        os.umask(0o077)
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
