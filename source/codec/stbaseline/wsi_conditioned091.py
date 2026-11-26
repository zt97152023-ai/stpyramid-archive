"""091 parent-conditioned HF with paid, reused089 anchor-correction streams.

Control and conditional use the same coefficients, order and reference slots;
only the known-parent property gate differs. Every lower level is direct JXL11.
This image-only custom Modular container is not a standard JPEG XL stream.
"""
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time
import zlib

import numpy as np

from . import slide_redundancy as base
from . import slide_redundancy_scan as scan
from . import wsi_jxl11_090 as reference090
from . import wsi_squeeze089 as previous089
from .integrity import sha256, write_json
from .reader import MeteredFile
from .wsi_shared import _disjoint, _name

FORMAT = 'stlossless-native-conditioned-091-1'
MAGIC, ANCHOR_MAGIC = b'STCON091', b'STANC091'
HEADER, ENTRY = base.HEADER, base.ENTRY
ANCHOR_ENTRY = struct.Struct('<I')
TILE, ABI_VERSION = 256, 1
JXL, CONTROL, CONDITIONAL = 1, 2, 3
NO_ANCHOR = 0xffffffff
MAX_STREAM_BYTES = 2 * 1024 * 1024
MAX_PAYLOAD = 2 * MAX_STREAM_BYTES + 260
MAX_QUERY_CACHE_BYTES = base.MAX_QUERY_CACHE_BYTES
PROFILES = ('direct11', 'control_pure', 'conditional_pure', 'control_hybrid', 'conditional_hybrid')
BASE_MEMBERS = {'images.bank', 'images.index', 'source.json'}
FIXED = {
    'tile': TILE, 'abi_version': ABI_VERSION, 'color_transform': 'G,R-G,B-G',
    'squeeze': 'H_then_V_first3_append', 'detail_predictor': 'Zero',
    'lowpass': 'Byte-identical089 anchor correction; no reencoding',
    'decode_order': 'LL,V,Hlow,H', 'parent_gate': 'control zero; conditional enabled',
    'known_channels': '0..2 LL projections; 6..8 Hlow projections; never tokenized',
    'target_channels': '3..5 V; 9..11 H', 'reference_properties': '16..35',
    'max_properties': 5, 'uint_method': 'kBest',
    'lz77_method': 'kOptimal_per_tile', 'tree_node_limit': 4095,
    'tree_budget_policy': 'bfs_parent_predictor_v1', 'candidate_level': 0,
    'lower_levels': 'independent JXL11', 'training': False, 'anchor_depth': 1,
    'anchor_geometry': 'floor native L1; min(origin//2+cell,L1_extent-1)',
    'anchor_fetch': 'one full independent nativeL1 tile, not only cropped anchor',
    'pixel_cache_bytes': MAX_QUERY_CACHE_BYTES, 'persistent_pixel_cache': False,
    'hybrid': 'full native record < direct payload; ties direct; complete package fallback',
}


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _read_bounded(path, limit):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError('Missing, unsafe or unbounded091 member: ' + path.name)
    data = path.read_bytes()
    if not 0 < len(data) <= limit:
        raise ValueError('Changed or unbounded091 member')
    return data


