"""Prepare RGB tile batches and invoke the native Windows encoder."""
import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
from pathlib import Path
import struct
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


def _tile_inputs(census, catalog, native_level, include_constant_tiles, parent_geometry):
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
            yield (gid - start, 0, x, y, width, height), gid, rgb, reference


def _write_tile_input(directory, tile, rgb, reference):
    local_gid = tile[0]
    rgb_name, reference_name = f"{local_gid:06d}.rgb", f"{local_gid:06d}.anchor"
    (directory / rgb_name).write_bytes(rgb.tobytes())
    (directory / reference_name).write_bytes(reference.tobytes())
    # The native encoder uses level-zero IDs within each batch.
    return "\t".join(map(str, (*tile, rgb_name, reference_name)))


def write_tile_inputs(census, catalog, input_dir, native_level, *, include_constant_tiles, parent_geometry=None,
                      partitions=1):
    """Write target and reference RGB, indexed by the encoder's level-local IDs."""
    input_dir.mkdir()
    index_rows = {}
    directories = [input_dir] if partitions == 1 else [input_dir / f"part_{i}" for i in range(partitions)]
    if partitions > 1:
        for directory in directories:
            directory.mkdir()
    for tile, gid, rgb, reference in _tile_inputs(
        census, catalog, native_level, include_constant_tiles, parent_geometry
    ):
        directory = directories[len(index_rows) % partitions]
        index_rows[gid] = _write_tile_input(directory, tile, rgb, reference)
    rows = list(index_rows.values())
    for index, directory in enumerate(directories):
        (directory / "index.tsv").write_text("".join(row + "\n" for row in rows[index::partitions]),
                                             encoding="ascii")
    return index_rows


def write_tile_batches(census, catalog, input_dir, native_level, batch_tiles):
    """Yield complete native inputs as soon as each batch is staged."""
    input_dir.mkdir()
    rows = {}
    batch = 0
    for tile, gid, rgb, reference in _tile_inputs(census, catalog, native_level, False, None):
        if not rows:
            directory = input_dir / f"part_{batch:03d}"
            directory.mkdir()
        rows[gid] = _write_tile_input(directory, tile, rgb, reference)
        if len(rows) == batch_tiles:
            (directory / "index.tsv").write_text("\n".join(rows.values()) + "\n", encoding="ascii")
            yield directory, rows.keys()
            rows = {}
            batch += 1
    if rows:
        (directory / "index.tsv").write_text("\n".join(rows.values()) + "\n", encoding="ascii")
        yield directory, rows.keys()


_CENSUS_PLAN_MAGIC = b"091RGB01"
_CENSUS_ENTRY = struct.Struct("<6IQ32sQ32s4I")


def write_census_batches(catalog, input_dir, native_level, batch_tiles):
    """Write offset and digest plans for the native encoder's shared RGB input."""
    input_dir.mkdir()
    rows = bytearray()
    gids = []
    batch = 0
    start = archive.starts(catalog["levels"])[native_level]
    levels = catalog["levels"]

    def write_plan():
        path = input_dir / f"part_{batch:03d}.idx"
        path.write_bytes(_CENSUS_PLAN_MAGIC + struct.pack("<I", len(gids)) + rows)
        return path, gids

    for desc in base._descriptors(levels):
        gid, level, x, y, width, height = desc
        if level < native_level:
            continue
        if level > native_level:
            break
        tile = catalog["tiles"][gid]
        if tile["constant_rgb"] is not None:
            continue
        parent_id, ys, xs = archive.anchor_layout(levels, desc)
        parent = catalog["tiles"][parent_id]
        rows.extend(_CENSUS_ENTRY.pack(gid - start, 0, x, y, width, height,
                                      tile["raw_offset"], bytes.fromhex(tile["native_sha256"]),
                                      parent["raw_offset"], bytes.fromhex(parent["native_sha256"]),
                                      parent["w"], parent["h"], int(xs[0]), int(ys[0])))
        gids.append(gid)
        if len(gids) == batch_tiles:
            yield write_plan()
            rows.clear()
            gids = []
            batch += 1
    if gids:
        yield write_plan()


def supports_census_input(exe, source, work):
    """Use the shared census when the staged native binary supports it."""
    source = source.absolute()
    if not str(source).isascii():
        work = work.absolute()
        if source.drive != work.drive or not os.path.relpath(source, work).isascii():
            return False
    check = subprocess.run([str(exe), "census_input_version"], capture_output=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    return check.returncode == 0 and check.stdout.strip() == _CENSUS_PLAN_MAGIC


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
    return _run_encoder(command, directory, output, timeout)


def run_census_encoder(exe, index, source, output, timeout=240, *, preset=""):
    directory = output.absolute().parent
    source = source.absolute()
    source_arg = str(source) if str(source).isascii() else os.path.relpath(source, directory)
    mode = f"encode_census_{preset}" if preset else "encode_census"
    command = [str(exe.absolute()), mode, os.path.relpath(index, directory),
               source_arg, os.path.join(os.curdir, output.name)]
    return _run_encoder(command, directory, output, timeout)


def _run_encoder(command, directory, output, timeout):
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
