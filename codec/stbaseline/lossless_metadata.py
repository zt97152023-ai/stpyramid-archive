"""Opt-in, layout-only HDF5 metadata repacking with the existing reader format.

The fixed profile compresses arrays of at least 1 KiB with built-in gzip level 9.
Numeric arrays use shuffle; strings do not. Scalars, empty/null datasets, small
arrays and uncommon datatypes are copied by HDF5 without reinterpreting values.
The 256 KiB chunk budget bounds fixed-width transfer buffers, not variable-length
element payloads, attributes, HDF5's own cache, or the existing digest's buffers.
Callers remain responsible for qualifying the source's total resource budget.
"""
from itertools import product
import os
from pathlib import Path

import h5py
import numpy as np

from .integrity import h5_digest


PROFILE = "hdf5-gzip9-shuffle-256k-v1"
CHUNK_BYTES = 256 * 1024
MIN_ARRAY_BYTES = 1024


def _has_reference(dtype):
    """Also reject references nested in compound, array and vlen datatypes."""
    if h5py.check_dtype(ref=dtype) is not None:
        return True
    if dtype.fields:
        return any(_has_reference(field[0]) for field in dtype.fields.values())
    if dtype.subdtype:
        return _has_reference(dtype.subdtype[0])
    variable = h5py.check_dtype(vlen=dtype)
    return isinstance(variable, np.dtype) and _has_reference(variable)


def _check_types(node):
    for name in node.attrs:
        attribute = node.attrs.get_id(name)
        try:
            if _has_reference(attribute.dtype):
                raise ValueError("HDF5 object references are unsupported")
        finally:
            attribute.close()
    if isinstance(node, h5py.Dataset):
        if _has_reference(node.dtype):
            raise ValueError("HDF5 object references are unsupported")
    elif isinstance(node, h5py.Group):
        for name in node:
            _check_types(node[name])
    else:
        raise ValueError("Named HDF5 datatypes are unsupported")


def _copy_attributes(source, target):
    # Reusing the actual HDF5 type/space avoids silently changing fixed strings,
    # ASCII/UTF-8 metadata, enums, endian order, or scalar/null attribute shapes.
    for name in source.attrs:
        original = source.attrs.get_id(name)
        try:
            datatype, space = original.get_type(), original.get_space()
            copied = h5py.h5a.create(target.id, name.encode("utf-8"), datatype, space)
            try:
                if original.shape is not None:
                    values = np.zeros(original.shape, dtype=original.dtype)
                    memory_type = (h5py.h5t.py_create(original.dtype)
                                   if original.dtype.hasobject else datatype)
                    original.read(values, mtype=memory_type)
                    copied.write(values, mtype=memory_type)
            finally:
                copied.close()
        finally:
            original.close()


