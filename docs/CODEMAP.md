# Code map

This is the shortest route from a user command to the implementation. Start here before searching the repository.

## The three core responsibilities

| Responsibility | Main implementation | What it owns |
|---|---|---|
| Count codec | [ `codec/integrated_encode.py` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/codec/integrated_encode.py), [ `codec/stbaseline/grouped_count_encode.py` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/codec/stbaseline/grouped_count_encode.py), [ `codec/stbaseline/grouped_count.py` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/codec/stbaseline/grouped_count.py) | H5AD validation, grouped Count stream writing, frame indexing and Count decoding |
| TIFF/image codec | [ `codec/archive_image_encode.py` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/codec/archive_image_encode.py), [ `codec/joint_next.py` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/codec/joint_next.py), [ `codec/native_adapter.py` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/codec/native_adapter.py), [ `native/` ](https://github.com/zt97152023-ai/stpyramid-archive/tree/product/java-spatial-archive/native/) | Native pyramid scanning, image candidate generation/selection, image records and native encode/decode |
| Unified archive | [ `codec/integrated_encode.py` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/codec/integrated_encode.py), [ `codec/container_compare.py` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/codec/container_compare.py) | Source identity, component assembly, manifest, verification, ROI/Count subset access and restoration |

The Count and image codecs remain independent. The archive layer joins their verified components and coordinate metadata; it does not combine their probability models.

## Start from the command

The Python CLI is [ `stpyramid.py` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/stpyramid.py).

| User command | Entry point | Implementation |
|---|---|---|
| `encode` | `stpyramid.py::encode` | `codec/integrated_encode.py::encode_archive` |
| `inspect` | `stpyramid.py::read_archive` | `codec/container_compare.py::IntegratedArchive.ledger` |
| `verify` | `stpyramid.py::read_archive` | `codec/container_compare.py::IntegratedArchive.verify` / `verify_files` |
| `restore` | `stpyramid.py::read_archive` | `codec/container_compare.py::IntegratedArchive.restore` |
| `image` | `stpyramid.py::read_archive` | `IntegratedArchive.image` |
| `counts` | `stpyramid.py::read_archive` | `IntegratedArchive.counts` |

## Encode path, in execution order

```text
stpyramid.py
  └── encode()
       └── integrated_encode.encode_archive()
            ├── validate paths, metadata and native runtime
            ├── scan TIFF pyramid
            ├── write_count_component()
            │    └── grouped_count_encode.write_grouped_candidates()
            ├── archive_image_encode.encode_images()
            │    ├── direct JPEG XL candidates
            │    ├── anchored / conditional native candidates
            │    └── retain selected records
            ├── container_compare.pack_integrated()
            ├── IntegratedArchive.verify()
            └── publish verified archive
```

Important implementation details:

- `encode_archive` orchestrates the complete job and publishes only after verification.
- `write_count_component` creates the typed H5AD skeleton, binds source identities and chooses a complete Count variant using total component bytes.
- `write_grouped_candidates` writes the grouped Count candidates.
- `encode_images` builds and selects image records; native implementations live under [ `native/` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/native/).
- `pack_integrated` binds the independently encoded components into the archive.
- `IntegratedArchive` owns archive inspection, verification, selective reads and restore.

## Desktop applications

There are two clients in this source tree. They are separate front ends and should not be mistaken for two codec implementations.

### Java desktop

- `java-desktop/src/stpyramid/Main.java` — Java command-line entry.
- `java-desktop/src/stpyramid/Desktop.java` — Swing desktop entry.
- `java-desktop/src/stpyramid/NativeEngine.java` — native backend integration.
- `java-desktop/src/stpyramid/SourceArchive.java` and `Sources.java` — platform-independent source archive workflow.
- `java-desktop/package*.sh`, `package.ps1` — platform packaging.

### Python desktop

- `desktop_app.py` — application window.
- `desktop_worker.py` — background job execution.
- `desktop_i18n.py` — interface text.
- `desktop/` — portable packaging and user guides.


## Other top-level scripts: what belongs where?

Not every root-level Python file is part of the normal integrated application. Some are retained compatibility entry points or research/benchmark harnesses. Do not import them into the desktop application or treat them as supported end-user commands without checking their scope.

| File | Classification | Purpose / caution |
|---|---|---|
| `fetch_count_runtime.py` | Runtime preparation | Fetches the pinned external Count source and validates exact sizes and SHA-256 hashes |
| `stage_runtime.py` | Build utility | Stages ABI-checked native Windows binaries from a local CMake build |
| `desktop_app.py`, `desktop_worker.py`, `desktop_i18n.py` | Python desktop client | Earlier Windows queue-based GUI and worker process |
| `pyramid_image_archive.py` | Image workflow | Native image-pyramid archive entry point used by the fused image path |
| `recursive_image_archive.py` | Experimental format | Separate recursive TIFF archive format and CLI; not the integrated Count + image format |
| `native_tile_encoder.py` | Research/benchmark support | Shared low-level helpers for image-only research scripts |
| `encode_image.py` | Historical experiment | L0-only candidate comparisons; not the primary integrated image encoder |
| `benchmark_read.py` | Benchmark support | ROI timing and image byte accounting |
| `cohort.py`, `select_cohort.py`, `COHORT.json` | Fixed-cohort research | Cohort selection and paired benchmark orchestration; not end-user workflow |
| `build_full.py` | Compatibility wrapper | Retained alias into `pyramid_image_archive.py`; avoid adding new logic here |

## Tests and evidence

| Need | Start here |
|---|---|
| Synthetic end-to-end test | [ `tests/smoke.py` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/tests/smoke.py) |
| Cohort selection and metadata | `cohort.py`, `select_cohort.py`, `COHORT.json` |
| Java HEST benchmark | `java-desktop/benchmark_hest.py` |
| Aggregate evidence ledger | `results/evidence.json` |
| Reproducibility notes | `results/README.md` |
| Historical compatibility evidence | `results/integrated-refactor-20261116/` |

## Safe change guide

| If you want to change… | First inspect… | Then validate… |
|---|---|---|
| CLI arguments or output format | [ `stpyramid.py` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/stpyramid.py) | CLI smoke test |
| Count stream layout | `grouped_count_encode.py`, `grouped_count.py` | Exact Count recovery and old archive reads |
| TIFF candidate selection | `archive_image_encode.py`, `joint_next.py` | Native RGB equality, ROI reads and restore |
| Archive members or manifest | `container_compare.py` | Full verification and historical archive compatibility |
| Native codec | [ `native/` ](https://github.com/zt97152023-ai/stpyramid-archive/blob/product/java-spatial-archive/native/), `CMakeLists.txt` | Native build, codec tests and full archive smoke |
| Java interface or packaging | `java-desktop/src/`, `java-desktop/package*` | Package launch and capability checks |

Do not change codec identifiers, manifest members, runtime pins or archive layout as a cleanup-only refactor. Treat each as a format/compatibility change and test representative historical archives with their declared runtime versions.
