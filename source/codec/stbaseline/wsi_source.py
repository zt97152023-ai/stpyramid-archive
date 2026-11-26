"""Bounded native RGB TIFF tile access for the opt-in WSI workflow.

This reader deliberately supports a small, explicit TIFF subset.  It never
materializes a whole image level, changes colour space, or silently substitutes
a decoder for unsupported input.  Metadata and tile-directory parsing still
scale with the number of TIFF tiles; pixel memory is bounded independently.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct

import numpy as np
import tifffile


TILE = 256
MAX_REGION_PIXELS = 16_777_216
MAX_LEVELS = 16
MAX_TOTAL_TILE_RECORDS = 4_000_000
MAX_SEGMENT_BYTES = 1_048_576
MAX_TAG_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 32 * 1024 * 1024
SUPPORTED_COMPRESSION = frozenset((1, 5, 8, 32946))
_TYPE_BYTES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1,
               8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8,
               17: 8, 18: 8}


def _integer(value):
    return isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_))


def _encode_tag_value(value):
    """Keep TIFF tag value types rather than stringify unknown metadata."""
    if value is None:
        return {"type": "none"}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "bytes", "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, str):
        return {"type": "str", "value": value}
    if isinstance(value, (bool, np.bool_)):
        return {"type": "bool", "value": bool(value)}
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject:
            raise ValueError("Unsupported object-valued TIFF metadata")
        return {"type": "ndarray", "dtype": value.dtype.str, "shape": list(value.shape),
                "base64": base64.b64encode(value.tobytes(order="C")).decode("ascii")}
    if isinstance(value, np.generic):
        return _encode_tag_value(np.asarray(value))
    if isinstance(value, int):
        return {"type": "int", "value": int(value)}
    if isinstance(value, float):
        return {"type": "float", "hex": value.hex()}
    if isinstance(value, (tuple, list)):
        return {"type": "tuple" if isinstance(value, tuple) else "list",
                "items": [_encode_tag_value(v) for v in value]}
    raise ValueError("Unsupported TIFF metadata value type: " + type(value).__name__)


def decode_tag_value(value):
    """Decode typed values emitted in ``NativeWSISource.metadata[*].tags``."""
    kind = value["type"]
    if kind == "none":
        return None
    if kind == "bytes":
        return base64.b64decode(value["base64"], validate=True)
    if kind in ("str", "bool", "int"):
        return value["value"]
    if kind == "float":
        return float.fromhex(value["hex"])
    if kind in ("tuple", "list"):
        items = [decode_tag_value(v) for v in value["items"]]
        return tuple(items) if kind == "tuple" else items
    if kind == "ndarray":
        dtype = np.dtype(value["dtype"])
        if dtype.hasobject:
            raise ValueError("Unsupported object-valued TIFF metadata")
        shape = tuple(value["shape"])
        if any(not _integer(v) or v < 0 for v in shape):
            raise ValueError("Invalid TIFF metadata array shape")
        payload = base64.b64decode(value["base64"], validate=True)
        if math.prod(shape) * dtype.itemsize != len(payload) or len(payload) > MAX_TAG_BYTES:
            raise ValueError("Invalid TIFF metadata array payload")
        return np.frombuffer(payload, dtype=dtype).reshape(shape).copy()
    raise ValueError("Unknown typed TIFF metadata value")


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _stat_identity(value):
    return (int(value.st_dev), int(value.st_ino), int(value.st_size),
            int(value.st_mtime_ns), int(value.st_ctime_ns))


def _uniform_sample_tag(page, code, expected, default):
    value = page.tags[code].value if code in page.tags else default
    if isinstance(value, (tuple, list)):
        return len(value) == 3 and all(v == expected for v in value)
    return value == expected


class NativeWSISource:
    """Read a main-IFD, strictly decreasing, native-256 tiled RGB8 pyramid.

    Supported compression is uncompressed, LZW, or DEFLATE.  JPEG/YCbCr,
    stripped TIFF, SubIFDs, rotations, extra channels, and other sample types
    are rejected.  ``read_tile`` takes tile indices; ``iter_tiles`` yields
    pixel origins ``(x, y, valid_edge_pixels)`` in row-major order.  Region
    boxes use exclusive pixel bounds ``(x0, y0, x1, y1)`` at the chosen level.

    The stability guard detects file replacement and stat changes.  It is not
    a cryptographic full-source hash or an operating-system access restriction.
    """

    def __init__(self, path, *, max_region_pixels=MAX_REGION_PIXELS):
        if not _integer(max_region_pixels) or not 0 < max_region_pixels <= MAX_REGION_PIXELS:
            raise ValueError("Invalid native image region pixel bound")
        self.max_region_pixels = int(max_region_pixels)
        self.path = Path(path).resolve(strict=True)
        self._stream = None
        self._tiff = None
        self._closed = False
        self._pages = []
        self._levels = []
        self._metadata = []
        self._tiles_decoded = 0
        self._source_bytes_read = 0
        self._decoded_pixels = 0
        self._returned_pixels = 0
        self._source_read_hash = hashlib.sha256()
        try:
            self._path_stat = _stat_identity(self.path.stat())
            self._stream = self.path.open("rb")
            opened = os.fstat(self._stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("Native TIFF source must be a regular file")
            self._stat = _stat_identity(opened)
            # On Windows, Path.stat ctime can be creation time while fstat
            # ctime is change time.  Compare each clock only to its own API's
            # baseline, while requiring common identity/size/mtime to agree.
            if self._stat[:4] != self._path_stat[:4]:
                raise ValueError("Native TIFF source changed during open")
            self._size = int(opened.st_size)
            self._tiff = tifffile.TiffFile(self._stream)
            self._inspect_headers()
            self.unchanged()
        except BaseException:
            self.close()
            raise

    @property
    def levels(self):
        return copy.deepcopy(self._levels)

    @property
    def metadata(self):
        return copy.deepcopy(self._metadata)

    @property
    def identity(self):
        return copy.deepcopy(self._identity)

    def _inspect_headers(self):
        previous = None
        total_tiles = 0
        layout_hash = hashlib.sha256()
        metadata_bytes = 0
        for level, page in enumerate(self._tiff.pages):
            if level >= MAX_LEVELS:
                raise ValueError("Too many native TIFF pyramid levels")
            if page.subifds:
                raise ValueError("SubIFD pyramids are not supported by this source adapter")
            compression = int(page.compression)
            if compression in (6, 7):
                raise ValueError("JPEG TIFF input requires the separate qualified JPEG route")
            if compression not in SUPPORTED_COMPRESSION:
                raise ValueError("Unsupported TIFF compression: " + str(compression))
            if not page.is_tiled or (int(page.tilelength), int(page.tilewidth)) != (TILE, TILE):
                raise ValueError("Require native 256 by 256 TIFF tiles; stripped TIFF is unsupported")
            orientation = int(page.tags[274].value) if 274 in page.tags else 1
            if orientation != 1:
                raise ValueError("Require orientation 1; implicit rotation is forbidden")
            if (page.dtype != np.dtype("uint8") or int(page.photometric) != 2
                    or not _uniform_sample_tag(page, 258, 8, 1)
                    or not _uniform_sample_tag(page, 339, 1, 1)
                    or int(page.planarconfig) != 1 or int(page.samplesperpixel) != 3
                    or int(page.imagedepth) != 1 or int(page.tiledepth) != 1
                    or len(page.shape) != 3 or page.shape[-1] != 3
                    or tuple(page.shape) != (int(page.imagelength), int(page.imagewidth), 3)
                    or page.extrasamples):
                raise ValueError("Require contiguous native RGB8 without extra samples or colour conversion")
            h, w, _ = map(int, page.shape)
            if h < 1 or w < 1:
                raise ValueError("Empty native image level")
            if previous and (h > previous[0] or w > previous[1] or (h, w) == previous):
                raise ValueError("Require strictly decreasing main-IFD native pyramid")
            previous = (h, w)
            nx, ny = (w + TILE - 1) // TILE, (h + TILE - 1) // TILE
            total_tiles += nx * ny
            if total_tiles > MAX_TOTAL_TILE_RECORDS:
                raise ValueError("Native TIFF tile-directory bound exceeded")
            if len(page.dataoffsets) != nx * ny or len(page.databytecounts) != nx * ny:
                raise ValueError("Unexpected TIFF tile directory length")
            layout_hash.update(struct.pack("<QQQ", level, w, h))
            for offset, length in zip(page.dataoffsets, page.databytecounts):
                offset, length = int(offset), int(length)
                if not 0 < length <= MAX_SEGMENT_BYTES or offset <= 0 or offset + length > self._size:
                    raise ValueError("Invalid, sparse, or oversized native TIFF segment")
                layout_hash.update(struct.pack("<QQ", offset, length))
            tags = {}
            for tag in page.tags.values():
                code = int(tag.code)
                if code in (324, 325):
                    continue  # Directory entries are bound by tile_layout_sha256.
                if str(code) in tags:
                    raise ValueError("Duplicate TIFF metadata tag is unsupported")
                dtype = int(tag.dtype)
                if dtype not in _TYPE_BYTES or int(tag.count) * _TYPE_BYTES[dtype] > MAX_TAG_BYTES:
                    raise ValueError("Unsupported or oversized TIFF metadata tag")
                tags[str(code)] = {"name": tag.name, "dtype": dtype,
                                   "count": int(tag.count), "value": _encode_tag_value(tag.value)}
            restore = {"icc_profile_base64": None, "resolution": None,
                       "resolutionunit": int(page.tags[296].value) if 296 in page.tags else 1,
                       "description": page.tags[270].value if 270 in page.tags else None,
                       "software": page.tags[305].value if 305 in page.tags else None,
                       "datetime": page.tags[306].value if 306 in page.tags else None,
                       "subfiletype": int(page.tags[254].value) if 254 in page.tags else 0}
            if 34675 in page.tags:
                icc = page.tags[34675].value
                if not isinstance(icc, bytes):
                    raise ValueError("Unsupported TIFF ICC value")
                restore["icc_profile_base64"] = base64.b64encode(icc).decode("ascii")
            if 282 in page.tags and 283 in page.tags:
                restore["resolution"] = [list(map(int, page.tags[k].value)) for k in (282, 283)]
            meta = {"level": level, "page_index": level, "tags": tags, "restore": restore}
            metadata_bytes += len(_json_bytes(meta))
            if metadata_bytes > MAX_METADATA_BYTES:
                raise ValueError("Native TIFF metadata bound exceeded")
            self._pages.append(page)
            self._levels.append({"level": level, "page_index": level, "shape": [h, w, 3],
                                 "dtype": "uint8", "tile_shape": [TILE, TILE],
                                 "tiles_x": nx, "tiles_y": ny, "compression": compression,
                                 "orientation": 1, "origin_l0": [0, 0]})
            self._metadata.append(meta)
        if not self._levels:
            raise ValueError("Native TIFF contains no image levels")
        h0, w0, _ = self._levels[0]["shape"]
        for info in self._levels:
            h, w, _ = info["shape"]
            info["downsample_x"] = [w0, w]
            info["downsample_y"] = [h0, h]
        header = {"byteorder": self._tiff.byteorder, "bigtiff": bool(self._tiff.is_bigtiff),
                  "levels": self._levels, "native_metadata": self._metadata}
        self._identity = {"name": self.path.name, "path": str(self.path), "bytes": self._size,
                          "mtime_ns": self._stat[3], "byteorder": self._tiff.byteorder,
                          "ctime_ns_fstat": self._stat[4], "ctime_ns_path_stat": self._path_stat[4],
                          "bigtiff": bool(self._tiff.is_bigtiff),
                          "metadata_sha256": hashlib.sha256(_json_bytes(header)).hexdigest(),
                          "tile_layout_sha256": layout_hash.hexdigest(),
                          "whole_file_sha256": None,
                          "stability": "open-file-and-path-stat; not full-source cryptographic identity"}

    def _ensure_open(self):
        if self._closed:
            raise ValueError("Native TIFF source is closed")

    def _level(self, level):
        self._ensure_open()
        if not _integer(level) or not 0 <= level < len(self._levels):
            raise ValueError("Invalid native image level")
        return self._levels[int(level)]

    def inspect(self):
        """Return JSON-compatible header information without decoding pixels."""
        self.unchanged()
        return {"format": "native-tiff-rgb256-source-1", "identity": self.identity,
                "levels": self.levels, "native_metadata": self.metadata,
                "max_region_pixels": self.max_region_pixels,
                "decoder": "tifffile " + tifffile.__version__,
                "exactness": "native RGB samples and retained metadata; not original container bytes"}

    def unchanged(self):
        self._ensure_open()
        if (_stat_identity(os.fstat(self._stream.fileno())) != self._stat
                or _stat_identity(self.path.stat()) != self._path_stat):
            raise ValueError("Native TIFF source changed while open")
        return {"ok": True, "check": "open-file-and-path-stat", "whole_file_sha256": False}

    def _decode_tile(self, level, tx, ty):
        info = self._level(level)
        if (not _integer(tx) or not _integer(ty) or not 0 <= tx < info["tiles_x"]
                or not 0 <= ty < info["tiles_y"]):
            raise ValueError("Invalid native image tile index")
        self.unchanged()
        level, tx, ty = int(level), int(tx), int(ty)
        index = ty * info["tiles_x"] + tx
        page = self._pages[level]
        offset, length = int(page.dataoffsets[index]), int(page.databytecounts[index])
        self._stream.seek(offset)
        blob = self._stream.read(length)
        if len(blob) != length:
            raise ValueError("Truncated native TIFF tile")
        decoded = page.decode(blob, index)[0]
        if (decoded is None or decoded.dtype != np.uint8
                or decoded.shape != (1, TILE, TILE, 3)):
            raise ValueError("Unexpected native TIFF tile decoder shape or dtype")
        self.unchanged()
        h, w, _ = info["shape"]
        pixels = np.ascontiguousarray(decoded[0, :min(TILE, h-ty*TILE), :min(TILE, w-tx*TILE)])
        self._tiles_decoded += 1
        self._source_bytes_read += length
        self._decoded_pixels += TILE * TILE
        self._source_read_hash.update(struct.pack("<QQQQ", level, tx, ty, length))
        self._source_read_hash.update(blob)
        return pixels

    def read_tile(self, level, tx, ty):
        pixels = self._decode_tile(level, tx, ty)
        self._returned_pixels += pixels.shape[0] * pixels.shape[1]
        return pixels

    def iter_tiles(self, level):
        info = self._level(level)
        self.unchanged()
        for ty in range(info["tiles_y"]):
            for tx in range(info["tiles_x"]):
                yield tx * TILE, ty * TILE, self.read_tile(level, tx, ty)
        self.unchanged()

    def read_region(self, level, box):
        info = self._level(level)
        if (not isinstance(box, (tuple, list, np.ndarray))
                or (isinstance(box, np.ndarray) and box.ndim != 1)
                or len(box) != 4 or any(not _integer(v) for v in box)):
            raise ValueError("Native image region requires four integer pixel bounds")
        x0, y0, x1, y1 = map(int, box)
        h, w, _ = info["shape"]
        if not 0 <= x0 < x1 <= w or not 0 <= y0 < y1 <= h:
            raise ValueError("Native image region outside level bounds")
        if (x1-x0) * (y1-y0) > self.max_region_pixels:
            raise ValueError("Native image region exceeds pixel bound")
        output = np.empty((y1-y0, x1-x0, 3), dtype=np.uint8)
        for ty in range(y0//TILE, (y1-1)//TILE+1):
            for tx in range(x0//TILE, (x1-1)//TILE+1):
                tile = self._decode_tile(level, tx, ty)
                xa, ya = max(x0, tx*TILE), max(y0, ty*TILE)
                xb, yb = min(x1, (tx+1)*TILE), min(y1, (ty+1)*TILE)
                output[ya-y0:yb-y0, xa-x0:xb-x0] = tile[ya-ty*TILE:yb-ty*TILE, xa-tx*TILE:xb-tx*TILE]
        self._returned_pixels += (x1-x0) * (y1-y0)
        return output

    def metrics(self):
        return {"tiles_decoded": self._tiles_decoded, "source_bytes_read": self._source_bytes_read,
                "decoded_pixels": self._decoded_pixels, "returned_pixels": self._returned_pixels,
                "decoded_pixel_cache_bytes": 0,
                "source_read_sha256": self._source_read_hash.hexdigest(),
                "source_read_hash_scope": "read order of <u64 level,tx,ty,length> then compressed tile bytes; not whole file",
                "source_bytes_scope": "compressed payload only; header/directory I/O excluded"}

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                if self._tiff is not None:
                    self._tiff.close()
            finally:
                if self._stream is not None:
                    self._stream.close()

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
