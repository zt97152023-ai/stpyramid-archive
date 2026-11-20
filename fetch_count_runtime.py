"""Fetch only the 19 public source files pinned by the Count codec.

The pin manifest is part of the trusted source checkout. Every cached file is
re-hashed before the worker treats the dependency as ready; existence alone is
never accepted as evidence of integrity.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from tempfile import NamedTemporaryFile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen

VENDOR = Path(__file__).resolve().parent / "codec/stbaseline/_shared_vendor"
ORIGIN = "https://raw.githubusercontent.com/zt97152023-ai/shared-st-count-codec/"
COMMIT = "4cf99cc6842cf9e9b680c1270a7e3375b6b402e1"
EXPECTED_FILES = 19
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _safe_target(row):
    raw = row.get("path") if isinstance(row, dict) else None
    if (not isinstance(raw, str) or not raw or "\\" in raw or ":" in raw
            or raw.startswith("/") or any(part in ("", ".", "..") for part in raw.split("/"))):
        raise ValueError("Unexpected Count source member")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or not relative.parts or relative.parts[0] != "baseline":
        raise ValueError("Unexpected Count source member")
    root = VENDOR.resolve()
    path = root.joinpath(*relative.parts)
    # Reject symlinks in every existing component, including the leaf.
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Count source path contains a symbolic link")
    if not path.resolve(strict=False).is_relative_to(root):
        raise ValueError("Count source path escapes the vendor directory")
    return path


def _validate_row(row):
    if not isinstance(row, dict):
        raise ValueError("Invalid Count source pin")
    path = _safe_target(row)
    size = row.get("bytes")
    digest = row.get("sha256")
    if (type(size) is not int or not 0 <= size <= MAX_SOURCE_FILE_BYTES
            or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None):
        raise ValueError("Invalid Count source size or SHA-256 pin")
    return path, size, digest


def _matches(path, size, expected):
    if path.is_symlink() or not path.is_file():
        return False
    if path.stat().st_size != size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == expected


def download(uri, limit):
    for attempt in range(4):
        try:
            with urlopen(uri, timeout=30) as stream:
                final = urlsplit(stream.geturl())
                if final.scheme != "https" or final.hostname != "raw.githubusercontent.com":
                    raise ValueError("Count source download redirected to an untrusted host")
                data = stream.read(limit)
                if len(data) >= limit:
                    raise ValueError("Count source download exceeded its pinned size")
                return data
        except OSError as exc:
            if attempt == 3 or (isinstance(exc, HTTPError) and exc.code != 429 and exc.code < 500):
                raise
            time.sleep(0.5 * 2**attempt)


def fetch(row):
    path, size, expected = _validate_row(row)
    if path.exists():
        if not _matches(path, size, expected):
            raise ValueError("Cached Count source differs from its pin: " + row["path"])
        return path

    data = download(ORIGIN + COMMIT + "/" + PurePosixPath(row["path"]).as_posix(), size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Downloaded Count source did not match its pin: " + row["path"])

    path.parent.mkdir(parents=True, exist_ok=True)
    # Re-check after directory creation to fail closed if a path was replaced.
    path, _, _ = _validate_row(row)
    if path.exists():
        if not _matches(path, size, expected):
            raise ValueError("Count source appeared with unexpected contents: " + row["path"])
        return path

    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Revalidate the destination immediately before the atomic publication.
        path, _, _ = _validate_row(row)
        if path.exists():
            if not _matches(path, size, expected):
                raise ValueError("Count source destination changed during download: " + row["path"])
        else:
            os.replace(temporary, path)
            temporary = None
        if not _matches(path, size, expected):
            raise ValueError("Published Count source failed its pin: " + row["path"])
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def pinned_files():
    manifest = json.loads((VENDOR / "SOURCE.json").read_text(encoding="utf-8-sig"))
    if (not isinstance(manifest, dict) or manifest.get("commit") != COMMIT
            or manifest.get("origin") != "https://github.com/zt97152023-ai/shared-st-count-codec"
            or not isinstance(manifest.get("files"), list)
            or len(manifest["files"]) != EXPECTED_FILES):
        raise ValueError("Count source manifest does not match the pinned upstream revision")
    rows = manifest["files"]
    seen = set()
    for row in rows:
        path, _, _ = _validate_row(row)
        relative = path.relative_to(VENDOR.resolve()).as_posix()
        if relative in seen:
            raise ValueError("Duplicate Count source pin: " + relative)
        seen.add(relative)
    return rows


def verify_cached_count_source():
    """Raise unless every cached dependency file matches its trusted pin."""
    rows = pinned_files()
    for row in rows:
        if not _matches(*_validate_row(row)):
            raise ValueError("Cached Count source differs from its pin: " + row["path"])
    return rows


def count_source_ready():
    """Return true only when every cached dependency file matches its pin."""
    try:
        verify_cached_count_source()
        return True
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def fetch_count_source():
    rows = pinned_files()
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(fetch, rows))


if __name__ == "__main__":
    files = fetch_count_source()
    print(f"Verified {len(files)} Count runtime source files at {COMMIT}")
