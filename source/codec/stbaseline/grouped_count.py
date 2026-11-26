"""Independent Count frames indexed by spatial block and gene group."""
import bz2
import io
from pathlib import Path
import struct
import zlib

import numpy as np
from numba import njit
from scipy.sparse import csr_matrix, vstack
import zstandard as zstd

from .count_support_sets import MAX_SET_ROWS, PROFILE_NAME, PROFILE_SHAPE, corrected_tables, decode_sets
from .reader import MeteredFile
from .shared_backend import MODEL_NAMES, model_tables

HEADER = struct.Struct("<4sIIII")
BLOCK = struct.Struct("<iiIIII")
FRAME = struct.Struct("<HIIII")
MODEL_HEADER = struct.Struct("<7I")
BLOCK_ORDER = b"SGR1"
GROUP_ORDER_MAGIC = b"SGL1"
GROUP_PROFILE_MAGIC = b"SGL2"
SUPPORT_PROFILE_MAGIC = b"SGS2"
SET_FLAG = 1 << 31
MACRO_BLOCKS = 32
LOWER = 1 << 23
SCALE = 4096
READ_GAP = 128
MAX_WINDOW = 256 * 1024
MAX_GRAPH_CACHE = 1024 * 1024
MAX_RESTORE_CELLS = 1_048_576
_EMPTY_POSITION_TABLES = np.empty((0, 0, 0), dtype=np.uint16)


def decode_zstd(data, maximum):
    """Only accept an exact single frame with a bounded declared size."""
    if not 0 < zstd.frame_content_size(data) <= maximum:
        raise ValueError("Invalid Count Zstandard frame size")
    return zstd.ZstdDecompressor().decompress(data, allow_extra_data=False)


def group_order(blocks):
    """Derive physical order from the logical directory, with no offset table."""
    for first in range(0, len(blocks), MACRO_BLOCKS):
        end = min(first + MACRO_BLOCKS, len(blocks))
        for bid in range(first, end):
            yield bid, None
        pairs = [(group, bid) for bid in range(first, end) for group in blocks[bid][2]]
        for group, bid in sorted(pairs):
            yield bid, group


@njit(cache=True, inline="always")
def value_context(row, total):
    if row < 6:
        return 7
    if total == 0:
        return 0
    if total < 12:
        return 1
    if total < 24:
        return 2
    if total < 48:
        return 3
    if total < 96:
        return 4
    return 5 if total < 384 else 6


@njit(cache=True)
def _state(blob):
    if len(blob) < 4:
        raise ValueError("Truncated Count frame header")
    state = np.uint64(0)
    for i in range(4):
        state |= np.uint64(blob[i]) << np.uint64(8 * i)
    if state < LOWER:
        raise ValueError("Invalid Count rANS state")
    return state


@njit(cache=True, inline="always")
def _take(blob, pos, state, start, freq):
    state = np.uint64(freq) * (state >> np.uint64(12)) + (state & np.uint64(4095)) - np.uint64(start)
    while state < LOWER:
        if pos >= len(blob):
            raise ValueError("Truncated Count frame")
        state = (state << np.uint64(8)) | np.uint64(blob[pos])
        pos += 1
    return pos, state


