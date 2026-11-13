"""Seven fixed HF/L1 profiles over the previous whole-slide champion.

091 at native L1 uses level-local IDs, not colliding global L0 identities.
Only the two candidate axes change; all native L>=2 are independent JXL9.
"""
import copy
from collections import OrderedDict
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import sys
import time
import zlib

import numpy as np
from stbaseline import slide_redundancy as base
from stbaseline import slide_redundancy_scan as scan
from stbaseline import wsi_squeeze089 as old
from stbaseline import wsi_conditioned091 as cond
from stbaseline.integrity import sha256, write_json
from stbaseline.reader import MeteredFile


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prior = _load('_prior_container_for_hf_l1', Path(__file__).with_name('joint.py'))
_hf_loaded = None


def _hf():
    global _hf_loaded
    if _hf_loaded is None:
        _hf_loaded = _load('_hf_adapter_for_joint_next', Path(__file__).with_name('hf_adapter.py'))
    return _hf_loaded


FORMAT = 'wsi-hf-l1-compat-20261102-v1'
MAGIC, AMAGIC = b'WSIHL101', b'HL1ANC01'
HEADER, ENTRY, AENTRY = base.HEADER, base.ENTRY, struct.Struct('<I')
JXL, ANCHORED089, CONTROL091, CONDITIONAL091, HF = 1, 3, 4, 5, 6
NO_ANCHOR = 0xffffffff
MEMBERS = {'images.bank', 'images.index', 'anchors.index', 'source.json'}
PROFILES = ('direct', 'retained', 'hf_only', 'l1_control', 'l1_conditional',
            'hf_l1_control', 'hf_l1_conditional')
FIXED = {
    'tile': 256, 'jpegxl_effort': 9, 'threads': 1, 'training': False,
    'HF': 'One fixed MS representation/reference construction on L0; frozen new runtime',
    'L1': 'Unchanged091 known-parent gate, same backend and paid089 correction',
    'gate_control': 'Parent properties zero; parent-derived thresholds and LL retained',
    'gate_conditional': 'Known-parent reference properties enabled',
    'max_depth': 2, 'independent_levels': 'all native levels >= 2',
    'native_record_id': 'gid minus true native level start; ABI virtual level0',
    'L1_selection': 'min currentP, directJXL9, allP089 anchored, named091; ties currentP',
    'L0_selection': 'min retained joint-champion record, fixedMS record; ties retained',
    'cache_bytes': 8*1024*1024, 'persistent_pixel_cache': False,
    'image_only': True, 'standard_jxl': False,
}
digest, _plain = prior.digest, prior._plain
starts, geometry = prior.starts, prior.geometry
local_id, anchor_layout, anchor_pixels = prior.local_id, prior.anchor_layout, prior.anchor_pixels


def axes(profile):
    if profile not in PROFILES:
        raise ValueError('Unknown HF/L1 profile')
    high = profile == 'hf_only' or profile.startswith('hf_l1_')
    low = None if profile in ('direct', 'retained', 'hf_only') else int(profile.endswith('conditional'))
    return high, low


def _limit(route):
    if route == JXL: return base.MAX_PAYLOAD
    if route == ANCHORED089: return old.MAX_PAYLOAD
    if route in (CONTROL091, CONDITIONAL091): return cond.MAX_PAYLOAD
    if route == HF: return _hf().MAX_PAYLOAD
    raise ValueError('Unknown HF/L1 route')


def validate_blob(route, blob, levels, desc):
    if type(route) is not int or not isinstance(blob, bytes) or not 0 < len(blob) <= _limit(route):
        raise ValueError('Record type/size bound')
    gid, level, x, y, w, h = desc
    if route == JXL: return None
    if level not in (0, 1): raise ValueError('Native record below L1 forbidden')
    rid = local_id(levels, desc)
    if route == ANCHORED089: return old._record(blob, rid, w, h, 1)
    if route in (CONTROL091, CONDITIONAL091): return cond._record(blob, rid, w, h, int(route == CONDITIONAL091))
    if level != 0: raise ValueError('HF MS only supported on native L0')
    return _hf()._record(blob, gid, w, h)


