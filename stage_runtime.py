"""Stage ABI-checked Anchor, Context and HF-Detail native runtimes from a local build."""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import shutil


def stage(source, output):
    # Build/ABI identifiers remain private compatibility details. Staged runtime
    # folders and binary names use the public mechanism-based codec names.
    items = {
        "anchor": ("slide_jxl089", "st089_abi_version", "anchor_codec"),
        "context": ("slide_jxl091", "st091_abi_version", "context_codec"),
        "hf-detail": ("slide_hf1", "sthf1_abi_version", "hf_detail_codec"),
    }
    source, output = source.resolve(strict=True), output.absolute()
    if output == source or source in output.resolve().parents:
        raise ValueError("Stage outside the build binary directory")
    for build_stem, symbol, _ in items.values():
        probe = getattr(ctypes.CDLL(str(source / (build_stem + ".dll"))), symbol)
        probe.argtypes, probe.restype = [], ctypes.c_uint32
        if probe() != 1 or not (source / (build_stem + ".exe")).is_file():
            raise ValueError("Missing native executable or ABI mismatch for codec runtime")
    output.mkdir(parents=True)
    for kind, (build_stem, _, public_stem) in items.items():
        folder = output / kind
        folder.mkdir()
        files = {}
        for suffix in (".exe", ".dll"):
            source_name = build_stem + suffix
            name = public_stem + suffix
            destination = folder / name
            shutil.copyfile(source / source_name, destination)
            files[name] = {
                "bytes": destination.stat().st_size,
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
        receipt = {"abi_version": 1, "release_profile": "local-source-v1", "outputs": files}
        if kind == "hf-detail":
            receipt.update(
                schema="stpyramid-hf-detail-runtime-build-1",
                representation="dense_magnitude_sign_v1",
            )
        (folder / "build_receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    stage(args.bin, args.output)
