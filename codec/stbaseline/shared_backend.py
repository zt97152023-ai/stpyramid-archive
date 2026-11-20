"""Shared B8/K32 integration: whole archive, independent blocks, shared model.

The frozen codec sources are byte-preserved. Local arms alter the dependency
domain and envelope, not the entropy coder. No decoded Count cache is retained.
"""
import base64
import bz2
import copy
from functools import lru_cache
import hashlib
import importlib.util
import inspect
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

import numpy as np
from scipy.sparse import csr_matrix

from .comparison import BaselineArchive, csr_from_h5
from .integrity import bz2_decompress_exact as _bz2_exact, sha256, write_json
from .reader import MeteredFile

FORMAT = "stbaseline-shared-1"
METHODS = ("BPCells", "Shared_whole", "Shared_blocks", "Shared_local")
MODEL_NAMES = ("base_probability.bz2", "shared_odds.u32", "value_q1.bz2",
               "value_group.bz2", "value_base_k.bz2", "value_odds.u32", "value_cond_k.bz2")
MAX_COUNT_INDEX_BYTES = 256 * 1024 * 1024
MAX_COUNT_MEMBER_BYTES = 512 * 1024 * 1024
MAX_COUNT_MODEL_MEMBER_BYTES = 16 * 1024 * 1024
MAX_NATIVE_COUNT_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_NATIVE_COUNT_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_MODEL_DECOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_GRAPH_DECOMPRESSED_BYTES = 512 * 1024 * 1024


def canonical(matrix, metadata, rows=None):
    from baseline.hest_large_runtime.adapter import CanonicalInput
    rt = runtime()[0]
    rows = np.arange(matrix.shape[0]) if rows is None else np.asarray(rows)
    x = matrix[rows].astype(np.int64).tocsr()
    x.sum_duplicates(); x.eliminate_zeros(); x.sort_indices()
    xy = np.ascontiguousarray(metadata["coordinates"][rows])
    md = {"schema": "047-exact-metadata-v1", "spot_ids": [metadata["spot_ids"][i] for i in rows],
          "gene_ids": metadata["gene_ids"], "coordinates_shape": list(xy.shape),
          "coordinates_dtype": str(xy.dtype), "coordinates_base64": base64.b64encode(xy.tobytes()).decode()}
    return CanonicalInput(Path("canonical.h5ad"), x, md, rt[0].jbytes(md), "csr_matrix")


def native_parts(path):
    from baseline.hest_large_runtime.archive import read
    man, parts, _, _ = read(path)
    return man, parts


def model_tables(parts, *, expected_genes, support_builder='numpy', value_builder='numpy'):
    if support_builder not in ('numpy', 'fused'):
        raise ValueError('Unknown support table builder')
    if value_builder not in ('numpy', 'fused'):
        raise ValueError('Unknown value table builder')
    if type(expected_genes) is not int or not 1 <= expected_genes <= 20_000_000:
        raise ValueError('Invalid expected gene count for Count model')
    _, entropy, sm, qm, qs, _ = runtime()[0]
    q0 = np.frombuffer(_bz2_exact(parts['base_probability.bz2'], expected_genes * 2, 'base_probability'), '<u2')
    support_odds = np.frombuffer(parts['shared_odds.u32'], '<u4').reshape(8, 7)
    if support_builder == 'numpy':
        support_q = sm.cdf(q0, support_odds)
    else:
        from .support_tables import fused_support_cdf
        support_q = fused_support_cdf(q0, support_odds, sm)
    groups = np.frombuffer(_bz2_exact(parts['value_group.bz2'], expected_genes, 'value_group'), np.uint8)
    q = qs.decode(parts['value_q1.bz2'], groups)
    base = np.frombuffer(_bz2_exact(parts['value_base_k.bz2'], 32 * 32 * 2, 'value_base_k'), '<u2').reshape(32, 32)
    odds = np.frombuffer(parts['value_odds.u32'], '<u4').reshape(32, 7)
    cond = np.frombuffer(_bz2_exact(parts['value_cond_k.bz2'], 32 * 7 * 32 * 2, 'value_cond_k'), '<u2').reshape(32, 7, 32)
    if value_builder == 'numpy':
        binary, tail, cdf = qm.tables(q, groups, base, odds, cond)
    else:
        from .value_tables import fused_value_tables
        binary, tail, cdf = fused_value_tables(q, groups, base, odds, cond, qm)
    return support_q, groups, binary, tail, cdf

