"""Independent-access image codecs with unchanged exact Shared Count.
Optional decode audit callbacks are excluded from latency measurements.
"""
import copy
import hashlib
import math
from pathlib import Path
import shutil
import numpy as np
from .integrity import sha256,write_json
from .reader import MeteredFile
from .shared_backend import SharedArchive

FORMAT='stbaseline-access-1'
METHODS=('Zarr3_sharded','B2ND','JP2_progressive')

def configure():
    import zarr,blosc2
    zarr.config.set({'async.concurrency':1,'threading.max_workers':1})
    blosc2.set_nthreads(1)

def write_level(pixels,path,method):
    configure()
    if method=='Zarr3_sharded':
        import zarr
        from zarr.codecs import ZstdCodec
        a=zarr.create_array(str(path),shape=pixels.shape,dtype='uint8',chunks=(256,256,3),
            shards=(1024,1024,3),compressors=[ZstdCodec(level=9,checksum=True)],zarr_format=3,overwrite=False)
        a[:]=pixels
    elif method=='B2ND':
        import blosc2
        blosc2.asarray(pixels,chunks=(1024,1024,3),blocks=(256,256,3),urlpath=str(path),mode='w',contiguous=True,
            cparams={'codec':blosc2.Codec.ZSTD,'clevel':9,'nthreads':1,
                     'filters':[blosc2.Filter.NOFILTER]*5+[blosc2.Filter.BITSHUFFLE]},dparams={'nthreads':1})
    elif method=='JP2_progressive':
        from .openjpeg_access import runtime
        runtime()
        import glymur
        glymur.set_option('lib.num_threads',1)
        h,w=pixels.shape[:2]
        glymur.Jp2k(str(path),data=pixels,tilesize=(min(256,h),min(256,w)),cratios=(16,8,1),
            irreversible=False,numres=min(6,int(math.log2(min(h,w)))+1),prog='LRCP',mct=True,plt=True,tlm=True)
    else: raise ValueError(method)

def pack_access(count_source,output,*,method):
    from .comparison import open_archive
    if method not in METHODS: raise ValueError(method)
    out=Path(output); out.mkdir(parents=True,exist_ok=False)
    (out/'INCOMPLETE').write_text('building access backend')
    with open_archive(count_source) as src:
        if src.manifest['method']!='Shared_local': raise ValueError('Expected qualified Shared_local')
        m=copy.deepcopy(src.manifest)
        for name,d in m['files'].items():
            if sha256(src.root/name)!=d['sha256']: raise ValueError('Source integrity: '+name)
        for name in ('counts.bank','counts.index.json','science.h5',*m.get('attachments',[])):
            (out/name).parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(src.root/name,out/name)
        ext={'Zarr3_sharded':'zarr','B2ND':'b2nd','JP2_progressive':'jp2'}[method]
        for info in m['image']['levels']:
            h,w,c=info['shape']
            if info['dtype']!='uint8' or c!=3: raise ValueError('RGB8 required')
            pixels=src.image(info['level'],[0,0,w,h])
            assert hashlib.sha256(pixels.tobytes()).hexdigest()==info['pixel_sha256']
            name=f"images.{info['level']}.{ext}"
            write_level(pixels,out/name,method); info['access_path']=name
            print(method,'level',info['level'],'complete',flush=True)
        m.update(format=FORMAT,method=method)
        m['image'].update(path='per-native-level',codec=method,
            design={'tile':256,'outer_chunk':1024,'zstd_level':9,'b2nd_filter':'bitshuffle',
                    'jp2_cratios':[16,8,1],'jp2_reversible':True,'jp2_progression':'LRCP',
                    'jp2_plt':True,'jp2_tlm':True,'jp2_stream_buffer':65536,
                    'native_levels_preserved':True,'generic_runtime_excluded':True,'learned_model_bytes':0})
        m['files']={p.relative_to(out).as_posix():{'bytes':p.stat().st_size,'sha256':sha256(p)}
                    for p in sorted(out.rglob('*')) if p.is_file() and p.name!='INCOMPLETE'}
        write_json(out/'manifest.json',m)
    (out/'INCOMPLETE').unlink()
    with AccessArchive(out) as a: return a.ledger()

