"""Opt-in native tiled WSI + newly encoded Shared Count packages.

Image input, verification and TIFF restoration use bounded tile-row stripes.
Count encoding deliberately retains the qualified 20-million-cell bound; this
module does not claim an out-of-core Count implementation or TIFF byte recovery.
"""
import base64
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import time
import zlib

import h5py
import numpy as np

from .comparison import csr_from_h5, science_skeleton
from .image_shared import ENTRY, HEADER, MAGIC
from .integrity import count_schema, h5_digest, sha256, write_json
from .lossless_components import ComponentArchive
from .lossless_metadata import _copy_attributes, repack_metadata
from .shared_backend import write_count_store
from .shared_streaming import SharedBlockArchive
from .wsi_source import NativeWSISource

FORMAT = "stlossless-native-wsi-1"
CONFIG_SCHEMA = "stlossless-native-wsi-config-1"
TILE = 256
MAX_CELLS = 20_000_000
MAX_ROI_PIXELS = 16_777_216
MAX_STRIPE_BYTES = 128 * 1024 * 1024
SOURCE_METADATA = "wsi_source.json"
REQUIRED = {"science.h5", "counts.bank", "counts.index.json", "images.bank", "images.index", SOURCE_METADATA}
RESERVED = REQUIRED | {"manifest.json", "expression.h5ad", "native_levels.tif", "source_metadata.json", "INCOMPLETE", "failure.json"}


def _integer(value):
    return isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_))


def _safe_name(name):
    return isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) is not None


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def _fresh(path):
    argument = Path(path)
    if argument.is_symlink():
        raise ValueError("Output symlinks are unsupported")
    result = argument.resolve()
    if result.exists():
        raise FileExistsError(result)
    return result


def load_config(config):
    """Small independent configuration; no stable-loader or profile changes."""
    base = Path.cwd()
    if isinstance(config, (str, Path)):
        path = Path(config).resolve(strict=True)
        base = path.parent
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("Configuration must be an object")
    c = copy.deepcopy(config)
    fields = {"schema", "sample_id", "h5ad", "wsi", "spatial_key", "xy_to_level0", "mapping_provenance", "attachments", "pixel_size_um"}
    if set(c) - fields or c.get("schema", CONFIG_SCHEMA) != CONFIG_SCHEMA:
        raise ValueError("Unknown native WSI configuration field or schema")
    c["schema"] = CONFIG_SCHEMA
    for name in ("sample_id", "mapping_provenance"):
        if not isinstance(c.get(name), str) or not c[name].strip():
            raise ValueError("Explicit " + name + " required")
    for name in ("h5ad", "wsi"):
        if not isinstance(c.get(name), (str, Path)):
            raise ValueError("Input path required: " + name)
        path = (base / c[name]).resolve(strict=True)
        if not path.is_file():
            raise ValueError("Regular input file required: " + name)
        c[name] = str(path)
    c.setdefault("spatial_key", "spatial")
    if not isinstance(c["spatial_key"], str) or not c["spatial_key"]:
        raise ValueError("Nonempty spatial_key required")
    transform = np.asarray(c.get("xy_to_level0"), dtype=np.float64)
    if (transform.shape != (3, 3) or not np.isfinite(transform).all()
            or not np.array_equal(transform[2], [0, 0, 1]) or np.linalg.det(transform[:2, :2]) == 0):
        raise ValueError("Explicit invertible 3x3 source-level0 affine required")
    c["xy_to_level0"] = transform.tolist()
    pixel_size = c.get("pixel_size_um")
    if pixel_size is not None:
        pixel_size = np.asarray(pixel_size, dtype=np.float64)
        if pixel_size.shape != (2,) or not np.isfinite(pixel_size).all() or np.any(pixel_size <= 0):
            raise ValueError("pixel_size_um must contain two positive finite values")
        c["pixel_size_um"] = pixel_size.tolist()
    attachments = c.get("attachments", {})
    if not isinstance(attachments, dict):
        raise ValueError("attachments must map safe names to paths")
    c["attachments"] = {}
    for name, value in attachments.items():
        if not _safe_name(name) or name in RESERVED or name in ("h5ad", "wsi"):
            raise ValueError("Reserved or unsafe attachment name")
        path = (base / value).resolve(strict=True)
        if not path.is_file():
            raise ValueError("Regular attachment file required")
        c["attachments"][name] = str(path)
    return c