class CountStore:
    def __init__(self, root):
        self.root = Path(root)
        index_path = self.root / "counts.index.json"
        index_size = index_path.stat().st_size
        if not 0 < index_size <= MAX_COUNT_INDEX_BYTES:
            raise ValueError("Shared Count index exceeds its 256 MiB bound")
        with index_path.open("rb") as handle:
            blob = handle.read(MAX_COUNT_INDEX_BYTES + 1)
        if len(blob) != index_size:
            raise ValueError("Shared Count index changed while reading")
        self.index = json.loads(blob)
        self.index_bytes = len(blob)
        self.tables = {}
        self.decoded_rows = 0
        self.decoded_blocks = 0
        self._zip = None
        method = self.index.get("method")
        if method not in ("Shared_whole", "Shared_blocks", "Shared_local"):
            raise ValueError("Unsupported Shared Count method")
        self._native = method == "Shared_whole"
        shape = self.index.get("shape")
        if (not isinstance(shape, list) or len(shape) != 2
                or any(type(v) is not int or v < 1 for v in shape)
                or shape[0] * shape[1] > 20_000_000):
            raise ValueError("Invalid Shared Count shape")
        n, g = shape
        archive_path = self.root / ("counts.whole.cnt" if self._native else "counts.bank")
        if self._native and archive_path.stat().st_size > MAX_NATIVE_COUNT_ARCHIVE_BYTES:
            raise ValueError("Native Count archive exceeds its 2 GiB bound")
        self.file = MeteredFile(archive_path)
        self.row_map = {}
        if self._native:
            self._zip = zipfile.ZipFile(self.file)
            manifest_info = self._zip.getinfo("manifest.json")
            if (manifest_info.file_size <= 0 or manifest_info.file_size > MAX_NATIVE_COUNT_MANIFEST_BYTES
                    or manifest_info.compress_type != zipfile.ZIP_STORED):
                raise ValueError("Invalid native Count manifest storage/bound")
            self.native_manifest = json.loads(self._zip.read("manifest.json"))
            native_files = self.native_manifest.get("files")
            names = self._zip.namelist()
            if (self.native_manifest.get("schema") != "qpatch-count-v1"
                    or self.native_manifest.get("method") != "Shared-only"
                    or self.native_manifest.get("shape") != shape
                    or not isinstance(native_files, dict)
                    or set(native_files) != set(MODEL_NAMES) | {"metadata.bz2", "support.rans", "graph.bz2", "values.rans"}
                    or len(names) != len(set(names))
                    or set(names) != set(native_files) | {"manifest.json"}):
                raise ValueError("Native Shared Count manifest/member mismatch")
        else:
            for bid, b in enumerate(self.index["blocks"]):
                for local, row in enumerate(b["rows"]):
                    if row in self.row_map or not 0 <= row < n:
                        raise ValueError("Invalid block row partition")
                    self.row_map[row] = (bid, local)
                for desc in b["members"].values():
                    if desc["offset"] < 0 or desc["offset"] + desc["bytes"] > (self.root / "counts.bank").stat().st_size:
                        raise ValueError("Invalid bank range")
            if len(self.row_map) != n:
                raise ValueError("Incomplete row partition")

    def member(self, desc, *, name=None):
        limit = MAX_COUNT_MODEL_MEMBER_BYTES if name in MODEL_NAMES else MAX_COUNT_MEMBER_BYTES
        if (not isinstance(desc, dict) or type(desc.get("offset")) is not int
                or type(desc.get("bytes")) is not int or desc["offset"] < 0
                or not 0 < desc["bytes"] <= limit):
            raise ValueError("Invalid or oversized Shared Count member descriptor")
        self.file.seek(desc["offset"])
        data = self.file.read(desc["bytes"])
        if len(data) != desc["bytes"] or hashlib.sha256(data).hexdigest() != desc["sha256"]:
            raise ValueError("Count bank member integrity failure")
        return data

    def native_member(self, name):
        d = self.native_manifest["files"][name]
        info = self._zip.getinfo(name)
        limit = MAX_COUNT_MODEL_MEMBER_BYTES if name in MODEL_NAMES else MAX_COUNT_MEMBER_BYTES
        if (type(d.get("bytes")) is not int or not 0 < d["bytes"] <= limit
                or info.file_size != d["bytes"] or info.compress_type != zipfile.ZIP_STORED):
            raise ValueError("Invalid native Shared member size/storage")
        data = self._zip.read(name)
        if len(data) != d["bytes"] or hashlib.sha256(data).hexdigest() != d["sha256"]:
            raise ValueError("Native Shared integrity failure")
        return data

    def decode_block(self, bid):
        codec_io, entropy, _, _, _, values = runtime()[0]
        if self._native:
            b = self.native_manifest
            nr, ng = b["shape"]
            model_id = "global"
            get = self.native_member
            model_get = get
        else:
            b = self.index["blocks"][bid]
            nr, ng = len(b["rows"]), self.index["shape"][1]
            model_id = b["model"]
            get = lambda name: self.member(b["members"][name], name=name)
            model_get = lambda name: self.member(self.index["models"][model_id][name], name=name)
        if model_id not in self.tables:
            self.tables[model_id] = model_tables({n: model_get(n) for n in MODEL_NAMES}, expected_genes=ng)
        sq, groups, binary, tail, cdf = self.tables[model_id]
        graph = np.frombuffer(_bz2_exact(get("graph.bz2"), nr * 6 * 4, "graph", max_bytes=MAX_GRAPH_DECOMPRESSED_BYTES), "<i4").reshape(nr, 6).copy()
        for i, row in enumerate(graph):
            k = min(i, 6)
            if np.any(row[:k] < 0) or np.any(row[:k] >= i) or len(set(row[:k])) != k or np.any(row[k:] != -1):
                raise ValueError("Noncausal block graph")
        mask = entropy.decode(np.frombuffer(get("support.rans"), np.uint8), graph, sq, nr, ng)
        ptr, idx = entropy.indices_from_support(mask, ng, b["nnz"])
        val, te, rb = values.decode(np.frombuffer(get("values.rans"), np.uint8), ptr, idx, graph, groups, binary, tail, cdf, True)
        x = codec_io.from_parts((nr, ng), ptr, idx, val)
        if te != b["tail_events"] or rb != b["remainder_bits"] or codec_io.csr_sha(x) != b["canonical_sha256"]:
            raise ValueError("Decoded Count identity mismatch")
        self.decoded_rows += nr
        self.decoded_blocks += 1
        return x

    def read(self, rows, genes):
        out = np.zeros((len(rows), len(genes)), np.uint64)
        if not len(rows) or not len(genes):
            return out
        if self._native:
            return self.decode_block(0)[rows][:, genes].toarray().astype(np.uint64)
        needed = {}
        for target, row in enumerate(rows):
            bid, local = self.row_map[int(row)]
            needed.setdefault(bid, []).append((target, local))
        for bid, selection in needed.items():
            targets, local = zip(*selection)
            out[list(targets)] = self.decode_block(bid)[list(local)][:, genes].toarray().astype(np.uint64)
        return out

    def close(self):
        if self._zip is not None:
            self._zip.close()
        self.file.close()


