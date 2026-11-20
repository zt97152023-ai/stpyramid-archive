"""Mature-codec backends. All required assets live inside the archive directory.

SpatialData stores a native Count table and linked spot geometry; the common
science.h5 skeleton preserves every original HDF5 attribute and non-X object.
The skeleton contains NO Count values. This counted compatibility sidecar is
needed for the stricter STBaseline restoration contract, not by SpatialData.
"""
import copy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import time

import h5py
import numpy as np

from .integrity import h5_digest, sha256, text_ids, write_json
from .reader import Archive, MeteredFile
from .local_ipc import authenticate_loopback, new_ipc_token

FORMAT = "stbaseline-comparison-1"
METHODS = ("A_spatialdata", "B_native_tiff", "C_zstd_tiff", "D_jxl_tiles")


def r_environment(library):
    env = os.environ.copy()
    env.update(R_LIBS_USER=str(library), LANG="C", LC_ALL="C",
               OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    return env


def csr_from_h5(path):
    from scipy.sparse import csr_matrix
    with h5py.File(path, "r") as f:
        x = f["X"]
        if isinstance(x, h5py.Dataset):
            return csr_matrix(x[:].astype(np.uint32))
        if np.any(x["data"][:] == 0):
            raise ValueError("This adapter requires canonical CSR without explicit stored zeros")
        return csr_matrix((x["data"][:].astype(np.uint32), x["indices"][:], x["indptr"][:]), shape=x.attrs["shape"])


def science_skeleton(source, target):
    with h5py.File(source, "r") as src, h5py.File(target, "x") as dst:
        for k, v in src.attrs.items():
            dst.attrs[k] = v
        for k in src:
            if k != "X":
                src.copy(k, dst)
        x = src["X"]
        if isinstance(x, h5py.Dataset):
            g = dst.create_dataset("X", shape=(0, 0), dtype=x.dtype)
        else:
            g = dst.create_group("X")
            if set(x) != {"data", "indices", "indptr"}:
                raise ValueError("Unexpected CSR children")
            for k in x:
                d = g.create_dataset(k, shape=(0,), dtype=x[k].dtype)
                for a, v in x[k].attrs.items():
                    d.attrs[a] = v
        for a, v in x.attrs.items():
            g.attrs[a] = v


def pack_baseline(reference, source_wsi, output, *, method, rscript, r_library,
                  tile=256, zstd_level=9, jpegxl_effort=5, zarr_level=5,
                  bpcells_source=None):
    import tifffile
    import imagecodecs
    if method not in METHODS:
        raise ValueError(method)
    out = Path(output)
    out.mkdir(parents=True, exist_ok=False)
    (out / "INCOMPLETE").write_text("Incomplete baseline build\n")
    start = time.perf_counter()
    try:
        with Archive(reference) as ref:
            m = copy.deepcopy(ref.manifest)
            m.update(format=FORMAT, method=method)
            science_skeleton(ref.root / "expression.h5ad", out / "science.h5")
            for a in m.get("attachments", []):
                shutil.copyfile(ref.root / a, out / a)
            m["expression"]["path"] = "science.h5"
            m["expression"]["storage"] = "zarr-csr" if method == METHODS[0] else "BPCells-uint32-genes-by-spots"
            m["compatibility_sidecar"] = "science.h5 preserves complete original non-X content and empty X dtype/attribute templates; no fallback Count values"
            if method == METHODS[0]:
                _write_spatialdata(ref, out, tile, zarr_level)
                m["image"].update(path="data.zarr", codec="spatialdata-zarr-blosc-zstd")
            else:
                if bpcells_source is None:
                    from scipy.io import mmwrite
                    with tempfile.TemporaryDirectory(prefix="stbaseline_bp_") as tmp:
                        inp = Path(tmp) / "input.mtx"
                        mmwrite(inp, csr_from_h5(ref.root / "expression.h5ad").T, field="integer")
                        run = subprocess.run([rscript, "--vanilla", str(Path(__file__).with_name("bpcells_worker.R")),
                                              "encode", str(inp), str((out / "counts.bp").resolve())],
                                             env=r_environment(r_library), cwd=tmp, capture_output=True, text=True, timeout=600)
                        if run.returncode:
                            raise RuntimeError(run.stdout + run.stderr)
                else:
                    shutil.copytree(bpcells_source, out / "counts.bp")
                if method == "B_native_tiff":
                    if sha256(source_wsi) != m["sources"]["wsi"]["sha256"]:
                        raise ValueError("Native WSI differs from qualified source")
                    shutil.copyfile(source_wsi, out / "images.tif")
                    m["image"].update(path="images.tif", codec="native-tiled-tiff")
                elif method == "C_zstd_tiff":
                    with tifffile.TiffWriter(out / "images.tif") as writer:
                        for info in m["image"]["levels"]:
                            h, w, _ = info["shape"]
                            pixels = ref.image(info["level"], [0, 0, w, h])
                            writer.write(pixels, photometric="rgb", tile=(tile, tile), compression="zstd",
                                         compressionargs={"level": zstd_level}, predictor=2, metadata=None,
                                         subfiletype=1 if info["level"] else 0, maxworkers=1)
                    m["image"].update(path="images.tif", codec="tiled-tiff-zstd-horizontal")
                else:
                    index = []
                    with open(out / "images.jxlbank", "xb") as bank:
                        for info in m["image"]["levels"]:
                            h, w, _ = info["shape"]
                            entries = []
                            for y in range(0, h, tile):
                                for x in range(0, w, tile):
                                    pixels = ref.image(info["level"], [x, y, min(x+tile,w), min(y+tile,h)])
                                    blob = imagecodecs.jpegxl_encode(pixels, lossless=True, effort=jpegxl_effort, numthreads=1)
                                    entries.append([bank.tell(), len(blob)])
                                    bank.write(blob)
                            index.append(entries)
                    write_json(out / "tiles.json", {"tile": tile, "levels": index})
                    m["image"].update(path="images.jxlbank", codec="independent-tile-jpegxl-lossless", index="tiles.json")
            m["build"] = {"tile": tile, "zstd_level": zstd_level, "jpegxl_effort": jpegxl_effort,
                          "zarr_level": zarr_level,
                          "versions": {n: importlib.metadata.version(n) for n in ("spatialdata", "zarr", "tifffile", "imagecodecs", "numpy", "h5py")},
                          "imagecodecs_libraries": imagecodecs.version()}
            m["files"] = {p.relative_to(out).as_posix(): {"bytes": p.stat().st_size, "sha256": sha256(p)}
                          for p in sorted(out.rglob("*")) if p.is_file() and p.name != "INCOMPLETE"}
            write_json(out / "manifest.json", m)
            (out / "INCOMPLETE").unlink()
        return {"method": method, "build_seconds": time.perf_counter()-start,
                "bytes": sum(p.stat().st_size for p in out.rglob("*") if p.is_file())}
    except Exception as e:
        write_json(out / "failure.json", {"error": repr(e)})
        raise


def _write_spatialdata(ref, out, tile, zarr_level):
    import anndata as ad
    import pandas as pd
    import geopandas as gpd
    import spatialdata as sd
    from spatialdata.models import Image2DModel, ShapesModel, TableModel
    from spatialdata.transformations import Identity, Scale
    from shapely.geometry import Point
    import zarr
    from numcodecs import Blosc
    # Explicit native levels: no pyramid generation or resampling.
    images = {}
    h0, w0, _ = ref.manifest["image"]["levels"][0]["shape"]
    for info in ref.manifest["image"]["levels"]:
        level = info["level"]
        h, w, _ = info["shape"]
        images[f"native_{level}"] = Image2DModel.parse(
            ref.image(level, [0,0,w,h]).transpose(2,0,1), dims=("c","y","x"),
            chunks=(3,tile,tile), c_coords=["R","G","B"],
            transformations={"global": Scale([w0/w,h0/h], axes=("x","y"))})
    meta = ref.metadata()
    shapes = ShapesModel.parse(gpd.GeoDataFrame({"geometry": [Point(*p) for p in meta["level0_xy"]],
                                               "radius": np.full(len(meta["spot_ids"]), .5)},
                                              index=np.arange(len(meta["spot_ids"]))),
                               transformations={"global": Identity()})
    obs = pd.DataFrame({"region": pd.Categorical(["spots"]*len(meta["spot_ids"])),
                        "instance_id": np.arange(len(meta["spot_ids"]))}, index=meta["spot_ids"])
    table = ad.AnnData(csr_from_h5(ref.root / "expression.h5ad"), obs=obs,
                       var=pd.DataFrame(index=meta["gene_ids"]))
    table = TableModel.parse(table, region="spots", region_key="region", instance_key="instance_id")
    data = sd.SpatialData(images=images, shapes={"spots": shapes}, tables={"counts": table})
    # SpatialData writes schema; explicit array codec replacement preserves that schema.
    data.write(out / "data.zarr")
    group = zarr.open_group(out / "data.zarr", mode="a", use_consolidated=False)
    _recompress_zarr(group, zarr_level)
    zarr.consolidate_metadata(out / "data.zarr")
    # Force an actual framework read, not merely a lookalike Zarr directory.
    loaded = sd.read_zarr(out / "data.zarr")
    if set(loaded.images) != set(images) or loaded.tables["counts"].shape != table.shape:
        raise ValueError("SpatialData native read failed")


def _recompress_zarr(group, level):
    import zarr
    from numcodecs import Blosc
    for name in list(group.keys()):
        obj = group[name]
        if isinstance(obj, zarr.Group):
            _recompress_zarr(obj, level)
        elif obj.dtype.kind in "ui f".replace(" ", ""):
            values, attrs, chunks = obj[:], dict(obj.attrs), obj.chunks
            dimensions = getattr(obj.metadata, "dimension_names", None)
            del group[name]
            codec = (Blosc(cname="zstd", clevel=level, shuffle=Blosc.SHUFFLE) if group.metadata.zarr_format == 2
                     else zarr.codecs.BloscCodec(cname="zstd", clevel=level, shuffle="shuffle"))
            opts = {"compressor": codec} if group.metadata.zarr_format == 2 else {"compressors": [codec]}
            if dimensions is not None:
                opts["dimension_names"] = dimensions
            group.create_array(name, data=values, chunks=chunks, attributes=attrs, **opts)


class BPWorker:
    def __init__(self, path, rscript, library):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        token = new_ipc_token()
        self.process = subprocess.Popen(
            [rscript, "--vanilla", str(Path(__file__).with_name("bpcells_worker.R")),
             "serve", str(path), str(listener.getsockname()[1])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=str(path.parent), env=r_environment(library))
        try:
            # The one-time secret travels over the private child stdin pipe, not
            # argv (which is commonly visible in process listings).
            self.process.stdin.write(token + "\\n")
            self.process.stdin.flush()
            self.process.stdin.close()
            self.channel = authenticate_loopback(listener, token, timeout=30)
            self.channel.settimeout(120)
            self.channel.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            raise
        finally:
            listener.close()
        self.last_native_seconds = None

    def _receive(self, n):
        result = bytearray(n)
        view = memoryview(result)
        done = 0
        while done < n:
            got = self.channel.recv_into(view[done:])
            if got == 0:
                raise RuntimeError("BPCells worker closed its binary connection")
            done += got
        return result

    def read(self, rows, genes, all_genes=False):
        if not len(rows) or not len(genes):
            return np.zeros((len(rows), len(genes)), dtype=np.uint64)
        field = "*" if all_genes else ",".join(map(str, genes))
        self.channel.sendall((",".join(map(str, rows)) + "\\t" + field + "\\n").encode("ascii"))
        elapsed, size = struct.unpack("<dd", self._receive(16))
        if elapsed < 0:
            raise RuntimeError(self._receive(int(size)).decode("utf-8", errors="replace"))
        if size != len(rows) * len(genes) or size < 0:
            raise ValueError("BPCells binary response shape mismatch")
        self.last_native_seconds = elapsed
        arr = np.frombuffer(self._receive(int(size) * 8), dtype="<f8").astype(np.uint64)
        return arr.reshape(len(rows), len(genes))

    def close(self):
        if self.process.poll() is None:
            self.channel.sendall(b"QUIT\\n")
            self.process.wait(timeout=15)
        self.channel.close()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


class BaselineArchive(Archive):
    FORMAT = FORMAT
    METHODS = METHODS
    def __init__(self, directory, *, rscript=None, r_library=None):
        self.root = Path(directory).resolve()
        if (self.root / "INCOMPLETE").exists():
            raise ValueError("Incomplete archive")
        blob = (self.root / "manifest.json").read_bytes()
        self.manifest, self.manifest_bytes = json.loads(blob), len(blob)
        if self.manifest.get("format") != self.FORMAT or self.manifest.get("method") not in self.METHODS:
            raise ValueError("Unknown comparison archive")
        self.files, self.handles, self._metadata = {}, {}, None
        self._bp, self._tiff, self._zarr, self._tile_index = None, None, None, None
        self._image_arrays = {}
        self._sparse = None
        self.rscript = rscript or os.environ.get("STBASELINE_RSCRIPT", "Rscript")
        self.r_library = r_library or os.environ.get("STBASELINE_R_LIBRARY", "")
        self.index_read_bytes = 0
        self.zarr_reads = []
        for name in self.manifest["files"]:
            p = self.root / name
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("Unsafe member name")

    def metadata(self):
        if self._metadata is None:
            f = self._open("science.h5")
            rows, genes = text_ids(f["obs"]), text_ids(f["var"])
            xy = f["obsm"][self.manifest["mapping"]["spatial_key"]][:]
            t = np.asarray(self.manifest["mapping"]["xy_to_level0"])
            self._metadata = {"spot_ids": rows, "gene_ids": genes, "coordinates": xy,
                              "level0_xy": (np.c_[xy,np.ones(len(xy))] @ t.T)[:,:2]}
        return self._metadata

    def _zgroup(self):
        if self._zarr is None:
            import zarr
            from zarr.storage import LocalStore, WrapperStore
            records = self.zarr_reads
            class MeteredStore(WrapperStore):
                async def get(self, key, prototype, byte_range=None):
                    result = await self._store.get(key, prototype, byte_range)
                    if result is not None:
                        records.append((key, len(result)))
                    return result
                async def _get_many(self, requests):
                    async for key, result in self._store._get_many(requests):
                        if result is not None:
                            records.append((key,len(result)))
                        yield key,result
                async def get_partial_values(self, prototype, key_ranges):
                    requests=list(key_ranges)
                    results=await self._store.get_partial_values(prototype,requests)
                    for (key,_),result in zip(requests,results):
                        if result is not None:
                            records.append((key,len(result)))
                    return results
            self._zarr = zarr.open_group(MeteredStore(LocalStore(self.root / "data.zarr", read_only=True)), mode="r", use_consolidated=False)
        return self._zarr

    def counts(self, rows=None, genes=None, *, max_cells=20_000_000):
        meta = self.metadata()
        ri, gi = self._select(rows,meta["spot_ids"]), self._select(genes,meta["gene_ids"])
        if len(ri)*len(gi)>max_cells:
            raise ValueError("Query too large")
        if self.manifest["method"] == "A_spatialdata":
            from anndata.io import sparse_dataset
            if self._sparse is None:
                self._sparse = sparse_dataset(self._zgroup()["tables/counts/X"])
            result = self._sparse[ri][:,gi].toarray().astype(np.uint64)
        else:
            if self._bp is None:
                self._bp = BPWorker(self.root / "counts.bp",self.rscript,self.r_library)
            result = self._bp.read(ri,gi,all_genes=genes is None)
        return {"counts":result,"spot_ids":np.asarray([meta["spot_ids"][i] for i in ri],dtype=str),
                "gene_ids":np.asarray([meta["gene_ids"][i] for i in gi],dtype=str),
                "coordinates":meta["coordinates"][ri],"level0_xy":meta["level0_xy"][ri]}

    def image(self, level, bbox):
        import tifffile
        import imagecodecs
        info = self._level(level)
        if len(bbox)!=4 or any(isinstance(v,(bool,np.bool_)) or not isinstance(v,(int,np.integer)) for v in bbox):
            raise ValueError("Integer image box required")
        x0,y0,x1,y1 = map(int,bbox)
        h,w,_ = info["shape"]
        if not (0<=x0<x1<=w and 0<=y0<y1<=h):
            raise ValueError("Image ROI outside native level")
        method = self.manifest["method"]
        if method == "A_spatialdata":
            if level not in self._image_arrays:
                group = self._zgroup()[f"images/native_{level}"]
                attrs = dict(group.attrs)
                path = attrs.get("ome",attrs)["multiscales"][0]["datasets"][0]["path"]
                self._image_arrays[level] = group[path]
            return self._image_arrays[level][:,y0:y1,x0:x1].transpose(1,2,0)
        name = self.manifest["image"]["path"]
        if name not in self.files:
            self.files[name] = MeteredFile(self.root / name)
        stream = self.files[name]
        result = np.empty((y1-y0,x1-x0,3),dtype=np.uint8)
        if method == "D_jxl_tiles":
            if self._tile_index is None:
                b = (self.root / "tiles.json").read_bytes()
                self.index_read_bytes += len(b)
                self._tile_index = json.loads(b)
            th = tw = self._tile_index["tile"]
            entries = self._tile_index["levels"][level]
        else:
            if self._tiff is None:
                self._tiff = tifffile.TiffFile(stream)
            page = self._tiff.pages[level]
            if not page.is_tiled or page.planarconfig != 1:
                raise ValueError("Only contiguous tiled RGB TIFF is supported")
            th,tw = page.tilelength,page.tilewidth
        nx = (w+tw-1)//tw
        for ty in range(y0//th,(y1-1)//th+1):
            for tx in range(x0//tw,(x1-1)//tw+1):
                i = ty*nx+tx
                offset,length = entries[i] if method=="D_jxl_tiles" else (page.dataoffsets[i],page.databytecounts[i])
                stream.seek(offset)
                blob = stream.read(length)
                if len(blob)!=length:
                    raise ValueError("Truncated image tile")
                pixels = (imagecodecs.jpegxl_decode(blob,numthreads=1) if method=="D_jxl_tiles"
                          else page.decode(blob,i)[0][0])
                xa,ya,xb,yb = max(x0,tx*tw),max(y0,ty*th),min(x1,(tx+1)*tw),min(y1,(ty+1)*th)
                result[ya-y0:yb-y0,xa-x0:xb-x0] = pixels[ya-ty*th:yb-ty*th,xa-tx*tw:xb-tx*tw]
        return result

    def ledger(self):
        members = {p.relative_to(self.root).as_posix():p.stat().st_size for p in self.root.rglob("*") if p.is_file()}
        if set(members) != {"manifest.json",*self.manifest["files"]}:
            raise ValueError("Unexpected package members")
        categories = {"image":0,"count":0,"science_metadata":0,"framework_metadata":0,"manifest":0,"attachments":0}
        for name,size in members.items():
            if name=="manifest.json": key="manifest"
            elif name in self.manifest.get("attachments",[]): key="attachments"
            elif name=="science.h5": key="science_metadata"
            elif name.startswith("counts.") or name.startswith("data.zarr/tables/"): key="count"
            elif name.startswith("images.") or name=="tiles.json" or name.startswith("data.zarr/images/"): key="image"
            else: key="framework_metadata"
            categories[key]+=size
        return {"members":members,"categories":categories,"total_package_bytes":sum(members.values()),
                "source_file_bytes":sum(v["bytes"] for v in self.manifest["sources"].values())}

    def metrics(self):
        members = {n:s.metrics() for n,s in self.files.items()}
        n = self.manifest_bytes+self.index_read_bytes+sum(v["file_read_bytes"] for v in members.values())+sum(v for _,v in self.zarr_reads)
        return {"instrumented_file_read_bytes":n,"members":members,"zarr_requested_bytes":sum(v for _,v in self.zarr_reads),
                "manifest_read_bytes":self.manifest_bytes,"index_read_bytes":self.index_read_bytes,
                "bpcells_read_bytes":None,"physical_device_bytes":None,
                "scope":"Application reads; BPCells native reads uninstrumented; OS cache uncontrolled"}

    def restore_expression(self, target):
        from scipy.sparse import csr_matrix, vstack
        shutil.copyfile(self.root / "science.h5",target)
        parts = [csr_matrix(self.counts(range(s,min(s+32,self.manifest["expression"]["shape"][0])))["counts"])
                 for s in range(0,self.manifest["expression"]["shape"][0],32)]
        matrix = vstack(parts,format="csr")
        with h5py.File(target,"r+") as f:
            x = f["X"]
            if isinstance(x,h5py.Dataset):
                attrs,dtype = dict(x.attrs),x.dtype
                del f["X"]
                x = f.create_dataset("X",data=matrix.toarray().astype(dtype),compression="gzip",compression_opts=4)
                x.attrs.update(attrs)
            else:
                for key,values in (("data",matrix.data),("indices",matrix.indices),("indptr",matrix.indptr)):
                    attrs,dtype = dict(x[key].attrs),x[key].dtype
                    del x[key]
                    d = x.create_dataset(key,data=values.astype(dtype),compression="gzip",compression_opts=4)
                    d.attrs.update(attrs)
        if h5_digest(target)!=self.manifest["expression"]["semantic_sha256"]:
            raise ValueError("Full original H5AD semantics did not restore")

    def verify(self):
        for name,expected in self.manifest["files"].items():
            if sha256(self.root/name)!=expected["sha256"]:
                raise ValueError("File SHA mismatch: "+name)
        with tempfile.TemporaryDirectory(prefix="stbaseline_verify_") as tmp:
            self.restore_expression(Path(tmp)/"expression.h5ad")
        checks=[]
        for info in self.manifest["image"]["levels"]:
            h,w,_ = info["shape"]
            digest=hashlib.sha256()
            for y in range(0,h,256):
                digest.update(self.image(info["level"],[0,y,w,min(y+256,h)]).tobytes())
            if digest.hexdigest()!=info["pixel_sha256"]:
                raise ValueError("Pixel mismatch at level "+str(info["level"]))
            checks.append({"level":info["level"],"pixels_exact":True})
        return {"ok":True,"h5ad_semantics_exact":True,"levels":checks,"ledger":self.ledger()}

    def restore(self,destination):
        import tifffile
        dest=Path(destination)
        dest.mkdir(parents=True,exist_ok=False)
        self.restore_expression(dest/"expression.h5ad")
        with tifffile.TiffWriter(dest/"native_levels.tif") as writer:
            for info in self.manifest["image"]["levels"]:
                h,w,_=info["shape"]
                pixels=self.image(info["level"],[0,0,w,h])
                if hashlib.sha256(pixels.tobytes()).hexdigest()!=info["pixel_sha256"]:
                    raise ValueError("Export image hash mismatch")
                writer.write(pixels,photometric="rgb",compression="deflate",metadata=None)
        for name in self.manifest.get("attachments",[]):
            if sha256(self.root/name)!=self.manifest["files"][name]["sha256"]:
                raise ValueError("Export attachment hash mismatch")
            shutil.copyfile(self.root/name,dest/name)
        shutil.copyfile(self.root/"manifest.json",dest/"source_metadata.json")
        return str(dest)

    def close(self):
        if self._bp is not None:
            self._bp.close()
        if self._tiff is not None:
            self._tiff.close()
        super().close()


def open_archive(directory, **kwargs):
    blob=(Path(directory)/"manifest.json").read_bytes()
    kind=json.loads(blob).get("format")
    if kind == "stbaseline-access-1":
        from .access_backend import AccessArchive
        result = AccessArchive(directory, **kwargs)
    elif kind == "stbaseline-progressive-1":
        from .progressive import ProgressiveArchive
        result = ProgressiveArchive(directory, **kwargs)
    elif kind == "stbaseline-image-shared-1":
        from .image_shared import ImageSharedArchive
        result = ImageSharedArchive(directory, **kwargs)
    elif kind == "stbaseline-shared-1":
        from .shared_backend import SharedArchive
        result = SharedArchive(directory, **kwargs)
    else:
        result=BaselineArchive(directory,**kwargs) if kind==FORMAT else Archive(directory)
    result.manifest_bytes += len(blob)  # Count the dispatch read as well as constructor read.
    return result

