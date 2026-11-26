"""087: exact RGB prediction with paid, shared Q12 conditional distributions.

Frozen predictors: planes G,R,B, in raster order within each plane. For each
channel, subtract the exact supplied anchor at the left/north/northwest points
(missing neighbors have residual zero), apply MED, add the current anchor and
clip to0..255. R and B additionally add the already decoded current G minus its
clipped G prediction, then clip again. An absent anchor is identically zero.

Context = 8*(final prediction//64) + gradient bin. Gradient is the maximum of
the three pairwise absolute differences of the neighboring residuals. Its bin
counts thresholds in [0,1,2,4,8,16,32] strictly below that gradient. No context
uses an unknown current channel or future pixel. The symbol is (X-P) modulo256.

Every tile restarts byte-rANS at2**23. Exact integer Q12 normalization reserves
one unit only for observed symbols, uses largest remainders (symbol-order ties)
and gives empty contexts a single zero symbol. Frequencies are paid codec state,
not a neural model. The caller pays geometry, anchor references and stream CRC
in its archive index; rANS termination is not a substitute for that CRC.

No global decoded caches, neural training, color normalization or quantization.
NUMBA_CACHE_DIR is controlled by the task launcher, outside protected inputs.
"""
from dataclasses import dataclass
import struct
import zlib

import numpy as np
from numba import njit

TILE = 256
PLANES = 3
CONTEXTS = 32
SYMBOLS = 256
BITS = 12
TOTAL = 1 << BITS
LOWER = 1 << 23
SHAPE = (PLANES, CONTEXTS, SYMBOLS)
MODEL_RAW_BYTES = PLANES * CONTEXTS * SYMBOLS * 2
MODEL_HEADER = struct.Struct('<8sIIII')
MODEL_MAGIC = b'WCTX0871'
MAX_MODEL_COMPRESSED_BYTES = 2 * MODEL_RAW_BYTES
MAX_U64 = (1 << 64) - 1


def _rgb(value):
    a = np.asarray(value)
    if (a.dtype != np.uint8 or a.ndim != 3 or a.shape[2] != 3
            or not 1 <= a.shape[0] <= TILE or not 1 <= a.shape[1] <= TILE):
        raise ValueError('Expected a nonempty RGB8 tile no larger than256x256')
    return np.ascontiguousarray(a)


def _anchor(value, pixels):
    if value is None:
        return np.zeros_like(pixels)
    a = _rgb(value)
    if a.shape != pixels.shape:
        raise ValueError('Anchor must have exactly the target tile shape')
    return a


@njit(cache=True, inline='always')
def _clip(value):
    return min(255, max(0, value))


@njit(cache=True, inline='always')
def _med(left, north, northwest):
    return min(max(left, north), max(min(left, north), left + north - northwest))


@njit(cache=True, inline='always')
def _prediction(pixels, anchor, g_prediction, plane, y, x):
    channel = 1 if plane == 0 else (0 if plane == 1 else 2)
    left = (np.int64(pixels[y, x-1, channel]) - np.int64(anchor[y, x-1, channel])) if x else 0
    north = (np.int64(pixels[y-1, x, channel]) - np.int64(anchor[y-1, x, channel])) if y else 0
    northwest = (np.int64(pixels[y-1, x-1, channel]) - np.int64(anchor[y-1, x-1, channel])) if x and y else 0
    pred = _clip(np.int64(anchor[y, x, channel]) + _med(left, north, northwest))
    if plane:
        pred = _clip(pred + np.int64(pixels[y, x, 1]) - np.int64(g_prediction[y, x]))
    gradient = max(abs(left-north), abs(left-northwest), abs(north-northwest))
    group = 0
    for threshold in (0, 1, 2, 4, 8, 16, 32):
        if gradient > threshold:
            group += 1
    return pred, (pred >> 6)*8 + group


@njit(cache=True)
def _extract(pixels, anchor):
    h, w, _ = pixels.shape
    symbols = np.empty((3, h, w), np.uint8)
    contexts = np.empty((3, h, w), np.uint8)
    g_prediction = np.empty((h, w), np.uint8)
    for plane in range(3):
        channel = 1 if plane == 0 else (0 if plane == 1 else 2)
        for y in range(h):
            for x in range(w):
                pred, context = _prediction(pixels, anchor, g_prediction, plane, y, x)
                if plane == 0:
                    g_prediction[y, x] = np.uint8(pred)
                symbols[plane, y, x] = np.uint8((np.int64(pixels[y, x, channel])-pred) & 255)
                contexts[plane, y, x] = np.uint8(context)
    return symbols, contexts


