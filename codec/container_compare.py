"""Physical Count/image composition without changing either compressed stream.

Decoder programs are supplied by the caller, separately from the data archive.
Opening or querying Count never constructs an image decoder. Full verification
is explicit and is not part of a selective-query timing measurement.
"""
import copy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile

import h5py
import numpy as np

from stbaseline.integrity import count_schema, h5_digest, sha256, text_ids, write_json
from stbaseline.lossless_metadata import _copy_attributes
from stbaseline.reader import Archive, MeteredFile
from stbaseline.compat_axes import MAP_NAME, write_map, validate_map

FORMAT = "st-comparison-compat-20261102-v1"
IMAGE_FORMAT = "wsi-hf-l1-compat-20261102-v1"
SHARED_COUNT_KINDS = ("Shared_local", "Shared_2")
COUNT_KINDS = ("hdf5_gzip9", "BPCells", *SHARED_COUNT_KINDS)
GROUPED_COUNT_MEMBERS = frozenset({"counts.bank", "counts.index.zst", "counts.model"})
OCCUPANCY_MEMBER = "counts.occupancy.zst"
IMAGE_PROFILES = ("direct", "hf_l1_conditional", "l1_conditional")

def qualified_image(format_name, profile):
    if format_name == IMAGE_FORMAT:
        return profile in IMAGE_PROFILES
    from image_compare import FORMAT as NEW_IMAGE_FORMAT, PROFILE_NAMES
    return format_name == NEW_IMAGE_FORMAT and profile in PROFILE_NAMES
IMAGE_FILES = {"manifest.json", "images.bank", "images.index", "anchors.index", "source.json"}
MAX_CELLS = 20_000_000
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")


def _plain(path, *, exists=True):
    path = Path(path).absolute()
    # Windows junctions are reparse points but are not necessarily symlinks.
    # Inspect each existing ancestor without following the final path entry;
    # fresh output leaves still require ordinary, non-aliased ancestors.
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("Symlink/reparse paths are unsupported")
    if exists and not path.exists():
        raise FileNotFoundError(path)
    return path.resolve()


def _name(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("Unsafe archive member")
    p = PurePosixPath(name)
    if p.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("Unsafe archive member")
    return name


def _read_bounded(path, limit=MAX_MANIFEST_BYTES):
    path = _plain(path)
    info = path.stat()
    if not path.is_file() or info.st_size <= 0 or info.st_size > limit:
        raise ValueError("Missing or oversized archive metadata: " + path.name)
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) != info.st_size or len(data) > limit:
        raise ValueError("Archive metadata changed or exceeded its bound: " + path.name)
    return data


def _json(path):
    return json.loads(_read_bounded(path))


def _identity(desc):
    if (not isinstance(desc, dict) or type(desc.get("bytes")) is not int
            or desc["bytes"] < 0 or not isinstance(desc.get("sha256"), str)
            or HEX.fullmatch(desc["sha256"]) is None):
        raise ValueError("Invalid member identity")


def _checked(path, desc):
    _identity(desc)
    path = _plain(path)
    if not path.is_file() or path.stat().st_size != desc["bytes"] or sha256(path) != desc["sha256"]:
        raise ValueError("Member identity mismatch: " + str(path))
    return path


def _copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _plain(source).open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src, dst)


def _fresh(path, *inputs):
    target = _plain(path, exists=False)
    if target.exists():
        raise FileExistsError(target)
    if any(target == p or target in p.parents or p in target.parents for p in inputs):
        raise ValueError("Output must be disjoint from immutable inputs")
    return target


def _shape(shape):
    if (not isinstance(shape, list) or len(shape) != 2
            or any(type(v) is not int or v < 1 for v in shape) or math.prod(shape) > MAX_CELLS):
        raise ValueError("Count shape exceeds qualified bound")


