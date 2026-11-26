"""Existing lossless components behind one complete, source-independent archive."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import zlib

import h5py
import numpy as np
from PIL import Image

from .integrity import count_schema, h5_digest, sha256, text_ids
from .reader import Archive, MeteredFile

FORMAT = "stlossless-components-1"
COUNT_IDS = ("hdf5", "shared_local", "shared_blocks")
IMAGE_IDS = ("hdf5", "jxl", "residual_zstd", "hybrid_independent", "zstd_tiff")


def _safe_name(name):
    return (isinstance(name, str) and bool(name) and name not in (".", "..")
            and not any(c in name for c in "/\\:") and Path(name).name == name)


def _image_bank(ref, out, method, tile, level, effort):
    from .image_shared import residuals, tiles, write_index
    if method == "jxl":
        import imagecodecs
    else:
        import zstandard
        compressor = zstandard.ZstdCompressor(level=level, write_checksum=True, threads=0)
    records = []
    with (out / "images.bank").open("xb") as stream:
        for native, box in tiles(ref.manifest["image"]["levels"], tile):
            pixels = ref.image(native, box)
            blob = (imagecodecs.jpegxl_encode(pixels, lossless=True, effort=effort, numthreads=1)
                    if method == "jxl" else compressor.compress(residuals(pixels).tobytes()))
            records.append((stream.tell(), len(blob), int(method == "jxl"), zlib.crc32(blob)))
            stream.write(blob)
    write_index(out / "images.index", tile, records)


def _hybrid_bank(workspace, out, tile):
    from .image_shared import read_index, write_index
    paths = [workspace / method for method in ("jxl", "residual_zstd")]
    indexes = [read_index((path / "images.index").read_bytes()) for path in paths]
    if any(t != tile for t, _ in indexes) or len(indexes[0][1]) != len(indexes[1][1]):
        raise ValueError("Hybrid tile grids differ")
    records = []
    with (paths[0] / "images.bank").open("rb") as a, (paths[1] / "images.bank").open("rb") as b, (out / "images.bank").open("xb") as dest:
        for ra, rb in zip(indexes[0][1], indexes[1][1]):
            record, source = (ra, a) if ra[1] <= rb[1] else (rb, b)
            offset, length, code, crc = record
            source.seek(offset)
            blob = source.read(length)
            if len(blob) != length or zlib.crc32(blob) != crc:
                raise ValueError("Hybrid source tile integrity failure")
            records.append((dest.tell(), length, code, crc))
            dest.write(blob)
    write_index(out / "images.index", tile, records)


def prepare_components(reference, workspace, *, tile=256, zstd_level=19,
                       jpegxl_effort=9, block_pixels=1024):
    """Build a bounded portfolio; failed optional components remain explicit."""
    reference, workspace = Path(reference), Path(workspace)
    workspace.mkdir(parents=True, exist_ok=False)
    result = {"counts": [], "images": [], "excluded": []}
    with Archive(reference) as ref:
        m = ref.manifest
        result["counts"].append({"id": "hdf5", "files": {"expression.h5ad": reference / "expression.h5ad"},
                                 "expression": copy.deepcopy(m["expression"]), "extra": {}})
        result["images"].append({"id": "hdf5", "files": {"images.h5": reference / "images.h5"},
                                 "image": copy.deepcopy(m["image"]), "extra": {}})
        for ident in COUNT_IDS[1:]:
            try:
                from .comparison import csr_from_h5, science_skeleton
                from .shared_backend import write_count_store
                dest = workspace / ident
                dest.mkdir()
                science_skeleton(reference / "expression.h5ad", dest / "science.h5")
                matrix = csr_from_h5(reference / "expression.h5ad")
                md = ref.metadata()
                method = {"shared_local": "Shared_local", "shared_blocks": "Shared_blocks"}[ident]
                index = write_count_store(matrix, md, md["level0_xy"], dest, method, block_pixels)
                expression = dict(m["expression"], path="science.h5", storage=method + "-B8-K32")
                result["counts"].append({"id": ident, "files": {n: dest / n for n in
                    ("science.h5", "counts.bank", "counts.index.json")}, "expression": expression,
                    "extra": {"count_parameters": {"method": method, "block_pixels": block_pixels,
                        "blocks": len(index["blocks"]), "decoded_count_cache": False}}})
            except Exception as exc:
                result["excluded"].append({"component": ident, "reason": f"{type(exc).__name__}: {exc}"})
        for ident in IMAGE_IDS[1:]:
            try:
                dest = workspace / ident
                dest.mkdir()
                info = copy.deepcopy(m["image"])
                if ident == "zstd_tiff":
                    import tifffile
                    with tifffile.TiffWriter(dest / "images.tif") as writer:
                        for native in info["levels"]:
                            h, w, _ = native["shape"]
                            writer.write(ref.image(native["level"], [0, 0, w, h]), photometric="rgb",
                                tile=(tile, tile), compression="zstd", compressionargs={"level": zstd_level},
                                predictor=2, metadata=None, subfiletype=int(native["level"] != 0), maxworkers=1)
                    names = ("images.tif",)
                    info.update(path="images.tif", codec="tiled-tiff-zstd-horizontal")
                else:
                    if ident == "hybrid_independent":
                        _hybrid_bank(workspace, dest, tile)
                    else:
                        _image_bank(ref, dest, ident, tile, zstd_level, jpegxl_effort)
                    names = ("images.bank", "images.index")
                    info.update(path="images.bank", codec="indexed-independent-image-tiles-v1", index="images.index")
                result["images"].append({"id": ident, "files": {n: dest / n for n in names}, "image": info,
                    "extra": {"image_parameters": {"tile": tile, "zstd_level": zstd_level,
                        "jpegxl_effort": jpegxl_effort, "dictionary_training": False}}})
            except Exception as exc:
                result["excluded"].append({"component": ident, "reason": f"{type(exc).__name__}: {exc}"})
    return result


class ComponentArchive(Archive):
    """Full H5AD semantics and native RGB8 pixels with independent component IO."""
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        if (self.root / "INCOMPLETE").exists():
            raise ValueError("Incomplete archive")
        manifest_path = self.root / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("Unsafe manifest")
        blob = manifest_path.read_bytes()
        self.manifest, self.manifest_bytes = json.loads(blob), len(blob)
        m = self.manifest
        if m.get("format") != FORMAT or m.get("count_component") not in COUNT_IDS or m.get("image_component") not in IMAGE_IDS:
            raise ValueError("Unsupported lossless component archive")
        count_files = {"expression.h5ad"} if m["count_component"] == "hdf5" else {"science.h5", "counts.bank", "counts.index.json"}
        image_files = ({"images.h5"} if m["image_component"] == "hdf5" else
                       {"images.tif"} if m["image_component"] == "zstd_tiff" else {"images.bank", "images.index"})
        attachments = m.get("attachments", [])
        reserved = count_files | image_files | {"manifest.json", "INCOMPLETE", "failure.json", "native_levels.tif", "source_metadata.json"}
        if len(set(attachments)) != len(attachments) or any(not _safe_name(n) or n in reserved for n in attachments):
            raise ValueError("Unsafe attachment names")
        if set(m["files"]) != count_files | image_files | set(attachments):
            raise ValueError("Unexpected component members")
        expected_expression = "expression.h5ad" if m["count_component"] == "hdf5" else "science.h5"
        expected_image = "images.h5" if m["image_component"] == "hdf5" else "images.tif" if m["image_component"] == "zstd_tiff" else "images.bank"
        if m["expression"]["path"] != expected_expression or m["image"]["path"] != expected_image:
            raise ValueError("Component paths disagree")
        for name, desc in m["files"].items():
            p = self.root / name
            if not _safe_name(name) or p.is_symlink() or p.resolve().parent != self.root or not p.is_file() or p.stat().st_size != desc["bytes"]:
                raise ValueError("Unsafe or size-mismatched member: " + name)
        self.handles, self.files, self._metadata = {}, {}, None
        self._count_store, self._tiff, self._entries = None, None, None
        self.index_read_bytes, self.image_tiles_decoded = 0, 0
        self._level_start, self._decompressors = [], {}
        self.ledger()

    def metadata(self):
        if self._metadata is None:
            f = self._open(self.manifest["expression"]["path"])
            rows, genes = text_ids(f["obs"]), text_ids(f["var"])
            xy = np.asarray(f["obsm"][self.manifest["mapping"]["spatial_key"]])
            if [len(rows), len(genes)] != self.manifest["expression"]["shape"] or xy.shape != (len(rows), 2):
                raise ValueError("Metadata shape mismatch")
            transform = np.asarray(self.manifest["mapping"]["xy_to_level0"])
            self._metadata = {"spot_ids": rows, "gene_ids": genes, "coordinates": xy,
                              "level0_xy": (np.c_[xy, np.ones(len(xy))] @ transform.T)[:, :2]}
        return self._metadata

    def counts(self, rows=None, genes=None, *, max_cells=20_000_000):
        if self.manifest["count_component"] == "hdf5":
            return super().counts(rows, genes, max_cells=max_cells)
        md = self.metadata()
        ri, gi = self._select(rows, md["spot_ids"]), self._select(genes, md["gene_ids"])
        if len(ri) * len(gi) > max_cells:
            raise ValueError("Query too large; read in row batches")
        values = np.zeros((len(ri), len(gi)), np.uint64)
        if len(ri) and len(gi):
            if self._count_store is None:
                from .shared_backend import CountStore
                if sha256(self.root / "counts.index.json") != self.manifest["files"]["counts.index.json"]["sha256"]:
                    raise ValueError("Count index integrity failure")
                self.index_read_bytes += (self.root / "counts.index.json").stat().st_size
                self._count_store = CountStore(self.root)
            values = self._count_store.read(ri, gi)
        return {"counts": values, "spot_ids": np.asarray([md["spot_ids"][i] for i in ri], dtype=str),
                "gene_ids": np.asarray([md["gene_ids"][i] for i in gi], dtype=str),
                "coordinates": md["coordinates"][ri], "level0_xy": md["level0_xy"][ri]}

    def _image_setup(self):
        from .image_shared import ImageSharedArchive
        ImageSharedArchive._image_setup(self)
        if any(record[2] not in (0, 1) for record in self._entries):
            raise ValueError("Unregistered dictionary codec")

    def image(self, level, bbox):
        if self.manifest["image_component"] == "hdf5":
            return super().image(level, bbox)
        info = self._level(level)
        if len(bbox) != 4 or any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)) for v in bbox):
            raise ValueError("Integer native image box required")
        x0, y0, x1, y1 = map(int, bbox)
        h, w, _ = info["shape"]
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            raise ValueError("Image ROI outside native level")
        if self.manifest["image_component"] == "zstd_tiff":
            import tifffile
            if self._tiff is None:
                self.files["images.tif"] = MeteredFile(self.root / "images.tif")
                self._tiff = tifffile.TiffFile(self.files["images.tif"])
            page = self._tiff.pages[level]
            if not page.is_tiled or page.planarconfig != 1 or page.dtype != np.uint8 or page.shape != (h, w, 3):
                raise ValueError("Only contiguous tiled RGB TIFF is supported")
            th, tw = page.tilelength, page.tilewidth
            stream = self.files["images.tif"]
        else:
            if self._entries is None:
                self._image_setup()
            th = tw = self._image_tile
            stream = self.files["images.bank"]
        output = np.empty((y1-y0, x1-x0, 3), np.uint8)
        nx = (w+tw-1)//tw
        for ty in range(y0//th, (y1-1)//th+1):
            for tx in range(x0//tw, (x1-1)//tw+1):
                index = ty*nx+tx
                if self.manifest["image_component"] == "zstd_tiff":
                    offset, length = page.dataoffsets[index], page.databytecounts[index]
                    stream.seek(offset)
                    blob = stream.read(length)
                    if len(blob) != length:
                        raise ValueError("Truncated TIFF tile")
                    pixels = page.decode(blob, index)[0][0]
                    if pixels.dtype != np.uint8 or pixels.ndim != 3 or pixels.shape[2] != 3:
                        raise ValueError("Decoded TIFF tile type mismatch")
                else:
                    offset, length, codec, crc = self._entries[self._level_start[level]+index]
                    stream.seek(offset)
                    blob = stream.read(length)
                    if len(blob) != length or zlib.crc32(blob) != crc:
                        raise ValueError("Image tile integrity failure")
                    height, width = min(th, h-ty*th), min(tw, w-tx*tw)
                    if codec == 1:
                        import imagecodecs
                        pixels = imagecodecs.jpegxl_decode(blob, numthreads=1)
                    else:
                        import zstandard
                        from .image_shared import reconstruct
                        raw = zstandard.ZstdDecompressor().decompress(blob, max_output_size=height*width*3, allow_extra_data=False)
                        if len(raw) != height*width*3:
                            raise ValueError("Residual tile size mismatch")
                        pixels = reconstruct(np.frombuffer(raw, np.uint8).reshape(3, height, width))
                    if pixels.shape != (height, width, 3) or pixels.dtype != np.uint8:
                        raise ValueError("Decoded image shape/type mismatch")
                xa, ya, xb, yb = max(x0, tx*tw), max(y0, ty*th), min(x1, (tx+1)*tw), min(y1, (ty+1)*th)
                output[ya-y0:yb-y0, xa-x0:xb-x0] = pixels[ya-ty*th:yb-ty*th, xa-tx*tw:xb-tx*tw]
                self.image_tiles_decoded += 1
        return output

    def metrics(self):
        result = super().metrics()
        result.update(index_read_bytes=self.index_read_bytes, image_tiles_decoded=self.image_tiles_decoded)
        result["total_file_read_bytes"] += self.index_read_bytes
        if self._count_store is not None:
            c = self._count_store
            result["shared_count"] = dict(c.file.metrics(), index_bytes=c.index_bytes,
                                          decoded_rows=c.decoded_rows, decoded_blocks=c.decoded_blocks)
            result["total_file_read_bytes"] += c.index_bytes + c.file.metrics()["file_read_bytes"]
        return result

    def restore_expression(self, target):
        target = Path(target).resolve()
        if target == self.root or self.root in target.parents:
            raise ValueError("Output must be outside the immutable archive")
        if target.exists():
            raise FileExistsError(target)
        if self.manifest["count_component"] == "hdf5":
            shutil.copyfile(self.root / "expression.h5ad", target)
        else:
            from .comparison import BaselineArchive
            BaselineArchive.restore_expression(self, target)
        if h5_digest(target) != self.manifest["expression"]["semantic_sha256"]:
            raise ValueError("H5AD semantic digest mismatch")

    def verify(self):
        ledger = self.ledger()
        for name, expected in self.manifest["files"].items():
            if sha256(self.root / name) != expected["sha256"]:
                raise ValueError("File SHA256 mismatch: " + name)
        with tempfile.TemporaryDirectory(prefix="stlossless-verify-") as temporary:
            expression = Path(temporary) / "expression.h5ad"
            self.restore_expression(expression)
            with h5py.File(expression, "r") as source:
                count_schema(source, self.manifest["mapping"]["spatial_key"])
        checks = []
        for info in self.manifest["image"]["levels"]:
            h, w, _ = info["shape"]
            digest = hashlib.sha256()
            for y in range(0, h, 256):
                pixels = self.image(info["level"], [0, y, w, min(y+256, h)])
                if pixels.dtype != np.uint8 or pixels.shape != (min(256, h-y), w, 3):
                    raise ValueError("Image schema mismatch")
                self._verify_native_chunk(info, y, pixels)
                digest.update(pixels.tobytes())
            if digest.hexdigest() != info["pixel_sha256"]:
                raise ValueError("Native pixel digest mismatch")
            checks.append({"level": info["level"], "pixels_exact": True})
        return {"ok": True, "h5ad_semantics_exact": True, "levels": checks, "ledger": ledger}

    def _verify_native_chunk(self, info, y, pixels):
        """Optional derived-data checks during the existing exact native scan."""
        return None

    def restore(self, destination):
        dest = Path(destination).resolve()
        if dest == self.root or self.root in dest.parents:
            raise ValueError("Output must be outside the immutable archive")
        if dest.exists():
            raise FileExistsError(dest)
        self.verify()
        dest.mkdir(parents=True, exist_ok=False)
        self.restore_expression(dest / "expression.h5ad")
        frames = []
        try:
            for info in self.manifest["image"]["levels"]:
                h, w, _ = info["shape"]
                frame = Image.fromarray(self.image(info["level"], [0, 0, w, h]))
                profile = info.get("source_tiff_tags", {}).get("34675")
                if isinstance(profile, dict) and "base64" in profile:
                    frame.info["icc_profile"] = base64.b64decode(profile["base64"])
                frames.append(frame)
            frames[0].save(dest / "native_levels.tif", save_all=True, append_images=frames[1:], compression="tiff_adobe_deflate")
        finally:
            for frame in frames:
                frame.close()
        for name in self.manifest.get("attachments", []):
            shutil.copyfile(self.root / name, dest / name)
        shutil.copyfile(self.root / "manifest.json", dest / "source_metadata.json")
        return str(dest)

    def close(self):
        if self._count_store is not None:
            self._count_store.close()
        if self._tiff is not None:
            self._tiff.close()
        super().close()
