"""Streaming integrity and HDF5 semantics, adapted from STCompBench metadata checks."""
import base64
import hashlib
import json
from pathlib import Path

import h5py
import numpy as np


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def token(value):
    a = np.asarray(value)
    head = json.dumps([str(a.dtype), a.shape]).encode()
    body = repr(a.tolist()).encode("utf-8") if a.dtype.kind in "OUS" else a.tobytes()
    return head + b"\n" + body


def validate_hdf5_links(root, *, max_objects=2_000_000):
    """Reject links and storage references that can escape the selected HDF5 file.

    Traverse link metadata before dereferencing children. Only hard links are
    accepted; aliases/cycles, soft/external links, virtual datasets, external
    raw storage and object references are outside the supported H5AD contract.
    """
    seen = set()
    pending = [root]
    while pending:
        node = pending.pop()
        address = h5py.h5o.get_info(node.id).addr
        if address in seen:
            raise ValueError("HDF5 hard-link aliases/cycles are unsupported")
        seen.add(address)
        if len(seen) > max_objects:
            raise ValueError("HDF5 object count exceeds the qualified bound")
        if isinstance(node, h5py.Dataset):
            if node.is_virtual or node.external:
                raise ValueError("External/virtual HDF5 datasets must be materialized first")
            if h5py.check_dtype(ref=node.dtype) is not None:
                raise ValueError("HDF5 object references are unsupported")
            continue
        for key in node:
            if not isinstance(node.get(key, getlink=True), h5py.HardLink):
                raise ValueError("Soft/external HDF5 links are unsupported")
            pending.append(node[key])


def h5_digest(path):
    """Includes dataset values, dtype, shape and all attributes, excluding layout."""
    h = hashlib.sha256()
    seen = set()

    def feed(v):
        b = token(v)
        h.update(len(b).to_bytes(8, "little"))
        h.update(b)

    def visit(node, name):
        address = h5py.h5o.get_info(node.id).addr
        if address in seen:
            raise ValueError("HDF5 hard-link aliases/cycles are unsupported")
        seen.add(address)
        feed(name)
        for key in sorted(node.attrs):
            value = node.attrs[key]
            if isinstance(value, (h5py.Reference, h5py.RegionReference)) or h5py.check_dtype(ref=np.asarray(value).dtype) is not None:
                raise ValueError("HDF5 object references are unsupported")
            feed(key)
            feed(node.attrs[key])
        if isinstance(node, h5py.Dataset):
            if h5py.check_dtype(ref=node.dtype) is not None:
                raise ValueError("HDF5 object references are unsupported")
            if node.is_virtual or node.external:
                raise ValueError("External/virtual HDF5 datasets must be materialized first")
            feed(str(node.dtype))
            feed(node.shape)
            if node.shape is None:
                return
            if not node.ndim:
                feed(node[()])
            else:
                for start in range(0, len(node), 64):
                    feed(node[start:start + 64])
        else:
            for key in sorted(node):
                if not isinstance(node.get(key, getlink=True), h5py.HardLink):
                    raise ValueError("Soft/external HDF5 links are unsupported")
                visit(node[key], name.rstrip("/") + "/" + key)

    with h5py.File(path, "r") as f:
        validate_hdf5_links(f)
        visit(f, "/")
    return h.hexdigest()


def jsonable(value):
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        return {"numerator": value.numerator, "denominator": value.denominator}
    raise ValueError(f"Unsupported TIFF metadata type: {type(value).__name__}")


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   allow_nan=False) + "\n", encoding="utf-8")


def text_ids(group):
    key = group.attrs.get("_index", "_index")
    node = group[key]
    if isinstance(node, h5py.Group):
        encoding = node.attrs.get("encoding-type", "")
        if encoding not in ("nullable-string-array", b"nullable-string-array") or "values" not in node:
            raise ValueError("Unsupported identifier encoding")
        if "mask" in node and np.any(node["mask"][:]):
            raise ValueError("Missing identifiers are not allowed")
        values = node["values"][:]
    else:
        values = node[:]
    return [x.decode("utf-8") if isinstance(x, bytes) else str(x) for x in values]


def count_schema(f, spatial_key):
    validate_hdf5_links(f)
    rows, genes = text_ids(f["obs"]), text_ids(f["var"])
    if not rows or not genes or len(set(rows)) != len(rows) :
        raise ValueError("Empty axes or duplicate obs IDs")
    xy = np.asarray(f["obsm"][spatial_key])
    if xy.shape != (len(rows), 2) or xy.dtype.kind not in "fiu" or not np.isfinite(xy).all():
        raise ValueError("Coordinates must be a finite N x 2 numeric array")
    x = f["X"]
    if isinstance(x, h5py.Dataset):
        shape, kind = x.shape, "dense"
        data = x
    else:
        enc = x.attrs.get("encoding-type", "")
        if isinstance(enc, bytes):
            enc = enc.decode()
        if enc != "csr_matrix":
            raise ValueError("Version 0.1 supports dense or CSR X; CSC needs an adapter")
        shape, kind, data = tuple(x.attrs["shape"]), "csr", x["data"]
        ptr = np.asarray(x["indptr"])
        if ptr.dtype.kind not in "iu" or ptr.shape != (len(rows) + 1,) or ptr[0] != 0:
            raise ValueError("Invalid CSR indptr")
        if np.any(ptr[1:] < ptr[:-1]) or ptr[-1] != len(data) or len(x["indices"]) != len(data):
            raise ValueError("Invalid CSR lengths")
        if x["indices"].dtype.kind not in "iu":
            raise ValueError("CSR indices must be integers")
        for row in range(len(rows)):
            idx = x["indices"][int(ptr[row]):int(ptr[row+1])]
            if np.any(idx < 0) or np.any(idx >= len(genes)) or np.any(idx[1:] <= idx[:-1]):
                raise ValueError("CSR rows must have sorted, unique, in-range indices")
    if tuple(shape) != (len(rows), len(genes)):
        raise ValueError("X and obs/var dimensions disagree")
    if data.dtype.kind not in "fiu":
        raise ValueError("X must be numeric nonnegative integer counts")
    for start in range(0, len(data), 1024):
        a = data[start:start + 1024]
        if not np.isfinite(a).all() or np.any(a < 0) or np.any(a > 2**32 - 1) or np.any(a != np.floor(a)):
            raise ValueError("X values must be exact nonnegative uint32-range counts")
    return {"shape": [int(v) for v in shape], "kind": kind, "dtype": str(data.dtype)}, rows, genes, xy
