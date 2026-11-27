"""Exact tile-local image prediction, paid shared Zstd dictionary, compact index.

This is an image-side analogue of shared statistical assets, not Count-driven
image prediction. Mature JXL and dictionary-free controls remain first class.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import struct
import zlib

import numpy as np
from numba import njit
import zstandard as zstd

from .comparison import open_archive
from .integrity import sha256, write_json
from .reader import MeteredFile
from .shared_backend import SharedArchive

FORMAT = "stbaseline-image-shared-1"
METHODS = ("Zstd_TIFF", "JXL", "Residual", "Shared_dict", "Hybrid_independent", "Hybrid_shared")
HEADER = struct.Struct("<8sII")
ENTRY = struct.Struct("<QIB3xI")
MAGIC = b"STIMGT01"


@njit(cache=True)
def predict(p, y, x, c):
    a = np.int32(p[y, x-1, c]) if x else np.int32(0)
    b = np.int32(p[y-1, x, c]) if y else np.int32(0)
    u = np.int32(p[y-1, x-1, c]) if y and x else np.int32(0)
    if u >= max(a, b):
        return min(a, b)
    if u <= min(a, b):
        return max(a, b)
    return a+b-u


@njit(cache=True)
def residuals(p):
    h, w, _ = p.shape
    r = np.empty((3, h, w), np.uint8)
    for y in range(h):
        for x in range(w):
            dg = np.int32(p[y,x,1])-predict(p,y,x,1)
            r[0,y,x] = dg & 255
            r[1,y,x] = (np.int32(p[y,x,0])-predict(p,y,x,0)-dg) & 255
            r[2,y,x] = (np.int32(p[y,x,2])-predict(p,y,x,2)-dg) & 255
    return r


@njit(cache=True)
def reconstruct(r):
    _, h, w = r.shape
    p = np.empty((h,w,3), np.uint8)
    for y in range(h):
        for x in range(w):
            pg = predict(p,y,x,1)
            p[y,x,1] = (pg+np.int32(r[0,y,x])) & 255
            dg = np.int32(p[y,x,1])-pg
            p[y,x,0] = (predict(p,y,x,0)+dg+np.int32(r[1,y,x])) & 255
            p[y,x,2] = (predict(p,y,x,2)+dg+np.int32(r[2,y,x])) & 255
    return p


def tiles(levels, tile):
    for info in levels:
        h,w,_ = info["shape"]
        for y in range(0,h,tile):
            for x in range(0,w,tile):
                yield info["level"], [x,y,min(x+tile,w),min(y+tile,h)]


def write_index(path, tile, records):
    with open(path, "xb") as f:
        f.write(HEADER.pack(MAGIC,tile,len(records)))
        for r in records:
            f.write(ENTRY.pack(*r))


def read_index(blob):
    if len(blob)<HEADER.size:
        raise ValueError("Short image index")
    magic,tile,n = HEADER.unpack_from(blob)
    if magic!=MAGIC or not 1<=tile<=4096 or len(blob)!=HEADER.size+n*ENTRY.size:
        raise ValueError("Invalid image index")
    records = [ENTRY.unpack_from(blob,HEADER.size+i*ENTRY.size) for i in range(n)]
    if any(r[2] not in (0,1,2) for r in records):
        raise ValueError("Unknown tile codec")
    return tile,records


def train_image_dictionary(reference, tile, size, samples_per_level):
    samples, selected = [], []
    for info in reference.manifest["image"]["levels"]:
        grid = list(tiles([info],tile))
        for j in np.unique(np.linspace(0,len(grid)-1,min(samples_per_level,len(grid)),dtype=int)):
            level, box = grid[j]
            samples.append(residuals(reference.image(level,box)).tobytes())
            selected.append({"level":level,"bbox":box})
    dictionary = zstd.train_dictionary(size,samples,k=64,d=8,dict_id=466004,level=9,threads=0,steps=1,split_point=1.0)
    return dictionary, {"tiles":selected,"sample_bytes":sum(map(len,samples)),"requested_bytes":size,
                        "actual_bytes":len(dictionary.as_bytes()),"k":64,"d":8,"dict_id":466004,"split_point":1.0}


def build_candidates(count_source, jxl_source, output, *, tile=256, zstd_level=9, dictionary_bytes=65536, samples_per_level=24):
    """Encode each residual candidate once; retain original JXL codestream bytes."""
    out = Path(output)
    out.mkdir(parents=True,exist_ok=False)
    with open_archive(count_source) as ref:
        dictionary, training = train_image_dictionary(ref,tile,dictionary_bytes,samples_per_level)
        (out/"images.dictionary").write_bytes(dictionary.as_bytes())
        write_json(out/"training.json",training)
        compressors = {"Residual":zstd.ZstdCompressor(level=zstd_level,write_checksum=True,threads=0),
                       "Shared_dict":zstd.ZstdCompressor(level=zstd_level,dict_data=dictionary,write_checksum=True,threads=0)}
        files = {name:open(out/(name+".bank"),"xb") for name in compressors}
        records = {name:[] for name in compressors}
        try:
            for j,(level,box) in enumerate(tiles(ref.manifest["image"]["levels"],tile)):
                raw = residuals(ref.image(level,box)).tobytes()
                for name,ctx in compressors.items():
                    b = ctx.compress(raw)
                    f = files[name]
                    records[name].append((f.tell(),len(b),0 if name=="Residual" else 2,zlib.crc32(b)))
                    f.write(b)
                if j%256==0:
                    print("image candidates",j,"tiles",flush=True)
        finally:
            for f in files.values(): f.close()
        for name in records:
            write_index(out/(name+".index"),tile,records[name])
    old = Path(jxl_source)
    m = json.loads((old/"manifest.json").read_bytes())
    for name in ("images.jxlbank","tiles.json"):
        if sha256(old/name)!=m["files"][name]["sha256"]:
            raise ValueError("JXL source integrity")
    index = json.loads((old/"tiles.json").read_bytes())
    if index["tile"] != tile:
        raise ValueError("JXL tile mismatch")
    shutil.copyfile(old/"images.jxlbank",out/"JXL.bank")
    entries = []
    with open(out/"JXL.bank","rb") as f:
        for level in index["levels"]:
            for offset,length in level:
                f.seek(offset); b=f.read(length)
                entries.append((offset,length,1,zlib.crc32(b)))
    if len(entries)!=len(records["Residual"]):
        raise ValueError("JXL tile count mismatch")
    write_index(out/"JXL.index",tile,entries)
    return {"tiles":len(entries),"dictionary":training,
            "assets":{p.name:{"bytes":p.stat().st_size,"sha256":sha256(p)} for p in out.iterdir() if p.is_file()}}


def assemble_image(candidates, output, method):
    src,out = Path(candidates),Path(output)
    kinds = ("JXL","Residual","Shared_dict")
    indexes = {k:read_index((src/(k+".index")).read_bytes()) for k in kinds}
    tile = indexes["JXL"][0]
    n = len(indexes["JXL"][1])
    if any(t!=tile or len(r)!=n for t,r in indexes.values()):
        raise ValueError("Candidate index mismatch")
    length = np.array([[r[1] for r in indexes[k][1]] for k in kinds],dtype=np.int64)
    independent = np.argmin(length[:2],axis=0)
    shared = np.argmin(length,axis=0)
    raw_gain = int(length[independent,np.arange(n)].sum()-length[shared,np.arange(n)].sum())
    dict_bytes = (src/"images.dictionary").stat().st_size
    # Conservative allowance for the additional dictionary manifest descriptor.
    use_shared = raw_gain>dict_bytes+512
    if method in kinds:
        selected = np.full(n,kinds.index(method),np.int64)
    elif method=="Hybrid_independent":
        selected = independent
    elif method=="Hybrid_shared":
        selected = shared if use_shared else independent
    else:
        raise ValueError(method)
    needed_dict = bool(np.any(selected==2))
    if needed_dict:
        shutil.copyfile(src/"images.dictionary",out/"images.dictionary")
    handles = {k:open(src/(k+".bank"),"rb") for k in kinds}
    records = []
    try:
        with open(out/"images.bank","xb") as f:
            for i,code in enumerate(selected):
                k = kinds[int(code)]
                offset,length_,codec,crc = indexes[k][1][i]
                handles[k].seek(offset); b=handles[k].read(length_)
                if len(b)!=length_ or zlib.crc32(b)!=crc:
                    raise ValueError("Candidate tile integrity")
                records.append((f.tell(),len(b),codec,crc)); f.write(b)
    finally:
        for f in handles.values(): f.close()
    write_index(out/"images.index",tile,records)
    return {"tile":tile,"tiles":n,"selected_tiles":{k:int(np.sum(selected==i)) for i,k in enumerate(kinds)},
            "dictionary_used":needed_dict,"dictionary_bytes":dict_bytes if needed_dict else 0,
            "hybrid_candidate_stream_saving":raw_gain,"hybrid_dictionary_admission_cost":dict_bytes+512,
            "selection":"actual tile bytes, codec tag and index charged; dictionary admitted only beyond full cost plus 512B allowance"}


def pack_image(count_source,candidates,output,*,method):
    if method not in METHODS:
        raise ValueError(method)
    out=Path(output); out.mkdir(parents=True,exist_ok=False)
    (out/"INCOMPLETE").write_text("building")
    with open_archive(count_source) as base:
        m=copy.deepcopy(base.manifest)
        if m["method"]!="Shared_local":
            raise ValueError("Expected qualified Shared_local Count source")
        for name in ("counts.bank","counts.index.json","science.h5",*m.get("attachments",[])):
            if sha256(base.root/name)!=m["files"][name]["sha256"]:
                raise ValueError("Count/source component changed")
            shutil.copyfile(base.root/name,out/name)
        m.update(format=FORMAT,method=method)
        if method=="Zstd_TIFF":
            if sha256(base.root/"images.tif")!=m["files"]["images.tif"]["sha256"]:
                raise ValueError("TIFF source integrity")
            shutil.copyfile(base.root/"images.tif",out/"images.tif")
        else:
            design=assemble_image(candidates,out,method)
            m["image"].update(path="images.bank",codec="indexed-independent-image-tiles-v1",index="images.index",design=design)
            m["image"]["predictor"]="tile-local RGB MED plus green-error coupling, modulo256 planar residuals"
        m["files"]={p.relative_to(out).as_posix():{"bytes":p.stat().st_size,"sha256":sha256(p)}
                    for p in sorted(out.rglob("*")) if p.is_file() and p.name!="INCOMPLETE"}
        write_json(out/"manifest.json",m)
    (out/"INCOMPLETE").unlink()
    with ImageSharedArchive(out) as a:
        return a.ledger()


class ImageSharedArchive(SharedArchive):
    FORMAT=FORMAT
    METHODS=METHODS

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self._entries=None
        self._decompressors={}
        self._level_start=[]
        self.image_tiles_decoded=0

    def _image_setup(self):
        b=(self.root/"images.index").read_bytes()
        if hashlib.sha256(b).hexdigest()!=self.manifest["files"]["images.index"]["sha256"]:
            raise ValueError("Image index integrity")
        self.index_read_bytes+=len(b)
        self._image_tile,self._entries=read_index(b)
        n=0
        for info in self.manifest["image"]["levels"]:
            self._level_start.append(n)
            h,w,_=info["shape"]; n+=((w+self._image_tile-1)//self._image_tile)*((h+self._image_tile-1)//self._image_tile)
        if n!=len(self._entries):
            raise ValueError("Image tile coverage")
        size=(self.root/"images.bank").stat().st_size
        expected=0
        for offset,length,codec,crc in self._entries:
            if offset!=expected or length<1 or offset+length>size:
                raise ValueError("Invalid image tile span")
            expected+=length
        if expected!=size:
            raise ValueError("Image bank trailing data")
        self.files["images.bank"]=MeteredFile(self.root/"images.bank")

    def image(self,level,bbox):
        if self.manifest["method"]=="Zstd_TIFF":
            return super().image(level,bbox)
        info=self._level(level)
        if len(bbox)!=4 or any(isinstance(v,(bool,np.bool_)) or not isinstance(v,(int,np.integer)) for v in bbox):
            raise ValueError("Integer native image box required")
        x0,y0,x1,y1=map(int,bbox); h,w,_=info["shape"]
        if not 0<=x0<x1<=w or not 0<=y0<y1<=h:
            raise ValueError("Image ROI outside native level")
        if self._entries is None: self._image_setup()
        t=self._image_tile; nx=(w+t-1)//t
        output=np.empty((y1-y0,x1-x0,3),np.uint8)
        for ty in range(y0//t,(y1-1)//t+1):
            for tx in range(x0//t,(x1-1)//t+1):
                i=self._level_start[level]+ty*nx+tx
                offset,length,codec,crc=self._entries[i]
                f=self.files["images.bank"]; f.seek(offset); blob=f.read(length)
                if len(blob)!=length or zlib.crc32(blob)!=crc:
                    raise ValueError("Image tile integrity")
                th,tw=min(t,h-ty*t),min(t,w-tx*t)
                if codec==1:
                    import imagecodecs
                    pixels=imagecodecs.jpegxl_decode(blob,numthreads=1)
                else:
                    if codec not in self._decompressors:
                        dictionary=None
                        if codec==2:
                            b=(self.root/"images.dictionary").read_bytes()
                            self.index_read_bytes+=len(b)
                            if hashlib.sha256(b).hexdigest()!=self.manifest["files"]["images.dictionary"]["sha256"]:
                                raise ValueError("Image dictionary integrity")
                            dictionary=zstd.ZstdCompressionDict(b)
                        self._decompressors[codec]=zstd.ZstdDecompressor(dict_data=dictionary)
                    raw=self._decompressors[codec].decompress(blob,max_output_size=th*tw*3,allow_extra_data=False)
                    if len(raw)!=th*tw*3:
                        raise ValueError("Residual tile size mismatch")
                    pixels=reconstruct(np.frombuffer(raw,np.uint8).reshape(3,th,tw))
                if pixels.shape!=(th,tw,3) or pixels.dtype!=np.uint8:
                    raise ValueError("Decoded tile shape/type")
                self.image_tiles_decoded+=1
                xa,ya,xb,yb=max(x0,tx*t),max(y0,ty*t),min(x1,(tx+1)*t),min(y1,(ty+1)*t)
                output[ya-y0:yb-y0,xa-x0:xb-x0]=pixels[ya-ty*t:yb-ty*t,xa-tx*t:xb-tx*t]
        return output

    def metrics(self):
        m=super().metrics()
        m["image_tiles_decoded"]=self.image_tiles_decoded
        return m