def extract(pixels, anchor=None):
    """Return uint8 symbols and context IDs in G,R,B plane/raster order."""
    a = _rgb(pixels)
    return _extract(a, _anchor(anchor, a))


def collect(pixels, anchor=None):
    """Count every native symbol once, using only decoder-available contexts."""
    symbols, contexts = extract(pixels, anchor)
    counts = np.empty(SHAPE, np.uint64)
    for plane in range(3):
        keys = contexts[plane].astype(np.int64)*256 + symbols[plane]
        counts[plane] = np.bincount(keys.ravel(), minlength=CONTEXTS*SYMBOLS).reshape(CONTEXTS, SYMBOLS)
    return counts


def normalize(counts):
    """Support-only deterministic integer largest-remainder Q12 allocation."""
    values = np.asarray(counts)
    if values.shape != SHAPE or values.dtype.kind not in 'iu' or np.any(values < 0):
        raise ValueError('Counts must be nonnegative integer3x32x256')
    result = np.zeros(SHAPE, np.uint16)
    for plane in range(3):
        for context in range(CONTEXTS):
            weights = [int(v) for v in values[plane, context]]
            mass = sum(weights)
            if mass > MAX_U64:
                raise ValueError('Context count total exceeds uint64')
            support = [i for i, value in enumerate(weights) if value]
            if not support:
                result[plane, context, 0] = TOTAL
                continue
            budget = TOTAL - len(support)
            parts = {i: divmod(budget*weights[i], mass) for i in support}
            left = TOTAL - sum(1 + parts[i][0] for i in support)
            if not 0 <= left < len(support):
                raise ValueError('Invalid normalization remainder')
            winners = set(sorted(support, key=lambda i: (-parts[i][1], i))[:left])
            for i in support:
                result[plane, context, i] = 1 + parts[i][0] + int(i in winners)
    return result


def _frequencies(value):
    a = np.asarray(value)
    if (a.shape != SHAPE or a.dtype.kind not in 'iu'
            or np.any(a < 0) or np.any(a > TOTAL)):
        raise ValueError('Frequencies must be Q12 integer3x32x256')
    if not np.all(a.sum(axis=2, dtype=np.uint64) == TOTAL):
        raise ValueError('Every context frequency sum must be4096')
    return np.ascontiguousarray(a, dtype=np.uint32)


def dumps(frequencies):
    """Paid canonical uint16 tables, compressed with fixed Zstd9, with bounds/CRC."""
    import imagecodecs
    raw = _frequencies(frequencies).astype('<u2').tobytes()
    compressed = imagecodecs.zstd_encode(raw, level=9)
    if not 0 < len(compressed) <= MAX_MODEL_COMPRESSED_BYTES:
        raise ValueError('Compressed model exceeds fixed limit')
    return MODEL_HEADER.pack(MODEL_MAGIC, len(raw), len(compressed), zlib.crc32(raw),
                             zlib.crc32(compressed)) + compressed


def loads(blob):
    import imagecodecs
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError('Model must be bytes')
    if not MODEL_HEADER.size < len(blob) <= MODEL_HEADER.size + MAX_MODEL_COMPRESSED_BYTES:
        raise ValueError('Model length outside fixed limits')
    magic, raw_size, stored_size, raw_crc, stored_crc = MODEL_HEADER.unpack_from(blob)
    if magic != MODEL_MAGIC or raw_size != MODEL_RAW_BYTES or stored_size != len(blob)-MODEL_HEADER.size:
        raise ValueError('Invalid model header or declared length')
    payload = bytes(blob[MODEL_HEADER.size:])
    if zlib.crc32(payload) != stored_crc:
        raise ValueError('Model payload CRC mismatch')
    try:
        raw = imagecodecs.zstd_decode(payload, out=MODEL_RAW_BYTES)
    except Exception as exc:
        raise ValueError('Invalid bounded model Zstd payload') from exc
    if len(raw) != MODEL_RAW_BYTES or zlib.crc32(raw) != raw_crc:
        raise ValueError('Model decoded size/CRC mismatch')
    return _frequencies(np.frombuffer(raw, '<u2').reshape(SHAPE)).astype(np.uint16)


def _cdf(frequencies):
    result = np.zeros((3, CONTEXTS, SYMBOLS+1), np.uint32)
    np.cumsum(frequencies, axis=2, dtype=np.uint32, out=result[:, :, 1:])
    return result


@dataclass(frozen=True)
class DecoderTables:
    """Validated internal reader-owned state, constructed only by prepare()."""
    frequencies: np.ndarray
    cdf: np.ndarray
    lookup: np.ndarray

    @property
    def nbytes(self):
        return self.frequencies.nbytes + self.cdf.nbytes + self.lookup.nbytes


