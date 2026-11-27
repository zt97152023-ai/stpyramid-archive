"""Fixed BPCells layout comparison; execution belongs to the reviewed supervisor.

All matrices exposed by readers use original spots-by-genes coordinates. The
on-disk BPCells logical matrix is genes-by-spots for every physical layout.
The calling container must verify its manifest's layout/payload member hashes
before opening a reader. This module does not change process resource limits.
"""
import copy
import json
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import h5py
import numpy as np
from scipy.io import mmwrite
from scipy.sparse import csr_matrix, vstack

from stbaseline import comparison as comparison_module
from stbaseline.comparison import BPWorker, csr_from_h5, r_environment
from stbaseline.integrity import count_schema, h5_digest, sha256
from stbaseline.lossless_metadata import _copy_attributes


DEFAULT_RSCRIPT = "Rscript"
DEFAULT_R_LIBRARY = ""
SCHEMA = "st-bpcells-order-1"
LAYOUT_NAME = "bp_layout.json"
PERMUTATION_NAME = "bp_permutation.u16"
PROFILES = {
    "cellmajor_gene_sum": ("col", "genes"),
    "featuremajor_original": ("row", None),
    "featuremajor_spot_sum": ("row", "spots"),
}
MAX_CELLS = 20_000_000
HEX = re.compile(r"[0-9a-f]{64}\Z")


def _json_new(path, value):
    with Path(path).open("x", encoding="utf8", newline="\n") as handle:
        json.dump(value, handle, sort_keys=True, indent=2, ensure_ascii=False,
                  allow_nan=False)
        handle.write("\n")


def _no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _file(path):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("Expected an ordinary file: " + str(path))
    return path


def _json_read(path, limit):
    path = _file(path)
    if path.stat().st_size > limit:
        raise ValueError("JSON byte bound exceeded")
    return json.loads(path.read_text(encoding="utf8"), object_pairs_hook=_no_duplicates,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))


def _shape(value):
    if (type(value) not in (list, tuple) or len(value) != 2
            or any(type(v) is not int or not 1 <= v <= 65536 for v in value)
            or value[0] * value[1] > MAX_CELLS):
        raise ValueError("Invalid or excessive original matrix shape")
    return tuple(value)


def stable_sum_order(matrix, axis):
    """Return stored-to-original order, ascending uint64 sums, stable ties."""
    if axis not in ("spots", "genes"):
        raise ValueError("Unknown permutation axis")
    _shape(list(matrix.shape))
    values = matrix.data
    if (values.dtype.kind not in "fiu" or not np.isfinite(values).all()
            or np.any(values < 0) or np.any(values > 2**32 - 1)
            or np.any(values != np.floor(values))):
        raise ValueError("Counts must be exact uint32-range integers")
    # Shape bounds make a uint64 sum exact even when every value is UINT32_MAX.
    sums = np.asarray(matrix.astype(np.uint64).sum(axis=1 if axis == "spots" else 0,
                                                  dtype=np.uint64)).ravel()
    return np.lexsort((np.arange(len(sums), dtype=np.uint64), sums)).astype("<u2")


def _write_layout(root, shape, profile, source_sha, permutation):
    physical, axis = PROFILES[profile]
    descriptor = None
    if axis is not None:
        with (root / PERMUTATION_NAME).open("xb") as handle:
            handle.write(np.asarray(permutation, dtype="<u2").tobytes())
        descriptor = {"path": PERMUTATION_NAME, "dtype": "<u2",
                      "length": shape[1 if axis == "genes" else 0],
                      "bytes": (root / PERMUTATION_NAME).stat().st_size,
                      "sha256": sha256(root / PERMUTATION_NAME)}
    layout = {"schema": SCHEMA, "profile": profile,
              "original_shape": list(shape), "logical_shape": [shape[1], shape[0]],
              "physical_storage_order": physical, "permutation_axis": axis,
              "direction": "stored_to_original", "permutation": descriptor,
              "sort": "uint64_sum_ascending_then_original_index" if axis else "original",
              "source_h5ad_sha256": source_sha}
    _json_new(root / LAYOUT_NAME, layout)
    return layout


