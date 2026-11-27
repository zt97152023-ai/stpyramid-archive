"""Metadata-only HDF5 datatype oracle for the R3 replay verifier.

H5Tencode stores a binary object description; H5Tequal checks datatype
equivalence. Different message versions/padding need not change the datatype.
This installed HDF5's equality can ignore vlen string charset, so strings also
require an explicit recursive charset/padding/variable-size contract.
Official semantics: https://support.hdfgroup.org/documentation/hdf5/latest/group___h5_t.html

Only use trusted capture-generated oracle JSON whose file hash is bound by the
calling experiment. Bounds and schema checks are not a sandbox for H5Tdecode.
Dataset and attribute values are deliberately NOT read here: the caller must
also compare the independent complete H5AD semantic/value digest.
"""
import json
from pathlib import Path
import re

import h5py


SCHEMA = "st-integrated-h5-type-oracle-1"
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_ATTRIBUTES = 100_000
MAX_NAME_BYTES = 4096
MAX_TYPE_BYTES = 64 * 1024
MAX_TOTAL_TYPE_BYTES = 8 * 1024 * 1024
MAX_RANK = 32
HEX = re.compile(r"[0-9a-f]+\Z")
SPACE_CLASSES = {h5py.h5s.NULL: "NULL", h5py.h5s.SCALAR: "SCALAR", h5py.h5s.SIMPLE: "SIMPLE"}


def _keys(value, expected, label):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError("Unexpected " + label + " schema")


def _name(value, *, node=False):
    if (type(value) is not str or not value or len(value.encode("utf8")) > MAX_NAME_BYTES
            or any(ord(c) < 32 for c in value)):
        raise ValueError("Invalid oracle name")
    if node and value != "/":
        if not value.startswith("/") or any(p in ("", ".", "..") for p in value[1:].split("/")):
            raise ValueError("Invalid absolute HDF5 object name")
    return value


def _space_schema(shape, space_class):
    if space_class == "NULL":
        valid = shape is None
    elif space_class == "SCALAR":
        valid = type(shape) is list and shape == []
    elif space_class == "SIMPLE":
        valid = (type(shape) is list and 1 <= len(shape) <= MAX_RANK
                 and all(type(v) is int and 0 <= v < 2**63 for v in shape))
    else:
        valid = False
    if not valid:
        raise ValueError("Invalid dataspace class/shape")