def _validate_component_binding(count_manifest, image_manifest, image_root):
    _shape(count_manifest["expression"]["shape"])
    for key in ("h5ad", "wsi"):
        _identity(count_manifest["sources"][key])
    if (not qualified_image(image_manifest.get("format"), image_manifest.get("profile"))
            or image_manifest.get("image_only") is not True):
        raise ValueError("Unsupported qualified image package")
    if set(image_manifest["files"]) | {"manifest.json"} != IMAGE_FILES:
        raise ValueError("Unexpected image component members")
    if {p.name for p in image_root.iterdir()} != IMAGE_FILES:
        raise ValueError("Unexpected physical image members")
    for name, desc in image_manifest["files"].items():
        _checked(image_root / _name(name), desc)
    image_source = _json(image_root / "source.json")
    source_sha = count_manifest["sources"]["wsi"]["sha256"]
    if (not isinstance(source_sha, str) or HEX.fullmatch(source_sha) is None
            or image_source.get("whole_file_sha256") != source_sha
            or image_source.get("identity", {}).get("whole_file_sha256") != source_sha):
        raise ValueError("Count and image source identities differ")
    count_levels, image_levels = count_manifest["image"]["levels"], image_manifest["image"]["levels"]
    if len(count_levels) != len(image_levels) or any(
            any(a.get(k) != b.get(k) for k in ("level", "shape", "dtype", "pixel_sha256"))
            for a, b in zip(count_levels, image_levels)):
        raise ValueError("Count and image native geometry/identity differ")
    return source_sha, image_levels


def _grouped_members(files):
    return GROUPED_COUNT_MEMBERS | ({OCCUPANCY_MEMBER} & files.keys())


def _select_count_members(root, manifest, count_kind, science_override, expression_override):
    selected = {}
    if count_kind == "hdf5_gzip9":
        if science_override is not None:
            # The full H5AD already contains metadata; never copy it twice.
            _plain(science_override)
        source = _plain(expression_override) if expression_override else _checked(
            root / manifest["expression"]["path"], manifest["files"][manifest["expression"]["path"]])
        if h5_digest(source) != manifest["expression"]["semantic_sha256"]:
            raise ValueError("HDF5 override changed scientific semantics")
        with h5py.File(source, "r") as handle:
            count_schema(handle, manifest["mapping"]["spatial_key"])
        selected["expression.h5ad"] = source
        expression_path = "expression.h5ad"
    else:
        if manifest.get("method") != count_kind:
            raise ValueError("Count source method differs")
        science = _plain(science_override) if science_override else _checked(root / "science.h5", manifest["files"]["science.h5"])
        with h5py.File(science, "r") as handle:
            x = handle["X"]
            if isinstance(x, h5py.Dataset):
                empty = x.shape == (0, 0)
            else:
                empty = set(x) == {"data", "indices", "indptr"} and all(x[k].shape == (0,) for k in x)
            if not empty:
                raise ValueError("Science skeleton must contain no X values")
        selected["science.h5"] = science
        expression_path = "science.h5"
        if count_kind == "Shared_2":
            names = _grouped_members(manifest["files"])
        elif count_kind == "Shared_local":
            names = {"counts.bank", "counts.index.json"}
        else:
            names = {n for n in manifest["files"] if n.startswith("counts.bp/")}
        if not names:
            raise ValueError("Missing Count payload")
        for name in names:
            selected[_name(name)] = _checked(root / name, manifest["files"][name])
    attachments = manifest.get("attachments", [])
    if (not isinstance(attachments, list) or len(set(attachments)) != len(attachments)
            or any(_name(n) != Path(n).name or n in selected or n in IMAGE_FILES
                   or (count_kind == "Shared_2" and n == OCCUPANCY_MEMBER)
                   or n in {"INCOMPLETE", "failure.json", "native_levels.tif", "expression.h5ad", "source_metadata.json", "package_manifest.json"}
                   for n in attachments)):
        raise ValueError("Unsafe attachment names")
    if MAP_NAME in attachments:
        raise ValueError("Position map must be generated from the scientific axis")
    for name in attachments:
        selected[name] = _checked(root / name, manifest["files"][name])
    return selected, expression_path, attachments


