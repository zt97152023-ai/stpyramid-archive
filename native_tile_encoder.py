"""Prepare RGB tile batches and invoke the native Windows encoder."""
import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
from pathlib import Path
import subprocess
import sys

import numpy as np

for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[name] = "1"
os.environ.pop("ST074_S", None)
os.environ.pop("ST074_T", None)

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime-windows-source"
RUNTIMES = tuple(RUNTIME / name for name in ("089", "091", "hf"))
sys.path.insert(0, str(ROOT / "codec"))
import joint_next as archive
from st262_controls import native_command
from stbaseline import slide_redundancy as base, slide_redundancy_scan as scan


def write_tile_inputs(census, catalog, input_dir, native_level, *, include_constant_tiles, parent_geometry=None,
                      partitions=1):
    """Write target and reference RGB, indexed by the encoder's level-local IDs."""
    input_dir.mkdir()
    index_rows = {}
    directories = [input_dir] if partitions == 1 else [input_dir / f"part_{i}" for i in range(partitions)]
    if partitions > 1:
        for directory in directories:
            directory.mkdir()
    start = archive.starts(catalog["levels"])[native_level]

    with scan.open_tile_reader(census, catalog) as read:
        read_parent = lru_cache(maxsize=32)(read)
        for gid, level, x, y, width, height in base._descriptors(catalog["levels"]):
            if level < native_level:
                continue
            if level > native_level:
                break
            if not include_constant_tiles and catalog["tiles"][gid]["constant_rgb"] is not None:
                continue
            desc = (gid, level, x, y, width, height)
            rgb = read(gid)
            if parent_geometry is None:
                reference, _ = archive.anchor_pixels(catalog["levels"], desc, read_parent)
            else:
                parent_id, rows, columns = parent_geometry(desc)
                reference = np.ascontiguousarray(read_parent(parent_id)[rows[:, None], columns[None, :]])
            local_gid = gid - start
            rgb_name, reference_name = f"{local_gid:06d}.rgb", f"{local_gid:06d}.anchor"
            directory = directories[len(index_rows) % partitions]
            (directory / rgb_name).write_bytes(rgb.tobytes())
            (directory / reference_name).write_bytes(reference.tobytes())
            # The legacy native index uses level 0 within each independently encoded batch.
            index_rows[gid] = "\t".join(map(str, (local_gid, 0, x, y, width, height, rgb_name, reference_name)))
    rows = list(index_rows.values())
    for index, directory in enumerate(directories):
        (directory / "index.tsv").write_text("".join(row + "\n" for row in rows[index::partitions]),
                                             encoding="ascii")
    return index_rows


def _child_cpu_seconds(process):
    get_times = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessTimes
    get_times.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    get_times.restype = wintypes.BOOL
    times = [wintypes.FILETIME() for _ in range(4)]
    if not get_times(wintypes.HANDLE(int(process._handle)), *(ctypes.byref(value) for value in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return sum((value.dwHighDateTime << 32) | value.dwLowDateTime for value in times[2:]) / 10_000_000


def run_native_encoder(exe, mode, spool, output, timeout=240):
    command, directory = native_command(exe, mode, spool, output)
    with output.with_suffix(".log").open("xb") as log:
        with subprocess.Popen(command, cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                              creationflags=subprocess.CREATE_NO_WINDOW) as process:
            try:
                status = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            cpu = _child_cpu_seconds(process)
    if status:
        raise RuntimeError(output.with_suffix(".log").read_text(encoding="utf8", errors="replace"))
    return cpu
