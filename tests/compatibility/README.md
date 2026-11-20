# Historical integrated archive compatibility

This harness verifies a historical integrated archive against its original TIFF and H5AD. It is designed for locally held research archives; no study data or binary archive fixtures are committed to Git.

## Required inputs

- The historical integrated archive directory.
- The exact original multi-page TIFF/BigTIFF used to create it.
- The exact original H5AD used to create it.
- The native runtime directory matching the archive's declared runtime pins.
- A new, empty restore output path.

For archives with a metadata attachment, also provide the exact original JSON file.

## Run

From a qualified Windows Python 3.12 environment with the required native runtime and pinned Shared Count source installed:

```powershell
python tests/compatibility/verify_integrated.py `
  --archive G:\\Archive\\historical.stpyramid `
  --original-tiff G:\\Original\\slide.tif `
  --original-h5ad G:\\Original\\counts.h5ad `
  --metadata G:\\Original\\metadata.json `
  --runtime-dir G:\\Runtimes\\runtime-windows `
  --output G:\\Validation\\historical-restored
```

The verifier performs a full STPyramid archive verification, restores the archive to a new directory, compares every decoded TIFF page pixel-for-pixel, compares the semantic H5AD digest, and checks the retained H5AD type/attribute contract. If `--metadata` is supplied, the restored metadata attachment must also be byte-identical.

The JSON report records the Count codec kind, archive byte total, TIFF level shapes and exact checks.

## Compatibility matrix to populate

| Archive family | Required runtime | Evidence status |
|---|---|---|
| Current Shared 2.0 + current image format | Current qualified runtime | Pending a locally available source/archive pair |
| Historical Shared_local + historical image format | Runtime matching the archive's native pins | Pending a locally available source/archive pair |
| Historical image-only archive | Runtime matching the image manifest pins | Not covered by the integrated verifier yet |

Do not mark a row as passed until the exact archive, original source files and matching runtime have been verified. A successful synthetic smoke test is not historical compatibility evidence.
