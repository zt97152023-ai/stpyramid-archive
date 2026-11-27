"""Fixed independent256 native RGB JPEG XL effort11 comparison, task090.

This is a complete image package, not a complete spatial-transcriptomics
package. It makes no assertion that independent tile streams are the strongest
possible JPEG XL configuration or that the JPEG XL format lacks group access.
Construction is split into fixed consecutive32-gid jobs. Every logical tile is
encoded; there are no alias, constant, effort-selection or candidate routes.
The root harness owns all resource guards, job limits and source-blocked replay.
"""
from __future__ import annotations

import copy
from functools import lru_cache
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import shutil
import time
import zlib

import numpy as np

from . import slide_redundancy as base
from . import slide_redundancy_scan as scan
from .integrity import sha256, write_json
from .reader import MeteredFile
from .wsi_shared import _disjoint, _name


FORMAT = 'stlossless-native-jxl11-090-1'
CHUNK_SCHEMA = 'stlossless-jxl11-chunk-090-1'
MAGIC = b'STJ11090'
HEADER, ENTRY = base.HEADER, base.ENTRY
TILE, CHUNK_SIZE, JXL = 256, 32, 1
MAX_PAYLOAD = base.MAX_PAYLOAD
MAX_RECEIPT = 2 * 1024 * 1024
MEMBERS = {'images.bank', 'images.index', 'source.json'}
_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = _ROOT / 'evidence' / 'STBASELINE-084' / 'jxl11.py'
ADAPTER_SHA256 = '88d37a0b12584c5eea0fabd45559610da5ef6480a55a3d69937c4e4c623d858d'
FIXED = {
    'tile': TILE, 'jpegxl_effort': 11, 'threads': 1, 'lossless': True,
    'distance': 0, 'uses_original_profile': True, 'raw_codestream': True,
    'expert_api': 'JxlEncoderAllowExpertOptions',
    'parallel_runner': 'NULL-single-thread', 'resampling': False,
    'logical_tile_encoding': 'every tile; no aliases/constants/effort selection',
    'max_tiles': base.MAX_TILES, 'max_roi_pixels': base.MAX_ROI_PIXELS,
    'per_query_cache_bytes': base.MAX_QUERY_CACHE_BYTES,
    'persistent_decoded_pixel_cache': False, 'training': False,
}
_TIMING_KEYS = {
    'catalog_verification_seconds', 'runtime_load_and_hash_seconds',
    'tile_input_seconds', 'encode_api_seconds', 'immediate_decode_check_seconds',
    'encode_plus_roundtrip_seconds', 'payload_write_seconds',
    'chunk_api_seconds_before_receipt_write',
}
_NATIVE_APIS = [
    'JxlEncoderSetParallelRunner', 'JxlEncoderUseContainer',
    'JxlEncoderSetBasicInfo', 'JxlEncoderSetColorEncoding',
    'JxlEncoderAllowExpertOptions', 'JxlEncoderSetFrameDistance',
    'JxlEncoderSetFrameLossless', 'JxlEncoderFrameSettingsSetOption',
    'JxlEncoderAddImageFrame',
]


def _digest(blob):
    return hashlib.sha256(blob).hexdigest()


