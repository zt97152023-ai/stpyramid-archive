"""Fetch only the 19 public source files pinned by the Count codec."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path, PurePosixPath
from urllib.request import urlopen

VENDOR = Path(__file__).resolve().parent / "codec/stbaseline/_shared_vendor"
ORIGIN = "https://raw.githubusercontent.com/zt97152023-ai/shared-st-count-codec/"
COMMIT = "4cf99cc6842cf9e9b680c1270a7e3375b6b402e1"


def fetch(row):
    relative = PurePosixPath(row["path"])
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "baseline":
        raise ValueError("Unexpected Count source member")
    path = VENDOR.joinpath(*relative.parts)
    size = row["bytes"]
    if path.exists():
        data = path.read_bytes()
    else:
        with urlopen(ORIGIN + COMMIT + "/" + relative.as_posix(), timeout=30) as stream:
            data = stream.read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError("Count source did not match its pin: " + relative.as_posix())
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
    return path


if __name__ == "__main__":
    manifest = json.loads((VENDOR / "SOURCE.json").read_text(encoding="utf-8"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        files = list(pool.map(fetch, manifest["files"]))
    print(f"Verified {len(files)} Count runtime source files at {COMMIT}")