@njit(cache=True)
def _frame(a, b, graph, width, start, nnz, support, groups, binary, tail, cdf, support_masks=None):
    # A group's dense scratch keeps causal lookup constant-time. Every entry is
    # reconstructed once, and only the requested rows/genes are returned.
    dense = np.zeros((len(graph), width), np.uint32)
    sa = _state(a) if support_masks is None else np.uint64(LOWER)
    sb, pa, pb, positives = _state(b), 4 if support_masks is None else len(a), 4, 0
    for row in range(len(graph)):
        has_six = row >= 6
        for local in range(width):
            col = start + local
            k, total = 0, np.int64(0)
            if has_six:
                for j in range(6):
                    x = dense[graph[row, j], local]
                    if support_masks is None:
                        k += x != 0
                    total += np.int64(x)
            if support_masks is None:
                p0 = SCALE - int(support[col, 42 + k if has_six else 0])
                present = int(sa & np.uint64(4095)) >= p0
                pa, sa = _take(a, pa, sa, p0 if present else 0, SCALE - p0 if present else p0)
            else:
                present = bool((support_masks[local] >> row) & 1)
            if not present:
                continue
            positives += 1
            ctx = value_context(row, total)
            p0 = SCALE - int(binary[col, ctx])
            one = int(sb & np.uint64(4095)) >= p0
            pb, sb = _take(b, pb, sb, p0 if one else 0, SCALE - p0 if one else p0)
            if one:
                dense[row, local] = 1
                continue
            group, bit = groups[col], 0
            symbol = int(sb & np.uint64(4095))
            while bit < 31 and symbol >= cdf[group, ctx, bit + 1]:
                bit += 1
            pb, sb = _take(b, pb, sb, cdf[group, ctx, bit], tail[group, ctx, bit])
            remainder = np.uint64(0)
            for position in range(bit - 1, -1, -1):
                p1 = 2048
                p0 = SCALE - p1
                high = (sb & np.uint64(4095)) >= p0
                pb, sb = _take(b, pb, sb, p0 if high else 0, p1 if high else p0)
                remainder = (remainder << np.uint64(1)) | np.uint64(high)
            value = (np.uint64(1) << np.uint64(bit)) + remainder + np.uint64(1)
            if value > 4294967295:
                raise ValueError("Unused uint32 Count codeword")
            dense[row, local] = np.uint32(value)
    if pa != len(a) or pb != len(b) or sa != LOWER or sb != LOWER or positives != nnz:
        raise ValueError("Count rANS termination or support count mismatch")
    return dense


@njit(cache=True)
def _decode_jobs(buffer, jobs, graphs, row_pairs, gene_pairs, output, full_axis, support, groups, binary, tail, cdf,
                 support_set_tables):
    for job in jobs:
        offset, a, b, graph_at, nr, start, width, row_lo, row_hi, gene_lo, gene_hi, nnz, is_set = job
        positions, values = buffer[offset:offset+a], buffer[offset+a:offset+a+b]
        graph = graphs[graph_at:graph_at+nr]
        if is_set:
            masks = decode_sets(positions, nr, support[start:start + width, 0], support_set_tables)
            dense = _frame(positions, values, graph, width, start, nnz, support, groups, binary, tail,
                           cdf, masks)
        else:
            dense = _frame(positions, values, graph, width, start, nnz, support, groups, binary, tail,
                           cdf)
        for r in range(row_lo, row_hi):
            target_row, local_row = row_pairs[r]
            if full_axis:
                output[target_row, start:start+width] = dense[local_row]
            else:
                for g in range(gene_lo, gene_hi):
                    target_gene, local_gene = gene_pairs[g]
                    output[target_row, target_gene] = dense[local_row, local_gene]


def _checked_graph(blob, count):
    with bz2.BZ2File(io.BytesIO(blob)) as stream:
        raw = stream.read(24 * count + 1)
    graph = np.frombuffer(raw, "<i4").reshape(count, 6).copy()
    for i, row in enumerate(graph):
        used = min(i, 6)
        if any(not 0 <= v < i for v in row[:used]) or len(set(row[:used])) != used or np.any(row[used:] != -1):
            raise ValueError("Invalid causal graph")
    return graph


