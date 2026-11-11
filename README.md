# STPyramid Archive

Research implementation for archiving an RGB8 whole-slide TIFF pyramid and its spatial-transcriptomics H5AD in one lossless directory package. The image encoder selects complete records from direct JPEG XL e9 and the 089/091/HF1 native-pyramid paths. The Count encoder uses the pinned Shared_local backend. This release wrapper was qualified on Windows with a synthetic end-to-end test and a read-only replay of the historical NCBI218 integrated archive.

## Inputs and recovery scope

- A supported tiled, contiguous RGB8 TIFF or BigTIFF with 3–16 stored levels. The two finest transitions require floor-sized 2× geometry, and this reader accepts uncompressed, LZW or Deflate TIFF tiles.
- An H5AD containing nonnegative integer-valued Count data and `obsm/spatial` in level-0 pixel coordinates. The qualified Count bound is 20 million matrix cells.
- A JSON metadata object to preserve as an attachment.
- Verification covers all decoded native RGB values, retained image metadata and supported H5AD contents/types. TIFF and H5AD container bytes can change on restoration.

## Windows quick start

Use 64-bit CPython 3.12. The local Windows delivery includes `runtime-windows`. For a source checkout, stage runtime binaries from a local CMake/Ninja build as shown below.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-windows-tested.txt
python fetch_count_runtime.py
python tests/smoke.py --runtime-dir .\runtime-windows --output .\runs\smoke
```

```powershell
python stpyramid.py encode --image slide.tif --counts spots.h5ad --metadata metadata.json --runtime-dir runtime-windows --output runs/archive
python stpyramid.py inspect runs/archive
python stpyramid.py verify runs/archive --runtime-dir runtime-windows
python stpyramid.py restore runs/archive --runtime-dir runtime-windows --output runs/restored
python stpyramid.py image runs/archive --runtime-dir runtime-windows --level 0 --bbox 0 0 64 64 --output runs/roi.npy
python stpyramid.py counts runs/archive --rows 0 --genes 0 1 --output runs/counts.npz
```

Use fresh outputs. Encoding keeps a sibling `archive.work` directory with intermediate streams and a verification receipt. Only the final archive directory enters the data-package ledger. The backend runs on one Windows logical CPU at low priority. The timeout is checked between stages and bounds native encoder subprocesses. This CLI does not provide the historical batch queue's OS process-tree memory supervisor.

## Build the native Windows runtime

With Visual Studio 2022 C++ tools, CMake and Ninja installed, run the following in an x64 Developer PowerShell. A clean Windows source build and the resulting end-to-end smoke passed with this toolchain. Use the resulting runtime for both encoding and verification. Existing archives bind their native runtime hashes and may require the matching historical binaries.

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5
cmake --build build --parallel 2 --target slide_jxl089 slide_jxl089_shared slide_jxl091 slide_jxl091_shared slide_hf1 slide_hf1_shared
python stage_runtime.py --bin build/bin --output runtime-windows
python tests/smoke.py --runtime-dir runtime-windows --output runs/source-build-smoke
```

The integrated CLI is currently scoped to Windows. The native source contains a macOS platform layer. The integrated Count+WSI workflow has not been qualified on macOS or Linux.

## Evidence and licensing

- [Results and reproducibility notes](results/README.md)
- [Numeric evidence ledger](results/evidence.json)
- [Third-party notices and scope](THIRD_PARTY.md)

The source release contains no study images, Count matrices, H5AD files or historical data archives. The included smoke test generates all its inputs. The published Count dependency is installed from its fixed commit and validated against 19 SHA-256 pins. Original project code has no new project-wide license grant in this release candidate.