def load_layout(count_root, expected_shape=None):
    """Validate the paid header/map and return (layout, inverse row, inverse gene)."""
    root = Path(count_root)
    layout = _json_read(root / LAYOUT_NAME, 65536)
    keys = {"schema", "profile", "original_shape", "logical_shape", "physical_storage_order",
            "permutation_axis", "direction", "permutation", "sort", "source_h5ad_sha256"}
    if type(layout) is not dict or set(layout) != keys or layout["schema"] != SCHEMA:
        raise ValueError("Invalid BPCells layout schema")
    if type(layout["profile"]) is not str or layout["profile"] not in PROFILES:
        raise ValueError("Invalid BPCells profile")
    shape = _shape(layout["original_shape"])
    if expected_shape is not None and shape != _shape(list(expected_shape)):
        raise ValueError("BPCells layout shape differs from scientific metadata")
    physical, axis = PROFILES[layout["profile"]]
    if (_shape(layout["logical_shape"]) != (shape[1], shape[0])
            or layout["physical_storage_order"] != physical
            or layout["permutation_axis"] != axis or layout["direction"] != "stored_to_original"
            or layout["sort"] != ("uint64_sum_ascending_then_original_index" if axis else "original")
            or type(layout["source_h5ad_sha256"]) is not str
            or HEX.fullmatch(layout["source_h5ad_sha256"]) is None):
        raise ValueError("Inconsistent BPCells layout header")
    rows, genes = np.arange(shape[0], dtype=np.int64), np.arange(shape[1], dtype=np.int64)
    descriptor = layout["permutation"]
    if axis is None:
        if descriptor is not None or (root / PERMUTATION_NAME).exists():
            raise ValueError("Original-order profile must not have a permutation")
    else:
        n = shape[1 if axis == "genes" else 0]
        if (type(descriptor) is not dict
                or set(descriptor) != {"path", "dtype", "length", "bytes", "sha256"}
                or descriptor["path"] != PERMUTATION_NAME or descriptor["dtype"] != "<u2"
                or type(descriptor["length"]) is not int or descriptor["length"] != n
                or type(descriptor["bytes"]) is not int or descriptor["bytes"] != n * 2
                or type(descriptor["sha256"]) is not str
                or HEX.fullmatch(descriptor["sha256"]) is None):
            raise ValueError("Invalid permutation descriptor")
        path = _file(root / PERMUTATION_NAME)
        if path.stat().st_size != n * 2 or sha256(path) != descriptor["sha256"]:
            raise ValueError("Permutation byte size or checksum differs")
        order = np.frombuffer(path.read_bytes(), dtype="<u2").astype(np.int64)
        if not np.array_equal(np.sort(order), np.arange(n, dtype=np.int64)):
            raise ValueError("Permutation must be an in-range bijection")
        inverse = np.empty(n, dtype=np.int64)
        inverse[order] = np.arange(n, dtype=np.int64)
        if axis == "genes":
            genes = inverse
        else:
            rows = inverse
    return layout, rows, genes


def _selection(values, size):
    raw = list(values)
    if any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer))
           or not 0 <= v < size for v in raw) or len(set(raw)) != len(raw):
        raise ValueError("Selection must contain unique in-range integer indices")
    return np.asarray(raw, dtype=np.int64)


class OrderedBPWorker:
    """BPWorker-compatible reader translating original coordinates, never sources."""
    def __init__(self, count_root, rscript=DEFAULT_RSCRIPT, library=DEFAULT_R_LIBRARY,
                 expected_shape=None):
        self.root = Path(count_root)
        self.layout, self._rows, self._genes = load_layout(self.root, expected_shape)
        self._worker = BPWorker(self.root / "counts.bp", str(rscript), str(library))
        self.last_native_seconds = None
        self._closed = False

    def read(self, rows, genes, all_genes=False):
        if self._closed:
            raise ValueError("BPCells reader is closed")
        ri, gi = _selection(rows, len(self._rows)), _selection(genes, len(self._genes))
        if all_genes and not np.array_equal(gi, np.arange(len(self._genes))):
            raise ValueError("all_genes requires every original gene in original order")
        # '*' bypasses the gene indices in BPWorker, so never use it on sorted genes.
        use_star = bool(all_genes and self.layout["permutation_axis"] != "genes")
        result = self._worker.read(self._rows[ri], self._genes[gi], all_genes=use_star)
        self.last_native_seconds = self._worker.last_native_seconds
        return result

    def close(self):
        if not self._closed:
            self._worker.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def open_worker(count_root, rscript=DEFAULT_RSCRIPT, library=DEFAULT_R_LIBRARY,
                expected_shape=None):
    """Retain the ordinary BPWorker for reused B0 components."""
    root = Path(count_root)
    if (root / LAYOUT_NAME).exists():
        return OrderedBPWorker(root, rscript, library, expected_shape)
    return BPWorker(root / "counts.bp", str(rscript), str(library))


