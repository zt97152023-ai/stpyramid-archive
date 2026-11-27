"""New dense-MS native adapter; old089/091/P code and artifacts stay immutable."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import zlib
from st262_controls import assigned_cpu, native_command, remaining_seconds
import numpy as np

from stbaseline import slide_redundancy as base
from stbaseline import slide_redundancy_scan as scan
from stbaseline import wsi_conditioned091 as conditioned
from stbaseline import wsi_squeeze089 as old
from stbaseline.integrity import sha256, write_json
from stbaseline.wsi_shared import _disjoint


THREAD_VARS = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
               'NUMEXPR_NUM_THREADS', 'NUMBA_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'BLIS_NUM_THREADS')
MAX_PAYLOAD = conditioned.MAX_PAYLOAD
DECODE_ORDER = 'LL,VM0,VS0,VM1,VS1,VM2,VS2,Hlow,HM0,HS0,HM1,HS1,HM2,HS2'
def _runtime(runtime_dir):
    arg = Path(runtime_dir)
    if arg.is_symlink() or not arg.is_dir():
        raise ValueError('Unsafe HF runtime directory')
    root = arg.resolve()
    raw = conditioned._read_bounded(root / 'build_receipt.json', 4*1024*1024)
    receipt = json.loads(raw)
    if (receipt.get('schema') != 'wsi-hf1-runtime-build-1' or receipt.get('abi_version') != 1
            or receipt.get('representation') != 'dense_magnitude_sign_v1'
            or (receipt.get('built_dependencies') is not False and receipt.get('release_profile') != 'local-source-v1')):
        raise ValueError('HF runtime recipe differs from fixed MS build')
    pins = {}
    for name in ('slide_hf1.exe', 'slide_hf1.dll'):
        p = root / name; info = receipt.get('outputs', {}).get(name, {})
        if (p.is_symlink() or not p.is_file() or p.stat().st_size != info.get('bytes')
                or sha256(p) != info.get('sha256')):
            raise ValueError('HF runtime differs from build receipt: ' + name)
        pins[name] = {'bytes': info['bytes'], 'sha256': info['sha256']}
    pins['build_receipt.json'] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    return root, pins


def qualified_runtimes(runtimeHF, runtime089):
    # Independent acceptance of the new build is enforced by the root gate.
    # A relocated decoder only needs its copied receipt and matching binaries.
    hfroot, hfpins = _runtime(runtimeHF); oldroot, oldpins = old._runtime(runtime089)
    return hfroot, oldroot, {'089': oldpins, 'hf': hfpins}


def _record(blob, gid, w, h):
    if not isinstance(blob, bytes) or not 260 <= len(blob) <= MAX_PAYLOAD or blob[:8] != b'SHF1TIL1':
        raise ValueError('HF1 record magic/length')
    # Header/framing is deliberately byte-layout compatible with091; the MS
    # magic and forced mode1 are checked separately, without changing old code.
    return conditioned._record(b'S091TIL1' + blob[8:], gid, w, h, 1)


class NativeRuntime:
    def __init__(self, runtime_dir, expected=None):
        self.root, self.pins = _runtime(runtime_dir)
        if expected is not None and self.pins != expected:
            raise ValueError('HF runtime differs from archive pins')
        self.library = lib = ctypes.CDLL(str(self.root / 'slide_hf1.dll'))
        lib.sthf1_abi_version.argtypes = []; lib.sthf1_abi_version.restype = ctypes.c_uint32
        if lib.sthf1_abi_version() != 1:
            raise ValueError('Unsupported HF ABI')
        ptr, size, u32 = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32
        lib.sthf1_decode.argtypes = [ptr, size, ptr, size, u32, u32, u32, ptr, size, ptr, size]
        lib.sthf1_decode.restype = ctypes.c_int

    def decode(self, blob, w, h, gid, anchor):
        if (type(w) is not int or type(h) is not int or not 1 <= w <= 256 or not 1 <= h <= 256
                or type(gid) is not int or not 0 <= gid < base.MAX_TILES):
            raise ValueError('HF requested identity out of range')
        _record(blob, gid, w, h)
        if not isinstance(anchor, np.ndarray) or anchor.dtype != np.uint8 or anchor.shape != ((h+1)//2, (w+1)//2, 3):
            raise ValueError('HF exact anchor array required')
        anchor = np.ascontiguousarray(anchor); pixels = np.empty((h, w, 3), np.uint8)
        payload = ctypes.create_string_buffer(blob); error = ctypes.create_string_buffer(2048)
        if self.library.sthf1_decode(payload, len(blob), anchor.ctypes.data, anchor.nbytes, gid, w, h,
                                     pixels.ctypes.data, pixels.nbytes, error, len(error)):
            raise ValueError('sthf1_decode: ' + error.value.decode('utf8', 'replace'))
        return pixels


def require_worker_resources():
    """Children inherit the supervised worker's assigned-CPU/Idle Windows process state."""
    if os.name != 'nt':
        raise RuntimeError('This task uses the frozen Windows native runtime')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
    kernel.GetProcessAffinityMask.restype = ctypes.c_int
    kernel.GetPriorityClass.argtypes = [ctypes.c_void_p]
    kernel.GetPriorityClass.restype = ctypes.c_uint32
    proc = kernel.GetCurrentProcess()
    mask, system = ctypes.c_size_t(), ctypes.c_size_t()
    if not kernel.GetProcessAffinityMask(proc, ctypes.byref(mask), ctypes.byref(system)):
        raise OSError(ctypes.get_last_error(), 'Cannot verify worker affinity')
    if mask.value != (1 << assigned_cpu()) or kernel.GetPriorityClass(proc) != 0x40:
        raise RuntimeError('Native children require supervised assigned-CPU-only/Idle worker')


