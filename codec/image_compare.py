"""Frozen independent256 lossless image baselines; no implicit codec fallback.

Only build() reads the qualified census/template. Readers use their five local
members and installed codec libraries. Existing bounded native preview/stripe
restore code is reused; no old experiment or production source is modified.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import stat
import struct
import time

import numpy as np

from stbaseline import slide_redundancy as base
from stbaseline import slide_redundancy_scan as scan
from stbaseline.integrity import sha256, write_json
from stbaseline.reader import MeteredFile

FORMAT = 'wsi-baselines-20261101-v1'
PROFILE_NAMES = (
    'JXL10_independent256', 'JPEG2000_reversible_MCT',
    'JPEG_LS_NEAR0_NONE', 'JPEG_LS_NEAR0_HP1', 'PNG9',
    'WebP_lossless_method6_quality100', 'HTJ2K_reversible',
)
MEMBERS = {'images.bank', 'images.index', 'anchors.index', 'source.json'}
HEADER = struct.Struct('<8sII')
# Codec-specific routes; SHA256 binds every separately addressable payload.
ENTRY = struct.Struct('<QIB3x32s')
MAGIC, AMAGIC = b'WSICMP01', b'WSICMA01'
TILE = 256
MAX_PAYLOAD = 2 * 1024**2
MAX_BANK = 4 * 1024**3
FIXED = {
    'tile': 256, 'threads': 1, 'training': False,
    'native_levels': 6, 'independent_tiles': True,
    'aliases': False, 'constant_omission': False,
    'max_roi_pixels': base.MAX_ROI_PIXELS,
    'per_query_cache_bytes': base.MAX_QUERY_CACHE_BYTES,
    'persistent_decoded_pixel_cache': False,
}
_CODECS = {
    PROFILE_NAMES[0]: ('JPEGXL', 'jpegxl', {'lossless': True, 'distance': 0,
        'effort': 10, 'numthreads': 1, 'usecontainer': False,
        'bitspersample': 8, 'planar': False, 'decodingspeed': 0,
        'photometric': 'RGB'}),
    PROFILE_NAMES[1]: ('JPEG2K', 'jpeg2k', {'level': 0, 'reversible': True,
        'mct': True, 'planar': False, 'bitspersample': 8,
        'codecformat': 'J2K', 'colorspace': 'SRGB', 'numthreads': 1}),
    PROFILE_NAMES[2]: ('JPEGLS', 'jpegls', {'level': 0,
        'transformation': 'NONE', 'bitspersample': 8, 'spiff': False}),
    PROFILE_NAMES[3]: ('JPEGLS', 'jpegls', {'level': 0,
        'transformation': 'HP1', 'bitspersample': 8, 'spiff': False}),
    PROFILE_NAMES[4]: ('PNG', 'png', {'level': 9, 'strategy': 'DEFAULT', 'filter': 'ALL'}),
    PROFILE_NAMES[5]: ('WEBP', 'webp', {'lossless': True, 'level': 100,
        'method': 6, 'numthreads': 1}),
    PROFILE_NAMES[6]: ('HTJ2K', 'htj2k', {'level': 0.0, 'reversible': True,
        'rgb': True, 'planar': False, 'bitspersample': 8,
        'block_size': (64, 64), 'prog_order': 'LRCP', 'resolutions': 5}),
}


class CodecUnavailable(RuntimeError):
    """Requested fixed profile cannot run; caller must record its rejection."""


def _plain(path, *, directory=False):
    p = Path(path).absolute()
    for item in (p, *p.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise ValueError('Symlink/reparse path forbidden')
    if directory and not p.is_dir():
        raise ValueError('Expected ordinary directory')
    return p.resolve()


def _json(blob):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('Duplicate JSON key')
            out[key] = value
        return out
    def invalid(value):
        raise ValueError('Nonfinite JSON value')
    value = json.loads(blob, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError('JSON object required')
    return value


def _read(path, limit):
    p = _plain(path)
    if not p.is_file() or not 0 <= p.stat().st_size <= limit:
        raise ValueError('Missing/unbounded member')
    return p.read_bytes()


def _typed_equal(left, right):
    return type(left) is type(right) and (
        left.keys() == right.keys() and all(_typed_equal(left[k], right[k]) for k in left)
        if isinstance(left, dict) else
        len(left) == len(right) and all(_typed_equal(a, b) for a, b in zip(left, right))
        if isinstance(left, (list, tuple)) else left == right)


def _parameters(profile):
    if profile not in PROFILE_NAMES:
        raise ValueError('Unknown fixed codec profile')
    options = copy.deepcopy(_CODECS[profile][2])
    if 'block_size' in options:
        options['block_size'] = list(options['block_size'])
    if profile == PROFILE_NAMES[1]:
        options['resolutions_rule'] = 'min(6, floor(log2(min(h,w)))+1)'
    if profile == PROFILE_NAMES[6]:
        options['decompositions_rule'] = 'Installed wrapper clamps requested5 to floor(log2(min(h,w)))'
    return {**FIXED, 'codec_options': options}


def _codec(profile):
    if profile not in PROFILE_NAMES:
        raise ValueError('Unknown fixed codec profile')
    import imagecodecs
    symbol, prefix, _ = _CODECS[profile]
    try:
        if not getattr(imagecodecs, symbol).available:
            raise CodecUnavailable('Unavailable fixed profile: ' + profile)
        encode = getattr(imagecodecs, prefix + '_encode')
        decode = getattr(imagecodecs, prefix + '_decode')
        version = getattr(imagecodecs, prefix + '_version')()
    except (AttributeError, ImportError) as exc:
        raise CodecUnavailable('Unavailable fixed profile: ' + profile) from exc
    return imagecodecs, encode, decode, version


def capability_receipt(profile):
    """Inspect requested runtime/options only; never encode or decode pixels."""
    ic, encode, decode, version = _codec(profile)
    return {'profile': profile, 'available': True,
            'parameters': _parameters(profile), 'imagecodecs': ic.__version__,
            'codec_library': version, 'encoder': encode.__name__, 'decoder': decode.__name__,
            'unspecified_options': 'Pinned installed library defaults; no parameter search',
            'adapter_sha256': sha256(Path(__file__)),
            'qualified_exact': False}


def encode_tile(profile, pixels):
    if (not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8
            or pixels.ndim != 3 or pixels.shape[2] != 3
            or not 1 <= min(pixels.shape[:2]) <= max(pixels.shape[:2]) <= TILE):
        raise ValueError('Bounded RGB8 tile required')
    _, encode, _, _ = _codec(profile)
    options = copy.deepcopy(_CODECS[profile][2])
    depth = min(pixels.shape[:2]).bit_length() - 1
    if profile == PROFILE_NAMES[1]:
        options['resolutions'] = min(6, depth + 1)
    # Every requested option is passed once. Errors are never downgraded.
    blob = bytes(encode(np.ascontiguousarray(pixels), **options))
    if not 0 < len(blob) <= MAX_PAYLOAD:
        raise ValueError('Codec payload exceeds fixed tile bound')
    return blob


def decode_tile(profile, blob, shape):
    if (not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_PAYLOAD
            or len(shape) != 3 or shape[2] != 3
            or any(type(v) is not int for v in shape)
            or not 1 <= min(shape[:2]) <= max(shape[:2]) <= TILE):
        raise ValueError('Bounded tile payload/shape required')
    _, _, decode, _ = _codec(profile)
    options = {'out': np.empty(shape, dtype=np.uint8)}
    if profile in (PROFILE_NAMES[0], PROFILE_NAMES[1]):
        options['numthreads'] = 1
    if profile in (PROFILE_NAMES[1], PROFILE_NAMES[6]):
        options['planar'] = False
    if profile == PROFILE_NAMES[6]:
        options['resilient'] = False
    pixels = decode(blob, **options)
    if pixels.dtype != np.uint8 or pixels.shape != tuple(shape):
        raise ValueError('Decoded RGB8 shape mismatch')
    return pixels


def _qualification_cases():
    rng = np.random.default_rng(20261101)
    cases = []
    for h, w in ((1, 1), (1, 17), (19, 1), (3, 5), (17, 19)):
        pixels = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
        pixels[0, 0] = (0, 255, 128)
        cases.append((f'edge_{h}x{w}', pixels))
    for name, color in (('black', (0, 0, 0)), ('white', (255, 255, 255)),
                        ('flat_color', (1, 128, 254))):
        pixels = np.empty((29, 31, 3), np.uint8)
        pixels[:] = color
        cases.append((name, pixels))
    yy, xx = np.indices((256, 256), dtype=np.uint16)
    cases.append(('gradient256', np.stack((xx, yy, (xx+yy)%256), axis=2).astype(np.uint8)))
    cases.append(('random31x29', rng.integers(0, 256, (31, 29, 3), dtype=np.uint8)))
    return cases


def qualify(output):
    """Create bounded synthetic exactness evidence; no real source/census reads.

    Only absent capability is an eligible=False rejection that may continue.
    Native codec errors or unequal decoded pixels are recorded qualification
    failures and raise after retaining the receipts. No silent fallback/skip.
    """
    out = _plain(output)
    if out.exists():
        raise FileExistsError(out)
    out.mkdir(parents=True)
    marker = out/'INCOMPLETE'
    marker.write_text('Fixed codec qualification\n', encoding='utf8')
    profiles, failures = {}, []
    try:
        cases = _qualification_cases()
        for profile in PROFILE_NAMES:
            record = {'eligible': False, 'parameters': _parameters(profile), 'cases': []}
            profiles[profile] = record
            try:
                record['capability'] = capability_receipt(profile)
            except CodecUnavailable as exc:
                record.update(status='unavailable', error_type=type(exc).__name__, error=str(exc))
                continue
            folder = out/profile
            folder.mkdir()
            try:
                for name, pixels in cases:
                    then = time.perf_counter()
                    blob = encode_tile(profile, pixels)
                    encode_seconds = time.perf_counter()-then
                    path = folder/(name+'.bin')
                    path.write_bytes(blob)
                    then = time.perf_counter()
                    decoded = decode_tile(profile, blob, list(pixels.shape))
                    decode_seconds = time.perf_counter()-then
                    exact = np.array_equal(decoded, pixels)
                    row = {'case': name, 'shape': list(pixels.shape), 'dtype': 'uint8',
                           'file': path.relative_to(out).as_posix(), 'bytes': len(blob),
                           'sha256': hashlib.sha256(blob).hexdigest(),
                           'input_sha256': hashlib.sha256(pixels.tobytes()).hexdigest(),
                           'decoded_sha256': hashlib.sha256(decoded.tobytes()).hexdigest(),
                           'exact': bool(exact), 'encode_seconds': encode_seconds,
                           'decode_seconds': decode_seconds}
                    record['cases'].append(row)
                    if not exact:
                        raise ValueError('Synthetic native RGB mismatch: '+name)
                record.update(eligible=True, status='exact_synthetic_qualified')
                record['capability']['qualified_exact'] = True
            except Exception as exc:
                record.update(status='qualification_error', error_type=type(exc).__name__, error=str(exc))
                failures.append(profile)
        result = {'ok': not failures, 'schema': 'image-comparison-qualification-20261101-v1',
                  'profiles': profiles, 'failed_profiles': failures,
                  'scope': 'Synthetic installed codecs only; real whole-slide qualification separate'}
        write_json(out/'qualification.json', result)
        if failures:
            raise RuntimeError('Codec qualification errors (not unsupported): '+', '.join(failures))
        marker.unlink()
        return result
    except BaseException as exc:
        write_json(out/'failure.json', {'type': type(exc).__name__, 'error': str(exc)})
        raise


def _source_template(directory, catalog, census):
    root = _plain(directory, directory=True)
    manifest = _json(_read(root / 'manifest.json', base.MAX_MANIFEST_BYTES))
    blob = _read(root / 'source.json', scan.MAX_SOURCE_METADATA_BYTES)
    identity = manifest.get('files', {}).get('source.json')
    expected = {'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
    if (not _typed_equal(identity, expected)
            or expected != catalog['files']['source.json']
            or blob != _read(census / 'source.json', scan.MAX_SOURCE_METADATA_BYTES)):
        raise ValueError('Template/census source metadata mismatch')
    return blob


def build(census, output, profile, *, source_template):
    """Encode all logical tiles once and create one complete five-member package.

    Returned timings are disjoint phases where named; total includes all work.
    No timestamps, absolute census paths or timings enter deterministic members.
    """
    started = time.perf_counter()
    _parameters(profile)
    ic, _, _, version = _codec(profile)
    census = _plain(census, directory=True)
    for name in ('catalog.json', 'tiles.rgb', 'source.json'):
        _plain(census / name)
    template = _plain(source_template, directory=True)
    out = _plain(output)
    for source in (census, template):
        if out == source or source in out.parents or out in source.parents:
            raise ValueError('Output overlaps a source')
    if out.exists():
        raise FileExistsError(out)
    catalog_sha = sha256(census / 'catalog.json')
    catalog = base._catalog(census)
    if len(catalog['levels']) != 6:
        raise ValueError('Exactly six stored native levels required')
    source_blob = _source_template(template, catalog, census)
    input_seconds = time.perf_counter() - started
    times = dict(input_verification_seconds=input_seconds, tile_input_seconds=0.0,
                 codec_encode_seconds=0.0, immediate_decode_check_seconds=0.0,
                 serialization_seconds=0.0, hash_seconds=0.0)
    out.mkdir(parents=True)
    marker = out / 'INCOMPLETE'
    marker.write_text('Frozen independent codec build\n', encoding='utf8')
    try:
        records = []
        route = PROFILE_NAMES.index(profile) + 1
        with (out / 'images.bank').open('xb') as bank:
            for tile in catalog['tiles']:
                then = time.perf_counter()
                pixels = scan.read_tile(census, tile['gid'], catalog=catalog)
                times['tile_input_seconds'] += time.perf_counter() - then
                then = time.perf_counter()
                blob = encode_tile(profile, pixels)
                times['codec_encode_seconds'] += time.perf_counter() - then
                then = time.perf_counter()
                restored = decode_tile(profile, blob, list(pixels.shape))
                if not np.array_equal(restored, pixels):
                    raise ValueError('Fixed codec does not restore exact native RGB')
                times['immediate_decode_check_seconds'] += time.perf_counter() - then
                then = time.perf_counter()
                digest = hashlib.sha256(blob).digest()
                times['hash_seconds'] += time.perf_counter() - then
                then = time.perf_counter()
                records.append((bank.tell(), len(blob), route, digest))
                if bank.tell() + len(blob) > MAX_BANK:
                    raise ValueError('Bank exceeds fixed bound')
                bank.write(blob)
                times['serialization_seconds'] += time.perf_counter() - then
        then = time.perf_counter()
        with (out / 'images.index').open('xb') as index:
            index.write(HEADER.pack(MAGIC, TILE, len(records)))
            for row in records:
                index.write(ENTRY.pack(*row))
        (out / 'anchors.index').write_bytes(HEADER.pack(AMAGIC, TILE, 0))
        (out / 'source.json').write_bytes(source_blob)
        times['serialization_seconds'] += time.perf_counter() - then
        then = time.perf_counter()
        if (sha256(census / 'catalog.json') != catalog_sha
                or sha256(census / 'tiles.rgb') != catalog['files']['tiles.rgb']['sha256']
                or _source_template(template, catalog, census) != source_blob):
            raise ValueError('Verified build input changed')
        files = {name: {'bytes': (out / name).stat().st_size,
                        'sha256': sha256(out / name)} for name in sorted(MEMBERS)}
        times['hash_seconds'] += time.perf_counter() - then
        manifest = {
            'format': FORMAT, 'profile': profile, 'image_only': True,
            'parameters': _parameters(profile),
            'image': {'path': 'images.bank', 'index': 'images.index',
                      'anchor_index': 'anchors.index', 'source_metadata': 'source.json',
                      'levels': copy.deepcopy(catalog['levels'])},
            'build': {'census_catalog_sha256': catalog_sha,
                      'source_whole_sha256': _json(source_blob)['whole_file_sha256'],
                      'adapter_sha256': sha256(Path(__file__)),
                      'imagecodecs': ic.__version__, 'codec_library': version},
            'files': files,
            'exactness': 'Stored native RGB and source metadata; not original TIFF container bytes',
        }
        write_json(out / 'manifest.json', manifest)
        marker.unlink()
        then = time.perf_counter()
        with Reader(out) as reader:
            verified = reader.verify(decode=True)
            ledger = reader.ledger()
        times['package_verification_seconds'] = time.perf_counter() - then
        times['total_seconds'] = time.perf_counter() - started
        return {'ok': True, 'profile': profile, 'ledger': ledger,
                'capability': capability_receipt(profile),
                'verification': verified, 'timings': times,
                'encoded_tiles': len(records), 'payload_bytes': files['images.bank']['bytes'],
                'members': {**files, 'manifest.json': {'bytes': (out/'manifest.json').stat().st_size,
                                                     'sha256': sha256(out/'manifest.json')}}}
    except BaseException as exc:
        write_json(out / 'failure.json', {'type': type(exc).__name__, 'error': str(exc)})
        raise


class Reader(base.Reader):
    def __init__(self, directory):
        self._bank = None
        self._closed = False
        self.root = _plain(directory, directory=True)
        if {p.name for p in self.root.iterdir()} != MEMBERS | {'manifest.json'}:
            raise ValueError('Unexpected/missing/incomplete package member')
        blob = _read(self.root / 'manifest.json', base.MAX_MANIFEST_BYTES)
        self.manifest = m = _json(blob)
        self.manifest_bytes = len(blob)
        self._manifest_sha256 = hashlib.sha256(blob).hexdigest()
        if (set(m) != {'format', 'profile', 'image_only', 'parameters', 'image',
                       'build', 'files', 'exactness'} or m.get('format') != FORMAT
                or m.get('image_only') is not True
                or not _typed_equal(m.get('parameters'), _parameters(m.get('profile')))):
            raise ValueError('Unsupported fixed image profile')
        self.profile = m['profile']
        self._route = PROFILE_NAMES.index(self.profile) + 1
        if (set(m['image']) != {'path', 'index', 'anchor_index', 'source_metadata', 'levels'}
                or {k: m['image'][k] for k in ('path', 'index', 'anchor_index', 'source_metadata')}
                != dict(path='images.bank', index='images.index', anchor_index='anchors.index', source_metadata='source.json')
                or len(m['image']['levels']) != 6 or set(m['files']) != MEMBERS):
            raise ValueError('Native coverage/member paths differ')
        self._descriptors = list(base._descriptors(m['image']['levels']))
        self._level_start = [next(d[0] for d in self._descriptors if d[1] == i) for i in range(6)]
        for name, item in m['files'].items():
            path = _plain(self.root / name)
            if (not path.is_file() or set(item) != {'bytes', 'sha256'}
                    or type(item['bytes']) is not int or item['bytes'] < 0
                    or path.stat().st_size != item['bytes']
                    or not isinstance(item['sha256'], str)
                    or base.HEX.fullmatch(item['sha256']) is None):
                raise ValueError('Member identity/size differs')
        if (not 0 < m['files']['images.bank']['bytes'] <= MAX_BANK
                or m['files']['images.index']['bytes'] != HEADER.size + len(self._descriptors)*ENTRY.size
                or m['files']['anchors.index']['bytes'] != HEADER.size
                or m['files']['source.json']['bytes'] > scan.MAX_SOURCE_METADATA_BYTES):
            raise ValueError('Member byte bound/coverage differs')
        self._entries = self._source_metadata = None
        self.index_read_bytes = self.anchor_index_read_bytes = self.source_metadata_read_bytes = 0
        self.jxl_tiles_decoded = self.constant_tiles_filled = self.logical_tiles_read = 0
        self.decoded_pixels = self.requested_pixels = self.cache_hits = 0
        self.last_query_cache_bytes = self.last_query_unique_payloads = 0
        self.codec_tiles_decoded = 0
        self.source_metadata()

    @property
    def levels(self):
        return copy.deepcopy(self.manifest['image']['levels'])

    def _member(self, name, limit):
        blob = _read(self.root / name, limit)
        item = self.manifest['files'][name]
        if len(blob) != item['bytes'] or hashlib.sha256(blob).hexdigest() != item['sha256']:
            raise ValueError('Member SHA256 mismatch')
        return blob

    def _ensure_open(self):
        if self._closed:
            raise ValueError('Closed image reader')

    def source_metadata(self):
        self._ensure_open()
        if self._source_metadata is None:
            blob = self._member('source.json', scan.MAX_SOURCE_METADATA_BYTES)
            source = _json(blob)
            self.source_metadata_read_bytes += len(blob)
            whole = source.get('whole_file_sha256')
            originals, metadata = source.get('native_levels'), source.get('native_metadata')
            if (source.get('schema') != scan.SOURCE_SCHEMA
                    or not isinstance(whole, str) or base.HEX.fullmatch(whole) is None
                    or whole != source.get('identity', {}).get('whole_file_sha256')
                    or whole != self.manifest['build'].get('source_whole_sha256')
                    or not isinstance(originals, list) or len(originals) != 6
                    or not isinstance(metadata, list) or len(metadata) != 6):
                raise ValueError('Original source identity differs')
            for i, (current, native, tags) in enumerate(zip(self.levels, originals, metadata)):
                if (not _typed_equal({k:v for k,v in current.items() if k != 'pixel_sha256'}, native)
                        or type(tags.get('level')) is not int or tags['level'] != i
                        or type(tags.get('page_index')) is not int or tags['page_index'] != i
                        or not isinstance(tags.get('tags'), dict)
                        or not isinstance(tags.get('restore'), dict)):
                    raise ValueError('Original native level metadata differs')
            self._source_metadata = source
        return self._source_metadata

    def _setup(self):
        self._ensure_open()
        if self._entries is not None:
            return
        n = len(self._descriptors)
        data = self._member('images.index', HEADER.size + n*ENTRY.size)
        anchors = self._member('anchors.index', HEADER.size)
        if (HEADER.unpack_from(data) != (MAGIC, TILE, n)
                or HEADER.unpack_from(anchors) != (AMAGIC, TILE, 0)):
            raise ValueError('Index header/coverage mismatch')
        self.index_read_bytes += len(data)
        self.anchor_index_read_bytes += len(anchors)
        entries = []
        end = 0
        for gid in range(n):
            pos = HEADER.size + gid*ENTRY.size
            offset, length, route, digest = ENTRY.unpack_from(data, pos)
            if (offset != end or not 0 < length <= MAX_PAYLOAD or route != self._route
                    or data[pos+13:pos+16] != b'\0'*3):
                raise ValueError('Invalid codec/span/reserved index field')
            end += length
            entries.append((offset, length, route, digest))
        if end != self.manifest['files']['images.bank']['bytes']:
            raise ValueError('Bank gap/overlap/trailing data')
        self._entries = entries
        self._bank = MeteredFile(_plain(self.root/'images.bank'))

    def _tile(self, gid, cache=None, cache_state=None):
        self._setup()
        if not base._integer(gid) or not 0 <= gid < len(self._entries):
            raise ValueError('Tile identity out of range')
        offset, length, _, digest = self._entries[gid]
        self._bank.seek(offset)
        blob = self._bank.read(length)
        if len(blob) != length or hashlib.sha256(blob).digest() != digest:
            raise ValueError('Tile payload SHA256 mismatch')
        _, _, _, _, w, h = self._descriptors[gid]
        pixels = decode_tile(self.profile, blob, [h, w, 3])
        self.codec_tiles_decoded += 1
        self.jxl_tiles_decoded += int(self.profile == PROFILE_NAMES[0])
        self.logical_tiles_read += 1
        self.decoded_pixels += h*w
        if cache_state is not None:
            cache_state['decoded'].add(gid)
        return pixels

    def image(self, level, bbox):
        self._ensure_open()
        return super().image(level, bbox)

    def _verify_files(self):
        self._ensure_open()
        if {p.name for p in self.root.iterdir()} != MEMBERS | {'manifest.json'}:
            raise ValueError('Package member set changed')
        for name, item in self.manifest['files'].items():
            p = _plain(self.root/name)
            if p.stat().st_size != item['bytes'] or sha256(p) != item['sha256']:
                raise ValueError('Package member SHA256 mismatch')
        if hashlib.sha256(_read(self.root/'manifest.json', base.MAX_MANIFEST_BYTES)).hexdigest() != self._manifest_sha256:
            raise ValueError('Manifest changed while open')

    def verify(self, *, decode=False):
        if type(decode) is not bool:
            raise ValueError('Explicit boolean decode policy required')
        self._verify_files()
        self._setup()
        self.source_metadata()
        if decode:
            return {**super().verify(), 'decoded': True}
        return {'ok': True, 'decoded': False, 'levels': 6, 'ledger': self.ledger(),
                'source_metadata_retained': True}

    def restore(self, destination):
        self._ensure_open()
        _plain(destination)
        return super().restore(destination)

    def metrics(self):
        result = super().metrics()
        result.update(codec=self.profile, codec_tiles_decoded=self.codec_tiles_decoded,
                      anchor_index_read_bytes=self.anchor_index_read_bytes,
                      source_metadata_read_bytes=self.source_metadata_read_bytes,
                      reference_tiles_decoded=0, reference_decoded_pixels=0,
                      max_observed_dependency_depth=0,
                      cache_scope='No decoded pixel cache; each touched independent tile decoded once per query')
        return result

    def close(self):
        if not self._closed:
            super().close()
            self._closed = True
