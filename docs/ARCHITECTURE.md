# STPyramid architecture

This note describes the current source layout and the boundaries between the two data codecs and the unified archive. It is intended as a navigation guide, not a replacement for implementation-level documentation.

## 1. System boundary

STPyramid accepts three inputs:

| Input | Role | Coordinate / data contract |
|---|---|---|
| TIFF / BigTIFF | Native RGB image pyramid | Tiled RGB8 levels; spatial queries are expressed in level-0 (L0) pixels |
| H5AD | Spatial Count matrix and scientific structure | Nonnegative integer-valued counts; `obsm/spatial` uses L0 pixel coordinates |
| JSON | User-supplied metadata attachment | Preserved as an archive member |

The Count and image components are encoded independently. The archive layer binds them to the same sample, coordinate frame and metadata.

## 2. Image codec modules

The integrated image path combines three mechanism-based modules: **Anchor Codec** (native-parent anchored correction), **Context Codec** (known-parent-conditioned detail coding), and **HF-Detail Codec** (dense high-frequency magnitude/sign representation). The archive selects routes per tile; these names do not imply three independent whole-slide formats. See [codec naming](CODEC_NAMING.md).

## 3. Main components

| Component | Main source | Responsibility |
|---|---|---|
| CLI | `stpyramid.py` | User-facing encode, inspect, verify, restore and read commands |
| Integrated orchestration | `codec/integrated_encode.py` | Validate inputs, coordinate component encoding, package and publish |
| Count writer | `codec/stbaseline/grouped_count_encode.py` | Build the grouped Count component and its indexes |
| Count reader | `codec/stbaseline/grouped_count.py` | Decode indexed Count frames and subsets |
| H5AD structure | `codec/h5_types.py` | Preserve and validate typed H5AD structure |
| Image candidate generation | `codec/archive_image_encode.py` | Generate eligible image records and select candidates |
| Image archive | `codec/joint_next.py` | Store selected image records and their references |
| Container / region reader | `codec/container_compare.py` | Bind members, inspect archives and serve spatial reads |
| Native codecs | `native/`, `CMakeLists.txt` | Native image encode/decode implementations |
| Java client | `java-desktop/src/` | Desktop workflow, validation and archive operations |
| Runtime capability check | `java-desktop/src/stpyramid/PlatformSupport.java` | Checks expected native bundle files before advertising or launching native compression |
| Python desktop client | `desktop_app.py`, `desktop_worker.py` | Queue-based Python desktop workflow |

## 3. Integrated request flow

```text
CLI / Desktop
     |
     v
Input validation and runtime checks
     |
     +-----------------------------+
     |                             |
     v                             v
H5AD / Count                    TIFF pyramid
     |                             |
Canonical structure,             Scan native levels,
Count grouping and frames        generate image candidates
     |                             |
     v                             v
Count component                 Image component
     |                             |
     +--------------+--------------+
                    |
                    v
          Unified container assembly
          - sample and source identity
          - L0 coordinate convention
          - component and member manifest
          - metadata attachment
                    |
                    v
             Full verification
                    |
          +---------+---------+
          |                   |
          v                   v
      Publish archive     Reject / retain work
          |
          +--> Inspect
          +--> Image ROI read
          +--> Count subset read
          +--> Full restore
```

The exact implementation entry points are `encode_archive` in `codec/integrated_encode.py`, the Count writer/reader under `codec/stbaseline/`, image candidate generation in `codec/archive_image_encode.py`, and container access in `codec/container_compare.py`. Consult those files for current function signatures and format details.

## 4. Archive and recovery invariants

1. **Independent codec boundaries.** Count and image data have separate encoders, decoders and component-level verification.
2. **Shared spatial reference.** The archive records the sample relationship and L0 coordinate convention used to connect Count spots and image pixels.
3. **Verify before publish.** A completed archive is published only after the required component and archive checks pass.
4. **Exact scientific recovery.** Verification checks decoded native RGB values and supported H5AD values, types and structure. Restored TIFF/H5AD container bytes are not required to be byte-identical to the original containers.
5. **Fresh output paths.** Each encode should use a new output path; intermediate work is kept separately from the published archive.

## 5. Client and platform boundary

There are two desktop clients in the repository; they should not be confused:

- **Java desktop** (`java-desktop/`): current cross-platform application shell. Its source-archive workflow is platform independent. Native Count + TIFF support is qualified on Windows; Linux/macOS packages must report unavailable native capabilities rather than imply full native parity.
- **Python desktop** (`desktop_app.py`, `desktop/`): earlier queue-based client with its own packaging and quick-start guide.

The Python integrated CLI and native codec build are also currently qualified on Windows. Platform packaging does not by itself establish that every native codec is supported on that platform.

## 6. Where to make changes

- Change command syntax or user-facing CLI behavior: start at `stpyramid.py`.
- Change the integrated encode/publish sequence: inspect `codec/integrated_encode.py`.
- Change Count coding or indexed reads: inspect `codec/stbaseline/` and the Count integration in `codec/integrated_encode.py`.
- Change image candidate generation or image archive layout: inspect `codec/archive_image_encode.py` and `codec/joint_next.py`.
- Change archive members, manifest or region reads: inspect `codec/container_compare.py`.
- Change native image implementations: inspect `native/` and `CMakeLists.txt`.
- Change Java user experience or packaging: inspect `java-desktop/src/` and the scripts in `java-desktop/`.

## 7. Validation

Run the synthetic end-to-end smoke test after changing codec or archive behavior:

```powershell
python tests/smoke.py --runtime-dir .\\runtime-windows --output .\\runs\\smoke
```

For format or compatibility changes, also verify representative historical archives with the runtime versions they declare. A synthetic smoke test alone does not establish backward compatibility.
