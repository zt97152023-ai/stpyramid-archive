"""Whole native-slide exact-content census, with bounded pixel buffers.

Near-white and cross-level similarity are descriptive only. Exact aliases are
formed only after shape and byte equality, never from a hash alone. The retained
canonical spool is task workspace, not itself a claimed compressed image.
"""
import copy
import hashlib
import json
from pathlib import Path
import re
import struct
import time

import numpy as np

from .integrity import sha256
from .wsi_shared import _disjoint
from .wsi_source import NativeWSISource

SCHEMA = 'stlossless-slide-census-086-1'
SOURCE_SCHEMA = 'stlossless-slide-source-086-1'
TILE = 256
MAX_TILES = 65_536
MAX_NATIVE_BYTES = 4 * 1024**3
MAX_STRIPE_BYTES = 128 * 1024**2
MAX_CATALOG_BYTES = 128 * 1024**2
MAX_SOURCE_METADATA_BYTES = 32 * 1024**2
_HEX = re.compile(r'[0-9a-f]{64}')


def _integer(value):
    return type(value) is int


def _write_json(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as handle:
        json.dump(value, handle, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write('\n')


def tile_identity(pixels):
    """Collision-prone lookup key only; callers must still compare bytes."""
    h, w, _ = pixels.shape
    digest = hashlib.sha256(struct.pack('<II', h, w))
    digest.update(np.ascontiguousarray(pixels).tobytes())
    return digest.hexdigest()


def _empty_difference():
    return {'pairs': 0, 'skipped_shape_pairs': 0, 'channel_values': 0,
            'sum_abs_channel_difference': 0, 'equal_channel_values': 0,
            'compared_pixels': 0, 'equal_pixels': 0, 'exact_equal_pairs': 0}


def _add_difference(total, left, right):
    if left.dtype != np.uint8 or right.dtype != np.uint8 or left.shape != right.shape:
        raise ValueError('Difference operands must be matching native RGB8')
    difference = np.abs(left.astype(np.int16) - right.astype(np.int16))
    equal = difference == 0
    total['pairs'] += 1
    total['channel_values'] += int(difference.size)
    total['sum_abs_channel_difference'] += int(difference.sum(dtype=np.uint64))
    total['equal_channel_values'] += int(equal.sum(dtype=np.uint64))
    total['compared_pixels'] += int(left.shape[0] * left.shape[1])
    total['equal_pixels'] += int(np.all(equal, axis=2).sum(dtype=np.uint64))
    total['exact_equal_pairs'] += int(bool(np.all(equal)))


def _finish_difference(total):
    result = dict(total)
    n = result['channel_values']
    result['mean_absolute_channel_difference'] = result['sum_abs_channel_difference'] / n if n else None
    return result


def _read_record(stream, record):
    stream.seek(record['raw_offset'])
    raw = stream.read(record['raw_length'])
    if len(raw) != record['raw_length'] or hashlib.sha256(raw).hexdigest() != record['native_sha256']:
        raise ValueError('Canonical raw tile failed length/SHA validation')
    return np.frombuffer(raw, np.uint8).reshape(record['h'], record['w'], 3)


def _validate_geometry(levels):
    if not isinstance(levels, list) or not 1 <= len(levels) <= 16:
        raise ValueError('Invalid native level directory')
    previous = None
    total = native = 0
    for level, info in enumerate(levels):
        shape = info.get('shape')
        if (not _integer(info.get('level')) or info.get('level') != level or info.get('dtype') != 'uint8' or not isinstance(shape, list)
                or len(shape) != 3 or shape[2] != 3 or any(not _integer(v) or v < 1 for v in shape)):
            raise ValueError('Require explicit RGB8 level shapes')
        h, w, _ = shape
        if previous is not None and (h > previous[0] or w > previous[1] or (h,w) == previous):
            raise ValueError('Native level shapes must strictly decrease')
        previous = (h,w)
        if (not _integer(info.get('tiles_x')) or not _integer(info.get('tiles_y'))
                or info.get('tiles_x') != (w+255)//256 or info.get('tiles_y') != (h+255)//256):
            raise ValueError('Native tile-grid shape mismatch')
        if min(h, TILE) * w * 3 > MAX_STRIPE_BYTES:
            raise ValueError('Native raster stripe exceeds bounded profile')
        total += info['tiles_x'] * info['tiles_y']
        native += h*w*3
    if total > MAX_TILES or native > MAX_NATIVE_BYTES:
        raise ValueError('Slide exceeds qualified census profile')
    return total, native


def _diagnostics(spool_path, levels, records):
    starts = []
    cursor = 0
    for info in levels:
        starts.append(cursor)
        cursor += info['tiles_x'] * info['tiles_y']
    neighbors = []
    pyramid = []
    with spool_path.open('rb') as stream:
        for info in levels:
            level, nx, ny = info['level'], info['tiles_x'], info['tiles_y']
            horizontal, vertical = _empty_difference(), _empty_difference()
            horizontal_nonconstant, vertical_nonconstant = _empty_difference(), _empty_difference()
            for ty in range(ny):
                for tx in range(nx):
                    gid = starts[level] + ty*nx + tx
                    item = records[gid]
                    pixels = _read_record(stream, item)
                    for enabled, other_gid, total, nonconstant in ((tx > 0, gid-1, horizontal,horizontal_nonconstant), (ty > 0, gid-nx, vertical,vertical_nonconstant)):
                        if not enabled:
                            continue
                        other = records[other_gid]
                        if (item['h'], item['w']) != (other['h'], other['w']):
                            total['skipped_shape_pairs'] += 1
                            if item['constant_rgb'] is None:
                                nonconstant['skipped_shape_pairs'] += 1
                        else:
                            reference = _read_record(stream, other)
                            _add_difference(total, pixels, reference)
                            if item['constant_rgb'] is None:
                                _add_difference(nonconstant, pixels, reference)
            neighbors.append({'level': level, 'horizontal': _finish_difference(horizontal),
                              'vertical': _finish_difference(vertical),
                              'horizontal_nonconstant_target':_finish_difference(horizontal_nonconstant),
                              'vertical_nonconstant_target':_finish_difference(vertical_nonconstant)})
            if level+1 == len(levels):
                continue
            coarse = levels[level+1]
            fh, fw, _ = info['shape']; ch, cw, _ = coarse['shape']
            difference = _empty_difference()
            difference_nonconstant = _empty_difference()
            fanout_histogram = {}; reference_uses = 0; maximum_fanout = 0
            fanout_nonconstant = {}; reference_uses_nonconstant = maximum_fanout_nonconstant = 0
            for ty in range(ny):
                for tx in range(nx):
                    item = records[starts[level] + ty*nx + tx]
                    pixels = _read_record(stream, item)
                    xs = (np.arange(item['x'], item['x']+item['w'], dtype=np.int64) * cw) // fw
                    ys = (np.arange(item['y'], item['y']+item['h'], dtype=np.int64) * ch) // fh
                    target = np.empty_like(pixels)
                    fanout = 0
                    for cy in np.unique(ys // TILE):
                        yi = np.flatnonzero(ys // TILE == cy)
                        for cx in np.unique(xs // TILE):
                            xi = np.flatnonzero(xs // TILE == cx)
                            coarse_gid = starts[level+1] + int(cy)*coarse['tiles_x'] + int(cx)
                            reference = _read_record(stream, records[coarse_gid])
                            target[np.ix_(yi,xi)] = reference[np.ix_(ys[yi] % TILE, xs[xi] % TILE)]
                            fanout += 1
                    reference_uses += fanout
                    maximum_fanout = max(maximum_fanout, fanout)
                    fanout_histogram[str(fanout)] = fanout_histogram.get(str(fanout), 0) + 1
                    _add_difference(difference, pixels, target)
                    if item['constant_rgb'] is None:
                        _add_difference(difference_nonconstant,pixels,target)
                        reference_uses_nonconstant += fanout
                        maximum_fanout_nonconstant = max(maximum_fanout_nonconstant,fanout)
                        fanout_nonconstant[str(fanout)] = fanout_nonconstant.get(str(fanout),0)+1
            pyramid.append({'fine_level': level, 'coarse_level': level+1,
                'predictor': 'coarse[floor(y*Hcoarse/Hfine),floor(x*Wcoarse/Wfine)]',
                'difference': _finish_difference(difference), 'reference_tile_uses': reference_uses,
                'difference_nonconstant_fine':_finish_difference(difference_nonconstant),
                'max_reference_tiles_per_fine_tile': maximum_fanout,
                'reference_fanout_histogram': fanout_histogram,
                'reference_tile_uses_nonconstant_fine':reference_uses_nonconstant,
                'max_reference_tiles_per_nonconstant_fine_tile':maximum_fanout_nonconstant,
                'reference_fanout_histogram_nonconstant_fine':fanout_nonconstant})
    return {'neighbors': neighbors, 'pyramid': pyramid,
            'meaning': 'Descriptive native-pixel comparisons only; nonconstant also includes noisy background; not entropy, coding gain, tissue classification or biological fidelity'}


def census(source, output):
    """Scan every native level once; other diagnostic passes use retained spool."""
    started = time.perf_counter()
    source = Path(source).resolve(strict=True)
    out = _disjoint(output, source)
    out.mkdir(parents=True)
    try:
        before = sha256(source)
        records = []; buckets = {}; constant_tiles = aliases = cross_level_aliases = 0
        near_white_pixels = all_near_white_tiles = raw_offset = 0
        then = time.perf_counter()
        with NativeWSISource(source) as native, (out/'tiles.rgb').open('x+b') as spool:
            original_levels = native.levels
            total_tiles, native_bytes = _validate_geometry(original_levels)
            identity = native.identity
            metadata = native.metadata
            levels = copy.deepcopy(original_levels)
            for info in levels:
                level = info['level']; h, w, _ = info['shape']
                raster = hashlib.sha256()
                for ty in range(info['tiles_y']):
                    stripe = np.empty((min(TILE,h-ty*TILE),w,3), np.uint8)
                    for tx in range(info['tiles_x']):
                        pixels = native.read_tile(level,tx,ty)
                        ph,pw,_ = pixels.shape
                        raw = pixels.tobytes(order='C')
                        native_sha = hashlib.sha256(raw).hexdigest()
                        key = tile_identity(pixels)
                        canonical = None
                        for candidate_gid in buckets.get(key,[]):
                            candidate = records[candidate_gid]
                            if (candidate['h'],candidate['w']) != (ph,pw):
                                continue
                            spool.seek(candidate['raw_offset'])
                            if spool.read(candidate['raw_length']) == raw:
                                canonical = candidate
                                break
                        gid = len(records)
                        if canonical is None:
                            spool.seek(raw_offset)
                            spool.write(raw)
                            offset = raw_offset
                            raw_offset += len(raw)
                            if raw_offset > MAX_NATIVE_BYTES:
                                raise ValueError('Canonical spool exceeds bounded profile')
                            canonical_gid = gid
                            buckets.setdefault(key,[]).append(gid)
                        else:
                            offset,canonical_gid = canonical['raw_offset'],canonical['gid']
                            aliases += 1
                            cross_level_aliases += int(canonical['level'] != level)
                        constant = bool(np.all(pixels == pixels[0,0]))
                        constant_rgb = pixels[0,0].tolist() if constant else None
                        white_count = int(np.all(pixels >= 240,axis=2).sum(dtype=np.uint64))
                        all_white = white_count == ph*pw
                        constant_tiles += int(constant)
                        all_near_white_tiles += int(all_white)
                        near_white_pixels += white_count
                        records.append({'gid':gid,'level':level,'x':tx*TILE,'y':ty*TILE,'w':pw,'h':ph,
                            'canonical_gid':canonical_gid,'raw_offset':offset,'raw_length':len(raw),
                            'native_sha256':native_sha,'identity_sha256':key,'constant_rgb':constant_rgb,
                            'near_white_pixels':white_count,'all_near_white':all_white})
                        stripe[:,tx*TILE:tx*TILE+pw] = pixels
                    raster.update(stripe.tobytes(order='C'))
                info['pixel_sha256'] = raster.hexdigest()
            spool.flush()
            native.unchanged()
            source_receipt = native.metrics()
            if len(records) != total_tiles or source_receipt['tiles_decoded'] != total_tiles:
                raise ValueError('Whole-slide coverage mismatch')
        scan_seconds = time.perf_counter()-then
        after = sha256(source)
        if before != after:
            raise ValueError('Whole source file changed during census')
        identity['whole_file_sha256'] = before
        source_metadata = {'schema':SOURCE_SCHEMA,'identity':identity,'native_levels':original_levels,
                           'native_metadata':metadata,'whole_file_sha256':before,
                           'exactness':'All native RGB values and typed metadata; not source TIFF container restoration'}
        _write_json(out/'source.json',source_metadata)
        then = time.perf_counter()
        diagnostic = _diagnostics(out/'tiles.rgb',levels,records)
        diagnostic_seconds = time.perf_counter()-then
        multiplicity = {}
        for record in records:
            key = str(record['canonical_gid'])
            multiplicity[key] = multiplicity.get(key,0)+1
        summary = {'logical_tiles':len(records),'canonical_tiles':len(multiplicity),'alias_tiles':aliases,
            'cross_level_alias_tiles':cross_level_aliases,'constant_tiles':constant_tiles,
            'constant_canonical_tiles':sum(int(r['constant_rgb'] is not None) for r in records if r['canonical_gid']==r['gid']),
            'near_white_threshold':240,'near_white_pixels':near_white_pixels,'all_near_white_tiles':all_near_white_tiles,
            'valid_native_pixels':native_bytes//3,'valid_native_RGB_bytes':native_bytes,
            'canonical_spool_bytes':raw_offset,'raw_duplicate_RGB_bytes':native_bytes-raw_offset,
            'canonical_multiplicity':multiplicity,
            'caution':'Raw duplicate bytes and near-white pixels are not compressed-byte savings'}
        files = {name:{'bytes':(out/name).stat().st_size,'sha256':sha256(out/name)} for name in ('tiles.rgb','source.json')}
        catalog = {'schema':SCHEMA,'tile_size':TILE,'levels':levels,'tiles':records,
                   'files':files,'summary':summary,'diagnostics':diagnostic,
                   'identity_scope':'Whole source SHA before/after plus native layout/metadata and actual tile-read receipt'}
        _write_json(out/'catalog.json',catalog)
        then = time.perf_counter()
        load_catalog(out,verify_spool=True)
        verification_seconds = time.perf_counter()-then
        # Timings and read metrics are outside the deterministic catalog/spool/metadata.
        result = {'ok':True,'scan_seconds':scan_seconds,'diagnostic_seconds':diagnostic_seconds,
                  'verification_seconds':verification_seconds,
                  'total_seconds':time.perf_counter()-started,'source_before_sha256':before,'source_after_sha256':after,
                  'source_read_receipt':source_receipt,'summary':summary,
                  'deterministic_files':['catalog.json','tiles.rgb','source.json'],
                  'workspace_only':'Canonical raw spool is retained construction evidence, not compressed output'}
        _write_json(out/'scan.json',result)
        return result
    except BaseException as exc:
        _write_json(out/'failure.json',{'type':type(exc).__name__,'error':str(exc)})
        raise


def load_catalog(directory, *, verify_spool=True):
    """Validate exact-content directory without reading any original source path."""
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('Symlink census root is not supported')
    root = directory.resolve(strict=True)
    if (root/'failure.json').exists():
        raise ValueError('Failed census cannot be consumed')
    path = root/'catalog.json'
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_CATALOG_BYTES:
        raise ValueError('Invalid census catalog member')
    catalog = json.loads(path.read_bytes())
    if catalog.get('schema') != SCHEMA or catalog.get('tile_size') != TILE:
        raise ValueError('Unsupported census schema')
    levels = catalog['levels']
    total, native_bytes = _validate_geometry(levels)
    if any(not isinstance(v.get('pixel_sha256'),str) or _HEX.fullmatch(v['pixel_sha256']) is None for v in levels):
        raise ValueError('Missing native level digest')
    if set(catalog.get('files',{})) != {'tiles.rgb','source.json'}:
        raise ValueError('Unexpected census member directory')
    for name, desc in catalog['files'].items():
        member = root/name
        if (member.is_symlink() or not member.is_file() or not _integer(desc.get('bytes')) or desc['bytes'] < 0
                or member.stat().st_size != desc['bytes'] or not isinstance(desc.get('sha256'),str)
                or _HEX.fullmatch(desc['sha256']) is None):
            raise ValueError('Census member length/identity mismatch')
        if (name != 'tiles.rgb' or verify_spool) and sha256(member) != desc['sha256']:
            raise ValueError('Census member SHA mismatch')
    if catalog['files']['source.json']['bytes'] > MAX_SOURCE_METADATA_BYTES:
        raise ValueError('Source metadata exceeds bounded profile')
    source = json.loads((root/'source.json').read_bytes())
    if (source.get('schema') != SOURCE_SCHEMA or len(source.get('native_levels',[])) != len(levels)
            or len(source.get('native_metadata',[])) != len(levels)
            or source.get('whole_file_sha256') != source.get('identity',{}).get('whole_file_sha256')
            or not isinstance(source.get('whole_file_sha256'),str) or _HEX.fullmatch(source['whole_file_sha256']) is None):
        raise ValueError('Typed source metadata coverage/identity mismatch')
    for level, original in zip(levels,source['native_levels']):
        if original.get('level') != level['level'] or original.get('shape') != level['shape']:
            raise ValueError('Source geometry differs from census')
    records = catalog.get('tiles')
    if not isinstance(records,list) or len(records) != total:
        raise ValueError('Tile directory coverage mismatch')
    cursor = gid = 0; canonical_count = 0; constants = aliases = white = all_white = cross = 0
    multiplicity = {}
    for level in levels:
        h,w,_ = level['shape']
        for ty in range(level['tiles_y']):
            for tx in range(level['tiles_x']):
                item = records[gid]
                expected = (gid,level['level'],tx*TILE,ty*TILE,min(TILE,w-tx*TILE),min(TILE,h-ty*TILE))
                keys = ('gid','level','x','y','w','h')
                if any(not _integer(item.get(k)) for k in keys) or tuple(item[k] for k in keys) != expected:
                    raise ValueError('Logical tile geometry/order mismatch')
                if (not _integer(item.get('raw_offset')) or item['raw_offset'] < 0
                        or not _integer(item.get('raw_length')) or item['raw_length'] != item['h']*item['w']*3
                        or not _integer(item.get('canonical_gid')) or not 0 <= item['canonical_gid'] <= gid):
                    raise ValueError('Invalid canonical raw span')
                for name in ('native_sha256','identity_sha256'):
                    if not isinstance(item.get(name),str) or _HEX.fullmatch(item[name]) is None:
                        raise ValueError('Invalid tile digest')
                const = item.get('constant_rgb')
                if const is not None and (not isinstance(const,list) or len(const)!=3 or any(not _integer(v) or not 0<=v<=255 for v in const)):
                    raise ValueError('Invalid exact constant RGB')
                pixels = item['h']*item['w']
                if (not _integer(item.get('near_white_pixels')) or not 0<=item['near_white_pixels']<=pixels
                        or type(item.get('all_near_white')) is not bool or item['all_near_white']!=(item['near_white_pixels']==pixels)):
                    raise ValueError('Invalid descriptive near-white count')
                canonical = records[item['canonical_gid']]
                if item['canonical_gid']==gid:
                    if item['raw_offset'] != cursor:
                        raise ValueError('Canonical raw ranges must be contiguous')
                    cursor += item['raw_length']; canonical_count += 1
                else:
                    if canonical['canonical_gid'] != canonical['gid']:
                        raise ValueError('Canonical aliases cannot form chains')
                    for name in ('w','h','raw_offset','raw_length','native_sha256','identity_sha256','constant_rgb','near_white_pixels','all_near_white'):
                        if item[name] != canonical[name]:
                            raise ValueError('Alias identity differs from canonical tile')
                    aliases += 1; cross += int(canonical['level'] != item['level'])
                constants += int(const is not None)
                white += item['near_white_pixels']; all_white += int(item['all_near_white'])
                key = str(item['canonical_gid']); multiplicity[key] = multiplicity.get(key,0)+1
                gid += 1
    if cursor != catalog['files']['tiles.rgb']['bytes'] or cursor > MAX_NATIVE_BYTES:
        raise ValueError('Spool has a gap, overlap or trailing content')
    expected_summary = {'logical_tiles':total,'canonical_tiles':canonical_count,'alias_tiles':aliases,
        'cross_level_alias_tiles':cross,'constant_tiles':constants,
        'constant_canonical_tiles':sum(int(r['constant_rgb'] is not None) for r in records if r['canonical_gid']==r['gid']),
        'near_white_threshold':240,'near_white_pixels':white,'all_near_white_tiles':all_white,
        'valid_native_pixels':native_bytes//3,'valid_native_RGB_bytes':native_bytes,'canonical_spool_bytes':cursor,
        'raw_duplicate_RGB_bytes':native_bytes-cursor,'canonical_multiplicity':multiplicity}
    if any(catalog.get('summary',{}).get(k) != v for k,v in expected_summary.items()):
        raise ValueError('Summary disagrees with exact logical/physical directory')
    return catalog


def read_tile(directory, gid, catalog=None):
    """Read one verified valid RGB tile from the retained canonical spool."""
    if catalog is None:
        catalog = load_catalog(directory,verify_spool=False)
    if not _integer(gid) or not 0 <= gid < len(catalog['tiles']):
        raise ValueError('Logical tile ID outside census')
    path = Path(directory)/'tiles.rgb'
    if path.is_symlink():
        raise ValueError('Symlink spool is not supported')
    with path.open('rb') as stream:
        pixels = _read_record(stream,catalog['tiles'][gid])
    constant = catalog['tiles'][gid]['constant_rgb']
    if constant is not None and not np.all(pixels==np.asarray(constant,np.uint8)):
        raise ValueError('Constant declaration differs from retained native pixels')
    return pixels
