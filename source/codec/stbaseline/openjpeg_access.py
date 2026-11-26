"""OpenJPEG byte-range callbacks; decode complete touched tiles, then crop.
Quality-limited decode does not imply incremental I/O or decoder-state reuse.
"""
import ctypes as C
import numpy as np

def runtime():
    from glymur.lib import openjp2 as opj
    if opj.OPENJP2 is None or opj.version() < '2.5.0':
        raise RuntimeError('OpenJPEG >=2.5 required; configure task-local glymurrc')
    return opj

def decode(f,bbox,*,layers=0,buffer_size=65536):
    opj=runtime(); lib=opj.OPENJP2
    READ=C.CFUNCTYPE(C.c_size_t,C.c_void_p,C.c_size_t,C.c_void_p)
    SKIP=C.CFUNCTYPE(C.c_int64,C.c_int64,C.c_void_p)
    SEEK=C.CFUNCTYPE(C.c_int32,C.c_int64,C.c_void_p)
    errors=[]
    @READ
    def read(dst,n,_):
        try:
            raw=f.read(n)
            if not raw: return C.c_size_t(-1).value
            C.memmove(dst,raw,len(raw)); return len(raw)
        except Exception as exc:
            errors.append(exc); return C.c_size_t(-1).value
    @SKIP
    def skip(n,_):
        try: f.seek(n,1); return n
        except Exception as exc: errors.append(exc); return -1
    @SEEK
    def seek(n,_):
        try: f.seek(n); return 1
        except Exception as exc: errors.append(exc); return 0
    lib.opj_stream_create.argtypes=[C.c_size_t,C.c_int32]
    lib.opj_stream_create.restype=C.c_void_p
    stream=lib.opj_stream_create(buffer_size,1)
    if not stream: raise MemoryError('OpenJPEG stream')
    codec=image=None
    try:
        for name,cb,typ in (('read',read,READ),('skip',skip,SKIP),('seek',seek,SEEK)):
            fun=getattr(lib,'opj_stream_set_'+name+'_function')
            fun.argtypes,fun.restype=[C.c_void_p,typ],None
            fun(stream,cb)
        lib.opj_stream_set_user_data_length.argtypes=[C.c_void_p,C.c_uint64]
        lib.opj_stream_set_user_data_length.restype=None
        size=f.seek(0,2); f.seek(0)
        lib.opj_stream_set_user_data_length(stream,size)
        codec=opj.create_decompress(opj.CODEC_JP2)
        opj.set_error_handler(codec,opj._ERROR_CALLBACK)
        opj.set_warning_handler(codec,opj._WARNING_CALLBACK)
        params=opj.set_default_decoder_parameters(); params.cp_layer=layers
        opj.setup_decoder(codec,params); opj.codec_set_threads(codec,1)
        image=opj.read_header(stream,codec)
        opj.set_decode_area(codec,image,*bbox)
        opj.decode(codec,stream,image); opj.end_decompress(codec,stream)
        if errors: raise errors[0]
        obj=image.contents
        if obj.numcomps!=3: raise ValueError('Expected RGB')
        channels=[]
        for i in range(3):
            c=obj.comps[i]
            if c.prec!=8 or c.sgnd or c.dx!=1 or c.dy!=1: raise ValueError('Expected RGB8')
            channels.append(np.ctypeslib.as_array(c.data,shape=(c.h*c.w,)).reshape(c.h,c.w))
        return np.stack(channels,axis=2).astype(np.uint8)
    finally:
        if image: opj.image_destroy(image)
        if codec: opj.destroy_codec(codec)
        opj.stream_destroy(stream)
