# STPyramid Archive

## Choose a version

| Version | Fixed tag | Scope |
|---|---|---|
| Desktop v0.1.0 | [desktop-v0.1.0](https://github.com/zt97152023-ai/stpyramid-archive/releases/tag/desktop-v0.1.0) | Windows app with English and Chinese interfaces for compression, verification and restoration |
| Maximum compression | [research-20261111](https://github.com/zt97152023-ai/stpyramid-archive/tree/research-20261111) | Original HF+L1 candidate search and Shared Count integration |
| Fast fused v1 | [fast-fused-v1-20261113](https://github.com/zt97152023-ai/stpyramid-archive/tree/fast-fused-v1-20261113) | One selected encoder per tile for the native RGB image pyramid |

This branch adds the fused image encoder, using the original container and a compatible decoder. The [fast Windows quick start](results/fast-fused-v1/README.md) covers encoding, verification, ROI reads and TIFF restoration. [Methods](results/fast-fused-v1/METHODS.md) and [ten-slide results](results/fast-fused-v1/RESULTS.md) report the image component. The integrated commands below still use the maximum-compression image path and Shared Count.

## Maximum-compression integrated workflow

Research implementation for archiving an RGB8 whole-slide TIFF pyramid and its spatial-transcriptomics H5AD in one lossless directory package. The image encoder selects complete records from direct JPEG XL e9 and the 089/091/HF1 native-pyramid paths. The Count encoder uses the pinned Shared_local backend. This release wrapper was qualified on Windows with a synthetic end-to-end test and a read-only replay of the historical NCBI218 integrated archive.

## Inputs and recovery scope

- A supported tiled, contiguous RGB8 TIFF or BigTIFF with 3–16 stored levels. The two finest transitions require floor-sized 2× geometry, and this reader accepts uncompressed, LZW or Deflate TIFF tiles.
- An H5AD containing nonnegative integer-valued Count data and `obsm/spatial` in level-0 pixel coordinates. The qualified Count bound is 20 million matrix cells.
- A JSON metadata object to preserve as an attachment.
- Verification covers all decoded native RGB values, retained image metadata and supported H5AD contents/types. TIFF and H5AD container bytes can change on restoration.

## Windows quick start

The [desktop app](desktop/README.md) runs the integrated Count and image workflow or the fast image-only workflow from a queue. Use `python desktop_app.py` from a qualified Windows environment, or build the portable app with `python desktop/build_portable.py --output runs/STPyramid-Desktop --zip` after installing the pinned Python dependencies. Open `STPyramid.exe` from the extracted directory. The app downloads and checks the pinned Shared_local source on the first Count job.

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

## Integrated code path

1. `stpyramid.py` handles the CLI. `encode_archive` validates the inputs and runtimes, scans the stored image levels, stages each component and publishes the directory once both modalities pass verification. [CLI](stpyramid.py) · [Integrated encoder](codec/integrated_encode.py)
2. `write_count_component` stores the typed H5AD structure and spatial axes in `science.h5`. `write_count_store` groups spots in 1024-pixel cells in L0 coordinates, stores one sample-wide Shared_local model, and writes the graph, support and integer values for each block. The index maps blocks to their original spot rows and bank offsets. [Count component](codec/integrated_encode.py) · [Count bank](codec/stbaseline/shared_backend.py)
3. `encode_images` produces direct JPEG XL records and the 089, 091 and HF candidates. It prepares native parent references for L0 and L1, retains the smallest eligible records in the prescribed order, and writes both the direct baseline and the selected HF + L1 image component. [Image candidate generation](codec/archive_image_encode.py) · [Image archive](codec/joint_next.py)
4. `pack_integrated` checks the component source and level identities, copies the verified members and binds them in one manifest. `IntegratedArchive.region` selects spots in a half-open L0 box and maps the box to covering pixels at the requested image level. Full restore writes the native TIFF levels and reinserts the decoded Count values into the original H5AD structure. [Integrated container and reader](codec/container_compare.py)

## Build the native Windows runtime

With Visual Studio 2022 C++ tools, CMake and Ninja installed, run the following in an x64 Developer PowerShell. A clean Windows source build and the resulting end-to-end smoke passed with this toolchain. Use the resulting runtime for both encoding and verification. Existing archives bind their native runtime hashes and may require the matching historical binaries.

```powershell
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5
cmake --build build --parallel 2 --target slide_jxl089 slide_jxl089_shared slide_jxl091 slide_jxl091_shared slide_hf1 slide_hf1_shared
python stage_runtime.py --bin build/bin --output runtime-windows
python tests/smoke.py --runtime-dir runtime-windows --output runs/source-build-smoke
```

The integrated CLI is currently scoped to Windows. The native source contains a macOS platform layer. The integrated Count+WSI workflow has not been qualified on macOS or Linux.

## Experimental recursive TIFF encoder

`recursive_image_archive.py` encodes eligible native levels against the next original TIFF level. Coarsest levels, scale transitions without exact floor-sized 2× geometry, and periodic checkpoints use independent lossless JPEG XL e9. The parent index resolves each conditional tile to one coarser tile. Every level and the source metadata are verified before the archive directory is published.

After compiling the native `slide_jxl091` target from this branch, run the experiment with a fresh work directory and output path.

```powershell
python recursive_image_archive.py encode slide.tif runs/recursive-image --work runs/recursive-work --native-encoder build/bin/slide_jxl091.exe --preset fast --max-depth 3
python recursive_image_archive.py verify runs/recursive-image
python recursive_image_archive.py restore runs/recursive-image runs/recursive-restored
```

`--max-depth 15` permits the full eligible chain for TIFFs with up to 16 levels. `--preset regular` retains the fused encoder search settings. `--preset fast` uses a lower MA-tree sample fraction and non-optimal LZ77 parsing. Both presets consolidate conditional-tile verification into the final recursive archive verification. This experiment uses its own image format identifier and CLI.

## Evidence and licensing

- [Results and reproducibility notes](results/README.md)
- [Numeric evidence ledger](results/evidence.json)
- [Third-party notices and scope](THIRD_PARTY.md)

The source release contains no study images, Count matrices, H5AD files or historical data archives. The included smoke test generates all its inputs. The published Count dependency is installed from its fixed commit and validated against 19 SHA-256 pins. Original project code has no new project-wide license grant in this release candidate.