def _count_preflight(path, spatial_key):
    with h5py.File(path, "r") as handle:
        x = handle["X"]
        shape = x.shape if isinstance(x, h5py.Dataset) else tuple(x.attrs["shape"])
        if (len(shape) != 2 or any(not _integer(v) or v < 1 for v in shape)
                or int(shape[0]) * int(shape[1]) > MAX_CELLS):
            raise ValueError("Count source exceeds the qualified 20,000,000-cell bound")
        schema, rows, genes, xy = count_schema(handle, spatial_key)
        data = x if isinstance(x, h5py.Dataset) else x["data"]
        if isinstance(x, h5py.Group):
            for start in range(0, len(data), 65536):
                if np.any(data[start:start + 65536] == 0):
                    raise ValueError("Explicit stored CSR zeros would change the original scientific structure")
        if data.dtype.kind == "f":
            for start in range(0, len(data), 64):
                values = data[start:start + 64]
                # A weak scalar comparison can round uint32 max to 2**32
                # when the source is float32. Promote the array explicitly so
                # the limit remains exact before any uint32 conversion.
                if np.any(values.astype(np.float64) > np.float64(2**32 - 1)):
                    raise ValueError("Floating Count values exceed the exact uint32 range")
                if np.any((values == 0) & np.signbit(values)):
                    raise ValueError("Negative-zero Count values cannot pass integer encoding exactly")
    return schema, rows, genes, xy


def _exact_skeleton(source, target):
    # Existing skeleton copies non-X trees. Preserve the actual attribute types
    # on the few objects it reconstructs, rather than recreating them from a
    # Python dictionary (which can change fixed strings and enum types).
    science_skeleton(source, target)
    with h5py.File(source, "r") as src, h5py.File(target, "r+") as dst:
        pairs = [(src, dst), (src["X"], dst["X"])]
        if isinstance(src["X"], h5py.Group):
            pairs.extend((src["X"][name], dst["X"][name]) for name in src["X"])
        for original, copied in pairs:
            for name in list(copied.attrs):
                del copied.attrs[name]
            _copy_attributes(original, copied)


def _stripe_shape(info):
    h, w, _ = info["shape"]
    shape = (min(TILE, h), w, 3)
    if math.prod(shape) > MAX_STRIPE_BYTES:
        raise ValueError("Native raster stripe exceeds 128 MiB")
    return shape


