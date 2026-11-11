"""Task-local process controls only; no scientific or codec implementation."""
import math
import os
from pathlib import Path
import stat
import time


def assigned_cpu():
    value = os.environ.get('ST262_CPU', '')
    if not value.isdecimal() or not 0 <= int(value) < 64:
        raise RuntimeError('ST262_CPU must be a single permitted Windows CPU index')
    return int(value)


def remaining_seconds():
    try:
        deadline = float(os.environ['ST262_DEADLINE_EPOCH'])
    except (KeyError, ValueError) as exc:
        raise RuntimeError('Finite supervised ST262_DEADLINE_EPOCH required') from exc
    if not math.isfinite(deadline):
        raise RuntimeError('Finite supervised ST262_DEADLINE_EPOCH required')
    remaining = deadline - time.time()
    if remaining <= 0:
        raise TimeoutError('ST262 worker deadline expired')
    return remaining


def plain_directory(value):
    path = Path(value).absolute()
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            attrs = getattr(item.lstat(), 'st_file_attributes', 0)
            if item.is_symlink() or attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('Reparse/alias worker path forbidden')
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError('Existing ordinary worker directory required')
    return resolved
