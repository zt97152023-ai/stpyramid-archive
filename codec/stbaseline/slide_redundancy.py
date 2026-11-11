"""Whole-slide exact-content sharing; no lossy background decisions or chains.

The paired direct package contains actual repeated JPEG XL payloads. Both modes
are built from the same unique encodes, so paired build time is not an independent
baseline encoding benchmark. The source census is build input only.
"""
import base64
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import struct
import time
import zlib

import numpy as np

from .integrity import sha256, write_json
from .reader import MeteredFile
from .wsi_shared import _disjoint, _name
from .wsi_source import NativeWSISource
from . import slide_redundancy_scan as scan

FORMAT = 'stlossless-slide-sharing-image-086-1'
MAGIC = b'STRED086'
HEADER = struct.Struct('<8sII')
ENTRY = struct.Struct('<QIB3xI')
TILE = 256
JXL, CONSTANT = 1, 3
MAX_TILES = 200_000
MAX_ROI_PIXELS = 16_777_216
MAX_STRIPE_BYTES = 128*1024*1024
MAX_PAYLOAD = 2*1024*1024
MAX_QUERY_CACHE_BYTES = 8*1024*1024
MAX_MANIFEST_BYTES = 4*1024*1024
MEMBERS = {'images.bank','images.index','source.json'}
HEX = re.compile(r'[0-9a-f]{64}')


def _integer(v):
    return isinstance(v,(int,np.integer)) and not isinstance(v,(bool,np.bool_))


