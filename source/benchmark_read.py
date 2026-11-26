"""Common reader measurements for the fixed whole-slide benchmark."""
import hashlib
from statistics import median
import time

from native_tile_encoder import RUNTIMES, archive, base


def roi_boxes(info):
    height, width, _ = info["shape"]
    edge = 256
    return [(x, y, x + edge, y + edge)
            for y in (round((height - edge) * fraction) for fraction in (0.125, 0.375, 0.625, 0.875))
            for x in (round((width - edge) * fraction) for fraction in (0.125, 0.375, 0.625, 0.875))]


def measure_rois(package, levels, runtimes=RUNTIMES):
    result = {}
    for level in (0, 1):
        boxes = roi_boxes(levels[level])
        durations = []
        expected = None
        with archive.Reader(package, *runtimes) as reader:
            for box in boxes:
                reader.image(level, box)
            before = reader.metrics()
            for _ in range(5):
                trial = []
                for box in boxes:
                    started = time.perf_counter()
                    rgb = reader.image(level, box)
                    durations.append(time.perf_counter() - started)
                    trial.append(hashlib.sha256(memoryview(rgb)).hexdigest())
                if expected is not None and trial != expected:
                    raise ValueError("ROI pixels differ between repeats")
                expected = trial
            after = reader.metrics()
        result[str(level)] = {"boxes": boxes, "roi_sha256": expected, "seconds": durations,
                              "median_seconds": median(durations),
                              "extra_reference_tiles": after["reference_tiles_decoded"] - before["reference_tiles_decoded"]}
    return result


def level_bytes(package, levels):
    index = (package / "images.index").read_bytes()
    result = {str(info["level"]): {"bank_bytes": 0, "routes": {}} for info in levels}
    for descriptor, (_, size, route, _) in zip(
        base._descriptors(levels), archive.ENTRY.iter_unpack(index[archive.HEADER.size:]), strict=True
    ):
        item = result[str(descriptor[1])]
        item["bank_bytes"] += size
        item["routes"][str(route)] = item["routes"].get(str(route), 0) + 1
    if sum(item["bank_bytes"] for item in result.values()) != (package / "images.bank").stat().st_size:
        raise ValueError("Per-level byte ledger differs from actual bank")
    return result