def choose(current, candidates):
    """Strictly smaller complete record only; preserve previous choice on ties."""
    winner = current
    for candidate in candidates:
        if len(candidate[1]) < len(winner[1]): winner = candidate
    return winner


def _record_array(catalog, direct, retained, *, fixed_l1=False):
    levels = catalog['levels']; geometry(levels)
    descs = list(base._descriptors(levels))
    arrays = (retained,) if direct is None else (retained, direct)
    if any(not isinstance(v, (list, tuple)) or len(v) != len(descs) for v in arrays):
        raise ValueError('Complete record arrays required')
    for gid, (desc, r) in enumerate(zip(descs, retained)):
        d = direct[gid] if direct is not None else None
        records = (r,) if direct is None else (d, r)
        if any(not isinstance(v, (tuple, list)) or len(v) != 2 for v in records):
            raise ValueError('Expected (route,bytes)')
        level = desc[1]
        allowed = {JXL, ANCHORED089, CONDITIONAL091} if level == 0 else ({JXL, ANCHORED089} if level == 1 else {JXL})
        if fixed_l1 and level == 1:
            allowed.add(CONDITIONAL091)
        if r[0] not in allowed or (d is not None and d[0] != JXL):
            raise ValueError('Invalid retained champion route')
        for route, blob in records:
            view = validate_blob(route, blob, levels, desc)
            if view is not None and view['raw_sha256'] != catalog['tiles'][desc[0]]['native_sha256']:
                raise ValueError('Retained record source identity differs')
        if d is not None and r[0] == JXL and tuple(r) != tuple(d):
            raise ValueError('Retained JXL bytes changed')
    return descs


