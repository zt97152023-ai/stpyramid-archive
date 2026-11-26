"""Apply the unchanged qualified091 paired backend to native L1.

Only its ABI spool is virtual L0: mappings retain the real L1/global identity.
All P089 anchored candidates, not just the 123 selected records, are reusable.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time

from stbaseline import slide_redundancy as base
from stbaseline import slide_redundancy_scan as scan
from stbaseline import wsi_squeeze089 as old
from stbaseline import wsi_conditioned091 as conditioned
from stbaseline.integrity import sha256, write_json
from stbaseline.wsi_shared import _disjoint

PRIOR_CODE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


qualified = _load('_qualified_joint_native_for_l1', PRIOR_CODE/'native_adapter.py')
geometry = _load('_qualified_joint_geometry_for_l1', PRIOR_CODE/'joint.py')


def virtual_scope(catalog):
    geometry.geometry(catalog['levels'])
    descriptors = [d for d in base._descriptors(catalog['levels']) if d[1] == 1]
    tiles, virtual = [], []
    for rid, desc in enumerate(descriptors):
        gid, level, x, y, w, h = desc
        if geometry.local_id(catalog['levels'], desc) != rid:
            raise ValueError('Noncontiguous L1 virtual identity')
        tile = catalog['tiles'][gid]
        if any(tile[k] != v for k, v in zip(('gid', 'level', 'x', 'y', 'w', 'h'), desc)):
            raise ValueError('Frozen catalog L1 directory differs')
        tiles.append(dict(tile, gid=rid, level=0))
        virtual.append((rid, 0, x, y, w, h))
    return descriptors, {'tiles': tiles}, virtual


def _p_receipt(native, count):
    path = geometry._plain(Path(native)/'receipt.json')
    blob = old._read_bounded(path, 16*1024*1024)
    receipt = json.loads(blob)
    fixed = {'format': 'st089-native-squeeze-bank-1', 'standard_jxl': False,
             'abi_version': 1, 'profile': 1, 'groups': count,
             'tree_node_limit': 4095, 'tree_budget_policy': 'bfs_parent_predictor_v1',
             'detail_predictor': 'Zero', 'lowpass_predictor': 'Best',
             'color_transform': 'G,R-G,B-G', 'squeeze': 'H_then_V_first3_append',
             'uint_method': 'kBest', 'lz77_method': 'kOptimal_per_stream'}
    if any(type(receipt.get(k)) is not type(v) or receipt[k] != v for k, v in fixed.items()):
        raise ValueError('P089 receipt recipe or L1 coverage differs')
    if not isinstance(receipt.get('tiles'), list) or len(receipt['tiles']) != count:
        raise ValueError('P089 receipt tile coverage differs')
    folder = geometry._plain(Path(native)/'anchored', directory=True)
    if {p.name for p in folder.iterdir()} != {f'{i:06d}.bin' for i in range(count)}:
        raise ValueError('All P089 L1 candidates required, not just selected records')
    return receipt, hashlib.sha256(blob).hexdigest()


def validate_p_record(blob, row, actual, rid, raw_sha, anchor_sha):
    gid, level, x, y, w, h = actual
    if level != 1 or type(rid) is not int or rid < 0:
        raise ValueError('P correction must bind a real L1 descriptor/local ID')
    expected = {'gid': rid, 'level': 0, 'x': x, 'y': y, 'w': w, 'h': h,
                'anchor_w': (w+1)//2, 'anchor_h': (h+1)//2,
                'raw_sha256': raw_sha, 'anchor_sha256': anchor_sha,
                'anchored_path': f'anchored/{rid:06d}.bin',
                'anchored_bytes': len(blob), 'anchored_sha256': geometry.digest(blob),
                'detail_shared_exact': True}
    if any(type(row.get(k)) is not type(v) or row[k] != v for k, v in expected.items()):
        raise ValueError('P record is not the matching native L1/L2 correction')
    view = old._record(blob, rid, w, h, 1)
    if view['raw_sha256'] != raw_sha or view['anchor_sha256'] != anchor_sha:
        raise ValueError('P089 serialized native L1/L2 binding differs')
    return view


def build_native(census, Pnative, out, r091, r089):
    """New paired L1 work only. Never reencode old P or any L0 candidate.

    Pnative is the qualified P job's native root containing anchored/receipt.
    Root runner independently binds that source to prior reviewed job receipts.
    """
    qualified.require_worker_resources()
    started = time.perf_counter()
    census = geometry._plain(census, directory=True)
    Pnative = geometry._plain(Pnative, directory=True)
    r091, r089 = geometry._plain(r091, directory=True), geometry._plain(r089, directory=True)
    output = _disjoint(geometry._plain(out), census, Pnative, r091, r089)
    output.mkdir(parents=True)
    marker = output/'INCOMPLETE'; marker.write_text('Native L1 paired091 adapter\n', encoding='utf8')
    try:
        then = time.perf_counter()
        runtime091, runtime089, runtime_pins = qualified.qualified_runtimes(r091, r089)
        catalog = base._catalog(census)
        identity = qualified._identity(catalog, census)
        actual, virtual_catalog, virtual_descs = virtual_scope(catalog)
        prior, prior_receipt_sha = _p_receipt(Pnative, len(actual))
        prior_hashes, anchors, mappings = {}, {}, []
        input_seconds = time.perf_counter()-then
        then = time.perf_counter()
        spool = output/'spool'; spool.mkdir()
        old_records = output/'old089'; old_records.mkdir()
        with (spool/'index.tsv').open('x', encoding='ascii', newline='\n') as handle:
            for rid, desc in enumerate(actual):
                gid, level, x, y, w, h = desc
                pixels = scan.read_tile(census, gid, catalog=catalog)
                anchor, aid = geometry.anchor_pixels(catalog['levels'], desc,
                    lambda parent: scan.read_tile(census, parent, catalog=catalog))
                raw, parent = pixels.tobytes(), anchor.tobytes()
                raw_sha, parent_sha = geometry.digest(raw), geometry.digest(parent)
                if raw_sha != catalog['tiles'][gid]['native_sha256']:
                    raise ValueError('Native L1 spool SHA differs from census')
                path = geometry._plain(Pnative/'anchored'/f'{rid:06d}.bin')
                blob = old._read_bounded(path, old.MAX_PAYLOAD)
                validate_p_record(blob, prior['tiles'][rid], desc, rid, raw_sha, parent_sha)
                anchors[rid] = parent_sha
                prior_hashes[path.name] = {'bytes': len(blob), 'sha256': geometry.digest(blob)}
                (old_records/f'{rid:06d}.old089').write_bytes(blob)
                (spool/f'{rid:06d}.old089').write_bytes(blob)
                (spool/f'{rid:06d}.rgb').write_bytes(raw)
                (spool/f'{rid:06d}.anchor').write_bytes(parent)
                handle.write(f'{rid}\t0\t{x}\t{y}\t{w}\t{h}\t{rid:06d}.rgb\t{rid:06d}.anchor\t{rid:06d}.old089\n')
                mappings.append({'global_gid': gid, 'native_level': 1, 'local_gid': rid,
                                 'abi_level': 0, 'x': x, 'y': y, 'w': w, 'h': h,
                                 'native_sha256': raw_sha, 'parent_global_gid': aid,
                                 'parent_native_level': 2,
                                 'parent_native_sha256': catalog['tiles'][aid]['native_sha256'],
                                 'parent_crop_sha256': parent_sha,
                                 'old_record_sha256': geometry.digest(blob)})
        write_json(output/'mappings.json', mappings)
        spool_pins = qualified._flat_pins(spool)
        old_pins = qualified._flat_pins(old_records)
        spool_seconds = time.perf_counter()-then
        native = output/'native'
        native_seconds = qualified.native_encode(runtime091, '091', spool, native)
        then = time.perf_counter()
        receipt = qualified.validate_native_directory(native, virtual_catalog, virtual_descs, anchors, old_records)
        validation_seconds = time.perf_counter()-then
        then = time.perf_counter()
        if qualified.qualified_runtimes(runtime091, runtime089)[2] != runtime_pins:
            raise ValueError('Qualified runtime changed')
        if _p_receipt(Pnative, len(actual))[1] != prior_receipt_sha:
            raise ValueError('P native receipt changed')
        for name, info in prior_hashes.items():
            path = geometry._plain(Pnative/'anchored'/name)
            if path.stat().st_size != info['bytes'] or sha256(path) != info['sha256']:
                raise ValueError('P source candidate changed')
        if qualified._flat_pins(spool) != spool_pins or qualified._flat_pins(old_records) != old_pins:
            raise ValueError('New L1 input spool changed')
        if qualified._identity(base._catalog(census), census) != identity:
            raise ValueError('Frozen census changed')
        final_seconds = time.perf_counter()-then
        result = {'ok': True, 'native_dir': str(native), 'native_receipt': receipt,
                  'old_records': str(old_records), 'mappings': mappings,
                  'mappings_sha256': sha256(output/'mappings.json'),
                  'input_identity': identity, 'P_receipt_sha256': prior_receipt_sha,
                  'P_anchored_records': prior_hashes, 'spool_identity': spool_pins,
                  'runtime_pins': runtime_pins,
                  'timing': {'input_runtime_checks_seconds': input_seconds,
                             'spooling_and_correction_reuse_seconds': spool_seconds,
                             'native_cli_seconds': native_seconds,
                             'receipt_validation_seconds': validation_seconds,
                             'final_pin_checks_seconds': final_seconds,
                             'total_seconds': time.perf_counter()-started,
                             'scope': 'New paired L1 encoding only; old P/L0/JXL/correction times historical'},
                  'scope': 'Native L1, all candidate tiles; virtual L0 is ABI-only. Native L2 parents; no third hop.'}
        write_json(output/'adapter.json', result)
        marker.unlink()
        return result
    except BaseException as exc:
        write_json(output/'failure.json', {'type': type(exc).__name__, 'error': str(exc)})
        raise

