"""Paid original-position binding. Scientific HDF5 retains exact raw IDs/types."""
import hashlib,json
from pathlib import Path
MAP_NAME='original_gene_positions.json'
def canonical(ids):
    return json.dumps(ids,ensure_ascii=False,separators=(',',':')).encode('utf8')
def document(ids):
    if not ids or len(ids)>65536 or any(type(s) is not str for s in ids):
        raise ValueError('Invalid original gene axis')
    return {'schema':'original-gene-positions-1','axis_length':len(ids),
            'ordered_labels_sha256':hashlib.sha256(canonical(ids)).hexdigest(),
            'positions':list(range(len(ids))),'labels':ids}
def write_map(path,ids):
    with Path(path).open('xb') as f: f.write(canonical(document(ids)))
def _pairs(pairs):
    d={}
    for k,v in pairs:
        if k in d: raise ValueError('Duplicate mapping key')
        d[k]=v
    return d
def validate_map(path,ids):
    p=Path(path)
    if p.stat().st_size>32*1024**2: raise ValueError('Position map too large')
    got=json.loads(p.read_bytes(),object_pairs_hook=_pairs)
    expected=document(ids)
    if (type(got) is not dict or set(got)!=set(expected)
            or type(got.get('axis_length')) is not int
            or type(got.get('positions')) is not list
            or any(type(x) is not int for x in got['positions']) or got!=expected):
        raise ValueError('Original gene-position binding differs from scientific axis')
    return got['positions']
