"""Experimental Shared block execution, with the existing bytes and model.

The model is fixed before independent encoding. This module does not estimate it,
change the wire format or make a block independent of its paid model/axis header.
Full restoration retains a bounded sparse matrix, never a decoded cross-query
cache. It is not a general out-of-core writer for unbounded slides.
"""
from pathlib import Path
import shutil

import h5py
import numpy as np
from scipy.sparse import csr_matrix, vstack

from .integrity import h5_digest, sha256
from .lossless_components import ComponentArchive
from .shared_backend import CountStore, MODEL_NAMES, local_streams, model_tables


class BlockCountStore(CountStore):
    """Same rANS streams, explicit independent blocks and exact fused tables."""

    def __init__(self, root):
        super().__init__(root)
        if self._native or self.index.get('version') != 1:
            self.close()
            raise ValueError('Block execution requires the version1 spatial Count store')
        if self.index.get('method') not in ('Shared_local', 'Shared_blocks'):
            self.close()
            raise ValueError('Unsupported spatial Count method')
        self.model_initializations = 0

    def _model(self, model_id):
        if model_id not in self.tables:
            parts = {n: self.member(self.index['models'][model_id][n]) for n in MODEL_NAMES}
            self.tables[model_id] = model_tables(parts, support_builder='fused', value_builder='fused')
            self.model_initializations += 1
        return self.tables[model_id]

    def decode_block(self, bid):
        if isinstance(bid, (bool, np.bool_)) or not isinstance(bid, (int, np.integer)) or not 0 <= bid < len(self.index['blocks']):
            raise ValueError('Invalid Count block id')
        self._model(self.index['blocks'][bid]['model'])
        # The original decoder retains graph causality, stream SHA, integer
        # event counts and decoded canonical CSR hash checks.
        return super().decode_block(bid)

    def encode_independent(self, matrix, coordinates, *, model_id='global'):
        """Encode one canonical block using only its data and an existing model.

        Caller supplies local coordinates in original row order. Global spot
        identities and gene axis are envelope responsibilities. Returned streams
        need that model and block descriptors; they are not a self-contained ST
        package. No other Count block is opened or decoded.
        """
        if not isinstance(matrix, csr_matrix) or not matrix.has_canonical_format:
            raise ValueError('Canonical CSR block required')
        if matrix.shape[0] < 1 or matrix.shape[1] != self.index['shape'][1]:
            raise ValueError('Count block shape disagrees with frozen model')
        matrix.check_format(full_check=True)
        values = matrix.data
        if (values.dtype.kind not in 'fiu' or not np.isfinite(values).all()
                or np.any(values <= 0) or np.any(values > 2**32 - 1)
                or np.any(values != np.floor(values))):
            raise ValueError('Count data must be canonical positive uint32-range integers')
        xy = np.asarray(coordinates)
        if xy.shape != (matrix.shape[0], 2) or xy.dtype.kind not in 'fiu' or not np.isfinite(xy).all():
            raise ValueError('Finite block coordinates required')
        return local_streams(matrix.astype(np.int64), xy, self._model(model_id))

    def sparse_all(self, *, max_cells=20_000_000):
        """Decode each block once and return canonical CSR in original row order.

        Memory is bounded by this cell limit and sparse assembly temporaries.
        Empty rows are retained. Spatial blocks need not contain contiguous rows.
        """
        n, g = self.index['shape']
        if n * g > max_cells:
            raise ValueError('Sparse assembly exceeds the qualified cell bound')
        if n == 0:
            return csr_matrix((n, g), dtype=np.int64)
        blocks, order = [], []
        for bid, desc in enumerate(self.index['blocks']):
            blocks.append(self.decode_block(bid))
            order.extend(desc['rows'])
        physical = vstack(blocks, format='csr')
        del blocks
        inverse = np.argsort(np.asarray(order, dtype=np.int64))
        return physical[inverse].tocsr()

    def execution_metrics(self):
        return {'model_initializations': self.model_initializations,
                'retained_model_array_bytes': sum(a.nbytes for tables in self.tables.values() for a in tables),
                'decoded_count_cache_bytes': 0,
                'table_backend': 'exact-fused-support-and-values',
                'independent_encoding_dependency': 'existing fixed paid model and gene axis'}


class SharedBlockArchive(ComponentArchive):
    """Opt-in compatible reader; original JPEG XL and ordinary reader unchanged."""

    def _store(self):
        if self._count_store is None:
            path = self.root / 'counts.index.json'
            if sha256(path) != self.manifest['files'][path.name]['sha256']:
                raise ValueError('Count index integrity failure')
            self.index_read_bytes += path.stat().st_size
            self._count_store = BlockCountStore(self.root)
            if self._count_store.index['shape'] != self.manifest['expression']['shape']:
                raise ValueError('Count shape disagrees with archive')
        return self._count_store

    def counts(self, rows=None, genes=None, *, max_cells=20_000_000):
        if self.manifest['count_component'] == 'hdf5':
            return super().counts(rows, genes, max_cells=max_cells)
        md = self.metadata()
        ri, gi = self._select(rows, md['spot_ids']), self._select(genes, md['gene_ids'])
        if len(ri) * len(gi) > max_cells:
            raise ValueError('Query too large; read in row batches')
        values = (self._store().read(ri, gi) if len(ri) and len(gi)
                  else np.zeros((len(ri), len(gi)), np.uint64))
        return {'counts': values, 'spot_ids': np.asarray([md['spot_ids'][i] for i in ri], dtype=str),
                'gene_ids': np.asarray([md['gene_ids'][i] for i in gi], dtype=str),
                'coordinates': md['coordinates'][ri], 'level0_xy': md['level0_xy'][ri]}

    def restore_expression(self, target):
        if self.manifest['count_component'] == 'hdf5':
            return super().restore_expression(target)
        arg = Path(target)
        if arg.is_symlink():
            raise ValueError('Symlink output is unsupported')
        target = arg.resolve()
        if target == self.root or self.root in target.parents:
            raise ValueError('Output must be outside the immutable archive')
        if target.exists():
            raise FileExistsError(target)
        source = self.root / 'science.h5'
        if sha256(source) != self.manifest['files']['science.h5']['sha256']:
            raise ValueError('Scientific metadata integrity failure')
        matrix = self._store().sparse_all()
        # Create-only copy; interrupted or corrupt restores remain as evidence.
        with source.open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst)
        with h5py.File(target, 'r+') as handle:
            x = handle['X']
            if isinstance(x, h5py.Dataset):
                attrs, dtype = dict(x.attrs), x.dtype
                del handle['X']
                x = handle.create_dataset('X', shape=matrix.shape, dtype=dtype,
                                          compression='gzip', compression_opts=4)
                x.attrs.update(attrs)
                # Densify only an output batch, never the complete matrix.
                for start in range(0, matrix.shape[0], 32):
                    stop = min(start + 32, matrix.shape[0])
                    x[start:stop] = matrix[start:stop].toarray().astype(dtype)
            else:
                for key, values in (('data', matrix.data), ('indices', matrix.indices), ('indptr', matrix.indptr)):
                    attrs, dtype = dict(x[key].attrs), x[key].dtype
                    del x[key]
                    dataset = x.create_dataset(key, data=values.astype(dtype), compression='gzip', compression_opts=4)
                    dataset.attrs.update(attrs)
        if h5_digest(target) != self.manifest['expression']['semantic_sha256']:
            raise ValueError('H5AD semantic digest mismatch')

    def metrics(self):
        result = super().metrics()
        if self._count_store is not None:
            result['shared_execution'] = self._count_store.execution_metrics()
        return result
