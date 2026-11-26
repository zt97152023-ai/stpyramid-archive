# Speed-first lossless Count and native TIFF archiving

## Objective

We measured the time from opening the source files to sealing a joint archive, and measured verification and region reads separately. A useful configuration should preserve all native TIFF levels, supported H5AD contents and types, and the original JSON. Archive size and latency for a newly visited region determine whether the faster import is worthwhile.

## Implementation

- Shared 2.0 stores Count and scientific metadata. The Fast-fused image path encodes L0/L1 with native parent references and uses independent lossless JPEG XL effort 3 for coarser levels.
- The import census retains source identities, stored-level geometry, native RGB hashes and a canonical RGB spool. The product path omits extra research diagnostics. Per-level tile preparation reuses a spool handle while checking individual tile hashes.
- Import publishes the assembled package and marks full verification as `not_run`. The separate `verify` operation checks members and reads back the stored modalities. Restore remains independently available.
- The optional `STPYRAMID_FUSED_WORKERS` budget extends to 24. Above eight, the worker selector prefers one logical thread per available physical core when Windows reports its topology. The default remains eight. A Python reader can retain up to 8 MiB of decoded RGB across region requests in the same session.

The whole-slide fast-preset experiment uses the experimental 091 executable with its faster search mode. This executable is distinct from the one bundled in the v0.2.6 Windows package. Experimental figures below are development measurements.

## Whole-slide comparison with direct JPEG XL effort 3

On `TENX139`, direct lossless JPEG XL effort 3 provided faster import and viewport reads. The conditional path used 22.5% fewer complete-package bytes. Both methods used the same frozen TIFF, H5AD and JSON, the same Windows machine and volume, 24 physical-core workers for import, and 8 scan workers for the separate full verification. Shared 2.0 and all Count members were identical.

| Measure | Shared_2 + direct JPEG XL e3 | Shared_2 + Fast fusion, fast preset |
|---|---:|---:|
| Image encoding and assembly | 29.85 s | 201.27 s |
| Publish complete archive | **106.86 s** | 283.74 s |
| Independent full verification | 19.74 s | 143.21 s |
| Import plus full verification | **126.59 s** | 426.94 s |
| Complete package | 1,189,752,797 B | **922,157,728 B** |
| Image component | 1,186,101,457 B | **918,506,381 B** |
| L0 ROI latency, median of 24 new windows | **21.21 ms** | 152.20 ms |

Each successful archive restored all 9 native RGB levels and the typed H5AD and original JSON. We independently rehashed all 24 package members across both archives. We measured 24 distinct 256 × 256 L0 windows in the same order on each archive after one reader warm-up. Across-query RGB caching was disabled, and the OS file cache was uncontrolled. All 24 ROI pixel hashes matched.

The table combines two successful sequential runs under the same nominal resource budget, with one timing per arm. The direct comparator bypassed immediate per-tile decoding and performed full validation after import. The earlier successful fusion run kept the immediate check for coarser direct tiles, while its experimental conditional encoder deferred verification to the full readback. Two later fusion attempts hit a Python `TypeError` during catalog validation or assembly, and an additional direct attempt exited with a Windows access violation. Those interrupted attempts are excluded from the timing table. The cause remains unresolved, so this experiment establishes an observed trade-off rather than a statistically estimated speed ratio or a reliability rate.

The measured trade-off suggests two useful profiles. Direct e3 is the stronger candidate for interactive import and sparse reads when storage allows. Fast fusion remains useful when reducing archive bytes is more important. Repeated-window cache gains are evaluated separately below.

## Established observations

### Native encoding throughput on fixed tiles

On Windows x64, we selected every eighth eligible L0/L1 tile from `TENX139`, yielding 1,700 L0 and 430 L1 tiles. Each of the six trials used the same pixel and parent inputs, the same encoder and fixed physical-core leases. These timings cover native conditional encoding of prescanned tiles. SHA-256 checks matched all 2,130 output records against the prior full encode in every trial.

| Workers | Timing repetitions | Median native encode wall time |
|---|---:|---:|
| 8 | 2 | 39.33 s |
| 16 | 2 | 31.02 s |
| 24 | 2 | 22.84 s |

### Whole-slide Fast fusion development runs

`TENX139` has 9 stored levels and 18,184 tiles. Its original TIFF contains 2,253,230,598 bytes, and the stored native layers decode to 3,511,116,912 RGB8 bytes. The sequential 8- and 24-worker development measurements used the same source, storage volume and experimental conditional encoder, with full verification outside import timing.

| Measure | 8 workers | 24 workers |
|---|---:|---:|
| L0/L1 preparation and conditional encoding | 350.93 s | 196.30 s |
| Image fusion stage | 357.26 s | 201.27 s |
| Complete package publication | 636.95 s | 283.74 s |
| Separate full verification | 149.51 s | 143.21 s |
| Complete package | 922,157,728 B | 922,157,728 B |

The archive manifest and all member hashes matched. The 8-worker run had an atypical 181 s Count stage. Standalone repeats on its runtime took 5.43 s and 2.86 s, with matching Count members. The conditional-stage timings and fixed-tile trials provide the narrower comparison for worker scaling.

### Session-local image access

With the same reader open after the first request, repeated reads of a fixed ROI had median latency of 64.6 ms without the cache and 0.124 ms with the 8 MiB cache on `TENX139`. On `NCBI29` the corresponding measurements were 156.0 ms and 0.107 ms. The returned pixels matched. This test measures reuse of decoded tiles, and the independent-window benchmark measures access when that reuse is unavailable.

### Historical JPEG XL and WebP context

The historical `NCBI29` test covered 1,396 tiles over 7 native levels. Its independent lossless WebP image component was 32,277,258 B and took 76.26 s to generate and verify. JPEG XL effort 3 used 30,531,524 B and 28.22 s. JPEG XL effort 9 used 27,535,315 B and 378.27 s. A later 8-worker Windows Fast fusion run took 40.95 s including Count and internal verification and had a 24,393,976 B image component. Different scheduling and package scope limit those earlier timings to historical context.

## Reproduction scope

The comparison driver is [`benchmarks/compare_native_import.py`](../../benchmarks/compare_native_import.py). Use a qualified local Windows app runtime containing the code on this branch and a 091 encoder implementing `encode_recursive_fast`. Invoke each `--arm` in a fresh process and output directory. The driver uses direct e3 for independent tiles and the same post-encode verifier in both arms. Compare the input, native level, Count member and ROI hashes in the two receipts. The table above uses the earlier successful fusion run and the first successful e3 run under the stated conditions.

[Aggregate evidence](summary.json) records the values in the main comparison and the worker trials. The repository contains summaries and comparison code. Input data, generated archives, binaries and local paths stay outside it.