def native_encode(runtime, number, spool, target):
    """One bounded child; also used for new synthetic089 fixture material only."""
    if number not in ('089', 'hf1'):
        raise ValueError('Only reused089/newHF1 native children are supported')
    require_worker_resources()
    runtime, spool, target = Path(runtime), Path(spool), Path(target)
    if target.exists() or target.is_symlink() or not target.parent.is_dir():
        raise FileExistsError('Fresh native output with existing parent required')
    env = os.environ.copy()
    for name in list(env):
        if name in ('ST074_S', 'ST074_T') or ('074' in name and ('JXL' in name or 'STBASELINE' in name)):
            env.pop(name)
    for name in THREAD_VARS:
        env[name] = '1'
    stdout, stderr = target.parent / (target.name + '.stdout.txt'), target.parent / (target.name + '.stderr.txt')
    executable = runtime / ('slide_hf1.exe' if number == 'hf1' else 'slide_jxl089.exe')
    command, directory = native_command(executable, 'encode', spool, target)
    started = time.perf_counter()
    with stdout.open('x', encoding='utf8') as out, stderr.open('x', encoding='utf8') as err:
        result = subprocess.run(command, cwd=directory,
                                stdout=out, stderr=err, env=env, timeout=remaining_seconds(),
                                creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        message = stderr.read_text(encoding='utf8', errors='replace')[-3000:]
        raise RuntimeError('Native' + number + ' child failed: ' + message)
    return time.perf_counter() - started


def _flat_pins(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Unsafe member directory')
    if any(p.is_symlink() or not p.is_file() for p in root.iterdir()):
        raise ValueError('Unexpected nested/unsafe adapter member')
    return base._members(root)


def validate_native_directory(native, catalog, descriptors, anchors, old_records):
    native, old_records = Path(native), Path(old_records)
    if native.is_symlink() or not native.is_dir() or {p.name for p in native.iterdir()} != {'ms', 'receipt.json'}:
        raise ValueError('HF output directory members differ')
    targets = [d for d in descriptors if d[1] == 0]
    expected_names = {f'{d[0]:06d}.bin' for d in targets}
    if (native / 'ms').is_symlink() or not (native / 'ms').is_dir() or set(_flat_pins(native / 'ms')) != expected_names:
        raise ValueError('HF exact output coverage differs')
    receipt = json.loads(conditioned._read_bounded(native / 'receipt.json', 16*1024*1024))
    fixed = {'format': 'sthf1-native-ms-bank-1', 'standard_jxl': False, 'abi_version': 1, 'profile': 1,
             'groups': len(targets), 'tree_node_limit': 4095, 'tree_budget_policy': 'bfs_parent_predictor_v1',
             'predictor': 'Zero', 'max_properties': 5, 'color_transform': 'G,R-G,B-G',
             'squeeze': 'H_then_V_first3_append', 'decode_order': DECODE_ORDER,
             'representation': 'dense_magnitude_sign_v1', 'uint_method': 'kBest', 'lz77_method': 'kOptimal_per_tile'}
    if any(type(receipt.get(k)) is not type(v) or receipt[k] != v for k, v in fixed.items()):
        raise ValueError('HF receipt differs from fixed representation')
    if len(receipt.get('tiles', [])) != len(targets):
        raise ValueError('HF receipt tile coverage')
    costs = dict.fromkeys(('ms_bytes', 'correction_stream_bytes', 'ms_hf_stream_bytes', 'signed_count', 'MS_target_count'), 0)
    for desc, record in zip(targets, receipt['tiles']):
        gid, level, x, y, w, h = desc; aw, ah = (w+1)//2, (h+1)//2
        signed = 3*w*h - 3*aw*ah; doubled = 2*signed
        oldblob = conditioned._read_bounded(old_records / f'{gid:06d}.old089', old.MAX_PAYLOAD)
        oldview = old._record(oldblob, gid, w, h, 1)
        expected = {'gid': gid, 'level': 0, 'x': x, 'y': y, 'w': w, 'h': h, 'anchor_w': aw, 'anchor_h': ah,
                    'raw_sha256': catalog['tiles'][gid]['native_sha256'], 'anchor_sha256': anchors[gid],
                    'old_record_sha256': hashlib.sha256(oldblob).hexdigest(), 'correction_reused_exact': True,
                    'signed_count': signed, 'MS_target_count': doubled, 'ms_path': f'ms/{gid:06d}.bin'}
        if any(type(record.get(k)) is not type(v) or record[k] != v for k, v in expected.items()):
            raise ValueError('HF source/parent/count/record identity mismatch')
        if oldview['raw_sha256'] != expected['raw_sha256'] or oldview['anchor_sha256'] != anchors[gid]:
            raise ValueError('HF old correction identity differs')
        blob = conditioned._read_bounded(native / record['ms_path'], MAX_PAYLOAD)
        if len(blob) != record.get('ms_bytes') or hashlib.sha256(blob).hexdigest() != record.get('ms_sha256'):
            raise ValueError('HF record byte/hash receipt differs')
        view = _record(blob, gid, w, h)
        for key in ('raw_sha256', 'anchor_sha256', 'prefix_v_sha256', 'prefix_h_sha256'):
            if view[key] != record.get(key):
                raise ValueError('HF serialized binding differs')
        if view['low']['frame'] != oldblob[-48-oldview['low']['bytes']:]:
            raise ValueError('HF paid correction frame not exact old089 reuse')
        ms = record['ms']; conditioned._validate_stream_receipt(ms, view['detail'], doubled, 'Zero')
        if (ms.get('coefficient_sha256') != record.get('hf_coefficient_sha256')
                or any(type(ms.get(k)) is not int or ms[k] != doubled for k in ('target_values', 'decoder_target_values'))):
            raise ValueError('HF canonical digest or dense MS token count mismatch')
        nodes, pairs = ms.get('parent_reference_node_count'), ms.get('parent_reference_split_channel_pairs')
        if (type(nodes) is not int or type(pairs) is not int or not 0 <= nodes <= max(0, (ms['tree_nodes']-1)//2)
                or not nodes <= pairs <= 12*nodes):
            raise ValueError('HF parent reference reachability count invalid')
        correction = record['correction']; stream = view['low']
        expected_low = {'bytes': stream['bytes'], 'bits': stream['bits'], 'consumed_bits': stream['bits'],
                        'sha256': stream['sha256'], 'coefficients': 3*aw*ah, 'reused': True}
        if any(type(correction.get(k)) is not type(v) or correction[k] != v for k, v in expected_low.items()):
            raise ValueError('HF reused correction receipt mismatch')
        if (type(correction.get('lz77_enabled')) is not bool or type(correction.get('tree_nodes')) is not int
                or not 1 <= correction['tree_nodes'] <= 4095 or correction['tree_nodes'] % 2 != 1
                or not isinstance(correction.get('coefficient_sha256'), str)
                or base.HEX.fullmatch(correction['coefficient_sha256']) is None):
            raise ValueError('HF reused correction model receipt invalid')
        costs['ms_bytes'] += len(blob); costs['ms_hf_stream_bytes'] += view['detail']['bytes']
        costs['correction_stream_bytes'] += stream['bytes']; costs['signed_count'] += signed; costs['MS_target_count'] += doubled
    costs['record_header_bytes_per_tile'] = 260
    if receipt.get('costs') != costs:
        raise ValueError('HF paid cost sums differ from records')
    return receipt

def _identity(catalog, census):
    logical = [{k: item[k] for k in ('gid', 'level', 'x', 'y', 'w', 'h', 'native_sha256')}
               for item in catalog['tiles']]
    data = json.dumps(logical, sort_keys=True, separators=(',', ':')).encode('ascii')
    return {'catalog_sha256': sha256(census / 'catalog.json'),
            'source_metadata': catalog['files']['source.json'],
            'canonical_spool': catalog['files']['tiles.rgb'],
            'logical_directory_sha256': hashlib.sha256(data).hexdigest(),
            'logical_tiles': len(logical), 'native_levels': len(catalog['levels']),
            'l0_targets': sum(item['level'] == 0 for item in logical)}


def build_native(census: Path, old_anchor_package: Path, output_dir: Path,
                 runtimeHF: Path, runtime089: Path) -> dict:
    """Encode one fixed dense-MS L0 bank with reused correction and paid models.

    Native lower levels and JXL9 references are not reencoded. The complete old
    anchor-pure package is verified and its L0 correction frames are copied.
    Returned timing is incremental MS-bank work, never full-slide encoding.
    """
    started = time.perf_counter()
    for value in (census, old_anchor_package, runtimeHF, runtime089):
        if Path(value).is_symlink() or not Path(value).is_dir():
            raise ValueError('Adapter inputs must be existing real directories')
    census, old_anchor_package = Path(census).resolve(), Path(old_anchor_package).resolve()
    output = _disjoint(output_dir, census, old_anchor_package, runtimeHF, runtime089)
    output.mkdir(parents=True)
    (output / 'INCOMPLETE').write_text('WSI-RD-HF-L1 dense MS native bank\n', encoding='utf8')
    try:
        then = time.perf_counter()
        native_runtime, old_runtime, runtime_pins = qualified_runtimes(runtimeHF, runtime089)
        runtime_seconds = time.perf_counter() - then
        then = time.perf_counter()
        catalog = base._catalog(census)
        conditioned._anchor_geometry(catalog['levels'])
        descriptors = list(base._descriptors(catalog['levels']))
        identity = _identity(catalog, census)
        old_pins = base._members(old_anchor_package)
        old_records = output / 'old089'; old_records.mkdir()
        with old.Reader(old_anchor_package, old_runtime) as reader:
            if reader.manifest['profile'] != 'anchor_pure' or reader.manifest['image']['levels'] != catalog['levels']:
                raise ValueError('Correction source must be matching frozen089 anchor_pure')
            if reader.manifest['files']['source.json'] != catalog['files']['source.json']:
                raise ValueError('Correction source provenance differs from census')
            if reader.manifest['build']['native_runtime'] != runtime_pins['089']:
                raise ValueError('Correction source used a different089 runtime')
            reader._verify_files(); reader._setup()
            for gid, level, _, _, w, h in descriptors:
                if level != 0:
                    continue
                offset, length, route, crc = reader._entries[gid]
                if route != old.ANCHORED:
                    raise ValueError('Every correction source L0 must be089 anchored')
                reader._bank.seek(offset); blob = reader._bank.read(length)
                if len(blob) != length or zlib.crc32(blob) != crc:
                    raise ValueError('Correction source record CRC/length mismatch')
                view = old._record(blob, gid, w, h, 1)
                if view['raw_sha256'] != catalog['tiles'][gid]['native_sha256']:
                    raise ValueError('Correction source native pixels differ')
                (old_records / f'{gid:06d}.old089').write_bytes(blob)
        input_seconds = time.perf_counter() - then
        then = time.perf_counter()
        spool = output / 'spool'; spool.mkdir(); anchors = {}
        with (spool / 'index.tsv').open('x', encoding='ascii', newline='\n') as index:
            for desc in descriptors:
                gid, level, x, y, w, h = desc
                if level != 0:
                    continue
                pixels = scan.read_tile(census, gid, catalog=catalog)
                anchor, _ = conditioned._anchor(catalog['levels'], desc,
                    lambda aid: scan.read_tile(census, aid, catalog=catalog))
                raw, low = pixels.tobytes(), anchor.tobytes()
                anchors[gid] = hashlib.sha256(low).hexdigest()
                blob = conditioned._read_bounded(old_records / f'{gid:06d}.old089', old.MAX_PAYLOAD)
                if old._record(blob, gid, w, h, 1)['anchor_sha256'] != anchors[gid]:
                    raise ValueError('Correction source anchor differs from exact nativeL1')
                (spool / f'{gid:06d}.rgb').write_bytes(raw)
                (spool / f'{gid:06d}.anchor').write_bytes(low)
                (spool / f'{gid:06d}.old089').write_bytes(blob)
                index.write(f'{gid}\t{level}\t{x}\t{y}\t{w}\t{h}\t{gid:06d}.rgb\t{gid:06d}.anchor\t{gid:06d}.old089\n')
                if gid % 64 == 0:
                    print(json.dumps({'stage': 'joint_native_spool', 'gid': gid}), flush=True)
        spool_pins = _flat_pins(spool)
        spool_seconds = time.perf_counter() - then
        native = output / 'native'
        native_seconds = native_encode(native_runtime, 'hf1', spool, native)
        then = time.perf_counter()
        receipt = validate_native_directory(native, catalog, descriptors, anchors, old_records)
        validation_seconds = time.perf_counter() - then
        then = time.perf_counter()
        if qualified_runtimes(native_runtime, old_runtime)[2] != runtime_pins:
            raise ValueError('Runtime changed during paired encoding')
        if base._members(old_anchor_package) != old_pins or _flat_pins(spool) != spool_pins:
            raise ValueError('Correction source/spool changed during paired encoding')
        if _identity(base._catalog(census), census) != identity:
            raise ValueError('Census changed during paired encoding')
        final_check_seconds = time.perf_counter() - then
        result = {'ok': True, 'native_dir': str(native), 'native_receipt': receipt,
                  'native_receipt_path': str(native / 'receipt.json'), 'runtime_pins': runtime_pins,
                  'spool': str(spool), 'old_records': str(old_records), 'input_identity': identity,
                  'spool_identity': spool_pins, 'correction_package_members': old_pins,
                  'representation': 'dense_magnitude_sign_v1',
                  'timing': {'runtime_qualification_seconds': runtime_seconds,
                             'input_verification_and_correction_reuse_seconds': input_seconds,
                             'native_spool_seconds': spool_seconds, 'native_cli_seconds': native_seconds,
                             'native_receipt_validation_seconds': validation_seconds,
                             'final_input_runtime_verification_seconds': final_check_seconds,
                             'total_adapter_seconds': time.perf_counter() - started,
                             'scope': 'Incremental MS L0 work; old signed091/P/JXL/correction encoding time is historical'},
                  'scope': 'Same canonical signed HF, newly factorized dense magnitude/sign and fixed causal reference construction. Whole representation comparison, not a pure parent gate ablation. No neural training.'}
        write_json(output / 'adapter.json', result)
        (output / 'INCOMPLETE').unlink()
        return result
    except BaseException as exc:
        write_json(output / 'failure.json', {'type': type(exc).__name__, 'error': str(exc)})
        raise
