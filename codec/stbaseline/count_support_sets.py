"""Small-block support as a shared count distribution followed by a subset rank."""
from functools import lru_cache
from math import comb

import numpy as np
from numba import njit

MAX_SET_ROWS = 12
TOTAL = 4096
LOWER = 1 << 23
PROFILE_EDGES = (2, 4, 8, 16, 32, 64, 128, 256, 512, 768, 1024, 1536, 2048, 2560,
                 3072, 3584, 3840, 3968, 4032)
PROFILE_SHAPE = (MAX_SET_ROWS + 1, len(PROFILE_EDGES) + 1, MAX_SET_ROWS + 1)
PROFILE_NAME = "counts.occupancy.zst"
BINOM = np.asarray([[comb(n, k) if k <= n else 0 for k in range(MAX_SET_ROWS + 1)]
                    for n in range(MAX_SET_ROWS + 1)], dtype=np.int64)


@njit(cache=True, inline="always")
def _put(state, start, freq, out, pos):
    f = np.uint64(freq)
    while state >= np.uint64((LOWER >> 12) << 8) * f:
        out[pos] = np.uint8(state & np.uint64(255))
        pos += 1
        state >>= np.uint64(8)
    return ((state // f) << np.uint64(12)) + (state % f) + np.uint64(start), pos


@njit(cache=True, inline="always")
def tail_class(value):
    k = 0
    while value > 1:
        value >>= 1
        k += 1
    return k


@lru_cache(maxsize=1)
def set_tables():
    """Deterministic Q12 binomial CDFs derived from the already paid gene priors."""
    q = np.arange(TOTAL, dtype=np.uint64)[:, None]
    mass = np.full((TOTAL, 1), 1 << 32, np.uint64)
    table = np.zeros((MAX_SET_ROWS + 1, TOTAL, MAX_SET_ROWS + 2), np.uint16)
    for n in range(1, MAX_SET_ROWS + 1):
        new = np.zeros((TOTAL, n + 1), np.uint64)
        new[:, :-1] += mass * (TOTAL - q)
        new[:, 1:] += mass * q
        mass = (new + TOTAL // 2) // TOTAL
        cumul = np.cumsum(mass, axis=1)
        weights = cumul[:, -1:]
        table[n, :, 1:n + 2] = (
            (cumul * (TOTAL - n - 1) + weights // 2) // weights + np.arange(1, n + 2, dtype=np.uint64)
        )
    return table


def corrected_tables(profile):
    q = np.arange(TOTAL)
    buckets = np.searchsorted(PROFILE_EDGES, q, side="right")
    table = set_tables().copy()
    for n in range(1, MAX_SET_ROWS + 1):
        frequency = np.diff(table[n, :, :n + 2], axis=1).astype(np.uint64)
        cumul = np.cumsum(frequency * profile[n, buckets, :n + 1], axis=1)
        weights = cumul[:, -1:]
        table[n, :, 1:n + 2] = (
            (cumul * (TOTAL - n - 1) + weights // 2) // weights + np.arange(1, n + 2, dtype=np.uint64)
        )
    return table


def fit_profile(matrix, blocks, width, priors):
    """Pool observed-to-expected occupancy ratios within shared density buckets."""
    buckets = np.searchsorted(PROFILE_EDGES, priors, side="right")
    expected = np.zeros(PROFILE_SHAPE, np.int64)
    observed = np.zeros(PROFILE_SHAPE, np.int64)
    table = set_tables()
    by_n = {}
    for rows, _, frames in blocks:
        n = len(rows)
        if n > MAX_SET_ROWS or not frames:
            continue
        cols = np.concatenate([np.arange(group * width, min((group + 1) * width, matrix.shape[1]))
                               for group in frames])
        frequency = by_n.setdefault(n, np.zeros(matrix.shape[1], np.int64))
        frequency[cols] += 1
        counts = np.bincount(matrix[rows].indices, minlength=matrix.shape[1])
        np.add.at(observed[n], (buckets[cols], counts[cols]), 1)
    for n, times in by_n.items():
        count_frequency = np.diff(table[n, priors, :n + 2], axis=1)
        np.add.at(expected[n, :, :n + 1], buckets, times[:, None] * count_frequency)
    # Add eight observations of shrinkage per cell and clamp the Q12 correction.
    smooth = 8 * TOTAL
    denominator = expected + smooth
    ratio = ((observed * TOTAL + smooth) * TOTAL + denominator // 2) // denominator
    return np.clip(ratio, TOTAL // 16, TOTAL * 15).astype("<u2")


@njit(cache=True)
def encode_sets(bits, priors, cdfs):
    n, width = len(bits), len(priors)
    out = np.empty(width * 4 + 4, np.uint8)
    pos, state = 0, np.uint64(LOWER)
    for g in range(width - 1, -1, -1):
        count, rank = 0, 0
        for row in range(n):
            if (bits[row, g >> 3] >> (g & 7)) & 1:
                count += 1
                rank += BINOM[row, count]
        options = BINOM[n, count]
        length = tail_class(options)
        cutoff = (1 << (length + 1)) - options
        if rank >= cutoff:
            rank += cutoff
            length += 1
        for bit in range(length):
            state, pos = _put(state, 2048 if (rank >> bit) & 1 else 0, 2048, out, pos)
        cdf = cdfs[n, priors[g]]
        state, pos = _put(state, cdf[count], int(cdf[count + 1]) - int(cdf[count]), out, pos)
    result = np.empty(pos + 4, np.uint8)
    for i in range(4):
        result[i] = np.uint8((state >> np.uint64(8 * i)) & np.uint64(255))
    for i in range(pos):
        result[4 + i] = out[pos - i - 1]
    return result


@njit(cache=True, inline="always")
def _take(blob, pos, state, start, freq):
    state = np.uint64(freq) * (state >> np.uint64(12)) + (state & np.uint64(4095)) - np.uint64(start)
    while state < LOWER:
        if pos >= len(blob):
            raise ValueError("Truncated Count support set")
        state = (state << np.uint64(8)) | np.uint64(blob[pos])
        pos += 1
    return pos, state


@njit(cache=True)
def decode_sets(blob, n, priors, cdfs):
    if len(blob) < 4 or not 1 <= n <= MAX_SET_ROWS:
        raise ValueError("Invalid Count support set")
    state = np.uint64(0)
    for i in range(4):
        state |= np.uint64(blob[i]) << np.uint64(8 * i)
    if state < LOWER:
        raise ValueError("Invalid support set rANS state")
    pos = 4
    masks = np.zeros(len(priors), np.uint16)
    for g in range(len(priors)):
        cdf = cdfs[n, priors[g]]
        symbol, count = int(state & np.uint64(4095)), 0
        while count < n and symbol >= cdf[count + 1]:
            count += 1
        pos, state = _take(blob, pos, state, cdf[count], int(cdf[count + 1]) - int(cdf[count]))
        options = BINOM[n, count]
        length = tail_class(options)
        cutoff = (1 << (length + 1)) - options
        rank = 0
        for _ in range(length):
            high = (state & np.uint64(4095)) >= 2048
            pos, state = _take(blob, pos, state, 2048 if high else 0, 2048)
            rank = (rank << 1) | int(high)
        if rank >= cutoff:
            high = (state & np.uint64(4095)) >= 2048
            pos, state = _take(blob, pos, state, 2048 if high else 0, 2048)
            rank = ((rank << 1) | int(high)) - cutoff
        for row in range(n - 1, -1, -1):
            if count and rank >= BINOM[row, count]:
                rank -= BINOM[row, count]
                masks[g] |= np.uint16(1 << row)
                count -= 1
        if rank or count:
            raise ValueError("Count support set rank mismatch")
    if pos != len(blob) or state != LOWER:
        raise ValueError("Count support set rANS termination mismatch")
    return masks
