"""Official B2ND C slicing with registered, metered, read-only local IO.

No alternative codec or cache. Native read calls flow through MeteredFile.
The callback objects are retained for the lifetime of the registered IO plugin.
"""
import ctypes as C
from pathlib import Path
import numpy as np

OPEN=C.CFUNCTYPE(C.c_void_p,C.c_char_p,C.c_char_p,C.c_void_p)
CLOSE=C.CFUNCTYPE(C.c_int,C.c_void_p)
SIZE=C.CFUNCTYPE(C.c_int64,C.c_void_p)
WRITE=C.CFUNCTYPE(C.c_int64,C.c_void_p,C.c_int64,C.c_int64,C.c_int64,C.c_void_p)
READ=C.CFUNCTYPE(C.c_int64,C.POINTER(C.c_void_p),C.c_int64,C.c_int64,C.c_int64,C.c_void_p)
TRUNCATE=C.CFUNCTYPE(C.c_int,C.c_void_p,C.c_int64)
DESTROY=C.CFUNCTYPE(C.c_int,C.c_void_p)
class IOCallbacks(C.Structure):
    _fields_=[('id',C.c_uint8),('name',C.c_char_p),('allocation',C.c_bool),
              ('open',OPEN),('close',CLOSE),('size',SIZE),('write',WRITE),('read',READ),
              ('truncate',TRUNCATE),('destroy',DESTROY)]
class IO(C.Structure):
    _fields_=[('id',C.c_uint8),('name',C.c_char_p),('params',C.c_void_p)]
_owners={}; _lib=None; _callbacks=None

def runtime():
    global _lib,_callbacks
    if _lib is not None: return _lib
    import blosc2
    lib=C.CDLL(str(Path(blosc2.__file__).parent/'lib/libblosc2.dll'))
    @OPEN
    def op(path,mode,params):
        owner=_owners.get(params)
        if owner is None or any(x in mode for x in (b'w',b'a',b'+')): return None
        if Path(path.decode()).resolve()!=owner.path: return None
        return params
    @CLOSE
    def close(_): return 0 # archive owns the metered file
    @SIZE
    def size(token): return _owners[token].file.seek(0,2)
    @WRITE
    def write(*_): return -1
    @READ
    def read(ptr,size,count,position,token):
        owner=_owners[token]
        try:
            if size<=0 or count<0 or position<0: return -1
            owner.file.seek(position); raw=owner.file.read(size*count)
            C.memmove(ptr[0],raw,len(raw)); return len(raw)//size
        except Exception as exc:
            owner.errors.append(exc); return -1
    @TRUNCATE
    def truncate(*_): return -1
    @DESTROY
    def destroy(_): return 0
    _callbacks=IOCallbacks(250,b'stbaseline-metered',True,op,close,size,write,read,truncate,destroy)
    lib.blosc2_init()
    lib.blosc2_register_io_cb.argtypes=[C.POINTER(IOCallbacks)]; lib.blosc2_register_io_cb.restype=C.c_int
    if lib.blosc2_register_io_cb(C.byref(_callbacks))<0: raise RuntimeError('Register Blosc2 IO')
    lib.blosc2_set_nthreads.argtypes=[C.c_int16]; lib.blosc2_set_nthreads(1)
    lib.blosc2_schunk_open_offset_udio.argtypes=[C.c_char_p,C.c_int64,C.POINTER(IO)]
    lib.blosc2_schunk_open_offset_udio.restype=C.c_void_p
    lib.b2nd_from_schunk.argtypes=[C.c_void_p,C.POINTER(C.c_void_p)]; lib.b2nd_from_schunk.restype=C.c_int
    lib.b2nd_free.argtypes=[C.c_void_p]; lib.b2nd_free.restype=C.c_int
    lib.blosc2_schunk_free.argtypes=[C.c_void_p]; lib.blosc2_schunk_free.restype=C.c_int
    lib.b2nd_get_slice_cbuffer.argtypes=[C.c_void_p,C.POINTER(C.c_int64),C.POINTER(C.c_int64),C.c_void_p,C.POINTER(C.c_int64),C.c_int64]
    lib.b2nd_get_slice_cbuffer.restype=C.c_int
    _lib=lib
    return lib

class NativeB2ND:
    def __init__(self,path,file):
        self.lib=runtime(); self.path=Path(path).resolve(); self.file=file; self.errors=[]
        self.token=id(self); self.array=C.c_void_p(); _owners[self.token]=self
        self.io=IO(250,b'stbaseline-metered',self.token)
        schunk=self.lib.blosc2_schunk_open_offset_udio(str(self.path).encode(),0,C.byref(self.io))
        if not schunk:
            del _owners[self.token]; raise ValueError('Native B2ND open failed')
        if self.lib.b2nd_from_schunk(schunk,C.byref(self.array))<0:
            self.lib.blosc2_schunk_free(schunk); del _owners[self.token]
            raise ValueError('Native B2ND array failed')
    def __getitem__(self,item):
        starts=[s.start or 0 for s in item]
        stops=[s.stop for s in item]
        if stops[2] is None: stops[2]=3
        shape=[b-a for a,b in zip(starts,stops)]
        result=np.empty(shape,dtype=np.uint8)
        vec=C.c_int64*3
        code=self.lib.b2nd_get_slice_cbuffer(self.array,vec(*starts),vec(*stops),result.ctypes.data,vec(*shape),result.nbytes)
        if self.errors: raise self.errors[0]
        if code<0: raise ValueError('Native B2ND slice failed: '+str(code))
        return result
    def close(self):
        if self.array:
            self.lib.b2nd_free(self.array); self.array=C.c_void_p()
            _owners.pop(self.token,None)