def _chunks(shape, itemsize):
    remaining = max(1, CHUNK_BYTES // max(1, itemsize))
    chunks = [1] * len(shape)
    # Keep trailing dimensions together where possible, then bound row groups.
    for axis in range(len(shape) - 1, -1, -1):
        chunks[axis] = min(shape[axis], remaining)
        remaining = max(1, remaining // chunks[axis])
    return tuple(chunks)


def _compression_policy(dataset):
    if dataset.shape is None:
        return "copy-null"
    if not dataset.ndim:
        return "copy-scalar"
    if not dataset.size:
        return "copy-empty"
    if dataset.size * dataset.dtype.itemsize < MIN_ARRAY_BYTES:
        return "copy-small"
    if dataset.dtype.kind in "biufc" and not dataset.dtype.hasobject:
        return "gzip-shuffle"
    if h5py.check_string_dtype(dataset.dtype) is not None:
        return "gzip-string"
    return "copy-special"


def _copy_array(source, parent, name, policy):
    if policy.startswith("copy-"):
        source.parent.copy(source, parent, name=name)
        return parent[name]
    chunks = _chunks(source.shape, source.dtype.itemsize)
    creation = source.id.get_create_plist().copy()
    while creation.get_nfilters():
        creation.remove_filter(creation.get_filter(0)[0])
    creation.set_chunk(chunks)
    if policy == "gzip-shuffle":
        creation.set_shuffle()
    creation.set_deflate(9)
    creation.set_obj_track_times(False)
    link = h5py.h5p.create(h5py.h5p.LINK_CREATE)
    link.set_char_encoding(h5py.h5t.CSET_UTF8)
    datatype = source.id.get_type()
    target = h5py.Dataset(h5py.h5d.create(parent.id, name.encode("utf-8"), datatype,
                                       source.id.get_space(), dcpl=creation, lcpl=link))
    _copy_attributes(source, target)
    for start in product(*(range(0, size, step) for size, step in zip(source.shape, chunks))):
        count = tuple(min(step, size - offset) for offset, size, step in zip(start, source.shape, chunks))
        if source.dtype.hasobject:
            selection = tuple(slice(offset, offset + length) for offset, length in zip(start, count))
            target[selection] = source[selection]
        else:
            # Identical file and memory datatypes avoid floating-point/endian
            # conversion, preserving NaN payloads and the sign bit of zero.
            values = np.empty(count, dtype=source.dtype)
            memory = h5py.h5s.create_simple(count)
            source_space, target_space = source.id.get_space(), target.id.get_space()
            source_space.select_hyperslab(start, count)
            target_space.select_hyperslab(start, count)
            source.id.read(memory, source_space, values, mtype=datatype)
            target.id.write(memory, target_space, values, mtype=datatype)
    return target


def _copy_group(source, target, datasets):
    _copy_attributes(source, target)
    for name in source:
        node = source[name]
        if isinstance(node, h5py.Group):
            creation = node.id.get_create_plist().copy()
            creation.set_obj_track_times(False)
            link = h5py.h5p.create(h5py.h5p.LINK_CREATE)
            link.set_char_encoding(h5py.h5t.CSET_UTF8)
            child = h5py.Group(h5py.h5g.create(target.id, name.encode("utf-8"), lcpl=link, gcpl=creation))
            _copy_group(node, child, datasets)
        else:
            policy = _compression_policy(node)
            child = _copy_array(node, target, name, policy)
            if not node.id.get_type().equal(child.id.get_type()) or node.shape != child.shape:
                raise ValueError("HDF5 datatype or shape changed during metadata repack")
            datasets.append({"path": node.name, "shape": None if node.shape is None else list(node.shape),
                             "dtype": str(node.dtype), "source_stored_bytes": node.id.get_storage_size(),
                             "target_stored_bytes": child.id.get_storage_size(),
                             "chunks": None if child.chunks is None else list(child.chunks),
                             "compression": child.compression, "compression_opts": child.compression_opts,
                             "shuffle": child.shuffle, "policy": policy})


def repack_metadata(source, target):
    """Repack a whole metadata HDF5 file into a new path and verify semantics.

    This also accepts the existing ``science.h5`` empty-X skeleton. The source is
    opened read-only. Unsupported links/references are rejected before creating
    output. A write/verification failure leaves the new file for diagnosis and
    raises; callers must not publish it without the returned success receipt.
    """
    source = Path(source).resolve(strict=True)
    requested_target = Path(target)
    target = requested_target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("Metadata source and target must be disjoint")
    if requested_target.is_symlink() or target.exists():
        raise FileExistsError(target)
    expected = h5_digest(source)
    datasets = []
    with h5py.File(source, "r") as original:
        _check_types(original)
        creation = h5py.h5p.create(h5py.h5p.FILE_CREATE)
        creation.set_obj_track_times(False)
        with h5py.File(h5py.h5f.create(os.fsencode(target), h5py.h5f.ACC_EXCL, fcpl=creation)) as copied:
            _copy_group(original, copied, datasets)
            copied.flush()
            for dataset in datasets:
                dataset["target_stored_bytes"] = copied[dataset["path"]].id.get_storage_size()
    actual = h5_digest(target)
    if actual != expected:
        raise ValueError("HDF5 metadata semantic roundtrip failed")
    source_bytes, target_bytes = source.stat().st_size, target.stat().st_size
    return {"profile": PROFILE, "source_bytes": source_bytes, "target_bytes": target_bytes,
            "saved_bytes": source_bytes - target_bytes, "semantic_sha256_before": expected,
            "semantic_sha256_after": actual, "datasets": datasets}
