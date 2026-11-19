# Quick start

This guide is for someone opening STPyramid for the first time. Choose the desktop route unless you specifically need the Python CLI.

## Option A — use the desktop app

1. Download the package for your operating system from the verified [STPyramid v0.2.5 release](https://github.com/zt97152023-ai/stpyramid-archive/releases/tag/v0.2.5):
   - [Windows x64 ZIP](https://github.com/zt97152023-ai/stpyramid-archive/releases/download/v0.2.5/STPyramid-Java-0.2.4-Windows-x64.zip)
   - [Linux x64 TAR.GZ](https://github.com/zt97152023-ai/stpyramid-archive/releases/download/v0.2.5/STPyramid-Java-0.2.5-Linux-x64.tar.gz)
   - [macOS Apple silicon (arm64)](https://github.com/zt97152023-ai/stpyramid-archive/releases/download/v0.2.5/STPyramid-Java-0.2.5-macOS-arm64.tar.gz)
   - [macOS Intel (x64)](https://github.com/zt97152023-ai/stpyramid-archive/releases/download/v0.2.5/STPyramid-Java-0.2.5-macOS-x64.tar.gz)
2. Extract the complete package to a writable folder. Keep the launcher, JAR and bundled `jre/` directory together.
3. Launch:
   - Windows: `Launch-STPyramidJava.cmd`
   - Linux: `Launch-STPyramidJava.sh`
   - macOS: `Launch-STPyramidJava.command`
4. Choose the operation that matches your data:
   - **Spatial archive**: package supported spatial-transcriptomics source files while preserving their original files and paths.
   - **Count + TIFF archive**: use the integrated native workflow where the installed package reports that the required backend is available.
5. Select the input and a new output location. Start the job and wait for its completion status.
6. Run **Verify** on the resulting archive. Restore to a separate, empty output folder when you need the original data back.

The release includes SHA-256 checksum files beside each package. Verify the download before launching. The Windows asset retains the `0.2.4` filename although it is distributed in release `v0.2.5`.

Read the [Java desktop guide](../java-desktop/README.md) for supported modes, platform capability limits, HEST batch workflows and packaging instructions.

> Platform note: the Java application and source-archive workflow are cross-platform. The native Count + TIFF workflow is qualified on Windows; a Linux/macOS package must not be assumed to provide that native backend.

## Option B — use the Python integrated CLI

The Python integrated Count + TIFF CLI is qualified on Windows and uses 64-bit CPython 3.12.

### 1. Prepare the environment

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-windows-tested.txt
python fetch_count_runtime.py
```

### 2. Check the installation

```powershell
python tests/smoke.py --runtime-dir .\runtime-windows --output .\runs\smoke
```

The smoke test creates synthetic inputs, encodes an archive, verifies it, restores it and checks exact recovery. It does not need a study dataset.

### 3. Encode, inspect, verify and restore

```powershell
python stpyramid.py encode --image slide.tif --counts spots.h5ad --metadata metadata.json --runtime-dir runtime-windows --output runs/sample.stpyramid
python stpyramid.py inspect runs/sample.stpyramid
python stpyramid.py verify runs/sample.stpyramid --runtime-dir runtime-windows
python stpyramid.py restore runs/sample.stpyramid --runtime-dir runtime-windows --output runs/restored
```

Use a new output path for each run. Do not place the output inside an input directory.

### 4. Read a region or a Count subset

```powershell
python stpyramid.py image runs/sample.stpyramid --runtime-dir runtime-windows --level 0 --bbox 0 0 64 64 --output runs/roi.npy
python stpyramid.py counts runs/sample.stpyramid --rows 0 --genes 0 1 --output runs/counts.npz
```

## What goes into an integrated archive?

| Input | Purpose |
|---|---|
| Tiled RGB8 TIFF / BigTIFF pyramid | Whole-slide image data |
| H5AD | Nonnegative integer Count matrix and spatial coordinates in L0 pixel coordinates |
| JSON object | Metadata attachment |

The archive stores Count and image as independently encoded components, then binds them through a shared sample identity and L0 spatial coordinate convention.

## Need to understand or modify the code?

- [Architecture](ARCHITECTURE.md) — component boundaries and request flow.
- [Code map](CODEMAP.md) — exact entry points and implementation files.
- [Advanced workflows](ADVANCED_WORKFLOWS.md) — compatibility, format constraints and experimental commands.