def _same_typed(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_same_typed(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same_typed(a, b) for a, b in zip(actual, expected))
    return actual == expected


def _read(path, limit):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError('Missing, unsafe or unbounded090 member: ' + path.name)
    return path.read_bytes()


def _code_pins():
    paths = [Path(__file__), Path(base.__file__), Path(scan.__file__),
             _ROOT/'src'/'stbaseline'/'integrity.py',
             _ROOT/'src'/'stbaseline'/'reader.py',
             _ROOT/'src'/'stbaseline'/'wsi_shared.py',
             _ROOT/'src'/'stbaseline'/'wsi_source.py', ADAPTER]
    return {p.relative_to(_ROOT).as_posix(): sha256(p) for p in paths}


@lru_cache(maxsize=1)
def _adapter():
    if ADAPTER.is_symlink() or sha256(ADAPTER) != ADAPTER_SHA256:
        raise ValueError('Immutable084 expert adapter source SHA differs')
    spec = importlib.util.spec_from_file_location('_stbaseline_jxl11_090_adapter', ADAPTER)
    module = importlib.util.module_from_spec(spec)
    # Execute the immutable source without creating an084 __pycache__ file.
    exec(compile(ADAPTER.read_bytes(), str(ADAPTER), 'exec'), module.__dict__)
    return module


def qualify_runtime():
    """Load/pin the existing native library; no images are encoded here."""
    native = _adapter().runtime_receipt()
    if (native['version_number'] != 12000 or native['effort'] != 11
            or native['threads'] != 1 or native['lossless'] is not True
            or native['uses_original_profile'] is not True
            or native['expert_api'] != 'JxlEncoderAllowExpertOptions'):
        raise ValueError('Unexpected expert11 native runtime configuration')
    return {'adapter_sha256': ADAPTER_SHA256, 'native': native,
            'code_sha256': _code_pins()}


def _census(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('Symlink census unsupported')
    catalog = base._catalog(directory)
    return directory.resolve(), catalog, sha256(directory/'catalog.json')


def _ranges(count):
    if type(count) is not int or not 1 <= count <= base.MAX_TILES:
        raise ValueError('Invalid090 tile count')
    return [(start, min(start+CHUNK_SIZE, count)) for start in range(0, count, CHUNK_SIZE)]


def chunk_ranges(censusdir):
    """Return the frozen32-gid partition after checking the qualified census."""
    _, catalog, _ = _census(censusdir)
    return _ranges(len(catalog['tiles']))


def _native_calls(calls):
    if not isinstance(calls, list) or [v.get('api') for v in calls] != _NATIVE_APIS:
        raise ValueError('Native expert11 API sequence differs from pinned adapter')
    for value in calls:
        expected = 'void-return' if value['api'] == 'JxlEncoderAllowExpertOptions' else 0
        if not _same_typed(value.get('status'), expected):
            raise ValueError('Native expert11 option was not accepted')
    effort = calls[7]
    if (not _same_typed(effort.get('value'), 11)
            or effort.get('option') != 'JXL_ENC_FRAME_SETTING_EFFORT'):
        raise ValueError('Native effort must be exactly11, without fallback')


def _native_receipt(receipt, pixels, blob):
    if (receipt.get('effort') != 11 or type(receipt.get('effort')) is not int
            or receipt.get('expert_api_called') is not True or receipt.get('lossless') is not True
            or receipt.get('uses_original_profile') is not True or receipt.get('threads') != 1
            or receipt.get('shape') != list(pixels.shape) or receipt.get('input_bytes') != pixels.nbytes
            or receipt.get('input_sha256') != _digest(pixels.tobytes())
            or receipt.get('encoded_bytes') != len(blob) or receipt.get('encoded_sha256') != _digest(blob)
            or receipt.get('process_output_status') != 0):
        raise ValueError('Actual native effort11 call receipt differs from tile')
    _native_calls(receipt.get('native_calls'))


def build_chunk(censusdir, output, start_gid, stop_gid):
    """Create one32-gid chunk; return saved receipt plus all output member hashes.

    output/receipt.json contains the payload hashes and tile/API timings. The
    returned dict additionally has output_files, including receipt.json itself.
    No timestamp, job timing or chunk receipt enters the final image package.
    """
    import imagecodecs
    started = time.perf_counter()
    census, catalog, catalog_hash = _census(censusdir)
    if (type(start_gid) is not int or type(stop_gid) is not int
            or (start_gid, stop_gid) not in _ranges(len(catalog['tiles']))):
        raise ValueError('Chunk must be one frozen consecutive32-gid interval')
    catalog_seconds = time.perf_counter()-started
    out = _disjoint(output, census)
    out.mkdir(parents=True)
    marker = out/'INCOMPLETE'; marker.write_text('090 fixed expert11 chunk\n', encoding='utf8')
    try:
        then = time.perf_counter(); runtime = qualify_runtime()
        runtime_seconds = time.perf_counter()-then
        rows = []; files = {}; input_seconds = write_seconds = encode_seconds = decode_seconds = 0.0
        for gid in range(start_gid, stop_gid):
            tile = catalog['tiles'][gid]
            then = time.perf_counter(); pixels = scan.read_tile(census, gid, catalog=catalog)
            input_seconds += time.perf_counter()-then
            then = time.perf_counter(); blob, native = _adapter().encode_with_receipt(pixels)
            encoded = time.perf_counter()-then; encode_seconds += encoded
            if not 0 < len(blob) <= MAX_PAYLOAD or not blob.startswith(b'\xff\x0a'):
                raise ValueError('Unbounded or non-raw expert11 stream')
            _native_receipt(native, pixels, blob)
            then = time.perf_counter()
            decoded = imagecodecs.jpegxl_decode(blob, numthreads=1)
            if decoded.dtype != np.uint8 or decoded.shape != pixels.shape or not np.array_equal(decoded, pixels):
                raise ValueError('Actual effort11 tile is not pixel-exact')
            elapsed = time.perf_counter()-then; decode_seconds += elapsed
            name = f'{gid:06d}.jxl'; then = time.perf_counter()
            with (out/name).open('xb') as stream: stream.write(blob)
            files[name] = {'bytes': len(blob), 'sha256': _digest(blob)}
            write_seconds += time.perf_counter()-then
            rows.append({'gid': gid, 'level': tile['level'], 'x': tile['x'], 'y': tile['y'],
                         'w': tile['w'], 'h': tile['h'], 'file': name,
                         'native_sha256': tile['native_sha256'],
                         'encoded_bytes': len(blob), 'encoded_sha256': _digest(blob),
                         'encode_seconds': encoded, 'roundtrip_decode_check_seconds': elapsed,
                         'exact': True, 'native_call_receipt': native})
            print(json.dumps({'stage': '090_effort11', 'gid': gid, 'bytes': len(blob),
                              'encode_seconds': encoded, 'roundtrip_seconds': elapsed}), flush=True)
        if sha256(census/'catalog.json') != catalog_hash or _code_pins() != runtime['code_sha256']:
            raise ValueError('Census or reviewed source changed during chunk construction')
        receipt = {'schema': CHUNK_SCHEMA, 'ok': True, 'start_gid': start_gid, 'stop_gid': stop_gid,
                   'tile_count': len(catalog['tiles']), 'chunk_size': CHUNK_SIZE,
                   'catalog_sha256': catalog_hash, 'source_metadata': catalog['files']['source.json'],
                   'spool_identity': catalog['files']['tiles.rgb'], 'parameters': copy.deepcopy(FIXED),
                   'runtime': runtime, 'files': files, 'tiles': rows,
                   'timing': {'catalog_verification_seconds': catalog_seconds,
                              'runtime_load_and_hash_seconds': runtime_seconds,
                              'tile_input_seconds': input_seconds,
                              'encode_api_seconds': encode_seconds,
                              'immediate_decode_check_seconds': decode_seconds,
                              'encode_plus_roundtrip_seconds': encode_seconds+decode_seconds,
                              'payload_write_seconds': write_seconds,
                              'chunk_api_seconds_before_receipt_write': time.perf_counter()-started}}
        write_json(out/'receipt.json', receipt); marker.unlink()
        return {**receipt, 'output_files': base._members(out)}
    except BaseException as exc:
        write_json(out/'failure.json', {'type': type(exc).__name__, 'error': str(exc)})
        raise


def _seconds(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def _chunk(directory, catalog, catalog_hash, interval, runtime):
    arg = Path(directory)
    if arg.is_symlink(): raise ValueError('Symlink090 chunk unsupported')
    root = arg.resolve()
    if (root/'INCOMPLETE').exists() or (root/'failure.json').exists():
        raise ValueError('Failed or incomplete090 chunk cannot be reused')
    receipt = json.loads(_read(root/'receipt.json', MAX_RECEIPT))
    start, stop = interval
    expected = {'schema': CHUNK_SCHEMA, 'ok': True, 'start_gid': start, 'stop_gid': stop,
                'tile_count': len(catalog['tiles']), 'chunk_size': CHUNK_SIZE,
                'catalog_sha256': catalog_hash, 'source_metadata': catalog['files']['source.json'],
                'spool_identity': catalog['files']['tiles.rgb'], 'parameters': FIXED, 'runtime': runtime}
    if any(not _same_typed(receipt.get(k), v) for k, v in expected.items()):
        raise ValueError('Chunk identity/settings/runtime differs from frozen090 scope')
    names = {f'{gid:06d}.jxl' for gid in range(start, stop)}
    if (set(receipt.get('files', {})) != names or {p.name for p in root.iterdir()} != names | {'receipt.json'}
            or len(receipt.get('tiles', [])) != stop-start):
        raise ValueError('Chunk has missing, duplicated or unlisted payloads')
    for row, gid in zip(receipt['tiles'], range(start, stop)):
        tile = catalog['tiles'][gid]; name = f'{gid:06d}.jxl'
        identity = {k: tile[k] for k in ('gid', 'level', 'x', 'y', 'w', 'h', 'native_sha256')}
        if (any(not _same_typed(row.get(k), v) for k, v in identity.items()) or row.get('file') != name
                or row.get('exact') is not True or not _seconds(row.get('encode_seconds'))
                or not _seconds(row.get('roundtrip_decode_check_seconds'))):
            raise ValueError('Chunk tile order, source identity or timing differs')
        blob = _read(root/name, MAX_PAYLOAD); info = {'bytes': len(blob), 'sha256': _digest(blob)}
        if (not _same_typed(receipt['files'][name], info) or row.get('encoded_bytes') != len(blob)
                or row.get('encoded_sha256') != info['sha256'] or not blob.startswith(b'\xff\x0a')):
            raise ValueError('Chunk payload bytes/hash differs from receipt')
        # Recheck proof of fixed settings and declared source pixels without
        # reopening the original TIFF or re-running an encoder.
        native = row.get('native_call_receipt', {})
        if (native.get('shape') != [tile['h'], tile['w'], 3]
                or native.get('input_sha256') != tile['native_sha256']
                or native.get('input_bytes') != tile['h']*tile['w']*3
                or native.get('encoded_bytes') != len(blob) or native.get('encoded_sha256') != info['sha256']
                or native.get('effort') != 11 or native.get('expert_api_called') is not True
                or native.get('lossless') is not True or native.get('uses_original_profile') is not True
                or native.get('threads') != 1 or native.get('process_output_status') != 0):
            raise ValueError('Stored native-call proof differs from chunk')
        _native_calls(native.get('native_calls'))
    timing = receipt.get('timing', {})
    if set(timing) != _TIMING_KEYS or any(not _seconds(v) for v in timing.values()):
        raise ValueError('Invalid chunk timing receipt')
    for key, row_key in [('encode_api_seconds', 'encode_seconds'),
                         ('immediate_decode_check_seconds', 'roundtrip_decode_check_seconds')]:
        value = sum(row[row_key] for row in receipt['tiles'])
        if key not in timing or not math.isclose(timing[key], value, rel_tol=1e-12, abs_tol=1e-8):
            raise ValueError('Chunk timing sum differs from actual tile receipts')
    if not math.isclose(timing.get('encode_plus_roundtrip_seconds', -1),
                        timing['encode_api_seconds']+timing['immediate_decode_check_seconds'],
                        rel_tol=1e-12, abs_tol=1e-8):
        raise ValueError('Chunk encode/roundtrip total differs')
    return root, receipt


def assemble(censusdir, chunk_dirs_in_gid_order, output):
    """Validate all completed chunks and assemble one deterministic image package."""
    started = time.perf_counter()
    census, catalog, catalog_hash = _census(censusdir)
    ranges = _ranges(len(catalog['tiles'])); chunks = list(chunk_dirs_in_gid_order)
    if len(chunks) != len(ranges) or len({Path(p).resolve() for p in chunks}) != len(chunks):
        raise ValueError('090 requires exactly one completed chunk per frozen interval')
    out = _disjoint(output, census, *chunks)
    stage = _disjoint(out.with_name(out.name+'.partial'), census, *chunks, out)
    stage.mkdir(parents=True); marker = stage/'INCOMPLETE'
    marker.write_text('090 effort11 assembly\n', encoding='utf8')
    try:
        runtime = qualify_runtime()
        validated = [_chunk(p, catalog, catalog_hash, interval, runtime) for p, interval in zip(chunks, ranges)]
        validation_seconds = time.perf_counter()-started; then = time.perf_counter()
        shutil.copyfile(census/'source.json', stage/'source.json')
        if sha256(stage/'source.json') != catalog['files']['source.json']['sha256']:
            raise ValueError('090 source metadata changed during copy')
        with (stage/'images.bank').open('xb') as bank, (stage/'images.index').open('xb') as index:
            index.write(HEADER.pack(MAGIC, TILE, len(catalog['tiles'])))
            for root, receipt in validated:
                for row in receipt['tiles']:
                    blob = _read(root/row['file'], MAX_PAYLOAD)
                    if _digest(blob) != row['encoded_sha256']:
                        raise ValueError('Chunk changed during assembly')
                    index.write(ENTRY.pack(bank.tell(), len(blob), JXL, zlib.crc32(blob)))
                    bank.write(blob)
        source = json.loads((stage/'source.json').read_bytes())
        files = {name: {'bytes': (stage/name).stat().st_size, 'sha256': sha256(stage/name)} for name in sorted(MEMBERS)}
        manifest = {'format': FORMAT, 'profile': 'direct11', 'image_only': True,
                    'image': {'path': 'images.bank', 'index': 'images.index', 'source_metadata': 'source.json',
                              'levels': copy.deepcopy(catalog['levels'])},
                    'parameters': copy.deepcopy(FIXED), 'files': files,
                    'build': {'runtime': runtime, 'source_census_sha256': catalog_hash,
                              'source_whole_sha256': source['whole_file_sha256'],
                              'encoded_tiles': len(catalog['tiles']), 'no_payload_reuse': True,
                              'scope': 'All native RGB/source image metadata; Count absent; runtime/scratch separate'}}
        write_json(stage/'manifest.json', manifest)
        if sha256(census/'catalog.json') != catalog_hash or _code_pins() != runtime['code_sha256']:
            raise ValueError('Census or reviewed source changed during assembly')
        marker.unlink(); assembly_seconds = time.perf_counter()-then
        then = time.perf_counter()
        with Reader(stage) as reader:
            verification = reader.verify(); ledger = reader.ledger()
        verification_seconds = time.perf_counter()-then
        timing_keys = sorted(validated[0][1]['timing'])
        result = {'ok': True, 'format': FORMAT, 'profile': 'direct11', 'ledger': ledger,
                'verification': verification, 'package_files': base._members(stage),
                'chunks': [{'start_gid': r['start_gid'], 'stop_gid': r['stop_gid'],
                            'receipt_sha256': sha256(p/'receipt.json'), 'output_files': base._members(p)}
                           for p, r in validated],
                'timing': {'input_and_chunk_verification_seconds': validation_seconds,
                           'assembly_seconds': assembly_seconds, 'verification_seconds': verification_seconds,
                           'assembly_api_seconds': time.perf_counter()-started,
                           'all_chunk_timing_sums': {k: sum(r['timing'][k] for _, r in validated) for k in timing_keys}},
                'cost_scope': 'Chunk API/encode/decode/write totals and assembly separate; job walls supplied by root'}
        stage.rename(out)
        return result
    except BaseException as exc:
        write_json(stage/'failure.json', {'type': type(exc).__name__, 'error': str(exc)})
        raise


class Reader(base.Reader):
    """Explicit090 effort11 envelope; generic exact ROI/restore algorithms only."""
    def __init__(self, directory):
        arg = Path(directory); self.root = arg.resolve()
        if (arg.is_symlink() or (self.root/'INCOMPLETE').exists() or (self.root/'failure.json').exists()):
            raise ValueError('Unsafe or incomplete090 archive')
        blob = _read(self.root/'manifest.json', base.MAX_MANIFEST_BYTES)
        self.manifest = m = json.loads(blob); self.manifest_bytes = len(blob)
        if (m.get('format') != FORMAT or m.get('profile') != 'direct11' or m.get('image_only') is not True
                or not _same_typed(m.get('parameters'), FIXED)):
            raise ValueError('Unsupported090 fixed effort11 envelope')
        self._descriptors = list(base._descriptors(m['image']['levels']))
        if (set(m.get('files', {})) != MEMBERS or {p.name for p in self.root.iterdir()} != MEMBERS | {'manifest.json'}
                or m['image'].get('path') != 'images.bank' or m['image'].get('index') != 'images.index'
                or m['image'].get('source_metadata') != 'source.json'):
            raise ValueError('Unexpected090 members or paths')
        for name, info in m['files'].items():
            path = self.root/name
            if (not _name(name) or path.is_symlink() or not path.is_file() or type(info.get('bytes')) is not int
                    or info['bytes'] <= 0 or path.stat().st_size != info['bytes']
                    or not isinstance(info.get('sha256'), str) or base.HEX.fullmatch(info['sha256']) is None):
                raise ValueError('Unsafe090 member/size/hash')
        if (m['files']['source.json']['bytes'] > scan.MAX_SOURCE_METADATA_BYTES
                or m['files']['images.index']['bytes'] != HEADER.size+len(self._descriptors)*ENTRY.size
                or m['files']['images.bank']['bytes'] > len(self._descriptors)*MAX_PAYLOAD):
            raise ValueError('090 source/index/bank exceeds fixed bounds')
        build = m.get('build', {}); runtime = build.get('runtime', {}); native = runtime.get('native', {})
        if (runtime.get('adapter_sha256') != ADAPTER_SHA256 or native.get('version_number') != 12000
                or native.get('effort') != 11 or native.get('lossless') is not True
                or native.get('uses_original_profile') is not True or native.get('threads') != 1
                or native.get('expert_api') != 'JxlEncoderAllowExpertOptions'
                or build.get('encoded_tiles') != len(self._descriptors) or build.get('no_payload_reuse') is not True):
            raise ValueError('090 expert11 build identity invalid')
        for key in ('source_census_sha256', 'source_whole_sha256'):
            if not isinstance(build.get(key), str) or base.HEX.fullmatch(build[key]) is None:
                raise ValueError('090 source identity hash missing')
        self._entries = self._bank = None; self._level_start = []; self._source_metadata = None; self._closed = False
        self.index_read_bytes = self.jxl_tiles_decoded = self.constant_tiles_filled = self.logical_tiles_read = 0
        self.decoded_pixels = self.requested_pixels = self.cache_hits = 0
        self.last_query_cache_bytes = self.last_query_unique_payloads = 0

    def _setup(self):
        if self._closed: raise ValueError('090 Reader is closed')
        if self._entries is not None: return
        blob = _read(self.root/'images.index', HEADER.size+base.MAX_TILES*ENTRY.size)
        self.index_read_bytes += len(blob)
        if (_digest(blob) != self.manifest['files']['images.index']['sha256']
                or HEADER.unpack_from(blob) != (MAGIC, TILE, len(self._descriptors))):
            raise ValueError('090 index integrity or identity differs')
        entries = []; end = 0
        for gid, _ in enumerate(self._descriptors):
            pos = HEADER.size+gid*ENTRY.size
            offset, length, codec, crc = ENTRY.unpack_from(blob, pos)
            if (blob[pos+13:pos+16] != b'\x00'*3 or codec != JXL or offset != end
                    or not 0 < length <= MAX_PAYLOAD):
                raise ValueError('090 index requires ordered independent JXL11 records')
            end += length; entries.append((offset, length, codec, crc))
        if end != self.manifest['files']['images.bank']['bytes']:
            raise ValueError('090 bank has trailing/missing bytes')
        self._entries = entries
        self._level_start = [next(d[0] for d in self._descriptors if d[1] == info['level']) for info in self.manifest['image']['levels']]
        self._bank = MeteredFile(self.root/'images.bank')

    def _tile(self, gid, cache=None, cache_state=None):
        if self._closed: raise ValueError('090 Reader is closed')
        return super()._tile(gid, cache, cache_state)

    def source_metadata(self):
        value = super().source_metadata()
        if value['whole_file_sha256'] != self.manifest['build']['source_whole_sha256']:
            raise ValueError('090 whole-source metadata binding differs')
        return value

    def close(self):
        super().close(); self._closed = True

    def metrics(self):
        result = super().metrics()
        result['codec'] = 'independent256-direct-JPEGXL-effort11'
        result['cache_scope'] = 'query-local native pixels; no persistent cache; no source dependency'
        return result