def prepare(frequencies):
    """Prepare one bounded lookup set per reader; no cross-reader global cache."""
    f = _frequencies(frequencies).copy()
    cdf = _cdf(f)
    lookup = np.empty((3, CONTEXTS, TOTAL), np.uint8)
    alphabet = np.arange(256, dtype=np.uint8)
    for plane in range(3):
        for context in range(CONTEXTS):
            lookup[plane, context] = np.repeat(alphabet, f[plane, context].astype(np.int64))
    for a in (f, cdf, lookup):
        a.flags.writeable = False
    return DecoderTables(f, cdf, lookup)


@njit(cache=True)
def _encode_symbols(symbols, contexts, frequencies, cdf):
    _, h, w = symbols.shape
    out = np.empty(symbols.size*2, np.uint8)
    used = 0
    state = np.uint64(LOWER)
    for plane in range(2, -1, -1):
        for y in range(h-1, -1, -1):
            for x in range(w-1, -1, -1):
                symbol = int(symbols[plane, y, x])
                context = int(contexts[plane, y, x])
                freq = np.uint64(frequencies[plane, context, symbol])
                if freq == 0:
                    raise ValueError('Symbol absent from supplied context model')
                start = np.uint64(cdf[plane, context, symbol])
                threshold = np.uint64((LOWER >> BITS) << 8)*freq
                while state >= threshold:
                    out[used] = np.uint8(state & np.uint64(255))
                    used += 1
                    state >>= np.uint64(8)
                state = ((state // freq) << np.uint64(BITS)) + state % freq + start
    result = np.empty(used+4, np.uint8)
    for i in range(4):
        result[i] = np.uint8((state >> np.uint64(i*8)) & np.uint64(255))
    for i in range(used):
        result[4+i] = out[used-1-i]
    return result


@njit(cache=True)
def _decode_pixels(blob, h, w, frequencies, cdf, lookup, anchor):
    state = np.uint64(0)
    for i in range(4):
        state |= np.uint64(blob[i]) << np.uint64(8*i)
    if state < LOWER or state >= np.uint64(LOWER) << np.uint64(8):
        raise ValueError('Invalid initial context rANS state')
    pixels = np.zeros((h, w, 3), np.uint8)
    g_prediction = np.empty((h, w), np.uint8)
    pos = 4
    for plane in range(3):
        channel = 1 if plane == 0 else (0 if plane == 1 else 2)
        for y in range(h):
            for x in range(w):
                pred, context = _prediction(pixels, anchor, g_prediction, plane, y, x)
                if plane == 0:
                    g_prediction[y, x] = np.uint8(pred)
                rem = int(state & np.uint64(TOTAL-1))
                symbol = np.int64(lookup[plane, context, rem])
                freq = np.uint64(frequencies[plane, context, symbol])
                start = np.uint64(cdf[plane, context, symbol])
                pixels[y, x, channel] = np.uint8((pred+symbol) & 255)
                state = freq*(state >> np.uint64(BITS)) + np.uint64(rem) - start
                while state < LOWER:
                    if pos >= len(blob):
                        raise ValueError('Truncated context rANS payload')
                    state = (state << np.uint64(8)) | np.uint64(blob[pos])
                    pos += 1
    if pos != len(blob) or state != LOWER:
        raise ValueError('Context rANS termination mismatch')
    return pixels


def encode(pixels, frequencies, anchor=None):
    """Encode an independent tile; caller pays model and anchor references."""
    a = _rgb(pixels)
    symbols, contexts = _extract(a, _anchor(anchor, a))
    if isinstance(frequencies, DecoderTables):
        f, cdf = frequencies.frequencies, frequencies.cdf
    else:
        f = _frequencies(frequencies)
        cdf = _cdf(f)
    return _encode_symbols(symbols, contexts, f, cdf).tobytes()


def decode(blob, height, width, prepared, anchor=None):
    """Decode RGB exactly from payload, paid model and optional exact anchor."""
    if any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer))
           or not 1 <= int(v) <= TILE for v in (height, width)):
        raise ValueError('Invalid independent256 tile geometry')
    h, w = int(height), int(width)
    if not isinstance(blob, (bytes, bytearray, memoryview)) or not 4 <= len(blob) <= 6*h*w+4:
        raise ValueError('Invalid context rANS payload length')
    state = prepared if isinstance(prepared, DecoderTables) else prepare(prepared)
    reference = _anchor(anchor, np.empty((h, w, 3), np.uint8))
    return _decode_pixels(np.frombuffer(blob, dtype=np.uint8), h, w,
                          state.frequencies, state.cdf, state.lookup, reference)
