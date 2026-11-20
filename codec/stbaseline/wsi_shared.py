"""Opt-in Shared + byte-exact WJXL envelope; no codec or stable format changes.

The external wsi_jxl package must be explicitly on PYTHONPATH. Coefficient mode
requires its supported native runtime. A portable path never masquerades as that
mode. Coordinates refer to the selected native TIFF pages, not resampled pixels.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import h5py
import numpy as np
import tifffile

from .integrity import sha256, validate_hdf5_links, write_json
from .shared_streaming import SharedBlockArchive

FORMAT = 'stlossless-wsi-shared-1'
UPSTREAM_COMMIT = '935633b2b2b0cbd0a621b8780df7ec0d99cf9eae'
MAX_PIXELS = 150_000_000
MAX_SEGMENT = 128 * 1024 * 1024


def _name(value):
    return isinstance(value, str) and value not in ('', '.', '..') and Path(value).name == value and not any(c in value for c in '/\\:')


def _disjoint(output, *inputs):
    if Path(output).is_symlink():
        raise ValueError('Symlink output unsupported')
    output = Path(output).resolve()
    for p in inputs:
        p = Path(p).resolve()
        if output == p or p in output.parents or output in p.parents:
            raise ValueError('Output must be disjoint from inputs')
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    return output


def _wjxl():
    try:
        from wsi_jxl.reader import ArchiveReader
        from wsi_jxl.archive import decode_archive
    except ImportError as exc:
        raise RuntimeError('Add the pinned wsi-jxl-lossless/src to PYTHONPATH') from exc
    return ArchiveReader, decode_archive


def _runtime_pin():
    import wsi_jxl
    root = Path(wsi_jxl.__file__).parent
    native = {}
    for key in ('WSI_COEFFICIENT_LIBRARY', 'WSI_JXL_LIBRARY'):
        value = os.environ.get(key)
        if value:
            p = Path(value)
            if not p.is_file():
                raise RuntimeError('Configured WJXL runtime library does not exist: '+key)
            native[key] = {'name': p.name, 'bytes': p.stat().st_size, 'sha256': sha256(p)}
    return {'base_commit': UPSTREAM_COMMIT,
            'source_status': 'Actual file hashes below include local platform adaptations',
            'python_sources': {p.name: sha256(p) for p in sorted(root.glob('*.py'))},
            'explicit_native_libraries': native}


def _require_coefficients():
    try:
        from wsi_jxl.coefficients import _library
        _library()
    except (RuntimeError, OSError) as exc:
        raise RuntimeError('Coefficient-mode WJXL requires a built native adapter; on Windows set WSI_COEFFICIENT_LIBRARY to the qualified DLL. No silent fallback') from exc


def capabilities(image_file=None):
    """Read-only runtime/header check; never encode or decode source pixels."""
    report = {'format': FORMAT, 'native_runtime': {}, 'training': False}
    for name in ('coefficients', 'native'):
        try:
            from importlib import import_module
            lib = import_module('wsi_jxl.'+name)._library()
            report['native_runtime'][name] = {'available': True}
            if name == 'native':
                report['native_runtime'][name]['libjxl_version'] = lib.JxlEncoderVersion()
        except (ImportError, RuntimeError, OSError, AttributeError) as exc:
            report['native_runtime'][name] = {'available': False, 'reason': str(exc)}
    if image_file is not None:
        from wsi_jxl.archive import source_spans
        jpeg, opaque = 0, 0
        for span in source_spans(image_file):
            if span.prefix is not None:
                jpeg += 1
            else:
                opaque += 1
        report['input'] = {'jpeg_segments': jpeg, 'opaque_spans': opaque,
                           'coefficient_candidate': 'requires_grid_and_exact_replay_checks' if jpeg else 'not_applicable_no_JPEG_segments',
                           'scope': 'TIFF headers only; no source pixels decoded or reencoded'}
    return report


def _describe_wjxl(path):
    from wsi_jxl.archive import parse_group_header
    reader_type, _ = _wjxl()
    with reader_type(path, cache_groups=0) as r:
        modes = {}
        for _, header, _ in r._entries:
            mode = str(parse_group_header(header)[0])
            modes[mode] = modes.get(mode, 0) + 1
        if '5' in modes:
            _require_coefficients()
        return {**r.metadata, 'group_modes': modes, 'groups': len(r._entries)}


def _page_schema(page, shape):
    if (tuple(page.shape) != tuple(shape) or page.dtype != np.uint8
            or len(shape) != 3 or shape[2] != 3 or shape[0]*shape[1] > MAX_PIXELS
            or page.planarconfig != 1 or int(page.photometric) not in (2, 6)
            or int(page.tags.get(274).value if 274 in page.tags else 1) != 1
            or page.imagedepth != 1):
        raise ValueError('Unsupported or mismatched native RGB TIFF page')
    if int(page.photometric) == 6 and int(page.compression) != 7:
        raise ValueError('Only JPEG YCbCr-to-RGB decoding is qualified')
    if not page.dataoffsets or len(page.dataoffsets) != len(page.databytecounts):
        raise ValueError('Invalid TIFF segments')


class WSISharedArchive(SharedBlockArchive):
    """Lazy independent modalities, full-spot Count queries, exact native ROIs.

    Two decoded WJXL groups may be cached (<=256 MiB by format limits). No
    decoded image tile or Count cache. A TIFF strip can span the entire width;
    native pixel and WJXL byte-group amplification are measured separately.
    """
    def __init__(self, directory, *, cache_groups=2):
        self.root = Path(directory).resolve()
        if (self.root / 'INCOMPLETE').exists():
            raise ValueError('Incomplete archive')
        manifest = self.root / 'manifest.json'
        if manifest.is_symlink():
            raise ValueError('Unsafe manifest')
        blob = manifest.read_bytes()
        self.manifest, self.manifest_bytes = json.loads(blob), len(blob)
        m = self.manifest
        if m.get('format') != FORMAT or m.get('count_component') not in ('shared_local', 'shared_blocks') or m.get('image_component') != 'wjxl':
            raise ValueError('Unsupported Shared/WJXL package')
        attachments = m.get('attachments', [])
        reserved = {'manifest.json', 'science.h5', 'counts.bank', 'counts.index.json', 'images.wjxl', 'original_image.tif', 'expression.h5ad', 'source_metadata.json', 'failure.json', 'INCOMPLETE'}
        if (len(set(attachments)) != len(attachments) or any(not _name(n) or n in reserved for n in attachments)
                or set(m['files']) != {'science.h5', 'counts.bank', 'counts.index.json', 'images.wjxl', *attachments}
                or m['expression']['path'] != 'science.h5' or m['image']['path'] != 'images.wjxl'):
            raise ValueError('Unsafe package members')
        for name, desc in m['files'].items():
            p = self.root / name
            if not _name(name) or p.is_symlink() or not p.is_file() or p.stat().st_size != desc['bytes']:
                raise ValueError('Unsafe or size-mismatched package member')
        with h5py.File(self.root / 'science.h5', 'r') as handle:
            validate_hdf5_links(handle)
        if type(cache_groups) is not int or not 0 <= cache_groups <= 2:
            raise ValueError('Group cache must be 0, 1 or 2')
        levels, pages = m['image']['levels'], m['wjxl']['pages']
        if len(levels) != len(pages) or not levels or any(type(i) is not int or i < 0 for i in pages) or len(set(pages)) != len(pages):
            raise ValueError('Invalid page mapping')
        self.handles, self.files, self._metadata = {}, {}, None
        self._count_store, self._tiff, self._image_stream = None, None, None
        self._entries, self._level_start, self._decompressors = None, [], {}
        self.index_read_bytes = self.image_tiles_decoded = self.decoded_pixels = 0
        self._cache_groups = cache_groups
        self.ledger()

    def counts(self, rows=None, genes=None, *, max_cells=20_000_000):
        if genes is not None:
            raise ValueError('This interface returns complete spots, all genes in original order')
        return super().counts(rows, None, max_cells=max_cells)

    def _image_open(self):
        if self._image_stream is None:
            reader_type, _ = _wjxl()
            stream = reader_type(self.root / 'images.wjxl', cache_groups=self._cache_groups)
            try:
                expected = self.manifest['wjxl']
                if any(stream.metadata[k] != expected[k] for k in ('source_bytes', 'source_sha256')):
                    raise ValueError('WJXL source identity mismatch')
                tf = tifffile.TiffFile(stream)
                for info, pi in zip(self.manifest['image']['levels'], expected['pages']):
                    if pi >= len(tf.pages):
                        raise ValueError('Missing mapped TIFF page')
                    _page_schema(tf.pages[pi], info['shape'])
                self._tiff, self._image_stream = tf, stream
            except BaseException:
                stream.close()
                raise

    def image(self, level, bbox):
        info = self._level(level)
        if len(bbox) != 4 or any(isinstance(v, (bool, np.bool_)) or not isinstance(v, (int, np.integer)) for v in bbox):
            raise ValueError('Image ROI requires four integers')
        x0, y0, x1, y1 = map(int, bbox)
        h, w, _ = info['shape']
        if not 0 <= x0 < x1 <= w or not 0 <= y0 < y1 <= h:
            raise ValueError('Image ROI outside native bounds')
        self._image_open()
        page = self._tiff.pages[self.manifest['wjxl']['pages'][level]]
        th, tw = (page.tilelength, page.tilewidth) if page.is_tiled else (page.rowsperstrip, w)
        nx = (w + tw - 1) // tw
        out = np.empty((y1-y0, x1-x0, 3), np.uint8)
        for ty in range(y0//th, (y1-1)//th+1):
            for tx in range(x0//tw, (x1-1)//tw+1):
                index = ty*nx+tx
                offset, length = int(page.dataoffsets[index]), int(page.databytecounts[index])
                if offset < 0 or not 0 < length <= MAX_SEGMENT or offset+length > self._image_stream.size or th*tw*3 > MAX_SEGMENT:
                    raise ValueError('TIFF segment exceeds bounded decoder contract')
                self._image_stream.seek(offset)
                blob = self._image_stream.read(length)
                decoded = page.decode(blob, index, jpegtables=page.jpegtables)[0]
                if decoded is None or decoded.ndim != 4 or decoded.shape[0] != 1 or decoded.dtype != np.uint8 or decoded.shape[-1] != 3:
                    raise ValueError('Unexpected decoded RGB segment')
                pixels = decoded[0]
                xa, ya, xb, yb = max(x0,tx*tw), max(y0,ty*th), min(x1,(tx+1)*tw), min(y1,(ty+1)*th)
                out[ya-y0:yb-y0, xa-x0:xb-x0] = pixels[ya-ty*th:yb-ty*th, xa-tx*tw:xb-tx*tw]
                self.image_tiles_decoded += 1
                self.decoded_pixels += pixels.shape[0]*pixels.shape[1]
        return out

    def metrics(self):
        # Reuse Shared counters; image stream is separate from HDF5 files.
        r = super().metrics()
        s = self._image_stream
        values = {k: getattr(s, k, 0) for k in ('index_bytes_read', 'payload_bytes_read', 'decoded_groups', 'decoded_source_bytes')}
        values.update(decoded_pixels=self.decoded_pixels, cache_groups=self._cache_groups,
                      resident_group_bytes=sum(len(v) for v in s._cache.values()) if s else 0,
                      decoded_pixel_cache_bytes=0)
        r['wjxl'] = values
        r['total_file_read_bytes'] += values['index_bytes_read'] + values['payload_bytes_read']
        return r

    def verify(self):
        result = super().verify()
        _, decode = _wjxl()
        whole = decode(self.root / 'images.wjxl')
        if whole['sha256'] != self.manifest['wjxl']['source_sha256']:
            raise ValueError('Original TIFF byte digest mismatch')
        result.update(original_image_file_exact=True, original_image_sha256=whole['sha256'])
        return result

    def restore_image_file(self, target):
        target = _disjoint(target, self.root)
        if sha256(self.root/'images.wjxl') != self.manifest['files']['images.wjxl']['sha256']:
            raise ValueError('WJXL file integrity failure')
        _, decode = _wjxl()
        result = decode(self.root/'images.wjxl', target)
        if result['sha256'] != self.manifest['wjxl']['source_sha256']:
            raise ValueError('Original TIFF byte digest mismatch')
        return result

    def restore(self, destination):
        dest = _disjoint(destination, self.root)
        self.verify()
        dest.mkdir(parents=True, exist_ok=False)
        self.restore_expression(dest/'expression.h5ad')
        self.restore_image_file(dest/'original_image.tif')
        for name in self.manifest.get('attachments', []):
            shutil.copyfile(self.root/name, dest/name)
        shutil.copyfile(self.root/'manifest.json', dest/'source_metadata.json')
        return str(dest)

    def close(self):
        super().close()
        if self._image_stream is not None:
            self._image_stream.close()
            self._image_stream = None


def assemble(shared, image_archive, output, *, pages=None):
    """Bind an existing WJXL image to unchanged Shared scientific contents.

    Image identity is checked against every native pixel digest in Shared, not
    only shape or file names. Existing mode5 streams work on supported runtimes.
    """
    started = time.perf_counter()
    if Path(image_archive).is_symlink():
        raise ValueError('Symlink image archive unsupported')
    shared, image_archive = Path(shared).resolve(), Path(image_archive).resolve()
    out = _disjoint(output, shared, image_archive)
    stage = _disjoint(out.with_name(out.name+'.partial'), shared, image_archive)
    if image_archive.is_symlink():
        raise ValueError('Symlink image archive unsupported')
    description = _describe_wjxl(image_archive)
    with SharedBlockArchive(shared) as base:
        if base.manifest['count_component'] not in ('shared_local', 'shared_blocks'):
            raise ValueError('An existing Shared spatial package is required')
        m = copy.deepcopy(base.manifest)
        before = {name: sha256(shared/name) for name in m['files']}
        if any(before[n] != d['sha256'] for n, d in m['files'].items()):
            raise ValueError('Input package integrity failure')
    image_sha = sha256(image_archive)
    page_map = list(range(len(m['image']['levels']))) if pages is None else list(pages)
    keep = {'science.h5', 'counts.bank', 'counts.index.json', *m.get('attachments', [])}
    stage.mkdir(parents=True, exist_ok=False)
    try:
        for name in sorted(keep):
            shutil.copyfile(shared/name, stage/name)
        shutil.copyfile(image_archive, stage/'images.wjxl')
        m.update(format=FORMAT, image_component='wjxl')
        m['image']['path'] = 'images.wjxl'
        m['image']['codec'] = 'wjxl-byte-exact-grouped-source-file'
        m['wjxl'] = {**description, 'pages': page_map, 'runtime': _runtime_pin(),
                     'contract': 'Entire supplied TIFF/SVS file exact; native page pixels match Shared image digests'}
        # Do not inherit the replaced JPEG XL tile policy/effort counters or
        # claim that the old whole-package build receipt describes this image.
        m['image_parameters'] = {'container': 'WJXL001', 'input_layout_preserved': True,
                                 'pixel_decode_unit': 'source TIFF tiles or strips',
                                 'coefficient_groups': description['group_modes'].get('5', 0)}
        m['build'] = {'operation': 'bind-existing-Shared-and-WJXL',
                      'source_shared_manifest_sha256': sha256(shared/'manifest.json'),
                      'shared_component_build': m.get('build'),
                      'timings': 'External task receipt; original image policy superseded'}
        m['sources']['wsi'] = {'name': description['source_name'], 'bytes': description['source_bytes'], 'sha256': description['source_sha256']}
        m['files'] = {p.name: {'bytes': p.stat().st_size, 'sha256': sha256(p)} for p in sorted(stage.iterdir())}
        write_json(stage/'manifest.json', m)
        with WSISharedArchive(stage) as reader:
            verified = reader.verify()
            ledger = reader.ledger()
        if sha256(image_archive) != image_sha or any(sha256(shared/n) != s for n, s in before.items()):
            raise ValueError('Input changed during integration')
        if out.exists():
            raise FileExistsError(out)
        stage.rename(out)
    except BaseException as exc:
        write_json(stage/'failure.json', {'error': str(exc)})
        raise
    return {'verified': verified, 'ledger': ledger, 'assembly_seconds': time.perf_counter()-started,
            'copied_shared_members': sorted(keep), 'wjxl': description}


def pack(shared, image_file, output, workspace, *, profile='joint_entropy'):
    """Encode exactly one supplied image file, then bind it to Shared.

    The upstream joint_entropy encoder may fit per-input reversible prediction
    parameters; those paid codec parameters are not neural model training.
    Unsupported raw TIFF spans use its original exact opaque-byte path.
    """
    if profile not in ('joint_entropy', 'portable'):
        raise ValueError('Unknown WJXL profile')
    out = _disjoint(output, shared, image_file)
    work = _disjoint(workspace, shared, image_file, out)
    from wsi_jxl.archive import encode_archive, source_spans
    jpeg_spans = sum(s.prefix is not None for s in source_spans(image_file))
    if profile == 'joint_entropy' and jpeg_spans:
        _require_coefficients()
        from wsi_jxl.native import _library
        _library()
    work.mkdir(parents=True, exist_ok=False)
    start = time.perf_counter()
    try:
        if profile == 'joint_entropy':
            from wsi_jxl.joint_archive import encode_joint_archive
            encoded = encode_joint_archive(image_file, work/'image.wjxl', group_tiles=16, coefficient_mode='entropy')
        else:
            encoded = encode_archive(image_file, work/'image.wjxl', group_tiles=16, workers=1, jpeg_effort=None)
        write_json(work/'encode.json', encoded)
        result = assemble(shared, work/'image.wjxl', out)
        result.update(profile=profile, jpeg_spans=jpeg_spans, encoding=encoded,
                      coefficient_mode_selected=encoded.get('selected_modes', {}).get('entropy', 0) > 0,
                      total_seconds=time.perf_counter()-start,
                      scope='Image encoding + copied existing Shared components + full verification; not full-source Count encoding')
        write_json(work/'result.json', result)
        return result
    except BaseException as exc:
        write_json(work/'failure.json', {'error': str(exc)})
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    a = sub.add_parser('capabilities')
    a.add_argument('--image')
    b = sub.add_parser('pack')
    for name in ('shared', 'image', 'output', 'workspace'):
        b.add_argument('--'+name, required=True)
    b.add_argument('--profile', choices=['joint_entropy', 'portable'], default='joint_entropy')
    a = sub.add_parser('attach')
    for name in ('shared', 'wjxl', 'output'):
        a.add_argument('--'+name, required=True)
    for command in ('inspect', 'verify', 'restore', 'counts', 'image', 'region'):
        a = sub.add_parser(command)
        a.add_argument('archive')
        if command == 'restore': a.add_argument('output')
        if command in ('image', 'region'):
            a.add_argument('--bbox', nargs=4, type=int, required=True)
            a.add_argument('--level', type=int, default=0)
        if command == 'counts': a.add_argument('--rows', nargs='*', type=int)
        if command in ('counts', 'image', 'region'): a.add_argument('--output', required=True)
    args = p.parse_args()
    if args.command == 'capabilities':
        result = capabilities(args.image)
    elif args.command == 'pack':
        result = pack(args.shared, args.image, args.output, args.workspace, profile=args.profile)
    elif args.command == 'attach':
        result = assemble(args.shared, args.wjxl, args.output)
    else:
        with WSISharedArchive(args.archive) as r:
            if args.command == 'inspect': result = {'manifest': r.manifest, 'ledger': r.ledger()}
            elif args.command == 'verify': result = r.verify()
            elif args.command == 'restore': result = r.restore(args.output)
            else:
                out = _disjoint(args.output, r.root)
                if args.command == 'counts': result = r.counts(args.rows)
                elif args.command == 'image': result = {'image': r.image(args.level, args.bbox)}
                else: result = r.region(args.bbox, level=args.level)
                with out.open('xb') as f: np.savez(f, **result)
                result = {'output': str(out), 'metrics': r.metrics()}
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
