"""Task-local metadata bound; immutable codecs and all validation stay intact."""
from pathlib import Path

OLD_LIMIT = 16 * 1024 * 1024
RECEIPT_LIMIT = 64 * 1024 * 1024


def wrap(original):
    def read(path, limit):
        path = Path(path)
        if path.name != 'receipt.json' or limit != OLD_LIMIT:
            return original(path, limit)
        for parent in (path, *path.parents):
            if getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('Reparse receipt path forbidden')
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= RECEIPT_LIMIT:
            raise ValueError('Missing, unsafe or oversized native receipt')
        with path.open('rb') as handle:
            data = handle.read(RECEIPT_LIMIT + 1)
        if not 0 < len(data) <= RECEIPT_LIMIT:
            raise ValueError('Changed or oversized native receipt')
        return data
    read._st262_receipt_bound = True
    return read


def install(*modules):
    for module in modules:
        if not getattr(module._read_bounded, '_st262_receipt_bound', False):
            module._read_bounded = wrap(module._read_bounded)
