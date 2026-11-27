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
from .integrity import sha256, write_json
from .reader import MeteredFile

FORMAT = "stbaseline-shared-1"
METHODS = ("BPCells", "Shared_whole", "Shared_blocks", "Shared_local")
MODEL_NAMES = ("base_probability.bz2", "shared_odds.u32", "value_q1.bz2",
               "value_group.bz2", "value_base_k.bz2", "value_odds.u32", "value_cond_k.bz2")


@lru_cache(maxsize=1)
def runtime():
    vendor = Path(__file__).with_name("_shared_vendor")
    record = json.loads((vendor / "SOURCE.json").read_text(encoding="utf-8-sig"))
    if not (vendor / 'baseline').is_dir():
        raise RuntimeError('Run python fetch_count_runtime.py to install the pinned Count source')
    for f in record["files"]:
        if sha256(vendor / f["path"]) != f["sha256"]:
            raise ValueError("Shared source pin mismatch: " + f["path"])
    sys.path.insert(0, str(vendor))
    p = vendor / "baseline/evidence/COUNT-HEST-SHARED-K32-001/worker.py"
    spec = importlib.util.spec_from_file_location("stbaseline_frozen_k32", p)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    rt = worker.load(32)
    from baseline.hest_large_runtime import shared
    # Same substitutions as the frozen K32 worker, without diagnostic wrappers.
    src = inspect.getsource(shared.encode).replace("load_qpatch_runtime()", "_runtime()")
    src = src.replace("    from .archive import write_shared",
                      "    info.update(value_group_count=32, value_group_rule='fixed-log-axis-v1')\n    from .archive import write_shared")
    ns = dict(shared.__dict__)
    ns.update(_runtime=lambda: rt)
    exec(compile(src, str(p), "exec"), ns)
    return rt, ns["encode"], shared.graph_for


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


def model_tables(parts, *, support_builder='numpy', value_builder='numpy'):
    if support_builder not in ('numpy', 'fused'):
        raise ValueError('Unknown support table builder')
    if value_builder not in ('numpy', 'fused'):
        raise ValueError('Unknown value table builder')
    _, entropy, sm, qm, qs, _ = runtime()[0]
    q0 = np.frombuffer(bz2.decompress(parts["base_probability.bz2"]), "<u2")
    support_odds = np.frombuffer(parts["shared_odds.u32"], "<u4").reshape(8, 7)
    if support_builder == 'numpy':
        support_q = sm.cdf(q0, support_odds)
    else:
        from .support_tables import fused_support_cdf
        support_q = fused_support_cdf(q0, support_odds, sm)
    groups = np.frombuffer(bz2.decompress(parts["value_group.bz2"]), np.uint8)
    q = qs.decode(parts["value_q1.bz2"], groups)
    base = np.frombuffer(bz2.decompress(parts["value_base_k.bz2"]), "<u2").reshape(32, 32)
    odds = np.frombuffer(parts["value_odds.u32"], "<u4").reshape(32, 7)
    cond = np.frombuffer(bz2.decompress(parts["value_cond_k.bz2"]), "<u2").reshape(32, 7, 32)
    if value_builder == 'numpy':
        binary, tail, cdf = qm.tables(q, groups, base, odds, cond)
    else:
        from .value_tables import fused_value_tables
        binary, tail, cdf = fused_value_tables(q, groups, base, odds, cond)
    return support_q, groups, binary, tail, cdf


def local_streams(x, coords, tables):
    _, entropy, _, _, _, values = runtime()[0]
    graph = runtime()[2](coords)
    sq, groups, binary, tail, cdf = tables
    support = entropy.packed_support(x.indptr, x.indices, *x.shape)
    sb, _ = entropy.encode(support, graph, sq, x.shape[1])
    vb, _, te, rb = values.encode(x.indptr, x.indices, x.data, graph, groups, binary, tail, cdf, True)
    return {"graph.bz2": bz2.compress(graph.astype("<i4").tobytes(), 9),
            "support.rans": sb.tobytes(), "values.rans": vb.tobytes()}, {"tail_events": int(te), "remainder_bits": int(rb)}


