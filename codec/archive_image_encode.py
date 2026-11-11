"""From-source image encoding extracted from the verified 262-sample build."""
import copy
from pathlib import Path

import hf_adapter as hf
import joint_next as j
import l1_adapter as la
import native_adapter as na
from stbaseline import slide_redundancy as base, slide_redundancy_scan as scan
from stbaseline import wsi_squeeze089 as old
from stbaseline.integrity import write_json
from receipt_bounds import install

install(old, na.conditioned)


def encode_images(census, catalog, work, output, runtimes, timed):
    R089, R091, RHF = runtimes
    directdir = work / 'direct_records'
    directdir.mkdir()
    direct = []
    def direct_encode():
        for tile in catalog['tiles']:
            blob = base._encode(scan.read_tile(census, tile['gid'], catalog=catalog))
            direct.append((1, blob))
            (directdir / f'{tile["gid"]:06d}.bin').write_bytes(blob)
    timed('direct_JXL9_all_tiles_including_immediate_decode_check', direct_encode)
    natives = []
    for level in (0, 1):
        stage = work / ('P089_L' + str(level))
        stage.mkdir()
        spool = stage / 'spool'
        spool.mkdir()
        anchors = {}
        descs = [d for d in base._descriptors(catalog['levels']) if d[1] == level]
        virtual = copy.deepcopy(catalog['levels'][level:])
        for index, info in enumerate(virtual):
            info['level'] = index
        def prepare_p():
            lines, mappings = [], []
            for rid, desc in enumerate(descs):
                gid, _, x, y, w, h = desc
                rgb = scan.read_tile(census, gid, catalog=catalog)
                anchor, parent = j.anchor_pixels(catalog['levels'], desc,
                    lambda aid: scan.read_tile(census, aid, catalog=catalog))
                (spool / f'{rid:06d}.rgb').write_bytes(rgb.tobytes())
                (spool / f'{rid:06d}.anchor').write_bytes(anchor.tobytes())
                anchors[rid] = j.digest(anchor.tobytes())
                lines.append(f'{rid}\t0\t{x}\t{y}\t{w}\t{h}\t{rid:06d}.rgb\t{rid:06d}.anchor')
                mappings.append({'global_gid': gid, 'local_gid': rid, 'level': level,
                                 'abi_level': 0, 'parent_global_gid': parent})
            (spool / 'index.tsv').write_text('\n'.join(lines)+'\n', encoding='ascii')
            write_json(stage / 'mappings.json', mappings)
        timed(f'P089_L{level}_source_spooling', prepare_p)
        timed(f'P089_L{level}_native_all_candidates', lambda: na.native_encode(R089, '089', spool, stage / 'native'))
        start = j.starts(catalog['levels'])[level]
        vcat = {'levels': virtual, 'tiles': [dict(native_sha256=t['native_sha256']) for t in catalog['tiles'][start:]]}
        timed(f'P089_L{level}_native_receipt_validation', lambda: old._validate_native(stage / 'native', vcat, list(base._descriptors(virtual)), anchors))
        natives.append(stage / 'native')
    timed('P089_direct_dependency_package_assembly', lambda: old._assemble(catalog, census, directdir,
          natives[0], work / 'old_direct', 'direct', old._runtime(R089)[1], {}))
    timed('P089_correction_source_package_assembly', lambda: old._assemble(catalog, census, directdir,
          natives[0], work / 'anchor', 'anchor_pure', old._runtime(R089)[1], base._members(work / 'old_direct')))
    prior = timed('L0_091_all_candidate_adapter_including_checks', lambda: na.build_native(census, work / 'anchor', work / 'prior091', R091, R089))
    high = timed('L0_HF_all_candidate_adapter_including_checks', lambda: hf.build_native(census, work / 'anchor', work / 'hf', RHF, R089))
    low = timed('L1_091_all_candidate_adapter_including_checks', lambda: la.build_native(census, natives[1], work / 'l1', R091, R089))
    retained = list(direct)
    def retention():
        for gid, level, *_ in base._descriptors(catalog['levels']):
            if level > 1:
                continue
            rid = gid-j.starts(catalog['levels'])[level]
            retained[gid] = j.choose(retained[gid], [(3, (natives[level] / 'anchored' / f'{rid:06d}.bin').read_bytes())])
            if level == 0:
                retained[gid] = j.choose(retained[gid], [(5, (Path(prior['native_dir']) / 'conditional' / f'{rid:06d}.bin').read_bytes())])
    timed('retained_089_091_strict_paid_record_selection', retention)
    hrecords = {t['gid']: (6, (Path(high['native_dir']) / t['ms_path']).read_bytes()) for t in high['native_receipt']['tiles']}
    pins = dict(low['runtime_pins'], hf=hf._runtime(RHF)[1])
    images = output
    images.mkdir()
    choices = {}
    for profile in ('direct', 'hf_l1_conditional'):
        choices[profile] = timed(profile + '_selection_and_image_package_assembly',
            lambda profile=profile: j.write_package(catalog, census, direct, retained,
                hrecords, Path(low['native_dir']), natives[1], images / profile, profile, pins))
    write_json(work / 'candidate_accounting.json', {
        'all_tiles_direct_encoded': len(direct), 'L0_tiles': j.starts(catalog['levels'])[1],
        'L1_tiles': j.starts(catalog['levels'])[2]-j.starts(catalog['levels'])[1],
        'choices': choices, 'adapter_timings': {'091_L0': prior['timing'], 'HF_L0': high['timing'], '091_L1': low['timing']},
        'scope': 'Every source-derived candidate was generated in this job. Adapter wording about historical inputs describes its local dependency; those dependencies were freshly encoded and charged above. No reused NCBI466 streams.'})
    return images



