# STPyramid Archive

**A unified archive for spatial-transcriptomics data.** STPyramid combines spatial Count data, whole-slide image pyramids and metadata in a verifiable package, with lossless recovery and spatial access.

[Releases](https://github.com/zt97152023-ai/stpyramid-archive/releases) · [Source scope](OPEN_SOURCE_SCOPE.md) · [Third-party notices](THIRD_PARTY.md) · [Results](results/README.md)

## Choose what you need

| Goal | Start here |
|---|---|
| Use the desktop application | [Desktop guide](java-desktop/README.md) |
| Run the Python integrated workflow | [CLI quick start](#python-cli) |
| Understand the system design | [Architecture](docs/ARCHITECTURE.md) |
| Build from source | [Build instructions](#build-from-source) |
| Check methods and evidence | [Results and reproducibility](results/README.md) |

## What is included

- **Count archive** — preserves typed H5AD structure and integer count values; the integrated writer uses Shared 2.0.
- **Image archive** — encodes native RGB image-pyramid levels and retains the information required for exact reconstruction.
- **Unified package** — records component identity and metadata, verifies the package before publication, and supports image-region and Count-subset reads.
- **Desktop applications** — Java desktop workflow and the earlier Python desktop workflow are maintained as distinct clients.

## Quick start: desktop

For the current Java desktop distribution, download the matching package from [Releases](https://github.com/zt97152023-ai/stpyramid-archive/releases), extract the complete folder, and launch it:

- Windows: `Launch-STPyramidJava.cmd`
- Linux: `Launch-STPyramidJava.sh`
- macOS: `Launch-STPyramidJava.command`

Keep the launcher, JAR and bundled `jre/` together. The package includes its Java runtime. See the [Java desktop guide](java-desktop/README.md) for platform capabilities, packaging and CLI commands.

The older Python desktop client is documented separately in [desktop/README.md](desktop/README.md).

## Python CLI

The integrated Python CLI is currently qualified for **Windows**. Use 64-bit CPython 3.12.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-windows-tested.txt
python fetch_count_runtime.py
python tests/smoke.py --runtime-dir .\runtime-windows --output .\runs\smoke
```

Create and inspect an archive:

```powershell
python stpyramid.py encode --image slide.tif --counts spots.h5ad --metadata metadata.json --runtime-dir runtime-windows --output runs/archive
python stpyramid.py inspect runs/archive
python stpyramid.py verify runs/archive --runtime-dir runtime-windows
python stpyramid.py restore runs/archive --runtime-dir runtime-windows --output runs/restored
```

Additional reads:

```powershell
python stpyramid.py image runs/archive --runtime-dir runtime-windows --level 0 --bbox 0 0 64 64 --output runs/roi.npy
python stpyramid.py counts runs/archive --rows 0 --genes 0 1 --output runs/counts.npz
```

Use a **new output path** for each encoding run. The encoder uses a sibling work directory for intermediate streams and verification receipts; only the verified final archive is published.

## Build from source

### Native Windows runtime

With Visual Studio 2022 C++ tools, CMake and Ninja:

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5
cmake --build build --parallel 2 --target slide_jxl089 slide_jxl089_shared slide_jxl091 slide_jxl091_shared slide_hf1 slide_hf1_shared
python stage_runtime.py --bin build/bin --output runtime-windows
python tests/smoke.py --runtime-dir runtime-windows --output runs/source-build-smoke
```

### Java desktop

Build and package on the target operating system. Linux and macOS packages bundle a matching JRE:

```sh
./java-desktop/package-linux.sh /path/to/jdk-17 /tmp/STPyramid-Linux-x64
./java-desktop/package-macos.sh /path/to/jdk-17 /tmp/STPyramid-macOS-arm64
```

See [java-desktop/README.md](java-desktop/README.md) for Windows packaging and details.

## Architecture at a glance

```text
Input data
  ├── H5AD ──> Count validation ──> Count codec ─────┐
  ├── TIFF ──> Pyramid scan ──────> Image codec ─────┤
  └── JSON ──> Metadata validation ─────────────────┤
                                                     v
                                      Unified archive + manifest
                                                     |
                                      Verify all components
                                                     |
                                Publish / Inspect / ROI read / Restore
```

The Count and image codecs retain independent encoding and decoding boundaries. The unified container binds their sample identity, coordinate system and metadata; it does not merge their probability models. See [the architecture note](docs/ARCHITECTURE.md) for module responsibilities and data flow.

## Supported scope and important limits

- Integrated Python Count + TIFF encoding/verification is qualified on Windows.
- Linux and macOS Java packages support the platform-independent source-archive workflow. Do not assume the native Count + TIFF backend is qualified on those platforms.
- TIFF input must meet the documented tiled RGB8 pyramid geometry and compression constraints; H5AD Count values must be nonnegative integers with spatial coordinates in L0 pixel coordinates.
- The source repository intentionally excludes study data and native runtime binaries. The Shared Count dependency is fetched at a pinned revision because its upstream original-code license remains pending.

For exact input constraints and distribution boundaries, read [OPEN_SOURCE_SCOPE.md](OPEN_SOURCE_SCOPE.md) and [THIRD_PARTY.md](THIRD_PARTY.md).

## Project structure

| Path | Responsibility |
|---|---|
| `stpyramid.py`, `codec/` | Python CLI and integrated archive/codec implementation |
| `java-desktop/` | Java desktop application, launchers and package builders |
| `desktop*.py`, `desktop/` | Python desktop client |
| `native/`, `CMakeLists.txt` | Native image codec source and build |
| `tests/` | Synthetic end-to-end smoke test |
| `results/` | Aggregate results, evidence ledger and release reports |
| `docs/` | System architecture and developer-facing documentation |
| `upstream/` | Third-party source and its retained notices |

## License and citation

STPyramid original source is released under the [BSD 3-Clause License](LICENSE). Third-party components retain their own licenses and notices; see [THIRD_PARTY.md](THIRD_PARTY.md). Dataset access and dataset licensing are separate from software licensing.
