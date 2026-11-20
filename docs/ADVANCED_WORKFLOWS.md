# Advanced workflows and format notes

This page keeps less frequently needed implementation details out of the main README. For the normal user path, start with the root [README](../README.md).

## Input and recovery scope

The integrated Python workflow accepts:

- A supported tiled, contiguous RGB8 TIFF or BigTIFF with 3–16 stored levels.
- The two finest pyramid transitions must follow floor-sized 2× geometry.
- TIFF tiles may be uncompressed, LZW or Deflate.
- An H5AD containing nonnegative integer-valued Count data and `obsm/spatial` in level-0 (L0) pixel coordinates.
- The qualified Count bound is 20 million matrix cells.
- An optional JSON metadata object is preserved as an attachment.

Verification covers decoded native RGB values, retained image metadata and supported H5AD contents and types. TIFF and H5AD container bytes may differ after restoration; scientific values and supported structure are the recovery target.

## Archive compatibility

The integrated writer in the current source branch uses Shared 2.0 for new Count archives. The reader also supports historical Shared_local archives. Use a reader/runtime combination compatible with the archive's declared codec and runtime identity. Existing archives can bind historical native runtime hashes and may require their matching historical binaries.

Do not infer backward compatibility from a successful new synthetic smoke test. Verify representative historical archives before changing archive members, codec identifiers, manifests or runtime dependencies.

## Runtime behavior

- The integrated Python CLI is qualified on Windows.
- The backend runs with bounded native subprocess execution. The timeout is checked between stages and bounds native encoder subprocesses.
- The CLI does not provide the historical batch queue's operating-system process-tree memory supervisor.
- The source repository excludes native runtime binaries. Build and stage a local runtime using the commands in the root README.

## Experimental recursive TIFF encoder

`recursive_image_archive.py` is a separate experimental image format and CLI. It encodes eligible native levels against the next original TIFF level. Coarsest levels, scale transitions without exact floor-sized 2× geometry, and periodic checkpoints use independent lossless JPEG XL e9. A parent index resolves each conditional tile to one coarser tile. Every level and source metadata are verified before the archive directory is published.

After building the **Context Codec** through the descriptive `stpyramid_context` target, set `PATH_TO_CONTEXT_CODEC_EXECUTABLE` to the matching native executable from your staged runtime. This experimental recursive image workflow is separate from the integrated Count + TIFF archive. Run with a fresh work directory and output path:

```powershell
$contextEncoder = Read-Host "Path to the Context Codec native executable"
python recursive_image_archive.py encode slide.tif runs/recursive-image --work runs/recursive-work --native-encoder $contextEncoder --preset fast --max-depth 3
python recursive_image_archive.py verify runs/recursive-image
python recursive_image_archive.py restore runs/recursive-image runs/recursive-restored
```

- `--max-depth 15` permits the full eligible chain for TIFFs with up to 16 levels.
- `--preset regular` retains the fused encoder search settings.
- `--preset fast` uses a lower MA-tree sample fraction and non-optimal LZ77 parsing.
- Both presets consolidate conditional-tile verification into final recursive archive verification.

This experimental format has its own format identifier and CLI. It should not be confused with the integrated Count + image archive workflow.
