"""Frozen P x 091-parent-property experiment; custom image-only container.

Input records use the old089/P route numbers: 1=JXL9, 3=089 anchored.
Output routes 4/5 are the unchanged091 control/conditional native records.
No encoding, model fitting, or inference is performed by this module.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import stat
import struct
import time
import zlib

import numpy as np

from stbaseline import slide_redundancy as base
from stbaseline import slide_redundancy_scan as scan
from stbaseline import wsi_squeeze089 as old
from stbaseline import wsi_conditioned091 as conditioned
from stbaseline.integrity import sha256, write_json
from stbaseline.reader import MeteredFile

FORMAT = 'wsi-joint-modular-20261031-v1'
MAGIC, AMAGIC = b'WSIJNT01', b'WSIANCJ1'
HEADER, ENTRY, AENTRY = base.HEADER, base.ENTRY, struct.Struct('<I')
JXL, ANCHORED089, CONTROL091, CONDITIONAL091 = 1, 3, 4, 5
ANCHORED = ANCHORED089
NO_ANCHOR = 0xffffffff
MEMBERS = {'images.bank', 'images.index', 'anchors.index', 'source.json'}
PROFILES = ('direct', 'retained_p1', 'p0_control_pure', 'p1_control_pure',
            'p0_conditional_pure', 'p1_conditional_pure',
            'p1_control_hybrid', 'p1_conditional_hybrid')
FIXED = {
    'tile': 256, 'jpegxl_effort': 9, 'threads': 1, 'training': False,
    'native089_abi': 1, 'native091_abi': 1,
    'native091': 'Unchanged qualified091 parent-property gate; not binary activity D',
    'control': 'Parent reference properties zero; parent-derived thresholds and LL remain',
    'conditional': 'Known-parent reference properties16..35 enabled',
    'pyramid': 'Frozen supplied P L1 records; all L>=2 independent JXL9',
    'max_depth': 2, 'record_id': 'gid minus native level start',
    'anchor_geometry': 'floor origin/2; ceil extent/2; clamp to next native extent',
    'cache_bytes': 8*1024*1024, 'persistent_pixel_cache': False,
    'hybrid': 'Complete record minimum of direct9, retained089 choice, named091; ties retain old choice',
    'native_models': 'Embedded and paid; no external learned model',
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _plain(path, *, directory=False):
    """Reject symlinks/junctions in the path and its existing ancestors."""
    p = Path(path).absolute()
    for item in (p, *p.parents):
        if item.exists() or item.is_symlink():
            st = item.lstat()
            if item.is_symlink() or getattr(st, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('Reparse/alias path forbidden')
    if directory and not p.is_dir():
        raise ValueError('Expected ordinary directory')
    return p.resolve()


def starts(levels):
    result, n = [], 0
    for info in levels:
        result.append(n)
        h, w, _ = info['shape']
        n += ((w+255)//256)*((h+255)//256)
    return result


def geometry(levels):
    list(base._descriptors(levels))
    if not 3 <= len(levels) <= 16:
        raise ValueError('Three through sixteen native levels required')
    for level in (0, 1):
        h, w, _ = levels[level]['shape']
        ah, aw, _ = levels[level+1]['shape']
        if (ah, aw) != (h//2, w//2) or min(ah, aw) < 1:
            raise ValueError('Native adjacent floor-size geometry required')


def local_id(levels, desc):
    return desc[0]-starts(levels)[desc[1]]


def anchor_layout(levels, desc):
    gid, level, x, y, w, h = desc
    if (level not in (0, 1) or x % 256 or y % 256
            or not 1 <= min(w, h) <= max(w, h) <= 256):
        raise ValueError('Only bounded L0/L1 anchor geometry allowed')
    ah, aw, _ = levels[level+1]['shape']
    xs = np.minimum(x//2 + np.arange((w+1)//2), aw-1)
    ys = np.minimum(y//2 + np.arange((h+1)//2), ah-1)
    tx, ty = int(xs[0]//256), int(ys[0]//256)
    if np.any(xs//256 != tx) or np.any(ys//256 != ty):
        raise ValueError('Anchor requires multiple parent tiles')
    aid = starts(levels)[level+1] + ty*((aw+255)//256) + tx
    return aid, ys % 256, xs % 256


def anchor_pixels(levels, desc, fetch):
    aid, ys, xs = anchor_layout(levels, desc)
    return np.ascontiguousarray(fetch(aid)[ys[:, None], xs[None, :]]), aid


def _profile(profile):
    if profile not in PROFILES:
        raise ValueError('Unknown joint profile')
    p1 = profile == 'retained_p1' or profile.startswith('p1_')
    mode = None if profile in ('direct', 'retained_p1') else int('_conditional_' in profile)
    return p1, mode, profile.endswith('_pure'), profile.endswith('_hybrid')


def _limit(route):
    if route == JXL:
        return base.MAX_PAYLOAD
    if route == ANCHORED089:
        return old.MAX_PAYLOAD
    if route in (CONTROL091, CONDITIONAL091):
        return conditioned.MAX_PAYLOAD
    raise ValueError('Unknown joint route')


def _validate_blob(route, blob, levels, desc):
    if type(route) is not int or not isinstance(blob, bytes) or not 0 < len(blob) <= _limit(route):
        raise ValueError('Record route/type/size bound')
    gid, level, x, y, w, h = desc
    if route == ANCHORED089:
        if level not in (0, 1):
            raise ValueError('089 anchor on independent layer')
        old._record(blob, local_id(levels, desc), w, h, 1)
    elif route in (CONTROL091, CONDITIONAL091):
        if level != 0:
            raise ValueError('091 record outside L0')
        conditioned._record(blob, gid, w, h, int(route == CONDITIONAL091))


def _inputs(levels, direct, previous, pyramid):
    descs = list(base._descriptors(levels))
    if any(not isinstance(a, (list, tuple)) or len(a) != len(descs) for a in (direct, previous, pyramid)):
        raise ValueError('Complete record arrays required')
    selected = []
    for desc, d, o, p in zip(descs, direct, previous, pyramid):
        if any(not isinstance(v, (list, tuple)) or len(v) != 2 for v in (d, o, p)):
            raise ValueError('Expected (old_route,bytes) records')
        level = desc[1]
        if d[0] != JXL or o[0] not in (JXL, ANCHORED089) or p[0] not in (JXL, ANCHORED089):
            raise ValueError('Input arrays use only old route1/3')
        for route, blob in (d, o, p):
            _validate_blob(route, blob, levels, desc)
        if level > 0 and tuple(o) != tuple(d):
            raise ValueError('Old lower layer must equal direct9')
        if level == 0 and tuple(p) != tuple(o):
            raise ValueError('Frozen P must retain exact old L0')
        if level >= 2 and tuple(p) != tuple(d):
            raise ValueError('P independent layers changed')
        if level == 1:
            if p[0] == JXL and tuple(p) != tuple(d):
                raise ValueError('P retained JXL bytes changed')
            if p[0] == ANCHORED089:
                selected.append(desc[0])
        if level == 0 and o[0] == JXL and tuple(o) != tuple(d):
            raise ValueError('Old retained JXL bytes changed')
    return descs, selected


def write_package(catalog, census, direct_records, old_records, pyramid_records,
                  native_dir, output, profile, runtime_pins):
    """Create-only assembly; input record identity/qualification belongs to runner.

    runtime_pins must be {'089': old._runtime(...)[1], '091': conditioned._runtime(...)[1]}.
    Native files use control|conditional/<global_L0_gid:06d>.bin.
    """
    p1, mode, pure, hybrid = _profile(profile)
    levels = catalog['levels']; geometry(levels)
    descs, frozen = _inputs(levels, direct_records, old_records, pyramid_records)
    if (not isinstance(runtime_pins, dict) or set(runtime_pins) != {'089', '091'}
            or any(not isinstance(v, dict) or not v for v in runtime_pins.values())):
        raise ValueError('Both qualified runtime pin maps required')
    census = _plain(census, directory=True)
    metadata = _plain(census/'source.json')
    expected = catalog['files']['source.json']
    if metadata.stat().st_size > scan.MAX_SOURCE_METADATA_BYTES or metadata.stat().st_size != expected['bytes'] or sha256(metadata) != expected['sha256']:
        raise ValueError('Frozen source metadata changed')
    output = _plain(output)
    if output.exists() or any(output == src or src in output.parents or output in src.parents for src in (census,)):
        raise ValueError('Fresh disjoint package output required')
    if mode is not None:
        native_dir = _plain(native_dir, directory=True)
        if output == native_dir or native_dir in output.parents or output in native_dir.parents:
            raise ValueError('Native input/output overlap')
    output.mkdir()
    marker = output/'INCOMPLETE'; marker.write_text('Joint package assembly\n', encoding='utf8')
    try:
        routes, new_selected = [], []
        with (output/'images.bank').open('xb') as bank, (output/'images.index').open('xb') as index:
            index.write(HEADER.pack(MAGIC, 256, len(descs)))
            for desc in descs:
                gid, level = desc[:2]
                route, blob = direct_records[gid]
                if p1 and level == 1:
                    route, blob = pyramid_records[gid]
                if level == 0 and profile == 'retained_p1':
                    route, blob = old_records[gid]
                if level == 0 and mode is not None:
                    family = 'conditional' if mode else 'control'
                    path = _plain(native_dir/family/f'{gid:06d}.bin')
                    candidate = old._read_bounded(path, conditioned.MAX_PAYLOAD)
                    nroute = CONDITIONAL091 if mode else CONTROL091
                    _validate_blob(nroute, candidate, levels, desc)
                    if pure:
                        route, blob = nroute, candidate
                    elif hybrid:
                        # Start from the qualified old choice; ties preserve it.
                        route, blob = old_records[gid]
                        if len(direct_records[gid][1]) < len(blob):
                            route, blob = direct_records[gid]
                        if len(candidate) < len(blob):
                            route, blob = nroute, candidate
                    if route == nroute:
                        new_selected.append(gid)
                _validate_blob(route, blob, levels, desc)
                routes.append(route)
                index.write(ENTRY.pack(bank.tell(), len(blob), route, zlib.crc32(blob)))
                bank.write(blob)
        targets = descs[:starts(levels)[2]]
        with (output/'anchors.index').open('xb') as handle:
            handle.write(HEADER.pack(AMAGIC, 256, len(targets)))
            for desc in targets:
                aid = anchor_layout(levels, desc)[0] if routes[desc[0]] != JXL else NO_ANCHOR
                handle.write(AENTRY.pack(aid))
        shutil.copyfile(metadata, output/'source.json')
        if sha256(output/'source.json') != expected['sha256']:
            raise ValueError('Copied metadata identity changed')
        files = {name: {'bytes': (output/name).stat().st_size, 'sha256': sha256(output/name)} for name in sorted(MEMBERS)}
        route_counts = {str(r): routes.count(r) for r in (JXL, ANCHORED089, CONTROL091, CONDITIONAL091)}
        manifest = {'format': FORMAT, 'profile': profile, 'image_only': True,
                    'image': {'path': 'images.bank', 'index': 'images.index',
                              'anchor_index': 'anchors.index', 'source_metadata': 'source.json',
                              'levels': copy.deepcopy(levels)},
                    'parameters': FIXED, 'frozen_pyramid_L1': frozen,
                    'routes': route_counts, 'files': files,
                    'build': {'native_runtimes': copy.deepcopy(runtime_pins),
                              'wrapper_sha256': sha256(Path(__file__)),
                              'source_census_sha256': sha256(_plain(census/'catalog.json')),
                              'timing_scope': 'Assembly only; baseline/P/correction bytes reused; new091 encoding measured separately'}}
        write_json(output/'manifest.json', manifest)
        marker.unlink()
        return {'selected_L1': frozen if p1 else [], 'new_selected_L0': new_selected,
                'frozen_pyramid_L1': frozen, 'routes': route_counts}
    except BaseException as exc:
        write_json(output/'failure.json', {'type': type(exc).__name__, 'error': str(exc)})
        raise


class Reader(base.Reader):
    def __init__(self, directory, runtime089=None, runtime091=None):
        self.root = _plain(directory, directory=True)
        if (self.root/'INCOMPLETE').exists() or (self.root/'failure.json').exists():
            raise ValueError('Incomplete joint package')
        blob = old._read_bounded(_plain(self.root/'manifest.json'), base.MAX_MANIFEST_BYTES)
        self.manifest_bytes = len(blob); self.manifest = m = json.loads(blob)
        if m.get('format') != FORMAT or m.get('parameters') != FIXED or m.get('image_only') is not True:
            raise ValueError('Unsupported joint parameters')
        self._p1, self._mode, self._pure, self._hybrid = _profile(m.get('profile'))
        geometry(m['image']['levels']); self._descriptors = list(base._descriptors(m['image']['levels']))
        self._level_start = starts(m['image']['levels'])
        if set(m['files']) != MEMBERS or {p.name for p in self.root.iterdir()} != MEMBERS | {'manifest.json'}:
            raise ValueError('Unlisted/missing joint members')
        if {k: m['image'].get(k) for k in ('path', 'index', 'anchor_index', 'source_metadata')} != dict(path='images.bank', index='images.index', anchor_index='anchors.index', source_metadata='source.json'):
            raise ValueError('Invalid member paths')
        for name, info in m['files'].items():
            path = _plain(self.root/name)
            if (not path.is_file() or type(info.get('bytes')) is not int or info['bytes'] < 0
                    or path.stat().st_size != info['bytes'] or not isinstance(info.get('sha256'), str)
                    or base.HEX.fullmatch(info['sha256']) is None):
                raise ValueError('Invalid member identity')
        if m['files']['source.json']['bytes'] > scan.MAX_SOURCE_METADATA_BYTES:
            raise ValueError('Source metadata limit')
        frozen = m.get('frozen_pyramid_L1')
        if (not isinstance(frozen, list) or any(type(i) is not int or not self._level_start[1] <= i < self._level_start[2] for i in frozen)
                or frozen != sorted(set(frozen))):
            raise ValueError('Invalid frozen P selection')
        self._frozen = set(frozen)
        pins = m.get('build', {}).get('native_runtimes')
        if not isinstance(pins, dict) or set(pins) != {'089', '091'} or any(not isinstance(v, dict) or not v for v in pins.values()):
            raise ValueError('Runtime pins missing')
        self._entries = self._bank = self._source_metadata = None
        self._native089 = self._native091 = None
        self._runtime089, self._runtime091 = runtime089, runtime091
        self._closed = False
        self.index_read_bytes = self.anchor_index_read_bytes = 0
        self.jxl_tiles_decoded = self.constant_tiles_filled = self.logical_tiles_read = 0
        self.decoded_pixels = self.requested_pixels = self.cache_hits = 0
        self.last_query_cache_bytes = self.last_query_unique_payloads = 0
        self.reference_tiles_decoded = self.reference_pixels = self.reference_bank_bytes = 0
        self.native_tiles_decoded = self.native089_tiles_decoded = self.native091_tiles_decoded = 0
        self.anchor_requests = self.anchor_cache_hits = self.max_observed_depth = 0
        self.runtime_load_seconds = self.native_decode_seconds = 0.

    def _member(self, name, limit):
        blob = old._read_bounded(_plain(self.root/name), limit)
        if len(blob) != self.manifest['files'][name]['bytes'] or digest(blob) != self.manifest['files'][name]['sha256']:
            raise ValueError('Joint member integrity')
        return blob

    def _allowed(self, desc):
        gid, level = desc[:2]
        if level == 1:
            return {ANCHORED089 if self._p1 and gid in self._frozen else JXL}
        if level >= 2 or self.manifest['profile'] == 'direct':
            return {JXL}
        if self.manifest['profile'] == 'retained_p1':
            return {JXL, ANCHORED089}
        native = CONDITIONAL091 if self._mode else CONTROL091
        return {native} if self._pure else {JXL, ANCHORED089, native}

    def _setup(self):
        if self._closed:
            raise ValueError('Closed joint reader')
        if self._entries is not None:
            return
        n, targets = len(self._descriptors), self._level_start[2]
        data = self._member('images.index', HEADER.size+n*ENTRY.size)
        if len(data) != HEADER.size+n*ENTRY.size or HEADER.unpack_from(data) != (MAGIC, 256, n):
            raise ValueError('Image index coverage/header')
        self.index_read_bytes += len(data)
        entries = [ENTRY.unpack_from(data, HEADER.size+gid*ENTRY.size) for gid in range(n)]
        end, counts = 0, {str(r): 0 for r in (JXL, ANCHORED089, CONTROL091, CONDITIONAL091)}
        for gid, (entry, desc) in enumerate(zip(entries, self._descriptors)):
            offset, length, route, crc = entry
            if (route not in self._allowed(desc) or offset != end or not 0 < length <= _limit(route)
                    or data[HEADER.size+gid*ENTRY.size+13:HEADER.size+gid*ENTRY.size+16] != b'\0'*3):
                raise ValueError('Invalid route/span/reserved field')
            end += length; counts[str(route)] += 1
        if end != self.manifest['files']['images.bank']['bytes'] or counts != self.manifest.get('routes'):
            raise ValueError('Bank coverage or route count')
        anchors = self._member('anchors.index', HEADER.size+targets*AENTRY.size)
        if len(anchors) != HEADER.size+targets*AENTRY.size or HEADER.unpack_from(anchors) != (AMAGIC, 256, targets):
            raise ValueError('Anchor index coverage/header')
        self.anchor_index_read_bytes += len(anchors)
        for desc in self._descriptors[:targets]:
            gid, level = desc[:2]
            aid = AENTRY.unpack_from(anchors, HEADER.size+gid*AENTRY.size)[0]
            expected = anchor_layout(self.manifest['image']['levels'], desc)[0] if entries[gid][2] != JXL else NO_ANCHOR
            if aid != expected:
                raise ValueError('Wrong/missing anchor identity')
            if aid != NO_ANCHOR and (self._descriptors[aid][1] != level+1 or (level == 1 and entries[aid][2] != JXL)):
                raise ValueError('Invalid two-hop DAG')
        self._entries = entries
        self._bank = MeteredFile(_plain(self.root/'images.bank'))

    def _tile(self, gid, cache=None, cache_state=None, depth=0):
        import imagecodecs
        self._setup()
        if not base._integer(gid) or not 0 <= gid < len(self._entries) or type(depth) is not int or not 0 <= depth <= 2:
            raise ValueError('Tile identity/dependency depth bound')
        self.max_observed_depth = max(self.max_observed_depth, depth)
        self.logical_tiles_read += 1
        if cache is not None and gid in cache:
            self.cache_hits += 1
            return cache[gid]
        offset, length, route, crc = self._entries[gid]
        self._bank.seek(offset); blob = self._bank.read(length)
        if len(blob) != length or zlib.crc32(blob) != crc:
            raise ValueError('Tile CRC/length mismatch')
        desc = self._descriptors[gid]; w, h = desc[4:]
        if depth:
            self.reference_tiles_decoded += 1; self.reference_pixels += w*h; self.reference_bank_bytes += length
        if route == JXL:
            pixels = imagecodecs.jpegxl_decode(blob, numthreads=1, out=np.empty((h, w, 3), np.uint8))
            self.jxl_tiles_decoded += 1
        else:
            _validate_blob(route, blob, self.manifest['image']['levels'], desc)
            aid, ys, xs = anchor_layout(self.manifest['image']['levels'], desc)
            self.anchor_requests += 1
            self.anchor_cache_hits += cache is not None and aid in cache
            parent = self._tile(aid, cache, cache_state, depth+1)
            anchor = np.ascontiguousarray(parent[ys[:, None], xs[None, :]])
            pins = self.manifest['build']['native_runtimes']
            if route == ANCHORED089:
                if self._native089 is None:
                    then = time.perf_counter(); self._native089 = old.NativeRuntime(self._runtime089, pins['089'])
                    self.runtime_load_seconds += time.perf_counter()-then
                native, rid = self._native089, local_id(self.manifest['image']['levels'], desc)
                self.native089_tiles_decoded += 1
            else:
                if self._native091 is None:
                    then = time.perf_counter(); self._native091 = conditioned.NativeRuntime(self._runtime091, pins['091'])
                    self.runtime_load_seconds += time.perf_counter()-then
                native, rid = self._native091, gid
                self.native091_tiles_decoded += 1
            then = time.perf_counter(); pixels = native.decode(blob, w, h, rid, anchor)
            self.native_decode_seconds += time.perf_counter()-then; self.native_tiles_decoded += 1
        if pixels.shape != (h, w, 3) or pixels.dtype != np.uint8:
            raise ValueError('Native decoded shape/type')
        self.decoded_pixels += w*h
        if cache_state is not None:
            cache_state['decoded'].add(gid)
            if cache_state['bytes']+pixels.nbytes <= FIXED['cache_bytes']:
                cache[gid] = pixels; cache_state['bytes'] += pixels.nbytes
        return pixels

    def metrics(self):
        out = super().metrics()
        out.update(anchor_index_read_bytes=self.anchor_index_read_bytes,
                   reference_tiles_decoded=self.reference_tiles_decoded,
                   reference_decoded_pixels=self.reference_pixels, reference_bank_bytes=self.reference_bank_bytes,
                   native_tiles_decoded=self.native_tiles_decoded,
                   native089_tiles_decoded=self.native089_tiles_decoded,
                   native091_tiles_decoded=self.native091_tiles_decoded,
                   anchor_requests=self.anchor_requests, anchor_cache_hits=self.anchor_cache_hits,
                   max_observed_dependency_depth=self.max_observed_depth,
                   runtime_load_seconds=self.runtime_load_seconds, native_decode_seconds=self.native_decode_seconds,
                   dependency='L0->L1->L2 at most; all L>=2 independent',
                   reference_counter_scope='Each actual reference decode/read once; cache hits do not increment pixels',
                   cache_scope='<=8MiB combined target/reference pixels per image call; no cross-call cache')
        return out

    def close(self):
        if not getattr(self, '_closed', True):
            super().close(); self._native089 = self._native091 = None; self._closed = True