def pack_integrated(count_source, image_source, output, *, count_kind,
                    science_override=None, expression_override=None):
    """Create a fresh package by verified copies; never encode Count or image.

    Overrides are caller-prepared, fixed-policy HDF5 files. The expression
    override must match the source semantic digest. The science override must
    contain an empty X template; full semantics are checked after restoration.
    """
    if count_kind not in COUNT_KINDS:
        raise ValueError("Unknown Count component")
    count_root, image_root = _plain(count_source), _plain(image_source)
    if not count_root.is_dir() or not image_root.is_dir() or any((count_root / n).exists() for n in ("INCOMPLETE", "failure.json")):
        raise ValueError("Complete component directories required")
    count_manifest, image_manifest = _json(count_root / "manifest.json"), _json(image_root / "manifest.json")
    source_sha256, levels = _validate_component_binding(count_manifest, image_manifest, image_root)
    inputs = [count_root, image_root]
    overrides = [p for p in (science_override, expression_override) if p is not None]
    inputs.extend(_plain(p) for p in overrides)
    target = _fresh(output, *inputs)
    count_members, expression_path, attachments = _select_count_members(
        count_root, count_manifest, count_kind, science_override, expression_override)
    target.mkdir(parents=True)
    (target / "INCOMPLETE").write_text("Integrated package assembly\n", encoding="utf8")
    try:
        for name, source in sorted(count_members.items()):
            _copy(source, target / "count" / name)
        for name in sorted(IMAGE_FILES):
            _copy(image_root / name, target / "image" / name)
        with h5py.File(target / "count" / expression_path, "r") as scientific:
            write_map(target / "count" / MAP_NAME, text_ids(scientific["var"]))
        attachments = list(attachments) + [MAP_NAME]
        expression = copy.deepcopy(count_manifest["expression"])
        expression["path"] = expression_path
        manifest = {"format": FORMAT, "count": {"kind": count_kind, "expression": expression,
                    "attachments": list(attachments)}, "mapping": copy.deepcopy(count_manifest["mapping"]),
                    "sources": copy.deepcopy(count_manifest["sources"]),
                    "image": {"format": image_manifest["format"], "profile": image_manifest["profile"],
                              "levels": copy.deepcopy(levels), "source_sha256": source_sha256},
                    "files": {p.relative_to(target).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)}
                              for p in sorted(target.rglob("*")) if p.is_file() and p.name != "INCOMPLETE"},
                    "build": {"operation": "verified-component-copy-and-fixed-HDF5-layout",
                              "count_source_manifest_sha256": sha256(count_root / "manifest.json"),
                              "image_source_manifest_sha256": sha256(image_root / "manifest.json")}}
        write_json(target / "manifest.json", manifest)
        (target / "INCOMPLETE").unlink()
        with IntegratedArchive(target) as reader:
            return reader.ledger()
    except BaseException as exc:
        write_json(target / "failure.json", {"type": type(exc).__name__, "error": str(exc)})
        raise


