"""Fetch only the 19 public source files pinned by the Count codec."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
import time
from urllib.error import HTTPError
from urllib.request import urlopen

VENDOR = Path(__file__).resolve().parent / "codec/stbaseline/_shared_vendor"
ORIGIN = "https://raw.githubusercontent.com/zt97152023-ai/shared-st-count-codec/"
COMMIT = "4cf99cc6842cf9e9b680c1270a7e3375b6b402e1"


def download(uri, limit):
    for attempt in range(4):
        try:
            with urlopen(uri, timeout=30) as stream:
                return stream.read(limit)
        except OSError as exc:
            if attempt == 3 or (isinstance(exc, HTTPError) and exc.code != 429 and exc.code < 500):
                raise
            time.sleep(0.5 * 2**attempt)


def fetch(row):
    relative = PurePosixPath(row["path"])
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "baseline":
        raise ValueError("Unexpected Count source member")
    path = VENDOR.joinpath(*relative.parts)
    size = row["bytes"]
    if path.exists():
        data = path.read_bytes()
    else:
        data = download(ORIGIN + COMMIT + "/" + relative.as_posix(), size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError("Count source did not match its pin: " + relative.as_posix())
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return path


def pinned_files():
    return json.loads((VENDOR / "SOURCE.json").read_text(encoding="utf-8-sig"))["files"]


def count_source_ready():
    return all((VENDOR / pin["path"]).is_file() for pin in pinned_files())


def fetch_count_source():
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(fetch, pinned_files()))


if __name__ == "__main__":
    files = fetch_count_source()
    print(f"Verified {len(files)} Count runtime source files at {COMMIT}")