class AccessArchive(SharedArchive):
    FORMAT=FORMAT
    METHODS=METHODS
    def __init__(self,*args,audit_decode=False,b2nd_mode='native',**kwargs):
        super().__init__(*args,**kwargs)
        if b2nd_mode not in ('proxy','native'): raise ValueError('Unknown B2ND reader')
        self.b2nd_mode=b2nd_mode
        self.audit_decode=audit_decode; self.decode_events=[]; self.logical_decode_bytes=0
        self._access_arrays={}; self._postfilters=[]; self._configured=False
    def _stream(self,name):
        if name not in self.files:
            if name not in self.manifest['files']: raise ValueError('Unlisted image member: '+name)
            self.files[name]=MeteredFile(self.root/name)
        return self.files[name]
    def _zarr_array(self,info):
        import zarr
        from zarr.storage import LocalStore
        from zarr.abc.store import RangeByteRequest,OffsetByteRequest,SuffixByteRequest
        from zarr.core.buffer import default_buffer_prototype
        owner=self; prefix=info['access_path']
        class RangeStore(LocalStore):
            def get_sync(store,key,prototype=None,byte_range=None):
                prototype=prototype or default_buffer_prototype(); name=prefix+'/'+key
                if name not in owner.manifest['files']: return None
                f=owner._stream(name); size=owner.manifest['files'][name]['bytes']
                if byte_range is None: start,end=0,size
                elif isinstance(byte_range,RangeByteRequest): start,end=byte_range.start,byte_range.end
                elif isinstance(byte_range,OffsetByteRequest): start,end=byte_range.offset,size
                elif isinstance(byte_range,SuffixByteRequest): start,end=max(0,size-byte_range.suffix),size
                else: raise TypeError(byte_range)
                f.seek(start); return prototype.buffer.from_bytes(f.read(end-start))
            async def get(store,key,prototype=None,byte_range=None): return store.get_sync(key,prototype,byte_range)
            async def get_partial_values(store,prototype,key_ranges):
                return [store.get_sync(k,prototype,r) for k,r in key_ranges]
            async def _get_many(store,requests):
                for k,p,r in requests: yield k,store.get_sync(k,p,r)
        return zarr.open_array(store=RangeStore(self.root/prefix,read_only=True),mode='r')
    def _b2nd_array(self,info):
        if self.b2nd_mode=='native':
            from .b2nd_native import NativeB2ND
            return NativeB2ND(self.root/info['access_path'],self._stream(info['access_path']))
        import blosc2
        from blosc2.proxy_source import ByteRangeNDSource
        owner=self; f=self._stream(info['access_path'])
        class Source(ByteRangeNDSource):
            def read_range(source,offset,size): f.seek(offset); return f.read(size)
        proxy=blosc2.Proxy(Source(str(self.root/info['access_path']),max_concurrency=1),dparams={'nthreads':1})
        if self.audit_decode:
            def record_block(input,output,offset):
                owner.decode_events.append({'codec':'B2ND','bytes':input.nbytes,'offset':int(offset)})
                output[:]=input
            record_block.__name__='audit_'+str(id(proxy))
            proxy._cache.schunk.postfilter(np.dtype('uint8'))(record_block)
            self._postfilters.append((proxy._cache.schunk,record_block.__name__))
        return proxy
    def _validate_box(self,level,bbox):
        if isinstance(level,bool) or not isinstance(level,(int,np.integer)) or not 0<=level<len(self.manifest['image']['levels']):
            raise ValueError('Invalid native level')
        if len(bbox)!=4 or any(isinstance(v,bool) or not isinstance(v,(int,np.integer)) for v in bbox):
            raise ValueError('Integer half-open bbox required')
        info=self.manifest['image']['levels'][int(level)]; h,w,_=info['shape']; x0,y0,x1,y1=map(int,bbox)
        if not (0<=x0<x1<=w and 0<=y0<y1<=h): raise ValueError('ROI outside native level')
        return info,(x0,y0,x1,y1)
    def image(self,level,bbox): return self._image(level,bbox,0)
    def image_quality(self,level,bbox,*,layers):
        if self.manifest['method']!='JP2_progressive' or isinstance(layers,bool) or layers not in (1,2,3):
            raise ValueError('JPEG2000 quality layers must be 1,2,3')
        return {'image':self._image(level,bbox,layers),'quality_layers':np.asarray(layers),
                'exact':np.asarray(layers==3),'error_bound_available':np.asarray(False)}
    def _image(self,level,bbox,layers):
        info,(x0,y0,x1,y1)=self._validate_box(level,bbox)
        if not self._configured: configure(); self._configured=True
        method=self.manifest['method']; h,w,_=info['shape']
        xa,ya=x0//256*256,y0//256*256; xb,yb=min((x1+255)//256*256,w),min((y1+255)//256*256,h)
        footprint=(xb-xa)*(yb-ya)*3 if method=='JP2_progressive' else ((x1-1)//256-x0//256+1)*((y1-1)//256-y0//256+1)*256*256*3
        self.logical_decode_bytes+=footprint
        if method=='JP2_progressive':
            from .openjpeg_access import decode
            result=decode(self._stream(info['access_path']),[xa,ya,xb,yb],layers=layers)
            if result.shape!=(yb-ya,xb-xa,3): raise ValueError('OpenJPEG decode area mismatch')
            if self.audit_decode: self.decode_events.append({'codec':'OpenJPEG','bytes':result.nbytes,'scope':'materialized touched tiles; int32 scratch excluded'})
            return result[y0-ya:y1-ya,x0-xa:x1-xa].copy()
        if level not in self._access_arrays:
            self._access_arrays[level]=self._zarr_array(info) if method=='Zarr3_sharded' else self._b2nd_array(info)
        a=self._access_arrays[level]
        if method=='B2ND' and self.b2nd_mode=='native' and self.audit_decode:
            self.decode_events.append({'codec':'B2ND native','bytes':footprint,'scope':'structural block footprint; not postfilter callback'})
        if method=='Zarr3_sharded' and self.audit_decode:
            from zarr.codecs import ZstdCodec
            original=ZstdCodec._decode_sync; events=self.decode_events
            def measured(codec,chunk_bytes,spec):
                value=original(codec,chunk_bytes,spec)
                events.append({'codec':'Zstd','bytes':len(value),'input_bytes':len(chunk_bytes)})
                return value
            ZstdCodec._decode_sync=measured
            try: return np.asarray(a[y0:y1,x0:x1,:])
            finally: ZstdCodec._decode_sync=original
        return np.asarray(a[y0:y1,x0:x1,:])
    def metrics(self):
        m=super().metrics()
        m.update(logical_image_decode_bytes=self.logical_decode_bytes,
                 audited_image_decode_bytes=sum(e['bytes'] for e in self.decode_events),
                 decode_events=self.decode_events.copy(),audit_decode=self.audit_decode)
        return m
    def close(self):
        for schunk,name in self._postfilters: schunk.remove_postfilter(name)
        for a in self._access_arrays.values():
            if hasattr(a,'close'): a.close()
        self._postfilters.clear(); self._access_arrays.clear(); super().close()
