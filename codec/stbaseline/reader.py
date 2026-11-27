"""Source-independent reader; metrics are application file reads, not device IO."""
import hashlib
import base64
import io
import json
import math
from pathlib import Path
import shutil

import h5py
import numpy as np
from PIL import Image

from .archive import FORMAT
from .integrity import count_schema, h5_digest, sha256, text_ids, write_json


class MeteredFile(io.RawIOBase):
    def __init__(self, path):
        super().__init__()
        self.handle = open(path, "rb", buffering=0)
        self.ranges = []

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, offset, whence=0):
        return self.handle.seek(offset, whence)

    def tell(self):
        return self.handle.tell()

    def readinto(self, buffer):
        position = self.tell()
        n = self.handle.readinto(buffer)
        self.ranges.append((position, n))
        return n

    def close(self):
        if not self.closed:
            self.handle.close()
        super().close()

    def metrics(self):
        end, unique = 0, 0
        for offset, length in sorted(self.ranges):
            unique += max(0, offset + length - max(offset, end))
            end = max(end, offset + length)
        return {"file_read_bytes": sum(n for _, n in self.ranges), "read_calls": len(self.ranges),
                "unique_file_bytes": unique}


class Archive:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.handles, self.files = {}, {}
        if (self.root / "INCOMPLETE").exists():
            raise ValueError("Incomplete archive")
        blob = (self.root / "manifest.json").read_bytes()
        self.manifest_bytes = len(blob)
        self.manifest = json.loads(blob)
        m = self.manifest
        if m.get("format") != FORMAT:
            raise ValueError("Unsupported archive version")
        if m["image"].get("codec") != "hdf5-gzip-shuffle-fletcher32":
            raise ValueError("Unsupported image codec; register an explicit decoder before reading")
        if set(m["files"]) != {"expression.h5ad", "images.h5", *m.get("attachments", [])} or m["expression"]["path"] != "expression.h5ad" or m["image"]["path"] != "images.h5":
            raise ValueError("Unexpected archive members")
        for name in m["files"]:
            if Path(name).name != name or name in ("", ".", "..", "manifest.json", "source_metadata.json", "native_levels.tif"):
                raise ValueError("Unsafe archive filename")
            p = self.root / name
            if p.is_symlink() or p.resolve().parent != self.root or p.stat().st_size != m["files"][name]["bytes"]:
                raise ValueError("Unsafe or size-mismatched member")
        self._metadata = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        for handle in self.handles.values():
            handle.close()
        for f in self.files.values():
            f.close()
        self.handles.clear()

    def _open(self, name):
        if name not in self.handles:
            stream = MeteredFile(self.root / name)
            self.files[name] = stream
            try:
                self.handles[name] = h5py.File(stream, "r")
            except Exception:
                stream.close()
                raise
        return self.handles[name]

    def metadata(self):
        if self._metadata is None:
            f = self._open("expression.h5ad")
            rows, genes = text_ids(f["obs"]), text_ids(f["var"])
            xy = np.asarray(f["obsm"][self.manifest["mapping"]["spatial_key"]])
            if [len(rows), len(genes)] != self.manifest["expression"]["shape"] or xy.shape != (len(rows), 2):
                raise ValueError("Metadata shape mismatch")
            t = np.asarray(self.manifest["mapping"]["xy_to_level0"])
            pixel_xy = np.c_[xy, np.ones(len(xy))] @ t.T
            self._metadata = {"spot_ids": rows, "gene_ids": genes, "coordinates": xy,
                              "level0_xy": pixel_xy[:, :2]}
        return self._metadata

    @staticmethod
    def _select(selection, identifiers):
        if selection is None:
            return np.arange(len(identifiers), dtype=np.int64)
        lookup = {}
        ambiguous = set()
        for i, label in enumerate(identifiers):
            if label in lookup:
                ambiguous.add(label)
            else:
                lookup[label] = i
        result = []
        for value in selection:
            if isinstance(value, str):
                if value in ambiguous:
                    raise ValueError("Ambiguous identifier; select an original integer position: " + value)
                if value not in lookup:
                    raise KeyError(value)
                value = lookup[value]
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or not 0 <= value < len(identifiers):
                raise ValueError("Row/gene selections must be valid IDs or integer indices")
            result.append(int(value))
        if len(set(result)) != len(result):
            raise ValueError("Duplicate selection IDs")
        return np.asarray(result, dtype=np.int64)

    def counts(self, rows=None, genes=None, *, max_cells=20_000_000):
        meta = self.metadata()
        ri, gi = self._select(rows, meta["spot_ids"]), self._select(genes, meta["gene_ids"])
        if len(ri) * len(gi) > max_cells:
            raise ValueError("Query too large; read in row batches")
        result = np.zeros((len(ri), len(gi)), dtype=np.uint64)
        x = self._open("expression.h5ad")["X"]
        inverse = {int(g): i for i, g in enumerate(gi)}
        for target, row in enumerate(ri):
            if isinstance(x, h5py.Dataset):
                result[target] = x[int(row), :][gi].astype(np.uint64)
            else:
                lo, hi = map(int, x["indptr"][int(row):int(row) + 2])
                idx, values = x["indices"][lo:hi], x["data"][lo:hi]
                for g, value in zip(idx, values):
                    if int(g) in inverse:
                        result[target, inverse[int(g)]] = int(value)
        return {"counts": result, "spot_ids": np.asarray([meta["spot_ids"][i] for i in ri], dtype=str),
                "gene_ids": np.asarray([meta["gene_ids"][i] for i in gi], dtype=str),
                "coordinates": meta["coordinates"][ri], "level0_xy": meta["level0_xy"][ri]}

    def _level(self, level):
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level < len(self.manifest["image"]["levels"]):
            raise ValueError("Invalid native image level")
        return self.manifest["image"]["levels"][level]

    def image(self, level, bbox):
        info = self._level(level)
        if len(bbox) != 4 or any(isinstance(v, bool) or not isinstance(v, (int, np.integer)) for v in bbox):
            raise ValueError("Image bbox requires four integer native-level coordinates")
        x0, y0, x1, y1 = map(int, bbox)
        h, w, _ = info["shape"]
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            raise ValueError("Image ROI is empty or outside native-level bounds")
        return self._open("images.h5")[str(level)][y0:y1, x0:x1, :]

    def region(self, bbox, *, level=0, genes=None):
        """Half-open level0 ROI, with native-level covering rectangle (no resampling)."""
        if len(bbox) != 4 or not np.isfinite(bbox).all():
            raise ValueError("Invalid level0 ROI")
        x0, y0, x1, y1 = bbox
        h0, w0, _ = self._level(0)["shape"]
        if not (0 <= x0 < x1 <= w0 and 0 <= y0 < y1 <= h0):
            raise ValueError("ROI outside level0")
        xy = self.metadata()["level0_xy"]
        rows = np.flatnonzero((xy[:, 0] >= x0) & (xy[:, 0] < x1) & (xy[:, 1] >= y0) & (xy[:, 1] < y1))
        result = self.counts(rows, genes)
        h, w, _ = self._level(level)["shape"]
        box = [math.floor(x0 * w / w0), math.floor(y0 * h / h0), math.ceil(x1 * w / w0), math.ceil(y1 * h / h0)]
        result.update(image=self.image(level, box), image_bbox=np.asarray(box), level0_bbox=np.asarray(bbox), level=np.asarray(level))
        return result

    def metrics(self):
        members = {name: stream.metrics() for name, stream in self.files.items()}
        return {"scope": "cumulative application file-object reads including archive open; OS cache not controlled",
                "manifest_read_bytes": self.manifest_bytes, "members": members,
                "total_file_read_bytes": self.manifest_bytes + sum(m["file_read_bytes"] for m in members.values()),
                "physical_device_bytes": None}

    def ledger(self):
        members = {p.name: p.stat().st_size for p in self.root.iterdir() if p.is_file()}
        required = {"manifest.json", *self.manifest["files"]}
        if set(members) != required or any(p.is_dir() for p in self.root.iterdir()):
            raise ValueError("Unexpected package files: reports and exports belong outside archive")
        return {"members": members, "total_package_bytes": sum(members.values()),
                "source_file_bytes": sum(v["bytes"] for v in self.manifest["sources"].values()),
                "scope": "all package data, HDF5 indexes, metadata, checksums and manifest; generic runtime excluded"}

    def verify(self):
        """Full archive scan; this is deliberately separate from ROI timing."""
        ledger = self.ledger()
        for name, expected in self.manifest["files"].items():
            if sha256(self.root / name) != expected["sha256"]:
                raise ValueError(f"File SHA256 mismatch: {name}")
        if h5_digest(self.root / "expression.h5ad") != self.manifest["expression"]["semantic_sha256"]:
            raise ValueError("H5AD semantic digest mismatch")
        with h5py.File(self.root / "expression.h5ad", "r") as f:
            count_schema(f, self.manifest["mapping"]["spatial_key"])
        checks = []
        for info in self.manifest["image"]["levels"]:
            ds = self._open("images.h5")[str(info["level"])]
            if list(ds.shape) != info["shape"] or ds.dtype != np.uint8:
                raise ValueError("Image schema mismatch")
            digest = hashlib.sha256()
            for row in range(0, len(ds), 128):
                digest.update(ds[row:row + 128].tobytes())
            if digest.hexdigest() != info["pixel_sha256"]:
                raise ValueError("Native image pixel hash mismatch")
            checks.append({"level": info["level"], "pixels_exact": True, "sha256": digest.hexdigest()})
        return {"ok": True, "h5ad_semantics_exact": True, "levels": checks, "ledger": ledger}

    def restore(self, destination):
        """Export standard H5AD and multi-page RGB TIFF; pixel/semantic exactness only."""
        destination = Path(destination).resolve()
        if destination == self.root or self.root in destination.parents:
            raise ValueError("Output must be outside the immutable archive")
        if destination.exists():
            raise FileExistsError(destination)
        self.verify()
        destination.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(self.root / "expression.h5ad", destination / "expression.h5ad")
        for name in self.manifest.get("attachments", []):
            shutil.copyfile(self.root / name, destination / name)
        frames = []
        try:
            for info in self.manifest["image"]["levels"]:
                h, w, _ = info["shape"]
                frame = Image.fromarray(self.image(info["level"], [0, 0, w, h]))
                profile = info.get("source_tiff_tags", {}).get("34675")
                if isinstance(profile, dict) and "base64" in profile:
                    frame.info["icc_profile"] = base64.b64decode(profile["base64"])
                frames.append(frame)
            frames[0].save(destination / "native_levels.tif", save_all=True, append_images=frames[1:], compression="tiff_adobe_deflate")
            # Original tags/ICC and coordinate mapping remain available in this sidecar.
            shutil.copyfile(self.root / "manifest.json", destination / "source_metadata.json")
        finally:
            for frame in frames:
                frame.close()
        return str(destination)