class IntegratedArchive:
    """Lazy independent modalities with a fixed source-coordinate binding."""
    def __init__(self, directory, *, image_factory=None, rscript=None, r_library=None):
        self.root = _plain(directory)
        if any((self.root / n).exists() for n in ("INCOMPLETE", "failure.json")):
            raise ValueError("Incomplete integrated package")
        raw = _read_bounded(self.root / "manifest.json")
        self.manifest, self.manifest_bytes = json.loads(raw), len(raw)
        m = self.manifest
        if m.get("format") != FORMAT or m.get("count", {}).get("kind") not in COUNT_KINDS:
            raise ValueError("Unsupported integrated package")
        self.kind = m["count"]["kind"]
        _shape(m["count"]["expression"]["shape"])
        for key in ("h5ad", "wsi"):
            _identity(m["sources"][key])
        expression_path = "expression.h5ad" if self.kind == "hdf5_gzip9" else "science.h5"
        if m["count"]["expression"]["path"] != expression_path:
            raise ValueError("Unexpected expression path")
        attachments = m["count"].get("attachments", [])
        if not isinstance(attachments, list) or len(set(attachments)) != len(attachments):
            raise ValueError("Invalid attachments")
        if MAP_NAME not in attachments:
            raise ValueError("Missing original gene-position map")
        reserved = IMAGE_FILES | {"INCOMPLETE", "failure.json", "science.h5", "counts.bank", "counts.index.json",
                                  "expression.h5ad", "native_levels.tif", "source_metadata.json", "package_manifest.json"}
        if self.kind == "Shared_2":
            reserved |= GROUPED_COUNT_MEMBERS | {OCCUPANCY_MEMBER}
        for name in attachments:
            if _name(name) != Path(name).name or name in reserved:
                raise ValueError("Unsafe attachment")
        files = m["files"]
        expected = {"image/" + n for n in IMAGE_FILES} | {"count/" + expression_path} | {"count/" + n for n in attachments}
        if self.kind == "Shared_2":
            expected |= {"count/" + n for n in GROUPED_COUNT_MEMBERS}
            expected |= {"count/" + OCCUPANCY_MEMBER} & files.keys()
        elif self.kind == "Shared_local":
            expected |= {"count/counts.bank", "count/counts.index.json"}
        elif self.kind == "BPCells":
            bp = {n for n in files if n.startswith("count/counts.bp/")}
            if not bp or any(len(n.split("/")) != 3 for n in bp):
                raise ValueError("Invalid BPCells member set")
            expected |= bp
        if set(files) != expected:
            raise ValueError("Unexpected component members")
        self._check_members()
        levels = m["image"]["levels"]
        if (not qualified_image(m["image"].get("format"), m["image"].get("profile"))
                or not isinstance(levels, list) or not 1 <= len(levels) <= 64):
            raise ValueError("Invalid native image descriptor")
        previous = None
        for i, info in enumerate(levels):
            shape = info.get("shape")
            if (info.get("level") != i or info.get("dtype") != "uint8" or not isinstance(shape, list)
                    or len(shape) != 3 or shape[2] != 3 or any(type(v) is not int or v < 1 for v in shape)
                    or not isinstance(info.get("pixel_sha256"), str) or HEX.fullmatch(info["pixel_sha256"]) is None
                    or (previous and (shape[0] > previous[0] or shape[1] > previous[1] or shape[:2] == previous))):
                raise ValueError("Invalid native pyramid geometry")
            previous = shape[:2]
        if m["image"].get("source_sha256") != m["sources"]["wsi"]["sha256"]:
            raise ValueError("Top-level source identities differ")
        transform = np.asarray(m["mapping"]["xy_to_level0"], dtype=np.float64)
        if (transform.shape != (3, 3) or not np.isfinite(transform).all()
                or not np.array_equal(transform[2], [0, 0, 1]) or np.linalg.det(transform[:2, :2]) == 0):
            raise ValueError("Invalid source-coordinate transform")
        self.image_factory, self.rscript, self.r_library = image_factory, rscript, r_library
        self._image = self._store = self._bp = self._science = self._science_file = self._metadata = None
        self.integrity_read_bytes = 0
        self.binding_read_bytes = 0
        self._verified = set()
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _check_members(self):
        expected = {"manifest.json", *self.manifest["files"]}
        actual, directories = set(), set()
        for p in self.root.rglob("*"):
            _plain(p)
            name = p.relative_to(self.root).as_posix()
            if p.is_file():
                actual.add(name)
            elif p.is_dir():
                directories.add(name)
            else:
                raise ValueError("Unsupported member type")
        allowed_dirs = {str(PurePosixPath(n).parent) for n in expected}
        allowed_dirs |= {"count", "image"}
        if actual != expected or not directories <= allowed_dirs:
            raise ValueError("Unexpected physical package members")
        for name, desc in self.manifest["files"].items():
            _name(name); _identity(desc)
            if (self.root / name).stat().st_size != desc["bytes"]:
                raise ValueError("Member size mismatch: " + name)

    def _verify_member(self, name):
        if name not in self._verified:
            desc = self.manifest["files"][name]
            _checked(self.root / name, desc)
            self.integrity_read_bytes += desc["bytes"]
            self._verified.add(name)

    def _science_open(self):
        if self._closed:
            raise ValueError("Archive is closed")
        if self._science is None:
            name = "count/" + self.manifest["count"]["expression"]["path"]
            self._verify_member(name)
            self._science_file = MeteredFile(self.root / name)
            try:
                self._science = h5py.File(self._science_file, "r")
            except BaseException:
                self._science_file.close()
                raise
        return self._science

    def metadata(self):
        if self._closed:
            raise ValueError("Archive is closed")
        if self._metadata is None:
            f = self._science_open()
            rows, genes = text_ids(f["obs"]), text_ids(f["var"])
            xy = np.asarray(f["obsm"][self.manifest["mapping"]["spatial_key"]])
            if ([len(rows), len(genes)] != self.manifest["count"]["expression"]["shape"]
                    or len(set(rows)) != len(rows)
                    or xy.shape != (len(rows), 2) or xy.dtype.kind not in "fiu" or not np.isfinite(xy).all()):
                raise ValueError("Invalid scientific axes/coordinates")
            self._verify_member("count/" + MAP_NAME)
            validate_map(self.root / "count" / MAP_NAME, genes)
            transform = np.asarray(self.manifest["mapping"]["xy_to_level0"])
            self._metadata = {"spot_ids": rows, "gene_ids": genes, "coordinates": xy,
                              "level0_xy": (np.c_[xy, np.ones(len(xy))] @ transform.T)[:, :2]}
        return self._metadata

    def _count_store(self):
        if self._store is None:
            shape = self.manifest["count"]["expression"]["shape"]
            if self.kind == "Shared_2":
                from stbaseline.grouped_count import GroupedCountStore

                self._verify_member("count/counts.index.zst")
                self._verify_member("count/counts.model")
                if "count/" + OCCUPANCY_MEMBER in self.manifest["files"]:
                    self._verify_member("count/" + OCCUPANCY_MEMBER)
                self._store = GroupedCountStore(self.root / "count", expected_shape=shape)
            else:
                from stbaseline.shared_streaming import BlockCountStore

                self._verify_member("count/counts.index.json")
                store = BlockCountStore(self.root / "count")
                if store.index["method"] != "Shared_local" or store.index["shape"] != shape:
                    store.close()
                    raise ValueError("Count index disagrees with container")
                self._store = store
        return self._store

    def _bpcells_worker(self):
        if self._bp is None:
            from stbaseline.comparison import BPWorker
            import os
            for name in self.manifest["files"]:
                if name.startswith("count/counts.bp/"):
                    self._verify_member(name)
            rscript = self.rscript or os.environ.get("STBASELINE_RSCRIPT", "Rscript")
            library = self.r_library or os.environ.get("STBASELINE_R_LIBRARY", "")
            if "count/bp_layout.json" in self.manifest["files"]:
                from count_compare import OrderedBPWorker
                self._verify_member("count/bp_layout.json")
                if "count/bp_permutation.u16" in self.manifest["files"]:
                    self._verify_member("count/bp_permutation.u16")
                self._bp = OrderedBPWorker(self.root / "count", rscript, library,
                    expected_shape=self.manifest["count"]["expression"]["shape"])
            else:
                self._bp = BPWorker(self.root / "count" / "counts.bp", rscript, library)
        return self._bp

    def _read_hdf5_counts(self, rows, genes, output):
        x = self._science_open()["X"]
        inverse = {int(g): i for i, g in enumerate(genes)}
        for target, row in enumerate(rows):
            if isinstance(x, h5py.Dataset):
                output[target] = x[int(row), :][genes].astype(np.uint64)
            else:
                lo, hi = map(int, x["indptr"][int(row):int(row) + 2])
                for g, value in zip(x["indices"][lo:hi], x["data"][lo:hi]):
                    if int(g) in inverse:
                        output[target, inverse[int(g)]] = int(value)

    def counts(self, rows=None, genes=None, *, max_cells=MAX_CELLS):
        if type(max_cells) is not int or max_cells < 1:
            raise ValueError("Positive integer query bound required")
        md = self.metadata()
        row_positions = Archive._select(rows, md["spot_ids"])
        gene_positions = Archive._select(genes, md["gene_ids"])
        if len(row_positions) * len(gene_positions) > max_cells:
            raise ValueError("Query too large; use row batches")
        values = np.zeros((len(row_positions), len(gene_positions)), np.uint64)
        if len(row_positions) and len(gene_positions):
            if self.kind in SHARED_COUNT_KINDS:
                values = self._count_store().read(row_positions, gene_positions)
            elif self.kind == "BPCells":
                values = self._bpcells_worker().read(row_positions, gene_positions, all_genes=genes is None)
            else:
                self._read_hdf5_counts(row_positions, gene_positions, values)
        return {"counts": values, "spot_ids": np.asarray([md["spot_ids"][i] for i in row_positions], dtype=str),
                "gene_ids": np.asarray([md["gene_ids"][i] for i in gene_positions], dtype=str),
                "gene_positions": gene_positions.copy(),
                "coordinates": md["coordinates"][row_positions], "level0_xy": md["level0_xy"][row_positions]}

    def _image_reader(self):
        if self._closed:
            raise ValueError("Archive is closed")
        if self._image is None:
            if self.image_factory is None:
                raise ValueError("An explicit qualified image decoder factory is required")
            for name in ("manifest.json", "source.json"):
                self._verify_member("image/" + name)
            m, source = _json(self.root / "image" / "manifest.json"), _json(self.root / "image" / "source.json")
            self.binding_read_bytes += sum(self.manifest["files"]["image/" + n]["bytes"] for n in ("manifest.json", "source.json"))
            if (m.get("format") != self.manifest["image"]["format"]
                    or m.get("profile") != self.manifest["image"]["profile"]
                    or m.get("image", {}).get("levels") != self.manifest["image"]["levels"]
                    or source.get("whole_file_sha256") != self.manifest["image"]["source_sha256"]
                    or source.get("identity", {}).get("whole_file_sha256") != self.manifest["image"]["source_sha256"]
                    or set(m.get("files", {})) != IMAGE_FILES - {"manifest.json"}
                    or any(desc != self.manifest["files"]["image/" + n] for n, desc in m["files"].items())):
                raise ValueError("Image child disagrees with integrated binding")
            self._image = self.image_factory(self.root / "image")
        return self._image

    def image(self, level, bbox):
        return self._image_reader().image(level, bbox)

    def preview(self, bbox=None, *, max_edge=1024):
        return self._image_reader().preview(bbox, max_edge=max_edge)

    def source_metadata(self):
        return self._image_reader().source_metadata()

    def _region_boxes(self, bbox, level):
        """Map half-open level-0 bounds to covering pixels of the native level."""
        levels = self.manifest["image"]["levels"]
        if type(level) is not int or not 0 <= level < len(levels):
            raise ValueError("Invalid native level")
        if len(bbox) != 4 or any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)) for v in bbox):
            raise ValueError("Integer source-level0 bounds required")
        x0, y0, x1, y1 = map(int, bbox)
        h0, w0, _ = levels[0]["shape"]
        if not (0 <= x0 < x1 <= w0 and 0 <= y0 < y1 <= h0):
            raise ValueError("Region outside source level0")
        h, w, _ = levels[level]["shape"]
        box = [x0*w//w0, y0*h//h0, (x1*w+w0-1)//w0, (y1*h+h0-1)//h0]
        return (x0, y0, x1, y1), box

    def region(self, bbox, *, level=0, genes=None, modalities=("counts", "image")):
        if (not isinstance(modalities, (list, tuple)) or not modalities
                or len(set(modalities)) != len(modalities) or any(v not in ("counts", "image") for v in modalities)):
            raise ValueError("Select counts and/or image")
        (x0, y0, x1, y1), box = self._region_boxes(bbox, level)
        result = {"level0_bbox": np.asarray(bbox), "image_bbox": np.asarray(box), "level": np.asarray(level)}
        if "counts" in modalities:
            xy = self.metadata()["level0_xy"]
            rows = np.flatnonzero((xy[:, 0] >= x0) & (xy[:, 0] < x1) & (xy[:, 1] >= y0) & (xy[:, 1] < y1))
            result.update(self.counts(rows, genes))
        if "image" in modalities:
            result["image"] = self.image(level, box)
        return result

    @staticmethod
    def _write_count_matrix(source, target, matrix):
        """Reinsert Count values using the source skeleton's X layout and attributes."""
        _copy(source, target)
        with h5py.File(source, "r") as original, h5py.File(target, "r+") as restored:
            old = original["X"]
            if isinstance(old, h5py.Dataset):
                del restored["X"]
                x = restored.create_dataset("X", shape=matrix.shape, dtype=old.dtype, compression="gzip", compression_opts=4)
                _copy_attributes(old, x)
                for start in range(0, matrix.shape[0], 32):
                    x[start:start+32] = matrix[start:start+32].toarray().astype(old.dtype)
            else:
                for key, values in (("data", matrix.data), ("indices", matrix.indices), ("indptr", matrix.indptr)):
                    del restored["X"][key]
                    node = restored["X"].create_dataset(key, data=values.astype(old[key].dtype), compression="gzip", compression_opts=4)
                    _copy_attributes(old[key], node)

    def restore_expression(self, target):
        self.metadata()  # Validate the paid original-position binding on full restore too.
        target = _fresh(target, self.root)
        source_name = "count/" + self.manifest["count"]["expression"]["path"]
        self._verify_member(source_name)
        source = self.root / source_name
        if self.kind == "hdf5_gzip9":
            _copy(source, target)
        else:
            from scipy.sparse import csr_matrix, vstack
            if self.kind in SHARED_COUNT_KINDS:
                matrix = self._count_store().sparse_all(max_cells=MAX_CELLS)
            else:
                n = self.manifest["count"]["expression"]["shape"][0]
                matrix = vstack([csr_matrix(self.counts(range(s, min(s+32, n)))["counts"])
                                 for s in range(0, n, 32)], format="csr")
            self._write_count_matrix(source, target, matrix)
        if h5_digest(target) != self.manifest["count"]["expression"]["semantic_sha256"]:
            raise ValueError("Scientific H5AD restoration mismatch")
        return str(target)

    def ledger(self):
        self._check_members()
        members = {"manifest.json": (self.root / "manifest.json").stat().st_size,
                   **{n: (self.root / n).stat().st_size for n in self.manifest["files"]}}
        categories = {"manifest": members["manifest.json"], "count": 0, "science_metadata": 0,
                      "image": 0, "attachments": 0}
        attachments = {"count/" + n for n in self.manifest["count"]["attachments"]}
        for name, size in members.items():
            if name == "manifest.json":
                continue
            key = ("image" if name.startswith("image/") else "attachments" if name in attachments
                   else "science_metadata" if name == "count/science.h5" else "count")
            categories[key] += size
        return {"members": members, "categories": categories, "total_package_bytes": sum(members.values()),
                "scope": "Actual complete data package; HDF5 Count includes its metadata once; decoder programs excluded and reported separately"}

    def verify(self):
        self.verify_files()
        with tempfile.TemporaryDirectory(prefix="st-integrated-verify-") as temporary:
            expression = Path(temporary) / "expression.h5ad"
            self.restore_expression(expression)
            with h5py.File(expression, "r") as handle:
                count_schema(handle, self.manifest["mapping"]["spatial_key"])
        image_reader = self._image_reader()
        image_result = (image_reader.verify() if self.manifest['image']['format'] == IMAGE_FORMAT
                        else image_reader.verify(decode=True))
        if image_result.get("ok") is not True:
            raise ValueError("Image verification did not pass")
        return {"ok": True, "h5ad_semantics_exact": True, "image": image_result, "ledger": self.ledger()}

    def verify_files(self):
        """Check every physical member and digest without decoding either modality."""
        if self._closed:
            raise ValueError("Archive is closed")
        self._check_members()
        self._verified.clear()
        for name in self.manifest["files"]:
            self._verify_member(name)
        return {"ok": True, "ledger": self.ledger()}

    def restore(self, destination):
        dest = _fresh(destination, self.root)
        self.verify_files()
        # The qualified image reader creates the destination and streams TIFF.
        self._image_reader().restore(dest)
        marker = dest / "INCOMPLETE"
        marker.write_text("Integrated Count restoration\n", encoding="utf8")
        try:
            self.restore_expression(dest / "expression.h5ad")
            for name in self.manifest["count"]["attachments"]:
                _copy(self.root / "count" / name, dest / name)
            _copy(self.root / "manifest.json", dest / "source_metadata.json")
            marker.unlink()
        except BaseException as exc:
            write_json(dest / "failure.json", {"type": type(exc).__name__, "error": str(exc)})
            raise
        return str(dest)

    def metrics(self):
        science = self._science_file.metrics() if self._science_file is not None else None
        count = None if self._store is None else {**self._store.file.metrics(), **self._store.execution_metrics(),
                    "index_bytes": self._store.index_bytes, "decoded_blocks": self._store.decoded_blocks,
                    "decoded_rows": self._store.decoded_rows}
        return {"manifest_read_bytes": self.manifest_bytes, "integrity_read_bytes": self.integrity_read_bytes,
                "binding_read_bytes": self.binding_read_bytes,
                "science": science, "shared_count": count, "image": None if self._image is None else self._image.metrics(),
                "bpcells_native_seconds": None if self._bp is None else self._bp.last_native_seconds,
                "bpcells_read_bytes": None, "physical_device_bytes": None,
                "scope": "Cumulative application reads; integrity scans explicit; BPCells native bytes and OS cache uncontrolled"}

    def close(self):
        if self._closed:
            return
        for obj in (self._image, self._store, self._bp, self._science, self._science_file):
            if obj is not None:
                obj.close()
        self._closed = True

