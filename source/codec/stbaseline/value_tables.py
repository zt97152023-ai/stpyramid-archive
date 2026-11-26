"""Exact CPU construction alternative for the frozen serialized K32 value model."""
import math
import numpy as np
from numba import njit

GROUPS = 32
RMAX = int(math.floor(math.exp(8) * 65536 + .5))


@njit(cache=True)
def _fused(q, groups, base_freq, odds, conditional_freq, has_odds):
    binary = np.empty((len(q), 8), dtype=np.uint16)
    tail = np.empty((GROUPS, 8, 32), dtype=np.uint16)
    cumulative = np.empty((GROUPS, 8, 33), dtype=np.uint32)
    for gene in range(len(q)):
        prior = np.uint64(q[gene])
        binary[gene, 7] = prior
        for k in range(7):
            probability = prior
            if has_odds:
                num = prior * np.uint64(odds[groups[gene], k])
                den = num + (np.uint64(4096) - prior) * np.uint64(65536)
                probability = (np.uint64(4096) * num + den // np.uint64(2)) // den
                if probability < 1:
                    probability = np.uint64(1)
                elif probability > 4095:
                    probability = np.uint64(4095)
            binary[gene, k] = probability
    for group in range(GROUPS):
        for context in range(8):
            cumulative[group, context, 0] = 0
            total = np.uint32(0)
            for symbol in range(32):
                frequency = base_freq[group, symbol]
                if has_odds and context < 7:
                    frequency = conditional_freq[group, context, symbol]
                tail[group, context, symbol] = frequency
                total += np.uint32(frequency)
                cumulative[group, context, symbol + 1] = total
    return binary, tail, cumulative


def _frequencies(array, shape, message):
    if (array is None or array.shape != shape or array.dtype != np.uint16
            or np.any(array < 1) or np.any(array.sum(axis=-1, dtype=np.uint32) != 4096)):
        raise ValueError(message)


def fused_value_tables(q, groups, base_freq, odds=None, conditional_freq=None):
    """Use the adapter's serialized integer domains; reject unsafe indices pre-kernel.

    K32 and uint16 frequency records are fixed by this adapter's format. This is
    not a replacement for arbitrary upstream fitting APIs or other K variants.
    """
    if (q.ndim != 1 or groups.shape != q.shape or q.dtype.kind not in 'ui'
            or groups.dtype.kind not in 'ui' or np.any(q < 1) or np.any(q > 4095)
            or np.any(groups < 0) or np.any(groups >= GROUPS)):
        raise ValueError('gene model')
    _frequencies(base_freq, (GROUPS, 32), 'base tail frequencies/shape')
    has_odds = odds is not None
    if has_odds:
        if (odds.shape != (GROUPS, 7) or odds.dtype.kind not in 'ui'
                or np.any(odds < 22) or np.any(odds > RMAX)):
            raise ValueError('odds')
        _frequencies(conditional_freq, (GROUPS, 7, 32), 'conditional frequencies/shape')
        odds = np.asarray(odds, dtype='<u4')
    else:
        odds = np.empty((0, 0), dtype='<u4')
        conditional_freq = np.empty((0, 0, 0), dtype='<u2')
    return _fused(np.asarray(q, dtype='<u2'), np.asarray(groups, dtype=np.uint8),
                  base_freq, odds, conditional_freq, has_odds)