def _hex(value):
    # H5Tencode's object header starts with the datatype-object marker/version.
    # Validate bounds before bytes.fromhex or handing a trusted buffer to HDF5.
    if (type(value) is not str or not 8 <= len(value) <= MAX_TYPE_BYTES * 2
            or len(value) % 2 or not value.startswith("0300") or HEX.fullmatch(value) is None):
        raise ValueError("Invalid or excessive encoded datatype hex")
    return len(value) // 2


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _oracle(oracle):
    if isinstance(oracle, (str, Path)):
        path = Path(oracle)
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("Oracle JSON exceeds byte bound")
        with path.open("rb") as handle:
            raw = handle.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError("Oracle JSON exceeds byte bound")
        oracle = json.loads(raw, object_pairs_hook=_no_duplicate_keys,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON constant")))
    _keys(oracle, {"schema", "records"}, "oracle")
    if oracle["schema"] != SCHEMA or type(oracle["records"]) is not list or not 1 <= len(oracle["records"]) <= MAX_RECORDS:
        raise ValueError("Invalid oracle schema or node bound")
    names, attributes, type_bytes = [], 0, 0
    for record in oracle["records"]:
        if type(record) is not dict:
            raise ValueError("Invalid node record")
        kind = record.get("kind")
        required = {"kind", "name", "attributes"}
        if kind == "dataset":
            required |= {"shape", "space_class", "type_hex"}
        elif kind != "group":
            raise ValueError("Unsupported oracle node kind")
        _keys(record, required, "node")
        names.append(_name(record["name"], node=True))
        if kind == "dataset":
            _space_schema(record["shape"], record["space_class"])
            type_bytes += _hex(record["type_hex"])
        attrs = record["attributes"]
        if type(attrs) is not list:
            raise ValueError("Invalid attribute list")
        attributes += len(attrs)
        if attributes > MAX_ATTRIBUTES:
            raise ValueError("Oracle attribute bound exceeded")
        attribute_names = []
        for attribute in attrs:
            _keys(attribute, {"name", "shape", "space_class", "type_hex"}, "attribute")
            attribute_names.append(_name(attribute["name"]))
            _space_schema(attribute["shape"], attribute["space_class"])
            type_bytes += _hex(attribute["type_hex"])
        if attribute_names != sorted(set(attribute_names)):
            raise ValueError("Attribute names must be unique and ordered")
        if type_bytes > MAX_TOTAL_TYPE_BYTES:
            raise ValueError("Oracle datatype budget exceeded")
    if names != sorted(set(names)) or names[0] != "/" or oracle["records"][0]["kind"] != "group":
        raise ValueError("Node names must be unique and ordered, with a group root")
    kinds = {r["name"]: r["kind"] for r in oracle["records"]}
    for name in names[1:]:
        parent = name.rsplit("/", 1)[0] or "/"
        if kinds.get(parent) != "group":
            raise ValueError("Oracle parent group coverage differs")
    return oracle


def _walk(handle):
    """Visit ordinary group/dataset metadata only; reject aliases and links."""
    seen = set()
    def walk(node, name):
        _name(name, node=True)
        address = h5py.h5o.get_info(node.id).addr
        if address in seen:
            raise ValueError("HDF5 hard-link aliases/cycles are unsupported")
        seen.add(address)
        if len(seen) > MAX_RECORDS:
            raise ValueError("HDF5 node bound exceeded")
        if not isinstance(node, (h5py.Group, h5py.Dataset)):
            raise ValueError("Only HDF5 group/dataset nodes are supported")
        yield name, node
        if isinstance(node, h5py.Group):
            for key in sorted(node):
                if not isinstance(node.get(key, getlink=True), h5py.HardLink):
                    raise ValueError("Soft/external HDF5 links are unsupported")
                yield from walk(node[key], name.rstrip("/") + "/" + key)
    yield from walk(handle, "/")


def _space(space, shape):
    result = {"space_class": SPACE_CLASSES.get(space.get_simple_extent_type()),
              "shape": None if shape is None else list(shape)}
    _space_schema(result["shape"], result["space_class"])
    return result


def _describe_type(owner, shape):
    datatype, space = owner.get_type(), owner.get_space()
    try:
        result = {"type_hex": datatype.encode().hex(), **_space(space, shape)}
        _hex(result["type_hex"])
        return result
    finally:
        datatype.close(); space.close()


def capture(path):
    """Return a deterministic JSON-serializable metadata-only source oracle."""
    records, attributes, type_bytes = [], 0, 0
    with h5py.File(path, "r") as handle:
        for name, node in _walk(handle):
            record = {"name": name, "kind": "dataset" if isinstance(node, h5py.Dataset) else "group", "attributes": []}
            if isinstance(node, h5py.Dataset):
                record.update(_describe_type(node.id, node.shape))
                type_bytes += len(record["type_hex"]) // 2
            for key in sorted(node.attrs):
                _name(key)
                attributes += 1
                if attributes > MAX_ATTRIBUTES:
                    raise ValueError("HDF5 attribute bound exceeded")
                attribute = node.attrs.get_id(key)
                try:
                    entry = {"name": key, **_describe_type(attribute, attribute.shape)}
                finally:
                    attribute.close()
                record["attributes"].append(entry)
                type_bytes += len(entry["type_hex"]) // 2
                if type_bytes > MAX_TOTAL_TYPE_BYTES:
                    raise ValueError("HDF5 datatype budget exceeded")
            records.append(record)
    result = {"schema": SCHEMA, "records": sorted(records, key=lambda r: r["name"])}
    return _oracle(result)


def _string_contract(datatype, depth=0):
    """Retain string semantics even where the installed H5Tequal ignores them."""
    if depth > 64:
        raise ValueError("Nested datatype depth exceeds verification bound")
    kind = datatype.get_class()
    if kind == h5py.h5t.STRING:
        return ("string", datatype.get_cset(), datatype.get_strpad(),
                bool(datatype.is_variable_str()), datatype.get_size())
    if kind == h5py.h5t.COMPOUND:
        members = []
        for i in range(datatype.get_nmembers()):
            member = datatype.get_member_type(i)
            try:
                members.append((datatype.get_member_name(i), datatype.get_member_offset(i),
                                _string_contract(member, depth + 1)))
            finally:
                member.close()
        return ("compound", tuple(members))
    if kind in (h5py.h5t.ARRAY, h5py.h5t.VLEN):
        base = datatype.get_super()
        try:
            nested = _string_contract(base, depth + 1)
            return (("array", tuple(datatype.get_array_dims()), nested)
                    if kind == h5py.h5t.ARRAY else ("vlen", nested))
        finally:
            base.close()
    return None


def _compare_type(owner, shape, expected, label):
    datatype, space = owner.get_type(), owner.get_space()
    decoded = None
    try:
        if _space(space, shape) != {"shape": expected["shape"], "space_class": expected["space_class"]}:
            raise ValueError("HDF5 dataspace mismatch: " + label)
        try:
            decoded = h5py.h5t.decode(bytes.fromhex(expected["type_hex"]))
        except Exception as exc:
            raise ValueError("Oracle datatype cannot be decoded: " + label) from exc
        if not decoded.equal(datatype) or _string_contract(decoded) != _string_contract(datatype):
            raise ValueError("HDF5 datatype mismatch: " + label)
    finally:
        if decoded is not None:
            decoded.close()
        datatype.close(); space.close()


def validate(path, oracle):
    """Check coverage/shapes, H5Tequal and recursive string semantics per type.

    Accept either a capture result or a bounded JSON file path. The caller binds
    the trusted oracle's file hash and separately verifies all scientific values.
    """
    oracle = _oracle(oracle)
    expected = {record["name"]: record for record in oracle["records"]}
    actual_names, types_checked, attributes = [], 0, 0
    with h5py.File(path, "r") as handle:
        for name, node in _walk(handle):
            actual_names.append(name)
            record = expected.get(name)
            kind = "dataset" if isinstance(node, h5py.Dataset) else "group"
            if record is None or kind != record["kind"]:
                raise ValueError("HDF5 node coverage/kind mismatch: " + name)
            if isinstance(node, h5py.Dataset):
                _compare_type(node.id, node.shape, record, name)
                types_checked += 1
            names = sorted(node.attrs)
            if names != [a["name"] for a in record["attributes"]]:
                raise ValueError("HDF5 attribute coverage mismatch: " + name)
            for entry in record["attributes"]:
                attribute = node.attrs.get_id(entry["name"])
                try:
                    _compare_type(attribute, attribute.shape, entry, name + "@" + entry["name"])
                finally:
                    attribute.close()
                types_checked += 1; attributes += 1
    if sorted(actual_names) != [r["name"] for r in oracle["records"]]:
        raise ValueError("HDF5 node coverage mismatch")
    return {"ok": True, "schema": SCHEMA, "nodes": len(actual_names), "types_checked": types_checked,
            "attributes_checked": attributes,
            "comparison": "H5Tdecode/H5Tequal plus recursive string contract and exact names/kinds/dataspaces",
            "read_scope": "HDF5 metadata and type descriptions only; no dataset or attribute values read"}
