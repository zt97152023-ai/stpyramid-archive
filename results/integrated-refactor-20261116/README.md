# Count and image archive refactor parity

The comparison replays the same input files against baseline commit `575d90351dcebc619742d4ec8c5a3aa75bd1d009` and the refactored implementation at `c254f78347083dda3f96fe047d9ea70e23bfee4c`. The runs used Windows x64, CPython 3.12.14, imagecodecs 2026.10.10, tifffile 2026.9.20 and h5py 3.16.0. The archive readers used the native runtimes identified by the archive manifests.

## Image archives

Each size is the physical sum of all five archive members. In each case the corresponding before and after bytes matched for `images.bank`, `images.index`, `anchors.index`, `source.json` and `manifest.json`. Cross-version reader checks passed for the native pixel hashes. The maximum-compression workflow also matched its 089 input files, global-to-local tile mappings and final candidate choices.

| Fixture | Fast fused (B) | Direct JPEG XL e9 in the fast workflow (B) | HF + L1 (B) | Direct JPEG XL e9 in the maximum-compression workflow (B) |
|---|---:|---:|---:|---:|
| Mixed | 246,509 | 266,495 | 246,175 | 266,495 |
| Constant | 21,200 | 21,205 | 21,216 | 21,205 |
| Odd edges | 749,524 | 723,017 | 723,028 | 723,017 |

[Fast and direct member SHA-256 records](fast-image-parity.json) · [Maximum-compression member, selection and input SHA-256 records](maximum-image-parity.json)

| Input TIFF | SHA-256 |
|---|---|
| Mixed | `60ec5a4e81af7b40eefaeca6066dd3e71cbbe0e84eb54ccd5b9a15dd88800a62` |
| Constant | `a75b801310a833bfed4958accd9629acc6236daf90f69af30bd6284a012d81e2` |
| Odd edges | `b6e1b19d03ea81665de16dd5d7b93d6cd3ed9c76a4dec5622eb94bfd2559449c` |

## Integrated Count and image archive

- The synthetic fixture produced a **56,788 B** integrated archive. All 11 members, including the top-level and image manifests, matched byte for byte. The refactored reader opened the baseline archive and the baseline reader opened the refactored archive. The selected Count array, image ROI and seven restored files matched the baseline outputs. [Integrated archive and restore member SHA-256 records](integrated-parity.json)
- The integrated smoke test checked all native RGB levels, typed H5AD, metadata, Count subsets, a joint Count and L1 image region, Count-only regional access and rejection of an altered payload. [Smoke result](integrated-smoke.json) · [Smoke test and fixture generator](../../tests/smoke.py)
- The additional Count replay covered Shared_local and Shared_blocks with noncontiguous original rows in the same spatial block, Shared_whole, and the integrated HDF5 compatibility path. The report records exact file hashes and the read checks. [Count parity](count-parity.json)

These are code compatibility results for the three fixed TIFFs and the synthetic Count fixtures. The sealed fast-fused release contains the separate ten-slide benchmark. [Ten-slide results](../fast-fused-v1/RESULTS.md)
