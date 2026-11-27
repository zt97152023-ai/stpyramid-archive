"""Exact CPU execution alternative for the pinned Shared base support table."""
import numpy as np
from numba import njit


@njit(cache=True)
def _fused(q0, groups, multipliers):
    out = np.empty((len(q0), 49), dtype=np.uint16)
    for gene in range(len(q0)):
        q = np.uint64(q0[gene])
        for context in range(42):
            out[gene, context] = q
        for k in range(7):
            numerator = q * np.uint64(multipliers[groups[gene], k])
            denominator = numerator + (np.uint64(4096) - q) * np.uint64(65536)
            probability = (np.uint64(4096) * numerator + denominator // np.uint64(2)) // denominator
            if probability < 1:
                probability = np.uint64(1)
            elif probability > 4095:
                probability = np.uint64(4095)
            out[gene, 42 + k] = probability
    return out


def fused_support_cdf(q0, multipliers, pinned_model):
    """Reuse upstream guards/buckets; only replace its table arithmetic execution.

    Bounded legal odds keep every intermediate below2**52, hence uint64 is exact.
    No fitted parameter, cross-query cache or serialized lookup is introduced.
    """
    groups = pinned_model.buckets(q0)
    if (multipliers.shape != (8, 7) or multipliers.dtype.kind != 'u'
            or np.any(multipliers < pinned_model.RMIN) or np.any(multipliers > pinned_model.RMAX)):
        raise ValueError('multiplier bounds/shape')
    return _fused(np.asarray(q0, dtype='<u2'), groups, multipliers)