def _encode_images(source, staging):
    import imagecodecs
    levels = copy.deepcopy(source.levels)
    total_tiles = sum(((p["shape"][0] + TILE - 1) // TILE) * ((p["shape"][1] + TILE - 1) // TILE) for p in levels)
    if not 1 <= total_tiles <= 2**32 - 1:
        raise ValueError("Image tile count exceeds index capacity")
    for info in levels:
        _stripe_shape(info)
    with (staging / "images.bank").open("xb") as bank, (staging / "images.index").open("xb") as index:
        index.write(HEADER.pack(MAGIC, TILE, total_tiles))
        for info in levels:
            h, w, _ = info["shape"]
            digest = hashlib.sha256()
            for y in range(0, h, TILE):
                stripe = np.empty((min(TILE, h - y), w, 3), dtype=np.uint8)
                for x in range(0, w, TILE):
                    pixels = source.read_tile(info["level"], x // TILE, y // TILE)
                    expected = (stripe.shape[0], min(TILE, w - x), 3)
                    if pixels.dtype != np.uint8 or pixels.shape != expected:
                        raise ValueError("Source tile differs from declared native schema")
                    stripe[:, x:x + pixels.shape[1]] = pixels
                    blob = imagecodecs.jpegxl_encode(np.ascontiguousarray(pixels), lossless=True, distance=0, effort=9, numthreads=1)
                    if not 0 < len(blob) <= 2**32 - 1:
                        raise ValueError("Encoded tile exceeds image index capacity")
                    index.write(ENTRY.pack(bank.tell(), len(blob), 1, zlib.crc32(blob)))
                    bank.write(blob)
                digest.update(memoryview(stripe))
            info.update(chunks=[min(TILE, h), min(TILE, w), 3], pixel_sha256=digest.hexdigest())
    return levels, total_tiles


def pack_wsi(config, output, workspace=None):
    """Encode original H5AD and native WSI once; verify before publishing."""
    started = time.perf_counter()
    c = load_config(config)
    out = _fresh(output)
    work = _fresh(workspace if workspace is not None else out.with_name(out.name + ".work"))
    staging = _fresh(out.with_name(out.name + ".staging"))
    if _overlap(out, work) or _overlap(staging, work):
        raise ValueError("Package and workspace must be disjoint")
    inputs = {"h5ad": Path(c["h5ad"]), "wsi": Path(c["wsi"]), **{k: Path(v) for k, v in c["attachments"].items()}}
    for source in inputs.values():
        if any(_overlap(source, target) for target in (out, work, staging)):
            raise ValueError("Input files and output trees must be disjoint")
    work.mkdir(parents=True, exist_ok=False)
    (work / "INCOMPLETE").write_text("Native WSI build in progress\n", encoding="utf-8")
    write_json(work / "resolved_config.json", c)
    report = {"schema": "stlossless-native-wsi-build-1", "status": "building", "timings": {}, "training": False,
              "scope": "Full native WSI image stream plus newly encoded source H5AD Count and scientific metadata; Count bounded, images streamed"}
    created = False
    try:
        then = time.perf_counter()
        schema, rows, genes, xy = _count_preflight(c["h5ad"], c["spatial_key"])
        transform = np.asarray(c["xy_to_level0"], dtype=np.float64)
        mapped = (np.c_[xy, np.ones(len(xy))] @ transform.T)[:, :2]
        if not np.isfinite(mapped).all() or np.any(np.abs(mapped) >= 2**62):
            raise ValueError("Mapped coordinates exceed finite spatial partition bounds")
        md = {"spot_ids": rows, "gene_ids": genes, "coordinates": xy, "level0_xy": mapped}
        semantic = h5_digest(c["h5ad"])
        sources = {name: {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
                   for name, path in inputs.items() if name != "wsi"}
        with NativeWSISource(c["wsi"]) as source:
            for info in source.levels:
                _stripe_shape(info)
            source.unchanged()
            report["timings"]["preflight_seconds"] = time.perf_counter() - then
            staging.mkdir(parents=True, exist_ok=False)
            created = True
            (staging / "INCOMPLETE").write_text("Native WSI package incomplete\n", encoding="utf-8")
            then = time.perf_counter()
            matrix = csr_from_h5(c["h5ad"])
            count_index = write_count_store(matrix, md, mapped, staging, "Shared_local", TILE)
            del matrix
            report["timings"]["count_encode_seconds"] = time.perf_counter() - then
            then = time.perf_counter()
            skeleton = work / "science_skeleton.h5"
            _exact_skeleton(c["h5ad"], skeleton)
            metadata_report = repack_metadata(skeleton, staging / "science.h5")
            write_json(work / "metadata.json", metadata_report)
            report["timings"]["metadata_seconds"] = time.perf_counter() - then
            then = time.perf_counter()
            levels, tile_count = _encode_images(source, staging)
            report["timings"]["image_read_encode_seconds"] = time.perf_counter() - then
            stable = source.unchanged()
            source_metrics = source.metrics()
            original_identity = copy.deepcopy(source.identity)
            identity = copy.deepcopy(original_identity)
            identity.setdefault("name", inputs["wsi"].name)
            identity.setdefault("bytes", inputs["wsi"].stat().st_size)
            sources["wsi"] = identity
            receipt_keys = ("tiles_decoded", "source_bytes_read", "decoded_pixels", "returned_pixels", "source_read_sha256",
                            "source_read_hash_scope", "source_bytes_scope")
            receipt = {k: source_metrics[k] for k in receipt_keys if k in source_metrics}
            write_json(staging / SOURCE_METADATA, {"schema": "stlossless-wsi-source-1", "identity": identity,
                       "native_levels": source.levels, "native_metadata": source.metadata,
                       "read_receipt": receipt, "stability_check": stable,
                       "identity_scope": "File/path stat guards, metadata/layout digests and actual source-tile read receipt; not a whole source-file hash",
                       "restoration_scope": "Native RGB8 pixels and retained source metadata; TIFF layout and container bytes may differ"})
        then = time.perf_counter()
        for name, source_path in c["attachments"].items():
            with Path(source_path).open("rb") as src, (staging / name).open("xb") as dst:
                shutil.copyfileobj(src, dst)
            if sha256(staging / name) != sources[name]["sha256"]:
                raise ValueError("Attachment changed while copying")
        import imagecodecs
        manifest = {"format": FORMAT, "sample_id": c["sample_id"], "sources": sources, "attachments": list(c["attachments"]),
                    "count_component": "shared_local", "image_component": "jxl",
                    "expression": {"path": "science.h5", "semantic_sha256": semantic, **schema, "storage": "Shared_local-B8-K32"},
                    "image": {"path": "images.bank", "index": "images.index", "levels": levels,
                              "codec": "indexed-independent-image-tiles-v1", "source_metadata": SOURCE_METADATA},
                    "mapping": {"spatial_key": c["spatial_key"], "xy_to_level0": c["xy_to_level0"], "pixel_size_um": c.get("pixel_size_um"),
                                "provenance": c["mapping_provenance"], "coordinate_frame": "original-source-level0", "source_origin": [0, 0]},
                    "semantics": {"roi": "half-open source-level0 boxes; exact native-level covering rectangles, no resampling",
                                  "image_exactness": "decoded RGB8 pixels at every stored native level; not source TIFF container bytes",
                                  "h5ad_exactness": "scientific values, dtypes, IDs, coordinates, gene axis and attributes; storage layout may differ",
                                  "preview": "existing native pyramid level; exact at that stored level, not generally exact level0"},
                    "count_parameters": {"method": "Shared_local", "block_pixels": TILE, "blocks": len(count_index["blocks"]), "decoded_count_cache": False},
                    "image_parameters": {"tile": TILE, "jpegxl_effort": 9, "numthreads": 1, "dictionary_training": False, "parameter_search": False},
                    "limits": {"max_count_cells": MAX_CELLS, "max_roi_pixels": MAX_ROI_PIXELS, "max_stripe_bytes": MAX_STRIPE_BYTES},
                    "build": {"tool": "stlossless-native-wsi-1", "adapter_sha256": sha256(Path(__file__)), "imagecodecs": imagecodecs.__version__,
                              "metadata_profile": metadata_report["profile"], "actual_count_encoding": True, "training": False,
                              "shared_source_receipt_sha256": sha256(Path(__file__).with_name("_shared_vendor") / "SOURCE.json")}}
        manifest["files"] = {name: {"bytes": (staging / name).stat().st_size, "sha256": sha256(staging / name)}
                             for name in sorted(REQUIRED | set(c["attachments"]))}
        write_json(staging / "manifest.json", manifest)
        (staging / "INCOMPLETE").unlink()
        report["timings"]["manifest_attachments_seconds"] = time.perf_counter() - then
        then = time.perf_counter()
        with WSIArchive(staging) as reader:
            verified = reader.verify()
            ledger = reader.ledger()
        report["timings"]["verification_seconds"] = time.perf_counter() - then
        for name, path in inputs.items():
            if name != "wsi" and sha256(path) != sources[name]["sha256"]:
                raise ValueError("Input changed during package construction: " + name)
        # The source adapter already checked WSI stat identity after its last
        # tile read. Reopen metadata only to detect changes during verification.
        with NativeWSISource(c["wsi"]) as recheck:
            if recheck.identity != original_identity:
                raise ValueError("WSI source identity changed during package verification")
            recheck.unchanged()
        if out.exists() or out.is_symlink():
            raise FileExistsError(out)
        staging.rename(out)
        report.update(status="complete", elapsed_seconds=time.perf_counter() - started,
                      total_package_bytes=ledger["total_package_bytes"], ledger=ledger, verified=verified,
                      image_tiles=tile_count, source_read_receipt=receipt)
        write_json(work / "build_report.json", report)
        (work / "INCOMPLETE").unlink()
        return {"ok": True, "archive": str(out), "total_package_bytes": ledger["total_package_bytes"],
                "ledger": ledger, "report": str(work / "build_report.json")}
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}", elapsed_seconds=time.perf_counter() - started)
        write_json(work / "build_report.json", report)
        if created and staging.exists():
            (staging / "INCOMPLETE").write_text("Failed native WSI build; evidence preserved\n", encoding="utf-8")
            write_json(staging / "failure.json", {"error": report["error"]})
        raise


class WSIArchive(SharedBlockArchive):
    """Independent modalities, bounded ROI arrays and streamed TIFF recovery."""

    def __init__(self, directory):
        argument = Path(directory)
        self.root = argument.resolve()
        path = self.root / "manifest.json"
        if argument.is_symlink() or path.is_symlink() or (self.root / "INCOMPLETE").exists() or (self.root / "failure.json").exists():
            raise ValueError("Unsafe or incomplete native WSI package")
        blob = path.read_bytes()
        self.manifest, self.manifest_bytes = json.loads(blob), len(blob)
        m = self.manifest
        if m.get("format") != FORMAT or m.get("count_component") != "shared_local" or m.get("image_component") != "jxl":
            raise ValueError("Unsupported native WSI package")
        attachments = m.get("attachments", [])
        if (not isinstance(attachments, list) or any(not _safe_name(n) or n in RESERVED for n in attachments)
                or len(set(attachments)) != len(attachments) or set(m["files"]) != REQUIRED | set(attachments)
                or m["expression"]["path"] != "science.h5" or m["image"]["path"] != "images.bank"
                or m["image"].get("index") != "images.index" or m["image"].get("source_metadata") != SOURCE_METADATA):
            raise ValueError("Unsafe or inconsistent native WSI members")
        for name, desc in m["files"].items():
            member = self.root / name
            if (not _safe_name(name) or member.is_symlink() or not member.is_file()
                    or member.resolve().parent != self.root or member.stat().st_size != desc["bytes"]):
                raise ValueError("Unsafe or size-mismatched member: " + name)
        shape = m["expression"]["shape"]
        if len(shape) != 2 or any(not _integer(n) or n < 1 for n in shape) or math.prod(shape) > MAX_CELLS:
            raise ValueError("Expression shape exceeds qualified Count bound")
        levels = m["image"]["levels"]
        if not isinstance(levels, list) or not 1 <= len(levels) <= 64:
            raise ValueError("Invalid native pyramid length")
        previous = None
        for i, info in enumerate(levels):
            shape = info["shape"]
            if (info.get("level") != i or info.get("dtype") != "uint8" or len(shape) != 3
                    or any(not _integer(n) or n < 1 for n in shape) or shape[2] != 3
                    or not isinstance(info.get("pixel_sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", info["pixel_sha256"]) is None):
                raise ValueError("Invalid native image schema")
            if previous and (shape[0] > previous[0] or shape[1] > previous[1] or shape[:2] == previous):
                raise ValueError("Native pyramid must decrease")
            _stripe_shape(info)
            previous = shape[:2]
        transform = np.asarray(m["mapping"]["xy_to_level0"], dtype=np.float64)
        if (transform.shape != (3, 3) or not np.isfinite(transform).all() or not np.array_equal(transform[2], [0, 0, 1])
                or np.linalg.det(transform[:2, :2]) == 0 or m["mapping"].get("source_origin") != [0, 0]):
            raise ValueError("Invalid source-level0 mapping")
        if m["count_parameters"].get("block_pixels") != TILE or m["image_parameters"].get("tile") != TILE:
            raise ValueError("Unexpected native WSI restart geometry")
        self.handles, self.files, self._metadata = {}, {}, None
        self._count_store, self._tiff, self._entries = None, None, None
        self.index_read_bytes, self.image_tiles_decoded = 0, 0
        self._level_start, self._decompressors = [], {}
        self.decoded_image_pixels, self.requested_image_pixels = 0, 0
        self._source_metadata_cache = None
        self.ledger()

    def _image_setup(self):
        super()._image_setup()
        if self._image_tile != TILE or any(record[2] != 1 for record in self._entries):
            raise ValueError("Native WSI requires independent256 lossless JPEG XL tiles")

    def _image_read(self, level, bbox, *, max_pixels=MAX_ROI_PIXELS):
        info = self._level(level)
        if len(bbox) != 4 or any(not _integer(v) for v in bbox):
            raise ValueError("Native image ROI must contain four integers")
        x0, y0, x1, y1 = map(int, bbox)
        h, w, _ = info["shape"]
        if not 0 <= x0 < x1 <= w or not 0 <= y0 < y1 <= h:
            raise ValueError("Native image ROI outside level")
        if (x1 - x0) * (y1 - y0) > max_pixels:
            raise ValueError("Returned image ROI exceeds explicit pixel bound; iterate tiles")
        result = ComponentArchive.image(self, level, [x0, y0, x1, y1])
        self.requested_image_pixels += (x1 - x0) * (y1 - y0)
        self.decoded_image_pixels += sum(min(TILE, h - ty * TILE) * min(TILE, w - tx * TILE)
                                        for ty in range(y0 // TILE, (y1 - 1) // TILE + 1)
                                        for tx in range(x0 // TILE, (x1 - 1) // TILE + 1))
        return result

    def image(self, level, bbox):
        return self._image_read(level, bbox)

    def iter_image_tiles(self, level):
        h, w, _ = self._level(level)["shape"]
        for y in range(0, h, TILE):
            for x in range(0, w, TILE):
                yield x, y, self.image(level, [x, y, min(x + TILE, w), min(y + TILE, h)])

    def _geometry(self, bbox, level):
        if len(bbox) != 4 or any(not _integer(v) for v in bbox):
            raise ValueError("Source-level0 region requires four integers")
        x0, y0, x1, y1 = map(int, bbox)
        h0, w0, _ = self._level(0)["shape"]
        if not 0 <= x0 < x1 <= w0 or not 0 <= y0 < y1 <= h0:
            raise ValueError("Source-level0 region outside native bounds")
        h, w, _ = self._level(level)["shape"]
        return [x0 * w // w0, y0 * h // h0, (x1 * w + w0 - 1) // w0, (y1 * h + h0 - 1) // h0]

    def region(self, bbox, *, level=0, genes=None, modalities=("counts", "image")):
        if (not isinstance(modalities, (tuple, list)) or not modalities or len(set(modalities)) != len(modalities)
                or any(name not in ("counts", "image") for name in modalities)):
            raise ValueError("Select independent counts and/or image modalities")
        box = self._geometry(bbox, level)
        result = {"level0_bbox": np.asarray(bbox, dtype=np.int64), "level": np.asarray(level), "image_bbox": np.asarray(box)}
        if "counts" in modalities:
            xy = self.metadata()["level0_xy"]
            x0, y0, x1, y1 = bbox
            rows = np.flatnonzero((xy[:, 0] >= x0) & (xy[:, 0] < x1) & (xy[:, 1] >= y0) & (xy[:, 1] < y1))
            result.update(self.counts(rows, genes))
        if "image" in modalities:
            result["image"] = self.image(level, box)
        return result

    def preview(self, bbox=None, *, max_edge=1024):
        if not _integer(max_edge) or not 1 <= max_edge <= int(math.isqrt(MAX_ROI_PIXELS)):
            raise ValueError("Preview max_edge must be 1..4096")
        h, w, _ = self._level(0)["shape"]
        bbox = [0, 0, w, h] if bbox is None else bbox
        for info in self.manifest["image"]["levels"]:
            level = info["level"]
            box = self._geometry(bbox, level)
            if max(box[2] - box[0], box[3] - box[1]) <= max_edge:
                return {"image": self.image(level, box), "level": level, "image_bbox": box, "level0_bbox": list(bbox),
                        "native_exact": level == 0, "stored_level_exact": True, "source": "native_pyramid"}
        raise ValueError("No stored native level fits the preview bound; no resampling is performed")

    def source_metadata(self):
        if self._source_metadata_cache is None:
            path = self.root / SOURCE_METADATA
            if sha256(path) != self.manifest["files"][SOURCE_METADATA]["sha256"]:
                raise ValueError("Source metadata integrity failure")
            metadata = json.loads(path.read_text(encoding="utf-8"))
            levels = self.manifest["image"]["levels"]
            if (metadata.get("schema") != "stlossless-wsi-source-1"
                    or len(metadata.get("native_levels", [])) != len(levels)
                    or len(metadata.get("native_metadata", [])) != len(levels)):
                raise ValueError("Source metadata page coverage disagrees")
            for i, (level, tags) in enumerate(zip(metadata["native_levels"], metadata["native_metadata"])):
                if (level.get("level") != i or level.get("shape") != levels[i]["shape"]
                        or tags.get("level") != i or not isinstance(tags.get("restore"), dict)):
                    raise ValueError("Source metadata level geometry disagrees")
            self._source_metadata_cache = metadata
        return self._source_metadata_cache

    def restore_expression(self, target):
        target = _fresh(target)
        if _overlap(target, self.root):
            raise ValueError("Restore output must be outside the package")
        source = self.root / "science.h5"
        if sha256(source) != self.manifest["files"]["science.h5"]["sha256"]:
            raise ValueError("Scientific metadata integrity failure")
        matrix = self._store().sparse_all(max_cells=MAX_CELLS)
        with source.open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst)
        with h5py.File(source, "r") as original, h5py.File(target, "r+") as restored:
            old = original["X"]
            if isinstance(old, h5py.Dataset):
                del restored["X"]
                x = restored.create_dataset("X", shape=matrix.shape, dtype=old.dtype, compression="gzip", compression_opts=4)
                _copy_attributes(old, x)
                for start in range(0, matrix.shape[0], 32):
                    x[start:start + 32] = matrix[start:start + 32].toarray().astype(old.dtype)
            else:
                x = restored["X"]
                for key, values in (("data", matrix.data), ("indices", matrix.indices), ("indptr", matrix.indptr)):
                    del x[key]
                    node = x.create_dataset(key, data=values.astype(old[key].dtype), compression="gzip", compression_opts=4)
                    _copy_attributes(old[key], node)
        if h5_digest(target) != self.manifest["expression"]["semantic_sha256"]:
            raise ValueError("Scientific H5AD semantic reconstruction mismatch")
        return str(target)

    def _verify_files(self):
        self.ledger()
        for name, desc in self.manifest["files"].items():
            if sha256(self.root / name) != desc["sha256"]:
                raise ValueError("Package member digest mismatch: " + name)

    def verify(self):
        import tempfile
        self._verify_files()
        with tempfile.TemporaryDirectory(prefix="stwsi-verify-") as temporary:
            restored = Path(temporary) / "expression.h5ad"
            self.restore_expression(restored)
            _count_preflight(restored, self.manifest["mapping"]["spatial_key"])
        checks = []
        for info in self.manifest["image"]["levels"]:
            h, w, _ = info["shape"]
            digest = hashlib.sha256()
            for y in range(0, h, TILE):
                pixels = self._image_read(info["level"], [0, y, w, min(y + TILE, h)], max_pixels=MAX_STRIPE_BYTES // 3)
                digest.update(memoryview(pixels))
            if digest.hexdigest() != info["pixel_sha256"]:
                raise ValueError("Native raster pixel digest mismatch")
            checks.append({"level": info["level"], "pixels_exact": True, "sha256": digest.hexdigest()})
        metadata = self.source_metadata()
        if len(metadata["native_metadata"]) != len(checks) or len(metadata["native_levels"]) != len(checks):
            raise ValueError("Native metadata page count disagrees")
        for level, descriptor in enumerate(metadata["native_levels"]):
            if descriptor["shape"] != self._level(level)["shape"] or descriptor["level"] != level:
                raise ValueError("Native metadata geometry disagrees")
        return {"ok": True, "h5ad_semantics_exact": True, "levels": checks, "source_metadata_retained": True, "ledger": self.ledger()}

    def restore(self, destination):
        """Write native TIFF tiles without constructing a full image level."""
        import tifffile
        dest = _fresh(destination)
        if _overlap(dest, self.root):
            raise ValueError("Restore output must be outside the package")
        self._verify_files()
        metadata = self.source_metadata()["native_metadata"]
        dest.mkdir(parents=True, exist_ok=False)
        (dest / "INCOMPLETE").write_text("Native WSI restoration in progress\n", encoding="utf-8")
        try:
            self.restore_expression(dest / "expression.h5ad")
            with tifffile.TiffWriter(dest / "native_levels.tif", bigtiff=True) as writer:
                for info, source_info in zip(self.manifest["image"]["levels"], metadata):
                    h, w, _ = info["shape"]
                    restore = source_info["restore"]
                    digest = hashlib.sha256()

                    def tiles():
                        for y in range(0, h, TILE):
                            stripe = np.empty((min(TILE, h - y), w, 3), dtype=np.uint8)
                            for x in range(0, w, TILE):
                                pixels = self.image(info["level"], [x, y, min(x + TILE, w), min(y + TILE, h)])
                                stripe[:, x:x + pixels.shape[1]] = pixels
                                # TiffWriter may stop after the declared final
                                # tile without resuming its generator. Finish
                                # this complete stripe before yielding its last
                                # tile so the final row cannot escape the hash.
                                if x + pixels.shape[1] == w:
                                    digest.update(memoryview(stripe))
                                if pixels.shape != (TILE, TILE, 3):
                                    padded = np.zeros((TILE, TILE, 3), dtype=np.uint8)
                                    padded[:pixels.shape[0], :pixels.shape[1]] = pixels
                                    yield padded
                                else:
                                    yield pixels

                    icc = restore.get("icc_profile_base64")
                    profile = None
                    if icc is not None:
                        profile = base64.b64decode(icc, validate=True)
                    kwargs = {"shape": (h, w, 3), "dtype": np.uint8, "photometric": "rgb", "planarconfig": "contig",
                              "tile": (TILE, TILE), "compression": "deflate", "metadata": None, "maxworkers": 1,
                              "description": restore.get("description"), "software": restore.get("software") or False,
                              "datetime": restore.get("datetime"), "subfiletype": restore.get("subfiletype", 0), "iccprofile": profile}
                    if restore.get("resolution") is not None:
                        kwargs["resolution"] = tuple(tuple(pair) for pair in restore["resolution"])
                        kwargs["resolutionunit"] = restore.get("resolutionunit", 1)
                    writer.write(tiles(), **kwargs)
                    if digest.hexdigest() != info["pixel_sha256"]:
                        raise ValueError("Restored tile stream pixel digest mismatch")
            # Independently decode the exported TIFF, preserving bounded memory.
            with NativeWSISource(dest / "native_levels.tif") as restored:
                if len(restored.levels) != len(self.manifest["image"]["levels"]):
                    raise ValueError("Restored TIFF level count differs")
                for info in self.manifest["image"]["levels"]:
                    h, w, _ = info["shape"]
                    digest = hashlib.sha256()
                    if restored.levels[info["level"]]["shape"] != info["shape"]:
                        raise ValueError("Restored TIFF dimensions differ")
                    for y in range(0, h, TILE):
                        stripe = np.empty((min(TILE, h - y), w, 3), dtype=np.uint8)
                        for x in range(0, w, TILE):
                            pixels = restored.read_tile(info["level"], x // TILE, y // TILE)
                            stripe[:, x:x + pixels.shape[1]] = pixels
                        digest.update(memoryview(stripe))
                    if digest.hexdigest() != info["pixel_sha256"]:
                        raise ValueError("Restored TIFF decoded pixel digest mismatch")
            for name in [SOURCE_METADATA, *self.manifest.get("attachments", [])]:
                with (self.root / name).open("rb") as src, (dest / name).open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                if sha256(dest / name) != self.manifest["files"][name]["sha256"]:
                    raise ValueError("Restore attachment or metadata integrity mismatch")
            shutil.copyfile(self.root / "manifest.json", dest / "source_metadata.json")
            (dest / "INCOMPLETE").unlink()
        except BaseException as exc:
            write_json(dest / "failure.json", {"error": f"{type(exc).__name__}: {exc}"})
            raise
        return str(dest)

    def metrics(self):
        result = super().metrics()
        result["native_wsi"] = {"decoded_image_pixels": self.decoded_image_pixels,
                                "requested_image_pixels": self.requested_image_pixels,
                                "pixel_read_amplification": self.decoded_image_pixels / self.requested_image_pixels if self.requested_image_pixels else None,
                                "decoded_image_cache_bytes": 0, "roi_limit_pixels": MAX_ROI_PIXELS,
                                "stripe_limit_bytes": MAX_STRIPE_BYTES}
        return result
