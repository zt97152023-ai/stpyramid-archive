"""Stage ABI-checked Anchor, Context and HF-Detail native runtimes from a local build."""
import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import shutil


def stage(source, output):
    items = {"089": ("slide_jxl089", "st089_abi_version"),
             "091": ("slide_jxl091", "st091_abi_version"),
             "hf": ("slide_hf1", "sthf1_abi_version")}
    source, output = source.resolve(strict=True), output.absolute()
    if output == source or source in output.resolve().parents:
        raise ValueError("Stage outside the build binary directory")
    for stem, symbol in items.values():
        probe = getattr(ctypes.CDLL(str(source / (stem + ".dll"))), symbol)
        probe.argtypes, probe.restype = [], ctypes.c_uint32
        if probe() != 1 or not (source / (stem + ".exe")).is_file():
            raise ValueError("Missing native executable or ABI mismatch: " + stem)
    output.mkdir(parents=True)
    for kind, (stem, _) in items.items():
        folder = output / kind
        folder.mkdir()
        files = {}
        for suffix in (".exe", ".dll"):
            name = stem + suffix
            destination = folder / name
            shutil.copyfile(source / name, destination)
            files[name] = {"bytes": destination.stat().st_size,
                           "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
        receipt = {"abi_version": 1, "release_profile": "local-source-v1", "outputs": files}
        if kind == "hf":
            receipt.update(schema="wsi-hf1-runtime-build-1", representation="dense_magnitude_sign_v1")
        (folder / "build_receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n",
                                                    encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    stage(args.bin, args.output)
