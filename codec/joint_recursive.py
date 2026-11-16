"""Lossless native TIFF levels with an explicitly bounded parent-tile graph."""
import hashlib
import shutil
import zlib

import numpy as np

import joint_next as image
from stbaseline.integrity import write_json

FORMAT = "stpyramid-recursive-native-rgb8-v1"
PRESETS = {"regular": "encode_recursive", "fast": "encode_recursive_fast"}
ROUTES = (image.JXL, image.ANCHORED089, image.CONTROL091, image.CONDITIONAL091, image.HF)


class PyramidLayout:
    def __init__(self, levels, max_depth):
        if type(max_depth) is not int or not 1 <= max_depth <= 15:
            raise ValueError("Reference depth must be between 1 and 15")
        self.levels = levels
        self.starts = image.starts(levels)
        self.dependent = set()
        depth = 0
        for level, (child, parent) in enumerate(zip(levels, levels[1:])):
            h, w, _ = child["shape"]
            parent_h, parent_w, _ = parent["shape"]
            if depth < max_depth and (parent_h, parent_w) == (h // 2, w // 2):
                self.dependent.add(level)
                depth += 1
            else:
                # This native level is an independent checkpoint for the finer chain.
                depth = 0

    def parent(self, descriptor):
        _, level, x, y, width, height = descriptor
        if level not in self.dependent:
            raise ValueError("No parent references at this level")
        parent_h, parent_w, _ = self.levels[level + 1]["shape"]
        xs = np.minimum(x // 2 + np.arange((width + 1) // 2), parent_w - 1)
        ys = np.minimum(y // 2 + np.arange((height + 1) // 2), parent_h - 1)
        tile_x, tile_y = int(xs[0] // 256), int(ys[0] // 256)
        if int(xs[-1] // 256) != tile_x or int(ys[-1] // 256) != tile_y:
            raise ValueError("Reference spans multiple parent tiles")
        parent_id = self.starts[level + 1] + tile_y * ((parent_w + 255) // 256) + tile_x
        return parent_id, ys % 256, xs % 256

    def conditioned_header(self, blob, descriptor):
        gid, level, _, _, width, height = descriptor
        if level not in self.dependent:
            raise ValueError("Conditional record at independent level")
        return image.cond._record(blob, gid - self.starts[level], width, height, 1)


class Reader(image.Reader):
    def _configure_layout(self, manifest):
        if (manifest.get("format") != FORMAT or manifest.get("image_only") is not True
                or manifest.get("profile") not in PRESETS):
            raise ValueError("Unsupported recursive image format")
        self._depth_limit = manifest.get("max_reference_depth")
        levels = manifest["image"]["levels"]
        self._layout = PyramidLayout(levels, self._depth_limit)
        if manifest.get("referenced_levels") != sorted(self._layout.dependent):
            raise ValueError("Recursive reference levels do not match native geometry")

    def _validate_retained_routes(self, manifest):
        if "retained_routes" in manifest:
            raise ValueError("Unexpected recursive archive route overrides")

    def _has_reference_level(self, level):
        return level in self._layout.dependent

    def _allowed(self, descriptor):
        return {image.JXL, image.CONDITIONAL091} if self._has_reference_level(descriptor[1]) else {image.JXL}

    def _validate_parent_index(self, entries):
        count = len(entries)
        data = self._member("anchors.index", image.HEADER.size + image.AENTRY.size * count)
        if (len(data) != image.HEADER.size + image.AENTRY.size * count
                or image.HEADER.unpack_from(data) != (image.AMAGIC, 256, count)):
            raise ValueError("Recursive parent index header or coverage")
        self.anchor_index_read_bytes += len(data)
        stored = image.AENTRY.iter_unpack(memoryview(data)[image.HEADER.size:])
        for descriptor, entry, (parent_id,) in zip(self._descriptors, entries, stored, strict=True):
            expected = (self._layout.parent(descriptor)[0] if entry[2] == image.CONDITIONAL091 else image.NO_ANCHOR)
            if parent_id != expected:
                raise ValueError("Recursive parent index mismatch")

    def _validated_parent(self, route, blob, descriptor):
        self._layout.conditioned_header(blob, descriptor)
        return self._layout.parent(descriptor)

    def metrics(self):
        return {**super().metrics(), "codec_scope": "091 conditional native parent graph and direct checkpoints",
                "reference_depth_limit": self._depth_limit}


def write_archive(catalog, census, output, layout, record, runtime_pins, *, max_depth, preset):
    metadata = image._plain(census / "source.json")
    identity = catalog["files"]["source.json"]
    if metadata.stat().st_size != identity["bytes"] or image.sha256(metadata) != identity["sha256"]:
        raise ValueError("Frozen source metadata changed")
    output.mkdir()
    marker = output / "INCOMPLETE"
    marker.touch(exist_ok=False)
    descriptors = list(image.base._descriptors(catalog["levels"]))
    counts = {str(route): 0 for route in ROUTES}
    level_bytes = {str(level["level"]): {"bank_bytes": 0, "conditioned_tiles": 0}
                   for level in layout.levels}
    with ((output / "images.bank").open("xb") as bank,
          (output / "images.index").open("xb") as index,
          (output / "anchors.index").open("xb") as parents):
        index.write(image.HEADER.pack(image.MAGIC, 256, len(descriptors)))
        parents.write(image.HEADER.pack(image.AMAGIC, 256, len(descriptors)))
        for descriptor in descriptors:
            gid, level = descriptor[:2]
            route, blob = record(descriptor)
            if not 0 < len(blob) <= image._limit(route):
                raise ValueError("Record byte bound")
            parent = image.NO_ANCHOR
            if route == image.CONDITIONAL091:
                view = layout.conditioned_header(blob, descriptor)
                if view["raw_sha256"] != catalog["tiles"][gid]["native_sha256"]:
                    raise ValueError("Conditional record source hash mismatch")
                parent, _, _ = layout.parent(descriptor)
                level_bytes[str(level)]["conditioned_tiles"] += 1
            elif route != image.JXL:
                raise ValueError("Unknown recursive route")
            index.write(image.ENTRY.pack(bank.tell(), len(blob), route, zlib.crc32(blob)))
            parents.write(image.AENTRY.pack(parent))
            bank.write(blob)
            counts[str(route)] += 1
            level_bytes[str(level)]["bank_bytes"] += len(blob)
    shutil.copyfile(metadata, output / "source.json")
    if image.sha256(output / "source.json") != identity["sha256"]:
        raise ValueError("Copied source metadata changed")
    members = {name: {"bytes": (output / name).stat().st_size, "sha256": image.sha256(output / name)}
               for name in sorted(image.MEMBERS)}
    manifest = {"format": FORMAT, "profile": preset, "image_only": True,
                "max_reference_depth": max_depth, "referenced_levels": sorted(layout.dependent),
                "image": {"path": "images.bank", "index": "images.index", "anchor_index": "anchors.index",
                          "source_metadata": "source.json", "levels": catalog["levels"]},
                "routes": counts, "files": members, "build": {"native_runtimes": runtime_pins}}
    write_json(output / "manifest.json", manifest)
    marker.unlink()
    return level_bytes