def write_package(catalog, census, direct, retained, hf_records, l1_native_dir,
                  oldL1native, target, profile, runtime_pins):
    """Fresh complete package; oldL1native is the P native root (anchored/...).

    hf_records maps each global L0 gid to its new route6 candidate. L1 records
    are control|conditional/<local_gid>.bin from l1_adapter.build_native.
    With no direct candidates, retained or l1_conditional may supply final records.
    The existing l1_conditional reader gets the final L1 route from the index.
    """
    high, low = axes(profile)
    fixed_l1 = direct is None and profile == 'l1_conditional'
    if direct is None and profile != 'retained' and not fixed_l1:
        raise ValueError('This profile requires direct candidates')
    if fixed_l1 and any(value is not None for value in (hf_records, l1_native_dir, oldL1native)):
        raise ValueError('Fixed L1 packaging takes only final selected records')
    levels = catalog['levels']; descs = _record_array(catalog, direct, retained, fixed_l1=fixed_l1)
    n0, n01 = starts(levels)[1:3]
    if set(runtime_pins) != {'089', '091', 'hf'} or any(not isinstance(v, dict) or not v for v in runtime_pins.values()):
        raise ValueError('Three runtime pin maps required')
    if high and (not isinstance(hf_records, dict) or any(type(k) is not int for k in hf_records) or set(hf_records) != set(range(n0))):
        raise ValueError('Complete global L0 HF candidate map required')
    census = _plain(census, directory=True)
    metadata = _plain(census/'source.json'); expected = catalog['files']['source.json']
    if metadata.stat().st_size != expected['bytes'] or metadata.stat().st_size > scan.MAX_SOURCE_METADATA_BYTES or sha256(metadata) != expected['sha256']:
        raise ValueError('Frozen source metadata changed')
    inputs = [census]
    if low is not None and not fixed_l1:
        l1_native_dir, oldL1native = _plain(l1_native_dir, directory=True), _plain(oldL1native, directory=True)
        inputs.extend((l1_native_dir, oldL1native))
    target = _plain(target)
    if target.exists() or any(target == p or p in target.parents or target in p.parents for p in inputs):
        raise ValueError('Fresh disjoint package target required')
    target.mkdir(); marker = target/'INCOMPLETE'; marker.write_text('HF/L1 package assembly\n', encoding='utf8')
    try:
        routes, selected_hf, selected_l1 = [], [], []
        with (target/'images.bank').open('xb') as bank, (target/'images.index').open('xb') as index:
            index.write(HEADER.pack(MAGIC, 256, len(descs)))
            for desc in descs:
                gid, level, x, y, w, h = desc
                selected = direct[gid] if profile == 'direct' else retained[gid]
                if level == 0 and high:
                    candidate = hf_records[gid]
                    if not isinstance(candidate, (tuple, list)) or len(candidate) != 2 or candidate[0] != HF:
                        raise ValueError('HF map requires route6 complete records')
                    view = validate_blob(*candidate, levels, desc)
                    if view['raw_sha256'] != catalog['tiles'][gid]['native_sha256']:
                        raise ValueError('HF candidate source binding')
                    selected = choose(selected, [candidate])
                    if selected[0] == HF: selected_hf.append(gid)
                if level == 1 and low is not None and not fixed_l1:
                    rid = local_id(levels, desc)
                    oldblob = old._read_bounded(_plain(oldL1native/'anchored'/f'{rid:06d}.bin'), old.MAX_PAYLOAD)
                    oldview = validate_blob(ANCHORED089, oldblob, levels, desc)
                    family = 'conditional' if low else 'control'
                    route = CONDITIONAL091 if low else CONTROL091
                    blob = old._read_bounded(_plain(l1_native_dir/family/f'{rid:06d}.bin'), cond.MAX_PAYLOAD)
                    view = validate_blob(route, blob, levels, desc)
                    if (view['raw_sha256'] != catalog['tiles'][gid]['native_sha256']
                            or view['raw_sha256'] != oldview['raw_sha256']
                            or view['anchor_sha256'] != oldview['anchor_sha256']
                            or view['low']['frame'] != oldblob[-48-oldview['low']['bytes']:]):
                        raise ValueError('L1 source/parent/correction reuse differs')
                    selected = choose(selected, [direct[gid], (ANCHORED089, oldblob), (route, blob)])
                    if selected[0] == route: selected_l1.append(gid)
                route, blob = selected
                if fixed_l1 and level == 1 and route == CONDITIONAL091:
                    selected_l1.append(gid)
                validate_blob(route, blob, levels, desc)
                routes.append(route)
                index.write(ENTRY.pack(bank.tell(), len(blob), route, zlib.crc32(blob))); bank.write(blob)
        with (target/'anchors.index').open('xb') as handle:
            handle.write(HEADER.pack(AMAGIC, 256, n01))
            for desc in descs[:n01]:
                handle.write(AENTRY.pack(anchor_layout(levels, desc)[0] if routes[desc[0]] != JXL else NO_ANCHOR))
        shutil.copyfile(metadata, target/'source.json')
        if sha256(target/'source.json') != expected['sha256']: raise ValueError('Copied metadata changed')
        counts = {str(route): routes.count(route) for route in (JXL, ANCHORED089, CONTROL091, CONDITIONAL091, HF)}
        manifest = {'format': FORMAT, 'profile': profile, 'image_only': True, 'parameters': FIXED,
                    'image': {'path': 'images.bank', 'index': 'images.index', 'anchor_index': 'anchors.index',
                              'source_metadata': 'source.json', 'levels': copy.deepcopy(levels)},
                    'retained_routes': [
                        JXL if fixed_l1 and desc[1] == 1 and record[0] == CONDITIONAL091 else record[0]
                        for desc, record in zip(descs, retained)
                    ], 'routes': counts,
                    'files': {name: {'bytes': (target/name).stat().st_size, 'sha256': sha256(target/name)} for name in sorted(MEMBERS)},
                    'build': {'native_runtimes': copy.deepcopy(runtime_pins), 'wrapper_sha256': sha256(Path(__file__)),
                              'source_census_sha256': sha256(_plain(census/'catalog.json')),
                              'timing_scope': ('Fixed selected records; encode stages timed by caller' if direct is None else
                                               'Incremental HF/L1 candidates; reused old encoding times historical')}}
        write_json(target/'manifest.json', manifest); marker.unlink()
        return {'new_hf_L0': selected_hf, 'new_091_L1': selected_l1, 'routes': counts}
    except BaseException as exc:
        write_json(target/'failure.json', {'type': type(exc).__name__, 'error': str(exc)}); raise