class GroupedCountStore:
    """Read the frozen group-aware Count layout without opening the image."""

    def __init__(self, root, *, expected_shape):
        self.root = Path(root)
        packed = (self.root / "counts.index.zst").read_bytes()
        self.index_bytes = len(packed)
        n, g = expected_shape
        maximum = HEADER.size + n * (BLOCK.size + 4 + ((g + 63) // 64) * FRAME.size)
        raw = decode_zstd(packed, maximum)
        magic, n, g, width, blocks = HEADER.unpack_from(raw)
        if (magic not in {BLOCK_ORDER, GROUP_ORDER_MAGIC, SUPPORT_PROFILE_MAGIC, GROUP_PROFILE_MAGIC}
                or (n, g) != tuple(expected_shape) or not 1 <= blocks <= n
                or width != (64 if g <= 1024 else 256)):
            raise ValueError("Invalid Count directory header")
        self.group_ordered = magic in (GROUP_ORDER_MAGIC, GROUP_PROFILE_MAGIC)
        self.support_profile = magic in (SUPPORT_PROFILE_MAGIC, GROUP_PROFILE_MAGIC)
        if self.group_ordered != (width == 64) or self.support_profile != (self.root / PROFILE_NAME).is_file():
            raise ValueError("Count layout or occupancy model disagrees with its directory")
        self.shape = n, g
        self.width = width
        self.set_frames = set()
        self.groups = (g + width - 1) // width
        self.model = None
        self.blocks, self.row_map = [], {}
        cursor, offset = HEADER.size, 0
        for bid in range(blocks):
            _, _, nr, nf, graph_size, graph_crc = BLOCK.unpack_from(raw, cursor)
            cursor += BLOCK.size
            if not 1 <= nr <= n or nf > self.groups or not graph_size:
                raise ValueError("Invalid group block")
            rows = np.frombuffer(raw, "<u4", count=nr, offset=cursor).tolist()
            cursor += 4 * nr
            graph = (offset, graph_size, graph_crc)
            offset += graph_size
            frames = {}
            for _ in range(nf):
                group, nnz, first, second, crc = FRAME.unpack_from(raw, cursor)
                cursor += FRAME.size
                if first & SET_FLAG:
                    if not self.support_profile or nr > MAX_SET_ROWS:
                        raise ValueError("Unexpected Count support-set frame")
                    self.set_frames.add((bid, group))
                    first ^= SET_FLAG
                width_here = min(width, g - group * width)
                if (group in frames or group >= self.groups or not 1 <= nnz <= nr * width_here
                        or min(first, second) < 4):
                    raise ValueError("Invalid Count frame directory entry")
                frames[group] = (offset, first, second, crc, nnz)
                offset += first + second
            for local, row in enumerate(rows):
                if row >= n or row in self.row_map:
                    raise ValueError("Invalid group-store row partition")
                self.row_map[row] = (bid, local)
            self.blocks.append((rows, graph, frames))
        if self.group_ordered:
            offset = 0
            for bid, group in group_order(self.blocks):
                rows, graph, frames = self.blocks[bid]
                if group is None:
                    self.blocks[bid] = (rows, (offset, *graph[1:]), frames)
                    offset += graph[1]
                else:
                    desc = frames[group]
                    frames[group] = (offset, *desc[1:])
                    offset += desc[1] + desc[2]
        path = self.root / "counts.bank"
        if cursor != len(raw) or len(self.row_map) != n or offset != path.stat().st_size:
            raise ValueError("Count directory and bank disagree")
        self.file = MeteredFile(path)
        self._position_tables = None
        self._graph_cache = {}
        self._graph_cache_bytes = 0
        self.decoded_rows = 0
        self.decoded_blocks = 0

    def _model(self):
        if self.model is None:
            raw = (self.root / "counts.model").read_bytes()
            lengths = MODEL_HEADER.unpack_from(raw)
            if not all(lengths) or sum(lengths) + MODEL_HEADER.size != len(raw):
                raise ValueError("Count model member lengths disagree")
            parts, start = {}, MODEL_HEADER.size
            for name, length in zip(MODEL_NAMES, lengths):
                parts[name] = raw[start:start + length]
                start += length
            self.model = model_tables(parts, support_builder="fused", value_builder="fused")
        return self.model

    def _support_set_tables(self):
        if not self.support_profile:
            return _EMPTY_POSITION_TABLES
        if self._position_tables is None:
            raw = decode_zstd((self.root / PROFILE_NAME).read_bytes(), int(np.prod(PROFILE_SHAPE)) * 2)
            profile = np.frombuffer(raw, "<u2").reshape(PROFILE_SHAPE)
            if np.any(profile < SCALE // 16) or np.any(profile > SCALE * 15):
                raise ValueError("Invalid Count occupancy multiplier")
            self._position_tables = corrected_tables(profile)
        return self._position_tables

    def _batch_members(self, requests):
        """Return checked member views. Bytes covered by merged gaps are metered."""
        members = {}
        cursor = 0
        while cursor < len(requests):
            start, size, _ = requests[cursor]
            end, stop = start + size, cursor + 1
            while stop < len(requests):
                offset, length, _ = requests[stop]
                if offset > end + READ_GAP or offset + length - start > MAX_WINDOW:
                    break
                end = offset + length
                stop += 1
            self.file.seek(start)
            region = self.file.read(end - start)
            if len(region) != end - start:
                raise ValueError("Truncated Count bank")
            view = memoryview(region)
            for offset, length, crc in requests[cursor:stop]:
                member = view[offset-start:offset-start+length]
                if zlib.crc32(member) != crc:
                    raise ValueError("Count member checksum mismatch")
                members[offset] = member
            cursor = stop
        return members

    def _graph_for(self, bid, rows, desc, members):
        if bid in self._graph_cache:
            return self._graph_cache[bid]
        graph = _checked_graph(members[desc[0]], len(rows))
        if self._graph_cache_bytes + graph.nbytes <= MAX_GRAPH_CACHE:
            self._graph_cache[bid] = graph
            self._graph_cache_bytes += graph.nbytes
        return graph

    def read(self, rows, genes):
        output = np.zeros((len(rows), len(genes)), dtype=np.uint64)
        if not len(rows) or not len(genes):
            return output
        full_axis = len(genes) == self.shape[1] and np.array_equal(genes, np.arange(self.shape[1]))
        selected_rows, selected_genes = {}, {}
        for target, row in enumerate(rows):
            bid, local = self.row_map[int(row)]
            selected_rows.setdefault(bid, []).append((target, local))
        if not full_axis:
            for target, gene in enumerate(genes):
                group, local = divmod(int(gene), self.width)
                selected_genes.setdefault(group, []).append((target, local))
        gene_pairs, gene_ranges = [], {}
        for group, pairs in selected_genes.items():
            gene_ranges[group] = (len(gene_pairs), len(gene_pairs) + len(pairs))
            gene_pairs.extend(pairs)

        requests, plan = [], []
        for bid, pairs in sorted(selected_rows.items()):
            stored, graph, frames = self.blocks[bid]
            hits = list(frames) if full_axis else sorted(frames.keys() & gene_ranges.keys())
            if not hits:
                continue
            if bid not in self._graph_cache:
                requests.append(graph)
            for group in hits:
                offset, a, b, crc, _ = frames[group]
                requests.append((offset, a + b, crc))
            plan.append((bid, stored, graph, frames, hits, pairs))
        if not plan:
            return output
        members = self._batch_members(sorted(requests))
        buffer, jobs, graphs, row_pairs, graph_at = bytearray(), [], [], [], 0
        for bid, stored, graph, frames, hits, pairs in plan:
            graphs.append(self._graph_for(bid, stored, graph, members))
            row_lo = len(row_pairs)
            row_pairs.extend(pairs)
            for group in hits:
                offset, a, b, _, nnz = frames[group]
                start = group * self.width
                width = min(self.shape[1] - start, self.width)
                gene_lo, gene_hi = (0, 0) if full_axis else gene_ranges[group]
                jobs.append((len(buffer), a, b, graph_at, len(stored), start, width, row_lo,
                             len(row_pairs), gene_lo, gene_hi, nnz, (bid, group) in self.set_frames))
                buffer.extend(members[offset])
            graph_at += len(stored)
        _decode_jobs(np.frombuffer(buffer, np.uint8), np.asarray(jobs, np.int64),
                     np.concatenate(graphs), np.asarray(row_pairs, np.int64),
                     np.asarray(gene_pairs, np.int64).reshape(-1, 2), output, full_axis, *self._model(),
                     self._support_set_tables())
        self.decoded_blocks += len(plan)
        self.decoded_rows += graph_at
        return output

    def sparse_all(self, *, max_cells=20_000_000):
        if self.shape[0] * self.shape[1] > max_cells:
            raise ValueError("Sparse assembly exceeds the qualified cell bound")
        genes = np.arange(self.shape[1])
        batches, order, pending = [], [], []
        limit = max(1, MAX_RESTORE_CELLS // len(genes))
        for rows, _, _ in self.blocks:
            if pending and len(pending) + len(rows) > limit:
                batches.append(csr_matrix(self.read(pending, genes)))
                order.extend(pending)
                pending = []
            pending.extend(rows)
        if pending:
            batches.append(csr_matrix(self.read(pending, genes)))
            order.extend(pending)
        return vstack(batches, format="csr")[np.argsort(order)].tocsr()

    def execution_metrics(self):
        return {"model_initializations": int(self.model is not None),
                "retained_model_array_bytes": sum(array.nbytes for array in self.model or ())
                    + (0 if self._position_tables is None else self._position_tables.nbytes),
                "decoded_count_cache_bytes": 0,
                "cached_graph_bytes": self._graph_cache_bytes,
                "group_width": self.width,
                "group_ordered": self.group_ordered,
                "table_backend": "exact-fused-support-and-values"}

    def close(self):
        self.file.close()