def _anchor_geometry(levels):
    if len(levels) < 2:
        raise ValueError('091 requires a nonempty floor-sized native L1')
    h0, w0, _ = levels[0]['shape']; h1, w1, _ = levels[1]['shape']
    if min(h0, w0) < 2 or (h1, w1) != (h0//2, w0//2):
        raise ValueError('091 requires a nonempty floor-sized native L1')


def _anchor_layout(levels, descriptor):
    """One full L1 tile supplies the clamped ceil(w/2)xceil(h/2) anchor."""
    _anchor_geometry(levels)
    _, level, x, y, w, h = descriptor
    if level != 0 or x % TILE or y % TILE or not (1 <= w <= TILE and 1 <= h <= TILE):
        raise ValueError('091 anchors apply only to independent native L0 tiles')
    h0, w0, _ = levels[0]['shape']; h1, w1, _ = levels[1]['shape']
    xs = np.minimum(x//2 + np.arange((w+1)//2), w1-1)
    ys = np.minimum(y//2 + np.arange((h+1)//2), h1-1)
    tx, ty = int(xs[0]//TILE), int(ys[0]//TILE)
    if np.any(xs//TILE != tx) or np.any(ys//TILE != ty):
        raise ValueError('091 anchor would need more than one L1 tile')
    start = ((w0+TILE-1)//TILE)*((h0+TILE-1)//TILE)
    gid = start + ty*((w1+TILE-1)//TILE) + tx
    return gid, ys % TILE, xs % TILE


def _anchor(levels, descriptor, fetch):
    gid, ys, xs = _anchor_layout(levels, descriptor)
    tile = fetch(gid)
    return np.ascontiguousarray(tile[ys[:, None], xs[None, :]]), gid


def _runtime(runtime_dir=None):
    value = runtime_dir or os.environ.get('ST091_RUNTIME')
    if not value:
        raise RuntimeError('ST091_RUNTIME or runtime_dir must name the qualified091 build')
    arg = Path(value)
    if arg.is_symlink() or not arg.is_dir():
        raise ValueError('Unsafe native091 runtime directory')
    root = arg.resolve()
    receipt = json.loads(_read_bounded(root/'build_receipt.json', 4*1024*1024))
    if receipt.get('abi_version') != ABI_VERSION or (receipt.get('built_dependencies') is not False and receipt.get('release_profile') != 'local-source-v1'):
        raise ValueError('Unqualified091 runtime receipt')
    pins = {}
    for name in ('slide_jxl091.exe', 'slide_jxl091.dll'):
        path = root/name; info = receipt.get('outputs', {}).get(name, {})
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != info.get('bytes')
                or sha256(path) != info.get('sha256')):
            raise ValueError('Runtime differs from build receipt: ' + name)
        pins[name] = {'bytes': info['bytes'], 'sha256': info['sha256']}
    return root, pins


class NativeRuntime:
    """Stateless native decoder; all per-stream models are inside the paid record."""
    def __init__(self, runtime_dir=None, expected=None):
        self.root, self.pins = _runtime(runtime_dir)
        if expected is not None and expected != self.pins:
            raise ValueError('Native091 runtime differs from archive pins')
        self.library = lib = ctypes.CDLL(str(self.root/'slide_jxl091.dll'))
        lib.st091_abi_version.argtypes = []; lib.st091_abi_version.restype = ctypes.c_uint32
        if lib.st091_abi_version() != ABI_VERSION:
            raise ValueError('Unsupported091 ABI')
        ptr, size, u32 = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32
        lib.st091_decode.argtypes = [ptr, size, ptr, size, u32, u32, u32, ptr, size, ptr, size]
        lib.st091_decode.restype = ctypes.c_int

    def decode(self, blob, w, h, gid, anchor=None):
        if (not base._integer(w) or not base._integer(h) or not 1 <= w <= TILE or not 1 <= h <= TILE
                or not base._integer(gid) or not 0 <= gid < base.MAX_TILES
                or not isinstance(blob, bytes) or not 260 <= len(blob) <= MAX_PAYLOAD):
            raise ValueError('091 native decode arguments exceed bounds')
        anchor_pointer, anchor_size = None, 0
        if not isinstance(anchor, np.ndarray) or anchor.dtype != np.uint8 or anchor.shape != ((h+1)//2, (w+1)//2, 3):
            raise ValueError('091 anchor array required with exact shape/type')
        anchor = np.ascontiguousarray(anchor)
        anchor_pointer, anchor_size = anchor.ctypes.data, anchor.nbytes
        payload = ctypes.create_string_buffer(blob)
        pixels = np.empty((h, w, 3), np.uint8); error = ctypes.create_string_buffer(2048)
        if self.library.st091_decode(payload, len(blob), anchor_pointer, anchor_size,
                                     gid, w, h, pixels.ctypes.data, pixels.nbytes, error, len(error)):
            raise ValueError('st091_decode: ' + error.value.decode('utf8', 'replace'))
        return pixels


def _native_encode(runtime, spool, target):
    env = os.environ.copy()
    for name in list(env):
        if name in ('ST074_S', 'ST074_T') or ('074' in name and ('JXL' in name or 'STBASELINE' in name)):
            env.pop(name)
    stdout, stderr = target.parent/'native.stdout.txt', target.parent/'native.stderr.txt'
    then = time.perf_counter()
    with stdout.open('x', encoding='utf8') as out, stderr.open('x', encoding='utf8') as err:
        result = subprocess.run([str(runtime/'slide_jxl091.exe'), 'encode', str(spool), str(target)],
                                stdout=out, stderr=err, env=env, timeout=2400,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('Native091 encoding failed: ' + stderr.read_text(encoding='utf8', errors='replace')[-3000:])
    return time.perf_counter()-then


def _record(blob, gid, w, h, mode):
    """Bounded outer parser; native additionally verifies coefficient/profile/RGB exactness."""
    if type(mode) is not int or mode not in (0, 1):
        raise ValueError('091 unknown parent gate')
    if not 260 <= len(blob) <= MAX_PAYLOAD or blob[:8] != b'S091TIL1':
        raise ValueError('091 record magic/length')
    if struct.unpack_from('<7I', blob, 8) != (1, mode, gid, w, h, (w+1)//2, (h+1)//2):
        raise ValueError('091 record mode/identity/geometry binding')
    pos = 164; streams = []
    for i in range(2):
        if pos+48 > len(blob):
            raise ValueError('Truncated091 stream header')
        bits, size = struct.unpack_from('<QQ', blob, pos); digest = blob[pos+16:pos+48]
        pos += 48
        empty = i == 1 and w == h == 1
        if (size > MAX_STREAM_BYTES or pos+size > len(blob)
                or (empty and (size != 0 or bits != 0))
                or (not empty and not (size > 0 and (size-1)*8 < bits <= size*8))):
            raise ValueError('091 stream bit/byte bounds')
        payload = blob[pos:pos+size]; pos += size
        if hashlib.sha256(payload).digest() != digest or (bits % 8 and payload[-1] >> (bits % 8)):
            raise ValueError('091 stream SHA/padding mismatch')
        streams.append({'bytes': size, 'bits': bits, 'sha256': digest.hex(), 'payload': payload,
                        'frame': blob[pos-size-48:pos]})
    if pos != len(blob):
        raise ValueError('Trailing091 record bytes')
    return {'raw_sha256': blob[36:68].hex(), 'anchor_sha256': blob[68:100].hex(),
            'prefix_v_sha256': blob[100:132].hex(), 'prefix_h_sha256': blob[132:164].hex(),
            'low': streams[0], 'detail': streams[1]}


def _validate_stream_receipt(record, stream, coefficients, predictor):
    if any(type(record.get(k)) is not type(v) or record[k] != v for k, v in
           {'bytes': stream['bytes'], 'bits': stream['bits'], 'consumed_bits': stream['bits'],
            'sha256': stream['sha256'], 'coefficients': coefficients, 'predictor': predictor,
            'empty': coefficients == 0}.items()):
        raise ValueError('091 stream receipt differs from actual bytes/coefficients')
    if not isinstance(record.get('coefficient_sha256'), str) or base.HEX.fullmatch(record['coefficient_sha256']) is None:
        raise ValueError('091 coefficient digest missing')
    if type(record.get('lz77_enabled')) is not bool:
        raise ValueError('091 LZ syntax flag must be boolean')
    learned, emitted = record.get('tree_learned_nodes'), record.get('tree_nodes')
    removed, collapsed, pruned = (record.get(k) for k in ('tree_pruned_splits', 'tree_collapsed_subtrees', 'tree_was_pruned'))
    if coefficients == 0:
        if (any(type(v) is not int or v != 0 for v in (learned, emitted, removed, collapsed))
                or pruned is not False or record['lz77_enabled'] is not False):
            raise ValueError('091 empty detail has model state')
    elif (type(learned) is not int or learned < 1 or learned % 2 != 1
          or type(emitted) is not int or not 1 <= emitted <= 4095 or emitted % 2 != 1
          or emitted > learned or type(removed) is not int or removed != (learned-emitted)//2
          or type(collapsed) is not int or not 0 <= collapsed <= removed
          or type(pruned) is not bool or pruned != (removed > 0)
          or (pruned and (learned <= 4095 or collapsed == 0))
          or (not pruned and (learned != emitted or collapsed != 0))):
        raise ValueError('091 tree budget/pruning receipt inconsistent')


def _validate_native(native, catalog, descriptors, anchors, old_records):
    receipt = json.loads(_read_bounded(native/'receipt.json', 16*1024*1024))
    targets = [d for d in descriptors if d[1] == 0]
    fixed = {'format': 'st091-native-parent-context-bank-1', 'standard_jxl': False,
             'abi_version': 1, 'profile': 1, 'groups': len(targets), 'tree_node_limit': 4095,
             'tree_budget_policy': 'bfs_parent_predictor_v1', 'predictor': 'Zero',
             'max_properties': 5, 'color_transform': 'G,R-G,B-G',
             'squeeze': 'H_then_V_first3_append', 'decode_order': 'LL,V,Hlow,H',
             'uint_method': 'kBest', 'lz77_method': 'kOptimal_per_tile'}
    if any(type(receipt.get(k)) is not type(v) or receipt[k] != v for k, v in fixed.items()):
        raise ValueError('Native091 receipt differs from fixed recipe')
    if len(receipt.get('tiles', [])) != len(targets):
        raise ValueError('Native091 receipt coverage')
    costs = dict.fromkeys(('control_bytes', 'conditional_bytes', 'correction_stream_bytes',
                          'control_hf_stream_bytes', 'conditional_hf_stream_bytes'), 0)
    for desc, record in zip(targets, receipt['tiles']):
        gid, level, x, y, w, h = desc; aw, ah = (w+1)//2, (h+1)//2
        old_blob = _read_bounded(old_records/f'{gid:06d}.old089', previous089.MAX_PAYLOAD)
        old = previous089._record(old_blob, gid, w, h, 1)
        expected = {'gid': gid, 'level': level, 'x': x, 'y': y, 'w': w, 'h': h,
                    'anchor_w': aw, 'anchor_h': ah, 'raw_sha256': catalog['tiles'][gid]['native_sha256'],
                    'anchor_sha256': anchors[gid], 'old_record_sha256': _hash(old_blob),
                    'correction_reused_exact': True}
        if any(type(record.get(k)) is not type(v) or record[k] != v for k, v in expected.items()):
            raise ValueError('Native091 source/anchor/old-record receipt differs')
        if old['raw_sha256'] != expected['raw_sha256'] or old['anchor_sha256'] != anchors[gid]:
            raise ValueError('Old089 LP source identity differs')
        for key in ('prefix_v_sha256', 'prefix_h_sha256', 'hf_coefficient_sha256'):
            if not isinstance(record.get(key), str) or base.HEX.fullmatch(record[key]) is None:
                raise ValueError('Native091 coefficient/prefix digest missing')
        views = []
        for mode, family in enumerate(('control', 'conditional')):
            path = f'{family}/{gid:06d}.bin'
            if record.get(f'{family}_path') != path:
                raise ValueError('Native091 record route mismatch')
            blob = _read_bounded(native/path, MAX_PAYLOAD)
            if len(blob) != record.get(f'{family}_bytes') or _hash(blob) != record.get(f'{family}_sha256'):
                raise ValueError('Native091 record byte/SHA receipt differs')
            view = _record(blob, gid, w, h, mode); views.append(view)
            for key in ('raw_sha256', 'anchor_sha256', 'prefix_v_sha256', 'prefix_h_sha256'):
                if view[key] != record[key]:
                    raise ValueError('Native091 serialized source/parent prefix binding differs')
            if view['low']['frame'] != old_blob[-48-old['low']['bytes']:]:
                raise ValueError('Native091 lowpass header/payload is not exact089 reuse')
            detail = record[family]
            _validate_stream_receipt(detail, view['detail'], 3*w*h-3*aw*ah, 'Zero')
            if any(type(detail.get(key)) is not int or detail[key] != detail['coefficients']
                   for key in ('target_values', 'decoder_target_values')):
                raise ValueError('Native091 target token/value count includes known prefix or is incomplete')
            if detail['coefficient_sha256'] != record['hf_coefficient_sha256']:
                raise ValueError('Native091 HF coefficient identity differs between controls')
            nodes, pairs = detail.get('parent_reference_node_count'), detail.get('parent_reference_split_channel_pairs')
            if (type(nodes) is not int or type(pairs) is not int
                    or not 0 <= nodes <= max(0, (detail['tree_nodes']-1)//2)
                    or not nodes <= pairs <= 6*nodes or (mode == 0 and (nodes or pairs))):
                raise ValueError('Native091 parent reference counts exceed tree/target bounds')
            costs[f'{family}_bytes'] += len(blob)
            costs[f'{family}_hf_stream_bytes'] += view['detail']['bytes']
        if views[0]['low'] != views[1]['low']:
            raise ValueError('Matched091 correction frames differ')
        correction = record['correction']; stream = views[0]['low']
        expected_correction = {'bytes': stream['bytes'], 'bits': stream['bits'],
                               'consumed_bits': stream['bits'], 'sha256': stream['sha256'],
                               'coefficients': 3*aw*ah, 'reused': True}
        if any(type(correction.get(k)) is not type(v) or correction[k] != v for k, v in expected_correction.items()):
            raise ValueError('Reused091 LP receipt differs from actual stream')
        if (not isinstance(correction.get('coefficient_sha256'), str)
                or base.HEX.fullmatch(correction['coefficient_sha256']) is None
                or type(correction.get('lz77_enabled')) is not bool
                or type(correction.get('tree_nodes')) is not int
                or not 1 <= correction['tree_nodes'] <= 4095 or correction['tree_nodes'] % 2 != 1):
            raise ValueError('Reused091 LP model receipt invalid')
        costs['correction_stream_bytes'] += stream['bytes']
    costs['record_header_bytes_per_tile'] = 260
    if receipt.get('costs') != costs:
        raise ValueError('Native091 costs differ from actual paid records')
    return receipt

def _assemble(catalog, census, direct, native, target, profile, runtime_pins, direct_pins, old_pins):
    target.mkdir(); descriptors = list(base._descriptors(catalog['levels']))
    family = profile.split('_')[0]; routes = [JXL]*len(descriptors)
    for gid, level, *_ in descriptors:
        if family != 'direct11' and level == 0:
            candidate = native/('control' if family == 'control' else 'conditional')/f'{gid:06d}.bin'
            if profile.endswith('_pure') or candidate.stat().st_size < (direct/f'{gid:06d}.bin').stat().st_size:
                routes[gid] = CONTROL if family == 'control' else CONDITIONAL
    shutil.copyfile(census/'source.json', target/'source.json')
    if sha256(target/'source.json') != catalog['files']['source.json']['sha256']:
        raise ValueError('091 source metadata changed during assembly')
    with (target/'images.bank').open('xb') as bank, (target/'images.index').open('xb') as index:
        index.write(HEADER.pack(MAGIC, TILE, len(descriptors)))
        for gid, *_ in descriptors:
            route = routes[gid]
            path = direct/f'{gid:06d}.bin' if route == JXL else native/('control' if route == CONTROL else 'conditional')/f'{gid:06d}.bin'
            data = _read_bounded(path, base.MAX_PAYLOAD if route == JXL else MAX_PAYLOAD)
            index.write(ENTRY.pack(bank.tell(), len(data), route, zlib.crc32(data))); bank.write(data)
    if family != 'direct11':
        targets = [d for d in descriptors if d[1] == 0]
        with (target/'anchors.index').open('xb') as index:
            index.write(HEADER.pack(ANCHOR_MAGIC, TILE, len(targets)))
            for desc in targets:
                aid = _anchor_layout(catalog['levels'], desc)[0] if routes[desc[0]] != JXL else NO_ANCHOR
                index.write(ANCHOR_ENTRY.pack(aid))
    route_counts = {'jpegxl': routes.count(JXL), 'control': routes.count(CONTROL), 'conditional': routes.count(CONDITIONAL)}
    manifest = {'format': FORMAT, 'profile': profile, 'image_only': True,
                'image': {'path': 'images.bank', 'index': 'images.index', 'source_metadata': 'source.json',
                          'anchor_index': 'anchors.index' if family != 'direct11' else None,
                          'levels': copy.deepcopy(catalog['levels'])}, 'parameters': FIXED,
                'files': base._members(target), 'routes': route_counts,
                'build': {'native_runtime': runtime_pins, 'source_census_sha256': sha256(census/'catalog.json'),
                          'reused_direct': direct_pins, 'reused_correction_source': old_pins,
                          'direct_timing': 'Reused090 JXL11 streams, not freshly timed encoding',
                          'archive_source_sha256': sha256(Path(__file__)),
                          'native_recipe': 'Identical089 LP correction; HF known-parent gate zero versus enabled'}}
    write_json(target/'manifest.json', manifest)
    return route_counts


def build(censusdir, direct090dir, anchor089dir, output, workspace, runtime_dir=None):
    """Build five actual complete packages using shared native encodes and reused JXL."""
    started = time.perf_counter(); census, direct_root = Path(censusdir).resolve(), Path(direct090dir).resolve()
    old_root = Path(anchor089dir).resolve()
    out = _disjoint(output, census, direct_root, old_root)
    stage = _disjoint(out.with_name(out.name+'.partial'), census, direct_root, old_root)
    work = _disjoint(workspace, census, direct_root, old_root, out, stage)
    stage.mkdir(parents=True); (stage/'INCOMPLETE').write_text('091 portfolio\n', encoding='utf8')
    try:
        runtime, runtime_pins = _runtime(runtime_dir); work.mkdir(parents=True)
        then = time.perf_counter(); catalog = base._catalog(census); _anchor_geometry(catalog['levels'])
        catalog_pin = sha256(census/'catalog.json'); descriptors = list(base._descriptors(catalog['levels']))
        direct_pins = base._members(direct_root); old_pins = base._members(old_root)
        with reference090.Reader(direct_root) as direct_reader:
            if direct_reader.manifest['profile'] != 'direct11' or direct_reader.manifest['image']['levels'] != catalog['levels']:
                raise ValueError('091 reused direct differs from native scope')
            if direct_reader.manifest['files']['source.json'] != catalog['files']['source.json']:
                raise ValueError('091 reused direct differs from native provenance')
            direct_reader._verify_files(); direct_reader._setup(); reused = work/'direct'; reused.mkdir()
            for gid, *_ in descriptors:
                offset, length, route, crc = direct_reader._entries[gid]
                if route != reference090.JXL:
                    raise ValueError('091 reference must contain only direct JXL')
                direct_reader._bank.seek(offset); data = direct_reader._bank.read(length)
                if len(data) != length or zlib.crc32(data) != crc:
                    raise ValueError('091 reused direct corrupt')
                (reused/f'{gid:06d}.bin').write_bytes(data)
        old_records = work/'old089'; old_records.mkdir()
        with previous089.Reader(old_root) as old_reader:
            if old_reader.manifest['profile'] != 'anchor_pure' or old_reader.manifest['image']['levels'] != catalog['levels']:
                raise ValueError('091 reused correction differs from frozen anchor-pure scope')
            if old_reader.manifest['files']['source.json'] != catalog['files']['source.json']:
                raise ValueError('091 reused correction differs from native provenance')
            old_reader._verify_files(); old_reader._setup()
            for gid, level, _, _, w, h in descriptors:
                if level != 0:
                    continue
                offset, length, route, crc = old_reader._entries[gid]
                if route != previous089.ANCHORED:
                    raise ValueError('091 old L0 record must use089 anchor correction')
                old_reader._bank.seek(offset); data = old_reader._bank.read(length)
                if len(data) != length or zlib.crc32(data) != crc:
                    raise ValueError('091 reused old089 record corrupt')
                view = previous089._record(data, gid, w, h, 1)
                if view['raw_sha256'] != catalog['tiles'][gid]['native_sha256']:
                    raise ValueError('091 old089 source pixels differ')
                (old_records/f'{gid:06d}.old089').write_bytes(data)
        input_seconds = time.perf_counter()-then
        then = time.perf_counter(); spool = work/'spool'; spool.mkdir(); anchors = {}
        with (spool/'index.tsv').open('x', encoding='ascii', newline='\n') as index:
            for desc in descriptors:
                gid, level, x, y, w, h = desc
                if level != 0:
                    continue
                pixels = scan.read_tile(census, gid, catalog=catalog)
                anchor, _ = _anchor(catalog['levels'], desc, lambda aid: scan.read_tile(census, aid, catalog=catalog))
                (spool/f'{gid:06d}.rgb').write_bytes(pixels.tobytes())
                data = anchor.tobytes(); anchors[gid] = _hash(data)
                (spool/f'{gid:06d}.anchor').write_bytes(data)
                old_blob = _read_bounded(old_records/f'{gid:06d}.old089', previous089.MAX_PAYLOAD)
                if previous089._record(old_blob, gid, w, h, 1)['anchor_sha256'] != anchors[gid]:
                    raise ValueError('091 old089 anchor differs from current exact nativeL1')
                (spool/f'{gid:06d}.old089').write_bytes(old_blob)
                index.write(f'{gid}\t{level}\t{x}\t{y}\t{w}\t{h}\t{gid:06d}.rgb\t{gid:06d}.anchor\t{gid:06d}.old089\n')
                if gid % 64 == 0:
                    print(json.dumps({'stage': '091_spool', 'gid': gid}), flush=True)
        spool_seconds = time.perf_counter()-then; native = work/'native'
        native_seconds = _native_encode(runtime, spool, native)
        then = time.perf_counter(); receipt = _validate_native(native, catalog, descriptors, anchors, old_records)
        receipt_seconds = time.perf_counter()-then; results = {}
        for profile in PROFILES:
            then = time.perf_counter()
            routes = _assemble(catalog, census, reused, native, stage/profile, profile, runtime_pins, direct_pins, old_pins)
            assembly_seconds = time.perf_counter()-then; then = time.perf_counter()
            with Reader(stage/profile, runtime) as reader:
                verification = reader.verify(); ledger = reader.ledger()
            results[profile] = {'ledger': ledger, 'members': base._members(stage/profile), 'routes': routes,
                                'verified': verification,
                                'timing': {'assembly_seconds': assembly_seconds, 'verification_seconds': time.perf_counter()-then}}
            print(json.dumps({'stage': '091_verified', 'profile': profile, 'bytes': ledger['total_package_bytes']}), flush=True)
        winner = min(PROFILES, key=lambda p: results[p]['ledger']['total_package_bytes'])
        shutil.copytree(stage/winner, stage/'best')
        if base._members(stage/'best') != results[winner]['members']:
            raise ValueError('091 best copy differs')
        if (base._members(direct_root) != direct_pins or base._members(old_root) != old_pins
                or sha256(census/'catalog.json') != catalog_pin
                or _runtime(runtime)[1] != runtime_pins):
            raise ValueError('091 frozen input/runtime changed during build')
        (stage/'INCOMPLETE').unlink(); stage.rename(out)
        return {'ok': True, 'profiles': results, 'best_profile': winner, 'winner': winner,
                'winning_package_bytes': results[winner]['ledger']['total_package_bytes'],
                'saving_bytes': results['direct11']['ledger']['total_package_bytes']-results[winner]['ledger']['total_package_bytes'],
                'native_receipt': receipt, 'runtime': runtime_pins,
                'timing': {'input_verification_and_direct_and_correction_reuse_seconds': input_seconds,
                           'native_spool_seconds': spool_seconds, 'native_cli_seconds': native_seconds,
                           'native_receipt_validation_seconds': receipt_seconds,
                           'total_portfolio_seconds': time.perf_counter()-started,
                           'scope': 'Shared-work finite portfolio including exact verification; reused090 direct and089 LP are not fresh encode timing'},
                'scope': 'All native image levels/source metadata; L0-only candidate; Count absent; runtime/scratch separate'}
    except BaseException as exc:
        write_json(stage/'failure.json', {'type': type(exc).__name__, 'error': str(exc)})
        raise


class Reader(base.Reader):
    """Query-local target+full-anchor cache; lower levels never depend on L0."""
    def __init__(self, directory, runtime_dir=None):
        arg = Path(directory); self.root = arg.resolve(); path = self.root/'manifest.json'
        if arg.is_symlink() or path.is_symlink() or (self.root/'INCOMPLETE').exists() or (self.root/'failure.json').exists():
            raise ValueError('Unsafe or incomplete091 archive')
        blob = _read_bounded(path, base.MAX_MANIFEST_BYTES); self.manifest_bytes = len(blob)
        self.manifest = m = json.loads(blob)
        if m.get('format') != FORMAT or m.get('profile') not in PROFILES or m.get('image_only') is not True:
            raise ValueError('Unsupported091 archive')
        if m.get('parameters') != FIXED or any(type(m['parameters'][k]) is not type(v) for k, v in FIXED.items()):
            raise ValueError('Unsupported091 fixed parameters')
        self.family = m['profile'].split('_')[0]
        self._descriptors = list(base._descriptors(m['image']['levels']))
        _anchor_geometry(m['image']['levels'])
        self._l0_count = sum(d[1] == 0 for d in self._descriptors)
        expected = BASE_MEMBERS | ({'anchors.index'} if self.family != 'direct11' else set())
        if (set(m['files']) != expected or {p.name for p in self.root.iterdir()} != expected | {'manifest.json'}
                or m['image'].get('path') != 'images.bank' or m['image'].get('index') != 'images.index'
                or m['image'].get('source_metadata') != 'source.json'
                or m['image'].get('anchor_index') != ('anchors.index' if self.family != 'direct11' else None)):
            raise ValueError('Unlisted/missing091 member or route')
        for name, info in m['files'].items():
            member = self.root/name
            if (not _name(name) or member.is_symlink() or not member.is_file()
                    or type(info.get('bytes')) is not int or info['bytes'] < 0
                    or member.stat().st_size != info['bytes'] or not isinstance(info.get('sha256'), str)
                    or base.HEX.fullmatch(info['sha256']) is None):
                raise ValueError('Unsafe091 member/size/hash')
        if (m['files']['source.json']['bytes'] > scan.MAX_SOURCE_METADATA_BYTES
                or m['files']['images.index']['bytes'] != HEADER.size+ENTRY.size*len(self._descriptors)
                or ('anchors.index' in expected and m['files']['anchors.index']['bytes'] != HEADER.size+ANCHOR_ENTRY.size*self._l0_count)):
            raise ValueError('091 metadata/index bound')
        self._runtime_argument = runtime_dir; self._native = None; self._closed = False
        self._entries = None; self._bank = None; self._level_start = []; self._source_metadata = None
        self.index_read_bytes = self.anchor_index_read_bytes = 0
        self.jxl_tiles_decoded = self.constant_tiles_filled = self.logical_tiles_read = 0
        self.decoded_pixels = self.requested_pixels = self.cache_hits = 0
        self.last_query_cache_bytes = self.last_query_unique_payloads = 0
        self.native_tiles_decoded = self.anchor_requests = self.anchor_cache_hits = 0
        self.anchor_tiles_decoded = self.anchor_decoded_pixels = self.anchor_decoded_bytes = 0
        self.anchor_bank_read_bytes = self.anchor_cropped_bytes = 0
        self.native_decode_seconds = self.runtime_load_seconds = 0.0

    def _member(self, name, limit):
        blob = _read_bounded(self.root/name, limit); info = self.manifest['files'][name]
        if len(blob) != info['bytes'] or _hash(blob) != info['sha256']:
            raise ValueError('091 member integrity: ' + name)
        return blob

    def _setup(self):
        if self._closed:
            raise ValueError('091 Reader is closed')
        if self._entries is not None:
            return
        data = self._member('images.index', HEADER.size+base.MAX_TILES*ENTRY.size)
        self.index_read_bytes += len(data)
        if HEADER.unpack_from(data) != (MAGIC, TILE, len(self._descriptors)):
            raise ValueError('091 index header differs')
        entries = [ENTRY.unpack_from(data, HEADER.size+i*ENTRY.size) for i in range(len(self._descriptors))]
        end = 0; counts = dict.fromkeys(('jpegxl', 'control', 'conditional'), 0)
        for gid, ((offset, length, route, _), desc) in enumerate(zip(entries, self._descriptors)):
            allowed = {JXL} if self.family == 'direct11' or desc[1] != 0 else {CONTROL if self.family == 'control' else CONDITIONAL}
            if self.manifest['profile'].endswith('_hybrid'):
                allowed.add(JXL)
            limit = base.MAX_PAYLOAD if route == JXL else MAX_PAYLOAD
            if (data[HEADER.size+gid*ENTRY.size+13:HEADER.size+gid*ENTRY.size+16] != b'\0'*3
                    or offset != end or not 0 < length <= limit or route not in allowed):
                raise ValueError('091 invalid independent span/route or lower-level dependency chain')
            end += length; counts[{JXL: 'jpegxl', CONTROL: 'control', CONDITIONAL: 'conditional'}[route]] += 1
        if (end != self.manifest['files']['images.bank']['bytes'] or counts != self.manifest.get('routes')
                or any(type(self.manifest['routes'].get(k)) is not int for k in counts)):
            raise ValueError('091 trailing/missing bank or route count')
        if self.family != 'direct11':
            data = self._member('anchors.index', HEADER.size+ANCHOR_ENTRY.size*self._l0_count)
            self.anchor_index_read_bytes += len(data)
            if HEADER.unpack_from(data) != (ANCHOR_MAGIC, TILE, self._l0_count):
                raise ValueError('091 anchor directory header')
            for desc in self._descriptors[:self._l0_count]:
                gid = desc[0]; expected = _anchor_layout(self.manifest['image']['levels'], desc)[0] if entries[gid][2] != JXL else NO_ANCHOR
                if ANCHOR_ENTRY.unpack_from(data, HEADER.size+gid*ANCHOR_ENTRY.size)[0] != expected:
                    raise ValueError('091 anchor dependency mapping differs')
                if expected != NO_ANCHOR and (self._descriptors[expected][1] != 1 or entries[expected][2] != JXL):
                    raise ValueError('091 anchor must be independent L1 JXL, no chains')
        self._entries = entries
        self._level_start = [next(d[0] for d in self._descriptors if d[1] == info['level']) for info in self.manifest['image']['levels']]
        self._bank = MeteredFile(self.root/'images.bank')

    def _tile(self, gid, cache=None, cache_state=None):
        import imagecodecs
        self._setup()
        if not base._integer(gid) or not 0 <= gid < len(self._entries):
            raise ValueError('091 tile outside archive')
        self.logical_tiles_read += 1
        if cache is not None and gid in cache:
            self.cache_hits += 1
            return cache[gid]
        offset, length, route, crc = self._entries[gid]
        self._bank.seek(offset); blob = self._bank.read(length)
        if len(blob) != length or zlib.crc32(blob) != crc:
            raise ValueError('091 tile payload integrity')
        w, h = self._descriptors[gid][4:]
        if route == JXL:
            pixels = imagecodecs.jpegxl_decode(blob, numthreads=1, out=np.empty((h, w, 3), np.uint8))
            self.jxl_tiles_decoded += 1
        else:
            _record(blob, gid, w, h, 1 if route == CONDITIONAL else 0)
            anchor = None
            if route != JXL:
                aid, ys, xs = _anchor_layout(self.manifest['image']['levels'], self._descriptors[gid])
                self.anchor_requests += 1
                cached = cache is not None and aid in cache
                if cached:
                    self.anchor_cache_hits += 1
                before_pixels = self.decoded_pixels; before_bytes = self._bank.metrics()['file_read_bytes']
                full_anchor = self._tile(aid, cache, cache_state)
                self.anchor_bank_read_bytes += self._bank.metrics()['file_read_bytes']-before_bytes
                self.anchor_decoded_pixels += self.decoded_pixels-before_pixels
                self.anchor_decoded_bytes += 3*(self.decoded_pixels-before_pixels)
                self.anchor_tiles_decoded += not cached
                anchor = np.ascontiguousarray(full_anchor[ys[:, None], xs[None, :]])
                self.anchor_cropped_bytes += anchor.nbytes
            if self._native is None:
                then = time.perf_counter(); self._native = NativeRuntime(self._runtime_argument, self.manifest['build']['native_runtime'])
                self.runtime_load_seconds += time.perf_counter()-then
            then = time.perf_counter(); pixels = self._native.decode(blob, w, h, gid, anchor)
            self.native_decode_seconds += time.perf_counter()-then; self.native_tiles_decoded += 1
        if pixels.dtype != np.uint8 or pixels.shape != (h, w, 3):
            raise ValueError('091 decoded native shape/type')
        self.decoded_pixels += w*h
        if cache_state is not None:
            cache_state['decoded'].add(gid)
            if cache_state['bytes']+pixels.nbytes <= MAX_QUERY_CACHE_BYTES:
                cache[gid] = pixels; cache_state['bytes'] += pixels.nbytes
        return pixels

    def close(self):
        if getattr(self, '_closed', True):
            return
        super().close(); self._native = None; self._closed = True

    def metrics(self):
        result = super().metrics()
        result.update(anchor_index_read_bytes=self.anchor_index_read_bytes,
                      native_tiles_decoded=self.native_tiles_decoded, anchor_requests=self.anchor_requests,
                      anchor_cache_hits=self.anchor_cache_hits, anchor_tiles_decoded=self.anchor_tiles_decoded,
                      anchor_decoded_pixels=self.anchor_decoded_pixels, anchor_decoded_bytes=self.anchor_decoded_bytes,
                      anchor_bank_read_bytes=self.anchor_bank_read_bytes, anchor_cropped_bytes=self.anchor_cropped_bytes,
                      target_decoded_pixels=self.decoded_pixels-self.anchor_decoded_pixels,
                      native_decode_seconds=self.native_decode_seconds, runtime_load_seconds=self.runtime_load_seconds,
                      dependency='Both native L0 routes have one-hop full nativeL1 JXL11 tile; lower levels independent',
                      cache_scope='Combined target+full-anchor pixels <=8MiB per query; cleared after query; cropped anchor is transient',
                      model_scope='Models inside each record; no persistent native model handles; process RSS includes transient models')
        return result


WSIConditionedArchive = Reader