def _descriptors(levels):
    if not isinstance(levels,list) or not 1 <= len(levels) <= 16:
        raise ValueError('Invalid native level directory')
    total = 0; previous = None
    for i,info in enumerate(levels):
        shape = info.get('shape')
        if (not _integer(info.get('level')) or info.get('level') != i or info.get('dtype') != 'uint8' or not isinstance(shape,list)
                or len(shape) != 3 or shape[2] != 3 or any(not _integer(v) or v < 1 for v in shape)):
            raise ValueError('Native RGB8 level required')
        h,w,_ = shape
        if previous and not (h <= previous[0] and w <= previous[1] and (h,w) != previous):
            raise ValueError('Native levels must decrease')
        if min(TILE,h)*w*3 > MAX_STRIPE_BYTES:
            raise ValueError('Native stripe exceeds128MiB')
        if not isinstance(info.get('pixel_sha256'),str) or HEX.fullmatch(info['pixel_sha256']) is None:
            raise ValueError('Native raster SHA required')
        previous = (h,w)
        total += ((h+255)//256)*((w+255)//256)
        if total > MAX_TILES:
            raise ValueError('Image exceeds bounded200000tile codec profile')
    gid = 0
    for info in levels:
        h,w,_ = info['shape']
        for y in range(0,h,TILE):
            for x in range(0,w,TILE):
                yield gid,info['level'],x,y,min(TILE,w-x),min(TILE,h-y)
                gid += 1


def _members(root):
    return {p.name:{'bytes':p.stat().st_size,'sha256':sha256(p)}
            for p in sorted(Path(root).iterdir()) if p.is_file()}


def _catalog(path):
    c = scan.load_catalog(path,verify_spool=True)
    descriptors = list(_descriptors(c['levels']))
    if len(c['tiles']) != len(descriptors):
        raise ValueError('Census tile coverage mismatch')
    for record,desc in zip(c['tiles'],descriptors):
        gid,level,x,y,w,h = desc
        if any(record.get(k) != v for k,v in zip(('gid','level','x','y','w','h'),desc)):
            raise ValueError('Census tile identity mismatch')
        canonical = record['canonical_gid']
        if not _integer(canonical) or not 0 <= canonical <= gid:
            raise ValueError('Census reference outside earlier canonical directory')
        parent = c['tiles'][canonical]
        if (parent['canonical_gid'] != canonical or (w,h) != (parent['w'],parent['h'])
                or record['native_sha256'] != parent['native_sha256']
                or record['constant_rgb'] != parent['constant_rgb']):
            raise ValueError('Census reference chain or content identity mismatch')
    return c


def _classes(catalog):
    counts = {}
    for tile in catalog['tiles']:
        gid = tile['canonical_gid']; counts[gid] = counts.get(gid,0)+1
    return counts


def _encode(pixels):
    import imagecodecs
    blob = imagecodecs.jpegxl_encode(np.ascontiguousarray(pixels),lossless=True,distance=0,effort=9,numthreads=1)
    if not 0 < len(blob) <= MAX_PAYLOAD:
        raise ValueError('JXL payload outside bounded profile')
    np.testing.assert_array_equal(imagecodecs.jpegxl_decode(blob,numthreads=1,out=np.empty_like(pixels)),pixels)
    return blob


def probe(censusdir, output):
    """Actual JXL payload omission bound, not a full-package rate claim."""
    started = time.perf_counter(); censusdir = Path(censusdir).resolve()
    out = _disjoint(output,censusdir); out.mkdir(parents=True)
    (out/'INCOMPLETE').write_text('Redundancy cost probe\n',encoding='utf8')
    try:
        catalog_sha=sha256(censusdir/'catalog.json')
        c = _catalog(censusdir); counts = _classes(c); streams = out/'streams'; streams.mkdir()
        items=[]; gross=0
        for gid,n in sorted(counts.items()):
            tile=c['tiles'][gid]; constant=tile['constant_rgb'] is not None
            if not constant and n==1: continue
            pixels=scan.read_tile(censusdir,gid,catalog=c)
            blob=_encode(pixels); (streams/f'{gid:08d}.jxl').write_bytes(blob)
            omitted=(n if constant else n-1)*len(blob); gross+=omitted
            items.append({'canonical_gid':gid,'multiplicity':n,'constant':constant,
                'jxl_bytes':len(blob),'jxl_sha256':hashlib.sha256(blob).hexdigest(),
                'gross_omitted_payload_bytes':omitted})
        if sha256(censusdir/'catalog.json')!=catalog_sha: raise ValueError('Census changed during cost probe')
        result={'ok':True,'eligible_classes':len(items),'items':items,'gross_omitted_payload_bytes':gross,
                'execution_gate_met':gross>=16384,'gate_bytes':16384,'elapsed_seconds':time.perf_counter()-started,
                'scope':'Actual eligible JXL streams only; not a complete package or independent direct encoding benchmark',
                'source_catalog_sha256':catalog_sha}
        write_json(out/'result.json',result); (out/'INCOMPLETE').unlink()
        return result
    except BaseException as exc:
        write_json(out/'failure.json',{'type':type(exc).__name__,'error':str(exc)})
        raise


def _constant_record(rgb):
    raw=bytes(rgb)
    if len(raw)!=3: raise ValueError('RGB triplet required')
    return int.from_bytes(raw,'little'),0,CONSTANT,zlib.crc32(raw)


def _write_package(censusdir,catalog,encoded,target,profile):
    import imagecodecs
    target.mkdir()
    shutil.copyfile(censusdir/'source.json',target/'source.json')
    if sha256(target/'source.json')!=catalog['files']['source.json']['sha256']:
        raise ValueError('Source metadata changed during packaging')
    spans={}; constant_tiles=aliases=0
    with (target/'images.bank').open('xb') as bank,(target/'images.index').open('xb') as index:
        index.write(HEADER.pack(MAGIC,TILE,len(catalog['tiles'])))
        for tile in catalog['tiles']:
            gid=tile['canonical_gid']
            if profile=='shared' and tile['constant_rgb'] is not None:
                record=_constant_record(tile['constant_rgb']); constant_tiles+=1
            elif profile=='shared' and gid in spans:
                record=spans[gid]; aliases+=1
            else:
                blob=(encoded/f'{gid:08d}.jxl').read_bytes()
                record=(bank.tell(),len(blob),JXL,zlib.crc32(blob)); bank.write(blob)
                if profile=='shared': spans[gid]=record
            index.write(ENTRY.pack(*record))
    manifest={'format':FORMAT,'profile':profile,'image_only':True,'image':{'levels':copy.deepcopy(catalog['levels']),
              'path':'images.bank','index':'images.index','source_metadata':'source.json'},
              'parameters':{'tile':TILE,'jpegxl_effort':9,'threads':1,'exact_constant_only':True,
              'alias':'same physical JXL span, equal valid shape; no chains','training':False,
              'persistent_decoded_pixel_cache':False,'per_query_cache_bytes':MAX_QUERY_CACHE_BYTES,
              'max_tiles':MAX_TILES,'max_roi_pixels':MAX_ROI_PIXELS},
              'build':{'task':'STBASELINE-086','adapter_sha256':sha256(Path(__file__)),
              'census_catalog_sha256':sha256(censusdir/'catalog.json'),'jpegxl':imagecodecs.jpegxl_version(),
              'constant_tiles':constant_tiles,'alias_tiles':aliases,'logical_tiles':len(catalog['tiles']),
              'capacity_control':'actual unique encoded streams; direct physically repeats records; not independent encoding time'},
              'files':_members(target),
              'exactness':'All stored native RGB values/source metadata; not original TIFF container bytes'}
    write_json(target/'manifest.json',manifest)


def pack_pair(censusdir, output, workspace):
    """Build and verify actual direct/shared packages, with bounded unique spool."""
    started=time.perf_counter(); censusdir=Path(censusdir).resolve()
    out=_disjoint(output,censusdir); stage=_disjoint(out.with_name(out.name+'.partial'),censusdir)
    work=_disjoint(workspace,censusdir,out,stage); stage.mkdir(parents=True)
    (stage/'INCOMPLETE').write_text('Paired image build\n',encoding='utf8')
    try:
        work.mkdir(parents=True); catalog_sha=sha256(censusdir/'catalog.json')
        c=_catalog(censusdir); encoded=work/'encoded'; encoded.mkdir(); counts=_classes(c)
        then=time.perf_counter()
        for gid in sorted(counts):
            blob=_encode(scan.read_tile(censusdir,gid,catalog=c))
            (encoded/f'{gid:08d}.jxl').write_bytes(blob)
        encoding_seconds=time.perf_counter()-then; results={}
        for profile in ('direct','shared'):
            then=time.perf_counter(); _write_package(censusdir,c,encoded,stage/profile,profile)
            writing_seconds=time.perf_counter()-then; then=time.perf_counter()
            with Reader(stage/profile) as reader: verified=reader.verify(); ledger=reader.ledger()
            results[profile]={'verified':verified,'ledger':ledger,'writing_seconds':writing_seconds,
                              'verification_seconds':time.perf_counter()-then,'members':_members(stage/profile)}
        if sha256(censusdir/'catalog.json') != catalog_sha or any(json.loads((stage/profile/'manifest.json').read_bytes())['build']['census_catalog_sha256']!=catalog_sha for profile in ('direct','shared')):
            raise ValueError('Census changed during build')
        (stage/'INCOMPLETE').unlink(); stage.rename(out)
        return {'ok':True,'profiles':results,'unique_encoded_classes':len(counts),
                'unique_encode_with_roundtrip_seconds':encoding_seconds,'total_pair_seconds':time.perf_counter()-started,
                'scope':'Build both actual image packages using shared unique encodes; not independent baseline encode time',
                'package_saving_bytes':results['direct']['ledger']['total_package_bytes']-results['shared']['ledger']['total_package_bytes']}
    except BaseException as exc:
        write_json(stage/'failure.json',{'type':type(exc).__name__,'error':str(exc)})
        raise


class Reader:
    def __init__(self,directory):
        arg=Path(directory); self.root=arg.resolve(); path=self.root/'manifest.json'
        if arg.is_symlink() or path.is_symlink() or (self.root/'failure.json').exists() or (self.root/'INCOMPLETE').exists():
            raise ValueError('Unsafe or incomplete sharing archive')
        if path.stat().st_size>MAX_MANIFEST_BYTES: raise ValueError('Manifest exceeds bounded profile')
        blob=path.read_bytes(); self.manifest=json.loads(blob); self.manifest_bytes=len(blob); m=self.manifest
        if m.get('format')!=FORMAT or m.get('profile') not in ('direct','shared') or m.get('image_only') is not True:
            raise ValueError('Unsupported sharing format')
        if set(m['files'])!=MEMBERS or m['image'].get('path')!='images.bank' or m['image'].get('index')!='images.index' or m['image'].get('source_metadata')!='source.json':
            raise ValueError('Unexpected sharing members')
        parameters=m.get('parameters',{})
        fixed={'tile':TILE,'jpegxl_effort':9,'threads':1,'max_tiles':MAX_TILES,
               'max_roi_pixels':MAX_ROI_PIXELS,'per_query_cache_bytes':MAX_QUERY_CACHE_BYTES}
        if any(not _integer(parameters.get(k)) or parameters[k]!=v for k,v in fixed.items()) or parameters.get('persistent_decoded_pixel_cache') is not False or parameters.get('training') is not False or parameters.get('exact_constant_only') is not True:
            raise ValueError('Unsupported fixed image-sharing profile')
        self._descriptors=list(_descriptors(m['image']['levels']))
        for name,desc in m['files'].items():
            member=self.root/name
            if not _name(name) or member.is_symlink() or not member.is_file() or type(desc.get('bytes')) is not int or desc['bytes']<0 or member.stat().st_size!=desc['bytes'] or not isinstance(desc.get('sha256'),str) or HEX.fullmatch(desc['sha256']) is None:
                raise ValueError('Unsafe or size-mismatched sharing member')
        if m['files']['source.json']['bytes']>scan.MAX_SOURCE_METADATA_BYTES:
            raise ValueError('Source metadata exceeds bounded profile')
        if m['files']['images.index']['bytes']!=HEADER.size+len(self._descriptors)*ENTRY.size:
            raise ValueError('Index byte count differs from native coverage')
        self._entries=None; self._bank=None; self._level_start=[]; self.index_read_bytes=0
        self._source_metadata=None
        self.jxl_tiles_decoded=self.constant_tiles_filled=self.logical_tiles_read=0
        self.decoded_pixels=self.requested_pixels=0
        self.cache_hits=0; self.last_query_cache_bytes=0; self.last_query_unique_payloads=0

    def __enter__(self): return self
    def __exit__(self,*args): self.close()
    def close(self):
        if self._bank is not None: self._bank.close(); self._bank=None

    def ledger(self):
        members={k:v['bytes'] for k,v in self.manifest['files'].items()}; members['manifest.json']=self.manifest_bytes
        return {'members':members,'total_package_bytes':sum(members.values()),'scope':'All image payload/index/source metadata/manifest; Count absent; runtime and scratch separate'}

    def _setup(self):
        if self._entries is not None: return
        path=self.root/'images.index'; blob=path.read_bytes(); self.index_read_bytes+=len(blob)
        if hashlib.sha256(blob).hexdigest()!=self.manifest['files']['images.index']['sha256'] or len(blob)<HEADER.size:
            raise ValueError('Sharing index integrity')
        magic,tile,count=HEADER.unpack_from(blob)
        if magic!=MAGIC or tile!=TILE or count!=len(self._descriptors) or len(blob)!=HEADER.size+count*ENTRY.size:
            raise ValueError('Sharing index coverage/header')
        entries=[ENTRY.unpack_from(blob,HEADER.size+i*ENTRY.size) for i in range(count)]
        if any(blob[HEADER.size+i*ENTRY.size+13:HEADER.size+i*ENTRY.size+16]!=b'\x00'*3 for i in range(count)):
            raise ValueError('Nonzero reserved index fields')
        size=self.manifest['files']['images.bank']['bytes']; spans={}; direct_end=0
        for record,desc in zip(entries,self._descriptors):
            offset,length,codec,crc=record; _,_,_,_,w,h=desc
            if codec==CONSTANT:
                if self.manifest['profile']!='shared' or length!=0 or offset>=1<<24 or crc!=zlib.crc32(offset.to_bytes(3,'little')):
                    raise ValueError('Invalid inline constant')
                continue
            if codec!=JXL or not 0<length<=MAX_PAYLOAD or offset+length>size:
                raise ValueError('Invalid sharing payload span')
            identity=(length,crc,w,h)
            if offset in spans and spans[offset]!=identity:
                raise ValueError('Alias dimension/length/CRC mismatch')
            spans[offset]=identity
            if self.manifest['profile']=='direct':
                if offset!=direct_end: raise ValueError('Direct profile contains aliased spans')
                direct_end+=length
        end=0
        for offset,(length,_,_,_) in sorted(spans.items()):
            if offset!=end: raise ValueError('Payload overlap or gap')
            end+=length
        if end!=size: raise ValueError('Trailing or missing bank data')
        self._entries=entries
        self._level_start=[next(d[0] for d in self._descriptors if d[1]==info['level']) for info in self.manifest['image']['levels']]
        if size: self._bank=MeteredFile(self.root/'images.bank')

    def _tile(self,gid,cache=None,cache_state=None):
        import imagecodecs
        if self._entries is None: self._setup()
        offset,length,codec,crc=self._entries[gid]; _,_,_,_,w,h=self._descriptors[gid]
        if codec==CONSTANT:
            rgb=np.frombuffer(offset.to_bytes(3,'little'),np.uint8); pixels=np.empty((h,w,3),np.uint8); pixels[:]=rgb
            self.constant_tiles_filled+=1
        else:
            key=(offset,length,crc)
            if cache is not None and key in cache:
                pixels=cache[key]; self.cache_hits+=1
            else:
                self._bank.seek(offset); blob=self._bank.read(length)
                if len(blob)!=length or zlib.crc32(blob)!=crc: raise ValueError('JXL record integrity')
                pixels=imagecodecs.jpegxl_decode(blob,numthreads=1,out=np.empty((h,w,3),np.uint8))
                if pixels.dtype!=np.uint8 or pixels.shape!=(h,w,3): raise ValueError('Native decoded shape/type mismatch')
                self.jxl_tiles_decoded+=1; self.decoded_pixels+=h*w
                if cache_state is not None:
                    cache_state['decoded'].add(key)
                    if cache_state['bytes']+pixels.nbytes<=MAX_QUERY_CACHE_BYTES:
                        cache[key]=pixels; cache_state['bytes']+=pixels.nbytes
        self.logical_tiles_read+=1
        return pixels

    def image(self,level,bbox):
        levels=self.manifest['image']['levels']
        if not _integer(level) or not 0<=level<len(levels): raise ValueError('Invalid native level')
        if len(bbox)!=4 or any(not _integer(v) for v in bbox): raise ValueError('Integer native ROI required')
        x0,y0,x1,y1=map(int,bbox); h,w,_=levels[level]['shape']
        if not 0<=x0<x1<=w or not 0<=y0<y1<=h or (x1-x0)*(y1-y0)>MAX_ROI_PIXELS:
            raise ValueError('ROI outside bounded native scope')
        if self._entries is None: self._setup()
        output=np.empty((y1-y0,x1-x0,3),np.uint8); columns=(w+255)//256
        cache={}; cache_state={'bytes':0,'decoded':set()}
        for ty in range(y0//256,(y1-1)//256+1):
            for tx in range(x0//256,(x1-1)//256+1):
                pixels=self._tile(self._level_start[level]+ty*columns+tx,cache,cache_state)
                xa,ya,xb,yb=max(x0,tx*256),max(y0,ty*256),min(x1,(tx+1)*256),min(y1,(ty+1)*256)
                output[ya-y0:yb-y0,xa-x0:xb-x0]=pixels[ya-ty*256:yb-ty*256,xa-tx*256:xb-tx*256]
        self.requested_pixels+=(x1-x0)*(y1-y0)
        self.last_query_cache_bytes=cache_state['bytes']; self.last_query_unique_payloads=len(cache_state['decoded'])
        cache.clear()
        return output

    def preview(self,bbox=None,*,max_edge=1024):
        if not _integer(max_edge) or not 1<=max_edge<=4096: raise ValueError('Preview bound1..4096 required')
        h0,w0,_=self.manifest['image']['levels'][0]['shape']; bbox=[0,0,w0,h0] if bbox is None else bbox
        if len(bbox)!=4 or any(not _integer(v) for v in bbox): raise ValueError('Integer level0 bounds required')
        x0,y0,x1,y1=map(int,bbox)
        if not 0<=x0<x1<=w0 or not 0<=y0<y1<=h0: raise ValueError('Preview bounds outside level0')
        for info in self.manifest['image']['levels']:
            h,w,_=info['shape']; box=[x0*w//w0,y0*h//h0,(x1*w+w0-1)//w0,(y1*h+h0-1)//h0]
            if max(box[2]-box[0],box[3]-box[1])<=max_edge:
                return {'image':self.image(info['level'],box),'level':info['level'],'image_bbox':box,
                        'level0_bbox':list(bbox),'stored_level_exact':True,'level0_exact':info['level']==0}
        raise ValueError('No stored native preview fits bound')

    def _verify_files(self):
        for name,desc in self.manifest['files'].items():
            if sha256(self.root/name)!=desc['sha256']: raise ValueError('Package member SHA mismatch')

    def source_metadata(self):
        if self._source_metadata is None:
            path=self.root/'source.json'
            if sha256(path)!=self.manifest['files']['source.json']['sha256']:
                raise ValueError('Source metadata SHA mismatch')
            data=json.loads(path.read_bytes()); levels=self.manifest['image']['levels']
            original=data.get('native_levels'); metadata=data.get('native_metadata'); identity=data.get('identity',{})
            whole=data.get('whole_file_sha256')
            if (data.get('schema')!=scan.SOURCE_SCHEMA or not isinstance(whole,str) or HEX.fullmatch(whole) is None
                    or whole!=identity.get('whole_file_sha256') or not isinstance(original,list)
                    or not isinstance(metadata,list) or len(original)!=len(levels) or len(metadata)!=len(levels)):
                raise ValueError('Source metadata coverage/identity mismatch')
            for i,(current,native,tags) in enumerate(zip(levels,original,metadata)):
                if (not _integer(native.get('level')) or native['level']!=i or native.get('shape')!=current['shape']
                        or native.get('dtype')!='uint8' or not _integer(tags.get('level')) or tags['level']!=i
                        or not _integer(tags.get('page_index')) or tags['page_index']!=i
                        or not isinstance(tags.get('tags'),dict) or not isinstance(tags.get('restore'),dict)):
                    raise ValueError('Source metadata native geometry mismatch')
            self._source_metadata=data
        return self._source_metadata

    def _stripes(self,info):
        h,w,_=info['shape']
        for y in range(0,h,TILE):
            stripe=np.empty((min(TILE,h-y),w,3),np.uint8)
            for x in range(0,w,TILE):
                stripe[:,x:min(x+TILE,w)]=self.image(info['level'],[x,y,min(x+TILE,w),min(y+TILE,h)])
            yield stripe

    def verify(self):
        self._verify_files(); self.source_metadata(); checks=[]
        for info in self.manifest['image']['levels']:
            digest=hashlib.sha256()
            for stripe in self._stripes(info): digest.update(memoryview(stripe))
            if digest.hexdigest()!=info['pixel_sha256']: raise ValueError('Native raster SHA mismatch')
            checks.append({'level':info['level'],'pixels_exact':True,'pixel_sha256':digest.hexdigest()})
        return {'ok':True,'levels':checks,'ledger':self.ledger(),'source_metadata_retained':True}

    def restore(self,destination):
        import tifffile
        dest=_disjoint(destination,self.root); self._verify_files(); dest.mkdir(parents=True)
        marker=dest/'INCOMPLETE'; marker.write_text('Native exact image restore\n',encoding='utf8')
        try:
            source=self.source_metadata(); metadata=source['native_metadata']
            if len(metadata)!=len(self.manifest['image']['levels']): raise ValueError('Source metadata level count')
            with tifffile.TiffWriter(dest/'native_levels.tif',bigtiff=True) as writer:
                for info,original in zip(self.manifest['image']['levels'],metadata):
                    h,w,_=info['shape']; restore=original['restore']; digest=hashlib.sha256()
                    def tiles():
                        for stripe in self._stripes(info):
                            digest.update(memoryview(stripe))
                            for x in range(0,w,TILE):
                                pixels=stripe[:,x:min(x+TILE,w)]
                                if pixels.shape!=(TILE,TILE,3):
                                    padded=np.zeros((TILE,TILE,3),np.uint8); padded[:pixels.shape[0],:pixels.shape[1]]=pixels; yield padded
                                else: yield np.ascontiguousarray(pixels)
                    icc=restore.get('icc_profile_base64')
                    kwargs={'shape':(h,w,3),'dtype':np.uint8,'photometric':'rgb','planarconfig':'contig',
                        'tile':(TILE,TILE),'compression':'deflate','metadata':None,'maxworkers':1,
                        'description':restore.get('description'),'software':restore.get('software') or False,
                        'datetime':restore.get('datetime'),'subfiletype':restore.get('subfiletype',0),
                        'iccprofile':None if icc is None else base64.b64decode(icc,validate=True)}
                    if restore.get('resolution') is not None:
                        kwargs['resolution']=tuple(tuple(pair) for pair in restore['resolution']); kwargs['resolutionunit']=restore.get('resolutionunit',1)
                    writer.write(tiles(),**kwargs)
                    if digest.hexdigest()!=info['pixel_sha256']: raise ValueError('Restored pixel stream mismatch')
            with NativeWSISource(dest/'native_levels.tif') as exported:
                if len(exported.levels)!=len(self.manifest['image']['levels']): raise ValueError('Restored TIFF level count')
                for info in self.manifest['image']['levels']:
                    h,w,_=info['shape']; digest=hashlib.sha256()
                    if exported.levels[info['level']]['shape']!=info['shape']: raise ValueError('Restored TIFF shape')
                    for y in range(0,h,TILE):
                        stripe=np.empty((min(TILE,h-y),w,3),np.uint8)
                        for x in range(0,w,TILE):
                            pixels=exported.read_tile(info['level'],x//TILE,y//TILE); stripe[:,x:x+pixels.shape[1]]=pixels
                        digest.update(memoryview(stripe))
                    if digest.hexdigest()!=info['pixel_sha256']: raise ValueError('Restored TIFF native SHA mismatch')
            shutil.copyfile(self.root/'source.json',dest/'source.json'); shutil.copyfile(self.root/'manifest.json',dest/'package_manifest.json')
            marker.unlink(); return str(dest)
        except BaseException as exc:
            write_json(dest/'failure.json',{'type':type(exc).__name__,'error':str(exc)}); raise

    def metrics(self):
        return {'index_read_bytes':self.index_read_bytes,'jxl_tiles_decoded':self.jxl_tiles_decoded,
                'constant_tiles_filled':self.constant_tiles_filled,'logical_tiles_read':self.logical_tiles_read,
                'decoded_pixels':self.decoded_pixels,'requested_pixels':self.requested_pixels,
                'decoded_pixel_cache_bytes':0,'per_query_cache_limit_bytes':MAX_QUERY_CACHE_BYTES,
                'last_query_cache_peak_bytes':self.last_query_cache_bytes,'cache_hits':self.cache_hits,
                'last_query_unique_payloads_decoded':self.last_query_unique_payloads,
                'cache_scope':'same physical-span policy for bothprofiles, query-local only, cleared before return',
                'image_bank':None if self._bank is None else self._bank.metrics()}


SlideImageArchive = Reader