def restore_expression(count_root, target, *, rscript=DEFAULT_RSCRIPT,
                       r_library=DEFAULT_R_LIBRARY):
    """Standalone Count restore for verification; preserve skeleton dtypes/attrs."""
    root, target = Path(count_root), Path(target)
    if target.exists():
        raise FileExistsError(target)
    layout, _, _ = load_layout(root)
    n, g = layout["original_shape"]
    with OrderedBPWorker(root, rscript, r_library, [n, g]) as worker:
        matrix = vstack([csr_matrix(worker.read(range(s, min(s + 32, n)), range(g), True))
                         for s in range(0, n, 32)], format="csr")
    with _file(root / "science.h5").open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst)
    with h5py.File(root / "science.h5", "r") as original, h5py.File(target, "r+") as restored:
        old = original["X"]
        if isinstance(old, h5py.Dataset):
            del restored["X"]
            node = restored.create_dataset("X", shape=matrix.shape, dtype=old.dtype,
                                           compression="gzip", compression_opts=4, track_times=False)
            _copy_attributes(old, node)
            for start in range(0, n, 32):
                node[start:start + 32] = matrix[start:start + 32].toarray().astype(old.dtype)
        else:
            for key, values in (("data", matrix.data), ("indices", matrix.indices),
                                ("indptr", matrix.indptr)):
                del restored["X"][key]
                node = restored["X"].create_dataset(key, data=values.astype(old[key].dtype),
                                                     compression="gzip", compression_opts=4,
                                                     track_times=False)
                _copy_attributes(old[key], node)
    return str(target)


def _skeleton(path, shape, kind):
    with h5py.File(path, "r") as handle:
        x = handle["X"]
        if kind == "dense":
            valid = isinstance(x, h5py.Dataset) and x.shape == (0, 0)
        else:
            valid = (isinstance(x, h5py.Group) and set(x) == {"data", "indices", "indptr"}
                     and all(isinstance(x[k], h5py.Dataset) and x[k].shape == (0,) for k in x)
                     and list(x.attrs.get("shape", [])) == list(shape))
        if not valid:
            raise ValueError("Science sidecar must contain empty X templates only")


