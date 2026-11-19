# Architecture

## 1. Product boundary

STPyramid creates one archive for a spatial-transcriptomics sample while keeping two independently decodable data components:

- **Count component:** preserves the H5AD scientific structure and stores integer expression values in independently indexed frames.
- **Image component:** preserves the native RGB image pyramid using lossless encoded records.
- **Archive layer:** binds both components to the same source identity, image-level geometry and level-0 coordinate system.

The archive is an integration and access layer, not a shared entropy model. Count and image streams keep their own codecs and decoding boundaries.

## 2. Encode flow

```text
                         INPUTS
              ┌────────────┴────────────┐
              │                         │
         TIFF / BigTIFF                 H5AD + JSON
              │                         │
        image census                schema preflight
              │                         │
       native level catalog          Count matrix
              │                         │
     image candidate encoding      Shared Count encoding
              │                         │
       image component               Count component
              └────────────┬────────────┘
                           │
                   identity / geometry
                        binding
                           │
                  integrated manifest
                           │
              full verification before publish
                           │
                    archive directory
```

The integrated Python path is:

1. `stpyramid.py` parses commands and delegates to the codec.
2. `codec/integrated_encode.py` validates inputs and runtime, creates the image census, encodes Count and image components, and assembles the candidate.
3. `codec/stbaseline/grouped_count_encode.py` writes the Shared 2.0 Count representation.
4. `codec/archive_image_encode.py` generates maximum-compression image candidates; the fused image workflow has its own entry.
5. `codec/container_compare.py` binds verified components, validates identities and exposes archive read, verify and restore operations.
6. The candidate is published only after full verification succeeds.

## 3. Read and restore flow

- **Inspect:** read the manifest and archive ledger.
- **Verify:** decode and check the supported scientific content and image pixels.
- **Image ROI:** map a level-0 bounding box to the requested pyramid level and read the covering pixels.
- **Count query:** retrieve selected spot rows and gene columns.
- **Restore:** reconstruct native TIFF levels and restore Count values into the H5AD structure.

Count queries do not need to construct an image decoder. Full verification is explicit and should not be mixed into selective-query timing.

## 4. Main code boundaries

| Component | Responsibility |
|---|---|
| `stpyramid.py` | User-facing Python CLI |
| `codec/integrated_encode.py` | Integrated workflow orchestration |
| `codec/stbaseline/grouped_count_encode.py` | Shared 2.0 Count writer |
| `codec/stbaseline/grouped_count.py` | Shared 2.0 Count reader |
| `codec/archive_image_encode.py` | Image candidate generation |
| `codec/joint_next.py` | Native image archive reader/writer path |
| `codec/container_compare.py` | Integrated manifest, verification, ROI and restore |
| `java-desktop/` | Java desktop UI, task orchestration and platform packaging |
| `native/` | Native C++ codec implementations |

## 5. Platform and release boundaries

- Java desktop UI and `.stsource` source-archive operations are cross-platform.
- Native Count + TIFF processing is qualified on Windows with the matching native runtime.
- Linux/macOS package scripts create platform-matched Java packages; they must not be described as providing the Windows native codec.
- Native runtime files are staged locally and are not committed to the source repository.
- The Shared Count dependency is fetched at a pinned revision and hash-checked; its upstream licensing status is documented separately.

## 6. Archive invariants

1. Count and image components must refer to the same source WSI identity.
2. Native image level identities and geometry must agree across component manifests.
3. Spatial coordinates are interpreted in level-0 pixel coordinates.
4. Every archive member is checked against its recorded size and SHA-256 digest.
5. The final archive is not published until verification succeeds.
6. Outputs must be fresh paths, separate from immutable inputs.
