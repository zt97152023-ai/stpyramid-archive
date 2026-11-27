"""087 whole-slide contextual RGB portfolio, with exact native pyramid anchors.

Only L0 changes between candidate recipes. Every lower native level remains
independent lossless JPEG XL9. Pure candidates and per-tile hybrids are actual
complete packages; the final selected package includes all model/index costs.
Build timings describe a shared-work portfolio, not seven standalone encoders.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import struct
import time
import zlib

import numpy as np

from . import slide_redundancy as base
from . import slide_redundancy_scan as scan
from . import wsi_context_codec as codec
from .integrity import sha256, write_json
from .reader import MeteredFile
from .wsi_shared import _disjoint, _name

FORMAT = 'stlossless-wsi-context-image-087-1'
MAGIC = b'STCTX087'
HEADER, ENTRY = base.HEADER, base.ENTRY
TILE = 256
JXL, LOCAL, SHARED, PYRAMID = 1, 2, 3, 4
MODEL = 'model.bin'
MODEL_MAX_BYTES = 98328
MAX_PAYLOAD = 2 * 1024 * 1024
MAX_QUERY_CACHE_BYTES = base.MAX_QUERY_CACHE_BYTES
MAX_TILES, MAX_ROI_PIXELS = base.MAX_TILES, base.MAX_ROI_PIXELS
PROFILES = ('direct', 'local_pure', 'shared_pure', 'pyramid_pure',
            'local_hybrid', 'shared_hybrid', 'pyramid_hybrid')
ROUTES = {'local': LOCAL, 'shared': SHARED, 'pyramid': PYRAMID}
BASE_MEMBERS = {'images.bank', 'images.index', 'source.json'}
PREDICTOR = 'clip(C+MED(causalX-C)); decodedG correction for R/B; clip again'


def _anchor_layout(levels, descriptor):
    gid, level, x, y, w, h = descriptor
    if level != 0 or len(levels) < 2:
        raise ValueError('Pyramid anchor requires L0 and an exact native L1')
    h0, w0, _ = levels[0]['shape']; h1, w1, _ = levels[1]['shape']
    xs = np.arange(x, x+w, dtype=np.int64) * w1 // w0
    ys = np.arange(y, y+h, dtype=np.int64) * h1 // h0
    cols = (w1 + TILE - 1) // TILE
    start = ((w0 + TILE - 1) // TILE) * ((h0 + TILE - 1) // TILE)
    entries = []
    for ty in np.unique(ys // TILE):
        yi = np.flatnonzero(ys // TILE == ty)
        for tx in np.unique(xs // TILE):
            xi = np.flatnonzero(xs // TILE == tx)
            entries.append((start + int(ty)*cols + int(tx), yi, xi, ys[yi] % TILE, xs[xi] % TILE))
    if not 1 <= len(entries) <= 4:
        raise ValueError('Pyramid reference count exceeds four independent L1 tiles')
    return entries


def _anchor(levels, descriptor, fetch):
    result = np.empty((descriptor[5], descriptor[4], 3), np.uint8)
    layout = _anchor_layout(levels, descriptor)
    for gid, yi, xi, ry, rx in layout:
        tile = fetch(gid)
        result[yi[:, None], xi[None, :]] = tile[ry[:, None], rx[None, :]]
    return result, len(layout)


def _catalog_anchor(root, catalog, descriptor):
    return _anchor(catalog['levels'], descriptor,
                   lambda gid: scan.read_tile(root, gid, catalog=catalog))


def _model_blob(frequencies):
    blob = codec.dumps(frequencies)
    if not 0 < len(blob) <= MODEL_MAX_BYTES:
        raise ValueError('Model exceeds fixed bounded serialization')
    return blob


def _payload_path(work, family, gid):
    return work / family / f'{gid:08d}.bin'


def _write_package(root, catalog, payloads, models, target, profile):
    import imagecodecs
    target.mkdir()
    source = root / 'source.json'; shutil.copyfile(source, target / 'source.json')
    if sha256(target / 'source.json') != catalog['files']['source.json']['sha256']:
        raise ValueError('Source metadata changed during package assembly')
    family = profile.split('_')[0]
    if family in ('shared', 'pyramid'):
        (target / MODEL).write_bytes(models[family])
    route_counts = {str(route): 0 for route in (JXL, LOCAL, SHARED, PYRAMID)}
    with (target / 'images.bank').open('xb') as bank, (target / 'images.index').open('xb') as index:
        index.write(HEADER.pack(MAGIC, TILE, len(catalog['tiles'])))
        for tile in catalog['tiles']:
            gid = tile['gid']; route = JXL
            direct = _payload_path(payloads, 'direct', gid)
            selected = direct
            if family != 'direct' and tile['level'] == 0:
                candidate = _payload_path(payloads, family, gid)
                if profile.endswith('_pure') or candidate.stat().st_size < direct.stat().st_size:
                    selected, route = candidate, ROUTES[family]
            blob = selected.read_bytes()
            if not 0 < len(blob) <= MAX_PAYLOAD:
                raise ValueError('Encoded tile exceeds bounded payload')
            index.write(ENTRY.pack(bank.tell(), len(blob), route, zlib.crc32(blob)))
            bank.write(blob); route_counts[str(route)] += 1
    manifest = {
        'format': FORMAT, 'profile': profile, 'image_only': True,
        'image': {'levels': copy.deepcopy(catalog['levels']), 'path': 'images.bank',
                  'index': 'images.index', 'source_metadata': 'source.json',
                  'model': MODEL if family in ('shared', 'pyramid') else None},
        'parameters': {'tile': TILE, 'jpegxl_effort': 9, 'threads': 1,
                       'context_bits': 12, 'contexts_per_plane': 32,
                       'context_planes': ['G', 'R', 'B'], 'candidate_level': 0,
                       'lower_levels': 'independent-JXL9', 'predictor': PREDICTOR,
                       'gradient_thresholds': [0, 1, 2, 4, 8, 16, 32],
                       'max_anchor_tiles': 4, 'reference_depth': 1,
                       'training': False, 'persistent_decoded_pixel_cache': False,
                       'per_query_cache_bytes': MAX_QUERY_CACHE_BYTES,
                       'max_tiles': MAX_TILES, 'max_roi_pixels': MAX_ROI_PIXELS},
        'build': {'task': 'STBASELINE-087', 'adapter_sha256': sha256(Path(__file__)),
                  'codec_sha256': sha256(Path(codec.__file__)),
                  'census_catalog_sha256': sha256(root / 'catalog.json'),
                  'jpegxl': imagecodecs.jpegxl_version(), 'route_counts': route_counts,
                  'logical_tiles': len(catalog['tiles']),
                  'capacity_control': 'Actual full packages share candidate encodes; not independent per-profile encoding timings'},
        'files': base._members(target),
        'exactness': 'All original native RGB levels/source metadata; not original TIFF container bytes',
    }
    write_json(target / 'manifest.json', manifest)
    return route_counts


def build(censusdir, output, workspace):
    """Create seven actual packages and a byte-identical whole-package winner.

    Source is an existing qualified086 census. No original TIFF, Count, old
    archives or biological labels are needed. All candidate streams are retained.
    """
    started = time.perf_counter(); root = Path(censusdir).resolve()
    out = _disjoint(output, root)
    stage = _disjoint(out.with_name(out.name + '.partial'), root)
    work = _disjoint(workspace, root, out, stage)
    stage.mkdir(parents=True)
    (stage / 'INCOMPLETE').write_text('087 whole-slide portfolio build\n', encoding='utf8')
    try:
        work.mkdir(parents=True)
        then = time.perf_counter(); catalog_pin = sha256(root / 'catalog.json')
        catalog = base._catalog(root)
        if len(catalog['levels']) < 2:
            raise ValueError('087 portfolio requires at least two exact native levels')
        descriptors = list(base._descriptors(catalog['levels']))
        catalog_seconds = time.perf_counter() - then
        payloads = work / 'payloads'; payloads.mkdir()
        for family in ('direct', 'local', 'shared', 'pyramid'):
            (payloads / family).mkdir()
        counts = {name: np.zeros((3, 32, 256), np.uint64) for name in ('shared', 'pyramid')}
        native_io_seconds = 0.0; max_references = 0
        then = time.perf_counter()
        for desc in descriptors:
            gid, level = desc[:2]
            if level != 0:
                continue
            read_start = time.perf_counter()
            pixels = scan.read_tile(root, gid, catalog=catalog)
            anchor, references = _catalog_anchor(root, catalog, desc)
            native_io_seconds += time.perf_counter() - read_start
            max_references = max(max_references, references)
            counts['shared'] += codec.collect(pixels)
            counts['pyramid'] += codec.collect(pixels, anchor=anchor)
            if gid % 64 == 0:
                print(json.dumps({'stage': '087_stats', 'gid': gid, 'tiles': len(descriptors)}), flush=True)
        statistics_seconds = time.perf_counter() - then
        then = time.perf_counter()
        frequencies = {name: codec.normalize(values) for name, values in counts.items()}
        models = {name: _model_blob(values) for name, values in frequencies.items()}
        prepared = {name: codec.prepare(values) for name, values in frequencies.items()}
        model_seconds = time.perf_counter() - then
        encode_seconds = {name: 0.0 for name in ('direct', 'local', 'shared', 'pyramid')}
        diagnostics = []
        for desc in descriptors:
            gid, level, x, y, w, h = desc
            read_start = time.perf_counter(); pixels = scan.read_tile(root, gid, catalog=catalog)
            anchor = None
            if level == 0:
                anchor, _ = _catalog_anchor(root, catalog, desc)
            native_io_seconds += time.perf_counter() - read_start
            then = time.perf_counter(); direct = base._encode(pixels)
            encode_seconds['direct'] += time.perf_counter() - then
            _payload_path(payloads, 'direct', gid).write_bytes(direct)
            sizes = {'direct': len(direct)}
            if level == 0:
                then = time.perf_counter()
                local_freq = codec.normalize(codec.collect(pixels)); local_model = _model_blob(local_freq)
                local_prepared = codec.prepare(local_freq)
                stream = codec.encode(pixels, local_prepared)
                np.testing.assert_array_equal(codec.decode(stream, h, w, local_prepared), pixels)
                local = struct.pack('<I', len(local_model)) + local_model + stream
                encode_seconds['local'] += time.perf_counter() - then
                _payload_path(payloads, 'local', gid).write_bytes(local); sizes['local'] = len(local)
                for family, condition in (('shared', None), ('pyramid', anchor)):
                    then = time.perf_counter()
                    stream = codec.encode(pixels, prepared[family], anchor=condition)
                    np.testing.assert_array_equal(codec.decode(stream, h, w, prepared[family], anchor=condition), pixels)
                    encode_seconds[family] += time.perf_counter() - then
                    _payload_path(payloads, family, gid).write_bytes(stream); sizes[family] = len(stream)
                near_white = np.all(pixels >= 240, axis=2)
                diagnostics.append({'gid': gid, 'native_bytes': pixels.nbytes,
                                    'near_white_pixels': int(near_white.sum()),
                                    'valid_pixels': w*h, 'payload_bytes': sizes})
            if gid % 64 == 0:
                print(json.dumps({'stage': '087_encode', 'gid': gid, 'tiles': len(descriptors)}), flush=True)
        results = {}
        for profile in PROFILES:
            then = time.perf_counter()
            routes = _write_package(root, catalog, payloads, models, stage / profile, profile)
            assembly_seconds = time.perf_counter() - then; then = time.perf_counter()
            with Reader(stage / profile) as reader:
                verified = reader.verify(); ledger = reader.ledger()
            results[profile] = {'verified': verified, 'ledger': ledger, 'route_counts': routes,
                                'assembly_seconds': assembly_seconds,
                                'verification_seconds': time.perf_counter()-then,
                                'members': base._members(stage / profile)}
            print(json.dumps({'stage': '087_verified', 'profile': profile,
                              'complete_bytes': ledger['total_package_bytes']}), flush=True)
        winner = min(PROFILES, key=lambda p: results[p]['ledger']['total_package_bytes'])
        shutil.copytree(stage / winner, stage / 'best')
        if base._members(stage / 'best') != results[winner]['members']:
            raise ValueError('Whole-package selection altered winning member bytes')
        if sha256(root / 'catalog.json') != catalog_pin:
            raise ValueError('Qualified census changed during build')
        (stage / 'INCOMPLETE').unlink(); stage.rename(out)
        direct_bytes = results['direct']['ledger']['total_package_bytes']
        return {'ok': True, 'profiles': results, 'winner': winner, 'best_profile': winner,
                'winning_package_bytes': results[winner]['ledger']['total_package_bytes'],
                'saving_bytes': direct_bytes-results[winner]['ledger']['total_package_bytes'],
                'candidate_tiles': diagnostics, 'maximum_anchor_tiles': max_references,
                'timing': {'catalog_verification_seconds': catalog_seconds,
                           'statistics_seconds': statistics_seconds,
                           'global_model_prepare_seconds': model_seconds,
                           'native_spool_and_anchor_IO_seconds': native_io_seconds,
                           'recipe_encode_and_roundtrip_seconds': encode_seconds,
                           'total_portfolio_seconds': time.perf_counter()-started,
                           'scope': 'Complete seven-package search including actual trials, statistics, assembly and verification; shared work prevents independent per-profile wall-time claim'},
                'scope': 'All native image levels and metadata; Count absent; runtime/JIT/scratch separate'}
    except BaseException as exc:
        write_json(stage / 'failure.json', {'type': type(exc).__name__, 'error': str(exc)})
        raise


class Reader(base.Reader):
    """Independent256 targets, optional one-hop independently coded L1 anchors."""
    def __init__(self, directory):
        arg = Path(directory); self.root = arg.resolve(); path = self.root / 'manifest.json'
        if (arg.is_symlink() or path.is_symlink() or (self.root / 'INCOMPLETE').exists()
                or (self.root / 'failure.json').exists()):
            raise ValueError('Unsafe or incomplete context archive')
        if path.stat().st_size > base.MAX_MANIFEST_BYTES:
            raise ValueError('Manifest exceeds bounded profile')
        blob = path.read_bytes(); self.manifest_bytes = len(blob)
        self.manifest = m = json.loads(blob)
        if m.get('format') != FORMAT or m.get('profile') not in PROFILES or m.get('image_only') is not True:
            raise ValueError('Unsupported context archive')
        self.family = m['profile'].split('_')[0]
        expected_members = BASE_MEMBERS | ({MODEL} if self.family in ('shared', 'pyramid') else set())
        expected_model = MODEL if MODEL in expected_members else None
        if (set(m['files']) != expected_members or m['image'].get('path') != 'images.bank'
                or m['image'].get('index') != 'images.index'
                or m['image'].get('source_metadata') != 'source.json'
                or m['image'].get('model') != expected_model):
            raise ValueError('Unexpected context package members')
        if {entry.name for entry in self.root.iterdir()} != expected_members | {'manifest.json'}:
            raise ValueError('Unlisted or missing context package entry')
        params = m.get('parameters', {})
        fixed = {'tile': TILE, 'jpegxl_effort': 9, 'threads': 1, 'context_bits': 12,
                 'contexts_per_plane': 32, 'candidate_level': 0, 'max_anchor_tiles': 4,
                 'reference_depth': 1, 'per_query_cache_bytes': MAX_QUERY_CACHE_BYTES,
                 'max_tiles': MAX_TILES, 'max_roi_pixels': MAX_ROI_PIXELS}
        if (any(not base._integer(params.get(k)) or params[k] != v for k, v in fixed.items())
                or params.get('training') is not False or params.get('persistent_decoded_pixel_cache') is not False
                or params.get('lower_levels') != 'independent-JXL9'
                or params.get('predictor') != PREDICTOR
                or params.get('gradient_thresholds') != [0, 1, 2, 4, 8, 16, 32]
                or params.get('context_planes') != ['G', 'R', 'B']):
            raise ValueError('Unsupported fixed context profile')
        self._descriptors = list(base._descriptors(m['image']['levels']))
        if len(m['image']['levels']) < 2:
            raise ValueError('Context portfolio requires native L1')
        for name, desc in m['files'].items():
            member = self.root / name
            if (not _name(name) or member.is_symlink() or not member.is_file()
                    or type(desc.get('bytes')) is not int or desc['bytes'] < 0
                    or member.stat().st_size != desc['bytes']
                    or not isinstance(desc.get('sha256'), str) or base.HEX.fullmatch(desc['sha256']) is None):
                raise ValueError('Unsafe or size-mismatched context member')
        if m['files']['source.json']['bytes'] > scan.MAX_SOURCE_METADATA_BYTES:
            raise ValueError('Source metadata exceeds bounded profile')
        if m['files']['images.index']['bytes'] != HEADER.size + len(self._descriptors)*ENTRY.size:
            raise ValueError('Index size differs from native coverage')
        if MODEL in m['files'] and not 0 < m['files'][MODEL]['bytes'] <= MODEL_MAX_BYTES:
            raise ValueError('Model exceeds bounded profile')
        self._entries = None; self._bank = None; self._level_start = []; self.index_read_bytes = 0
        self._source_metadata = None; self._prepared = None
        self.jxl_tiles_decoded = self.constant_tiles_filled = self.logical_tiles_read = 0
        self.decoded_pixels = self.requested_pixels = self.cache_hits = 0
        self.last_query_cache_bytes = self.last_query_unique_payloads = 0
        self.context_tiles_decoded = self.local_model_prepares = self.global_model_prepares = 0
        self.model_read_bytes = self.prepared_model_bytes = 0
        self.anchor_tile_requests = self.anchor_tiles_decoded = self.anchor_decoded_pixels = 0
        self.target_tile_requests = self.max_anchor_tiles = 0

    def close(self):
        super().close(); self._prepared = None; self.prepared_model_bytes = 0

    def _setup(self):
        if self._entries is not None:
            return
        blob = (self.root / 'images.index').read_bytes(); self.index_read_bytes += len(blob)
        if hashlib.sha256(blob).hexdigest() != self.manifest['files']['images.index']['sha256']:
            raise ValueError('Context index SHA mismatch')
        if len(blob) < HEADER.size:
            raise ValueError('Truncated context index')
        magic, tile, count = HEADER.unpack_from(blob)
        if magic != MAGIC or tile != TILE or count != len(self._descriptors) or len(blob) != HEADER.size+count*ENTRY.size:
            raise ValueError('Context index header/coverage mismatch')
        entries = [ENTRY.unpack_from(blob, HEADER.size+i*ENTRY.size) for i in range(count)]
        allowed = {JXL} if self.family == 'direct' else {ROUTES[self.family]}
        if self.manifest['profile'].endswith('_hybrid'):
            allowed.add(JXL)
        end = 0
        for i, (entry, desc) in enumerate(zip(entries, self._descriptors)):
            offset, length, route, crc = entry
            if blob[HEADER.size+i*ENTRY.size+13:HEADER.size+i*ENTRY.size+16] != b'\0'*3:
                raise ValueError('Reserved index fields must be zero')
            if (offset != end or not 0 < length <= MAX_PAYLOAD
                    or route not in (allowed if desc[1] == 0 else {JXL})):
                raise ValueError('Invalid context route or contiguous span; anchors must be independent JXL')
            end += length
        if end != self.manifest['files']['images.bank']['bytes']:
            raise ValueError('Trailing or missing bank bytes')
        self._entries = entries
        self._level_start = [next(d[0] for d in self._descriptors if d[1] == info['level'])
                             for info in self.manifest['image']['levels']]
        self._bank = MeteredFile(self.root / 'images.bank')

    def _model(self):
        if self._prepared is None:
            blob = (self.root / MODEL).read_bytes(); self.model_read_bytes += len(blob)
            if hashlib.sha256(blob).hexdigest() != self.manifest['files'][MODEL]['sha256']:
                raise ValueError('Context model SHA mismatch')
            self._prepared = codec.prepare(codec.loads(blob))
            self.global_model_prepares += 1; self.prepared_model_bytes = self._prepared.nbytes
        return self._prepared

    def _tile(self, gid, cache=None, cache_state=None, *, anchor_role=False):
        import imagecodecs
        if self._entries is None:
            self._setup()
        if not base._integer(gid) or not 0 <= gid < len(self._entries):
            raise ValueError('Tile identity outside package')
        if anchor_role:
            self.anchor_tile_requests += 1
        else:
            self.target_tile_requests += 1
        self.logical_tiles_read += 1
        offset, length, route, crc = self._entries[gid]
        desc = self._descriptors[gid]; w, h = desc[4:]
        key = (offset, length, crc)
        if cache is not None and key in cache:
            self.cache_hits += 1
            return cache[key]
        self._bank.seek(offset); blob = self._bank.read(length)
        if len(blob) != length or zlib.crc32(blob) != crc:
            raise ValueError('Context tile payload integrity')
        if route == JXL:
            pixels = imagecodecs.jpegxl_decode(blob, numthreads=1, out=np.empty((h, w, 3), np.uint8))
            self.jxl_tiles_decoded += 1
            if anchor_role:
                self.anchor_tiles_decoded += 1; self.anchor_decoded_pixels += h*w
        elif route == LOCAL:
            if len(blob) < 8:
                raise ValueError('Truncated local model record')
            model_len = struct.unpack_from('<I', blob)[0]
            if not 0 < model_len <= MODEL_MAX_BYTES or 4+model_len+4 > len(blob):
                raise ValueError('Local model bounds')
            model = codec.prepare(codec.loads(blob[4:4+model_len])); self.local_model_prepares += 1
            pixels = codec.decode(blob[4+model_len:], h, w, model)
            self.context_tiles_decoded += 1
        elif route in (SHARED, PYRAMID):
            condition = None
            if route == PYRAMID:
                condition, references = _anchor(self.manifest['image']['levels'], desc,
                    lambda anchor_gid: self._tile(anchor_gid, cache, cache_state, anchor_role=True))
                self.max_anchor_tiles = max(self.max_anchor_tiles, references)
            pixels = codec.decode(blob, h, w, self._model(), anchor=condition)
            self.context_tiles_decoded += 1
        else:
            raise ValueError('Unknown context codec')
        if pixels.dtype != np.uint8 or pixels.shape != (h, w, 3):
            raise ValueError('Native decoded shape/type mismatch')
        self.decoded_pixels += h*w
        if cache_state is not None:
            cache_state['decoded'].add(key)
            if cache_state['bytes'] + pixels.nbytes <= MAX_QUERY_CACHE_BYTES:
                cache[key] = pixels; cache_state['bytes'] += pixels.nbytes
        return pixels

    def metrics(self):
        result = super().metrics()
        result.update(context_tiles_decoded=self.context_tiles_decoded,
                      local_model_prepares=self.local_model_prepares,
                      global_model_prepares=self.global_model_prepares,
                      model_read_bytes=self.model_read_bytes,
                      prepared_model_bytes=self.prepared_model_bytes,
                      target_tile_requests=self.target_tile_requests,
                      anchor_tile_requests=self.anchor_tile_requests,
                      anchor_tiles_decoded=self.anchor_tiles_decoded,
                      anchor_decoded_pixels=self.anchor_decoded_pixels,
                      max_anchor_tiles_per_target=self.max_anchor_tiles,
                      dependency='At most one L1 hop; L1 and lower levels are independent JXL',
                      cache_scope='Same8MiB query-local pixel budget including anchors and targets; global prepared model is separately reported')
        return result


WSIContextArchive = Reader
