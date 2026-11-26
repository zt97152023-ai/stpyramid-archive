# STPyramid Archive

## Latest development work

The [speed-first native archive study](results/fast-import-20261125/README.md)
reports Windows Count + TIFF import, independent verification, archive size,
and region reads. It includes the comparison with direct lossless JPEG XL
effort 3 and the 8-, 16-, and 24-worker scaling measurements. The Windows
package listed under Releases is a separately versioned distribution.

## Choose a version

| Version | Fixed tag | Scope |
|---|---|---|
| Desktop v0.1.0 | [desktop-v0.1.0](https://github.com/zt97152023-ai/stpyramid-archive/releases/tag/desktop-v0.1.0) | Windows app with English and Chinese interfaces for compression, verification and restoration |
| Maximum compression | [research-20261111](https://github.com/zt97152023-ai/stpyramid-archive/tree/research-20261111) | Original HF+L1 candidate search and Shared Count integration |
| Fast fused v1 | [fast-fused-v1-20261113](https://github.com/zt97152023-ai/stpyramid-archive/tree/fast-fused-v1-20261113) | One selected encoder per tile for the native RGB image pyramid |
| Java Desktop v0.2.4 | [java-desktop-v0.2.4](https://github.com/zt97152023-ai/stpyramid-archive/tree/java-desktop-v0.2.4) | No-configuration Java desktop, Windows native backend, Linux/macOS source-archive packages |
| Published v0.2.5 | [v0.2.5 release](https://github.com/zt97152023-ai/stpyramid-archive/releases/tag/v0.2.5) | Source release and platform packages. The tagged Windows integrated encoder writes historical Shared_local Count. |

## Open-source source release

The current source branch is `product/java-spatial-archive`. It contains the
Java desktop, the original codec source, cross-platform launchers and package
builders. The repository intentionally excludes study data, native binaries
and the external Count source whose upstream license is pending. See
[OPEN_SOURCE_SCOPE.md](OPEN_SOURCE_SCOPE.md), [LICENSE](LICENSE) and
[THIRD_PARTY.md](THIRD_PARTY.md).

Windows Java packages include a matching JRE. See the
[Java desktop build and packaging guide](java-desktop/README.md) for the
local offline and first-use Count dependency builds. Linux and macOS packages
provide byte-exact source archiving with a bundled JRE.

## Current native Count + TIFF modes

On Windows, the Java desktop defaults to **Fast fusion** for HEST Count +
TIFF, and also offers **Maximum compression**. Both modes in the current
source write Shared 2.0 Count (`Shared_2`) and read historical
`Shared_local` archives. The Java CLI selects Fast fusion with
`hest-fused`, `hest-batch-fused` or `paired-fused`.
The existing Java commands without the suffix and the Python
`stpyramid.py encode` command use maximum compression.

The [Fast-fused methods and ten-slide results](results/fast-fused-v1/RESULTS.md)
cover the image component. The [Shared 2.0 integration verification](results/shared2-integration.md)
covers six Count development inputs and one real Fast-fused Count + TIFF pair.

## Inputs and recovery scope

- A supported tiled, contiguous RGB8 TIFF or BigTIFF with 3–16 stored levels. The two finest transitions require floor-sized 2× geometry, and this reader accepts uncompressed, LZW or Deflate TIFF tiles.
- An H5AD containing nonnegative integer-valued Count data and `obsm/spatial` in level-0 pixel coordinates. The qualified Count bound is 20 million matrix cells.
- A JSON metadata object to preserve as an attachment.
- Verification covers all decoded native RGB values, retained image metadata and supported H5AD contents/types. TIFF and H5AD container bytes can change on restoration.

## Windows quick start

Use the [Java desktop guide](java-desktop/README.md) for the integrated Fast
fusion workflow. The [Python desktop guide](desktop/README.md) covers its
maximum-compression Count + TIFF and fast image-only workflows.

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

Use fresh outputs. Encoding keeps a sibling `archive.work` directory with intermediate streams and an encode receipt. Only the final archive directory enters the data-package ledger. The backend runs on one Windows logical CPU at low priority. The timeout is checked between stages and bounds native encoder subprocesses. This CLI does not provide the historical batch queue's OS process-tree memory supervisor.

## Integrated code path

1. `stpyramid.py` handles the CLI. `encode_archive` validates the inputs and runtimes, scans the native image levels, and publishes the assembled directory. The encode receipt records full verification as `not_run`. Use `verify` to check archive members and read back both modalities. [CLI](stpyramid.py) · [Integrated encoder](codec/integrated_encode.py)
2. Shared 2.0 stores the typed H5AD structure in `science.h5` and indexes independent Count frames by 1024-pixel L0 spatial block and gene group. Panels with at most 1,024 genes use groups of 64 and cluster same-group frames within runs of 32 blocks. Larger panels use groups of 256 in spatial block order. For blocks with at most 12 spots, the encoder compares the original position stream against the nonzero cardinality and subset rank stream. It keeps the occupancy model only when it lowers the complete Count component size, including the model, index and manifest. [Count component](codec/integrated_encode.py) · [Writer](codec/stbaseline/grouped_count_encode.py) · [Reader](codec/stbaseline/grouped_count.py)
3. Fast fusion submits L0/L1 batches as soon as their inputs are ready. A native encoder with census input support reads the shared RGB spool directly and checks the tile hashes before encoding. An older encoder uses staged RGB and parent-reference files. Both paths keep immediate native tile reconstruction checks. Independent tiles in stored levels L2 and coarser use lossless JPEG XL effort 3. Maximum compression evaluates direct JPEG XL and 089/091/HF candidates before writing the HF + L1 image component. [Fast fusion](pyramid_image_archive.py) · [Maximum-compression candidates](codec/archive_image_encode.py) · [Image archive](codec/joint_next.py)
4. `pack_integrated` checks the component source and level identities, copies the verified members and binds them in one manifest. `IntegratedArchive.region` selects spots in a half-open L0 box and maps the box to covering pixels at the requested image level. Full restore writes the native TIFF levels and reinserts the decoded Count values into the original H5AD structure. [Integrated container and reader](codec/container_compare.py)

For repeated Python ROI reads, keep the same qualified image reader open.
It keeps up to 8 MiB of decoded RGB tiles per reader and asks the caller
to reopen an archive whose member files have changed. Small regions spanning
two to eight tiles can decode independent tiles in parallel once their exact
parents are ready. This path uses up to four workers and stages at most 8 MiB
of compressed tile payloads per request. Larger regions and deeper dependency
chains use the serial region reader. Set `roi_workers=1` for serial timing and
`roi_cache=False` to disable the session pixel cache.

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

The source release contains no study images, Count matrices, H5AD files or historical data archives. The included smoke test generates all its inputs. The published Count dependency is installed from its fixed commit and validated against 19 SHA-256 pins. The license for original STPyramid source is recorded in [LICENSE](LICENSE).