def pack_shared(fixed_backend, reference, output, *, method, block_pixels=1024):
    if method not in METHODS:
        raise ValueError(method)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    (out / "INCOMPLETE").write_text("building")
    with BaselineArchive(fixed_backend) as fixed:
        m = copy.deepcopy(fixed.manifest)
        if m["method"] != "C_zstd_tiff":
            raise ValueError("Fixed backend must be the qualified Zstd TIFF control")
        for name in ("images.tif", "science.h5", *m.get("attachments", [])):
            if sha256(fixed.root / name) != m["files"][name]["sha256"]:
                raise ValueError("Fixed component source changed")
            shutil.copyfile(fixed.root / name, out / name)
        m.update(format=FORMAT, method=method)
        if method == "BPCells":
            shutil.copytree(fixed.root / "counts.bp", out / "counts.bp")
        else:
            runtime()
            matrix = csr_from_h5(Path(reference) / "expression.h5ad")
            md = fixed.metadata()
            index = write_count_store(matrix, md, md["level0_xy"], out, method, block_pixels)
            m["expression"]["storage"] = method + "-B8-K32"
            m["count_parameters"] = {"block_pixels": block_pixels, "blocks": len(index["blocks"]) or 1,
                  "parameter_policy": "whole-slide frozen parameters" if method == "Shared_local" else "fit per coding object",
                  "context_policy": "original whole graph" if method == "Shared_whole" else "within spatial block; original order; no cross-block predecessors",
                  "decoded_count_cache": False, "source_snapshot_sha256": sha256(Path(__file__).with_name("_shared_vendor") / "SOURCE.json")}
        m["files"] = {p.relative_to(out).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)}
                      for p in sorted(out.rglob("*")) if p.is_file() and p.name != "INCOMPLETE"}
        write_json(out / "manifest.json", m)
    (out / "INCOMPLETE").unlink()
    with SharedArchive(out) as a:
        return a.ledger()


class SharedArchive(BaselineArchive):
    FORMAT = FORMAT
    METHODS = METHODS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._count_store = None

    def counts(self, rows=None, genes=None, *, max_cells=20_000_000):
        if self.manifest["method"] == "BPCells":
            return super().counts(rows, genes, max_cells=max_cells)
        md = self.metadata()
        ri, gi = self._select(rows, md["spot_ids"]), self._select(genes, md["gene_ids"])
        if len(ri) * len(gi) > max_cells:
            raise ValueError("Query too large")
        if len(ri) and len(gi):
            if self._count_store is None:
                self._count_store = CountStore(self.root)
            x = self._count_store.read(ri, gi)
        else:
            x = np.zeros((len(ri), len(gi)), np.uint64)
        return {"counts": x, "spot_ids": np.asarray([md["spot_ids"][i] for i in ri], dtype=str),
                "gene_ids": np.asarray([md["gene_ids"][i] for i in gi], dtype=str),
                "coordinates": md["coordinates"][ri], "level0_xy": md["level0_xy"][ri]}

    def metrics(self):
        m = super().metrics()
        if self._count_store is not None:
            c = self._count_store
            m["shared_count"] = {**c.file.metrics(), "index_bytes": c.index_bytes,
                                  "decoded_rows": c.decoded_rows, "decoded_blocks": c.decoded_blocks}
            m["instrumented_file_read_bytes"] += c.index_bytes + c.file.metrics()["file_read_bytes"]
        return m

    def close(self):
        if self._count_store is not None:
            self._count_store.close()
        super().close()

