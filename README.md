# STPyramid Archive

**A unified archive for spatial-transcriptomics data:** preserve whole-slide image pyramids and spatial gene-expression counts together, verify their integrity, and retrieve selected regions when needed.

STPyramid has two complementary components:

- **Count** — stores the H5AD scientific structure and losslessly encodes integer expression values.
- **Image** — stores the native RGB levels of a tiled TIFF/BigTIFF pyramid using lossless image records.

Both components are bound by one manifest, source identity and level-0 coordinate system. They retain independent decoding boundaries.

## Choose how to use STPyramid

| Goal | Recommended entry | Platform / scope |
|---|---|---|
| Use the desktop application | [Java Desktop guide](java-desktop/README.md) | Windows, Linux and macOS; source-archive workflow |
| Run the integrated Count + TIFF codec | Python CLI: `stpyramid.py` | Qualified Windows native runtime |
| Run the fast fused image encoder | Java Desktop, select **Fast fusion** | Use the matching packaged native runtime |
| Run maximum-compression image search | Python CLI / research workflow | Qualified Windows native runtime |

**Important platform distinction:** the Java application and `.stsource` source-archive workflow are cross-platform. The native Count + TIFF codec is currently qualified on Windows. Linux and macOS packages do not provide the native Count + TIFF backend.

## Quick start

### Desktop application

See [Java Desktop installation, packaging and usage](java-desktop/README.md). Platform packages include a matching Java runtime; users do not need to install Java or configure `JAVA_HOME`.

### Python integrated workflow (Windows)

Requirements: 64-bit CPython 3.12 and the pinned dependencies.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-windows-tested.txt
python fetch_count_runtime.py
```

Build or stage the qualified native runtime as described in [Native runtime build](#native-runtime-build), then run:

```powershell
python stpyramid.py encode --image slide.tif --counts spots.h5ad --metadata metadata.json --runtime-dir runtime-windows --output runs/archive
python stpyramid.py inspect runs/archive
python stpyramid.py verify runs/archive --runtime-dir runtime-windows
python stpyramid.py restore runs/archive --runtime-dir runtime-windows --output runs/restored
```

Use a **new output path** for every run. Encoding writes intermediate files and a receipt to a sibling `archive.work` directory; only the verified archive is published at the requested output path.

### Query selected data

```powershell
# Read an image region; bbox is x0 y0 x1 y1 in level-0 coordinates
python stpyramid.py image runs/archive --runtime-dir runtime-windows --level 0 --bbox 0 0 64 64 --output runs/roi.npy

# Read selected Count rows and genes
python stpyramid.py counts runs/archive --rows 0 --genes 0 1 --output runs/counts.npz
```

## How it works

```text
TIFF / BigTIFF ──> image census ──> lossless image encoding ──┐
                                                              ├─> shared manifest
H5AD + metadata ─> schema check ─> Shared Count encoding ─────┘
                                                                    │
                                               full decode + integrity verification
                                                                    │
                                                         published archive
                                                                    │
                                         inspect / ROI read / Count read / restore
```

The two encoders operate independently. Integration binds their source identities, image-level geometry and spatial coordinate mapping; it does not merge their bitstreams.

See [Architecture and data flow](docs/ARCHITECTURE.md) for component boundaries and the archive layout.

## Input and verification scope

- **Image:** tiled, contiguous RGB8 TIFF or BigTIFF with 3–16 stored levels. The two finest transitions require floor-sized 2× geometry. Supported tile compression: uncompressed, LZW or Deflate.
- **Count:** H5AD with nonnegative integer-valued counts and `obsm/spatial` coordinates in level-0 pixel coordinates. Qualified matrix bound: 20 million cells.
- **Metadata:** one JSON object, retained as an archive attachment.
- **Verification:** checks decoded native RGB values, retained image metadata, supported H5AD contents and types, and source/component identities. Restored TIFF/H5AD container bytes may differ; scientific content is what is verified.

## Native runtime build

On Windows, use Visual Studio 2022 C++ tools, CMake and Ninja in an x64 Developer PowerShell:

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5
cmake --build build --parallel 2 --target slide_jxl089 slide_jxl089_shared slide_jxl091 slide_jxl091_shared slide_hf1 slide_hf1_shared
python stage_runtime.py --bin build/bin --output runtime-windows
python tests/smoke.py --runtime-dir runtime-windows --output runs/source-build-smoke
```

The smoke test generates its own inputs. Use the same qualified runtime for encoding and verification. Historical archives may bind older native runtime hashes and require their matching runtime.

## Repository map

| Path | Purpose |
|---|---|
| `stpyramid.py` | Python command-line entry |
| `codec/integrated_encode.py` | Integrated Count + image encoding |
| `codec/container_compare.py` | Archive assembly, reader, verification and restore |
| `codec/stbaseline/` | Count and image codec components |
| `native/` | Native C++ encoder sources and adapters |
| `java-desktop/` | Java desktop application, launchers and package builders |
| `tests/` | Reproducible smoke tests |
| `results/` | Aggregate results, methods and evidence records |
| `upstream/libjxl/` | Vendored third-party libjxl source; see its retained notices |

Research scripts and historical implementations remain in the repository for reproducibility. They are not all part of the recommended user entry path.

## Documentation

- [Java Desktop guide](java-desktop/README.md)
- [Architecture and data flow](docs/ARCHITECTURE.md)
- [Results and reproducibility](results/README.md)
- [Open-source scope](OPEN_SOURCE_SCOPE.md)
- [Third-party notices](THIRD_PARTY.md)

## Licensing and data

Original STPyramid source is released under the [BSD 3-Clause License](LICENSE). Third-party components retain their own licenses and notices. The Shared Count dependency is fetched from a pinned revision because its upstream original-code license remains pending; see [THIRD_PARTY.md](THIRD_PARTY.md).

This source repository does not include study images, H5AD datasets, complete archive outputs or the binary Windows runtime. Do not commit patient data, private datasets, generated archives, runtime binaries or credentials.
