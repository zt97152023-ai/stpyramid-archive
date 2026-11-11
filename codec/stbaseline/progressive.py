"""Tile-local RGB8 precision layers; exact APIs always decode all three layers.

This reference backend uses existing Zstd and makes no entropy-coding novelty
claim. A prefix is useful only at declared layer boundaries with its full index.
"""
import copy
import hashlib
from pathlib import Path
import shutil
import struct
import tempfile
import zlib

import numpy as np
import zstandard as zstd

from .comparison import open_archive
from .integrity import sha256, write_json
from .reader import MeteredFile
from .shared_backend import SharedArchive

FORMAT = "stbaseline-progressive-1"
METHOD = "Progressive_bits"
PRECISIONS = (4, 6, 8)
WIDTHS = (4, 2, 2)
SHIFTS = (4, 2, 0)
HEADER = struct.Struct("<8sIIII")
ENTRY = struct.Struct("<QIB3xI")
MAGIC = b"STPROG01"


def _integer(value):
    return isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_))


def pack_symbols(values, bits):
    """Pack low-bit symbols MSB first, zero padding the final byte."""
    if bits not in (2, 4):
        raise ValueError("Only two/four bit symbols are supported")
    values = np.asarray(values)
    if values.dtype != np.uint8 or np.any(values >= (1 << bits)):
        raise ValueError("Symbol outside declared precision")
    flat = values.ravel()
    group = 8 // bits
    padded = np.zeros(((flat.size + group - 1) // group) * group, np.uint8)
    padded[:flat.size] = flat
    shifts = np.arange(8 - bits, -1, -bits, dtype=np.uint8)
    return np.bitwise_or.reduce(padded.reshape(-1, group) << shifts, axis=1).tobytes()


def unpack_symbols(raw, bits, count):
    if bits not in (2, 4) or not _integer(count) or count < 0:
        raise ValueError("Invalid symbol dimensions")
    if len(raw) != (count * bits + 7) // 8:
        raise ValueError("Packed layer length mismatch")
    unused = (-count * bits) % 8
    if unused and raw[-1] & ((1 << unused) - 1):
        raise ValueError("Nonzero layer padding")
    shifts = np.arange(8 - bits, -1, -bits, dtype=np.uint8)
    return ((np.frombuffer(raw, np.uint8)[:, None] >> shifts) & ((1 << bits) - 1)).ravel()[:count]


def _grid(levels, tile):
    for info in levels:
        h, w, _ = info["shape"]
        for y in range(0, h, tile):
            for x in range(0, w, tile):
                yield info["level"], [x, y, min(x + tile, w), min(y + tile, h)]


def pack_progressive(count_source, output, *, method=METHOD, tile=256, zstd_level=9):
    """Create a separate immutable package; failed builds remain INCOMPLETE."""
    if method != METHOD or not _integer(tile) or not 1 <= tile <= 4096:
        raise ValueError("Invalid progressive method/tile size")
    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    (out / "INCOMPLETE").write_text("building progressive archive\n")
    with open_archive(count_source) as base:
        if base.manifest.get("method") != "Shared_local":
            raise ValueError("Expected qualified Shared_local source")
        m = copy.deepcopy(base.manifest)
        # Check all source members, including the native image that will be read.
        for name, desc in m["files"].items():
            if sha256(base.root / name) != desc["sha256"]:
                raise ValueError("Source component integrity: " + name)
        for name in ("counts.bank", "counts.index.json", "science.h5", *m.get("attachments", [])):
            (out / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(base.root / name, out / name)
        levels = m["image"]["levels"]
        for i, info in enumerate(levels):
            if info["level"] != i or info["dtype"] != "uint8" or len(info["shape"]) != 3 or info["shape"][2] != 3:
                raise ValueError("Contiguous native RGB8 levels required")
        compressor = zstd.ZstdCompressor(level=zstd_level, write_checksum=True, threads=0)
        records = [[], [], []]
        # Disk spools avoid keeping an image pyramid or its encoded layers in RAM.
        with tempfile.TemporaryDirectory(prefix="stprogressive_", dir=out.parent) as tmp:
            spools = [open(Path(tmp) / str(i), "xb") for i in range(3)]
            try:
                for j, (level, box) in enumerate(_grid(levels, tile)):
                    pixels = base.image(level, box)
                    for layer, (width, shift) in enumerate(zip(WIDTHS, SHIFTS)):
                        raw = pack_symbols((pixels >> shift) & ((1 << width) - 1), width)
                        encoded = compressor.compress(raw)
                        codec, blob = (1, encoded) if len(encoded) < len(raw) else (0, raw)
                        records[layer].append((len(blob), codec, zlib.crc32(blob)))
                        spools[layer].write(blob)
                    if j % 256 == 0:
                        print("progressive tiles", j, flush=True)
            finally:
                for f in spools:
                    f.close()
            n = len(records[0])
            boundaries, offset = [], 0
            with open(out / "images.bank", "xb") as bank, open(out / "images.index", "xb") as index:
                index.write(HEADER.pack(MAGIC, tile, n, 3, 1))
                for layer, group in enumerate(records):
                    with open(Path(tmp) / str(layer), "rb") as source:
                        shutil.copyfileobj(source, bank)
                    for length, codec, crc in group:
                        index.write(ENTRY.pack(offset, length, codec, crc))
                        offset += length
                    boundaries.append(offset)
        m.update(format=FORMAT, method=METHOD)
        m["image"].update(path="images.bank", index="images.index", codec="rgb8-packed-precision-zstd-v1",
                          predictor="none; MSB-first precision partition with zero-fill reconstruction",
                          design={"tile": tile, "tiles": n, "precisions": list(PRECISIONS),
                                  "layer_bits": list(WIDTHS), "bank_prefix_ends": boundaries,
                                  "zstd_level": zstd_level, "zstd_checksum": True, "raw_fallback": True,
                                  "layout": "layer-major; native-level then row-major tile order",
                                  "index_entry_bytes": ENTRY.size, "index_header_bytes": HEADER.size,
                                  "final_pixels_exact": True, "generic_runtime_bytes_excluded": True})
        m["files"] = {p.relative_to(out).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)}
                      for p in sorted(out.rglob("*")) if p.is_file() and p.name != "INCOMPLETE"}
        write_json(out / "manifest.json", m)
    (out / "INCOMPLETE").unlink()
    with ProgressiveArchive(out) as a:
        return a.ledger()


class ProgressiveArchive(SharedArchive):
    FORMAT = FORMAT
    METHODS = (METHOD,)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._entries = None
        self._level_start = []
        self._closed = False
        self.image_layers_decoded = 0
        self._decoder = zstd.ZstdDecompressor()

    def _image_setup(self):
        blob = (self.root / "images.index").read_bytes()
        self.index_read_bytes += len(blob)
        if hashlib.sha256(blob).hexdigest() != self.manifest["files"]["images.index"]["sha256"]:
            raise ValueError("Progressive index integrity")
        if len(blob) < HEADER.size:
            raise ValueError("Short progressive index")
        magic, tile, n, layers, version = HEADER.unpack_from(blob)
        if magic != MAGIC or layers != 3 or version != 1 or not 1 <= tile <= 4096 or n < 1:
            raise ValueError("Invalid progressive index header")
        if len(blob) != HEADER.size + layers * n * ENTRY.size:
            raise ValueError("Invalid progressive index length")
        starts, total = [], 0
        for i, info in enumerate(self.manifest["image"]["levels"]):
            h, w, channels = info["shape"]
            if info["level"] != i or h < 1 or w < 1 or channels != 3 or info["dtype"] != "uint8":
                raise ValueError("Invalid native RGB8 level")
            starts.append(total)
            total += ((w + tile - 1) // tile) * ((h + tile - 1) // tile)
        if total != n:
            raise ValueError("Progressive tile coverage")
        entries = [ENTRY.unpack_from(blob, HEADER.size + i * ENTRY.size) for i in range(layers * n)]
        expected, boundaries = 0, []
        declared_size = self.manifest["files"]["images.bank"]["bytes"]
        for i, (offset, length, codec, _) in enumerate(entries):
            if offset != expected or length < 1 or codec not in (0, 1) or offset + length > declared_size:
                raise ValueError("Invalid progressive layer span")
            expected += length
            if (i + 1) % n == 0:
                boundaries.append(expected)
        design = self.manifest["image"]["design"]
        if expected != declared_size or boundaries != design["bank_prefix_ends"] or design["tile"] != tile or design["precisions"] != list(PRECISIONS):
            raise ValueError("Progressive layout mismatch")
        # A partially delivered bank may serve complete prefixes, but never pass
        # full verification. Each requested frame is checked before use.
        if (self.root / "images.bank").stat().st_size > declared_size:
            raise ValueError("Progressive bank trailing data")
        self.files["images.bank"] = MeteredFile(self.root / "images.bank")
        self._image_tile, self._ntiles = tile, n
        self._level_start, self._entries = starts, entries

    def _layer(self, tile_id, layer, shape):
        offset, length, codec, crc = self._entries[layer * self._ntiles + tile_id]
        f = self.files["images.bank"]
        f.seek(offset)
        blob = f.read(length)
        if len(blob) != length or zlib.crc32(blob) != crc:
            raise ValueError("Progressive layer integrity or truncation")
        count = int(np.prod(shape))
        packed_size = (count * WIDTHS[layer] + 7) // 8
        if codec == 1:
            # Zstd ignores max_output_size for frames with a declared content
            # size: check it before decompression to keep allocations bounded.
            if zstd.frame_content_size(blob) != packed_size:
                raise ValueError("Progressive Zstd content size mismatch")
            raw = self._decoder.decompress(blob, max_output_size=packed_size, allow_extra_data=False)
        else:
            raw = blob
        symbols = unpack_symbols(raw, WIDTHS[layer], count).reshape(shape)
        self.image_layers_decoded += 1
        return symbols << SHIFTS[layer]

    def start_image(self, level, bbox, *, bits=4):
        session = ImageSession(self, level, bbox)
        session.refine(bits)
        return session

    def image_progressive(self, level, bbox, *, bits=4):
        return self.start_image(level, bbox, bits=bits).result()

    def image(self, level, bbox):
        """The common image/region/restore contract remains exact."""
        return self.image_progressive(level, bbox, bits=8)["image"]

    def metrics(self):
        return {**super().metrics(), "image_layers_decoded": self.image_layers_decoded}

    def close(self):
        self._closed = True
        super().close()


class ImageSession:
    """Same-ROI continuation; holds ROI pixels, never a full decoded tile cache.

    A layer is committed only after every touched tile passes integrity checks.
    Readers/sessions are synchronous and must not share a reader across threads.
    """
    def __init__(self, archive, level, bbox):
        if archive._closed:
            raise ValueError("Closed progressive reader")
        if not _integer(level):
            raise ValueError("Integer native level required")
        info = archive._level(int(level))
        if len(bbox) != 4 or not all(_integer(v) for v in bbox):
            raise ValueError("Integer native image box required")
        x0, y0, x1, y1 = map(int, bbox)
        h, w, _ = info["shape"]
        if not 0 <= x0 < x1 <= w or not 0 <= y0 < y1 <= h:
            raise ValueError("Image ROI outside native level")
        self.archive, self.level = archive, int(level)
        self.bbox, self.shape = (x0, y0, x1, y1), (h, w, 3)
        self._pixels = np.zeros((y1 - y0, x1 - x0, 3), np.uint8)
        self.bits = 0

    def refine(self, bits):
        if not _integer(bits) or bits not in PRECISIONS or bits < self.bits:
            raise ValueError("Precision must advance through 4, 6, 8")
        a = self.archive
        if a._closed:
            raise ValueError("Closed progressive reader")
        if a._entries is None:
            a._image_setup()
        x0, y0, x1, y1 = self.bbox
        h, w, _ = self.shape
        tile = a._image_tile
        nx = (w + tile - 1) // tile
        for layer, precision in enumerate(PRECISIONS):
            if self.bits >= precision or precision > bits:
                continue
            delta = np.zeros_like(self._pixels)
            for ty in range(y0 // tile, (y1 - 1) // tile + 1):
                for tx in range(x0 // tile, (x1 - 1) // tile + 1):
                    tile_id = a._level_start[self.level] + ty * nx + tx
                    shape = (min(tile, h - ty * tile), min(tile, w - tx * tile), 3)
                    pixels = a._layer(tile_id, layer, shape)
                    xa, ya = max(x0, tx * tile), max(y0, ty * tile)
                    xb, yb = min(x1, (tx + 1) * tile), min(y1, (ty + 1) * tile)
                    delta[ya - y0:yb - y0, xa - x0:xb - x0] = pixels[ya - ty * tile:yb - ty * tile, xa - tx * tile:xb - tx * tile]
            self._pixels |= delta
            self.bits = precision
        return self

    def result(self):
        if self.bits not in PRECISIONS:
            raise ValueError("No complete precision layer")
        return {"image": self._pixels.copy(), "bits": np.asarray(self.bits),
                "max_abs_error": np.asarray((1 << (8 - self.bits)) - 1),
                "exact": np.asarray(self.bits == 8)}
