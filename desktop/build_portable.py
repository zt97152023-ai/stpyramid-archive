"""Build a relocatable Windows desktop directory from the qualified Python environment."""
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import site
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DISTRIBUTIONS = ("numpy", "scipy", "h5py", "tifffile", "imagecodecs", "psutil", "numba", "llvmlite", "Pillow",
                 "zstandard")
SCRIPTS = ("desktop_app.py", "desktop_worker.py", "native_tile_encoder.py", "pyramid_image_archive.py", "THIRD_PARTY.md")
CACHE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.nbc", "*.nbi")


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def install_python(output):
    base = Path(sys.base_prefix)
    output.mkdir()
    for name in ("python.exe", "pythonw.exe", "python3.dll", "python312.dll", "vcruntime140.dll",
                 "vcruntime140_1.dll", "LICENSE.txt"):
        copy_file(base / name, output / name)
    for name in ("DLLs", "tcl"):
        shutil.copytree(base / name, output / name, ignore=CACHE)
    shutil.copytree(base / "Lib", output / "Lib",
                    ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc"))
    packages = output / "Lib" / "site-packages"
    package_roots = [Path(path).resolve() for path in site.getsitepackages() if Path(path).name == "site-packages"]
    for name in DISTRIBUTIONS:
        distribution = metadata.distribution(name)
        for relative in distribution.files:
            source = distribution.locate_file(relative).resolve()
            if source.is_file() and source.suffix not in {".pyc", ".nbc", ".nbi"}:
                for root in package_roots:
                    if source.is_relative_to(root):
                        copy_file(source, packages / source.relative_to(root))
                        break


def install_app(output):
    for name in SCRIPTS:
        copy_file(ROOT / name, output / name)
    for file in (ROOT / "codec").rglob("*"):
        rel = file.relative_to(ROOT)
        if file.is_file() and file.suffix in {".py", ".json", ".R"} and "_shared_vendor" not in rel.parts:
            copy_file(file, output / rel)
    vendor = Path("codec/stbaseline/_shared_vendor")
    manifest = vendor / "SOURCE.json"
    copy_file(ROOT / manifest, output / manifest)
    for pin in json.loads((ROOT / manifest).read_text(encoding="utf-8-sig"))["files"]:
        relative = vendor / pin["path"]
        source = ROOT / relative
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != pin["sha256"]:
            raise RuntimeError("Run fetch_count_runtime.py with the qualified local environment before packaging.")
        copy_file(source, output / relative)
    shutil.copytree(ROOT / "runtime-windows-source", output / "runtime-windows-source")
    copy_file(ROOT / "bin/slide_jxl091.exe", output / "bin/slide_jxl091.exe")
    for name in ("upstream/libjxl/LICENSE", "upstream/libjxl/PATENTS", "upstream/libjxl/third_party/brotli/LICENSE",
                 "upstream/libjxl/third_party/highway/LICENSE", "upstream/libjxl/third_party/skcms/LICENSE",
                 "native/091/vendor091/LICENSE", "native/hf1/vendorhf/LICENSE"):
        copy_file(ROOT / name, output / name)


def build(output, make_zip):
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12) or struct.calcsize("P") != 8:
        raise RuntimeError("Use the qualified Windows x64 CPython 3.12 environment.")
    output = output.resolve()
    zip_path = output.with_suffix(".zip")
    if make_zip and zip_path.exists():
        raise FileExistsError(zip_path)
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    output.mkdir(parents=True)
    install_python(output / "python")
    install_app(output / "app")
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/reference:System.Windows.Forms.dll",
                    f"/out:{output / 'STPyramid.exe'}", str(ROOT / "desktop/Launcher.cs")], check=True)
    guide = (ROOT / "desktop/使用说明.txt").read_text(encoding="utf-8")
    (output / "使用说明.txt").write_text(guide, encoding="utf-8-sig")
    if make_zip:
        shutil.make_archive(str(zip_path.with_suffix("")), "zip", output.parent, output.name)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--zip", action="store_true", dest="make_zip")
    args = parser.parse_args()
    print(build(args.output, args.make_zip))