def partition(xy, block_pixels):
    if not isinstance(block_pixels, int) or block_pixels < 1:
        raise ValueError("Positive integer block size required")
    cells = np.floor(np.asarray(xy) / block_pixels).astype(np.int64)
    keys = sorted(set(map(tuple, cells.tolist())), key=lambda c: (c[1], c[0]))
    return [(list(map(int, k)), np.flatnonzero(np.all(cells == k, axis=1)).tolist()) for k in keys]


def write_count_store(matrix, metadata, level0_xy, output, method, block_pixels=1024):
    """Common compact envelope for both block arms; identities stored once outside."""
    output = Path(output)
    codec, encode_native, _ = runtime()
    full_input = canonical(matrix, metadata)
    index = {"version": 1, "method": method, "shape": list(matrix.shape), "block_pixels": block_pixels,
             "canonical_sha256": codec[0].csr_sha(full_input.matrix), "blocks": [], "models": {}}
    if method == "Shared_whole":
        encode_native(full_input, output / "counts.whole.cnt")
        index["native_file"] = "counts.whole.cnt"
        write_json(output / "counts.index.json", index)
        return index
    spatial_blocks = partition(level0_xy, block_pixels)
    with tempfile.TemporaryDirectory(prefix="stshared_encode_") as tmp, (output / "counts.bank").open("xb") as bank:
        def append_member(data, category):
            member = {"offset": bank.tell(), "bytes": len(data),
                      "sha256": hashlib.sha256(data).hexdigest(), "category": category}
            bank.write(data)
            return member

        def append_model(parts):
            return {name: append_member(parts[name], "models") for name in MODEL_NAMES}

        if method == "Shared_local":
            # Derive the sample-wide model once. The bank receives only its model members.
            model_source = Path(tmp) / "whole.cnt"
            encode_native(full_input, model_source)
            _, model_parts = native_parts(model_source)
            index["models"]["global"] = append_model(model_parts)
            tables = model_tables(model_parts)
            del model_parts
        for block_id, (cell, rows) in enumerate(spatial_blocks):
            block_input = canonical(matrix, metadata, rows)
            if method == "Shared_blocks":
                native_record = Path(tmp) / f"{block_id}.cnt"
                encode_native(block_input, native_record)
                stats, parts = native_parts(native_record)
                model_id = str(block_id)
                index["models"][model_id] = append_model(parts)
            else:
                parts, stats = local_streams(block_input.matrix, metadata["coordinates"][rows], tables)
                model_id = "global"
            streams = {name: append_member(parts[name], "graph" if name == "graph.bz2" else "streams")
                       for name in ("graph.bz2", "support.rans", "values.rans")}
            index["blocks"].append({
                "cell": cell, "rows": rows, "nnz": int(block_input.matrix.nnz),
                "model": model_id, "members": streams, "canonical_sha256": codec[0].csr_sha(block_input.matrix),
                "tail_events": stats["tail_events"], "remainder_bits": stats["remainder_bits"],
            })
    write_json(output / "counts.index.json", index)
    return index


class CountStore:
    def __init__(self, root):
        self.root = Path(root)
        blob = (self.root / "counts.index.json").read_bytes()
        self.index = json.loads(blob)
        self.index_bytes = len(blob)
        self.tables = {}
        self.decoded_rows = 0
        self.decoded_blocks = 0
        self._zip = None
        self._native = self.index["method"] == "Shared_whole"
        self.file = MeteredFile(self.root / ("counts.whole.cnt" if self._native else "counts.bank"))
        n, g = self.index["shape"]
        self.row_map = {}
        if self._native:
            self._zip = zipfile.ZipFile(self.file)
            self.native_manifest = json.loads(self._zip.read("manifest.json"))
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

    def member(self, desc):
        self.file.seek(desc["offset"])
        data = self.file.read(desc["bytes"])
        if len(data) != desc["bytes"] or hashlib.sha256(data).hexdigest() != desc["sha256"]:
            raise ValueError("Count bank member integrity failure")
        return data

    def native_member(self, name):
        data = self._zip.read(name)
        d = self.native_manifest["files"][name]
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
            get = lambda name: self.member(b["members"][name])
            model_get = lambda name: self.member(self.index["models"][model_id][name])
        if model_id not in self.tables:
            self.tables[model_id] = model_tables({n: model_get(n) for n in MODEL_NAMES})
        sq, groups, binary, tail, cdf = self.tables[model_id]
        graph = np.frombuffer(bz2.decompress(get("graph.bz2")), "<i4").reshape(nr, 6).copy()
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