def build(source_h5ad, template_source, science_source, out, profile, *,
          rscript=DEFAULT_RSCRIPT, r_library=DEFAULT_R_LIBRARY):
    """Create one component and a sibling .work evidence directory, without overwrite.

    template_source is the old BPCells component directory; science_source is the
    reviewed empty-X exact scientific skeleton. Timing and work artifacts are
    excluded from deterministic package bytes. The caller must apply the qualified
    R3 datatype oracle to the returned restored_h5ad in addition to semantic checks.
    """
    started = time.perf_counter()
    if profile not in PROFILES:
        raise ValueError("Unknown fixed BPCells profile")
    source, template, science, out = map(Path, (source_h5ad, template_source, science_source, out))
    source, science = _file(source), _file(science)
    cm = _json_read(template / "manifest.json", 16 * 1024 * 1024)
    source_sha = sha256(source)
    if cm.get("method") != "BPCells" or source_sha != cm["sources"]["h5ad"]["sha256"]:
        raise ValueError("Source differs from explicit BPCells template")
    with h5py.File(source, "r") as handle:
        expression, _, _, _ = count_schema(handle, cm["mapping"]["spatial_key"])
    shape = _shape(expression["shape"])
    if any(expression[k] != cm["expression"][k] for k in ("shape", "kind", "dtype")):
        raise ValueError("Source Count schema differs from template")
    semantic_sha = h5_digest(source)
    if semantic_sha != cm["expression"]["semantic_sha256"]:
        raise ValueError("Source scientific digest differs from template")
    _skeleton(science, shape, expression["kind"])
    matrix = csr_from_h5(source)
    physical, axis = PROFILES[profile]
    permutation = stable_sum_order(matrix, axis) if axis else None
    ordered = matrix[:, permutation] if axis == "genes" else matrix[permutation, :] if axis else matrix
    ordered = ordered.tocsr()
    ordered.sort_indices()
    work = out.with_name(out.name + ".work")
    if out.exists() or work.exists():
        raise FileExistsError("Output and its .work sibling must both be new")
    out.mkdir(parents=True, exist_ok=False)
    work.mkdir(exist_ok=False)
    (out / "INCOMPLETE").write_text("Count build incomplete\n", encoding="utf8")
    try:
        shutil.copyfile(science, out / "science.h5")
        attachments = list(cm.get("attachments", []))
        if len(set(attachments)) != len(attachments):
            raise ValueError("Duplicate template attachment")
        for name in attachments:
            if (type(name) is not str or not name or Path(name).name != name
                    or any(c in name for c in ("/", "\\", ":"))
                    or name in {LAYOUT_NAME, PERMUTATION_NAME, "manifest.json", "science.h5", "INCOMPLETE"}):
                raise ValueError("Unsafe or reserved template attachment")
            path = _file(template / name)
            info = cm["files"][name]
            if path.stat().st_size != info["bytes"] or sha256(path) != info["sha256"]:
                raise ValueError("Template attachment checksum differs")
            shutil.copyfile(path, out / name)
        _write_layout(out, shape, profile, source_sha, permutation)
        attachments += [LAYOUT_NAME] + ([PERMUTATION_NAME] if axis else [])
        inp = work / "input.mtx"
        mmwrite(inp, ordered.T, field="integer", symmetry="general")
        preprocess_seconds = time.perf_counter() - started
        r_started = time.perf_counter()
        r_code = Path(__file__).with_name("count_worker.R").resolve()
        env = r_environment(r_library)
        env.update(MKL_NUM_THREADS="1", NUMEXPR_NUM_THREADS="1", VECLIB_MAXIMUM_THREADS="1",
                   RCPP_PARALLEL_NUM_THREADS="1")
        (work / "r_tmp").mkdir()
        env.update(TMPDIR=str((work / "r_tmp").resolve()), TEMP=str((work / "r_tmp").resolve()),
                   TMP=str((work / "r_tmp").resolve()))
        with (work / "r_stdout.txt").open("x", encoding="utf8") as stdout, (work / "r_stderr.txt").open("x", encoding="utf8") as stderr:
            completed = subprocess.run([str(rscript), "--vanilla", str(r_code), "encode",
                                        str(inp.resolve()), str((out / "counts.bp").resolve()),
                                        physical, str((work / "r_tmp").resolve()),
                                        str((work / "r_receipt.json").resolve())],
                                       cwd=str(work.resolve()), env=env, stdout=stdout, stderr=stderr,
                                       timeout=600, check=False)
        r_wall_seconds = time.perf_counter() - r_started
        if completed.returncode:
            raise RuntimeError("BPCells encoder failed; inspect sibling .work R logs")
        r_receipt = _json_read(work / "r_receipt.json", 65536)
        if (r_receipt["physical_storage_order"] != physical
                or r_receipt["matrix_type"] != "uint32_t"
                or r_receipt["logical_shape"] != [shape[1], shape[0]]):
            raise ValueError("R encoder output layout/type/shape differs")
        verify_started = time.perf_counter()
        restored = work / "restored.h5ad"
        restore_expression(out, restored, rscript=rscript, r_library=r_library)
        if h5_digest(restored) != semantic_sha:
            raise ValueError("Full original-order scientific restoration differs")
        with h5py.File(restored, "r") as handle:
            if count_schema(handle, cm["mapping"]["spatial_key"])[0] != expression:
                raise ValueError("Restored Count schema differs")
        verification_seconds = time.perf_counter() - verify_started
        serialize_started = time.perf_counter()
        m = copy.deepcopy(cm)
        m["expression"].update(path="science.h5", storage="BPCells-uint32-genes-by-spots")
        m["attachments"] = attachments
        m["build"] = {"profile": profile, "compress": True, "BPCells_version": r_receipt["BPCells_version"],
                      "load_bytes": 4194304, "sort_bytes": 67108864,
                      "ordering": "uint64_sum_ascending_then_original_index" if axis else "original"}
        m["files"] = {}
        for path in sorted(out.rglob("*")):
            if path.is_file() and path.name != "INCOMPLETE":
                _file(path)
                m["files"][path.relative_to(out).as_posix()] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
        _json_new(out / "manifest.json", m)
        (out / "INCOMPLETE").unlink()
        serialization_seconds = time.perf_counter() - serialize_started
        payload = sum(v["bytes"] for k, v in m["files"].items() if k.startswith("counts.bp/"))
        order_bytes = sum(m["files"][k]["bytes"] for k in (LAYOUT_NAME, PERMUTATION_NAME) if k in m["files"])
        return {"profile": profile, "output": str(out), "work_directory": str(work),
                "restored_h5ad": str(restored), "shape": list(shape),
                "source_sha256": source_sha, "science_sha256": sha256(science),
                "h5ad_semantic_exact": True, "type_oracle_required": "qualified R3 h5_types",
                "manifest_sha256": sha256(out / "manifest.json"),
                "bytes": {"bp_payload": payload, "ordering": order_bytes,
                          "count_plus_ordering": payload + order_bytes,
                          "science": m["files"]["science.h5"]["bytes"],
                          "component_total": sum(v["bytes"] for v in m["files"].values()) + (out / "manifest.json").stat().st_size},
                "seconds": {"preprocess": preprocess_seconds, "r_write_wall": r_wall_seconds,
                            "verification": verification_seconds, "serialization": serialization_seconds,
                            "total": time.perf_counter() - started},
                "r_receipt": r_receipt,
                "code_pins": {"count_compare.py": sha256(__file__), "count_worker.R": sha256(r_code),
                              "bpcells_worker.R": sha256(Path(comparison_module.__file__).with_name("bpcells_worker.R"))}}
    except BaseException as exc:
        _json_new(work / "failure.json", {"type": type(exc).__name__, "error": str(exc)})
        raise