class _ScanReferenceCache:
    """Bound decoded parent pixels for one sequential native-level scan."""

    def __init__(self):
        self.tiles = OrderedDict()
        self.bytes = 0

    def get(self, gid):
        pixels = self.tiles.get(gid)
        if pixels is not None:
            self.tiles.move_to_end(gid)
        return pixels

    def put(self, gid, pixels):
        while self.bytes + pixels.nbytes > FIXED['cache_bytes']:
            _, oldest = self.tiles.popitem(last=False)
            self.bytes -= oldest.nbytes
        self.tiles[gid] = pixels
        self.bytes += pixels.nbytes


class Reader(prior.Reader):
    def __init__(self, directory, r089=None, r091=None, rhf=None):
        self.root = _plain(directory, directory=True)
        if any((self.root/name).exists() for name in ('INCOMPLETE', 'failure.json')): raise ValueError('Incomplete HF/L1 package')
        blob = old._read_bounded(_plain(self.root/'manifest.json'), base.MAX_MANIFEST_BYTES)
        self.manifest_bytes = len(blob); self.manifest = m = json.loads(blob)
        if m.get('format') != FORMAT or m.get('parameters') != FIXED or m.get('image_only') is not True: raise ValueError('Unsupported HF/L1 parameters')
        self._high, self._low = axes(m.get('profile'))
        geometry(m['image']['levels']); self._descriptors = list(base._descriptors(m['image']['levels']))
        self._level_start = starts(m['image']['levels'])
        if set(m['files']) != MEMBERS or {p.name for p in self.root.iterdir()} != MEMBERS | {'manifest.json'}: raise ValueError('Unexpected members')
        if {k: m['image'].get(k) for k in ('path', 'index', 'anchor_index', 'source_metadata')} != dict(path='images.bank', index='images.index', anchor_index='anchors.index', source_metadata='source.json'): raise ValueError('Member paths')
        for name, info in m['files'].items():
            path = _plain(self.root/name)
            if (not path.is_file() or type(info.get('bytes')) is not int or info['bytes'] < 0
                    or path.stat().st_size != info['bytes'] or not isinstance(info.get('sha256'), str)
                    or base.HEX.fullmatch(info['sha256']) is None): raise ValueError('Member identity')
        if m['files']['source.json']['bytes'] > scan.MAX_SOURCE_METADATA_BYTES: raise ValueError('Metadata bound')
        retained = m.get('retained_routes')
        if not isinstance(retained, list) or len(retained) != len(self._descriptors): raise ValueError('Retained route coverage')
        for route, desc in zip(retained, self._descriptors):
            allowed = {JXL, ANCHORED089, CONDITIONAL091} if desc[1] == 0 else ({JXL, ANCHORED089} if desc[1] == 1 else {JXL})
            if type(route) is not int or route not in allowed: raise ValueError('Retained route identity')
        pins = m.get('build', {}).get('native_runtimes')
        if not isinstance(pins, dict) or set(pins) != {'089', '091', 'hf'} or any(not isinstance(v, dict) or not v for v in pins.values()): raise ValueError('Runtime pins')
        self._entries = self._bank = self._source_metadata = None
        self._native089 = self._native091 = self._native_hf = None
        self._runtime089, self._runtime091, self._runtime_hf = r089, r091, rhf
        self._closed = False
        for name in ('index_read_bytes', 'anchor_index_read_bytes', 'jxl_tiles_decoded', 'constant_tiles_filled',
                     'logical_tiles_read', 'decoded_pixels', 'requested_pixels', 'cache_hits',
                     'last_query_cache_bytes', 'last_query_unique_payloads', 'reference_tiles_decoded',
                     'reference_pixels', 'reference_bank_bytes', 'native_tiles_decoded', 'native089_tiles_decoded',
                     'native091_tiles_decoded', 'native_hf_tiles_decoded', 'anchor_requests', 'anchor_cache_hits',
                     'max_observed_depth', 'scan_parent_cache_peak_bytes'):
            setattr(self, name, 0)
        self.runtime_load_seconds = self.native_decode_seconds = 0.

    def _allowed(self, desc):
        gid, level = desc[:2]
        if self.manifest['profile'] == 'direct' or level >= 2: return {JXL}
        retained = self.manifest['retained_routes'][gid]
        if level == 0: return {retained, HF} if self._high else {retained}
        return {JXL, ANCHORED089, CONDITIONAL091 if self._low else CONTROL091} if self._low is not None else {retained}

    def _setup(self):
        if self._closed: raise ValueError('Closed HF/L1 reader')
        if self._entries is not None: return
        n, targets = len(self._descriptors), self._level_start[2]
        data = self._member('images.index', HEADER.size+n*ENTRY.size)
        if len(data) != HEADER.size+n*ENTRY.size or HEADER.unpack_from(data) != (MAGIC, 256, n): raise ValueError('Image index header/coverage')
        self.index_read_bytes += len(data)
        entries = [ENTRY.unpack_from(data, HEADER.size+i*ENTRY.size) for i in range(n)]
        end, counts = 0, {str(r): 0 for r in (JXL, ANCHORED089, CONTROL091, CONDITIONAL091, HF)}
        for gid, (entry, desc) in enumerate(zip(entries, self._descriptors)):
            offset, length, route, crc = entry
            if (route not in self._allowed(desc) or offset != end or not 0 < length <= _limit(route)
                    or data[HEADER.size+gid*ENTRY.size+13:HEADER.size+gid*ENTRY.size+16] != b'\0'*3): raise ValueError('Route/span/reserved field')
            end += length; counts[str(route)] += 1
        if end != self.manifest['files']['images.bank']['bytes'] or counts != self.manifest.get('routes'): raise ValueError('Bank coverage/route counts')
        anchors = self._member('anchors.index', HEADER.size+targets*AENTRY.size)
        if len(anchors) != HEADER.size+targets*AENTRY.size or HEADER.unpack_from(anchors) != (AMAGIC, 256, targets): raise ValueError('Anchor index header/coverage')
        self.anchor_index_read_bytes += len(anchors)
        for desc in self._descriptors[:targets]:
            gid, level = desc[:2]
            aid = AENTRY.unpack_from(anchors, HEADER.size+gid*AENTRY.size)[0]
            expected = anchor_layout(self.manifest['image']['levels'], desc)[0] if entries[gid][2] != JXL else NO_ANCHOR
            if aid != expected: raise ValueError('Anchor identity')
            if aid != NO_ANCHOR and (self._descriptors[aid][1] != level+1 or (level == 1 and entries[aid][2] != JXL)): raise ValueError('Reference DAG')
        self._entries = entries; self._bank = MeteredFile(_plain(self.root/'images.bank'))

    def _tile(self, gid, cache=None, cache_state=None, depth=0, *, scan_cache=None):
        import imagecodecs
        self._setup()
        if not base._integer(gid) or not 0 <= gid < len(self._entries) or type(depth) is not int or not 0 <= depth <= 2: raise ValueError('Tile/depth bound')
        self.max_observed_depth = max(self.max_observed_depth, depth); self.logical_tiles_read += 1
        if cache is not None and gid in cache:
            self.cache_hits += 1; return cache[gid]
        if depth and scan_cache is not None:
            pixels = scan_cache.get(gid)
            if pixels is not None:
                self.cache_hits += 1
                return pixels
        offset, length, route, crc = self._entries[gid]
        self._bank.seek(offset); blob = self._bank.read(length)
        if len(blob) != length or zlib.crc32(blob) != crc: raise ValueError('Tile integrity')
        desc = self._descriptors[gid]; w, h = desc[4:]
        if depth:
            self.reference_tiles_decoded += 1; self.reference_pixels += w*h; self.reference_bank_bytes += length
        if route == JXL:
            pixels = imagecodecs.jpegxl_decode(blob, numthreads=1, out=np.empty((h, w, 3), np.uint8)); self.jxl_tiles_decoded += 1
        else:
            validate_blob(route, blob, self.manifest['image']['levels'], desc)
            aid, ys, xs = anchor_layout(self.manifest['image']['levels'], desc)
            self.anchor_requests += 1
            self.anchor_cache_hits += ((cache is not None and aid in cache)
                                       or (scan_cache is not None and aid in scan_cache.tiles))
            parent = self._tile(aid, cache, cache_state, depth+1, scan_cache=scan_cache)
            anchor = np.ascontiguousarray(parent[ys[:, None], xs[None, :]])
            pins = self.manifest['build']['native_runtimes']
            if route == ANCHORED089:
                if self._native089 is None:
                    then = time.perf_counter(); self._native089 = old.NativeRuntime(self._runtime089, pins['089']); self.runtime_load_seconds += time.perf_counter()-then
                native = self._native089; self.native089_tiles_decoded += 1
            elif route in (CONTROL091, CONDITIONAL091):
                if self._native091 is None:
                    then = time.perf_counter(); self._native091 = cond.NativeRuntime(self._runtime091, pins['091']); self.runtime_load_seconds += time.perf_counter()-then
                native = self._native091; self.native091_tiles_decoded += 1
            else:
                if self._native_hf is None:
                    then = time.perf_counter(); self._native_hf = _hf().NativeRuntime(self._runtime_hf, pins['hf']); self.runtime_load_seconds += time.perf_counter()-then
                native = self._native_hf; self.native_hf_tiles_decoded += 1
            rid = local_id(self.manifest['image']['levels'], desc)
            then = time.perf_counter(); pixels = native.decode(blob, w, h, rid, anchor)
            self.native_decode_seconds += time.perf_counter()-then; self.native_tiles_decoded += 1
        if pixels.shape != (h, w, 3) or pixels.dtype != np.uint8: raise ValueError('Decoded native shape/type')
        self.decoded_pixels += w*h
        if cache_state is not None:
            cache_state['decoded'].add(gid)
            if cache_state['bytes']+pixels.nbytes <= FIXED['cache_bytes']:
                cache[gid] = pixels; cache_state['bytes'] += pixels.nbytes
        if depth and scan_cache is not None:
            scan_cache.put(gid, pixels)
            self.scan_parent_cache_peak_bytes = max(self.scan_parent_cache_peak_bytes, scan_cache.bytes)
        return pixels

    def _stripes(self, info):
        height, width, _ = info['shape']
        level = info['level']
        tile_size = base.TILE
        columns = (width + tile_size - 1) // tile_size
        scan_cache = _ScanReferenceCache() if level < 2 else None
        for ty, y in enumerate(range(0, height, tile_size)):
            stripe_height = min(tile_size, height - y)
            stripe = np.empty((stripe_height, width, 3), np.uint8)
            for tx, x in enumerate(range(0, width, tile_size)):
                gid = self._level_start[level] + ty * columns + tx
                pixels = self._tile(gid, scan_cache=scan_cache)
                stripe[:, x:x + pixels.shape[1]] = pixels
                self.requested_pixels += stripe_height * pixels.shape[1]
            yield stripe

    def metrics(self):
        result = super().metrics()
        result['native_hf_tiles_decoded'] = self.native_hf_tiles_decoded
        result['scan_parent_cache_peak_bytes'] = self.scan_parent_cache_peak_bytes
        result['codec_scope'] = 'Native MS L0 or091 L0/L1; every reference remains exact and paid'
        return result

    def close(self):
        super().close(); self._native_hf = None
