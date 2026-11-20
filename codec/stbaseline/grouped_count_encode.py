"""Write the qualified grouped Count layout with one fit of the shared model."""
import bz2
from contextlib import ExitStack
import io
from pathlib import Path
import zlib

import imagecodecs
import numpy as np

from .count_support_sets import MAX_SET_ROWS, PROFILE_NAME, corrected_tables, encode_sets, fit_profile
from .grouped_count import (
    BLOCK, BLOCK_ORDER, FRAME, GROUP_ORDER_MAGIC, GROUP_PROFILE_MAGIC, HEADER,
    MACRO_BLOCKS, MODEL_HEADER, SET_FLAG, SUPPORT_PROFILE_MAGIC,
)
from .shared_backend import MODEL_NAMES, canonical, model_tables, native_parts, partition, runtime


def _write_frame_bank(bank, blocks, group_ordered):
    if group_ordered:
        for graph, _ in blocks:
            bank.write(graph)
        frames = [(group, block, data) for block, (_, members) in enumerate(blocks)
                  for group, data in members.items()]
        for _, _, data in sorted(frames):
            bank.write(data)
    else:
        for graph, members in blocks:
            bank.write(graph)
            for data in members.values():
                bank.write(data)


def write_grouped_candidates(matrix, metadata, output):
    """Stage normal and occupancy Count streams with the same values and layout."""
    n, g = matrix.shape
    width = 64 if g <= 1024 else 256
    if not 0 < (g + width - 1) // width <= 65536:
        raise ValueError("Count gene group index exceeds uint16")
    group_ordered = width == 64
    spatial = partition(metadata["coordinates"], 1024)
    rt, encode_model, graph_for = runtime()
    _, entropy, _, _, _, values = rt

    output = Path(output)
    plain = output / "plain"
    plain.mkdir()
    model_source = output / "whole.cnt"
    encode_model(canonical(matrix, metadata), model_source)
    _, parts = native_parts(model_source)
    model_source.unlink()
    model = MODEL_HEADER.pack(*(len(parts[name]) for name in MODEL_NAMES))
    (output / "counts.model").write_bytes(model + b"".join(parts[name] for name in MODEL_NAMES))
    support, groups, binary, tail, cdf = model_tables(parts, expected_genes=g)

    eligible = []
    for _, rows in spatial:
        if len(rows) <= MAX_SET_ROWS:
            present = np.unique(matrix[rows].indices // width)
            if len(present):
                eligible.append((rows, None, present.tolist()))
    table = None
    candidates = [plain]
    magics = [GROUP_ORDER_MAGIC if group_ordered else BLOCK_ORDER]
    if eligible:
        alternative = output / "occupancy"
        alternative.mkdir()
        profile = fit_profile(matrix, eligible, width, support[:, 0])
        table = corrected_tables(profile)
        (alternative / PROFILE_NAME).write_bytes(imagecodecs.zstd_encode(profile.tobytes(), level=3, numthreads=1))
        candidates.append(alternative)
        magics.append(GROUP_PROFILE_MAGIC if group_ordered else SUPPORT_PROFILE_MAGIC)

    indexes = [io.BytesIO(HEADER.pack(magic, n, g, width, len(spatial))) for magic in magics]
    for index in indexes:
        index.seek(0, io.SEEK_END)
    window = MACRO_BLOCKS if group_ordered else 1
    with ExitStack() as resources:
        banks = [resources.enter_context((folder / "counts.bank").open("xb")) for folder in candidates]
        for start in range(0, len(spatial), window):
            payloads = [[] for _ in candidates]
            for cell, rows in spatial[start:start + window]:
                block = matrix[rows].tocsc()
                graph = graph_for(metadata["coordinates"][rows])
                graph_bytes = bz2.compress(graph.astype("<i4").tobytes(), 9)
                entries = [{} for _ in candidates]
                frames = [bytearray() for _ in candidates]
                for group, first_col in enumerate(range(0, g, width)):
                    last_col = min(g, first_col + width)
                    panel = block[:, first_col:last_col].tocsr()
                    if not panel.nnz:
                        continue
                    sl = slice(first_col, last_col)
                    bits = entropy.packed_support(panel.indptr, panel.indices, *panel.shape)
                    encoded_positions, _ = entropy.encode(bits, graph, support[sl], panel.shape[1])
                    encoded_values, *_ = values.encode(panel.indptr, panel.indices, panel.data, graph,
                                                      groups[sl], binary[sl], tail, cdf, True)
                    positions, positives = encoded_positions.tobytes(), encoded_values.tobytes()
                    for i in range(len(candidates)):
                        selected, flag = positions, 0
                        if i and len(rows) <= MAX_SET_ROWS:
                            ranked = encode_sets(bits, support[sl, 0], table).tobytes()
                            if len(ranked) < len(positions):
                                selected, flag = ranked, SET_FLAG
                        data = selected + positives
                        frames[i].extend(FRAME.pack(group, panel.nnz, len(selected) | flag,
                                                   len(positives), zlib.crc32(data)))
                        entries[i][group] = data
                rows_bytes = np.asarray(rows, dtype="<u4").tobytes()
                graph_crc = zlib.crc32(graph_bytes)
                for index, directory, block_members, streams in zip(indexes, frames, payloads, entries):
                    index.write(BLOCK.pack(*cell, len(rows), len(streams), len(graph_bytes), graph_crc))
                    index.write(rows_bytes)
                    index.write(directory)
                    block_members.append((graph_bytes, streams))
            for bank, blocks in zip(banks, payloads):
                _write_frame_bank(bank, blocks, group_ordered)
    for folder, index in zip(candidates, indexes):
        (folder / "counts.index.zst").write_bytes(imagecodecs.zstd_encode(index.getbuffer(), level=3, numthreads=1))
    return candidates
