"""Portable baseline: independent HDF5 lossless chunks, unchanged scientific values."""
import hashlib
import json
from pathlib import Path
import platform
import shutil
import warnings

import h5py
import numpy as np
from PIL import Image

from .integrity import count_schema, h5_digest, jsonable, sha256, write_json

FORMAT = "stbaseline-1"


def _repack_expression(source, target, gzip):
    expected = h5_digest(source)
    with h5py.File(source, "r") as src, h5py.File(target, "x") as dst:
        for key, value in src.attrs.items():
            dst.attrs[key] = value
        for key in src:
            if key != "X":
                src.copy(key, dst)
        x = src["X"]

        def copy_array(a, parent, name):
            opts = dict(compression="gzip", compression_opts=gzip, shuffle=True, fletcher32=True) if a.size else {}
            b = parent.create_dataset(name, shape=a.shape, dtype=a.dtype, **opts)
            for start in range(0, len(a), 1024):
                b[start:start + 1024] = a[start:start + 1024]
            for key, value in a.attrs.items():
                b.attrs[key] = value

        if isinstance(x, h5py.Dataset):
            copy_array(x, dst, "X")
        else:
            g = dst.create_group("X")
            for key, value in x.attrs.items():
                g.attrs[key] = value
            for key in x:
                if key in ("data", "indices", "indptr"):
                    copy_array(x[key], g, key)
                else:
                    x.copy(key, g)
    if h5_digest(target) != expected:
        raise ValueError("H5AD semantic roundtrip failed")
    return expected


def build(h5ad, wsi, output, *, sample_id, xy_to_level0, spatial_key="spatial",
          tile=256, gzip=4, mapping_provenance="user-supplied", pixel_size_um=None,
          expected_sha256=None, max_level_pixels=150_000_000, attachments=None):
    """Build create-only archive; failures retain an unmistakably incomplete folder."""
    h5ad, wsi, out = Path(h5ad), Path(wsi), Path(output)
    if out.exists():
        raise FileExistsError(out)
    if not sample_id or not 16 <= tile <= 2048 or not 0 <= gzip <= 9:
        raise ValueError("Invalid sample ID, tile size or gzip level")
    transform = np.asarray(xy_to_level0, dtype=np.float64)
    if transform.shape != (3, 3) or not np.isfinite(transform).all() or not np.array_equal(transform[2], [0, 0, 1]) or np.linalg.det(transform[:2, :2]) == 0:
        raise ValueError("Mapping must be an invertible 2D affine transform")
    if pixel_size_um is not None and (len(pixel_size_um) != 2 or not np.isfinite(pixel_size_um).all() or min(pixel_size_um) <= 0):
        raise ValueError("Invalid pixel sizes")
    sources = {"h5ad": {"name": h5ad.name, "bytes": h5ad.stat().st_size, "sha256": sha256(h5ad)},
               "wsi": {"name": wsi.name, "bytes": wsi.stat().st_size, "sha256": sha256(wsi)}}
    attachments = attachments or {}
    reserved = {"manifest.json", "expression.h5ad", "images.h5", "INCOMPLETE", "failure.json", "source_metadata.json", "native_levels.tif", "h5ad", "wsi"}
    for name, source in attachments.items():
        if Path(name).name != name or name in reserved or not name or name in (".", ".."):
            raise ValueError("Attachment names must be unique safe filenames")
        sources[name] = {"name": Path(source).name, "bytes": Path(source).stat().st_size, "sha256": sha256(source)}
    if expected_sha256 and any(sources[k]["sha256"] != v for k, v in expected_sha256.items()):
        raise ValueError("Source SHA256 differs from qualified input")
    with h5py.File(h5ad, "r") as f:
        schema, _, _, _ = count_schema(f, spatial_key)
    out.mkdir(parents=True)
    (out / "INCOMPLETE").write_text("Build in progress or failed. Do not use.\n")
    try:
        semantic = _repack_expression(h5ad, out / "expression.h5ad", gzip)
        for name, source in attachments.items():
            shutil.copyfile(source, out / name)
            if sha256(out / name) != sources[name]["sha256"]:
                raise ValueError("Attachment changed while copying")
        levels = []
        # A single source level is materialized at once. No synthesized pyramid.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(wsi) as img, h5py.File(out / "images.h5", "x") as dst:
                if img.format != "TIFF":
                    raise ValueError("Version 0.1 source adapter accepts multi-page TIFF only")
                previous = None
                for level in range(img.n_frames):
                    img.seek(level)
                    width, height = img.size
                    if width * height > max_level_pixels:
                        raise ValueError("Source level exceeds explicit adapter memory bound")
                    if img.mode != "RGB" or int(img.tag_v2.get(274, 1)) != 1:
                        raise ValueError("Version 0.1 requires RGB8, orientation=1; no implicit conversion")
                    if previous and (width > previous[0] or height > previous[1] or (width, height) == previous):
                        raise ValueError("TIFF pages must be one strictly decreasing native pyramid")
                    tags = {str(k): jsonable(v) for k, v in img.tag_v2.items()}
                    pixels = np.asarray(img)
                    if pixels.dtype != np.uint8 or pixels.shape != (height, width, 3):
                        raise ValueError("Unexpected native pixel representation")
                    ds = dst.create_dataset(str(level), data=pixels, chunks=(min(tile, height), min(tile, width), 3),
                                            compression="gzip", compression_opts=gzip, shuffle=True, fletcher32=True)
                    levels.append({"level": level, "shape": list(pixels.shape), "dtype": "uint8",
                                   "chunks": list(ds.chunks), "pixel_sha256": hashlib.sha256(pixels.tobytes()).hexdigest(),
                                   "source_tiff_tags": tags})
                    previous = (width, height)
                    del pixels
        if any(sha256(p) != sources[k]["sha256"] for k, p in [("h5ad", h5ad), ("wsi", wsi), *attachments.items()]):
            raise ValueError("Source changed during build")
        manifest = {"format": FORMAT, "sample_id": sample_id, "sources": sources, "attachments": list(attachments),
                    "expression": {"path": "expression.h5ad", "semantic_sha256": semantic, **schema},
                    "image": {"path": "images.h5", "codec": "hdf5-gzip-shuffle-fletcher32", "levels": levels},
                    "mapping": {"spatial_key": spatial_key, "xy_to_level0": transform.tolist(),
                                "pixel_size_um": pixel_size_um, "provenance": mapping_provenance},
                    "semantics": {"roi": "half-open integer pixel boxes; no resampling", "image_exactness": "native decoded RGB8 pixels at every stored level",
                                  "h5ad_exactness": "all HDF5 scientific values, dtypes and attributes; container bytes may differ"},
                    "build": {"python": platform.python_version(), "numpy": np.__version__, "h5py": h5py.__version__,
                              "pillow": Image.__version__, "tile": tile, "gzip": gzip},
                    "files": {name: {"bytes": (out / name).stat().st_size, "sha256": sha256(out / name)}
                              for name in ("expression.h5ad", "images.h5", *attachments)}}
        write_json(out / "manifest.json", manifest)
        (out / "INCOMPLETE").unlink()
        return manifest
    except Exception as exc:
        write_json(out / "failure.json", {"type": type(exc).__name__, "message": str(exc)})
        raise
