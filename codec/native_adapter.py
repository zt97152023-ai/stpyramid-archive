"""New-task adapter for the immutable, qualified 091 paired Modular backend.

This module deliberately does not call 091.build or its historical execution
gate. The new task uses paid 089 corrections and JXL9 references; only the L0
paired HF streams are new work. All output identities are create-only.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import zlib
from st262_controls import assigned_cpu, remaining_seconds

from stbaseline import slide_redundancy as base
from stbaseline import slide_redundancy_scan as scan
from stbaseline import wsi_conditioned091 as conditioned
from stbaseline import wsi_squeeze089 as old
from stbaseline.integrity import sha256, write_json
from stbaseline.wsi_shared import _disjoint


THREAD_VARS = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
               'NUMEXPR_NUM_THREADS', 'NUMBA_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'BLIS_NUM_THREADS')


def qualified_runtimes(runtime091, runtime089):
    """Require self-consistent explicit runtime receipts for both native stages."""
    root091, pins091 = conditioned._runtime(runtime091)
    root089, pins089 = old._runtime(runtime089)
    return root091, root089, {'089': pins089, '091': pins091}


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
    if number not in ('089', '091'):
        raise ValueError('Only frozen089/091 native children are supported')
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
    started = time.perf_counter()
    with stdout.open('x', encoding='utf8') as out, stderr.open('x', encoding='utf8') as err:
        result = subprocess.run([str(runtime / ('slide_jxl' + number + '.exe')), 'encode', str(spool), str(target)],
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
    """Check exact directory coverage before the qualified record/receipt parser."""
    native = Path(native)
    if native.is_symlink() or not native.is_dir():
        raise ValueError('Missing or unsafe native output')
    if {p.name for p in native.iterdir()} != {'control', 'conditional', 'receipt.json'}:
        raise ValueError('Unexpected or missing native output member')
    expected = {f'{d[0]:06d}.bin' for d in descriptors if d[1] == 0}
    for arm in ('control', 'conditional'):
        member = native / arm
        if member.is_symlink() or not member.is_dir() or {p.name for p in member.iterdir()} != expected:
            raise ValueError('Native arm coverage differs from exact L0 directory')
        if any(p.is_symlink() or not p.is_file() for p in member.iterdir()):
            raise ValueError('Unsafe native arm member')
    return conditioned._validate_native(native, catalog, descriptors, anchors, Path(old_records))


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
                 runtime091: Path, runtime089: Path) -> dict:
    """Encode one paired L0 bank with shared transform/correction and paid models.

    Native lower levels and JXL9 references are not reencoded. The complete old
    anchor-pure package is verified and its L0 correction frames are copied.
    Returned timing is incremental paired-bank work, never full-slide encoding.
    """
    started = time.perf_counter()
    for value in (census, old_anchor_package, runtime091, runtime089):
        if Path(value).is_symlink() or not Path(value).is_dir():
            raise ValueError('Adapter inputs must be existing real directories')
    census, old_anchor_package = Path(census).resolve(), Path(old_anchor_package).resolve()
    output = _disjoint(output_dir, census, old_anchor_package, runtime091, runtime089)
    output.mkdir(parents=True)
    (output / 'INCOMPLETE').write_text('WSI-RD-JOINT paired native bank\n', encoding='utf8')
    try:
        then = time.perf_counter()
        native_runtime, old_runtime, runtime_pins = qualified_runtimes(runtime091, runtime089)
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
        native_seconds = native_encode(native_runtime, '091', spool, native)
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
                  'timing': {'runtime_qualification_seconds': runtime_seconds,
                             'input_verification_and_correction_reuse_seconds': input_seconds,
                             'native_spool_seconds': spool_seconds, 'native_cli_seconds': native_seconds,
                             'native_receipt_validation_seconds': validation_seconds,
                             'final_input_runtime_verification_seconds': final_check_seconds,
                             'total_adapter_seconds': time.perf_counter() - started,
                             'scope': 'Paired incremental L0 work; shared work is not split into invented per-arm times; old089 correction/P/JXL encoding time is historical'},
                  'scope': 'Same coefficients and backend; only explicit known-parent property gate differs. Both arms retain parent-derived threshold candidates. No neural training.'}
        write_json(output / 'adapter.json', result)
        (output / 'INCOMPLETE').unlink()
        return result
    except BaseException as exc:
        write_json(output / 'failure.json', {'type': type(exc).__name__, 'error': str(exc)})
        raise


